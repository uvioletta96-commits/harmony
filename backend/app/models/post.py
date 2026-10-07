"""Posts, media, reactions and threaded comments."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db
from .base import (
    PrimaryKeyMixin,
    PublicIdMixin,
    TimestampMixin,
    UTCDateTime,
    enum_column,
    iso,
)
from .user import User

#: Where the line between a short clip and a hosted video sits: three minutes.
#:
#: Defined here rather than read from config because it is part of what a media row
#: *means*, not a tuning knob - the same number decides what the vertical feed shows
#: and what the video library lists, and a model whose `to_dict` depends on app
#: config is a model that cannot be serialised outside a request.
#:
#: Three minutes is where the two forms genuinely differ rather than where a round
#: number sits. Under it, a clip is watched standing up and its author is the point.
#: Over it, the video is the point and the author is in the corner - which is a
#: different interface, not a longer version of the same one.
SHORT_CLIP_MAX_MS = 3 * 60 * 1000


class PostVisibility(str, Enum):
    PUBLIC = "public"
    FOLLOWERS = "followers"
    PRIVATE = "private"


class PostStatus(str, Enum):
    PUBLISHED = "published"
    HIDDEN = "hidden"  # soft-removed by its author
    UNDER_REVIEW = "under_review"  # held by automated moderation
    REMOVED = "removed"  # removed by a moderator
    ARCHIVED = "archived"  # scheduled for erasure by a retention job


class ReactionType(str, Enum):
    LIKE = "like"


class Post(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "posts"
    __table_args__ = (
        # Composite index that exactly matches the keyset feed query:
        # WHERE status='published' ORDER BY created_at DESC, id DESC
        Index("ix_posts_feed", "status", "created_at", "id"),
        Index("ix_posts_author_feed", "author_id", "status", "created_at", "id"),
        Index("ix_posts_visibility", "visibility", "status"),
    )

    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    visibility: Mapped[str] = enum_column(
        PostVisibility, "post_visibility", default=PostVisibility.PUBLIC, server_default=PostVisibility.PUBLIC.value
    )
    status: Mapped[str] = enum_column(
        PostStatus, "post_status", default=PostStatus.PUBLISHED, server_default=PostStatus.PUBLISHED.value
    )
    status_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Denormalised counters: read on every feed render, so they must not be
    # recomputed with COUNT() per row. Updated inside the same transaction.
    likes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    comments_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    views_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Moderation bookkeeping
    moderation_score: Mapped[float] = mapped_column(default=0.0, nullable=False, server_default="0")
    moderation_labels: Mapped[dict | None] = mapped_column(db.JSON, nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    author = relationship("User", back_populates="posts", lazy="joined")
    media = relationship(
        "PostMedia",
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PostMedia.position",
    )
    reactions = relationship("Reaction", back_populates="post", cascade="all, delete-orphan", passive_deletes=True)
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan", passive_deletes=True)

    @property
    def is_public(self) -> bool:
        return self.status == PostStatus.PUBLISHED.value and self.visibility == PostVisibility.PUBLIC.value

    @property
    def is_editable(self) -> bool:
        return self.status in (PostStatus.PUBLISHED.value, PostStatus.HIDDEN.value)

    def is_visible_to(self, viewer: User | None) -> bool:
        if self.author_id and viewer is not None and viewer.id == self.author_id:
            return self.status != PostStatus.REMOVED.value
        if self.status != PostStatus.PUBLISHED.value:
            return False
        if self.visibility == PostVisibility.PUBLIC.value:
            return True
        if viewer is None or not viewer.is_usable_account:
            return False
        if self.visibility == PostVisibility.PRIVATE.value:
            return False
        return self.author.is_followed_by(viewer) if self.author else False

    def to_dict(
        self,
        viewer: User | None = None,
        *,
        viewer_has_liked: bool | None = None,
        include_body: bool = True,
    ) -> dict[str, Any]:
        liked = viewer_has_liked
        if liked is None and viewer is not None:
            liked = any(r.user_id == viewer.id for r in self.reactions or [])
        data: dict[str, Any] = {
            "id": self.public_id,
            "author": self.author.to_public_dict(viewer) if self.author else None,
            "visibility": self.visibility,
            "status": self.status,
            "likes_count": self.likes_count,
            "comments_count": self.comments_count,
            "viewer_has_liked": bool(liked),
            # Sorted here rather than relying on the relationship's
            # ``order_by``: that applies when the collection is loaded from the
            # database, but attachments claimed onto a brand-new post are
            # appended in memory, and the reader arranged them deliberately in
            # the composer. One explicit sort makes the order the same on the
            # response that created the post and on every later read.
            "media": [m.to_dict() for m in sorted(self.media or [], key=lambda m: m.position)],
            # The stored total. Buffered views are added on top of this by
            # `get_video_feed`, which is the only place a live counter is read -
            # a denormalised column that nobody reconciles is a number that is
            # quietly wrong, and quietly wrong is worse than absent.
            "views_count": self.views_count,
            "created_at": iso(self.created_at),
            "edited_at": iso(self.edited_at),
            "is_owner": bool(viewer is not None and self.author_id == viewer.id),
        }
        if include_body:
            data["body"] = self.body
        if self.status != PostStatus.PUBLISHED.value and data.get("is_owner"):
            data["status_reason"] = self.status_reason
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Post {self.public_id} by {self.author_id} {self.status}>"


class PostMedia(PrimaryKeyMixin, TimestampMixin, db.Model):
    """An image uploaded to storage and (optionally) attached to a post.

    Bytes live on disk or in object storage; only metadata is in Postgres.
    ``post_id`` is NULL between upload and attachment — that window is why
    ``owner_id`` exists, so one account can never attach another's upload.
    """

    __tablename__ = "post_media"
    __table_args__ = (
        UniqueConstraint("post_id", "position", name="uq_post_media_position"),
        Index("ix_post_media_owner", "owner_id", "post_id"),
    )

    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/jpeg")
    #: How long a video runs, in milliseconds. Reported by the client, which knows
    #: it while the file is still in the browser.
    #:
    #: This is what separates a short clip from a hosted video, and it is stored
    #: rather than recomputed because the server cannot decode a container without
    #: ffmpeg - which is not on this machine. Zero means "not reported", and a post
    #: with a zero-length clip is treated as short, so a client that omits the field
    #: lands in the feed that always exists rather than in one keyed on a number
    #: nobody supplied.
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    alt_text: Mapped[str | None] = mapped_column(String(240), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    moderation_score: Mapped[float] = mapped_column(default=0.0, nullable=False, server_default="0")

    post = relationship("Post", back_populates="media")

    def to_dict(self) -> dict[str, Any]:
        is_video = self.mime_type.startswith("video/")
        return {
            "id": str(self.id),
            "url": self.url,
            "thumbnail_url": self.thumbnail_url or self.url,
            "width": self.width,
            "height": self.height,
            "byte_size": self.byte_size,
            "mime_type": self.mime_type,
            "duration_ms": self.duration_ms,
            # The client's own reading of the length, resolved against the configured
            # cut-off. A clip is something you watch standing up; a hosted video is
            # something you came for. Sent with the attachment so the card in the
            # video library can draw the badge without a second request.
            "is_short_clip": is_video and self.duration_ms <= SHORT_CLIP_MAX_MS,
            "alt_text": self.alt_text,
        }


class Reaction(PrimaryKeyMixin, TimestampMixin, db.Model):
    """A like. Unique per (user, post) so double-likes cannot inflate counters."""

    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", "type", name="uq_reaction_user_post_type"),
        Index("ix_reactions_post", "post_id", "created_at"),
        Index("ix_reactions_user", "user_id", "created_at"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = enum_column(
        ReactionType, "reaction_type", default=ReactionType.LIKE, server_default=ReactionType.LIKE.value
    )

    user = relationship("User")
    post = relationship("Post", back_populates="reactions")

    def to_dict(self, viewer: User | None = None) -> dict[str, Any]:
        return {
            "type": self.type,
            "user": self.user.to_public_dict(viewer) if self.user else None,
            "created_at": iso(self.created_at),
        }


class Comment(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "comments"
    __table_args__ = (
        # Thread ordering for a single post: root comments first, then replies.
        Index("ix_comments_post_created", "post_id", "created_at", "id"),
        Index("ix_comments_parent", "parent_id", "created_at"),
        Index("ix_comments_author", "author_id", "created_at"),
    )

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)
    root_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = enum_column(
        PostStatus, "comment_status", default=PostStatus.PUBLISHED, server_default=PostStatus.PUBLISHED.value
    )
    status_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    likes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    replies_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    moderation_score: Mapped[float] = mapped_column(default=0.0, nullable=False, server_default="0")
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    author = relationship("User", back_populates="comments", lazy="joined")
    post = relationship("Post", back_populates="comments")
    # ``parent_id`` and ``root_id`` are both self-referencing FKs, so each
    # relationship must name its column explicitly.
    parent = relationship(
        "Comment",
        remote_side="Comment.id",
        foreign_keys="Comment.parent_id",
        back_populates="replies",
    )
    replies = relationship(
        "Comment",
        remote_side="Comment.parent_id",
        foreign_keys="Comment.parent_id",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )

    @property
    def is_soft_deleted(self) -> bool:
        return self.status != PostStatus.PUBLISHED.value

    def to_dict(self, viewer: User | None = None, *, replies: list[Comment] | None = None) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "post_id": self.post.public_id if self.post else None,
            "parent_id": self.parent.public_id if self.parent else None,
            "depth": self.depth,
            "author": self.author.to_public_dict(viewer) if self.author else None,
            "body": self.body,
            "status": self.status,
            "likes_count": self.likes_count,
            "replies_count": self.replies_count,
            "created_at": iso(self.created_at),
            "edited_at": iso(self.edited_at),
            "is_owner": bool(viewer is not None and self.author_id == viewer.id),
            "replies": [r.to_dict(viewer) for r in (replies if replies is not None else self.replies or [])],
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Comment {self.public_id} post={self.post_id}>"


def published_posts_query():  # type: ignore[no-untyped-def]
    """Base select for any feed query. Kept here so filters stay consistent."""
    return select(Post).where(
        Post.status == PostStatus.PUBLISHED.value,
        Post.deleted_at.is_(None),
    )


__all__ = [
    "SHORT_CLIP_MAX_MS",
    "Comment",
    "Post",
    "PostMedia",
    "PostStatus",
    "PostVisibility",
    "Reaction",
    "ReactionType",
    "published_posts_query",
]

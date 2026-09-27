"""Posts, media attachment and the feed."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from flask import current_app
from sqlalchemy import Select, and_, func, or_, select, update

from ..extensions import db
from ..models.base import utcnow
from ..models.post import (
    Comment,
    Post,
    PostMedia,
    PostStatus,
    PostVisibility,
    Reaction,
    ReactionType,
)
from ..models.user import Relationship, User, UserStatus
from ..security import spam
from ..security.content_moderation import get_engine as moderation_engine
from ..security.xss import linkify, sanitize_plain_text
from ..utils.logging import get_logger, log_event
from ..utils.pagination import Page, clamp_page_size, keyset_page
from ..utils.responses import ConflictError, NotFoundError, PermissionError_, ValidationError

logger = get_logger("harmony.service.posts")

create_post_schema_fields = ("body", "visibility", "media_ids", "alt_texts")

FEED_MODES = ("for_you", "shuffled", "latest", "following", "saved")

#: The mode used when the client does not ask for one. Deliberately not
#: ``latest``: a chronological feed is the same page in the same order for
#: everyone, which is a fine archive and a poor home page.
DEFAULT_FEED_MODE = "for_you"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def visible_posts_query(viewer: User | None, *, mode: str = "latest") -> Select:
    """Base feed query honouring visibility, status and blocking.

    Every branch is index-friendly: the leading predicates match the composite
    indexes declared on ``posts``.
    """
    query = select(Post).where(
        Post.status == PostStatus.PUBLISHED.value,
        Post.deleted_at.is_(None),
    )

    if viewer is None:
        return query.where(Post.visibility == PostVisibility.PUBLIC.value)

    following_ids = select(Relationship.followee_id).where(
        Relationship.follower_id == viewer.id, Relationship.status == "following"
    )
    own_id = viewer.id

    visibility_clause = or_(
        Post.visibility == PostVisibility.PUBLIC.value,
        Post.author_id == own_id,
        and_(Post.visibility == PostVisibility.FOLLOWERS.value, Post.author_id.in_(following_ids)),
    )
    if mode == "following":
        query = query.where(Post.author_id.in_(following_ids))
    return query.where(visibility_clause)


def get_post(
    public_id: str,
    viewer: User | None = None,
    *,
    for_update: bool = False,
    include_hidden: bool = False,
) -> Post:
    """Fetch a post, applying visibility rules.

    ``include_hidden`` skips the visibility check and is for the owner's own
    delete/restore paths, where the post is invisible *by definition* and
    refusing to load it would make it impossible to undo.
    """
    post = db.session.scalar(select(Post).options(*_post_options()).where(Post.public_id == public_id))
    if post is None:
        raise NotFoundError("Пост не найден.", code="post_not_found")
    if for_update:
        db.session.refresh(post, with_for_update=True)
    if not include_hidden and not post.is_visible_to(viewer):
        # Report "not found" rather than "forbidden" so that a private post's
        # existence is not disclosed to users who cannot see it.
        raise NotFoundError("Пост не найден.", code="post_not_found")
    return post


def _post_options() -> list[Any]:  # type: ignore[no-untyped-def]
    from sqlalchemy.orm import selectinload

    return [selectinload(Post.media), selectinload(Post.reactions)]


def liked_post_ids(user: User | None, posts: Sequence[Post]) -> set[int]:
    if user is None or not posts:
        return set()
    internal_ids = [post.id for post in posts]
    rows = db.session.query(Reaction.post_id).filter(
        Reaction.user_id == user.id,
        Reaction.type == ReactionType.LIKE.value,
        Reaction.post_id.in_(internal_ids),
    )
    return {row[0] for row in rows}


def serialise_posts(posts: Sequence[Post], viewer: User | None) -> list[dict[str, Any]]:
    liked = liked_post_ids(viewer, posts)
    return [post.to_dict(viewer, viewer_has_liked=post.id in liked) for post in posts]


def get_feed(
    viewer: User | None,
    *,
    mode: str = DEFAULT_FEED_MODE,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page:
    """Return one page of the feed.

    ``for_you`` and ``shuffled`` are the orderings most readers want and are
    served from a pinned snapshot so a scroll is stable; ``latest`` and
    ``following`` stay keyset-paginated on ``created_at`` because a fixed
    order is exactly what those modes are for.
    """
    if mode not in FEED_MODES:
        raise ValidationError("Неизвестный режим ленты.", code="invalid_feed_mode")

    # ``limit`` arrives from the query string, so it is a string. The keyset path
    # clamps it inside ``keyset_page``; the shuffled path has to do it here, or
    # a value like "3" ends up as a slice bound and raises TypeError - a 500
    # for a malformed request, and no bounds checking at all for "0".
    page_size = clamp_page_size(limit, default=int(current_app.config.get("FEED_PAGE_SIZE", 15)))

    if mode in ("for_you", "shuffled"):
        from . import recommendation_service

        personalised = mode == "for_you" and recommendation_service.should_personalise(viewer)
        base = visible_posts_query(viewer).options(*_post_options())
        rows, next_cursor, has_more = recommendation_service.shuffled_page(
            base, viewer, cursor=cursor, limit=page_size, personalised=personalised
        )
        return Page(
            items=serialise_posts(rows, viewer),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    query = visible_posts_query(viewer, mode="following" if mode == "following" else "latest")
    page = keyset_page(
        query, model=Post, page_size=page_size, cursor=cursor, descending=True, order_fields=("created_at", "id")
    )
    return Page(
        items=serialise_posts(page.items, viewer),
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def get_user_posts(
    owner: User,
    viewer: User | None,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    include_hidden: bool = False,
) -> Page:
    page_size = limit or int(current_app.config.get("FEED_PAGE_SIZE", 15))
    query = select(Post).options(*_post_options()).where(Post.author_id == owner.id, Post.deleted_at.is_(None))

    if not include_hidden or (viewer is not None and viewer.id != owner.id and not viewer.is_moderator):
        query = query.where(Post.status == PostStatus.PUBLISHED.value)
    if viewer is None or viewer.id != owner.id:
        query = query.where(Post.visibility.in_([PostVisibility.PUBLIC.value, PostVisibility.FOLLOWERS.value]))
        if not owner.can_be_viewed_by(viewer):
            raise NotFoundError("Профиль не найден.", code="profile_not_found")

    page = keyset_page(
        query, model=Post, page_size=page_size, cursor=cursor, descending=True, order_fields=("created_at", "id")
    )
    return Page(items=serialise_posts(page.items, viewer), next_cursor=page.next_cursor, has_more=page.has_more)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def create_post(
    author: User,
    body: str,
    *,
    visibility: str = PostVisibility.PUBLIC.value,
    media: list[PostMedia] | None = None,
    alt_texts: dict[str, str] | None = None,
) -> Post:
    """Validate, moderate, persist a new post.

    Order matters: sanitise → spam score → moderate → persist. Storing
    unsanitised text even temporarily would put an XSS payload in the database
    where every other reader would have to defend against it.

    ``media`` is a list of *unclaimed* :class:`PostMedia` rows created by the
    upload endpoint. They are attached to this post here, so the bytes and their
    metadata are written once and referenced by an internal id — the client can
    never dictate a URL or a MIME type.
    """
    max_chars = int(current_app.config.get("POST_MAX_CHARS", 5000))
    max_media = int(current_app.config.get("MAX_POST_MEDIA", 6))

    clean_body = sanitize_plain_text(body or "", max_length=max_chars)
    if not clean_body and not media:
        raise ValidationError(
            "Пост не может быть пустым.",
            code="empty_post",
            fields={"body": "Введите текст или добавьте изображение."},
        )
    if len(media or []) > max_media:
        raise ValidationError(f"Можно прикрепить не более {max_media} изображений.", code="too_many_media")

    visibility = visibility if visibility in {item.value for item in PostVisibility} else PostVisibility.PUBLIC.value

    spam.guard_write(clean_body, author)

    decision = moderation_engine().screen(clean_body, context="post")
    if decision.blocked:
        spam.record_signal("content_blocked", scope="user", identifier=str(author.id), weight=0.5)
    status = PostStatus.UNDER_REVIEW.value if decision.needs_review else PostStatus.PUBLISHED.value

    post = Post(
        author_id=author.id,
        body=clean_body,
        visibility=visibility,
        status=status,
        moderation_score=decision.score,
        moderation_labels={"categories": decision.categories, "provider": decision.provider},
        moderated_at=utcnow() if status != PostStatus.PUBLISHED.value else None,
    )
    db.session.add(post)
    db.session.flush()

    for position, item in enumerate((media or [])[:max_media]):
        item.post_id = post.id
        item.position = position
        alt = (alt_texts or {}).get(item.storage_key)
        if alt:
            item.alt_text = sanitize_plain_text(alt, max_length=240)

    if status == PostStatus.PUBLISHED.value:
        author.posts_count = (author.posts_count or 0) + 1
    db.session.commit()

    _after_write(post, author, event="post.created")
    log_event(
        logger, "INFO", "post.created", user_id=author.id, post_id=post.public_id, status=status, visibility=visibility
    )
    return post


def update_post(post: Post, actor: User, payload: dict[str, Any]) -> Post:
    if post.author_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Вы можете редактировать только свои посты.", code="not_post_owner")
    if post.status in (PostStatus.REMOVED.value, PostStatus.ARCHIVED.value):
        raise ConflictError("Этот пост больше нельзя редактировать.", code="post_not_editable")

    max_chars = int(current_app.config.get("POST_MAX_CHARS", 5000))
    body = payload.get("body")
    if body is None:
        clean_body = post.body
    else:
        clean_body = sanitize_plain_text(body, max_length=max_chars)
        # Refused rather than accepted. `body or ""` turned a whitespace-only
        # edit into an empty post, so one stray request - or a client that
        # sends the trimmed field without checking it - destroyed the text with
        # no error and no way back. A post whose text is gone is not a state
        # the product should have.
        if not clean_body:
            raise ValidationError(
                "Публикация не может стать пустой.",
                code="empty_post",
                fields={"body": "Текст публикации не может быть пустым."},
            )

    decision = moderation_engine().screen(clean_body, context="post")
    decision.raise_for_decision(kind="post_update")

    post.body = clean_body
    if payload.get("visibility"):
        post.visibility = payload["visibility"]
    post.edited_at = utcnow()
    post.moderation_score = decision.score
    db.session.commit()

    _after_write(post, post.author, event="post.updated")
    log_event(logger, "INFO", "post.updated", user_id=actor.id, post_id=post.public_id)
    return post


def delete_post(post: Post, actor: User) -> None:
    """Soft delete.

    The row is retained with ``status = REMOVED`` so that comment threads,
    moderation history and third-party links resolve to a tombstone rather than
    a confusing 404 - and, crucially, so the action can be reversed by
    ``/restore``. Blanking the text here would make that promise a lie, so the
    body is kept: the post is invisible to everyone, and the content is erased
    for real only by :func:`user_service.purge_account` on account deletion or
    by a retention job.
    """
    if post.author_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Вы можете удалять только свои посты.", code="not_post_owner")

    was_published = post.status == PostStatus.PUBLISHED.value
    post.status = PostStatus.REMOVED.value
    post.deleted_at = utcnow()
    if was_published and post.author_id:
        post.author.posts_count = max(0, (post.author.posts_count or 1) - 1)
    db.session.commit()

    _after_write(post, post.author, event="post.deleted")
    log_event(logger, "INFO", "post.deleted", user_id=actor.id, post_id=post.public_id, moderator=actor.is_moderator)


def restore_post(post: Post, actor: User) -> Post:
    if post.author_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Недостаточно прав для восстановления.", code="not_post_owner")
    if post.status != PostStatus.REMOVED.value:
        raise ConflictError("Пост не был удалён.", code="post_not_removed")
    post.status = PostStatus.PUBLISHED.value
    post.deleted_at = None
    if post.author:
        post.author.posts_count = (post.author.posts_count or 0) + 1
    db.session.commit()
    _after_write(post, post.author, event="post.restored")
    return post


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def set_reaction(
    post: Post,
    user: User,
    *,
    liked: bool,
    reaction_type: ReactionType = ReactionType.LIKE,
) -> tuple[bool, int]:
    """Idempotently put the user in (or out of) the set of likers.

    Idempotent on purpose. A client that retries a like because the first
    response was lost must not end up *un*-liking the post, which is what a
    naive toggle gives you. The counter is recomputed from the rows inside the
    same transaction rather than incremented blindly, so neither a retry nor a
    concurrent like can drift the count away from reality.
    """
    existing = db.session.scalar(
        select(Reaction).where(
            Reaction.user_id == user.id,
            Reaction.post_id == post.id,
            Reaction.type == reaction_type.value,
        )
    )
    if liked and existing is None:
        db.session.add(Reaction(user_id=user.id, post_id=post.id, type=reaction_type.value))
    elif not liked and existing is not None:
        db.session.delete(existing)

    db.session.flush()
    actual = (
        db.session.scalar(
            select(func.count(Reaction.id)).where(Reaction.post_id == post.id, Reaction.type == reaction_type.value)
        )
        or 0
    )
    if post.likes_count != actual:
        db.session.execute(update(Post).where(Post.id == post.id).values(likes_count=actual))
        post.likes_count = actual
    db.session.commit()

    _after_write(post, post.author, event="post.reaction")
    return liked, post.likes_count


def toggle_reaction(post: Post, user: User, *, reaction_type: ReactionType = ReactionType.LIKE) -> tuple[bool, int]:
    """Flip the caller's like. Explicit opt-in, for a heart button."""
    existing = db.session.scalar(
        select(Reaction).where(
            Reaction.user_id == user.id,
            Reaction.post_id == post.id,
            Reaction.type == reaction_type.value,
        )
    )
    return set_reaction(post, user, liked=existing is None, reaction_type=reaction_type)


def list_reactions(post: Post, viewer: User | None, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = (
        db.session.query(Reaction)
        .join(User, User.id == Reaction.user_id)
        .filter(Reaction.post_id == post.id)
        .order_by(Reaction.created_at.desc())
        .limit(limit)
        .all()
    )
    return [row.to_dict(viewer) for row in rows]


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def claim_media(
    post: Post,
    actor: User,
    placeholders: list[PostMedia],
    *,
    alt_texts: dict[str, str] | None = None,
) -> list[PostMedia]:
    """Attach previously uploaded but unattached images to an existing post."""
    if post.author_id != actor.id and not actor.is_moderator:
        from ..utils.responses import PermissionError_

        raise PermissionError_("Вы можете изменять только свои посты.", code="not_post_owner")
    existing = len(post.media or [])
    if existing + len(placeholders) > int(current_app.config.get("MAX_POST_MEDIA", 6)):
        raise ValidationError("Превышен лимит вложений.", code="too_many_media")

    for offset, item in enumerate(placeholders):
        item.post_id = post.id
        item.position = existing + offset
        alt = (alt_texts or {}).get(item.storage_key)
        if alt:
            item.alt_text = sanitize_plain_text(alt, max_length=240)
    db.session.commit()
    _after_write(post, post.author, event="post.media")
    return placeholders


def unclaimed_media_for(user: User, storage_keys: list[str]) -> list[PostMedia]:
    """Fetch one unclaimed upload row per storage key, owned by ``user``.

    Ownership is part of the filter: without it, one user could attach
    another's upload to their own post.

    **One row per key, not one per match.** Storage keys are content-addressed -
    the same bytes uploaded twice produce the same key - so a user who uploads
    the same photo twice, or retries an upload that looked like it failed, ends
    up with two rows under one key. Returning both attaches the image twice: the
    reader sees a duplicate they never chose, and it consumes two of the
    attachment budget rather than one. The oldest row wins, since it is the one
    the cleanup task has had longest to reclaim if it is never claimed.

    The result follows the order the client asked for. The composer lets a
    reader arrange their attachments, and leaving the order to the database
    means two identical posts can present them differently.
    """
    if not storage_keys:
        return []

    rows = (
        db.session.query(PostMedia)
        .filter(
            PostMedia.storage_key.in_(set(storage_keys)),
            PostMedia.post_id.is_(None),
            PostMedia.owner_id == user.id,
        )
        .order_by(PostMedia.id.asc())
        .all()
    )

    oldest: dict[str, PostMedia] = {}
    for row in rows:
        # setdefault, not assignment: the first row for a key is the oldest.
        oldest.setdefault(row.storage_key, row)

    # A key the client repeated resolves to its single row rather than
    # consuming two slots of the attachment budget.
    return [oldest[key] for key in storage_keys if key in oldest]


def record_post_view(post: Post) -> None:
    """Count a view without a per-request UPDATE.

    Buffered in Redis and flushed by Celery: a view is a weak signal and does
    not justify a write amplification cost on the read path.
    """
    from .cache_service import rate_counter_add

    rate_counter_add(f"views:{post.public_id}", 1, ttl=86400)


def rich_body(post: Post) -> list[dict[str, Any]]:
    """Typed spans for the client, so rendering stays markup-free."""
    return linkify(post.body)


# ---------------------------------------------------------------------------
# Counters & cache
# ---------------------------------------------------------------------------


def refresh_post_counts(post_id: int) -> None:
    """Recompute denormalised counters. Used after bulk moderation actions."""
    post = db.session.get(Post, post_id)
    if post is None:
        return
    likes = db.session.scalar(select(func.count(Reaction.id)).where(Reaction.post_id == post_id)) or 0
    comments = (
        db.session.scalar(
            select(func.count(Comment.id)).where(
                Comment.post_id == post_id, Comment.status == PostStatus.PUBLISHED.value
            )
        )
        or 0
    )
    db.session.execute(update(Post).where(Post.id == post_id).values(likes_count=likes, comments_count=comments))
    db.session.commit()


def _after_write(post: Post, author: User | None, *, event: str) -> None:
    """Post-commit side effects: cache invalidation and async fan-out.

    Nothing here runs inside the request's critical section, and every task is
    dispatched with ``.delay`` so a slow notification worker can never add
    latency to a write.
    """
    from . import cache_service

    cache_service.invalidate_posts(post.public_id, author.id if author else None, author.public_id if author else None)
    if event == "post.created" and author is not None:
        _enqueue_follower_notifications(post.id)


def _enqueue_follower_notifications(post_id: int) -> None:
    """Queue the follower fan-out. Best-effort, and never blocking.

    See :func:`app.tasks.dispatch.enqueue` for why the availability check comes
    before the publish rather than being left to a ``try`` around it.
    """
    from ..tasks.dispatch import enqueue
    from .notification_service import notify_followers_of_post

    enqueue(notify_followers_of_post, post_id)


def public_counts() -> dict[str, int]:
    """Platform-wide totals for the landing page."""
    return {
        "posts": db.session.scalar(select(func.count(Post.id)).where(Post.status == PostStatus.PUBLISHED.value)) or 0,
        "users": db.session.scalar(select(func.count(User.id)).where(User.status == UserStatus.ACTIVE.value)) or 0,
    }


__all__ = [
    "FEED_MODES",
    "claim_media",
    "create_post",
    "delete_post",
    "get_feed",
    "get_post",
    "get_user_posts",
    "liked_post_ids",
    "list_reactions",
    "public_counts",
    "record_post_view",
    "refresh_post_counts",
    "restore_post",
    "rich_body",
    "serialise_posts",
    "set_reaction",
    "toggle_reaction",
    "update_post",
    "visible_posts_query",
]

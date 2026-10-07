"""Direct messaging: conversations, membership and messages."""

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
    func,
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


class ConversationKind(str, Enum):
    DIRECT = "direct"
    GROUP = "group"


class MessageStatus(str, Enum):
    QUEUED = "queued"  # accepted by the API, not yet persisted
    SENT = "sent"
    BLOCKED = "blocked"  # rejected by moderation before persistence
    DELETED = "deleted"


class Conversation(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_updated", "updated_at"),)

    kind: Mapped[str] = enum_column(ConversationKind, "conversation_kind", default=ConversationKind.DIRECT)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Stable key for 1:1 threads: sorted "lowId:highId". Guarantees at most one
    # direct conversation per user pair without a race-prone lookup-then-insert.
    direct_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(160), nullable=True)
    messages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    members = relationship(
        "ConversationMember", back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True
    )
    messages = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True
    )

    @staticmethod
    def build_direct_key(a: int, b: int) -> str:
        low, high = sorted((a, b))
        return f"{low}:{high}"

    @property
    def is_direct(self) -> bool:
        return self.kind == ConversationKind.DIRECT.value

    def member_user_ids(self) -> list[int]:
        return [m.user_id for m in self.members or []]

    def has_member(self, user: User) -> bool:
        return any(m.user_id == user.id and not m.left_at for m in self.members or [])

    def other_member(self, user: User) -> User | None:
        for member in self.members or []:
            if member.user_id != user.id and member.user is not None:
                return member.user
        return None

    def to_dict(self, viewer: User | None = None) -> dict[str, Any]:
        others = [m.user for m in self.members or [] if m.user and (viewer is None or m.user_id != viewer.id)]
        own_membership = next((m for m in self.members or [] if viewer is not None and m.user_id == viewer.id), None)
        data: dict[str, Any] = {
            "id": self.public_id,
            "kind": self.kind,
            "title": self.title,
            "participants": [u.to_public_dict(viewer) for u in others],
            "messages_count": self.messages_count,
            "last_message_at": iso(self.last_message_at),
            "last_message_preview": self.last_message_preview,
            "created_at": iso(self.created_at),
        }
        if own_membership is not None:
            data["unread_count"] = own_membership.unread_count
            data["last_read_at"] = iso(own_membership.last_read_at)
            data["is_muted"] = own_membership.is_muted
        if not self.is_direct and self.title is None and others:
            data["title"] = ", ".join(u.name for u in others)
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation {self.public_id} {self.kind}>"


class ConversationMember(PrimaryKeyMixin, TimestampMixin, db.Model):
    __tablename__ = "conversation_members"
    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_conversation_member"),
        Index("ix_conversation_members_user", "user_id", "last_read_at"),
    )

    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    is_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    conversation = relationship("Conversation", back_populates="members")
    user = relationship("User", lazy="joined")


class AttachmentKind(str, Enum):
    """What a message attachment is.

    ``VOICE`` and ``CIRCLE`` are the same bytes and the same player; the
    difference is purely how it is presented - a bar in the bubble against a round
    avatar. Keeping them apart means the client does not have to guess from
    geometry, and the wave data can be stored once for both.
    """

    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"
    CIRCLE = "circle"


class MessageAttachment(PrimaryKeyMixin, TimestampMixin, db.Model):
    """One file attached to a message.

    Separate from :class:`Message` rather than columns on it, because a message can
    carry several files and a photo sent alone must still have somewhere to live.

    Bytes are on disk; only metadata is here, and the URL is written by the upload
    endpoint - the client can never dictate where a file lives or what it is.
    """

    __tablename__ = "message_attachments"
    __table_args__ = (
        UniqueConstraint("message_id", "position", name="uq_message_attachment_position"),
        Index("ix_message_attachments_message", "message_id", "position"),
        Index("ix_message_attachments_owner", "owner_id"),
    )

    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    kind: Mapped[str] = enum_column(AttachmentKind, "attachment_kind", default=AttachmentKind.IMAGE)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Milliseconds. Recorded by the client from its own recording, because the
    #: server cannot decode the container to check it - and a voice message whose
    #: length is wrong cannot be rendered as a progress bar.
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Peaks for the waveform, 0-100, computed by the client while recording.
    #: Stored as a compact JSON array because the player draws it as bars and the
    #: client would otherwise have to decode the audio file to draw them.
    waveform: Mapped[list[int] | None] = mapped_column(db.JSON, nullable=True)
    alt_text: Mapped[str | None] = mapped_column(String(240), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    message = relationship("Message", back_populates="attachments")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "url": self.url,
            "thumbnail_url": self.thumbnail_url or self.url,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "width": self.width,
            "height": self.height,
            "duration_ms": self.duration_ms,
            "waveform": self.waveform or [],
            "alt_text": self.alt_text,
        }


class Message(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "messages"
    __table_args__ = (
        # Keyset pagination for "load older messages in this thread".
        Index("ix_messages_conversation_created", "conversation_id", "created_at", "id"),
        Index("ix_messages_sender", "sender_id", "created_at"),
    )

    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = enum_column(
        MessageStatus, "message_status", default=MessageStatus.SENT, server_default=MessageStatus.SENT.value
    )
    moderation_score: Mapped[float] = mapped_column(default=0.0, nullable=False, server_default="0")
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    conversation = relationship("Conversation", back_populates="messages")
    sender = relationship("User", lazy="joined")
    reply_to = relationship("Message", remote_side="Message.id")
    attachments = relationship(
        "MessageAttachment",
        back_populates="message",
        # Sorted explicitly rather than by the relationship's `order_by`, for the
        # same reason posts do it: attachments claimed onto a brand-new message are
        # appended in memory, and the sender arranged them deliberately.
        order_by="MessageAttachment.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self, viewer: User | None = None) -> dict[str, Any]:
        attachments = [a.to_dict() for a in sorted(self.attachments or [], key=lambda a: a.position)]

        if self.status == MessageStatus.DELETED.value:
            return {
                "id": self.public_id,
                "conversation_id": self.conversation.public_id if self.conversation else None,
                "body": "",
                # Attachments go with the text. A "deleted" message that still
                # serves its photos is not deleted.
                "attachments": [],
                "status": self.status,
                "created_at": iso(self.created_at),
                "is_deleted": True,
            }
        return {
            "id": self.public_id,
            "conversation_id": self.conversation.public_id if self.conversation else None,
            "sender": self.sender.to_public_dict(viewer) if self.sender else None,
            "reply_to_id": self.reply_to.public_id if self.reply_to else None,
            "body": self.body,
            "attachments": attachments,
            "status": self.status,
            "created_at": iso(self.created_at),
            "edited_at": iso(self.edited_at),
            "is_deleted": False,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self.public_id} conv={self.conversation_id}>"


def conversation_for_user(user: User, other: User) -> Conversation | None:
    key = Conversation.build_direct_key(user.id, other.id)
    return db.session.scalar(select(Conversation).where(Conversation.direct_key == key))


def recent_conversations_query(user_id: int):  # type: ignore[no-untyped-def]
    """Conversations for a user ordered by recency, excluding ones they left."""
    return (
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(
            ConversationMember.user_id == user_id,
            ConversationMember.left_at.is_(None),
        )
        .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc())
    )


def message_count_since(conversation_id: int, since: datetime | None) -> int:  # pragma: no cover
    query = db.session.query(func.count(Message.id)).filter(
        Message.conversation_id == conversation_id,
        Message.status == MessageStatus.SENT.value,
    )
    if since is not None:
        query = query.filter(Message.created_at > since)
    return query.scalar() or 0


__all__ = [
    "AttachmentKind",
    "Conversation",
    "ConversationKind",
    "ConversationMember",
    "Message",
    "MessageAttachment",
    "MessageStatus",
    "conversation_for_user",
    "message_count_since",
    "recent_conversations_query",
]

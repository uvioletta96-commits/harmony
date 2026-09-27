"""Cross-cutting system records: notifications, audit trail, GDPR artefacts, mail log."""

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


class NotificationKind(str, Enum):
    LIKE = "like"
    COMMENT = "comment"
    REPLY = "reply"
    FOLLOW = "follow"
    MESSAGE = "message"
    MODERATION = "moderation"
    WARNING = "warning"
    SYSTEM = "system"
    REMINDER = "reminder"


class Notification(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_inbox", "user_id", "read_at", "created_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = enum_column(NotificationKind, "notification_kind")
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    actor = relationship("User", foreign_keys=[actor_id], lazy="joined")

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "is_read": self.is_read,
            "actor": self.actor.to_public_dict() if self.actor else None,
            "created_at": iso(self.created_at),
        }


class AuditLog(PrimaryKeyMixin, TimestampMixin, db.Model):
    """Append-only audit trail.

    Never holds message bodies or raw content — only identifiers, actions and a
    redacted context blob. Stores the actor, the effective request id and the
    client IP *hash* so investigations work without retaining personal data.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor", "actor_id", "created_at"),
        # Named for the composite, not the column: ``action`` already carries a
        # single-column index from ``index=True`` and the two must not collide.
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_request", "request_id"),
    )

    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success", server_default="success")
    context: Mapped[dict | None] = mapped_column(db.JSON, nullable=True)

    actor = relationship("User", foreign_keys=[actor_id])

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "actor": self.actor_label or (self.actor.username if self.actor else "system"),
            "target_type": self.target_type,
            "target_id": self.target_id,
            "outcome": self.outcome,
            "request_id": self.request_id,
            "context": self.context or {},
            "created_at": iso(self.created_at),
        }


class LegalDocument(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Versioned legal text with the acceptance hash users actually agreed to."""

    __tablename__ = "legal_documents"
    __table_args__ = (UniqueConstraint("slug", "version", name="uq_legal_slug_version"),)

    slug: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="ru", server_default="ru")

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slug": self.slug,
            "version": self.version,
            "title": self.title,
            "locale": self.locale,
            "effective_from": iso(self.effective_from),
            "is_current": self.is_current,
            "content_hash": self.content_hash,
        }
        if include_content:
            data["content"] = self.content
        return data


class ConsentRecord(db.Model, PrimaryKeyMixin, TimestampMixin):
    """GDPR Art. 7(1) proof of consent: what, which version, when, from where."""

    __tablename__ = "consent_records"
    __table_args__ = (
        # ``user_id`` is already indexed by the column definition; this
        # composite is what "consents for this user, newest first" uses.
        Index("ix_consent_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    document_slug: Mapped[str] = mapped_column(String(48), nullable=False)
    document_version: Mapped[str] = mapped_column(String(16), nullable=False)
    document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(48), nullable=False, default="account")
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user = relationship("User")

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_slug": self.document_slug,
            "document_version": self.document_version,
            "document_hash": self.document_hash,
            "purpose": self.purpose,
            "granted": self.granted,
            "withdrawn_at": iso(self.withdrawn_at),
            "created_at": iso(self.created_at),
        }


class DataRequestKind(str, Enum):
    EXPORT = "export"  # Art. 15 / 20 — access & portability
    RECTIFY = "rectify"  # Art. 16
    ERASE = "erase"  # Art. 17 — right to be forgotten
    RESTRICT = "restrict"  # Art. 18
    OBJECT = "object"  # Art. 21 — objection to processing


class DataRequestStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DataRequest(db.Model, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """Data-subject request tracker (GDPR chapter III)."""

    __tablename__ = "data_requests"
    __table_args__ = (Index("ix_data_requests_user_status", "user_id", "status"),)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = enum_column(DataRequestKind, "data_request_kind")
    status: Mapped[str] = enum_column(
        DataRequestStatus,
        "data_request_status",
        default=DataRequestStatus.PENDING,
        server_default=DataRequestStatus.PENDING.value,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user = relationship("User")

    @property
    def is_open(self) -> bool:
        return self.status in (DataRequestStatus.PENDING.value, DataRequestStatus.PROCESSING.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "created_at": iso(self.created_at),
            "due_at": iso(self.due_at),
            "processed_at": iso(self.processed_at),
            "rejection_reason": self.rejection_reason,
            "download_url": self.payload_url if self.status == DataRequestStatus.COMPLETED.value else None,
            "is_open": self.is_open,
        }


class AccountDeletionRequest(db.Model, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    """Soft-deletion schedule. A grace period allows cancellation before the
    irreversible purge runs, which is both kinder and GDPR-compliant."""

    __tablename__ = "account_deletion_requests"
    __table_args__ = (Index("ix_account_deletion_status", "status", "scheduled_for"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled", server_default="scheduled")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reauth_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reauth_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user = relationship("User")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "status": self.status,
            "reason": self.reason,
            "scheduled_for": iso(self.scheduled_for),
            "cancelled_at": iso(self.cancelled_at),
            "completed_at": iso(self.completed_at),
        }


class EmailDelivery(PrimaryKeyMixin, TimestampMixin, db.Model):
    """Audit of transactional mail. Stores no message body — only metadata."""

    __tablename__ = "email_deliveries"
    __table_args__ = (Index("ix_email_deliveries_user", "user_id", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    to_address: Mapped[str] = mapped_column(String(255), nullable=False)
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="smtp", server_default="smtp")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "status": self.status,
            "provider": self.provider,
            "attempts": self.attempts,
            "error": self.error,
            "sent_at": iso(self.sent_at),
            "created_at": iso(self.created_at),
        }


__all__ = [
    "AccountDeletionRequest",
    "AuditLog",
    "ConsentRecord",
    "DataRequest",
    "DataRequestKind",
    "DataRequestStatus",
    "EmailDelivery",
    "LegalDocument",
    "Notification",
    "NotificationKind",
]

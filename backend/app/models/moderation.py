"""Moderation domain: user reports, moderator decisions, sanctions, abuse signals."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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


class ReportReason(str, Enum):
    SPAM = "spam"
    HARASSMENT = "harassment"
    HATE = "hate"
    VIOLENCE = "violence"
    SEXUAL = "sexual_content"
    SELF_HARM = "self_harm"
    MISINFORMATION = "misinformation"
    IMPERSONATION = "impersonation"
    COPYRIGHT = "copyright"
    PRIVACY = "privacy"
    OTHER = "other"


class ReportStatus(str, Enum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class TargetType(str, Enum):
    USER = "user"
    POST = "post"
    COMMENT = "comment"
    MESSAGE = "message"


class ActionType(str, Enum):
    NONE = "none"  # report dismissed, content left alone
    WARN = "warn"  # formal warning issued to the author
    HIDE = "hide"  # content hidden from the feed, author notified
    REMOVE = "remove"  # content removed, counter strike recorded
    RESTRICT = "restrict"  # account suspended for a period
    SUSPEND = "suspend"  # longer suspension
    BAN = "ban"  # permanent ban
    UNBAN = "unban"
    RESTORE = "restore"  # reinstate previously hidden/removed content


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Actions that take an account offline, mapped to the resulting user status.
STATUS_ACTIONS = {
    ActionType.RESTRICT.value: "suspended",
    ActionType.SUSPEND.value: "suspended",
    ActionType.BAN.value: "banned",
}


class Report(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_queue", "status", "priority", "created_at"),
        Index("ix_reports_target", "target_type", "target_id"),
        UniqueConstraint("reporter_id", "target_type", "target_id", name="uq_report_reporter_target"),
    )

    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = enum_column(TargetType, "report_target_type")
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str] = enum_column(ReportReason, "report_reason")
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = enum_column(
        Severity, "report_severity", default=Severity.LOW, server_default=Severity.LOW.value
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = enum_column(
        ReportStatus, "report_status", default=ReportStatus.OPEN, server_default=ReportStatus.OPEN.value
    )
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    auto_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    reporter = relationship("User", foreign_keys=[reporter_id], lazy="joined")
    target_author = relationship("User", foreign_keys=[target_author_id])
    assigned_to = relationship("User", foreign_keys=[assigned_to_id])

    @property
    def is_open(self) -> bool:
        return self.status in (ReportStatus.OPEN.value, ReportStatus.IN_REVIEW.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "target_type": self.target_type,
            "target_id": str(self.target_id),
            "reason": self.reason,
            "details": self.details,
            "severity": self.severity,
            "status": self.status,
            "duplicate_count": self.duplicate_count,
            "auto_flagged": self.auto_flagged,
            "reporter": self.reporter.to_public_dict() if self.reporter else None,
            "assigned_to": self.assigned_to.to_public_dict() if self.assigned_to else None,
            "resolution": self.resolution,
            "created_at": iso(self.created_at),
            "resolved_at": iso(self.resolved_at),
        }


class ModerationAction(PrimaryKeyMixin, PublicIdMixin, TimestampMixin, db.Model):
    """Immutable record of every enforcement step, for appeals and audits."""

    __tablename__ = "moderation_actions"
    __table_args__ = (
        Index("ix_moderation_actions_target", "target_type", "target_id", "created_at"),
        Index("ix_moderation_actions_subject", "subject_id", "created_at"),
    )

    action: Mapped[str] = enum_column(ActionType, "moderation_action_type")
    subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id", ondelete="SET NULL"), nullable=True)
    moderator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual", server_default="manual")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Named ``context`` rather than ``metadata``: ``metadata`` is the
    # declarative class attribute and a column with that name shadows it.
    context: Mapped[dict | None] = mapped_column(db.JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    note_to_user: Mapped[str | None] = mapped_column(Text, nullable=True)

    subject = relationship("User", foreign_keys=[subject_id])
    moderator = relationship("User", foreign_keys=[moderator_id])
    report = relationship("Report", foreign_keys=[report_id])

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.now(self.expires_at.tzinfo or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.public_id,
            "action": self.action,
            "subject": self.subject.to_public_dict() if self.subject else None,
            "target_type": self.target_type,
            "target_id": str(self.target_id) if self.target_id else None,
            "moderator": self.moderator.to_public_dict() if self.moderator else None,
            "source": self.source,
            "reason": self.reason,
            "note_to_user": self.note_to_user,
            "context": self.context or {},
            "is_active": self.is_active,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
        }


class AbuseEvent(PrimaryKeyMixin, TimestampMixin, db.Model):
    """Raw anti-automation signal: burst writes, scraping, throttle decisions.

    Consumed by :mod:`app.security.spam` to build a per-subject risk score.
    """

    __tablename__ = "abuse_events"
    __table_args__ = (
        Index("ix_abuse_events_subject", "subject_type", "subject_id", "created_at"),
        Index("ix_abuse_events_ip", "ip_hash", "created_at"),
    )

    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default="1")
    detail: Mapped[dict | None] = mapped_column(db.JSON, nullable=True)
    ip_count_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "detail": self.detail or {},
            "created_at": iso(self.created_at),
        }


class SpamSignal(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Per-subject rolling spam risk used to gate registration and posting."""

    __tablename__ = "spam_signals"
    __table_args__ = (Index("ix_spam_signals_scope", "scope", "identifier", "created_at"),)

    scope: Mapped[str] = mapped_column(String(24), nullable=False)  # ip | email_hash | user
    identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    signals: Mapped[dict | None] = mapped_column(db.JSON, nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    last_signal_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    @property
    def last_signal_age_seconds(self) -> float | None:
        if self.last_signal_at is None:
            return None
        value = self.last_signal_at
        now = datetime.now(value.tzinfo or None)
        return (now - value).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "identifier": self.identifier[:8] + "…",
            "score": self.score,
            "blocked": self.blocked,
            "signals": self.signals or {},
            "last_signal_at": iso(self.last_signal_at),
        }


def count_open_reports_for(target_type: str, target_id: int) -> int:  # pragma: no cover
    return (
        db.session.query(func.count(Report.id))
        .filter(
            Report.target_type == target_type,
            Report.target_id == target_id,
            Report.status.in_([ReportStatus.OPEN.value, ReportStatus.IN_REVIEW.value]),
        )
        .scalar()
        or 0
    )


__all__ = [
    "STATUS_ACTIONS",
    "AbuseEvent",
    "ActionType",
    "ModerationAction",
    "Report",
    "ReportReason",
    "ReportStatus",
    "Severity",
    "SpamSignal",
    "TargetType",
    "count_open_reports_for",
]

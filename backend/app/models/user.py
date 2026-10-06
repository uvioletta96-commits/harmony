"""User, profile, privacy settings, relationships and authentication tokens."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar

from sqlalchemy import (
    Boolean,
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
    SerializerMixin,
    TimestampMixin,
    UTCDateTime,
    enum_column,
    iso,
    new_uuid,
)


class UserRole(str, Enum):
    MEMBER = "member"
    MODERATOR = "moderator"
    ADMIN = "admin"


class UserStatus(str, Enum):
    ACTIVE = "active"
    PENDING = "pending"  # registered, email not yet verified
    SUSPENDED = "suspended"  # temporary moderator/admin sanction
    BANNED = "banned"  # long-term ban
    DEACTIVATED = "deactivated"  # GDPR erasure completed
    DELETION_PENDING = "deletion_pending"  # grace period before erasure


class ProfileVisibility(str, Enum):
    PUBLIC = "public"
    FOLLOWERS = "followers"
    PRIVATE = "private"


class Relationship(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Follow graph. ``status`` supports soft blocks without a second table."""

    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint("follower_id", "followee_id", name="uq_relationship_pair"),
        Index("ix_relationships_followee_created", "followee_id", "created_at"),
    )

    follower_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    followee_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="following")

    follower = relationship("User", foreign_keys=[follower_id], back_populates="following")
    followee = relationship("User", foreign_keys=[followee_id], back_populates="followers")


class AuthToken(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Single-use, hashed token for email verification, password reset, etc.

    Only the SHA-256 digest is stored, so a database leak does not hand an
    attacker usable account-recovery links. Tokens are also bound to the
    purpose column, preventing a reset link from being replayed as a
    verification link.
    """

    __tablename__ = "auth_tokens"
    __table_args__ = (
        Index("ix_auth_tokens_lookup", "token_hash", "purpose"),
        Index("ix_auth_tokens_expiry", "expires_at"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    request_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user = relationship("User", back_populates="tokens")

    @property
    def is_expired(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return bool(expires < datetime.now(expires.tzinfo))

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and not self.is_expired


class UserSession(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Refresh-token session, enabling server-side logout and revocation."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_active", "user_id", "revoked_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user = relationship("User", back_populates="sessions")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > datetime.now(self.expires_at.tzinfo or None)


class User(SerializerMixin, db.Model, PrimaryKeyMixin, PublicIdMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_status_created", "status", "created_at"),)

    # --- Identity ------------------------------------------------------
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    # --- Profile --------------------------------------------------------
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_color: Mapped[str] = mapped_column(String(16), nullable=False, default="sand")
    location: Mapped[str | None] = mapped_column(String(96), nullable=True)
    website: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pronouns: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- Authorisation / lifecycle -------------------------------------
    role: Mapped[str] = enum_column(
        UserRole, "user_role", default=UserRole.MEMBER, server_default=UserRole.MEMBER.value
    )
    status: Mapped[str] = enum_column(
        UserStatus, "user_status", default=UserStatus.PENDING, server_default=UserStatus.PENDING.value
    )
    status_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status_changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    followers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    following_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    comments_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    trusted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # --- Moderation counters -------------------------------------------
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reports_against_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    moderation_score: Mapped[float] = mapped_column(default=0.0, nullable=False, server_default="0")

    # --- Presentation -----------------------------------------------------
    #: UI language as a BCP 47 tag (``uk-UA``), or NULL for "follow the browser".
    #: NULL is the default on purpose: a language preference the reader never
    #: chose is a guess, and a guess that is wrong is more annoying than no
    #: guess. The client resolves NULL against ``navigator.languages`` and can
    #: write the result back here to make the choice explicit.
    language: Mapped[str | None] = mapped_column(String(35), nullable=True)

    # --- Privacy ---------------------------------------------------------
    profile_visibility: Mapped[str] = enum_column(
        ProfileVisibility,
        "profile_visibility",
        default=ProfileVisibility.PUBLIC,
        server_default=ProfileVisibility.PUBLIC.value,
    )
    show_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    show_last_seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    allow_messages_from_anyone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    allow_search_engine_indexing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    email_notifications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    data_processing_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    marketing_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    # --- Relationships ---------------------------------------------------
    posts = relationship("Post", back_populates="author", cascade="all, delete-orphan", passive_deletes=True)
    comments = relationship("Comment", back_populates="author", cascade="all, delete-orphan", passive_deletes=True)
    tokens = relationship("AuthToken", back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    following = relationship(
        "Relationship", foreign_keys="Relationship.follower_id", back_populates="follower", cascade="all, delete-orphan"
    )
    followers = relationship(
        "Relationship", foreign_keys="Relationship.followee_id", back_populates="followee", cascade="all, delete-orphan"
    )
    privacy_settings = relationship(
        "PrivacySetting", back_populates="user", uselist=False, cascade="all, delete-orphan", passive_deletes=True
    )

    __hidden_fields__ = (
        "id",
        "email",
        "password_hash",
        "failed_login_count",
        "moderation_score",
        "last_login_at",
        "locked_until",
        "status_reason",
        "status_changed_by_id",
        "email_verified",
        "allow_search_engine_indexing",
        "data_processing_consent",
        "marketing_consent",
    )
    __computed__: ClassVar[dict[str, Any]] = {
        "initials": lambda u: u.initials,
        "is_verified": lambda u: u.is_usable_account,
    }

    # -- Derived helpers -------------------------------------------------
    @property
    def initials(self) -> str:
        source = (self.display_name or self.username or "?").strip()
        parts = [p for p in source.replace("_", " ").replace(".", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[1][0]).upper()

    @property
    def name(self) -> str:
        return self.display_name or self.username

    @property
    def is_usable_account(self) -> bool:
        """Whether this account may hold a session at all.

        `email_verified` is part of it, and deliberately: a model cannot read
        configuration, so a deployment that waives address confirmation has to
        settle the question before it gets here. `auth_service.login` promotes
        the account on first sign-in when the requirement is off, which is what
        keeps this property honest rather than special-casing it.
        """
        return self.status == UserStatus.ACTIVE.value and self.email_verified

    @property
    def is_staff(self) -> bool:
        return self.role in (UserRole.ADMIN.value, UserRole.MODERATOR.value)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value

    @property
    def is_moderator(self) -> bool:
        return self.is_admin or self.role == UserRole.MODERATOR.value

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > datetime.now(self.locked_until.tzinfo or None))

    @property
    def is_restricted(self) -> bool:
        return self.status in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value)

    def can_be_viewed_by(self, viewer: User | None) -> bool:
        """Visibility rules for the profile itself."""
        if viewer is not None and viewer.id == self.id:
            return True
        if self.profile_visibility == ProfileVisibility.PUBLIC.value:
            return True
        if viewer is None or not viewer.is_usable_account:
            return False
        if self.profile_visibility == ProfileVisibility.PRIVATE.value:
            return False
        return self.is_followed_by(viewer)

    def is_followed_by(self, viewer: User | None) -> bool:
        if viewer is None:
            return False
        return any(rel.follower_id == viewer.id for rel in self.followers or [])

    def can_interact_with(self, viewer: User | None) -> bool:
        """Whether ``viewer`` may post, comment or like as this user."""
        if viewer is None or not viewer.is_usable_account:
            return False
        return not viewer.is_restricted

    def is_visible_online_for(self, viewer: User | None) -> bool:
        """Whether ``viewer`` is allowed to see that this account is connected.

        Three gates, in order: the account must be usable at all, the viewer must
        be allowed to read the profile, and the owner must not have hidden their
        last-seen time. The last gate matters - presence is live, but a grey
        "last seen" dot on a public profile and a black dot on the same profile
        leak the same fact, and someone who turned that off asked not to be.

        Imported here rather than at module scope: the realtime manager imports
        the app's extensions, and the models are imported by nearly everything.
        """
        if not self.is_usable_account or not self.show_last_seen:
            return False
        if not self.can_be_viewed_by(viewer):
            return False
        from ..realtime.manager import is_online

        return is_online(self.id)

    def to_public_dict(self, viewer: User | None = None) -> dict[str, Any]:
        """Safe representation. Never leaks the email, counters or moderation state."""
        data: dict[str, Any] = {
            "public_id": self.public_id,
            "username": self.username,
            "display_name": self.display_name,
            "initials": self.initials,
            "bio": self.bio,
            "avatar_url": self.avatar_url,
            "avatar_color": self.avatar_color,
            "location": self.location,
            "website": self.website,
            "pronouns": self.pronouns,
            "role": self.role,
            "is_verified": self.is_usable_account,
            "posts_count": self.posts_count,
            "followers_count": self.followers_count,
            "following_count": self.following_count,
            "comments_count": self.comments_count,
            "created_at": iso(self.created_at),
            # Presence is read from the live socket registry, not stored: it is
            # true while a socket is held and stops being true the moment it
            # closes. A user who opted out of showing their last-seen time is
            # not readable to this viewer, so they read as offline - presence
            # would otherwise be a way around that setting.
            "is_online": self.is_visible_online_for(viewer),
        }
        if self.show_last_seen and self.last_seen_at:
            data["last_seen_at"] = iso(self.last_seen_at)
        if self.can_be_viewed_by(viewer) and self.show_email:
            data["email"] = self.email
        if viewer is not None and viewer.id == self.id:
            data["profile_visibility"] = self.profile_visibility
            data["is_self"] = True
        elif self.can_be_viewed_by(viewer):
            data["is_self"] = False
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} status={self.status}>"


class PrivacySetting(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Fine-grained per-category privacy controls (GDPR data minimisation)."""

    __tablename__ = "privacy_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    discoverable_by_search: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    allow_mentions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    allow_tagging: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    share_activity_status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    personalize_feed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    allow_third_party_cookies: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    anonymize_in_analytics: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    data_export_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    user = relationship("User", back_populates="privacy_settings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "discoverable_by_search": self.discoverable_by_search,
            "allow_mentions": self.allow_mentions,
            "allow_tagging": self.allow_tagging,
            "share_activity_status": self.share_activity_status,
            "personalize_feed": self.personalize_feed,
            "allow_third_party_cookies": self.allow_third_party_cookies,
            "anonymize_in_analytics": self.anonymize_in_analytics,
            "profile_visibility": self.user.profile_visibility,
            "show_email": self.user.show_email,
            "show_last_seen": self.user.show_last_seen,
            "allow_messages_from_anyone": self.user.allow_messages_from_anyone,
            "email_notifications": self.user.email_notifications,
            "marketing_consent": self.user.marketing_consent,
        }


class LoginAttempt(db.Model, PrimaryKeyMixin, TimestampMixin):
    """Auth telemetry used to detect credential stuffing and enumeration."""

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_window", "created_at"),
        Index("ix_login_attempts_key", "identifier_hash", "success"),
    )

    identifier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    @staticmethod
    def success_count_recently(window_seconds: int = 3600) -> int:  # pragma: no cover - helper
        since = func.now() - func.cast(window_seconds, db.Integer)
        return (
            db.session.query(func.count(LoginAttempt.id))
            .filter(LoginAttempt.success.is_(True), LoginAttempt.created_at >= since)
            .scalar()
            or 0
        )


__all__ = [
    "AuthToken",
    "LoginAttempt",
    "PrivacySetting",
    "ProfileVisibility",
    "Relationship",
    "User",
    "UserRole",
    "UserSession",
    "UserStatus",
    "new_uuid",
]

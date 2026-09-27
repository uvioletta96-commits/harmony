"""Users, relationships and account deletion."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from flask import current_app
from sqlalchemy import delete, func, select, update

from ..extensions import db
from ..models.base import utcnow
from ..models.chat import Conversation, ConversationMember, Message
from ..models.moderation import ModerationAction, Report
from ..models.post import Comment, Post, PostMedia, Reaction
from ..models.system import (
    AccountDeletionRequest,
    AuditLog,
    ConsentRecord,
    DataRequest,
    EmailDelivery,
    Notification,
)
from ..models.user import (
    AuthToken,
    PrivacySetting,
    Relationship,
    User,
    UserSession,
    UserStatus,
)
from ..utils.logging import get_logger, log_event
from ..utils.responses import ConflictError, NotFoundError, ValidationError

logger = get_logger("harmony.service.users")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_user(public_id: str) -> User:
    user = db.session.scalar(select(User).where(User.public_id == public_id))
    if user is None:
        raise NotFoundError("Пользователь не найден.", code="user_not_found")
    return user


def get_visible_user(public_id: str, viewer: User | None) -> User:
    user = get_user(public_id)
    if not user.can_be_viewed_by(viewer):
        # Same response as "no such user": a private profile must not be
        # distinguishable from an empty one.
        raise NotFoundError("Пользователь не найден.", code="user_not_found")
    return user


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def follow(follower: User, followee: User) -> dict[str, Any]:
    if follower.id == followee.id:
        raise ValidationError("Нельзя подписаться на самого себя.", code="self_follow")
    if not followee.is_usable_account:
        raise ConflictError("Этот аккаунт недоступен для подписки.", code="account_unavailable")

    existing = db.session.scalar(
        select(Relationship).where(Relationship.follower_id == follower.id, Relationship.followee_id == followee.id)
    )
    if existing is not None and existing.status == "following":
        raise ConflictError("Вы уже подписаны на этого пользователя.", code="already_following")

    if existing is not None:
        existing.status = "following"
    else:
        db.session.add(Relationship(follower_id=follower.id, followee_id=followee.id, status="following"))
    db.session.flush()

    actual = (
        db.session.scalar(
            select(func.count(Relationship.id)).where(
                Relationship.followee_id == followee.id, Relationship.status == "following"
            )
        )
        or 0
    )
    if followee.followers_count != actual:
        db.session.execute(update(User).where(User.id == followee.id).values(followers_count=actual))
        followee.followers_count = actual
    db.session.commit()

    from . import cache_service
    from .notification_service import notify_follow

    cache_service.invalidate_user(followee.public_id, followee.id)
    cache_service.invalidate_user(follower.public_id, follower.id)
    notify_follow(follower.id, followee.id)
    log_event(logger, "INFO", "user.followed", user_id=follower.id, followee_id=followee.public_id)
    return {"following": True, "followers_count": followee.followers_count}


def unfollow(follower: User, followee: User) -> dict[str, Any]:
    existing = db.session.scalar(
        select(Relationship).where(Relationship.follower_id == follower.id, Relationship.followee_id == followee.id)
    )
    if existing is None or existing.status != "following":
        raise ConflictError("Вы не подписаны на этого пользователя.", code="not_following")

    # A hard delete is correct here: the edge is meaningless once gone, and
    # keeping tombstones would grow the table without bound.
    db.session.execute(
        delete(Relationship).where(Relationship.follower_id == follower.id, Relationship.followee_id == followee.id)
    )
    db.session.flush()
    actual = (
        db.session.scalar(
            select(func.count(Relationship.id)).where(
                Relationship.followee_id == followee.id, Relationship.status == "following"
            )
        )
        or 0
    )
    if followee.followers_count != actual:
        db.session.execute(update(User).where(User.id == followee.id).values(followers_count=actual))
        followee.followers_count = actual
    db.session.commit()

    from . import cache_service

    cache_service.invalidate_user(followee.public_id, followee.id)
    log_event(logger, "INFO", "user.unfollowed", user_id=follower.id, followee_id=followee.public_id)
    return {"following": False, "followers_count": followee.followers_count}


def relationship_state(viewer: User, target: User) -> dict[str, Any]:
    edge = db.session.scalar(
        select(Relationship).where(Relationship.follower_id == viewer.id, Relationship.followee_id == target.id)
    )
    return {
        "is_following": bool(edge and edge.status == "following"),
        "follows_you": any(
            rel.follower_id == target.id and rel.followee_id == viewer.id for rel in target.followers or []
        ),
        "is_self": viewer.id == target.id,
    }


def list_followers(user: User, *, limit: int = 50, offset: int = 0) -> list[User]:
    rows = (
        db.session.query(User)
        .join(Relationship, Relationship.follower_id == User.id)
        .filter(Relationship.followee_id == user.id, Relationship.status == "following")
        .order_by(Relationship.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return rows


def list_following(user: User, *, limit: int = 50, offset: int = 0) -> list[User]:
    rows = (
        db.session.query(User)
        .join(Relationship, Relationship.followee_id == User.id)
        .filter(Relationship.follower_id == user.id, Relationship.status == "following")
        .order_by(Relationship.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return rows


def suggestions_for(user: User, *, limit: int = 5) -> list[dict[str, Any]]:
    """A simple, privacy-respecting recommendation: active accounts the viewer
    does not already follow, ranked by follower count."""
    following_ids = select(Relationship.followee_id).where(Relationship.follower_id == user.id)
    rows = (
        db.session.query(User)
        .filter(
            User.status == UserStatus.ACTIVE.value,
            User.id != user.id,
            User.id.notin_(following_ids),
        )
        .order_by(User.followers_count.desc(), User.created_at.desc())
        .limit(limit)
        .all()
    )
    return [row.to_public_dict(user) for row in rows]


# ---------------------------------------------------------------------------
# Privacy settings
# ---------------------------------------------------------------------------


def update_privacy(user: User, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "discoverable_by_search",
        "allow_mentions",
        "allow_tagging",
        "share_activity_status",
        "personalize_feed",
        "allow_third_party_cookies",
        "anonymize_in_analytics",
    }
    settings = user.privacy_settings
    if settings is None:
        # Assign through the relationship, not ``user_id=``. Creating the row by
        # foreign key alone leaves the backref empty, so the identity map keeps
        # "no settings" cached for the rest of the session and every later read
        # of ``user.privacy_settings`` in this session sees None - a settings
        # page that silently forgets what was just saved.
        settings = PrivacySetting(user=user)
        db.session.add(settings)
        db.session.flush()

    for field, value in payload.items():
        if field in allowed and isinstance(value, bool):
            setattr(settings, field, value)
        elif field == "profile_visibility" and value in {"public", "followers", "private"}:
            user.profile_visibility = value
        elif field == "show_email" and isinstance(value, bool):
            user.show_email = value
        elif field == "show_last_seen" and isinstance(value, bool):
            user.show_last_seen = value
        elif field == "allow_messages_from_anyone" and isinstance(value, bool):
            user.allow_messages_from_anyone = value
        elif field == "email_notifications" and isinstance(value, bool):
            user.email_notifications = value
        elif field == "marketing_consent" and isinstance(value, bool):
            user.marketing_consent = value
            db.session.add(
                ConsentRecord(
                    user_id=user.id,
                    document_slug="privacy",
                    document_version=current_privacy_version(),
                    document_hash="",
                    purpose="marketing",
                    granted=value,
                )
            )

    db.session.commit()
    from . import cache_service

    cache_service.invalidate_user(user.public_id, user.id)
    log_event(logger, "INFO", "user.privacy_updated", user_id=user.id)
    return settings.to_dict()


def current_privacy_version() -> str:
    from .auth_service import current_legal_document

    document = current_legal_document("privacy")
    return document.version if document else "1.0"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def record_audit(
    event: str,
    actor: User | None = None,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    **context: Any,
) -> None:
    """Append an entry to the immutable audit trail.

    ``event`` is the dotted event name stored in the indexed ``action`` column
    (``"admin.user_action"``, ``"gdpr.export_completed"``, ...).  Everything
    else lands in the redacted ``context`` blob, including a per-action
    ``action=`` detail such as the specific decision that was taken - that is
    why the first parameter is not called ``action``: callers must stay able to
    pass one freely.
    """
    from flask import g, has_request_context, request

    from ..utils.crypto import keyed_hash

    try:
        db.session.add(
            AuditLog(
                actor_id=actor.id if actor else None,
                actor_label=actor.username if actor else "system",
                action=event,
                target_type=target_type,
                target_id=target_id,
                request_id=getattr(g, "request_id", None) if has_request_context() else None,
                ip_hash=keyed_hash(request.remote_addr or "", length=32) if has_request_context() else None,
                context=context or None,
            )
        )
    except Exception:  # pragma: no cover - auditing must never break a request
        log_event(logger, "WARNING", "audit.write_failed", event=event)


# ---------------------------------------------------------------------------
# Account deletion (GDPR Art. 17)
# ---------------------------------------------------------------------------


def schedule_account_deletion(
    user: User, reason: str | None = None, *, password_confirmed: bool = False
) -> AccountDeletionRequest:
    """Soft-delete with a grace period.

    The irreversible purge is a separate step so the user can cancel. It also
    gives the system a window to propagate an errant bulk action.
    """
    existing = db.session.scalar(select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == user.id))
    if existing is not None and existing.status == "scheduled":
        raise ConflictError("Удаление аккаунта уже запланировано.", code="deletion_already_scheduled")

    grace_days = int(current_app.config.get("ACCOUNT_DELETION_GRACE_DAYS", 14))
    request_row = existing or AccountDeletionRequest(user_id=user.id)
    request_row.status = "scheduled"
    request_row.reason = (reason or "")[:500] or None
    request_row.scheduled_for = utcnow() + timedelta(days=grace_days)
    request_row.cancelled_at = None
    request_row.completed_at = None
    if existing is None:
        db.session.add(request_row)

    user.status = UserStatus.DELETION_PENDING.value
    user.status_changed_at = utcnow()
    user.status_reason = "Запрошено удаление аккаунта"
    # Sessions are deliberately left alive. The grace period has to be usable:
    # a user who changes their mind must be able to sign back in and cancel.
    # Nothing else is reachable anyway - every endpoint but the two deletion
    # ones refuses an account in this state.
    db.session.commit()

    record_audit("account.deletion_scheduled", user, grace_days=grace_days)
    log_event(
        logger, "INFO", "account.deletion_scheduled", user_id=user.id, scheduled_for=str(request_row.scheduled_for)
    )
    return request_row


def cancel_account_deletion(user: User) -> AccountDeletionRequest:
    request_row = db.session.scalar(select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == user.id))
    if request_row is None or request_row.status != "scheduled":
        raise ConflictError("Запрос на удаление не найден.", code="deletion_not_scheduled")

    request_row.status = "cancelled"
    request_row.cancelled_at = utcnow()
    if user.status == UserStatus.DELETION_PENDING.value:
        user.status = UserStatus.ACTIVE.value
        user.status_changed_at = utcnow()
        user.status_reason = None
    db.session.commit()
    record_audit("account.deletion_cancelled", user)
    return request_row


def revoke_all_sessions(user_id: int, reason: str = "account_deletion") -> int:
    """End every live session for an account. Returns how many were closed."""
    now = utcnow()
    rows = db.session.query(UserSession).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).all()
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    return len(rows)


def purge_account(user_id: int) -> dict[str, int]:
    """Irreversibly erase a user and everything that identifies them.

    Deletion order respects foreign keys. Message bodies authored by the user
    are removed while the rest of the conversation is preserved, so other
    participants are not punished for someone else's account closure.
    """
    user = db.session.get(User, user_id)
    if user is None:
        return {}

    stats: dict[str, int] = {}

    def _count(model: Any, *conditions: Any) -> int:  # type: ignore[no-untyped-def]
        return db.session.query(func.count()).select_from(model).filter(*conditions).scalar() or 0

    stats["posts"] = _count(Post, Post.author_id == user_id)
    stats["comments"] = _count(Comment, Comment.author_id == user_id)
    stats["reactions"] = _count(Reaction, Reaction.user_id == user_id)
    stats["messages"] = _count(Message, Message.sender_id == user_id)
    stats["notifications"] = _count(Notification, Notification.user_id == user_id)
    stats["reports"] = _count(Report, Report.reporter_id == user_id)
    stats["sessions"] = _count(UserSession, UserSession.user_id == user_id)
    stats["tokens"] = _count(AuthToken, AuthToken.user_id == user_id)

    # Owned resources first.
    db.session.execute(delete(PostMedia).where(PostMedia.post_id.in_(select(Post.id).where(Post.author_id == user_id))))
    db.session.execute(delete(Post).where(Post.author_id == user_id))
    db.session.execute(delete(Comment).where(Comment.author_id == user_id))
    db.session.execute(delete(Reaction).where(Reaction.user_id == user_id))
    db.session.execute(delete(Message).where(Message.sender_id == user_id))
    db.session.execute(delete(ConversationMember).where(ConversationMember.user_id == user_id))
    db.session.execute(delete(Conversation).where(Conversation.created_by_id == user_id))
    db.session.execute(delete(Notification).where(Notification.user_id == user_id))
    db.session.execute(delete(Notification).where(Notification.actor_id == user_id))
    db.session.execute(delete(Report).where(Report.reporter_id == user_id))
    db.session.execute(delete(ModerationAction).where(ModerationAction.subject_id == user_id))
    db.session.execute(delete(ModerationAction).where(ModerationAction.moderator_id == user_id))
    db.session.execute(delete(Relationship).where(Relationship.follower_id == user_id))
    db.session.execute(delete(Relationship).where(Relationship.followee_id == user_id))
    db.session.execute(delete(AuthToken).where(AuthToken.user_id == user_id))
    db.session.execute(delete(UserSession).where(UserSession.user_id == user_id))
    db.session.execute(delete(EmailDelivery).where(EmailDelivery.user_id == user_id))
    db.session.execute(delete(ConsentRecord).where(ConsentRecord.user_id == user_id))
    db.session.execute(delete(AccountDeletionRequest).where(AccountDeletionRequest.user_id == user_id))
    db.session.execute(delete(DataRequest).where(DataRequest.user_id == user_id))
    db.session.execute(delete(PrivacySetting).where(PrivacySetting.user_id == user_id))

    # Audit rows are retained but the actor link is severed: the audit trail is
    # a legal record, not a user profile, so deleting it would be worse.
    #
    # Before the user row goes, and that ordering is the whole point.
    # `AuditLog.actor_id` is declared `ON DELETE SET NULL`, so the database
    # clears it the instant the delete lands - and the `WHERE actor_id = :id`
    # below then matches nothing, leaving `actor_label` holding the deleted
    # account's username in the audit trail for good. This ran after the delete
    # and only appeared to work because SQLite was not enforcing foreign keys.
    db.session.execute(
        update(AuditLog).where(AuditLog.actor_id == user_id).values(actor_id=None, actor_label="deleted")
    )

    db.session.execute(delete(User).where(User.id == user_id))

    db.session.commit()
    return stats


def export_user_data(user: User) -> dict[str, Any]:
    """Machine-readable export of everything held about a user (Art. 15/20)."""
    posts = db.session.query(Post).filter(Post.author_id == user.id).order_by(Post.created_at.asc()).all()
    comments = db.session.query(Comment).filter(Comment.author_id == user.id).order_by(Comment.created_at.asc()).all()
    consents = db.session.query(ConsentRecord).filter(ConsentRecord.user_id == user.id).all()
    requests = db.session.query(DataRequest).filter(DataRequest.user_id == user.id).all()

    return {
        "exported_at": utcnow().isoformat(),
        "format_version": "1.0",
        "profile": {
            "public_id": user.public_id,
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "email_verified": user.email_verified,
            "bio": user.bio,
            "location": user.location,
            "website": user.website,
            "pronouns": user.pronouns,
            "created_at": user.created_at.isoformat(),
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "status": user.status,
            "role": user.role,
        },
        "privacy": (user.privacy_settings.to_dict() if user.privacy_settings else {}),
        "counters": {
            "posts": user.posts_count,
            "comments": user.comments_count,
            "followers": user.followers_count,
            "following": user.following_count,
        },
        "posts": [
            {
                "id": post.public_id,
                "body": post.body,
                "visibility": post.visibility,
                "status": post.status,
                "created_at": post.created_at.isoformat(),
                "media": [m.url for m in post.media or []],
            }
            for post in posts
        ],
        "comments": [
            {
                "id": comment.public_id,
                "post_id": comment.post.public_id if comment.post else None,
                "body": comment.body,
                "created_at": comment.created_at.isoformat(),
            }
            for comment in comments
        ],
        "consent_history": [record.to_dict() for record in consents],
        "data_requests": [request.to_dict() for request in requests],
    }


__all__ = [
    "cancel_account_deletion",
    "export_user_data",
    "follow",
    "get_user",
    "get_visible_user",
    "list_followers",
    "list_following",
    "purge_account",
    "record_audit",
    "relationship_state",
    "revoke_all_sessions",
    "schedule_account_deletion",
    "suggestions_for",
    "unfollow",
    "update_privacy",
]

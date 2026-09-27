"""Moderation operations: reporting, the review queue, and enforcement.

Escalation policy — a user reaching the report threshold for a post is
automatically hidden pending human review. This is deliberate: a false positive
costs the author a few minutes of delay, whereas a false *negative* on content
that is genuinely harmful is not recoverable.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from flask import current_app
from sqlalchemy import func, select, update

from ..extensions import db
from ..models.base import utcnow
from ..models.chat import Message, MessageStatus
from ..models.moderation import (
    STATUS_ACTIONS,
    ActionType,
    ModerationAction,
    Report,
    ReportReason,
    ReportStatus,
    Severity,
    TargetType,
)
from ..models.post import Comment, Post, PostStatus
from ..models.system import Notification, NotificationKind
from ..models.user import User, UserSession, UserStatus
from ..utils.logging import get_logger, log_event
from ..utils.pagination import Page, offset_page
from ..utils.responses import ConflictError, NotFoundError, ValidationError

logger = get_logger("harmony.service.moderation")

#: Distinct *people* who must report an item before it is auto-hidden. Two is the
#: floor and it is deliberately low: a wrong auto-hide costs the author a delay
#: they can appeal, whereas a missed one costs everyone who saw it. The count is
#: of reporters, never of reports, so one determined person filing repeatedly can
#: never hide content alone.
MIN_DISTINCT_REPORTERS_FOR_AUTO_HIDE = 2

#: Configurable name for the same knob. Operators may raise it to make hiding
#: more conservative; :func:`_maybe_auto_hide` clamps it to the floor above.
AUTO_HIDE_REPORT_THRESHOLD = MIN_DISTINCT_REPORTERS_FOR_AUTO_HIDE

WARNING_LIMIT_BEFORE_SUSPENSION = 3


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

REPORT_REASON_WEIGHT: dict[str, int] = {
    ReportReason.SPAM.value: 1,
    ReportReason.HARASSMENT.value: 3,
    ReportReason.HATE.value: 4,
    ReportReason.VIOLENCE.value: 5,
    ReportReason.SEXUAL.value: 3,
    ReportReason.SELF_HARM.value: 5,
    ReportReason.MISINFORMATION.value: 1,
    ReportReason.IMPERSONATION.value: 2,
    ReportReason.COPYRIGHT.value: 2,
    ReportReason.PRIVACY.value: 3,
    ReportReason.OTHER.value: 1,
}

SEVERITY_BY_SCORE = {
    "1": Severity.LOW.value,
    "3": Severity.MEDIUM.value,
    "4": Severity.HIGH.value,
    "5": Severity.CRITICAL.value,
}


def create_report(
    reporter: User,
    target_type: str,
    target_public_id: str,
    *,
    reason: str,
    details: str | None = None,
) -> Report:
    """File a report. The unique constraint makes repeat reports a no-op upsert."""
    target, target_author, target_id = _resolve_target(target_type, target_public_id)
    if target_author is not None and target_author.id == reporter.id:
        raise ValidationError("Нельзя пожаловаться на собственный материал.", code="self_report")
    if reason not in REPORT_REASON_WEIGHT:
        raise ValidationError("Укажите причину жалобы.", code="invalid_reason")

    from ..security.xss import sanitize_plain_text

    report = db.session.scalar(
        select(Report).where(
            Report.reporter_id == reporter.id,
            Report.target_type == target_type,
            Report.target_id == target_id,
        )
    )
    if report is not None:
        raise ConflictError("Вы уже отправляли жалобу на этот объект.", code="already_reported")

    priority = REPORT_REASON_WEIGHT.get(reason, 1)
    report = Report(
        reporter_id=reporter.id,
        target_type=target_type,
        target_id=target_id,
        target_author_id=target_author.id if target_author else None,
        reason=reason,
        details=sanitize_plain_text(details or "", max_length=1000) or None,
        severity=SEVERITY_BY_SCORE.get(str(priority), Severity.LOW.value),
        priority=priority,
        auto_flagged=False,
    )
    db.session.add(report)
    db.session.flush()
    _bump_duplicate_count(target_type, target_id)
    _maybe_auto_hide(target_type, target, report)

    if target_author is not None:
        target_author.reports_against_count = (target_author.reports_against_count or 0) + 1
    db.session.commit()

    from . import user_service

    user_service.record_audit(
        "moderation.report_created",
        reporter,
        target_type=target_type,
        target_id=target_public_id,
        reason=reason,
    )
    log_event(
        logger, "INFO", "moderation.report_created", target_type=target_type, reason=reason, reporter_id=reporter.id
    )
    return report


def _resolve_target(target_type: str, public_id: str) -> tuple[Any, User | None, int]:
    if target_type == TargetType.POST.value:
        post = db.session.scalar(select(Post).where(Post.public_id == public_id))
        if post is None:
            raise NotFoundError("Пост не найден.", code="post_not_found")
        return post, post.author, post.id
    if target_type == TargetType.COMMENT.value:
        comment = db.session.scalar(select(Comment).where(Comment.public_id == public_id))
        if comment is None:
            raise NotFoundError("Комментарий не найден.", code="comment_not_found")
        return comment, comment.author, comment.id
    if target_type == TargetType.MESSAGE.value:
        message = db.session.scalar(select(Message).where(Message.public_id == public_id))
        if message is None:
            raise NotFoundError("Сообщение не найден.", code="message_not_found")
        return message, message.sender, message.id
    if target_type == TargetType.USER.value:
        user = db.session.scalar(select(User).where(User.public_id == public_id))
        if user is None:
            raise NotFoundError("Пользователь не найден.", code="user_not_found")
        return user, user, user.id
    raise ValidationError("Неизвестный тип объекта жалобы.", code="invalid_target_type")


def _bump_duplicate_count(target_type: str, target_id: int) -> None:
    count = (
        db.session.scalar(
            select(func.count(Report.id)).where(Report.target_type == target_type, Report.target_id == target_id)
        )
        or 0
    )
    db.session.execute(
        update(Report)
        .where(Report.target_type == target_type, Report.target_id == target_id)
        .values(duplicate_count=count)
    )


def _maybe_auto_hide(target_type: str, target: Any, report: Report) -> None:
    """Hide content once enough *distinct* people have reported it.

    The count is of distinct reporters, not of reports. A raw count would let a
    single person filing the same complaint repeatedly hide content on their own -
    the most likely false positive, and the most expensive one, because the
    author's thread disappears until a moderator looks at it.
    """
    if target is None or target_type not in (TargetType.POST.value, TargetType.COMMENT.value):
        return
    if getattr(target, "status", None) != PostStatus.PUBLISHED.value:
        return

    def _count(column: Any) -> int:
        return int(
            db.session.scalar(select(column).where(Report.target_type == target_type, Report.target_id == target.id))
            or 0
        )

    distinct_reporters = _count(func.count(func.distinct(Report.reporter_id)))
    total = _count(func.count(Report.id))

    threshold = int(current_app.config.get("AUTO_HIDE_REPORT_THRESHOLD", AUTO_HIDE_REPORT_THRESHOLD))
    # The distinct-reporter floor is not negotiable by configuration: an operator
    # may make hiding easier to trigger, never easier for a single reporter.
    threshold = max(threshold, MIN_DISTINCT_REPORTERS_FOR_AUTO_HIDE)
    if distinct_reporters < threshold:
        return

    target_id = target.id
    target.status = PostStatus.UNDER_REVIEW.value
    target.status_reason = f"\u0421\u043a\u0440\u044b\u0442\u043e \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438: \u0436\u0430\u043b\u043e\u0431 ({total})"
    report.auto_flagged = True
    db.session.add(
        ModerationAction(
            action=ActionType.HIDE.value,
            target_type=target_type,
            target_id=target_id,
            source="automatic",
            reason=f"\u0410\u0432\u0442\u043e\u0436\u0430\u043b\u043e\u0431\u0430: {distinct_reporters} \u0447\u0435\u043b\u043e\u0432\u0435\u043a, {total} \u0436\u0430\u043b\u043e\u0431",
            note_to_user="\u042d\u0442\u043e \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0441\u043a\u0440\u044b\u0442\u043e \u0438 \u043e\u0436\u0438\u0434\u0430\u0435\u0442 \u0440\u0435\u0448\u0435\u043d\u0438\u044f \u043c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440\u0430.",
        )
    )
    author = getattr(target, "author", None)
    if author is not None:
        _notify(
            author,
            NotificationKind.MODERATION,
            "\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0441\u043a\u0440\u044b\u0442\u043e \u0434\u043e \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438",
            target,
        )
    log_event(
        logger,
        "WARNING",
        "moderation.auto_hidden",
        target_type=target_type,
        target_id=target_id,
        reports=total,
        distinct_reporters=distinct_reporters,
    )


def _public_id_of(target_type: str, target_id: int) -> str:
    model = Post if target_type == TargetType.POST.value else Comment
    row = db.session.scalar(select(model.public_id).where(model.id == target_id))
    return row or ""


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


def review_queue(
    moderator: User,
    *,
    status: str | None = ReportStatus.OPEN.value,
    target_type: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> Page:
    query = select(Report)
    if status:
        query = query.where(Report.status == status)
    if target_type:
        query = query.where(Report.target_type == target_type)
    query = query.order_by(Report.priority.desc(), Report.created_at.asc())
    result = offset_page(query, page=page, page_size=per_page, serialize=lambda r: _report_with_context(r, moderator))
    return result


def _report_with_context(report: Report, moderator: User) -> dict[str, Any]:
    """Attach the reported content so a moderator never has to make a second call."""
    data = report.to_dict()
    data["content"] = _content_snapshot(report)
    data["author_history"] = _author_history(report.target_author_id)
    return data


def _content_snapshot(report: Report) -> dict[str, Any] | None:
    try:
        target, _author, _ = _resolve_target(report.target_type, _public_id_of(report.target_type, report.target_id))
    except (NotFoundError, ValidationError):
        return None
    if isinstance(target, Message):
        return {"type": "message", "body": target.body, "created_at": target.created_at.isoformat()}
    return {
        "type": report.target_type,
        "body": getattr(target, "body", ""),
        "media": [m.url for m in getattr(target, "media", []) or []],
        "status": getattr(target, "status", None),
        "created_at": getattr(target, "created_at", None).isoformat() if getattr(target, "created_at", None) else None,
        "public_id": getattr(target, "public_id", None),
    }


def _author_history(user_id: int | None) -> dict[str, Any] | None:
    if user_id is None:
        return None
    user = db.session.get(User, user_id)
    if user is None:
        return None
    return {
        "username": user.username,
        "status": user.status,
        "warnings": user.warning_count,
        "reports_against": user.reports_against_count,
        "created_at": user.created_at.isoformat(),
        "prior_actions": db.session.scalar(
            select(func.count(ModerationAction.id)).filter(ModerationAction.subject_id == user_id)
        )
        or 0,
    }


def assign_report(report: Report, moderator: User) -> Report:
    report.assigned_to_id = moderator.id
    if report.status == ReportStatus.OPEN.value:
        report.status = ReportStatus.IN_REVIEW.value
    db.session.commit()
    return report


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def resolve_report(
    report: Report,
    moderator: User,
    *,
    action: str = ActionType.NONE.value,
    note: str = "",
    duration_hours: int | None = None,
) -> ModerationAction:
    """Apply an enforcement decision and close the report."""
    if action not in {item.value for item in ActionType}:
        raise ValidationError("Неизвестное действие модерации.", code="invalid_action")
    if report.resolved_at is not None:
        raise ConflictError("Жалоба уже рассмотрена.", code="report_already_resolved")

    target, author, _ = _resolve_target(report.target_type, _public_id_of(report.target_type, report.target_id))
    expires_at = utcnow() + timedelta(hours=duration_hours) if duration_hours else None

    moderation_action = ModerationAction(
        action=action,
        subject_id=author.id if author else None,
        target_type=report.target_type,
        target_id=report.target_id,
        report_id=report.id,
        moderator_id=moderator.id,
        source="manual",
        reason=note or f"Жалоба {report.public_id}",
        note_to_user=note or None,
        expires_at=expires_at,
    )
    db.session.add(moderation_action)

    _apply_action(action, target, author, moderator, note, expires_at)

    report.status = ReportStatus.RESOLVED.value if action != ActionType.NONE.value else ReportStatus.DISMISSED.value
    report.resolution = note
    report.resolved_by_id = moderator.id
    report.resolved_at = utcnow()
    db.session.commit()

    from . import user_service

    user_service.record_audit(
        "moderation.action",
        moderator,
        action=action,
        target_type=report.target_type,
        target_id=_public_id_of(report.target_type, report.target_id),
        subject_id=author.public_id if author else None,
    )
    log_event(
        logger,
        "WARNING",
        "moderation.resolved",
        action=action,
        target_type=report.target_type,
        moderator_id=moderator.id,
    )
    return moderation_action


def _apply_action(
    action: str,
    target: Any,
    author: User | None,
    moderator: User,
    note: str,
    expires_at: Any = None,
) -> None:
    if action == ActionType.NONE.value:
        _restore(target)
        return
    if action == ActionType.WARN.value:
        if author is not None:
            author.warning_count = (author.warning_count or 0) + 1
            _notify(author, NotificationKind.WARNING, "Предупреждение за нарушение правил", target, note)
            if author.warning_count >= WARNING_LIMIT_BEFORE_SUSPENSION:
                _set_user_status(author, UserStatus.SUSPENDED.value, note, moderator, expires_at)
        return
    if action == ActionType.HIDE.value:
        _hide(target, "Скрыто модератором")
        if author is not None:
            _notify(author, NotificationKind.MODERATION, "Ваша публикация скрыта", target, note)
        return
    if action == ActionType.REMOVE.value:
        _remove(target, "Удалено модератором")
        if author is not None:
            _notify(author, NotificationKind.MODERATION, "Ваша публикация удалена", target, note)
        return
    if action == ActionType.RESTORE.value:
        _restore(target)
        return
    if action in STATUS_ACTIONS and author is not None:
        new_status = STATUS_ACTIONS[action]
        _set_user_status(author, new_status, note or f"Действие: {action}", moderator, expires_at)
        _revoke_sessions(author.id, reason=action)
        return
    if action == ActionType.UNBAN.value and author is not None:
        author.status = UserStatus.ACTIVE.value
        author.status_changed_at = utcnow()
        author.status_reason = None
        _notify(author, NotificationKind.MODERATION, "Блокировка снята", None, "")


def _hide(target: Any, reason: str) -> None:
    if target is not None and hasattr(target, "status"):
        target.status = PostStatus.UNDER_REVIEW.value
        target.status_reason = reason


def _remove(target: Any, reason: str) -> None:
    if target is None:
        return
    if isinstance(target, Post):
        was_published = target.status == PostStatus.PUBLISHED.value
        target.status = PostStatus.REMOVED.value
        target.status_reason = reason
        target.deleted_at = utcnow()
        target.body = ""
        if was_published and target.author is not None:
            target.author.posts_count = max(0, (target.author.posts_count or 1) - 1)
    elif isinstance(target, Comment):
        target.status = PostStatus.REMOVED.value
        target.status_reason = reason
        target.deleted_at = utcnow()
        target.body = ""
        if target.post is not None:
            target.post.comments_count = max(0, (target.post.comments_count or 1) - 1)
    elif isinstance(target, Message):
        target.status = MessageStatus.DELETED.value
        target.body = ""
        target.deleted_at = utcnow()


def _restore(target: Any) -> None:
    if target is None or not hasattr(target, "status"):
        return
    if target.status in (PostStatus.UNDER_REVIEW.value, PostStatus.REMOVED.value):
        target.status = PostStatus.PUBLISHED.value
        target.status_reason = None
        target.deleted_at = None
        if isinstance(target, Post) and target.author is not None:
            target.author.posts_count = (target.author.posts_count or 0) + 1
        if isinstance(target, Comment) and target.post is not None:
            target.post.comments_count = (target.post.comments_count or 0) + 1


def _set_user_status(user: User, status: str, reason: str, moderator: User, expires_at: Any) -> None:
    user.status = status
    user.status_reason = reason[:256]
    user.status_changed_at = utcnow()
    user.status_changed_by_id = moderator.id
    _notify(user, NotificationKind.MODERATION, _status_headline(status), None, reason)


def _status_headline(status: str) -> str:
    return {
        UserStatus.SUSPENDED.value: "Аккаунт приостановлен",
        UserStatus.BANNED.value: "Аккаунт заблокирован",
    }.get(status, "Изменение статуса аккаунта")


def _revoke_sessions(user_id: int, *, reason: str) -> int:
    now = utcnow()
    rows = db.session.query(UserSession).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).all()
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    return len(rows)


def _notify(user: User, kind: NotificationKind, title: str, target: Any = None, body: str = "") -> None:
    db.session.add(
        Notification(
            user_id=user.id,
            kind=kind if isinstance(kind, str) else kind.value,
            title=title,
            body=body[:1000],
            resource_type="post" if isinstance(target, Post) else None,
            resource_id=getattr(target, "public_id", None),
        )
    )


# ---------------------------------------------------------------------------
# Audit trail & stats
# ---------------------------------------------------------------------------


def action_history(*, subject_id: int | None = None, page: int = 1, per_page: int = 25) -> Page:
    query = select(ModerationAction)
    if subject_id:
        query = query.where(ModerationAction.subject_id == subject_id)
    query = query.order_by(ModerationAction.created_at.desc())
    return offset_page(query, page=page, page_size=per_page, serialize=lambda a: a.to_dict())


def dashboard_stats() -> dict[str, Any]:
    open_reports = (
        db.session.scalar(
            select(func.count(Report.id)).filter(
                Report.status.in_([ReportStatus.OPEN.value, ReportStatus.IN_REVIEW.value])
            )
        )
        or 0
    )
    by_reason = {
        row[0]: row[1]
        for row in db.session.execute(
            select(Report.reason, func.count(Report.id))
            .filter(Report.status.in_([ReportStatus.OPEN.value, ReportStatus.IN_REVIEW.value]))
            .group_by(Report.reason)
        ).all()
    }
    under_review = (
        db.session.scalar(select(func.count(Post.id)).filter(Post.status == PostStatus.UNDER_REVIEW.value)) or 0
    )
    active_users = db.session.scalar(select(func.count(User.id)).filter(User.status == UserStatus.ACTIVE.value)) or 0
    sanctioned = (
        db.session.scalar(
            select(func.count(User.id)).filter(User.status.in_([UserStatus.SUSPENDED.value, UserStatus.BANNED.value]))
        )
        or 0
    )
    return {
        "open_reports": open_reports,
        "reports_by_reason": by_reason,
        "posts_under_review": under_review,
        "active_users": active_users,
        "sanctioned_users": sanctioned,
        "actions_last_24h": db.session.scalar(
            select(func.count(ModerationAction.id)).filter(
                ModerationAction.created_at >= utcnow() - timedelta(hours=24)
            )
        )
        or 0,
    }


def expire_temporary_actions() -> int:
    """Lift suspensions whose expiry has passed. Run hourly by Celery."""
    now = utcnow()
    actions = (
        db.session.query(ModerationAction)
        .filter(
            ModerationAction.action.in_([ActionType.RESTRICT.value, ActionType.SUSPEND.value]),
            ModerationAction.expires_at.isnot(None),
            ModerationAction.expires_at <= now,
            ModerationAction.revoked_at.is_(None),
        )
        .all()
    )
    released = 0
    for action in actions:
        action.revoked_at = now
        action.revoked_by_id = None
        subject = action.subject
        if subject is not None and subject.status == UserStatus.SUSPENDED.value:
            subject.status = UserStatus.ACTIVE.value
            subject.status_changed_at = now
            subject.status_reason = None
        released += 1
    if actions:
        db.session.commit()
        log_event(logger, "INFO", "moderation.actions_expired", count=released)
    return released


__all__ = [
    "AUTO_HIDE_REPORT_THRESHOLD",
    "action_history",
    "assign_report",
    "create_report",
    "dashboard_stats",
    "expire_temporary_actions",
    "resolve_report",
    "review_queue",
]

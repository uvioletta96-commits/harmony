"""Moderator and administrator endpoints.

Every route here is behind ``@roles_required("moderator", "admin")``; the
decorator enforces it before the view body runs, so a new endpoint added to
this blueprint is protected by default rather than by remembering to guard it.
"""

from __future__ import annotations

from flask import Blueprint, g, request

from ..extensions import db
from ..models.moderation import ActionType, ReportStatus
from ..models.user import UserRole, UserStatus
from ..security.decorators import admin_required, moderator_required, verify_csrf
from ..security.validators import Schema
from ..services import moderation_service, post_service, user_service
from ..utils.query import int_arg
from ..utils.responses import NotFoundError, ValidationError, ok

bp = Blueprint("admin", __name__)

resolve_schema = (
    Schema()
    .choice("action", [item.value for item in ActionType], default=ActionType.NONE.value)
    .string("note", required=False, max_length=2000, allow_newlines=True, default="")
    .integer("duration_hours", minimum=1, maximum=24 * 365, default=None)
    .ignore_unknown()
)

user_action_schema = (
    Schema()
    .choice(
        "action",
        [
            ActionType.WARN.value,
            ActionType.RESTRICT.value,
            ActionType.SUSPEND.value,
            ActionType.BAN.value,
            ActionType.UNBAN.value,
            ActionType.NONE.value,
        ],
        required=True,
    )
    .string("reason", required=False, max_length=2000, allow_newlines=True, default="")
    # ``note`` is the message shown to the user being acted upon. Accepted as an
    # alias so a caller that only wants to explain themselves to the user does
    # not have to duplicate the text into the internal reason field.
    .string("note", required=False, max_length=1000, allow_newlines=True, default="")
    .integer("duration_hours", minimum=1, maximum=24 * 365, default=None)
    .ignore_unknown()
)

role_schema = Schema().choice("role", [item.value for item in UserRole], required=True).ignore_unknown()
status_schema = Schema().choice("status", [item.value for item in UserStatus], required=True).ignore_unknown()

#: Fallback texts so an action is never recorded without a reason. A moderation
#: log that says only "ban" cannot be explained to an appeal six months later.
DEFAULT_ACTION_REASON = "Административная мера"
DEFAULT_SUSPEND_REASON = "Приостановлен администратором"
DEFAULT_BAN_REASON = "Заблокирован администратором"


def _status_reason(data: dict, default: str) -> str:  # type: ignore[no-untyped-def]
    """The reason shown to the user whose access is being restricted."""
    return ((data.get("note") or data.get("reason") or "").strip() or default)[:256]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@bp.get("/admin/stats")
@moderator_required
def stats():
    return ok(
        {
            "moderation": moderation_service.dashboard_stats(),
            "platform": post_service.public_counts(),
        }
    )


# ---------------------------------------------------------------------------
# Report queue
# ---------------------------------------------------------------------------


@bp.get("/admin/reports")
@moderator_required
def queue():
    page = moderation_service.review_queue(
        g.current_user,
        status=request.args.get("status", ReportStatus.OPEN.value),
        target_type=request.args.get("target_type"),
        page=int_arg("page", default=1),
        per_page=int_arg("per_page", default=25),
    )
    return ok(page.items, meta=page.to_meta({"status": request.args.get("status", ReportStatus.OPEN.value)}))


@bp.post("/admin/reports/<report_id>/assign")
@moderator_required
def assign(report_id: str):
    verify_csrf()
    report = _get_report(report_id)
    moderation_service.assign_report(report, g.current_user)
    return ok({"report": report.to_dict()})


@bp.post("/admin/reports/<report_id>/resolve")
@moderator_required
def resolve(report_id: str):
    """Apply an enforcement decision. This is the single audited write path."""
    verify_csrf()
    report = _get_report(report_id)
    data = resolve_schema(request.get_json(silent=True) or {})
    action = moderation_service.resolve_report(
        report,
        g.current_user,
        action=data["action"],
        note=data.get("note") or "",
        duration_hours=data.get("duration_hours"),
    )
    return ok({"action": action.to_dict(), "report": report.to_dict()})


def _get_report(report_id: str):  # type: ignore[no-untyped-def]
    from ..extensions import db
    from ..models.moderation import Report

    report = db.session.scalar(db.select(Report).where(Report.public_id == report_id))
    if report is None:
        raise NotFoundError("Жалоба не найдена.", code="report_not_found")
    return report


# ---------------------------------------------------------------------------
# Content actions
# ---------------------------------------------------------------------------


@bp.post("/admin/posts/<post_id>/action")
@moderator_required
def moderate_post(post_id: str):
    verify_csrf()
    data = resolve_schema(request.get_json(silent=True) or {})
    post = post_service.get_post(post_id, g.current_user)
    action = data["action"]

    if action == ActionType.HIDE.value:
        post.status = "under_review"
        post.status_reason = data.get("note") or "Скрыто модератором"
    elif action == ActionType.REMOVE.value:
        post_service.delete_post(post, g.current_user)
    elif action == ActionType.RESTORE.value:
        post_service.restore_post(post, g.current_user)
    else:
        raise ValidationError("Действие неприменимо к посту.", code="invalid_action_for_post")
    db.session.commit()
    user_service.record_audit("admin.post_action", g.current_user, action=action, target_id=post_id)
    return ok({"post": post.to_dict(g.current_user)})


@bp.post("/admin/comments/<comment_id>/action")
@moderator_required
def moderate_comment(comment_id: str):
    verify_csrf()
    from ..services import comment_service

    data = resolve_schema(request.get_json(silent=True) or {})
    comment = comment_service.get_comment(comment_id, g.current_user)
    action = data["action"]
    if action == ActionType.REMOVE.value:
        comment_service.delete_comment(comment, g.current_user)
    else:
        raise ValidationError("Действие неприменимо к комментарию.", code="invalid_action_for_comment")
    user_service.record_audit("admin.comment_action", g.current_user, action=action, target_id=comment_id)
    return ok({"comment": comment.to_dict(g.current_user)})


# ---------------------------------------------------------------------------
# User administration
# ---------------------------------------------------------------------------


@bp.get("/admin/users")
@moderator_required
def list_users():
    from sqlalchemy import or_, select

    from ..models.user import User
    from ..security.xss import sanitize_plain_text
    from ..utils.pagination import offset_page

    query = select(User)
    status = request.args.get("status")
    if status:
        query = query.where(User.status == status)
    term = sanitize_plain_text(request.args.get("q") or "", max_length=64)
    if term:
        pattern = f"%{term}%"
        query = query.where(
            or_(
                User.username.ilike(pattern, escape="\\"),
                User.display_name.ilike(pattern, escape="\\"),
            )
        )
    query = query.order_by(User.created_at.desc())
    page = offset_page(
        query,
        page=int_arg("page", default=1),
        page_size=int_arg("per_page", default=25),
        serialize=lambda u: {
            **u.to_public_dict(None),
            "status": u.status,
            "warnings": u.warning_count,
            "reports_against": u.reports_against_count,
            "created_at": u.created_at.isoformat(),
        },
    )
    return ok(page.items, meta=page.to_meta())


@bp.post("/admin/users/<public_id>/action")
@moderator_required
def moderate_user(public_id: str):
    """Warn, suspend, ban or reinstate an account."""
    verify_csrf()
    data = user_action_schema(request.get_json(silent=True) or {})
    target = user_service.get_user(public_id)

    if target.id == g.current_user.id:
        raise ValidationError("Нельзя применить действие к собственному аккаунту.", code="self_action")
    if target.is_admin and not g.current_user.is_admin:
        raise ValidationError("Недостаточно прав для действий с администратором.", code="insufficient_role")

    from datetime import timedelta

    from ..models.base import utcnow
    from ..models.moderation import ModerationAction

    action = data["action"]
    duration = data.get("duration_hours")
    expires_at = utcnow() + timedelta(hours=duration) if duration else None
    now = utcnow()

    db.session.add(
        ModerationAction(
            action=action,
            subject_id=target.id,
            moderator_id=g.current_user.id,
            source="manual",
            reason=(data.get("reason") or "").strip() or DEFAULT_ACTION_REASON,
            note_to_user=(data.get("note") or data.get("reason") or "").strip() or None,
            expires_at=expires_at,
        )
    )

    if action == ActionType.WARN.value:
        target.warning_count = (target.warning_count or 0) + 1
    elif action in (ActionType.RESTRICT.value, ActionType.SUSPEND.value):
        target.status = UserStatus.SUSPENDED.value
        target.status_reason = _status_reason(data, DEFAULT_SUSPEND_REASON)
        target.status_changed_at = now
        target.status_changed_by_id = g.current_user.id
        _revoke_sessions(target.id, now, action)
    elif action == ActionType.BAN.value:
        target.status = UserStatus.BANNED.value
        target.status_reason = _status_reason(data, DEFAULT_BAN_REASON)
        target.status_changed_at = now
        target.status_changed_by_id = g.current_user.id
        _revoke_sessions(target.id, now, action)
    elif action == ActionType.UNBAN.value:
        target.status = UserStatus.ACTIVE.value
        target.status_reason = None
        target.status_changed_at = now
    else:
        raise ValidationError("Действие не распознано.", code="unknown_action")

    db.session.commit()
    user_service.record_audit(
        "admin.user_action", g.current_user, action=action, target_id=public_id, duration_hours=duration
    )
    return ok(
        {
            "user": {**target.to_public_dict(None), "status": target.status, "warnings": target.warning_count},
            "action": action,
        }
    )


def _revoke_sessions(user_id: int, now, reason: str) -> None:
    from ..extensions import db
    from ..models.user import UserSession

    for session in (
        db.session.query(UserSession).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).all()
    ):
        session.revoked_at = now
        session.revoked_reason = reason


@bp.post("/admin/users/<public_id>/role")
@admin_required
def set_role(public_id: str):
    """Change a role. Admin-only: a moderator must not be able to promote."""
    verify_csrf()
    data = role_schema(request.get_json(silent=True) or {})
    target = user_service.get_user(public_id)
    if target.id == g.current_user.id:
        raise ValidationError("Нельзя изменить собственную роль.", code="self_role_change")
    target.role = data["role"]
    db.session.commit()
    user_service.record_audit("admin.role_change", g.current_user, target_id=public_id, role=data["role"])
    return ok({"user": target.to_public_dict(None), "role": target.role})


@bp.post("/admin/users/<public_id>/status")
@admin_required
def set_status(public_id: str):
    """Override an account's status directly, outside the audited action flow.

    Banning and suspending go through ``/action`` instead, which records who did
    it and why. This endpoint exists for the cases that are not sanctions -
    reactivating, correcting a mistaken status - and is admin-only because it
    writes the same field a sanction does.
    """
    verify_csrf()
    from ..models.base import utcnow

    data = status_schema(request.get_json(silent=True) or {})
    target = user_service.get_user(public_id)
    target.status = data["status"]
    target.status_changed_at = utcnow()
    target.status_changed_by_id = g.current_user.id
    db.session.commit()
    user_service.record_audit("admin.status_change", g.current_user, target_id=public_id, status=data["status"])
    return ok({"user": target.to_public_dict(None), "status": target.status})


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@bp.get("/admin/actions")
@moderator_required
def action_history():
    page = moderation_service.action_history(
        subject_id=request.args.get("subject_id", type=int),
        page=int_arg("page", default=1),
        per_page=int_arg("per_page", default=25),
    )
    return ok(page.items, meta=page.to_meta())


@bp.get("/admin/audit")
@moderator_required
def audit_log():
    from sqlalchemy import select

    from ..models.system import AuditLog
    from ..utils.pagination import offset_page

    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    action = request.args.get("action")
    if action:
        query = query.filter(AuditLog.action == action)
    page = offset_page(
        query,
        page=int_arg("page", default=1),
        page_size=int_arg("per_page", default=50),
        serialize=lambda a: a.to_dict(),
    )
    return ok(page.items, meta=page.to_meta())


__all__ = ["bp"]

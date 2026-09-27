"""Notification inbox endpoints."""

from __future__ import annotations

from flask import Blueprint, g, request

from ..security.decorators import auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..services import notification_service
from ..utils.responses import ok

bp = Blueprint("notifications", __name__)


@bp.get("/notifications")
@auth_required
def list_notifications():
    enforce("global")
    page = notification_service.list_notifications(
        g.current_user,
        unread_only=(request.args.get("unread") == "1"),
        cursor=request.args.get("cursor"),
        limit=int(request.args.get("limit", 30) or 30),
    )
    return ok(
        page.items,
        meta=page.to_meta({"unread_count": notification_service.unread_count(g.current_user)}),
    )


@bp.get("/notifications/unread-count")
@auth_required
def unread_count():
    return ok({"unread_count": notification_service.unread_count(g.current_user)})


@bp.post("/notifications/read")
@auth_required
def mark_read():
    verify_csrf()
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if ids and isinstance(ids, list):
        count = notification_service.mark_read(g.current_user, [str(item) for item in ids][:100])
    elif payload.get("all"):
        count = notification_service.mark_all_read(g.current_user)
    else:
        count = notification_service.mark_read(g.current_user)
    return ok({"marked": count, "unread_count": notification_service.unread_count(g.current_user)})


@bp.delete("/notifications/<notification_id>")
@auth_required
def delete_one(notification_id: str):
    verify_csrf()
    from ..extensions import db
    from ..models.system import Notification

    row = db.session.scalar(
        db.select(Notification).where(
            Notification.public_id == notification_id, Notification.user_id == g.current_user.id
        )
    )
    if row is None:
        from ..utils.responses import NotFoundError

        raise NotFoundError("Уведомление не найдено.", code="notification_not_found")
    db.session.delete(row)
    db.session.commit()
    return ok({"deleted": True})


__all__ = ["bp"]

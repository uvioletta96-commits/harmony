"""Comment endpoints."""

from __future__ import annotations

from flask import Blueprint, g, request

from ..security.decorators import auth_optional, auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..security.validators import Schema
from ..services import comment_service, post_service
from ..utils.responses import created, no_content, ok

bp = Blueprint("comments", __name__)

# ``body`` is deliberately not marked required. Letting it through to the
# service means an empty comment produces the domain-specific ``empty_comment``
# error with an actionable message, instead of a generic schema failure.
create_schema = (
    Schema()
    .string("body", required=False, max_length=2000, allow_newlines=True, default="")
    .raw("parent_id", default=None)
    .ignore_unknown()
)
update_schema = Schema().string("body", max_length=2000, allow_newlines=True).ignore_unknown()


def _viewer():  # type: ignore[no-untyped-def]
    return getattr(g, "current_user", None)


def _broadcast(event: str, payload: dict) -> None:  # type: ignore[type-arg]
    """Push over WebSocket. Failures are logged, never raised into the write.

    Realtime delivery is a courtesy: if a socket is wedged, the comment must
    still be persisted and the request must still succeed.
    """
    from ..realtime.manager import emit_global

    try:
        emit_global(event, payload)
    except Exception:  # pragma: no cover - best-effort
        from ..utils.logging import get_logger

        get_logger("harmony.api.comments").warning("realtime broadcast failed", exc_info=True)


@bp.get("/posts/<post_id>/comments")
@auth_optional
def list_comments(post_id: str):
    post = post_service.get_post(post_id, _viewer())
    page = comment_service.list_comments(
        post,
        _viewer(),
        cursor=request.args.get("cursor"),
        limit=request.args.get("limit"),
        sort=(request.args.get("sort") or "top").lower(),
    )
    return ok(page.items, meta=page.to_meta({"post_id": post.public_id, "sort": (request.args.get("sort") or "top")}))


@bp.post("/posts/<post_id>/comments")
@auth_required
def create_comment(post_id: str):
    verify_csrf()
    enforce("comment:create")
    post = post_service.get_post(post_id, g.current_user)
    data = create_schema(request.get_json(silent=True) or {})

    parent = None
    parent_id = data.get("parent_id")
    if parent_id:
        parent = comment_service.get_comment(str(parent_id), g.current_user)

    comment = comment_service.create_comment(post, g.current_user, data["body"], parent=parent)
    payload = comment.to_dict(g.current_user)
    _broadcast("comment.new", {"post_id": post.public_id, "comment": payload})
    return created({"comment": payload})


@bp.patch("/comments/<comment_id>")
@auth_required
def update_comment(comment_id: str):
    verify_csrf()
    enforce("post:update")
    comment = comment_service.get_comment(comment_id, g.current_user)
    data = update_schema(request.get_json(silent=True) or {})
    updated = comment_service.update_comment(comment, g.current_user, data["body"])
    return ok({"comment": updated.to_dict(g.current_user)})


@bp.delete("/comments/<comment_id>")
@auth_required
def delete_comment(comment_id: str):
    verify_csrf()
    enforce("post:delete")
    comment = comment_service.get_comment(comment_id, g.current_user)
    comment_service.delete_comment(comment, g.current_user)
    return no_content()


@bp.get("/comments/<comment_id>/replies")
@auth_optional
def list_replies(comment_id: str):
    from ..extensions import db
    from ..models.post import Comment, PostStatus
    from ..utils.pagination import offset_page

    comment = comment_service.get_comment(comment_id, _viewer())
    query = (
        db.select(Comment)
        .where(Comment.root_id == comment.id, Comment.status == PostStatus.PUBLISHED.value)
        .order_by(Comment.created_at.asc())
    )
    page = offset_page(
        query,
        page=int(request.args.get("page", 1) or 1),
        page_size=int(request.args.get("limit", 30) or 30),
        serialize=lambda c: c.to_dict(_viewer()),
    )
    return ok(page.items, meta=page.to_meta({"root_id": comment.public_id}))


__all__ = ["bp"]

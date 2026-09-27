"""Chat endpoints (HTTP side).

Message *delivery* happens over WebSocket; these endpoints cover conversation
lifecycle, history, and a polling fallback used when the socket cannot connect.
"""

from __future__ import annotations

from flask import Blueprint, g, request

from ..security.decorators import auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..security.validators import Schema
from ..services import chat_service, user_service
from ..utils.responses import NotFoundError, created, no_content, ok

bp = Blueprint("chat", __name__)

start_schema = Schema().raw("user_id", required=True).ignore_unknown()
# ``body`` is deliberately optional at the schema layer: the service decides
# whether a blank message is an ``empty_message`` or merely short, and its error
# names the field the user actually has to fix.
send_schema = Schema().string("body", required=False, max_length=4000, allow_newlines=True).ignore_unknown()


@bp.get("/conversations")
@auth_required
def list_conversations():
    rows = chat_service.list_conversations(
        g.current_user,
        limit=int(request.args.get("limit", 30) or 30),
        offset=int(request.args.get("offset", 0) or 0),
    )
    return ok({"conversations": rows, "total_unread": chat_service.total_unread(g.current_user)})


@bp.post("/conversations")
@auth_required
def start_conversation():
    verify_csrf()
    enforce("global")
    data = start_schema(request.get_json(silent=True) or {})
    other = user_service.get_user(str(data["user_id"]))
    conversation, created_now = chat_service.get_or_create_direct(g.current_user, other)
    return created({"conversation": conversation.to_dict(g.current_user), "created": created_now})


@bp.get("/conversations/<conversation_id>")
@auth_required
def get_conversation(conversation_id: str):
    conversation = chat_service.get_conversation(conversation_id, g.current_user)
    return ok({"conversation": conversation.to_dict(g.current_user)})


@bp.get("/conversations/<conversation_id>/messages")
@auth_required
def list_messages(conversation_id: str):
    """Paginated history. Returns oldest-first for direct rendering."""
    enforce("global")
    conversation = chat_service.get_conversation(conversation_id, g.current_user)
    page = chat_service.list_messages(
        conversation, g.current_user, cursor=request.args.get("cursor"), limit=request.args.get("limit", 40)
    )
    return ok(page.items, meta=page.to_meta({"conversation_id": conversation.public_id}))


@bp.post("/conversations/<conversation_id>/messages")
@auth_required
def send_message(conversation_id: str):
    """REST fallback for sending. The WebSocket path is preferred."""
    verify_csrf()
    enforce("chat:send")
    conversation = chat_service.get_conversation(conversation_id, g.current_user)
    data = send_schema(request.get_json(silent=True) or {})
    message = chat_service.send_message(conversation, g.current_user, data["body"])
    return created({"message": message.to_dict(g.current_user)})


@bp.post("/conversations/<conversation_id>/read")
@auth_required
def mark_read(conversation_id: str):
    verify_csrf()
    conversation = chat_service.get_conversation(conversation_id, g.current_user)
    payload = request.get_json(silent=True) or {}
    result = chat_service.mark_read(conversation, g.current_user, up_to_message_id=payload.get("up_to_message_id"))
    return ok(result)


@bp.delete("/messages/<message_id>")
@auth_required
def delete_message(message_id: str):
    verify_csrf()
    from ..extensions import db
    from ..models.chat import Message

    message = db.session.scalar(db.select(Message).where(Message.public_id == message_id))
    if message is None:
        raise NotFoundError("Сообщение не найдено.", code="message_not_found")
    chat_service.delete_message(message, g.current_user)
    return no_content()


@bp.post("/conversations/<conversation_id>/leave")
@auth_required
def leave(conversation_id: str):
    verify_csrf()
    conversation = chat_service.get_conversation(conversation_id, g.current_user)
    chat_service.leave_conversation(conversation, g.current_user)
    return no_content()


@bp.get("/messages/unread-count")
@auth_required
def unread_count():
    return ok({"unread": chat_service.total_unread(g.current_user)})


__all__ = ["bp"]

"""Socket.IO event handlers.

Authentication happens in the ``connect`` handler rather than per-message, so an
unauthenticated socket never exists and every subsequent handler can trust
``request.environ['harmony_user']``.

Rate limiting is applied to the *message* events (the expensive, abusable
ones) rather than the connection itself: a user legitimately reconnects often
on mobile networks, but does not legitimately send 200 messages a minute.
"""

from __future__ import annotations

from typing import Any

from flask import request
from flask_socketio import ConnectionRefusedError, emit  # noqa: A004

from ..extensions import db, socketio
from ..models.base import utcnow
from ..models.chat import Conversation
from ..models.user import User
from ..security.rate_limit import limiter
from ..security.validators import Schema
from ..services import chat_service
from ..utils.logging import get_logger, log_event
from ..utils.responses import ApiError, ValidationError

logger = get_logger("harmony.realtime.events")

send_message_schema = (
    Schema().string("body", max_length=4000, allow_newlines=True).raw("reply_to_id", default=None).ignore_unknown()
)
typing_schema = Schema().raw("conversation_id", required=True).boolean("is_typing", default=True).ignore_unknown()


def _current_user() -> User | None:
    return request.environ.get("harmony_user")


def _client_ip() -> str:
    from ..security.rate_limit import NO_IP, client_ip

    try:
        return client_ip()
    except Exception:
        return request.remote_addr or NO_IP


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@socketio.on("connect")
def handle_connect(auth: dict | None = None):
    """Authenticate the handshake. Failure refuses the connection outright."""
    from ..security.decorators import ACCESS_COOKIE, REFRESH_COOKIE, load_user_from_token

    token = None
    if auth and auth.get("token"):
        token = str(auth["token"])
    else:
        token = request.cookies.get(ACCESS_COOKIE) or request.cookies.get(REFRESH_COOKIE)
    if not token:
        log_event(logger, "INFO", "ws.connect_unauthenticated", ip=_client_ip())
        raise ConnectionRefusedError("authentication required")

    try:
        user = load_user_from_token(token)
    except ApiError as exc:
        log_event(logger, "INFO", "ws.connect_rejected", reason=exc.code)
        raise ConnectionRefusedError(exc.message) from exc

    if user is None or not user.is_usable_account:
        raise ConnectionRefusedError("account unavailable")

    request.environ["harmony_user"] = user
    from .manager import register

    register(request.sid, user)
    emit(
        "connected",
        {
            "user": user.to_public_dict(user),
            "online": True,
            "server_time": utcnow().isoformat(),
        },
    )


@socketio.on("disconnect")
def handle_disconnect(*args: Any, **kwargs: Any) -> None:
    from .manager import unregister

    unregister(request.sid)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@socketio.on("conversation:join")
def handle_join(data: dict | None) -> Any:
    user = _current_user()
    if user is None:
        return {"ok": False, "error": "unauthenticated"}
    conversation_id = str((data or {}).get("conversation_id") or "")
    if not conversation_id:
        return {"ok": False, "error": "conversation_id required"}

    conversation = db.session.scalar(db.select(Conversation).where(Conversation.public_id == conversation_id))
    if conversation is None or not conversation.has_member(user):
        # Same response as for a missing conversation: no existence disclosure.
        return {"ok": False, "error": "conversation not found"}

    from .manager import add_to_conversation

    add_to_conversation(request.sid, conversation.id)
    return {"ok": True, "conversation": conversation.to_dict(user)}


@socketio.on("conversation:leave")
def handle_leave(data: dict | None) -> Any:
    from .manager import remove_from_conversation

    conversation_id = str((data or {}).get("conversation_id") or "")
    conversation = db.session.scalar(db.select(Conversation).where(Conversation.public_id == conversation_id))
    if conversation is not None:
        remove_from_conversation(request.sid, conversation.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


@socketio.on("message:send")
def handle_send(data: dict | None) -> Any:
    """Send a message. Returns an ack the client renders optimistically."""
    user = _current_user()
    if user is None:
        return {"ok": False, "error": "unauthenticated"}

    payload = data or {}
    verdict = limiter.check("chat:send", f"user:{user.id}")
    if not verdict.allowed:
        emit("error:notice", {"code": "rate_limited", "retry_after": verdict.retry_after}, to=request.sid)
        return {"ok": False, "error": "rate_limited", "retry_after": verdict.retry_after}

    try:
        body = send_message_schema(payload)
    except ValidationError as exc:
        return {"ok": False, "error": exc.code, "fields": exc.fields}

    conversation_id = str(payload.get("conversation_id") or "")
    conversation = db.session.scalar(db.select(Conversation).where(Conversation.public_id == conversation_id))
    if conversation is None or not conversation.has_member(user):
        return {"ok": False, "error": "conversation not found"}

    try:
        message = chat_service.send_message(conversation, user, body["body"])
    except ApiError as exc:
        # Content policy rejections are reported precisely; anything else is
        # a generic failure so internals are not leaked over the socket.
        log_event(logger, "INFO", "ws.message_rejected", code=exc.code, user_id=user.id)
        return {"ok": False, "error": exc.code, "message": exc.message}
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        log_event(logger, "ERROR", "ws.message_failed", error=exc.__class__.__name__)
        return {"ok": False, "error": "internal_error"}

    from .manager import add_to_conversation

    add_to_conversation(request.sid, conversation.id)
    return {"ok": True, "message": message.to_dict(user)}


@socketio.on("conversation:read")
def handle_read(data: dict | None) -> Any:
    user = _current_user()
    if user is None:
        return {"ok": False, "error": "unauthenticated"}
    conversation_id = str((data or {}).get("conversation_id") or "")
    conversation = db.session.scalar(db.select(Conversation).where(Conversation.public_id == conversation_id))
    if conversation is None or not conversation.has_member(user):
        return {"ok": False, "error": "conversation not found"}
    return {
        "ok": True,
        **chat_service.mark_read(conversation, user, up_to_message_id=(data or {}).get("up_to_message_id")),
    }


@socketio.on("message:typing")
def handle_typing(data: dict | None) -> Any:
    """Presence only. Never persisted, and never rate-limited into spam."""
    user = _current_user()
    if user is None:
        return {"ok": False, "error": "unauthenticated"}
    try:
        parsed = typing_schema(data or {})
    except ValidationError:
        return {"ok": False, "error": "invalid_payload"}
    conversation = db.session.scalar(
        db.select(Conversation).where(Conversation.public_id == str(parsed["conversation_id"]))
    )
    if conversation is None or not conversation.has_member(user):
        return {"ok": False, "error": "conversation not found"}
    from .manager import emit_to_conversation

    emit_to_conversation(
        conversation,
        "message:typing",
        {"user": user.to_public_dict(user), "is_typing": parsed["is_typing"]},
        exclude_sid=request.sid,
    )
    return {"ok": True}


@socketio.on("presence:ping")
def handle_ping(data: dict | None = None) -> Any:
    return {"ok": True, "server_time": utcnow().isoformat()}


__all__ = [
    "handle_connect",
    "handle_disconnect",
    "handle_join",
    "handle_leave",
    "handle_read",
    "handle_send",
    "handle_typing",
]

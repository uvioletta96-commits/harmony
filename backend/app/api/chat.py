"""Chat endpoints (HTTP side).

Message *delivery* happens over WebSocket; these endpoints cover conversation
lifecycle, history, and a polling fallback used when the socket cannot connect.
"""

from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, current_app, g, request

from ..extensions import db
from ..models.chat import MessageAttachment
from ..security.decorators import auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..security.validators import Schema
from ..services import chat_service, upload_service, user_service
from ..utils.query import int_arg
from ..utils.responses import NotFoundError, PermissionError_, ValidationError, created, no_content, ok
from .uploads import collect_upload_payload

bp = Blueprint("chat", __name__)

start_schema = Schema().raw("user_id", required=True).ignore_unknown()
# ``body`` is deliberately optional at the schema layer: the service decides
# whether a blank message is an ``empty_message`` or merely short, and its error
# names the field the user actually has to fix.
send_schema = Schema().string("body", required=False, max_length=4000, allow_newlines=True).ignore_unknown()
#: Attachment ids, in the order the reader should see them. Declared as a raw field
#: so `ignore_unknown` keeps it - an undeclared key is stripped, and a message sent
#: with files would arrive as an empty one.
#:
#: The count is capped in the service rather than here: a schema maximum cannot
#: express "at most ten, and at least one when there is no text", which is the actual
#: rule.
attachment_schema = Schema().raw("media_ids", required=False, default=None).ignore_unknown()


@bp.get("/conversations")
@auth_required
def list_conversations():
    rows = chat_service.list_conversations(
        g.current_user,
        limit=int_arg("limit", default=30),
        offset=int_arg("offset", default=0),
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


@bp.post("/conversations/<conversation_id>/media")
@auth_required
def upload_attachment(conversation_id: str):
    """Upload a file to attach to a message: photo, video or a voice recording.

    Same two-step shape as a post attachment - upload first, claim by id when the
    message is sent - so an abandoned upload is a row with ``message_id = NULL``
    that the nightly sweep collects, rather than an orphaned file nothing knows
    about.

    The type comes from the bytes, not from the field name or the client's
    Content-Type. ``kind`` says how it should be *presented*; a file claiming to be
    a voice recording and turning out to be a video is stored as a video, and the
    client renders what the server says rather than what it asked for.
    """
    verify_csrf()
    enforce("chat:send")
    conversation = chat_service.get_conversation(conversation_id, g.current_user)

    files, inline = collect_upload_payload()
    if not files:
        raise ValidationError("Не передано ни одного файла.", code="no_files")

    if not chat_service.can_send_to(conversation, g.current_user):
        # Checked here as well as at send time, so the file is never written for a
        # conversation the sender is not allowed to use. Uploading first and being
        # refused at send would leave bytes on disk for nothing.
        raise PermissionError_(
            "Этот пользователь не принимает сообщения от незнакомцев.", code="messages_not_allowed"
        )

    kind = (request.form.get("kind") or (inline or {}).get("kind") or "image").strip().lower()
    if kind not in ("image", "video", "voice", "circle"):
        raise ValidationError("Неизвестный тип вложения.", code="bad_attachment_kind")

    duration_ms = request.form.get("duration_ms") or (inline or {}).get("duration_ms") or 0
    alt_text = (request.form.get("alt_text") or (inline or {}).get("alt_text") or "").strip()[:240]

    # The waveform arrives as a JSON string in a form field on the multipart path,
    # and as a real list in a JSON body. Reading only the JSON shape means the
    # player draws no bars at all for every voice message sent the normal way.
    raw_waveform = request.form.get("waveform")
    if raw_waveform is None:
        raw_waveform = (inline or {}).get("waveform")
    waveform: Any = raw_waveform
    if isinstance(raw_waveform, str) and raw_waveform.strip():
        try:
            waveform = json.loads(raw_waveform)
        except json.JSONDecodeError:
            waveform = []
    else:
        waveform = []

    stored: list[Any] = []
    rows: list[MessageAttachment] = []
    for payload, _filename in files:
        media = upload_service.store_chat_media(payload, user_id=g.current_user.id, kind=kind)
        row = MessageAttachment(
            owner_id=g.current_user.id,
            message_id=None,
            position=len(stored),
            kind=media.kind,
            storage_key=media.storage_key,
            url=media.url,
            thumbnail_url=media.thumbnail_url,
            mime_type=media.mime_type,
            byte_size=media.byte_size,
            width=media.width,
            height=media.height,
            duration_ms=_clamp_duration(duration_ms),
            waveform=_clamp_waveform(waveform),
            alt_text=alt_text or None,
            content_hash=media.content_hash,
        )
        db.session.add(row)
        stored.append(media)
        rows.append(row)

    db.session.commit()
    return created({"files": [row.to_dict() | {"id": row.id} for row in rows]})


def _clamp_duration(value: Any) -> int:
    """Milliseconds, bounded. A client that reports a year-long recording should not
    be able to make the player draw a progress bar nobody can scrub."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return 0
    limit = int(current_app.config.get("CHAT_MAX_VOICE_SECONDS", 300)) * 1000
    return max(0, min(millis, limit))


def _clamp_waveform(value: Any) -> list[int]:
    """Peaks for the player's bars: a bounded length, each 0-100.

    Read from the client because it is the only party that saw the audio as it was
    recorded. Bounded because it is rendered as one bar per entry and an unbounded
    array from a client is a rendering problem on the reader's phone.
    """
    if not isinstance(value, (list, tuple)):
        return []
    peaks = []
    for entry in list(value)[:96]:
        try:
            peaks.append(max(0, min(100, int(entry))))
        except (TypeError, ValueError):
            peaks.append(0)
    return peaks


@bp.post("/conversations/<conversation_id>/messages")
@auth_required
def send_message(conversation_id: str):
    """REST fallback for sending. The WebSocket path is preferred."""
    verify_csrf()
    enforce("chat:send")
    conversation = chat_service.get_conversation(conversation_id, g.current_user)
    payload = request.get_json(silent=True) or {}
    data = send_schema(payload)
    claims = chat_service.claim_attachments(g.current_user, attachment_schema(payload).get("media_ids") or [])
    # `.get`, not `["body"]`: the schema only puts a key in the result when a value
    # was actually provided, so a message sent as files alone has no "body" key at
    # all. Indexing it was a KeyError - and therefore a 500 - on exactly the case
    # attachments exist to serve.
    message = chat_service.send_message(
        conversation, g.current_user, data.get("body", ""), attachments=claims
    )
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

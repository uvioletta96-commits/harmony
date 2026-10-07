"""Direct messaging: conversations, membership, message delivery."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from flask import current_app
from sqlalchemy import func, select

from ..extensions import db
from ..models.base import utcnow
from ..models.chat import (
    AttachmentKind,
    Conversation,
    ConversationKind,
    ConversationMember,
    Message,
    MessageAttachment,
    MessageStatus,
    conversation_for_user,
)
from ..models.user import User, UserStatus
from ..security import spam
from ..security.content_moderation import get_engine as moderation_engine
from ..security.xss import sanitize_plain_text
from ..utils.logging import get_logger, log_event
from ..utils.pagination import Page, keyset_page
from ..utils.responses import ConflictError, NotFoundError, PermissionError_, ValidationError

logger = get_logger("harmony.service.chat")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_conversation(public_id: str, viewer: User) -> Conversation:
    conversation = db.session.scalar(select(Conversation).where(Conversation.public_id == public_id))
    if conversation is None:
        raise NotFoundError("Чат не найден.", code="conversation_not_found")
    if not conversation.has_member(viewer):
        # Non-members learn nothing, not even that the thread exists.
        raise NotFoundError("Чат не найден.", code="conversation_not_found")
    return conversation


def get_or_create_direct(viewer: User, other: User) -> tuple[Conversation, bool]:
    """Return the 1:1 conversation, creating it if needed.

    The ``direct_key`` unique constraint makes concurrent creation safe: the
    loser of the race catches ``IntegrityError`` and re-reads the winner's row.
    """
    if viewer.id == other.id:
        raise ValidationError("Нельзя создать чат с самим собой.", code="self_conversation")
    if other.status != UserStatus.ACTIVE.value:
        raise ConflictError("Этот аккаунт недоступен для общения.", code="account_unavailable")

    existing = conversation_for_user(viewer, other)
    if existing is not None:
        _rejoin(existing, viewer)
        db.session.commit()
        return existing, False

    key = Conversation.build_direct_key(viewer.id, other.id)
    conversation = Conversation(
        kind=ConversationKind.DIRECT.value,
        direct_key=key,
        created_by_id=viewer.id,
    )
    db.session.add(conversation)
    try:
        db.session.flush()
        db.session.add(ConversationMember(conversation_id=conversation.id, user_id=viewer.id))
        db.session.add(ConversationMember(conversation_id=conversation.id, user_id=other.id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        existing = conversation_for_user(viewer, other)
        if existing is None:
            raise
        return existing, False

    log_event(logger, "INFO", "chat.conversation_created", user_id=viewer.id, other=other.public_id)
    return conversation, True


def _rejoin(conversation: Conversation, user: User) -> None:
    membership = next((m for m in conversation.members or [] if m.user_id == user.id), None)
    if membership is not None and membership.left_at is not None:
        membership.left_at = None
        membership.unread_count = 0


def list_conversations(user: User, *, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    rows = (
        db.session.query(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(
            ConversationMember.user_id == user.id,
            ConversationMember.left_at.is_(None),
        )
        .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.id.desc())
        .limit(limit)
        .offset(max(0, offset))
        .all()
    )
    return [conversation.to_dict(user) for conversation in rows]


def list_messages(
    conversation: Conversation,
    viewer: User,
    *,
    cursor: str | None = None,
    limit: int = 40,
) -> Page:
    """Messages, oldest-first, keyset paginated for infinite scroll upwards."""
    page = keyset_page(
        select(Message).where(
            Message.conversation_id == conversation.id,
            Message.status != MessageStatus.DELETED.value,
        ),
        model=Message,
        page_size=limit,
        cursor=cursor,
        descending=True,
        order_fields=("created_at", "id"),
    )
    return Page(
        items=[message.to_dict(viewer) for message in reversed(page.items)],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_message(
    conversation: Conversation,
    sender: User,
    body: str,
    *,
    attachments: Sequence[MessageAttachment] | None = None,
    alt_texts: dict[int, str] | None = None,
) -> Message:
    """Send a message, with text, with files, or with both.

    A message may be empty of text as long as it carries a file - a photo with no
    caption and a voice message are both valid - so the emptiness check is against
    the two together.
    """
    if conversation.is_locked and not sender.is_moderator:
        raise PermissionError_("Чат заблокирован администратором.", code="conversation_locked")

    limit = int(current_app.config.get("CHAT_MAX_MESSAGE_CHARS", 4000))
    clean_body = sanitize_plain_text(body or "", max_length=limit)
    claims = list(attachments or [])
    if not clean_body and not claims:
        raise ValidationError(
            "Сообщение не может быть пустым.", code="empty_message", fields={"body": "Введите текст сообщения."}
        )

    # The recipient's preference applies to a message that is only a file just as
    # much as to one that is only text: someone who does not want messages from
    # strangers does not want their photos either.
    blocked = [
        member
        for member in conversation.members or []
        if member.user_id != sender.id
        and not member.left_at
        and member.user is not None
        and not member.user.allow_messages_from_anyone
        and not _is_mutual_contact(sender, member.user)
    ]
    if blocked and not sender.is_moderator:
        raise PermissionError_("Этот пользователь не принимает сообщения от незнакомцев.", code="messages_not_allowed")

    spam.guard_write(clean_body, sender)
    # Screened with the text that will be shown. An empty body is not a reason to
    # skip the check - a photo-only message is the one most likely to be abuse, and
    # the file's own scan happens at upload.
    decision = moderation_engine().screen(clean_body or "медиа", context="message")
    decision.raise_for_decision(kind="message")

    message = Message(
        conversation_id=conversation.id,
        sender_id=sender.id,
        body=clean_body,
        status=MessageStatus.SENT.value,
        moderation_score=decision.score,
    )
    db.session.add(message)
    db.session.flush()

    texts = alt_texts or {}
    for position, claim in enumerate(claims):
        claim.message_id = message.id
        claim.position = position
        if texts.get(position):
            claim.alt_text = texts[position][:240]

    conversation.last_message_at = message.created_at
    # The preview is what the conversation list shows, so a photo-only message needs
    # something to say - "Фото" rather than an empty line.
    conversation.last_message_preview = (clean_body[:160] if clean_body else _preview_for(claims))[:160]
    conversation.messages_count = (conversation.messages_count or 0) + 1

    recipients = 0
    for member in conversation.members or []:
        if member.user_id == sender.id or member.left_at:
            continue
        if member.last_read_at is None or member.last_read_at < message.created_at:
            member.unread_count = (member.unread_count or 0) + 1
            recipients += 1
    db.session.commit()

    _broadcast(conversation, message, sender)
    log_event(
        logger,
        "INFO",
        "chat.message_sent",
        user_id=sender.id,
        conversation_id=conversation.public_id,
        message_id=message.public_id,
        recipients=recipients,
    )
    return message


def _preview_for(attachments: Sequence[MessageAttachment]) -> str:
    """One line for the conversation list when a message is only a file.

    Without it a photo-only message leaves an empty row and the thread looks
    finished when it is not.
    """
    kinds = {a.kind for a in attachments}
    if AttachmentKind.VOICE.value in kinds or AttachmentKind.CIRCLE.value in kinds:
        return "Голосовое сообщение"
    if AttachmentKind.VIDEO.value in kinds:
        return "Видео"
    return "Фото"


def claim_attachments(
    owner: User,
    attachment_ids: Sequence[Any],
) -> list[MessageAttachment]:
    """Resolve client-supplied attachment ids to rows the sender actually owns.

    An id belonging to somebody else is a 404, not a silent skip: a client that
    believes it attached a photo should be told the photo is not theirs, rather than
    posting a message with a hole in it.

    Rows already claimed by a message are refused too, which is what stops one
    attachment being replayed into many messages.

    No conversation parameter, deliberately. The ids are internal and unique, so a
    row that belongs to the caller is the caller's row whichever conversation they
    send it to - and adding the conversation here would suggest the check is
    stronger than it is. The endpoint it is reached from is already scoped to one.
    """
    resolved: list[MessageAttachment] = []
    for raw in attachment_ids or []:
        try:
            numeric = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Некорректный идентификатор вложения.", code="bad_attachment_id") from exc

        row = db.session.get(MessageAttachment, numeric)
        if row is None or row.owner_id != owner.id:
            raise NotFoundError("Вложение не найдено.", code="attachment_not_found")
        if row.message_id is not None:
            raise ConflictError("Вложение уже отправлено.", code="attachment_already_sent")
        resolved.append(row)
    return resolved


def can_send_to(conversation: Conversation, sender: User) -> bool:
    """Whether ``sender`` may post in this conversation at all.

    Split out of :func:`send_message` so the attachment endpoint can ask the
    question *before* writing bytes. Upload-then-refuse would leave a file on disk
    for a conversation the sender was never allowed to use, and nothing would
    collect it until the nightly sweep.
    """
    blocked = [
        member
        for member in conversation.members or []
        if member.user_id != sender.id
        and not member.left_at
        and member.user is not None
        and not member.user.allow_messages_from_anyone
        and not _is_mutual_contact(sender, member.user)
    ]
    return not (blocked and not sender.is_moderator)


def _is_mutual_contact(sender: User, other: User) -> bool:
    """True when the sender is already in the recipient's following set."""
    from ..models.user import Relationship

    return (
        db.session.scalar(
            select(func.count(Relationship.id)).where(
                Relationship.follower_id == other.id,
                Relationship.followee_id == sender.id,
                Relationship.status == "following",
            )
        )
        or 0
    ) > 0


def mark_read(conversation: Conversation, user: User, *, up_to_message_id: str | None = None) -> dict[str, Any]:
    membership = next((m for m in conversation.members or [] if m.user_id == user.id), None)
    if membership is None:
        raise NotFoundError("Чат не найден.", code="conversation_not_found")

    now = utcnow()
    boundary = now
    if up_to_message_id:
        message = db.session.scalar(
            select(Message).where(Message.conversation_id == conversation.id, Message.public_id == up_to_message_id)
        )
        if message is not None:
            boundary = message.created_at

    # Messages that arrived since the previous read marker.
    unread = 0
    if membership.last_read_at is not None:
        unread = (
            db.session.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation.id,
                    Message.sender_id != user.id,
                    Message.status == MessageStatus.SENT.value,
                    Message.created_at > membership.last_read_at,
                )
            )
            or 0
        )
    membership.last_read_at = boundary
    membership.unread_count = 0
    db.session.commit()
    return {"unread_count": 0, "marked_read": unread, "last_read_at": boundary.isoformat()}


def delete_message(message: Message, actor: User) -> None:
    if message.sender_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Вы можете удалять только свои сообщения.", code="not_message_owner")

    # The files go before the text, not with it.
    #
    # A soft delete that clears the body but leaves the attachment rows serves the
    # photos anyway - the row is still there and the URL still resolves. "Delete
    # this message" has to mean the photograph is gone, not that the sentence
    # introducing it is. Soft-deleting rather than deleting the row keeps the
    # message's place in the thread for the other reader.
    discard_attachments(list(message.attachments or []))

    message.status = MessageStatus.DELETED.value
    message.body = ""
    message.deleted_at = utcnow()
    if message.conversation is not None and message.conversation.last_message_at == message.created_at:
        replacement = db.session.scalar(
            select(Message)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.id != message.id,
                Message.status == MessageStatus.SENT.value,
            )
            .order_by(Message.created_at.desc())
        )
        message.conversation.last_message_at = replacement.created_at if replacement else None
        message.conversation.last_message_preview = replacement.body[:160] if replacement else None
    db.session.commit()
    _broadcast(message.conversation, message, actor, event="message.deleted")


def discard_attachments(attachments: Sequence[MessageAttachment]) -> None:
    """Drop attachment rows and the bytes behind them.

    Both halves. The row alone leaves the file reachable through a URL someone
    already has; the file alone leaves a row pointing at nothing, which the nightly
    orphan sweep would then try to unlink again.

    A file that cannot be removed is logged rather than raised: the row is about to
    be deleted either way, and failing the whole delete because of one locked file
    would leave the user unable to remove the message at all.
    """
    from . import upload_service

    for attachment in attachments:
        try:
            upload_service.remove_stored(attachment.storage_key)
        except (OSError, ValueError) as exc:
            log_event(
                logger, "WARNING", "chat.attachment_file_kept",
                storage_key=attachment.storage_key, error=exc.__class__.__name__,
            )
        db.session.delete(attachment)


def leave_conversation(conversation: Conversation, user: User) -> None:
    membership = next((m for m in conversation.members or [] if m.user_id == user.id), None)
    if membership is None:
        raise NotFoundError("Чат не найден.", code="conversation_not_found")
    membership.left_at = utcnow()
    db.session.commit()


def total_unread(user: User) -> int:
    return (
        db.session.query(func.coalesce(func.sum(ConversationMember.unread_count), 0))
        .filter(ConversationMember.user_id == user.id, ConversationMember.left_at.is_(None))
        .scalar()
        or 0
    )


def ensure_conversation_participants(conversation_id: int, user_ids: Sequence[int]) -> None:
    """Idempotently add members. Used by the WebSocket invite path."""
    existing = {
        member.user_id
        for member in db.session.query(ConversationMember).filter(ConversationMember.conversation_id == conversation_id)
    }
    added = 0
    for user_id in user_ids:
        if user_id in existing:
            continue
        db.session.add(ConversationMember(conversation_id=conversation_id, user_id=user_id))
        added += 1
    if added:
        db.session.commit()


def _broadcast(conversation: Conversation, message: Message, sender: User, *, event: str = "message.new") -> None:
    """Push over WebSocket. Failures are logged, never raised into the write path."""
    from ..realtime.manager import emit_to_conversation

    payload = message.to_dict(sender)
    try:
        emit_to_conversation(conversation, event, payload)
    except Exception as exc:  # pragma: no cover - realtime is best-effort
        log_event(logger, "WARNING", "chat.broadcast_failed", error=exc.__class__.__name__)


__all__ = [
    "delete_message",
    "ensure_conversation_participants",
    "get_conversation",
    "get_or_create_direct",
    "leave_conversation",
    "list_conversations",
    "list_messages",
    "mark_read",
    "send_message",
    "total_unread",
]

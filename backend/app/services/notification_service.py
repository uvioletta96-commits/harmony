"""Notifications: persistence, fan-out and delivery preferences."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update

from ..extensions import celery_app, db
from ..models.base import utcnow
from ..models.post import Comment, Post
from ..models.system import Notification, NotificationKind
from ..models.user import Relationship, User
from ..utils.logging import get_logger, log_event
from ..utils.pagination import Page, keyset_page

logger = get_logger("harmony.service.notifications")


def create(
    user_id: int,
    kind: NotificationKind,
    *,
    title: str,
    body: str = "",
    actor_id: int | None = None,
    url: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> Notification:
    """Create one notification.

    Self-notifications are dropped: nobody needs to be told they liked their own
    post, and they are the single largest source of notification noise.
    """
    if actor_id is not None and actor_id == user_id:
        raise ValueError("self_notification_suppressed")
    notification = Notification(
        user_id=user_id,
        actor_id=actor_id,
        kind=kind if isinstance(kind, str) else kind.value,
        title=title[:160],
        body=body[:1000],
        url=url,
        resource_type=resource_type,
        resource_id=str(resource_id)[:64] if resource_id else None,
    )
    db.session.add(notification)
    return notification


def bulk_create(rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        try:
            create(**row)
            count += 1
        except ValueError:
            continue
    return count


def list_notifications(
    user: User,
    *,
    unread_only: bool = False,
    cursor: str | None = None,
    limit: int = 30,
) -> Page:
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    page = keyset_page(
        query, model=Notification, page_size=limit, cursor=cursor, descending=True, order_fields=("created_at", "id")
    )
    return Page(items=[item.to_dict() for item in page.items], next_cursor=page.next_cursor, has_more=page.has_more)


def unread_count(user: User) -> int:
    return (
        db.session.query(func.count(Notification.id))
        .filter(Notification.user_id == user.id, Notification.read_at.is_(None))
        .scalar()
        or 0
    )


def mark_read(user: User, notification_ids: list[str] | None = None) -> int:
    now = utcnow()
    query = db.session.query(Notification).filter(Notification.user_id == user.id, Notification.read_at.is_(None))
    if notification_ids:
        query = query.filter(Notification.public_id.in_(notification_ids))
    rows = query.all()
    for row in rows:
        row.read_at = now
    db.session.commit()
    return len(rows)


def mark_all_read(user: User) -> int:
    result = db.session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    db.session.commit()
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Celery tasks
#
# These live in the service module so the fan-out logic is unit-testable
# without a broker, while still running asynchronously in production.
# ---------------------------------------------------------------------------


@celery_app.task(name="notifications.followers_of_post", bind=True, max_retries=3)
def notify_followers_of_post(self, post_id: int) -> int:  # type: ignore[no-untyped-def]
    """Notify every follower of the author about a new post."""
    post = db.session.get(Post, post_id)
    if post is None:
        return 0
    author = post.author
    if author is None or not author.email_notifications:
        return 0

    followers = (
        db.session.query(Relationship.follower_id)
        .filter(Relationship.followee_id == author.id, Relationship.status == "following")
        .all()
    )
    rows = [
        {
            "user_id": follower_id,
            "kind": NotificationKind.FOLLOW,
            "title": f"{author.name} опубликовал(а) новую запись",
            "body": post.body[:180],
            "actor_id": author.id,
            "url": f"/post/{post.public_id}",
            "resource_type": "post",
            "resource_id": post.public_id,
        }
        for (follower_id,) in followers
        if follower_id != author.id
    ]
    if not rows:
        return 0
    created = bulk_create(rows)
    db.session.commit()
    log_event(logger, "INFO", "notify.followers", post_id=post_id, count=created)
    return created


@celery_app.task(name="notifications.comment", bind=True, max_retries=3)
def enqueue_comment_notifications(self, post_id: int, comment_id: int) -> int:  # type: ignore[no-untyped-def]
    """Notify the post author and the parent commenter about a new comment."""
    comment = db.session.get(Comment, comment_id)
    if comment is None:
        return 0
    post = db.session.get(Post, post_id)
    if post is None:
        return 0

    recipients: set[int] = set()
    if post.author_id and post.author_id != comment.author_id:
        recipients.add(post.author_id)
    if comment.parent is not None and comment.parent.author_id != comment.author_id:
        recipients.add(comment.parent.author_id)

    author = comment.author
    rows = [
        {
            "user_id": recipient,
            "kind": NotificationKind.REPLY if recipient != post.author_id else NotificationKind.COMMENT,
            "title": f"{author.name if author else 'Кто-то'} {'ответил(а) вам' if recipient != post.author_id else 'прокомментировал(а) вашу запись'}",
            "body": comment.body[:180],
            "actor_id": comment.author_id,
            "url": f"/post/{post.public_id}#comment-{comment.public_id}",
            "resource_type": "comment",
            "resource_id": comment.public_id,
        }
        for recipient in recipients
    ]
    created = bulk_create(rows)
    db.session.commit()
    return created


@celery_app.task(name="notifications.reaction", bind=True, max_retries=3)
def enqueue_reaction_notification(self, post_id: int, user_id: int) -> int:  # type: ignore[no-untyped-def]
    """Notify a post author about a like."""
    post = db.session.get(Post, post_id)
    if post is None or post.author_id == user_id:
        return 0
    actor = db.session.get(User, user_id)
    created = bulk_create(
        [
            {
                "user_id": post.author_id,
                "kind": NotificationKind.LIKE,
                "title": f"{actor.name if actor else 'Кто-то'} оценил(а) вашу запись",
                "url": f"/post/{post.public_id}",
                "actor_id": user_id,
                "resource_type": "post",
                "resource_id": post.public_id,
            }
        ]
    )
    db.session.commit()
    return created


@celery_app.task(name="notifications.follow", bind=True, max_retries=3)
def notify_follow(self, follower_id: int, followee_id: int) -> int:  # type: ignore[no-untyped-def]
    if follower_id == followee_id:
        return 0
    follower = db.session.get(User, follower_id)
    followee = db.session.get(User, followee_id)
    if follower is None or followee is None:
        return 0
    created = bulk_create(
        [
            {
                "user_id": followee_id,
                "kind": NotificationKind.FOLLOW,
                "title": f"{follower.name} подписался(ась) на вас",
                "actor_id": follower_id,
                "url": f"/u/{follower.username}",
                "resource_type": "user",
                "resource_id": follower.public_id,
            }
        ]
    )
    db.session.commit()
    return created


@celery_app.task(name="notifications.message", bind=True, max_retries=3)
def notify_new_message(self, message_id: int) -> int:  # type: ignore[no-untyped-def]
    from ..models.chat import Message

    message = db.session.get(Message, message_id)
    if message is None or message.status != "sent":
        return 0
    recipients = [
        member.user_id
        for member in message.conversation.members or []
        if member.user_id != message.sender_id and not member.left_at
    ]
    sender = message.sender
    created = bulk_create(
        [
            {
                "user_id": recipient,
                "kind": NotificationKind.MESSAGE,
                "title": f"{sender.name if sender else 'Кто-то'} отправил(а) вам сообщение",
                "body": message.body[:140],
                "actor_id": message.sender_id,
                "url": f"/chat/{message.conversation.public_id}",
                "resource_type": "conversation",
                "resource_id": message.conversation.public_id,
            }
            for recipient in recipients
        ]
    )
    db.session.commit()
    return created


@celery_app.task(name="notifications.purge_expired")
def purge_expired_notifications() -> int:  # type: ignore[no-untyped-def]
    """Retention job. Notifications are the least valuable data we hold about a
    user, so they expire first."""
    from datetime import timedelta

    from flask import current_app

    days = int(current_app.config.get("NOTIFICATION_RETENTION_DAYS", 90))
    cutoff = utcnow() - timedelta(days=days)
    result = db.session.execute(
        db.delete(Notification).where(Notification.created_at < cutoff)  # type: ignore[attr-defined]
    )
    db.session.commit()
    return int(result.rowcount or 0)


__all__ = [
    "bulk_create",
    "create",
    "enqueue_comment_notifications",
    "enqueue_reaction_notification",
    "list_notifications",
    "mark_all_read",
    "mark_read",
    "notify_follow",
    "notify_followers_of_post",
    "notify_new_message",
    "unread_count",
]

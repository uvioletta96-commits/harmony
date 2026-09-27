"""Threaded comments with depth limiting and subtree-aware counter maintenance."""

from __future__ import annotations

from collections.abc import Sequence

from flask import current_app
from sqlalchemy import Select, func, select, update

from ..extensions import db
from ..models.base import utcnow
from ..models.post import Comment, Post, PostStatus
from ..models.user import User
from ..security import spam
from ..security.content_moderation import get_engine as moderation_engine
from ..security.xss import sanitize_plain_text
from ..utils.logging import get_logger, log_event
from ..utils.pagination import Page, keyset_page
from ..utils.responses import ConflictError, NotFoundError, PermissionError_, ValidationError

logger = get_logger("harmony.service.comments")

MAX_COMMENT_CHARS = 2000


def get_comment(public_id: str, viewer: User | None = None) -> Comment:
    comment = db.session.scalar(select(Comment).where(Comment.public_id == public_id))
    if comment is None or comment.status == PostStatus.REMOVED.value:
        raise NotFoundError("Комментарий не найден.", code="comment_not_found")
    if viewer is None or not comment.post.is_visible_to(viewer):
        raise NotFoundError("Комментарий не найден.", code="comment_not_found")
    return comment


def list_comments(
    post: Post,
    viewer: User | None,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    sort: str = "top",
) -> Page:
    """Fetch a page of root comments, each carrying a small preview of replies.

    Replies are loaded with a single grouped query rather than N+1 per comment —
    on a post with 20 comments that is 1 query instead of 21, which is the
    difference between a fast and a slow thread view.
    """
    page_size = limit or int(current_app.config.get("COMMENT_PAGE_SIZE", 20))
    query = select(Comment).where(
        Comment.post_id == post.id,
        Comment.parent_id.is_(None),
        Comment.status != PostStatus.REMOVED.value,
    )
    if sort == "newest":
        page = keyset_page(
            query,
            model=Comment,
            page_size=page_size,
            cursor=cursor,
            descending=True,
            order_fields=("created_at", "id"),
        )
    else:
        page = _top_sorted_page(query, page_size=page_size, cursor=cursor)

    replies_map = _replies_for([comment.id for comment in page.items], viewer, per_thread=3)

    items = [comment.to_dict(viewer, replies=replies_map.get(comment.id, [])) for comment in page.items]
    return Page(items=items, next_cursor=page.next_cursor, has_more=page.has_more)


def _top_sorted_page(query: Select, *, page_size: int, cursor: str | None) -> Page:
    """Rank by (likes, created_at) using a window function.

    ``OFFSET`` alone would be wrong here: a highly-liked new comment pushes
    older ones off the first page permanently. Ranking first, then paging by
    rank, keeps the ordering stable under concurrent writes.
    """
    from ..utils.pagination import decode_cursor, encode_cursor

    ranked = query.add_columns(
        func.row_number()
        .over(order_by=(Comment.likes_count.desc(), Comment.created_at.asc(), Comment.id.asc()))
        .label("_rank")
    ).subquery()

    offset = 0
    if cursor:
        offset = max(0, int(decode_cursor(cursor).get("offset", 0)))

    stmt = select(ranked).order_by(ranked.c._rank).limit(page_size + 1).offset(offset)
    rows = list(db.session.execute(stmt).mappings().all())
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    items: list[Comment] = []
    for row in rows:
        entity = db.session.get(Comment, row["id"])
        if entity is not None:
            items.append(entity)

    next_cursor = encode_cursor({"offset": offset + page_size}) if has_more else None
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


def _replies_for(root_ids: Sequence[int], viewer: User | None, *, per_thread: int = 3) -> dict[int, list[Comment]]:
    """Preview a few replies per root comment, keyed by the root's id.

    Keyed on ``root_id`` rather than ``parent_id`` so the preview and
    ``/comments/<id>/replies`` show the *same* thread: a reply to a reply still
    belongs to the conversation the reader is looking at, and hiding it behind a
    collapsed second level makes a busy thread look empty.
    """
    if not root_ids:
        return {}
    query = (
        select(Comment)
        .where(Comment.root_id.in_(root_ids), Comment.status != PostStatus.REMOVED.value)
        .order_by(Comment.created_at.asc())
    )
    grouped: dict[int, list[Comment]] = {}
    extra: dict[int, int] = dict.fromkeys(root_ids, 0)
    for comment in db.session.execute(query).scalars():
        bucket = grouped.setdefault(comment.root_id, [])
        if len(bucket) < per_thread:
            bucket.append(comment)
        else:
            extra[comment.root_id] = extra.get(comment.root_id, 0) + 1
    # Signal "N more replies" without a second round-trip.
    for parent_id, bucket in grouped.items():
        if extra.get(parent_id):
            bucket[-1]._extra_replies = extra[parent_id]
    return grouped


def create_comment(post: Post, author: User, body: str, *, parent: Comment | None = None) -> Comment:
    if not post.is_visible_to(author):
        raise NotFoundError("Пост не найден.", code="post_not_found")

    clean_body = sanitize_plain_text(body or "", max_length=MAX_COMMENT_CHARS)
    if not clean_body:
        raise ValidationError(
            "Комментарий не может быть пустым.", code="empty_comment", fields={"body": "Введите текст комментария."}
        )

    if parent is not None:
        if parent.post_id != post.id:
            raise ValidationError("Родительский комментарий принадлежит другому посту.", code="parent_mismatch")
        max_depth = int(current_app.config.get("MAX_COMMENT_DEPTH", 3))
        if (parent.depth or 0) + 1 > max_depth:
            raise ValidationError(
                f"Максимальная глубина вложенности комментариев — {max_depth}.",
                code="max_depth_exceeded",
            )

    spam.guard_write(clean_body, author)
    decision = moderation_engine().screen(clean_body, context="comment")
    decision.raise_for_decision(kind="comment")

    # Thread root: a reply to a root comment belongs to that root, and a reply
    # to a reply keeps the same root. The guard has to wrap the whole
    # expression — ``parent.root_id or (parent.id if parent else None)``
    # dereferences ``parent`` before the None check.
    root_id = (parent.root_id or parent.id) if parent is not None else None
    comment = Comment(
        post_id=post.id,
        author_id=author.id,
        parent_id=parent.id if parent else None,
        root_id=root_id,
        depth=(parent.depth or 0) + 1 if parent else 0,
        body=clean_body,
        moderation_score=decision.score,
        status=PostStatus.PUBLISHED.value,
    )
    db.session.add(comment)
    db.session.flush()

    if parent is not None:
        parent.replies_count = (parent.replies_count or 0) + 1
    post.comments_count = (post.comments_count or 0) + 1
    post.author.comments_count = (post.author.comments_count or 0) + 1
    db.session.commit()

    _invalidate(post, author)
    _notify(post, comment, parent, author)
    log_event(
        logger, "INFO", "comment.created", user_id=author.id, post_id=post.public_id, comment_id=comment.public_id
    )
    return comment


def update_comment(comment: Comment, actor: User, body: str) -> Comment:
    if comment.author_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Вы можете редактировать только свои комментарии.", code="not_comment_owner")
    clean_body = sanitize_plain_text(body or "", max_length=MAX_COMMENT_CHARS)
    if not clean_body:
        raise ValidationError("Комментарий не может быть пустым.", code="empty_comment")

    decision = moderation_engine().screen(clean_body, context="comment")
    decision.raise_for_decision(kind="comment_update")
    comment.body = clean_body
    comment.edited_at = utcnow()
    db.session.commit()
    _invalidate(comment.post, actor)
    return comment


def delete_comment(comment: Comment, actor: User) -> None:
    """Soft-delete, keeping the thread shape so replies are not orphaned."""
    if comment.author_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Вы можете удалять только свои комментарии.", code="not_comment_owner")

    was_visible = comment.status == PostStatus.PUBLISHED.value
    comment.status = PostStatus.REMOVED.value
    comment.deleted_at = utcnow()
    comment.body = ""

    if was_visible:
        post = comment.post
        post.comments_count = max(0, (post.comments_count or 1) - 1)
        comment.author.comments_count = max(0, (comment.author.comments_count or 1) - 1)
        if comment.parent is not None:
            comment.parent.replies_count = max(0, (comment.parent.replies_count or 1) - 1)
    db.session.commit()
    _invalidate(comment.post, actor)
    log_event(logger, "INFO", "comment.deleted", user_id=actor.id, comment_id=comment.public_id)


def restore_comment(comment: Comment, actor: User) -> Comment:
    if comment.author_id != actor.id and not actor.is_moderator:
        raise PermissionError_("Недостаточно прав.", code="not_comment_owner")
    if comment.status != PostStatus.REMOVED.value:
        raise ConflictError("Комментарий не был удалён.", code="comment_not_removed")
    # The body was cleared on delete, so a restore can only make it visible as
    # a tombstone; the author must repost.
    comment.status = PostStatus.ARCHIVED.value
    db.session.commit()
    return comment


def recount_thread(post_id: int) -> None:
    """Recompute comment counters for a post after a bulk action."""
    total = (
        db.session.scalar(
            select(func.count(Comment.id)).where(
                Comment.post_id == post_id, Comment.status == PostStatus.PUBLISHED.value
            )
        )
        or 0
    )
    db.session.execute(update(Post).where(Post.id == post_id).values(comments_count=total))


def _invalidate(post: Post | None, actor: User | None) -> None:
    from . import cache_service

    if post is not None:
        cache_service.invalidate_posts(post.public_id, post.author_id, post.author.public_id if post.author else None)
    if actor is not None:
        cache_service.invalidate_user(actor.public_id, actor.id)


def _notify(post: Post, comment: Comment, parent: Comment | None, author: User) -> None:
    """Tell the people involved. Best-effort, and never blocking the request."""
    from ..tasks.dispatch import enqueue
    from .notification_service import enqueue_comment_notifications

    enqueue(enqueue_comment_notifications, post.id, comment.id)


__all__ = [
    "MAX_COMMENT_CHARS",
    "create_comment",
    "delete_comment",
    "get_comment",
    "list_comments",
    "recount_thread",
    "restore_comment",
    "update_comment",
]

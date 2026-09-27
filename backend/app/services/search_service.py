"""Search over users and posts.

Implementation notes:

* PostgreSQL full-text search (``tsvector``) is used when the dialect supports
  it, with a ``LIKE`` fallback so the same code runs on SQLite in tests.
* Queries are always bounded, parameterised and executed with a statement
  timeout; a user-supplied search term never reaches SQL as text.
* Minimum query length and per-IP rate limits keep this from becoming a cheap
  way to enumerate the user directory.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import Select, func, or_, select

from ..extensions import db
from ..models.post import Post, PostStatus
from ..models.user import ProfileVisibility, User, UserStatus
from ..security.xss import sanitize_plain_text
from ..utils.logging import get_logger
from ..utils.pagination import Page, clamp_page_size
from ..utils.responses import ValidationError

logger = get_logger("harmony.service.search")

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 80

USERNAME_HIGHLIGHT_FIELDS = ("username", "display_name")


def normalise_query(raw: str) -> str:
    """Trim, collapse whitespace and bound the length before any SQL runs."""
    text = sanitize_plain_text(raw or "", max_length=MAX_QUERY_LENGTH)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _validate(raw: str) -> str:
    text = normalise_query(raw)
    if len(text) < MIN_QUERY_LENGTH:
        raise ValidationError(
            f"Введите не менее {MIN_QUERY_LENGTH} символов для поиска.",
            code="query_too_short",
            fields={"q": "Слишком короткий запрос."},
        )
    return text


def _is_postgres() -> bool:
    return db.session.get_bind().dialect.name == "postgresql"


def _like_pattern(term: str) -> str:
    """Escape LIKE wildcards so a user cannot craft an unbounded match."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_users(
    viewer: User | None,
    raw_query: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> Page:
    """Find users by username, display name or bio.

    Results are filtered by the *target's* discoverability settings, not the
    viewer's: a user who opts out of search is not returned to anyone.
    """
    term = _validate(raw_query)
    limit = clamp_page_size(limit, default=20, maximum=50)
    pattern = _like_pattern(term)

    query = (
        select(User)
        .where(
            User.status == UserStatus.ACTIVE.value,
            or_(
                User.username.ilike(pattern, escape="\\"),
                User.display_name.ilike(pattern, escape="\\"),
                User.bio.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(User.followers_count.desc(), User.username.asc())
    )

    query = _apply_discoverability(query)
    if viewer is not None:
        query = query.where(User.id != viewer.id)

    total = db.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(query.limit(limit).offset(max(0, offset)).scalars().all())
    items = [row.to_public_dict(viewer) for row in rows]
    return Page(items=items, next_cursor=None, has_more=offset + len(rows) < total, total=total)


def _apply_discoverability(query: Select) -> Select:
    """Exclude users who are private, or who opted out of search discovery.

    A user with no ``privacy_settings`` row at all is treated as opted-in, since
    the row is created lazily and its absence must not make someone unfindable.
    """
    privacy = db.metadata.tables["privacy_settings"]
    has_row = select(privacy.c.user_id).where(privacy.c.user_id == User.id).exists()
    opted_in = (
        select(privacy.c.user_id)
        .where(privacy.c.user_id == User.id, privacy.c.discoverable_by_search.is_(True))
        .exists()
    )
    return query.where(
        User.profile_visibility == ProfileVisibility.PUBLIC.value,
        or_(opted_in, ~has_row),
    )


def search_posts(
    viewer: User | None,
    raw_query: str,
    *,
    limit: int = 15,
    cursor: str | None = None,
) -> Page:
    """Full-text post search with the same visibility rules as the feed."""
    from .post_service import serialise_posts, visible_posts_query

    term = _validate(raw_query)
    limit = clamp_page_size(limit, default=15, maximum=50)
    base = visible_posts_query(viewer)

    if _is_postgres():
        from sqlalchemy.dialects.postgresql import plainto_tsquery  # type: ignore

        base = base.where(func.to_tsvector("russian", Post.body).op("@@")(plainto_tsquery("russian", term))).order_by(
            func.ts_rank(func.to_tsvector("russian", Post.body), plainto_tsquery("russian", term)).desc()
        )
    else:
        base = base.where(Post.body.ilike(_like_pattern(term), escape="\\"))

    from ..utils.pagination import keyset_page

    page = keyset_page(
        base, model=Post, page_size=limit, cursor=cursor, descending=True, order_fields=("created_at", "id")
    )
    return Page(items=serialise_posts(page.items, viewer), next_cursor=page.next_cursor, has_more=page.has_more)


def discover(viewer: User | None, *, limit: int = 10) -> list[dict[str, Any]]:
    """Default landing content: a light mix of newest posts and active users."""
    from .post_service import get_feed
    from .user_service import suggestions_for

    feed = get_feed(viewer, mode="latest", limit=limit)
    users = suggestions_for(viewer, limit=limit) if viewer else []
    return {"posts": feed.items, "people": users}


def trending_tags(limit: int = 8) -> list[dict[str, Any]]:
    """Derive popular hashtags from recent published posts.

    Computed in Python over a bounded recent slice rather than a correlated
    subquery: the working set is small and fixed, and the result is cached.
    """
    from ..services.cache_service import _key
    from .cache_service import get_or_set

    def _produce() -> list[dict[str, Any]]:
        from ..security.xss import _HASHTAG_RE

        recent = (
            db.session.query(Post.body)
            .filter(Post.status == PostStatus.PUBLISHED.value, Post.created_at >= _days_ago(7))
            .limit(500)
            .all()
        )
        counts: dict[str, int] = {}
        for (body,) in recent:
            for match in _HASHTAG_RE.finditer(body or ""):
                tag = match.group(1).lower()
                counts[tag] = counts.get(tag, 0) + 1
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        ]

    return get_or_set(_key("search", "trending", limit), _produce, ttl=300)


def _days_ago(days: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from ..models.base import utcnow

    return utcnow() - timedelta(days=days)


__all__ = [
    "MIN_QUERY_LENGTH",
    "discover",
    "normalise_query",
    "search_posts",
    "search_users",
    "trending_tags",
]

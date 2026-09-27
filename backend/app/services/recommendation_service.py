"""Personalised and shuffled feed ordering.

The chronological feed is not a good default for a social network: on a small
instance it hands every reader the same page in the same order, which makes the
product feel like an archive rather than a place. This module produces the two
orderings that fix that, and keeps both of them paginated correctly.

**Why not ``ORDER BY random()``.** It is the obvious spelling and it is wrong
twice over. It re-draws on every query, so page two is a fresh lottery and the
reader sees the same post three times and never sees the rest. And it forces a
full scan plus a sort on every request, which is exactly the cost the indexes
on ``posts`` exist to avoid.

**What this does instead.** A traversal takes a *snapshot* of the candidate
pool, pins a random seed, and orders that pool once. The cursor carries the
seed, the offset and the snapshot's watermark, so every page of one scroll sees
the same order and the same set. Starting a new scroll re-draws both.

**Why the ordering is done in Python.** The tiebreak has to be a hash of
``seed:post_id`` that is identical on every page and on every database.
PostgreSQL has ``md5``; SQLite does not, and the tests run on SQLite, so a SQL
ordering would mean two implementations of the same idea - one of which would
only ever be exercised in production. The pool is bounded by
``FEED_SHUFFLE_POOL``, so this is a sort of a few hundred integers rather than a
scan of the table.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import Select, and_, or_, select

from ..extensions import db
from ..models.post import Comment, Post, Reaction, ReactionType
from ..models.user import PrivacySetting, Relationship, User
from ..utils.crypto import UTC, utcnow
from ..utils.logging import get_logger
from ..utils.pagination import _parse_timestamp, decode_cursor, encode_cursor

logger = get_logger("harmony.service.recommend")

#: Score weights. Chosen so an explicit follow outweighs accumulated passive
#: interest, which outweighs raw engagement - the ordering a reader is asking
#: for is "people I chose, then things I reacted to, then things that are simply
#: popular", and one like should not outrank a follow.
WEIGHT_FOLLOW = 100.0
WEIGHT_LIKED_AUTHOR = 30.0
WEIGHT_COMMENTED_AUTHOR = 18.0
WEIGHT_ENGAGEMENT = 0.5
#: Multiplier turning the 0..1 freshness term into the same units as the rest.
WEIGHT_FRESHNESS = 25.0

#: Scores are rounded down to a bucket before the random tiebreak, so posts
#: close enough to tie *do* tie and get shuffled between themselves. Without
#: this the "random" feed is a strict ranking with jitter, and a reader
#: scrolling far enough down watches the ranking reassert itself.
SCORE_BUCKET = 4.0

#: A half-life over 36 hours, roughly one news cycle. Three days ago is not
#: "stale news", it is just old, and the curve should be well past that by then.
FRESHNESS_HALF_LIFE = timedelta(hours=36)

#: Bits of randomness in a traversal seed. 63 rather than 64 so the value is
#: always a positive int on any platform and survives a JSON round-trip.
SEED_BITS = 63


def _seeded_rank(seed: int, post_id: int) -> int:
    """A stable pseudo-random position for one post under one seed.

    blake2b rather than the built-in ``hash()``, which is salted per process:
    the same seed would give a different order on the next request, which is
    precisely the bug this module exists to avoid.
    """
    digest = hashlib.blake2b(f"{seed}:{post_id}".encode("ascii"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _affinity(viewer: User | None) -> dict[int, float]:
    """Score every author the viewer has shown an interest in.

    Built from three signals the database already holds, all of them explicit
    actions rather than inferences: who they follow, whose posts they liked and
    whose posts they replied to. Nothing is tracked passively, which is what
    lets the result be offered honestly under a privacy promise.
    """
    if viewer is None:
        return {}

    scores: dict[int, float] = {}

    def bump(author_id: int | None, amount: float) -> None:
        # A reader's own posts are excluded: affinity is about who to show, and
        # the reader already knows what they wrote.
        if author_id is not None and author_id != viewer.id:
            scores[author_id] = scores.get(author_id, 0.0) + amount

    for (author_id,) in db.session.execute(
        select(Relationship.followee_id).where(Relationship.follower_id == viewer.id)
    ):
        bump(author_id, WEIGHT_FOLLOW)

    for (author_id,) in db.session.execute(
        select(Post.author_id)
        .join(Reaction, Reaction.post_id == Post.id)
        .where(Reaction.user_id == viewer.id, Reaction.type == ReactionType.LIKE.value)
    ):
        bump(author_id, WEIGHT_LIKED_AUTHOR)

    for (author_id,) in db.session.execute(
        select(Post.author_id).join(Comment, Comment.post_id == Post.id).where(Comment.author_id == viewer.id)
    ):
        bump(author_id, WEIGHT_COMMENTED_AUTHOR)

    return scores


def _freshness(created_at: datetime, now: datetime) -> float:
    """Exponential decay: 1.0 at publication, 0.5 after one half-life."""
    age = max(0.0, (now - _aware(created_at)).total_seconds())
    return 0.5 ** (age / FRESHNESS_HALF_LIFE.total_seconds())


def _relevance(post: Post, affinity: dict[int, float], now: datetime) -> float:
    score = affinity.get(post.author_id, 0.0)
    if post.author_id not in affinity:
        # Engagement is a weak signal, and it must not let a stranger outrank a
        # different stranger the reader already reacted to.
        score += WEIGHT_ENGAGEMENT * min(20, len(post.reactions or []))
    return score + WEIGHT_FRESHNESS * _freshness(post.created_at, now)


def _order(
    rows: Sequence[Post],
    seed: int,
    affinity: dict[int, float] | None,
    now: datetime,
) -> list[Post]:
    """Sort the pool: by relevance bucket when personalising, else purely at random."""
    if affinity is None:
        return sorted(rows, key=lambda post: _seeded_rank(seed, post.id))

    def sort_key(post: Post) -> tuple[int, int]:
        bucket = int(_relevance(post, affinity, now) // SCORE_BUCKET)
        return (-bucket, _seeded_rank(seed, post.id))

    return sorted(rows, key=sort_key)


def _traversal_state(cursor: str | None) -> tuple[int, int, datetime | None, int]:
    """Recover ``(seed, offset, watermark, watermark_id)`` or start a traversal."""
    if not cursor:
        return secrets.randbits(SEED_BITS), 0, None, 0
    try:
        state = decode_cursor(cursor)
        return (
            int(state["s"]),
            int(state.get("o", 0)),
            _parse_timestamp(state["w"]),
            int(state.get("w_id", 0)),
        )
    except (KeyError, TypeError, ValueError):
        # A cursor from a different traversal shape is not worth a 422. Starting
        # the feed over is what the reader wanted in the first place.
        logger.info("feed.cursor_unusable", extra={"event": "feed.cursor_unusable"})
        return secrets.randbits(SEED_BITS), 0, None, 0


def shuffled_page(
    base_query: Select,
    viewer: User | None,
    *,
    cursor: str | None,
    limit: int,
    personalised: bool,
) -> tuple[list[Post], str | None, bool]:
    """Return one page of a shuffled - optionally personalised - feed.

    :param personalised: rank by the viewer's interests first, then shuffle
        within each score bucket. When false the pool is shuffled outright.
    :returns: ``(posts, next_cursor, has_more)``
    """
    seed, offset, watermark, watermark_id = _traversal_state(cursor)
    now = utcnow()

    query = base_query
    if watermark is not None:
        # The watermark pins the snapshot. Without it, a post published midway
        # through a scroll shifts every later row down by one and the reader
        # sees the tail of the previous page a second time.
        query = query.where(
            or_(
                Post.created_at < watermark,
                and_(Post.created_at == watermark, Post.id <= watermark_id),
            )
        )

    pool_size = int(current_app.config.get("FEED_SHUFFLE_POOL", 150))
    rows = list(db.session.scalars(query.limit(pool_size)).unique().all())
    if not rows:
        return [], None, False

    affinity = _affinity(viewer) if personalised else None
    window = _order(rows, seed, affinity, now)

    page = window[offset : offset + limit]
    has_more = len(window) > offset + limit
    if not has_more:
        return page, None, False

    newest = max(rows, key=lambda post: (post.created_at, post.id))
    next_cursor = encode_cursor(
        {
            "s": seed,
            "o": offset + limit,
            "w": newest.created_at.isoformat(),
            "w_id": newest.id,
            "m": "personal" if personalised else "shuffle",
        }
    )
    return page, next_cursor, True


def should_personalise(viewer: User | None) -> bool:
    """Honour the privacy switch before ranking anything.

    A reader who turned personalisation off gets the plain shuffled feed. The
    decision is made here rather than at the call site so there is exactly one
    place where it can be got wrong.
    """
    if viewer is None:
        return False
    # By ``user_id``, not by primary key: the two coincide for the very first
    # account and for no other, so ``session.get`` here would appear to work in
    # development and silently return the default for every other user.
    settings = db.session.scalar(select(PrivacySetting).where(PrivacySetting.user_id == viewer.id))
    if settings is None:
        return True
    return bool(settings.personalize_feed)


__all__ = ["SEED_BITS", "should_personalise", "shuffled_page"]

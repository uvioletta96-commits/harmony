"""Redis caching with explicit invalidation.

Two rules keep this from becoming a source of stale or wrong data:

1. **Only immutable or clearly-scoped data is cached.** A cached post payload is
   keyed by id *and* invalidated on every write to that post; nothing is cached
   under a key that includes "now".
2. **Fails open.** A cache outage must never fail a read. Every helper degrades
   to calling through, and the miss is counted, not raised.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Callable
from typing import Any

from flask import current_app

from ..extensions import get_redis
from ..utils.logging import get_logger
from ..utils.metrics import cache_operations_total

logger = get_logger("harmony.cache")

NAMESPACE = "harmony"
DEFAULT_TTL = 60


def _enabled() -> bool:
    try:
        return bool(current_app.config.get("CACHE_ENABLED", True))
    except RuntimeError:
        return False


def _key(*parts: Any) -> str:
    """Build a cache key as ``harmony:<tag>:<digest>``.

    The leading ``<tag>`` is the first argument (e.g. ``feed``, ``post``) and
    stays human-readable, so a whole category can be invalidated with a SCAN
    pattern even though the identifying part is hashed.
    """
    tag = str(parts[0]) if parts else "misc"
    raw = ":".join(str(part) for part in parts)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()
    return f"{NAMESPACE}:{tag}:{digest}"


def get(key: str, default: Any = None) -> Any:
    if not _enabled():
        return default
    client = get_redis()
    if client is None:
        return default
    try:
        raw = client.get(key)
    except Exception as exc:
        cache_operations_total.labels(op="get", result="error").inc()
        logger.debug("cache get failed: %s", exc)
        return default
    if raw is None:
        cache_operations_total.labels(op="get", result="miss").inc()
        return default
    try:
        cache_operations_total.labels(op="get", result="hit").inc()
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def set(key: str, value: Any, ttl: int | None = None) -> None:  # noqa: A001
    if not _enabled():
        return
    client = get_redis()
    if client is None:
        return
    try:
        client.setex(
            key, int(ttl or current_app.config.get("CACHE_DEFAULT_TTL", DEFAULT_TTL)), json.dumps(value, default=str)
        )
        cache_operations_total.labels(op="set", result="ok").inc()
    except Exception as exc:
        cache_operations_total.labels(op="set", result="error").inc()
        logger.debug("cache set failed: %s", exc)


def delete(*keys: str) -> int:
    if not _enabled() or not keys:
        return 0
    client = get_redis()
    if client is None:
        return 0
    try:
        removed = client.delete(*keys)
        cache_operations_total.labels(op="delete", result="ok").inc()
        return int(removed)
    except Exception as exc:
        cache_operations_total.labels(op="delete", result="error").inc()
        logger.debug("cache delete failed: %s", exc)
        return 0


def delete_pattern(pattern: str) -> int:
    """Invalidate by pattern using SCAN (never KEYS, which blocks Redis)."""
    if not _enabled():
        return 0
    client = get_redis()
    if client is None:
        return 0
    removed = 0
    try:
        for key in client.scan_iter(match=pattern, count=500):
            removed += client.delete(key)
    except Exception as exc:
        logger.debug("cache scan failed: %s", exc)
    return removed


def get_or_set(key: str, producer: Callable[[], Any], ttl: int | None = None) -> Any:
    """Read-through cache. Exceptions inside ``producer`` propagate untouched."""
    cached = get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    value = producer()
    set(key, value, ttl)
    return value


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


_MISSING = _Missing()


def cached(
    prefix: str, ttl: int | None = None, *, key_builder: Callable[..., str] | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Memoise a function's return value in Redis.

    Only appropriate for pure functions of its arguments — for anything that
    reads mutable state, prefer an explicit ``get_or_set`` at the call site so
    the invalidation is visible in the code that causes the mutation.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _enabled():
                return func(*args, **kwargs)
            key = _key(prefix, key_builder(*args, **kwargs) if key_builder else f"{args}:{sorted(kwargs.items())}")
            cached_value = get(key, _MISSING)
            if cached_value is not _MISSING:
                return cached_value
            value = func(*args, **kwargs)
            set(key, value, ttl)
            return value

        wrapper.cache_prefix = prefix  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Invalidation helpers
# ---------------------------------------------------------------------------


def post_keys(post_public_id: str) -> list[str]:
    return [_key("post", post_public_id)]


def user_keys(user_public_id: str) -> list[str]:
    return [_key("user", user_public_id), _key("user-stats", user_public_id)]


def feed_keys(user_id: int | None = None) -> list[str]:
    keys = [_key("feed", "global"), _key("feed", "recent")]
    if user_id:
        keys.append(_key("feed", "personal", user_id))
    return keys


def invalidate_posts(post_public_id: str, author_id: int | None = None, author_public_id: str | None = None) -> None:
    """Drop a post, its author's stats and every feed page that may embed it.

    Feed pages are cached under cursor-derived keys, so the exact set is not
    known; rather than guessing we drop the feed namespace prefix, which is
    small and cheap to rebuild.
    """
    keys = post_keys(post_public_id) + feed_keys(author_id)
    if author_public_id:
        keys.extend(user_keys(author_public_id))
    delete(*keys)
    delete_pattern(f"{NAMESPACE}:feed:*")


def invalidate_user(user_public_id: str, user_id: int | None = None) -> None:
    delete(*user_keys(user_public_id), *feed_keys(user_id))


def invalidate_feed() -> None:
    delete(*feed_keys())


def rate_counter_add(key: str, amount: int = 1, ttl: int = 3600) -> int:  # pragma: no cover
    client = get_redis()
    if client is None:
        return 0
    try:
        pipe = client.pipeline()
        pipe.incrby(key, amount)
        pipe.expire(key, ttl)
        return int(pipe.execute()[0])
    except Exception:
        return 0


def rate_counter_set_once(key: str, ttl: int = 3600) -> bool:
    """Claim a key for an account. True if this call was the one that set it.

    For deduplicating a counter that must not be inflated by one reader acting
    twice. `SET key 1 NX EX ttl` rather than a get followed by a set: two requests
    from two tabs interleave, and both would see "not seen" and both count.
    """
    client = get_redis()
    if client is None:
        # Without Redis there is nowhere to record the claim, so every call counts.
        # Over-counting is the better failure than a view count frozen at zero.
        return True
    try:
        return bool(client.set(key, 1, nx=True, ex=ttl))
    except Exception:
        return True


def counters_read(keys: list[str]) -> dict[str, int]:
    """Read several counters in one round trip.

    A pipeline rather than a loop: the vertical feed wants a view count per post,
    and eight posts means eight round trips to a server this box also has to run
    PostgreSQL and the app on. Returns only the keys that exist - a missing counter
    is a post nobody has watched yet, which is not an error.
    """
    client = get_redis()
    if client is None or not keys:
        return {}
    try:
        pipe = client.pipeline()
        for key in keys:
            pipe.get(key)
        values = pipe.execute()
    except Exception:
        return {}
    return {
        key: int(value)
        for key, value in zip(keys, values, strict=True)
        if value is not None
    }


__all__ = [
    "NAMESPACE",
    "cached",
    "counters_read",
    "delete",
    "delete_pattern",
    "feed_keys",
    "get",
    "get_or_set",
    "invalidate_feed",
    "invalidate_posts",
    "invalidate_user",
    "post_keys",
    "rate_counter_set_once",
    "set",
    "user_keys",
]

"""Redis-backed rate limiting with an in-memory fallback.

Algorithm: a **sliding window log** per (route scope, subject). Each hit is a
member in a sorted set timestamped in milliseconds; a Lua script prunes expired
entries, counts the remainder and appends the new hit atomically. A fixed-window
counter would be cheaper but lets an attacker send 2x the limit across a window
boundary; the sorted set costs one O(log n) op and does not have that hole.

Keys embed a keyed hash of the subject rather than the raw value, so keys are
stable but not reversible from a Redis dump.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import Any

from flask import current_app, g, has_request_context, request

from ..utils.logging import get_logger, log_event
from ..utils.responses import RateLimitError

logger = get_logger("harmony.ratelimit")

#: Stand-in address for work that has no request behind it (CLI, Celery, tests).
#: Constant on purpose: a fixed subject must never be able to exhaust a bucket
#: that real clients share.
NO_IP = "0.0.0.0"  # noqa: S104 - a sentinel value, not a bind address

# KEYS[1] = sorted set for this scope/subject
# ARGV[1] = now (ms), ARGV[2] = window (ms), ARGV[3] = limit, ARGV[4] = member id
_SLIDING_WINDOW_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local win   = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now - win

redis.call('ZREMRANGEBYSCORE', key, 0, cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = win
  if oldest[2] then
    retry = (tonumber(oldest[2]) + win) - now
  end
  return {0, limit - count, retry, 0}
end

redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, win)
return {1, limit - count - 1, 0, 1}
"""

WINDOW_SECONDS = {
    "second": 1,
    "sec": 1,
    "s": 1,
    "minute": 60,
    "min": 60,
    "m": 60,
    "hour": 3600,
    "h": 3600,
    "hourly": 3600,
    "day": 86400,
    "d": 86400,
    "daily": 86400,
    "week": 604800,
    "w": 604800,
    "month": 2592000,
}


@dataclass(frozen=True)
class Limit:
    """One ``count per period`` rule."""

    count: int
    window: int  # seconds

    def __str__(self) -> str:
        for name, seconds in sorted(WINDOW_SECONDS.items(), key=lambda kv: kv[1]):
            if seconds == self.window:
                return f"{self.count}/{name}"
        return f"{self.count}/{self.window}s"


def parse_limit(value: str | None, *, default: str = "60/minute") -> list[Limit]:
    """Parse ``"5/hour;20/day"`` into a list of windows (all must pass)."""
    from ..utils.responses import ValidationError

    raw = (value or default).strip()
    if not raw:
        return []
    limits: list[Limit] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        count_raw, _, period = chunk.partition("/")
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise ValidationError(f"Некорректный лимит: {chunk!r}") from exc
        if count <= 0:
            continue
        if period in WINDOW_SECONDS:
            window = WINDOW_SECONDS[period]
        elif period.isdigit():
            window = int(period)
        else:
            raise ValidationError(f"Некорректный период в лимите: {period!r}")
        limits.append(Limit(count=count, window=window))
    return limits


@dataclass
class Verdict:
    allowed: bool
    limit: str = ""
    remaining: int = 0
    retry_after: int = 0
    scope: str = ""
    degraded: bool = False  # True when served by the in-memory fallback

    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.limit:
            out["X-RateLimit-Limit"] = self.limit
        out["X-RateLimit-Remaining"] = str(max(0, self.remaining))
        if not self.allowed:
            out["Retry-After"] = str(self.retry_after)
        return out


class _MemoryBackend:
    """Process-local fallback used when Redis is unavailable.

    Correct per worker process, not across the fleet — good enough to keep a
    single-node dev/test instance protected, and it makes the dependency
    optional rather than fatal.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, window: int, limit: int, member: str) -> tuple[bool, int, int]:
        now = time.time()
        cutoff = now - window
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry = int(bucket[0] + window - now) + 1
                return False, max(0, limit - len(bucket)), max(1, retry)
            bucket.append(now)
            remaining = max(0, limit - len(bucket))
            if len(bucket) > 10_000:  # bound memory under flood
                del self._buckets[key]
            return True, remaining, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class RateLimiter:
    """Facade combining the Redis and in-memory backends."""

    def __init__(self) -> None:
        self._memory = _MemoryBackend()
        self._script_sha: str | None = None
        self._warned = False

    # -- backend selection ------------------------------------------------
    def _client(self):  # type: ignore[no-untyped-def]
        from ..extensions import get_redis

        return get_redis()

    @property
    def enabled(self) -> bool:
        return bool(current_app.config.get("RATE_LIMIT_ENABLED", True))

    def limits_for(self, scope: str) -> list[Limit]:
        table: dict[str, str] = current_app.config.get("RATE_LIMITS", {})
        return parse_limit(table.get(scope) or current_app.config.get("RATE_LIMIT_DEFAULT"))

    def check(self, scope: str, subject: str, limits: Sequence[Limit] | None = None) -> Verdict:
        """Evaluate every window for ``subject``; the tightest verdict wins."""
        if not self.enabled:
            return Verdict(allowed=True, scope=scope)

        rules = list(limits if limits is not None else self.limits_for(scope))
        if not rules:
            return Verdict(allowed=True, scope=scope)

        client = self._client()
        allowed, tightest_remaining, retry_after, limit_label = True, None, 0, ""
        for rule in rules:
            key = self._key(scope, subject, rule.window)
            if client is None:
                ok, remaining, retry = self._memory.hit(key, rule.window, rule.count, uuid.uuid4().hex)
            else:
                ok, remaining, retry, _hit_shared = self._redis_hit(client, key, rule.window, rule.count)
            if not self._warned and client is None and scope != "internal":
                self._warned = True
                log_event(logger, "WARNING", "ratelimit.degraded", scope=scope)
            if retry_after == 0 or (retry > 0 and retry_after < retry):
                retry_after = max(retry, 0)
            limit_label = f"{limit_label}, " if limit_label else ""
            limit_label += f"{rule.count}/{rule.window}s"
            if tightest_remaining is None or remaining < tightest_remaining:
                tightest_remaining = remaining
            allowed = allowed and ok

        return Verdict(
            allowed=allowed,
            limit=limit_label,
            remaining=int(tightest_remaining or 0),
            retry_after=retry_after,
            scope=scope,
            degraded=client is None,
        )

    def _redis_hit(self, client, key: str, window: int, limit: int) -> tuple[bool, int, int, bool]:  # type: ignore[no-untyped-def]
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}:{uuid.uuid4().hex[:8]}"
        try:
            if self._script_sha is None:
                self._script_sha = client.script_load(_SLIDING_WINDOW_LUA)
            result = client.evalsha(self._script_sha, 1, key, now_ms, window * 1000, limit, member)
        except Exception as exc:
            # NOSCRIPT after a Redis restart, or a connection blip: degrade to
            # memory rather than failing the request. A rate limiter that causes
            # an outage is worse than one that is briefly per-process.
            log_event(logger, "WARNING", "ratelimit.redis_error", key=key, error=type(exc).__name__)
            ok, remaining, retry = self._memory.hit(key, window, limit, member)
            return ok, remaining, retry, True
        allowed, remaining, retry_ms, _ = (int(v) for v in result)
        return bool(allowed), remaining, int(retry_ms / 1000) + 1 if retry_ms else 0, False

    def _key(self, scope: str, subject: str, window: int) -> str:
        from ..utils.crypto import keyed_hash

        return f"rl:{scope}:{window}:{keyed_hash(subject, length=16)}"

    def reset(self, scope: str | None = None, subject: str | None = None) -> None:
        """Test/ops helper: drop counters for a scope (optionally a subject)."""
        from ..utils.crypto import keyed_hash

        client = self._client()
        if client is None:
            self._memory.reset()
            return
        pattern = f"rl:{scope}:*:{keyed_hash(subject, length=16)}" if subject else f"rl:{scope}:*"
        for key in client.scan_iter(match=pattern, count=500):
            client.delete(key)


limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Subject resolution
# ---------------------------------------------------------------------------


def client_ip() -> str:
    """Resolve the client address, honouring exactly N trusted proxy hops.

    Trusting ``X-Forwarded-For`` unconditionally would let any caller spoof
    their IP and bypass IP-based limits, so the header is only read when the
    request arrived over a configured number of proxies.

    Also callable outside a request - CLI commands, Celery tasks and tests reach
    the spam scorer directly - and resolves to a constant there rather than
    raising, so background work never has to fake a request.
    """
    if not has_request_context():
        return NO_IP
    if not current_app.config.get("RATE_LIMIT_TRUST_PROXY", True):
        return request.remote_addr or NO_IP
    hops = int(current_app.config.get("TRUSTED_PROXY_COUNT", 1))
    if hops <= 0:
        return request.remote_addr or NO_IP
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            index = max(0, len(parts) - hops)
            return parts[index]
    return request.remote_addr or NO_IP


def current_subject() -> str:
    """Rate-limit subject: authenticated user id when known, else IP."""
    if not has_request_context():
        return f"ip:{NO_IP}"
    user = getattr(g, "current_user", None)
    if user is not None and getattr(user, "id", None):
        return f"user:{user.id}"
    return f"ip:{client_ip()}"


def enforce(scope: str, *, subject: str | None = None, limits: Sequence[Limit] | None = None) -> Verdict:
    """Check a limit and raise :class:`RateLimitError` when exceeded."""
    verdict = limiter.check(scope, subject or current_subject(), limits)
    if not verdict.allowed:
        from ..utils.metrics import rate_limit_hits_total

        rate_limit_hits_total.labels(scope=scope).inc()
        log_event(
            logger,
            "INFO",
            "ratelimit.exceeded",
            scope=scope,
            limit=verdict.limit,
            subject_kind="user" if subject is None else "explicit",
        )
        raise RateLimitError(retry_after=verdict.retry_after, limit=verdict.limit)
    if hasattr(g, "_rate_limit_headers"):
        g._rate_limit_headers = verdict.headers()  # type: ignore[attr-defined]
    return verdict


def rate_limit(
    scope: str | Callable[[Any], str] | None = None,
    *,
    key: Callable[[Any], str] | None = None,
    limits: Sequence[Limit] | str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator form of :func:`enforce`.

    ``scope`` may be a callable so a single rule can be reused with a dynamic
    bucket (e.g. per-IP uploads versus per-user uploads).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        resolved_limits = parse_limit(limits) if isinstance(limits, str) else limits

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            active_scope = scope() if callable(scope) else (scope or "global")
            if key is not None:
                subject = key(*args, **kwargs)
            else:
                subject = current_subject()
            enforce(active_scope, subject=subject, limits=resolved_limits)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def headers_from_g() -> dict[str, str]:
    return dict(getattr(g, "_rate_limit_headers", {}) or {})


__all__ = [
    "Limit",
    "RateLimiter",
    "Verdict",
    "client_ip",
    "current_subject",
    "enforce",
    "headers_from_g",
    "limiter",
    "parse_limit",
    "rate_limit",
]

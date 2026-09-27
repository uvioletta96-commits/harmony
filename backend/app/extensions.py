"""Shared extension singletons.

Extensions are instantiated without an application here and bound to the app
in ``create_app``. Importing this module is cheap and side-effect free, which
keeps unit tests and Celery workers free of circular imports.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import bcrypt
import redis as redis_lib
from celery import Celery
from flask import current_app
from flask_cors import CORS
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# Deterministic constraint names keep Alembic autogenerate diffs stable across
# databases and make it possible to drop constraints by name in migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


@event.listens_for(Engine, "connect")
def _sqlite_enforce_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """Make SQLite honour ``ON DELETE CASCADE``, which it ignores by default.

    SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys`` is turned
    on, and it defaults to off. Every cascade in this schema is declared but, on
    the development and test databases, inert - so deleting a user left a row
    behind in every child table.

    That is not cosmetic. The orphan then collides with the next account: the
    ``users.id`` sequence hands the id straight back, ``privacy_settings.user_id``
    is unique, and registration dies with an ``IntegrityError`` that surfaces as
    a 500 on a form the reader filled in correctly. It also means the GDPR
    account-deletion path leaves personal data in the database while reporting
    that it removed it, which is the opposite of what it promises.

    The pragma is per-connection, so it has to be set on every connect rather
    than once at startup - a pooled connection would otherwise silently go back
    to enforcing nothing. Scoped to SQLite by module name so PostgreSQL, which
    always enforces, is left alone.
    """
    if type(dbapi_connection).__module__.split(".")[0] != "sqlite3":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def as_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden by models
        raise NotImplementedError


db = SQLAlchemy(model_class=Base, session_options={"expire_on_commit": False})
migrate = Migrate(compare_type=True, render_as_batch=False)
cors = CORS()
socketio = SocketIO(
    async_mode="threading",
    cors_allowed_origins=[],
    message_queue=None,
    logger=False,
    engineio_logger=False,
    ping_timeout=25,
    ping_interval=15,
    manage_session=False,
)

_redis_client: redis_lib.Redis | None = None

#: Negative cache: after a failed connection, do not retry for a while. Without
#: it, a Redis outage would add a connect timeout to *every* request that
#: touches the cache - turning a degradation into an outage.
#:
#: The interval backs off rather than staying fixed. A flat 30 s looks harmless
#: in a design and is not: on a machine that will never run Redis - a laptop
#: without Docker, a unit-test process - somebody pays the connect timeout
#: every thirty seconds, forever, on a request they did not think was touching
#: Redis. Doubling up to a few minutes means the cost of a service that is
#: definitively not there converges to zero.
_REDIS_RETRY_AFTER = 30.0
_REDIS_RETRY_MAX = 300.0
_redis_next_attempt: float = 0.0
_redis_backoff: float = _REDIS_RETRY_AFTER

#: Connect and command budgets for the shared client, in seconds. See
#: ``init_redis`` for why they are this small.
_REDIS_CONNECT_TIMEOUT = 0.25
_REDIS_SOCKET_TIMEOUT = 0.5

#: Guards the negative cache while a probe is in flight.
#:
#: The cache is only written once a probe *fails*, so every request that arrives
#: during the probe window sees an expired cache and starts a probe of its own.
#: A browser opening a page fires half a dozen calls at once, and a Flask
#: reloader runs two processes, so that turned one unreachable Redis into a
#: dozen simultaneous two-second stalls - the thundering herd that a negative
#: cache is supposed to prevent in the first place.
_redis_probe_lock = threading.Lock()


def redis_is_degraded() -> bool:
    """True while the client is in its negative cache."""
    return time.monotonic() < _redis_next_attempt


def init_redis(app: Any) -> redis_lib.Redis | None:
    """Create the shared Redis client.

    Returns ``None`` when Redis is disabled or unreachable; every caller
    degrades to a local implementation rather than failing the request.
    """
    if _redis_client is not None:
        return _redis_client

    url = app.config.get("REDIS_URL") or ""
    if not url:
        return None

    now = time.monotonic()
    if now < _redis_next_attempt:
        return None

    # Non-blocking. Whoever gets the lock probes; everyone else is told "not
    # available" immediately and falls back to the local implementation, which
    # is what they would have done a moment later anyway.
    if not _redis_probe_lock.acquire(blocking=False):
        return None

    try:
        # Re-checked under the lock: the thread that held it may have just
        # established the answer, and paying the timeout again for a known
        # answer is the exact cost this is here to remove.
        if time.monotonic() < _redis_next_attempt:
            return None
        return _connect_and_ping(app, url)
    finally:
        _redis_probe_lock.release()


def _connect_and_ping(app: Any, url: str) -> redis_lib.Redis | None:
    global _redis_next_attempt, _redis_backoff, _redis_client

    now = time.monotonic()
    try:
        client = redis_lib.from_url(
            url,
            decode_responses=True,
            # Short on purpose. This client is a cache and a shared rate-limit
            # counter: every caller has a correct answer without it, so a slow
            # "no" is worth much more than a slow "yes". At one and two seconds
            # a machine without Redis paid two seconds of frozen page on the
            # first request of every backoff window - a stall the app does not
            # need in order to know Redis is not there.
            socket_connect_timeout=_REDIS_CONNECT_TIMEOUT,
            socket_timeout=_REDIS_SOCKET_TIMEOUT,
            health_check_interval=30,
            retry_on_timeout=False,  # a retry here would multiply the timeout
            socket_keepalive=True,
        )
        client.ping()
    except Exception as exc:  # pragma: no cover - depends on infra
        _redis_next_attempt = now + _redis_backoff
        app.logger.warning(
            "Redis unavailable (%s); continuing without cache and shared rate limits. Next connection attempt in %ss.",
            exc.__class__.__name__,
            int(_redis_backoff),
        )
        _redis_backoff = min(_redis_backoff * 2, _REDIS_RETRY_MAX)
        return None

    if _redis_backoff != _REDIS_RETRY_AFTER:
        app.logger.info("Redis reachable again; retry interval reset.")
    _redis_backoff = _REDIS_RETRY_AFTER
    _redis_client = client
    return client


def get_redis() -> redis_lib.Redis | None:
    """Return the Redis client, or ``None`` if it is not currently available.

    Deliberately read-only. This is called from the cache, the rate limiter and
    the metrics sink - that is, from inside request handling - and every one of
    them has a correct answer without Redis. A function that can spend a network
    timeout before saying "no" is therefore a latency bug waiting for the next
    backoff window to expire, which is exactly what it was: one two-second
    frozen page every thirty seconds on a machine with no Redis, and a five
    hundred millisecond one even after the timeouts came down.

    The client is established by :func:`start_redis_supervisor` on a background
    thread, so the answer is already here by the time a request asks. Callers
    that genuinely need to establish it - the CLI, the tests - call
    :func:`init_redis` directly.
    """
    return _redis_client


def start_redis_supervisor(app: Any) -> None:
    """Keep the Redis client's availability up to date off the request path.

    One daemon thread per process. It sleeps until the negative cache expires,
    probes, and goes back to sleep: present means re-arm on the short interval,
    absent means re-arm on the backoff. Nothing a request does can make it wait.

    Best-effort throughout. A failure here leaves ``_redis_client`` as it was,
    which is the same state every caller already handles.
    """
    if not app.config.get("REDIS_URL"):
        return

    def loop() -> None:  # pragma: no cover - timing-dependent
        while True:
            try:
                init_redis(app)
            except Exception:
                pass
            # Wake just after the next attempt is due, so the answer is in place
            # before the window rather than on the first request inside it.
            delay = max(1.0, _redis_next_attempt - time.monotonic() + 0.5)
            time.sleep(delay)

    threading.Thread(target=loop, name="redis-supervisor", daemon=True).start()


def reset_redis() -> None:
    """Drop the cached client, the negative cache and the backoff.

    Used by tests and by the ``flask`` shell helper. Resetting only the client
    would leave a test waiting out the backoff of the previous failure, which is
    the kind of "flaky because of ordering" that is expensive to diagnose.
    """
    global _redis_client, _redis_next_attempt, _redis_backoff
    _redis_client = None
    _redis_next_attempt = 0.0
    _redis_backoff = _REDIS_RETRY_AFTER


def hash_password(raw: str, rounds: int | None = None) -> str:
    """Hash a plaintext password with bcrypt.

    ``bcrypt`` silently truncates at 72 bytes, so longer inputs are pre-hashed
    with SHA-256 to keep every byte significant while staying bcrypt-native.
    """
    if rounds is None:
        rounds = current_app.config.get("BCRYPT_ROUNDS", 12) if current_app else 12
    payload = _prepare_password(raw)
    return bcrypt.hashpw(payload, bcrypt.gensalt(rounds=int(rounds))).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    """Constant-time bcrypt verification that tolerates malformed hashes."""
    if not raw or not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare_password(raw), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _prepare_password(raw: str) -> bytes:
    import hashlib

    encoded = raw.encode("utf-8")
    if len(encoded) <= 72:
        return encoded
    return hashlib.sha256(encoded).hexdigest().encode("ascii")


def needs_rehash(hashed: str, rounds: int | None = None) -> bool:
    """True when a stored hash uses weaker parameters than the current policy."""
    if rounds is None:
        rounds = current_app.config.get("BCRYPT_ROUNDS", 12) if current_app else 12
    try:
        parts = hashed.split("$")
        cost = int(parts[2])
    except (IndexError, ValueError):
        return True
    return cost < int(rounds)


celery_app = Celery(
    "harmony", include=["app.tasks.email_tasks", "app.tasks.moderation_tasks", "app.tasks.maintenance_tasks"]
)


__all__ = [
    "Base",
    "bcrypt",
    "celery_app",
    "cors",
    "db",
    "get_redis",
    "hash_password",
    "init_redis",
    "migrate",
    "needs_rehash",
    "reset_redis",
    "socketio",
    "verify_password",
]

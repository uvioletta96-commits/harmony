"""Housekeeping jobs: retention, orphan cleanup, cache warming, backups.

These are the tasks that keep a long-running instance healthy without operator
attention. Each is idempotent, so re-running one is always safe.
"""

from __future__ import annotations

import os
import re
import time
from datetime import timedelta
from typing import Any

from flask import current_app

from ..extensions import celery_app, db
from ..models.base import utcnow
from ..models.chat import Message
from ..models.post import Post, PostMedia, PostStatus
from ..models.system import EmailDelivery
from ..models.user import AuthToken, User, UserSession
from ..services import upload_service
from ..utils.logging import get_logger, log_event

logger = get_logger("harmony.tasks.maintenance")

#: A table or column name is an identifier, not a value, and ``count(*)`` takes
#: no bind parameter - so any name interpolated into SQL is validated against
#: this first. Deliberately strict: anything outside [a-z_][a-z0-9_]* is refused.
_SAFE_IDENTIFIER = re.compile(r"\A[a-z_][a-z0-9_]*\Z")


@celery_app.task(name="maintenance.purge_expired_tokens", bind=True)
def purge_expired_tokens(self) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Delete spent and expired one-time tokens.

    Kept for 30 days after use rather than immediately: an expired row is the
    evidence that a link was already consumed, which matters when a user reports
    that a reset link "did not work".
    """
    now = utcnow()
    stale = now - timedelta(days=30)
    expired = db.session.execute(db.delete(AuthToken).where(AuthToken.expires_at < stale)).rowcount or 0
    used = (
        db.session.execute(
            db.delete(AuthToken).where(AuthToken.used_at.isnot(None), AuthToken.used_at < stale)
        ).rowcount
        or 0
    )
    db.session.commit()
    return {"expired": int(expired), "used": int(used)}


@celery_app.task(name="maintenance.purge_expired_sessions")
def purge_expired_sessions() -> int:
    cutoff = utcnow() - timedelta(days=7)
    removed = db.session.execute(db.delete(UserSession).where(UserSession.expires_at < cutoff)).rowcount or 0
    db.session.commit()
    return int(removed)


@celery_app.task(name="maintenance.purge_orphan_uploads", bind=True)
def purge_orphan_uploads(self, older_than_hours: int = 24, dry_run: bool = False) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Delete uploads that were never attached to a post.

    An attacker can upload files and abandon them; without this task the disk
    fills up while the database stays empty.
    """
    orphans = upload_service.orphan_media_report()
    if not orphans:
        return {"deleted": 0, "scanned": 0}

    cutoff = time.time() - max(1, older_than_hours) * 3600
    root = upload_service.ensure_upload_root()
    deleted = 0
    for key in orphans:
        path = os.path.join(root, key)
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            continue
        if dry_run:
            log_event(logger, "INFO", "maintenance.orphan_detected", key=key)
            continue
        if upload_service.delete_image(key):
            deleted += 1

    if not dry_run and deleted:
        # Drop the metadata rows too, or the table accumulates dead references.
        for key in orphans[:deleted]:
            db.session.query(PostMedia).filter(PostMedia.storage_key == key).delete()
        db.session.commit()

    log_event(logger, "INFO", "maintenance.orphan_uploads", found=len(orphans), deleted=deleted, dry_run=dry_run)
    return {"deleted": deleted, "scanned": len(orphans)}


@celery_app.task(name="maintenance.execute_scheduled_deletions", bind=True)
def execute_scheduled_deletions(self, batch: int = 50) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Irreversibly erase accounts whose grace period has elapsed.

    Batched so a large backlog never holds a long transaction open.
    """
    from ..models.system import AccountDeletionRequest
    from ..services.user_service import purge_account, revoke_all_sessions

    now = utcnow()
    due = (
        db.session.query(AccountDeletionRequest)
        .filter(
            AccountDeletionRequest.status == "scheduled",
            AccountDeletionRequest.scheduled_for <= now,
        )
        .limit(batch)
        .all()
    )
    # Close the door a little before the erasure lands. A session kept alive
    # through the whole grace period is a live credential for an account the
    # user already asked us to destroy; an hour's warning is more than enough
    # for anyone who still intends to cancel.
    soon = (
        db.session.query(AccountDeletionRequest)
        .filter(
            AccountDeletionRequest.status == "scheduled",
            AccountDeletionRequest.scheduled_for > now,
            AccountDeletionRequest.scheduled_for <= now + timedelta(hours=1),
        )
        .all()
    )
    closed = sum(revoke_all_sessions(row.user_id) for row in soon)
    if closed:
        log_event(logger, "INFO", "account.deletion_sessions_revoked", sessions=closed, accounts=len(soon))

    erased = 0
    for request_row in due:
        user = db.session.get(User, request_row.user_id)
        if user is None:
            request_row.status = "completed"
            request_row.completed_at = now
            continue
        stats = purge_account(user.id)
        erased += 1
        log_event(logger, "INFO", "account.purged", user_id=user.id, records=stats)

    if due or closed:
        db.session.commit()
    return {"erased": erased, "sessions_revoked": closed}


@celery_app.task(name="maintenance.archive_stale_posts")
def archive_stale_posts(older_than_days: int | None = None) -> int:
    """Apply the content retention policy (GDPR Art. 5(1)(e))."""
    days = older_than_days or int(current_app.config.get("DATA_RETENTION_DAYS", 365))
    cutoff = utcnow() - timedelta(days=days)
    archived = (
        db.session.execute(
            db.update(Post)
            .where(Post.created_at < cutoff, Post.status == PostStatus.PUBLISHED.value)
            .values(status=PostStatus.ARCHIVED.value, status_reason=f"Архив: старше {days} дней")
        ).rowcount
        or 0
    )
    db.session.commit()
    if archived:
        log_event(logger, "INFO", "maintenance.posts_archived", count=archived, retention_days=days)
    return int(archived)


@celery_app.task(name="maintenance.purge_email_log")
def purge_email_log(days: int = 90) -> int:
    cutoff = utcnow() - timedelta(days=days)
    removed = db.session.execute(db.delete(EmailDelivery).where(EmailDelivery.created_at < cutoff)).rowcount or 0
    db.session.commit()
    return int(removed)


@celery_app.task(name="maintenance.purge_chat_history")
def purge_chat_history(days: int | None = None) -> int:
    """Apply the chat retention window."""
    retention = days or int(current_app.config.get("CHAT_RETENTION_DAYS", 365))
    cutoff = utcnow() - timedelta(days=retention)
    removed = db.session.execute(db.delete(Message).where(Message.created_at < cutoff)).rowcount or 0
    db.session.commit()
    if removed:
        log_event(logger, "INFO", "maintenance.chat_purged", count=removed, retention_days=retention)
    return int(removed)


@celery_app.task(name="maintenance.warm_cache")
def warm_cache() -> dict[str, Any]:
    """Pre-compute the most requested read paths so the first users of the
    hour do not pay for a cold cache."""
    from ..services import cache_service
    from ..services.post_service import get_feed, public_counts
    from ..services.search_service import trending_tags

    started = time.perf_counter()
    produced = {"feed": 0, "trending": 0, "counts": 0}
    try:
        produced["feed"] = len(get_feed(None, mode="latest", limit=15).items)
        produced["trending"] = len(trending_tags())
        produced["counts"] = len(public_counts())
        cache_service.set(cache_service._key("stats", "public"), public_counts(), ttl=120)
    except Exception as exc:  # pragma: no cover
        log_event(logger, "WARNING", "maintenance.warm_cache_failed", error=exc.__class__.__name__)
    return {"produced": produced, "duration_ms": round((time.perf_counter() - started) * 1000, 1)}


@celery_app.task(name="maintenance.vacuum_metrics")
def vacuum_metrics() -> int:
    """Drop per-process metric files that are no longer being written."""
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not directory or not os.path.isdir(directory):
        return 0
    removed = 0
    for filename in os.listdir(directory):
        if not filename.startswith("gauge_"):
            continue
        path = os.path.join(directory, filename)
        if os.path.getmtime(path) < time.time() - 3600:
            try:
                os.remove(path)
                removed += 1
            except OSError:  # pragma: no cover
                pass
    return removed


@celery_app.task(name="maintenance.db_stats")
def db_stats() -> dict[str, Any]:
    """Row counts per table, for capacity planning dashboards.

    The table name is interpolated because ``count(*)`` takes no bind
    parameters, so a placeholder is impossible. The value comes from the
    database's own catalogue rather than from a request, and the identifier is
    re-validated against that catalogue before use, so a crafted name cannot
    reach the database.
    """
    from sqlalchemy import MetaData, Table, func, inspect, select

    bind = db.session.get_bind()
    metadata = MetaData()
    counts: dict[str, int] = {}
    for table in sorted(inspect(bind).get_table_names()):
        if not _SAFE_IDENTIFIER.fullmatch(table):
            # The name reaches SQL as an identifier, and count(*) takes no bind
            # parameter, so anything outside [a-z_][a-z0-9_]* is refused rather
            # than escaped.
            logger.warning("maintenance.db_stats_skipped_table", extra={"table": table})
            continue
        try:
            reflected = Table(table, metadata, autoload_with=bind)
            counts[table] = db.session.scalar(select(func.count()).select_from(reflected)) or 0
        except Exception:  # pragma: no cover - permission or lock
            counts[table] = -1
    return {"counts": counts, "generated_at": utcnow().isoformat()}


@celery_app.task(name="maintenance.check_backups")
def check_backups() -> dict[str, Any]:
    """Verify the configured backup directory has recent artefacts.

    A backup job that silently stops is worse than no backup at all, so this
    raises an alertable metric rather than failing quietly.
    """
    directory = os.environ.get("BACKUP_DIR", "")
    if not directory or not os.path.isdir(directory):
        return {"configured": False}
    files = [os.path.join(directory, name) for name in os.listdir(directory)]
    if not files:
        return {"configured": True, "files": 0, "latest_age_hours": None, "healthy": False}
    newest = max(os.path.getmtime(path) for path in files)
    age_hours = (time.time() - newest) / 3600
    healthy = age_hours < 48
    if not healthy:
        log_event(logger, "ERROR", "maintenance.backup_stale", age_hours=round(age_hours, 1))
    return {"configured": True, "files": len(files), "latest_age_hours": round(age_hours, 1), "healthy": healthy}


@celery_app.task(name="gdpr.purge_exports")
def purge_data_exports() -> int:
    """Delete GDPR export archives once their download window has closed.

    Named for the ``gdpr.*`` queue so it is obvious these tasks exist to
    satisfy a legal obligation rather than routine housekeeping.
    """
    from ..services.gdpr_service import purge_expired_exports

    return purge_expired_exports()


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

#: ``celery_app.conf.beat_schedule``. Times are UTC.
SCHEDULE: dict[str, Any] = {
    "purge-expired-tokens": {
        "task": "maintenance.purge_expired_tokens",
        "schedule": timedelta(hours=6),
    },
    "purge-sessions": {"task": "maintenance.purge_expired_sessions", "schedule": timedelta(hours=12)},
    "purge-orphan-uploads": {"task": "maintenance.purge_orphan_uploads", "schedule": timedelta(hours=3)},
    "execute-scheduled-deletions": {
        "task": "maintenance.execute_scheduled_deletions",
        "schedule": timedelta(hours=2),
    },
    "archive-stale-posts": {"task": "maintenance.archive_stale_posts", "schedule": timedelta(days=1)},
    "purge-email-log": {"task": "maintenance.purge_email_log", "schedule": timedelta(days=1)},
    "purge-chat-history": {"task": "maintenance.purge_chat_history", "schedule": timedelta(days=1)},
    "purge-notifications": {
        "task": "notifications.purge_expired",
        "schedule": timedelta(days=1),
    },
    "purge-data-exports": {"task": "gdpr.purge_exports", "schedule": timedelta(hours=6)},
    "expire-moderation-actions": {"task": "moderation.expire_actions", "schedule": timedelta(hours=1)},
    "rotate-spam-scores": {"task": "moderation.rotate_spam_scores", "schedule": timedelta(hours=1)},
    "detect-mass-reporting": {"task": "moderation.detect_mass_reporting", "schedule": timedelta(hours=6)},
    "moderate-recent": {"task": "moderation.sweep_recent", "schedule": timedelta(hours=4), "kwargs": {"hours": 12}},
    "warm-cache": {"task": "maintenance.warm_cache", "schedule": timedelta(minutes=30)},
    "vacuum-metrics": {"task": "maintenance.vacuum_metrics", "schedule": timedelta(hours=1)},
    "check-backups": {"task": "maintenance.check_backups", "schedule": timedelta(hours=6)},
    "collect-metrics": {
        "task": "maintenance.db_stats",
        "schedule": timedelta(hours=6),
    },
}


__all__ = [
    "SCHEDULE",
    "archive_stale_posts",
    "check_backups",
    "db_stats",
    "execute_scheduled_deletions",
    "purge_chat_history",
    "purge_data_exports",
    "purge_email_log",
    "purge_expired_sessions",
    "purge_expired_tokens",
    "purge_orphan_uploads",
    "vacuum_metrics",
    "warm_cache",
]

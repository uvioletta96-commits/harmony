"""Moderation and abuse-detection background jobs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..extensions import celery_app, db
from ..models.base import utcnow
from ..models.post import Post, PostStatus
from ..security import spam
from ..security.content_moderation import get_engine as moderation_engine
from ..utils.logging import get_logger, log_event
from ..utils.metrics import moderation_decisions_total

logger = get_logger("harmony.tasks.moderation")


@celery_app.task(name="moderation.recheck_post", bind=True, max_retries=2)
def recheck_post(self, post_id: int) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Re-screen a published post with the current classifier.

    Published content is re-checked on a schedule so that a new provider, an
    updated word list or a newly-reported pattern takes effect on existing
    content without an operator having to sweep the table by hand.
    """
    post = db.session.get(Post, post_id)
    if post is None:
        return {"status": "missing"}
    if post.status in (PostStatus.REMOVED.value, PostStatus.ARCHIVED.value):
        return {"status": "skipped"}

    decision = moderation_engine().screen(post.body, context="post", use_remote=True)
    previous = post.status
    if decision.blocked:
        post.status = PostStatus.UNDER_REVIEW.value
        post.status_reason = "Повторная проверка: сработала автоматическая модерация"
    elif decision.needs_review and post.status == PostStatus.PUBLISHED.value:
        post.status = PostStatus.UNDER_REVIEW.value
        post.status_reason = "Повторная проверка: требуется подтверждение"
    post.moderation_score = decision.score
    post.moderation_labels = {"categories": decision.categories, "provider": decision.provider}
    post.moderated_at = utcnow()
    db.session.commit()

    moderation_decisions_total.labels(decision=decision.decision).inc()
    if post.status != previous:
        from ..services import cache_service

        cache_service.invalidate_posts(post.public_id, post.author_id)
        log_event(
            logger,
            "WARNING",
            "moderation.recheck_changed_status",
            post_id=post.public_id,
            previous=previous,
            current=post.status,
        )
    return {"status": post.status, **decision.to_dict()}


@celery_app.task(name="moderation.sweep_recent", bind=True)
def sweep_recent(self, hours: int = 24, batch: int = 500) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Queue re-screening for posts published in the last ``hours``."""
    since = utcnow() - timedelta(hours=max(1, min(hours, 24 * 30)))
    ids = [
        row[0]
        for row in db.session.execute(
            db.select(Post.id)
            .where(Post.created_at >= since, Post.status == PostStatus.PUBLISHED.value)
            .order_by(Post.created_at.desc())
            .limit(batch)
        ).all()
    ]
    for post_id in ids:
        recheck_post.delay(post_id)
    log_event(logger, "INFO", "moderation.sweep_queued", count=len(ids), hours=hours)
    return {"queued": len(ids)}


@celery_app.task(name="moderation.expire_actions")
def expire_temporary_actions() -> int:
    """Lift expired suspensions. Hourly schedule."""
    from ..services.moderation_service import expire_temporary_actions as _expire

    return _expire()


@celery_app.task(name="moderation.rotate_spam_scores")
def rotate_spam_scores() -> dict[str, int]:
    """Recompute decayed risk scores and clear rows that have gone quiet.

    The decay is applied on read, so this job is housekeeping: it removes
    signals older than the window entirely, keeping the table small.
    """
    from ..models.moderation import AbuseEvent, SpamSignal

    cutoff = utcnow() - timedelta(hours=spam.DECAY_HOURS)
    removed = db.session.execute(db.delete(AbuseEvent).where(AbuseEvent.created_at < cutoff)).rowcount or 0
    cleared = (
        db.session.execute(
            db.update(SpamSignal)
            .where(SpamSignal.last_signal_at.isnot(None), SpamSignal.last_signal_at < cutoff)
            .values(score=0.0, signals=None, blocked=False)
        ).rowcount
        or 0
    )
    db.session.commit()
    log_event(logger, "INFO", "spam.scores_rotated", removed=removed, cleared=cleared)
    return {"removed": int(removed), "cleared": int(cleared)}


@celery_app.task(name="moderation.detect_mass_reporting")
def detect_mass_reporting(threshold: int = 5) -> int:
    """Flag accounts that report in bulk — a common malicious-report pattern."""
    from sqlalchemy import func

    from ..models.moderation import Report

    since = utcnow() - timedelta(hours=24)
    rows = (
        db.session.query(Report.reporter_id, func.count(Report.id))
        .filter(Report.created_at >= since)
        .group_by(Report.reporter_id)
        .having(func.count(Report.id) >= threshold)
        .all()
    )
    flagged = 0
    for reporter_id, count in rows:
        assessment = spam.score_scope("user", str(reporter_id))
        if not assessment.allowed:
            continue
        spam.record_signal(
            "bulk_reporting", scope="user", identifier=str(reporter_id), weight=1.0, detail={"reports": int(count)}
        )
        flagged += 1
    if flagged:
        db.session.commit()
        log_event(logger, "WARNING", "moderation.mass_reporting_detected", accounts=flagged)
    return flagged


__all__ = [
    "detect_mass_reporting",
    "expire_temporary_actions",
    "recheck_post",
    "rotate_spam_scores",
    "sweep_recent",
]

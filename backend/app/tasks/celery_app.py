"""Celery application factory and worker configuration.

A single Flask application object is pushed for the duration of every task, so
tasks can use ``current_app``, the ORM session and the extension registry
exactly as a request handler would.

Two settings matter operationally:

* ``task_acks_late`` + ``worker_prefetch_multiplier = 1`` — a task is
  acknowledged only after it runs, and workers take one task at a time. Without
  both, a worker crash silently discards queued work.
* ``task_time_limit`` — a runaway task is killed and the worker survives instead
  of blocking the queue forever.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun, task_retry
from flask import Flask


def configure_celery(app: Flask, celery_app: Celery) -> Celery:
    celery_app.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"] or None,
        result_backend=app.config["CELERY_RESULT_BACKEND"] or None,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=600,
        task_soft_time_limit=540,
        result_expires=3600,
        task_default_queue="default",
        task_routes={
            "notifications.*": {"queue": "notifications"},
            "moderation.*": {"queue": "moderation"},
            "maintenance.*": {"queue": "maintenance"},
            "email.*": {"queue": "email"},
        },
        task_always_eager=app.config.get("CELERY_ALWAYS_EAGER", False),
        task_eager_propagates=True,
        broker_connection_retry_on_startup=True,
        task_default_retry_delay=30,
        task_max_retries=3,
    )

    class ContextTask(celery_app.Task):  # type: ignore[misc, valid-type]
        """Bind an application context to every task execution."""

        abstract = True

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            with app.app_context():
                from ..utils.logging import configure_logging

                configure_logging(
                    app, level=app.config.get("LOG_LEVEL", "INFO"), fmt=app.config.get("LOG_FORMAT", "json")
                )
                return self.run(*args, **kwargs)

    celery_app.Task = ContextTask  # type: ignore[misc, assignment]
    celery_app.flask_app = app  # type: ignore[attr-defined]
    _install_signals(celery_app)
    return celery_app


def _install_signals(celery_app: Celery) -> None:
    from ..utils.logging import get_logger, log_event
    from ..utils.metrics import celery_tasks_total

    logger = get_logger("harmony.celery")

    @task_prerun.connect
    def _prerun(task_id: str | None = None, task=None, **_: Any) -> None:
        from flask import g

        g.celery_task_id = task_id

    @task_postrun.connect
    def _postrun(task=None, **_: Any) -> None:
        if task is not None:
            celery_tasks_total.labels(task=task.name, state="ok").inc()

    @task_failure.connect
    def _failure(task_id: str | None = None, exception=None, sender=None, **_: Any) -> None:
        name = getattr(sender, "name", "unknown")
        celery_tasks_total.labels(task=name, state="failure").inc()
        log_event(
            logger,
            "ERROR",
            "celery.task_failed",
            task=name,
            task_id=task_id,
            error=type(exception).__name__ if exception else "unknown",
        )

    @task_retry.connect
    def _retry(request=None, reason=None, sender=None, **_: Any) -> None:
        name = getattr(sender, "name", "unknown")
        celery_tasks_total.labels(task=name, state="retry").inc()
        log_event(logger, "WARNING", "celery.task_retry", task=name, reason=str(reason)[:200])


def create_worker_app() -> Flask:
    """Entry point for ``celery -A celery_worker.celery_app worker``."""
    from .. import create_app

    return create_app()


__all__ = ["configure_celery", "create_worker_app"]

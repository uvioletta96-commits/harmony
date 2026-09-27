"""Dispatching a Celery task without letting the broker stall the request.

A ``task.delay()`` is a network publish, and a network publish to a broker that
is not there does not fail immediately. Celery retries the connection twenty
times at one-second intervals before giving up, so a call site that wraps
``.delay()`` in a ``try`` still blocks its caller for roughly 108 seconds and
only then reaches the ``except``. On a machine without Redis that turns every
post, every comment and every registration into a two-minute spinner for a
message that was never going to be delivered.

Nothing is lost by checking first. ``get_redis`` keeps a negative cache with an
exponential backoff, so the check is free while the broker is down, and a task
that cannot be queued is a task that was not going to run anyway. The point is
only that the reader should not have to wait to find out.
"""

from __future__ import annotations

from typing import Any

from ..extensions import get_redis
from ..utils.logging import get_logger, log_event

logger = get_logger("harmony.tasks.dispatch")


def broker_available() -> bool:
    """Whether a task could be queued right now."""
    return get_redis() is not None


def enqueue(task: Any, *args: Any, **kwargs: Any) -> bool:
    """Queue ``task`` if the broker is reachable. Returns whether it was.

    Never raises. A failed enqueue is logged and reported as ``False``, because
    every current caller has the same shape - do the work, and treat the
    background half as best-effort - and a caller that has to catch this
    itself will eventually forget to.

    The availability check is not a guarantee, only an optimisation: the broker
    can go away between the check and the publish, so the ``except`` stays.
    """
    if not broker_available():
        log_event(logger, "INFO", "task.enqueue_skipped", task=getattr(task, "name", "?"), reason="broker_unavailable")
        return False

    name = getattr(task, "name", str(task))
    try:
        task.delay(*args, **kwargs)
    except Exception as exc:  # the broker went away between the check and the publish
        log_event(logger, "WARNING", "task.enqueue_failed", task=name, error=exc.__class__.__name__)
        return False
    return True


__all__ = ["broker_available", "enqueue"]

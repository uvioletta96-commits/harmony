"""Celery worker entrypoint.

Run a worker::

    celery -A celery_worker.celery_app worker -l info -Q default,notifications,email

Run the beat scheduler (exactly one instance, ever)::

    celery -A celery_worker.celery_app beat -l info
"""

from __future__ import annotations

from app import create_app
from app.extensions import celery_app
from app.tasks.celery_app import configure_celery

flask_app = create_app()
configure_celery(flask_app, celery_app)

# The worker must not fork before the database pool is created, or every fork
# inherits connections that are then shared across processes.
celery_app.conf.update(worker_max_tasks_per_child=1000, broker_pool_limit=10)

__all__ = ["celery_app", "flask_app"]

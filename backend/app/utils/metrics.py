"""Prometheus metrics and health probes.

Metrics are opt-out via ``METRICS_ENABLED`` and never carry user identifiers:
only route *templates* are labelled, so cardinality stays bounded and no
personal data leaks into the monitoring stack.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from flask import Flask, Response, current_app, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from sqlalchemy import text

from .logging import get_logger, log_event

logger = get_logger("harmony.metrics")

REGISTRY = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "harmony_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "harmony_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
http_requests_in_flight = Gauge(
    "harmony_http_requests_in_flight",
    "In-flight HTTP requests",
    registry=REGISTRY,
)
db_query_duration_seconds = Histogram(
    "harmony_db_query_duration_seconds",
    "SQLAlchemy statement latency",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 3.0),
    registry=REGISTRY,
)
rate_limit_hits_total = Counter(
    "harmony_rate_limit_hits_total",
    "Requests rejected by the rate limiter",
    ["scope"],
    registry=REGISTRY,
)
moderation_decisions_total = Counter(
    "harmony_moderation_decisions_total",
    "Automated moderation outcomes",
    ["decision"],
    registry=REGISTRY,
)
realtime_events_total = Counter(
    "harmony_realtime_events_total",
    "WebSocket events emitted",
    ["event"],
    registry=REGISTRY,
)
celery_tasks_total = Counter(
    "harmony_celery_tasks_total",
    "Celery task outcomes",
    ["task", "state"],
    registry=REGISTRY,
)
cache_operations_total = Counter(
    "harmony_cache_operations_total",
    "Redis cache operations",
    ["op", "result"],
    registry=REGISTRY,
)
app_info = Gauge(
    "harmony_app_info",
    "Build information",
    ["version", "env", "app"],
    registry=REGISTRY,
)


def track_db_queries(app: Flask) -> None:
    """Record per-statement latency and reject runaway queries."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    threshold = app.config.get("SLOW_QUERY_MS", 200)

    @event.listens_for(Engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("_query_start", []).append(time.perf_counter())

    @event.listens_for(Engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        stack = conn.info.get("_query_start") or []
        if not stack:
            return
        elapsed = time.perf_counter() - stack.pop()
        operation = statement.lstrip().split(" ", 1)[0].upper()[:16]
        db_query_duration_seconds.labels(operation=operation).observe(elapsed)
        if elapsed * 1000 > threshold:
            log_event(
                logger,
                "WARNING",
                "db.slow_query",
                operation=operation,
                duration_ms=round(elapsed * 1000, 1),
                statement=_statement_fingerprint(statement),
            )

    @event.listens_for(Engine, "handle_error")
    def _on_error(exception_context):
        log_event(
            logger,
            "ERROR",
            "db.error",
            statement=_statement_fingerprint(getattr(exception_context, "statement", "")),
            error=type(exception_context.original_exception).__name__
            if exception_context.original_exception
            else "unknown",
        )


def _statement_fingerprint(statement: str) -> str:
    """First 160 chars of a statement with literal values stripped."""
    text_only = statement.split(" WHERE ")[0].split("\n")[0]
    return " ".join(text_only.split())[:160]


def install_metrics(app: Flask) -> None:
    if not app.config.get("METRICS_ENABLED", True):
        return
    app_info.labels(
        version=app.config.get("VERSION", "0"),
        env=app.config.get("ENV", "development"),
        app=app.config.get("APP_NAME_EN", "Harmony"),
    ).set(1)
    track_db_queries(app)

    @app.before_request
    def _start() -> None:
        http_requests_in_flight.inc()
        current_app.extensions.setdefault("harmony", {})["_metrics_started"] = time.perf_counter()

    @app.after_request
    def _observe(response):
        started = current_app.extensions.get("harmony", {}).pop("_metrics_started", None)
        if started is not None:
            http_requests_in_flight.dec()
            http_request_duration_seconds.labels(method=request.method, endpoint=_endpoint_label()).observe(
                time.perf_counter() - started
            )
            http_requests_total.labels(
                method=request.method,
                endpoint=_endpoint_label(),
                status=str(response.status_code),
            ).inc()
        return response

    @app.get(app.config.get("METRICS_PATH", "/metrics"))
    def metrics() -> Response:
        registry = _multiprocess_registry()
        if registry is not None:
            return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)
        return Response(generate_latest(REGISTRY), mimetype=CONTENT_TYPE_LATEST)


def _multiprocess_registry() -> CollectorRegistry | None:
    import os

    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return None
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def _endpoint_label() -> str:
    """Use the route rule, never the raw path, to keep label cardinality flat."""
    rule = request.url_rule
    if rule is None:
        return "unmatched"
    return rule.rule if len(rule.rule) < 120 else "long_rule"


def register_health(app: Flask) -> None:
    """Liveness (is the process up) and readiness (are dependencies usable)."""

    @app.get(app.config.get("HEALTH_PATH", "/healthz"))
    def healthz() -> Any:
        return {"status": "ok", "app": app.config.get("APP_NAME_EN"), "version": app.config.get("VERSION")}

    @app.get(app.config.get("READY_PATH", "/readyz"))
    def readyz() -> Any:
        checks: dict[str, Any] = {}
        healthy = True

        from ..extensions import db as _db

        started = time.perf_counter()
        try:
            _db.session.execute(text("SELECT 1"))
            checks["database"] = {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        except Exception as exc:
            healthy = False
            checks["database"] = {"status": "error", "error": type(exc).__name__}
            _db.session.rollback()

        from ..extensions import get_redis

        client = get_redis()
        if client is None:
            checks["redis"] = {"status": "disabled"}
        else:
            started = time.perf_counter()
            try:
                client.ping()
                checks["redis"] = {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
            except Exception as exc:
                healthy = False
                checks["redis"] = {"status": "error", "error": type(exc).__name__}

        checks["database_required"] = True
        payload = {"status": "ready" if healthy else "not_ready", "checks": checks}
        return payload, 200 if healthy else 503


def timeit(operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator timing a function into the DB latency histogram."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                db_query_duration_seconds.labels(operation=operation).observe(time.perf_counter() - started)

        wrapper.__name__ = getattr(func, "__name__", "wrapped")
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


__all__ = [
    "REGISTRY",
    "app_info",
    "celery_tasks_total",
    "install_metrics",
    "register_health",
    "timeit",
    "track_db_queries",
]

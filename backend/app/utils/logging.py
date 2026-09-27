"""Structured logging with per-request correlation ids and secret redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from flask import Flask, g, has_request_context, request

#: Log record attributes that are always replaced with a mask. Appending to
#: this list is the only way to make a new field safe for structured logs.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "new_password",
        "old_password",
        "confirm_password",
        "token",
        "access_token",
        "refresh_token",
        "csrf_token",
        "authorization",
        "cookie",
        "set-cookie",
        "secret",
        "secret_key",
        "jwt",
        "jwt_secret_key",
        "api_key",
        "moderation_api_key",
        "mail_password",
        "token_hash",
        "password_hash",
        "email",
        "identifier",
        "phone",
        "ip",
        "ip_address",
    }
)

_MASK = "[redacted]"


def _redact(value: Any, depth: int = 0) -> Any:
    """Recursively mask sensitive values before they reach a log sink."""
    if depth > 6:
        return _MASK
    if isinstance(value, dict):
        return {key: (_MASK if _is_sensitive(key) else _redact(item, depth + 1)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, str) and len(value) > 4096:
        return value[:4096] + "…[truncated]"
    return value


def _is_sensitive(key: Any) -> bool:
    name = str(key).lower()
    if name in REDACTED_KEYS:
        return True
    return any(token in name for token in ("password", "secret", "token", "apikey", "api_key"))


def _iso_utc(record: logging.LogRecord) -> str:
    """RFC 3339 UTC timestamp with millisecond precision.

    Assembled by hand rather than handed to :meth:`logging.Formatter.formatTime`:
    that method uses :func:`time.strftime`, which rejects ``%f`` and ``%03d`` -
    both ``datetime`` directives, not ``time`` ones. A format string containing
    either raises ``ValueError: Invalid format string`` and takes down *every*
    log line, which is how a formatting bug stays invisible until production.
    """
    moment = datetime.fromtimestamp(record.created, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{int(record.msecs):03d}Z"


class JsonFormatter(logging.Formatter):
    """One JSON object per line — parseable by Loki, CloudWatch, Datadog, etc."""

    converter = time.gmtime  # type: ignore[assignment]

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _iso_utc(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
            if record.exc_info[1] is not None:
                payload["exception_type"] = type(record.exc_info[1]).__name__
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _redact(value)
        if has_request_context():
            payload.setdefault("request_id", getattr(g, "request_id", None))
            payload.setdefault("method", request.method)
            payload.setdefault("path", request.path)
            user_id = getattr(g, "user_id", None)
            if user_id:
                payload.setdefault("user_id", user_id)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }
        if extras:
            base += " " + " ".join(f"{k}={_redact(v)!s}" for k, v in sorted(extras.items()))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        if has_request_context():
            rid = getattr(g, "request_id", None)
            if rid:
                base += f" [request_id={rid}]"
        return base


_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def configure_logging(app: Flask | None = None, *, level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler; safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for noisy in ("werkzeug", "socketio", "engineio", "urllib3", "botocore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if app is not None:
        app.logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        # Flask's own logger would double-print through propagation.
        app.logger.handlers = []
        app.logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: str, event: str, **context: Any) -> None:
    """Emit a structured event with a mandatory ``action`` field.

    ``event`` is the stable name used for alerting and dashboards; keep it in
    the snake_case dotted form, e.g. ``auth.login_failed``. It is recorded under
    the ``action`` key so log processors keep one field name for it - which is
    also why the parameter is *not* called ``action``: callers routinely pass a
    per-event ``action=`` detail (the specific decision that was taken) and must
    stay able to.
    """
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        event,
        extra={"action": event, **context},
    )


def new_request_id() -> str:
    return uuid.uuid4().hex


def install_request_context(app: Flask) -> None:
    """Attach a request id, timings and user identity to ``flask.g``."""

    @app.before_request
    def _begin() -> None:
        g.request_id = request.headers.get("X-Request-ID") or new_request_id()
        g.started_at = time.perf_counter()
        g.user_id = None

    @app.after_request
    def _finish(response):
        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers.setdefault("X-Request-ID", request_id)
        started = getattr(g, "started_at", None)
        if started is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
            threshold = app.config.get("SLOW_REQUEST_MS", 1200)
            if elapsed_ms > threshold:
                log_event(
                    get_logger("harmony.performance"),
                    "WARNING",
                    "http.slow_request",
                    method=request.method,
                    path=request.path,
                    status=response.status_code,
                    duration_ms=round(elapsed_ms, 1),
                )
        return response


def get_request_id() -> str | None:
    return getattr(g, "request_id", None) if has_request_context() else None


__all__ = [
    "REDACTED_KEYS",
    "JsonFormatter",
    "TextFormatter",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "install_request_context",
    "log_event",
    "new_request_id",
]

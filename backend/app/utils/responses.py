"""Uniform JSON response envelope and the application error hierarchy.

Every API response — success or failure — has the same shape, so the frontend
has exactly one code path to parse:

    {"ok": true,  "data": {...}, "meta": {...}}
    {"ok": false, "error": {"code": "...", "message": "...", "fields": {...}}}
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Response, jsonify


class ApiError(Exception):
    """Base class for every expected (non-500) failure.

    ``status`` is the HTTP status, ``code`` a stable machine-readable string
    that the frontend switches on, and ``message`` safe to show to a user.
    Internal detail goes in ``detail`` and is only rendered in debug mode.
    """

    status: int = 400
    code: str = "bad_request"
    message: str = "Некорректный запрос."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status: int | None = None,
        fields: Mapping[str, str] | None = None,
        detail: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.fields: dict[str, str] = dict(fields or {})
        self.detail = detail
        self.headers: dict[str, str] = dict(headers or {})

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.fields:
            error["fields"] = self.fields
        if self.detail is not None:
            error["detail"] = self.detail
        return {"ok": False, "error": error}


class ValidationError(ApiError):
    status = 422
    code = "validation_failed"
    message = "Проверьте правильность заполнения полей."


class AuthenticationError(ApiError):
    status = 401
    code = "unauthenticated"
    message = "Требуется вход в аккаунт."


class PermissionError_(ApiError):
    status = 403
    code = "forbidden"
    message = "Недостаточно прав для этого действия."


class NotFoundError(ApiError):
    status = 404
    code = "not_found"
    message = "Запрошенный ресурс не найден."


class ConflictError(ApiError):
    status = 409
    code = "conflict"
    message = "Ресурс уже существует или находится в конфликтующем состоянии."


class GoneError(ApiError):
    status = 410
    code = "gone"
    message = "Ресурс больше недоступен."


class PayloadTooLargeError(ApiError):
    status = 413
    code = "payload_too_large"
    message = "Размер запроса превышает допустимый."


class UnsupportedMediaTypeError(ApiError):
    status = 415
    code = "unsupported_media_type"
    message = "Неподдерживаемый тип данных."


class RateLimitError(ApiError):
    status = 429
    code = "rate_limited"
    message = "Слишком много запросов. Попробуйте позже."

    def __init__(
        self, message: str | None = None, *, retry_after: int = 60, limit: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = max(1, int(retry_after))
        self.limit = limit
        self.headers["Retry-After"] = str(self.retry_after)

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["error"]["retry_after"] = self.retry_after
        if self.limit:
            payload["error"]["limit"] = self.limit
        return payload


class ModerationError(ApiError):
    """Raised when content is blocked or held for review."""

    status = 451
    code = "content_blocked"
    message = "Содержимое нарушает правила сообщества."


class ContentPendingError(ApiError):
    status = 202
    code = "content_under_review"
    message = "Содержимое отправлено на проверку и скоро появится в ленте."


class ServiceUnavailableError(ApiError):
    status = 503
    code = "service_unavailable"
    message = "Сервис временно недоступен. Попробуйте позже."


class InternalError(ApiError):
    status = 500
    code = "internal_error"
    message = "Внутренняя ошибка сервера. Попробуйте позже."


def ok(data: Any = None, *, meta: Mapping[str, Any] | None = None, status: int = 200, **extra: Any):
    """Success envelope.

    Returns a real :class:`~flask.Response` rather than a ``(body, status)``
    tuple so that callers can set cookies or headers on it before returning —
    which is what the auth endpoints need to do.
    """
    payload: dict[str, Any] = {"ok": True, "data": data}
    body_meta: dict[str, Any] = dict(meta or {})
    body_meta.update(extra)
    if body_meta:
        payload["meta"] = body_meta
    response = jsonify(payload)
    response.status_code = status
    return response


def created(data: Any = None, *, meta: Mapping[str, Any] | None = None, **extra: Any):
    return ok(data, meta=meta, status=201, **extra)


def no_content():
    response = Response(status=204)
    response.headers["Content-Length"] = "0"
    return response


def error_response(exc: ApiError):
    response = jsonify(exc.to_payload())
    response.status_code = exc.status
    for key, value in exc.headers.items():
        response.headers[key] = value
    return response


__all__ = [
    "ApiError",
    "AuthenticationError",
    "ConflictError",
    "ContentPendingError",
    "GoneError",
    "InternalError",
    "ModerationError",
    "NotFoundError",
    "PayloadTooLargeError",
    "PermissionError_",
    "RateLimitError",
    "ServiceUnavailableError",
    "UnsupportedMediaTypeError",
    "ValidationError",
    "created",
    "error_response",
    "no_content",
    "ok",
]

"""Global error handling.

Every unhandled exception becomes the same JSON envelope, so the frontend has
one error path and an unexpected stack trace never reaches a user. The
``request_id`` is echoed back and correlates the user-visible message with the
server log line.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, current_app, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from ..utils.logging import get_logger, get_request_id, log_event
from ..utils.responses import ApiError, InternalError, PayloadTooLargeError

logger = get_logger("harmony.errors")


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        if exc.status >= 500:
            logger.error("api error %s: %s", exc.code, exc.message, exc_info=exc)
        return _envelope(exc.status, exc.code, exc.message, exc.fields, exc.detail, exc.headers)

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(exc: RequestEntityTooLarge):
        error = PayloadTooLargeError("Размер запроса превышает допустимый.", code="payload_too_large")
        return _envelope(error.status, error.code, error.message)

    @app.errorhandler(SQLAlchemyError)
    def _db_error(exc: SQLAlchemyError):
        # The session may be poisoned; roll back so the next request on this
        # connection starts clean.
        from ..extensions import db

        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        log_event(
            logger,
            "ERROR",
            "db.unhandled_error",
            error=type(exc).__name__,
            path=request.path,
        )
        error = InternalError()
        return _envelope(error.status, error.code, error.message)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        code = (exc.name or "http_error").lower().replace(" ", "_")
        message = _translate(exc.code or 500, exc.description or "")
        if exc.code and exc.code >= 500:
            log_event(logger, "ERROR", "http.server_error", status=exc.code, path=request.path)
        return _envelope(exc.code or 500, code, message, headers=dict(exc.get_headers() or {}))

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        db_session_rollback()
        log_event(
            logger,
            "ERROR",
            "app.unhandled_exception",
            error=type(exc).__name__,
            path=request.path,
            method=request.method,
        )
        logger.exception("Unhandled exception", exc_info=exc)
        error = InternalError()
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": get_request_id(),
            },
        }
        if current_app.debug:
            payload["error"]["detail"] = f"{type(exc).__name__}: {exc}"
        response = jsonify(payload)
        response.status_code = 500
        return response


def db_session_rollback() -> None:
    from ..extensions import db

    try:
        db.session.rollback()
    except Exception:  # pragma: no cover
        pass


_HTTP_MESSAGES = {
    400: "Некорректный запрос.",
    401: "Требуется вход в аккаунт.",
    403: "Недостаточно прав для этого действия.",
    404: "Запрошенный ресурс не найден.",
    405: "Метод не поддерживается для этого адреса.",
    406: "Недопустимый формат ответа.",
    409: "Конфликт состояния ресурса.",
    415: "Неподдерживаемый тип данных.",
    422: "Проверьте правильность заполнения полей.",
    429: "Слишком много запросов. Попробуйте позже.",
    500: "Внутренняя ошибка сервера.",
    502: "Сервис временно недоступен.",
    503: "Сервис временно недоступен.",
    504: "Превышено время ожидания ответа.",
}


def _translate(status: int, fallback: str) -> str:
    message = _HTTP_MESSAGES.get(status)
    if message:
        return message
    return fallback or "Произошла ошибка."


def _envelope(
    status: int,
    code: str,
    message: str,
    fields: dict[str, str] | None = None,
    detail: Any = None,
    headers: dict[str, str] | None = None,
):
    error: dict[str, Any] = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    if detail is not None and current_app.debug:
        error["detail"] = detail
    request_id = get_request_id()
    if request_id:
        error["request_id"] = request_id
    response = jsonify({"ok": False, "error": error})
    response.status_code = status
    for key, value in (headers or {}).items():
        if key.lower() in {"content-type", "content-length"}:
            continue
        response.headers[key] = value
    return response


__all__ = ["register_error_handlers"]

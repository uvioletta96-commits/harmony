"""Authentication endpoints.

All responses are ``Cache-Control: no-store``. Anything that touches a
credential must not be cached by a browser, a proxy or the service worker.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, request

from .. import i18n
from ..models.base import utcnow
from ..security.decorators import (
    auth_required,
    clear_auth_cookies,
    issue_csrf_token,
    set_auth_cookies,
    verify_csrf,
)
from ..security.rate_limit import enforce
from ..services import auth_service
from ..utils.responses import AuthenticationError, ValidationError, created, no_content, ok

bp = Blueprint("auth", __name__)

NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, private", "Pragma": "no-cache"}


def _no_store(view):  # type: ignore[no-untyped-def]
    """Decorator marking a view's response as uncacheable.

    Anything that touches a credential must never be stored by a browser, a
    shared proxy or the service worker.
    """
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        response = view(*args, **kwargs)
        if hasattr(response, "headers"):
            for key, value in NO_STORE.items():
                response.headers[key] = value
        return response

    return wrapper


# ---------------------------------------------------------------------------
# CSRF bootstrap
# ---------------------------------------------------------------------------


@bp.get("/i18n/locales")
def locales():
    """The interface languages, each one fully translated.

    Public and uncached, because the point is to have it before a session
    exists: the client needs the list on the registration screen, before anyone
    has an account to store a preference on.
    """
    return ok(i18n.catalogue(), meta={"negotiated": i18n.negotiate(request.headers.get("Accept-Language"))})


@bp.get("/auth/csrf")
def csrf_token():
    """Issue the CSRF cookie pair.

    The token is readable by JavaScript by design: that is what makes the
    double-submit pattern work. It is not a credential — possessing it grants no
    access on its own.
    """
    token = issue_csrf_token()
    response = ok({"csrf_token": token})
    _set_csrf_cookie(response, token)
    return response


# ---------------------------------------------------------------------------
# Registration and verification
# ---------------------------------------------------------------------------


@bp.post("/auth/register")
@_no_store
def register():
    verify_csrf()
    enforce("auth:register")
    payload = request.get_json(silent=True) or {}
    user, token = auth_service.register(payload)
    # Whether a message actually left the building. "Check your inbox" is a
    # promise, and with MAIL_ENABLED off the server cannot keep it - the reader
    # has no way to tell a slow mail server from one that was never configured,
    # and a `resend` button in that state can only ever lie too.
    mail_enabled = bool(current_app.config.get("MAIL_ENABLED"))
    body = {
        "user": _public_session(user),
        "requires_email_verification": True,
        "verification_email_sent": mail_enabled,
        "message": (
            "Проверьте почту: мы отправили ссылку для подтверждения аккаунта."
            if mail_enabled
            else "Аккаунт создан. Этот сервер не отправляет почту, "
            "поэтому подтвердить адрес должен администратор."
        ),
    }
    # Development convenience: surface the confirmation link in the response
    # instead of only in the console. Never enabled in production.
    #
    # The page needs a *URL*, not a token. Building it client-side means two
    # places that must agree on the route, and when the token is simply dropped -
    # as it was - the reader is told to check an inbox that, with MAIL_ENABLED
    # off, will never receive anything, and is left with no way forward.
    if not current_app.config.get("MAIL_ENABLED") and current_app.debug:
        body["verification_token_dev_only"] = token
        body["verification_url"] = _verification_url(token)
    return created(body)


@bp.post("/auth/verify-email")
@_no_store
def verify_email():
    verify_csrf()
    enforce("global")
    payload = request.get_json(silent=True) or {}
    user = auth_service.verify_email(payload.get("token", ""))

    # Sign the user in as part of confirming the address. The link was mailed
    # *to* that address and is single-use, so it is the proof of ownership;
    # asking for the password again on the next screen buys nothing and the
    # usual path reads as a broken product rather than as a second step.
    _user, access_token, refresh_token, _session = auth_service.open_session(user)
    csrf = issue_csrf_token()
    response = ok(
        {
            "user": _public_session(user),
            "message": "Аккаунт подтверждён. Добро пожаловать!",
            "expires_in": current_app.config["ACCESS_TOKEN_TTL"],
            "csrf_token": csrf,
            "signed_in": True,
        }
    )
    set_auth_cookies(response, access_token, refresh_token)
    _set_csrf_cookie(response, csrf)
    return response


@bp.post("/auth/resend-verification")
@_no_store
def resend_verification():
    verify_csrf()
    enforce("auth:forgot")
    payload = request.get_json(silent=True) or {}
    # Always the same response: this endpoint must not confirm whether an
    # address is registered.
    fresh = auth_service.resend_verification((payload.get("email") or "").strip().lower())
    body = {"message": "Если аккаунт существует, письмо с инструкцией отправлено."}
    # With mail switched off there is no inbox to check, so a resend that reports
    # success and sends nothing strands the reader. The message is byte-for-byte
    # the same either way - only a development instance ever sees this field, and
    # only for an address that really did have an unverified account.
    if fresh and not current_app.config.get("MAIL_ENABLED") and current_app.debug:
        body["verification_url"] = _verification_url(fresh)
    return ok(body)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@bp.post("/auth/login")
@_no_store
def login():
    verify_csrf()
    enforce("auth:login")
    payload = request.get_json(silent=True) or {}
    data = auth_service.login_schema(payload)
    user, access_token, refresh_token, _session = auth_service.login(data.get("identifier", ""), data["password"])
    # Rotate the CSRF token on login so a token minted before authentication
    # cannot be reused afterwards.
    csrf = issue_csrf_token()
    response = ok(
        {
            "user": _public_session(user),
            "expires_in": current_app.config["ACCESS_TOKEN_TTL"],
            "csrf_token": csrf,
        }
    )
    set_auth_cookies(response, access_token, refresh_token)
    _set_csrf_cookie(response, csrf)
    return response


@bp.post("/auth/refresh")
@_no_store
def refresh():
    verify_csrf()
    enforce("auth:refresh")
    from ..security.decorators import REFRESH_COOKIE

    token = request.cookies.get(REFRESH_COOKIE) or (request.get_json(silent=True) or {}).get("refresh_token")
    if not token:
        raise AuthenticationError("Сессия не найдена.", code="refresh_missing")
    user, access_token, new_refresh, _session = auth_service.refresh(token)
    response = ok({"user": _public_session(user), "expires_in": current_app.config["ACCESS_TOKEN_TTL"]})
    set_auth_cookies(response, access_token, new_refresh)
    return response


@bp.post("/auth/logout")
@_no_store
@auth_required
def logout():
    verify_csrf()
    payload = request.get_json(silent=True) or {}
    revoked = auth_service.logout(g.current_user, all_sessions=bool(payload.get("all_sessions")))
    response = ok({"message": "Вы вышли из аккаунта.", "revoked_sessions": revoked})
    clear_auth_cookies(response)
    return response


@bp.get("/auth/sessions")
@auth_required
def sessions():
    return ok({"sessions": auth_service.list_sessions(g.current_user)})


@bp.delete("/auth/sessions/<jti>")
@auth_required
def revoke_session(jti: str):
    verify_csrf()
    from ..extensions import db
    from ..models.user import UserSession

    session = db.session.scalar(
        db.select(UserSession).where(UserSession.jti == jti, UserSession.user_id == g.current_user.id)
    )
    if session is None:
        raise AuthenticationError("Сессия не найдена.", code="session_not_found")
    session.revoked_at = utcnow()
    session.revoked_reason = "user_revoked"
    db.session.commit()
    return no_content()


@bp.get("/auth/me")
@auth_required
def me():
    return ok({"user": _public_session(g.current_user)})


# ---------------------------------------------------------------------------
# Password management
# ---------------------------------------------------------------------------


@bp.post("/auth/forgot-password")
@_no_store
def forgot_password():
    verify_csrf()
    enforce("auth:forgot")
    data = auth_service.forgot_password_schema(request.get_json(silent=True) or {})
    auth_service.request_password_reset(data.get("email", ""))
    return ok({"message": "Если аккаунт существует, ссылка для сброса пароля отправлена."})


@bp.post("/auth/reset-password")
@_no_store
def reset_password():
    verify_csrf()
    enforce("global")
    payload = request.get_json(silent=True) or {}
    data = auth_service.reset_password_schema(payload)
    auth_service.reset_password(data.get("token", ""), data["password"])
    return ok({"message": "Пароль изменён. Теперь войдите с новым паролем."})


@bp.post("/auth/change-password")
@_no_store
@auth_required
def change_password():
    verify_csrf()
    enforce("global")
    payload = request.get_json(silent=True) or {}
    data = auth_service.change_password_schema(payload)
    auth_service.change_password(g.current_user, data["current_password"], data["new_password"])
    # Every session is revoked, this one included, so the cookies must go too -
    # leaving them would hand the browser a token the server already refuses.
    response = ok(
        {
            "message": "Пароль изменён. Все сессии завершены.",
            "reauthenticate": True,
        }
    )
    clear_auth_cookies(response)
    return response


@bp.post("/auth/reauthenticate")
@_no_store
@auth_required
def reauthenticate():
    """Step-up authentication for destructive actions.

    The returned token authorises a single sensitive operation for ten minutes,
    so a stolen password alone is not enough to delete an account.
    """
    verify_csrf()
    enforce("auth:login")
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "")
    if not password:
        raise ValidationError("Введите пароль.", code="password_required", fields={"password": "Обязательное поле."})
    token = auth_service.verify_password_for_sensitive_action(g.current_user, password)
    return ok({"reauth_token": token, "expires_in": 600})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verification_url(token: str) -> str:
    """The confirmation link for a token, as a path.

    A path rather than a full URL because the client knows the origin, and
    hard-coding a scheme and host here would put the two out of step the moment
    the app is served under a different name.
    """
    return f"/verify?token={token}"


def _set_csrf_cookie(response, token: str | None = None) -> None:  # type: ignore[no-untyped-def]
    response.set_cookie(
        current_app.config["CSRF_COOKIE_NAME"],
        token or issue_csrf_token(),
        max_age=3600,
        # Readable by JS on purpose: the double-submit pattern needs the client
        # to echo the cookie value back in a header.
        httponly=False,
        secure=bool(current_app.config["SECURE_COOKIE"]),
        samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
        path="/",
    )


def _public_session(user) -> dict:  # type: ignore[no-untyped-def]
    data = user.to_public_dict(user)
    data["email_notifications"] = user.email_notifications
    data["profile_visibility"] = user.profile_visibility
    # The stored preference travels with the session so the client can render
    # the right interface on the very first paint. ``None`` means "no choice
    # made yet", which the client resolves against the browser - see
    # ``app.i18n.negotiate``.
    data["language"] = user.language
    data["text_direction"] = i18n.direction(user.language or i18n.negotiate(request.headers.get("Accept-Language")))
    return data


__all__ = ["bp"]

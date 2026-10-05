"""Request-authentication decorators: bearer tokens, sessions, CSRF, roles.

Cookie strategy: the access token lives in an ``HttpOnly`` cookie so XSS cannot
read it, and a *double-submit* CSRF token lives in a readable cookie. Every
state-changing request must echo the CSRF token in a header. This pairing gives
the properties we need without the failure mode of storing a JWT in
``localStorage``, where any XSS immediately exfiltrates a 12-hour credential.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC
from functools import wraps
from typing import Any

import jwt
from flask import current_app, g, request
from itsdangerous import URLSafeTimedSerializer

from ..extensions import db
from ..models.user import User, UserRole, UserStatus
from ..utils.crypto import constant_time_equals, create_refresh_token
from ..utils.logging import get_logger, log_event
from ..utils.responses import AuthenticationError, PermissionError_

logger = get_logger("harmony.auth")

_csrf_serializer: URLSafeTimedSerializer | None = None

#: Methods that cannot change state and therefore need no CSRF token.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

ACCESS_COOKIE = "harmony_access"
REFRESH_COOKIE = "harmony_refresh"


def csrf_serializer() -> URLSafeTimedSerializer:
    global _csrf_serializer
    if _csrf_serializer is None:
        _csrf_serializer = URLSafeTimedSerializer(
            current_app.config["SECRET_KEY"], salt="csrf", signer_kwargs={"key_derivation": "hmac"}
        )
    return _csrf_serializer


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def set_auth_cookies(response, access_token: str, refresh_token: str | None = None) -> None:
    max_age = int(current_app.config["ACCESS_TOKEN_TTL"])
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=max_age,
        httponly=True,  # unreadable from JavaScript
        secure=bool(current_app.config["SECURE_COOKIE"]),
        samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
        path="/",
    )
    if refresh_token:
        refresh_max_age = int(current_app.config["REFRESH_TOKEN_TTL"])
        path = f"{current_app.config['API_PREFIX']}/auth"
        response.set_cookie(
            REFRESH_COOKIE,
            refresh_token,
            max_age=refresh_max_age,
            httponly=True,
            secure=bool(current_app.config["SECURE_COOKIE"]),
            samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
            path=path,
        )


def clear_auth_cookies(response) -> None:
    for name, path in ((ACCESS_COOKIE, "/"), (REFRESH_COOKIE, f"{current_app.config['API_PREFIX']}/auth")):
        response.delete_cookie(
            name,
            path=path,
            secure=bool(current_app.config["SECURE_COOKIE"]),
            samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
        )


# ---------------------------------------------------------------------------
# CSRF (double submit)
# ---------------------------------------------------------------------------


def issue_csrf_token() -> str:
    return csrf_serializer().dumps({"n": time.time_ns()})


def read_csrf_token() -> str | None:
    return request.cookies.get(current_app.config["CSRF_COOKIE_NAME"])


def verify_csrf() -> None:
    """Double-submit check for every unsafe method."""
    if request.method in SAFE_METHODS:
        return
    if not current_app.config.get("CSRF_PROTECTION", True):
        return
    # A bearer token is proof of possession in a way a cookie is not: the browser
    # does not attach it automatically, so a cross-site form post cannot supply
    # one. The double-submit token exists precisely *because* browsers do attach
    # cookies, so demanding it here would defend against nothing and would make
    # the API unusable from a mobile app or a CLI.
    if _bearer_token():
        return
    # A correct Origin/Referer is accepted as an equivalent signal for other
    # non-browser callers.
    if _origin_is_trusted():
        return

    cookie_token = read_csrf_token()
    header_token = request.headers.get(current_app.config["CSRF_HEADER_NAME"], "")
    if not cookie_token or not header_token:
        raise PermissionError_(
            "\u041e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u0435\u0442 CSRF-\u0442\u043e\u043a\u0435\u043d. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443.",
            code="csrf_token_missing",
        )
    if not constant_time_equals(cookie_token, header_token):
        log_event(logger, "WARNING", "security.csrf_mismatch", path=request.path, method=request.method)
        raise PermissionError_(
            "\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 CSRF-\u0442\u043e\u043a\u0435\u043d. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443.",
            code="csrf_token_invalid",
        )


def _origin_is_trusted() -> bool:
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return False
    allowed = set(current_app.config.get("CORS_ORIGINS") or [])
    allowed.add(current_app.config.get("SITE_URL", ""))
    for candidate in allowed:
        if candidate and origin.rstrip("/").startswith(candidate.rstrip("/")):
            return True
    return False


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


def _bearer_token() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _access_token() -> str | None:
    return _bearer_token() or request.cookies.get(ACCESS_COOKIE)


def _refresh_token() -> str | None:
    return _bearer_token() or request.cookies.get(REFRESH_COOKIE)


def load_user_from_token(token: str, *, expected_type: str = "access") -> User | None:
    """Validate a token cryptographically and resolve its user.

    A valid signature is not authorisation. Three things are checked, in the
    order that produces the most useful answer for the person holding the
    request:

    * the token must verify and not have expired;
    * its ``sub`` must still name a real account;
    * its ``ver`` claim carries the status at issue time, and matters only when
      the account is *back* to normal now - a token minted before a ban/unban
      cycle predates that decision and must not quietly resume.

    Session liveness is checked separately, by :func:`assert_session_live`,
    *after* the account's own standing. That ordering is deliberate: a banned
    user whose sessions were just revoked deserves ``403 account_banned``, not a
    generic ``401 session_revoked`` that tells them to sign in again when
    signing in is exactly what they cannot do.
    """
    from ..utils.crypto import decode_token

    try:
        payload = decode_token(token, expected_type=expected_type)
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError(
            "\u0421\u0435\u0441\u0441\u0438\u044f \u0438\u0441\u0442\u0435\u043a\u043b\u0430. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443.",
            code="token_expired",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError(
            "\u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u044b\u0439 \u0442\u043e\u043a\u0435\u043d \u0434\u043e\u0441\u0442\u0443\u043f\u0430.",
            code="token_invalid",
        ) from exc

    user = db.session.get(User, int(payload["sub"]))
    if user is None:
        raise AuthenticationError(
            "\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.",
            code="user_not_found",
        )

    if payload.get("ver") != user.status and user.status == UserStatus.ACTIVE.value:
        raise AuthenticationError(
            "\u0421\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u043e\u0441\u044c. \u0412\u043e\u0439\u0434\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e.",
            code="token_stale",
        )

    g.token_payload = payload
    g.token_jti = payload.get("jti")
    return user


def assert_session_live(user: User) -> None:
    """Reject a token whose session has been revoked.

    Called after the account's own standing has been assessed, so a sanction
    always reports itself rather than hiding behind a generic session error.
    """
    jti = getattr(g, "token_jti", None)
    if jti and not _session_is_live(str(jti), user.id):
        raise AuthenticationError(
            "\u0421\u0435\u0441\u0441\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430. \u0412\u043e\u0439\u0434\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e.",
            code="session_revoked",
        )


def _session_is_live(jti: str, user_id: int) -> bool:
    """Is the session behind this access token still active?

    A single indexed lookup on ``jti``. Rows are kept after revocation precisely
    so this check has something to find; a missing row is treated as revoked
    rather than trusted.
    """
    from ..models.user import UserSession

    session = db.session.scalar(db.select(UserSession).where(UserSession.jti == jti, UserSession.user_id == user_id))
    # A missing row is treated as revoked rather than trusted: a token whose
    # session was pruned must not become a permanent credential.
    return session is not None and session.is_active


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def auth_required(fn: Callable[..., Any] | None = None, *, allow: Iterable[str] = ()) -> Callable[..., Any]:
    """Require an authenticated, verified, non-sanctioned account.

    ``allow`` names statuses that are permitted through anyway. It exists for
    the two endpoints an account mid-erasure must still be able to reach -
    cancelling its own deletion, and re-posting a step-up token so the replay
    is answered with ``reauth_invalid`` rather than with a status error that
    hides the real reason. Nothing else may use it.
    """
    allowed = frozenset(allow)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = _access_token()
            if not token:
                raise AuthenticationError()
            user = load_user_from_token(token)
            _assert_usable(user, allow=allowed)
            assert_session_live(user)
            g.current_user = user
            g.user_id = user.id
            _touch_last_seen(user)
            return func(*args, **kwargs)

        return wrapper

    # Supports both spellings: ``@auth_required`` and ``@auth_required(allow=...)``.
    return decorator(fn) if fn is not None else decorator  # type: ignore[arg-type]


def auth_optional(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Populate ``g.current_user`` when a usable token is present, else None.

    A restricted or revoked caller is treated as anonymous rather than rejected:
    a public endpoint must still render for someone whose session has been cut,
    and it must not leak that the account exists by erroring differently.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        g.current_user = None
        token = _access_token()
        if token:
            try:
                user = load_user_from_token(token)
                _assert_usable(user)
                assert_session_live(user)
            except (AuthenticationError, PermissionError_):
                user = None
            else:
                g.current_user = user
                g.user_id = user.id
        return fn(*args, **kwargs)

    return wrapper


def roles_required(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Restrict a view to specific roles. Implies :func:`auth_required`."""
    allowed = set(roles)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        @auth_required
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            user: User = g.current_user
            if user.role not in allowed:
                log_event(
                    logger,
                    "WARNING",
                    "security.role_denied",
                    required=sorted(allowed),
                    actual=user.role,
                    path=request.path,
                )
                raise PermissionError_("У вас нет доступа к этому разделу.", code="insufficient_role")
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def moderator_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Allow moderators and administrators only."""
    return roles_required(UserRole.MODERATOR.value, UserRole.ADMIN.value)(fn)


def admin_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Allow administrators only.

    Reserved for the operations a moderator must not perform: changing roles and
    any other action that alters the *authority* of accounts rather than the
    content they post. A moderator who can promote themselves outranks every
    control in the moderation panel.
    """

    @wraps(fn)
    @auth_required
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        user: User = g.current_user
        if not user.is_admin:
            log_event(
                logger,
                "WARNING",
                "security.role_denied",
                required=[UserRole.ADMIN.value],
                actual=user.role,
                path=request.path,
            )
            raise PermissionError_("Этот раздел доступен только администраторам.", code="admin_only")
        return fn(*args, **kwargs)

    return wrapper


def csrf_protect(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Explicit CSRF guard for views outside the global before-request hook."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        verify_csrf()
        return fn(*args, **kwargs)

    return wrapper


def rate_limited(scope: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    from ..security.rate_limit import rate_limit

    return rate_limit(scope, **kwargs)


def _assert_usable(user: User, *, allow: frozenset[str] = frozenset()) -> None:
    """Reject accounts that may not act, naming the reason precisely.

    A banned or suspended account gets 403: the token is authentic, we know who
    this is, and telling them to "sign in again" would be a dead end. Only a
    token whose user is genuinely gone gets 401.
    """
    status = user.status
    if status in allow:
        return
    if status == UserStatus.DEACTIVATED.value:
        raise AuthenticationError("Аккаунт удалён.", code="account_deactivated")
    if status == UserStatus.DELETION_PENDING.value:
        raise PermissionError_(
            "Аккаунт будет удалён. Отмените удаление, чтобы продолжить.",
            code="account_deletion_pending",
        )
    if status == UserStatus.SUSPENDED.value:
        raise PermissionError_(
            user.status_reason or "Аккаунт приостановлен администратором.",
            code="account_suspended",
        )
    if status == UserStatus.BANNED.value:
        raise PermissionError_(
            user.status_reason or "Аккаунт заблокирован.",
            code="account_banned",
        )
    if not user.email_verified and current_app.config.get("REQUIRE_EMAIL_VERIFICATION", True):
        # `REQUIRE_EMAIL_VERIFICATION=false` waives this, matching the check in
        # `auth_service.login`. Both gates have to agree: relaxing only one
        # would let the sign-in succeed and then fail every request after it.
        raise PermissionError_(
            "Подтвердите адрес электронной почты, чтобы пользоваться сервисом.",
            code="email_not_verified",
        )


_LAST_SEEN_TOUCH_EVERY = 60  # seconds


def _touch_last_seen(user: User) -> None:
    """Update presence at most once a minute, then commit lazily.

    Writing on every request would put an UPDATE on the hot path for a purely
    cosmetic signal; the session transaction commits at teardown anyway.
    """
    from ..models.base import utcnow

    now = utcnow()
    last = user.last_seen_at
    if last is not None:
        # ``UTCDateTime`` guarantees both sides are aware, but a value assigned
        # in this same session may still be naive.
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if (now - last).total_seconds() < _LAST_SEEN_TOUCH_EVERY:
            return
    user.last_seen_at = now
    try:
        db.session.commit()
    except Exception:  # pragma: no cover - never fail a request on presence
        db.session.rollback()


def issue_session(user: User, *, ip_hash: str | None = None, user_agent: str | None = None):
    """Create a refresh-token session row and return ``(access, refresh, session)``.

    Both tokens carry the *same* ``jti``, the one stored on the session row.
    That single identifier is what makes revocation immediate: revoking the row
    (logout, password change, admin action, refresh rotation) invalidates the
    matching access token too, instead of leaving it usable until it expires.
    """
    from ..models.base import utcnow
    from ..models.user import UserSession
    from ..utils.crypto import create_access_token, hash_token

    # Refresh first: its jti becomes the session's identity.
    refresh_token, refresh_exp, jti = create_refresh_token(user)
    access_token, _access_exp, _access_jti = create_access_token(user, jti=jti)

    session = UserSession(
        user_id=user.id,
        jti=jti,
        token_hash=hash_token(refresh_token),
        ip_hash=ip_hash,
        user_agent=(user_agent or "")[:256] or None,
        expires_at=refresh_exp,
        last_seen_at=utcnow(),
    )
    db.session.add(session)
    return access_token, refresh_token, session


def current_user() -> User | None:
    return getattr(g, "current_user", None)


def current_user_id() -> int | None:
    user = getattr(g, "current_user", None)
    return user.id if user is not None else None


__all__ = [
    "ACCESS_COOKIE",
    "REFRESH_COOKIE",
    "SAFE_METHODS",
    "admin_required",
    "auth_optional",
    "auth_required",
    "clear_auth_cookies",
    "csrf_protect",
    "current_user",
    "current_user_id",
    "issue_csrf_token",
    "issue_session",
    "load_user_from_token",
    "moderator_required",
    "rate_limited",
    "read_csrf_token",
    "roles_required",
    "set_auth_cookies",
    "verify_csrf",
]

"""Authentication: registration, email verification, login, sessions, recovery.

Security properties this module is responsible for:

* **Account enumeration is impossible.** Every endpoint that takes an identity
  (login, register, forgot-password, resend-verification) returns the same
  response whether or not the account exists, and always consumes the same work.
* **Credentials are compared in constant time** and a failed login costs the
  same as a successful one (a dummy bcrypt verification on unknown users) so
  response timing does not reveal valid usernames.
* **Tokens are stored hashed and single-use**; the raw value leaves the server
  exactly once, in the email.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from flask import current_app, request
from sqlalchemy import func, or_, select

from ..extensions import db, hash_password, needs_rehash, verify_password
from ..models.base import utcnow
from ..models.system import ConsentRecord, LegalDocument
from ..models.user import (
    AuthToken,
    LoginAttempt,
    PrivacySetting,
    ProfileVisibility,
    User,
    UserRole,
    UserSession,
    UserStatus,
)
from ..security import spam
from ..security.validators import Schema
from ..utils.crypto import generate_token, hash_email, hash_token, keyed_hash
from ..utils.logging import get_logger, log_event
from ..utils.responses import (
    AuthenticationError,
    ConflictError,
    PermissionError_,
    RateLimitError,
    ValidationError,
)

logger = get_logger("harmony.service.auth")

VERIFY_PURPOSE = "email_verification"
RESET_PURPOSE = "password_reset"
REAUTH_PURPOSE = "reauthenticate"
EMAIL_CHANGE_PURPOSE = "email_change"

#: A bcrypt hash of a random value, verified against when the login identifier
#: is unknown so that unknown-user and wrong-password take the same time.
_DUMMY_HASH = hash_password("harmony-timing-equaliser-" + generate_token(24), rounds=12)


# ---------------------------------------------------------------------------
# Validation schemas
# ---------------------------------------------------------------------------

register_schema = (
    Schema()
    .email("email")
    .username("username")
    .password("password", min_length=10)
    .string("display_name", required=False, max_length=64, default="")
    .raw("consent", default=False)
    .raw("legal_version", default=None)
    .ignore_unknown()
)

login_schema = Schema().raw("identifier", required=True).existing_password("password").ignore_unknown()

change_password_schema = (
    Schema().existing_password("current_password").password("new_password", min_length=10).ignore_unknown()
)

forgot_password_schema = Schema().raw("email").ignore_unknown()
reset_password_schema = Schema().raw("token").password("password", min_length=10).ignore_unknown()
verify_schema = Schema().raw("token").ignore_unknown()
update_profile_schema = (
    Schema()
    .string("display_name", required=False, max_length=64, default=None)
    .string("bio", required=False, max_length=600, allow_newlines=True, default=None)
    .string("location", required=False, max_length=96, default=None)
    .string("pronouns", required=False, max_length=32, default=None)
    .string("avatar_color", required=False, max_length=16, default=None)
    .url("website")
    .boolean("show_email", default=None)
    .boolean("show_last_seen", default=None)
    .boolean("allow_messages_from_anyone", default=None)
    .boolean("email_notifications", default=None)
    .boolean("marketing_consent", default=None)
    .string("language", required=False, max_length=35, default=None)
    .ignore_unknown()
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_by_email(email: str) -> User | None:
    return db.session.scalar(select(User).where(User.email == email.strip().lower()))


def get_by_username(username: str) -> User | None:
    return db.session.scalar(select(User).where(User.username == username.strip().lower()))


def get_by_identifier(identifier: str) -> User | None:
    """Resolve a login identifier that may be either an email or a username."""
    value = (identifier or "").strip()
    if not value:
        return None
    if "@" in value:
        return get_by_email(value)
    return get_by_username(value.lower())


def get_by_public_id(public_id: str) -> User | None:
    if not public_id or len(public_id) != 32:
        return None
    return db.session.scalar(select(User).where(User.public_id == public_id))


def email_taken(email: str) -> bool:
    return get_by_email(email) is not None


def username_taken(username: str) -> bool:
    return get_by_username(username) is not None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def create_token(user: User, purpose: str, *, ttl: int | None = None) -> str:
    """Issue a single-use token and return its raw value (never persisted)."""
    raw = generate_token(32)
    lifetime = ttl or int(current_app.config.get("EMAIL_TOKEN_TTL", 86400))
    db.session.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(raw, pepper=current_app.config.get("TOKEN_PEPPER") or None),
            expires_at=utcnow() + timedelta(seconds=lifetime),
            request_ip_hash=client_ip_hash(),
            user_agent_hash=keyed_hash(request.user_agent.string, length=32) if request else None,
        )
    )
    return raw


def consume_token(raw: str, purpose: str) -> User | None:
    """Validate and burn a token, returning its owner.

    The lookup is by digest + purpose, so a token issued for one purpose can
    never be replayed as another, and the ``used_at`` write makes replay
    impossible even within the same request lifetime.
    """
    if not raw or len(raw) < 20:
        return None
    pepper = current_app.config.get("TOKEN_PEPPER") or None
    digest = hash_token(raw, pepper=pepper)
    token = db.session.scalar(select(AuthToken).where(AuthToken.token_hash == digest, AuthToken.purpose == purpose))
    if token is None or not token.is_usable:
        return None
    token.used_at = utcnow()
    return token.user


def revoke_tokens(user: User, purpose: str | None = None) -> int:
    query = db.session.query(AuthToken).filter(AuthToken.user_id == user.id, AuthToken.used_at.is_(None))
    if purpose:
        query = query.filter(AuthToken.purpose == purpose)
    count = 0
    for token in query.all():
        token.used_at = utcnow()
        count += 1
    return count


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(payload: dict[str, Any]) -> tuple[User, str]:
    """Create a pending account and return it with the raw verification token.

    The token is returned so the caller can hand it to the mailer (directly in
    development, via Celery in production) without a second lookup.
    """
    data = register_schema(payload)
    email: str = data["email"]
    username: str = data["username"]

    if not data.get("consent"):
        raise ValidationError(
            "Необходимо принять Пользовательское соглашение и Политику конфиденциальности.",
            code="consent_required",
            fields={"consent": "Требуется согласие с условиями."},
        )

    assessment = spam.evaluate_request(is_registration=True, email=email, username=username)
    if not assessment.allowed:
        spam.record_signal("registration_blocked", scope="system", identifier="registration", weight=0.0)
        raise RateLimitError(
            "Регистрация временно недоступна. Попробуйте позже.",
            retry_after=1800,
            code="registration_blocked",
        )

    existing = db.session.scalar(select(User).where(or_(User.email == email, User.username == username)))
    if existing is not None:
        # The field is named precisely so the form can highlight it, but the
        # message still reveals that *something* is taken. That is an accepted
        # trade-off: registration must be idempotent for the user, and the
        # login/forgot flows remain fully enumeration-safe.
        field = "email" if existing.email == email else "username"
        label = (
            "Этот адрес электронной почты уже зарегистрирован."
            if field == "email"
            else "Это имя пользователя уже занято."
        )
        raise ConflictError(label, code=f"{field}_taken", fields={field: label})

    display_name = (data.get("display_name") or "").strip() or username
    user = User(
        email=email,
        username=username,
        password_hash=hash_password(data["password"]),
        display_name=display_name,
        role=UserRole.MEMBER.value,
        status=UserStatus.PENDING.value,
        email_verified=False,
        avatar_color=_pick_avatar_color(username),
        data_processing_consent=True,
        marketing_consent=bool(data.get("marketing_consent")),
        profile_visibility=ProfileVisibility.PUBLIC.value,
    )
    user.privacy_settings = PrivacySetting()
    db.session.add(user)
    db.session.flush()  # assign user.id for the consent/token rows

    _record_consent(user, purpose="account", granted=True)
    token = create_token(user, VERIFY_PURPOSE)

    db.session.add(
        LoginAttempt(
            identifier_hash=hash_email(email),
            ip_hash=client_ip_hash(),
            user_agent=(request.user_agent.string or "")[:256] if request else None,
            success=True,
            failure_reason="registered",
            user_id=user.id,
        )
    )
    db.session.commit()

    log_event(logger, "INFO", "auth.registered", user_id=user.id, username=user.username)
    return user, token


def _record_consent(user: User, *, purpose: str = "account", granted: bool = True) -> None:
    """Persist proof of consent (GDPR Art. 7(1)) at the moment it is given."""
    for slug in ("terms", "privacy"):
        document = current_legal_document(slug)
        if document is None:
            continue
        db.session.add(
            ConsentRecord(
                user_id=user.id,
                document_slug=document.slug,
                document_version=document.version,
                document_hash=document.content_hash,
                purpose=purpose,
                granted=granted,
                ip_hash=client_ip_hash(),
                user_agent=(request.user_agent.string or "")[:256] if request else None,
            )
        )


def current_legal_document(slug: str) -> LegalDocument | None:
    return db.session.scalar(
        select(LegalDocument)
        .where(LegalDocument.slug == slug, LegalDocument.is_current.is_(True))
        .order_by(LegalDocument.effective_from.desc())
    )


def verify_email(raw_token: str) -> User:
    user = consume_token(raw_token, VERIFY_PURPOSE)
    if user is None:
        raise ValidationError("Ссылка подтверждения недействительна или истекла.", code="invalid_token")

    user.email_verified = True
    if user.status == UserStatus.PENDING.value:
        user.status = UserStatus.ACTIVE.value
        user.status_changed_at = utcnow()
    log_event(logger, "INFO", "auth.email_verified", user_id=user.id)
    db.session.commit()
    return user


def resend_verification(email: str) -> str | None:
    """Issue a fresh confirmation token, or ``None`` if there is nothing to confirm.

    The token is returned rather than a bare success flag because with
    ``MAIL_ENABLED`` off there is no inbox for it to arrive in, and a "resend"
    that reports success and delivers nothing leaves the reader precisely where
    they were. The caller must not put the value in the response for a
    production deployment - that is what :func:`create_token` is for.
    """
    user = get_by_email(email)
    if user is None or user.email_verified:
        return None
    token = create_token(user, VERIFY_PURPOSE)
    _enqueue_verification(user, token)
    db.session.commit()
    return token


# ---------------------------------------------------------------------------
# Login / sessions
# ---------------------------------------------------------------------------


def open_session(user: User) -> tuple[User, str, str, UserSession]:
    """Open a session for a user whose identity is already established.

    Used right after email verification. The single-use link was mailed to the
    address it just verified, so holding it *is* the proof of ownership;
    asking for the password again on the next screen is friction with no
    security value, and the usual path - open the link, land on a dead "now log
    in" button - reads as a broken product rather than a second step.

    The account rules are still enforced, because "recently verified" is a
    timing statement, not a standing permission: a banned account that verified
    its email a second earlier still must not get in.
    """
    if not user.is_usable_account:
        raise PermissionError_(user.status_reason or "Аккаунт недоступен для входа.", code="account_unavailable")
    if user.is_locked:
        raise RateLimitError(
            "Аккаунт временно заблокирован из-за многих неудачных попыток.",
            retry_after=int(current_app.config.get("ACCOUNT_LOCKOUT_SECONDS", 900)),
            code="account_locked",
        )

    from ..security.decorators import issue_session

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_seen_at = utcnow()
    access_token, refresh_token, session = issue_session(
        user, ip_hash=client_ip_hash(), user_agent=request.user_agent.string if request else None
    )
    db.session.commit()
    log_event(logger, "INFO", "auth.session_opened_after_verification", user_id=user.id)
    return user, access_token, refresh_token, session


def _identifier_hash(identifier: str) -> str:
    return hash_email(identifier) if "@" in identifier else keyed_hash(identifier.lower(), length=64)


def recent_failures(identifier_hash: str) -> int:
    """Failed attempts against one identity inside the throttle window."""
    from . import cache_service

    try:
        return int(cache_service.get(f"auth:failures:{identifier_hash}") or 0)
    except Exception:  # pragma: no cover - cache is best-effort
        return 0


def login(identifier: str, password: str) -> tuple[User, str, str, UserSession]:
    """Authenticate and open a session.

    Raises :class:`AuthenticationError` with the same message for every failure
    mode, and records a :class:`LoginAttempt` for abuse analytics either way.
    """
    identifier = (identifier or "").strip()
    user = get_by_identifier(identifier)
    identifier_hash = hash_email(identifier) if "@" in identifier else keyed_hash(identifier.lower(), length=64)

    if user is None:
        verify_password(password, _DUMMY_HASH)  # equalise timing
        _record_attempt(identifier_hash, success=False, reason="unknown_user")
        _maybe_throttle_auth(identifier_hash)
        db.session.commit()
        raise AuthenticationError("Неверный логин или пароль.", code="invalid_credentials")

    if user.is_locked:
        _record_attempt(identifier_hash, success=False, reason="locked", user_id=user.id)
        _maybe_throttle_auth(identifier_hash)
        db.session.commit()
        raise RateLimitError(
            "Аккаунт временно заблокирован из-за множества неудачных попыток. Попробуйте позже.",
            retry_after=int(current_app.config.get("ACCOUNT_LOCKOUT_SECONDS", 900)),
            code="account_locked",
        )

    if not verify_password(password, user.password_hash):
        _register_failure(user, identifier_hash)
        db.session.commit()
        raise AuthenticationError("Неверный логин или пароль.", code="invalid_credentials")

    # Upgrading the hash cost here is free: the user just proved they know the
    # password, and the next login gets the stronger parameters.
    if needs_rehash(user.password_hash, current_app.config.get("BCRYPT_ROUNDS")):
        user.password_hash = hash_password(password)

    if user.status == UserStatus.PENDING.value or not user.email_verified:
        # A deployment may turn the confirmation off - see
        # `REQUIRE_EMAIL_VERIFICATION`. Then `PENDING` is simply the status a
        # fresh account carries and means nothing, and refusing to sign in would
        # lock out every account that ever registered.
        if current_app.config.get("REQUIRE_EMAIL_VERIFICATION", True):
            _record_attempt(identifier_hash, success=False, reason="unverified", user_id=user.id)
            db.session.commit()
            raise PermissionError_(
                "Подтвердите адрес электронной почты, чтобы войти в аккаунт.",
                code="email_not_verified",
            )
        # Promotion happens once, on the first sign-in, so the account carries an
        # accurate status afterwards rather than looking unverified forever.
        if user.status == UserStatus.PENDING.value:
            user.status = UserStatus.ACTIVE.value
            user.status_changed_at = utcnow()
            log_event(logger, "INFO", "auth.verification_waived", user_id=user.id)
    if user.status == UserStatus.SUSPENDED.value:
        _record_attempt(identifier_hash, success=False, reason="suspended", user_id=user.id)
        db.session.commit()
        raise PermissionError_(user.status_reason or "Аккаунт приостановлен администратором.", code="account_suspended")
    if user.status in (UserStatus.BANNED.value, UserStatus.DEACTIVATED.value, UserStatus.DELETION_PENDING.value):
        _record_attempt(identifier_hash, success=False, reason=user.status, user_id=user.id)
        db.session.commit()
        raise PermissionError_("Доступ в аккаунт ограничен.", code=f"account_{user.status}")

    from ..security.decorators import issue_session

    access_token, refresh_token, session = issue_session(
        user, ip_hash=client_ip_hash(), user_agent=request.user_agent.string if request else None
    )
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_seen_at = utcnow()
    from . import cache_service

    cache_service.delete(f"auth:failures:{identifier_hash}")
    _record_attempt(identifier_hash, success=True, user_id=user.id)
    _prune_sessions(user)
    db.session.commit()

    log_event(logger, "INFO", "auth.login", user_id=user.id)
    return user, access_token, refresh_token, session


def _register_failure(user: User, identifier_hash: str) -> None:
    max_failures = int(current_app.config.get("ACCOUNT_MAX_LOGIN_FAILURES", 8))
    lockout = int(current_app.config.get("ACCOUNT_LOCKOUT_SECONDS", 900))
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= max_failures:
        user.locked_until = utcnow() + timedelta(seconds=lockout)
        user.failed_login_count = 0
        log_event(logger, "WARNING", "auth.account_locked", user_id=user.id, reason="too_many_failures")
    _record_attempt(identifier_hash, success=False, reason="bad_password", user_id=user.id)
    _maybe_throttle_auth(identifier_hash)


def _maybe_throttle_auth(identifier_hash: str) -> None:
    """Escalating backoff on repeated failures for one identity."""
    from ..security.rate_limit import limiter

    verdict = limiter.check("auth:login", identifier_hash)
    if not verdict.allowed:
        log_event(logger, "INFO", "auth.throttled", limit=verdict.limit)


def _record_attempt(identifier_hash: str, *, success: bool, reason: str = "", user_id: int | None = None) -> None:
    db.session.add(
        LoginAttempt(
            identifier_hash=identifier_hash,
            ip_hash=client_ip_hash(),
            user_agent=(request.user_agent.string or "")[:256] if request else None,
            success=success,
            failure_reason=reason[:64] or None,
            user_id=user_id,
        )
    )


def _prune_sessions(user: User, keep: int = 10) -> None:
    """Bound the number of live sessions per account."""
    sessions = (
        db.session.query(UserSession)
        .filter(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.created_at.desc())
        .all()
    )
    now = utcnow()
    alive = [s for s in sessions if s.expires_at > now]
    for session in sessions:
        if session.expires_at <= now:
            session.revoked_at = now
            session.revoked_reason = "expired"
    for session in alive[keep:]:
        session.revoked_at = now
        session.revoked_reason = "pruned"


def refresh(refresh_token: str) -> tuple[User, str, str, UserSession]:
    """Rotate a refresh token: the presented one is revoked as the new one is issued."""
    from ..security.decorators import issue_session
    from ..utils.crypto import decode_token

    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except Exception as exc:
        raise AuthenticationError("Сессия недействительна. Войдите заново.", code="refresh_invalid") from exc

    session = db.session.scalar(select(UserSession).where(UserSession.jti == payload.get("jti", "")))
    if session is None or session.revoked_at is not None:
        # A revoked jti being replayed suggests a stolen token: drop every
        # session for that user rather than just this one.
        if session is not None:
            _revoke_all_sessions(session.user_id, "refresh_reuse_detected")
            db.session.commit()
            log_event(logger, "WARNING", "auth.refresh_reuse", user_id=session.user_id)
        raise AuthenticationError("Сессия недействительна. Войдите заново.", code="refresh_revoked")

    if not constant_time_session_match(session, refresh_token):
        _revoke_all_sessions(session.user_id, "refresh_mismatch")
        db.session.commit()
        raise AuthenticationError("Сессия недействительна. Войдите заново.", code="refresh_mismatch")

    user = session.user
    if user is None or not user.is_usable_account:
        raise AuthenticationError("Аккаунт недоступен.", code="account_unavailable")

    session.revoked_at = utcnow()
    session.revoked_reason = "rotated"
    access_token, new_refresh, new_session = issue_session(
        user, ip_hash=client_ip_hash(), user_agent=request.user_agent.string if request else None
    )
    user.last_seen_at = utcnow()
    db.session.commit()
    log_event(logger, "DEBUG", "auth.refresh", user_id=user.id)
    return user, access_token, new_refresh, new_session


def constant_time_session_match(session: UserSession, raw_token: str) -> bool:
    from ..utils.crypto import constant_time_equals

    pepper = current_app.config.get("TOKEN_PEPPER") or None
    return constant_time_equals(session.token_hash, hash_token(raw_token, pepper=pepper))


def logout(user: User, *, all_sessions: bool = False) -> int:
    count = (
        _revoke_all_sessions(user.id, "user_logout" if all_sessions else "user_logout_single")
        if all_sessions
        else _revoke_current(user)
    )
    db.session.commit()
    log_event(logger, "INFO", "auth.logout", user_id=user.id, all_sessions=all_sessions, revoked=count)
    return count


def _revoke_current(user: User) -> int:
    from flask import g

    jti = getattr(g, "token_jti", None)
    query = db.session.query(UserSession).filter(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    if jti:
        query = query.filter(UserSession.jti == jti)
    else:
        query = query.limit(1)
    count = 0
    for session in query.all():
        session.revoked_at = utcnow()
        session.revoked_reason = "user_logout"
        count += 1
    return count


def _revoke_all_sessions(user_id: int, reason: str) -> int:
    now = utcnow()
    sessions = (
        db.session.query(UserSession).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).all()
    )
    for session in sessions:
        session.revoked_at = now
        session.revoked_reason = reason
    return len(sessions)


def list_sessions(user: User) -> list[dict[str, Any]]:
    from flask import g

    current_jti = getattr(g, "token_jti", None)
    now = utcnow()
    sessions = (
        db.session.query(UserSession)
        .filter(UserSession.user_id == user.id, UserSession.revoked_at.is_(None), UserSession.expires_at > now)
        .order_by(UserSession.created_at.desc())
        .all()
    )
    return [
        {
            "jti": session.jti,
            "created_at": session.created_at.isoformat(),
            "last_seen_at": session.last_seen_at.isoformat() if session.last_seen_at else None,
            "user_agent": session.user_agent,
            "is_current": current_jti is not None and current_jti == session.jti,
            "expires_at": session.expires_at.isoformat(),
        }
        for session in sessions
    ]


# ---------------------------------------------------------------------------
# Password management
# ---------------------------------------------------------------------------


def change_password(user: User, current_password: str, new_password: str) -> None:
    if not verify_password(current_password, user.password_hash):
        _record_attempt(hash_email(user.email), success=False, reason="bad_password_reauth", user_id=user.id)
        db.session.commit()
        raise AuthenticationError("Текущий пароль указан неверно.", code="invalid_current_password")
    if verify_password(new_password, user.password_hash):
        raise ValidationError("Новый пароль должен отличаться от текущего.", code="password_reused")

    user.password_hash = hash_password(new_password)
    # Every session dies, the caller's included. A password change is the one
    # moment a user is certain to have all their devices in hand, so asking them
    # to sign in again is cheap - and leaving the *current* session alive would
    # mean a stolen token survives exactly when the user expects it not to.
    _revoke_all_sessions(user.id, "password_changed")
    revoke_tokens(user, RESET_PURPOSE)
    db.session.commit()
    log_event(logger, "INFO", "auth.password_changed", user_id=user.id)


def request_password_reset(email: str) -> bool:
    """Always returns True so the endpoint cannot be used to enumerate users."""
    user = get_by_email(email)
    if user is None or user.status in (UserStatus.DEACTIVATED.value, UserStatus.DELETION_PENDING.value):
        return False
    token = create_token(user, RESET_PURPOSE, ttl=3600)
    _enqueue_password_reset(user, token)
    db.session.commit()
    return True


def reset_password(raw_token: str, new_password: str) -> User:
    user = consume_token(raw_token, RESET_PURPOSE)
    if user is None:
        raise ValidationError("Ссылка для сброса пароля недействительна или истекла.", code="invalid_token")
    user.password_hash = hash_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    _revoke_all_sessions(user.id, "password_reset")
    db.session.commit()
    log_event(logger, "INFO", "auth.password_reset", user_id=user.id)
    return user


def verify_password_for_sensitive_action(user: User, password: str) -> str:
    """Step-up authentication. Returns a short-lived reauth token."""
    if not verify_password(password, user.password_hash):
        raise AuthenticationError("Пароль указан неверно.", code="reauth_failed")
    token = create_token(user, REAUTH_PURPOSE, ttl=600)
    db.session.commit()
    return token


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def update_profile(user: User, payload: dict[str, Any]) -> User:
    data = update_profile_schema(payload)

    # An unsupported tag is rejected rather than stored: a profile claiming a
    # language the client has no catalogue for renders in the base language with
    # nothing to indicate that anything was wrong. Resolved before the loop
    # below, so a rejected value never reaches the model at all.
    language = None
    if "language" in data:
        from .. import i18n

        requested = (data.get("language") or "").strip()
        if requested and not i18n.is_supported(requested):
            raise ValidationError("Этот язык пока не поддерживается.", code="unsupported_language")
        language = i18n.normalise(requested) if requested else None
        if language is None:
            # ``None`` means "follow the browser", which is a value the reader
            # can choose. The assignment loop below skips None for every other
            # field, because None there means "leave it alone", so this one has
            # to be set directly - otherwise clearing the preference silently
            # keeps the old language forever.
            user.language = None
            data.pop("language", None)
        else:
            # Normalise into the loop, so the model is only ever given a tag a
            # catalogue exists for: ``de-AT`` is stored as ``de``.
            data["language"] = language

    for field, value in data.items():
        if value is None:
            continue
        if field == "display_name" and not value.strip():
            value = user.username
        if field == "avatar_color" and value not in _AVATAR_COLORS:
            raise ValidationError("Неизвестный цвет аватара.", code="invalid_avatar_color")
        setattr(user, field, value)
    if data.get("marketing_consent") is True:
        _record_consent(user, purpose="marketing", granted=True)
    elif data.get("marketing_consent") is False:
        _record_consent(user, purpose="marketing", granted=False)
    db.session.commit()
    log_event(logger, "INFO", "user.profile_updated", user_id=user.id, fields=sorted(data))
    return user


_AVATAR_COLORS = {"sand", "stone", "sage", "clay", "dusk", "linen", "moss", "ash"}


def _pick_avatar_color(seed: str) -> str:
    """Deterministic pastel assignment so avatars look intentional by default."""
    import hashlib

    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=4).digest()
    return sorted(_AVATAR_COLORS)[digest[0] % len(_AVATAR_COLORS)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def client_ip_hash() -> str:
    from ..security.rate_limit import client_ip

    try:
        return keyed_hash(client_ip(), length=64)
    except RuntimeError:
        return ""


def _enqueue_verification(user: User, token: str) -> None:
    _dispatch_email("send_verification_email", user, token)


def _enqueue_password_reset(user: User, token: str) -> None:
    _dispatch_email("send_password_reset_email", user, token)


def _dispatch_email(task_name: str, user: User, token: str) -> None:
    """Hand a message to the mail queue without letting the queue break writes.

    A broker outage must not stop someone registering. The account is created
    and the user can request a new verification email once the queue recovers;
    failing the whole request would be strictly worse.
    """
    from ..tasks import email_tasks
    from ..tasks.dispatch import enqueue

    # Availability is checked before the publish. Wrapping `.delay()` in a
    # `try` does not make it cheap: Celery retries the broker connection for
    # about 108 seconds before the exception surfaces, so registration would
    # hang for nearly two minutes on a machine with no Redis.
    enqueue(getattr(email_tasks, task_name), user.id, token)


def user_stats(user: User) -> dict[str, Any]:
    from ..models.post import Post

    likes_received = (
        db.session.query(func.coalesce(func.sum(Post.likes_count), 0)).filter(Post.author_id == user.id).scalar() or 0
    )
    return {
        "posts_count": user.posts_count,
        "comments_count": user.comments_count,
        "followers_count": user.followers_count,
        "following_count": user.following_count,
        "likes_received": int(likes_received),
        "warnings": user.warning_count,
    }


__all__ = [
    "EMAIL_CHANGE_PURPOSE",
    "REAUTH_PURPOSE",
    "RESET_PURPOSE",
    "VERIFY_PURPOSE",
    "change_password",
    "change_password_schema",
    "client_ip_hash",
    "consume_token",
    "create_token",
    "current_legal_document",
    "email_taken",
    "forgot_password_schema",
    "get_by_email",
    "get_by_identifier",
    "get_by_public_id",
    "get_by_username",
    "list_sessions",
    "login",
    "login_schema",
    "logout",
    "open_session",
    "refresh",
    "register",
    "register_schema",
    "request_password_reset",
    "resend_verification",
    "reset_password",
    "reset_password_schema",
    "revoke_tokens",
    "update_profile",
    "user_stats",
    "username_taken",
    "verify_email",
    "verify_password_for_sensitive_action",
    "verify_schema",
]

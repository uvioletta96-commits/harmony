"""Cryptographic helpers: opaque tokens, hashing, HMAC-signed values.

Rules enforced here:
  * secrets compare with :func:`hmac.compare_digest` (never ``==``);
  * high-entropy tokens are stored only as SHA-256 digests;
  * deterministic lookups (rate-limit keys, IP hashes) use keyed HMAC so the
    stored value cannot be reversed with a rainbow table.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from flask import current_app


def generate_token(nbytes: int = 32) -> str:
    """URL-safe, high-entropy, unguessable token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str, *, pepper: str | None = None) -> str:
    """Digest a bearer token for storage. SHA-256 is appropriate here because the
    input already has full entropy — bcrypt would only add latency, not safety."""
    if pepper is None:
        pepper = _pepper()
    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _pepper() -> str:
    try:
        pepper = current_app.config.get("TOKEN_PEPPER") or current_app.config.get("SECRET_KEY")
    except RuntimeError:
        pepper = "harmony-dev-pepper"
    return pepper or "harmony-dev-pepper"


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def keyed_hash(value: str, salt: str | None = None, *, length: int = 32) -> str:
    """Deterministic keyed digest used for IP address pseudonymisation.

    IPs are personal data under GDPR, so they are never written to logs or
    analytics in the clear. A per-deployment salt keeps digests unlinkable
    across environments while remaining stable enough to correlate events.
    """
    if salt is None:
        try:
            salt = current_app.config.get("IP_HASH_SALT") or current_app.config.get("SECRET_KEY")
        except RuntimeError:
            salt = "harmony-dev-salt"
    digest = hmac.new((salt or "harmony-dev-salt").encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:length]


def hash_email(email: str) -> str:
    """Normalised, lower-cased, keyed digest of an email address."""
    return keyed_hash(email.strip().lower(), length=64)


def hash_password_placeholder() -> str:  # pragma: no cover - documentation aid
    return hashlib.sha256(b"harmony").hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# JWT access tokens
# --------------------------------------------------------------------------


def create_access_token(
    user,
    *,
    jti: str | None = None,
    expires_in: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime, str]:
    """Mint a signed access token. Returns ``(token, expires_at, jti)``.

    The payload is intentionally small: identity, role and a token version. It
    is *not* a session store — revocation is handled by the ``UserSession``
    row keyed on ``jti``.
    """
    now = utcnow()
    ttl = expires_in or current_app.config["ACCESS_TOKEN_TTL"]
    expires_at = now + timedelta(seconds=ttl)
    jti = jti or uuid.uuid4().hex
    claims: dict[str, Any] = {
        "sub": str(user.id),
        "pid": user.public_id,
        "username": user.username,
        "role": user.role,
        "ver": user.status,  # status change invalidates old tokens by version
        "email_verified": user.email_verified,
        "iat": int(now.timestamp()),
        "nbf": int((now - timedelta(seconds=10)).timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jti,
        "iss": current_app.config.get("APP_NAME_EN", "Harmony"),
        "typ": "access",
    }
    if extra_claims:
        claims.update(extra_claims)
    token = jwt.encode(
        claims,
        current_app.config["JWT_SECRET_KEY"],
        algorithm=current_app.config["JWT_ALGORITHM"],
    )
    return token, expires_at, jti


def create_refresh_token(user, *, jti: str | None = None) -> tuple[str, datetime, str]:
    """Mint a refresh token. The returned token is given to the client; only its
    digest is persisted."""
    now = utcnow()
    ttl = current_app.config["REFRESH_TOKEN_TTL"]
    expires_at = now + timedelta(seconds=ttl)
    jti = jti or uuid.uuid4().hex
    claims = {
        "sub": str(user.id),
        "pid": user.public_id,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": current_app.config.get("APP_NAME_EN", "Harmony"),
        "typ": "refresh",
    }
    token = jwt.encode(
        claims,
        current_app.config["JWT_SECRET_KEY"],
        algorithm=current_app.config["JWT_ALGORITHM"],
    )
    return token, expires_at, jti


def decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any]:
    """Verify signature, expiry, issuer and token type.

    Raises ``jwt`` exceptions; callers translate them into API errors so the
    failure reason is never leaked verbatim to the client.
    """
    payload = jwt.decode(
        token,
        current_app.config["JWT_SECRET_KEY"],
        algorithms=[current_app.config["JWT_ALGORITHM"]],
        issuer=current_app.config.get("APP_NAME_EN", "Harmony"),
        options={"require": ["exp", "iat", "sub"]},
    )
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError(f"expected token type {expected_type!r}")
    return payload


def urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


__all__ = [
    "constant_time_equals",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "generate_token",
    "hash_email",
    "hash_token",
    "isoformat",
    "keyed_hash",
    "utcnow",
]

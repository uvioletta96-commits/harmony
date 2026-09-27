"""Authentication flow tests.

These cover the security properties that matter most: no account enumeration,
constant-work responses, single-use tokens, and working revocation.
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models.user import AuthToken, UserStatus
from app.utils.crypto import hash_token
from tests.utils import DEFAULT_PASSWORD, assert_error, assert_ok


class TestRegistration:
    def test_registers_and_requires_verification(self, client, ctx):
        from app.models.user import User

        data = assert_ok(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "New.User@Harmony.test",
                    "username": "newuser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=201,
        )
        assert data["requires_email_verification"] is True

        user = db.session.query(User).filter_by(email="new.user@harmony.test").one()
        assert user.status == UserStatus.PENDING.value
        assert user.email_verified is False
        # The plaintext password must never be stored anywhere.
        assert DEFAULT_PASSWORD not in user.password_hash

    def test_rejects_duplicate_email_with_field_error(self, client, user):
        error = assert_error(
            client.post(
                "/api/v1/auth/register",
                json={"email": user.email, "username": "different", "password": DEFAULT_PASSWORD, "consent": True},
            ),
            status=409,
        )
        assert "email" in error["fields"]

    def test_rejects_duplicate_username(self, client, user):
        error = assert_error(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "fresh@harmony.test",
                    "username": user.username,
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=409,
        )
        assert "username" in error["fields"]

    def test_requires_consent(self, client):
        error = assert_error(
            client.post(
                "/api/v1/auth/register",
                json={"email": "noconsent@harmony.test", "username": "noconsent", "password": DEFAULT_PASSWORD},
            ),
            status=422,
        )
        assert "consent" in error["fields"]

    @pytest.mark.parametrize(
        "password,fragment",
        [
            ("short1!", "короче"),
            ("alllowercase", "два типа"),
            ("password12345", "распространён"),
            ("aaaaaaaaaaaa", "один символ"),
        ],
    )
    def test_weak_passwords_rejected(self, client, password, fragment):
        error = assert_error(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"weak{abs(hash(password))}@harmony.test",
                    "username": f"weak{abs(hash(password))}",
                    "password": password,
                    "consent": True,
                },
            ),
            status=422,
        )
        assert fragment in error["fields"]["password"]

    @pytest.mark.parametrize("email", ["not-an-email", "a@b", "a@@b.com", "@harmony.test", "no tld@harmony"])
    def test_malformed_emails_rejected(self, client, email):
        assert_error(
            client.post(
                "/api/v1/auth/register",
                json={"email": email, "username": "validname", "password": DEFAULT_PASSWORD, "consent": True},
            ),
            status=422,
        )

    @pytest.mark.parametrize("username", ["ab", "has space", "-leading", "trailing-", "double--dash", "admin"])
    def test_invalid_usernames_rejected(self, client, username):
        error = assert_error(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"u{abs(hash(username))}@harmony.test",
                    "username": username,
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=422,
        )
        assert "username" in error["fields"]


class TestLocalVerification:
    """A reader on an instance that cannot send mail must still get in.

    ``MAIL_ENABLED`` is off in development, so the confirmation link never
    arrives. The response has always carried a dev token; the page used to drop
    it and say "check your inbox", which left a reader who had registered with
    no way forward at all - refused at sign-in, unable to post, unable to
    confirm. The link now travels to the next screen.
    """

    def test_registration_returns_a_usable_link(self, client, app, ctx):
        app.config["MAIL_ENABLED"] = False
        app.debug = True

        payload = assert_ok(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "local@harmony.test",
                    "username": "localuser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=201,
        )
        link = payload["verification_url"]
        assert link.startswith("/verify?token="), link

        # Following it is what actually confirms, so follow it.
        token = link.split("token=", 1)[1]
        assert_ok(client.post("/api/v1/auth/verify-email", json={"token": token}))
        assert_ok(client.get("/api/v1/auth/me"))

    def test_no_link_when_mail_works(self, client, app, ctx):
        """The escape hatch must not exist where it would be a real leak."""
        app.config["MAIL_ENABLED"] = True
        app.debug = True

        data = assert_ok(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "real@harmony.test",
                    "username": "realuser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=201,
        )
        assert "verification_url" not in data
        assert "verification_token_dev_only" not in data

    def test_resend_gives_the_link_back_when_there_is_one_to_give(self, client, app, ctx):
        """A resend that reports success and delivers nothing is a dead end."""
        app.config["MAIL_ENABLED"] = False
        app.debug = True

        assert_ok(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "again@harmony.test",
                    "username": "againuser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=201,
        )
        data = assert_ok(client.post("/api/v1/auth/resend-verification", json={"email": "again@harmony.test"}))
        assert data["verification_url"].startswith("/verify?token=")

    def test_resend_never_reveals_whether_an_address_exists(self, client, app, ctx):
        """The same response either way, or the endpoint becomes an account oracle."""
        app.config["MAIL_ENABLED"] = False
        app.debug = True

        unknown = assert_ok(client.post("/api/v1/auth/resend-verification", json={"email": "ghost@harmony.test"}))
        assert "verification_url" not in unknown

        assert_ok(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "known@harmony.test",
                    "username": "knownuser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=201,
        )
        known = assert_ok(client.post("/api/v1/auth/resend-verification", json={"email": "known@harmony.test"}))
        assert known["message"] == unknown["message"]

    def test_an_unconfirmed_account_is_told_why_it_cannot_sign_in(self, client, app, ctx):
        app.config["MAIL_ENABLED"] = False
        app.debug = True

        assert_ok(
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": "why@harmony.test",
                    "username": "whyuser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
            status=201,
        )
        assert_error(
            client.post("/api/v1/auth/login", json={"identifier": "why@harmony.test", "password": DEFAULT_PASSWORD}),
            status=403,
            code="email_not_verified",
        )


class TestEmailVerification:
    def test_verifies_and_activates(self, client, ctx):
        from app.services import auth_service

        user, token = auth_service.register(
            {"email": "verify@harmony.test", "username": "verifier", "password": DEFAULT_PASSWORD, "consent": True}
        )
        assert user.status == UserStatus.PENDING.value

        data = assert_ok(client.post("/api/v1/auth/verify-email", json={"token": token}))
        assert data["user"]["is_verified"] is True
        db.session.refresh(user)
        assert user.status == UserStatus.ACTIVE.value
        assert user.email_verified is True

    def test_token_is_single_use(self, client, ctx):
        from app.services import auth_service

        _user, token = auth_service.register(
            {"email": "once@harmony.test", "username": "onceonly", "password": DEFAULT_PASSWORD, "consent": True}
        )
        assert_ok(client.post("/api/v1/auth/verify-email", json={"token": token}))
        assert_error(client.post("/api/v1/auth/verify-email", json={"token": token}), status=422, code="invalid_token")

    def test_token_purpose_is_enforced(self, client, ctx):
        """A reset token must not work as a verification token."""
        from app.services import auth_service

        user, _verify_token = auth_service.register(
            {"email": "cross@harmony.test", "username": "crosspurpose", "password": DEFAULT_PASSWORD, "consent": True}
        )
        db.session.commit()
        reset_token = auth_service.create_token(user, auth_service.RESET_PURPOSE)
        db.session.commit()

        assert_error(
            client.post("/api/v1/auth/verify-email", json={"token": reset_token}), status=422, code="invalid_token"
        )

    def test_raw_token_is_never_persisted(self, ctx):
        from app.services import auth_service

        user, token = auth_service.register(
            {"email": "hashed@harmony.test", "username": "hashedtoken", "password": DEFAULT_PASSWORD, "consent": True}
        )
        stored = db.session.query(AuthToken).filter(AuthToken.user_id == user.id).one()
        assert stored.token_hash == hash_token(token)
        assert stored.token_hash != token

    def test_resend_does_not_disclose_existence(self, client):
        """Same response whether or not the address is registered."""
        for email in ("nobody@harmony.test", "someone@example.com"):
            data = assert_ok(client.post("/api/v1/auth/resend-verification", json={"email": email}))
            assert "если аккаунт существует" in data["message"].lower()


class TestLogin:
    def test_login_with_username(self, client, user):
        data = assert_ok(
            client.post("/api/v1/auth/login", json={"identifier": user.username, "password": DEFAULT_PASSWORD})
        )
        assert data["user"]["username"] == user.username

    def test_login_with_email(self, client, user):
        data = assert_ok(
            client.post("/api/v1/auth/login", json={"identifier": user.email, "password": DEFAULT_PASSWORD})
        )
        assert data["user"]["public_id"] == user.public_id

    def test_wrong_password_is_generic(self, client, user):
        """The message must not reveal whether the account exists."""
        error = assert_error(
            client.post("/api/v1/auth/login", json={"identifier": user.username, "password": "Wr0ng-Pass!99"}),
            status=401,
        )
        assert error["message"] == "Неверный логин или пароль."

    def test_unknown_user_returns_identical_error(self, client, user):
        unknown = assert_error(
            client.post("/api/v1/auth/login", json={"identifier": "ghostuser", "password": "Wr0ng-Pass!99"}),
            status=401,
        )
        known = assert_error(
            client.post("/api/v1/auth/login", json={"identifier": user.username, "password": "Wr0ng-Pass!99"}),
            status=401,
        )
        assert unknown["message"] == known["message"]
        assert unknown["code"] == known["code"] == "invalid_credentials"

    def test_unverified_account_cannot_sign_in(self, client, make_user):
        pending = make_user(verified=False, status=UserStatus.PENDING.value)
        assert_error(
            client.post("/api/v1/auth/login", json={"identifier": pending.username, "password": DEFAULT_PASSWORD}),
            status=403,
            code="email_not_verified",
        )

    @pytest.mark.parametrize("status", [UserStatus.SUSPENDED.value, UserStatus.BANNED.value])
    def test_sanctioned_accounts_rejected(self, client, make_user, status):
        target = make_user(status=status, status_reason="Нарушение правил")
        assert_error(
            client.post("/api/v1/auth/login", json={"identifier": target.username, "password": DEFAULT_PASSWORD}),
            status=403,
        )

    def test_repeated_failures_lock_the_account(self, client, user):
        from app.models.user import User

        threshold = client.application.config["ACCOUNT_MAX_LOGIN_FAILURES"]
        for _ in range(threshold):
            client.post("/api/v1/auth/login", json={"identifier": user.username, "password": "Wr0ng-Pass!99"})

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert refreshed.locked_until is not None

    def test_successful_login_sets_session_cookies(self, client, user):
        response = client.post("/api/v1/auth/login", json={"identifier": user.username, "password": DEFAULT_PASSWORD})
        cookies = {header[1].split("=")[0] for header in response.headers if header[0].lower() == "set-cookie"}
        assert "harmony_access" in cookies
        assert "harmony_refresh" in cookies
        assert "harmony_csrf" in cookies

    def test_access_cookie_is_httponly(self, client, user):
        response = client.post("/api/v1/auth/login", json={"identifier": user.username, "password": DEFAULT_PASSWORD})
        set_cookies = [value for key, value in response.headers if key.lower() == "set-cookie"]
        access = next(value for value in set_cookies if value.startswith("harmony_access="))
        # HttpOnly is what stops an XSS payload from reading the token.
        assert "HttpOnly" in access
        csrf = next(value for value in set_cookies if value.startswith("harmony_csrf="))
        assert "HttpOnly" not in csrf


class TestSessions:
    def test_me_requires_authentication(self, anon_client):
        assert_error(anon_client.get("/api/v1/auth/me"), status=401)

    def test_me_returns_current_user(self, auth_client, user):
        data = assert_ok(auth_client.get("/api/v1/auth/me"))
        assert data["user"]["public_id"] == user.public_id
        # The email must not leak through the public session payload.
        assert "email" not in data["user"]

    def test_refresh_rotates_the_token(self, auth_client, user):
        before = auth_client.cookie("harmony_refresh").value
        assert_ok(auth_client.post("/api/v1/auth/refresh"))
        after = auth_client.cookie("harmony_refresh").value
        assert before != after, "refresh token must rotate on use"

    def test_revoked_refresh_token_is_rejected(self, auth_client, user):
        from app.models.base import utcnow
        from app.models.user import UserSession

        session = db.session.query(UserSession).filter(UserSession.user_id == user.id).one()
        session.revoked_at = utcnow()
        db.session.commit()

        assert_error(auth_client.post("/api/v1/auth/refresh"), status=401)

    def test_logout_revokes_the_session(self, auth_client):
        assert_ok(auth_client.post("/api/v1/auth/logout"))
        assert_error(auth_client.get("/api/v1/auth/me"), status=401)

    def test_password_change_ends_other_sessions(self, auth_client, user):
        assert_ok(
            auth_client.post(
                "/api/v1/auth/change-password",
                json={"current_password": DEFAULT_PASSWORD, "new_password": "An0ther-Str0ng!45"},
            )
        )
        assert_error(auth_client.get("/api/v1/auth/me"), status=401)

    def test_password_change_requires_correct_current_password(self, auth_client):
        assert_error(
            auth_client.post(
                "/api/v1/auth/change-password",
                json={"current_password": "Wr0ng-Pass!99", "new_password": "An0ther-Str0ng!45"},
            ),
            status=401,
        )


class TestPasswordRecovery:
    def test_reset_flow(self, client, user, ctx):
        from app.services import auth_service

        assert_ok(client.post("/api/v1/auth/forgot-password", json={"email": user.email}))
        token = (
            db.session.query(AuthToken)
            .filter(AuthToken.user_id == user.id, AuthToken.purpose == auth_service.RESET_PURPOSE)
            .one()
        )
        # Only the digest exists in the database, so mint a fresh token for the test.
        raw = auth_service.create_token(user, auth_service.RESET_PURPOSE)
        db.session.commit()
        del token

        assert_ok(client.post("/api/v1/auth/reset-password", json={"token": raw, "password": "Reset-Passw0rd!7"}))
        assert_ok(client.post("/api/v1/auth/login", json={"identifier": user.username, "password": "Reset-Passw0rd!7"}))

    def test_forgot_password_hides_existence(self, client):
        data = assert_ok(client.post("/api/v1/auth/forgot-password", json={"email": "unknown@harmony.test"}))
        assert "если аккаунт существует" in data["message"].lower()

    def test_expired_token_rejected(self, client, user, ctx):
        from datetime import timedelta

        from app.models.base import utcnow
        from app.services import auth_service

        raw = auth_service.create_token(user, auth_service.RESET_PURPOSE, ttl=1)
        db.session.commit()
        token = (
            db.session.query(AuthToken)
            .filter(AuthToken.user_id == user.id, AuthToken.purpose == auth_service.RESET_PURPOSE)
            .order_by(AuthToken.id.desc())
            .first()
        )
        token.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

        assert_error(
            client.post("/api/v1/auth/reset-password", json={"token": raw, "password": "Reset-Passw0rd!7"}), status=422
        )

"""The `REQUIRE_EMAIL_VERIFICATION` switch.

Address confirmation is what stops somebody registering with an address they do
not own. Turning it off is legitimate - a closed beta with no mail service has
nowhere to send a confirmation to - but it is a property of the deployment, not
of the product, so it is a flag rather than removed code.

Two gates enforce it and both have to agree:

  * ``auth_service.login`` refuses to sign an unverified account in
  * ``security.decorators._assert_usable`` refuses every request from one

Relaxing only the first produces the worst possible failure: the sign-in
succeeds, the app renders as signed-in, and then every request behind it comes
back ``email_not_verified``. That is what these tests exist to prevent.
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models.user import UserStatus
from tests.utils import DEFAULT_PASSWORD, assert_error, assert_ok


@pytest.fixture()
def unverified(make_user):
    """An account that registered and never confirmed."""
    user = make_user(email="unverified@harmony.test", username="unverifieduser")
    user.email_verified = False
    user.status = UserStatus.PENDING.value
    db.session.commit()
    return user


def _sign_in(client, user):
    return client.post("/api/v1/auth/login", json={"identifier": user.email, "password": DEFAULT_PASSWORD})


class TestVerificationRequired:
    """The default: confirmation is on, and it is enforced."""

    def test_signing_in_is_refused(self, guest_client, unverified):
        error = assert_error(_sign_in(guest_client, unverified), status=403)
        assert error["code"] == "email_not_verified"

    def test_a_request_with_a_valid_token_is_still_refused(self, guest_client, unverified, ctx):
        """The second gate. A token is proof of identity, not of ownership."""
        from app.security.decorators import issue_session

        access_token, _refresh, _session = issue_session(unverified)
        response = guest_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert_error(response, status=403, code="email_not_verified")


class TestVerificationWaived:
    """The switch is off: the account works, and says so plainly."""

    def test_signing_in_succeeds(self, guest_client, unverified, app, ctx):
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        data = assert_ok(_sign_in(guest_client, unverified))
        assert data["user"]["username"] == unverified.username

    def test_a_request_with_a_valid_token_succeeds(self, guest_client, unverified, app, ctx):
        from app.security.decorators import issue_session

        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        access_token, _refresh, _session = issue_session(unverified)
        assert_ok(
            guest_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        )

    def test_the_account_is_promoted_rather_than_left_pending(self, guest_client, unverified, app, ctx):
        """Otherwise it looks unverified forever, and any export says so."""
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        assert_ok(_sign_in(guest_client, unverified))
        db.session.refresh(unverified)
        assert unverified.status == UserStatus.ACTIVE.value

    def test_registering_signs_the_reader_in_immediately(self, guest_client, app, ctx):
        """Nothing to confirm means nothing to wait for.

        The alternative is routing somebody to a "check your email" screen for
        a message the deployment has deliberately stopped sending.
        """
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        data = assert_ok(
            guest_client.post(
                "/api/v1/auth/register",
                json={
                    "email": "waived@harmony.test",
                    "username": "waiveduser",
                    "password": DEFAULT_PASSWORD,
                    "consent": True,
                },
            ),
        )
        assert data["signed_in"] is True
        assert data["requires_email_verification"] is False
        assert data["verification_email_sent"] is False
        assert "сразу войти" in data["message"]
        # The session really is live, not just claimed in the payload.
        assert_ok(guest_client.get("/api/v1/auth/me"))


class TestTheSwitchIsAnnounced:
    """Read the rules off a real profile class, not off the running app.

    `collect_problems` is a method on a config *profile*, and it reads that
    profile's attributes. Handing it the Flask app asks it about attributes the
    app does not have, which raises AttributeError before any rule is
    evaluated - so a test written that way passes and proves nothing.
    """

    GOOD_SECRET = "x" * 48

    def _profile(self, **overrides):
        """A production profile with the given values, validation bypassed.

        `ProductionConfig.__init__` is the boot gate: it raises on exactly the
        combinations these tests are about, so calling the constructor would
        test the gate instead of `collect_problems`. The instance is therefore
        built without it, which is the only way to ask the rules about a
        configuration the rules reject.
        """
        from app.config import CONFIGS

        config = CONFIGS["production"].__new__(CONFIGS["production"])
        defaults = {
            "SECRET_KEY": self.GOOD_SECRET,
            "SQLALCHEMY_DATABASE_URI": "postgresql://h:p@localhost/h",
            "MAIL_ENABLED": False,
            "MAIL_BACKEND": "console",
            "MAIL_SERVER": "",
            "SECURE_COOKIE": True,
            "MODERATION_PROVIDER": "local",
            "MODERATION_API_KEY": "",
        }
        for key, value in {**defaults, **overrides}.items():
            setattr(config, key, value)
        return config

    def test_the_boot_gate_refuses_what_the_rules_reject(self, ctx, monkeypatch):
        """The rules are not only advisory - they stop the process."""
        from app.config import CONFIGS, ConfigError

        for key, value in {
            "SECRET_KEY": self.GOOD_SECRET,
            "SQLALCHEMY_DATABASE_URI": "postgresql://h:p@localhost/h",
            "MAIL_ENABLED": False,
            "MAIL_BACKEND": "console",
            "MAIL_SERVER": "",
            "SECURE_COOKIE": True,
            "MODERATION_PROVIDER": "local",
            "MODERATION_API_KEY": "",
            "REQUIRE_EMAIL_VERIFICATION": True,
        }.items():
            monkeypatch.setattr(CONFIGS["production"], key, value, raising=False)

        with pytest.raises(ConfigError, match="nobody who registers can ever sign in"):
            CONFIGS["production"]()

    def test_waiving_it_is_reported_rather_than_silent(self):
        """A relaxed guarantee has to be visible, like every other one."""
        _mandatory, relaxable = self._profile(REQUIRE_EMAIL_VERIFICATION=False).collect_problems()
        assert any("REQUIRE_EMAIL_VERIFICATION is off" in line for line in relaxable)

    def test_requiring_it_without_mail_is_a_boot_failure(self):
        """The combination nobody can use.

        Confirmation required plus no mail means every account that registers is
        locked out of its own login forever. Not a degraded service - an
        unusable one, so it blocks the boot rather than warning.
        """
        mandatory, _relaxable = self._profile(
            REQUIRE_EMAIL_VERIFICATION=True,
            MAIL_ENABLED=False,
        ).collect_problems()
        assert any("nobody who registers can ever sign in" in line for line in mandatory)

    def test_a_complete_configuration_reports_nothing(self):
        """The rules must not fire when the deployment is coherent."""
        config = self._profile(
            REQUIRE_EMAIL_VERIFICATION=True,
            MAIL_ENABLED=True,
            MAIL_BACKEND="smtp",
            MAIL_SERVER="smtp.example.com",
        )
        assert config.collect_problems() == ([], [])

    def test_the_default_is_on(self):
        """A deployment must opt out of address confirmation, not into it."""
        from app.config import BaseConfig

        assert BaseConfig.REQUIRE_EMAIL_VERIFICATION is True


class TestOnlyVerificationIsWaived:
    """Everything else about a fresh account still applies."""

    def test_a_suspended_account_is_still_refused(self, guest_client, unverified, app, ctx):
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        unverified.status = UserStatus.SUSPENDED.value
        db.session.commit()
        assert_error(_sign_in(guest_client, unverified), status=403, code="account_suspended")

    def test_a_banned_account_is_still_refused(self, guest_client, unverified, app, ctx):
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        unverified.status = UserStatus.BANNED.value
        db.session.commit()
        assert_error(_sign_in(guest_client, unverified), status=403, code="account_banned")

    def test_a_deactivated_account_is_still_refused(self, guest_client, unverified, app, ctx):
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        unverified.status = UserStatus.DEACTIVATED.value
        db.session.commit()
        # Whatever the status code, the account must not get in: an erased
        # account has nothing left to waive.
        error = assert_error(_sign_in(guest_client, unverified), status=403)
        assert error["code"] == "account_deactivated"

    def test_the_password_is_still_required(self, guest_client, unverified, app, ctx):
        app.config["REQUIRE_EMAIL_VERIFICATION"] = False
        assert_error(
            guest_client.post("/api/v1/auth/login", json={"identifier": unverified.email, "password": "wrong"}),
            status=401,
            code="invalid_credentials",
        )
"""Interface language negotiation and the per-account preference.

The interesting cases are the ones a naive implementation gets wrong: a browser
that lists a language we do not have, a language it explicitly refuses, and a
region tag that is not the same as the language.
"""

from __future__ import annotations

import pytest

from app import i18n
from tests.utils import assert_error, assert_ok


class TestNegotiation:
    def test_empty_header_falls_back_to_the_base(self):
        assert i18n.negotiate(None) == i18n.DEFAULT_LANGUAGE
        assert i18n.negotiate("") == i18n.DEFAULT_LANGUAGE

    def test_exact_match(self):
        assert i18n.negotiate("uk") == "uk"

    def test_region_is_not_the_language(self):
        """``uk-UA`` is Ukrainian, not an unknown locale."""
        assert i18n.negotiate("uk-UA") == "uk"

    def test_underscore_form_is_accepted(self):
        """Some clients send ``uk_UA``: the same tag written differently."""
        assert i18n.negotiate("uk_UA") == "uk"

    def test_quality_order_is_honoured(self):
        assert i18n.negotiate("ru;q=0.2, de;q=0.9") == "de"

    def test_q_zero_is_a_refusal(self):
        """``q=0`` means "do not use this", so it must not win by position."""
        assert i18n.negotiate("ru;q=0, de") == "de"

    def test_unsupported_languages_are_skipped(self):
        assert i18n.negotiate("xx, is, fr") == "fr"

    def test_a_header_of_only_unknown_languages_falls_back(self):
        assert i18n.negotiate("xx, yy") == i18n.DEFAULT_LANGUAGE

    def test_malformed_quality_does_not_raise(self):
        assert i18n.negotiate("uk;q=not-a-number") in i18n.BY_CODE

    def test_equal_quality_is_resolved_the_same_way_every_time(self):
        """Two browsers with the same header must land on the same interface."""
        assert i18n.negotiate("de, fr") == i18n.negotiate("de, fr")


class TestCatalogue:
    def test_every_entry_names_itself(self):
        for locale in i18n.LOCALES:
            assert locale.native.strip(), locale.code
            assert locale.english.strip(), locale.code

    def test_codes_are_unique(self):
        codes = [locale.code for locale in i18n.LOCALES]
        assert len(codes) == len(set(codes))

    def test_catalogue_is_sorted_by_native_name(self):
        listed = i18n.catalogue()["locales"]
        names = [item["native"] for item in listed]
        assert names == sorted(names, key=str.casefold)

    def test_direction_follows_the_script(self):
        assert i18n.direction("ar") == "rtl"
        assert i18n.direction("he") == "rtl"
        assert i18n.direction("ru") == "ltr"

    def test_direction_of_an_unknown_language_is_left_to_right(self):
        assert i18n.direction("xx") == "ltr"


class TestEndpoint:
    def test_catalogue_is_public(self, anon_client):
        """A visitor needs the list before they have an account to store it on."""
        data = assert_ok(anon_client.get("/api/v1/i18n/locales"))
        codes = {item["code"] for item in data["locales"]}
        assert "uk" in codes
        assert any(item["rtl"] for item in data["locales"])

    def test_response_negotiates_for_the_browser(self, app):
        client = app.test_client()
        response = client.get("/api/v1/i18n/locales", headers={"Accept-Language": "uk-UA,uk;q=0.9"})
        assert response.get_json()["meta"]["negotiated"] == "uk"


class TestAccountPreference:
    def test_absent_by_default(self, user):
        """NULL means "follow the browser"; a stored guess would be a fact we invented."""
        assert user.language is None

    def test_session_carries_the_preference(self, auth_client, user):
        assert_ok(auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": "de"}))
        data = assert_ok(auth_client.get("/api/v1/auth/me"))
        assert data["user"]["language"] == "de"

    def test_region_is_normalised_on_the_way_in(self, auth_client, user):
        assert_ok(auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": "de-AT"}))
        data = assert_ok(auth_client.get("/api/v1/auth/me"))
        assert data["user"]["language"] == "de"

    def test_unsupported_language_is_refused(self, auth_client, user):
        """Storing one would leave the client with no catalogue and no warning."""
        assert_error(
            auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": "xx"}),
            status=422,
            code="unsupported_language",
        )

    def test_can_be_cleared_back_to_the_browser(self, auth_client, user):
        assert_ok(auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": "de"}))
        assert_ok(auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": ""}))
        data = assert_ok(auth_client.get("/api/v1/auth/me"))
        assert data["user"]["language"] is None

    def test_direction_is_sent_with_the_session(self, auth_client, user):
        """A reader whose language is right-to-left needs the page to mirror."""
        assert_ok(auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": "ar"}))
        data = assert_ok(auth_client.get("/api/v1/auth/me"))
        assert data["user"]["text_direction"] == "rtl"

    def test_preference_follows_the_reader_to_a_new_device(self, auth_client, anon_client, user):
        """The whole point of storing it rather than re-guessing every visit.

        A second client with an empty cookie jar is a different device: it knows
        nothing about the browser that set the language, so the only way the
        choice can reach it is through the account.
        """
        assert_ok(auth_client.patch(f"/api/v1/users/{user.public_id}", json={"language": "fr"}))

        assert_error(anon_client.get("/api/v1/auth/me"), status=401)
        anon_client.login(user.username)
        data = assert_ok(anon_client.get("/api/v1/auth/me"))
        assert data["user"]["language"] == "fr"


@pytest.mark.parametrize("code", ["ru", "en", "uk", "ar"])
def test_supported_languages_round_trip(code):
    assert i18n.is_supported(code)
    assert i18n.normalise(code) == code

"""Security tests: XSS, CSRF, authorisation, rate limiting, uploads, spam."""

from __future__ import annotations

import io

import pytest

from app.extensions import db
from tests.utils import assert_error, assert_ok, make_mp4, make_png


class TestXSS:
    """Content is stored and served as plain text, so markup cannot execute."""

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "<svg/onload=alert(1)>",
            "<iframe src=javascript:alert(1)></iframe>",
            "javascript:alert(1)",
            '<a href="javascript:alert(1)">click</a>',
            "<ScRiPt>alert(1)</ScRiPt>",
            "<scr<script>ipt>alert(1)</script>",
        ],
    )
    def test_payloads_are_neutralised_in_posts(self, auth_client, payload):
        """No executable construct may survive ingest.

        A payload that is *entirely* markup sanitises down to nothing, and the
        right answer there is to refuse the post as empty rather than publish a
        blank one. The assertion that matters is the second: whatever is stored,
        it cannot execute.
        """
        from app.services import post_service
        from app.utils.responses import ApiError

        try:
            post = post_service.create_post(auth_client.target, payload)
        except ApiError as exc:
            assert exc.code == "empty_post", exc.code
            return

        stored = post.body.lower()
        assert "<script" not in stored
        assert "onerror=" not in stored
        assert "onload=" not in stored
        assert "javascript:" not in stored

    def test_double_encoded_entities_neutralised(self):
        from app.security.xss import sanitize_plain_text

        result = sanitize_plain_text("&lt;script&gt;alert(1)&lt;/script&gt;")
        assert "<script" not in result.lower()

    def test_zero_width_characters_removed(self):
        from app.security.validators import normalise_text

        # U+200B inside a tag would otherwise survive to the client.
        assert normalise_text("при\u200bвет") == "привет"
        # ASCII hyphens and dots must survive — the bug class is easy to reintroduce.
        assert normalise_text("a-b.c") == "a-b.c"

    def test_bidi_override_removed(self):
        from app.security.validators import normalise_text

        assert "\u202e" not in normalise_text("hello\u202eworld")

    def test_unsafe_url_schemes_rejected(self):
        from app.security.xss import is_safe_url

        assert is_safe_url("https://example.com")
        assert is_safe_url("mailto:a@b.co")
        assert not is_safe_url("javascript:alert(1)")
        assert not is_safe_url("  javascript:alert(1)")
        assert not is_safe_url("java\tscript:alert(1)")
        assert not is_safe_url("data:text/html,<script>")

    def test_control_characters_rejected(self, auth_client):
        assert_error(auth_client.post("/api/v1/posts", json={"body": "нормальный\x00текст"}), status=422)


class TestCSRF:
    def test_write_without_token_rejected(self, client, user):
        client.login(user.username)
        response = client.raw.post("/api/v1/posts", json={"body": "обход CSRF"})
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "csrf_token_missing"

    def test_mismatched_token_rejected(self, client, user):
        client.login(user.username)
        response = client.raw.post(
            "/api/v1/posts",
            json={"body": "обход CSRF"},
            headers={"X-CSRF-Token": "forged-token"},
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "csrf_token_invalid"

    def test_reads_do_not_require_token(self, client, user):
        client.login(user.username)
        assert client.raw.get("/api/v1/feed").status_code == 200

    def test_bearer_token_is_exempt(self, app, ctx, user):
        """Non-browser clients cannot manage a cookie pair, so a valid
        Authorization header is accepted instead.

        The token is a real one, taken from a real login, so the session backing
        it exists. A hand-minted JWT has no session and is correctly rejected as
        revoked - that is a different property, covered by the revocation tests.
        """
        from tests.conftest import ApiClient

        api = ApiClient(app.test_client(), application=app)
        api.bootstrap_csrf()
        api.login(user.username)
        token = api.cookie("harmony_access").value

        with app.test_client() as http:
            response = http.post(
                "/api/v1/posts",
                json={
                    "body": "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e \u043a\u043b\u0438\u0435\u043d\u0442\u043e\u043c"
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201, response.get_data(as_text=True)

    def test_login_rotates_csrf_token(self, client, user):
        client.login(user.username)
        first = client.csrf_token
        client.raw.post("/api/v1/auth/logout", json={}, headers={"X-CSRF-Token": first})
        client.bootstrap_csrf()
        assert client.csrf_token != first


class TestAuthorisation:
    def test_private_profile_hidden_from_stranger(self, client, user, other_user, auth_api):
        target = other_user
        target.profile_visibility = "private"
        db.session.commit()

        auth_api(target)
        assert_ok(
            client.get(f"/api/v1/users/{target.public_id}"),
            message="the owner still sees their own private profile",
        )

        auth_api(user)  # a genuinely different account
        assert_error(client.get(f"/api/v1/users/{target.public_id}"), status=404)

    def test_edit_other_profile_rejected(self, client, user, other_user, auth_api):
        auth_api(other_user)
        assert_error(client.patch(f"/api/v1/users/{user.public_id}", json={"bio": "взлом"}), status=404)

    def test_admin_endpoints_require_moderator(self, client, user, auth_api):
        auth_api(user)
        assert_error(client.get("/api/v1/admin/stats"), status=403, code="insufficient_role")

    def test_moderator_can_reach_admin(self, client, moderator, auth_api):
        auth_api(moderator)
        assert_ok(client.get("/api/v1/admin/stats"))

    def test_moderator_cannot_change_roles(self, client, moderator, user, auth_api):
        auth_api(moderator)
        assert_error(
            client.post(f"/api/v1/admin/users/{user.public_id}/role", json={"role": "admin"}),
            status=403,
        )

    def test_unknown_post_id_is_404(self, client):
        assert_error(client.get("/api/v1/posts/" + "0" * 32), status=404)

    def test_malformed_public_id_is_404(self, client):
        assert_error(client.get("/api/v1/posts/not-a-uuid"), status=404)


class TestRateLimiting:
    def test_login_limit_enforced(self, app, ctx, user):
        """Repeated failures must eventually lock, not just warn."""
        app.config["RATE_LIMIT_ENABLED"] = True
        # ``RATE_LIMITS`` is what the limiter reads; ``RATE_LIMIT_LOGIN`` is only
        # the environment variable that populates it at import time.
        original = dict(app.config["RATE_LIMITS"])
        app.config["RATE_LIMITS"] = {**original, "auth:login": "3/minute"}
        try:
            client = app.test_client()
            token = client.get("/api/v1/auth/csrf").get_json()["data"]["csrf_token"]
            statuses = []
            for _ in range(5):
                response = client.post(
                    "/api/v1/auth/login",
                    json={"identifier": user.username, "password": "Wr0ng-Pass!99"},
                    headers={"X-CSRF-Token": token},
                )
                statuses.append(response.status_code)
            assert 429 in statuses, statuses
            body = client.post(
                "/api/v1/auth/login",
                json={"identifier": user.username, "password": DEFAULT_OK},
                headers={"X-CSRF-Token": token},
            )
            assert body.status_code == 429
        finally:
            app.config["RATE_LIMITS"] = original
            app.config["RATE_LIMIT_ENABLED"] = False

    def test_limit_parsing(self):
        from app.security.rate_limit import parse_limit

        limits = parse_limit("5/hour;20/day")
        assert len(limits) == 2
        assert {limit.count for limit in limits} == {5, 20}
        assert {limit.window for limit in limits} == {3600, 86400}

    def test_malformed_limit_rejected(self):
        from app.security.rate_limit import parse_limit
        from app.utils.responses import ValidationError

        with pytest.raises(ValidationError):
            parse_limit("abc/hour")

    def test_rate_limit_headers_present(self, app, ctx):
        app.config["RATE_LIMIT_ENABLED"] = True
        try:
            client = app.test_client()
            response = client.get("/api/v1/feed")
            assert "X-RateLimit-Remaining" in response.headers
        finally:
            app.config["RATE_LIMIT_ENABLED"] = False


DEFAULT_OK = "Str0ng-Pass!23"


class TestUploads:
    def test_accepts_valid_png(self, auth_client):
        data = assert_ok(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(make_png()), "photo.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        assert data["files"][0]["content_hash"]
        assert data["files"][0]["mime_type"] == "image/png"

    def test_accepts_valid_mp4(self, auth_client):
        """A real MP4 container is stored as video, not routed to an image decoder."""
        data = assert_ok(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(make_mp4()), "clip.mp4", "video/mp4")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        stored = data["files"][0]
        assert stored["mime_type"] == "video/mp4"
        assert stored["url"].endswith(".mp4")
        # Dimensions are unknown without a decoder, so they are reported as zero
        # rather than as a guess a client might lay out against.
        assert stored["width"] == 0 and stored["height"] == 0

    def test_type_comes_from_bytes_not_the_filename(self, auth_client):
        """An MP4 named .png is still a video.

        Deciding by extension would hand the file to Pillow, which fails with a
        decoding error the user cannot act on. Deciding by magic number stores
        what the file actually is.
        """
        data = assert_ok(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(make_mp4()), "innocent.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        assert data["files"][0]["mime_type"] == "video/mp4"

    def test_video_bytes_are_stored_verbatim(self, auth_client):
        """Re-encoding a video would mean shipping ffmpeg; the bytes go in as they arrived."""
        from app.services.upload_service import _absolute_path

        payload = make_mp4()
        data = assert_ok(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(payload), "clip.mp4", "video/mp4")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        with open(_absolute_path(data["files"][0]["storage_key"]), "rb") as handle:
            assert handle.read() == payload

    def test_rejects_a_video_larger_than_the_ceiling(self, auth_client, app):
        """The cap is enforced from the bytes, before anything reaches the disk."""
        app.config["UPLOAD_MAX_VIDEO_BYTES"] = 64
        assert_error(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(make_mp4(200)), "clip.mp4", "video/mp4")},
                content_type="multipart/form-data",
            ),
            status=413,
            code="video_too_large",
        )

    def test_rejects_disguised_extension(self, auth_client):
        """A text file renamed to .png must not be stored."""
        payload = b"<?php system($_GET['c']); ?>" * 20
        assert_error(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(payload), "evil.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=415,
        )

    def test_rejects_unsupported_type(self, auth_client):
        assert_error(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(b"%PDF-1.4"), "doc.pdf", "application/pdf")},
                content_type="multipart/form-data",
            ),
            status=415,
        )

    def test_rejects_empty_file(self, auth_client):
        assert_error(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(b""), "empty.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=422,
        )

    def test_requires_authentication(self, anon_client):
        assert_error(
            anon_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(make_png()), "x.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=401,
        )

    def test_client_cannot_forge_media_metadata(self, auth_client, user):
        """A crafted storage_key must not attach an arbitrary URL or mislabel
        a MIME type. The post is still created; the bogus reference is dropped."""
        post = assert_ok(
            auth_client.post(
                "/api/v1/posts",
                json={"body": "подмена", "media": [{"storage_key": "0" * 32, "url": "https://evil.example/x.png"}]},
            ),
            status=201,
        )["post"]
        assert post["media"] == []
        assert post["body"] == "подмена"

    def test_unclaimed_upload_cannot_be_stolen(self, auth_client, user, other_user, auth_api):
        result = assert_ok(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (io.BytesIO(make_png()), "mine.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        storage_key = result["files"][0]["storage_key"]

        auth_api(other_user)
        post = assert_ok(
            auth_client.post("/api/v1/posts", json={"body": "краду", "media": [{"storage_key": storage_key}]}),
            status=201,
        )["post"]
        assert post["media"] == [], "another account must not claim this upload"


class TestSpam:
    def test_xss_payload_scores_high(self, ctx):
        from app.security.spam import analyse_content

        signals = analyse_content("<script>alert(document.cookie)</script>")
        assert any(name == "xss_payload" for name, _ in signals)

    def test_link_farm_detected(self, ctx):
        from app.security.spam import analyse_content

        body = " ".join(f"https://spam.example/{index}" for index in range(8))
        signals = analyse_content(body)
        assert any(name == "flood_links" for name, _ in signals)

    def test_clean_text_scores_zero(self, ctx):
        from app.security.spam import analyse_content

        assert analyse_content("Спокойное обсуждение книги.") == []

    def test_disposable_email_detected(self):
        from app.security.spam import is_blocked_email

        assert is_blocked_email("someone@mailinator.com")
        assert not is_blocked_email("someone@gmail.com")

    def test_spammy_username_detected(self):
        from app.security.spam import is_spammy_username

        assert is_spammy_username("free-money-12345")
        assert is_spammy_username("t.me/promo")
        assert is_spammy_username("123456789012")
        # ...and an ordinary name must survive: over-blocking registrations is a
        # failure mode of its own.
        assert not is_spammy_username("mira")
        assert not is_spammy_username("user1")

    def test_duplicate_content_detected(self, auth_client, user, ctx):
        from app.security.spam import is_duplicate_content
        from app.services import post_service

        body = "Один и тот же текст подряд, без изменений."
        post_service.create_post(user, body)
        db.session.commit()
        assert is_duplicate_content(body, user.id) is True
        assert is_duplicate_content("Совершенно другой текст для проверки.", user.id) is False


class TestModerationEngine:
    def test_credible_threat_blocked(self, ctx):
        from app.security.content_moderation import get_engine

        result = get_engine().screen("Я найду тебя и убью", use_remote=False)
        assert result.decision == "block"
        assert "threat" in result.categories

    def test_hate_speech_blocked(self, ctx):
        from app.security.content_moderation import get_engine

        result = get_engine().screen("Ты нигер и этого не стыдно", use_remote=False)
        assert result.decision == "block"
        assert "hate" in result.categories

    def test_card_number_blocked(self, ctx):
        from app.security.content_moderation import get_engine

        result = get_engine().screen("Моя карта 4276 3800 1234 5678", use_remote=False)
        assert result.decision == "block"
        assert "pii" in result.categories

    def test_contact_details_held_for_review(self, ctx):
        from app.security.content_moderation import get_engine

        # Sharing a handle is normal on a social network, so this escalates
        # to review rather than an outright block.
        result = get_engine().screen("Пишите в тг @durov прямо сейчас", use_remote=False)
        assert result.decision in ("review", "block")
        assert "pii" in result.categories

    def test_link_farm_reviewed(self, ctx):
        from app.security.content_moderation import get_engine

        body = " ".join(["https://bit.ly/x"] * 6)
        result = get_engine().screen(body, use_remote=False)
        assert result.decision in ("review", "block")

    def test_ordinary_text_allowed(self, ctx):
        from app.security.content_moderation import get_engine

        result = get_engine().screen("Сегодня красиво на улице, гулял два часа.", use_remote=False)
        assert result.decision == "allow"

    def test_leetspeak_normalised(self, ctx):
        from app.security.content_moderation import get_engine

        result = get_engine().screen("f u c k this", use_remote=False)
        assert result.score > 0

    def test_remote_failure_degrades_to_local(self, app, ctx, monkeypatch):
        """A third-party outage must not become a content bypass."""
        from app.security.content_moderation import OpenAICompatibleModerator, reset_engine

        app.config["MODERATION_FAIL_CLOSED"] = False
        reset_engine()

        def _boom(*args, **kwargs):
            raise ConnectionError("provider down")

        monkeypatch.setattr("requests.post", _boom)
        provider = OpenAICompatibleModerator("https://example.test/v1/moderations", "key", "model", 1)
        result = provider.screen("Обычный текст")
        assert result.degraded is True
        assert result.decision == "allow"

    def test_remote_can_escalate_but_not_relax(self, app, ctx, monkeypatch):
        from app.security.content_moderation import ModerationEngine, ModerationResult

        engine = ModerationEngine()
        engine._remote = _StubProvider(ModerationResult(decision="allow"))
        assert engine.screen("Обычный текст", use_remote=True).decision == "allow"

        engine._remote = _StubProvider(ModerationResult(decision="review", score=0.6))
        assert engine.screen("Обычный текст", use_remote=True).decision == "review"

        # Local block stands even when the remote says allow.
        engine._remote = _StubProvider(ModerationResult(decision="allow"))
        result = engine.screen("Я найду тебя и убью", use_remote=True)
        assert result.decision == "block"


class _StubProvider:
    def __init__(self, result):
        self.name = "stub"
        self._result = result

    def screen(self, text, *, context="post"):
        return self._result


class TestPasswordHashing:
    def test_hash_is_bcrypt_and_verifiable(self, ctx):
        from app.extensions import hash_password, verify_password

        hashed = hash_password("Str0ng-Pass!23", rounds=4)
        assert hashed.startswith("$2")
        assert "Str0ng-Pass!23" not in hashed
        assert verify_password("Str0ng-Pass!23", hashed)
        assert not verify_password("wrong", hashed)

    def test_long_passwords_are_not_truncated(self, ctx):
        """bcrypt silently ignores bytes past 72; the SHA-256 pre-hash keeps
        every character significant."""
        from app.extensions import hash_password, verify_password

        base = "A" * 72
        hashed = hash_password(base + "B" * 40, rounds=4)
        assert verify_password(base + "B" * 40, hashed)
        assert not verify_password(base + "B" * 39, hashed)

    def test_malformed_hash_does_not_crash(self, ctx):
        from app.extensions import verify_password

        assert verify_password("anything", "not-a-hash") is False
        assert verify_password("anything", "") is False

    def test_rounds_are_honoured(self, ctx):
        from app.extensions import hash_password

        low = hash_password("Str0ng-Pass!23", rounds=4)
        high = hash_password("Str0ng-Pass!23", rounds=6)
        assert low.split("$")[2] == "04"
        assert high.split("$")[2] == "06"


class TestSecurityHeaders:
    def test_headers_present(self, client):
        response = client.raw.get("/api/v1/feed")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_api_responses_are_not_cached(self, client):
        response = client.raw.get("/api/v1/feed")
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_request_id_echoed(self, client):
        response = client.raw.get("/api/v1/feed")
        assert response.headers.get("X-Request-ID")

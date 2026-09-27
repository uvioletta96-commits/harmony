"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the ``app`` package importable when pytest is run from the backend root.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("MAIL_ENABLED", "0")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

DEFAULT_PASSWORD = "Str0ng-Pass!23"


class ApiClient:
    """Thin wrapper over the Flask test client.

    Its only job is to attach the CSRF header to unsafe methods, because the
    application correctly rejects requests without one. Every test therefore
    exercises the real CSRF path instead of disabling it to make tests easier.
    """

    def __init__(self, client, *, csrf_token: str | None = None, application: Any = None) -> None:
        self._client = client
        self.csrf_token = csrf_token
        self.user: Any = None
        #: The Flask app behind ``client``; tests read config straight from it.
        self.application = application

    # -- verbs ---------------------------------------------------------
    def get(self, url: str, **kwargs: Any):
        return self._client.get(url, **kwargs)

    def post(self, url: str, json: Any = None, **kwargs: Any):
        return self._request("post", url, json, **kwargs)

    def patch(self, url: str, json: Any = None, **kwargs: Any):
        return self._request("patch", url, json, **kwargs)

    def put(self, url: str, json: Any = None, **kwargs: Any):
        return self._request("put", url, json, **kwargs)

    def delete(self, url: str, json: Any = None, **kwargs: Any):
        return self._request("delete", url, json, **kwargs)

    def upload(self, url: str, files: dict[str, Any], **kwargs: Any):
        """POST a multipart body.

        ``files`` maps a field name to ``(stream|bytes, filename, content_type)``.
        Werkzeug's test client turns that into a real multipart body, so the
        request genuinely exercises the upload path.
        """
        kwargs.setdefault("content_type", "multipart/form-data")
        return self._request("post", url, None, data=files, **kwargs)

    def open(self, method: str, url: str, **kwargs: Any):
        return self._request(method, url, kwargs.pop("json", None), **kwargs)

    # -- internals -----------------------------------------------------
    def _request(self, method: str, url: str, json: Any = None, **kwargs: Any):
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.csrf_token and "X-CSRF-Token" not in headers:
            headers["X-CSRF-Token"] = self.csrf_token
        kwargs["headers"] = headers
        if json is not None:
            kwargs["json"] = json
        response = getattr(self._client, method)(url, **kwargs)
        self._sync_csrf()
        return response

    def _sync_csrf(self) -> None:
        """Adopt a rotated CSRF token, the way a browser does.

        The server rotates the token whenever it opens or closes a session, and
        the browser picks that up for free because ``api.js`` re-reads
        ``document.cookie`` on every request. A test client that remembers the
        token it was handed at bootstrap instead would start failing the *next*
        request with ``csrf_token_invalid`` - which reads as a broken endpoint
        and hides whatever the test was actually about. Re-reading the jar after
        every response keeps this helper honest instead of leaving every future
        test to remember the rule.
        """
        current = self._read_csrf_cookie()
        if current and current != self.csrf_token:
            self.csrf_token = current

    def bootstrap_csrf(self) -> str:
        response = self._client.get("/api/v1/auth/csrf")
        self.csrf_token = response.get_json()["data"]["csrf_token"]
        return self.csrf_token

    def login(self, identifier: str, password: str = DEFAULT_PASSWORD):
        """Perform a real login so the session cookies are genuine.

        The server rotates the CSRF token on login, so it must be re-read
        afterwards вЂ” exactly as a browser would pick it up from the new cookie.
        """
        self.bootstrap_csrf()
        response = self._client.post(
            "/api/v1/auth/login",
            json={"identifier": identifier, "password": password},
            headers={"X-CSRF-Token": self.csrf_token},
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        payload = response.get_json()["data"]
        self.user = payload["user"]
        self.csrf_token = payload.get("csrf_token") or self._read_csrf_cookie()
        return response

    def _read_csrf_cookie(self) -> str | None:
        cookie = self.cookie("harmony_csrf")
        return cookie.value if cookie else None

    def cookie(self, name: str) -> Any:
        """Fetch a cookie by name regardless of the path it was scoped to.

        Werkzeug keys its jar by ``(domain, path, name)`` and the refresh token
        deliberately lives under the narrower auth path, so a plain lookup
        misses it. Tests care about the value, not the scope.
        """
        paths = ["/"]
        if self.application is not None:
            paths.append(f"{self.application.config['API_PREFIX']}/auth")
        for path in paths:
            found = self._client.get_cookie(name, path=path)
            if found is not None:
                return found
        return None

    def logout(self):
        return self._client.post("/api/v1/auth/logout", json={}, headers={"X-CSRF-Token": self.csrf_token})

    # -- passthrough ---------------------------------------------------
    @property
    def raw(self):
        """Escape hatch to the underlying Werkzeug client."""
        return self._client


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    from app import create_app

    application = create_app("testing")
    if os.environ.get("HARMONY_DEBUG_ERRORS") == "1":
        _install_debug_handler(application)
    yield application


def _install_debug_handler(application) -> None:
    """Replace the deliberately opaque 500 handler with one that re-raises.

    Must run before the first request: Flask refuses to register a handler once
    it has started dispatching, which is why this lives in the app fixture
    rather than in a per-test fixture.
    """
    import traceback

    def _rethrow(exc: Exception):
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        return {"ok": False, "error": {"code": "debug", "message": str(exc)}}, 500

    application.register_error_handler(Exception, _rethrow)


@pytest.fixture(scope="session", autouse=True)
def _schema(app):
    """Create the schema once for the whole session.

    The in-memory database lives for the life of the process, so the schema is
    built a single time and each test starts from empty tables instead.
    """
    from app.extensions import db

    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def ctx(app):
    """Push an application context with empty tables."""
    from app.extensions import db

    with app.app_context():
        _truncate_all()
        yield app
        db.session.rollback()
        db.session.remove()
        _truncate_all()


def _truncate_all() -> None:
    """DELETE every row, in reverse dependency order.

    Cheaper and more predictable than re-creating the schema, and it resets
    sequences implicitly on the dialects this project targets.
    """
    from app.extensions import db

    db.session.rollback()
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


@pytest.fixture()
def client(app, ctx, user):
    """Primary client, signed in as the ``user`` fixture.

    Most tests act *as a user*, so that is the default. Tests that need an
    anonymous caller use ``anon_client``, which is a genuinely separate client
    with its own empty cookie jar.
    """
    api = ApiClient(app.test_client(), application=app)
    api.bootstrap_csrf()
    api.login(user.username)
    api.target = user
    return api


@pytest.fixture()
def guest_client(app, ctx):
    """Unauthenticated client with a ready CSRF token."""
    api = ApiClient(app.test_client(), application=app)
    api.bootstrap_csrf()
    return api


@pytest.fixture()
def runner(app, ctx):
    return app.test_cli_runner()


# ---------------------------------------------------------------------------
# User factories
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_user(ctx):
    from app.extensions import db, hash_password
    from app.models.user import ProfileVisibility, User, UserRole, UserStatus

    counter = {"n": 0}

    def _factory(
        username: str | None = None,
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        verified: bool = True,
        status: str = UserStatus.ACTIVE.value,
        role: str = UserRole.MEMBER.value,
        display_name: str | None = None,
        **kwargs: Any,
    ):
        counter["n"] += 1
        index = counter["n"]
        user = User(
            email=email or f"user{index}@harmony.test",
            username=username or f"user{index}",
            password_hash=hash_password(password, rounds=4),
            display_name=display_name or f"User {index}",
            role=role,
            status=status,
            email_verified=verified,
            avatar_color="sand",
            profile_visibility=ProfileVisibility.PUBLIC.value,
            data_processing_consent=True,
            **kwargs,
        )
        db.session.add(user)
        db.session.commit()
        return user

    _factory.password = DEFAULT_PASSWORD  # type: ignore[attr-defined]
    return _factory


@pytest.fixture()
def user(make_user):
    return make_user()


@pytest.fixture()
def other_user(make_user):
    return make_user()


@pytest.fixture()
def moderator(make_user):
    from app.models.user import UserRole

    return make_user(role=UserRole.MODERATOR.value)


@pytest.fixture()
def admin(make_user):
    from app.models.user import UserRole

    return make_user(role=UserRole.ADMIN.value)


# ---------------------------------------------------------------------------
# Authenticated clients
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client(client, user):
    """Alias of ``client``, for tests that read better with the name.

    Both share one underlying client and therefore one cookie jar: a session
    established through either must be visible to the other, or a test can
    silently assert 401 against an endpoint that is working.
    """
    return client


@pytest.fixture()
def auth_api(client):
    """Authenticated client factory: ``auth_api(target)``."""

    def _factory(target, password: str = DEFAULT_PASSWORD):
        identifier = getattr(target, "username", target)
        client.login(str(identifier), password)
        client.target = target  # type: ignore[attr-defined]
        return client

    return _factory


@pytest.fixture()
def anon_client(app, ctx):
    """A genuinely separate client with an empty cookie jar.

    Used to assert that an endpoint rejects an anonymous caller. It must not
    share cookies with ``client`` вЂ” otherwise "signed out" would actually be
    "still signed in".
    """
    fresh = ApiClient(app.test_client(), application=app)
    fresh.bootstrap_csrf()
    return fresh


# ---------------------------------------------------------------------------
# Content factories
# ---------------------------------------------------------------------------


@pytest.fixture()
def post_factory(ctx, user):
    from app.extensions import db
    from app.models.post import Post, PostStatus, PostVisibility

    def _factory(author=None, body: str = "РўРµСЃС‚РѕРІР°СЏ РїСѓР±Р»РёРєР°С†РёСЏ", **kwargs: Any):
        author = author or user
        post = Post(
            author_id=author.id,
            body=body,
            visibility=kwargs.pop("visibility", PostVisibility.PUBLIC.value),
            status=kwargs.pop("status", PostStatus.PUBLISHED.value),
            **kwargs,
        )
        db.session.add(post)
        db.session.commit()
        return post

    return _factory


@pytest.fixture()
def auth_post(post_factory):
    return post_factory()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_json():
    def _parse(response):
        return response.get_json()

    return _parse


@pytest.fixture()
def reset_state(ctx):
    """Truncate every table on demand."""
    return _truncate_all


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _surface_exceptions():
    """No-op marker: the debug handler is installed by the ``app`` fixture.

    Set ``HARMONY_DEBUG_ERRORS=1`` to replace the production error handler
    (which deliberately hides internals) with one that prints the exception.
    Without this flag the application behaves exactly as it does in production.
    """
    yield

"""Realtime broadcast helpers.

Every one of these is called from an ordinary HTTP request handler - after a
post is published, a comment is added, a message is sent - never from inside a
Socket.IO event handler. flask_socketio's module-level ``emit`` reads
``flask.request.namespace``, which only exists inside a Socket.IO handler, so
the "obvious" call raises AttributeError and the broadcast never leaves the
process.

That failure is invisible by construction: every caller treats realtime delivery
as a courtesy and swallows the exception, so the symptom is a log line and live
updates that never arrive. Found on the deployed server while testing comments:

    emit_global(event, payload)
      File flask_socketio/__init__.py, line 899, in emit
        namespace = flask.request.namespace
    AttributeError: 'Request' object has no attribute 'namespace'
"""

from __future__ import annotations

import pathlib

import pytest
from flask import Flask, request

from app.realtime.manager import emit_global, emit_to_conversation, emit_to_user


@pytest.fixture()
def bare_app():
    """A Flask app with Socket.IO initialised, and no request in scope.

    Deliberately not the project's `app` fixture: the point is that these
    helpers work from a plain HTTP handler, so the surrounding application must
    not be what makes them pass.
    """
    from flask_socketio import SocketIO

    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test-only"
    application.config["SOCKETIO_MESSAGE_QUEUE"] = None
    SocketIO(application, async_mode="threading", cors_allowed_origins="*")

    @application.get("/ping")
    def ping():  # pragma: no cover - the route is the request context
        return {"ok": True}

    return application


def _in_request(app):
    return app.test_request_context("/ping")


def test_emit_global_from_a_plain_http_handler(bare_app):
    """The bug: this raised AttributeError on every post and every comment."""
    with _in_request(bare_app):
        emit_global("post:created", {"id": "abc"})


def test_emit_to_user_from_a_plain_http_handler(bare_app):
    with _in_request(bare_app):
        emit_to_user(7, "notification:new", {"id": "abc"})


def test_emit_to_conversation_from_a_plain_http_handler(bare_app):
    with _in_request(bare_app):
        emit_to_conversation(3, "message:new", {"body": "hi"})


def test_emit_to_conversation_can_exclude_the_sender(bare_app):
    with _in_request(bare_app):
        emit_to_conversation(3, "message:new", {"body": "hi"}, exclude_sid="abc123")


def test_the_helpers_work_outside_a_request_too(bare_app):
    """A background task or a CLI command has no request at all."""
    with bare_app.app_context():
        emit_global("post:created", {"id": "abc"})


def test_the_regression_is_what_we_think_it_is(bare_app):
    """Pin the underlying behaviour so the fix cannot be undone by accident.

    Without a `namespace`, flask_socketio looks the attribute up on the request
    and raises. That is the exact line the deployed traceback stopped on.
    """
    from flask_socketio import emit

    with _in_request(bare_app):
        # No namespace: flask_socketio reads request.namespace, which a plain
        # request does not have.
        with pytest.raises(AttributeError, match="namespace"):
            emit("post:created", {"id": "abc"})

        # Namespace given, but no room and no broadcast: it then reaches for
        # request.sid, which is also missing. Both arguments are needed.
        with pytest.raises(AttributeError, match="sid"):
            emit("post:created", {"id": "abc"}, namespace="/")

        # With both, it works - and that is the combination the helpers use.
        emit("post:created", {"id": "abc"}, namespace="/", broadcast=True)
        assert request.path == "/ping"

# ---------------------------------------------------------------------------
# Presence
#
# The online dot on an avatar had a rule in CSS the whole time and nothing that
# ever applied it, so no account - connected or not - ever showed one. The dot is
# driven by `User.to_public_dict()["is_online"]`, which reads the live socket
# registry. Two things have to hold, and the second is easy to get wrong:
#
#   1. an account holding a socket reads as online;
#   2. an account that turned its last-seen time off reads as offline even while
#      it is connected.
#
# (2) is not a nicety. "Last seen two hours ago" and "online right now" are the
# same disclosure, so honouring `show_last_seen` for one and not the other hands
# the setting straight back to anyone who watches an avatar instead of a
# timestamp.
# ---------------------------------------------------------------------------


def _connect(user, sid: str = "sid-1") -> None:
    """Seed the socket registry the way `manager.register` does.

    Not by calling `register`: that also calls flask_socketio's `join_room`,
    which reads `flask.request.sid` and so needs a live Socket.IO handler behind
    it. Presence only reads the registry, so this writes the same entry
    `register` would and leaves the room bookkeeping alone. `test_presence_...`
    below is about what the registry says, not about joining rooms.
    """
    from app.realtime.manager import _connections, user_room

    _connections[sid] = {"user_id": user.id, "username": user.username,
                         "rooms": {user_room(user.id)}}


def _disconnect(sid: str = "sid-1") -> None:
    from app.realtime.manager import unregister

    unregister(sid)


@pytest.fixture()
def registry(app):
    """A clean socket registry around each test.

    The registry is module-level process state, not per-test state, so a
    connection left open by one test would make an unrelated account look online
    in the next one.
    """
    from app.realtime.manager import clear_registry

    clear_registry()
    yield
    clear_registry()


def test_a_connected_account_reads_as_online(app, make_user, registry):
    viewer = make_user()
    target = make_user()
    _connect(target)

    assert target.to_public_dict(viewer)["is_online"] is True
    assert viewer.to_public_dict(target)["is_online"] is False


def test_a_disconnected_account_reads_as_offline(app, make_user, registry):
    from datetime import UTC, datetime

    viewer = make_user()
    target = make_user()
    # Recently seen, to prove the dot is not derived from `last_seen_at`.
    target.last_seen_at = datetime.now(UTC)

    assert target.to_public_dict(viewer)["is_online"] is False


def test_hiding_last_seen_also_hides_presence(app, make_user, registry):
    """The bug this guards: a live dot on an account that asked not to be seen."""
    viewer = make_user()
    private = make_user(show_last_seen=False)
    _connect(private)

    assert private.is_usable_account, "precondition: the account is otherwise usable"
    assert private.to_public_dict(viewer)["is_online"] is False


def test_presence_does_not_leak_a_private_profile(app, make_user, registry):
    """A private profile is not readable by a non-follower, so it cannot be
    observed to be online either."""
    from app.extensions import db
    from app.models.user import ProfileVisibility

    viewer = make_user()
    target = make_user()
    target.profile_visibility = ProfileVisibility.PRIVATE.value
    db.session.commit()
    _connect(target)

    assert target.to_public_dict(viewer)["is_online"] is False
    # And a follower, who may read the profile, does see the dot.
    assert target.to_public_dict(target)["is_online"] is True


def test_a_guest_sees_the_dot_on_a_public_profile(app, make_user, registry):
    """Gating on a signed-in viewer would hide every dot from signed-out
    readers - which is most of the sign-up page and the whole guest feed."""
    target = make_user()
    _connect(target)

    assert target.to_public_dict(None)["is_online"] is True


def test_two_tabs_of_one_account_stay_online_when_one_closes(app, make_user, registry):
    """One tab closing is not the person logging off."""
    viewer = make_user()
    target = make_user()
    _connect(target, "sid-a")
    _connect(target, "sid-b")

    assert target.to_public_dict(viewer)["is_online"] is True
    _disconnect("sid-a")
    assert target.to_public_dict(viewer)["is_online"] is True
    _disconnect("sid-b")
    assert target.to_public_dict(viewer)["is_online"] is False


def test_a_banned_account_never_reads_as_online(app, make_user, registry):
    from app.extensions import db
    from app.models.user import UserStatus

    viewer = make_user()
    target = make_user()
    target.status = UserStatus.BANNED.value
    db.session.commit()
    _connect(target)

    assert target.to_public_dict(viewer)["is_online"] is False


def test_is_online_agrees_with_the_dict(app, make_user, registry):
    """The helper and the serialised field must not drift apart."""
    target = make_user()
    _connect(target)

    from app.realtime.manager import is_online

    assert is_online(target.id) is True
    assert is_online(target.id + 100_000) is False


def test_presence_appears_in_the_search_payload(app, make_user, registry, auth_api):
    """The dot is only useful if the field survives the API, so check the route
    rather than the model."""
    target = make_user(display_name="Presence Target")
    _connect(target)

    rows = auth_api(make_user()).get(
        "/api/v1/users/search", query_string={"q": "Presence"}
    ).get_json()["data"]
    found = [row for row in rows if row["username"] == target.username]

    assert found, "the connected account should appear in its own search results"
    assert found[0]["is_online"] is True

# ---------------------------------------------------------------------------
# Self-hosting
#
# The Socket.IO client used to be fetched from cdn.socket.io by a plain
# <script> tag. `connect()` bails out - returning null and logging one warning -
# when `window.io` is missing, so every realtime feature degraded to REST polling
# with nothing on screen to say so. Presence was the visible symptom: nobody ever
# showed as online because nobody was ever connected.
#
# These assert the dependency is gone rather than merely unused, because a script
# tag that points nowhere is exactly the thing that would come back.
# ---------------------------------------------------------------------------

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
INDEX = FRONTEND / "index.html"
VENDORED_CLIENT = FRONTEND / "vendor" / "socket.io.min.js"
#: sha256 of the socket.io client 4.7.5 release, as downloaded from cdn.socket.io.
#: Pinned because a vendored bundle can otherwise be hand-edited without any
#: test noticing, and a modified Socket.IO client is not reviewable by reading it.
VENDORED_CLIENT_SHA256 = "73eba16bc895fdfa454e27ecb80def31ede8d861f99e175ff93b110eabec044f"


def _script_sources() -> list[str]:
    import re

    html = INDEX.read_text(encoding="utf-8")
    return re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)


def test_index_html_loads_no_third_party_scripts():
    """An external script tag is remote code for every visitor, run with the
    page's origin and the session cookie."""
    sources = _script_sources()

    assert sources, "precondition: index.html loads at least one script"
    external = [src for src in sources if not src.startswith("/")]
    assert external == [], f"third-party script tags: {external}"


def test_the_socket_io_client_is_vendored_and_shipped():
    """Pointing at a local path only works if the file is actually in the repo."""
    sources = _script_sources()
    socket_tags = [src for src in sources if "socket.io" in src]

    assert socket_tags == ["/vendor/socket.io.min.js"], socket_tags
    assert VENDORED_CLIENT.is_file(), f"{VENDORED_CLIENT} is referenced but missing"
    head = VENDORED_CLIENT.read_text(encoding="utf-8")[:400]
    assert "Socket.IO" in head
    assert "MIT License" in head, "redistributing it needs its licence header intact"


def test_the_vendored_client_is_the_released_file():
    import hashlib

    digest = hashlib.sha256(VENDORED_CLIENT.read_bytes()).hexdigest()

    assert digest == VENDORED_CLIENT_SHA256, (
        "frontend/vendor/socket.io.min.js is not the pinned 4.7.5 release. "
        "Re-download it and update the sha256 in frontend/index.html."
    )


def test_csp_no_longer_carves_out_the_socket_cdn(app):
    """With the bundle local there is no reason for the policy to name the CDN,
    and the entry would be a hole with no door left in it."""
    header = app.test_client().get("/").headers["Content-Security-Policy"]
    script_src = next(d for d in header.split("; ") if d.startswith("script-src"))

    assert script_src.strip() == "script-src 'self'", script_src
    assert "cdn.socket.io" not in header
    assert "cdn.jsdelivr.net" not in script_src


# ---------------------------------------------------------------------------
# Handshake
#
# Everything above is about presence being *reported* correctly. These are about
# the socket server actually having the handlers that report it.
# ---------------------------------------------------------------------------

#: Every event the frontend can send. A missing one is a feature that silently
#: does nothing - the client emits, the server has no listener, no error.
EXPECTED_EVENTS = {
    "connect",
    "disconnect",
    "conversation:join",
    "conversation:leave",
    "conversation:read",
    "message:send",
    "message:typing",
    "presence:ping",
}


def test_handlers_survive_init_app(app):
    """The regression: `socketio.server.handlers['/']` was empty.

    `SocketIO(...)` builds its server eagerly, so a module decorated with
    `@socketio.on` registers on that server; `init_app` then replaces the server
    with a fresh one and replays only the (empty) pending queue. Handlers
    registered before `init_app` are lost without a word.
    """
    from app.extensions import socketio

    assert socketio.server is not None, "precondition: init_app built a server"
    assert set(socketio.server.handlers.get("/", {})) >= EXPECTED_EVENTS


def test_the_realtime_package_does_not_import_events_eagerly():
    """Pin the fix. `app.realtime.__init__` used to do `from . import events`,
    which pulled the handlers in before `init_app` and got them discarded.

    Checked against the source rather than `sys.modules`, because by the time this
    runs the factory has imported `events` on purpose and the module is
    legitimately loaded.
    """
    import ast

    package = pathlib.Path(__file__).resolve().parents[1] / "app" / "realtime" / "__init__.py"
    tree = ast.parse(package.read_text(encoding="utf-8"))

    eager = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("events", None) and node.level:
            eager.update(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            eager.update(alias.name for alias in node.names)

    assert "events" not in eager, (
        "app/realtime/__init__.py imports events, which registers the handlers "
        "before init_app replaces the server and throws them away"
    )


def _issue(user):
    from app.security.decorators import issue_session

    access_token, _refresh, _session = issue_session(user)
    return access_token


def test_handshake_registers_the_user(app, make_user):
    """A real handshake puts the account in the registry, and leaves it on
    disconnect."""
    from app.extensions import socketio
    from app.realtime.manager import _connections, clear_registry

    user = make_user()
    clear_registry()

    client = socketio.test_client(app)
    client.connect(auth={"token": _issue(user)})

    assert _connections, "the handshake registered nothing"
    assert {entry["user_id"] for entry in _connections.values()} == {user.id}

    client.disconnect()
    assert not _connections, "a disconnect left the connection registered"


def test_a_handshake_without_a_token_is_refused(app):
    """The other half of the bug: with no handlers, `handle_connect` never ran, so
    nothing refused an anonymous socket either. An unauthenticated client could
    open a connection to the live chat endpoint."""
    from app.extensions import socketio

    client = socketio.test_client(app)
    assert not client.connect(auth={}), "an unauthenticated socket was accepted"


def test_a_handshake_with_a_bogus_token_is_refused(app, make_user):
    from app.extensions import socketio

    client = socketio.test_client(app)
    assert not client.connect(auth={"token": "not-a-real-token"})


def test_a_handshake_for_a_banned_account_is_refused(app, make_user):
    from app.extensions import db, socketio
    from app.models.user import UserStatus

    banned = make_user()
    token = _issue(banned)
    banned.status = UserStatus.BANNED.value
    db.session.commit()

    client = socketio.test_client(app)
    assert not client.connect(auth={"token": token})


def test_presence_becomes_true_for_a_connected_account(app, make_user):
    """The two halves together: the handshake registers, and the field reports it."""
    from app.extensions import socketio
    from app.realtime.manager import clear_registry

    user = make_user()
    clear_registry()
    assert user.to_public_dict(user)["is_online"] is False

    client = socketio.test_client(app)
    client.connect(auth={"token": _issue(user)})

    assert user.to_public_dict(user)["is_online"] is True

    client.disconnect()
    assert user.to_public_dict(user)["is_online"] is False

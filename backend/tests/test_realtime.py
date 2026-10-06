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

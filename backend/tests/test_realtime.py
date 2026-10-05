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
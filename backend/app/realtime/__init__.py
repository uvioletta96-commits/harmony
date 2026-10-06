"""Realtime (WebSocket) layer.

``events`` is deliberately *not* imported here.

flask_socketio's ``@socketio.on`` decorator registers on ``socketio.server`` if
one exists, and otherwise queues the handler for ``SocketIO.init_app`` to replay.
``SocketIO(...)`` builds its server eagerly, so importing a module that
decorates handlers always registers them on *that* server - and
``init_app`` later replaces the server with a fresh one, replaying only the
empty queue. Every handler is lost.

The symptom is a socket server that accepts any connection, authenticated or
not, and answers none of them: no handshake authentication, no presence, no
``message:send``, no typing, no read receipts. Nothing raises, because nothing
runs. Chat kept working over the REST fallback, which is why it went unnoticed.

So the import of ``events`` belongs to the app factory, after ``init_app``, where
``_init_extensions`` does it. Nothing may import ``events`` before that point.
``test_handlers_survive_init_app`` is the regression test.
"""

from . import manager

__all__ = ["manager"]
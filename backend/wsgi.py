"""WSGI entrypoint.

Gunicorn command used in production::

    gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
             -w 1 -k geventwebsocket -b 0.0.0.0:8000 wsgi:app

The WebSocket-capable worker class and a single worker are required when using
the threading async mode. With ``SOCKETIO_ASYNC_MODE=eventlet``/``gevent`` and a
configured message queue, scale workers horizontally and drop the class flag.
"""

from __future__ import annotations

import os

from app import create_app
from app.extensions import socketio

# ``SocketIO.init_app`` installs its middleware as ``app.wsgi_app``, so the
# Flask app *is* already the combined HTTP + WebSocket callable. There is no
# separate wrapper to construct: pointing a server at anything other than this
# object silently drops the WebSocket route.
app = create_app()

# Servers disagree about which module-level name they load, so both point at
# the same object rather than leaving one of them undefined.
application = app
wsgi_app = app


if __name__ == "__main__":  # pragma: no cover - local development only
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=app.debug, allow_unsafe_werkzeug=True)

"""WSGI entry point for PythonAnywhere.

PythonAnywhere imports this file and looks for a module-level callable called
``application``. It is a plain WSGI server, not gunicorn and not the Flask dev
server, so this is the whole of the integration.

Two things about this host are worth stating rather than discovering:

**No certificate on the free tier.** The URL is plain HTTP, so the session
cookie cannot carry ``Secure`` or the browser will never send it back and
nobody can sign in. That is why this file selects the ``preview`` profile:
it is the one that accepts the setting and prints a warning on every start.

**No WebSockets.** Socket.IO cannot hold a connection open here, so chat and
live notification counts fall back to whatever the page does on load. The
application already degrades that way when the message queue is unreachable;
this is the same code path.

Everything else - accounts, posts, media, comments, moderation, the feed,
settings, the GDPR endpoints - is ordinary HTTP and works normally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Where the application package actually is, given that this file may be copied
# somewhere else. On PythonAnywhere the file the user pastes into the web-app
# editor lives outside the project entirely, so guessing "next to me" is wrong
# half the time. Both layouts are tried: this file inside `deploy/`, and this
# file sitting beside `backend/`.
for candidate in (HERE / "backend", HERE.parent / "backend"):
    if candidate.is_dir():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break
else:  # pragma: no cover - a misconfigured upload
    raise RuntimeError(
        "Could not find the backend package. Expected a 'backend' directory "
        f"beside {HERE} or beside its parent {HERE.parent}."
    )

# Defaults a developer should not have to type, overridable from the web UI's
# environment variables. `setdefault` so anything already set wins.
os.environ.setdefault("APP_ENV", "preview")
os.environ.setdefault("SECURE_COOKIE", "false")
os.environ.setdefault("SERVE_FRONTEND", "1")
os.environ.setdefault("MAIL_ENABLED", "0")
os.environ.setdefault("MAIL_BACKEND", "console")
# No Redis on the free tier. The application already degrades to local counters,
# and saying so explicitly keeps the startup logs readable.
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("SOCKETIO_MESSAGE_QUEUE", "")
# Uploads live on the host's own disk, which is what this tier gives us.
os.environ.setdefault("UPLOAD_DIR", str(HERE / "uploads"))

from app import create_app  # noqa: E402  (path and environment set up above)

application = create_app()
app = application

__all__ = ["app", "application"]

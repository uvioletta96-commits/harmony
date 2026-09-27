"""Serverless entrypoint for Vercel.

Vercel guesses where a Python application starts by scanning a handful of
default locations for a module-level variable called ``app``. This repository
has two, and both look identical to that scan:

* ``backend/wsgi.py`` - the real one
* ``backend/tests/conftest.py`` - a *pytest fixture* that happens to be named
  ``app``, which is a test, not an entrypoint

So the scan gives up and refuses to build. Naming the entrypoint here removes
the guesswork: this file is the only thing Vercel has to look at.

It stays a thin shim on purpose. ``backend/wsgi.py`` is the single place that
constructs the application, and a second construction path would be a second
place for the deployment and the tests to disagree about.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The repository root, so `import app` resolves to `backend/app` without the
# backend directory having to be the build root.
ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("APP_ENV", "production")
# Vercel's static build does not run the Flask dev server and does not mount
# `frontend/`, so nothing here should try to serve it. Left explicit because a
# missing value and a wrong value are not the same thing to guess at.
os.environ.setdefault("SERVE_FRONTEND", "0")

from wsgi import app  # noqa: E402  (path set up above, then import)

# The name Vercel's Python runtime looks for. `app` is the module-level object
# it binds; `application` and `wsgi_app` are the other names a WSGI server may
# ask for, and all three are the same object.
application = app
wsgi_app = app

__all__ = ["app", "application", "wsgi_app"]

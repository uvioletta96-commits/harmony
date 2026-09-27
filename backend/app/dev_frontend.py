"""Static file serving for local development.

In production nginx owns the static tier: it serves the frontend bundle, the
uploaded media and the SPA fallback, and proxies only ``/api/`` and
``/socket.io/`` to the application. That separation is deliberate — it keeps
large assets off the application servers and lets the two scale independently.

None of it exists when you run ``flask run`` on a laptop, where ``/`` is a 404
and the whole thing looks broken. This module fills that gap *for the
development profile only*.

It is never registered in production, and the guard below makes that a
property rather than a convention: serving user-uploaded content from the API
origin would collapse the two-origin split that the CSP and the cookie
attributes are built around.
"""

from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, Flask, abort, current_app, jsonify, send_from_directory

FRONTEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "frontend")
)

#: Prefixes that must 404 rather than fall through to the SPA shell. A missing
#: script that returns ``index.html`` with a 200 fails in the browser with a
#: parse error and no indication of which file was wrong.
ASSET_PREFIXES = ("js/", "css/", "assets/")

#: Prefixes owned by the application rather than by the router. Without these an
#: unknown ``/api/...`` path answers 200 with the HTML shell, which reads as a
#: live endpoint: a removed route, or a typo in a client, looks like it worked.
#: The failure then surfaces much later as a JSON parse error in the console with
#: nothing to connect it back to the path that was actually wrong.
APP_PREFIXES = ("api/", "socket.io/")


def register_dev_frontend(app: Flask) -> bool:
    """Mount the frontend bundle. Returns whether it was actually mounted.

    Governed by ``SERVE_FRONTEND``, which only the development profile turns on.
    An explicit flag rather than sniffing ``DEBUG``: a flag can be asserted in a
    test, and "did we accidentally enable this in production" becomes a
    one-line config read instead of an argument about boolean precedence.
    """
    if not app.config.get("SERVE_FRONTEND"):
        return False
    if not os.path.isdir(FRONTEND_DIR):  # pragma: no cover - broken checkout
        app.logger.warning("Dev frontend not mounted: %s does not exist", FRONTEND_DIR)
        return False

    spa = Blueprint("dev_frontend", __name__)

    def no_store(response):
        """Never let the browser cache a dev asset.

        ES modules are cached per-URL in the page's module map *and* by the HTTP
        cache, so an ordinary reload after editing a file serves the old one and
        the change appears to have had no effect. Production is a different
        problem entirely - there the HTML shell is revalidated and the assets are
        immutable - but locally a stale module is just a lie.
        """
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @spa.get("/")
    @spa.get("/<path:subpath>")
    def serve(subpath: str = "") -> Any:
        if subpath.startswith(APP_PREFIXES):
            # An application path with no route behind it. Answering with the
            # shell would report a working endpoint that does not exist.
            missing = jsonify({"error": {"code": "not_found", "message": "Not found."}, "ok": False})
            missing.status_code = 404
            return no_store(missing)

        if subpath.startswith(ASSET_PREFIXES):
            # A real asset path: let a miss be a 404, not a shell full of HTML.
            return no_store(send_from_directory(FRONTEND_DIR, subpath))

        upload_prefix = current_app.config.get("UPLOAD_URL_PREFIX", "/uploads").strip("/")
        if upload_prefix and (subpath == upload_prefix or subpath.startswith(f"{upload_prefix}/")):
            return _serve_upload(subpath[len(upload_prefix) :].lstrip("/"))

        candidate = os.path.join(FRONTEND_DIR, subpath) if subpath else ""
        if subpath and os.path.isfile(candidate):
            return no_store(send_from_directory(FRONTEND_DIR, subpath))

        # Client-side routes such as /post/<id> or /profile/<name> are resolved
        # by the router, so every unmatched navigation returns the shell.
        return no_store(send_from_directory(FRONTEND_DIR, "index.html"))

    app.register_blueprint(spa)
    app.logger.info("Dev frontend mounted at / from %s", FRONTEND_DIR)
    return True


def _serve_upload(relative: str):
    """Serve a stored upload for local development.

    ``send_from_directory`` is safe against traversal by construction: it
    resolves the path and rejects anything that escapes the root.
    """
    if not relative:
        abort(404)
    root = os.path.abspath(current_app.config["UPLOAD_DIR"])
    return send_from_directory(root, relative)

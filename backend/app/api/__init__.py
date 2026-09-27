"""API blueprint registry.

All endpoints live under a single versioned prefix (``/api/v1``) except the
health and metrics probes, which load balancers and scrapers need to reach
without knowing the API version.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Flask, jsonify, render_template, send_from_directory

from . import admin, auth, chat, comments, errors, moderation, notifications, posts, privacy, uploads, users

BLUEPRINT_MODULES = (auth, users, posts, comments, uploads, chat, notifications, moderation, privacy, admin)


def register_blueprints(app: Flask) -> None:
    prefix = app.config.get("API_PREFIX", "/api/v1")
    for module in BLUEPRINT_MODULES:
        app.register_blueprint(module.bp, url_prefix=prefix)
    app.register_blueprint(api_root(app))
    errors.register_error_handlers(app)


def api_root(app: Flask) -> Blueprint:
    """Service discovery, OpenAPI document and interactive docs.

    Takes the app rather than reading ``current_app`` because it is called at
    registration time, when no application context is pushed yet.
    """
    root = Blueprint("api_root", __name__, url_prefix="/api")
    api_version = app.config.get("API_VERSION", "v1")

    @root.get("/")
    def index() -> Any:
        return jsonify(
            {
                "ok": True,
                "data": {
                    "name": app.config.get("APP_NAME"),
                    "name_en": app.config.get("APP_NAME_EN"),
                    "version": app.config.get("VERSION"),
                    "api_version": api_version,
                    "documentation": "/api/docs",
                    "openapi": "/api/openapi.json",
                    "health": app.config.get("HEALTH_PATH", "/healthz"),
                    "ready": app.config.get("READY_PATH", "/readyz"),
                    "metrics": app.config.get("METRICS_PATH", "/metrics"),
                },
            }
        )

    @root.get("/openapi.json")
    def openapi() -> Any:
        """The OpenAPI 3.1 document, generated from ``spec/openapi.yaml``."""
        return send_from_directory(app.static_folder, "openapi.json", mimetype="application/json", max_age=300)

    @root.get("/docs")
    def docs() -> Any:
        """Interactive API reference."""
        return render_template("docs.html", api_version=api_version)

    return root


__all__ = ["BLUEPRINT_MODULES", "api_root", "register_blueprints"]

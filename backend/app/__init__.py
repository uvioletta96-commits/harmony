"""Application factory.

``create_app()`` is the single composition root. It is intentionally explicit
about the order of operations, because several of these steps depend on the
ones before them:

1. config   — nothing else can be configured before this
2. logging  — so that later steps can report problems
3. extensions — ORM, Redis, CORS, migrations
4. security headers + proxy handling
5. blueprints, error handlers
6. observability (metrics, health)
7. CLI commands and the bootstrap admin
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, g, request

# Must precede every other relative import. `.config` evaluates `env("...")` in
# each profile's class body, so its values are snapshotted when `app.config` is
# first imported. Reading `.env` after that point loads the file into
# os.environ too late and every value in it is ignored - which only shows up
# where the file is the sole source of configuration, i.e. on a real server.
from .env_loader import load_dotenv_files as _load_dotenv_files

_load_dotenv_files()

from .config import ConfigError, get_config
from .extensions import celery_app, cors, db, init_redis, migrate, socketio, start_redis_supervisor
from .utils.logging import configure_logging, get_logger, install_request_context
from .utils.metrics import install_metrics, register_health

logger = get_logger("harmony.app")

__version__ = "1.0.0"


def create_app(config_name: str | None = None, **overrides: Any) -> Flask:
    """Build and configure a Flask application instance.

    The ``.env`` read is repeated here on purpose. It is idempotent and costs
    nothing, and it keeps the guarantee that every entry point - ``flask --app``,
    ``gunicorn wsgi:app``, the test suite - behaves the same even if something
    cleared the environment after import.
    """
    _load_dotenv_files()

    app = Flask(
        __name__,
        instance_relative_config=True,
        static_folder="static",
        template_folder="templates",
    )

    config = get_config(config_name)
    app.config.from_object(config)
    app.config.update(overrides)

    configure_logging(app, level=app.config.get("LOG_LEVEL", "INFO"), fmt=app.config.get("LOG_FORMAT", "json"))

    _ensure_upload_root(app)
    _init_extensions(app)
    _install_middleware(app)
    _register_blueprints(app)
    _install_security_headers(app)
    _install_observability(app)
    _register_commands(app)
    _bootstrap(app)

    logger.info(
        "Harmony application ready",
        extra={"env": app.config.get("ENV"), "version": app.config.get("VERSION"), "debug": app.debug},
    )
    return app


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _ensure_upload_root(app: Flask) -> None:
    directory = app.config.get("UPLOAD_DIR")
    if not directory:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        # Uploads are served by the app only when it is the sole node; in a
        # multi-node deployment nginx fronts this directory instead.
        os.makedirs(os.path.join(directory, "..", "exports"), exist_ok=True)
    except OSError as exc:  # pragma: no cover - misconfiguration
        app.logger.error("Cannot create upload directory %s: %s", directory, exc)


def on_serverless() -> bool:
    """True when running inside a short-lived, frozen function container.

    Two things this codebase does are actively wrong there, and both are
    correct everywhere else, so the environment has to be asked about rather
    than the features removed.

    A daemon thread is killed the moment the function returns, so a supervisor
    loop is pointless work on every cold start; and a long-lived WebSocket
    holds the invocation open until it is killed by the platform's timeout,
    which turns a realtime feature into a guaranteed failure and a bill.

    ``VERCEL`` is set by Vercel itself. The other two names cover Azure
    Functions and AWS Lambda, which have the same constraints, because a
    deployment that works on one serverless platform and not the next is worse
    than one that is honestly unsupported on both.
    """
    if os.environ.get("VERCEL"):
        return True
    if os.environ.get("FUNCTIONS_WORKER_RUNTIME"):  # Azure Functions
        return True
    runtime = os.environ.get("AWS_EXECUTION_ENV", "")
    return bool(runtime) and runtime.startswith("AWS_Lambda_")


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db, directory=_migrations_dir())

    # Probed first: the Socket.IO decision below depends on the answer, and
    # probing twice would pay the connect timeout twice. Then a background thread
    # keeps the answer current, so no request ever pays a connect timeout to
    # discover Redis is not there.
    init_redis(app)
    if not on_serverless():
        start_redis_supervisor(app)

    from .realtime import events as _events  # noqa: F401  (registers handlers)

    # The message queue is what makes Socket.IO scale across processes, so it
    # should be on wherever Redis is *reachable* - not merely wherever a URL is
    # configured. Pointed at a Redis that is not running, python-socketio retries
    # the connection forever and floods the log, while every broadcast silently
    # goes nowhere. So the decision is made from a real probe, and logged.
    message_queue = app.config.get("SOCKETIO_MESSAGE_QUEUE")
    if message_queue and not on_serverless():
        from .extensions import get_redis

        if get_redis() is None:
            logger.warning(
                "Socket.IO message queue disabled: %s is unreachable. Realtime events will not cross worker processes.",
                message_queue,
            )
            message_queue = None
        else:
            logger.info("Socket.IO message queue enabled: %s", message_queue)

    if on_serverless():
        # Not attached. `init_app` wraps `app.wsgi_app` in engineio's middleware,
        # and a WebSocket request then holds the invocation open until the
        # platform kills it - a guaranteed timeout rather than a fast failure.
        # The handlers stay registered, and the client's connection attempt
        # fails immediately, which is the same shape it already has when the
        # message queue is unreachable.
        logger.warning(
            "Socket.IO not attached: this platform freezes between invocations, "
            "so a long-lived connection cannot outlive one. Realtime updates are "
            "unavailable; everything else works."
        )
    else:
        socketio.init_app(
            app,
            async_mode=app.config.get("SOCKETIO_ASYNC_MODE", "threading"),
            message_queue=message_queue,
            cors_allowed_origins=app.config.get("SOCKETIO_CORS_ALLOWED_ORIGINS") or [],
            manage_session=False,
        )

    origins = app.config.get("CORS_ORIGINS") or []
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": origins}},
        supports_credentials=bool(app.config.get("CORS_SUPPORTS_CREDENTIALS")),
        allow_headers=[
            "Content-Type",
            "Authorization",
            app.config.get("CSRF_HEADER_NAME", "X-CSRF-Token"),
            "X-Request-ID",
        ],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Remaining",
            "X-RateLimit-Limit",
            "X-Response-Time-ms",
            "Retry-After",
        ],
        max_age=3600,
    )
    from .tasks.celery_app import configure_celery

    configure_celery(app, celery_app)
    from .tasks.maintenance_tasks import SCHEDULE

    celery_app.conf.beat_schedule = SCHEDULE


def _migrations_dir() -> str | None:
    for candidate in (
        os.environ.get("MIGRATIONS_DIR"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"),
    ):
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def _install_middleware(app: Flask) -> None:
    install_request_context(app)

    @app.before_request
    def _global_guards() -> None:
        from .security.decorators import verify_csrf
        from .security.rate_limit import enforce

        # CSRF is verified before anything else so no state-changing handler can
        # run ahead of it.
        verify_csrf()

        # The global limiter protects traffic that never reaches a
        # route-specific limit: scanners, asset harvesters, malformed paths.
        if request.path.startswith(app.config.get("API_PREFIX", "/api")):
            g._rate_limit_headers = enforce("global").headers()

    @app.teardown_appcontext
    def _cleanup(exception: BaseException | None = None) -> None:
        """Return the session to the pool in a clean state.

        On an unhandled error the session is rolled back first; without this a
        poisoned session would be reused by the next request on the same
        connection and fail in a confusing, distant place.
        """
        if exception is not None:
            try:
                db.session.rollback()
            except Exception:  # pragma: no cover
                pass
        try:
            db.session.remove()
        except Exception:  # pragma: no cover
            pass


def _register_blueprints(app: Flask) -> None:
    from .api import register_blueprints

    register_blueprints(app)

    # Registered last on purpose: the SPA fallback is a catch-all, and a
    # catch-all added first would shadow anything registered after it.
    from .dev_frontend import register_dev_frontend

    register_dev_frontend(app)


def _install_security_headers(app: Flask) -> None:
    """Response headers applied to every request."""

    @app.after_request
    def _headers(response):
        config = app.config
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("X-XSS-Protection", "0")  # legacy header; CSP replaces it
        response.headers.setdefault(
            "Content-Security-Policy",
            _csp(app),
        )
        if config.get("FORCE_HTTPS"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={config.get('HSTS_MAX_AGE')}; includeSubDomains={str(config.get('HSTS_INCLUDE_SUBDOMAINS')).lower()}"
                + ("; preload" if config.get("HSTS_PRELOAD") else ""),
            )
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")

        # The verdict computed in before_request carries the budget the client
        # still has. Publishing it lets a well-behaved client back off *before*
        # it is rejected, and it is the only way a caller can tell a slow
        # endpoint from a throttled one.
        for name, value in (getattr(g, "_rate_limit_headers", None) or {}).items():
            response.headers.setdefault(name, value)

        if request.path.startswith(config.get("API_PREFIX", "/api")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    if app.config.get("FORCE_HTTPS"):
        _install_https_redirect(app)


def _csp(app: Flask) -> str:
    """Content Security Policy.

    ``default-src 'self'`` with no ``unsafe-inline`` for scripts is the single
    most effective anti-XSS control, and it stays that way. Inline *styles* are
    allowed because the design system applies CSS custom properties through
    style attributes for dynamic values such as avatar colours.

    The extra sources below are the ones ``frontend/index.html`` genuinely
    loads, listed per directive so that each grant is visible and reviewable.
    A single catch-all ``default-src`` relaxation would let a compromised font
    CDN run script, which is precisely the attack CSP exists to stop. The
    intended end state is self-hosting both, at which point the entries go away
    and the policy tightens on its own.
    """
    config = app.config
    extra_script = " ".join(config.get("CSP_SCRIPT_SRC", ()))
    extra_style = " ".join(config.get("CSP_STYLE_SRC", ()))
    extra_font = " ".join(config.get("CSP_FONT_SRC", ()))
    extra_connect = " ".join(config.get("CSP_CONNECT_SRC", ()))

    def directive(name: str, *sources: str, extras: str = "") -> str:
        parts = [name, "'self'", *sources]
        if extras:
            parts.append(extras)
        return " ".join(part for part in parts if part)

    return "; ".join(
        [
            "default-src 'self'",
            directive("script-src", *config.get("CSP_SCRIPT_SELF_SRC", ()), extras=extra_script),
            # 'unsafe-inline' for styles only: the design system sets custom
            # properties inline. It says nothing about scripts.
            directive("style-src", "https://cdn.jsdelivr.net", extras="'unsafe-inline' " + extra_style),
            directive("img-src", "data:", "blob:"),
            directive("font-src", "data:", extras=extra_font),
            directive("connect-src", "ws:", "wss:", extras=extra_connect),
            "frame-ancestors 'none'",
            "form-action 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "upgrade-insecure-requests",
        ]
    )


def _install_https_redirect(app: Flask) -> None:
    """Redirect plain HTTP to HTTPS, trusting exactly N proxy hops.

    Getting this wrong is a real outage: trusting ``X-Forwarded-Proto``
    unconditionally lets a client claim HTTPS, and trusting none of it behind
    a TLS-terminating proxy produces a redirect loop.
    """

    @app.before_request
    def _redirect():
        from flask import redirect

        if request.method == "OPTIONS" or request.path.startswith(("/healthz", "/readyz", "/metrics")):
            return None
        forwarded = request.headers.get("X-Forwarded-Proto", "")
        # Take the *last* value: the entry added by the proxy closest to us is
        # the one that reflects the client-facing connection.
        proto = forwarded.split(",")[-1].strip() if forwarded else request.scheme
        if proto == "https":
            return None
        target = request.url.replace("http://", "https://", 1)
        return redirect(target, code=308)


def _install_observability(app: Flask) -> None:
    install_metrics(app)
    register_health(app)


def _register_commands(app: Flask) -> None:
    from .cli import register_commands

    register_commands(app)


def _bootstrap(app: Flask) -> None:
    """First-run setup, safe to run on every boot."""
    if app.config.get("TESTING"):
        return
    with app.app_context():
        try:
            from .services.gdpr_service import ensure_legal_documents

            ensure_legal_documents()
        except Exception as exc:  # pragma: no cover - DB may not be up yet
            app.logger.warning("Legal document bootstrap skipped: %s", exc)

        try:
            _ensure_admin(app)
        except Exception as exc:  # pragma: no cover
            app.logger.warning("Admin bootstrap skipped: %s", exc)


def _ensure_admin(app: Flask) -> None:
    """Create the bootstrap administrator if configured and not yet present.

    Idempotent: it only ever runs on an empty ``users`` table with the
    credentials supplied through the environment, and it refuses to invent a
    password.
    """
    from .extensions import db as _db
    from .extensions import hash_password
    from .models.user import User, UserRole, UserStatus

    email = app.config.get("ADMIN_EMAIL")
    password = app.config.get("ADMIN_PASSWORD")
    username = app.config.get("ADMIN_USERNAME") or "admin"

    if not email or not password:
        return
    if len(password) < 12:
        app.logger.error("ADMIN_PASSWORD is too short (minimum 12 characters); admin not created.")
        return
    if _db.session.query(User).filter(User.role == UserRole.ADMIN.value).first() is not None:
        return

    admin = User(
        email=email.strip().lower(),
        username=username,
        password_hash=hash_password(password),
        display_name="Администратор",
        role=UserRole.ADMIN.value,
        status=UserStatus.ACTIVE.value,
        email_verified=True,
        avatar_color="stone",
        data_processing_consent=True,
    )
    _db.session.add(admin)
    _db.session.commit()
    app.logger.warning("Bootstrap administrator created", extra={"username": username})


# ``ConfigError``, ``celery_app`` and ``socketio`` are re-exported so the CLI and
# the worker entry points can do ``from app import ...`` without reaching into
# submodules.
__all__ = ["ConfigError", "__version__", "celery_app", "create_app", "socketio"]

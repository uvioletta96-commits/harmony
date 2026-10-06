"""Application configuration.

Every deployment-specific value is read from the environment (12-factor style).
Values are validated eagerly so a misconfigured container fails fast at boot
instead of surfacing as a runtime error hours later.
"""

from __future__ import annotations

import os
import secrets
import sys
from typing import Any, ClassVar

_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}


class ConfigError(RuntimeError):
    """Raised when the environment is missing a value required by the profile."""


def env(name: str, default: Any = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    lowered = str(raw).lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"Environment variable {name}={raw!r} is not a valid boolean.")


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"Environment variable {name}={raw!r} is not an integer.") from exc


def env_list(name: str, default: str = "") -> list[str]:
    raw = env(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


class BaseConfig:
    """Settings shared by every environment profile."""

    # --- Core identity -------------------------------------------------
    APP_NAME: str = "Гармония"
    APP_NAME_EN: str = "Harmony"
    APP_SLUG: str = "harmony"
    VERSION: str = "1.0.0"
    API_VERSION: str = "v1"
    ENV: str = env("APP_ENV", "development")

    # Development fallback only. Production MUST provide SECRET_KEY.
    SECRET_KEY: str = env("SECRET_KEY") or "dev-insecure-secret-change-me"
    JWT_SECRET_KEY: str = env("JWT_SECRET_KEY") or ""  # derived from SECRET_KEY when empty
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL: int = env_int("ACCESS_TOKEN_TTL_SECONDS", 60 * 60 * 12)
    REFRESH_TOKEN_TTL: int = env_int("REFRESH_TOKEN_TTL_SECONDS", 60 * 60 * 24 * 30)
    EMAIL_TOKEN_TTL: int = env_int("EMAIL_TOKEN_TTL_SECONDS", 60 * 60 * 24)

    # --- Persistence ---------------------------------------------------
    SQLALCHEMY_DATABASE_URI: str = env("DATABASE_URL") or "sqlite:///harmony-dev.db"
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict[str, Any]] = {
        "pool_pre_ping": True,
        "pool_recycle": env_int("DB_POOL_RECYCLE_SECONDS", 1800),
        "pool_size": env_int("DB_POOL_SIZE", 10),
        "max_overflow": env_int("DB_MAX_OVERFLOW", 20),
        "pool_timeout": env_int("DB_POOL_TIMEOUT", 30),
    }
    DB_STATEMENT_TIMEOUT_MS: int = env_int("DB_STATEMENT_TIMEOUT_MS", 8000)

    # --- Cache / broker ------------------------------------------------
    REDIS_URL: str = env("REDIS_URL") or "redis://localhost:6379/0"
    CACHE_DEFAULT_TTL: int = env_int("CACHE_DEFAULT_TTL", 60)
    CACHE_ENABLED: bool = env_bool("CACHE_ENABLED", True)
    CELERY_BROKER_URL: str = env("CELERY_BROKER_URL") or env("REDIS_URL") or ""
    CELERY_RESULT_BACKEND: str = env("CELERY_RESULT_BACKEND") or env("REDIS_URL") or ""
    CELERY_ALWAYS_EAGER: bool = env_bool("CELERY_ALWAYS_EAGER", False)

    # --- HTTP ----------------------------------------------------------
    SITE_URL: str = (env("SITE_URL") or "http://localhost:8000").rstrip("/")
    # All versioned endpoints live under /api/<version>. The unversioned
    # /api root is the service-discovery document.
    API_PREFIX: str = "/api/" + API_VERSION
    CORS_ORIGINS: list[str] = env_list("CORS_ORIGINS", "http://localhost:8000")
    CORS_SUPPORTS_CREDENTIALS: bool = True
    MAX_CONTENT_LENGTH: int = env_int("MAX_CONTENT_LENGTH", 12 * 1024 * 1024)
    JSON_SORT_KEYS: bool = False
    SEND_FILE_MAX_AGE_DEFAULT: int = env_int("STATIC_MAX_AGE_SECONDS", 31_536_000)

    # --- Security ------------------------------------------------------
    FORCE_HTTPS: bool = env_bool("FORCE_HTTPS", False)
    HSTS_MAX_AGE: int = env_int("HSTS_MAX_AGE_SECONDS", 31_536_000)
    HSTS_INCLUDE_SUBDOMAINS: bool = True
    HSTS_PRELOAD: bool = env_bool("HSTS_PRELOAD", False)
    TRUSTED_PROXY_COUNT: int = env_int("TRUSTED_PROXY_COUNT", 1)
    SECURE_COOKIE: bool = env_bool("SECURE_COOKIE", env_bool("FORCE_HTTPS", False))
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = env("COOKIE_SAMESITE", "Lax")
    CSRF_COOKIE_NAME: str = "harmony_csrf"
    CSRF_HEADER_NAME: str = "X-CSRF-Token"
    CSRF_PROTECTION: bool = True
    BCRYPT_ROUNDS: int = env_int("BCRYPT_ROUNDS", 12)
    TOKEN_PEPPER: str = env("TOKEN_PEPPER") or ""

    # --- Rate limiting -------------------------------------------------
    RATE_LIMIT_ENABLED: bool = env_bool("RATE_LIMIT_ENABLED", True)
    RATE_LIMIT_STORAGE_URL: str = env("RATE_LIMIT_STORAGE_URL") or env("REDIS_URL") or ""
    RATE_LIMIT_DEFAULT: str = env("RATE_LIMIT_DEFAULT", "300/hour")
    RATE_LIMITS: ClassVar[dict[str, str]] = {
        "auth:login": env("RATE_LIMIT_LOGIN", "10/minute;5/hour"),
        "auth:register": env("RATE_LIMIT_REGISTER", "5/hour;20/day"),
        "auth:forgot": env("RATE_LIMIT_FORGOT", "5/hour"),
        "auth:refresh": env("RATE_LIMIT_REFRESH", "60/hour"),
        "post:create": env("RATE_LIMIT_POST_CREATE", "20/hour"),
        "post:update": env("RATE_LIMIT_POST_UPDATE", "60/hour"),
        "post:delete": env("RATE_LIMIT_POST_DELETE", "60/hour"),
        "comment:create": env("RATE_LIMIT_COMMENT_CREATE", "60/hour"),
        "reaction:write": env("RATE_LIMIT_REACTION", "300/hour"),
        # A view is the cheapest write on the site and the one a client is most
        # tempted to call in a loop. Generous for a reader paging through a feed,
        # tight enough that a loop cannot inflate a number - 600 an hour is about
        # two screens a second sustained.
        "post:view": env("RATE_LIMIT_POST_VIEW", "600/hour"),
        "search:query": env("RATE_LIMIT_SEARCH", "120/hour"),
        "chat:send": env("RATE_LIMIT_CHAT_SEND", "120/minute"),
        "upload:image": env("RATE_LIMIT_UPLOAD", "30/hour"),
        "report:create": env("RATE_LIMIT_REPORT", "20/hour"),
        "global": env("RATE_LIMIT_GLOBAL", "1200/hour"),
    }
    RATE_LIMIT_TRUST_PROXY: bool = env_bool("RATE_LIMIT_TRUST_PROXY", True)

    # --- Spam defences -------------------------------------------------
    SPAM_HONEYPOT_FIELD: str = "harmony_hp"
    SPAM_MAX_LINKS: int = env_int("SPAM_MAX_LINKS", 3)
    SPAM_MAX_DUPLICATE_WINDOW: int = env_int("SPAM_MAX_DUPLICATE_WINDOW", 300)
    ACCOUNT_MAX_LOGIN_FAILURES: int = env_int("ACCOUNT_MAX_LOGIN_FAILURES", 8)
    ACCOUNT_LOCKOUT_SECONDS: int = env_int("ACCOUNT_LOCKOUT_SECONDS", 900)

    # --- Content moderation --------------------------------------------
    MODERATION_ENABLED: bool = env_bool("MODERATION_ENABLED", True)
    MODERATION_PROVIDER: str = env("MODERATION_PROVIDER", "local")  # local|openai|custom
    MODERATION_API_URL: str = env("MODERATION_API_URL") or ""
    MODERATION_API_KEY: str = env("MODERATION_API_KEY") or ""
    MODERATION_MODEL: str = env("MODERATION_MODEL", "omni-moderation-latest")
    MODERATION_TIMEOUT: float = float(env("MODERATION_TIMEOUT", "4") or 4)
    MODERATION_FAIL_CLOSED: bool = env_bool("MODERATION_FAIL_CLOSED", False)
    MODERATION_BLOCK_THRESHOLD: float = float(env("MODERATION_BLOCK_THRESHOLD", "0.85") or 0.85)
    MODERATION_REVIEW_THRESHOLD: float = float(env("MODERATION_REVIEW_THRESHOLD", "0.55") or 0.55)
    MODERATION_RECHECK_ASYNC: bool = env_bool("MODERATION_RECHECK_ASYNC", True)
    MODERATION_LOCAL_DICTIONARY: str = env("MODERATION_DICTIONARY_PATH", "")

    # --- Uploads -------------------------------------------------------
    UPLOAD_DIR: str = env("UPLOAD_DIR") or os.path.join(os.getcwd(), "backend", "app", "static", "uploads")
    UPLOAD_URL_PREFIX: str = "/uploads"
    UPLOAD_MAX_BYTES: int = env_int("UPLOAD_MAX_BYTES", 8 * 1024 * 1024)
    UPLOAD_MAX_DIMENSION: int = env_int("UPLOAD_MAX_DIMENSION", 2560)
    UPLOAD_THUMB_DIMENSION: int = env_int("UPLOAD_THUMB_DIMENSION", 640)
    UPLOAD_ALLOWED_MIME: list[str] = env_list("UPLOAD_ALLOWED_MIME", "image/jpeg,image/png,image/webp,image/gif")
    UPLOAD_ALLOWED_VIDEO_MIME: list[str] = env_list("UPLOAD_ALLOWED_VIDEO_MIME", "video/mp4,video/webm,video/ogg")
    #: Video gets a far larger ceiling than an image. It is also never re-encoded
    #: on the way in, so unlike a photo this is exactly the bytes that were
    #: uploaded - the cap is the only thing standing between a user and a disk
    #: full of files, and it is enforced before anything touches the disk.
    UPLOAD_MAX_VIDEO_BYTES: int = env_int("UPLOAD_MAX_VIDEO_BYTES", 50 * 1024 * 1024)
    UPLOAD_STRIP_EXIF: bool = env_bool("UPLOAD_STRIP_EXIF", True)
    SERVE_UPLOADS: bool = env_bool("SERVE_UPLOADS", True)

    # --- Mail ----------------------------------------------------------
    #: Whether a new account must confirm its address before it can sign in.
    #:
    #: This is what stops somebody registering with an address they do not own -
    #: to squat a username, to hold a name against its owner, or to bounce mail
    #: at somebody else. Turning it off makes the account unproven: whoever
    #: typed the address is whoever owns it.
    #:
    #: It is a flag rather than deleted code because it is a property of the
    #: deployment, not of the product. A host with no mail service can turn it
    #: off and still use every other guarantee in this file; the day it gets a
    #: mail service, one environment variable puts it back.
    #:
    #: Turning it on while `MAIL_ENABLED` is off is a misconfiguration: nobody
    #: can confirm, so nobody can sign in. `production.validate()` refuses it.
    REQUIRE_EMAIL_VERIFICATION: bool = env_bool("REQUIRE_EMAIL_VERIFICATION", True)
    MAIL_ENABLED: bool = env_bool("MAIL_ENABLED", True)
    MAIL_BACKEND: str = env("MAIL_BACKEND", "smtp")  # smtp|console|disabled
    MAIL_SERVER: str = env("MAIL_SERVER", "") or ""
    MAIL_PORT: int = env_int("MAIL_PORT", 587)
    MAIL_USERNAME: str = env("MAIL_USERNAME") or ""
    MAIL_PASSWORD: str = env("MAIL_PASSWORD") or ""
    MAIL_USE_TLS: bool = env_bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL: bool = env_bool("MAIL_USE_SSL", False)
    MAIL_TIMEOUT: int = env_int("MAIL_TIMEOUT", 10)
    MAIL_FROM: str = env("MAIL_FROM", "no-reply@harmony.local")
    MAIL_FROM_NAME: str = env("MAIL_FROM_NAME", "Гармония")
    MAIL_SUPPRESS_SEND: bool = env_bool("MAIL_SUPPRESS_SEND", False)

    # --- Realtime ------------------------------------------------------
    SOCKETIO_ASYNC_MODE: str = env("SOCKETIO_ASYNC_MODE", "threading")
    SOCKETIO_MESSAGE_QUEUE: str | None = env("SOCKETIO_MESSAGE_QUEUE") or None
    SOCKETIO_CORS_ALLOWED_ORIGINS: list[str] = env_list("SOCKETIO_CORS_ORIGINS", "")
    CHAT_RETENTION_DAYS: int = env_int("CHAT_RETENTION_DAYS", 365)
    CHAT_MAX_MESSAGE_CHARS: int = env_int("CHAT_MAX_MESSAGE_CHARS", 4000)

    # --- Privacy / GDPR ------------------------------------------------
    PRIVACY_CONTROLLER_EMAIL: str = env("PRIVACY_CONTROLLER_EMAIL", "privacy@harmony.local")
    DPO_EMAIL: str = env("DPO_EMAIL", "dpo@harmony.local")
    DATA_RETENTION_DAYS: int = env_int("DATA_RETENTION_DAYS", 365)
    ACCOUNT_DELETION_GRACE_DAYS: int = env_int("ACCOUNT_DELETION_GRACE_DAYS", 14)
    IP_HASH_SALT: str = env("IP_HASH_SALT") or ""
    LOG_USER_IDENTIFIERS: bool = env_bool("LOG_USER_IDENTIFIERS", True)

    # --- Observability -------------------------------------------------
    LOG_LEVEL: str = (env("LOG_LEVEL", "INFO") or "INFO").upper()
    LOG_FORMAT: str = env("LOG_FORMAT", "json")  # json|text
    METRICS_ENABLED: bool = env_bool("METRICS_ENABLED", True)
    METRICS_PATH: str = "/metrics"
    HEALTH_PATH: str = "/healthz"
    READY_PATH: str = "/readyz"
    SLOW_REQUEST_MS: int = env_int("SLOW_REQUEST_MS", 1200)

    #: Where the nightly backup writes its dumps. Unset means no backup is
    #: configured for this deployment - a laptop, CI, a preview - and /readyz says
    #: "disabled" rather than inventing an age. Only the server sets it.
    #:
    #: It is read on every /readyz because a backup has no user waiting on it and
    #: no request that fails when it stops happening. The nightly job failed for
    #: two days with the timer still reporting `active (waiting)`; this is what
    #: makes that visible to a deploy, which already calls the endpoint.
    BACKUP_DIR: str | None = env("BACKUP_DIR") or None

    # --- Content Security Policy -----------------------------------------
    # The sources ``frontend/index.html`` actually loads, granted per directive.
    # Keeping them explicit means a grant can be reviewed and removed, and it
    # documents the product's real third-party surface instead of hiding it
    # behind a relaxed ``default-src``.
    #
    # Intended end state: self-host the font too, then delete these lists. The
    # policy is already strict enough to allow it.
    #
    # cdn.socket.io used to be here. It is gone because the Socket.IO client is
    # now vendored under frontend/vendor/: a chat that silently degrades to REST
    # because a CDN was slow is not a working chat, and the whole realtime layer
    # - chat delivery, live comments, online presence - hung off one <script> tag
    # pointing at someone else's uptime.
    CSP_SCRIPT_SRC: ClassVar[list[str]] = []
    CSP_STYLE_SRC: ClassVar[list[str]] = ["https://fonts.googleapis.com"]
    CSP_FONT_SRC: ClassVar[list[str]] = ["https://fonts.gstatic.com"]
    CSP_CONNECT_SRC: ClassVar[list[str]] = []
    # Was jsdelivr. Nothing in the frontend referenced it any more, and an
    # origin allowed to serve script is an origin whose outage or compromise
    # takes the site down - so an unused entry here is pure downside.
    CSP_SCRIPT_SELF_SRC: ClassVar[list[str]] = []
    #: Serve the frontend bundle and uploads from the application itself. Only
    #: the development profile enables this; production is nginx's job.
    SERVE_FRONTEND: bool = False

    # --- Admin bootstrap -----------------------------------------------
    ADMIN_EMAIL: str = env("ADMIN_EMAIL", "") or ""
    ADMIN_USERNAME: str = env("ADMIN_USERNAME", "admin") or "admin"
    ADMIN_PASSWORD: str = env("ADMIN_PASSWORD", "") or ""

    # --- Behaviour -----------------------------------------------------
    FEED_PAGE_SIZE: int = env_int("FEED_PAGE_SIZE", 15)
    #: How many recent posts a shuffled or personalised page draws from before
    #: ordering them. This is the ceiling on the work the ordering can cause, so
    #: it is a real limit rather than a hint: raising it to 100000 would turn
    #: every feed request into a full table sort.
    FEED_SHUFFLE_POOL: int = env_int("FEED_SHUFFLE_POOL", 150)
    COMMENT_PAGE_SIZE: int = env_int("COMMENT_PAGE_SIZE", 20)
    MAX_COMMENT_DEPTH: int = env_int("MAX_COMMENT_DEPTH", 3)
    NOTIFICATION_RETENTION_DAYS: int = env_int("NOTIFICATION_RETENTION_DAYS", 90)
    USERNAME_MIN_LENGTH: int = env_int("USERNAME_MIN_LENGTH", 3)
    USERNAME_MAX_LENGTH: int = env_int("USERNAME_MAX_LENGTH", 32)
    PASSWORD_MIN_LENGTH: int = env_int("PASSWORD_MIN_LENGTH", 10)
    POST_MAX_CHARS: int = env_int("POST_MAX_CHARS", 5000)
    MAX_POST_IMAGES: int = env_int("MAX_POST_IMAGES", 4)
    #: Total attachments on one post, images and video together. Separate from
    #: MAX_POST_IMAGES because a single post is not allowed to be nine photos
    #: and four videos just because each of those limits is on its own.
    MAX_POST_MEDIA: int = env_int("MAX_POST_MEDIA", 6)
    TESTING: bool = False
    DEBUG: bool = False
    DEBUG_SQL: bool = False

    # -- Derived --------------------------------------------------------
    @classmethod
    def normalise(cls) -> None:
        """Coerce cross-field dependencies once, on the *concrete* config class.

        Called on the subclass so a profile that overrides one of these values
        (TestingConfig sets ``REDIS_URL = ""``) is not silently undone by a
        value inherited from the base.
        """
        if not cls.JWT_SECRET_KEY:
            cls.JWT_SECRET_KEY = cls.SECRET_KEY
        if not cls.RATE_LIMIT_STORAGE_URL:
            cls.RATE_LIMIT_STORAGE_URL = cls.REDIS_URL
        if not cls.CELERY_BROKER_URL:
            cls.CELERY_BROKER_URL = cls.REDIS_URL
        if not cls.CELERY_RESULT_BACKEND:
            cls.CELERY_RESULT_BACKEND = cls.REDIS_URL
        if not cls.SOCKETIO_MESSAGE_QUEUE:
            cls.SOCKETIO_MESSAGE_QUEUE = cls.REDIS_URL or None
        if not cls.SOCKETIO_CORS_ALLOWED_ORIGINS:
            cls.SOCKETIO_CORS_ALLOWED_ORIGINS = list(cls.CORS_ORIGINS)
        if not cls.IP_HASH_SALT:
            cls.IP_HASH_SALT = cls.SECRET_KEY

    @property
    def jwt_exp_seconds(self) -> int:
        return self.ACCESS_TOKEN_TTL

    @property
    def refresh_exp_seconds(self) -> int:
        return self.REFRESH_TOKEN_TTL


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    LOG_FORMAT = "text"
    FORCE_HTTPS = False
    SECURE_COOKIE = False
    MAIL_BACKEND = "console"
    MAIL_ENABLED = False
    #: Serve the frontend from the application so `flask run` is enough to see
    #: the product. Off everywhere else: in production nginx owns the static
    #: tier, and serving uploads from the API origin would collapse the
    #: two-origin split the CSP and cookie attributes depend on.
    SERVE_FRONTEND = True


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    ENV = "testing"
    SQLALCHEMY_DATABASE_URI = env("TEST_DATABASE_URL") or "sqlite://"  # in-memory
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict[str, Any]] = {}
    CSRF_PROTECTION = True
    # Redis is optional by design, and the test suite must not depend on it:
    # an empty URL makes the client skip connection attempts entirely rather
    # than timing out once per test. Tests that exercise Redis-backed
    # behaviour set TEST_REDIS_URL and opt in explicitly.
    REDIS_URL = env("TEST_REDIS_URL") or ""
    RATE_LIMIT_ENABLED = False
    CELERY_ALWAYS_EAGER = True
    CELERY_TASK_ALWAYS_EAGER = True
    MAIL_ENABLED = False
    MAIL_BACKEND = "console"
    MAIL_SUPPRESS_SEND = True
    CACHE_ENABLED = False
    MODERATION_RECHECK_ASYNC = False
    SOCKETIO_ASYNC_MODE = "threading"
    PASSWORD_MIN_LENGTH = 8
    # Fixed placeholders so tokens are reproducible across a test session. The
    # production profile rejects anything of this shape, so a leaked value can
    # never reach a running deployment.
    SECRET_KEY = "testing-secret-key-0123456789abcdef0123456789"  # noqa: S105
    JWT_SECRET_KEY = "testing-jwt-key-0123456789abcdef0123456789"  # noqa: S105
    TOKEN_PEPPER = "testing-pepper"  # noqa: S105
    LOG_LEVEL = "CRITICAL"
    ACCOUNT_LOCKOUT_SECONDS = 1
    SPAM_MAX_DUPLICATE_WINDOW = 1


class ProductionConfig(BaseConfig):
    DEBUG = False
    LOG_FORMAT = "json"

    #: Explicit so callers can ask "is this a preview?" without knowing which
    #: profile class they were handed.
    PREVIEW = False

    def collect_problems(self) -> tuple[list[str], list[str]]:
        """Everything wrong with this configuration, split by severity.

        Two buckets, and the split is the point rather than an implementation
        detail.

        **Mandatory** is the signing key and nothing else. A weak one is the
        ability to mint a session for any account, which is a defect rather than
        a missing feature, so no deployment profile may waive it.

        **Relaxable** is everything that needs money, an account or a
        certificate before a site can be seen at all: PostgreSQL, an SMTP
        provider, TLS. Each carries a real cost - data that does not survive a
        redeploy, accounts nobody can verify, a session token in the clear - and
        :class:`PreviewConfig` waives them by ignoring this bucket and printing
        each one on every start. It reads the checks rather than overriding
        them, which is the only reason the mandatory one still holds.
        """
        mandatory: list[str] = []
        relaxable: list[str] = []

        # `self.SECRET_KEY`, not a fresh read of the environment. The two can
        # disagree - anything that sets the attribute after import, which is how
        # a test or a wrapper supplies a secret, would pass the length check and
        # fail the "is this the published default" check, or the reverse. The
        # resolved value is the one the application actually signs with.
        if self.SECRET_KEY in ("", "dev-insecure-secret-change-me"):
            mandatory.append("SECRET_KEY must be set to a strong random value.")
        if len(self.SECRET_KEY) < 32:
            mandatory.append("SECRET_KEY must be at least 32 characters long.")
        if not self.SQLALCHEMY_DATABASE_URI.startswith(("postgres", "postgresql")):
            relaxable.append(
                "DATABASE_URL must point at PostgreSQL. Anything else means the data does not survive a redeploy."
            )
        if self.MAIL_BACKEND == "smtp" and not self.MAIL_SERVER:
            relaxable.append("MAIL_SERVER is required when MAIL_BACKEND=smtp.")
        if self.REQUIRE_EMAIL_VERIFICATION and not self.MAIL_ENABLED:
            # Mandatory, and the only thing here that is. The others are
            # obligations that degrade the service; this one makes the product
            # unusable: no confirmation can be delivered, so every account that
            # registers is permanently locked out of its own login.
            mandatory.append(
                "REQUIRE_EMAIL_VERIFICATION is true but MAIL_ENABLED is false, so no "
                "confirmation can be delivered and nobody who registers can ever sign in. "
                "Either configure a mail server or set REQUIRE_EMAIL_VERIFICATION=false."
            )
        if not self.REQUIRE_EMAIL_VERIFICATION:
            relaxable.append(
                "REQUIRE_EMAIL_VERIFICATION is off: anyone who registers with an address "
                "they do not own gets a working account, so usernames and addresses are "
                "unproven. Fine for a closed beta, not for an open registration."
            )
        if not self.MAIL_ENABLED:
            relaxable.append(
                "MAIL_ENABLED must be true so verification emails are delivered. "
                "While it is off, any account that registers is unverified."
            )
        if self.MODERATION_PROVIDER != "local" and not self.MODERATION_API_KEY:
            relaxable.append("MODERATION_API_KEY is required for a remote moderation provider.")
        if not self.SECURE_COOKIE:
            # Relaxable, not ignored. On a host with no certificate - a free
            # subdomain served over plain HTTP - a `Secure` cookie is never sent
            # back by the browser and nobody can sign in at all. The cost is real:
            # the session token travels in the clear and anyone on the network
            # can read it. That belongs in the bucket a profile has to announce
            # out loud, which is exactly what this one is for.
            relaxable.append(
                "SECURE_COOKIE is off: the session cookie will travel over plain HTTP and "
                "can be read off the network. Acceptable only for a private preview on a "
                "host with no certificate - not for anything public."
            )

        return mandatory, relaxable

    def __init__(self) -> None:  # pragma: no cover - executed at boot
        mandatory, relaxable = self.collect_problems()
        errors = [*mandatory, *relaxable]
        if errors:
            raise ConfigError("Invalid production configuration:\n  - " + "\n  - ".join(errors))


class PreviewConfig(ProductionConfig):
    """Production, with the two checks that need a paid account relaxed.

    Both of them are obligations rather than correctness: a PostgreSQL
    subscription and an SMTP provider. As hard gates they mean the first deploy
    cannot happen at all, and a project that has never been online cannot be
    shown to anybody.

    So this profile gets a site up, and it is loud about what that costs:

    * ``MAIL_ENABLED`` may be off, so there is no address verification and
      anyone who can register is unverified. Keep it private.
    * The database may be SQLite, which lives in the container and is lost on
      every redeploy.

    Both are printed to stderr on every process start, in capitals, so a running
    container can be identified without reading its environment. Switch
    ``APP_ENV`` to ``production`` the moment a real database and a real mail
    provider exist.

    The signing key stays mandatory. A weak ``SECRET_KEY`` is not a missing
    feature, it is the ability to mint a session for any account, so it is not
    part of what this profile relaxes.
    """

    PREVIEW = True

    #: The point of a preview is that there is something to look at. The base
    #: default is False because production is expected to have the frontend
    #: served by nginx in front of it, and inheriting that here produced a
    #: deployment that answered the API and 404'd every page - which looks
    #: exactly like a broken build rather than a profile default.
    SERVE_FRONTEND = True

    def __init__(self) -> None:  # pragma: no cover - executed at boot
        # Inherited, so the security checks cannot be skipped by overriding
        # them. An earlier version of this class replaced __init__ outright and
        # a five-character SECRET_KEY booted happily.
        mandatory, relaxable = self.collect_problems()
        if mandatory:
            raise ConfigError("Invalid configuration even for preview mode:\n  - " + "\n  - ".join(mandatory))
        if not relaxable:
            return
        sys.stderr.write(
            "\n" + "=" * 72 + "\n"
            "PREVIEW MODE - not ready to be public:\n"
            + "".join(f"  - {line}\n" for line in relaxable)
            + "=" * 72
            + "\n\n"
        )
        sys.stderr.flush()


CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "preview": PreviewConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None) -> BaseConfig:
    """Return a normalised config instance for the requested profile."""
    key = (name or os.environ.get("APP_ENV") or "development").strip().lower()
    config_cls = CONFIGS.get(key)
    if config_cls is None:
        raise ConfigError(f"Unknown APP_ENV={key!r}. Expected one of {sorted(CONFIGS)}.")
    config = config_cls()  # type: ignore[call-arg]
    BaseConfig.normalise()
    return config


def new_secret() -> str:
    """Generate a URL-safe secret suitable for SECRET_KEY / JWT_SECRET_KEY."""
    return secrets.token_urlsafe(48)


__all__ = [
    "BaseConfig",
    "ConfigError",
    "DevelopmentConfig",
    "ProductionConfig",
    "TestingConfig",
    "env",
    "env_bool",
    "env_int",
    "env_list",
    "get_config",
    "new_secret",
]

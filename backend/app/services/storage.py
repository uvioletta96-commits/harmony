"""Where uploaded bytes actually go.

Two backends behind one interface, chosen by configuration:

* **local** - a directory on the machine. The default, and it needs no account,
  no key and no network, so a fresh checkout works the moment it is cloned.
* **s3** - any S3-compatible bucket: Cloudflare R2, DigitalOcean Spaces,
  Backblaze B2, MinIO, AWS S3. Turned on with ``S3_BUCKET`` and friends.

The split exists because a disk is the only option that is always available and
a bucket is the only one that survives a machine being replaced. Development
and a single small server want the first; anything with more than one process,
more than one machine, or more media than a disk holds wants the second.

**Keys are identical in both.** ``upload_service`` builds a content-addressed
key, stores it, and stores the same key in the database, so moving between
backends changes configuration and not a row.

Deliberately lazy about the S3 client: ``boto3`` is imported when the S3 backend
is first used, not at startup. A deployment that never selects it should not
have to install it, and a broken/absent S3 configuration should not stop the app
from booting - it should fail on the first write, where the problem is visible.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Any, Protocol

from flask import current_app

#: MIME types that must be served with a content type the browser will not
#: second-guess. Everything is stored as bytes and re-served verbatim, so this
#: matters most for the types a browser will refuse to render from the wrong
#: header.
_CONTENT_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".ogg": "video/ogg",
    ".ogv": "video/ogg",
}


def content_type_for(key: str) -> str:
    suffix = posixpath.splitext(key)[1].lower()
    return _CONTENT_TYPES.get(suffix, "application/octet-stream")


class StorageBackend(Protocol):  # pragma: no cover - interface only
    """The three operations the rest of the application needs."""

    def write(self, key: str, data: bytes) -> str:
        """Store ``data`` under ``key`` and return the URL to serve it from."""
        ...

    def delete(self, key: str) -> bool:
        """Remove ``key``. Returns whether anything was there."""
        ...

    def url(self, key: str) -> str:
        """The URL a browser can fetch to read ``key``."""
        ...

    def describe(self) -> str:
        """One line for the log, so a boot log says where media is going."""
        ...


# ---------------------------------------------------------------------------
# Local disk
# ---------------------------------------------------------------------------


class LocalStorage:
    """A directory on the machine running the process."""

    def __init__(self, root: str, url_prefix: str) -> None:
        self.root = os.path.abspath(root)
        self.url_prefix = url_prefix.rstrip("/") or "/uploads"

    def _path(self, key: str) -> str:
        target = os.path.abspath(os.path.join(self.root, key))
        # Path traversal guard. `key` is built from a hash and a user id, so
        # this should be unreachable, but a guard that can be wrong is not a
        # guard - and this one is the boundary between "user supplied a name"
        # and "process reads a file".
        if not target.startswith(self.root + os.sep) and target != self.root:
            raise ValueError("Ключ хранилища уходит за пределы каталога загрузок.")
        return target

    def write(self, key: str, data: bytes) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = Path(f"{path}.part")
        temporary.write_bytes(data)
        # Rename, so a reader never sees a half-written file - the URL is handed
        # to the browser the moment the post is created.
        temporary.replace(path)
        try:
            os.chmod(path, 0o644)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return self.url(key)

    def delete(self, key: str) -> bool:
        try:
            os.remove(self._path(key))
            return True
        except FileNotFoundError:
            return False

    def url(self, key: str) -> str:
        return f"{self.url_prefix}/{key}"

    def describe(self) -> str:
        return f"local disk at {self.root}"


# ---------------------------------------------------------------------------
# S3-compatible bucket
# ---------------------------------------------------------------------------


class S3Storage:
    """Any S3-compatible bucket.

    ``boto3`` is imported here rather than at module scope so that an install
    that never selects this backend does not need the dependency, and so that a
    misconfigured bucket produces one clear error on first use rather than an
    import-time failure that stops the whole application from booting.
    """

    def __init__(self, app: Any) -> None:
        self.bucket: str = app.config["S3_BUCKET"]
        self.region: str = app.config.get("S3_REGION") or "auto"
        self.endpoint: str | None = app.config.get("S3_ENDPOINT_URL") or None
        self.public_base: str = (app.config.get("S3_PUBLIC_BASE_URL") or "").rstrip("/")
        self.prefix: str = (app.config.get("S3_KEY_PREFIX") or "").strip("/")
        self.presign_ttl: int = int(app.config.get("S3_PRESIGN_TTL_SECONDS", 60 * 60 * 24 * 30))
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:  # pragma: no cover - depends on the install
            raise RuntimeError(
                "S3_BUCKET is set but boto3 is not installed. "
                "Run `pip install boto3`, or unset S3_BUCKET to use the local disk."
            ) from error

        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            aws_access_key_id=current_app.config.get("S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=current_app.config.get("S3_SECRET_ACCESS_KEY") or None,
            # R2 and Spaces both need addressing style addressed by name, and
            # retries are the difference between a transparent blip and a
            # failed upload the reader sees.
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def write(self, key: str, data: bytes) -> str:
        target = self._key(key)
        self._get_client().put_object(
            Bucket=self.bucket,
            Key=target,
            Body=data,
            ContentType=content_type_for(key),
            # The key is a content hash, so the bytes at it never change. This
            # is what lets a CDN in front of the bucket hold them forever.
            CacheControl="public, max-age=31536000, immutable",
        )
        return self.url(key)

    def delete(self, key: str) -> bool:
        self._get_client().delete_object(Bucket=self.bucket, Key=self._key(key))
        return True

    def url(self, key: str) -> str:
        target = self._key(key)
        if self.public_base:
            return f"{self.public_base}/{target}"
        # No public base configured: hand out a time-limited link instead. Note
        # that these expire, so this suits a private bucket for development and
        # not a media URL stored permanently in a post.
        return self._get_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": target},
            ExpiresIn=self.presign_ttl,
        )

    def describe(self) -> str:
        where = self.endpoint or f"s3://{self.bucket}"
        return f"S3 bucket {self.bucket} at {where}"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

#: Key in ``app.extensions``. Flask's own per-application namespace, so the
#: backend is built once per app and cannot leak between two apps in one process
#: - which is what a module-level cache keyed on ``id(app)`` risks, because an
#: id is reused once the object it referred to is collected.
_CACHE_KEY = "harmony_storage"


def get_storage(app: Any | None = None) -> StorageBackend:
    """The backend for this application, built once and reused.

    A client is expensive enough to be worth holding onto - boto3 clients keep a
    connection pool - and the configuration cannot change under a running
    process, so this is cached rather than rebuilt per request.
    """
    target = app or current_app

    existing = target.extensions.get(_CACHE_KEY) if hasattr(target, "extensions") else None
    if existing is not None:
        return existing  # type: ignore[no-any-return]

    backend: StorageBackend
    if target.config.get("S3_BUCKET"):
        backend = S3Storage(target)
    else:
        backend = LocalStorage(
            target.config["UPLOAD_DIR"],
            target.config.get("UPLOAD_URL_PREFIX", "/uploads"),
        )

    if hasattr(target, "extensions"):
        target.extensions[_CACHE_KEY] = backend
    return backend


def reset_storage(app: Any | None = None) -> None:
    """Drop a cached backend so the next call re-reads the configuration.

    For tests, and for anything that mutates config after the app is built.
    """
    target = app or current_app
    if hasattr(target, "extensions"):
        target.extensions.pop(_CACHE_KEY, None)


__all__ = [
    "LocalStorage",
    "S3Storage",
    "content_type_for",
    "get_storage",
    "reset_storage",
]

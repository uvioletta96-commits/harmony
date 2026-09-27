"""Model base classes, mixins and shared column types."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ..extensions import db

NAMESPACE_UUID = uuid.UUID("6f1a3f2c-8b1d-4a1e-9a4a-2d0b7c9e5f31")


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps on every dialect.

    PostgreSQL returns ``timestamptz`` values as aware datetimes, but SQLite
    and MySQL return naive ones. Mixing the two makes ``a - b`` raise
    ``TypeError`` deep inside a view, far from the cause. Normalising at the
    type boundary means the rest of the codebase can assume every datetime it
    reads from the ORM is aware and in UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def timestamp_column(**kwargs: Any):  # type: ignore[no-untyped-def]
    """Create a ``DateTime`` column guaranteed to yield aware UTC values."""
    kwargs.setdefault("nullable", False)
    return mapped_column(UTCDateTime, **kwargs)


def new_uuid() -> str:
    """Generate a v4 UUID as a compact 32-char hex string."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Timezone-aware UTC now, used as the default for every timestamp column."""
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    """Serialise a datetime to a stable ISO-8601 UTC string."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PrimaryKeyMixin:
    """Internal surrogate key.

    The public surface never exposes this value: enumerating sequential ids is
    an information leak and makes authorisation bugs easier to exploit. Every
    API-visible resource is addressed by :class:`PublicIdMixin` instead.
    """

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)


class PublicIdMixin:
    """Opaque, URL-safe public identifier."""

    public_id: Mapped[str] = mapped_column(db.String(32), unique=True, nullable=False, default=new_uuid, index=True)


class TimestampMixin:
    """Creation and modification stamps.

    The default is Python-side (``utcnow``) rather than ``server_default=now()``
    on purpose. ``CURRENT_TIMESTAMP`` renders differently per dialect - SQLite
    writes ``YYYY-MM-DD HH:MM:SS`` with no fractional part, PostgreSQL writes a
    full ``timestamptz`` - and on SQLite a column of DATETIME is compared as
    *text*. Two writers therefore produce two incomparable representations, and
    keyset pagination silently stops filtering anything. One writer, one format.
    """

    created_at: Mapped[datetime] = timestamp_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = timestamp_column(default=utcnow, onupdate=utcnow)


class SerializerMixin:
    """Default ``to_dict`` behaviour: recurse over mapped columns, hide secrets.

    Columns listed in ``__hidden_fields__`` are dropped; ``__computed__`` maps a
    public name to either a plain value or a callable evaluated per request.
    """

    __hidden_fields__: ClassVar[tuple[str, ...]] = ("id",)
    __computed__: ClassVar[dict[str, Any]] = {}

    def to_dict(self, *, include: tuple[str, ...] | None = None, computed: bool = True) -> dict[str, Any]:
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(type(self))
        data: dict[str, Any] = {}
        for column in mapper.columns:
            name = column.key
            if name in self.__hidden_fields__ or name.startswith("_"):
                continue
            value = getattr(self, name, None)
            data[name] = _jsonify(value)
        if computed:
            for key, source in self.__computed__.items():
                try:
                    value = source(self) if callable(source) else getattr(self, source)
                except Exception:  # pragma: no cover - never break a response on a computed field
                    continue
                data[key] = _jsonify(value)
        if include is not None:
            allowed = set(include)
            data = {k: v for k, v in data.items() if k in allowed}
        return data


def _jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, db.Model):
        return getattr(value, "public_id", str(value))
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item) for item in value]
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    return value


def enum_column(python_enum: type, name: str, **kwargs: Any):  # type: ignore[no-untyped-def]
    """Build a native SQLAlchemy Enum column bound to a ``str`` python enum.

    ``values_callable`` keeps the stored values equal to the enum *values* so
    renaming a Python member never silently changes persisted data.
    """
    from enum import Enum

    assert issubclass(python_enum, Enum)
    return mapped_column(
        SAEnum(
            python_enum,
            name=name,
            native_enum=True,
            values_callable=lambda e: [item.value for item in e],
            validate_strings=True,
        ),
        nullable=kwargs.pop("nullable", False),
        **kwargs,
    )


def text_column(**kwargs: Any):  # type: ignore[no-untyped-def]
    kwargs.setdefault("nullable", False)
    return mapped_column(Text, **kwargs)


__all__ = [
    "NAMESPACE_UUID",
    "PrimaryKeyMixin",
    "PublicIdMixin",
    "SerializerMixin",
    "TimestampMixin",
    "UTCDateTime",
    "enum_column",
    "iso",
    "new_uuid",
    "text_column",
    "utcnow",
]

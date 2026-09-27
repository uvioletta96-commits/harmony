"""Cursor (keyset) pagination for feeds and chat, plus offset pagination for
deep, mutable collections like comments.

Keyset pagination is used wherever rows are appended over time: it stays O(log n)
on the index and never skips or duplicates rows when new items arrive mid-scroll,
which offset pagination cannot guarantee.
"""

from __future__ import annotations

import base64
import binascii
import operator
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, and_, or_

from ..extensions import db
from ..utils.responses import ValidationError

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 15


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("Некорректный курсор пагинации.", code="invalid_cursor") from exc


def encode_cursor(payload: dict[str, Any]) -> str:
    import json

    return _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def decode_cursor(cursor: str) -> dict[str, Any]:
    import json

    try:
        data = json.loads(_b64decode(cursor).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValidationError("Некорректный курсор пагинации.", code="invalid_cursor") from exc
    if not isinstance(data, dict):
        raise ValidationError("Некорректный курсор пагинации.", code="invalid_cursor")
    return data


def _parse_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        text = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("Некорректный курсор пагинации.", code="invalid_cursor") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ValidationError("Некорректный курсор пагинации.", code="invalid_cursor")


def clamp_page_size(value: Any, default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE) -> int:
    if value is None or value == "":
        return default
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Параметр limit должен быть целым числом.", code="invalid_limit") from exc
    if size < 1:
        raise ValidationError("Параметр limit должен быть не меньше 1.", code="invalid_limit")
    return min(size, maximum)


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool
    total: int | None = None

    @property
    def count(self) -> int:
        return len(self.items)

    def to_meta(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "count": self.count,
            "has_more": self.has_more,
            "next_cursor": self.next_cursor,
        }
        if self.total is not None:
            meta["total"] = self.total
        if extra:
            meta.update(extra)
        return meta


def keyset_page(
    query: Select,
    *,
    model: type,
    page_size: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    descending: bool = True,
    order_fields: Sequence[str] = ("created_at", "id"),
    serialize: Any = None,
) -> Page:
    """Paginate with a composite keyset cursor.

    ``order_fields`` must be unique per row (the trailing primary key guarantees
    this) and must appear in a matching index for the query to stay fast.
    """
    page_size = clamp_page_size(page_size)
    order = [getattr(model, f).desc() if descending else getattr(model, f).asc() for f in order_fields]
    query = query.order_by(*order)

    if cursor:
        payload = decode_cursor(cursor)
        values = []
        for field in order_fields:
            value = payload.get(field)
            if value is None:
                raise ValidationError("Устаревший курсор пагинации.", code="invalid_cursor")
            if field == "created_at":
                value = _parse_timestamp(value)
            elif field == "id":
                value = int(value)
            values.append(value)
        query = query.where(_keyset_predicate(model, order_fields, values, descending=descending))

    rows = list(db.session.execute(query.limit(page_size + 1)).scalars().all())
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        # Cursor values are JSON, not Python: a raw ``datetime`` would blow up
        # ``json.dumps`` deep inside the encoder and surface as an opaque 500 on
        # the *first* paged request, long after the code that caused it.
        next_cursor = encode_cursor({f: _cursor_value(getattr(last, f)) for f in order_fields})

    items = [serialize(row) if serialize else row for row in rows]
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


def _cursor_value(value: Any) -> Any:
    """Coerce a row attribute into something JSON can carry."""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex"):  # uuid.UUID
        return value.hex
    return value


def _keyset_predicate(
    model: type,
    order_fields: Sequence[str],
    values: Sequence[Any],
    *,
    descending: bool = True,
) -> Any:  # type: ignore[no-untyped-def]
    """Build the "strictly after this position" predicate for a keyset cursor.

    ``(a, b) < (c, d)`` expands to ``a < c OR (a = c AND b < d)``, applied
    right-to-left so the disjunction reads from the most significant column
    down. Written out rather than delegated to ``tuple_()`` because row-value
    comparison binds the boundary as *untyped* parameters - a timestamp cursor
    would be sent as a string and quietly never match a DATETIME column, which
    is the kind of bug that makes pagination loop forever on the first page.

    Expanding it this way also keeps the composite index usable: the leading
    terms are simple inequalities in index order.
    """
    columns = [getattr(model, field) for field in order_fields]
    comparison = operator.lt if descending else operator.gt

    predicate = None
    for index in range(len(columns) - 1, -1, -1):
        # The first (most significant) column has nothing to be equal to; an
        # empty and_() is both a deprecation warning and a future hard error.
        equals = (
            and_(*[col == value for col, value in zip(columns[:index], values[:index], strict=True)]) if index else None
        )
        term = (
            comparison(columns[index], values[index])
            if equals is None
            else and_(equals, comparison(columns[index], values[index]))
        )
        predicate = term if predicate is None else or_(predicate, term)
    return predicate


def offset_page(
    query: Select,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    serialize: Any = None,
) -> Page:
    """Classic page/size pagination for admin tables and comment threads."""
    page_size = clamp_page_size(page_size)
    page = max(1, int(page or 1))
    rows = list(db.session.execute(query.limit(page_size + 1).offset((page - 1) * page_size)).scalars().all())
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = [serialize(row) if serialize else row for row in rows]
    return Page(items=items, next_cursor=None, has_more=has_more)


def keyset_filters(model: type, order_fields: Sequence[str], cursor: str | None, descending: bool = True):
    """Expose the cursor predicate for callers that compose their own query."""
    if not cursor:
        return None
    payload = decode_cursor(cursor)
    values = []
    for field in order_fields:
        value = payload.get(field)
        if value is None:
            raise ValidationError("Устаревший курсор пагинации.", code="invalid_cursor")
        if field == "created_at":
            value = _parse_timestamp(value)
        elif field == "id":
            value = int(value)
        values.append(value)
    return _keyset_predicate(model, order_fields, values, descending=descending)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "clamp_page_size",
    "decode_cursor",
    "encode_cursor",
    "keyset_filters",
    "keyset_page",
    "offset_page",
]

"""Query-string parameters that views read directly.

Every one of these is a number a client controls, and `int("abc")` raises
`ValueError`. In a view that is an unhandled exception, so a bad parameter
returns **500** where the contract says **422** - a crash the client cannot
distinguish from the server being broken, and cannot retry.

Found by sweeping the deployed server with hostile parameters:

    GET /api/v1/users/search?q=lt3000&limit=abc    500   ValueError
    GET /api/v1/users/search?q=lt3000&offset=abc   500   ValueError

`validate_int` already exists and raises `FieldError`; the gap was that nothing
turned that into an HTTP response for *query* parameters. `Schema` does it for
JSON bodies, so this is the same idea for the query string.

Deliberately strict rather than forgiving. `posts/search` ignored `offset=abc`
and returned 200, `users/search` crashed on it: two behaviours for the same
mistake, one of them a 500. Clients deserve one answer, and the honest one is
"that is not a number".
"""

from __future__ import annotations

from typing import Any

from flask import request

from ..security.validators import FieldError, validate_int
from ..utils.responses import ValidationError


def int_arg(
    name: str,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer from the query string, or raise a 422.

    An absent or empty parameter takes ``default``. Anything else that is not an
    integer is rejected - including a float, because ``int("1.5")`` raises rather
    than truncating and silently answering a different question than the one
    asked.
    """
    raw: Any = request.args.get(name)
    try:
        parsed = validate_int(
            raw,
            default=default,
            minimum=minimum,
            maximum=maximum,
            field_name=name,
        )
    except FieldError as error:
        raise ValidationError(message=str(error), fields={name: str(error)}) from error
    # validate_int returns None only when the value is absent and no default was
    # given; `default` is mandatory here, so this cannot happen.
    return default if parsed is None else parsed


def has_control_characters(value: str) -> bool:
    """Whether a path parameter contains a NUL or another C0/C7 control character.

    A NUL is the dangerous one: it cannot appear in a username, so the value can
    only be malformed, and passing it to the driver raises

        ValueError: A string literal cannot contain NUL (0x00) characters.

    which is a 500. Found by sweeping the deployed server:

        GET /api/v1/users/by-username/lt3000%00   500

    Control characters have no business in a handle either, and a tab or newline
    in a query is a log-injection vector, so the check covers the range rather
    than just NUL. DEL (0x7f) is included for the same reason.
    """
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


__all__ = ["has_control_characters", "int_arg"]
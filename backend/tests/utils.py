"""Test-suite helpers."""

from __future__ import annotations

import io
import struct
import zlib
from typing import Any

DEFAULT_PASSWORD = "Str0ng-Pass!23"


def response_json(response) -> Any:
    """Parse a JSON response, asserting the envelope shape first."""
    payload = response.get_json()
    assert payload is not None, f"Expected JSON, got {response.status_code} {response.data[:200]!r}"
    return payload


def assert_ok(response, *, status: int = 200, message: str | None = None) -> Any:
    """Assert a successful envelope and return ``data``.

    ``message`` is appended to the failure output; useful for a request whose
    success is the surprising part of the assertion.
    """
    payload = response_json(response)
    expected = message or "expected a successful envelope"
    assert response.status_code == status, f"{expected}: {response.status_code}: {payload}"
    assert payload["ok"] is True, f"{expected}: {payload}"
    return payload.get("data")


def assert_error(response, *, status: int, code: str | None = None) -> Any:
    """Assert a failure envelope and return ``error``."""
    payload = response_json(response)
    assert response.status_code == status, f"{response.status_code}: {payload}"
    assert payload["ok"] is False, payload
    error = payload["error"]
    assert error.get("message"), "error must carry a user-facing message"
    if code is not None:
        assert error.get("code") == code, f"expected code {code!r}, got {error.get('code')!r}"
    return error


def make_mp4(size: int = 512) -> bytes:
    """Build a byte string that sniffs as an MP4 container.

    Only the ``ftyp`` box the sniffer looks at is written; the rest is filler.
    The upload path stores a video verbatim and never decodes it, so nothing
    downstream would notice the file is not playable - which is precisely why
    the type check is the part that has to be honest.
    """
    head = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
    return head + b"\x00" * max(0, size - len(head))


def make_png(width: int = 8, height: int = 8, colour: tuple[int, int, int] = (180, 170, 150)) -> bytes:
    """Build a valid, tiny PNG in memory — no Pillow needed in tests."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(colour) * width

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_corrupt_png() -> bytes:
    """A PNG whose header is valid but whose ``IDAT`` checksum is not.

    This is what a truncated download or a partial write produces, and it is
    the interesting case: ``IHDR`` parses, so the file gets past identification,
    and Pillow only reaches the broken checksum once it walks the chunk table -
    where it raises the **builtin** ``SyntaxError``. That is neither an
    ``OSError`` nor a ``ValueError``, so a pipeline catching only those answers
    a damaged file with a 500 and an "internal error" message.

    Corrupting the header checksum instead would raise
    ``UnidentifiedImageError`` at ``Image.open()``, which the existing handler
    already covers, and the test would pass against the buggy code.
    """

    def chunk(tag: bytes, data: bytes, crc: int | None = None) -> bytes:
        checksum = zlib.crc32(tag + data) & 0xFFFFFFFF if crc is None else crc
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", checksum)

    raw = b""
    for _ in range(8):
        raw += b"\x00" + bytes((180, 170, 150)) * 8
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw), crc=0xDEADBEEF)
        + chunk(b"IEND", b"")
    )


def make_truncated_png() -> bytes:
    """A valid PNG cut off part-way through ``IDAT``.

    The chunk table never closes, so this fails at identification rather than
    at the decode step. It is the corruption a half-finished upload actually
    produces, and it must be a client error too.
    """
    full = make_png()
    return full[: len(full) // 2]


def multipart_image(name: str = "photo.png", data: bytes | None = None) -> dict[str, Any]:
    return {"file": (io.BytesIO(data or make_png()), name, "image/png")}


def create_post(client, body: str = "Тестовая публикация", **extra: Any):
    """Publish a post through the API and return its payload."""
    payload = {"body": body}
    payload.update(extra)
    return assert_ok(client.post("/api/v1/posts", json=payload), status=201)["post"]


__all__ = [
    "DEFAULT_PASSWORD",
    "assert_error",
    "assert_ok",
    "create_post",
    "make_corrupt_png",
    "make_png",
    "make_truncated_png",
    "multipart_image",
    "response_json",
]

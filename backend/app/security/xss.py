"""XSS defence: neutralise markup on ingest, escape on render.

Content is stored as **plain text**. The API never returns HTML fragments, and
the frontend writes user content with ``textContent``/``value`` rather than
``innerHTML``. That combination removes the entire class of stored-XSS bugs,
which is why markup-stripping here is a belt-and-braces second layer rather
than the primary control.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

try:  # optional: only needed if rich text is ever enabled
    import bleach  # type: ignore
except ImportError:  # pragma: no cover - bleach is a declared dependency
    bleach = None  # type: ignore

#: Allowed when sanitising a limited inline-HTML subset.
ALLOWED_TAGS: list[str] = [
    "p",
    "br",
    "strong",
    "em",
    "u",
    "s",
    "blockquote",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "a",
    "h3",
    "h4",
    "span",
]
ALLOWED_ATTRIBUTES: dict[str, list[str]] = {"a": ["href", "rel", "title"]}
ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto"]

_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_SCRIPT_BLOCK_RE = re.compile(
    r"<(script|style|iframe|object|embed|svg|math|form|meta|link)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SELF_CLOSING_DANGEROUS_RE = re.compile(
    r"<\s*/?\s*(script|style|iframe|object|embed|svg|math|form|meta|link|base)\b[^>]*/?>",
    re.IGNORECASE,
)
DANGEROUS_SCHEMES = ("javascript:", "data:", "vbscript:", "file:", "about:", "blob:")

_HTML_ENTITY_RE = re.compile(r"&(?!(?:[a-zA-Z]{1,31}|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});)")
#: Script-capable schemes are removed wherever they appear, because neither word
#: ever occurs legitimately in prose. ``data:``/``file:`` are only stripped when
#: followed by a MIME type - "file: the report.pdf" is a sentence,
#: "data:text/html,..." is an attack, and a filter unable to tell them apart
#: would corrupt the first to defend against the second.
_SCRIPT_SCHEME_RE = re.compile(r"(?<![\w:/])\b(?:javascript|vbscript|livescript)\s*:", re.IGNORECASE)
_TYPED_SCHEME_RE = re.compile(r"(?<![\w:/])\b(?:data|file|about|blob)\s*:\s*[a-z]+/", re.IGNORECASE)
_URL_RE = re.compile(r"(?<![\w@/])(https?://[^\s<>\"'\)\]]{4,500})", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9._-]{2,31})", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"(?<![\w#])#([\wа-яё]{2,40})", re.IGNORECASE)


def strip_html(value: str) -> str:
    """Remove markup and neutralise obfuscation attempts."""
    if not value:
        return ""
    text = _SCRIPT_BLOCK_RE.sub(" ", value)
    text = _SELF_CLOSING_DANGEROUS_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    # Unescape twice: attackers double-encode to smuggle tags past filters.
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
        text = _TAG_RE.sub("", text)
    # Stored content is plain text and cannot execute, so this is defence in
    # depth: it means the literal string "javascript:" never reaches the
    # database, and so cannot be resurrected by one careless innerHTML in some
    # future view.
    text = _SCRIPT_SCHEME_RE.sub("", text)
    text = _TYPED_SCHEME_RE.sub("", text)
    return text


def sanitize_plain_text(value: str, *, max_length: int | None = None) -> str:
    """Canonical form stored in the database for user-authored text."""
    from .validators import normalise_text

    if not value:
        return ""
    cleaned = strip_html(value)
    cleaned = normalise_text(cleaned)
    cleaned = _HTML_ENTITY_RE.sub("&amp;", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if max_length is not None:
        cleaned = cleaned[:max_length]
    return cleaned


def sanitize_html(value: str) -> str:
    """Sanitise a limited inline-HTML subset. Used only for admin-authored
    system messages; user content never reaches this function."""
    if not value:
        return ""
    if bleach is not None:
        return bleach.clean(
            value,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=ALLOWED_PROTOCOLS,
            strip=True,
        )
    return strip_html(value)


def escape(value: Any) -> str:
    """HTML-escape any value for safe interpolation into a template."""
    return html.escape("" if value is None else str(value), quote=True)


def is_safe_url(url: str) -> bool:
    """True only for http(s)/mailto. Rejects every script-capable scheme."""
    if not url:
        return False
    candidate = url.strip()
    # Strip characters an attacker can use to hide the scheme, e.g. "java\0script:".
    candidate = re.sub(r"[\x00-\x20\x7f]", "", candidate).lower()
    if candidate.startswith(DANGEROUS_SCHEMES):
        return False
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    return parts.scheme in ("http", "https", "mailto")


def safe_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.strip() if is_safe_url(url) else None


def linkify(value: str) -> list[dict[str, Any]]:
    """Segment text into link/mention/hashtag/plain spans for the client.

    Returning typed spans (rather than HTML) keeps rendering in the frontend
    where it can be unit-tested and styled by CSS alone.
    """
    spans: list[dict[str, Any]] = []
    tokens: list[tuple[int, int, dict[str, Any]]] = []

    for match in _URL_RE.finditer(value):
        if is_safe_url(match.group(1)):
            tokens.append((match.start(), match.end(), {"type": "link", "url": match.group(1), "text": match.group(1)}))
    for match in _MENTION_RE.finditer(value):
        if not _overlaps(match.start(), match.end(), tokens):
            tokens.append(
                (
                    match.start(),
                    match.end(),
                    {"type": "mention", "username": match.group(1).lower(), "text": match.group(0)},
                )
            )
    for match in _HASHTAG_RE.finditer(value):
        if not _overlaps(match.start(), match.end(), tokens):
            tokens.append(
                (match.start(), match.end(), {"type": "hashtag", "tag": match.group(1).lower(), "text": match.group(0)})
            )

    tokens.sort(key=lambda item: item[0])
    cursor = 0
    for start, end, payload in tokens:
        if start > cursor:
            spans.append({"type": "text", "text": value[cursor:start]})
        spans.append(payload)
        cursor = end
    if cursor < len(value):
        spans.append({"type": "text", "text": value[cursor:]})
    return spans or [{"type": "text", "text": value}]


def _overlaps(start: int, end: int, tokens: Iterable[tuple[int, int, Any]]) -> bool:
    return any(not (end <= t_start or start >= t_end) for t_start, t_end, _ in tokens)


def detect_payload(value: str) -> str | None:
    """Return a label when the input looks like an injection attempt.

    Not a filter for words — a signal for the spam scorer and the audit log.
    """
    lowered = value.lower()
    if _SELF_CLOSING_DANGEROUS_RE.search(value) or "<script" in lowered or "</script" in lowered:
        return "script_tag"
    if "javascript:" in lowered.replace(" ", "") or "onerror=" in lowered or "onload=" in lowered:
        return "event_handler"
    if "<iframe" in lowered or "<svg" in lowered:
        return "embedded_element"
    if "%3cscript" in lowered or "&#x3c;" in lowered:
        return "encoded_payload"
    return None


__all__ = [
    "ALLOWED_TAGS",
    "DANGEROUS_SCHEMES",
    "detect_payload",
    "escape",
    "is_safe_url",
    "linkify",
    "safe_url",
    "sanitize_html",
    "sanitize_plain_text",
    "strip_html",
]

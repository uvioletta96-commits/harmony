"""Supported interface languages.

The list is deliberately short and every entry in it is fully translated. A
picker offering a hundred and eighty locales where ninety percent of them fall
back to the base language reads as broken - the reader picks their language, and
then half the interface is in the wrong one.

What is offered here is a fixed set of complete translations, the two
directions, and the native name each language calls itself by. A native name is
not a nicety: a reader who does not read the current language still recognises
"Deutsch" or "Ελληνικά" and find theirs among two hundred rows.

The browser's ``Accept-Language`` is matched here too, so a first-time visitor
with a Ukrainian browser gets the Ukrainian interface before they have an
account, and the choice is only stored once they make it themselves.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class Locale(NamedTuple):
    """One fully translated interface language."""

    code: str
    #: The language's name in that language, for the picker.
    native: str
    #: The language's name in English, for search and for anyone who does not
    #: read the script in front of them.
    english: str
    rtl: bool = False


#: Ordered by how widely each is read, because the picker shows the first
#: entries without a search box and the most-used languages should be reachable
#: without typing.
LOCALES: tuple[Locale, ...] = (
    Locale("ru", "Русский", "Russian"),
    Locale("en", "English", "English"),
    Locale("uk", "Українська", "Ukrainian"),
    Locale("be", "Беларуская", "Belarusian"),
    Locale("kk", "Қазақша", "Kazakh"),
    Locale("uz", "Oʻzbekcha", "Uzbek"),
    Locale("ky", "Кыргызча", "Kyrgyz"),
    Locale("tg", "Тоҷикӣ", "Tajik"),
    Locale("az", "Azərbaycan dili", "Azerbaijani"),
    Locale("hy", "Հայերեն", "Armenian"),
    Locale("ka", "ქართული", "Georgian"),
    Locale("de", "Deutsch", "German"),
    Locale("fr", "Français", "French"),
    Locale("es", "Español", "Spanish"),
    Locale("it", "Italiano", "Italian"),
    Locale("pt", "Português", "Portuguese"),
    Locale("nl", "Nederlands", "Dutch"),
    Locale("pl", "Polski", "Polish"),
    Locale("cs", "Čeština", "Czech"),
    Locale("sv", "Svenska", "Swedish"),
    Locale("tr", "Türkçe", "Turkish"),
    Locale("zh", "中文", "Chinese", rtl=False),
    Locale("ja", "日本語", "Japanese"),
    Locale("ko", "한국어", "Korean"),
    Locale("ar", "العربية", "Arabic", rtl=True),
    Locale("he", "עברית", "Hebrew", rtl=True),
    Locale("fa", "فارسی", "Persian", rtl=True),
    Locale("ur", "اردو", "Urdu", rtl=True),
    Locale("hi", "हिन्दी", "Hindi"),
    Locale("id", "Bahasa Indonesia", "Indonesian"),
    Locale("vi", "Tiếng Việt", "Vietnamese"),
)

BY_CODE: dict[str, Locale] = {locale.code: locale for locale in LOCALES}

#: What the interface falls back to for a key a translation is missing. Also the
#: language the source strings are written in.
BASE_LANGUAGE = "ru"

#: Enough of a BCP 47 tag to match a browser preference: the primary subtag is
#: the language, the rest is a region. ``zh-Hans-CN`` and ``zh`` are the same
#: translation here, so the region carries no weight.
DEFAULT_LANGUAGE = BASE_LANGUAGE


def is_supported(code: str | None) -> bool:
    """Whether a catalogue exists for this tag, region included.

    ``de-AT`` is supported, because :func:`normalise` will store it as ``de``
    and that is the catalogue it will be served from. Judging the raw tag
    instead would reject a tag the header parser accepts, so a browser sending
    ``de-AT`` could read the interface in German while the settings page
    refused to save the same language.
    """
    return normalise(code) is not None


def normalise(code: str | None) -> str | None:
    """Reduce a BCP 47 tag to a supported code, or return ``None``."""
    if not code:
        return None
    tag = code.strip().lower().replace("_", "-")
    if tag in BY_CODE:
        return tag
    primary = tag.split("-", 1)[0]
    return primary if primary in BY_CODE else None


def negotiate(accept_language: str | None) -> str:
    """Pick the best supported language from an ``Accept-Language`` header.

    Quality values are honoured, including ``q=0``, which is a refusal: a
    browser sending ``ru;q=0, uk`` has said it does not want Russian, and
    returning it because it appeared first would ignore the only signal the
    header carries.

    Ties are broken by the order in :data:`LOCALES`, so two browsers with the
    same preferences can land on the same interface.
    """
    if not accept_language:
        return DEFAULT_LANGUAGE

    candidates: list[tuple[float, int, str]] = []
    for part in accept_language.split(","):
        tag, _, parameters = part.strip().partition(";")
        code = normalise(tag)
        if code is None:
            continue
        quality = 1.0
        for parameter in parameters.split(";"):
            name, _, value = parameter.partition("=")
            if name.strip() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        rank = LOCALES.index(BY_CODE[code])
        candidates.append((quality, -rank, code))

    if not candidates:
        return DEFAULT_LANGUAGE
    candidates.sort(reverse=True)
    return candidates[0][2]


def catalogue() -> dict[str, Any]:
    """The list the client renders, sorted by native name.

    ``rtl`` travels with the language because the direction is a property of the
    script, not of the page: deriving it client-side from a hard-coded list
    would be a second place for the same fact to be wrong.
    """
    return {
        "base": BASE_LANGUAGE,
        "default": DEFAULT_LANGUAGE,
        "locales": [
            {"code": item.code, "native": item.native, "english": item.english, "rtl": item.rtl}
            for item in sorted(LOCALES, key=lambda item: item.native)
        ],
    }


def direction(code: str | None) -> str:
    """Writing direction for a language tag, defaulting to left-to-right.

    ``BY_CODE.get(key, DEFAULT_LANGUAGE)`` looked right and was not: the default
    is the string ``"ru"``, and a string has no ``.rtl``, so asking for the
    direction of a language we do not have raised instead of answering.
    """
    locale = BY_CODE.get(normalise(code) or "") or BY_CODE[DEFAULT_LANGUAGE]
    return "rtl" if locale.rtl else "ltr"


__all__ = [
    "BASE_LANGUAGE",
    "BY_CODE",
    "DEFAULT_LANGUAGE",
    "LOCALES",
    "Locale",
    "catalogue",
    "direction",
    "is_supported",
    "negotiate",
    "normalise",
]

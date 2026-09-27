"""Declarative, dependency-free input validation.

Every write endpoint validates through a :class:`Schema`, which produces field
level error messages in one pass. Validation runs *before* the handler body, so
no unvalidated value can reach the ORM, the mailer or the filesystem.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..utils.responses import ValidationError

# ---------------------------------------------------------------------------
# Character policy
# ---------------------------------------------------------------------------

#: Characters that break logging, headers or SQL text mode. Rejected outright
#: rather than escaped, because no legitimate username or post needs them.
FORBIDDEN_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$")
USERNAME_RESERVED = frozenset(
    {
        "admin",
        "administrator",
        "root",
        "system",
        "support",
        "help",
        "moderator",
        "mod",
        "harmony",
        "api",
        "www",
        "null",
        "undefined",
        "me",
        "you",
        "about",
        "contact",
        "legal",
        "privacy",
        "terms",
        "settings",
        "login",
        "logout",
        "register",
        "search",
        "post",
        "posts",
        "chat",
        "feed",
        "static",
        "assets",
        "uploads",
    }
)
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s().-]{6,19}$")
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")

#: Homoglyph folding applied before username checks. Users type "П" and "п"
#: interchangeably; we normalise so the namespace stays unambiguous.
CYRILLIC_MAP = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "b",
        "г": "r",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "x",
        "з": "3",
        "и": "u",
        "й": "u",
        "к": "k",
        "л": "n",
        "м": "m",
        "н": "h",
        "о": "o",
        "п": "n",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "ф": "f",
        "х": "x",
        "ц": "u",
        "ч": "4",
        "ш": "w",
        "щ": "w",
        "ъ": "",
        "ы": "b",
        "ь": "",
        "э": "e",
        "ю": "io",
        "я": "r",
    }
)

PASSWORD_MIN = 8
PASSWORD_MAX = 256
#: Glyph pairs that are easy to confuse when read aloud or re-typed. These
#: are reported to the user, never silently rewritten: folding "user1" into
#: "userl" would hand the user a handle they never chose and could collide
#: with an existing account.
CONFUSABLE_PAIRS = (("0", "o"), ("1", "l"), ("1", "i"), ("5", "s"), ("8", "b"))

#: Invisible characters that enable homograph and "Trojan Source" attacks.
#: Written with explicit escapes: a literal hyphen inside this class would
#: start a character range and silently swallow ordinary punctuation.
ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


class FieldError(Exception):
    """Internal signal; collected into a single ValidationError by the Schema."""


def normalise_text(value: str) -> str:
    """NFC-normalise, strip control characters, collapse exotic whitespace."""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = CONTROL_CHARS.sub("", value)
    # Zero-width and bidi-override characters enable homograph and
    # "Trojan Source" style attacks inside otherwise valid text. Written as
    # explicit escapes: a literal ``-`` inside this class would silently start
    # a range and swallow ordinary punctuation.
    value = ZERO_WIDTH_RE.sub("", value)
    return value


def slugify_username(value: str) -> str:
    """Transliterate Cyrillic, lowercase, and nothing else.

    Nothing is silently deleted or folded. Stripping would turn "has space"
    into "hasspace" and "u_s_e_r" into "user"; folding would turn "user1" into
    "userl". Either way the user ends up with a handle they did not choose, and
    two different inputs can collide on a single account. Whatever is not
    transliterated is left in place for the strict format check to reject.
    """
    return normalise_text(value).strip().lower().translate(CYRILLIC_MAP)


def slug_strict(value: str) -> str:
    """Slugify by removal. For internal identifiers, never for user handles."""
    return re.sub(r"[^a-z0-9._-]+", "", slugify_username(value))


def validate_username(value: str, *, min_length: int = 3, max_length: int = 32) -> str:
    raw = normalise_text(str(value)).strip()
    candidate = slugify_username(raw)
    if not candidate:
        raise FieldError("Имя пользователя должно содержать латинские буквы или цифры.")
    if len(candidate) < min_length:
        raise FieldError(f"Имя пользователя должно быть не короче {min_length} символов.")
    if len(candidate) > max_length:
        raise FieldError(f"Имя пользователя должно быть не длиннее {max_length} символов.")
    if not USERNAME_RE.match(candidate):
        raise FieldError("Имя пользователя: допустимы латинские буквы, цифры, точка, дефис и подчёркивание.")
    # Check both the raw lowercase form and the mapped form. "admin" maps to
    # "admln" under the ambiguous-glyph fold, and without this check a
    # reserved name would slip through wearing a different spelling.
    if raw.lower() in USERNAME_RESERVED or candidate in USERNAME_RESERVED:
        raise FieldError("Это имя пользователя зарезервировано. Выберите другое.")
    if "--" in candidate or "__" in candidate or ".." in candidate:
        raise FieldError("Имя пользователя не может содержать повторяющиеся специальные символы.")
    return candidate


def validate_email(value: str) -> str:
    raw = normalise_text(str(value)).strip()
    if not raw:
        raise FieldError("Укажите адрес электронной почты.")
    if len(raw) > 254:
        raise FieldError("Адрес электронной почты слишком длинный.")
    if ".." in raw.split("@")[0] or raw.startswith(".") or raw.startswith("@"):
        raise FieldError("Некорректный адрес электронной почты.")
    if not EMAIL_RE.match(raw):
        raise FieldError("Некорректный адрес электронной почты.")
    local, _, domain = raw.partition("@")
    if len(local) > 64:
        raise FieldError("Некорректный адрес электронной почты.")
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha():
        raise FieldError("Некорректный адрес электронной почты.")
    return raw.lower()


#: Deliberately permissive: length + composition + a blocklist of the passwords
#: that dominate every credential-stuffing list. A strict composition rule
#: ("one uppercase, one symbol") measurably *reduces* entropy by pushing users
#: toward predictable substitutions, so it is intentionally not enforced.
PASSWORD_BLOCKLIST = frozenset(
    {
        "password",
        "пароль",
        "пароли",
        "qwerty",
        "12345",
        "admin",
        "welcome",
        "letmein",
        "iloveyou",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "abc123",
        "111111",
        "000000",
        "passw0rd",
        "superman",
        "trustno1",
        "starwars",
        "sunshine",
        "princess",
        "zaq12wsx",
        "qwertyuiop",
        "asdfghjkl",
    }
)

#: Symbol-for-letter substitutions that carry no entropy in practice. Folding
#: them makes "p@ssw0rd" as detectable as "password". Applied before separators
#: are stripped, because these symbols are letters wearing a costume - unlike
#: digits, which may be a decoration and so are handled one step later.
_SYMBOL_FOLD = str.maketrans({"@": "a", "$": "s", "!": "i", "|": "l", "0": "o"})

#: Digit-for-letter substitutions, applied only after a trailing numeric
#: decoration has been removed.
_LEET_FOLD = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t"})

#: A blocklisted word may be padded with up to this many characters and still be
#: rejected ("password12345", "admin2026", "qwerty!"). Beyond it the user is
#: adding real material, not decorating a known-bad password.
_BLOCKLIST_SLACK = 3

#: Numeric suffix length still treated as decoration rather than as material.
_DIGIT_SUFFIX_SLACK = 6

PASSWORD_REPEAT_RE = re.compile(r"(.)\1{3,}")


def _password_skeleton(raw: str) -> str:
    """Reduce a password to the word a cracker would actually guess.

    Order matters. Separators go first, then a *trailing* run of digits is
    dropped as decoration (``password12345`` -> ``password``), and only then is
    leet-speak unfolded. Folding before stripping digits would turn ``12345``
    into ``lzeas`` and hide exactly the passwords we are looking for.

    Comparing the blocklist against this skeleton - rather than searching for a
    forbidden substring anywhere in the input - is what keeps the filter from
    rejecting ``Reset-Passw0rd!7``: the substring "pass" is present, but the
    password is not the guessable thing.
    """
    alnum = re.sub(r"[^0-9a-z\u0430-\u044f]", "", raw.casefold().translate(_SYMBOL_FOLD))
    trimmed = re.sub(rf"\d{{1,{_DIGIT_SUFFIX_SLACK}}}$", "", alnum)
    if trimmed:
        alnum = trimmed
    return re.sub(r"[^a-z\u0430-\u044f]", "", alnum.translate(_LEET_FOLD))


#: Blocklist entries pre-reduced with the same function as user input, so the
#: two sides of the comparison are always in the same alphabet.
_BLOCKLIST_SKELETONS = frozenset(_password_skeleton(entry) for entry in PASSWORD_BLOCKLIST) - {""}


def is_blocklisted_password(raw: str) -> bool:
    """True when ``raw`` is a decorated form of a well-known password."""
    core = _password_skeleton(raw)
    if not core:
        return False
    for entry in _BLOCKLIST_SKELETONS:
        if core == entry:
            return True
        if core.startswith(entry) and len(core) - len(entry) <= _BLOCKLIST_SLACK:
            return True
    return False


def validate_password(value: str, *, min_length: int = PASSWORD_MIN) -> str:
    raw = str(value)
    if len(raw) < min_length:
        raise FieldError(f"Пароль должен быть не короче {min_length} символов.")
    if len(raw) > PASSWORD_MAX:
        raise FieldError("Пароль слишком длинный.")
    if FORBIDDEN_CHARS.search(raw):
        raise FieldError("Пароль содержит недопустимые символы.")
    if PASSWORD_REPEAT_RE.search(raw):
        raise FieldError("Пароль не должен содержать один символ четыре раза подряд.")
    if is_blocklisted_password(raw):
        raise FieldError("Этот пароль слишком распространён. Выберите другой.")
    classes = sum(
        (
            bool(re.search(r"[a-zа-яё]", raw)),
            bool(re.search(r"[A-ZА-ЯЁ]", raw)),
            bool(re.search(r"\d", raw)),
            bool(re.search(r"[^\w\s]", raw, re.UNICODE)),
        )
    )
    if classes < 2:
        raise FieldError("Пароль должен содержать минимум два типа символов: буквы, цифры или знаки.")
    return raw


def validate_text(
    value: Any,
    *,
    min_length: int = 0,
    max_length: int = 10000,
    required: bool = True,
    field_name: str = "text",
    allow_newlines: bool = True,
) -> str:
    if value is None:
        if required:
            raise FieldError(f"Поле «{field_name}» обязательно.")
        return ""
    raw = str(value)
    if FORBIDDEN_CHARS.search(raw):
        raise FieldError(f"Поле «{field_name}» содержит недопустимые символы.")
    cleaned = normalise_text(raw)
    if not allow_newlines:
        cleaned = cleaned.replace("\n", " ")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if required and not cleaned:
        raise FieldError(f"Поле «{field_name}» не может быть пустым.")
    if len(cleaned) < min_length:
        raise FieldError(f"Поле «{field_name}»: минимум {min_length} символов.")
    if len(cleaned) > max_length:
        raise FieldError(f"Поле «{field_name}»: максимум {max_length} символов.")
    return cleaned


def validate_url(value: Any, *, field_name: str = "Ссылка", allow_empty: bool = True) -> str | None:
    if value in (None, ""):
        if allow_empty:
            return None
        raise FieldError(f"Поле «{field_name}» обязательно.")
    raw = str(value).strip()
    if len(raw) > 512:
        raise FieldError(f"Поле «{field_name}» слишком длинное.")
    if not raw.lower().startswith(("http://", "https://")):
        raise FieldError(f"Поле «{field_name}»: допустимы только адреса http:// и https://.")
    if not URL_RE.match(raw):
        raise FieldError(f"Поле «{field_name}»: некорректный адрес.")
    # Guard against javascript:/data: smuggling through redirect-style links.
    lowered = raw.lower()
    if "javascript:" in lowered or "data:text/html" in lowered or "vbscript:" in lowered:
        raise FieldError(f"Поле «{field_name}»: небезопасная схема.")
    return raw


def _validate_existing_password(value: Any) -> str:
    """Presence + control-character check for an already-valid password."""
    raw = "" if value is None else str(value)
    if not raw:
        raise FieldError("Введите пароль.")
    if len(raw) > PASSWORD_MAX:
        raise FieldError("Пароль слишком длинный.")
    if FORBIDDEN_CHARS.search(raw):
        raise FieldError("Пароль содержит недопустимые символы.")
    return raw


def validate_int(
    value: Any,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    default: int | None = None,
    field_name: str = "значение",
) -> int | None:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FieldError(f"Поле «{field_name}» должно быть целым числом.") from exc
    if minimum is not None and parsed < minimum:
        raise FieldError(f"Поле «{field_name}»: минимум {minimum}.")
    if maximum is not None and parsed > maximum:
        raise FieldError(f"Поле «{field_name}»: максимум {maximum}.")
    return parsed


def validate_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on", "да"}:
        return True
    if lowered in {"0", "false", "no", "off", "нет"}:
        return False
    raise FieldError("Ожидалось логическое значение.")


def validate_enum(
    value: Any, allowed: Iterable[str], *, field_name: str = "значение", default: str | None = None
) -> str:
    options = list(allowed)
    if value in (None, ""):
        if default is not None:
            return default
        raise FieldError(f"Поле «{field_name}» обязательно.")
    candidate = str(value)
    if candidate not in options:
        raise FieldError(f"Поле «{field_name}»: допустимые значения — {', '.join(options)}.")
    return candidate


def validate_public_id(value: Any, *, field_name: str = "идентификатор") -> str:
    raw = str(value or "").strip()
    if not UUID_RE.match(raw):
        raise FieldError(f"Поле «{field_name}»: некорректный идентификатор.")
    return raw


def validate_timestamp(value: Any, *, field_name: str = "дата") -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not TIMESTAMP_RE.match(raw):
        raise FieldError(f"Поле «{field_name}»: ожидается формат ISO-8601.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FieldError(f"Поле «{field_name}»: некорректная дата.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def validate_list(value: Any, *, max_items: int = 20, field_name: str = "список") -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [part for part in value.split(",") if part.strip()]
    elif isinstance(value, Sequence):
        items = list(value)
    else:
        raise FieldError(f"Поле «{field_name}» должно быть списком.")
    if len(items) > max_items:
        raise FieldError(f"Поле «{field_name}»: максимум {max_items} элементов.")
    return items


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class Field:
    name: str
    parser: Callable[[Any], Any]
    required: bool = False
    default: Any = None
    label: str = ""

    def run(self, raw: Any, *, provided: bool) -> Any:
        if not provided and not self.required:
            return self.default() if callable(self.default) else self.default
        return self.parser(raw)


@dataclass
class Schema:
    """Validate a payload, collecting every field error in one pass.

    Returning all errors at once (instead of failing on the first) matters for
    forms: the user should not have to submit six times to discover six
    problems.
    """

    fields: list[Field] = field(default_factory=list)
    _allow_unknown: bool = False
    _unknown_handled: bool = False

    def add(self, name: str, parser: Callable[[Any], Any], **kwargs: Any) -> Schema:
        self.fields.append(Field(name=name, parser=parser, **kwargs))
        return self

    def string(self, name: str, **kwargs: Any) -> Schema:
        label = kwargs.pop("label", name)
        kwargs.pop("default", None)

        def parser(raw: Any) -> str:
            return validate_text(
                raw,
                min_length=kwargs.get("min_length", 0),
                max_length=kwargs.get("max_length", 255),
                required=kwargs.get("required", True),
                field_name=label,
                allow_newlines=kwargs.get("allow_newlines", False),
            )

        return self.add(name, parser, required=kwargs.get("required", True), default=kwargs.get("default"), label=label)

    def email(self, name: str = "email", *, required: bool = True) -> Schema:
        return self.add(name, validate_email, required=required)

    def username(self, name: str = "username", *, required: bool = True, **lengths: int) -> Schema:
        def parser(raw: Any) -> str:
            return validate_username(
                raw, min_length=lengths.get("min_length", 3), max_length=lengths.get("max_length", 32)
            )

        return self.add(name, parser, required=required)

    def password(self, name: str = "password", *, required: bool = True, min_length: int = PASSWORD_MIN) -> Schema:
        return self.add(
            name, lambda raw: validate_password(raw, min_length=min_length), required=required, label="пароль"
        )

    def existing_password(self, name: str = "password", *, required: bool = True) -> Schema:
        """Accept an *already established* password without re-applying policy.

        Login and password-change must never re-run the registration strength
        rules: a legacy account created under older, laxer policy would be
        locked out of its own account, and telling an anonymous caller how
        strong their password is helps an attacker more than it helps anyone.
        Only presence and the absence of control characters are checked.
        """
        return self.add(name, _validate_existing_password, required=required, label="пароль")

    def integer(self, name: str, **kwargs: Any) -> Schema:
        label = kwargs.pop("label", name)
        return self.add(
            name,
            lambda raw: validate_int(raw, field_name=label, **kwargs),
            required=False,
            default=kwargs.get("default"),
            label=label,
        )

    def boolean(self, name: str, *, default: bool | None = None) -> Schema:
        return self.add(name, lambda raw: validate_bool(raw, default=default), required=False, default=default)

    def choice(
        self, name: str, options: Iterable[str], *, default: str | None = None, required: bool = False
    ) -> Schema:
        options = list(options)
        label = name
        return self.add(
            name,
            lambda raw: validate_enum(raw, options, field_name=label, default=default),
            required=required,
            default=default,
        )

    def public_id(self, name: str, *, required: bool = True) -> Schema:
        return self.add(name, validate_public_id, required=required, label="идентификатор")

    def url(self, name: str, *, allow_empty: bool = True) -> Schema:
        return self.add(name, lambda raw: validate_url(raw, allow_empty=allow_empty), required=False, default=None)

    def list(self, name: str, *, max_items: int = 20) -> Schema:
        return self.add(name, lambda raw: validate_list(raw, max_items=max_items), required=False, default=list)

    def raw(self, name: str, *, required: bool = False, default: Any = None) -> Schema:
        return self.add(name, lambda raw: raw, required=required, default=default)

    def ignore_unknown(self) -> Schema:
        self._allow_unknown = True
        return self

    def run(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        data = dict(payload or {})
        errors: dict[str, str] = {}
        result: dict[str, Any] = {}
        seen: set[str] = set()

        for spec in self.fields:
            seen.add(spec.name)
            provided = spec.name in data and data[spec.name] is not None
            try:
                value = spec.run(data.get(spec.name), provided=provided)
            except FieldError as exc:
                errors[spec.name] = str(exc)
            except ValidationError as exc:
                errors[spec.name] = exc.message
            else:
                if value is not None or spec.required:
                    result[spec.name] = value

        if not self._allow_unknown:
            unknown = sorted(set(data) - seen)
            if unknown:
                errors.setdefault("_unknown", f"Неизвестные поля: {', '.join(unknown)}.")

        if errors:
            raise ValidationError(fields=errors)
        return result

    __call__ = run


def require_fields(payload: Mapping[str, Any], fields: Sequence[str]) -> None:
    missing = [name for name in fields if not payload.get(name)]
    if missing:
        raise ValidationError(
            "Не все обязательные поля заполнены.",
            fields=dict.fromkeys(missing, "Обязательное поле."),
        )


__all__ = [
    "Field",
    "FieldError",
    "Schema",
    "normalise_text",
    "require_fields",
    "slug_strict",
    "slugify_username",
    "validate_bool",
    "validate_email",
    "validate_enum",
    "validate_int",
    "validate_list",
    "validate_password",
    "validate_public_id",
    "validate_text",
    "validate_timestamp",
    "validate_url",
    "validate_username",
]

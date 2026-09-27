"""Content moderation with a two-layer design.

Layer 1 is a **local, always-on** analyser: deterministic, instant, no network
hop, no third-party data transfer, and it catches the unambiguous cases (PII
leaks, injection payloads, link farms, known slurs and profanity).

Layer 2 is an optional **external classifier** reached over HTTP. It is the only
layer with real semantic understanding, and it is allowed to *escalate* — but
never to *relax* — a layer-1 block. That asymmetry is deliberate: a third-party
API outage, quota error or misconfiguration must not become a content bypass.

    local  →  allow | review | block
                 ↓
    remote →  may only turn allow into review/block
                 ↓
    final  →  allow | review (held) | block (rejected)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from flask import current_app

from ..utils.logging import get_logger, log_event
from ..utils.responses import ContentPendingError, ModerationError

logger = get_logger("harmony.moderation")

Decision = str  # "allow" | "review" | "block"

#: Sentinel marking "remote provider not built yet".
_UNRESOLVED = "__unresolved__"

# ---------------------------------------------------------------------------
# Local dictionaries
#
# Deliberately small. A broad hard-coded list is trivially defeated by leetspeak
# and by the sheer number of languages involved; the local layer exists to catch
# cheap spam and obvious abuse instantly, while the remote layer handles nuance.
# Production deployments should point MODERATION_DICTIONARY_PATH at a maintained
# word list instead of relying on these seeds.
# ---------------------------------------------------------------------------

SEED_PROFANITY: frozenset[str] = frozenset(
    {
        "бля",
        "блядь",
        "ебан",
        "ебать",
        "ебану",
        "хуй",
        "хуя",
        "хуе",
        "пизд",
        "ебал",
        "ебаный",
        "сука",
        "суки",
        "мудак",
        "мудила",
        "гандон",
        "долбоеб",
        "долбоёб",
        "ублюдок",
        "мразь",
        "сволочь",
        "psych",
        "fuck",
        "shit",
        "bitch",
        "cunt",
        "asshole",
        "bastard",
        "dickhead",
        "whore",
        "slut",
        "faggot",
        "retard",
    }
)
SEED_SLURS: frozenset[str] = frozenset(
    {"нигер", "негр", "хохол", "жид", "кавказец", "nigger", "faggot", "tranny", "kike", "raghead"}
)
SEED_DRUGS: frozenset[str] = frozenset(
    {"героин", "кокаин", "мефедрон", "метамфетамин", "heroin", "cocaine", "methamphetamine", "fentanyl"}
)
SEED_VIOLENCE: frozenset[str] = frozenset({"убью", "застрелю", "избить насмерть", "kill you", "shoot you", "bomb the"})

#: Obfuscations stripped before dictionary lookup.
LEET_MAP = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a", "$": "s", "!": "i", "|": "l"}
)

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    "cvv": re.compile(r"\bcvv[\s:=-]*\d{3,4}\b", re.IGNORECASE),
    "russian_passport": re.compile(r"\b\d{2}\s?\d{2}\s?\d{6}\b"),
    "snils": re.compile(r"\b\d{3}-?\d{3}-?\d{3}-?\d{2}\b"),
    "phone_ru": re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"),
    "email_external": re.compile(r"\b[\w.%-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE),
    # ``\b@\w{4,}`` can never match: ``@`` is not a word character, so there is
    # no word boundary between a space and an ``@``. The lookbehind states the
    # real intent - "not part of an address or another handle".
    "telegram": re.compile(r"(?:\bt\.me\b|\btelegram\.me\b|(?<![\w@])@\w{4,})", re.IGNORECASE),
    "crypto_wallet": re.compile(r"\b(?:bc1[a-z0-9]{25,62}|0x[a-f0-9]{40}|t1[A-Za-z0-9]{33})\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

THREAT_PATTERNS: dict[str, re.Pattern[str]] = {
    "credible_threat": re.compile(
        r"(?:убью|уничтожу|застрелю|найду тебя|kill (?:you|him|her|them)|shoot (?:you|him|her))",
        re.IGNORECASE,
    ),
    "self_harm": re.compile(
        r"(?:покончу с собой|не хочу жить|суицид|kill myself|end my life|self[- ]harm)",
        re.IGNORECASE,
    ),
    "doxxing": re.compile(
        r"(?:вот его адрес|вот её адрес|проживает по адресу|here is (?:his|her) address)", re.IGNORECASE
    ),
    "extremist": re.compile(r"(?:терактом|взорвать|взрывчатк|extremist|terror(?:ist)? attack)", re.IGNORECASE),
    "child_safety": re.compile(r"\b(?:child\s?(?:porn|abuse)|детск\w*\s+порно|расчленён\w+)\b", re.IGNORECASE),
}

SPAM_PATTERNS: dict[str, re.Pattern[str]] = {
    "link_farm": re.compile(r"(?:https?://\S+[\s,]*){5,}"),
    "shill": re.compile(r"(?:пишите в лс|пиши в лс|dm me|вступайте в группу|join my|подписывайтесь)", re.IGNORECASE),
    "giveaway": re.compile(r"(?:забери бесплатно|получи бесплатно|free (?:gift|voucher|money)|выигрыш)", re.IGNORECASE),
    "crypto_promo": re.compile(r"(?:инвестируй|вложи в|крипто|bitcoin|ethereum|памп|tokenomics)", re.IGNORECASE),
    "repetitive": re.compile(r"(?:(\S+)\s+\1){3,}"),
}


@dataclass
class ModerationResult:
    """Aggregated outcome of every layer that ran."""

    decision: Decision = "allow"
    score: float = 0.0
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    provider: str = "local"
    layers: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    degraded: bool = False

    @property
    def blocked(self) -> bool:
        return self.decision == "block"

    @property
    def needs_review(self) -> bool:
        return self.decision == "review"

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def raise_for_decision(self, *, kind: str = "post") -> None:
        """Enforce the decision with the right HTTP status.

        ``review`` is a 202: the write succeeded, the content is simply not
        published yet. That distinction matters for the client, which should
        show "pending review" rather than an error.
        """
        if self.decision == "block":
            reason = self._user_facing_reason()
            log_event(
                logger,
                "WARNING",
                "moderation.blocked",
                kind=kind,
                categories=self.categories,
                score=self.score,
            )
            raise ModerationError(reason, code="content_blocked", detail={"categories": self.categories})
        if self.decision == "review":
            from ..utils.metrics import moderation_decisions_total

            moderation_decisions_total.labels(decision="review").inc()
            raise ContentPendingError(detail={"categories": self.categories})

    def _user_facing_reason(self) -> str:
        if "threat" in self.categories or "violence" in self.categories:
            return "Сообщение содержит угрозы и нарушает правила сообщества."
        if "hate" in self.categories:
            return "Сообщение содержит язык ненависти и нарушает правила сообщества."
        if "pii" in self.categories:
            return "Сообщение содержит личные данные. Удалите их перед публикацией."
        if "spam" in self.categories:
            return "Сообщение выглядит как спам."
        if "sexual" in self.categories:
            return "Сообщение содержит недопустимый контент."
        return "Сообщение нарушает правила сообщества."

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "score": round(self.score, 3),
            "categories": sorted(set(self.categories)),
            "reasons": self.reasons,
            "provider": self.provider,
            "layers": self.layers,
            "degraded": self.degraded,
        }


# ---------------------------------------------------------------------------
# Layer 1: local analyser
# ---------------------------------------------------------------------------


class LocalModerator:
    """Deterministic, offline, zero-latency screening."""

    name = "local"

    def __init__(self) -> None:
        self.profanity = set(SEED_PROFANITY)
        self.slurs = set(SEED_SLURS)
        self.drugs = set(SEED_DRUGS)
        self.violence = set(SEED_VIOLENCE)
        self._load_overrides()

    def _load_overrides(self) -> None:
        """Merge an operator-supplied word list, one lowercase word per line."""
        path = current_app.config.get("MODERATION_LOCAL_DICTIONARY") or ""
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    word = line.strip().lower()
                    if not word or word.startswith("#"):
                        continue
                    self.profanity.add(word)
        except OSError as exc:  # pragma: no cover - misconfiguration
            log_event(logger, "ERROR", "moderation.dictionary_load_failed", path=path, error=exc.__class__.__name__)

    def screen(self, text: str, *, context: str = "post") -> ModerationResult:
        result = ModerationResult(decision="allow", provider=self.name, layers=[self.name])
        if not text or not text.strip():
            return result

        normalised = self._normalise(text)
        tokens = set(re.findall(r"[a-zа-яё0-9']{2,}", normalised))
        # Second pass over the de-spaced form, so "f u c k" and "f.u.c.k" reach
        # the same lexicon entry. Without it, splitting an obvious word into
        # single characters is a free bypass.
        tokens |= set(re.findall(r"[a-zа-яё0-9']{2,}", self._despace(normalised)))

        if tokens & self.slurs:
            self._flag(result, "hate", 1.0, "hate_speech_lexicon")
        if tokens & self.profanity:
            self._flag(result, "profanity", 0.45, "profanity_lexicon")
        if tokens & self.drugs:
            self._flag(result, "drugs", 0.55, "controlled_substance_lexicon")
        if tokens & self.violence:
            self._flag(result, "violence", 0.7, "violence_lexicon")

        for name, pattern in THREAT_PATTERNS.items():
            if pattern.search(text):
                severity = 1.0 if name in {"credible_threat", "child_safety", "self_harm"} else 0.8
                self._flag(result, "threat" if name != "child_safety" else "sexual", severity, name)

        for name, pattern in PII_PATTERNS.items():
            match = pattern.search(text)
            if match:
                # A shared contact handle is normal in a social network, so it
                # escalates to review rather than an outright block - and 0.6 is
                # deliberately just past the review threshold: a handle in a bio
                # is not abuse, but it is exactly the kind of thing a moderator
                # should look at, and 0.5 would have let it pass unread.
                severity = (
                    0.95 if name in {"card_number", "cvv", "private_key", "russian_passport", "snils", "iban"} else 0.6
                )
                self._flag(result, "pii", severity, name, detail=_mask(match.group(0)))

        for name, pattern in SPAM_PATTERNS.items():
            if pattern.search(normalised) or pattern.search(text):
                self._flag(result, "spam", 0.6, name)

        self._apply_thresholds(result)
        return result

    @staticmethod
    def _normalise(text: str) -> str:
        value = text.lower().translate(LEET_MAP)
        # Collapse intra-word separators: "f.u.c.k" and "f*ck" -> "fuck".
        value = re.sub(r"[^\w\s]", "", value)
        value = re.sub(r"\s+", " ", value)
        return value

    @staticmethod
    def _despace(normalised: str) -> str:
        """Rejoin letters the author split apart: ``f u c k`` -> ``fuck``.

        Only runs of *single* characters collapse, so ordinary text is left
        alone: "a b testing" and "1 2 3" never form a word, while four
        consecutive one-letter tokens are not a sentence anyone writes.
        """
        return re.sub(
            r"\b(?:[^\W\d_]\s+){2,}[^\W\d_]\b",
            lambda match: match.group(0).replace(" ", ""),
            normalised,
        )

    def _flag(self, result: ModerationResult, category: str, severity: float, reason: str, detail: str = "") -> None:
        result.categories.append(category)
        result.reasons.append(reason)
        result.score = max(result.score, severity)
        if detail:
            result.reasons.append(f"{reason}({detail})")

    def _apply_thresholds(self, result: ModerationResult) -> None:
        block_at = float(current_app.config.get("MODERATION_BLOCK_THRESHOLD", 0.85))
        review_at = float(current_app.config.get("MODERATION_REVIEW_THRESHOLD", 0.55))
        if result.score >= block_at or {"threat", "hate", "sexual"} & set(result.categories):
            result.decision = "block"
        elif result.score >= review_at:
            result.decision = "review"
        else:
            result.decision = "allow"


def _mask(value: str) -> str:
    """Show enough of a leaked value for a moderator, but never all of it."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


# ---------------------------------------------------------------------------
# Layer 2: external classifiers
# ---------------------------------------------------------------------------


class RemoteModerator(Protocol):
    name: str

    def screen(self, text: str, *, context: str = "post") -> ModerationResult: ...


class OpenAICompatibleModerator:
    """Works with OpenAI's ``/v1/moderations`` and any API that mirrors it."""

    name = "openai"

    def __init__(self, url: str, api_key: str, model: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def screen(self, text: str, *, context: str = "post") -> ModerationResult:
        result = ModerationResult(provider=self.name, layers=[self.name])
        payload = {"model": self.model, "input": text[:20000]}
        try:
            import requests

            response = requests.post(
                self.url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log_event(
                logger,
                "ERROR",
                "moderation.remote_unavailable",
                provider=self.name,
                error=exc.__class__.__name__,
            )
            return self._degraded(exc)

        results = data.get("results") or [{}]
        first = results[0] if results else {}
        flagged = bool(first.get("flagged"))
        categories = {name: score for name, score in (first.get("categories") or {}).items() if score > 0.0}
        block_at = float(current_app.config.get("MODERATION_BLOCK_THRESHOLD", 0.85))
        review_at = float(current_app.config.get("MODERATION_REVIEW_THRESHOLD", 0.55))

        peak = max(categories.values(), default=0.0)
        if flagged and peak >= block_at:
            result.decision = "block"
        elif flagged or peak >= review_at:
            result.decision = "review"
        else:
            result.decision = "allow"
        result.score = peak
        result.categories = [name.split("/")[-1] for name in categories]
        result.reasons = [f"{name}={score}" for name, score in sorted(categories.items(), key=lambda kv: -kv[1])]
        result.raw = data
        return result

    def _degraded(self, exc: Exception) -> ModerationResult:
        """Outage handling. Fail-closed puts the whole platform in review mode;
        fail-open trusts layer 1 alone. Neither is silently the default in prod."""
        fail_closed = bool(current_app.config.get("MODERATION_FAIL_CLOSED"))
        log_event(
            logger,
            "WARNING",
            "moderation.fail_mode",
            provider=self.name,
            mode="closed" if fail_closed else "open",
            error=exc.__class__.__name__,
        )
        return ModerationResult(
            decision="review" if fail_closed else "allow",
            provider=self.name,
            layers=[self.name],
            degraded=True,
            categories=["provider_unavailable"],
        )


class CustomHttpModerator:
    """Adapter for a bespoke moderation endpoint.

    Expected contract — documented in ``docs/MODERATION.md`` — a POST to
    ``MODERATION_API_URL`` with ``{"text": ..., "context": ...}`` returning
    ``{"decision": "allow|review|block", "score": 0.0-1.0, "categories": [...]}``.
    """

    name = "custom"

    def __init__(self, url: str, api_key: str, timeout: float) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def screen(self, text: str, *, context: str = "post") -> ModerationResult:
        result = ModerationResult(provider=self.name, layers=[self.name])
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            import requests

            response = requests.post(
                self.url,
                json={"text": text[:20000], "context": context},
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log_event(logger, "ERROR", "moderation.custom_unavailable", error=exc.__class__.__name__)
            fail_closed = bool(current_app.config.get("MODERATION_FAIL_CLOSED"))
            return ModerationResult(
                decision="review" if fail_closed else "allow",
                provider=self.name,
                layers=[self.name],
                degraded=True,
                categories=["provider_unavailable"],
            )

        decision = str(data.get("decision", "allow")).lower()
        if decision not in {"allow", "review", "block"}:
            decision = "review" if current_app.config.get("MODERATION_FAIL_CLOSED") else "allow"
        result.decision = decision
        result.score = float(data.get("score", 0.0) or 0.0)
        result.categories = [str(item) for item in (data.get("categories") or [])]
        result.reasons = [str(item) for item in (data.get("reasons") or [])]
        result.raw = data
        return result


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ModerationEngine:
    """Runs layer 1, then layer 2, merging with escalation-only semantics."""

    def __init__(self) -> None:
        self.local = LocalModerator()
        self._remote: RemoteModerator | str | None = _UNRESOLVED

    def _remote_moderator(self) -> RemoteModerator | None:
        # Resolved lazily: building it reads current_app config, which is not
        # available at import time.
        if isinstance(self._remote, str):
            self._remote = self._build_remote()
        return self._remote  # type: ignore[return-value]

    def _build_remote(self) -> RemoteModerator | None:
        provider = (current_app.config.get("MODERATION_PROVIDER") or "local").lower()
        url = current_app.config.get("MODERATION_API_URL") or ""
        key = current_app.config.get("MODERATION_API_KEY") or ""
        timeout = float(current_app.config.get("MODERATION_TIMEOUT", 4))
        if provider == "openai" and url and key:
            self._remote = OpenAICompatibleModerator(
                url, key, current_app.config.get("MODERATION_MODEL", "omni-moderation-latest"), timeout
            )
        elif provider == "custom" and url:
            self._remote = CustomHttpModerator(url, key, timeout)
        else:
            self._remote = None
        return self._remote

    def screen(self, text: str, *, context: str = "post", use_remote: bool = True) -> ModerationResult:
        if not current_app.config.get("MODERATION_ENABLED", True):
            return ModerationResult(decision="allow", provider="disabled", layers=["disabled"])

        local_result = self.local.screen(text, context=context)
        if not use_remote or local_result.decision == "block":
            return local_result

        remote = self._remote_moderator()
        if remote is None:
            return local_result

        try:
            remote_result = remote.screen(text, context=context)
        except Exception as exc:  # defensive: a provider must never 500 a write
            log_event(logger, "ERROR", "moderation.provider_raised", error=exc.__class__.__name__)
            return local_result

        # Escalation-only merge.
        severity_order = {"allow": 0, "review": 1, "block": 2}
        decision = local_result.decision
        if severity_order[remote_result.decision] > severity_order[decision]:
            decision = remote_result.decision
        return ModerationResult(
            decision=decision,  # type: ignore[arg-type]
            score=max(local_result.score, remote_result.score),
            categories=local_result.categories + remote_result.categories,
            reasons=[f"local:{reason}" for reason in local_result.reasons]
            + [f"{remote_result.provider}:{reason}" for reason in remote_result.reasons],
            provider=f"{local_result.provider}+{remote_result.provider}",
            layers=[local_result.provider, remote_result.provider],
            raw=remote_result.raw,
            degraded=remote_result.degraded,
        )

    def screen_image_metadata(self, *, width: int, height: int, byte_size: int, mime_type: str) -> ModerationResult:
        """Cheap structural checks for uploads.

        Full image classification is a separate (and much heavier) service;
        this gate rejects decompression-bomb geometry and implausible ratios
        before the bytes are ever written to disk.
        """
        result = ModerationResult(provider="local", layers=["local", "image"])
        if width <= 0 or height <= 0:
            result.decision, result.categories, result.reasons = "block", ["invalid_image"], ["zero_dimension"]
        elif width * height > 80_000_000:
            result.decision, result.categories, result.reasons = "block", ["decompression_bomb"], ["too_many_pixels"]
        elif min(width, height) < 16:
            result.decision, result.categories, result.reasons = "review", ["low_resolution"], ["tiny_image"]
        elif max(width, height) / max(1, min(width, height)) > 6:
            result.categories.append("extreme_aspect_ratio")
            result.score = max(result.score, 0.4)
            result.decision = "review"
        if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            result.decision, result.categories = "block", ["unsupported_format"]
        if byte_size <= 0:
            result.decision = "block"
        return result


_engine: ModerationEngine | None = None


def get_engine() -> ModerationEngine:
    global _engine
    if _engine is None:
        _engine = ModerationEngine()
    return _engine


def reset_engine() -> None:
    """Test hook: forces dictionaries and the remote client to be re-read."""
    global _engine
    _engine = None


def screen(text: str, *, context: str = "post", use_remote: bool = True) -> ModerationResult:
    return get_engine().screen(text, context=context, use_remote=use_remote)


def moderate_payload(fields: dict[str, str], *, context: str = "post") -> ModerationResult:
    """Screen a multi-field payload by concatenating its parts."""
    return screen("\n".join(value for value in fields.values() if value), context=context)


__all__ = [
    "CustomHttpModerator",
    "Decision",
    "LocalModerator",
    "ModerationEngine",
    "ModerationResult",
    "OpenAICompatibleModerator",
    "get_engine",
    "moderate_payload",
    "reset_engine",
    "screen",
]

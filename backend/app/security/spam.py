"""Anti-spam: honeypots, duplicate detection, burst detection, risk scoring.

The design is a *scoring* system rather than a blocklist. A single signal (say,
a link in a new account's first post) is weak evidence; several weak signals
combined are strong. Signals decay over time, so a legitimate user who trips
something once is not punished days later.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from flask import current_app, has_request_context, request

from ..extensions import db
from ..models.moderation import AbuseEvent, SpamSignal
from ..models.post import Post, PostStatus
from ..utils.crypto import hash_email, keyed_hash
from ..utils.logging import get_logger, log_event
from ..utils.responses import RateLimitError

logger = get_logger("harmony.spam")

#: Weight per signal kind. Tuned so that no single low-signal event can block
#: a user, but three or more of them can.
SIGNAL_WEIGHTS: dict[str, float] = {
    "honeypot_filled": 5.0,  # a human never sees a hidden field
    "solve_too_fast": 2.0,
    "xss_payload": 4.0,
    "flood_links": 1.5,
    "duplicate_content": 2.0,
    "burst_publish": 1.5,
    "new_account_mass_activity": 1.0,
    "impossible_travel": 1.0,
    "inactive_account": 0.8,
    "risk_ip": 1.2,
    "email_disposable": 0.6,
    "username_spam": 1.0,
    "many_reports_against": 1.5,
    "rapid_auth_failure": 0.7,
}

#: Score at which the account is blocked outright rather than merely throttled.
BLOCK_THRESHOLD = 6.0
#: Score at which write actions are throttled.
THROTTLE_THRESHOLD = 2.5
#: Signals older than this stop counting toward the score.
DECAY_HOURS = 24.0

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
SHORTENER_RE = re.compile(
    r"(?:bit\.ly|t\.me|bit\.do|clck\.ru|is\.gd|goo\.gl|ow\.ly|cutt\.ly|tinyurl|shorturl|qr\.ae|vk\.cc)", re.IGNORECASE
)
REPEAT_CHAR_RE = re.compile(r"(.)\1{4,}")
#: Words that appear in a username only when the account exists to advertise.
#: Combined with a numeric tail (see :func:`is_spammy_username`) rather than
#: matched alone - "best", "top" and "vip" are ordinary enough on their own.
PROMO_WORD_RE = re.compile(
    r"(?:free|bonus|gift|promo|discount|sale|coupon|deal|cheap|cash|money|win|"
    r"crypto|bitcoin|invest|loan|follow|likes|subs|seo|promo)",
    re.IGNORECASE,
)
DIGIT_TAIL_RE = re.compile(r"\d{4,}$")
UPPERCASE_RE = re.compile(r"[A-ZА-ЯЁ]{8,}")
CONTACT_RE = re.compile(
    r"(?:\+?\d[\s\-().]{7,}\d)|(?:[\w.%-]+@[\w.-]+\.\w{2,})|(?:t\.me/|wa\.me|whatsapp|viber)",
    re.IGNORECASE,
)
DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "throwawaymail.com",
        "yopmail.com",
        "trashmail.com",
        "getnada.com",
        "sharklasers.com",
        "temp-mail.org",
        "dispostable.com",
        "maildrop.cc",
    }
)
URL_FARM_HOSTS = frozenset({"bit.ly", "t.me", "vk.cc", "qr.ae", "tinyurl.com", "clck.ru", "cutt.ly", "is.gd"})


@dataclass
class SpamAssessment:
    """Outcome of an anti-spam evaluation."""

    allowed: bool = True
    score: float = 0.0
    signals: list[str] = field(default_factory=list)
    reason: str = ""
    block: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "score": round(self.score, 2),
            "signals": self.signals,
            "reason": self.reason,
            "block": self.block,
        }


# ---------------------------------------------------------------------------
# Content heuristics
# ---------------------------------------------------------------------------


def analyse_content(
    body: str,
    *,
    link_count: int | None = None,
    account_age_days: float | None = None,
    author_id: int | None = None,
) -> list[tuple[str, float]]:
    """Return ``(signal, weight)`` pairs implied by the content itself."""
    from .xss import detect_payload

    signals: list[tuple[str, float]] = []
    if not body:
        return signals

    payload = detect_payload(body)
    if payload:
        signals.append(("xss_payload", SIGNAL_WEIGHTS["xss_payload"]))

    links = len(URL_RE.findall(body))
    if link_count is not None:
        links = max(links, link_count)
    max_links = int(current_app.config.get("SPAM_MAX_LINKS", 3))
    if links > max_links:
        signals.append(("flood_links", SIGNAL_WEIGHTS["flood_links"]))
    if any(SHORTENER_RE.search(m.group(0)) for m in URL_RE.finditer(body)):
        signals.append(("flood_links", SIGNAL_WEIGHTS["flood_links"] * 0.5))
    if CONTACT_RE.search(body):
        signals.append(("flood_links", SIGNAL_WEIGHTS["flood_links"] * 0.4))
    if len(body.strip()) < 8 and links:
        signals.append(("flood_links", 0.3))
    if REPEAT_CHAR_RE.search(body) or UPPERCASE_RE.search(body):
        signals.append(("flood_links", 0.4))
    if account_age_days is not None and account_age_days < 1 and links >= 1:
        signals.append(("inactive_account", SIGNAL_WEIGHTS["inactive_account"]))
    return signals


def is_duplicate_content(
    body: str,
    author_id: int,
    *,
    window_seconds: int | None = None,
) -> bool:
    """True when the same author posted near-identical text moments ago."""
    window = window_seconds or int(current_app.config.get("SPAM_MAX_DUPLICATE_WINDOW", 300))
    fingerprint = _content_fingerprint(body)
    since = datetime.now(UTC) - timedelta(seconds=window)
    count = (
        db.session.query(db.func.count(Post.id))
        .filter(
            Post.author_id == author_id,
            Post.created_at >= since,
            Post.status.in_([PostStatus.PUBLISHED.value, PostStatus.UNDER_REVIEW.value]),
        )
        .scalar()
    )
    if not count:
        return False
    for row in db.session.query(Post.body).filter(Post.author_id == author_id, Post.created_at >= since).all():
        if _content_fingerprint(row[0]) == fingerprint:
            return True
    return False


def _content_fingerprint(body: str) -> str:
    import re

    normalized = re.sub(r"\s+", " ", (body or "").strip().lower())
    return keyed_hash(normalized, length=24)


def count_recent_posts(author_id: int, *, minutes: int = 10) -> int:
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    return (
        db.session.query(db.func.count(Post.id)).filter(Post.author_id == author_id, Post.created_at >= since).scalar()
        or 0
    )


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------


def _scope_rows(scope: str, identifier: str, *, since: datetime) -> Iterable[AbuseEvent]:
    return (
        db.session.query(AbuseEvent)
        .filter(AbuseEvent.subject_type == scope, AbuseEvent.subject_id == identifier, AbuseEvent.created_at >= since)
        .all()
    )


def load_signal(scope: str, identifier: str) -> SpamSignal:
    """Fetch or create the rolling signal row for a scope."""
    signal = db.session.query(SpamSignal).filter(SpamSignal.scope == scope, SpamSignal.identifier == identifier).first()
    if signal is None:
        signal = SpamSignal(scope=scope, identifier=identifier, score=0.0, signals={})
        db.session.add(signal)
    return signal


def score_scope(scope: str, identifier: str) -> SpamAssessment:
    """Compute the decayed risk score for a scope/subject pair."""
    since = datetime.now(UTC) - timedelta(hours=DECAY_HOURS)
    total = 0.0
    signals: list[str] = []
    detail: dict[str, float] = {}
    for event in _scope_rows(scope, identifier, since=since):
        age_hours = max(0.0, (datetime.now(UTC) - _aware(event.created_at)).total_seconds() / 3600.0)
        decay = max(0.0, 1.0 - (age_hours / DECAY_HOURS))
        weight = SIGNAL_WEIGHTS.get(event.kind, 1.0) * decay
        total += weight
        signals.append(event.kind)
        detail[event.kind] = round(detail.get(event.kind, 0.0) + weight, 3)

    signal_row = load_signal(scope, identifier)
    signal_row.score = round(total, 3)
    signal_row.signals = detail
    signal_row.blocked = total >= BLOCK_THRESHOLD
    if signals:
        signal_row.last_signal_at = datetime.now(UTC)

    assessment = SpamAssessment(
        allowed=total < BLOCK_THRESHOLD,
        score=total,
        signals=sorted(set(signals)),
        block=total >= BLOCK_THRESHOLD,
    )
    if assessment.block:
        assessment.reason = "Обнаружена подозрительная активность. Попробуйте позже."
    return assessment


def record_signal(
    kind: str,
    *,
    scope: str = "ip",
    identifier: str | None = None,
    weight: float | None = None,
    detail: dict[str, Any] | None = None,
) -> SpamAssessment:
    """Persist one signal and return the recomputed assessment."""
    from .rate_limit import client_ip

    identifier = identifier or keyed_hash(client_ip(), length=64)
    event = AbuseEvent(
        subject_type=scope,
        subject_id=identifier,
        ip_hash=identifier if scope == "ip" else "",
        kind=kind,
        weight=weight if weight is not None else SIGNAL_WEIGHTS.get(kind, 1.0),
        detail=detail or {},
    )
    db.session.add(event)
    db.session.flush()
    assessment = score_scope(scope, identifier)
    log_event(
        logger,
        "INFO",
        "spam.signal",
        signal=kind,
        scope=scope,
        score=assessment.score,
        blocked=assessment.block,
    )
    return assessment


def is_blocked_email(email: str) -> bool:
    domain = email.rpartition("@")[2].lower()
    return domain in DISPOSABLE_DOMAINS


def is_spammy_username(username: str) -> list[str]:
    """Flag usernames that exist purely to be advertised."""
    reasons: list[str] = []
    if SHORTENER_RE.search(username) or URL_FARM_HOSTS.intersection(username.split(".")):
        reasons.append("url_in_username")
    if CONTACT_RE.search(username):
        reasons.append("contact_in_username")
    digits = sum(character.isdigit() for character in username)
    if digits > len(username) * 0.5 and digits > 4:
        reasons.append("mostly_digits")
    if REPEAT_CHAR_RE.search(username):
        reasons.append("repeated_chars")
    # "free-money-12345" and "vip2026" are account names built to be found by a
    # search, not by people who know the person. The promotional word alone is
    # too weak a signal to act on - hence the required numeric tail.
    if PROMO_WORD_RE.search(username) and DIGIT_TAIL_RE.search(username):
        reasons.append("promotional_name")
    return reasons


def check_ip_reputation(ip_hash: str) -> SpamAssessment:
    return score_scope("ip", ip_hash)


def _honeypot_value() -> str | None:
    """Read the hidden field a real browser never fills in.

    Returns ``None`` outside a request: Celery re-scores queued content, and a
    background job has no form to inspect. Absence of a honeypot is the correct
    reading there, not an error.
    """
    if not has_request_context():
        return None
    if request.headers.get("X-HP"):
        return request.headers["X-HP"]
    if not request.form:
        return None
    return request.form.get(current_app.config.get("SPAM_HONEYPOT_FIELD", "harmony_hp")) or None


# ---------------------------------------------------------------------------
# Request-scoped evaluation
# ---------------------------------------------------------------------------


def evaluate_request(
    *,
    user=None,
    body: str = "",
    is_registration: bool = False,
    email: str | None = None,
    username: str | None = None,
) -> SpamAssessment:
    """Full pre-write check combining reputation and content heuristics."""
    from .rate_limit import client_ip

    signals: list[tuple[str, float]] = []
    ip_hash = keyed_hash(client_ip(), length=64)

    if is_registration and email:
        if is_blocked_email(email):
            signals.append(("email_disposable", SIGNAL_WEIGHTS["email_disposable"]))
        email_hash = hash_email(email)
        prior = score_scope("email", email_hash)
        if prior.block:
            signals.extend((s, SIGNAL_WEIGHTS.get(s, 0.5)) for s in prior.signals)
        signals.append(("email_disposable", prior.score * 0.5))

    if is_registration and username and is_spammy_username(username):
        signals.append(("username_spam", SIGNAL_WEIGHTS["username_spam"]))

    honeypot = _honeypot_value()
    if honeypot:
        signals.append(("honeypot_filled", SIGNAL_WEIGHTS["honeypot_filled"]))

    if body and user is not None:
        age_days = (datetime.now(UTC) - _aware(user.created_at)).total_seconds() / 86400.0 if user.created_at else 999
        signals.extend(analyse_content(body, account_age_days=age_days, author_id=user.id))
        if count_recent_posts(user.id) > 8:
            signals.append(("burst_publish", SIGNAL_WEIGHTS["burst_publish"]))
        if is_duplicate_content(body, user.id):
            signals.append(("duplicate_content", SIGNAL_WEIGHTS["duplicate_content"]))

    if user is not None and getattr(user, "reports_against_count", 0) >= 3:
        signals.append(("many_reports_against", SIGNAL_WEIGHTS["many_reports_against"]))

    ip_assessment = check_ip_reputation(ip_hash)
    if ip_assessment.block:
        signals.append(("risk_ip", SIGNAL_WEIGHTS["risk_ip"]))

    total = sum(weight for _, weight in signals)
    # Reputation from other subsystems is additive but capped, so a large
    # volume of trivial signals cannot dominate a single strong one.
    total += min(ip_assessment.score, 3.0) * 0.5

    assessment = SpamAssessment(
        allowed=total < BLOCK_THRESHOLD,
        score=total,
        signals=[name for name, _ in signals],
        block=total >= BLOCK_THRESHOLD,
    )
    if not assessment.allowed:
        assessment.reason = "Действие заблокировано системой защиты от спама."
    return assessment


def enforce_clean(assessment: SpamAssessment) -> None:
    """Translate a blocked assessment into an HTTP error."""
    if assessment.block:
        record_signal("bulk_publish", scope="system", identifier="enforcement", weight=0.0)
        raise RateLimitError(
            assessment.reason or "Действие временно заблокировано.",
            retry_after=900,
            code="spam_blocked",
        )


def guard_write(body: str, user=None) -> SpamAssessment:
    """Convenience wrapper used by post/comment/message services."""
    assessment = evaluate_request(user=user, body=body)
    if not assessment.allowed:
        enforce_clean(assessment)
    return assessment


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "BLOCK_THRESHOLD",
    "DECAY_HOURS",
    "SIGNAL_WEIGHTS",
    "THROTTLE_THRESHOLD",
    "SpamAssessment",
    "analyse_content",
    "check_ip_reputation",
    "count_recent_posts",
    "enforce_clean",
    "evaluate_request",
    "guard_write",
    "is_blocked_email",
    "is_duplicate_content",
    "is_spammy_username",
    "load_signal",
    "record_signal",
    "score_scope",
]

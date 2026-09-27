"""Transactional email, delivered asynchronously.

Every message is rendered from a template and logged to ``email_deliveries``
with metadata only — never the body, which may contain a password-reset token.
Delivery failures are retried with exponential backoff and then recorded, so a
broken SMTP server degrades to "verification emails are late" rather than
"accounts cannot be created".
"""

from __future__ import annotations

import smtplib
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from flask import current_app, render_template

from ..extensions import celery_app, db
from ..models.base import utcnow
from ..models.system import EmailDelivery
from ..models.user import User
from ..utils.logging import get_logger, log_event
from ..utils.responses import ServiceUnavailableError

logger = get_logger("harmony.tasks.email")

#: Exponential backoff: 30s, 2m, 8m.
RETRY_BACKOFF = (30, 120, 480)


def _render(template: str, **context: Any) -> tuple[str, str, str]:
    """Return ``(subject, html, text)`` for a template set."""
    subject = render_template(f"email/{template}_subject.txt", **context).strip()
    html = render_template(f"email/{template}.html", **context)
    text = render_template(f"email/{template}.txt", **context)
    return subject, html, text


def _delivery_row(user_id: int | None, to_address: str, template: str, subject: str) -> EmailDelivery:
    row = EmailDelivery(
        user_id=user_id,
        to_address=to_address,
        template=template,
        subject=subject[:200],
        status="queued",
    )
    db.session.add(row)
    return row


def _send_smtp(message: EmailMessage) -> None:
    config = current_app.config
    if not config.get("MAIL_ENABLED"):
        raise ServiceUnavailableError("Отправка писем отключена.", code="mail_disabled")
    if config.get("MAIL_SUPPRESS_SEND"):
        return

    server = config.get("MAIL_SERVER")
    if not server:
        # Console backend: log the message body so local flows are testable.
        logger.info("[mail:console] to=%s subject=%s", message["To"], message["Subject"])
        return

    port = int(config.get("MAIL_PORT", 587))
    timeout = int(config.get("MAIL_TIMEOUT", 10))
    try:
        if config.get("MAIL_USE_SSL"):
            client: smtplib.SMTP = smtplib.SMTP_SSL(server, port, timeout=timeout)
        else:
            client = smtplib.SMTP(server, port, timeout=timeout)
        with client:
            client.ehlo()
            if config.get("MAIL_USE_TLS") and not config.get("MAIL_USE_SSL"):
                client.starttls()
                client.ehlo()
            username = config.get("MAIL_USERNAME")
            if username:
                client.login(username, config.get("MAIL_PASSWORD") or "")
            client.send_message(message)
    except smtplib.SMTPException as exc:
        log_event(logger, "ERROR", "mail.smtp_failed", error=exc.__class__.__name__)
        raise


def _deliver(
    *,
    user_id: int | None,
    to_address: str,
    template: str,
    context: dict[str, Any],
) -> bool:
    site_url = current_app.config.get("SITE_URL", "")
    subject, html, text = _render(
        template, site_url=site_url, app_name=current_app.config.get("APP_NAME", "Гармония"), **context
    )

    row = _delivery_row(user_id, to_address, template, subject)
    db.session.commit()

    message = EmailMessage()
    message["Subject"] = subject
    sender_name = str(Header(current_app.config.get("MAIL_FROM_NAME", "Гармония"), "utf-8"))
    message["From"] = formataddr((sender_name, current_app.config["MAIL_FROM"]))
    message["To"] = to_address
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        _send_smtp(message)
    except Exception as exc:
        row.status = "failed"
        row.error = f"{exc.__class__.__name__}: {exc}"[:512]
        row.attempts += 1
        db.session.commit()
        log_event(logger, "ERROR", "mail.delivery_failed", template=template, error=exc.__class__.__name__)
        return False

    row.status = "sent"
    row.sent_at = utcnow()
    row.attempts += 1
    row.provider = current_app.config.get("MAIL_BACKEND", "smtp")
    db.session.commit()
    log_event(logger, "INFO", "mail.sent", template=template, user_id=user_id)
    return True


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@celery_app.task(name="email.verify", bind=True, max_retries=3)
def send_verification_email(self, user_id: int, token: str) -> bool:  # type: ignore[no-untyped-def]
    user = db.session.get(User, user_id)
    if user is None or user.email_verified:
        return False
    ok = _deliver(
        user_id=user.id,
        to_address=user.email,
        template="verify",
        context={"user": user, "token": token, "link": f"{current_app.config['SITE_URL']}/verify?token={token}"},
    )
    if not ok:
        raise self.retry(countdown=RETRY_BACKOFF[min(self.request.retries, 2)])  # type: ignore[attr-defined]
    return True


@celery_app.task(name="email.reset_password", bind=True, max_retries=3)
def send_password_reset_email(self, user_id: int, token: str) -> bool:  # type: ignore[no-untyped-def]
    user = db.session.get(User, user_id)
    if user is None:
        return False
    ok = _deliver(
        user_id=user.id,
        to_address=user.email,
        template="reset",
        context={
            "user": user,
            "token": token,
            "link": f"{current_app.config['SITE_URL']}/reset-password?token={token}",
        },
    )
    if not ok:
        raise self.retry(countdown=RETRY_BACKOFF[min(self.request.retries, 2)])  # type: ignore[attr-defined]
    return True


@celery_app.task(name="email.welcome", bind=True, max_retries=2)
def send_welcome_email(self, user_id: int) -> bool:  # type: ignore[no-untyped-def]
    user = db.session.get(User, user_id)
    if user is None:
        return False
    return _deliver(
        user_id=user.id,
        to_address=user.email,
        template="welcome",
        context={"user": user},
    )


@celery_app.task(name="email.moderation_notice", bind=True, max_retries=3)
def send_moderation_notice(self, user_id: int, title: str, body: str) -> bool:  # type: ignore[no-untyped-def]
    user = db.session.get(User, user_id)
    if user is None:
        return False
    return _deliver(
        user_id=user.id,
        to_address=user.email,
        template="moderation",
        context={"user": user, "notice_title": title, "notice_body": body},
    )


@celery_app.task(name="email.data_request_ready", bind=True, max_retries=2)
def send_data_request_ready(self, user_id: int, request_id: str) -> bool:  # type: ignore[no-untyped-def]
    user = db.session.get(User, user_id)
    if user is None:
        return False
    link = f"{current_app.config['SITE_URL']}/settings/data?request={request_id}"
    return _deliver(
        user_id=user.id,
        to_address=user.email,
        template="data_ready",
        context={"user": user, "link": link, "request_id": request_id},
    )


__all__ = [
    "send_data_request_ready",
    "send_moderation_notice",
    "send_password_reset_email",
    "send_verification_email",
    "send_welcome_email",
]

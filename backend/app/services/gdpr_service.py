"""Data-subject rights (GDPR chapter III) and the privacy-policy surface.

Every mechanism here is required by the regulation rather than invented for
this project, so the mapping is documented explicitly:

    Art. 15/20  access & portability   -> :func:`request_export`
    Art. 16     rectification          -> profile update endpoint
    Art. 17     erasure                 -> :mod:`app.services.user_service`
    Art. 18     restriction            -> :func:`request_restriction`
    Art. 21     objection              -> consent withdrawal
    Art. 30     records of processing  -> :func:`processing_register`
    Art. 33     breach notification    -> :class:`BreachRecord`
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import Any

from flask import current_app

from ..extensions import db
from ..models.base import utcnow
from ..models.system import (
    DataRequest,
    DataRequestKind,
    DataRequestStatus,
    LegalDocument,
)
from ..models.user import User
from ..utils.crypto import generate_token
from ..utils.logging import get_logger, log_event
from ..utils.responses import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)

logger = get_logger("harmony.service.gdpr")

#: Statutory deadline for responding to a data-subject request.
RESPONSE_DEADLINE_DAYS = 30
#: How long a generated export stays downloadable.
EXPORT_TTL_DAYS = 7


# ---------------------------------------------------------------------------
# Legal documents
# ---------------------------------------------------------------------------

# Version 1.1: the stored content is now a fragment rather than a whole HTML
# page. It has always been injected into a div by `pages/legal.js`, so the
# doctype, <head> and the template's own <style> were either discarded by the
# parser or applied on top of the app's stylesheet - the legal pages rendered in
# a foreign typeface, and the template's <main> became a second <main> inside
# the shell's. The wording did not change, but the stored artefact did, and the
# content hash travels with each consent record, so this is a new version.
DOCUMENTS: dict[str, tuple[str, str, str]] = {
    "terms": (
        "1.1",
        "Пользовательское соглашение",
        "terms",
    ),
    "privacy": (
        "1.1",
        "Политика конфиденциальности",
        "privacy",
    ),
}


def ensure_legal_documents() -> list[LegalDocument]:
    """Create the current version of each legal document from its template.

    Documents live as files under ``app/templates/legal`` and are hashed at load
    time. The hash is what gets attached to each consent record, so we can prove
    *which* wording a user agreed to even after the text is edited.
    """
    created: list[LegalDocument] = []
    template_dir = os.path.join(current_app.root_path, "templates", "legal")
    for slug, (version, title, filename) in DOCUMENTS.items():
        existing = db.session.scalar(
            db.select(LegalDocument).where(LegalDocument.slug == slug, LegalDocument.version == version)
        )
        if existing is not None:
            continue
        path = os.path.join(template_dir, f"{filename}.html")
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            log_event(logger, "ERROR", "gdpr.document_missing", slug=slug, path=path)
            continue
        document = LegalDocument(
            slug=slug,
            version=version,
            title=title,
            content=content,
            content_hash=_content_hash(content),
            effective_from=utcnow(),
            is_current=True,
            locale="ru",
        )
        db.session.add(document)
        created.append(document)
    if created:
        db.session.commit()
        log_event(logger, "INFO", "gdpr.documents_seeded", count=len(created))
    return created


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_document(slug: str, *, include_content: bool = True) -> dict[str, Any]:
    document = db.session.scalar(
        db.select(LegalDocument)
        .where(LegalDocument.slug == slug, LegalDocument.is_current.is_(True))
        .order_by(LegalDocument.effective_from.desc())
    )
    if document is None:
        raise NotFoundError("Документ не найден.", code="document_not_found")
    return document.to_dict(include_content=include_content)


def list_documents() -> list[dict[str, Any]]:
    rows = db.session.query(LegalDocument).where(LegalDocument.is_current.is_(True)).order_by(LegalDocument.slug).all()
    return [row.to_dict() for row in rows]


# ---------------------------------------------------------------------------
# Data-subject requests
# ---------------------------------------------------------------------------


def _new_request(user: User, kind: str, reason: str | None) -> DataRequest:
    open_request = db.session.scalar(
        db.select(DataRequest).where(
            DataRequest.user_id == user.id,
            DataRequest.kind == kind,
            DataRequest.status.in_([DataRequestStatus.PENDING.value, DataRequestStatus.PROCESSING.value]),
        )
    )
    if open_request is not None:
        raise ConflictError("Такой запрос уже обрабатывается.", code="request_already_open")

    request_row = DataRequest(
        user_id=user.id,
        kind=kind,
        status=DataRequestStatus.PENDING.value,
        reason=(reason or "")[:1000] or None,
        due_at=utcnow() + timedelta(days=RESPONSE_DEADLINE_DAYS),
    )
    db.session.add(request_row)
    return request_row


def request_export(user: User, reason: str | None = None) -> DataRequest:
    """Art. 15/20 — machine-readable copy of everything held about the user."""
    from .user_service import export_user_data

    request_row = _new_request(user, DataRequestKind.EXPORT.value, reason)
    try:
        payload = export_user_data(user)
    except Exception as exc:
        db.session.rollback()
        log_event(logger, "ERROR", "gdpr.export_failed", user_id=user.id, error=exc.__class__.__name__)
        raise ServiceUnavailableError(
            "Не удалось сформировать выгрузку. Попробуйте позже.", code="export_failed"
        ) from exc

    path = _write_export(user, payload)
    request_row.status = DataRequestStatus.COMPLETED.value
    request_row.processed_at = utcnow()
    request_row.payload_url = path
    request_row.payload_expires_at = utcnow() + timedelta(days=EXPORT_TTL_DAYS)
    db.session.commit()

    from .user_service import record_audit

    record_audit("gdpr.export_completed", user, request_id=request_row.public_id)
    log_event(logger, "INFO", "gdpr.export_completed", user_id=user.id, request_id=request_row.public_id)
    return request_row


def request_restriction(user: User, reason: str | None = None) -> DataRequest:
    """Art. 18 — processing limited to storage while a dispute is resolved."""
    request_row = _new_request(user, DataRequestKind.RESTRICT.value, reason)
    request_row.status = DataRequestStatus.COMPLETED.value
    request_row.processed_at = utcnow()
    user.status_reason = "Обработка ограничена по запросу субъекта данных"
    db.session.commit()
    return request_row


def request_erasure(user: User, reason: str | None = None) -> DataRequest:
    """Art. 17 — delegates to the account-deletion flow with its grace period."""
    from .user_service import schedule_account_deletion

    request_row = _new_request(user, DataRequestKind.ERASE.value, reason)
    request_row.status = DataRequestStatus.COMPLETED.value
    request_row.processed_at = utcnow()
    schedule_account_deletion(user, reason)
    db.session.commit()
    return request_row


def request_objection(user: User, reason: str | None = None) -> DataRequest:
    """Art. 21 — withdraw consent for a specific processing purpose."""
    from .user_service import record_audit

    request_row = _new_request(user, DataRequestKind.OBJECT.value, reason)
    user.marketing_consent = False
    user.email_notifications = False
    request_row.status = DataRequestStatus.COMPLETED.value
    request_row.processed_at = utcnow()
    db.session.commit()
    record_audit("gdpr.objection_recorded", user, request_id=request_row.public_id)
    return request_row


def list_requests(user: User) -> list[dict[str, Any]]:
    rows = (
        db.session.query(DataRequest)
        .filter(DataRequest.user_id == user.id)
        .order_by(DataRequest.created_at.desc())
        .all()
    )
    return [row.to_dict() for row in rows]


def download_export(user: User, request_id: str) -> str:
    """Authorise and return the path to the user's own export."""
    request_row = db.session.scalar(
        db.select(DataRequest).where(DataRequest.user_id == user.id, DataRequest.public_id == request_id)
    )
    if request_row is None:
        raise NotFoundError("Запрос не найден.", code="request_not_found")
    if request_row.status != DataRequestStatus.COMPLETED.value or not request_row.payload_url:
        raise ConflictError("Выгрузка ещё не готова.", code="export_not_ready")
    if request_row.payload_expires_at and request_row.payload_expires_at < utcnow():
        raise ConflictError("Срок действия выгрузки истёк. Запросите новую.", code="export_expired")
    return request_row.payload_url


def purge_expired_exports() -> int:
    """Delete generated export files past their TTL (Art. 5(1)(e) storage limitation)."""
    cutoff = utcnow()
    rows = (
        db.session.query(DataRequest)
        .filter(
            DataRequest.kind == DataRequestKind.EXPORT.value,
            DataRequest.payload_expires_at.isnot(None),
            DataRequest.payload_expires_at <= cutoff,
        )
        .all()
    )
    removed = 0
    for row in rows:
        if row.payload_url and os.path.isfile(row.payload_url):
            try:
                os.remove(row.payload_url)
                removed += 1
            except OSError:  # pragma: no cover
                pass
        row.payload_url = None
        row.status = DataRequestStatus.COMPLETED.value
    if rows:
        db.session.commit()
        log_event(logger, "INFO", "gdpr.exports_purged", count=removed)
    return removed


def _write_export(user: User, payload: dict[str, Any]) -> str:
    directory = os.path.join(current_app.config["UPLOAD_DIR"], "..", "exports")
    directory = os.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)
    filename = f"export-{user.public_id}-{generate_token(8)}.json"
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    os.chmod(path, 0o600)  # contains personal data: owner-only
    return f"/api/v1/me/data/export/{filename}"


# ---------------------------------------------------------------------------
# Transparency artefacts
# ---------------------------------------------------------------------------


def processing_register() -> dict[str, Any]:
    """Art. 30 record of processing activities, generated from live config.

    Kept next to the code that implements each purpose, so the register cannot
    silently drift from reality the way a hand-maintained document does.
    """
    retention = int(current_app.config.get("DATA_RETENTION_DAYS", 365))
    return {
        "controller": {
            "name": current_app.config.get("APP_NAME"),
            "contact": current_app.config.get("PRIVACY_CONTROLLER_EMAIL"),
            "dpo": current_app.config.get("DPO_EMAIL"),
        },
        "generated_at": utcnow().isoformat(),
        "activities": [
            {
                "purpose": "Регистрация и аутентификация",
                "legal_basis": "Исполнение договора (ст. 6(1)(b) GDPR)",
                "data_categories": ["email", "username", "password hash", "IP (псевдонимизированный)"],
                "retention": "До удаления аккаунта",
                "automated_decision_making": False,
            },
            {
                "purpose": "Публикация и чтение пользовательского контента",
                "legal_basis": "Исполнение договора",
                "data_categories": ["текст публикаций", "изображения", "комментарии", "метаданные"],
                "retention": f"{retention} дней с момента публикации",
                "automated_decision_making": True,
                "notes": "Автоматическая модерация; решение может быть обжаловано через support.",
            },
            {
                "purpose": "Антиспам и защита от злоупотреблений",
                "legal_basis": "Законные интересы (ст. 6(1)(f) GDPR)",
                "data_categories": ["события злоупотребления", "оценка риска"],
                "retention": "24 часа",
                "automated_decision_making": True,
            },
            {
                "purpose": "Аналитика и мониторинг",
                "legal_basis": "Законные интересы",
                "data_categories": ["агрегированные метрики без идентификаторов"],
                "retention": "30 дней",
                "automated_decision_making": False,
            },
            {
                "purpose": "Транзакционные уведомления",
                "legal_basis": "Исполнение договора",
                "data_categories": ["email", "шаблоны писем"],
                "retention": "Журнал доставки 90 дней, содержимое не сохраняется",
                "automated_decision_making": False,
            },
        ],
        "transfers": [
            {
                "destination": "Хостинг-провайдер",
                "safeguard": "Договор обработки (SCC / GDPR Art. 28)",
            }
        ],
        "rights": {
            "access": f"GET {current_app.config['API_PREFIX']}/me/data",
            "portability": f"POST {current_app.config['API_PREFIX']}/me/data/export",
            "erasure": f"DELETE {current_app.config['API_PREFIX']}/me",
            "restriction": f"POST {current_app.config['API_PREFIX']}/me/data/restriction",
            "objection": f"POST {current_app.config['API_PREFIX']}/me/data/objection",
            "withdraw_consent": f"PATCH {current_app.config['API_PREFIX']}/me/privacy",
            "complaint": "Уполномоченный орган по защите данных",
        },
    }


def cookie_inventory() -> list[dict[str, Any]]:
    """Document every cookie the platform sets, for the transparency page."""
    secure = bool(current_app.config.get("SECURE_COOKIE"))
    same_site = current_app.config.get("SESSION_COOKIE_SAMESITE", "Lax")
    return [
        {
            "name": "harmony_access",
            "purpose": "Аутентификация (JWT в HttpOnly-cookie)",
            "http_only": True,
            "secure": secure,
            "same_site": same_site,
            "duration_days": round(int(current_app.config["ACCESS_TOKEN_TTL"]) / 86400, 2),
            "category": "necessary",
        },
        {
            "name": "harmony_refresh",
            "purpose": "Обновление сессии",
            "http_only": True,
            "secure": secure,
            "same_site": same_site,
            "duration_days": round(int(current_app.config["REFRESH_TOKEN_TTL"]) / 86400, 2),
            "category": "necessary",
        },
        {
            "name": current_app.config.get("CSRF_COOKIE_NAME", "harmony_csrf"),
            "purpose": "Защита от межсайтовых запросов (double submit)",
            "http_only": False,
            "secure": secure,
            "same_site": same_site,
            "duration_days": 1,
            "category": "necessary",
        },
    ]


__all__ = [
    "EXPORT_TTL_DAYS",
    "RESPONSE_DEADLINE_DAYS",
    "cookie_inventory",
    "download_export",
    "ensure_legal_documents",
    "get_document",
    "list_documents",
    "list_requests",
    "processing_register",
    "purge_expired_exports",
    "request_erasure",
    "request_export",
    "request_objection",
    "request_restriction",
]

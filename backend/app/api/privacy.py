"""Privacy, data-subject rights and account lifecycle."""

from __future__ import annotations

import os

from flask import Blueprint, current_app, g, request, send_file

from ..extensions import db
from ..models.user import UserStatus
from ..security.decorators import auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..services import gdpr_service, user_service
from ..utils.responses import NotFoundError, PermissionError_, ok

bp = Blueprint("privacy", __name__)


# ---------------------------------------------------------------------------
# Public legal surfaces
# ---------------------------------------------------------------------------


@bp.get("/legal/documents")
def legal_documents():
    return ok({"documents": gdpr_service.list_documents()})


@bp.get("/legal/documents/<slug>")
def legal_document(slug: str):
    return ok({"document": gdpr_service.get_document(slug)})


@bp.get("/legal/cookies")
def cookies():
    return ok({"cookies": gdpr_service.cookie_inventory()})


@bp.get("/legal/processing-register")
def processing_register():
    return ok(gdpr_service.processing_register())


# ---------------------------------------------------------------------------
# Data subject requests (Art. 15–21)
# ---------------------------------------------------------------------------


@bp.get("/me/data")
@auth_required
def export_data():
    """Immediate machine-readable view of everything held about the caller."""
    enforce("global")
    return ok(user_service.export_user_data(g.current_user))


@bp.get("/me/data/requests")
@auth_required
def list_requests():
    return ok({"requests": gdpr_service.list_requests(g.current_user)})


@bp.post("/me/data/export")
@auth_required
def request_export():
    """Produce a downloadable archive and email the (time-limited) link."""
    verify_csrf()
    enforce("global")
    payload = request.get_json(silent=True) or {}
    data_request = gdpr_service.request_export(g.current_user, payload.get("reason"))
    return ok(
        {
            "request": data_request.to_dict(),
            "message": "Выгрузка сформирована. Ссылка будет действовать 7 дней.",
        }
    )


@bp.get("/me/data/export/<request_id>")
@auth_required
def download_export(request_id: str):
    """Stream the caller's own archive back as a file download."""
    enforce("global")
    location = gdpr_service.download_export(g.current_user, request_id)
    filename = location.rsplit("/", 1)[-1]
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise NotFoundError("Файл не найден.", code="export_not_found")

    directory = os.path.abspath(os.path.join(current_app.config["UPLOAD_DIR"], "..", "exports"))
    path = os.path.abspath(os.path.join(directory, filename))
    # Defence in depth: the resolved path must remain inside the export dir.
    if not path.startswith(directory + os.sep) or not os.path.isfile(path):
        raise NotFoundError("Файл не найден или срок действия истёк.", code="export_not_found")
    return send_file(path, as_attachment=True, download_name=f"harmony-data-{g.current_user.username}.json")


@bp.post("/me/data/restriction")
@auth_required
def request_restriction():
    verify_csrf()
    enforce("global")
    payload = request.get_json(silent=True) or {}
    data_request = gdpr_service.request_restriction(g.current_user, payload.get("reason"))
    return ok({"request": data_request.to_dict()})


@bp.post("/me/data/objection")
@auth_required
def request_objection():
    verify_csrf()
    enforce("global")
    payload = request.get_json(silent=True) or {}
    data_request = gdpr_service.request_objection(g.current_user, payload.get("reason"))
    return ok({"request": data_request.to_dict(), "message": "Обработка для маркетинговых целей прекращена."})


@bp.post("/me/data/consent")
@auth_required
def withdraw_consent():
    """Withdraw marketing consent independently of other processing."""
    verify_csrf()
    user = g.current_user
    user.marketing_consent = False
    user.email_notifications = False
    db.session.commit()
    user_service.record_audit("gdpr.consent_withdrawn", user, purpose="marketing")
    return ok({"marketing_consent": False, "email_notifications": False})


# ---------------------------------------------------------------------------
# Account deletion (Art. 17)
# ---------------------------------------------------------------------------


@bp.get("/me/deletion")
@auth_required
def deletion_status():
    from ..models.system import AccountDeletionRequest

    row = db.session.scalar(
        db.select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == g.current_user.id)
    )
    return ok({"deletion": row.to_dict() if row else None})


@bp.post("/me/deletion")
@auth_required(allow=(UserStatus.DELETION_PENDING.value,))
def schedule_deletion():
    """Schedule irreversible erasure after a grace period.

    Requires the ``reauth_token`` issued by ``/auth/reauthenticate``: a hijacked
    session alone must not be able to destroy an account.

    ``DELETION_PENDING`` is allowed through on purpose. A second attempt while
    the first is still pending must be answered with ``reauth_invalid`` (the step
    -up token was already spent) rather than with an account-status error that
    tells the user nothing about why the call failed.
    """
    verify_csrf()
    enforce("auth:login")

    payload = request.get_json(silent=True) or {}
    _require_reauth(payload)

    request_row = user_service.schedule_account_deletion(
        g.current_user, payload.get("reason"), password_confirmed=bool(payload.get("password_confirmed"))
    )
    return ok(
        {
            "deletion": request_row.to_dict(),
            "message": (
                f"Аккаунт будет удалён {request_row.scheduled_for:%d.%m.%Y}. "
                "До этого момента вы можете отменить удаление."
            ),
        }
    )


@bp.post("/me/deletion/cancel")
@auth_required(allow=(UserStatus.DELETION_PENDING.value,))
def cancel_deletion():
    """Withdraw a scheduled deletion. The grace period exists to be used."""
    verify_csrf()
    request_row = user_service.cancel_account_deletion(g.current_user)
    return ok({"deletion": request_row.to_dict(), "message": "Удаление отменено. Аккаунт снова активен."})


def _require_reauth(payload: dict) -> None:  # type: ignore[no-untyped-def]
    """Validate a step-up token issued by ``/auth/reauthenticate``."""
    from ..services import auth_service

    token = str(payload.get("reauth_token") or "")
    if not token:
        raise PermissionError_("Подтвердите действие паролем.", code="reauth_required")
    user = auth_service.consume_token(token, auth_service.REAUTH_PURPOSE)
    if user is None or user.id != g.current_user.id:
        raise PermissionError_("Подтверждение устарело. Повторите ввод пароля.", code="reauth_invalid")
    db.session.commit()


__all__ = ["bp"]

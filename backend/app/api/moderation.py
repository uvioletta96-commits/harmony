"""Reporting and user-facing moderation endpoints."""

from __future__ import annotations

from flask import Blueprint, g, request

from ..models.moderation import ReportReason, TargetType
from ..security.decorators import auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..security.validators import Schema
from ..services import moderation_service
from ..utils.responses import created, ok

bp = Blueprint("moderation", __name__)

report_schema = (
    Schema()
    .choice("target_type", [item.value for item in TargetType], required=True)
    .raw("target_id", required=True)
    .choice("reason", [item.value for item in ReportReason], required=True)
    .string("details", required=False, max_length=1000, allow_newlines=True, default="")
    .ignore_unknown()
)


@bp.post("/reports")
@auth_required
def create_report():
    """File a report on a post, comment, message or account.

    Reports are deduplicated per (reporter, target) so one user cannot flood
    the queue, and the reporter is never told what action was taken.
    """
    verify_csrf()
    enforce("report:create")
    data = report_schema(request.get_json(silent=True) or {})
    report = moderation_service.create_report(
        g.current_user,
        data["target_type"],
        str(data["target_id"]),
        reason=data["reason"],
        details=data.get("details") or None,
    )
    return created(
        {
            "report": {"id": report.public_id, "status": report.status, "created_at": report.created_at.isoformat()},
            "message": "Жалоба отправлена. Спасибо — мы её рассмотрим.",
        }
    )


@bp.get("/reports/mine")
@auth_required
def my_reports():
    """Let a user see the status of what they reported — transparency matters,
    and it also stops people filing duplicates while waiting for a reply."""
    from ..extensions import db
    from ..models.moderation import Report

    rows = (
        db.session.query(Report)
        .filter(Report.reporter_id == g.current_user.id)
        .order_by(Report.created_at.desc())
        .limit(50)
        .all()
    )
    return ok({"reports": [row.to_dict() for row in rows]})


@bp.get("/moderation/guidelines")
def guidelines():
    """The rules of the community, as data so the UI and the API cannot drift."""
    from ..services import gdpr_service

    return ok(
        {
            "reasons": [item.value for item in ReportReason],
            "targets": [item.value for item in TargetType],
            "documents": gdpr_service.list_documents(),
        }
    )


__all__ = ["bp"]

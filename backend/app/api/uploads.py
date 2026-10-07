"""Image upload endpoint.

Accepts ``multipart/form-data`` and, for clients that prefer it, a JSON body
with base64 payloads. The response carries a storage key the client later
references when creating a post.
"""

from __future__ import annotations

import base64
import binascii

from flask import Blueprint, current_app, g, request

from ..extensions import db
from ..models.post import PostMedia
from ..security.decorators import auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..services import upload_service
from ..services.upload_service import StoredImage
from ..utils.logging import get_logger
from ..utils.responses import ValidationError, created

bp = Blueprint("uploads", __name__)
logger = get_logger("harmony.api.uploads")

MAX_FILES_PER_REQUEST = 4


@bp.post("/uploads/images")
@auth_required
def upload_image():
    """Store one or more attachments - images or video - and return unclaimed references.

    Rows are created with ``post_id = NULL``; a later post-creation call claims
    them. A cleanup task removes uploads that were never claimed, so an
    attacker cannot fill the disk by uploading and abandoning files.

    The route keeps its historical name so existing clients do not break; the
    type of each file is decided by its magic number, not by the endpoint, the
    filename or the client's Content-Type.
    """
    verify_csrf()
    enforce("upload:image")

    files, inline = collect_upload_payload()
    if not files:
        raise ValidationError("Не передано ни одного файла.", code="no_files")

    max_per_request = min(MAX_FILES_PER_REQUEST, int(current_app.config.get("MAX_POST_MEDIA", 6)))
    if len(files) > max_per_request:
        raise ValidationError(f"За один раз можно загрузить не более {max_per_request} файлов.", code="too_many_files")

    alt_text = (request.form.get("alt_text") or (inline or {}).get("alt_text") or "").strip()[:240]

    # The rows are kept in step with the stored files as they are created.
    # Re-querying them afterwards and zipping by storage_key would silently
    # pair the wrong file with the wrong row whenever two uploads collide.
    stored: list[StoredImage] = []
    rows: list[PostMedia] = []
    for payload, _filename in files:
        media = upload_service.store_media(payload, user_id=g.current_user.id)
        row = PostMedia(
            owner_id=g.current_user.id,
            post_id=None,
            position=len(stored),
            storage_key=media.storage_key,
            url=media.url,
            thumbnail_url=media.thumbnail_url,
            width=media.width,
            height=media.height,
            byte_size=media.byte_size,
            mime_type=media.mime_type,
            alt_text=alt_text or None,
            content_hash=media.content_hash,
            is_processed=True,
        )
        db.session.add(row)
        stored.append(media)
        rows.append(row)

    db.session.commit()
    return created(
        {
            "files": [
                {**media.to_dict(), "id": row.id, "alt_text": row.alt_text}
                for media, row in zip(stored, rows, strict=True)
            ]
        }
    )


def collect_upload_payload() -> tuple[list[tuple[bytes, str]], dict]:  # type: ignore[no-untyped-def]
    """Accept multipart uploads or a JSON base64 body.

    Shared with the chat attachment endpoint, which accepts the same two shapes for
    the same reason. One base64 parser rather than two copies that can drift.
    """
    files: list[tuple[bytes, str]] = []
    inline: dict = {}

    if request.files:
        for key in sorted(request.files.keys()):
            for storage in request.files.getlist(key):
                data = storage.read()
                files.append((data, storage.filename or key))
        return files, inline

    payload = request.get_json(silent=True) or {}
    inline = payload
    for key in ("data", "base64", "content"):
        value = payload.get(key)
        if not value:
            continue
        if "," in value and value.strip().startswith("data:"):
            value = value.split(",", 1)[1]
        try:
            files.append((base64.b64decode(value, validate=True), str(payload.get("filename") or "inline")))
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("Некорректные данные изображения.", code="invalid_base64") from exc
        break

    if len(files) == 1 and isinstance(payload.get("files"), list):
        extra: list[tuple[bytes, str]] = []
        for item in payload["files"][:MAX_FILES_PER_REQUEST]:
            value = item.get("data") if isinstance(item, dict) else item
            if not value:
                continue
            if "," in str(value) and str(value).strip().startswith("data:"):
                value = str(value).split(",", 1)[1]
            try:
                extra.append((base64.b64decode(value, validate=True), "inline"))
            except (binascii.Error, ValueError) as exc:
                raise ValidationError("Некорректные данные изображения.", code="invalid_base64") from exc
        files.extend(extra)

    return files, inline


__all__ = ["bp"]

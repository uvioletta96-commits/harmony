"""Image upload pipeline: validate, sanitise, resize, store.

The order is deliberate. Bytes are validated by **magic number** before any
decode, decoded with Pillow's decompression-bomb guard, re-encoded (which drops
all EXIF and any embedded payload), and only then written under a
server-generated name. The original filename never reaches the filesystem, so
path traversal and double-extension tricks have nothing to attack.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import struct
from dataclasses import dataclass
from typing import Any

from flask import current_app

from ..security.content_moderation import get_engine as moderation_engine
from ..utils.logging import get_logger, log_event
from ..utils.responses import PayloadTooLargeError, UnsupportedMediaTypeError, ValidationError

logger = get_logger("harmony.service.uploads")

#: Pillow reports "this is not a usable image" through several unrelated
#: exceptions. A bad chunk CRC raises the **builtin** ``SyntaxError``; a
#: truncated file raises ``OSError``; an unrecognised magic number raises
#: ``UnidentifiedImageError`` (itself an ``OSError`` subclass); a malformed
#: chunk header raises ``struct.error``. To somebody who uploaded a damaged
#: file they all mean the same thing, and none of them is a fault in this code,
#: so none of them may escape as a 500. ``DecompressionBombError`` is handled
#: separately because it gets its own message.
IMAGE_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    SyntaxError,
    struct.error,
)

#: Magic-number signatures. Each entry is ``(mime, signature, length)``; the
#: first ``length`` bytes of the payload are compared after ASCII case-folding.
#: Length rather than offset, because every format we accept puts its signature
#: at the very start of the file.
SIGNATURES: tuple[tuple[str, bytes, int], ...] = (
    ("image/jpeg", b"\xff\xd8\xff", 3),
    ("image/png", b"\x89PNG\r\n\x1a\n", 8),
    ("image/gif", b"GIF87a", 6),
    ("image/gif", b"GIF89a", 6),
)
WEBP_SIGNATURE = b"RIFF"
WEBP_MARKER = b"WEBP"

#: Video containers, detected the same way images are: by what the bytes say,
#: not by the filename or the client's Content-Type. Each entry is
#: ``(mime, signature, offset, length)`` - the offset matters here because the
#: ISO base media container puts its brand at byte 4, not at the start.
VIDEO_SIGNATURES: tuple[tuple[str, bytes, int, int], ...] = (
    ("video/mp4", b"ftyp", 4, 4),  # MP4, MOV, 3GP and friends
    ("video/webm", b"\x1a\x45\xdf\xa3", 0, 4),  # Matroska / WebM
    ("video/ogg", b"OggS", 0, 4),
)

#: Audio containers for voice messages, detected the same way. The set is what a
#: browser's `MediaRecorder` actually produces - which is not the same as "every
#: audio format": WebM/Opus on Firefox and Chrome, MP4/AAC on Safari and iOS.
#: Accepting only what the recorder emits is what makes the client code portable,
#: because it can hand over whatever it was given without inspecting it.
AUDIO_SIGNATURES: tuple[tuple[str, bytes, int, int], ...] = (
    ("audio/webm", b"\x1a\x45\xdf\xa3", 0, 4),  # same EBML header as WebM video
    ("audio/mp4", b"ftyp", 4, 4),  # what Safari's MediaRecorder writes
    ("audio/ogg", b"OggS", 0, 4),
    ("audio/mpeg", b"\xff\xfb", 0, 2),  # MP3 frame sync, without the ID3 tag
    ("audio/wav", b"RIFF", 0, 4),
)

SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}

#: Formats we re-encode to. GIF is passed through untouched because re-encoding
#: would destroy the animation that is often the point of the upload.
RE_ENCODE = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


@dataclass
class StoredMedia:
    storage_key: str
    url: str
    thumbnail_url: str | None
    width: int
    height: int
    byte_size: int
    mime_type: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_key": self.storage_key,
            "url": self.url,
            "thumbnail_url": self.thumbnail_url,
            "width": self.width,
            "height": self.height,
            "byte_size": self.byte_size,
            "mime_type": self.mime_type,
            "content_hash": self.content_hash,
        }


#: The record stores images and video alike - same fields, same shape, one code
#: path on the client. The historical name is kept so existing imports keep
#: resolving; renaming it everywhere would be churn that hides the real change.
StoredImage = StoredMedia


def sniff_video_mime(data: bytes) -> str | None:
    """Identify a video container from its magic bytes.

    The same discipline as :func:`sniff_mime` and for the same reason: the
    extension and the client's Content-Type are both attacker-controlled, so
    the bytes have to be what decides. Unlike an image, a video is not
    re-encoded on the way in, so this is the only check the payload ever gets.
    """
    if not data:
        return None
    for mime, signature, offset, length in VIDEO_SIGNATURES:
        if data[offset : offset + length] == signature:
            return mime
    return None


def sniff_audio_mime(data: bytes) -> str | None:
    """Identify an audio container from its magic bytes.

    Same discipline as the image and video sniffers, and the same reason: the
    extension and the client's Content-Type are both attacker-controlled.

    Order matters where signatures overlap. WebM video and WebM audio share an
    EBML header, and `audio/mp4` shares the ISO brand with `video/mp4` - so a
    caller that does not know what it has gets the video answer, which is the
    safer of the two to over-report.
    """
    if not data:
        return None
    for mime, signature, offset, length in AUDIO_SIGNATURES:
        if data[offset : offset + length] == signature:
            return mime
    return None


def sniff_media_mime(data: bytes) -> str | None:
    """Identify an upload as an image, a video or an audio file.

    Image first, then video, then audio: the sniffers overlap where the formats
    share a container, and reporting the richer type is the one that renders.
    """
    return sniff_mime(data) or sniff_video_mime(data) or sniff_audio_mime(data)


def sniff_mime(data: bytes) -> str | None:
    """Identify the format from the file signature, not the client's claim."""
    if not data:
        return None
    head = data[:12]
    for mime, signature, length in SIGNATURES:
        if head[:length].upper() == signature.upper():
            return mime
    if head[:4] == WEBP_SIGNATURE and head[8:12] == WEBP_MARKER:
        return "image/webp"
    return None


def build_storage_key(user_id: int, mime_type: str, content_hash: str) -> str:
    """``<yyyy>/<mm>/<user>/<hash>.<ext>``.

    Hashed so the same upload is stored once per account and the key leaks no
    sequential information; sharded by date so a directory never grows unbounded.
    """
    from ..models.base import utcnow

    now = utcnow()
    suffix = SUFFIXES.get(mime_type, ".bin")
    token = content_hash[:32] or secrets.token_hex(16)
    return f"{now:%Y}/{now:%m}/{user_id}/{token}{suffix}"


def public_url(storage_key: str) -> str:
    """Where a stored key can be fetched from.

    Delegates to the configured backend, so the same key resolves to a local
    path in development and to a bucket URL in production. Nothing above this
    function knows which it is.
    """
    from .storage import get_storage

    return get_storage().url(storage_key)


def _absolute_path(storage_key: str) -> str:
    """Local path for a key, or raise.

    Only the orphan-cleanup job needs a filesystem path, and only when the
    backend is the local disk. Raising for any other backend is deliberate
    rather than returning a plausible-looking wrong answer.
    """
    from .storage import LocalStorage, get_storage

    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise ValidationError("Хранилище не является локальным диском.", code="storage_not_local")
    try:
        return storage._path(storage_key)
    except ValueError as error:
        raise ValidationError("Некорректное имя файла.", code="invalid_storage_key") from error


def ensure_upload_root() -> str:
    root = current_app.config["UPLOAD_DIR"]
    os.makedirs(root, exist_ok=True)
    return root


def _write(storage_key: str, payload: bytes) -> str:
    """Store bytes under a key in whichever backend is configured.

    One function so there is a single place that knows how bytes reach storage,
    and a single place to look when an upload fails.
    """
    from .storage import get_storage

    return get_storage().write(storage_key, payload)


def remove_stored(*storage_keys: str | None) -> None:
    """Delete stored files by key, ignoring any that are already gone.

    Delegates to :func:`delete_image`, whose name predates video and audio but
    whose body is type-agnostic - it removes a key and the thumbnail beside it,
    which is exactly what any attachment needs. Reimplemented here it would be a
    second copy of the `_thumb` convention to keep in step.
    """
    for key in storage_keys:
        if key:
            delete_image(key)


def store_image(
    data: bytes,
    *,
    user_id: int,
    alt_text: str | None = None,
    make_thumbnail: bool = True,
) -> StoredImage:
    """Validate, normalise and persist one uploaded image."""
    if not data:
        raise ValidationError("Файл пуст.", code="empty_upload")
    max_bytes = int(current_app.config.get("UPLOAD_MAX_BYTES", 8 * 1024 * 1024))
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"Файл превышает допустимый размер ({max_bytes // (1024 * 1024)} МБ).", code="file_too_large"
        )

    mime_type = sniff_mime(data)
    allowed = set(current_app.config.get("UPLOAD_ALLOWED_MIME") or [])
    if mime_type is None or mime_type not in allowed:
        raise UnsupportedMediaTypeError(
            "Поддерживаются только изображения JPEG, PNG, WebP и GIF.", code="unsupported_image"
        )

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a declared dependency
        logger.error("Pillow is not installed; image processing is disabled")
        raise ValidationError("Обработка изображений временно недоступна.", code="imaging_unavailable") from exc

    try:
        Image.MAX_IMAGE_PIXELS = 80_000_000  # decompression-bomb ceiling
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()  # structural validation, O(1) for most formats
    except Image.DecompressionBombError as exc:
        raise ValidationError("Изображение слишком большое.", code="decompression_bomb") from exc
    except IMAGE_PARSE_ERRORS as exc:
        raise ValidationError("Не удалось прочитать изображение. Файл повреждён?", code="corrupt_image") from exc

    content_hash = hashlib.sha256(data).hexdigest()

    # `verify()` checks structure, not decodability: a file can pass it and
    # still fail in `load()`, so the decode step needs the same net.
    try:
        image, meta = _decode_and_normalise(data, mime_type)
    except Image.DecompressionBombError as exc:
        raise ValidationError("Изображение слишком большое.", code="decompression_bomb") from exc
    except IMAGE_PARSE_ERRORS as exc:
        raise ValidationError("Не удалось прочитать изображение. Файл повреждён?", code="corrupt_image") from exc
    if image is None:
        raise ValidationError("Не удалось обработать изображение.", code="image_processing_failed")

    structural = moderation_engine().screen_image_metadata(
        width=meta["width"], height=meta["height"], byte_size=len(data), mime_type=mime_type
    )
    if structural.blocked:
        raise ValidationError("Изображение отклонено проверкой безопасности.", code="image_rejected")
    if structural.needs_review:
        log_event(logger, "INFO", "upload.image_review", reasons=structural.reasons)

    storage_key = build_storage_key(user_id, meta["mime_type"], content_hash)
    payload = meta["bytes"]
    _write(storage_key, payload)

    thumbnail_url = None
    if make_thumbnail:
        thumbnail_url = _write_thumbnail(image, storage_key, user_id)

    log_event(
        logger,
        "INFO",
        "upload.stored",
        user_id=user_id,
        mime_type=meta["mime_type"],
        byte_size=len(payload),
        width=meta["width"],
        height=meta["height"],
    )
    return StoredImage(
        storage_key=storage_key,
        url=public_url(storage_key),
        thumbnail_url=thumbnail_url,
        width=meta["width"],
        height=meta["height"],
        byte_size=len(payload),
        mime_type=meta["mime_type"],
        content_hash=content_hash,
    )


def store_video(
    data: bytes,
    *,
    user_id: int,
    alt_text: str | None = None,
) -> StoredMedia:
    """Validate and persist one uploaded video.

    Unlike an image, a video is stored byte-for-byte. Re-encoding would mean
    shipping ffmpeg and accepting a transcode queue, which is a different
    product with different failure modes; here the goal is to accept a file the
    browser can already play, verify that it really is one, and get it out of
    the upload directory. Dimensions and thumbnails stay empty rather than being
    guessed at - nothing downstream requires them to be truthful, because
    nothing downstream reads them.
    """
    if not data:
        raise ValidationError("Файл пуст.", code="empty_upload")

    max_bytes = int(current_app.config.get("UPLOAD_MAX_VIDEO_BYTES", 50 * 1024 * 1024))
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"Видео превышает допустимый размер ({max_bytes // (1024 * 1024)} МБ).", code="video_too_large"
        )

    mime_type = sniff_video_mime(data)
    allowed = set(current_app.config.get("UPLOAD_ALLOWED_VIDEO_MIME") or [])
    if mime_type is None or mime_type not in allowed:
        raise UnsupportedMediaTypeError("Поддерживаются только видео MP4, WebM и Ogg.", code="unsupported_video")

    content_hash = hashlib.sha256(data).hexdigest()
    storage_key = build_storage_key(user_id, mime_type, content_hash)
    _write(storage_key, data)

    log_event(logger, "INFO", "upload.video_stored", user_id=user_id, mime_type=mime_type, byte_size=len(data))
    return StoredMedia(
        storage_key=storage_key,
        url=public_url(storage_key),
        thumbnail_url=None,
        width=0,
        height=0,
        byte_size=len(data),
        mime_type=mime_type,
        content_hash=content_hash,
    )


def store_audio(
    data: bytes,
    *,
    user_id: int,
    alt_text: str | None = None,
) -> StoredMedia:
    """Validate and persist one recorded voice clip.

    Stored byte-for-byte, like video: a recording is already whatever the browser
    produced, and re-encoding it server-side would mean shipping ffmpeg for no gain.

    The duration is not read from the file. The container's header field is not
    reliable across the four formats above, and a voice message whose length is
    wrong cannot be drawn as a progress bar - so the client reports it while
    recording, where it knows exactly, and :func:`store_audio` is told.
    """
    if not data:
        raise ValidationError("Файл пуст.", code="empty_upload")

    max_bytes = int(current_app.config.get("UPLOAD_MAX_AUDIO_BYTES", 25 * 1024 * 1024))
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"Голосовое сообщение слишком длинное ({max_bytes // (1024 * 1024)} МБ).",
            code="audio_too_large",
        )

    mime_type = sniff_audio_mime(data)
    allowed = set(current_app.config.get("UPLOAD_ALLOWED_AUDIO_MIME") or [])
    if mime_type is None or mime_type not in allowed:
        raise UnsupportedMediaTypeError(
            "Поддерживаются только записи голоса.", code="unsupported_audio"
        )

    content_hash = hashlib.sha256(data).hexdigest()
    storage_key = build_storage_key(user_id, mime_type, content_hash)
    _write(storage_key, data)

    log_event(logger, "INFO", "upload.audio_stored", user_id=user_id, mime_type=mime_type, byte_size=len(data))
    return StoredMedia(
        storage_key=storage_key,
        url=public_url(storage_key),
        thumbnail_url=None,
        width=0,
        height=0,
        byte_size=len(data),
        mime_type=mime_type,
        content_hash=content_hash,
    )


def store_chat_media(data: bytes, *, user_id: int, kind: str = "image") -> StoredChatMedia:
    """Persist a file destined for a chat message.

    `kind` says how the client wants it presented. What it *is* is decided by the
    bytes, and a file that is not even close is refused - which is the check that
    matters, because a JPEG uploaded as a voice recording is a client bug or an
    attempt to get something past the audio path, and either way should not be
    stored.

    The audio case has an honest limitation. WebM video and WebM audio share an
    EBML header, and MP4 video and MP4 audio share an ISO brand, so the first four
    bytes cannot tell a recording from a clip in either container - and those are
    exactly the two formats `MediaRecorder` produces. So for a voice message the
    declared kind is what disambiguates the container family, and the bytes are
    only checked for belonging to that family at all. Distinguishing them properly
    means walking the EBML tree for a video track, which is a decoder's job and
    belongs in ffmpeg, not in a sniffer.

    Everything else is checked strictly: a photo must sniff as an image and a video
    must sniff as a video.
    """
    sniffed = sniff_media_mime(data)
    if sniffed is None:
        raise UnsupportedMediaTypeError(
            "Не удалось определить тип файла.", code="unsupported_chat_media"
        )

    families = {
        "image": ("image/",),
        "video": ("video/",),
        "voice": ("audio/", "video/"),
        "circle": ("audio/", "video/"),
    }.get(kind)
    if families is None:
        raise ValidationError("Неизвестный тип вложения.", code="bad_attachment_kind")

    if not any(sniffed.startswith(family) for family in families):
        raise UnsupportedMediaTypeError(
            "Файл не соответствует выбранному типу.", code="attachment_kind_mismatch"
        )

    # An audio file is stored as audio; a video that arrived as a "voice message"
    # is stored as a video, so the stored type still tells the truth and the client
    # can render something that plays.
    if sniffed.startswith("audio/"):
        media = store_audio(data, user_id=user_id)
    elif sniffed.startswith("video/"):
        media = store_video(data, user_id=user_id)
    else:
        media = store_image(data, user_id=user_id)

    return StoredChatMedia(
        kind=kind,
        storage_key=media.storage_key,
        url=media.url,
        thumbnail_url=media.thumbnail_url,
        width=media.width,
        height=media.height,
        byte_size=media.byte_size,
        mime_type=media.mime_type,
        content_hash=media.content_hash,
    )


@dataclass(slots=True)
class StoredChatMedia:
    """What :func:`store_chat_media` returns: a stored file plus how it is presented."""

    kind: str
    storage_key: str
    url: str
    thumbnail_url: str | None
    width: int
    height: int
    byte_size: int
    mime_type: str
    content_hash: str


def store_media(
    data: bytes,
    *,
    user_id: int,
    alt_text: str | None = None,
    make_thumbnail: bool = True,
) -> StoredMedia:
    """Persist an upload, dispatching on what the bytes actually are.

    The type comes from the magic number rather than the extension or the
    client's Content-Type, so a ``.png`` that is really an MP4 is stored as
    video - which is what it is - instead of failing an image decoder and
    reporting a confusing error.
    """
    if sniff_video_mime(data):
        return store_video(data, user_id=user_id, alt_text=alt_text)
    return store_image(data, user_id=user_id, alt_text=alt_text, make_thumbnail=make_thumbnail)


def _decode_and_normalise(data: bytes, mime_type: str) -> tuple[Any, dict[str, Any]]:
    """Re-encode to strip EXIF and cap the dimensions.

    Re-encoding is the reliable way to remove GPS coordinates and camera
    serial numbers that phones embed in every JPEG. A file that keeps its
    original bytes keeps its metadata.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        source.load()
        width, height = source.size
        max_dimension = int(current_app.config.get("UPLOAD_MAX_DIMENSION", 2560))

        # Animated GIF: keep the frames, strip per-frame metadata.
        if mime_type == "image/gif":
            cleaned = _clean_gif(source)
            buffer = io.BytesIO()
            cleaned.save(buffer, format="GIF", optimize=True)
            return None, {
                "bytes": buffer.getvalue(),
                "width": width,
                "height": height,
                "mime_type": "image/gif",
            }

        frame = _strip_metadata(source)
        if max(frame.size) > max_dimension:
            frame.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        if frame.mode not in ("RGB", "L"):
            frame = frame.convert("RGB")

        buffer = io.BytesIO()
        target_format = RE_ENCODE.get(mime_type, "PNG")
        save_kwargs: dict[str, Any] = {"optimize": True, "quality": 86}
        frame.save(buffer, format=target_format, **save_kwargs)
        return frame, {
            "bytes": buffer.getvalue(),
            "width": frame.width,
            "height": frame.height,
            "mime_type": {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[target_format],
        }


def _strip_metadata(source: Any) -> Any:  # type: ignore[no-untyped-def]
    """Copy the pixels into a fresh image, dropping every EXIF/ICC/text block.

    ``Image.getdata`` is deprecated in Pillow 12 and removed in 14, so the
    replacement is looked up by name. ``putdata`` still accepts whatever
    iterable we hand it, and iterating the image directly is both the supported
    path and one object fewer per pixel.
    """
    from PIL import Image

    if current_app.config.get("UPLOAD_STRIP_EXIF", True):
        clean = Image.new(source.mode, source.size)
        pixels = source.get_flattened_data() if hasattr(source, "get_flattened_data") else source.getdata()
        clean.putdata(list(pixels))
        return clean
    return source.copy()


def _clean_gif(source: Any) -> Any:  # type: ignore[no-untyped-def]
    """Re-save a GIF frame-by-frame so comment/extension blocks are dropped."""

    frames: list[Any] = []
    try:
        while True:
            frames.append(source.copy())
            source.seek(source.tell() + 1)
    except EOFError:
        pass
    if not frames:
        frames = [source.copy()]
    cleaned = frames[0]
    for frame in frames[1:]:
        cleaned = cleaned.convert("RGBA")
        cleaned.paste(frame.convert("RGBA"), (0, 0), None)
    durations = [100] * len(frames)
    cleaned.info = {"duration": durations[0]}
    return cleaned


def _write_thumbnail(image: Any, storage_key: str, user_id: int) -> str | None:  # type: ignore[no-untyped-def]
    if image is None:
        return None
    from PIL import Image

    size = int(current_app.config.get("UPLOAD_THUMB_DIMENSION", 640))
    thumb = image.copy()
    thumb.thumbnail((size, size), Image.LANCZOS)
    buffer = io.BytesIO()
    target_format = (current_app.config.get("UPLOAD_ALLOWED_MIME") or ["image/jpeg"])[0]
    thumb_format = RE_ENCODE.get(target_format, "JPEG")
    if thumb_format == "PNG" and thumb.mode == "RGB":
        thumb = thumb.convert("RGBA")
    thumb.save(buffer, format=thumb_format, optimize=True)

    stem, _, suffix = storage_key.rpartition(".")
    thumb_key = f"{stem}_thumb.{suffix}"
    _write(thumb_key, buffer.getvalue())
    return public_url(thumb_key)


def delete_image(storage_key: str) -> bool:
    """Remove a stored image and its generated thumbnail."""
    stem, dot, suffix = storage_key.rpartition(".")
    targets = [storage_key]
    if dot and stem:
        targets.append(f"{stem}_thumb.{suffix}")
    removed = False
    for target in targets:
        try:
            os.remove(_absolute_path(target))
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:  # pragma: no cover
            log_event(logger, "ERROR", "upload.delete_failed", error=exc.__class__.__name__)
    return removed


def orphan_media_report() -> list[str]:
    """Storage keys not referenced by any post — used by a cleanup task."""
    from sqlalchemy import select

    from ..extensions import db
    from ..models.post import PostMedia

    referenced = {row[0] for row in db.session.execute(select(PostMedia.storage_key)).all()}
    root = ensure_upload_root()
    orphans: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if (
                filename.endswith("_thumb.jpg")
                or filename.endswith("_thumb.png")
                or filename.endswith("_thumb.webp")
                or filename.endswith("_thumb.gif")
            ):
                continue
            key = os.path.relpath(os.path.join(dirpath, filename), root).replace(os.sep, "/")
            if key not in referenced:
                orphans.append(key)
    return orphans


__all__ = [
    "StoredImage",
    "build_storage_key",
    "delete_image",
    "ensure_upload_root",
    "orphan_media_report",
    "public_url",
    "sniff_mime",
    "store_image",
]

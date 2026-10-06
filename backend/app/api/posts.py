"""Post endpoints: feed, CRUD, media and reactions."""

from __future__ import annotations

from flask import Blueprint, g, request

from ..models.post import PostVisibility
from ..security.decorators import auth_optional, auth_required, verify_csrf
from ..security.rate_limit import enforce
from ..security.validators import Schema
from ..services import post_service
from ..utils.responses import ValidationError, created, no_content, ok

bp = Blueprint("posts", __name__)

create_schema = (
    Schema()
    .string("body", required=False, max_length=5000, allow_newlines=True, default="")
    .choice("visibility", [item.value for item in PostVisibility], default=PostVisibility.PUBLIC.value)
    .list("media", max_items=4)
    .ignore_unknown()
)

update_schema = (
    Schema()
    .string("body", required=False, max_length=5000, allow_newlines=True, default=None)
    .choice("visibility", [item.value for item in PostVisibility], default=None)
    .ignore_unknown()
)

react_schema = Schema().choice("type", ["like", "unlike", "toggle"], default="like").ignore_unknown()


def _viewer():  # type: ignore[no-untyped-def]
    return getattr(g, "current_user", None)


# ---------------------------------------------------------------------------
# Feed and discovery
# ---------------------------------------------------------------------------


@bp.get("/feed")
@auth_optional
def feed():
    """Main feed.

    ``mode`` selects the ordering: ``for_you`` (the default) ranks by the
    viewer's interests and shuffles within each score band, ``shuffled`` is
    random with no ranking at all, ``latest`` is strictly chronological and
    ``following`` restricts the feed to accounts the viewer follows.
    """
    enforce("global")
    mode = (request.args.get("mode") or post_service.DEFAULT_FEED_MODE).strip().lower()
    page = post_service.get_feed(
        _viewer(), mode=mode, cursor=request.args.get("cursor"), limit=request.args.get("limit")
    )
    return ok(page.items, meta=page.to_meta({"mode": mode}))


@bp.get("/videos")
@auth_optional
def video_feed():
    """Vertical feed: one post per screen, filling it with one attachment.

    Separate from ``/feed`` rather than a ``mode`` on it, because the two are not
    the same list in a different order - this one takes a single attachment per
    post and shows nothing else.

    ``kind`` picks video (the default), photo, or both.
    """
    enforce("global")
    page = post_service.get_video_feed(
        _viewer(),
        cursor=request.args.get("cursor"),
        limit=request.args.get("limit"),
        kind=(request.args.get("kind") or "video").strip().lower(),
    )
    return ok(page.items, meta=page.to_meta({"mode": "videos"}))


@bp.get("/posts/search")
@auth_optional
def search():
    from ..services.search_service import search_posts

    enforce("search:query")
    page = search_posts(
        _viewer(),
        request.args.get("q") or "",
        limit=request.args.get("limit", 15),
        cursor=request.args.get("cursor"),
    )
    return ok(page.items, meta=page.to_meta())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@bp.get("/posts/<public_id>")
@auth_optional
def get_post(public_id: str):
    post = post_service.get_post(public_id, _viewer())
    viewer = _viewer()
    payload = post.to_dict(viewer)
    payload["spans"] = post_service.rich_body(post)
    payload["can_edit"] = bool(viewer and (viewer.id == post.author_id or viewer.is_moderator))
    payload["can_delete"] = payload["can_edit"]
    payload["can_report"] = bool(viewer and viewer.id != post.author_id)
    post_service.record_post_view(post)
    return ok({"post": payload})


@bp.post("/posts")
@auth_required
def create_post():
    verify_csrf()
    enforce("post:create")
    data = create_schema(request.get_json(silent=True) or {})

    placeholders, alt_texts = _resolve_media(g.current_user, data.get("media") or [])
    post = post_service.create_post(
        g.current_user,
        data.get("body") or "",
        visibility=data["visibility"],
        media=placeholders,
        alt_texts=alt_texts,
    )
    payload = post.to_dict(g.current_user)
    if post.status == "under_review":
        return created(
            {
                "post": payload,
                "status": "under_review",
                "message": "Публикация отправлена на проверку модератора.",
            }
        )
    _broadcast("post.new", payload)
    return created({"post": payload})


@bp.patch("/posts/<public_id>")
@auth_required
def update_post(public_id: str):
    verify_csrf()
    enforce("post:update")
    post = post_service.get_post(public_id, g.current_user)
    data = update_schema(request.get_json(silent=True) or {})
    updated = post_service.update_post(post, g.current_user, data)
    return ok({"post": updated.to_dict(g.current_user)})


@bp.delete("/posts/<public_id>")
@auth_required
def delete_post(public_id: str):
    verify_csrf()
    enforce("post:delete")
    post = post_service.get_post(public_id, g.current_user)
    post_service.delete_post(post, g.current_user)
    return no_content()


@bp.post("/posts/<public_id>/restore")
@auth_required
def restore_post(public_id: str):
    verify_csrf()
    enforce("post:update")
    # A deleted post is invisible, so the ordinary lookup would 404 here - and
    # making a deletion irreversible by accident is exactly what this endpoint
    # exists to prevent.
    post = post_service.get_post(public_id, g.current_user, include_hidden=True)
    restored = post_service.restore_post(post, g.current_user)
    return ok({"post": restored.to_dict(g.current_user)})


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


@bp.post("/posts/<public_id>/views")
@auth_optional
def record_view(public_id: str):
    """Count one view of a post.

    Separate from opening the post: the vertical feed shows a clip without anyone
    visiting its page, and a view count that only moves when the page is opened
    reports zero plays for a video that has been watched fifty times.

    Buffer-only, so this is cheap and takes no lock. It is also the endpoint a
    client is most tempted to call in a loop, so the per-account rate limit is
    tight and a rejected call is a normal outcome the client should ignore.
    """
    enforce("post:view")
    post = post_service.get_post(public_id, _viewer())
    post_service.record_post_view(post, viewer=g.current_user)
    return no_content()


@bp.post("/posts/<public_id>/reactions")
@auth_required
def react(public_id: str):
    """Set the caller's reaction to a post.

    ``like`` and ``unlike`` are idempotent, so a client that retries after a
    dropped response lands on the state it asked for rather than flipping it.
    ``toggle`` is the explicit flip, for a heart button whose click means "the
    opposite of what I have".
    """
    verify_csrf()
    enforce("reaction:write")
    post = post_service.get_post(public_id, g.current_user)
    data = react_schema(request.get_json(silent=True) or {})

    if data["type"] == "toggle":
        liked, count = post_service.toggle_reaction(post, g.current_user)
    else:
        liked, count = post_service.set_reaction(post, g.current_user, liked=data["type"] == "like")

    if liked:
        _enqueue_reaction_notification(post)
    return ok({"liked": liked, "likes_count": count})


@bp.get("/posts/<public_id>/reactions")
@auth_optional
def list_reactions(public_id: str):
    post = post_service.get_post(public_id, _viewer())
    return ok({"reactions": post_service.list_reactions(post, _viewer())})


# ---------------------------------------------------------------------------
# Media attachment
# ---------------------------------------------------------------------------


@bp.post("/posts/<public_id>/media")
@auth_required
def attach_media(public_id: str):
    """Attach images uploaded earlier but not yet bound to a post."""
    verify_csrf()
    enforce("upload:image")
    post = post_service.get_post(public_id, g.current_user)
    data = request.get_json(silent=True) or {}
    placeholders, alt_texts = _resolve_media(g.current_user, data.get("media") or [], _require_nonempty=True)
    post_service.claim_media(post, g.current_user, placeholders, alt_texts=alt_texts)
    return created({"post": post.to_dict(g.current_user)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_media(
    user,
    raw: list,
    *,
    _require_nonempty: bool = False,
) -> tuple[list, dict[str, str]]:
    """Resolve client-supplied references into trusted, owned upload rows.

    The client sends a storage key; the server looks up what was actually
    written at upload time and filters on ownership. Nothing about the URL,
    dimensions or MIME type is taken from the request, so a crafted payload
    cannot make a post display an arbitrary or mislabelled image.
    """
    if not raw:
        if _require_nonempty:
            raise ValidationError("Укажите файлы для прикрепления.", code="media_required")
        return [], {}

    keys: list[str] = []
    alt_texts: dict[str, str] = {}
    for item in raw:
        if isinstance(item, str):
            key = item
        elif isinstance(item, dict):
            key = str(item.get("storage_key") or item.get("id") or "")
            alt = str(item.get("alt_text") or "").strip()
            if alt:
                alt_texts[key] = alt[:240]
        else:
            key = ""
        if key:
            keys.append(key)

    if not keys:
        if _require_nonempty:
            raise ValidationError("Укажите корректные файлы для прикрепления.", code="media_invalid")
        return [], {}

    placeholders = post_service.unclaimed_media_for(user, keys)
    if not placeholders:
        if _require_nonempty:
            raise ValidationError("Файлы не найдены или уже прикреплены к другой публикации.", code="media_not_found")
        return [], alt_texts
    return placeholders, alt_texts


def _broadcast(event: str, payload: dict) -> None:
    from ..realtime.manager import emit_global

    try:
        emit_global(event, payload)
    except Exception:  # pragma: no cover - realtime is best-effort
        pass


def _enqueue_reaction_notification(post) -> None:  # type: ignore[no-untyped-def]
    """Tell the author someone liked their post. Never blocks the request."""
    from ..services.notification_service import enqueue_reaction_notification
    from ..tasks.dispatch import enqueue

    enqueue(enqueue_reaction_notification, post.id, g.current_user.id)


__all__ = ["bp"]

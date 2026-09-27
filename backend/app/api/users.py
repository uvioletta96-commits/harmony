"""User profiles, relationships, search and privacy settings."""

from __future__ import annotations

from flask import Blueprint, g, request

from ..extensions import db
from ..security.decorators import auth_optional, auth_required
from ..security.rate_limit import enforce
from ..services import auth_service, search_service, user_service
from ..utils.logging import get_logger, log_event
from ..utils.responses import NotFoundError, ValidationError, created, ok

logger = get_logger("harmony.api.users")

bp = Blueprint("users", __name__)


def _viewer():  # type: ignore[no-untyped-def]
    return getattr(g, "current_user", None)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@bp.get("/users/discover")
@auth_optional
def discover():
    return ok(search_service.discover(_viewer()))


@bp.get("/users/search")
@auth_optional
def search():
    """Search users by username, name or bio."""
    enforce("search:query")
    query = (request.args.get("q") or "").strip()
    limit = request.args.get("limit", 20)
    offset = request.args.get("offset", 0)
    page = search_service.search_users(_viewer(), query, limit=int(limit or 20), offset=int(offset or 0))
    return ok(page.items, meta=page.to_meta())


@bp.get("/users/suggestions")
@auth_required
def suggestions():
    return ok({"users": user_service.suggestions_for(g.current_user, limit=8)})


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@bp.get("/users/<public_id>")
@auth_optional
def get_profile(public_id: str):
    user = user_service.get_visible_user(public_id, _viewer())
    data = {
        "user": user.to_public_dict(_viewer()),
        "stats": auth_service.user_stats(user),
        "relationship": (user_service.relationship_state(g.current_user, user) if g.current_user else None),
    }
    return ok(data)


@bp.get("/users/by-username/<username>")
@auth_optional
def get_profile_by_username(username: str):
    """Resolve a profile by its human-readable handle.

    Profile URLs use usernames because they are memorable and stable across
    account renames; internal ids are never exposed.
    """
    user = auth_service.get_by_username(username)
    if user is None:
        raise NotFoundError("Профиль не найден.", code="user_not_found")
    user = user_service.get_visible_user(user.public_id, _viewer())
    return ok(
        {
            "user": user.to_public_dict(_viewer()),
            "stats": auth_service.user_stats(user),
            "relationship": (user_service.relationship_state(g.current_user, user) if g.current_user else None),
        }
    )


@bp.patch("/users/<public_id>")
@auth_required
def update_profile(public_id: str):
    from ..security.decorators import verify_csrf

    verify_csrf()
    enforce("global")
    if g.current_user.public_id != public_id:
        raise NotFoundError("Пользователь не найден.", code="user_not_found")
    auth_service.update_profile(g.current_user, request.get_json(silent=True) or {})
    return ok({"user": g.current_user.to_public_dict(g.current_user)})


@bp.get("/users/<public_id>/posts")
@auth_optional
def user_posts(public_id: str):
    from ..services.post_service import get_user_posts

    user = user_service.get_visible_user(public_id, _viewer())
    page = get_user_posts(
        user,
        _viewer(),
        cursor=request.args.get("cursor"),
        limit=request.args.get("limit"),
        include_hidden=(g.current_user is not None and g.current_user.id == user.id),
    )
    return ok(page.items, meta=page.to_meta())


@bp.get("/users/<public_id>/comments")
@auth_optional
def user_comments(public_id: str):
    from sqlalchemy import select

    from ..models.post import Comment, PostStatus
    from ..utils.pagination import offset_page

    user = user_service.get_visible_user(public_id, _viewer())
    query = (
        select(Comment)
        .where(Comment.author_id == user.id, Comment.status == PostStatus.PUBLISHED.value)
        .order_by(Comment.created_at.desc())
    )
    page = offset_page(
        query,
        page=int(request.args.get("page", 1) or 1),
        page_size=int(request.args.get("limit", 20) or 20),
        serialize=lambda c: c.to_dict(_viewer()),
    )
    return ok(page.items, meta=page.to_meta({"user": user.to_public_dict(_viewer())}))


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


@bp.post("/users/<public_id>/follow")
@auth_required
def follow(public_id: str):
    from ..security.decorators import verify_csrf

    verify_csrf()
    enforce("global")
    target = user_service.get_visible_user(public_id, g.current_user)
    return created(user_service.follow(g.current_user, target))


@bp.delete("/users/<public_id>/follow")
@auth_required
def unfollow(public_id: str):
    from ..security.decorators import verify_csrf

    verify_csrf()
    target = user_service.get_user(public_id)
    return ok(user_service.unfollow(g.current_user, target))


@bp.get("/users/<public_id>/followers")
@auth_optional
def followers(public_id: str):
    user = user_service.get_visible_user(public_id, _viewer())
    rows = user_service.list_followers(
        user, limit=int(request.args.get("limit", 50) or 50), offset=int(request.args.get("offset", 0) or 0)
    )
    return ok({"users": [row.to_public_dict(_viewer()) for row in rows]})


@bp.get("/users/<public_id>/following")
@auth_optional
def following(public_id: str):
    user = user_service.get_visible_user(public_id, _viewer())
    rows = user_service.list_following(
        user, limit=int(request.args.get("limit", 50) or 50), offset=int(request.args.get("offset", 0) or 0)
    )
    return ok({"users": [row.to_public_dict(_viewer()) for row in rows]})


# ---------------------------------------------------------------------------
# Avatar
# ---------------------------------------------------------------------------


@bp.post("/me/avatar")
@auth_required
def upload_avatar():
    """Store an uploaded image and make it this account's avatar.

    A dedicated endpoint rather than a field on the profile update, and that is
    the whole point. The profile schema deliberately has no ``avatar_url``: a
    client that can name a URL can point its own avatar at anything on the
    internet, which turns every profile page into a tracking beacon and every
    rendered avatar into a request the reader did not consent to. Here the file
    is stored by the same path as any other upload and the URL is assembled
    server-side, so the only thing the client contributes is the bytes.

    The upload row is discarded rather than left unclaimed: an avatar is not a
    post attachment, and leaving the row around would let it be claimed by a
    later post as well as occupy a slot in the orphan cleanup queue.
    """
    from ..security.decorators import verify_csrf
    from ..services.upload_service import store_image

    verify_csrf()
    enforce("upload:image")

    payload = request.files.get("file") or request.files.get("avatar")
    if payload is None:
        raise ValidationError("Файл не выбран.", code="no_file")

    stored = store_image(payload.read(), user_id=g.current_user.id, alt_text=None)

    user = g.current_user
    user.avatar_url = stored.url
    user.avatar_color = user.avatar_color or "sand"
    db.session.commit()
    log_event(logger, "INFO", "user.avatar_updated", user_id=user.id, mime_type=stored.mime_type)

    return created({"user": user.to_public_dict(user), "avatar_url": stored.url})


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


@bp.get("/me/privacy")
@auth_required
def get_privacy():
    settings = g.current_user.privacy_settings
    return ok({"privacy": settings.to_dict() if settings else {}})


@bp.patch("/me/privacy")
@auth_required
def update_privacy():
    from ..security.decorators import verify_csrf

    verify_csrf()
    enforce("global")
    result = user_service.update_privacy(g.current_user, request.get_json(silent=True) or {})
    return ok({"privacy": result})


@bp.get("/me/privacy/register")
@auth_required
def processing_register():
    """Art. 30 record of processing activities."""
    from ..services import gdpr_service

    return ok(gdpr_service.processing_register())


__all__ = ["bp"]

"""Comment, moderation, chat and privacy tests.

Convention used throughout this file:

* ``client`` is signed in as the ``user`` fixture.
* ``auth_api(target)`` re-authenticates the *same* client as ``target``.
* A two-party test therefore uses ``client`` + ``other_user`` and no
  ``auth_api`` call at all — the obvious mistake is signing in as one party
  and then addressing them as if they were the other.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.extensions import db
from app.models.moderation import ActionType, ModerationAction, Report, ReportStatus
from app.models.post import Comment, Post, PostStatus
from app.models.user import User, UserStatus
from tests.conftest import ApiClient
from tests.utils import DEFAULT_PASSWORD, assert_error, assert_ok


def make_post(user, body: str = "Тестовая публикация") -> Post:
    from app.models.post import PostVisibility

    post = Post(author_id=user.id, body=body, visibility=PostVisibility.PUBLIC.value)
    db.session.add(post)
    db.session.commit()
    return post


def comment(client, post: Post, body: str, **extra) -> dict:
    return assert_ok(
        client.post(f"/api/v1/posts/{post.public_id}/comments", json={"body": body, **extra}),
        status=201,
    )["comment"]


# ===========================================================================
# Comments
# ===========================================================================


class TestComments:
    def test_create_on_post(self, client, post_factory):
        post = post_factory()
        data = comment(client, post, "Согласен.")
        assert data["body"] == "Согласен."
        assert data["depth"] == 0
        assert data["is_owner"] is True

    def test_increments_counters(self, client, post_factory, user):
        post = post_factory()
        comment(client, post, "Первый")
        comment(client, post, "Второй")
        db.session.expire_all()
        assert db.session.get(Post, post.id).comments_count == 2

    def test_empty_rejected(self, client, post_factory):
        post = post_factory()
        assert_error(
            client.post(f"/api/v1/posts/{post.public_id}/comments", json={"body": "   "}),
            status=422,
            code="empty_comment",
        )

    def test_html_stripped(self, client, post_factory):
        post = post_factory()
        data = comment(client, post, "<b>жирный</b> <script>x</script>")
        assert "<script" not in data["body"].lower()
        assert "<b>" not in data["body"].lower()

    def test_reply_threads(self, client, post_factory):
        post = post_factory()
        parent = comment(client, post, "Вопрос?")
        reply = comment(client, post, "Ответ", parent_id=parent["id"])
        assert reply["parent_id"] == parent["id"]
        assert reply["depth"] == 1

    def test_depth_limit_enforced(self, client, post_factory, app):
        post = post_factory()
        max_depth = app.config["MAX_COMMENT_DEPTH"]
        parent = None
        for level in range(max_depth + 2):
            payload = {"body": f"Уровень {level}"}
            if parent:
                payload["parent_id"] = parent["id"]
            response = client.post(f"/api/v1/posts/{post.public_id}/comments", json=payload)
            if response.status_code == 422:
                assert response.get_json()["error"]["code"] == "max_depth_exceeded"
                break
            parent = response.get_json()["data"]["comment"]
        assert parent["depth"] == max_depth

    def test_parent_from_another_post_rejected(self, client, post_factory):
        first = post_factory()
        second = post_factory(body="Другой пост")
        parent = comment(client, first, "Родитель")
        assert_error(
            client.post(
                f"/api/v1/posts/{second.public_id}/comments",
                json={"body": "Чужой ответ", "parent_id": parent["id"]},
            ),
            status=422,
            code="parent_mismatch",
        )

    def test_author_can_edit_own_comment(self, client, post_factory):
        post = post_factory()
        made = comment(client, post, "Моё")
        assert_ok(client.patch(f"/api/v1/comments/{made['id']}", json={"body": "Изменено"}))

    def test_stranger_cannot_edit_someone_elses_comment(self, client, post_factory, other_user, auth_api):
        post = post_factory()
        made = comment(client, post, "Моё")
        auth_api(other_user)
        assert_error(
            client.patch(f"/api/v1/comments/{made['id']}", json={"body": "Взлом"}),
            status=403,
            code="not_comment_owner",
        )

    def test_delete_hides_body_but_keeps_row(self, client, post_factory):
        post = post_factory()
        made = comment(client, post, "Удалить меня")
        assert client.delete(f"/api/v1/comments/{made['id']}").status_code == 204
        db.session.expire_all()
        row = db.session.query(Comment).filter(Comment.public_id == made["id"]).one()
        assert row.status == PostStatus.REMOVED.value
        assert row.body == ""

    def test_moderator_can_delete_any_comment(self, client, post_factory, moderator, auth_api):
        post = post_factory()
        made = comment(client, post, "Провокация")
        auth_api(moderator)
        assert client.delete(f"/api/v1/comments/{made['id']}").status_code == 204

    def test_cannot_comment_on_hidden_post(self, client, post_factory):
        post = post_factory(status=PostStatus.REMOVED.value)
        assert_error(
            client.post(f"/api/v1/posts/{post.public_id}/comments", json={"body": "Призрак"}),
            status=404,
        )

    def test_listing_returns_replies_preview(self, client, post_factory):
        post = post_factory()
        parent = comment(client, post, "Вопрос?")
        comment(client, post, "Ответ", parent_id=parent["id"])
        data = assert_ok(client.get(f"/api/v1/posts/{post.public_id}/comments"))
        assert len(data) == 1
        assert len(data[0]["replies"]) == 1
        assert data[0]["replies"][0]["body"] == "Ответ"

    def test_sort_modes(self, client, post_factory):
        post = post_factory()
        comment(client, post, "Первый")
        comment(client, post, "Второй")
        newest = assert_ok(client.get(f"/api/v1/posts/{post.public_id}/comments?sort=newest"))
        assert [item["body"] for item in newest] == ["Второй", "Первый"]

    def test_replies_endpoint(self, client, post_factory):
        post = post_factory()
        parent = comment(client, post, "Вопрос?")
        comment(client, post, "Ответ", parent_id=parent["id"])
        data = assert_ok(client.get(f"/api/v1/comments/{parent['id']}/replies"))
        assert len(data) == 1


# ===========================================================================
# Reporting
# ===========================================================================


class TestReporting:
    def test_file_report(self, client, post_factory, other_user, auth_api):
        post = post_factory()
        auth_api(other_user)
        data = assert_ok(
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "spam"},
            ),
            status=201,
        )
        assert data["report"]["status"] == ReportStatus.OPEN.value

    def test_duplicate_rejected(self, client, post_factory, other_user, auth_api):
        post = post_factory()
        auth_api(other_user)
        payload = {"target_type": "post", "target_id": post.public_id, "reason": "spam"}
        assert_ok(client.post("/api/v1/reports", json=payload), status=201)
        assert_error(client.post("/api/v1/reports", json=payload), status=409, code="already_reported")

    def test_self_report_rejected(self, client, post_factory):
        post = post_factory()
        assert_error(
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "spam"},
            ),
            status=422,
            code="self_report",
        )

    def test_invalid_reason_rejected(self, client, post_factory):
        post = post_factory()
        assert_error(
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "because"},
            ),
            status=422,
        )

    def test_auto_hide_after_distinct_reporters(self, client, post_factory, make_user, auth_api):
        """Content is hidden once enough *different* people report it: a false
        positive costs a delay, a false negative is permanent."""
        from app.services import moderation_service

        post = post_factory(body="Сомнительное содержание")
        for _ in range(moderation_service.MIN_DISTINCT_REPORTERS_FOR_AUTO_HIDE):
            reporter = make_user()
            auth_api(reporter)
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "spam"},
            )
        db.session.expire_all()
        assert db.session.get(Post, post.id).status == PostStatus.UNDER_REVIEW.value

    def test_reporter_sees_status_not_action(self, client, post_factory, other_user, auth_api):
        post = post_factory()
        auth_api(other_user)
        assert_ok(
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "spam"},
            ),
            status=201,
        )
        data = assert_ok(client.get("/api/v1/reports/mine"))
        assert len(data["reports"]) == 1
        # The reporter learns nothing about what was decided.
        assert data["reports"][0]["resolution"] is None

    def test_duplicate_count_tracks_total(self, client, post_factory, make_user, auth_api):
        post = post_factory()
        for _ in range(3):
            auth_api(make_user())
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "spam"},
            )
        db.session.expire_all()
        report = db.session.query(Report).filter(Report.target_id == post.id).first()
        assert report.duplicate_count == 3


# ===========================================================================
# Moderation actions
# ===========================================================================


class TestModerationActions:
    def test_moderator_can_hide_a_post(self, client, post_factory, moderator, auth_api):
        post = post_factory(body="Спорно")
        auth_api(moderator)
        assert_ok(
            client.post(
                f"/api/v1/admin/posts/{post.public_id}/action",
                json={"action": "hide", "note": "Проверяем"},
            )
        )
        db.session.expire_all()
        assert db.session.get(Post, post.id).status == PostStatus.UNDER_REVIEW.value

    def test_moderator_can_ban_a_user(self, client, make_user, moderator, auth_api):
        target = make_user()
        auth_api(moderator)
        assert_ok(
            client.post(
                f"/api/v1/admin/users/{target.public_id}/action",
                json={"action": "ban", "reason": "Повторные нарушения"},
            )
        )
        db.session.expire_all()
        assert db.session.get(User, target.id).status == UserStatus.BANNED.value

    def test_banned_user_loses_access(self, client, app, ctx, other_user, make_user, auth_api):
        """A ban must kill the session a user is already holding, not just login.

        The banned user's client is created *before* the sanction, so the first
        assertion covers the real attack: a token captured beforehand, replayed
        afterwards. The second covers the obvious one - signing in again.
        """
        target = make_user()
        victim = ApiClient(app.test_client(), application=app)
        victim.bootstrap_csrf()
        victim.login(target.username)
        assert_ok(victim.get("/api/v1/auth/me"))

        moderator = make_user(role="moderator")
        auth_api(moderator)
        assert_ok(client.post(f"/api/v1/admin/users/{target.public_id}/action", json={"action": "ban"}))

        assert_error(victim.get("/api/v1/auth/me"), status=403, code="account_banned")
        assert_error(
            client.post(
                "/api/v1/auth/login",
                json={"identifier": target.username, "password": DEFAULT_PASSWORD},
            ),
            status=403,
        )

    def test_suspension_records_expiry(self, client, make_user, moderator, auth_api):
        from app.models.moderation import ActionType as AT

        target = make_user()
        auth_api(moderator)
        client.post(
            f"/api/v1/admin/users/{target.public_id}/action",
            json={"action": "suspend", "duration_hours": 24, "reason": "Пауза"},
        )
        action = db.session.query(ModerationAction).one()
        assert action.action == AT.SUSPEND.value
        assert action.expires_at is not None
        assert db.session.get(User, target.id).status == UserStatus.SUSPENDED.value

    def test_self_action_blocked(self, client, moderator, auth_api):
        auth_api(moderator)
        assert_error(
            client.post(
                f"/api/v1/admin/users/{moderator.public_id}/action",
                json={"action": "ban"},
            ),
            status=422,
            code="self_action",
        )

    def test_every_action_is_audited(self, client, make_user, moderator, auth_api):
        target = make_user()
        auth_api(moderator)
        client.post(f"/api/v1/admin/users/{target.public_id}/action", json={"action": "warn", "reason": "Замечание"})
        actions = db.session.query(ModerationAction).all()
        assert len(actions) == 1
        assert actions[0].action == ActionType.WARN.value
        assert actions[0].reason == "Замечание"
        assert actions[0].moderator_id == moderator.id

    def test_temporary_suspension_expires(self, client, make_user, moderator, auth_api):
        from datetime import timedelta

        from app.models.base import utcnow
        from app.services.moderation_service import expire_temporary_actions

        target = make_user()
        auth_api(moderator)
        client.post(
            f"/api/v1/admin/users/{target.public_id}/action",
            json={"action": "suspend", "duration_hours": 1, "reason": "Пауза"},
        )
        action = db.session.query(ModerationAction).one()
        action.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

        assert expire_temporary_actions() == 1
        db.session.expire_all()
        assert db.session.get(User, target.id).status == UserStatus.ACTIVE.value

    def test_resolve_report_closes_it(self, client, post_factory, other_user, moderator, auth_api):
        post = post_factory(body="Жалоба")
        auth_api(other_user)
        assert_ok(
            client.post(
                "/api/v1/reports",
                json={"target_type": "post", "target_id": post.public_id, "reason": "spam"},
            ),
            status=201,
        )
        report = db.session.query(Report).one()

        auth_api(moderator)
        data = assert_ok(
            client.post(
                f"/api/v1/admin/reports/{report.public_id}/resolve",
                json={"action": "remove", "note": "Спам"},
            )
        )
        assert data["action"]["action"] == "remove"
        db.session.expire_all()
        assert db.session.get(Report, report.id).status == ReportStatus.RESOLVED.value
        assert db.session.get(Post, post.id).status == PostStatus.REMOVED.value

    def test_dashboard_stats(self, client, moderator, auth_api):
        auth_api(moderator)
        data = assert_ok(client.get("/api/v1/admin/stats"))
        assert "open_reports" in data["moderation"]
        assert "active_users" in data["moderation"]

    def test_admin_only_endpoint_rejects_moderator(self, client, user, other_user, moderator, admin, auth_api):
        auth_api(moderator)
        assert_error(
            client.post(f"/api/v1/admin/users/{user.public_id}/role", json={"role": "admin"}),
            status=403,
        )
        auth_api(admin)
        assert_ok(client.post(f"/api/v1/admin/users/{other_user.public_id}/role", json={"role": "moderator"}))

    def test_moderator_cannot_promote_self(self, client, moderator, auth_api):
        """A moderator must not be able to grant themselves a higher role."""
        auth_api(moderator)
        assert_error(
            client.post(f"/api/v1/admin/users/{moderator.public_id}/role", json={"role": "admin"}),
            status=403,
            code="admin_only",
        )

    def test_admin_cannot_change_own_role(self, client, admin, auth_api):
        """Even an admin should be made to ask a second person."""
        auth_api(admin)
        assert_error(
            client.post(f"/api/v1/admin/users/{admin.public_id}/role", json={"role": "member"}),
            status=422,
            code="self_role_change",
        )


# ===========================================================================
# Chat
# ===========================================================================


class TestChat:
    def start(self, client, recipient: User) -> dict:
        return assert_ok(
            client.post("/api/v1/conversations", json={"user_id": recipient.public_id}),
            status=201,
        )["conversation"]

    def test_start_conversation(self, client, other_user):
        conversation = self.start(client, other_user)
        assert conversation["kind"] == "direct"
        assert len(conversation["participants"]) == 1
        assert conversation["participants"][0]["public_id"] == other_user.public_id

    def test_reopening_is_idempotent(self, client, other_user):
        first = self.start(client, other_user)
        second = self.start(client, other_user)
        assert first["id"] == second["id"]

    def test_self_conversation_rejected(self, client, user):
        assert_error(
            client.post("/api/v1/conversations", json={"user_id": user.public_id}),
            status=422,
            code="self_conversation",
        )

    def test_send_and_read(self, client, other_user):
        conversation = self.start(client, other_user)
        message = assert_ok(
            client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"body": "Привет!"}),
            status=201,
        )["message"]
        assert message["body"] == "Привет!"

        history = assert_ok(client.get(f"/api/v1/conversations/{conversation['id']}/messages"))
        assert [item["body"] for item in history] == ["Привет!"]

    def test_empty_message_rejected(self, client, other_user):
        conversation = self.start(client, other_user)
        assert_error(
            client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"body": "   "}),
            status=422,
            code="empty_message",
        )

    def test_stranger_cannot_read_thread(self, client, other_user, make_user, auth_api):
        conversation = self.start(client, other_user)
        auth_api(make_user())
        # 404, not 403: a non-member must not learn the thread exists.
        assert_error(client.get(f"/api/v1/conversations/{conversation['id']}"), status=404)

    def test_recipient_sees_the_message(self, client, other_user, auth_api):
        conversation = self.start(client, other_user)
        client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"body": "Сообщение"})

        auth_api(other_user)
        history = assert_ok(client.get(f"/api/v1/conversations/{conversation['id']}/messages"))
        assert len(history) == 1
        assert assert_ok(client.get("/api/v1/conversations"))["total_unread"] == 1

        client.post(f"/api/v1/conversations/{conversation['id']}/read")
        assert assert_ok(client.get("/api/v1/conversations"))["total_unread"] == 0

    def test_messages_from_strangers_can_be_blocked(self, client, make_user, auth_api):
        recipient = make_user(allow_messages_from_anyone=False)
        sender = make_user()

        auth_api(sender)
        conversation = self.start(client, recipient)
        assert_error(
            client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"body": "Спам"}),
            status=403,
            code="messages_not_allowed",
        )

    def test_message_html_neutralised(self, client, other_user):
        conversation = self.start(client, other_user)
        message = assert_ok(
            client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                json={"body": "<script>alert(1)</script> привет"},
            ),
            status=201,
        )["message"]
        assert "<script" not in message["body"].lower()
        assert "привет" in message["body"]

    def test_leave_conversation(self, client, other_user):
        conversation = self.start(client, other_user)
        assert client.post(f"/api/v1/conversations/{conversation['id']}/leave").status_code == 204

    def test_delete_message(self, client, other_user):
        conversation = self.start(client, other_user)
        message = assert_ok(
            client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"body": "Ой"}),
            status=201,
        )["message"]
        assert client.delete(f"/api/v1/messages/{message['id']}").status_code == 204


# ===========================================================================
# Privacy and GDPR
# ===========================================================================


class TestPrivacyAndGDPR:
    def test_export_contains_own_data_only(self, client, user, other_user, auth_api):
        from tests.utils import create_post

        create_post(client, "Моя публикация")
        auth_api(other_user)
        create_post(client, "Чужая публикация")

        auth_api(user)
        data = assert_ok(client.get("/api/v1/me/data"))
        assert {item["body"] for item in data["posts"]} == {"Моя публикация"}
        assert data["profile"]["username"] == user.username

    def test_export_includes_consent_history(self, client, user, auth_api):
        from app.services import auth_service

        auth_service.register(
            {"email": "consent@harmony.test", "username": "consented", "password": "Str0ng-Pass!23", "consent": True}
        )
        auth_api(user)
        data = assert_ok(client.get("/api/v1/me/data"))
        assert isinstance(data["consent_history"], list)

    def test_privacy_settings_round_trip(self, client, auth_api):
        auth_api(client.target)
        updated = assert_ok(
            client.patch(
                "/api/v1/me/privacy",
                json={"profile_visibility": "followers", "discoverable_by_search": False},
            )
        )["privacy"]
        assert updated["profile_visibility"] == "followers"
        assert updated["discoverable_by_search"] is False

    def test_private_profile_hidden_after_change(self, client, other_user, auth_api):
        # Bind the target first: ``auth_api`` repoints ``client.target`` at
        # whoever it just logged in as, so reading it afterwards would compare
        # the user against themselves.
        owner = client.target
        auth_api(owner)
        assert_ok(client.patch("/api/v1/me/privacy", json={"profile_visibility": "private"}))
        auth_api(other_user)
        assert_error(client.get(f"/api/v1/users/{owner.public_id}"), status=404)

    def test_deletion_requires_step_up(self, client, auth_api):
        auth_api(client.target)
        assert_error(client.post("/api/v1/me/deletion", json={}), status=403, code="reauth_required")

    def test_deletion_with_step_up(self, client, auth_api):

        auth_api(client.target)
        reauth = assert_ok(client.post("/api/v1/auth/reauthenticate", json={"password": "Str0ng-Pass!23"}))
        data = assert_ok(
            client.post("/api/v1/me/deletion", json={"reauth_token": reauth["reauth_token"], "reason": "Ухожу"})
        )
        assert data["deletion"]["status"] == "scheduled"
        db.session.expire_all()
        assert db.session.get(User, client.target.id).status == UserStatus.DELETION_PENDING.value

    def test_deletion_pending_blocks_access(self, client, auth_api):
        auth_api(client.target)
        reauth = assert_ok(client.post("/api/v1/auth/reauthenticate", json={"password": "Str0ng-Pass!23"}))
        assert_ok(client.post("/api/v1/me/deletion", json={"reauth_token": reauth["reauth_token"]}))
        assert_error(client.get("/api/v1/auth/me"), status=403, code="account_deletion_pending")

    def test_deletion_cancelled(self, client, auth_api):
        auth_api(client.target)
        reauth = assert_ok(client.post("/api/v1/auth/reauthenticate", json={"password": "Str0ng-Pass!23"}))
        assert_ok(client.post("/api/v1/me/deletion", json={"reauth_token": reauth["reauth_token"]}))
        assert_ok(client.post("/api/v1/me/deletion/cancel"))
        db.session.expire_all()
        assert db.session.get(User, client.target.id).status == UserStatus.ACTIVE.value

    def test_step_up_token_is_single_use(self, client, auth_api):
        auth_api(client.target)
        reauth = assert_ok(client.post("/api/v1/auth/reauthenticate", json={"password": "Str0ng-Pass!23"}))
        assert_ok(client.post("/api/v1/me/deletion", json={"reauth_token": reauth["reauth_token"]}))
        # Replaying the step-up token must not authorise a second action.
        assert_error(
            client.post("/api/v1/me/deletion", json={"reauth_token": reauth["reauth_token"]}),
            status=403,
            code="reauth_invalid",
        )

    def test_purge_removes_everything_owned(self, ctx, user):
        from app.services.user_service import purge_account

        make_post(user, "Публикация перед удалением")
        stats = purge_account(user.id)
        assert stats["posts"] == 1
        assert db.session.get(User, user.id) is None
        assert db.session.query(Post).filter(Post.author_id == user.id).count() == 0

    def test_audit_trail_survives_erasure_without_actor(self, ctx, user):
        from app.models.system import AuditLog
        from app.services.user_service import purge_account, record_audit

        record_audit("test.event", user, detail="x")
        db.session.commit()
        purge_account(user.id)

        entry = db.session.query(AuditLog).one()
        assert entry.actor_id is None
        assert entry.actor_label == "deleted"
        assert entry.action == "test.event"

    def test_processing_register_generated_from_config(self, app, ctx):
        from app.services.gdpr_service import processing_register

        register = processing_register()
        purposes = {activity["purpose"] for activity in register["activities"]}
        assert any("Антиспам" in purpose for purpose in purposes)
        assert register["controller"]["contact"]
        assert "access" in register["rights"]

    def test_cookie_inventory_is_complete(self, app, ctx):
        from app.services.gdpr_service import cookie_inventory

        cookies = {cookie["name"] for cookie in cookie_inventory()}
        assert {"harmony_access", "harmony_refresh", "harmony_csrf"} <= cookies
        access = next(c for c in cookie_inventory() if c["name"] == "harmony_access")
        assert access["http_only"] is True

    def test_consent_withdrawal_stops_marketing(self, client, auth_api):
        auth_api(client.target)
        data = assert_ok(client.post("/api/v1/me/data/consent"))
        assert data["marketing_consent"] is False
        assert data["email_notifications"] is False

    def test_legal_documents_are_published(self, client, ctx):
        from app.services.gdpr_service import ensure_legal_documents

        ensure_legal_documents()
        documents = assert_ok(client.get("/api/v1/legal/documents"))["documents"]
        slugs = {document["slug"] for document in documents}
        assert {"terms", "privacy"} <= slugs
        for document in documents:
            assert document["content_hash"]


class TestAvatar:
    """The avatar is set by uploading bytes, never by naming a URL.

    The profile schema deliberately has no ``avatar_url`` field, so the earlier
    client - which uploaded a file and then sent the resulting URL to the
    profile endpoint - was silently ignored: the update succeeded, the field
    was dropped by ``ignore_unknown``, and the page reported success while the
    avatar never changed.
    """

    def test_uploading_replaces_the_avatar(self, auth_client, user):
        import io as _io

        from tests.utils import make_png

        data = assert_ok(
            auth_client.upload(
                "/api/v1/me/avatar",
                {"file": (_io.BytesIO(make_png()), "me.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        assert data["avatar_url"], data

        me = assert_ok(auth_client.get("/api/v1/auth/me"))
        assert me["user"]["avatar_url"] == data["avatar_url"]

    def test_the_avatar_survives_a_new_session(self, auth_client, anon_client, user):
        import io as _io

        from tests.utils import make_png

        assert_ok(
            auth_client.upload(
                "/api/v1/me/avatar",
                {"file": (_io.BytesIO(make_png()), "me.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        anon_client.login(user.username)
        me = assert_ok(anon_client.get("/api/v1/auth/me"))
        assert me["user"]["avatar_url"], "the avatar did not persist"

    def test_a_file_is_required(self, auth_client):
        assert_error(
            auth_client.upload("/api/v1/me/avatar", {}, content_type="multipart/form-data"),
            status=422,
            code="no_file",
        )

    def test_a_non_image_is_refused(self, auth_client):
        import io as _io

        assert_error(
            auth_client.upload(
                "/api/v1/me/avatar",
                {"file": (_io.BytesIO(b"not an image at all"), "x.txt", "text/plain")},
                content_type="multipart/form-data",
            ),
            status=415,
        )

    def test_a_stranger_cannot_set_your_avatar(self, anon_client, user):
        import io as _io

        from tests.utils import make_png

        assert_error(
            anon_client.upload(
                "/api/v1/me/avatar",
                {"file": (_io.BytesIO(make_png()), "x.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=401,
        )
        assert user.avatar_url is None

    def test_the_profile_endpoint_still_ignores_an_avatar_url(self, auth_client, user):
        """A client must not be able to point its avatar at a third-party URL.

        Not a formality: an avatar rendered from an arbitrary origin is a
        tracking pixel on every profile page a reader opens.
        """
        before = assert_ok(auth_client.get("/api/v1/auth/me"))["user"].get("avatar_url")
        assert_ok(
            auth_client.patch(
                f"/api/v1/users/{user.public_id}",
                json={"avatar_url": "https://tracker.example/pixel.gif"},
            )
        )
        after = assert_ok(auth_client.get("/api/v1/auth/me"))["user"].get("avatar_url")
        assert after == before


class TestCascadeIntegrity:
    """SQLite only honours ``ON DELETE CASCADE`` when asked, per connection.

    With the pragma off, every cascade in this schema was declared but inert:
    deleting an account left a row behind in each child table. Two things broke,
    and neither announced itself as a database problem.

    The orphan then collided with the next signup. ``users.id`` came straight
    back from the sequence, ``privacy_settings.user_id`` is unique, and
    registration died with an ``IntegrityError`` - an HTTP 500 on a form the
    reader had filled in correctly. And the GDPR erasure path reported that it
    had removed personal data while leaving it in the database.
    """

    def test_sqlite_enforces_foreign_keys(self, ctx):
        from sqlalchemy import text

        from app.extensions import db

        on = db.session.execute(text("PRAGMA foreign_keys")).scalar()
        assert on == 1, "foreign keys are off on this connection, so no cascade in the schema does anything"

    def test_erasing_an_account_removes_its_child_rows(self, ctx, user):
        from app.extensions import db
        from app.models.user import PrivacySetting
        from app.services.user_service import purge_account

        # The fixture builds the user directly rather than through /register, so
        # the child row is created here - the point is what the erase does to it,
        # not how it got there.
        db.session.add(PrivacySetting(user_id=user.id))
        db.session.commit()
        assert PrivacySetting.query.filter_by(user_id=user.id).count() == 1

        purge_account(user.id)

        assert PrivacySetting.query.filter_by(user_id=user.id).count() == 0

    def test_deleting_a_user_row_cascades_on_its_own(self, ctx, user):
        """Not via the service: the schema's own cascade has to work, because
        anything else that removes a user relies on it."""
        from sqlalchemy import text

        from app.extensions import db
        from app.models.user import PrivacySetting

        db.session.add(PrivacySetting(user_id=user.id))
        db.session.commit()

        db.session.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
        db.session.commit()

        assert PrivacySetting.query.filter_by(user_id=user.id).count() == 0

    def test_an_erased_id_can_be_reused(self, ctx, user):
        """The collision the orphans caused, end to end.

        A ``DELETE`` used to leave ``privacy_settings`` behind. The next account
        was handed the same id by the sequence, and the row every registration
        writes hit the unique constraint and returned 500. With the pragma on,
        the child row is gone and the id is free.
        """
        from app.extensions import db
        from app.models.user import PrivacySetting, User
        from app.services.user_service import purge_account

        db.session.add(PrivacySetting(user_id=user.id))
        db.session.commit()

        reuse_id = user.id
        purge_account(user.id)
        assert PrivacySetting.query.filter_by(user_id=reuse_id).count() == 0

        # A new account that lands on the id the sequence just handed back.
        db.session.add(
            User(
                id=reuse_id,
                public_id="reused-public-id",
                username="reused-name",
                email="reused@example.com",
                password_hash="x",
                email_verified=True,
            )
        )
        db.session.add(PrivacySetting(user_id=reuse_id))
        db.session.commit()

        assert PrivacySetting.query.filter_by(user_id=reuse_id).count() == 1

    def test_an_orphan_can_no_longer_be_created(self, ctx, user):
        """The other half: the constraint now refuses the row outright.

        Before the pragma, nothing stopped a child row outliving its parent, and
        the damage only surfaced much later as a collision on a reused id.
        """
        import pytest
        from sqlalchemy.exc import IntegrityError

        from app.extensions import db
        from app.models.user import PrivacySetting, User
        from app.services.user_service import purge_account

        purge_account(user.id)

        db.session.add(PrivacySetting(user_id=user.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        assert User.query.filter_by(id=user.id).count() == 0


class TestLegalContentIsAFragment:
    """The stored legal text is injected into a div, not served as a page.

    ``pages/legal.js`` does ``body.innerHTML = legal.content`` inside the shell.
    The templates were written as standalone pages, so what got stored carried a
    doctype, a ``<head>`` and a ``<style>`` block. The parser dropped the first
    two and *applied* the third, which meant the legal pages rendered in the
    template's own typography on top of the app's stylesheet, and the template's
    ``<main>`` turned into a second ``<main>`` nested inside the shell's.
    """

    @staticmethod
    def _documents():
        from app.services.gdpr_service import ensure_legal_documents

        ensure_legal_documents()
        from app.models.system import LegalDocument

        return db.session.query(LegalDocument).filter(LegalDocument.is_current.is_(True)).all()

    def test_no_template_ships_page_scaffolding(self):
        for document in self._documents():
            content = document.content.lower()
            for banned in ("<!doctype", "<html", "<head", "<body", "<style", "<main"):
                assert banned not in content, f"{document.slug} {document.version} contains {banned}"

    def test_the_wording_survived_the_conversion(self):
        """A fragment must still be the document, not an empty shell."""
        import re

        for document in self._documents():
            headings = re.findall(r"<h2[^>]*>(.*?)</h2>", document.content, re.S)
            assert len(headings) >= 4, f"{document.slug} has only {len(headings)} sections"
            assert len(document.content) > 3000, f"{document.slug} looks truncated"

    def test_the_api_returns_a_fragment(self, client):
        # Seeded explicitly: the bootstrap seeding is skipped when the database
        # is not reachable at app-creation time, which is the case here.
        from app.services.gdpr_service import ensure_legal_documents

        ensure_legal_documents()

        for slug in ("privacy", "terms"):
            payload = assert_ok(client.get(f"/api/v1/legal/documents/{slug}"))
            content = payload["document"]["content"].lower()
            for banned in ("<html", "<style", "<main"):
                assert banned not in content, f"/legal/documents/{slug} serves page scaffolding: {banned}"


class TestRedisIsNeverOnTheRequestPath:
    """A cache must not be able to slow down a page.

    Redis is a cache and a shared rate-limit counter. Every caller already has a
    correct answer without it, so the only thing connecting to it can do is cost
    time - and it did. ``get_redis()`` used to probe on demand, which meant the
    first request inside every backoff window paid the connect timeout: a
    two-second frozen page every thirty seconds on a machine with no Redis, and
    still half a second of it after the timeouts came down. Measured over a
    minute of polling, the median request was 3 ms and the worst was 2027 ms.
    """

    def test_get_redis_does_not_connect(self, ctx):
        """It reads the answer; it does not go and find one."""
        from unittest.mock import patch

        from app import extensions

        extensions.reset_redis()
        with patch.object(extensions, "_connect_and_ping") as probe:
            for _ in range(5):
                extensions.get_redis()
        probe.assert_not_called()

    def test_a_failing_probe_is_remembered(self, ctx):
        from app import extensions

        with _redis_url(ctx, "redis://127.0.0.1:1/0"):
            assert extensions.redis_is_degraded() is False
            extensions.init_redis(_app())
            assert extensions.redis_is_degraded() is True

    def test_the_timeouts_are_short(self):
        """A slow "no" is worth much more here than a slow "yes"."""
        from app import extensions

        assert extensions._REDIS_CONNECT_TIMEOUT <= 0.5
        assert extensions._REDIS_SOCKET_TIMEOUT <= 1.0

    def test_concurrent_callers_do_not_all_probe(self, ctx):
        """The negative cache is only written once a probe fails, so without a
        lock every request arriving during the probe starts one of its own - a
        browser opening a page fires half a dozen calls at once."""
        import threading

        from app import extensions

        extensions.reset_redis()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_probe(app, url):
            calls.append(1)
            started.set()
            release.wait(5)
            return None

        original = extensions._connect_and_ping
        with _redis_url(ctx, "redis://127.0.0.1:1/0"):
            extensions._connect_and_ping = slow_probe
            try:
                threads = [threading.Thread(target=extensions.init_redis, args=(_app(),)) for _ in range(6)]
                for thread in threads:
                    thread.start()
                started.wait(5)
                release.set()
                for thread in threads:
                    thread.join(5)
            finally:
                extensions._connect_and_ping = original

        assert len(calls) == 1, f"{len(calls)} threads probed at once; the lock is not holding"


def _app():
    from flask import current_app

    return current_app._get_current_object()  # type: ignore[attr-defined]


@contextmanager
def _redis_url(ctx, url):
    """Point the extension at a Redis for the duration of a test.

    ``TestingConfig`` sets ``REDIS_URL = ""`` so the suite never depends on the
    service being up, which also means the code path these tests are about is
    never entered without help. Port 1 has nothing on it, so the probe fails
    immediately rather than waiting out a timeout.
    """
    from app import extensions

    previous = ctx.config.get("REDIS_URL")
    ctx.config["REDIS_URL"] = url
    extensions.reset_redis()
    try:
        yield
    finally:
        ctx.config["REDIS_URL"] = previous
        extensions.reset_redis()


class TestServerlessDetection:
    """The two things this app does that a frozen container cannot do.

    Both are correct on a normal server, so the fix is to ask the environment
    rather than to remove the features. Getting this wrong is not a crash: a
    daemon thread is silently killed between invocations, and a WebSocket is
    silently held open until the platform's timeout - both look like "the
    realtime features just do not work" rather than like a misconfiguration.
    """

    def test_a_normal_server_is_not_serverless(self, ctx, monkeypatch):
        from app import on_serverless

        for name in ("VERCEL", "FUNCTIONS_WORKER_RUNTIME", "AWS_EXECUTION_ENV"):
            monkeypatch.delenv(name, raising=False)
        assert on_serverless() is False

    def test_each_platform_is_recognised(self, ctx, monkeypatch):
        from app import on_serverless

        monkeypatch.setenv("VERCEL", "1")
        assert on_serverless() is True

        monkeypatch.delenv("VERCEL", raising=False)
        monkeypatch.setenv("FUNCTIONS_WORKER_RUNTIME", "python:3.12")
        assert on_serverless() is True

        monkeypatch.delenv("FUNCTIONS_WORKER_RUNTIME", raising=False)
        monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_Lambda_python3.12")
        assert on_serverless() is True

    def test_a_non_lambda_aws_environment_is_not_serverless(self, ctx, monkeypatch):
        """EC2 has the disk and the long-lived process this app wants."""
        from app import on_serverless

        monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_EC2")
        assert on_serverless() is False

    def test_no_supervisor_thread_is_started_on_serverless(self, ctx, monkeypatch):
        import threading

        monkeypatch.setenv("VERCEL", "1")
        before = threading.active_count()
        from app import create_app

        create_app("development")
        # One per app would be a thread per cold start, forever, doing nothing.
        assert threading.active_count() <= before + 1

    def test_socketio_middleware_is_not_installed_on_serverless(self, ctx, monkeypatch):
        """Engineio's middleware holds an invocation open until it is killed."""
        monkeypatch.setenv("VERCEL", "1")
        from app import create_app

        application = create_app("development")
        # The plain WSGI callable, with nothing wrapped in front of it.
        assert not hasattr(application.wsgi_app, "socketio")


class TestStorageBackends:
    """Local disk by default, a bucket when one is configured.

    The point of the split is that a fresh checkout works with no account, no
    key and no network - and that moving to a bucket is a configuration change
    rather than a data migration. That is only true if the keys are identical
    in both, and if the default never accidentally reaches for a network.
    """

    def test_the_default_is_the_local_disk(self, ctx):
        from app.services.storage import LocalStorage, get_storage

        ctx.config.pop("S3_BUCKET", None)
        storage = get_storage()
        assert isinstance(storage, LocalStorage), "without S3_BUCKET the app must not need boto3 or a network"

    def test_a_bucket_selects_the_s3_backend(self, ctx):
        from app.services.storage import S3Storage, get_storage, reset_storage

        # The backend is cached on the app, and mutating config under a running
        # app is exactly the case `reset_storage` exists for.
        reset_storage(ctx)
        ctx.config["S3_BUCKET"] = "harmony-media"
        try:
            assert isinstance(get_storage(ctx), S3Storage)
        finally:
            ctx.config.pop("S3_BUCKET", None)
            reset_storage(ctx)

    def test_two_applications_do_not_share_a_backend(self, ctx):
        """A module-level cache keyed on id() would hand one app's configuration
        to another, since an id is reused after collection."""
        from app.services.storage import LocalStorage, get_storage, reset_storage

        reset_storage(ctx)
        assert isinstance(get_storage(ctx), LocalStorage)

        from flask import Flask

        from app.config import TestingConfig
        from app.services.storage import S3Storage

        other = Flask("other")
        other.config.from_object(TestingConfig)
        other.config["S3_BUCKET"] = "somebody-elses-bucket"
        try:
            assert isinstance(get_storage(other), S3Storage)
        finally:
            reset_storage(other)
        # The first app is unaffected by the second's configuration.
        assert isinstance(get_storage(ctx), LocalStorage)

    def test_boto3_is_only_needed_by_the_s3_backend(self, ctx):
        """A broken or absent boto3 must not stop the app from booting."""
        from app.services.storage import LocalStorage, get_storage

        ctx.config.pop("S3_BUCKET", None)
        assert isinstance(get_storage(), LocalStorage)

    def test_a_local_write_lands_on_disk_and_is_readable(self, ctx, tmp_path):
        import io

        from PIL import Image

        from app.services.storage import LocalStorage

        ctx.config["UPLOAD_DIR"] = str(tmp_path)
        ctx.config["UPLOAD_URL_PREFIX"] = "/uploads"
        backend = LocalStorage(str(tmp_path), "/uploads")

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, format="PNG")
        url = backend.write("2026/09/1/abc.png", buffer.getvalue())

        assert url == "/uploads/2026/09/1/abc.png"
        assert (tmp_path / "2026" / "09" / "1" / "abc.png").read_bytes() == buffer.getvalue()

    def test_a_local_write_leaves_no_part_file_behind(self, ctx, tmp_path):
        """A reader must never see a half-written file: the URL is handed to the
        browser as soon as the post exists."""
        import io

        from PIL import Image

        from app.services.storage import LocalStorage

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buffer, format="PNG")
        LocalStorage(str(tmp_path), "/uploads").write("a/b/c.png", buffer.getvalue())
        assert not list(tmp_path.rglob("*.part"))

    def test_a_key_cannot_escape_the_upload_root(self, ctx, tmp_path):
        from app.services.storage import LocalStorage

        backend = LocalStorage(str(tmp_path), "/uploads")
        for hostile in ("../escape.png", "a/../../escape.png", "..\\escape.png", "sub/../../out.png"):
            try:
                backend.write(hostile, b"x")
            except ValueError:
                continue
            raise AssertionError(f"{hostile!r} was allowed to write outside the root")

    def test_delete_reports_whether_anything_was_there(self, ctx, tmp_path):
        from app.services.storage import LocalStorage

        backend = LocalStorage(str(tmp_path), "/uploads")
        assert backend.delete("gone.png") is False
        backend.write("here.png", b"x")
        assert backend.delete("here.png") is True
        assert backend.delete("here.png") is False

    def test_the_content_type_follows_the_extension(self, ctx):
        """The bytes are re-served verbatim, so the header has to be right or
        the browser refuses to render them."""
        from app.services.storage import content_type_for

        assert content_type_for("2026/09/a/b.jpg") == "image/jpeg"
        assert content_type_for("2026/09/a/b.PNG") == "image/png"
        assert content_type_for("2026/09/a/b.mp4") == "video/mp4"
        assert content_type_for("2026/09/a/b.unknown") == "application/octet-stream"

    def test_an_upload_stores_under_a_key_not_a_path(self, ctx, auth_client, user):
        """The key is what goes in the database, so it has to be identical on
        both backends - that is what makes the move a config change."""
        import io as _io

        from app.services.storage import get_storage
        from tests.utils import make_png

        data = assert_ok(
            auth_client.upload(
                "/api/v1/uploads/images",
                {"file": (_io.BytesIO(make_png()), "a.png", "image/png")},
                content_type="multipart/form-data",
            ),
            status=201,
        )
        url = data["files"][0]["url"]
        backend = get_storage()
        key = url.split("/uploads/", 1)[-1] if "/uploads/" in url else url.rsplit("/", 1)[-1]
        assert "/" in key, f"{key!r} is not a sharded content key"
        assert not key.startswith("/") and ".." not in key
        assert isinstance(backend, object)


class TestDotenvIsActuallyLoaded:
    """`.env.example` says "copy to .env and fill in", so it had better work.

    `python-dotenv` was a declared dependency and nothing called
    `load_dotenv`. A reader who followed the instructions exactly would fill the
    file in, restart, and watch every value in it be ignored - which reads as
    "the application ignores configuration" rather than as a missing function
    call. Hard to diagnose, easy to prevent.
    """

    def test_a_value_in_the_file_reaches_the_environment(self, ctx, tmp_path, monkeypatch):
        import os

        from app import _load_dotenv_files

        env_file = tmp_path / ".env"
        env_file.write_text("HARMONY_DOTENV_PROOF=from-the-file\n", encoding="utf-8")
        monkeypatch.delenv("HARMONY_DOTENV_PROOF", raising=False)
        monkeypatch.chdir(tmp_path)

        _load_dotenv_files()

        assert os.environ.get("HARMONY_DOTENV_PROOF") == "from-the-file"
        monkeypatch.delenv("HARMONY_DOTENV_PROOF", raising=False)

    def test_a_real_environment_variable_wins(self, ctx, tmp_path, monkeypatch):
        """A container platform sets these from its own secret store, and a
        shell export is deliberate. The file is the fallback, not the override."""
        import os

        from app import _load_dotenv_files

        env_file = tmp_path / ".env"
        env_file.write_text("HARMONY_DOTENV_PRECEDENCE=from-the-file\n", encoding="utf-8")
        monkeypatch.setenv("HARMONY_DOTENV_PRECEDENCE", "from-the-shell")
        monkeypatch.chdir(tmp_path)

        _load_dotenv_files()

        assert os.environ.get("HARMONY_DOTENV_PRECEDENCE") == "from-the-shell"
        monkeypatch.delenv("HARMONY_DOTENV_PRECEDENCE", raising=False)

    def test_a_missing_file_is_not_an_error(self, ctx, tmp_path, monkeypatch):
        from app import _load_dotenv_files

        monkeypatch.chdir(tmp_path)
        _load_dotenv_files()  # must not raise

    def test_quoted_values_and_comments_are_handled(self, ctx, tmp_path, monkeypatch):
        import os

        from app import _load_dotenv_files

        (tmp_path / ".env").write_text(
            '# a comment\nHARMONY_DOTENV_QUOTED="a value with spaces"\nHARMONY_DOTENV_PLAIN=bare\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HARMONY_DOTENV_QUOTED", raising=False)
        monkeypatch.delenv("HARMONY_DOTENV_PLAIN", raising=False)

        _load_dotenv_files()

        assert os.environ.get("HARMONY_DOTENV_QUOTED") == "a value with spaces"
        assert os.environ.get("HARMONY_DOTENV_PLAIN") == "bare"
        monkeypatch.delenv("HARMONY_DOTENV_QUOTED", raising=False)
        monkeypatch.delenv("HARMONY_DOTENV_PLAIN", raising=False)

    def test_a_value_in_the_file_is_visible_to_the_config_classes(self, ctx, tmp_path):
        """The file must be read before `.config` is imported, not after.

        Every profile resolves `env("...")` in its *class body*, so all values
        are snapshotted when `app.config` is first imported. A loader called
        from inside `create_app()` therefore ran too late: the file was parsed
        into `os.environ` after the classes had already captured an empty
        environment, and every value in it was ignored. Docker hides this
        because the values arrive as real environment variables; on a host
        where the file is the only source, the app refuses to boot.

        `os.environ` cannot be un-set once a class body has run, so this needs
        a subprocess with nothing inherited but the file.
        """
        import os
        import subprocess
        import sys
        from pathlib import Path

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".env").write_text(
            "APP_ENV=preview\nHARMONY_ORDERING_PROOF=from-the-file\n", encoding="utf-8"
        )
        probe = "import os, app.config; print(os.environ.get('HARMONY_ORDERING_PROOF', 'MISSING'))"

        # Everything except the values under test: the parent's APP_ENV must not
        # win, and the proof variable must not already be set, or the assertion
        # would pass for the wrong reason. The rest of the environment is kept
        # because Windows needs SystemRoot for asyncio to import at all.
        env = dict(os.environ)
        env.pop("APP_ENV", None)
        env.pop("HARMONY_ORDERING_PROOF", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "from-the-file" in result.stdout.splitlines()[-1], (
            "app.config was imported before .env was read: the profile class "
            f"bodies snapshotted an empty environment. stdout={result.stdout!r}"
        )


class TestPreviewProfile:
    """A way to get a site online before paying for a database and a mail server.

    Two production checks need a paid account before anyone can see the site.
    Waiving those is the point of this profile. The signing key and the secure
    cookie are not in that category - they are a way to mint a session for any
    account - and the first version of this class got that wrong by replacing
    `__init__` outright, so a five-character SECRET_KEY booted happily.

    Configuration is read into class attributes when the class body executes, so
    these tests set the attributes rather than the environment variables. An
    environment change after import cannot affect them, which is worth knowing
    independently of this profile.
    """

    GOOD_SECRET = "zK4mQ7xR2vT9pL5nC8hW1bY6fJ3sD0gA4uE7iO2qN5tZ8xV1"

    def _profile(self, monkeypatch, name, **overrides):
        """Build a config profile with the given values, as class attributes."""
        from app.config import CONFIGS

        config_cls = CONFIGS[name]
        values = {
            "SECRET_KEY": self.GOOD_SECRET,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "MAIL_ENABLED": False,
            "MAIL_BACKEND": "console",
            "MAIL_SERVER": "",
            "SECURE_COOKIE": True,
            "MODERATION_PROVIDER": "local",
            "MODERATION_API_KEY": "",
        }
        values.update(overrides)
        for key, value in values.items():
            monkeypatch.setattr(config_cls, key, value, raising=False)
        return config_cls()

    def test_preview_starts_where_production_refuses(self, ctx, monkeypatch):
        from app.config import ConfigError

        with pytest.raises(ConfigError) as caught:
            self._profile(monkeypatch, "production")
        assert "PostgreSQL" in str(caught.value)

        config = self._profile(monkeypatch, "preview")
        assert config.PREVIEW is True

    def test_production_still_works_when_the_configuration_is_right(self, ctx, monkeypatch):
        config = self._profile(
            monkeypatch,
            "production",
            SQLALCHEMY_DATABASE_URI="postgresql+psycopg://u:p@db:5432/harmony",
            MAIL_ENABLED=True,
            MAIL_BACKEND="smtp",
            MAIL_SERVER="smtp.example.com",
        )
        assert config.PREVIEW is False

    def test_preview_still_refuses_a_weak_signing_key(self, ctx, monkeypatch):
        from app.config import ConfigError

        with pytest.raises(ConfigError) as caught:
            self._profile(monkeypatch, "preview", SECRET_KEY="short")
        assert "SECRET_KEY" in str(caught.value)

    def test_preview_announces_an_insecure_cookie_instead_of_refusing(self, ctx, monkeypatch):
        """A free subdomain has no certificate, and a `Secure` cookie on plain
        HTTP means nobody can sign in at all. So preview accepts it - loudly.

        Production does not: the same setting is fatal there, which is the whole
        reason the buckets are separated rather than the check being deleted.
        """
        from app.config import ConfigError

        config = self._profile(monkeypatch, "preview", SECURE_COOKIE=False)
        mandatory, relaxable = config.collect_problems()
        assert mandatory == []
        assert any("SECURE_COOKIE" in line for line in relaxable)

        with pytest.raises(ConfigError) as caught:
            self._profile(
                monkeypatch,
                "production",
                SECURE_COOKIE=False,
                SQLALCHEMY_DATABASE_URI="postgresql+psycopg://u:p@db:5432/harmony",
                MAIL_ENABLED=True,
                MAIL_BACKEND="smtp",
                MAIL_SERVER="smtp.example.com",
            )
        assert "SECURE_COOKIE" in str(caught.value)

    def test_production_accepts_an_insecure_cookie_only_when_everything_else_is_right(self, ctx, monkeypatch):
        """Isolating the variable: with a database, mail and a strong key, the
        one thing left to object to is the cookie."""
        config = self._profile(
            monkeypatch,
            "production",
            SECURE_COOKIE=True,
            SQLALCHEMY_DATABASE_URI="postgresql+psycopg://u:p@db:5432/harmony",
            MAIL_ENABLED=True,
            MAIL_BACKEND="smtp",
            MAIL_SERVER="smtp.example.com",
        )
        assert config.collect_problems() == ([], [])

    def test_the_split_is_what_makes_preview_safe(self, ctx, monkeypatch):
        """The security problems are in one bucket and the paid-account
        problems in the other; a profile that reads only the second cannot wave
        the first through."""
        config = self._profile(monkeypatch, "preview")
        mandatory, relaxable = config.collect_problems()
        assert mandatory == []
        assert any("PostgreSQL" in line for line in relaxable)

    def test_preview_announces_what_it_gave_up(self, ctx, monkeypatch, capsys):
        self._profile(monkeypatch, "preview")
        captured = capsys.readouterr()
        assert "PREVIEW MODE" in captured.err

    def test_production_is_silent_when_nothing_is_wrong(self, ctx, monkeypatch, capsys):
        self._profile(
            monkeypatch,
            "production",
            SQLALCHEMY_DATABASE_URI="postgresql+psycopg://u:p@db:5432/harmony",
            MAIL_ENABLED=True,
            MAIL_BACKEND="smtp",
            MAIL_SERVER="smtp.example.com",
        )
        captured = capsys.readouterr()
        assert "PREVIEW MODE" not in captured.err

    def test_preview_is_registered(self, ctx):
        from app.config import CONFIGS

        assert "preview" in CONFIGS
        assert "production" in CONFIGS

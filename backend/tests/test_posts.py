"""Post and feed tests: publication, visibility, reactions, pagination."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models.post import Post, PostStatus, PostVisibility
from tests.utils import assert_error, assert_ok, create_post, make_mp4, make_png


class TestCreate:
    def test_publishes_text(self, auth_client, user):
        post = create_post(auth_client, "Спокойное утро, тихий город.")
        assert post["body"] == "Спокойное утро, тихий город."
        assert post["author"]["public_id"] == user.public_id
        assert post["status"] == "published"
        assert post["is_owner"] is True

    def test_increments_author_counter(self, auth_client, user):
        from app.models.user import User

        create_post(auth_client, "Одна")
        create_post(auth_client, "Другая")
        db.session.expire_all()
        assert db.session.get(User, user.id).posts_count == 2

    def test_empty_post_rejected(self, auth_client):
        assert_error(
            auth_client.post("/api/v1/posts", json={"body": "   "}),
            status=422,
            code="empty_post",
        )

    def test_overlong_post_rejected(self, auth_client):
        assert_error(auth_client.post("/api/v1/posts", json={"body": "я" * 5001}), status=422)

    def test_html_is_stripped_not_stored(self, auth_client, user):
        post = create_post(auth_client, "Привет <script>alert(1)</script> мир")
        assert "<script" not in post["body"].lower()
        assert "alert(1)" not in post["body"]

    def test_visibility_validated(self, auth_client):
        assert_error(
            auth_client.post("/api/v1/posts", json={"body": "тест", "visibility": "invisible"}),
            status=422,
        )

    @pytest.mark.parametrize("visibility", ["public", "followers", "private"])
    def test_accepted_visibilities(self, auth_client, visibility):
        post = create_post(auth_client, "тест", visibility=visibility)
        assert post["visibility"] == visibility

    def test_requires_authentication(self, anon_client):
        assert_error(anon_client.post("/api/v1/posts", json={"body": "аноним"}), status=401)

    def test_suspended_user_cannot_publish(self, client, make_user):
        from app.models.user import UserStatus

        # Sign in first, then sanction. A suspended account cannot log in at
        # all, so logging in after the sanction would test the wrong thing.
        target = make_user()
        client.login(target.username)

        target.status = UserStatus.SUSPENDED.value
        target.status_reason = (
            "\u041d\u0430\u0440\u0443\u0448\u0435\u043d\u0438\u0435 \u043f\u0440\u0430\u0432\u0438\u043b"
        )
        db.session.commit()

        assert_error(
            client.post("/api/v1/posts", json={"body": "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430"}),
            status=403,
            code="account_suspended",
        )


class TestFeed:
    def test_feed_returns_published_posts(self, client, post_factory, user):
        post_factory(body="Первая")
        post_factory(body="Вторая")
        data = assert_ok(client.get("/api/v1/feed"))
        assert len(data) == 2
        assert {item["body"] for item in data} == {"Первая", "Вторая"}

    def test_hides_unpublished(self, client, post_factory):
        post_factory(body="Опубликована")
        post_factory(body="На проверке", status=PostStatus.UNDER_REVIEW.value)
        post_factory(body="Удалена", status=PostStatus.REMOVED.value)
        data = assert_ok(client.get("/api/v1/feed"))
        assert [item["body"] for item in data] == ["Опубликована"]

    def test_latest_is_strictly_newest_first(self, client, post_factory):
        """``mode=latest`` is the archive ordering, and it stays exact."""
        for index in range(3):
            post_factory(body=f"post {index}")
        data = assert_ok(client.get("/api/v1/feed?mode=latest"))
        assert data[0]["body"] == "post 2"

    def test_default_feed_is_not_chronological(self, client, post_factory):
        """The default must not be ``latest``.

        A home page in a fixed order is the same page for every reader, which is
        the complaint that made this mode the default in the first place. With
        three posts a random order can coincide with the chronological one, so
        the test walks several traversals and requires that they differ.
        """
        for index in range(8):
            post_factory(body=f"post {index}")
        newest_first = [f"post {index}" for index in reversed(range(8))]

        orders = set()
        for _ in range(12):
            data = assert_ok(client.get("/api/v1/feed"))
            orders.add(tuple(item["body"] for item in data))

        for order in orders:
            assert sorted(order) == sorted(newest_first), "a post went missing from the feed"
        assert any(list(order) != newest_first for order in orders), "the default feed is still chronological"
        assert len(orders) > 1, "every traversal returned the same order; the feed is not shuffled"

    def test_shuffled_order_is_stable_across_one_traversal(self, client, post_factory):
        """Paging one scroll must not re-draw the lottery.

        Without the seed carried in the cursor, page two is a fresh shuffle: the
        posts the reader already passed come back and the rest never arrive.
        """
        for index in range(9):
            post_factory(body=f"post {index}")

        first = client.get("/api/v1/feed?mode=shuffled&limit=4")
        page_one = assert_ok(first)
        meta = first.get_json()["meta"]
        assert meta["has_more"] is True

        second = client.get(f"/api/v1/feed?mode=shuffled&limit=4&cursor={meta['next_cursor']}")
        page_two = assert_ok(second)
        assert not {item["id"] for item in page_one} & {item["id"] for item in page_two}

        third_meta = second.get_json()["meta"]
        assert third_meta["has_more"] is True
        third = client.get(f"/api/v1/feed?mode=shuffled&limit=4&cursor={third_meta['next_cursor']}")
        page_three = assert_ok(third)

        seen = [item["id"] for item in page_one + page_two + page_three]
        assert len(seen) == len(set(seen)) == 9

    def test_a_post_published_mid_scroll_does_not_repeat(self, client, post_factory):
        """The snapshot watermark stops a new post shifting the pages under the reader."""
        for index in range(6):
            post_factory(body=f"post {index}")

        first = client.get("/api/v1/feed?mode=shuffled&limit=3")
        page_one = assert_ok(first)
        cursor = first.get_json()["meta"]["next_cursor"]

        # Newer than everything in the snapshot; it arrives between the pages.
        post_factory(body="just now")

        page_two = assert_ok(client.get(f"/api/v1/feed?mode=shuffled&limit=3&cursor={cursor}"))
        assert not {item["id"] for item in page_one} & {item["id"] for item in page_two}

    def test_for_you_ranks_a_followed_author_first(self, auth_client, user, post_factory, make_user):
        """A followed author outranks a stranger, even a more recent one."""
        from app.extensions import db
        from app.models.user import Relationship

        followed = make_user(username="followed", email="followed@harmony.test")
        stranger = make_user(username="stranger", email="stranger@harmony.test")

        post_factory(body="from the stranger", author=stranger)
        post_factory(body="from the followed", author=followed)

        db.session.add(Relationship(follower_id=user.id, followee_id=followed.id, status="following"))
        db.session.commit()

        bodies = [item["body"] for item in assert_ok(auth_client.get("/api/v1/feed?mode=for_you"))]
        assert bodies.index("from the followed") < bodies.index("from the stranger")

    def test_personalisation_off_gives_a_shuffled_feed(self, auth_client, post_factory, make_user):
        """The privacy switch is honoured by the ranking, not merely stored.

        Set through the real endpoint, because a test that flips the column
        directly would still pass if the product route were broken - and the
        claim being made is about what the reader experiences, not about what a
        row says.
        """
        followed = make_user(username="followed2", email="followed2@harmony.test")
        for index in range(6):
            post_factory(body=f"post {index}", author=followed)

        assert_ok(auth_client.patch("/api/v1/me/privacy", json={"personalize_feed": False}))

        # Read it back rather than trusting the PATCH response: the response is
        # built from the object that was just mutated, so it would report the
        # new value even if nothing was written. The failure this guards against
        # is a settings page that forgets what was saved.
        stored = assert_ok(auth_client.get("/api/v1/me/privacy"))
        assert stored["privacy"]["personalize_feed"] is False

        orders = {
            tuple(item["id"] for item in assert_ok(auth_client.get("/api/v1/feed?mode=for_you"))) for _ in range(10)
        }
        assert len(orders) > 1, "personalisation is off but the order never changed"

    def test_pagination_returns_cursor(self, client, post_factory):
        for index in range(6):
            post_factory(body=f"Пост {index}")
        first = client.get("/api/v1/feed?limit=3")
        page_one = assert_ok(first)
        meta = first.get_json()["meta"]
        assert len(page_one) == 3
        assert meta["has_more"] is True
        assert meta["next_cursor"]

        second = client.get(f"/api/v1/feed?limit=3&cursor={meta['next_cursor']}")
        page_two = assert_ok(second)
        assert len(page_two) == 3
        # No overlap between pages.
        assert not {item["id"] for item in page_one} & {item["id"] for item in page_two}

    def test_uploading_the_same_bytes_twice_attaches_it_once(self, auth_client, user):
        """A retried upload must not become a duplicate attachment.

        Storage keys are content-addressed, so the same bytes uploaded twice
        produce two rows under one key. Returning both would attach the image
        twice: the reader sees a duplicate they never chose, and it consumes
        two of the attachment budget instead of one.
        """
        import io as _io

        from app.extensions import db
        from app.models.post import PostMedia

        def upload():
            return assert_ok(
                auth_client.upload(
                    "/api/v1/uploads/images",
                    {"file": (_io.BytesIO(make_png()), "same.png", "image/png")},
                    content_type="multipart/form-data",
                ),
                status=201,
            )["files"][0]

        first = upload()
        second = upload()
        assert first["storage_key"] == second["storage_key"], "the fixture should be content-addressed"
        assert first["id"] != second["id"], "two uploads should be two rows"

        orphan = db.session.get(PostMedia, second["id"])
        assert orphan.post_id is None

        data = assert_ok(
            auth_client.post(
                "/api/v1/posts",
                json={"body": "once", "media": [{"storage_key": first["storage_key"]}]},
            ),
            status=201,
        )
        media = data["post"]["media"]
        assert len(media) == 1, f"expected one attachment, got {len(media)}"

    def test_attachment_order_follows_the_client(self, auth_client):
        """The composer lets a reader arrange attachments; the post keeps that order."""
        import io as _io

        def upload(payload, name, mime):
            return assert_ok(
                auth_client.upload(
                    "/api/v1/uploads/images",
                    {"file": (_io.BytesIO(payload), name, mime)},
                    content_type="multipart/form-data",
                ),
                status=201,
            )["files"][0]

        picture = upload(make_png(), "a.png", "image/png")
        clip = upload(make_mp4(), "b.mp4", "video/mp4")

        data = assert_ok(
            auth_client.post(
                "/api/v1/posts",
                json={
                    "body": "video first",
                    "media": [{"storage_key": clip["storage_key"]}, {"storage_key": picture["storage_key"]}],
                },
            ),
            status=201,
        )
        assert [item["mime_type"] for item in data["post"]["media"]] == ["video/mp4", "image/png"]

    def test_editing_replaces_the_text(self, auth_client):
        created = assert_ok(auth_client.post("/api/v1/posts", json={"body": "Черновик."}), status=201)["post"]
        updated = assert_ok(auth_client.patch(f"/api/v1/posts/{created['id']}", json={"body": "Исправленный текст."}))[
            "post"
        ]
        assert updated["body"] == "Исправленный текст."
        assert updated["edited_at"], "an edited post must say so"

    def test_an_empty_edit_does_not_wipe_the_post(self, auth_client):
        """`body or ""` made a whitespace-only edit an empty post.

        One stray request destroyed the text with no error and no way back, so
        the edit is refused instead.
        """
        created = assert_ok(auth_client.post("/api/v1/posts", json={"body": "Текст, который жалко."}), status=201)[
            "post"
        ]
        assert_error(
            auth_client.patch(f"/api/v1/posts/{created['id']}", json={"body": "   "}),
            status=422,
            code="empty_post",
        )
        after = assert_ok(auth_client.get(f"/api/v1/posts/{created['id']}"))["post"]
        assert after["body"] == "Текст, который жалко."

    def test_editing_without_a_body_leaves_the_text_alone(self, auth_client):
        """A visibility-only edit must not blank the post."""
        created = assert_ok(auth_client.post("/api/v1/posts", json={"body": "Остаётся."}), status=201)["post"]
        updated = assert_ok(auth_client.patch(f"/api/v1/posts/{created['id']}", json={"visibility": "followers"}))[
            "post"
        ]
        assert updated["body"] == "Остаётся."
        assert updated["visibility"] == "followers"

    def test_a_stranger_cannot_edit_your_post(self, auth_client, anon_client, make_user, post_factory):
        """Refused for an anonymous caller *and* for a different signed-in one.

        The second case is the one that matters: an unauthenticated request is
        stopped by the decorator before it ever reaches the ownership check, so
        passing it says nothing about whether another account can overwrite a
        post that is not its own.
        """
        created = post_factory(body="Чужое.")

        assert_error(
            anon_client.patch(f"/api/v1/posts/{created.public_id}", json={"body": "Подмена."}),
            status=401,
        )
        after = assert_ok(auth_client.get(f"/api/v1/posts/{created.public_id}"))["post"]
        assert after["body"] == "Чужое."

        stranger = make_user(username="stranger", email="stranger@harmony.test")
        anon_client.login(stranger.username)
        assert_error(
            anon_client.patch(f"/api/v1/posts/{created.public_id}", json={"body": "Подмена."}),
            status=403,
        )
        after = assert_ok(auth_client.get(f"/api/v1/posts/{created.public_id}"))["post"]
        assert after["body"] == "Чужое."

    def test_two_comments_in_a_row(self, auth_client, post_factory):
        """The comment box used to remove itself after the first one."""
        post = post_factory(body="Спорим?")
        assert_ok(
            auth_client.post(f"/api/v1/posts/{post.public_id}/comments", json={"body": "Первый."}),
            status=201,
        )
        assert_ok(
            auth_client.post(f"/api/v1/posts/{post.public_id}/comments", json={"body": "Второй."}),
            status=201,
        )

        listed = assert_ok(auth_client.get(f"/api/v1/posts/{post.public_id}/comments"))
        bodies = {item["body"] for item in listed}
        assert {"Первый.", "Второй."} <= bodies, bodies

    def test_a_reply_is_a_separate_comment(self, auth_client, post_factory):
        post = post_factory(body="Ветка?")
        parent = assert_ok(
            auth_client.post(f"/api/v1/posts/{post.public_id}/comments", json={"body": "Первый."}),
            status=201,
        )["comment"]
        reply = assert_ok(
            auth_client.post(
                f"/api/v1/posts/{post.public_id}/comments", json={"body": "Ответ.", "parent_id": parent["id"]}
            ),
            status=201,
        )["comment"]
        assert reply.get("parent_id") in (None, parent["id"])

        # The listing is a thread, not a flat list: one root carrying its
        # replies. Asserting on the shape rather than on a count is what keeps
        # the test honest about which of the two it is checking.
        listed = assert_ok(auth_client.get(f"/api/v1/posts/{post.public_id}/comments"))
        assert len(listed) == 1, listed
        root = listed[0]
        assert root["body"] == "Первый."
        assert root["depth"] == 0
        replies = root.get("replies") or root.get("children") or []
        assert [item["body"] for item in replies] == ["Ответ."], replies
        assert replies[0].get("depth", 1) >= 1, replies[0]

    def test_invalid_cursor_rejected(self, client):
        assert_error(client.get("/api/v1/feed?cursor=not-a-cursor"), status=422, code="invalid_cursor")

    def test_limit_bounds_enforced(self, client):
        assert_error(client.get("/api/v1/feed?limit=0"), status=422, code="invalid_limit")
        assert_error(client.get("/api/v1/feed?limit=abc"), status=422, code="invalid_limit")

    def test_anonymous_sees_only_public(self, client, post_factory, other_user):
        post_factory(body="Публичная")
        post_factory(author=other_user, body="Приватная", visibility=PostVisibility.PRIVATE.value)
        data = assert_ok(client.get("/api/v1/feed"))
        assert [item["body"] for item in data] == ["Публичная"]

    def test_following_mode_filtered(self, client, user, make_user, post_factory, auth_api):
        from app.models.user import Relationship

        followed = make_user()
        stranger = make_user()
        post_factory(author=followed, body="От подписчика")
        post_factory(author=stranger, body="От незнакомца")

        db.session.add(Relationship(follower_id=user.id, followee_id=followed.id, status="following"))
        db.session.commit()

        auth_api(user)
        data = assert_ok(client.get("/api/v1/feed?mode=following"))
        assert [item["body"] for item in data] == ["От подписчика"]


class TestVisibility:
    def test_private_post_hidden_from_others(self, client, post_factory, other_user, auth_api):
        post = post_factory(body="Личное", visibility=PostVisibility.PRIVATE.value)
        auth_api(other_user)
        assert_error(client.get(f"/api/v1/posts/{post.public_id}"), status=404, code="post_not_found")

    def test_author_sees_own_private_post(self, client, post_factory, user, auth_api):
        post = post_factory(body="Личное", visibility=PostVisibility.PRIVATE.value)
        auth_api(user)
        data = assert_ok(client.get(f"/api/v1/posts/{post.public_id}"))
        assert data["post"]["body"] == "Личное"

    def test_followers_only_visible_to_followers(self, client, post_factory, user, other_user, auth_api):
        from app.models.user import Relationship

        post = post_factory(body="Для подписчиков", visibility=PostVisibility.FOLLOWERS.value)
        db.session.add(Relationship(follower_id=other_user.id, followee_id=user.id, status="following"))
        db.session.commit()

        auth_api(other_user)
        assert_ok(client.get(f"/api/v1/posts/{post.public_id}"))

    def test_non_follower_gets_404_not_403(self, client, post_factory, other_user, auth_api):
        """A 403 would confirm the post exists; a 404 does not."""
        post = post_factory(body="Для подписчиков", visibility=PostVisibility.FOLLOWERS.value)
        auth_api(other_user)
        assert_error(client.get(f"/api/v1/posts/{post.public_id}"), status=404)


class TestReactions:
    def test_like_and_unlike(self, auth_client, post_factory):
        post = post_factory()
        liked = assert_ok(auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "like"}))
        assert liked["liked"] is True
        assert liked["likes_count"] == 1

        unliked = assert_ok(auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "unlike"}))
        assert unliked["liked"] is False
        assert unliked["likes_count"] == 0

    def test_double_like_does_not_increment_twice(self, auth_client, post_factory):
        post = post_factory()
        assert_ok(auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "like"}))
        again = assert_ok(auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "like"}))
        assert again["likes_count"] == 1

    def test_counter_matches_reality(self, auth_client, post_factory, other_user, auth_api):

        post = post_factory()
        auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "like"})
        auth_api(other_user)
        auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "like"})

        db.session.expire_all()
        assert db.session.get(Post, post.id).likes_count == 2

    def test_cannot_like_missing_post(self, auth_client):
        assert_error(auth_client.post("/api/v1/posts/deadbeef/reactions", json={}), status=404)

    def test_reaction_list(self, auth_client, post_factory, user):
        post = post_factory()
        auth_client.post(f"/api/v1/posts/{post.public_id}/reactions", json={"type": "like"})
        data = assert_ok(auth_client.get(f"/api/v1/posts/{post.public_id}/reactions"))
        assert len(data["reactions"]) == 1


class TestUpdateDelete:
    def test_owner_can_edit(self, auth_client, post_factory):
        post = post_factory(body="Исходный текст")
        data = assert_ok(auth_client.patch(f"/api/v1/posts/{post.public_id}", json={"body": "Изменённый текст"}))
        assert data["post"]["body"] == "Изменённый текст"
        assert data["post"]["edited_at"]

    def test_stranger_cannot_edit(self, client, post_factory, other_user, auth_api):
        post = post_factory()
        auth_api(other_user)
        assert_error(
            client.patch(f"/api/v1/posts/{post.public_id}", json={"body": "взлом"}),
            status=403,
            code="not_post_owner",
        )

    def test_moderator_can_edit(self, client, post_factory, moderator, auth_api):
        post = post_factory()
        auth_api(moderator)
        assert_ok(client.patch(f"/api/v1/posts/{post.public_id}", json={"body": "Отредактировано модератором"}))

    def test_delete_hides_but_retains_row(self, auth_client, post_factory, user):
        """A delete must be reversible, so the text is kept on a hidden row.

        Real erasure is the account-deletion path, not this one.
        """
        post = post_factory(body="Удаляемая запись")
        assert auth_client.delete(f"/api/v1/posts/{post.public_id}").status_code == 204
        db.session.expire_all()
        stored = db.session.get(Post, post.id)
        assert stored.status == PostStatus.REMOVED.value
        assert stored.deleted_at is not None
        # Invisible to everyone, including its own author.
        assert_error(auth_client.get(f"/api/v1/posts/{post.public_id}"), status=404)

    def test_restore(self, auth_client, post_factory, user):
        post = post_factory()
        auth_client.delete(f"/api/v1/posts/{post.public_id}")
        data = assert_ok(auth_client.post(f"/api/v1/posts/{post.public_id}/restore"))
        assert data["post"]["status"] == "published"

    def test_restore_requires_deletion(self, auth_client, post_factory):
        post = post_factory()
        assert_error(auth_client.post(f"/api/v1/posts/{post.public_id}/restore"), status=409)


class TestSpans:
    def test_text_is_segmented_for_safe_rendering(self, auth_client, post_factory):
        post = post_factory(body="Смотри https://example.com и @mira и #тишина")
        detail = assert_ok(auth_client.get(f"/api/v1/posts/{post.public_id}"))
        kinds = [span["type"] for span in detail["post"]["spans"]]
        assert "link" in kinds
        assert "mention" in kinds
        assert "hashtag" in kinds
        # The raw body is plain text; the client renders spans as DOM nodes.
        assert "<" not in detail["post"]["body"]

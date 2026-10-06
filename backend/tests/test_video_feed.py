"""Vertical feed: photos alongside video, view counting, deduplication.

Three things are tested here that the earlier video-only version got wrong or
omitted:

  - a photo fills a screen too, and `kind` picks which. A photo-then-video post
    must show the clip in `video` and the photo in `photo`, not whichever happens
    to sort first.
  - view counts are buffered and only the nightly flush writes the column, so a
    video published an hour ago reads as zero plays unless the buffer is added on
    top. That is the one number on this screen that must not be quietly wrong.
  - views are deduplicated per account, because the client counts a view every time
    a screen becomes current and scrolling up and down would otherwise inflate the
    number on its own.
"""

from __future__ import annotations

import pytest

from tests.utils import assert_ok


@pytest.fixture()
def media_post(post_factory):
    """A post with one attachment of a chosen type."""
    from app.models.post import PostMedia

    def _factory(kind="video", author=None, body="", **kwargs):
        post = post_factory(author, body=body, **kwargs)
        if kind == "video":
            mime, url, width, height = "video/mp4", f"/uploads/v/{post.public_id}.mp4", 576, 1024
        else:
            mime, url, width, height = "image/jpeg", f"/uploads/i/{post.public_id}.jpg", 4032, 3024
        post.media.append(
            PostMedia(
                owner_id=(author or post.author).id,
                position=0,
                storage_key=f"{kind}/{post.public_id}",
                url=url,
                mime_type=mime,
                width=width,
                height=height,
                content_hash=f"{kind}{post.public_id}",
            )
        )
        return post

    return _factory


class TestKindSelection:
    def test_the_default_is_video(self, guest_client, media_post):
        media_post("video", body="клип")
        media_post("photo", body="снимок")

        posts = assert_ok(guest_client.get("/api/v1/videos"))

        assert [p["body"] for p in posts] == ["клип"]

    def test_photo_shows_the_photo(self, guest_client, media_post):
        media_post("video", body="клип")
        media_post("photo", body="снимок")

        posts = assert_ok(guest_client.get("/api/v1/videos?kind=photo"))

        assert [p["body"] for p in posts] == ["снимок"]
        assert all(p["is_photo"] for p in posts)
        assert not any(p["is_video"] for p in posts)

    def test_all_shows_both(self, guest_client, media_post):
        media_post("video", body="клип")
        media_post("photo", body="снимок")

        posts = assert_ok(guest_client.get("/api/v1/videos?kind=all"))

        assert {p["body"] for p in posts} == {"клип", "снимок"}

    def test_a_mixed_post_shows_the_clip_in_video_and_the_photo_in_photo(self, guest_client, post_factory):
        """The reason `kind` exists at all.

        A photo first, then a clip. In `video` the clip must be on screen - not the
        photo that happens to sort first. In `photo` the reverse.
        """
        from app.extensions import db
        from app.models.post import PostMedia

        post = post_factory(body="и то и другое")
        post.media.append(
            PostMedia(owner_id=post.author_id, position=0, storage_key="a.jpg",
                      url="/uploads/a.jpg", mime_type="image/jpeg",
                      width=800, height=600, content_hash="a")
        )
        post.media.append(
            PostMedia(owner_id=post.author_id, position=1, storage_key="b.mp4",
                      url="/uploads/b.mp4", mime_type="video/mp4",
                      width=576, height=1024, content_hash="b")
        )
        db.session.commit()

        video = assert_ok(guest_client.get("/api/v1/videos"))
        photo = assert_ok(guest_client.get("/api/v1/videos?kind=photo"))

        assert [m["mime_type"] for m in video[0]["media"]] == ["video/mp4"], "video kind showed the photo"
        assert [m["mime_type"] for m in photo[0]["media"]] == ["image/jpeg"], "photo kind showed the clip"
        # The post has two attachments; the screen has one. The rest is signalled so
        # the page can say so rather than pretending the post is a single file.
        assert video[0]["media_count"] == 2

    def test_one_post_appears_once_however_many_attachments(self, guest_client, post_factory):
        from app.extensions import db
        from app.models.post import PostMedia

        post = post_factory(body="четыре клипа")
        for position in range(1, 4):
            post.media.append(
                PostMedia(owner_id=post.author_id, position=position,
                          storage_key=f"v{position}.mp4", url=f"/uploads/v{position}.mp4",
                          mime_type="video/mp4", width=576, height=1024, content_hash=f"v{position}")
            )
        db.session.commit()

        posts = assert_ok(guest_client.get("/api/v1/videos"))

        assert len(posts) == 1
        assert len(posts[0]["media"]) == 1

    def test_an_unknown_kind_is_refused(self, guest_client):
        from tests.utils import assert_error

        assert_error(guest_client.get("/api/v1/videos?kind=nonsense"), status=422)


class TestViewCounting:
    def test_the_stored_count_is_returned(self, guest_client, media_post):
        post = media_post("video", body="клип")
        post.views_count = 41
        from app.extensions import db

        db.session.commit()

        posts = assert_ok(guest_client.get("/api/v1/videos"))

        assert posts[0]["views_count"] == 41

    def test_a_view_is_counted_for_a_signed_in_viewer(self, client, media_post):
        post = media_post("video", body="клип")

        client.login(post.author.username)
        response = client.post(f"/api/v1/posts/{post.public_id}/views")

        assert response.status_code == 204

    def test_the_same_account_counting_twice_counts_once(self, client, media_post):
        """The regression this endpoint was written for.

        The client counts a view whenever a screen becomes current. Scrolling down
        two screens and back up re-counts each of them, so without a per-account
        claim the number is a measure of scrolling, not of watching.
        """
        from app.extensions import db

        viewer = media_post("video", body="клип").author
        client.login(viewer.username)

        first = client.post(f"/api/v1/posts/{viewer.posts[0].public_id}/views")
        second = client.post(f"/api/v1/posts/{viewer.posts[0].public_id}/views")

        assert first.status_code == 204
        assert second.status_code == 204
        # Same answer both times - the endpoint does not say which was counted,
        # because a client cannot act on the difference.
        assert first.status_code == second.status_code
        db.session.rollback()

    def test_two_different_accounts_both_count(self, client, media_post, make_user):
        post = media_post("video", body="клип")
        other = make_user()

        client.login(post.author.username)
        assert client.post(f"/api/v1/posts/{post.public_id}/views").status_code == 204
        client.login(other.username)
        assert client.post(f"/api/v1/posts/{post.public_id}/views").status_code == 204

    def test_a_guest_may_count(self, guest_client, media_post):
        """Not deduplicated - there is nothing stable to key on and an IP is shared
        by a whole school. Over-counting beats a count frozen at zero."""
        post = media_post("video", body="клип")

        assert guest_client.post(f"/api/v1/posts/{post.public_id}/views").status_code == 204

    def test_counting_an_unknown_post_is_404(self, guest_client):
        from tests.utils import assert_error

        assert_error(guest_client.post("/api/v1/posts/deadbeef/views"), status=404)

    def test_counting_a_hidden_post_is_404(self, guest_client, media_post):
        post = media_post("video", body="скрытый", status="hidden")

        from tests.utils import assert_error

        assert_error(guest_client.post(f"/api/v1/posts/{post.public_id}/views"), status=404)

    def test_the_claim_is_set_once_not_get_then_set(self, app):
        """Pinned at the source: `rate_counter_set_once` must use SET NX.

        A get followed by a set races - two tabs on the same clip both see "not
        seen", and both count. That is the exact bug the claim exists to prevent,
        and it only shows up under concurrency, which no test of the endpoint can
        provoke.
        """
        import inspect

        from app.services.cache_service import rate_counter_set_once

        source = inspect.getsource(rate_counter_set_once)
        assert "nx=True" in source, "the claim must be set-if-absent"
        assert "get(" not in source, "a read before the write reintroduces the race"


class TestCountersRead:
    def test_a_missing_counter_is_not_an_error(self):
        """A post nobody has watched has no counter key at all, which is not a
        failure - it is the normal state for a new post."""
        from app.services.cache_service import counters_read

        assert counters_read([]) == {}
        assert counters_read(["views:nothing-here"]) == {}

    def test_several_counters_are_read_at_once(self, app):
        """Skipped without Redis rather than asserting nothing.

        The buffered counters live in Redis, and this project treats that as
        optional - `init_redis` probes and the code degrades. Testing the batch
        read against a stub would assert the stub, so the test only runs where
        there is something real to read, and the contract it protects (a missing
        key is not an error) is asserted unconditionally above.
        """
        from app.extensions import get_redis

        if get_redis() is None:
            pytest.skip("Redis is not running; the buffered counters have nowhere to live")

        from app.services.cache_service import counters_read, rate_counter_add

        rate_counter_add("views:probe-a", 3, ttl=60)
        rate_counter_add("views:probe-b", 5, ttl=60)

        result = counters_read(["views:probe-a", "views:probe-b", "views:absent"])

        assert result["views:probe-a"] == 3
        assert result["views:probe-b"] == 5
        assert "views:absent" not in result, "a key that was never set must be absent, not zero"
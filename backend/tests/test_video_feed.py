"""Vertical video feed.

One clip per screen, so the list is posts with video and nothing else, and each
post appears exactly once. The second property is the one that reads as "the feed
is broken" if it is wrong: a post with three clips appearing three times looks
like a bug to a viewer even when it is only a consequence of the query.
"""

from __future__ import annotations

import pytest

from tests.utils import assert_error, assert_ok

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32


@pytest.fixture()
def video_post(post_factory):
    """A post whose only attachment is a clip."""
    from app.models.post import PostMedia

    def _factory(author=None, **kwargs):
        post = post_factory(author, **kwargs)
        post.media.append(
            PostMedia(
                owner_id=(author or post.author).id,
                position=0,
                storage_key=f"videos/{post.public_id}.mp4",
                url=f"/uploads/videos/{post.public_id}.mp4",
                mime_type="video/mp4",
                content_hash=f"v{post.public_id}",
            )
        )
        return post

    return _factory


@pytest.fixture()
def image_post(post_factory):
    from app.models.post import PostMedia

    def _factory(author=None, **kwargs):
        post = post_factory(author, **kwargs)
        post.media.append(
            PostMedia(
                owner_id=(author or post.author).id,
                position=0,
                storage_key=f"images/{post.public_id}.jpg",
                url=f"/uploads/images/{post.public_id}.jpg",
                mime_type="image/jpeg",
                content_hash=f"i{post.public_id}",
            )
        )
        return post

    return _factory


class TestVideoFeed:
    def test_returns_only_posts_with_video(self, guest_client, video_post, image_post):
        video_post(body="со видео")
        image_post(body="просто фото")

        posts = assert_ok(guest_client.get("/api/v1/videos"))

        assert [p["body"] for p in posts] == ["со видео"]

    def test_a_post_with_three_clips_appears_once(self, guest_client, video_post):
        from app.extensions import db
        from app.models.post import PostMedia

        post = video_post(body="три клипа")
        for position in (1, 2):
            post.media.append(
                PostMedia(
                    owner_id=post.author_id,
                    position=position,
                    storage_key=f"videos/{post.public_id}-{position}.mp4",
                    url=f"/uploads/videos/{post.public_id}-{position}.mp4",
                    mime_type="video/mp4",
                    content_hash=f"v{post.public_id}-{position}",
                )
            )
        db.session.commit()

        posts = assert_ok(guest_client.get("/api/v1/videos"))

        assert len(posts) == 1, "one post with three clips must not fill three screens"
        # And only the first clip is offered, so the screen is not a gallery.
        assert len(posts[0]["media"]) == 1
        assert posts[0]["is_video"] is True

    def test_a_mixed_post_shows_the_clip_not_the_photo(self, guest_client, post_factory):
        """A photo first, then a clip. The vertical feed is for video, so the
        screen has to be the video - not the photo that happens to sort first."""
        from app.extensions import db
        from app.models.post import PostMedia

        post = post_factory(body="фото и видео")
        post.media.append(
            PostMedia(owner_id=post.author_id, position=0, storage_key="a.jpg",
                      url="/uploads/a.jpg", mime_type="image/jpeg", content_hash="a")
        )
        post.media.append(
            PostMedia(owner_id=post.author_id, position=1, storage_key="b.mp4",
                      url="/uploads/b.mp4", mime_type="video/mp4", content_hash="b")
        )
        db.session.commit()

        posts = assert_ok(guest_client.get("/api/v1/videos"))

        assert len(posts) == 1
        assert [m["mime_type"] for m in posts[0]["media"]] == ["video/mp4"]

    def test_empty_when_there_is_no_video(self, guest_client, image_post):
        image_post(body="только фото")

        assert assert_ok(guest_client.get("/api/v1/videos")) == []

    def test_honours_visibility(self, guest_client, video_post, post_factory, make_user, client):
        """A private clip belongs to its author and to nobody else.

        This is the check that matters most for the vertical feed: it is the one
        place a private video is most likely to leak, because the screen shows the
        clip large and the viewer has just asked for video specifically.
        """
        from app.models.user import ProfileVisibility

        author = make_user()
        mine = make_user()
        video_post(author=author, body="открытый клип")

        # A second author with a followers-only clip, which a non-follower must not
        # be shown.
        other = make_user()
        private = video_post(author=other, body="закрытый клип")

        private.visibility = ProfileVisibility.FOLLOWERS.value
        from app.extensions import db

        db.session.commit()

        client.login(mine.username)
        bodies = [p["body"] for p in assert_ok(client.get("/api/v1/videos"))]
        assert "закрытый клип" not in bodies, bodies
        assert "открытый клип" in bodies, bodies

        # And the author does see their own.
        client.login(other.username)
        own = [p["body"] for p in assert_ok(client.get("/api/v1/videos"))]
        assert "закрытый клип" in own

    def test_hidden_posts_are_not_in_the_feed(self, guest_client, video_post):
        """`hidden` is what a moderator sets. A hidden clip must not keep playing
        on the vertical feed just because it is still in the database."""
        video_post(body="скрытый", status="hidden")

        assert assert_ok(guest_client.get("/api/v1/videos")) == []

    def test_the_cursor_paginates_without_repeating(self, guest_client, video_post):
        for index in range(5):
            video_post(body=f"клип {index}")

        first = guest_client.get("/api/v1/videos?limit=2").get_json()
        assert len(first["data"]) == 2
        cursor = first["meta"]["next_cursor"]
        assert cursor, "expected a cursor when there is more than one page"

        seen = [p["body"] for p in first["data"]]
        while cursor:
            page = guest_client.get(f"/api/v1/videos?limit=2&cursor={cursor}").get_json()
            seen.extend(p["body"] for p in page["data"])
            cursor = page["meta"]["next_cursor"]

        assert len(seen) == 5, seen
        assert len(set(seen)) == 5, f"a clip repeated across pages: {seen}"

    def test_a_hostile_limit_is_refused(self, guest_client):
        assert_error(guest_client.get("/api/v1/videos?limit=abc"), status=422)

    def test_an_oversized_limit_is_clamped_rather_than_refused(self, guest_client, video_post):
        video_post(body="один")

        posts = assert_ok(guest_client.get("/api/v1/videos?limit=99999"))

        assert len(posts) == 1
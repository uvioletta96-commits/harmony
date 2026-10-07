"""Two video surfaces, one threshold: «Клипы» up to three minutes, «Эфир» past it.

The split is the whole design and it is easy to get quietly wrong, because every
way of being wrong still returns a page. A long video in the clip feed is a reader
who cannot swipe past it. A short clip in the library is a card that is not what
the card says it is. Neither raises an error; both are just wrong.

So these check the boundary itself, and both sides of it.
"""

from __future__ import annotations

import io

import pytest

from tests.utils import assert_error, assert_ok

# Valid enough headers for the sniffers, which read magic bytes only and never
# decode. Video is stored byte-for-byte, so a valid header is the whole contract.
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40
PNG = None  # built by tests.utils.make_png

from tests.utils import make_png  # noqa: E402

THREE_MINUTES = 180_000
TWO_MINUTES = 120_000
FOUR_MINUTES = 240_000


@pytest.fixture()
def author(auth_client, make_user):
    return make_user(username="video_author")


def _upload(client, blob: bytes, filename: str, duration_ms: int | None = None):
    form = {} if duration_ms is None else {"durations": str(duration_ms)}
    files = {"file": (io.BytesIO(blob), filename, "application/octet-stream")}
    return client.upload("/api/v1/uploads/images", files=files, form=form)


def _clip(client, blob: bytes, filename: str, duration_ms: int | None = None):
    """Upload one file and attach it to a post, returning the post."""
    stored = assert_ok(_upload(client, blob, filename, duration_ms), status=201)["files"][0]
    return assert_ok(
        client.post("/api/v1/posts", json={"body": "ролик", "media": [{"storage_key": stored["storage_key"]}]}),
        status=201,
    )


class TestUploadRecordsDuration:
    """The server cannot decode a container without ffmpeg, so the client's figure
    is what gets stored - and it has to be bounded, because it chooses the feed."""

    def test_a_clip_reports_its_length(self, auth_client):
        stored = assert_ok(_upload(auth_client, MP4, "c.mp4", TWO_MINUTES), status=201)["files"][0]

        assert stored["duration_ms"] == TWO_MINUTES

    def test_a_clip_with_no_reported_length_is_zero_not_absent(self, auth_client):
        """Zero means "not reported", and it lands the video in the feed that always
        exists. Missing the key entirely would leave the column at its default
        anyway, but silently - and the difference shows up as a filter that admits
        a long video."""
        stored = assert_ok(_upload(auth_client, MP4, "c.mp4"), status=201)["files"][0]

        assert stored["duration_ms"] == 0

    def test_a_lie_is_bounded(self, auth_client):
        """A client claiming six hours cannot put its video past the library's
        ceiling, and one claiming a negative length cannot write a negative
        number."""
        stored = assert_ok(_upload(auth_client, MP4, "c.mp4", 99_999_999_999), status=201)["files"][0]

        assert stored["duration_ms"] == 3_600_000

    def test_a_negative_length_is_refused_not_stored(self, auth_client):
        stored = assert_ok(_upload(auth_client, MP4, "c.mp4", -5000), status=201)["files"][0]

        assert stored["duration_ms"] == 0, "a negative duration reached the database"

    def test_a_non_numeric_length_does_not_fail_the_upload(self, auth_client):
        """The field is one nobody sees. Refusing a video that plays perfectly
        because of a number attached to it would be the wrong trade."""
        response = _upload(auth_client, MP4, "c.mp4")
        files = {"file": (io.BytesIO(MP4), "c.mp4", "application/octet-stream")}

        bad = auth_client.upload(
            "/api/v1/uploads/images", files=files, form={"durations": "soon"}
        )

        assert response.status_code == 201
        assert bad.status_code == 201
        assert bad.get_json()["data"]["files"][0]["duration_ms"] == 0

    def test_several_clips_get_their_own_lengths(self, auth_client):
        """Positional, in the order the files were sent. Repeating the last figure
        for the rest would give every clip the same feed."""
        response = auth_client.upload(
            "/api/v1/uploads/images",
            files={
                "a": (io.BytesIO(MP4), "a.mp4", "application/octet-stream"),
                "b": (io.BytesIO(MP4), "b.mp4", "application/octet-stream"),
            },
            form={"durations": "60000,300000"},
        )

        lengths = [f["duration_ms"] for f in response.get_json()["data"]["files"]]
        assert lengths == [60_000, 300_000]

    def test_a_photo_gets_no_duration(self, auth_client):
        """A still that carries a length is a still that looks like a clip in
        anything grouping by it."""
        stored = assert_ok(_upload(auth_client, make_png(), "p.png"), status=201)["files"][0]

        assert stored["duration_ms"] == 0


class TestTheFeedTakesClipsOnly:
    def test_a_two_minute_clip_is_in_the_feed(self, auth_client, author):
        _clip(auth_client, MP4, "a.mp4", TWO_MINUTES)

        items = assert_ok(auth_client.get("/api/v1/videos?kind=video"))

        assert len(items) == 1, "a two-minute clip should be in «Клипы»"

    def test_a_four_minute_video_is_not(self, auth_client, author):
        """The reason the threshold exists: a reader who cannot swipe past a video
        is stuck on it."""
        _clip(auth_client, MP4, "a.mp4", FOUR_MINUTES)

        items = assert_ok(auth_client.get("/api/v1/videos?kind=video"))

        assert items == [], "a long video reached the clip feed"

    def test_exactly_three_minutes_is_a_clip(self, auth_client, author):
        """The boundary is inclusive, and the test says so: an off-by-one here puts
        every three-minute video on the wrong side depending on which comparison
        somebody happened to write."""
        _clip(auth_client, MP4, "a.mp4", THREE_MINUTES)

        items = assert_ok(auth_client.get("/api/v1/videos?kind=video"))

        assert len(items) == 1, "three minutes exactly should still be a clip"

    def test_one_millisecond_over_is_not(self, auth_client, author):
        _clip(auth_client, MP4, "a.mp4", THREE_MINUTES + 1)

        items = assert_ok(auth_client.get("/api/v1/videos?kind=video"))

        assert items == [], "three minutes and a millisecond is not a clip"

    def test_an_unreported_length_stays_in_the_feed(self, auth_client, author):
        """A client that sends no length gets the feed, not the library. It is the
        better place to be wrong: an over-long clip in the feed is a screen the
        reader swipes past."""
        _clip(auth_client, MP4, "a.mp4")

        items = assert_ok(auth_client.get("/api/v1/videos?kind=video"))

        assert len(items) == 1

    def test_a_long_clip_on_a_photo_post_is_not_shown(self, auth_client, author):
        """A post with a photo and a long clip is admitted by the "has a photo"
        condition, so the choice of what fills the screen has to apply the same rule
        again. Without that second check the query admits the post and the screen
        shows the long clip anyway."""
        stored = assert_ok(_upload(auth_client, MP4, "a.mp4", FOUR_MINUTES), status=201)["files"][0]
        photo = assert_ok(_upload(auth_client, make_png(), "p.png"), status=201)["files"][0]
        assert_ok(
            auth_client.post(
                "/api/v1/posts",
                json={
                    "body": "смешанное",
                    "media": [
                        {"storage_key": photo["storage_key"]},
                        {"storage_key": stored["storage_key"]},
                    ],
                },
            ),
            status=201,
        )

        items = assert_ok(auth_client.get("/api/v1/videos?kind=video"))

        assert items == [], "the long clip reached the screen despite the filter"

    def test_photos_are_unaffected_by_the_threshold(self, auth_client, author):
        """The duration rule is about clips. A photo feed must not start dropping
        posts because the column happens to default to zero."""
        photo = assert_ok(_upload(auth_client, make_png(), "p.png"), status=201)["files"][0]
        assert_ok(
            auth_client.post(
                "/api/v1/posts", json={"body": "фото", "media": [{"storage_key": photo["storage_key"]}]}
            ),
            status=201,
        )

        items = assert_ok(auth_client.get("/api/v1/videos?kind=photo"))

        assert len(items) == 1


class TestTheLibraryTakesLongVideosOnly:
    def test_a_four_minute_video_is_listed(self, auth_client, author):
        _clip(auth_client, MP4, "a.mp4", FOUR_MINUTES)

        items = assert_ok(auth_client.get("/api/v1/videos/library"))

        assert len(items) == 1, "a four-minute video should be in «Эфир»"

    def test_a_short_clip_is_not(self, auth_client, author):
        _clip(auth_client, MP4, "a.mp4", TWO_MINUTES)

        items = assert_ok(auth_client.get("/api/v1/videos/library"))

        assert items == [], "a two-minute clip reached the library"

    def test_the_card_carries_the_length_and_the_flag(self, auth_client, author):
        """The badge and the two feeds both need to know which side of the line a
        video is on without a second request."""
        _clip(auth_client, MP4, "a.mp4", FOUR_MINUTES)

        item = assert_ok(auth_client.get("/api/v1/videos/library"))[0]

        assert item["media"][0]["duration_ms"] == FOUR_MINUTES
        assert item["media"][0]["is_short_clip"] is False

    def test_a_clip_is_flagged_as_short(self, auth_client, author):
        _clip(auth_client, MP4, "a.mp4", TWO_MINUTES)

        item = assert_ok(auth_client.get("/api/v1/videos?kind=video"))[0]

        assert item["media"][0]["is_short_clip"] is True

    def test_the_two_feeds_partition_the_videos(self, auth_client, author):
        """Together they hold every video, with none in both. The natural way to get
        this wrong is to make one query a subset of the other without noticing."""
        _clip(auth_client, MP4, "short.mp4", TWO_MINUTES)
        _clip(auth_client, MP4, "long.mp4", FOUR_MINUTES)

        clips = assert_ok(auth_client.get("/api/v1/videos?kind=video"))
        library = assert_ok(auth_client.get("/api/v1/videos/library"))

        clip_ids = {item["id"] for item in clips}
        library_ids = {item["id"] for item in library}
        assert len(clip_ids & library_ids) == 0, "a video is in both feeds"
        assert len(clip_ids | library_ids) == 2, "a video is in neither feed"


class TestLibraryControls:
    def test_an_unknown_sort_is_refused(self, auth_client):
        assert_error(auth_client.get("/api/v1/videos/library?sort=sideways"), status=422)

    def test_the_sorts_are_all_accepted(self, auth_client, author):
        _clip(auth_client, MP4, "a.mp4", FOUR_MINUTES)

        for sort in ("recent", "views", "longest"):
            response = auth_client.get(f"/api/v1/videos/library?sort={sort}")
            assert response.status_code == 200, sort
            assert len(assert_ok(response)) == 1, sort

    def test_an_unknown_author_is_a_404(self, auth_client):
        assert_error(auth_client.get("/api/v1/videos/library?author=nobody_here"), status=404)

    def test_one_authors_videos_can_be_listed(self, client, user, make_user):
        """The `client` fixture is re-signed-in rather than given a second one: two
        test clients share nothing, and `anon_client` shows how much scaffolding a
        second one needs. Re-signing is what the API itself would do."""
        other = make_user(username="other_author")
        client.login(other.username)
        _clip(client, MP4, "a.mp4", FOUR_MINUTES)

        items = assert_ok(client.get("/api/v1/videos/library?author=other_author"))

        assert len(items) == 1
        assert items[0]["author"]["username"] == "other_author"

    def test_a_guest_can_read_the_library(self, anon_client, auth_client, author):
        """A link somebody sent. The clip feed was once missing from the public
        routes and every signed-out visitor was redirected to the login page for a
        page that existed and worked."""
        _clip(auth_client, MP4, "a.mp4", FOUR_MINUTES)

        response = anon_client.get("/api/v1/videos/library")

        assert response.status_code == 200, response.get_data(as_text=True)
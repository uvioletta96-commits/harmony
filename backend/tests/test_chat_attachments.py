"""Attachments on chat messages: photos, video, voice messages and voice circles.

The four kinds share one table and one upload path, and the interesting cases are
the boundaries between them:

  - a file may be *presented* as something it is not. The bytes decide what it is;
    the client renders what the server says. Coercing it quietly hides a client bug.
  - an attachment cannot be claimed twice, or one photo can be replayed into fifty
    messages.
  - an attachment belonging to another account must be a 404, not a silent skip -
    a client that thinks it attached something should be told.
  - a voice message with no text is a message; a voice message with no duration
    cannot be drawn as a progress bar, so the client's figure is bounded rather than
    trusted.
"""

from __future__ import annotations

import io

import pytest

from tests.utils import assert_error, assert_ok, make_png

# The photo path re-encodes to strip EXIF, so the bytes have to decode - a file that
# only carries a JPEG signature is rejected as `corrupt_image` before anything else
# is checked. Video and audio are stored byte-for-byte and are never decoded, so
# for those a valid header is the whole contract.
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 40


def _jpeg() -> bytes:
    return make_png()


@pytest.fixture()
def thread(client, make_user):
    """Two accounts with an open conversation, signed in as the sender.

    Signing in has to happen before the upload: `get_conversation` answers 404 for a
    non-member rather than 403, so an unsigned-in fixture looks exactly like a
    conversation that does not exist.
    """
    from app.services import chat_service

    alice, bob = make_user(username="alice_chat"), make_user(username="bob_chat")
    conversation, _created = chat_service.get_or_create_direct(alice, bob)
    client.login(alice.username)
    return client, alice, bob, conversation


def _upload(client, conversation, blob: bytes, filename: str, kind: str, **extra):
    return client.upload(
        f"/api/v1/conversations/{conversation.public_id}/media",
        files={"file": (io.BytesIO(blob), filename, "application/octet-stream")},
        form={"kind": kind, **extra},
    )


class TestUpload:
    def test_a_photo_is_accepted_and_stored(self, thread):
        client, _alice, _bob, conversation = thread

        response = _upload(client, conversation, _jpeg(), "photo.jpg", "image")

        assert response.status_code == 201, response.get_data(as_text=True)
        stored = response.get_json()["data"]["files"][0]
        assert stored["kind"] == "image"
        assert stored["mime_type"] == "image/png", "the stored type is what the bytes are, not what the file was named"

    def test_a_video_is_accepted(self, thread):
        client, _alice, _bob, conversation = thread

        stored = _upload(client, conversation, MP4, "clip.mp4", "video").get_json()["data"]["files"][0]

        assert stored["kind"] == "video"
        assert stored["mime_type"] == "video/mp4"

    def test_a_voice_recording_is_accepted_with_its_duration(self, thread):
        client, _alice, _bob, conversation = thread

        stored = _upload(
            client, conversation, WEBM, "voice.webm", "voice", duration_ms="4200"
        ).get_json()["data"]["files"][0]

        assert stored["kind"] == "voice"
        assert stored["duration_ms"] == 4200

    def test_a_circle_is_accepted(self, thread):
        client, _alice, _bob, conversation = thread

        stored = _upload(
            client, conversation, WEBM, "circle.webm", "circle", duration_ms="1500"
        ).get_json()["data"]["files"][0]

        assert stored["kind"] == "circle"

    def test_the_waveform_is_kept(self, thread):
        """The player draws bars from it; without them it has to decode the audio to
        render a shape."""
        import json

        client, _alice, _bob, conversation = thread
        peaks = [10, 40, 90, 30]

        stored = client.upload(
            f"/api/v1/conversations/{conversation.public_id}/media",
            files={"file": (io.BytesIO(WEBM), "v.webm", "application/octet-stream")},
            form={"kind": "voice", "waveform": json.dumps(peaks)},
        ).get_json()["data"]["files"][0]

        assert stored["waveform"] == peaks

    def test_the_waveform_is_bounded(self, thread):
        """A client that reports ten thousand peaks must not make the reader's
        phone render ten thousand bars."""
        import json

        client, _alice, _bob, conversation = thread

        stored = client.upload(
            f"/api/v1/conversations/{conversation.public_id}/media",
            files={"file": (io.BytesIO(WEBM), "v.webm", "application/octet-stream")},
            form={"kind": "voice", "waveform": json.dumps([5] * 5000)},
        ).get_json()["data"]["files"][0]

        assert len(stored["waveform"]) <= 96

    def test_the_waveform_is_clamped_to_a_percentage(self, thread):
        import json

        client, _alice, _bob, conversation = thread

        stored = client.upload(
            f"/api/v1/conversations/{conversation.public_id}/media",
            files={"file": (io.BytesIO(WEBM), "v.webm", "application/octet-stream")},
            form={"kind": "voice", "waveform": json.dumps([-5, 500])},
        ).get_json()["data"]["files"][0]

        assert stored["waveform"] == [0, 100]

    def test_a_lying_duration_is_bounded(self, thread):
        """A client claiming a year-long recording must not produce a progress bar
        nobody can scrub."""
        client, _alice, _bob, conversation = thread

        stored = _upload(
            client, conversation, WEBM, "v.webm", "voice", duration_ms="99999999999"
        ).get_json()["data"]["files"][0]

        assert stored["duration_ms"] <= 300_000

    def test_a_garbage_duration_is_ignored_rather_than_crashing(self, thread):
        client, _alice, _bob, conversation = thread

        stored = _upload(
            client, conversation, WEBM, "v.webm", "voice", duration_ms="soon"
        ).get_json()["data"]["files"][0]

        assert stored["duration_ms"] == 0

    def test_a_file_that_is_not_what_it_claims_is_refused(self, thread):
        """The bytes decide. A photo uploaded as a voice recording is a client bug,
        and correcting it quietly hides that until the rendering is wrong."""
        client, _alice, _bob, conversation = thread

        assert_error(
            _upload(client, conversation, _jpeg(), "lying.webm", "voice"),
            status=415,
        )

    def test_a_video_uploaded_as_a_photo_is_refused(self, thread):
        client, _alice, _bob, conversation = thread

        assert_error(_upload(client, conversation, MP4, "lying.jpg", "image"), status=415)

    def test_an_unrecognisable_file_is_refused(self, thread):
        client, _alice, _bob, conversation = thread

        assert_error(
            _upload(client, conversation, b"not a media file at all", "x.bin", "image"),
            status=415,
        )

    def test_an_unknown_kind_is_refused(self, thread):
        client, _alice, _bob, conversation = thread

        assert_error(_upload(client, conversation, _jpeg(), "p.jpg", "hologram"), status=422)

    def test_nobody_but_a_member_can_upload(self, client, make_user, thread):
        """Bytes are not written for a conversation the sender cannot use.

        404 rather than 403: `get_conversation` answers "not found" to a non-member
        so that a stranger learns nothing, not even that the thread exists. Uploading
        is scoped the same way as reading and sending.
        """
        _client, alice, bob, conversation = thread
        stranger = make_user(username="stranger_chat")

        client.login(stranger.username)

        assert_error(
            _upload(client, conversation, _jpeg(), "p.jpg", "image"),
            status=404,
        )
        assert alice.id != stranger.id and bob.id != stranger.id


class TestSending:
    def test_a_photo_only_message_is_sent(self, thread):
        """No text is fine when a file is attached - that is the common case."""
        client, _alice, _bob, conversation = thread
        attachment = _upload(client, conversation, _jpeg(), "p.jpg", "image").get_json()["data"]["files"][0]

        response = client.post(
            f"/api/v1/conversations/{conversation.public_id}/messages",
            json={"body": "", "media_ids": [attachment["id"]]},
        )

        assert response.status_code == 201, response.get_data(as_text=True)
        message = response.get_json()["data"]["message"]
        assert message["body"] == ""
        assert len(message["attachments"]) == 1
        assert message["attachments"][0]["kind"] == "image"

    def test_a_voice_message_needs_no_text(self, thread):
        client, _alice, _bob, conversation = thread
        attachment = _upload(
            client, conversation, WEBM, "v.webm", "voice", duration_ms="3000"
        ).get_json()["data"]["files"][0]

        response = client.post(
            f"/api/v1/conversations/{conversation.public_id}/messages",
            json={"media_ids": [attachment["id"]]},
        )

        assert response.status_code == 201
        assert response.get_json()["data"]["message"]["attachments"][0]["duration_ms"] == 3000

    def test_several_photos_keep_their_order(self, thread):
        client, _alice, _bob, conversation = thread
        ids = [
            _upload(client, conversation, _jpeg(), f"p{n}.jpg", "image").get_json()["data"]["files"][0]["id"]
            for n in range(3)
        ]

        message = client.post(
            f"/api/v1/conversations/{conversation.public_id}/messages",
            json={"body": "три", "media_ids": ids},
        ).get_json()["data"]["message"]

        # Ids cross the wire as strings everywhere else in the API, so comparing
        # against the integers the upload returned asserts a type mismatch rather
        # than an ordering.
        assert [a["id"] for a in message["attachments"]] == [str(i) for i in ids]

    def test_a_message_with_neither_text_nor_file_is_refused(self, thread):
        client, _alice, _bob, conversation = thread

        assert_error(
            client.post(
                f"/api/v1/conversations/{conversation.public_id}/messages", json={"body": "   "}
            ),
            status=422,
        )

    def test_an_attachment_cannot_be_claimed_twice(self, thread):
        """Otherwise one upload could be replayed into fifty messages."""
        client, _alice, _bob, conversation = thread
        attachment = _upload(client, conversation, _jpeg(), "p.jpg", "image").get_json()["data"]["files"][0]
        path = f"/api/v1/conversations/{conversation.public_id}/messages"

        assert client.post(path, json={"body": "первый", "media_ids": [attachment["id"]]}).status_code == 201
        assert_error(client.post(path, json={"body": "второй", "media_ids": [attachment["id"]]}), status=409)

    def test_somebody_elses_attachment_is_not_found(self, thread, make_user):
        """A 404, not a silent skip: a client that believes it attached something
        should be told the attachment is not its own."""
        client, _alice, _bob, conversation = thread
        attachment = _upload(client, conversation, _jpeg(), "p.jpg", "image").get_json()["data"]["files"][0]

        mallory = make_user(username="mallory_chat")
        client.login(mallory.username)

        assert_error(
            client.post(
                f"/api/v1/conversations/{conversation.public_id}/messages",
                json={"body": "моё", "media_ids": [attachment["id"]]},
            ),
            status=404,
        )

    def test_a_nonsense_attachment_id_is_refused(self, thread):
        client, _alice, _bob, conversation = thread

        assert_error(
            client.post(
                f"/api/v1/conversations/{conversation.public_id}/messages",
                json={"body": "что", "media_ids": ["не число"]},
            ),
            status=422,
        )

    def test_the_thread_preview_names_a_file_only_message(self, thread):
        """Otherwise a photo-only message leaves an empty row and the thread looks
        finished when it is not."""
        client, _alice, _bob, conversation = thread
        attachment = _upload(client, conversation, _jpeg(), "p.jpg", "image").get_json()["data"]["files"][0]

        client.post(
            f"/api/v1/conversations/{conversation.public_id}/messages",
            json={"media_ids": [attachment["id"]]},
        )

        listed = client.get("/api/v1/conversations").get_json()["data"]["conversations"]
        preview = next(row["last_message_preview"] for row in listed)
        assert preview, "a photo-only message left no preview"

    def test_a_voice_preview_names_itself(self, thread):
        client, _alice, _bob, conversation = thread
        attachment = _upload(
            client, conversation, WEBM, "v.webm", "voice"
        ).get_json()["data"]["files"][0]

        client.post(
            f"/api/v1/conversations/{conversation.public_id}/messages",
            json={"media_ids": [attachment["id"]]},
        )

        listed = client.get("/api/v1/conversations").get_json()["data"]["conversations"]
        preview = next(row["last_message_preview"] for row in listed)
        assert "олос" in preview, preview


class TestDeletion:
    def test_deleting_a_message_takes_its_files_with_it(self, thread):
        """A "deleted" message that still serves its photos is not deleted.

        History excludes deleted rows outright rather than returning a tombstone,
        so the assertion is that the message is gone from the thread and its
        attachment rows are gone with it - not merely that its body was cleared.
        """
        from app.extensions import db
        from app.models.chat import MessageAttachment

        client, _alice, _bob, conversation = thread
        attachment = _upload(client, conversation, _jpeg(), "p.jpg", "image").get_json()["data"]["files"][0]
        message = client.post(
            f"/api/v1/conversations/{conversation.public_id}/messages",
            json={"body": "секрет", "media_ids": [attachment["id"]]},
        ).get_json()["data"]["message"]
        assert len(message["attachments"]) == 1

        assert client.delete(f"/api/v1/messages/{message['id']}").status_code == 204

        history = assert_ok(client.get(f"/api/v1/conversations/{conversation.public_id}/messages"))
        assert message["id"] not in [row["id"] for row in history], (
            "the deleted message is still readable in the thread"
        )
        assert not db.session.get(MessageAttachment, attachment["id"]), (
            "the attachment row outlived its message"
        )
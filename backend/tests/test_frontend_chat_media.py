"""Chat attachments on the client: composer controls, recording, players, teardown.

These are structural checks against the source rather than a running browser,
which is the same discipline the vertical feed's tests use. The things pinned here
are the ones that are invisible in a screenshot and expensive to discover later:
a microphone that never releases, a recording sent twice, a chip that comes back
after being removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEDIA = (ROOT / "frontend" / "js" / "components" / "chatMedia.js").read_text(encoding="utf-8")
CHAT = (ROOT / "frontend" / "js" / "pages" / "chat.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "css" / "components.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def media():
    return MEDIA


@pytest.fixture(scope="module")
def chat():
    return CHAT


def code_only(source: str) -> str:
    """Source with comments stripped.

    Several of the rules below name the very properties they forbid, so a presence
    check on the raw text reports correct code as broken.
    """
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.S)


def body_of(source: str, header: str) -> str:
    """The block that starts at `header` and ends at the next dedent to `header`."""
    start = source.index(header)
    rest = source[start:]
    lines = rest.split("\n")
    indent = len(lines[0]) - len(lines[0].lstrip())
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecording:
    def test_the_container_is_asked_for_rather_than_assumed(self, media):
        """Safari and iOS have no `audio/webm`. Asking for one there produces a
        recorder that records nothing at all, and silently."""
        assert "isTypeSupported" in media, (
            "the container is hard-coded, so the recorder produces silence on Safari "
            "and iOS rather than failing loudly"
        )
        probe = body_of(media, "function preferredMimeType()")
        assert "audio/mp4" in probe, "no MP4 option, which is the only one iOS offers"

    def test_the_microphone_is_released(self, media):
        """A stream whose tracks are never stopped is a microphone indicator that
        stays lit after the reader has let go."""
        cleanup = body_of(media, "  function cleanup()")
        assert "track.stop()" in cleanup, "the stream's tracks are never stopped"
        assert "clearInterval" in cleanup, "the level and tick timers keep running"

        # And it is reached from every exit.
        stop = body_of(media, "    stop() {")
        assert "cleanup()" in stop, "stopping normally leaves the microphone open"
        cancel = body_of(media, "    cancel() {")
        assert "cleanup()" in cancel, "cancelling leaves the microphone open"

    def test_a_mis_tap_sends_nothing(self, media):
        """Under 400ms is a thumb brushing the button, and the bubble it produces
        has nothing in it the reader can hear."""
        stop = body_of(media, "    stop() {")
        assert "400" in stop, (
            "a recording of any length is accepted, so a mis-tap becomes an "
            "unspeakable message"
        )
        assert "resolve(null)" in stop, "the short case does not resolve to nothing"

    def test_cancelling_resolves_to_nothing_rather_than_rejecting(self, media):
        """Releasing the button early is a decision, not an error."""
        stop = body_of(media, "    stop() {")
        assert "if (stopped) return Promise.resolve(null)" in code_only(stop), (
            "stopping twice rejects, so a pointerleave after a pointerup surfaces as "
            "an error the reader did not cause"
        )

    def test_the_recording_is_capped_before_it_is_uploaded(self, media):
        """A recording the server will refuse is worse than one that stopped
        cleanly."""
        assert "MAX_SECONDS" in media, "an unbounded recording is uploaded and refused"
        tick = body_of(media, "    tickTimer = setInterval(() => {")
        assert "MAX_SECONDS" in tick, (
            "the cap is defined but never applied while recording"
        )

    def test_the_waveform_cannot_leak_an_audio_context(self, media):
        """Chrome keeps an AudioContext alive until it is closed, so one leaked per
        recording is one leaked per message."""
        waveform = body_of(media, "async function computeWaveform(blob)")
        assert "finally" in waveform, "a failed decode leaves the context open"
        assert "close" in waveform, "the AudioContext is never closed"

    def test_the_waveform_is_normalised(self, media):
        """A quiet recording should still draw a readable shape rather than a flat
        line, and the reader watches this to decide whether to play the message."""
        assert "loudest" in media, (
            "peaks are used as absolute amplitudes, so a quiet recording draws as "
            "silence and a loud one clips to a solid block"
        )


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


class TestComposer:
    def test_the_tray_is_built_from_one_source(self, chat):
        """The earlier version removed a chip's node and then re-appended it, so a
        reader who removed a photo got it back."""
        paint = body_of(chat, "  const paintTray = () => {")
        assert "pending.map" in code_only(paint), (
            "the tray is not redrawn from the pending list, so removing a chip and "
            "the list disagree"
        )

    def test_removing_a_chip_actually_removes_it(self, chat):
        remove = re.search(r"remove\.addEventListener\('click', \(\) => \{(.*?)\n    \}\);", chat, re.S)
        assert remove, "no remove handler"
        inner = code_only(remove.group(1))
        assert "pending.splice" in inner, "the attachment stays on the pending list"
        # And nothing re-adds it.
        for forbidden in ("replaceChild", "append(chip)", "append(remove)"):
            assert forbidden not in inner, (
                f"the handler calls {forbidden} after removing, so the chip comes back"
            )

    def test_a_message_can_be_files_only(self, chat):
        """The common case. A send button gated on text alone makes it unsendable."""
        send = body_of(chat, "  const send = async () => {")
        assert "!body && !files.length" in code_only(send), (
            "sending requires text, so a photo with no caption cannot be sent"
        )
        sync = body_of(chat, "  const syncSend = () => {")
        assert "pending.length" in sync, "the send button ignores pending files"

    def test_recording_is_hold_to_record(self, chat):
        """The microphone is open only while a finger is down, so there is no
        recording left running after the reader lets go."""
        assert "pointerdown" in chat, "recording starts only on a click"
        for name in ("pointerup", "pointercancel", "pointerleave"):
            assert name in chat, f"{name} does not end the recording"

    def test_recording_is_reachable_from_the_keyboard(self, chat):
        """Hold-to-record has no keyboard gesture of its own, so without this the
        only way to record is a pointer."""
        record = body_of(chat, "  const recordButton = el('button', {")
        tail = chat[chat.index(record):]
        keydown = re.search(r"recordButton\.addEventListener\('keydown'.*?\n  \}\);", tail, re.S)
        assert keydown, "the record button has no keydown handler"
        assert "Enter" in keydown.group(0) or " " in keydown.group(0), (
            "no key starts or stops the recording"
        )

    def test_the_record_button_is_not_a_text_selection(self, chat):
        """A long press on a button is a context menu and a text selection on every
        mobile browser, which is exactly the gesture recording depends on."""
        for prop in ("touch-action", "user-select", "-webkit-touch-callout"):
            assert prop in CSS, f"{prop} is not set on the record button"

    def test_a_refused_microphone_is_named(self, chat):
        """A refused permission and a browser with no device both land in the same
        catch, and the reader needs to know which."""
        begin = body_of(chat, "  const beginRecording = async () => {")
        assert "NotAllowedError" in begin, (
            "a refused microphone permission is reported as a generic failure"
        )


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


class TestPlayback:
    def test_one_voice_message_plays_at_a_time(self, media):
        """Three overlapping recordings on a phone is noise with no way to tell
        which to mute."""
        assert "chat-audio" in media, "the audio elements are not addressable"
        voice = body_of(media, "export function voiceMessage(attachment)")
        assert "other.pause()" in voice, (
            "two voice messages can play at once"
        )

    def test_the_waveform_marks_what_has_played(self, media):
        """The bars are the progress indicator; without a played state the reader
        cannot tell where they are without a separate element."""
        voice = body_of(media, "export function voiceMessage(attachment)")
        assert "is-played" in voice, "the waveform never shows progress"

    def test_the_circle_shows_its_length_without_a_waveform(self, media):
        """A circle has nowhere to put bars, so the length is an arc around the
        avatar - readable without a legend."""
        circle = body_of(media, "export function voiceCircle(attachment, sender)")
        assert "--played" in circle, "the ring does not show progress"
        assert "conic-gradient" in CSS, "the ring is not drawn as an arc"

    def test_the_circle_shows_the_senders_own_avatar(self, media):
        """Otherwise it is an anonymous circle and the reader cannot tell who is
        speaking."""
        circle = body_of(media, "export function voiceCircle(attachment, sender)")
        assert "avatar_url" in circle, "the circle does not show the sender's avatar"

    def test_leaving_the_thread_stops_everything(self, chat):
        """An audio element that keeps playing after the thread is gone keeps
        pulling bytes over a mobile connection for audio nobody can reach the
        control for."""
        unmount = body_of(chat, "    unmount() {")
        assert ".pause?.()" in unmount, (
            "media left playing after the thread is unmounted"
        )

    def test_leaving_the_thread_releases_the_microphone(self, chat):
        """A recording left running after the reader navigates away is a
        microphone indicator that never goes away, and the button that stops it has
        just left the document."""
        unmount = body_of(chat, "    unmount() {")
        assert "stopRecording" in unmount, (
            "leaving the thread does not release the microphone"
        )
        # And it is cancelled rather than stopped, so nothing is recorded into the
        # void and then uploaded.
        composer = body_of(chat, "    stopRecording: () => {")
        assert "cancel()" in composer, (
            "unmount finishes the recording instead of discarding it, so a file is "
            "uploaded for a thread the reader has left"
        )

    def test_the_microphone_is_released_before_anything_else(self, chat):
        """The pause loop touches nodes that are about to be replaced; teardown that
        throws part-way leaves the microphone open, which is the part the reader
        cannot fix."""
        unmount = body_of(chat, "    unmount() {")
        assert unmount.index("stopRecording") < unmount.index(".pause?.()"), (
            "media is paused before the microphone is released"
        )

    def test_an_unplayable_recording_is_reported(self, media):
        """A voice message that silently does nothing looks identical to one the
        reader has ignored."""
        voice = body_of(media, "export function voiceMessage(attachment)")
        assert "audio.play().catch" in code_only(voice), (
            "a rejected play is swallowed, so a broken recording looks untapped"
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_an_empty_body_does_not_leave_a_gap(self, chat):
        """A photo-only message would otherwise get an empty text div: a gap in the
        bubble and a stray set of line breaks under the picture."""
        bubble = body_of(chat, "function bubble(message, mine) {")
        assert "message.body ? el('div', { class: 'bubble-text'" in bubble, (
            "the text node is built unconditionally, so a photo-only message has an "
            "empty paragraph in it"
        )

    def test_a_photo_is_not_cropped(self):
        """The point of sending a picture in a message is that the other person sees
        the thing that was sent."""
        image_rule = re.search(r"\.bubble-image,\n\.bubble-video \{(.*?)\n\}", CSS, re.S)
        assert image_rule, "no rule for a photo inside a bubble"
        body = image_rule.group(1)
        assert "object-fit: contain" in body, (
            "a portrait photo is cropped to a fixed height, so the reader sees only "
            "the middle of what was sent"
        )

    def test_a_photo_bubble_is_not_capped_at_the_text_width(self):
        """The 72% cap exists so a long sentence wraps. Applying it to a photograph
        just makes it small."""
        assert re.search(r"\.bubble-has-media \{[^}]*max-width:\s*min\(8", CSS), (
            "a bubble carrying a photo is still limited to the text width"
        )

    def test_every_class_the_player_writes_has_a_rule(self, media):
        """An unstyled class is invisible until someone opens devtools. Checked by
        name so a rename in one file without the other is caught here."""
        used = set(re.findall(r"class:\s*'([a-z][a-z0-9-]*(?:\s+[a-z0-9-]+)*)'", media))
        names = {
            part
            for group in used
            for part in group.split()
            if part.startswith(("voice-", "circle-", "bubble-", "composer-", "chat-audio"))
        }
        missing = sorted(name for name in names if f".{name}" not in CSS)
        assert not missing, f"used in JS but never styled: {missing}"

    def test_a_deleted_message_shows_no_files(self):
        """Server-side: a soft delete that clears the body leaves the attachment
        rows, and the URL still resolves."""
        model = (ROOT / "backend" / "app" / "models" / "chat.py").read_text(encoding="utf-8")
        deleted = re.search(r"if self\.status == MessageStatus\.DELETED\.value:(.*?)\n        return", model, re.S)
        assert deleted, "no deleted-message branch"
        assert '"attachments": []' in deleted.group(1), (
            "a deleted message still serialises its attachments"
        )

    def test_a_bubble_with_a_photo_opens_it(self, chat):
        """Tapping a photograph in a message is the only way to see it full size on
        a phone."""
        bubble = body_of(chat, "function bubble(message, mine) {")
        assert "onOpen" in bubble, "the bubble does not wire a lightbox"

    def test_a_lone_video_does_not_autoplay_with_sound(self, media):
        """The same rule the vertical feed follows: a file that starts making noise
        without being asked is the thing users complain about."""
        video = body_of(media, "export function mediaItem(attachment, { onOpen } = {})")
        code = code_only(video)
        assert "autoplay" not in code, "a chat video autoplays"
        assert "muted: false" not in code and 'muted: \'false\'' not in code


class TestClientServerAgreement:
    """The client and the server must agree on the vocabulary, or a file is stored
    under one kind and drawn as another."""

    def test_the_four_kinds_match(self, media):
        api = (ROOT / "backend" / "app" / "api" / "chat.py").read_text(encoding="utf-8")
        assert '"image", "video", "voice", "circle"' in api, (
            "the endpoint accepts a set of kinds the client does not send, or the "
            "reverse"
        )
        for kind in ("image", "video", "voice", "circle"):
            assert kind in media, f"the client never sends kind={kind}"

    def test_the_client_sends_the_field_the_server_reads(self, chat):
        api = (ROOT / "backend" / "app" / "api" / "chat.py").read_text(encoding="utf-8")
        assert 'request.form.get("duration_ms")' in api
        assert 'request.form.get("waveform")' in api, (
            "the waveform is read only from the JSON body, so on the multipart path "
            "every voice message is drawn with no bars"
        )
        # A JSON body, so the field name is an object key rather than a FormData
        # append - which is exactly why guessing the shape here is a way to write a
        # passing test for a broken send.
        assert "media_ids: mediaIds" in code_only(chat), (
            "the client does not claim attachments the way the server expects"
        )

    def test_the_upload_path_is_the_one_the_server_serves(self):
        api = (ROOT / "backend" / "app" / "api" / "chat.py").read_text(encoding="utf-8")
        route = re.search(r'@bp\.post\("(/conversations/[^"]+)"\)', api)
        assert route, "no attachment route"
        # The client's template has an {id} in it; the server's has a converter.
        assert route.group(1).replace("<conversation_id>", "{id}") == "/conversations/{id}/media", route.group(1)
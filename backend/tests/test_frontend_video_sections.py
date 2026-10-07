"""«Клипы» and «Эфир»: immersive mode, the clip/library split, and what breaks.

The things here are the ones a screenshot cannot show: that the immersive class is
removed on the way out, that a long video cannot reach the feed through a second
path, and that the client's duration reading survives a file the browser cannot
decode.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEOS = (ROOT / "frontend" / "js" / "pages" / "videos.js").read_text(encoding="utf-8")
EFIR = (ROOT / "frontend" / "js" / "pages" / "efir.js").read_text(encoding="utf-8")
CHAT = (ROOT / "frontend" / "js" / "pages" / "chat.js").read_text(encoding="utf-8")
MEDIA = (ROOT / "frontend" / "js" / "components" / "chatMedia.js").read_text(encoding="utf-8")
COMPOSER = (ROOT / "frontend" / "js" / "components" / "composer.js").read_text(encoding="utf-8")
SHELL = (ROOT / "frontend" / "js" / "components" / "shell.js").read_text(encoding="utf-8")
MAIN = (ROOT / "frontend" / "js" / "main.js").read_text(encoding="utf-8")
VIDEOS_CSS = (ROOT / "frontend" / "css" / "videos.css").read_text(encoding="utf-8")
EFIR_CSS = (ROOT / "frontend" / "css" / "efir.css").read_text(encoding="utf-8")


def body_of(source: str, header: str) -> str:
    """The block starting at `header`, from its opening brace to its match.

    Balanced braces rather than indentation: the earlier version walked lines and
    stopped at the first one dedented to the header's column, which cut every block
    containing a nested `const fn = () => {` in half - so the tests that inspect
    these blocks were reading truncated text and passing for the wrong reason.
    """
    start = source.index(header)
    # Skip the parameter list before looking for the body's brace. A destructured
    # default (`{ onLevel, onTick } = {}`) contains braces of its own, and matching
    # from the first of them balances against the body and returns the header alone.
    paren = source.index(")", source.index("(", start))
    brace = source.index("{", paren)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"no closing brace for {header!r}")


def code_only(source: str) -> str:
    """Comments stripped.

    Several checks below name the very properties they forbid, so a presence check
    on raw text reports correct code as broken - and one ordering check has to see
    through a docstring that discusses the bug it is testing.
    """
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.S)


# ---------------------------------------------------------------------------
# Immersive mode
# ---------------------------------------------------------------------------


class TestImmersive:
    def test_arriving_hides_the_sites_own_chrome(self):
        """The part the reader means by "на весь экран". The browser's own bars are
        already at the top and bottom; ours were in between, eating a fifth of the
        height."""
        assert "body.is-immersive" in VIDEOS_CSS, (
            "nothing hides the site's header, tabs or bottom bar, so the clip sits in "
            "a slot rather than filling the display"
        )
        for selector in (".topbar", ".mobile-nav", ".videos-tabs", ".videos-head"):
            assert f"body.is-immersive {selector}" in VIDEOS_CSS, (
                f"{selector} is still visible in immersive mode"
            )

    def test_there_is_a_way_out(self):
        """Hiding every exit from a full-screen mode is the failure worth avoiding
        rather than the tidy one to aim for."""
        assert "body.is-immersive .videos-exit" in VIDEOS_CSS, (
            "the exit button is hidden with everything else, so a reader who went "
            "immersive on a phone cannot get back"
        )
        assert "body.is-immersive .videos-exit" in VIDEOS_CSS.split(".videos-still")[0], (
            "the exit button is restyled for immersive mode but never shown"
        )

    def test_the_class_is_removed_when_the_page_is_left(self):
        """It is set on `body`, which outlives the page. Left behind, every other
        route renders with its chrome hidden and no way to bring it back."""
        unmount = body_of(VIDEOS, "export function unmount()")
        assert "classList.remove('is-immersive')" in unmount, (
            "the immersive class is added to body and never removed, so every "
            "subsequent page renders without its own header"
        )

    def test_it_is_remembered_but_can_be_turned_off(self):
        """A reader who left immersive once should not be put straight back on every
        visit - a mode you cannot easily re-enter feels hostile."""
        arm = body_of(VIDEOS, "function armAutoFullscreen()")
        assert "videosImmersive" in arm, "the choice is not remembered"
        assert "store.get(IMMERSIVE_KEY) !== false" in arm, (
            "immersive mode cannot be declined once"
        )

    def test_the_browser_fullscreen_waits_for_a_gesture(self):
        """It cannot happen on load: `requestFullscreen` needs a user activation, so
        a call from `render()` is refused by every engine. The reader's first tap is
        the first moment it can work."""
        arm = body_of(VIDEOS, "function armAutoFullscreen()")
        assert "requestFullscreen" not in arm, (
            "fullscreen is requested while rendering, where no browser will grant it"
        )
        assert "enterBrowserFullscreen" in arm
        for event in ("touchstart", "pointerdown", "keydown"):
            assert event in arm, f"{event} does not arm the request"

    def test_the_gesture_listener_removes_itself(self):
        """Otherwise the second tap is swallowed by a listener whose only job was
        the first, and it fights the double-tap gesture that likes a post."""
        arm = body_of(VIDEOS, "function armAutoFullscreen()")
        assert "disarm" in arm, "the gesture listeners are never removed"
        disarm = body_of(arm, "const disarm = () => {")
        assert "removeEventListener" in disarm, "disarm does not remove anything"
        assert "armed" in disarm, "disarm can fire twice and does not guard"

    def test_a_refused_request_is_silent(self):
        """The site's chrome is already hidden by then, so a refusal costs the reader
        nothing - and a toast on arrival is noise about something they did not ask
        for."""
        fire = body_of(VIDEOS, "function enterBrowserFullscreen()")
        assert "catch" in fire, "a refused fullscreen throws out of the handler"
        assert "toast" not in fire, (
            "a refusal to fullscreen reports itself, when the reader cannot tell it "
            "failed from the page having simply loaded"
        )

    def test_a_restored_scroll_position_does_not_count_as_a_gesture(self):
        """A reader can arrive with the wheel already turning over a position the
        browser restored, which is not them asking for anything."""
        arm = body_of(VIDEOS, "function armAutoFullscreen()")
        scroll_line = re.search(r"window\.addEventListener\('scroll',[^\n]*", arm)
        assert scroll_line, "scroll does not arm the request at all"
        assert "passive: true" in scroll_line.group(0)
        assert "// `scroll` does not count" in arm or "does not count" in arm, (
            "scroll is armed without saying why, and will fire on a restored position"
        )


# ---------------------------------------------------------------------------
# Fitting the clip
# ---------------------------------------------------------------------------


class TestClipFitting:
    def test_a_clip_fills_its_screen(self):
        """`contain` on a 16:9 clip in a 19.5:9 phone letterboxes it into about
        half the screen, which is what "обрезается по размеру" describes."""
        rule = re.search(r"\.videos-clip \{(.*?)\n\}", VIDEOS_CSS, re.S)
        assert rule, "no .videos-clip rule"
        assert "object-fit: cover" in rule.group(1), (
            "the clip is letterboxed rather than filling the screen"
        )

    def test_a_photo_is_still_not_cropped(self):
        """The opposite trade on purpose. A still has no panning gesture, so `cover`
        would hide part of the picture and the reader would never know."""
        assert ".videos-still { object-fit: contain; }" in VIDEOS_CSS, (
            "a photo is cropped to fill its screen, silently hiding part of it"
        )

    def test_landscape_gets_the_other_trade(self):
        """A portrait clip with `cover` on a landscape phone throws away the top and
        the bottom - somebody's face and whatever they are holding."""
        landscape = re.search(
            r"@media \(orientation: landscape\)[^{]*\{(.*?)\n\}", VIDEOS_CSS, re.S
        )
        assert landscape, "no landscape override"
        assert "object-fit: contain" in landscape.group(1), (
            "a portrait clip is cropped top and bottom on a landscape phone"
        )

    def test_the_crop_is_advertised(self):
        """`cover` crops the sides of a wide clip. The reader can pan with a finger,
        which is not obvious - and without saying so it looks like a bug."""
        assert "cursor: grab" in VIDEOS_CSS, (
            "the clip is cropped with no indication that it can be panned"
        )


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


class TestTheSplit:
    def test_the_two_sections_are_named_differently(self):
        """'Видео' for both would leave a reader with two identically-named tabs and
        no way to tell a swipeable clip from a video to watch."""
        assert "Клипы" in SHELL, "the short feed is still called 'Видео'"
        assert "Эфир" in SHELL, "the library has no name of its own"
        assert "Видео'" not in SHELL, "a bare 'Видео' still labels one of them"

    def test_the_library_is_reachable_from_the_clip_feed(self):
        """Otherwise the only way in is the desktop rail, which does not exist on a
        phone - and the bottom bar is hidden in immersive mode."""
        page = VIDEOS[VIDEOS.index("const shell = el('section'"):]
        page = page[: page.index("\n  function setChrome")]
        assert "'/efir'" in page, (
            "there is no link from the clip feed to the library, so on a phone there "
            "is no way to reach it at all"
        )

    def test_the_library_is_a_public_route(self):
        """A link somebody sent. `/videos` was once missing here and every
        signed-out visitor was redirected to the login page for a page that
        existed and worked."""
        routes = re.search(r"const PUBLIC_ROUTES = \[(.*?)\];", MAIN, re.S)
        assert routes, "no PUBLIC_ROUTES"
        assert "'/efir'" in routes.group(1), (
            "the library redirects a signed-out visitor to the login page, so a link "
            "somebody sent does not work"
        )

    def test_the_library_has_its_own_route(self):
        assert "router.define('/efir'" in MAIN, "the library page is unreachable"

    def test_the_client_sends_the_length_the_server_uses(self):
        """The server cannot decode a container - there is no ffmpeg on this machine
        - so a client that sends no length sends a video the server cannot place."""
        assert "durations" in COMPOSER, (
            "the composer never reports how long a clip is, so the server has to "
            "guess and every video lands in the same feed"
        )
        assert "measureDuration" in COMPOSER

    def test_the_length_is_read_before_the_file_is_sent(self):
        """The only moment the browser knows it: after upload there is nothing left
        to read, because the bytes are on the server and the container header is not
        decoded anywhere."""
        upload = body_of(COMPOSER, "  async function uploadPending()")
        assert upload.index("measureDuration") < upload.index("api.upload"), (
            "the duration is measured after the upload, where it is not knowable"
        )

    def test_an_unmeasurable_file_uploads_anyway(self):
        """Zero, not a refusal. A clip the browser cannot measure still plays; a
        video refused over a number nobody sees does not exist for the reader."""
        measure = body_of(COMPOSER, "  async function measureDuration(file)")
        assert "error" in measure, "a file the browser cannot decode never resolves"
        assert "finish(0)" in measure, "the failure path does not resolve to zero"
        assert re.search(r"setTimeout\(\(\) => finish\(0\)", measure), (
            "a file that loads neither metadata nor an error leaves the upload "
            "hanging for ever"
        )

    def test_an_infinite_duration_is_not_a_long_video(self):
        """What a container with no duration header reports, which a live recording
        or a badly muxed file can. Treating it as hours would file it in the
        library."""
        measure = body_of(COMPOSER, "  async function measureDuration(file)")
        assert "isFinite" in measure, (
            "an Infinity duration is stored as-is and files the clip in the library"
        )

    def test_the_object_url_is_released(self):
        """An object URL whose file is in memory pins the whole video; a reader
        picking several clips would hold all of them."""
        measure = body_of(COMPOSER, "  async function measureDuration(file)")
        assert "URL.revokeObjectURL(url)" in measure
        assert "finally" in measure, "a failed measurement leaks the URL"

    def test_the_library_shows_the_length_and_a_player(self):
        """A card without a length tells a reader nothing about what they are about
        to spend four minutes on."""
        card = body_of(EFIR, "function videoCard(item)")
        assert "duration_ms" in card, "the card badge does not show the length"
        assert "controls" in EFIR, (
            "the library's player has no controls, so a long video cannot be seeked "
            "in - which is the reason it is not in the feed"
        )

    def test_a_long_video_does_not_autoplay_in_the_feed(self):
        """The feed's own rule: sound is on, so nothing plays without being asked."""
        render = body_of(EFIR, "export async function render({ author = null } = {})")
        assert "autoplay" not in render, "the library grid starts playing by itself"


# ---------------------------------------------------------------------------
# Recording on an insecure origin
# ---------------------------------------------------------------------------


class TestTheRecordingBlocker:
    """The bug the reader reported as "браузер не умеет записывать голосовые".

    Measured on the deployed site over plain HTTP:

        isSecureContext                false
        typeof navigator.mediaDevices  "undefined"
        typeof MediaRecorder           "function"

    The encoder exists and the microphone does not. "Does your browser support
    recording" is the wrong question, and answering it wrongly sends a reader
    looking for a setting that does not exist.
    """

    def test_the_reason_names_https_rather_than_the_browser(self):
        blocker = body_of(MEDIA, "export function recordBlocker()")
        assert "isSecureContext" in blocker, (
            "the blocker does not check the secure context, which is the condition "
            "that actually fails here"
        )
        assert "HTTPS" in blocker, (
            "the message blames the browser for a browser security rule the site "
            "cannot work around"
        )

    def test_the_media_devices_object_is_not_probed_directly_first(self):
        """`navigator.mediaDevices` is `undefined` here, so a check that starts there
        and falls through reports "your browser cannot record" - the wrong answer,
        and the one the reader was given."""
        code = code_only(body_of(MEDIA, "export function recordBlocker()"))
        assert code.index("isSecureContext") < code.index("mediaDevices"), (
            "the insecure-origin case is not reported as itself"
        )

    def test_the_button_looks_unavailable_instead_of_broken(self):
        """A button that accepts a tap and then says no is exactly what "не
        нажимается" describes."""
        build = body_of(CHAT, "  if (blocked) {")
        assert "is-blocked" in build, (
            "the record button looks the same whether or not it can work"
        )
        assert "composer-mic-note" in build, (
            "the reason is only given when pressed, and a button that does nothing "
            "when pressed is the complaint"
        )

    def test_the_reason_is_visible_not_only_on_press(self):
        build = body_of(CHAT, "  if (blocked) {")
        assert "insertAdjacentElement" in build, (
            "the note is built only for the tooltip, so it is never read"
        )

    def test_the_mode_toggle_still_works(self):
        """Choosing between a bar and a circle needs no microphone.

        Both buttons are greyed, because both lead to a recording and pretending
        otherwise would be worse. But the toggle must stay live: `disabled` and
        `pointer-events: none` would take its click handler with it, and a reader
        would lose the mode switch as well as the recording.
        """
        build = code_only(body_of(CHAT, "  if (blocked) {"))
        # `aria-disabled` is the right thing and is not what this is about: it
        # announces the state to a screen reader while leaving the control focusable
        # and clickable. The `disabled` *property* is what would remove the handler.
        assert not re.search(r"\.disabled\s*=|disabled:\s*true", build), (
            "the buttons are disabled outright, which removes their click handlers - "
            "so the reader loses the mode switch as well as the recording"
        )
        assert "aria-disabled" in build, (
            "the state is not announced at all, so a screen reader passes over a "
            "button that cannot work"
        )
        assert "pointer-events" not in build, (
            "the buttons stop receiving taps, which is a disabled control with worse "
            "feedback"
        )
        # And the toggle's own handler is never guarded on `blocked`.
        toggle = code_only(body_of(CHAT, "circleButton.addEventListener('click', () => {"))
        assert "blocked" not in toggle, (
            "the circle mode toggle is gated on recording being available, so a "
            "reader cannot see that the two modes exist"
        )

    def test_the_thrown_error_carries_the_reason(self):
        """The composer's own catch would otherwise flatten every cause into one
        generic message - which is how this reached the reader as a browser bug."""
        code = code_only(body_of(MEDIA, "export async function startRecording({ onLevel, onTick } = {})"))
        assert "recordBlocker()" in code, (
            "startRecording checks its own idea of what is supported rather than the "
            "one reason the composer already computed"
        )
        # Built and then thrown, with a flag, so the composer's catch can tell "you
        # cannot" from "the microphone was refused" - which need different messages.
        assert re.search(r"new Error\(blocked\)", code), "the reason is discarded before throwing"
        assert ".blocked = true" in code, (
            "the thrown error carries no marker, so the composer's catch cannot tell "
            "an unavailable microphone from a refused permission"
        )

    def test_no_leftover_generic_message(self):
        """The old wording, in either of the two places that used to say it.

        Matched with the closing quote so the surviving message for the genuinely
        unsupported case - "Браузер не умеет записывать голосовые", for a browser
        with no encoder at all - is not mistaken for it. That message is correct for
        its case and is kept.
        """
        stale = "Браузер не умеет записывать голос'"
        for source, where in ((MEDIA, "chatMedia.js"), (CHAT, "chat.js")):
            assert stale not in source, (
                f"{where} still reports a browser limitation rather than HTTPS"
            )
        # The one message that legitimately blames the browser - a browser with no
        # encoder at all, which is a real and different case - is kept.
        assert "Браузер не умеет записывать голосовые'" in MEDIA

    def test_can_record_still_answers_the_simple_question(self):
        """It is exported and may be used elsewhere; the specific reason is the new
        entry point, not a replacement for this one."""
        assert "export function canRecord()" in MEDIA
        assert "recordBlocker() === null" in MEDIA
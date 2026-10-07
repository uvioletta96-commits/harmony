"""Hold to double speed, double tap to seek, and a seek bar on short clips.

The hard part is not any one gesture. It is that four gestures now share one small
screen: a tap pauses, a double tap likes, a hold doubles the speed, and a drag on
the bar scrubs. Every way they can be confused produces a symptom that looks like a
different bug - a clip that pauses when the reader was scrubbing, a like that fires
because somebody was seeking, a clip left running at 2× for the next post.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GESTURES = (ROOT / "frontend" / "js" / "components" / "videoGestures.js").read_text(encoding="utf-8")
VIDEOS = (ROOT / "frontend" / "js" / "pages" / "videos.js").read_text(encoding="utf-8")
EFIR = (ROOT / "frontend" / "js" / "pages" / "efir.js").read_text(encoding="utf-8")
VIDEOS_CSS = (ROOT / "frontend" / "css" / "videos.css").read_text(encoding="utf-8")


def block(source: str, header: str) -> str:
    """The block at `header`, from its body's brace to its match."""
    start = source.index(header)
    paren = source.index(")", source.index("(", start))
    brace = source.index("{", paren)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"no closing brace for {header!r}")


def code(source: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", source, flags=re.S)


# ---------------------------------------------------------------------------
# Hold to double speed
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_the_hold_is_registered_for_teardown(self):
        """A hold running when the reader navigates away has nothing left to release
        it. The `dismiss` path covers closing the player; this covers leaving the
        page with the player open, which is a different route entirely."""
        efir = code(block(EFIR, "function openPlayer(item, media)"))
        push = re.search(r"teardown\.push\(\(\)\s*=>\s*\{([^}]*)\}\);", efir)
        assert push, "the player registers no teardown"
        assert "hold.cancel()" in push.group(1), (
            "leaving the page with a player open leaves its hold running"
        )
        assert "seek.cancel()" in push.group(1), (
            "leaving the page leaves a seek indicator on the screen forever"
        )


class TestHold:
    def test_the_rate_is_restored_on_release(self):
        """The whole point of a hold is that it ends. A clip left at 2× is one the
        reader swipes back to and finds inexplicably fast."""
        hold = code(block(GESTURES, "export function holdToSpeed(target, {"))
        assert "video.playbackRate = 2" in hold, "the hold does not change the rate"
        assert "video.playbackRate = 1" in hold, (
            "the rate is never restored, so every clip after a hold is double speed"
        )
        for event in ("pointerup", "pointercancel", "pointerleave"):
            assert event in hold, f"{event} does not end the hold, leaving the rate at 2"

    def test_it_is_cancelled_on_teardown(self):
        """A hold running when the reader navigates away has nothing left to release
        it, and the next clip starts at 2× for no stated reason."""
        assert "cancel: stop" in GESTURES, "the hold cannot be stopped from outside"
        assert "bound.hold?.cancel()" in VIDEOS, (
            "leaving a clip does not release its hold, so the rate survives the slide"
        )
        assert "hold.cancel()" in EFIR, (
            "closing the player does not release the hold"
        )

    def test_a_hold_cancels_the_pending_tap(self):
        """The interaction that matters most on the clip feed.

        A tap pauses, and a tap is deferred by one tap interval so a double tap does
        not also pause. So a hold ends with a click - and without the guard, releasing
        a two-second hold would pause the clip the reader was deliberately speeding
        through.
        """
        click = code(block(VIDEOS, "    bound.onClick = () => {"))
        assert "bound.hold?.holding" in click, (
            "the click that ends a hold also queues a pause, so a deliberate hold "
            "pauses the clip when released"
        )

    def test_a_second_finger_is_not_a_hold(self):
        """Somebody pinching to zoom or scrubbing is not asking for double speed, and
        dropping the rate mid-gesture is worse than not offering it."""
        down = code(block(GESTURES, "  const down = (event) => {"))
        assert "isPrimary === false" in down, (
            "a second finger arms the hold, so a two-handed pinch doubles the rate"
        )

    def test_only_the_primary_button_arms_it(self):
        """A right-click is a context menu, not a hold."""
        down = code(block(GESTURES, "  const down = (event) => {"))
        assert "button !== 0" in down, "a right-click arms the hold"

    def test_the_badge_is_shown(self):
        """A clip at 2× looks exactly like a clip at 1×. A reader who holds and
        nothing appears to happen will assume the gesture is not supported, and never
        try it again."""
        assert "is-held" in GESTURES, "holding changes the rate and shows nothing"
        assert "is-held::after" in VIDEOS_CSS, "the hold has no visible state"
        assert "content: '2×'" in VIDEOS_CSS, "no badge saying the clip is doubled"

    def test_the_badge_does_not_intercept_the_finger(self):
        """It is drawn over the middle of the clip, and a pointer-events rule that
        lets it catch the press would make the middle of the screen not-a-hold."""
        rule = re.search(r"\.videos-stage\.is-held::after,[^{]*\{([^}]*)\}", VIDEOS_CSS, re.S)
        assert rule, "no rule for the hold badge"
        assert "pointer-events: none" in rule.group(1), (
            "the badge catches the pointer, so the middle of the screen cannot be held"
        )


# ---------------------------------------------------------------------------
# Double tap to seek
# ---------------------------------------------------------------------------


class TestSeek:
    def test_the_left_half_goes_back_and_the_right_half_goes_forward(self):
        """One gesture that has to mean two things, so the screen is split. Anywhere
        else would be a guess, and a reader who wanted to go forward and went back
        would not try again."""
        seek = code(block(GESTURES, "  const dbl = (event) => {"))
        assert "forward = x >" in seek, "the direction is not taken from where the tap landed"
        assert "Math.min" in seek and "Math.max(0" in seek, (
            "a seek is not clamped, so it can run past either end of the video"
        )

    def test_the_click_coordinates_are_used_not_the_elements(self):
        """During a double tap the pointer moves a few pixels. A reader who meant to
        tap the right side and landed one pixel over the midpoint should not go
        backwards."""
        seek = code(block(GESTURES, "  const dbl = (event) => {"))
        assert "event.clientX" in seek, (
            "the direction is taken from the element's bounds, so a double tap that "
            "drifts a few pixels seeks the wrong way"
        )

    def test_it_says_which_way_and_by_how_much(self):
        """A seek with no visible response reads as a dropped frame, and a reader
        will tap again - and again - until the clip is somewhere they did not ask
        for."""
        assert "seek-flash" in GESTURES, "the seek has no visible response"
        assert "is-forward" in GESTURES and "is-back" in GESTURES, (
            "the flash does not distinguish the two directions"
        )
        assert "Вперёд на" in GESTURES and "Назад на" in GESTURES, (
            "the flash says something happened but not what"
        )

    def test_the_two_directions_do_not_overwrite_each_other(self):
        """A pair of flashes in the same place, one on top of the other, for a
        gesture that is meant to be one action."""
        flash = code(block(GESTURES, "  const flash = (text, forward) => {"))
        assert "hide()" in flash, (
            "a second flash is added without clearing the first, so two overlapping "
            "flashes stack on the same spot for a gesture meant to be one action"
        )
        assert flash.index("hide()") < flash.index("target.append(indicator)"), (
            "the previous flash is cleared after the new one is put on screen, which "
            "removes the new one too"
        )
        # And `hide` is what actually detaches the node.
        assert "indicator?.remove()" in code(block(GESTURES, "  const hide = () => {"))

    def test_a_flash_cannot_be_stacked_by_holding_the_button_down(self):
        """Repeated taps while the first animation runs leave nodes behind for a
        while; the indicator is removed on a timer as well as on the next gesture."""
        assert "setTimeout(hide" in GESTURES, (
            "the flash is only removed by the next gesture, so it can linger"
        )

    def test_it_does_not_fire_on_the_native_control_bar(self):
        """The player's own controls are drawn inside the video, and a press on the
        scrub or the play button is not a double tap. Dropping the rate because
        somebody pressed play would be worse than not offering the gesture."""
        efir = code(block(EFIR, "  const CONTROLS_ZONE = 0.16;"))
        assert "clientY" in efir, "the controls zone is measured from the left edge"
        assert "overControls" in efir
        own_seek = re.search(r"own:\s*\(event\)\s*=>\s*!overControls\(event\)", EFIR)
        assert own_seek, "the seek fires over the browser's own control bar"

    def test_the_speed_menu_is_excluded_from_the_hold(self):
        """Opening the speed menu must not double the rate."""
        hold = re.search(r"own:\s*\(event\)\s*=>[^,]*efir-speed", EFIR)
        assert hold, "pressing the speed button also arms the hold"


# ---------------------------------------------------------------------------
# The seek bar
# ---------------------------------------------------------------------------


class TestScrubber:
    def test_it_exists_on_a_clip(self):
        assert "buildScrubber" in VIDEOS, "a clip has no way to be seeked"
        assert ".videos-scrub" in VIDEOS_CSS, "the bar has no rules"

    def test_a_drag_on_the_bar_is_not_also_a_tap(self):
        """The interaction that would make the bar useless.

        The stage reads a tap as pause and a double tap as like. A reader dragging
        the bar - which ends in a release, which the browser calls a click - would
        pause the clip they were trying to scrub, and a quick double-tap-and-adjust
        would like the post.
        """
        down = code(block(VIDEOS, "  const down = (event) => {"))
        assert "stopPropagation" in down, (
            "a press on the bar reaches the stage, so dragging scrubs *and* pauses"
        )
        click = code(block(VIDEOS, "  const click = (event) => event.stopPropagation();"))
        assert click, "the release after a drag is not stopped"

    def test_it_does_not_arm_the_hold(self):
        """Otherwise a reader dragging the bar finds the clip speeding up under their
        finger."""
        own = re.search(r"own:\s*\(pressEvent\)\s*=>[^,]*videos-scrub", VIDEOS)
        assert own, "a press on the seek bar also arms the double-speed hold"

    def test_the_touch_target_is_far_bigger_than_the_line(self):
        """A two-pixel bar is a two-pixel target for a thumb, however good it
        looks."""
        rule = re.search(r"\.videos-scrub \{([^}]*)\}", VIDEOS_CSS, re.S)
        assert rule, "no .videos-scrub rule"
        assert "touch-action: none" in rule.group(1), (
            "the browser may take the gesture for scrolling, so the bar never sees it"
        )
        padded = re.search(r"\.videos-scrub \{[^}]*padding:[^;]*;", VIDEOS_CSS, re.S)
        assert padded, "the bar is only as tall as its two-pixel line"
        track = re.search(r"\.videos-scrub-track \{([^}]*)\}", VIDEOS_CSS)
        assert track and "height: 2px" in track.group(1), (
            "the drawn line is not the thin one; the padding is what makes it a target"
        )

    def test_it_is_thin_until_touched(self):
        """A permanent fat bar across the bottom of a full-bleed clip is a permanent
        obstruction, and it sits exactly where the caption is."""
        assert ".videos-scrub.is-dragging .videos-scrub-thumb" in VIDEOS_CSS, (
            "the bar does not respond to being dragged"
        )
        assert "scale(0)" in VIDEOS_CSS, "the thumb is visible when the bar is not being touched"

    def test_a_drag_keeps_working_past_the_end_of_the_bar(self):
        """Otherwise scrubbing past either end stops dead, which is the one place a
        reader pushes hardest."""
        down = code(block(VIDEOS, "  const down = (event) => {"))
        assert "setPointerCapture" in down, (
            "the drag ends the moment the finger leaves the bar"
        )
        ratio = code(block(VIDEOS, "  const ratioAt = (clientX) => {"))
        assert "Math.max(0" in ratio and "Math.min(1" in ratio, (
            "a press outside the bar seeks to an unbounded position"
        )

    def test_it_is_reachable_from_a_keyboard(self):
        """A slider that cannot be reached with a keyboard is not a slider."""
        rule = re.search(r"\.videos-scrub \{([^}]*)\}", VIDEOS_CSS, re.S)
        assert rule
        keys = code(block(VIDEOS, "  bar.addEventListener('keydown', (event) => {"))
        assert "ArrowRight" in keys and "ArrowLeft" in keys, "no arrow-key seeking"
        assert "Home" in keys and "End" in keys, "no way to jump to either end"
        # Built through `el()`, so the role is an object key rather than an attribute
        # in the markup.
        assert re.search(r"role:\s*'slider'", VIDEOS), (
            "the bar is not announced as a slider, so a screen reader passes over the "
            "only way to seek a clip without a pointer"
        )
        assert re.search(r"tabindex:\s*'0'", VIDEOS), "the bar cannot be focused"
        assert "'aria-valuenow'" in VIDEOS or "aria-valuenow" in VIDEOS, (
            "the bar does not report its position to a screen reader"
        )
        assert "aria-valuetext" in VIDEOS, (
            "the bar announces a percentage rather than a time, which is not what the "
            "reader is choosing"
        )

    def test_the_polling_stops(self):
        """One interval per clip, all of them polling a detached element after the
        page is left."""
        assert "dispose()" in VIDEOS, "the seek bar cannot be torn down"
        assert "clearInterval(timer)" in VIDEOS
        assert "scrubber?.dispose()" in VIDEOS, (
            "leaving the page leaves every clip's interval running"
        )

    def test_it_is_reachable_when_there_is_no_length_yet(self):
        """A clip whose metadata has not arrived has a duration of NaN, and every
        seek computed from it is NaN - which sets `currentTime` to nothing and looks
        like a bar that does not work."""
        assert "duration() <= 0" in VIDEOS or "total <= 0" in VIDEOS, (
            "the bar seeks without checking that the video has a length"
        )

    def test_it_ignores_repaints_that_change_nothing(self):
        """A layout write on every tick, for a bar whose value has not visibly
        changed."""
        assert "0.002" in VIDEOS, "the bar repaints on every tick regardless"
        assert "dragging ||" in VIDEOS, (
            "the poll fights the drag, so the bar jumps back to the playback position "
            "under the reader's finger"
        )


class TestBothSurfaces:
    def test_efir_has_both_gestures(self):
        assert "holdToSpeed(frame" in EFIR, "a long video cannot be held for double speed"
        assert "doubleTapToSeek(frame" in EFIR, "a long video cannot be seeked by tapping"

    def test_clips_have_the_hold_and_the_bar(self):
        assert "holdToSpeed(stage" in VIDEOS, "a short clip cannot be held for double speed"
        assert "buildScrubber(video)" in VIDEOS, "a short clip has no seek bar"

    def test_neither_surface_autoplays_a_held_clip(self):
        """The gesture changes the rate of a clip that is already playing. It must
        not start one that is not - the site's rule everywhere is that sound is on
        and nothing plays without being asked."""
        hold = code(block(GESTURES, "export function holdToSpeed(target, {"))
        assert ".play()" not in hold, "holding starts a paused clip"
        assert "paused" not in hold, "the gesture inspects the play state to start it"

    def test_the_same_module_serves_both(self):
        """Two copies of a gesture is two copies to keep in step, and the two
        surfaces are meant to feel like the same site."""
        assert GESTURES.count("export function holdToSpeed") == 1
        assert GESTURES.count("export function doubleTapToSeek") == 1
        assert "videoGestures.js" in VIDEOS and "videoGestures.js" in EFIR
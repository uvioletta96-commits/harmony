"""The vertical feed must not defer the image the reader is looking at.

Found on the deployed site: the first photo screen rendered a blank rectangle.
`loading="lazy"` was on every photo, and it stays that way in most codebases
because it usually works. It did not here - see below.
"""

from __future__ import annotations

import pathlib
import re

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
JS = FRONTEND / "js"
VIDEOS = JS / "pages" / "videos.js"
CSS = FRONTEND / "css" / "videos.css"


@pytest.fixture(scope="module")
def source() -> str:
    return VIDEOS.read_text(encoding="utf-8")


def test_a_photo_is_not_loaded_lazily(source):
    """`loading="lazy"` on a snap-scroller photo leaves the first screen blank.

    The browser decides what is "near the viewport" against the layout viewport.
    The slide is inside a nested scroller whose content extends past it, so the
    estimate is wrong for exactly the screen the reader is on. Measured: bytes
    arrive with a 200, the same bytes decode as a detached image, and the one on
    screen stays at naturalWidth 0 forever.
    """
    image_block = re.search(r"el\('img',\s*\{(.*?)\}\)", source, re.S)
    assert image_block, "no <img> found in videos.js - the photo path changed?"

    assert "loading:" not in image_block.group(1), (
        "a photo in the vertical feed must not be loading=\"lazy\": on a snap "
        "scroller the browser's viewport estimate misses the current slide and the "
        "reader gets a blank rectangle"
    )


def test_a_photo_carries_its_intrinsic_size(source):
    """So the box is the right shape before the bytes arrive.

    Without width and height the caption and the controls shift when the image
    lands, which on a full-bleed screen is very visible.
    """
    image_block = re.search(r"el\('img',\s*\{(.*?)\}\)", source, re.S)
    assert "width:" in image_block.group(1)
    assert "height:" in image_block.group(1)


def test_decoding_is_still_async(source):
    """Deferring the decode is what keeps scrolling smooth, and unlike `loading`
    it does not defer deciding whether to fetch."""
    image_block = re.search(r"el\('img',\s*\{(.*?)\}\)", source, re.S)
    assert "decoding: 'async'" in image_block.group(1)


def test_the_scroller_is_the_scroll_container_not_the_document(source):
    """The reason the viewport estimate goes wrong, kept so the fix is not undone
    by moving the feed back into the document flow."""
    assert "'videos-scroller'" in source
    css = CSS.read_text(encoding="utf-8")
    scroller_rule = re.search(r"\.videos-scroller \{(.*?)\}", css, re.S)
    assert "overflow-y: auto" in scroller_rule.group(1)
    page_rule = re.search(r"\.videos-page \{(.*?)\}", css, re.S)
    assert "overflow: hidden" in page_rule.group(1), (
        "the page must clip, or a swipe at the end scrolls the document and the "
        "header slides away"
    )


def test_fullscreen_hides_the_sites_own_chrome():
    """In fullscreen the browser's bars are already gone. The site's header, tabs
    and exit button are three more controls between the reader and the video.

    Keyed on `body:has(...)` rather than on `.videos-page:fullscreen` alone,
    because iOS Safari only offers fullscreen on the *video element* - so the
    page never becomes the fullscreen element and the old rule never applied on
    the platform most readers are on. The old selector is kept as a fallback for
    browsers where the container is the fullscreen element.
    """
    css = CSS.read_text(encoding="utf-8")
    body_rule = re.search(r"body:has\([^)]*fullscreen[^)]*\)[^{]*\{[^}]*display:\s*none", css, re.S)
    assert body_rule, (
        "nothing hides the header when the *video* is the fullscreen element, which "
        "is the only route iOS Safari offers"
    )
    assert ".videos-head" in body_rule.group(0), "the body rule hides something that is not the header"


def test_the_fullscreen_key_is_handled_off_the_scroller(source):
    """`f` did nothing.

    The handler was bound to the scroller, which has `tabindex="0"` but is never
    focused: a reader arrives by tapping, so nothing has focus at all and the
    single-letter shortcuts never fired. Bound to the document while the page is
    mounted, with teardown to remove it, and ignoring keys typed into a field.
    """
    assert "document.addEventListener('keydown'" in source, (
        "the shortcut handler is not on the document, so a reader who has not "
        "focused the scroller cannot use it"
    )
    assert "'f'" in source, "no f key"

    handler = re.search(r"const onDocumentKeyDown = \(event\) => \{(.*?)\n  \};", source, re.S)
    assert handler, "no document key handler to inspect"
    body = handler.group(1)
    for guard, why in (
        ("isContentEditable", "so `f` typed into a comment box does not go fullscreen"),
        ("INPUT", "so a shortcut does not fire while the reader is typing"),
        ("metaKey", "so `cmd+f` stays the browser's find"),
    ):
        assert guard in body, f"the document key handler ignores {guard}, {why}"

    teardown = re.search(r"document\.removeEventListener\('keydown', onDocumentKeyDown\)", source)
    assert teardown, "the document key handler is never removed, so it outlives the page"


def test_fullscreen_prefers_the_video_element(source):
    """Asking for the scroller on iOS "succeeds" and then shows a black rectangle
    with no way out. The video is the only element iOS will honour."""
    handler = re.search(r"async function toggleFullscreen\(\) \{(.*?)\n  \}", source, re.S)
    assert handler, "no toggleFullscreen to inspect"
    body = handler.group(1)
    assert "requestFullscreen" in body
    assert "webkitRequestFullscreen" in body, (
        "iOS Safari only has the prefixed form, and it is the platform most "
        "readers are on"
    )
    assert re.search(r"const element = video \|\| target", body), (
        "fullscreen is requested on the container rather than the video, so on iOS "
        "it shows a black rectangle with no way out"
    )
    # `navigationUI` is the specific thing that made it throw.
    assert "navigationUI" not in body, (
        "requestFullscreen({navigationUI: 'hide'}) is rejected by some engines, so "
        "the whole call throws and nothing happens"
    )


def test_the_deferred_pause_is_cleared_on_teardown(source):
    """A pending pause that is never cancelled fires after the page is gone, and
    touches a node that is no longer in the document."""
    blocks = re.findall(r"teardown\.push\(\(\) => \{(.*?)\n  \}\);", source, re.S)
    assert blocks, "no teardown blocks"
    assert any("clearTimeout" in block for block in blocks), (
        "no teardown entry clears the pending-tap timer, so a scheduled pause fires "
        "after the page has gone"
    )


def test_a_double_tap_does_not_also_pause(source):
    """The two gestures overlap for the first few hundred milliseconds.

    So the pause has to be deferred past the double-tap window, not applied on the
    first click. Measured on the deployed site with the per-slide binding: the heart
    appeared and the clip stopped, which means a double tap both liked *and*
    paused - the reader has to tap twice more just to get sound back.

    `event.detail` cannot be used to tell them apart: the first click of a real pair
    already arrives with `detail === 1`. Only a deferred decision works, cancelled
    by the `dblclick` that follows.
    """
    handler = re.search(r"bound\.onClick = \(\) => \{(.*?)\n    \};", source, re.S)
    assert handler, "the click handler does not defer its decision"

    body = handler.group(1)
    assert "setTimeout" in body, (
        "the pause fires on the first click, so every double tap also pauses - and "
        "event.detail cannot be used instead, because the first click of a pair "
        "already arrives with detail 1"
    )

    dbl = re.search(r"bound\.onDblClick = \(event\) => \{(.*?)\n    \};", source, re.S)
    assert dbl, "no double-tap handler to cancel the pending pause"

    # The guard matters as much as the call. `if (false) clearTimeout(...)` still
    # contains `clearTimeout`, and the pending pause is never cancelled - which is
    # the bug this test exists for, and which a presence-only check reports as
    # fixed. Verified by making the guard constant and watching the test pass anyway.
    body = dbl.group(1)
    assert "clearTimeout(tapState.timer)" in body, (
        "dblclick does not cancel the pending pause, so a double tap pauses as well "
        "as liking"
    )
    # The guard matters as much as the call: `if (false) clearTimeout(...)` still
    # contains `clearTimeout`, and the pause is never cancelled. Checked by making
    # the guard constant and watching this test fail - a presence-only check
    # reported that broken version as fixed.
    assert re.search(r"if \(\s*!?\w+\.timer\s*\)\s*\{\s*clearTimeout\(\w+\.timer\)", body), (
        "the cancel is not guarded on the pending timer, so it never runs"
    )



def test_the_scroller_is_the_offset_parent_for_its_slides():
    """`offsetTop` is measured from the nearest *positioned* ancestor, and the
    scroller was not one.

    Measured on the deployed site: the first slide reported `offsetTop: 195`
    while `scrollTop` started at 0. "Nearest slide to the middle of the viewport"
    was therefore comparing a scroll-relative number against a page-relative one
    and was wrong by the height of the header - so it named the current screen
    incorrectly for the whole scroll, and named it differently three times per
    swipe.

    The JS now uses `getBoundingClientRect`, which puts both sides in one
    coordinate system. This rule makes `offsetTop` correct as well, so the two
    cannot be used inconsistently by a later change.
    """
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.videos-scroller \{(.*?)\n\}", css, re.S)
    assert rule, "no .videos-scroller rule"
    assert "position: relative" in rule.group(1), (
        "the scroller is not the offset parent for its slides, so a slide's "
        "offsetTop is measured from the page rather than from the scroll position"
    )


def test_the_current_screen_is_measured_in_one_coordinate_system(source):
    """Both sides of the "which slide is current" comparison must be viewport
    coordinates.

    Mixing `offsetTop` with `scrollTop` is the bug this pins: it looks cheaper,
    reads identically on the first slide, and is wrong by the height of the header
    on every one of them.
    """
    sync = re.search(r"const syncPlaybackToScroll = \(\) => \{(.*?)\n  \};", source, re.S)
    assert sync, "no syncPlaybackToScroll to inspect"
    body = sync.group(1)

    assert "getBoundingClientRect" in body, (
        "the current screen is not measured with getBoundingClientRect, so it "
        "compares offsetTop (page-relative) against scrollTop (scroll-relative)"
    )

    # Comments are stripped first: the block explains this very bug and names both
    # properties, so a presence check on the raw text reports the fix as broken.
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)
    assert "offsetTop" not in code, (
        "offsetTop is page-relative unless the scroller is positioned; use "
        "getBoundingClientRect so the answer does not depend on that"
    )
    assert "scrollTop" not in code, (
        "scrollTop is scroll-relative and must not be mixed with a viewport "
        "measurement"
    )


def test_scrolling_does_not_restart_the_current_clip(source):
    """The bug the reader reported: the sound cutting and restarting over and over.

    Measured on the deployed site, one screen of scrolling produced `plays: 3,
    pauses: 3`; six seconds of doing nothing produced none of either. So the churn
    was entirely scroll-driven, and the cause is that `play()` stops whatever was
    playing first - so any reach of the sync that does not change the current
    screen restarts the clip from zero, and the nearest-slide calculation flips
    more than once as a snap settles.

    The fix is the early return, and it is the whole fix. Pinned here because the
    code looks redundant: there is a second, weaker guard further down that
    catches most of it, which is exactly why it was written twice and fixed once.
    """
    sync = re.search(r"const syncPlaybackToScroll = \(\) => \{(.*?)\n  \};", source, re.S)
    assert sync, "no syncPlaybackToScroll to inspect"
    body = sync.group(1)

    early = re.search(r"if \(best === current\) return;", body)
    assert early, (
        "the sync runs its playback work on every scroll event instead of only when "
        "the current screen actually changes, so the clip restarts from zero and the "
        "sound cuts and restarts on each swipe"
    )

    # And it must come before anything that touches playback, otherwise it guards
    # nothing.
    play_call = body.find("play(video)")
    assert 0 <= early.start() < play_call, (
        "the early return is after the play() call, so it no longer prevents the "
        "restart"
    )


def test_playback_stops_when_leaving_the_page(source):
    """Sound continuing with nothing on screen.

    Not hypothetical: this was the failure the first version of this file's header
    comment was written to prevent, and the only thing that catches it is `unmount`.

    Checked as a sequence rather than by matching one shape of code: the teardown
    block grew a second arrow function when the gesture bindings moved out of the
    delegated handlers, so a test that pinned one `teardown.push` would have started
    failing for a reason that has nothing to do with playback.
    """
    assert re.search(r"export function unmount\b", source)

    unmount = re.search(r"export function unmount\b(.*?)\n}", source, re.S)
    assert unmount, "no unmount() to inspect"
    body = unmount.group(1)
    assert "teardown" in body, "unmount() does not run the teardown list"
    assert "stopPlayback()" in body, "unmount() leaves the clip playing"

    # And every teardown entry is inspected together, because there are now more
    # than one and a test that only looked at the first would pass while the
    # playback stop moved to a second block.
    assert len(re.findall(r"teardown\.push\(", source)) >= 1
    pushes = re.findall(r"teardown\.push\(\s*(?:\(\) => \{(.*?)\}|(\w+))\s*\)", source, re.S)
    bodies = " ".join(block or name for block, name in pushes)
    assert "stopPlayback" in bodies, (
        "no teardown entry stops playback, so unmount() has nothing to run"
    )
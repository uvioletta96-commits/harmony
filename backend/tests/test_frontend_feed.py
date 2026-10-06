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


def test_fullscreen_hides_the_sites_own_chrome(source):
    """In fullscreen the browser's bars are already gone. The site's header, tabs
    and exit button are three more controls between the reader and the video."""
    css = CSS.read_text(encoding="utf-8")
    fullscreen = re.search(r"\.videos-page:fullscreen(.*?)\{", css, re.S)
    assert fullscreen, "no :fullscreen rule"

    block = re.search(r"\.videos-page:fullscreen \.videos-head,\s*\.videos-page:fullscreen \.videos-tabs \{(.*?)\}", css, re.S)
    assert block and "display: none" in block.group(1)


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


def test_the_deferred_pause_is_cleared_on_teardown(source):
    """A pending pause that is never cancelled fires after the page is gone, and
    touches a node that is no longer in the document."""
    teardown_block = re.search(r"teardown\.push\(\(\) => \{(.*?)\n  \}\);", source, re.S)
    assert teardown_block, "no teardown block"
    assert "clearTimeout" in teardown_block.group(1), (
        "teardown does not clear the pending-tap timer"
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
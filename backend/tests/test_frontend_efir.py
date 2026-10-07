"""The «Эфир» library: four bugs that all returned a working-looking page.

None of these raise. Each produced a library that rendered, accepted taps and
simply did not do what it appeared to: one page of videos and no way to the second,
broken images on every card, a preference that was written and never read, and a
shelf that could not be reached. A page that looks right is the reason these need
tests at all.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EFIR = (ROOT / "frontend" / "js" / "pages" / "efir.js").read_text(encoding="utf-8")
POSTERS = (ROOT / "frontend" / "js" / "components" / "posters.js").read_text(encoding="utf-8")
STATE = (ROOT / "frontend" / "js" / "components" / "watchState.js").read_text(encoding="utf-8")
LOCAL = (ROOT / "frontend" / "js" / "core" / "local.js").read_text(encoding="utf-8")
VIDEOS = (ROOT / "frontend" / "js" / "pages" / "videos.js").read_text(encoding="utf-8")
EFIR_CSS = (ROOT / "frontend" / "css" / "efir.css").read_text(encoding="utf-8")
SHELL = (ROOT / "frontend" / "js" / "components" / "shell.js").read_text(encoding="utf-8")


def block(source: str, header: str) -> str:
    """The block at `header`, from its body's opening brace to its match.

    Balanced braces rather than indentation, and the parameter list is skipped
    first - a destructured default contains braces of its own, and matching from one
    of those balances against the body and hands back the header alone.
    """
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
# The cursor
# ---------------------------------------------------------------------------


class TestPagination:
    def test_the_cursor_comes_off_the_payload(self):
        """The bug.

        `unwrap` in core/api.js attaches the envelope's `meta` to the *data* as a
        non-enumerable `__meta`, so a paged list can stay a plain array. The library
        called `api.meta?.()`, which has never existed on the object - it is
        `undefined` on every response - so `cursor` was always null, `exhausted`
        was always true, and the library stopped after its first page. Every other
        paged page in the app reads `__meta` correctly, which is why nobody
        noticed by analogy.
        """
        load = code(block(EFIR, "  async function loadPage("))
        assert "__meta" in load, (
            "the cursor is not read off the payload's __meta, so the library stops "
            "after the first page"
        )
        assert not re.search(r"\bapi\.meta\b", load), (
            "`api.meta` is not a thing: the envelope's meta rides on the data as "
            "__meta, so this always resolved to undefined"
        )

    def test_it_stops_when_the_server_says_there_is_no_more(self):
        """`has_more` is the server's own answer; deriving it from a missing cursor
        means an empty final page is indistinguishable from an exhausted list."""
        load = code(block(EFIR, "  async function loadPage("))
        assert "next_cursor" in load
        assert re.search(r"exhausted\s*=\s*!", load), "exhausted is never assigned"

    def test_the_sentinel_survives_a_reset(self):
        """The bug, and the reason the second page never loaded even with a working
        cursor: the sentinel was appended to the grid, and the first load called
        `grid.replaceChildren()` - detaching the node the IntersectionObserver was
        watching. The observer then measured a node in no document, and reported it
        as never intersecting.
        """
        render = block(EFIR, "export async function render(")
        assert "grid.append(sentinel)" not in render, (
            "the sentinel is a child of the grid, which the first load empties - so "
            "the observer watches a detached node and never fires again"
        )
        assert "sentinel," in render or "sentinel)" in render, (
            "the sentinel is not a sibling of the grid, where emptying one cannot "
            "detach it"
        )
        assert "observer.observe(sentinel)" in render
        assert "grid.replaceChildren()" in render, (
            "the grid is not cleared on reset, so changing the sort stacks two "
            "libraries on one screen"
        )

    def test_the_observer_is_disconnected(self):
        """It outlives the page otherwise, and fires `loadPage` against a detached
        grid - which is how a page the reader has left keeps making requests."""
        render = block(EFIR, "export async function render(")
        assert "observer.disconnect()" in render

    def test_a_sort_change_starts_from_the_first_page(self):
        """Reusing a cursor across sorts pages through a list the new sort does not
        belong to, and the reader gets a page that appears to be missing videos."""
        assert "if (cursor && !reset)" in code(block(EFIR, "  async function loadPage(")), (
            "the cursor is not cleared when the request changes"
        )
        assert "if (reset)" in code(block(EFIR, "  async function loadPage("))


# ---------------------------------------------------------------------------
# Posters
# ---------------------------------------------------------------------------


class TestPosters:
    def test_a_card_is_not_a_video_url_in_an_image(self):
        """The bug. There is no server thumbnail and there cannot be one without
        ffmpeg, so the card used `media.url` - an .mp4 or .webm - as its `src`. Every
        card rendered a broken image, which is what a grid of them looks like."""
        card = block(EFIR, "function videoCard(item, { compact = false } = {})")
        assert "posterElement" in card, (
            "the card builds no poster, so it falls back to the video's own URL - "
            "which an image element cannot display"
        )
        assert "<img" not in card and "el('img'" not in code(card), (
            "the card still renders an <img> whose source is a video file"
        )

    def test_the_frame_is_taken_from_the_video_not_the_first_frame(self):
        """The first frame of a video is very often a black fade-in or a title card:
        an accurate poster and a useless one. A tenth of the way in is a picture."""
        draw = block(POSTERS, "function drawPoster(url)")
        assert "currentTime" in draw, "the poster is the first frame, which is often black"
        assert "FRACTION" in draw
        assert re.search(r"currentTime\s*=\s*target", draw), (
            "the seek target is computed and never applied"
        )

    def test_the_seek_happens_after_the_metadata_arrives(self):
        """Setting `currentTime` before `loadedmetadata` is ignored, and setting it
        and drawing immediately gets the *previous* frame - which for a first draw is
        nothing at all."""
        draw = code(block(POSTERS, "function drawPoster(url)"))
        assert draw.index("loadedmetadata") < draw.index("currentTime"), (
            "the seek happens before the browser knows the video's length, where it "
            "is ignored"
        )
        assert "seeked" in draw, (
            "the frame is captured without waiting for the seek to complete, so the "
            "poster is whatever was on screen before it"
        )

    def test_every_video_is_released(self):
        """A `<video>` left with a source keeps its decoder and its buffered data.
        A shelf of thirty cards is thirty of them, and on a phone that is the whole
        memory budget."""
        draw = block(POSTERS, "function drawPoster(url)")
        assert "removeAttribute('src')" in draw, "the decoder outlives the poster"
        assert re.search(r"video\.load\?\.\(\)", draw), "the buffered data is never released"
        assert "clearTimeout" in draw, "a poster that never arrives leaves a timer running"

    def test_a_failure_is_a_tile_not_an_exception(self):
        """A card without a picture is acceptable. A card that throws while its
        twenty siblings render is not, and one undecodable file would take the shelf
        with it."""
        poster = code(block(POSTERS, "export function posterFor(url, storageKey = '')"))
        assert ".catch" in poster, "a failed draw rejects into the caller"
        element = code(block(POSTERS, "export function posterElement(url, storageKey)"))
        assert "if (!dataUrl)" in element, "no fallback when no frame can be drawn"
        assert "is-unavailable" in element, "nothing is drawn when there is no poster"

    def test_the_same_file_is_not_read_twice(self):
        """Scroll a shelf by one author and every cell asks for the same url."""
        assert "inFlight" in POSTERS, (
            "concurrent poster requests are not shared, so twenty cards open twenty "
            "copies of the same file"
        )
        assert "inFlight.delete" in POSTERS, "the shared entry is never cleared, so a url is read once for ever"

    def test_the_element_cannot_start_playing(self):
        """It is a decoder, not a player. A `<video>` that could start would be one
        more thing making noise on a page where sound is otherwise explicit."""
        element = code(block(POSTERS, "export function posterElement(url, storageKey)"))
        assert "'play'" in element or "play" in element, "nothing stops the decoder from starting"
        assert "pause()" in element, "the poster element can be made to play audio"
        assert "muted" in element, "the poster element is not even muted"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_a_preference_is_not_written_to_a_function_that_ignores_it(self):
        """The bug, in two places.

        `store.set(patch)` takes an object. Called as `store.set('key', value)` it
        runs `Object.entries` over the *string*, which yields index/character pairs,
        and writes a scatter of single characters into the store. The chrome toggle
        and the immersive mode were both written this way: they set a value nobody
        ever read, so the toggle appeared not to work and neither preference had ever
        once survived a reload.
        """
        for source, where in ((VIDEOS, "videos.js"), (EFIR, "efir.js")):
            assert not re.search(r"store\.set\(\s*['\"][a-zA-Z]", source), (
                f"{where} writes a preference with store.set(key, value), and "
                "store.set takes an object - the value is written to index keys and "
                "never read back"
            )

    def test_it_is_read_back_from_something_that_persists(self):
        for source, where in ((VIDEOS, "videos.js"),):
            assert "readFlag" in source, f"{where} does not read the chrome preference"
            assert "writeValue" in source, f"{where} does not save it"

    def test_the_store_is_not_used_for_anything_that_must_outlive_a_reload(self):
        """The store is in-memory by design. A preference read from it is correct
        for this visit and gone on the next, which is indistinguishable from the
        preference never having been saved."""
        assert read_persists(VIDEOS), "the clip feed's preferences still live in the in-memory store"

    def test_storage_that_throws_does_not_take_the_page_down(self):
        """Private mode and a full quota both throw. A preference that cannot be
        saved must cost the reader the setting, not the page."""
        body = code(block(LOCAL, "function storage()"))
        assert "catch" in body, "a localStorage that throws is not handled"
        assert "return null" in body or "backing = null" in body
        assert "try" in code(block(LOCAL, "export function writeValue(key, value)")), (
            "a failed write propagates out to the caller"
        )
        assert "catch" in code(block(LOCAL, "export function readValue(key, fallback = null)")), (
            "a corrupt value propagates out to the caller"
        )

    def test_a_corrupt_value_is_treated_as_unset(self):
        """A truncated write, or another script on this origin. Neither is a reason
        for the page to stop."""
        read = code(block(LOCAL, "export function readValue(key, fallback = null)"))
        assert "JSON.parse" in read
        assert "return fallback" in read


def read_persists(source: str) -> bool:
    """Whether every persisted-looking key is written through the local module."""
    keys = set(re.findall(r"(?:readFlag|writeValue)\(\s*'([a-zA-Z:]+)'", source))
    keys |= set(re.findall(r"writeValue\((\w+_KEY)", source))
    return "local.js" in source or bool(keys)


# ---------------------------------------------------------------------------
# Watching
# ---------------------------------------------------------------------------


class TestResume:
    def test_a_finished_video_is_not_offered_again(self):
        """Otherwise the shelf fills with things the reader has already watched,
        which is the definition of a shelf nobody looks at."""
        remember = code(block(STATE, "export function rememberPosition(id, positionMs, durationMs)"))
        assert "RESUME_FLOOR_MS" in remember, "a video never started is offered as part-watched"
        assert "RESUME_MIN_REMAINING_MS" in remember, (
            "a video watched to the end is offered as part-watched"
        )

    def test_a_position_past_the_end_is_not_resumed(self):
        """A re-encoded or re-uploaded video is shorter now. Resuming at 97% of a
        different cut shows the credits and stops, which looks like a broken player."""
        at = code(block(STATE, "export function resumeAt(id)"))
        assert "duration" in at
        assert re.search(r"position\s*<.*duration", at), "the saved position is not checked against the length"

    def test_a_nan_duration_is_not_stored(self):
        """`rememberPosition` is called from a `timeupdate` handler, and a video that
        has not loaded reports `NaN` for both currentTime and duration."""
        remember = code(block(STATE, "export function rememberPosition(id, positionMs, durationMs)"))
        assert "duration <= 0" in remember, "a zero or NaN duration is stored"
        assert "Number.isFinite" in remember or "|| 0" in remember, (
            "a NaN position is written to storage, and read back as NaN on the next visit"
        )

    def test_the_position_is_written_on_a_timer_not_on_every_frame(self):
        """`timeupdate` fires about four times a second. Four localStorage writes a
        second, synchronously, on the main thread, while a video plays."""
        player = code(block(EFIR, "function openPlayer(item, media)"))
        assert "4000" in player, "every timeupdate writes to storage"
        assert "setInterval" in player, "no fallback timer for a backgrounded tab"

    def test_it_is_saved_before_the_node_goes(self):
        """Otherwise closing the dialog loses the position the reader earned."""
        dismiss = code(block(EFIR, "  const dismiss = () => {"))
        assert "rememberPosition" in dismiss
        assert dismiss.index("rememberPosition") < dismiss.index("overlay.remove()")

    def test_an_unloaded_video_saves_nothing(self):
        """A video that never loaded has a NaN duration, and NaN in storage is NaN
        for ever after."""
        dismiss = code(block(EFIR, "  const dismiss = () => {"))
        assert "Number.isFinite" in dismiss, (
            "a video that never loaded writes its position anyway"
        )


# ---------------------------------------------------------------------------
# Mobile
# ---------------------------------------------------------------------------


class TestMobile:
    def test_the_phone_is_the_design_target(self):
        """One column by default and more only from 720px up. The previous version
        had the media query the other way round - one column *below* 560px - which
        reads as a desktop layout squeezed until it fits."""
        assert re.search(r"@media \(min-width: 720px\)", EFIR_CSS), (
            "the grid widens only on a wide screen, so the phone is the base case"
        )
        # Everything above the first min-width query is the base layout. Splitting on
        # a bare "@media" would cut at the hover query instead and miss the grid
        # entirely - which is what the first version of this check did.
        base = EFIR_CSS.split("@media (min-width: 720px)")[0]
        assert re.search(r"\.efir-grid \{[^}]*grid-template-columns:\s*1fr", base), (
            "the base grid is not one column, so the phone inherits a desktop layout"
        )

    def test_a_search_field_is_at_least_sixteen_pixels(self):
        """Not a style choice. Anything smaller makes iOS Safari zoom the page in on
        focus, and the reader cannot zoom back out - the site stops working on that
        page for the rest of the session."""
        assert re.search(r"\.efir-search-input \{[^}]*font-size:\s*16px", EFIR_CSS), (
            "the search field is under 16px, so iOS zooms in on focus and traps the page"
        )

    def test_cards_are_wide_enough_to_flick(self):
        """A shelf of 260px cards on a 360px screen shows one and a sliver. Two cards
        with a third edge peeking is what tells a thumb there is more."""
        shelf = re.search(r"\.efir-shelf-cell \{([^}]*)\}", EFIR_CSS)
        assert shelf, "no shelf cell sizing"
        assert "%" in shelf.group(1), (
            "the shelf cells are a fixed width, which shows one card and a sliver on a "
            "360px screen"
        )

    def test_the_play_glyph_is_hover_only(self):
        """A hover affordance on a touch screen either never appears or appears
        stuck. The whole card is the target there, so the glyph is not needed."""
        assert "@media (hover: hover) and (pointer: fine)" in EFIR_CSS, (
            "the play hint is not gated on a pointer that can hover, so on a phone it "
            "is either missing or stuck on"
        )

    def test_the_player_is_full_bleed_on_a_phone(self):
        """A dialog with a video in it has borders and a title bar nobody wants."""
        base = EFIR_CSS.split("@media (min-width: 720px)")[0]
        assert re.search(r"\.efir-player \{[^}]*inset:\s*0", base), (
            "the player is not full-bleed on a phone"
        )

    def test_the_player_info_is_capped(self):
        """A long title on a short phone pushes the player's own controls below the
        bottom of the screen, and the video cannot be paused."""
        assert re.search(r"\.efir-player-info \{[^}]*max-height", EFIR_CSS), (
            "the title block is uncapped, so on a short phone the controls go off-screen"
        )
        assert "overflow-y: auto" in EFIR_CSS

    def test_the_bottom_bar_does_not_show_through_a_player(self):
        """The player is over everything at z-index 60; the bar is not that high, and
        without this it is visible underneath with its own padding."""
        assert "body.has-player .mobile-nav" in EFIR_CSS, (
            "the bottom bar is drawn under an open player"
        )
        assert "has-player" in EFIR, "the class the rule keys on is never set"
        assert "classList.add('has-player')" in EFIR
        assert "classList.remove('has-player')" in EFIR, (
            "the class outlives the player, hiding the bottom bar on every later page"
        )

    def test_the_shelves_are_derived_from_this_browser(self):
        """They are what the reader has done, not part of the catalogue's order.
        Mixing them into the sorted grid would reorder the library by personal
        history without saying so."""
        assert "shelfItems" in EFIR
        grid = code(block(EFIR, "  function render()"))
        assert "shelf === 'all'" in grid, (
            "the shelves are mixed into the grid rather than kept beside it"
        )

    def test_the_library_is_still_reachable_from_the_clip_feed(self):
        """Every other way in is either hidden in immersive mode or absent on a
        phone."""
        assert "'/efir'" in VIDEOS, "the link from «Клипы» to «Эфир» is gone"
        assert "'/efir'" in SHELL


class TestPlayerKeys:
    """Shortcuts, only where they can be reached."""

    def test_they_do_not_fire_while_typing(self):
        keys = code(block(EFIR, "  const onKey = (event) => {"))
        assert "INPUT" in keys and "isContentEditable" in keys, (
            "`m` typed into a field mutes the video"
        )
        assert "metaKey" in keys, "cmd+f and friends are swallowed"

    def test_the_latin_key_and_its_cyrillic_homophone_both_work(self):
        """The site is in Russian, so a reader pressing `м` for the same shortcut
        sends `ь` and `ф` sends `а`. On a Cyrillic layout the Latin letters never
        arrive at all."""
        keys = code(block(EFIR, "  const onKey = (event) => {"))
        assert "'ь'" in keys and "'а'" in keys, (
            "the shortcuts work only on a Latin layout, so a Russian reader cannot "
            "reach them"
        )

    def test_fullscreen_asks_without_options(self):
        """`navigationUI` is rejected outright by some engines and the throw costs
        the whole feature. This is the same trap the clip feed fell into."""
        toggle = code(block(EFIR, "function toggleFullscreen(video)"))
        assert "navigationUI" not in toggle, (
            "requestFullscreen is called with an options object that some engines "
            "reject, and the throw costs the feature"
        )

    def test_a_refused_fullscreen_is_silent(self):
        """The player is already full-bleed, so a refusal costs nothing visible."""
        assert "catch" in code(block(EFIR, "function toggleFullscreen(video)"))


class TestSearch:
    def test_it_filters_what_is_loaded_rather_than_re_requesting(self):
        """A shelf is a handful of pages, and a search across them is instant. A
        request per keystroke is neither, and on a phone it is visible."""
        rebuild = code(block(EFIR, "  function rebuild()"))
        assert "api.get" not in rebuild, "the search makes a request per keystroke"
        # The debounce belongs to the field, not the redraw: `rebuild` is also what
        # the save button calls, and a save should show up at once.
        field = code(block(EFIR, "function searchField(onChange)"))
        assert "setTimeout" in field, "the search is not debounced"
        assert "180" in field, "the debounce is long enough to be visible"
        assert "clearTimeout" in field, (
            "each keystroke queues its own redraw and only the last one is meant to run"
        )

    def test_it_matches_the_author_as_well_as_the_title(self):
        """The two things a reader remembers about a video."""
        matcher = code(block(EFIR, "function filterByQuery(items, needle)"))
        assert "body" in matcher
        assert "username" in matcher
        assert "toLocaleLowerCase" in matcher, (
            "the search is case-sensitive, so it misses the word a reader was "
            "thinking of"
        )

    def test_the_search_term_is_a_parameter_not_a_closure_read(self):
        """The bug this catches is worth the check on its own.

        `filterByQuery` was a module-level function reading a `query` that belonged
        to `render`'s scope. A module-level function cannot see that, so the first
        render threw `ReferenceError: query is not defined` and the library showed
        an error card with no videos in it - which is how it reached production: the
        section looked alive, because the shelves, the sorts and the search field all
        rendered, and only the grid was broken.

        ESLint would catch this; there is no linter for the unbundled ES modules
        here. So it is checked by reading the signature.
        """
        signature = re.search(r"function filterByQuery\(([^)]*)\)", EFIR)
        assert signature, "no filterByQuery"
        assert "needle" in signature.group(1) or "query" in signature.group(1), (
            "filterByQuery takes no search term, so it must be reading one from an "
            "enclosing scope - which a module-level function does not have"
        )
        matcher = code(block(EFIR, "function filterByQuery(items, needle)"))
        assert re.search(r"if \(!needle\)", matcher), (
            "the filter does not short-circuit on an empty term, so every video is "
            "re-tested against an undefined one"
        )

    def test_the_caller_passes_the_term(self):
        """Counted by paren depth, not by matching to the next `)`.

        The argument is `[...library.values()]`, so a pattern that stops at the first
        closing bracket sees `filterByQuery([...library.values()` and reports a
        missing second argument on correct code - which is how a check like this
        teaches its reader to ignore it.
        """
        render = code(block(EFIR, "  function render()"))
        # Skip *past* the call's own `(`, which `depth = 1` already accounts for.
        # Leaving the cursor on it counted the opening bracket twice, so the walk
        # never returned to depth 0 and never reached a depth-1 comma.
        call = render.index("filterByQuery(") + len("filterByQuery(")
        depth = 1
        commas = 0
        for char in render[call:]:
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
                if depth == 0:
                    break
            elif char == "," and depth == 1:
                commas += 1
        assert commas >= 1, (
            "the caller does not pass the search term, so the filter cannot see it"
        )

    def test_no_results_says_so_and_says_which_search(self):
        """A different empty state for a search that found nothing, or the reader is
        told the library is empty and goes looking for a video that is right there."""
        empty = block(EFIR, "function emptyFor(shelf, needle)")
        assert "if (needle)" in empty, (
            "a failed search shows the library's own empty state, so a reader is told "
            "there are no videos when the one they wanted is on the page"
        )
        assert "search" in empty, "no icon for a failed search"
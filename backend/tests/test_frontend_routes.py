"""Frontend route registration and the public-route list.

These are the two things that decide whether a page is reachable at all, and both
fail silently: a route defined in `main.js` but absent from `PUBLIC_ROUTES`
sends every signed-out visitor to /login, and a page module that exists but is
never defined renders as "not found". Neither raises, and neither shows up in a
backend test.

Found by opening `/videos` on the deployed site and being redirected to /login.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# main.js lives at frontend/js/main.js, so the js directory is its parent - not
# `frontend/`, which resolves imports relative to the wrong root and reports every
# page module as missing.
JS = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "js"
MAIN = JS / "main.js"


@pytest.fixture(scope="module")
def source() -> str:
    return MAIN.read_text(encoding="utf-8")


def exported_names(text: str) -> set[str]:
    """Every name a module exports, covering the three shapes used here.

      export async function render()        declaration (most pages)
      export const { a, b } = ...           destructured export
      export { x as render } from './y'     re-export under another name
      export { a, b } from './y'            plain re-export block (login.js)

    Looking only for `export function render` reports two working pages as broken,
    and a test that cries wolf gets ignored.
    """
    names: set[str] = set()
    names.update(re.findall(r"export (?:async )?function (\w+)", text))
    names.update(re.findall(r"export (?:const|let|var|class) (\w+)", text))

    for block in re.findall(r"export \{(.*?)\}", text, re.S):
        for part in block.split(","):
            part = part.strip()
            if not part:
                continue
            names.add(part.split(" as ")[-1].strip())
    return names


def test_every_defined_page_imports_a_real_module(source):
    """`import * as x from './pages/x.js'` must resolve to a real file, and that
    file must export the name the router actually uses for it.

    Checked per alias rather than per file, because `legal.js` exports
    `renderTerms` and `renderVerify` and is routed under neither `render`.
    """
    imports = re.findall(r"import \* as (\w+) from '(\./pages/[\w./-]+)';", source)
    assert imports, "no page modules found - the pattern may have changed"

    for alias, relative in imports:
        path = (JS / relative.lstrip("./")).resolve()
        assert path.is_file(), f"{alias} imports {relative}, which does not exist"

    # Every `alias.member` the router reaches for has to be an export.
    for alias, _relative in imports:
        text = (JS / dict(imports)[alias].lstrip("./")).read_text(encoding="utf-8")
        for member in sorted(set(re.findall(rf"\b{alias}\.(\w+)", source))):
            assert member in exported_names(text), (
                f"main.js uses {alias}.{member}, which {alias} does not export"
            )


def test_every_imported_page_is_used(source):
    """An import nothing references is a page someone wrote and forgot.

    `legal.js` is legitimately routed under two aliases (`renderTerms`,
    `renderVerify`), so this checks that the alias is used *somewhere* rather than
    that it appears in a `router.define(... .render)` call.
    """
    imports = re.findall(r"import \* as (\w+) from '(\./pages/[\w./-]+)';", source)

    for alias, relative in imports:
        uses = re.findall(rf"\b{alias}\.\w+", source)
        assert uses, f"{relative} is imported as {alias} but never used"


def test_every_registered_route_imports_its_page(source):
    """The other direction: a route pointing at a page that was never imported is
    a ReferenceError at boot, which takes the whole app down."""
    registered = dict(re.findall(r"router\.define\('([^']+)',\s*(\w+)\.render\)", source))
    imported = dict(re.findall(r"import \* as (\w+) from '(\./pages/[\w./-]+)';", source))

    for path, alias in sorted(registered.items()):
        assert alias in imported, f"route {path} uses {alias}, which is not imported"


def test_the_video_feed_is_reachable_without_signing_in(source):
    """The regression.

    `/videos` was routed but absent from PUBLIC_ROUTES, so the route guard sent
    every signed-out visitor to /login. A public read endpoint behind a private
    guard: the backend happily served the feed, and nobody could reach it.

    Nothing raised - the guard is a redirect, the endpoint is `@auth_optional`, and
    the only symptom is a page that does not appear. So this compares the guard's
    list against what is actually readable in both directions, rather than
    asserting one entry.
    """
    block = re.search(r"const PUBLIC_ROUTES = \[(.*?)\];", source, re.S)
    assert block, "PUBLIC_ROUTES not found"
    listed = set(re.findall(r"'([^']+)'", block.group(1)))

    readable_by_anyone = {"/", "/videos", "/search", "/terms", "/privacy"}
    missing = readable_by_anyone - listed
    assert not missing, (
        f"{sorted(missing)} are readable by anyone but are not in PUBLIC_ROUTES, so "
        "signed-out visitors are redirected to /login and never see the page"
    )

    # The other direction, which is the more serious of the two and the harder one
    # to notice: a page that needs a session, listed as public.
    for path in ("/settings", "/chat", "/notifications", "/admin"):
        assert path not in listed, f"{path} requires a session and must not be listed"


def test_the_video_page_declares_unmount(source):
    """It holds a clip, an IntersectionObserver and a scroll listener. Leaving any
    of those running means sound continues with nothing on screen."""
    videos = JS / "pages" / "videos.js"
    text = videos.read_text(encoding="utf-8")

    assert re.search(r"export function unmount\b", text), (
        "videos.js must export unmount() so playback and listeners stop on navigation"
    )
    assert "observer.disconnect()" in text, "the prefetch observer is never disconnected"
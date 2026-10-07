"""Every `var(--name)` in the stylesheets must be defined.

An undefined custom property does not fail a build and does not fail a test: the
declaration is simply dropped, so the element renders with whatever it inherited.
In this stylesheet that means an invisible element - a waveform drawn in the page's
own background colour, a record button with no background - which looks like a
layout bug and gets debugged as one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = ROOT / "frontend" / "css"

#: Names that come from somewhere other than `:root`, and so cannot be found by
#: scanning the stylesheets for a definition.
EXTERNAL = {
    # Set from JS: the live level meter and the circle's progress arc.
    "--level",
    "--played",
}


def defined() -> set[str]:
    names: set[str] = set()
    for path in sorted(CSS_DIR.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r"(--[a-z0-9-]+)\s*:", text))
    return names


def used() -> dict[str, list[str]]:
    """`var(--name)` occurrences with **no fallback**, mapped to where.

    A fallback is what makes a custom property safe, so reporting
    `var(--watched, 0%)` as undefined is a false alarm - and false alarms are how a
    check like this stops being read.

    The fallback is detected by the delimiter immediately after the name: a comma
    means one follows, a closing bracket means none does. The first version of this
    matched everything up to the next comma or bracket, which for
    `var(--watched, 0%)` captured the empty string and so reported a variable that
    did have a fallback as undefined.
    """
    found: dict[str, list[str]] = {}
    pattern = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*([,)])")
    for path in sorted(CSS_DIR.glob("*.css")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip().startswith("*") or line.strip().startswith("/*"):
                continue
            for name, delimiter in pattern.findall(line):
                if delimiter == ")":
                    found.setdefault(name, []).append(f"{path.name}:{line_no}")
    return found


class TestCustomProperties:
    def test_every_variable_used_is_defined(self):
        """The whole point: a dropped declaration is invisible."""
        known = defined() | EXTERNAL
        unknown = {name: where for name, where in used().items() if name not in known}
        assert not unknown, (
            "referenced but never defined, so the declaration is dropped and the "
            f"element renders as if the rule were not there: {unknown}"
        )

    def test_the_variables_added_for_the_chat_players_exist(self):
        """The two custom properties the new stylesheet depends on, checked by name
        so the reason survives a rename."""
        known = defined()
        for name in ("--sand-400", "--sand-300", "--danger-soft", "--danger", "--radius-full"):
            assert name in known, f"{name} is used but never defined"

    def test_the_ring_arc_has_a_fallback(self):
        """`--played` is set from JS once audio metadata arrives. Before that it is
        unset, and `var(--played)` with no fallback makes the whole `background`
        declaration invalid - so the ring draws nothing at all until the first
        frame of playback."""
        css = (CSS_DIR / "components.css").read_text(encoding="utf-8")
        ring = re.search(r"\.circle-ring \{(.*?)\n\}", css, re.S)
        assert ring, "no .circle-ring rule"
        for occurrence in re.findall(r"var\(--played([^)]*)\)", ring.group(1)):
            # The fallback is what is *inside* the parentheses. Checking that the
            # whole `var(...)` ends in a percent sign would reject the correct code,
            # since it ends in a closing bracket.
            assert "," in occurrence, (
                "var(--played) has no fallback, so the ring is invisible until the "
                "audio reports a duration"
            )
            assert occurrence.strip().lstrip(",").strip().endswith("%"), (
                f"the fallback {occurrence!r} is not a percentage"
            )
        assert ring.group(1).count("var(--played") >= 2, (
            "the ring needs one fallback at each gradient stop, not one in the block"
        )

    def test_the_level_meter_has_a_zero_height_fallback(self):
        """A meter with no level yet would otherwise take its height from the
        transition's initial value and show a full bar before recording starts."""
        css = (CSS_DIR / "components.css").read_text(encoding="utf-8")
        meter = re.search(r"\.composer-meter-fill \{(.*?)\n\}", css, re.S)
        assert meter, "no .composer-meter-fill rule"
        assert re.search(r"height:\s*calc\(var\(--level,\s*0\)", meter.group(1)), (
            "the meter's height has no fallback, so it is full before recording begins"
        )

    @pytest.mark.parametrize("name", sorted(EXTERNAL))
    def test_each_external_variable_is_actually_set_from_js(self, name):
        """An entry in `EXTERNAL` is a promise that JavaScript sets it. Checked, or
        the exemption quietly becomes a hole."""
        js = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "frontend" / "js").rglob("*.js")
        )
        bare = name.lstrip("-")
        assert f"setProperty('{name}'" in js or f"setProperty(\"{name}\"" in js, (
            f"{name} is exempted from the check but nothing sets it"
        )
        del bare


def bare_class_selectors(css: str) -> set[str]:
    """Class names defined by a selector that is *only* that one class.

    Deliberately narrow: `.chat-list .empty` styles a descendant and does not
    redefine `.chat-list`, so counting it would report a conflict where there is
    none. One line, one class, no combinator, attribute or pseudo-class after it.
    """
    names = set()
    for line in css.splitlines():
        match = re.match(r"^\.([a-z][a-z0-9-]*)\s*\{", line)
        if match:
            names.add(match.group(1))
    return names


def test_the_stylesheets_load_in_a_documented_order() -> None:
    """The order is load-bearing and lives in only one place.

    `pages.css` is loaded last, so a class defined in both `pages.css` and
    `components.css` silently resolves to whichever is later. `.bubble-image` was
    exactly that: two files describing one class, with the wrong one winning.
    """
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'href="/css/([a-z-]+\.css)"', html)
    assert order, "no stylesheet links found in index.html"
    assert order[0] == "base.css", "base.css defines the custom properties and must come first"
    assert order[-1] == "pages.css", "the documented order has changed; check the duplicates below"

    shared = bare_class_selectors((CSS_DIR / "components.css").read_text(encoding="utf-8"))
    shared &= bare_class_selectors((CSS_DIR / "pages.css").read_text(encoding="utf-8"))
    assert not shared, (
        "defined in both components.css and pages.css, and pages.css loads last, so "
        f"the definition that wins is the one nobody looks at: {sorted(shared)}"
    )


class TestCommentIndentation:
    """Found by the variable check above: three of the four nesting levels of a
    comment had no indent at all.

    An undefined custom property does not fail a build or a test - the declaration
    is dropped and the property falls back to its initial value. `padding-left: 0`
    for depth 2 and 3 means a reply to a reply renders flush left, *less* indented
    than the reply above it, and the level that most needs to show its nesting is
    the one showing none.
    """

    def test_every_level_has_an_indent(self):
        for name in ("components.css", "layout.css"):
            css = (CSS_DIR / name).read_text(encoding="utf-8")
            for depth in (1, 2, 3):
                rule = re.search(rf"\.comment\[data-depth=\"{depth}\"\][^{{]*\{{([^}}]*)\}}", css)
                assert rule, f"{name} has no rule for comment depth {depth}"
                padding = re.search(r"padding-left:\s*var\((--[a-z0-9-]+)\)", rule.group(1))
                assert padding, (
                    f"{name} depth {depth} has no padding-left, so the level most in "
                    "need of showing its nesting shows none"
                )

    def test_the_indent_grows_with_depth(self):
        """Otherwise the levels are not ordered, and the reader cannot tell which
        reply answers which."""
        for name in ("components.css", "layout.css"):
            css = (CSS_DIR / name).read_text(encoding="utf-8")
            values = []
            for depth in (1, 2, 3):
                rule = re.search(rf"\.comment\[data-depth=\"{depth}\"\][^{{]*\{{([^}}]*)\}}", css)
                values.append(int(re.search(r"padding-left:\s*var\(--space-(\d+)\)", rule.group(1)).group(1)))
            assert values == sorted(values), f"{name} indents do not grow: {values}"

    def test_the_narrow_breakpoint_reduces_the_indent(self):
        """The old block increased it: 48px, 96px, then a step past the end of the
        scale. On a 360px phone a 96px indent leaves about 240px of text, and the
        third level asked for 224px - a few characters per line."""
        wide = (CSS_DIR / "components.css").read_text(encoding="utf-8")
        narrow = (CSS_DIR / "layout.css").read_text(encoding="utf-8")

        def indent_for(css: str, depth: int) -> int:
            rule = re.search(rf"\.comment\[data-depth=\"{depth}\"\][^{{]*\{{([^}}]*)\}}", css)
            return int(re.search(r"padding-left:\s*var\(--space-(\d+)\)", rule.group(1)).group(1))

        for depth in (1, 2, 3):
            assert indent_for(narrow, depth) < indent_for(wide, depth), (
                f"depth {depth} is not narrower on a phone, where horizontal room is "
                "the scarce resource"
            )
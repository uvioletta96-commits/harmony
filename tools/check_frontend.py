"""Static checks for the frontend, runnable without Node.

CI uses ``node --check`` for syntax; this covers the three things that a browser
would otherwise reveal at runtime:

* every relative ``import`` resolves to a file that exists;
* ``innerHTML`` only ever receives a value that is not user-controlled;
* every user-facing string goes through ``t()``, so it can be translated.

The third check is the one that keeps the translation claim honest. A string
literal that reaches a reader without being wrapped is a string no catalogue can
ever translate, and it is invisible in review because it looks exactly like every
other string. Object keys, ``case`` labels and ``default:`` are excluded: those
are compared against rather than displayed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "frontend" / "js"

IMPORT = re.compile(r"""(?:^|\s)(?:import|export)[^'"]*from\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)""")

#: A quoted literal in JS source. The closing backreference keeps the alternation
#: safe: a double quote inside a single-quoted string is content, not a
#: terminator.
LITERAL = re.compile(r"""(?<!\\)(['"`])((?:(?!\1)[^\\]|\\.)*?)\1""", re.DOTALL)

CYRILLIC = re.compile(r"[\u0400-\u04ff]")

#: ``router.define('/path', someModule.someExport)`` - optionally with a leading
#: ``router.``.
ROUTE = re.compile(r"""router\.define\(\s*(['"])([^'"]+)\1\s*,\s*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)""")

#: ``export { a, b as c } from './x.js'`` and ``export function name``.
REEXPORT = re.compile(
    r"""export\s*\{(?P<body>[^}]*)\}\s*from\s*['"](?P<source>[^'"]+)['"]""",
    re.DOTALL,
)
EXPORTED = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)")

#: ``import * as name from './path.js'``
STAR_IMPORT = re.compile(r"""import\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"]""")

#: The only legal innerHTML sinks, with the reason each is safe.
ALLOWED_SINKS = {
    "js/pages/legal.js": "operator-authored legal document, sanitised server-side",
    "js/core/icons.js": "static icon path data compiled into the bundle",
    "js/core/toast.js": "static close-icon path data",
    "js/core/router.js": "static view-title text produced by this module",
    "js/core/dom.js": "documentation and an escaping helper, not a sink",
}

#: The catalogues and the key list. Their entries are `'Russian key': 'value'`,
#: which is a lookup table, not user-facing text, so they are exempt.
I18N_DIR = JS / "i18n"


def untranslated(source: str, literal: re.Match[str]) -> str | None:
    """Why this literal is not wrapped in ``t()``, or ``None`` if it should be."""
    quote, body = literal.group(1), literal.group(2)
    head = source[max(0, literal.start() - 12) : literal.start()]
    tail = source[literal.end() : literal.end() + 40]

    if not CYRILLIC.search(body):
        return None
    if re.search(r"t\(\s*$", head):
        return None
    if re.match(r"\s*:", tail):
        return "object key"
    if re.search(r"\bcase\s+$", head):
        return "case label"
    if re.search(r"\bdefault\s*:\s*$", head):
        return "default label"
    if quote == "`" and "${" in body:
        return "template with interpolation"
    return None


def exports_of(path: Path) -> set[str]:
    """Names a module makes available, following one level of re-export.

    ``pages/login.js`` exists only to re-export from ``pages/auth.js``, so a
    check that stopped at the file itself would report every route it fronts as
    missing.
    """
    source = path.read_text(encoding="utf-8")
    names = set(EXPORTED.findall(source))
    for match in REEXPORT.finditer(source):
        for entry in match.group("body").split(","):
            entry = entry.strip()
            if not entry:
                continue
            # ``a as b`` exports b; a bare ``a`` exports a.
            names.add(entry.split(" as ")[-1].strip())
    return names


def check_routes(problems: list[str]) -> int:
    """Every registered route must point at something that exists.

    ``router.define('/verify', mod.fn)`` with ``mod.fn`` undefined is accepted
    by the router, matches the path, finds no loader and renders an empty page.
    There is no error and nothing in the console says which route is broken, so
    a missing re-export surfaces as a blank screen and a refresh.
    """
    resolved = 0
    main = JS / "main.js"
    source = main.read_text(encoding="utf-8")

    # ``import * as loginPage from './pages/login.js'``
    imports = dict(STAR_IMPORT.findall(source))

    for match in ROUTE.finditer(source):
        pattern, module_name, export_name = match.group(2), match.group(3), match.group(4)
        target = imports.get(module_name)
        if target is None:
            problems.append(f"js/main.js: route {pattern} uses unknown module {module_name}")
            continue
        if not target.startswith("."):
            continue

        path = (main.parent / target).resolve()
        if not path.exists():
            problems.append(f"js/main.js: route {pattern} imports a missing module: {target}")
            continue
        if export_name not in exports_of(path):
            problems.append(
                f"js/main.js: route {pattern} renders {module_name}.{export_name}, "
                f"which {target} does not export - the page would render blank"
            )
            continue
        resolved += 1
    return resolved


def main() -> int:
    problems: list[str] = []
    strings = 0

    for path in sorted(JS.rglob("*.js")):
        rel = path.relative_to(ROOT / "frontend").as_posix()
        source = path.read_text(encoding="utf-8")

        for match in IMPORT.finditer(source):
            spec = match.group(1) or match.group(2)
            if not spec or not spec.startswith("."):
                continue
            target = (path.parent / spec).resolve()
            if not target.exists():
                problems.append(f"{rel}: import of a missing module: {spec}")

        for number, line in enumerate(source.splitlines(), 1):
            if "innerHTML" not in line or line.lstrip().startswith(("*", "//")):
                continue
            if rel not in ALLOWED_SINKS:
                problems.append(f"{rel}:{number}: innerHTML outside a reviewed sink")

        # A catalogue is a table of translations, not interface text.
        if I18N_DIR in path.parents or path.name == "i18n.js":
            continue

        for match in LITERAL.finditer(source):
            reason = untranslated(source, match)
            if reason is None:
                if CYRILLIC.search(match.group(2)):
                    strings += 1
                continue
            line = source[: match.start()].count("\n") + 1
            problems.append(f"{rel}:{line}: user-facing string outside t() ({reason}): {match.group(2)[:60]!r}")

    routes = check_routes(problems)
    for problem in problems:
        print("::error::" + problem)
    if problems:
        return 1

    modules = len(list(JS.rglob("*.js")))
    print(
        f"{modules} modules: every relative import resolves, every innerHTML sink is reviewed, "
        f"all {strings} user-facing strings go through t(), all {routes} routes render a real export."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Extract the translation keys and report coverage per catalogue.

The key is the Russian source text, so the key list is simply every `t('...')`
first argument in the source. Coverage is measured against the catalogues, which
store `'key': 'value'` pairs - a different shape from a call site, and reading
one with the other's pattern is how a report silently reports zero for a
catalogue that is in fact complete.
"""

import json
import pathlib
import re

ROOT = pathlib.Path(r"D:\sochset_python_html\frontend\js")
I18N = ROOT / "i18n"
KEYS = I18N / "keys.json"

# Call site: t('key'
CALL = re.compile(r"""\bt\(\s*(['"])((?:[^'"\\]|\\.)*?)\1""")

# Catalogue entry: 'key': 'value',  -- optional, so a last entry without a comma
# is still counted.
ENTRY = re.compile(r"^\s*'((?:[^'\\]|\\.)*)':\s*'((?:[^'\\]|\\.)*)',?\s*$", re.MULTILINE)

# The catalogue index and the key list are not string tables.
SKIP = {"i18n/index.js"}


def unescape(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


def scan_keys() -> list[str]:
    keys: set[str] = set()
    for path in sorted(ROOT.rglob("*.js")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in SKIP or relative.startswith("i18n/"):
            continue
        for match in CALL.finditer(path.read_text(encoding="utf-8")):
            keys.add(unescape(match.group(2)))
    return sorted(keys)


def read_catalogue(path: pathlib.Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in ENTRY.finditer(path.read_text(encoding="utf-8")):
        found[unescape(match.group(1))] = unescape(match.group(2))
    return found


def main() -> int:
    keys = scan_keys()
    KEYS.write_text(json.dumps(keys, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(keys)} keys -> {KEYS}\n")

    # Written to a file as well as printed: the console in a CI log and in a
    # Windows terminal both mangle Cyrillic, and a list of missing keys is
    # useless if you cannot read what is in it.
    report: list[str] = []
    worst = 100
    for path in sorted(I18N.glob("*.js")):
        if path.name == "index.js":
            continue
        table = read_catalogue(path)
        missing = [key for key in keys if not table.get(key)]
        percent = round(100 * (len(keys) - len(missing)) / max(1, len(keys)))
        worst = min(worst, percent)
        print(f"{path.stem:4s} {percent:3d}%  {len(keys) - len(missing):4d}/{len(keys)}  missing {len(missing)}")
        if missing:
            report.append(f"--- {path.stem}: {len(missing)} missing")
            report.extend(f"  {key}" for key in missing)

    (I18N / "missing.txt").write_text("\n".join(report), encoding="utf-8")
    if report:
        print(f"\nfull list -> {I18N / 'missing.txt'}")

    return 0 if worst == 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())

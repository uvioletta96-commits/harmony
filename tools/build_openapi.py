"""Generate ``backend/app/static/openapi.json`` from ``spec/openapi.yaml``.

Keeps a single source of truth: the YAML spec is hand-edited and reviewed, the
JSON is what the browser loads. Run this after every spec change, and in CI as
a check that the committed JSON is not stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "openapi.yaml"
OUTPUT = ROOT / "backend" / "app" / "static" / "openapi.json"


def load_spec() -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        sys.exit("PyYAML is required: pip install pyyaml")

    with SPEC.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build() -> dict:
    spec = load_spec()
    spec["info"]["x-generated-from"] = "spec/openapi.yaml"
    return spec


def render(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed JSON is out of date.")
    args = parser.parse_args()

    rendered = render(build())

    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT} does not exist; run without --check to generate it.")
            return 1
        if OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"{OUTPUT.name} is out of date with spec/openapi.yaml. Run: python tools/build_openapi.py")
            return 1
        print(f"{OUTPUT.name} is up to date.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({len(rendered) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

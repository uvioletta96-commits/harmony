#!/usr/bin/env python
"""Fail if the OpenAPI spec and the route table have drifted apart.

Two directions matter, and a one-way check catches only one of them:

* an undocumented route - the endpoint exists, so the published contract is a
  lie about what the server accepts;
* a documented operation with no route - the contract promises something the
  server will 404.

Path-parameter *naming* is deliberately not compared. Flask registers
``/posts/<post_id>`` and the spec writes ``/posts/{post_id}``; those are the same
route and a client generator substitutes the name positionally, so only the
shape has to agree.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("APP_ENV", "testing")

#: Any path-parameter placeholder, whatever spelling it arrives in.
PARAM = re.compile(r"\{[^}]*\}|<[^:>]+>")

#: The single shape both sides are reduced to before comparison.
ANON = "{}"

#: Routes that exist for operations, not for the API contract.
IGNORED = {
    "/",
    "/static/<path:filename>",
    "/<path:subpath>",
    "/uploads/<path:filename>",
}

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def route_shape(rule: str, prefix: str) -> str | None:
    """Flask rule -> comparable path shape, or None when it is not an API route."""
    rule = rule.rstrip("/") or "/"
    if not rule.startswith(prefix):
        return None
    return PARAM.sub(ANON, rule[len(prefix) :] or "/")


def spec_shape(path: str) -> str:
    return PARAM.sub(ANON, path.rstrip("/") or "/")


def main() -> int:
    import yaml

    from app import create_app
    from app.config import get_config

    prefix = get_config("testing").API_PREFIX
    app = create_app("testing")
    spec = yaml.safe_load((ROOT / "spec" / "openapi.yaml").read_text(encoding="utf-8"))

    implemented: set[str] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule in IGNORED:
            continue
        shape = route_shape(str(rule), prefix)
        if shape is not None:
            implemented.add(shape)

    documented: dict[str, str] = {}
    for path, operations in spec["paths"].items():
        methods = sorted(m.upper() for m in operations if m.lower() in METHODS)
        documented[spec_shape(path)] = f"{','.join(methods)} {path}"

    missing = sorted(implemented - documented.keys())
    stale = sorted(documented[key] for key in documented.keys() - implemented)

    if missing or stale:
        if missing:
            print("::error::routes with no documentation: " + ", ".join(missing))
        if stale:
            print("::error::documented operations with no route: " + ", ".join(stale))
        return 1

    print(f"{len(implemented)} route shapes and {len(documented)} documented paths agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

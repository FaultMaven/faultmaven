#!/usr/bin/env python3
"""Prove a route move left the served route table unchanged, in order.

PROVES: the app built from HEAD serves exactly the routes the app built from
BASE serves, in the same order, with the same methods, path, endpoint name,
response model, status code, tags, dependencies count and include_in_schema
flag. Only the endpoint's defining MODULE may differ (that is the move). Route
ORDER matters: FastAPI matches the first route that fits, so a reorder can
change which handler answers ``GET /cases/health`` vs ``GET /cases/{case_id}``.

Uses the project's own ``faultmaven.api.route_enumeration.iter_served_routes``
(the one place the included-router walk lives), in a subprocess per tree with
PYTHONPATH pinned to that tree and ``faultmaven.__file__`` asserted inside it.

USAGE: route_table.py --base BASE_WORKTREE --head HEAD_WORKTREE [--python PY]
EXIT: 0 identical, 1 differs, 2 could not build an app.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DUMP = r"""
import json, os, sys
root = os.environ["TREE"]
import faultmaven
assert faultmaven.__file__.startswith(root), faultmaven.__file__
from fastapi.routing import APIRoute
from faultmaven.main import app

def deps(d, out):
    for x in d.dependencies:
        c = getattr(x, "call", None)
        out.append(getattr(c, "__qualname__", None) or type(c).__qualname__)
        deps(x, out)
    return out

rows = []
for route in app.routes:
    kind = type(route).__name__
    if kind == "_IncludedRouter":
        sys.exit("this FastAPI hides included routes behind _IncludedRouter; route_table.py reads the pinned 0.136 flat shape")
    row = {"kind": kind, "path": getattr(route, "path", None), "name": getattr(route, "name", None)}
    if isinstance(route, APIRoute):
        ep = route.endpoint
        row.update({
            "methods": sorted(route.methods or []),
            "qualname": ep.__qualname__,
            "module": ep.__module__,
            "response_model": repr(route.response_model),
            "status_code": route.status_code,
            "tags": list(route.tags or []),
            "include_in_schema": route.include_in_schema,
            "deprecated": route.deprecated,
            "operation_id": route.unique_id,
            "resolved_deps": sorted(deps(route.dependant, [])),
            "doc": (ep.__doc__ or "").strip(),
        })
    rows.append(row)
print(json.dumps(rows))
"""


def dump(tree: str, py: str):
    env = dict(os.environ, PYTHONPATH=tree, TREE=tree, SKIP_SERVICE_CHECKS="true")
    p = subprocess.run(
        [py, "-c", DUMP], cwd=tree, env=env, capture_output=True, text=True, timeout=600
    )
    if p.returncode != 0:
        print(p.stderr[-3000:])
        return None
    return json.loads(p.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()
    # absolute: the child asserts faultmaven.__file__ lives under the tree it was given
    base, head = str(Path(a.base).resolve()), str(Path(a.head).resolve())
    b, h = dump(base, a.python), dump(head, a.python)
    if b is None or h is None:
        print("could not build an app")
        return 2
    strip = lambda rows: [{k: v for k, v in r.items() if k != "module"} for r in rows]
    if strip(b) == strip(h):
        moved = sum(1 for x, y in zip(b, h) if x.get("module") != y.get("module"))
        print(
            f"IDENTICAL: {len(h)} routes in the same order ({moved} changed defining module)"
        )
        return 0
    print(f"DIFFERS: base {len(b)} routes, head {len(h)}")
    for i, (x, y) in enumerate(zip(strip(b), strip(h))):
        if x != y:
            print(f"  first difference at #{i}:\n    base {x}\n    head {y}")
            break
    else:
        print("  one table is a prefix of the other")
    return 1


if __name__ == "__main__":
    sys.exit(main())

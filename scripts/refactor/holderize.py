#!/usr/bin/env python3
"""Move an owner class's injected dependencies into ONE shared holder object.

DOES: step E0 of an Extract Class refactor. Every dependency attribute the
owner's ``__init__`` sets (``self.X = ...``) becomes a field of a generated
dataclass (default ``EngineDeps`` in ``dependencies.py``); ``__init__`` builds
one instance as ``self.deps``; every other method's ``self.X`` read becomes
``self.deps.X``. State attributes you name with ``--state`` stay on the owner.

WHY: once parts of the owner move into collaborator objects, a dependency the
owner AND a collaborator both read must have exactly one binding. If each held
its own reference, ``owner.X = stub`` (a test, or a late write) would reach half
the code and nothing would fail. With one holder, ``owner.deps.X = stub`` reaches
every reader. verify_extract.py proves the rewrite (spec "holder" with
"flat_base": true).

Only the owner module is rewritten. Tests and other callers that read or set a
flat ``owner.X`` are found (and rewritten) by audit_seams.py --holder.

USAGE: holderize.py --repo WT --source path/to/engine.py --class MilestoneEngine
         --module path/to/dependencies.py [--holder deps] [--holder-class EngineDeps]
         [--state _case_locks,_inflight_vectorize] [--apply]
Prints the field list; --apply writes both files. Exit 0 ok, 1 error.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--class", dest="cls", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--holder", default="deps")
    ap.add_argument("--holder-class", default="EngineDeps")
    ap.add_argument("--state", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    repo = Path(a.repo)
    src_path = repo / a.source
    text = src_path.read_text()
    lines = text.splitlines(keepends=True)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))
    off = lambda l, c: starts[l - 1] + len(lines[l - 1].encode()[:c].decode())
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == a.cls)
    init = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    state = {x for x in a.state.split(",") if x}
    params = {x.arg: x for x in init.args.args[1:] + init.args.kwonlyargs}

    fields, assigns = [], []
    for st in init.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)):
            tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
            if (
                isinstance(tg, ast.Attribute)
                and isinstance(tg.value, ast.Name)
                and tg.value.id == "self"
                and tg.attr not in state
                and not tg.attr.startswith("_")
            ):
                fields.append((tg.attr, st))
                assigns.append(st)
    if a.holder in {f for f, _ in fields}:
        print(f"ERROR: the owner already has an attribute named {a.holder}")
        return 1
    names = [f for f, _ in fields]
    print(f"{len(names)} fields: {', '.join(names)}")
    print(f"stays on the owner: {sorted(state) or '(none)'}")

    edits = []
    # every other method: self.X -> self.deps.X
    for m in cls.body:
        if (
            not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            or m.name == "__init__"
        ):
            continue
        for n in ast.walk(m):
            if (
                isinstance(n, ast.Attribute)
                and n.attr in names
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
            ):
                if isinstance(n.ctx, ast.Store):
                    print(
                        f"ERROR: {m.name} assigns self.{n.attr}: a dependency must not be rebound after construction"
                    )
                    return 1
                e = off(n.value.end_lineno, n.value.end_col_offset)
                edits.append((e, e, f".{a.holder}"))
    # __init__: the flat assignments become one holder construction at the first one's place
    kw = []
    for f, st in fields:
        kw.append(f"            {f}={ast.get_source_segment(text, st.value)},\n")
    first = assigns[0]
    ind = " " * first.col_offset
    build = f"{ind}self.{a.holder} = {a.holder_class}(\n" + "".join(kw) + f"{ind})\n"
    for i, st in enumerate(assigns):
        s0, e0 = starts[st.lineno - 1], starts[st.end_lineno]
        edits.append((s0, e0, build if i == 0 else ""))
    # dataclass module
    field_lines = []
    for f, st in fields:
        ann = (
            params[f].annotation
            if f in params and params[f].annotation is not None
            else None
        )
        t = ast.unparse(ann) if ann is not None else "Any"
        if "None" not in t and t != "Any":
            t = f"{t} | None"
        field_lines.append(f"    {f}: {t} = None\n")
    mod_path = Path(a.module)
    dotted_owner = ".".join(Path(a.source).with_suffix("").parts)
    module_text = (
        f'"""The dependencies of {a.cls}, held once and shared with its collaborators.\n\n'
        f"{a.cls} and every collaborator it builds read a dependency through the one\n"
        f"``{a.holder_class}`` instance the engine owns, so each dependency has exactly one\n"
        f"binding: replacing ``engine.{a.holder}.X`` reaches every reader. Every field\n"
        f"defaults to None so a test can build a holder with only what it exercises;\n"
        f"``{a.cls}.__init__`` always passes every field.\n"
        '"""\n\nfrom __future__ import annotations\n\nfrom dataclasses import dataclass\nfrom typing import Any\n\n\n'
        f"@dataclass\nclass {a.holder_class}:\n"
        f'    """Every dependency {a.cls} and its collaborators read."""\n\n'
        + "".join(field_lines)
    )
    out = text
    for s0, e0, r in sorted(edits, reverse=True):
        out = out[:s0] + r + out[e0:]
    # import the holder class after the owner's last top-level import
    last = max(
        n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    )
    ol = out.splitlines(keepends=True)
    imp = f"from {'.'.join(mod_path.with_suffix('').parts)} import {a.holder_class}\n"
    out = "".join(ol[:last]) + imp + "".join(ol[last:])
    if a.apply:
        src_path.write_text(out)
        (repo / mod_path).write_text(module_text)
        print(
            f"wrote {a.source} and {a.module}; run ruff --fix and black on both, then fix the field types"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

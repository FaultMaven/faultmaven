#!/usr/bin/env python3
"""Check a proposed module split for closure before anyone moves code.

PROVES: whether a proposed grouping of an old module's top-level statements
into submodules is a valid split at all — no group needs a name that stays
in the facade remainder (a cycle through the very file the split is meant to
delete), no two groups need each other (a cycle nothing can order), and which
groups would separate a patched name from the reader that made the patch
bite.

WHY: the cheapest time to discover a split doesn't close is before anyone
moves a line. Discovering it after — verify_move.py and clean_refs.py both
pass on a decomposition that just re-introduces the cycle through a
different name (an ``import X from the package`` inside a submodule) — costs
a second decomposition. This tool takes the same grouping a human would sketch
on paper (submodule -> line ranges) and checks its name graph before any code
moves.

Given the old module and a grouping (submodule -> list of 'start-end' line
ranges of top-level statements), reports per submodule:
  * size in lines;
  * which top-level names of the old module it needs from each other group,
    and from what stays in __init__ ("__init__" is the implicit remainder
    group) -- a submodule needing __init__ is a cycle through the facade and
    is illegal;
  * group-to-group cycles;
  * which PATCHED names (from a baseline audit_patches.py JSON) its function
    bodies read -- each is a patch that would stop biting unless retargeted.

USAGE: plan_split.py <old.py> <grouping.json> [<baseline_audit.json>]

  grouping.json: {"submodule_name": ["12-40", "88-120"], ...} — 1-indexed,
  inclusive line ranges of top-level statements in <old.py>. Anything not
  covered by a range is the implicit "__init__" remainder group.
  baseline_audit.json: the --json output of audit_patches.py against the
  pre-split module, used only to flag PATCHED names (optional).

This is advisory: it prints a report and always exits 0 (there is no single
right split to gate on — ILLEGAL cycles and "moved-group cycles" are the
findings a human reviewing the plan needs to see and resolve before moving
any code).
"""

import ast
import json
import sys
from collections import defaultdict


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, grouping_path = sys.argv[1], sys.argv[2]
    audit = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
    src = open(path).read()
    tree = ast.parse(src)
    groups = json.load(open(grouping_path))
    ranges = []
    for g, rs in groups.items():
        for r in rs:
            a, b = (int(x) for x in r.split("-"))
            ranges.append((a, b, g))

    def group_of(line):
        for a, b, g in ranges:
            if a <= line <= b:
                return g
        return "__init__"

    defined = {}  # name -> group
    stmt_group = []
    for node in tree.body:
        start = min(
            [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
        )
        g = group_of(start)
        if g != "__init__" and group_of(node.end_lineno) != g:
            print(
                f"WARNING: statement {start}-{node.end_lineno} straddles a range boundary"
            )
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.append((a.asname or a.name).split(".")[0])
            g = "__imports__"
        for n in names:
            defined.setdefault(n, g)
        stmt_group.append((node, g, start))

    patched = set()
    if audit:
        for r in audit["refs"]:
            if r["kind"] in ("string", "object") and r.get("verdict") == "OK":
                patched.add(r["target"].split(".")[-1])

    size = defaultdict(int)
    needs = defaultdict(lambda: defaultdict(set))
    reads_patched = defaultdict(set)
    imports_needed = defaultdict(set)
    for node, g, start in stmt_group:
        if g == "__imports__":
            continue
        size[g] += node.end_lineno - start + 1
        # every Name load anywhere in the statement (module-level expressions too)
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Load)
                and n.id in defined
            ):
                dg = defined[n.id]
                if dg == "__imports__":
                    imports_needed[g].add(n.id)
                elif dg != g:
                    needs[g][dg].add(n.id)
                if n.id in patched:
                    reads_patched[g].add(n.id)

    total = len(src.splitlines())
    print(
        f"# {path}: {total} lines; groups: {', '.join(groups)} (+ __init__ remainder)"
    )
    for g in list(groups) + ["__init__"]:
        print(f"\n## {g}: {size[g]} lines of top-level statements")
        for dg, names in sorted(needs[g].items()):
            tag = (
                "  <-- ILLEGAL (facade cycle)"
                if dg == "__init__" and g != "__init__"
                else ""
            )
            print(
                f"  needs from {dg}: {len(names)} {sorted(names)[:25]}{' ...' if len(names) > 25 else ''}{tag}"
            )
        if reads_patched[g] and g != "__init__":
            print(
                f"  reads PATCHED names (retarget needed): {sorted(reads_patched[g])}"
            )
    # cycles between moved groups
    edges = {
        (g, dg)
        for g in needs
        for dg in needs[g]
        if g != "__init__" and dg != "__init__"
    }
    cyc = [(a, b) for (a, b) in edges if (b, a) in edges and a < b]
    print("\nmoved-group cycles:", cyc or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())

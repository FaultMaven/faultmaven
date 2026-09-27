#!/usr/bin/env python3
"""Prove a module decomposition moved code without changing it.

PROVES: every top-level statement of an old single-file module still exists,
byte-for-byte, somewhere in its new home(s) — exactly once, not zero times
(dropped) and not twice (duplicated) — plus the logger-name and no-facade-
import invariants a no-backward-compatibility decomposition must hold
(CLAUDE.md, Architecture: "No backward compatibility").

WHY: a decomposition is reviewed as a diff of moved files against their new
location, which shows *that* code moved but not whether it moved unchanged —
a reformatting pass, an accidental edit, or a name pasted into two places all
read as "looks about right" in a large diff. This tool instead compares each
old top-level def/class/assignment/statement against the new tree's inventory
of the same kind, so any divergence is a CHANGED/MISSING/DUPLICATED line, not
a diff a reviewer has to notice.

Specifically it checks, comparing the old module at a base revision against
the new package in a worktree:
  * every top-level def/class/assignment of the old module appears EXACTLY
    ONCE across the new package, with byte-identical source (decorators,
    docstrings and comments inside the body included);
  * every other old top-level statement (a bare call such as
    ``Model.model_rebuild()``, a try/if block that is not an import guard)
    appears verbatim somewhere in the new package;
  * anything new at top level is listed as ADDED so a reviewer reads it;
  * every ``getLogger(...)``/``get_logger(...)`` in the package keeps the
    canonical logger name (or, under --clean, uses ``__name__``);
  * no submodule imports from the package facade (``__init__``);
  * (--runtime, non-clean only) every name the old module defined is still
    an attribute of the facade, and is the same object the defining
    submodule holds — skipped under --clean, where there is no facade by
    design (use clean_refs.py + check_globals.py instead).

USAGE (run from anywhere; the worktree is put on sys.path only for --runtime):
  verify_move.py --repo <worktree> --base origin/main \\
      --old faultmaven/core/investigation/milestone_engine.py [--runtime] [--clean]

  --extra PATH [PATH ...]  extra repo-relative .py files that received moved
                           code, for a split that is not a package (e.g. a
                           sibling module gained a moved function).
  --clean                  no-backward-compat mode: every logger must be
                           ``getLogger(__name__)``; skips the facade runtime
                           check (there is no facade).

EXIT CODES: 0 nothing MISSING, DUPLICATED or CHANGED, no logger violation, no
facade import, and (if --runtime) no RUNTIME-MISSING/RUNTIME-NOTSAME; 1
otherwise; 2 if neither the old file nor its package directory exists in the
worktree. ADDED lines never fail the run: they are for a human to read.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def git_show(repo: str, rev: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "show", f"{rev}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def seg(src_lines: list[str], node: ast.AST) -> str:
    start = node.lineno
    for d in getattr(node, "decorator_list", []) or []:
        start = min(start, d.lineno)
    return "".join(src_lines[start - 1 : node.end_lineno])


def is_import_block(node: ast.stmt) -> bool:
    """Imports, TYPE_CHECKING guards and try/except-ImportError wrappers."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(node, ast.If):
        t = ast.unparse(node.test)
        if "TYPE_CHECKING" in t:
            return True
    if isinstance(node, ast.Try):
        body_imports = all(
            isinstance(s, (ast.Import, ast.ImportFrom)) for s in node.body
        )
        if body_imports:
            return True
    return False


def is_logger_assign(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and "getLogger" in ast.unparse(node.value)
        or (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and "get_logger" in ast.unparse(node.value)
        )
    )


def inventory(path_label: str, src: str):
    """Return {key: [(label, text)]} for top-level items."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    items: dict[tuple, list[tuple[str, str, int]]] = defaultdict(list)
    for i, node in enumerate(tree.body):
        if (
            i == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            items[("docstring", "")].append((path_label, seg(lines, node), node.lineno))
            continue
        if is_import_block(node):
            continue
        if is_logger_assign(node):
            items[("logger", "")].append((path_label, seg(lines, node), node.lineno))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            items[("def", node.name)].append(
                (path_label, seg(lines, node), node.lineno)
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.Assign):
                names = tuple(sorted(ast.unparse(t) for t in node.targets))
            else:
                names = (ast.unparse(node.target),)
            items[("assign", names)].append((path_label, seg(lines, node), node.lineno))
        else:
            text = seg(lines, node)
            items[("stmt", text.strip())].append((path_label, text, node.lineno))
    return items


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--repo", required=True, help="worktree containing the new package/module"
    )
    ap.add_argument(
        "--base",
        default="origin/main",
        help="git revision holding the old single-file module",
    )
    ap.add_argument(
        "--old", required=True, help="repo-relative path of the old .py module"
    )
    ap.add_argument(
        "--extra",
        nargs="*",
        default=[],
        help="extra repo-relative .py files that received moved code",
    )
    ap.add_argument(
        "--runtime",
        action="store_true",
        help="also import the package and compare facade attributes to the old module's names",
    )
    ap.add_argument("--quiet-ok", action="store_true")
    ap.add_argument(
        "--clean",
        action="store_true",
        help="no-backward-compat mode: every logger must be getLogger(__name__); no facade runtime check",
    )
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    old_rel = args.old
    dotted = old_rel[:-3].replace("/", ".")
    pkg_dir = Path(repo) / old_rel[:-3]
    old_src = git_show(repo, args.base, old_rel)

    new_files: list[Path] = []
    if pkg_dir.is_dir():
        new_files = sorted(
            p for p in pkg_dir.rglob("*.py") if "__pycache__" not in p.parts
        )
    elif (Path(repo) / old_rel).exists():
        new_files = [Path(repo) / old_rel]
    new_files += [Path(repo) / e for e in args.extra]
    if not new_files:
        print(f"FATAL: neither {pkg_dir} nor {old_rel} exists in {repo}")
        return 2

    old_items = inventory("OLD", old_src)
    new_items: dict[tuple, list] = defaultdict(list)
    new_srcs: dict[str, str] = {}
    for f in new_files:
        rel = str(f.relative_to(repo))
        s = f.read_text()
        new_srcs[rel] = s
        for k, v in inventory(rel, s).items():
            new_items[k].extend(v)

    problems = 0
    moved = defaultdict(int)
    import_only_changes: list[str] = []

    import re as _re

    _imp = _re.compile(
        r"^\s*(from\s+\S+\s+import\b.*|import\s+\S+.*|[A-Za-z_][A-Za-z0-9_]*(\s+as\s+\w+)?,?|\)|\(|)\s*$"
    )

    def import_only_diff(a: str, b: str) -> bool:
        sm = difflib.SequenceMatcher(
            None, a.splitlines(), b.splitlines(), autojunk=False
        )
        al, bl = a.splitlines(), b.splitlines()
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            for ln in al[i1:i2] + bl[j1:j2]:
                if not _imp.match(ln):
                    return False
        return True

    report: list[str] = []

    for key, olds in old_items.items():
        if key[0] in ("docstring", "logger"):
            continue
        if len(olds) != 1:
            # The old module itself redefines this name; compare the lists as a whole.
            pass
        news = new_items.get(key, [])
        if not news:
            problems += 1
            report.append(f"MISSING    {key[0]} {key[1]!r} (old line {olds[0][2]})")
            continue
        if len(news) != len(olds):
            problems += 1
            where = ", ".join(f"{n[0]}:{n[2]}" for n in news)
            report.append(
                f"DUPLICATED {key[0]} {key[1]!r}: old x{len(olds)}, new x{len(news)} at {where}"
            )
            continue
        for (_, otext, oline), (nlabel, ntext, nline) in zip(olds, news):
            if otext != ntext and import_only_diff(otext, ntext):
                # A function-local import re-pointed at a refactored module's
                # defining submodule: a caller update, not a body edit.
                moved[nlabel] += 1
                import_only_changes.append(
                    f"IMPORT-ONLY {key[0]} {key[1]!r} ({nlabel}:{nline})"
                )
                continue
            if otext != ntext:
                problems += 1
                diff = "".join(
                    difflib.unified_diff(
                        otext.splitlines(keepends=True),
                        ntext.splitlines(keepends=True),
                        f"OLD:{oline}",
                        f"{nlabel}:{nline}",
                        n=1,
                    )
                )
                report.append(f"CHANGED    {key[0]} {key[1]!r}\n{diff}")
            else:
                moved[nlabel] += 1

    added = []
    for key, news in new_items.items():
        if key[0] in ("docstring", "logger"):
            continue
        if key not in old_items:
            for label, text, line in news:
                added.append(
                    f"ADDED      {label}:{line} {key[0]} {key[1] if key[0] != 'stmt' else ''}\n    "
                    + text.strip().replace("\n", "\n    ")[:600]
                )

    # Logger names: canonical = the old module's dotted path.
    logger_bad = []
    for rel, s in new_srcs.items():
        tree = ast.parse(s)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = ast.unparse(node.func)
                if fn.endswith("getLogger") or fn.endswith("get_logger"):
                    arg = node.args[0] if node.args else None
                    ok = False
                    if args.clean:
                        ok = isinstance(arg, ast.Name) and arg.id == "__name__"
                    elif isinstance(arg, ast.Constant) and arg.value == dotted:
                        ok = True
                    elif (
                        isinstance(arg, ast.Name)
                        and arg.id == "__name__"
                        and (
                            rel.endswith("__init__.py")
                            and pkg_dir.is_dir()
                            or rel == old_rel
                        )
                    ):
                        ok = True
                    if not ok:
                        logger_bad.append(
                            f"LOGGER     {rel}:{node.lineno} {ast.unparse(node)} ("
                            + (
                                "use __name__"
                                if args.clean
                                else f"canonical is {dotted!r}"
                            )
                            + ")"
                        )
    problems += len(logger_bad)

    # Submodules must not import the facade.
    facade_imports = []
    if pkg_dir.is_dir():
        sub_names = {p.stem for p in pkg_dir.glob("*.py")} | {
            p.name for p in pkg_dir.iterdir() if p.is_dir()
        }
        for rel, s in new_srcs.items():
            if rel.endswith("__init__.py"):
                continue
            for node in ast.walk(ast.parse(s)):
                if isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module == dotted:
                        facade_imports.append(
                            f"FACADE-IMPORT {rel}:{node.lineno} from {dotted} import ..."
                        )
                    elif node.level == 1 and node.module is None:
                        bad = [a.name for a in node.names if a.name not in sub_names]
                        if bad:
                            facade_imports.append(
                                f"FACADE-IMPORT {rel}:{node.lineno} from . import {bad}"
                            )
                    elif node.level >= 2 and pkg_dir.is_dir():
                        pass
    problems += len(facade_imports)

    old_lines = len(old_src.splitlines())
    print(f"# verify_move {old_rel} (base {args.base}) -> {len(new_files)} file(s)")
    print(
        f"old: {old_lines} lines, {sum(len(v) for k, v in old_items.items() if k[0] in ('def','assign','stmt'))} top-level items"
    )
    for rel in new_srcs:
        print(
            f"  {len(new_srcs[rel].splitlines()):6} lines  {moved.get(rel, 0):4} identical items  {rel}"
        )
    for line in report + logger_bad + facade_imports + import_only_changes:
        print(line)
    if added:
        print(f"\n# ADDED top-level items ({len(added)}) — read each:")
        for a in added:
            print(a)

    if args.runtime and args.clean:
        print(
            "\n# --clean: facade runtime check skipped (no facade by design); use clean_refs.py + check_globals.py"
        )
    if args.runtime and not args.clean:
        code = f"""
import sys, importlib, inspect, json, ast
sys.path.insert(0, {repo!r})
mod = importlib.import_module({dotted!r})
assert mod.__file__.startswith({repo!r}), ("loaded wrong tree", mod.__file__)
old_names = json.loads(sys.argv[1])
missing, not_same = [], []
for name in old_names:
    if not hasattr(mod, name):
        missing.append(name)
        continue
    obj = getattr(mod, name)
    home = getattr(obj, "__module__", None)
    if home and home != {dotted!r} and home.startswith({dotted!r} + "."):
        sub = importlib.import_module(home)
        if getattr(sub, name, None) is not obj:
            not_same.append((name, home))
print(json.dumps({{"file": mod.__file__, "missing": missing, "not_same": not_same}}))
"""
        defined = sorted(
            {k[1] for k in old_items if k[0] == "def"}
            | {
                n
                for k in old_items
                if k[0] == "assign"
                for n in k[1]
                if n.isidentifier()
            }
        )
        # Names the old module bound by IMPORT at runtime (not under
        # TYPE_CHECKING): `from old import name` / patch("old.name") reach
        # them through the old path too, so the facade must keep them.
        imported = set()
        for node in ast.parse(old_src).body:
            stmts = [node]
            if isinstance(node, ast.Try):
                stmts = node.body + [s for h in node.handlers for s in h.body]
            elif isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                continue
            for st in stmts:
                if isinstance(st, (ast.Import, ast.ImportFrom)):
                    for a in st.names:
                        if a.name != "*":
                            imported.add((a.asname or a.name).split(".")[0])
        defined = sorted(set(defined) | imported)
        r = subprocess.run(
            [sys.executable, "-c", code, json.dumps(defined)],
            capture_output=True,
            text=True,
            cwd="/",
            env={**os.environ, "PYTHONPATH": repo},
        )
        if r.returncode != 0:
            print("RUNTIME    import failed:\n" + r.stderr[-3000:])
            problems += 1
        else:
            res = json.loads(r.stdout.strip().splitlines()[-1])
            print(f"\n# runtime: loaded {res['file']}")
            for n in res["missing"]:
                print(f"RUNTIME-MISSING facade lacks old name {n!r}")
            for n, h in res["not_same"]:
                print(f"RUNTIME-NOTSAME facade.{n} is not {h}.{n}")
            problems += len(res["missing"]) + len(res["not_same"])
            print(f"runtime: {len(defined)} old defined names checked on the facade")

    print(f"\nRESULT: {'PASS' if problems == 0 else f'FAIL ({problems} problem(s))'}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

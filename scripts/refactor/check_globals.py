#!/usr/bin/env python3
"""Prove moved code still resolves every global name to the same object.

PROVES: for every function/class the old module defined, that each global
name its body reads resolves, in the new home, to the very same object (an
imported dependency) or the package's own moved copy (a name the old module
defined) — not a look-alike bound under the same name.

WHY: verify_move.py proves a moved body is byte-identical text. It cannot
prove the body MEANS the same thing: a submodule that imports a different
object under the same name (``import datetime`` vs ``from datetime import
datetime``, or a same-named class from another module) changes behaviour
with identical text, and no linter flags it. This is exactly the blind spot a
line-based diff and an import-sorter both share: they only see spelling.

This loads the OLD single-file module from the base tree under a throwaway
name inside the HEAD tree's interpreter (so both share every dependency
object), then, for every function/class the old module defined, compares
each global name its code reads:
  * a name the old module IMPORTED must resolve, in the new home, to the
    very same object;
  * a name the old module DEFINED must resolve, in the new home, to the
    package's object of the same name (the moved copy), same kind.

Reads are found with ``symtable`` rather than a plain AST walk, so a name
used only inside a lambda or a class body — the blind spot a naive walk that
stops at the first function boundary misses — is still counted as a global
read of its enclosing module.

USAGE:
  check_globals.py --repo <head worktree> --base <base worktree> \\
      --old faultmaven/core/investigation/causal_graph.py \\
      [--homes dotted.module ...]   # extra modules that received moved code

--repo and --base are both worktree directories (not revisions): --base must
be a checkout of the revision that still has the old single-file module, so
its file can be exec'd standalone under a private module name.

EXIT CODES: 0 no REBOUND/DIVERGED/VALUE/UNBOUND finding; 1 at least one
finding; 2 the runtime subprocess itself failed (import error, syntax error) —
distinct from 1 because it means the check could not run, not that it ran
and found nothing wrong.
"""

import argparse
import json
import os
import subprocess
import sys

CODE = r"""
import ast, builtins, importlib, importlib.util, json, pkgutil, symtable, sys
repo, base_file, dotted = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)
new = importlib.import_module(dotted)
assert new.__file__.startswith(repo), ("loaded wrong tree", new.__file__)
parent, leaf = dotted.rsplit(".", 1)
old_name = f"{parent}._refactor_tools_old_{leaf}"
spec = importlib.util.spec_from_file_location(old_name, base_file)
old = importlib.util.module_from_spec(spec)
sys.modules[old_name] = old
spec.loader.exec_module(old)

tree = ast.parse(open(base_file).read())
defined = set()
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined.add(n.name)
    elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
            for x in ast.walk(t):
                if isinstance(x, ast.Name): defined.add(x.id)

def global_reads(path):
    # Every name any scope of the file (module, class bodies, functions,
    # lambdas, comprehensions) resolves through MODULE GLOBALS.
    top = symtable.symtable(open(path).read(), path, "exec")
    out = set()
    def walk(t):
        for sym in t.get_symbols():
            if not sym.is_referenced():
                continue
            if t.get_type() == "module" or sym.is_global():
                out.add(sym.get_name())
        for c in t.get_children():
            walk(c)
    walk(top)
    return out

mods = [new]
if hasattr(new, "__path__"):
    for info in pkgutil.walk_packages(new.__path__, dotted + "."):
        mods.append(importlib.import_module(info.name))
for extra in [x for x in sys.argv[4].split(",") if x]:
    mods.append(importlib.import_module(extra))

# Where each name is DEFINED now (no facade to ask): scan every home module's AST.
home_obj = {}
defined_by_module = {}
for m in mods:
    t = ast.parse(open(m.__file__).read())
    for n in t.body:
        names = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for tt in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                names += [x.id for x in ast.walk(tt) if isinstance(x, ast.Name)]
        for nm in names:
            home_obj.setdefault(nm, getattr(m, nm, None))
            defined_by_module.setdefault(m.__name__, set()).add(nm)

sentinel = object()
problems, checked = [], 0
for m in mods:
    for g in sorted(global_reads(m.__file__)):
        if g not in old.__dict__ or g.startswith("__"):
            continue
        ov = old.__dict__[g]
        nv = m.__dict__.get(g, sentinel)
        if nv is sentinel:
            nv = getattr(builtins, g, sentinel)
        checked += 1
        where = m.__name__
        if g in defined:
            # A name the reader's own module defines (its own logger, say) is
            # compared against that module's binding, not another home's.
            own_defs = defined_by_module.get(m.__name__, set())
            pv = getattr(m, g) if g in own_defs else home_obj.get(g, getattr(new, g, sentinel))
            if nv is sentinel:
                problems.append(f"UNBOUND {where}: reads {g!r}, which it does not bind")
            elif nv is pv:
                pass
            elif callable(ov) or isinstance(ov, type):
                problems.append(f"DIVERGED {where}: {g!r} is not the package's {g}")
            else:
                try:
                    same = bool(nv == ov)
                except Exception:
                    same = False
                if not same:
                    problems.append(f"VALUE {where}: constant {g!r} differs from the old module's")
        else:
            if nv is not ov:
                problems.append(f"REBOUND {where}: global {g!r} was {ov!r}, now {nv!r}")
print(json.dumps({"checked": checked, "problems": problems, "new_file": new.__file__, "modules": len(mods)}))
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--repo", required=True, help="worktree holding the new package (the HEAD side)"
    )
    ap.add_argument(
        "--base",
        required=True,
        help="worktree holding the old single-file module (a checkout, not a revision)",
    )
    ap.add_argument(
        "--old",
        required=True,
        help="repo-relative path of the old module, resolved under --base",
    )
    ap.add_argument(
        "--homes",
        nargs="*",
        default=[],
        help="extra dotted modules that received moved code (non-package splits)",
    )
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    base_file = os.path.join(os.path.abspath(a.base), a.old)
    dotted = a.old[:-3].replace("/", ".")
    r = subprocess.run(
        [sys.executable, "-c", CODE, repo, base_file, dotted, ",".join(a.homes)],
        capture_output=True,
        text=True,
        cwd="/",
        env={**os.environ, "PYTHONPATH": repo},
    )
    if r.returncode != 0:
        print("FAILED TO RUN:\n" + r.stderr[-4000:])
        return 2
    res = json.loads(r.stdout.strip().splitlines()[-1])
    print(f"# check_globals {dotted}: loaded {res['new_file']}")
    print(f"global references checked: {res['checked']}")
    for p in res["problems"]:
        print(p)
    print(
        f"RESULT: {'PASS' if not res['problems'] else 'FAIL (%d)' % len(res['problems'])}"
    )
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())

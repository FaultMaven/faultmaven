#!/usr/bin/env python3
"""Audit whether every test patch of a decomposed module still bites.

PROVES: for every ``patch``-like reference to a module under decomposition,
whether the code path under test actually reads the name through the
namespace the patch replaces — i.e. whether the patch still does anything.

WHY: ``patch("pkg.mod.name")`` replaces ONE binding: the attribute ``name``
on ``pkg.mod``. Code sees the replacement only if it looks ``name`` up
through ``pkg.mod``'s globals at call time. When a module becomes a package
and a caller of ``name`` moves into a submodule, the facade still HAS
``name`` (so the patch applies without error) but the moved caller reads its
own submodule's binding — the patch goes silently inert for it. A test using
that patch keeps passing throughout: nothing raises, nothing is asserted
false, because the double it built was simply never consulted. This is the
"a green test proved nothing" failure mode, made mechanical rather than
something a reviewer has to notice by reading every patch site.

For every patch-like reference to the module (string targets in
patch/mock.patch/mocker.patch/monkeypatch.setattr, and patch.object/setattr
on a module object) this tool resolves, at runtime in the given worktree:

  patched namespace  = the module whose attribute is replaced
  readers            = the package's modules whose FUNCTION BODIES read
                        ``name`` as a global (not a parameter or local)
  bypass             = readers - {patched namespace}

VERDICTS: OK (bypass empty), FLAG (some reader bypasses the patch), INERT (no
reader goes through the patched namespace at all), CLASS_ATTR (patching an
attribute of a class/object — always bites), MISSING (the name no longer
exists: the patch would raise), DICT (patch.dict mutates in place — bites).

Run it on the base worktree and on the lane worktree, then --compare: only
verdicts that got WORSE are the lane's to resolve (retarget the patch string
to the namespace the code under test reads, or show the test exercises only
readers in the patched namespace). A FLAG/INERT verdict is a hypothesis, not
a verdict on its own — adjudicate it dynamically with poison_plugin.py before
treating it as a real defect.

USAGE:
  audit_patches.py --repo <tree> --module faultmaven.core.investigation.causal_graph \\
      [--scan tests faultmaven scripts] [--json out.json] [--compare base.json]

EXIT CODES: without --compare, always 0 (this is an audit, not a gate — read
the FLAG/INERT/MISSING lines). With --compare: 0 nothing got worse and no
patch key was lost; 1 at least one reference regressed or a patch key present
in the baseline is gone from the head. 2 the runtime resolution subprocess
failed to run at all.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

PATCH_FUNCS = (
    "patch",
    "patch.object",
    "setattr",
    "patch.dict",
    "patch.multiple",
    "delattr",
)


def call_name(node: ast.Call) -> str:
    try:
        return ast.unparse(node.func)
    except Exception:
        return "?"


def is_patch_like(fn: str) -> bool:
    return any(fn == p or fn.endswith("." + p) for p in PATCH_FUNCS)


def module_exists(repo: Path, dotted: str) -> bool:
    p = repo / dotted.replace(".", "/")
    return p.with_suffix(".py").exists() or (p / "__init__.py").exists()


def file_aliases(tree: ast.Module, repo: Path, rel_pkg: str | None) -> dict[str, str]:
    """name -> dotted module, for names bound to MODULES by this file's imports."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    out[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level and rel_pkg is not None:
                parts = rel_pkg.split(".")
                parts = (
                    parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                )
                base = ".".join(parts + ([node.module] if node.module else []))
            for a in node.names:
                cand = f"{base}.{a.name}" if base else a.name
                if module_exists(repo, cand):
                    out[a.asname or a.name] = cand
    return out


def resolve_expr_module(expr: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id)
    if isinstance(expr, ast.Attribute):
        parent = resolve_expr_module(expr.value, aliases)
        if parent:
            return f"{parent}.{expr.attr}"
    return None


def collect_refs(repo: Path, module: str, scan: list[str]):
    refs = []
    for d in scan:
        root = repo / d
        if not root.exists():
            continue
        for f in root.rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            rel = str(f.relative_to(repo))
            try:
                src = f.read_text()
                tree = ast.parse(src)
            except Exception:
                continue
            if module.split(".")[-1] not in src:
                continue
            rel_pkg = ".".join(f.relative_to(repo).with_suffix("").parts[:-1])
            aliases = file_aliases(tree, repo, rel_pkg)
            seen_consts: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = call_name(node)
                args = list(node.args) + [
                    k.value for k in node.keywords if k.arg in ("target",)
                ]
                if is_patch_like(fn):
                    # string target form
                    for a in args[:1]:
                        if (
                            isinstance(a, ast.Constant)
                            and isinstance(a.value, str)
                            and (a.value == module or a.value.startswith(module + "."))
                        ):
                            seen_consts.add(id(a))
                            kind = "dict" if fn.endswith("patch.dict") else "string"
                            refs.append(
                                {
                                    "file": rel,
                                    "line": node.lineno,
                                    "call": fn,
                                    "kind": kind,
                                    "target": a.value,
                                }
                            )
                    # object form: patch.object(mod, "name") / setattr(mod, "name", v)
                    if (
                        len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)
                    ):
                        m = resolve_expr_module(node.args[0], aliases)
                        if m and (m == module or m.startswith(module + ".")):
                            refs.append(
                                {
                                    "file": rel,
                                    "line": node.lineno,
                                    "call": fn,
                                    "kind": "object",
                                    "target": f"{m}.{node.args[1].value}",
                                }
                            )
                elif fn.endswith("reload") and node.args:
                    m = resolve_expr_module(node.args[0], aliases)
                    if m and (m == module or m.startswith(module + ".")):
                        refs.append(
                            {
                                "file": rel,
                                "line": node.lineno,
                                "call": fn,
                                "kind": "reload",
                                "target": m,
                            }
                        )
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in seen_consts
                ):
                    v = node.value
                    if (
                        (v == module or v.startswith(module + "."))
                        and " " not in v
                        and len(v) < 200
                    ):
                        refs.append(
                            {
                                "file": rel,
                                "line": getattr(node, "lineno", 0),
                                "call": "(literal)",
                                "kind": "other",
                                "target": v,
                            }
                        )
    return refs


RUNTIME = r"""
import ast, importlib, json, sys, os
from pathlib import Path
repo = sys.argv[1]; module = sys.argv[2]; targets = json.loads(sys.argv[3])
sys.path.insert(0, repo)
root = importlib.import_module(module)
assert root.__file__.startswith(repo), ("loaded wrong tree", root.__file__)

def pkg_files(modname):
    m = importlib.import_module(modname)
    if hasattr(m, "__path__"):
        d = Path(m.__path__[0])
        base = modname
        out = {}
        for f in d.rglob("*.py"):
            if "__pycache__" in f.parts: continue
            relparts = f.relative_to(d).with_suffix("").parts
            name = base if relparts == ("__init__",) else base + "." + ".".join(p for p in relparts if p != "__init__")
            out[name] = f
        return out
    return {modname: Path(m.__file__)}

files = pkg_files(module)
readers_cache = {}

def global_reads(path):
    if path in readers_cache: return readers_cache[path]
    tree = ast.parse(Path(path).read_text())
    reads = set()
    def visit_fn(fn):
        local = set()
        a = fn.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []):
            local.add(arg.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                local.add(n.id)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                for g in n.names: local.discard(g)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in local:
                reads.add(n.id)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if isinstance(n, ast.Lambda):
                for m in ast.walk(n.body):
                    if isinstance(m, ast.Name) and isinstance(m.ctx, ast.Load): reads.add(m.id)
            else:
                visit_fn(n)
    readers_cache[path] = reads
    return reads

out = {}
for t in targets:
    parts = t.split(".")
    mod = None; i = len(parts)
    while i > 0:
        try:
            mod = importlib.import_module(".".join(parts[:i])); break
        except Exception:
            i -= 1
    modname = ".".join(parts[:i]); rest = parts[i:]
    if not rest:
        out[t] = {"verdict": "MODULE", "patched": modname}; continue
    if len(rest) > 1:
        out[t] = {"verdict": "CLASS_ATTR", "patched": modname}; continue
    name = rest[0]
    if not hasattr(mod, name):
        out[t] = {"verdict": "MISSING", "patched": modname}; continue
    readers = sorted(m for m, f in files.items() if name in global_reads(str(f)))
    bypass = [r for r in readers if r != modname]
    if not bypass:
        v = "OK"
    elif modname not in readers:
        v = "INERT"
    else:
        v = "FLAG"
    out[t] = {"verdict": v, "patched": modname, "readers": readers, "bypass": bypass,
              "home": getattr(getattr(mod, name), "__module__", None) if callable(getattr(mod, name)) else None}
print(json.dumps(out))
"""

RANK = {
    "OK": 0,
    "CLASS_ATTR": 0,
    "DICT": 0,
    "MODULE": 0,
    "FLAG": 2,
    "INERT": 3,
    "MISSING": 4,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument(
        "--module", required=True, help="dotted module or package under decomposition"
    )
    ap.add_argument(
        "--scan",
        nargs="*",
        default=["tests", "faultmaven", "scripts"],
        help="repo-relative directories to scan for patch sites",
    )
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument(
        "--compare", help="base result JSON; report only verdicts that got worse"
    )
    args = ap.parse_args()
    repo = Path(os.path.abspath(args.repo))

    refs = collect_refs(repo, args.module, args.scan)
    targets = sorted({r["target"] for r in refs if r["kind"] in ("string", "object")})
    r = subprocess.run(
        [sys.executable, "-c", RUNTIME, str(repo), args.module, json.dumps(targets)],
        capture_output=True,
        text=True,
        cwd="/",
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    if r.returncode != 0:
        print("RUNTIME FAILED:\n" + r.stderr[-4000:])
        return 2
    verdicts = json.loads(r.stdout.strip().splitlines()[-1])
    for ref in refs:
        if ref["kind"] == "dict":
            ref["verdict"] = "DICT"
        elif ref["kind"] in ("string", "object"):
            ref.update(verdicts.get(ref["target"], {"verdict": "?"}))
        else:
            ref["verdict"] = ref["kind"].upper()

    result = {"repo": str(repo), "module": args.module, "refs": refs}
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))

    c = Counter(ref["verdict"] for ref in refs)
    print(f"# audit_patches {args.module} in {repo}")
    print("verdicts:", dict(c))
    for ref in refs:
        if ref["verdict"] in ("FLAG", "INERT", "MISSING", "RELOAD"):
            extra = (
                f" readers={ref.get('readers')} bypass={ref.get('bypass')}"
                if ref.get("readers") is not None
                else ""
            )
            print(
                f"{ref['verdict']:8} {ref['file']}:{ref['line']} {ref['call']}({ref['target']!r}){extra}"
            )

    if args.compare:
        base = json.loads(Path(args.compare).read_text())

        # Key by (file, target-name-leaf, call) with multiplicity; a retargeted
        # patch changes its dotted prefix but keeps its leaf name.
        def key(ref):
            return (ref["file"], ref["target"].split(".")[-1], ref["call"])

        base_worst = defaultdict(int)
        for ref in base["refs"]:
            base_worst[key(ref)] = max(
                base_worst[key(ref)], RANK.get(ref["verdict"], 0)
            )
        worse = [
            ref
            for ref in refs
            if RANK.get(ref["verdict"], 0) > base_worst.get(key(ref), 0)
        ]
        base_n = Counter(
            key(ref)
            for ref in base["refs"]
            if ref["kind"] in ("string", "object", "dict")
        )
        head_n = Counter(
            key(ref) for ref in refs if ref["kind"] in ("string", "object", "dict")
        )
        dropped = [k for k in base_n if head_n[k] < base_n[k]]
        print(
            f"\n# compare against {args.compare}: {len(worse)} reference(s) got worse, {len(dropped)} patch key(s) lost"
        )
        for ref in worse:
            print(
                f"WORSE    {ref['verdict']:6} {ref['file']}:{ref['line']} {ref['call']}({ref['target']!r}) bypass={ref.get('bypass')}"
            )
        for k in dropped:
            print(f"LOST     {k} base x{base_n[k]} head x{head_n[k]}")
        return 1 if worse or dropped else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

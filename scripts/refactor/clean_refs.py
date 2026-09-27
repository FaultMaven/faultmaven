#!/usr/bin/env python3
"""No-backward-compat checker and import codemod for a module decomposition.

PROVES: that every symbol a decomposed module used to define now has exactly
ONE import path anywhere it is referenced — the module that DEFINES it — and
that nothing reaches it through a stale path: the old module's own name, an
aliased import of it, a patch string built from it, a re-export, or a doc
mention.

WHY: a decomposition that keeps a facade "for now" (an ``__init__.py`` that
re-exports everything, or a caller nobody updated) looks correct — imports
resolve, tests pass — right up until the facade is deleted or a caller reads
a name a sibling submodule shadowed under the same import. CLAUDE.md's
Architecture section rules this out categorically for this codebase ("No
backward compatibility — the system is pre-user"): there is exactly one
canonical path per symbol, decided at merge time, not discovered later by a
NameError.

A decomposed module D now lives in a set of "home" modules: the package's
submodules (and, for a non-package split, D itself plus sibling modules).
Every symbol has ONE canonical import path: the module that DEFINES it
(top-level def / class / assignment). Anything that reaches a symbol through
another path is STALE:

  * `from D import X` / `from <pkg-of-D> import X` (absolute or relative)
    where X is neither defined in D's own file nor a submodule of D;
  * `import D as m` (or `from parent import leaf as m`) then `m.X`, or
    `patch.object(m, "X")` / `monkeypatch.setattr(m, "X", ...)`, same rule;
  * a string literal `"D.X..."` (patch targets, import_module) with X not
    defined in D and not a submodule;
  * inside a package `__init__.py`: any `from .sub import X` whose X is not
    used by `__init__.py` itself (a pure re-export), and any def/class unless
    --allow-init-code;
  * any `getLogger("<literal>")` in the home modules (use `__name__`).

Reported, not failed: exact logger-name comparisons in tests (`.name ==
"D..."`), and docs mentioning `<D path>.py` / `<pkg>/__init__.py`.

USAGE:
  clean_refs.py --repo W --old faultmaven/core/investigation/causal_graph.py \\
      [--homes faultmaven/modules/case/api/routes.py faultmaven/modules/case/api/title_generation.py] \\
      [--rewrite]     # codemod: rewrite stale `from D import ...` to the defining modules
      [--allow-init-code]
      [--scan faultmaven tests scripts]   # dirs (repo-relative) to scan; default shown

The old module's AST at --base (default origin/main) supplies where each
IMPORTED name came from, so `from D import json` is rewritten to an
`import`-from its true origin.

EXIT CODES: 0 no stale reference found; 1 at least one STALE-*/UNUSED-IMPORT/
REEXPORT/INIT-CODE/LOGGER-LIT finding (WARN-* findings never fail the run —
they are for a human to read, since a doc mention or a test's logger-name
literal is not itself a broken import path).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def dotted_of(repo: Path, f: Path) -> str:
    rel = f.relative_to(repo).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def top_defs(tree: ast.Module) -> set[str]:
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in tgts:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        out.add(x.id)
    return out


def resolve_from(mod_dotted: str, is_pkg_init: bool, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    parts = mod_dotted.split(".")
    base = parts if is_pkg_init else parts[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument(
        "--old",
        required=True,
        help="repo-relative path of the decomposed module, at --base",
    )
    ap.add_argument(
        "--base",
        default="origin/main",
        help="git revision holding the old module (for import provenance)",
    )
    ap.add_argument(
        "--homes",
        nargs="*",
        default=None,
        help="extra repo-relative home modules, for a non-package split",
    )
    ap.add_argument(
        "--rewrite",
        action="store_true",
        help="codemod: rewrite stale `from D import ...` to the defining modules",
    )
    ap.add_argument("--allow-init-code", action="store_true")
    ap.add_argument(
        "--scan",
        nargs="*",
        default=["faultmaven", "tests", "scripts"],
        help="repo-relative directories to scan for stale references",
    )
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    D = a.old[:-3].replace("/", ".")
    pkg_dir = repo / a.old[:-3]
    is_pkg = pkg_dir.is_dir()

    homes: dict[str, Path] = {}
    if is_pkg:
        for f in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" not in f.parts:
                homes[dotted_of(repo, f)] = f
    for h in a.homes or []:
        homes[dotted_of(repo, repo / h)] = repo / h
    if not is_pkg and (repo / a.old).exists():
        homes.setdefault(D, repo / a.old)
    submods = {d.split(".")[-1] for d in homes if d != D and d.startswith(D + ".")}

    # name -> defining module (among homes)
    defined_in: dict[str, str] = {}
    home_trees = {}
    for d, f in homes.items():
        t = ast.parse(f.read_text())
        home_trees[d] = t
        for n in top_defs(t):
            defined_in.setdefault(n, d)
    own_D = top_defs(home_trees[D]) if D in home_trees else set()
    # Names D itself binds AND uses (defined, or imported and read by D's own code):
    # a patch/attribute reference to one of these targets a namespace that really
    # reads it, so it is legitimate even when D is not the defining module.
    bound_used_D = set(own_D)
    if D in home_trees:
        tD = home_trees[D]
        loadsD = {
            x.id
            for x in ast.walk(tD)
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
        }
        for n in tD.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for al in n.names:
                    nm = (al.asname or al.name).split(".")[0]
                    if nm in loadsD:
                        bound_used_D.add(nm)

    def ref_target(name: str):
        """For patch strings / patch.object / attribute reads: stale only if D no longer binds-and-uses name."""
        if (
            name in bound_used_D
            or name in submods
            or (name.startswith("__") and name.endswith("__"))
        ):
            return None
        return canonical(name) or ("?", name)

    # origin of names the OLD module imported
    old_src = subprocess.run(
        ["git", "-C", str(repo), "show", f"{a.base}:{a.old}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    origin: dict[str, tuple[str, str]] = {}  # alias -> (module, original name)
    for n in ast.walk(ast.parse(old_src)):
        if isinstance(n, ast.ImportFrom):
            src_mod = resolve_from(D, False, n)
            for al in n.names:
                origin[al.asname or al.name] = (src_mod, al.name)
        elif isinstance(n, ast.Import):
            for al in n.names:
                if al.asname:
                    origin[al.asname] = (al.name, None)

    def canonical(name: str) -> tuple[str, str] | None:
        """(module, name-in-module) where `name` should be imported from, or None if fine via D."""
        if (
            name in submods
            or name in own_D
            or (name.startswith("__") and name.endswith("__"))
        ):
            return None
        if name in defined_in:
            return (defined_in[name], name)
        if name in origin:
            return origin[name]
        return ("?", name)

    stale: list[str] = []
    warns: list[str] = []
    rewrites: dict[Path, list[tuple[int, int, str]]] = defaultdict(list)

    files = []
    for s in a.scan:
        root = repo / s
        if root.exists():
            files += [f for f in root.rglob("*.py") if "__pycache__" not in f.parts]
    leaf = D.split(".")[-1]
    for f in files:
        src = f.read_text()
        if leaf not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = str(f.relative_to(repo))
        mod = dotted_of(repo, f)
        is_init = f.name == "__init__.py"
        lines = src.splitlines(keepends=True)
        aliases: dict[str, str] = {}  # local name -> D
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for al in n.names:
                    if al.name == D and al.asname:
                        aliases[al.asname] = D
            elif isinstance(n, ast.ImportFrom):
                src_mod = resolve_from(mod, is_init, n)
                for al in n.names:
                    if src_mod and f"{src_mod}.{al.name}" == D:
                        aliases[al.asname or al.name] = D
                if src_mod != D or (
                    f in homes.values() and dotted_of(repo, f) == D and is_pkg
                ):
                    continue
                bad = [(al, canonical(al.name)) for al in n.names if canonical(al.name)]
                if not bad:
                    continue
                for al, c in bad:
                    stale.append(
                        f"STALE-IMPORT {rel}:{n.lineno} from {D} import {al.name}  ->  {c[0]}"
                    )
                # codemod: split this ImportFrom by destination
                groups: dict[str, list[str]] = defaultdict(list)
                for al in n.names:
                    c = canonical(al.name)
                    dest, real = (D, al.name) if c is None else c
                    if dest == "?":
                        groups["__UNKNOWN__"].append(al.name)
                        continue
                    piece = (
                        real
                        if (al.asname or al.name) == real
                        else f"{real} as {al.asname or al.name}"
                    )
                    groups[dest].append(piece)
                if "__UNKNOWN__" in groups:
                    continue
                indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
                new = "".join(
                    f"{indent}from {dest} import {', '.join(sorted(set(p)))}\n"
                    for dest, p in groups.items()
                )
                rewrites[f].append((n.lineno, n.end_lineno, new))
        # attribute access through a module alias bound to D
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and aliases.get(n.value.id) == D
            ):
                if ref_target(n.attr):
                    stale.append(
                        f"STALE-ATTR   {rel}:{n.lineno} {n.value.id}.{n.attr}  ->  {ref_target(n.attr)[0]}"
                    )
            if (
                isinstance(n, ast.Call)
                and len(n.args) >= 2
                and isinstance(n.args[0], ast.Name)
                and aliases.get(n.args[0].id) == D
            ):
                if (
                    isinstance(n.args[1], ast.Constant)
                    and isinstance(n.args[1].value, str)
                    and ref_target(n.args[1].value)
                ):
                    stale.append(
                        f"STALE-PATCHOBJ {rel}:{n.lineno} ({n.args[0].id}, {n.args[1].value!r})  ->  {ref_target(n.args[1].value)[0]}"
                    )
            if (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and n.value.startswith(D + ".")
            ):
                nxt = n.value[len(D) + 1 :].split(".")[0]
                if ref_target(nxt):
                    stale.append(
                        f"STALE-STRING {rel}:{getattr(n, 'lineno', 0)} {n.value!r}  ->  {ref_target(nxt)[0]}"
                    )
            if isinstance(n, ast.Compare) and any(
                isinstance(c, ast.Constant)
                and isinstance(c.value, str)
                and c.value.startswith(D)
                for c in n.comparators
            ):
                if "name" in ast.unparse(n.left):
                    warns.append(
                        f"WARN-LOGNAME {rel}:{n.lineno} {ast.unparse(n)[:120]}"
                    )

    # __init__ hygiene and logger literals in homes
    for d, t in home_trees.items():
        f = homes[d]
        rel = str(f.relative_to(repo))
        # Any home module: an imported name the module never uses is a re-export
        # kept "for parity" -- dead under the no-compat rule. (A name used only in
        # a string annotation counts as used.)
        used_any = {
            x.id
            for x in ast.walk(t)
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
        }
        used_any |= {x.attr for x in ast.walk(t) if isinstance(x, ast.Attribute)}
        strs = " ".join(
            x.value
            for x in ast.walk(t)
            if isinstance(x, ast.Constant) and isinstance(x.value, str)
        )
        for n in t.body:
            stmts = [n]
            if isinstance(n, ast.If) and "TYPE_CHECKING" in ast.unparse(n.test):
                stmts = n.body
            for st in stmts:
                if isinstance(st, (ast.Import, ast.ImportFrom)) and not (
                    isinstance(st, ast.ImportFrom) and st.module == "__future__"
                ):
                    for al in st.names:
                        nm = (al.asname or al.name).split(".")[0]
                        if (
                            nm not in used_any
                            and not re.search(r"\b" + re.escape(nm) + r"\b", strs)
                            and nm not in submods
                        ):
                            stale.append(f"UNUSED-IMPORT {rel}:{st.lineno} {nm}")
        if f.name == "__init__.py":
            used = {
                x.id
                for x in ast.walk(t)
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
            }
            for n in t.body:
                if isinstance(n, ast.ImportFrom) and n.level == 1:
                    for al in n.names:
                        if (
                            al.asname or al.name
                        ) not in used and al.name not in submods:
                            stale.append(f"REEXPORT     {rel}:{n.lineno} {al.name}")
                if (
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and not a.allow_init_code
                ):
                    stale.append(f"INIT-CODE    {rel}:{n.lineno} {n.name}")
        for n in ast.walk(t):
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(
                ("getLogger", "get_logger")
            ):
                if n.args and isinstance(n.args[0], ast.Constant):
                    stale.append(
                        f"LOGGER-LIT   {rel}:{n.lineno} {ast.unparse(n)}  (use __name__)"
                    )

    # docs mentions (report)
    base_short = a.old[:-3].split("faultmaven/", 1)[-1]
    pats = (
        [base_short + ".py", base_short + "/__init__.py"]
        if is_pkg
        else [base_short + "/__init__.py", base_short + "/"]
    )
    doc_files = [p for p in (repo / "docs").rglob("*.md") if "archive" not in p.parts]
    doc_files += [
        p for p in (repo / ".claude").rglob("*.md") if "worktrees" not in p.parts
    ]
    for p in doc_files:
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            for pat in pats:
                if re.search(re.escape(pat) + r"(?![A-Za-z0-9_])", line):
                    warns.append(
                        f"WARN-DOC     {p.relative_to(repo)}:{i} mentions {pat}"
                    )

    # Dotted references in docs that name a moved symbol through the old path, in
    # full (`faultmaven.x.D.name`) or by leaf (`D-leaf.name`): under the
    # one-canonical-path rule they must name the defining module.
    leafname = D.split(".")[-1]
    dot_re = re.compile(
        r"(?<![\w/])(?:"
        + re.escape(D)
        + "|"
        + re.escape(leafname)
        + r")\.([A-Za-z_]\w*)"
    )
    for p in doc_files:
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            for mm in dot_re.finditer(line):
                nm = mm.group(1)
                if (
                    nm in ("py",)
                    or nm in submods
                    or nm in own_D
                    or (nm.startswith("__") and nm.endswith("__"))
                ):
                    continue
                c = canonical(nm)
                if c and c[0] != "?":
                    warns.append(
                        f"WARN-DOC-DOTTED {p.relative_to(repo)}:{i} {mm.group(0)}  ->  {c[0]}.{nm}"
                    )

    if a.rewrite:
        for f, edits in rewrites.items():
            lines = f.read_text().splitlines(keepends=True)
            for start, end, new in sorted(edits, reverse=True):
                lines[start - 1 : end] = [new]
            f.write_text("".join(lines))
        print(
            f"rewrote {sum(len(e) for e in rewrites.values())} import statement(s) in {len(rewrites)} file(s); now run: ruff check --fix <files>; black <files>"
        )

    for s in stale:
        print(s)
    for w in warns:
        print(w)
    kinds = defaultdict(int)
    for s in stale:
        kinds[s.split()[0]] += 1
    print(
        f"\n# clean_refs {D}: homes={len(homes)} stale={len(stale)} {dict(kinds)} warnings={len(warns)}"
    )
    print("RESULT:", "PASS" if not stale else "FAIL")
    return 0 if not stale else 1


if __name__ == "__main__":
    sys.exit(main())

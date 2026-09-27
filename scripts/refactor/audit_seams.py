#!/usr/bin/env python3
"""Find references an Extract Function / Extract Class refactor left stale or vacuous.

After members of an owner class move to module functions or collaborator
objects, four kinds of reference can survive and still RUN without error while
testing nothing -- or fail far from the cause:

  STALE-ATTR    ``x._moved(...)`` / ``x._moved`` where ``_moved`` no longer lives
                on the owner (and ``x`` is not the collaborator that now holds it)
  STALE-PATCH   ``patch.object(x, "_moved")`` / ``monkeypatch.setattr(x, "_moved", ..)``
                / ``patch("pkg.mod.Owner._moved")`` on the old home
  VACUOUS-SET   ``x._moved = stub`` -- silently creates an attribute nothing reads
  DEP-SWAP      ``x.dep = y`` / ``patch.object(x, "dep")`` on the OWNER for a
                dependency the owner no longer reads (a collaborator holds its own
                reference, so the swap never reaches the code under test)
  NEW-BUILT     ``Owner.__new__(Owner)`` in a file that also reaches a collaborator
                (informational: such an owner has no collaborators)
  OUTSIDE-READ  production code outside the owner package reading an owner
                attribute the refactor dropped (R5: it must be kept)
  AMBIGUOUS     ``x._moved`` where another class in the tree still defines a
                method named ``_moved`` (e.g. a sibling repository sharing the
                owner's method names): x may well be that class. Never
                rewritten; read it.
  HOLDER-FLAT   (--holder) ``engine.X`` for a field X of the shared dependency
                holder: the owner no longer has X, so a read raises and an
                assignment silently creates an attribute nothing reads.
                Rewritten to ``engine.deps.X`` when the receiver is named like an
                engine; any other receiver is reported for a human.

Reads the codemod report (extract_members.py --report) for the homes, public
names, collaborator attributes and dropped attributes. Receivers are matched by
name only, so a hit on an unrelated object that happens to share a private
member name is possible: every hit is a lead to read, and the tool errs toward
reporting.

USAGE: audit_seams.py --repo HEAD --spec spec.json --report codemod_report.json
                      [--dropped attr,attr] [--paths tests faultmaven] [--rewrite]

--rewrite fixes the mechanical cases in place and leaves the rest reported:
  x._fn(args)            -> _fn(x.<dep>..., args)   (+ import)   function members
  Owner._fn(args)        -> _fn(args)               (+ import)   static members, no deps
  x._m / x._m(args)      -> x.<attr>.<name>                       collaborator members
  patch.object(x, "_m")  -> patch.object(x.<attr>, "<name>")      collaborator members
VACUOUS-SET, DEP-SWAP, STALE-PATCH on a function member, and NEW-BUILT always
need a human: they change what the test sets up, not just a spelling.
EXIT: 0 clean, 1 findings.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument(
        "--dropped", default="", help="owner attributes removed by R5 (comma-separated)"
    )
    ap.add_argument("--paths", nargs="*", default=["tests", "faultmaven", "scripts"])
    ap.add_argument("--rewrite", action="store_true")
    ap.add_argument(
        "--holder",
        default="",
        help="attribute name of the shared dependency holder, e.g. deps",
    )
    ap.add_argument("--holder-fields", default="", help="comma-separated holder fields")
    ap.add_argument(
        "--holder-class",
        default="",
        help="module:Class of the holder, for __new__-built owners",
    )
    a = ap.parse_args()
    repo = Path(a.repo)
    spec = json.loads(Path(a.spec).read_text())
    rep = json.loads(Path(a.report).read_text())
    owner = spec["class"]
    homes = rep["homes"]  # member -> module or None
    collab_attr = rep.get("collab_attr", {})  # member -> attr
    rename = rep.get("rename", {})
    moved = {m for m, h in homes.items() if h is not None}
    collab_attrs = set(collab_attr.values())
    collab_deps = {d for ds in rep.get("collab_deps", {}).values() for d in ds}
    dropped = {x for x in a.dropped.split(",") if x}
    # the owner still holds these but no owner code reads them: a swap on the owner reaches nothing
    swap_attrs = dropped | set(rep.get("unread", []))
    owner_pkg = str(Path(spec["source"]).parent)
    group_modules = {g["module"] for g in spec["groups"]}

    findings = []
    holder = a.holder
    hfields = {x for x in a.holder_fields.split(",") if x}
    engine_like = re.compile(r"(^|_)(milestone_)?eng(ine)?$", re.I)
    # method names still defined by OTHER classes: a reference to one of these
    # may be on that class, so it is never rewritten and is reported AMBIGUOUS.
    elsewhere: set[str] = set()
    for root_ in a.paths:
        for f_ in (repo / root_).rglob("*.py"):
            try:
                t_ = ast.parse(f_.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for c_ in ast.walk(t_):
                if isinstance(c_, ast.ClassDef) and c_.name != owner:
                    for m_ in c_.body:
                        if (
                            isinstance(m_, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and m_.name in moved
                        ):
                            elsewhere.add(m_.name)

    def ambiguous(name, recv) -> bool:
        return name in elsewhere and ast.unparse(recv).split(".")[-1] != owner

    fn_deps = rep.get("deps", {})
    collab_class = rep.get("collab_class", {})

    def dotted(path: str) -> str:
        return ".".join(Path(path).with_suffix("").parts)

    _defines_cache: dict[str, set[str]] = {}

    def module_defines(mod: str | None) -> set[str]:
        """Top-level names a first-party module defines (empty if not resolvable)."""
        if not mod:
            return set()
        if mod not in _defines_cache:
            names: set[str] = set()
            base = repo / Path(*mod.split("."))
            for cand in (base.with_suffix(".py"), base / "__init__.py"):
                if cand.exists():
                    try:
                        for node in ast.parse(cand.read_text()).body:
                            if isinstance(
                                node,
                                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                            ):
                                names.add(node.name)
                    except SyntaxError:
                        pass
                    break
            _defines_cache[mod] = names
        return _defines_cache[mod]

    def emit(kind, path, node, msg):
        findings.append(f"{kind:<12} {path}:{getattr(node, 'lineno', 0)}  {msg}")

    def receiver_is_collab(recv: ast.AST) -> bool:
        u = ast.unparse(recv)
        return any(u == c or u.endswith("." + c) for c in collab_attrs)

    for root in a.paths:
        for f in sorted((repo / root).rglob("*.py")):
            rel = str(f.relative_to(repo))
            if rel == spec["source"] or rel in group_modules:
                continue
            try:
                src = f.read_text()
                t = ast.parse(src)
            except (SyntaxError, UnicodeDecodeError):
                continue
            is_test = rel.startswith("tests/")
            lines = src.splitlines(keepends=True)
            starts = [0]
            for ln in lines:
                starts.append(starts[-1] + len(ln))

            def off(lineno, col, _l=lines, _s=starts):
                return _s[lineno - 1] + len(_l[lineno - 1].encode()[:col].decode())

            parents = {c: p for p in ast.walk(t) for c in ast.iter_child_nodes(p)}
            # names bound to MODULES in this file: `from pkg import mod as m`, `import pkg.mod as m`
            module_alias: dict[str, str] = {}
            for imp in ast.walk(t):
                if isinstance(imp, ast.ImportFrom) and imp.module:
                    for al in imp.names:
                        module_alias[al.asname or al.name] = f"{imp.module}.{al.name}"
                elif isinstance(imp, ast.Import):
                    for al in imp.names:
                        if al.asname:
                            module_alias[al.asname] = al.name
            edits, imports = [], set()
            reaches_collab = any(
                f".{c}." in src or f".{c})" in src for c in collab_attrs
            )
            for n in ast.walk(t):
                # Owner.__new__(Owner)
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "__new__"
                    and ast.unparse(n.func.value).split(".")[-1] == owner
                    and reaches_collab
                ):
                    emit(
                        "NEW-BUILT",
                        rel,
                        n,
                        f"{owner}.__new__ in a file that reaches a collaborator",
                    )
                if holder and isinstance(n, ast.Attribute) and n.attr in hfields:
                    recv = ast.unparse(n.value)
                    last = recv.split(".")[-1]
                    if last != holder and not (
                        isinstance(n.value, ast.Name)
                        and n.value.id == "self"
                        and not is_test
                    ):
                        if engine_like.search(last):
                            if a.rewrite:
                                e0 = off(n.value.end_lineno, n.value.end_col_offset)
                                edits.append((e0, e0, f".{holder}"))
                                continue
                            emit(
                                "HOLDER-FLAT",
                                rel,
                                n,
                                f"{ast.unparse(n)} -> {recv}.{holder}.{n.attr}",
                            )
                        elif is_test and isinstance(n.ctx, ast.Store) and owner in src:
                            emit(
                                "HOLDER-FLAT",
                                rel,
                                n,
                                f"{ast.unparse(n)} = ...: if {recv} is an engine this is vacuous; use {recv}.{holder}.{n.attr}",
                            )
                if isinstance(n, ast.Attribute) and n.attr in moved:
                    if receiver_is_collab(n.value):
                        continue
                    if isinstance(n.value, ast.Name) and n.attr in module_defines(
                        module_alias.get(n.value.id)
                    ):
                        continue  # mod._name where mod really defines _name (its new home, or a sibling's)
                    if ambiguous(n.attr, n.value):
                        emit(
                            "AMBIGUOUS",
                            rel,
                            n,
                            f"{ast.unparse(n)}: another class still defines {n.attr}; is the receiver a {owner}?",
                        )
                        continue
                    kind = (
                        "VACUOUS-SET" if isinstance(n.ctx, ast.Store) else "STALE-ATTR"
                    )
                    if (
                        a.rewrite
                        and kind == "VACUOUS-SET"
                        and n.attr in collab_attr
                        and ast.unparse(n.value).split(".")[-1] != owner
                    ):
                        # a stub set on the shared collaborator instance reaches every caller
                        sp = (
                            off(n.lineno, n.col_offset),
                            off(n.end_lineno, n.end_col_offset),
                        )
                        edits.append(
                            (
                                *sp,
                                f"{ast.unparse(n.value)}.{collab_attr[n.attr]}.{rename.get(n.attr, n.attr)}",
                            )
                        )
                        continue
                    if a.rewrite and kind == "STALE-ATTR":
                        par = parents.get(n)
                        is_call = isinstance(par, ast.Call) and par.func is n
                        recv = ast.unparse(n.value)
                        sp = (
                            off(n.lineno, n.col_offset),
                            off(n.end_lineno, n.end_col_offset),
                        )
                        if n.attr in collab_attr:
                            new = rename.get(n.attr, n.attr)
                            if recv.split(".")[-1] == owner:
                                edits.append((*sp, f"{collab_class[n.attr]}.{new}"))
                                imports.add((homes[n.attr], collab_class[n.attr]))
                            else:
                                edits.append(
                                    (*sp, f"{recv}.{collab_attr[n.attr]}.{new}")
                                )
                            continue
                        deps = fn_deps.get(n.attr, [])
                        if is_call and (not deps or recv.split(".")[-1] != owner):
                            edits.append((*sp, n.attr))
                            if deps:
                                ins = ", ".join(f"{recv}.{d}" for d in deps) + (
                                    ", " if (par.args or par.keywords) else ""
                                )
                                edits.append((sp[1] + 1, sp[1] + 1, ins))
                            imports.add((homes[n.attr], n.attr))
                            continue
                        if not is_call and not deps:
                            edits.append((*sp, n.attr))
                            imports.add((homes[n.attr], n.attr))
                            continue
                    emit(
                        kind,
                        rel,
                        n,
                        f"{ast.unparse(n)} -> now {homes[n.attr]}"
                        + (
                            f" as .{collab_attr[n.attr]}.{rename.get(n.attr, n.attr)}"
                            if n.attr in collab_attr
                            else ""
                        ),
                    )
                if isinstance(n, ast.Call):
                    fn = ast.unparse(n.func)
                    if (
                        fn.endswith("patch.object")
                        or fn.endswith("monkeypatch.setattr")
                        or fn == "setattr"
                    ):
                        if (
                            len(n.args) >= 2
                            and isinstance(n.args[1], ast.Constant)
                            and isinstance(n.args[1].value, str)
                        ):
                            name = n.args[1].value
                            recv = n.args[0]
                            if (
                                holder
                                and name in hfields
                                and engine_like.search(ast.unparse(recv).split(".")[-1])
                            ):
                                if a.rewrite:
                                    e0 = off(recv.end_lineno, recv.end_col_offset)
                                    edits.append((e0, e0, f".{holder}"))
                                else:
                                    emit(
                                        "HOLDER-FLAT",
                                        rel,
                                        n,
                                        f"{fn}({ast.unparse(recv)}, {name!r}) -> {ast.unparse(recv)}.{holder}",
                                    )
                            if name in moved and ambiguous(name, recv):
                                emit(
                                    "AMBIGUOUS",
                                    rel,
                                    n,
                                    f"{fn}({ast.unparse(recv)}, {name!r}): another class still defines {name}",
                                )
                            elif (
                                name in moved
                                and not receiver_is_collab(recv)
                                and a.rewrite
                                and name in collab_attr
                            ):
                                ru = ast.unparse(recv)
                                new = rename.get(name, name)
                                s0 = off(recv.lineno, recv.col_offset)
                                e0 = off(recv.end_lineno, recv.end_col_offset)
                                if ru.split(".")[-1] == owner:
                                    edits.append((s0, e0, collab_class[name]))
                                    imports.add((homes[name], collab_class[name]))
                                else:
                                    edits.append((s0, e0, f"{ru}.{collab_attr[name]}"))
                                a1 = n.args[1]
                                edits.append(
                                    (
                                        off(a1.lineno, a1.col_offset),
                                        off(a1.end_lineno, a1.end_col_offset),
                                        repr(new).replace("'", '"'),
                                    )
                                )
                            elif name in moved and not receiver_is_collab(recv):
                                emit(
                                    "STALE-PATCH",
                                    rel,
                                    n,
                                    f"{fn}({ast.unparse(recv)}, {name!r}) -> now {homes[name]}",
                                )
                            elif (
                                name in swap_attrs
                                and not receiver_is_collab(recv)
                                and is_test
                            ):
                                emit(
                                    "DEP-SWAP",
                                    rel,
                                    n,
                                    f"{fn}({ast.unparse(recv)}, {name!r}): the owner no longer reads {name}",
                                )
                    if fn.endswith("patch") or fn.endswith("mock.patch"):
                        if (
                            n.args
                            and isinstance(n.args[0], ast.Constant)
                            and isinstance(n.args[0].value, str)
                        ):
                            target = n.args[0].value
                            last = target.rsplit(".", 1)[-1]
                            if last in moved and f".{owner}." in target:
                                emit(
                                    "STALE-PATCH",
                                    rel,
                                    n,
                                    f"patch({target!r}) -> now {homes[last]}",
                                )
                if (
                    isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                    and is_test
                ):
                    tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for tg in tgts:
                        if (
                            isinstance(tg, ast.Attribute)
                            and tg.attr in swap_attrs
                            and not receiver_is_collab(tg.value)
                        ):
                            if isinstance(tg.value, ast.Name) and tg.value.id == "self":
                                continue
                            emit(
                                "DEP-SWAP",
                                rel,
                                tg,
                                f"{ast.unparse(tg)} = ...: the owner no longer reads {tg.attr}",
                            )
            if a.rewrite and holder and a.holder_class and f"__new__({owner})" in src:
                hmod, hcls = a.holder_class.split(":")
                for n in ast.walk(t):
                    if (
                        isinstance(n, ast.Assign)
                        and isinstance(n.value, ast.Call)
                        and isinstance(n.value.func, ast.Attribute)
                        and n.value.func.attr == "__new__"
                        and ast.unparse(n.value.func.value).split(".")[-1] == owner
                        and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                    ):
                        var = n.targets[0].id
                        if f"{var}.{holder} = " in src:
                            continue
                        ind = " " * n.col_offset
                        pos = starts[n.end_lineno]
                        edits.append((pos, pos, f"{ind}{var}.{holder} = {hcls}()\n"))
                        imports.add((hmod.replace(".", "/") + ".py", hcls))
            if a.rewrite and edits:
                out = src
                for s0, e0, r in sorted(edits, reverse=True):
                    out = out[:s0] + r + out[e0:]
                if imports:
                    tt = ast.parse(src)
                    last = max(
                        (
                            x.end_lineno
                            for x in tt.body
                            if isinstance(x, (ast.Import, ast.ImportFrom))
                        ),
                        default=0,
                    )
                    # insert after the last top-level import of the ORIGINAL text; edits never add lines
                    ol = out.splitlines(keepends=True)
                    add = "".join(
                        f"from {dotted(m)} import {nm}\n" for m, nm in sorted(imports)
                    )
                    out = "".join(ol[:last]) + add + "".join(ol[last:])
                f.write_text(out)
                print(f"rewrote {rel}: {len(edits)} edit(s), {len(imports)} import(s)")
            if not is_test and not rel.startswith(owner_pkg):
                for n in ast.walk(t):
                    if (
                        isinstance(n, ast.Attribute)
                        and n.attr in dropped
                        and isinstance(n.ctx, ast.Load)
                    ):
                        emit(
                            "OUTSIDE-READ",
                            rel,
                            n,
                            f"{ast.unparse(n)}: production reads an attribute the owner dropped",
                        )
    for f in findings:
        print(f)
    kinds = {}
    for f in findings:
        k = f.split()[0]
        kinds[k] = kinds.get(k, 0) + 1
    print(f"\n{len(findings)} finding(s): {kinds}" if findings else "RESULT: CLEAN")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

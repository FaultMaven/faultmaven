#!/usr/bin/env python3
"""Prove an Extract Function / Extract Class refactor preserved every method body.

PROVES: every method of the base class appears exactly once at head (still on
the owner, as a module-level function, or as a method of a collaborator
class), and each one is AST-identical to its base version once the rewrites an
extraction needs are canonicalised on BOTH sides:

  * member references  ``self._m(...)`` / ``Owner._m`` (base) and
    ``_m(<deps>, ...)`` / ``self.<attr>.m(...)`` / ``<attr>.m`` / ``Class.m``
    (head) all become ``CALL<_m>``;
  * dependency reads   ``self.dep`` (base) and the leading parameter ``dep``
    of a function or ``self.dep`` of a collaborator (head) become ``DEP<dep>``;
  * constants          ``self.C`` / ``cls.C`` / ``Owner.C`` (base) and
    ``Class.C`` / ``self.C`` / hoisted ``C`` (head) become ``CONST<C>``;
  * docstrings are compared after ``inspect.cleandoc`` (dedent re-indents them).

It also checks the dependency arguments passed at every rewritten call site are
exactly the callee's dependency parameters, that a collaborator method is
public iff something outside the collaborator calls it (computed from the
BASE call graph), that the owner ``__init__`` builds each collaborator with
exactly its constructor parameters, that owned state is initialised with the
base initialiser, and that moved class constants and module-level statements
are unchanged and defined once.

Independent of extract_members.py by design: it shares no code with the
codemod and reads only the spec (the plan) and the two trees.

USAGE: verify_extract.py --base BASE_WORKTREE --head HEAD_WORKTREE --spec spec.json
EXIT: 0 PASS, 1 FAIL, 2 usage.
"""

from __future__ import annotations

import argparse
import ast
import copy
import inspect
import json
import sys
from pathlib import Path

FAIL: list[str] = []


def fail(msg):
    FAIL.append(msg)


def pname(attr: str) -> str:
    """Parameter name for an injected attribute: leading underscores dropped."""
    return attr.lstrip("_")


def parse(p: Path):
    return ast.parse(p.read_text())


def find_class(tree, name):
    return next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name), None
    )


def funcs_of(node):
    return {
        n.name: n
        for n in node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def consts_of(cls):
    out = {}
    for n in cls.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out[n.target.id] = n
    return out


def first_param(fn):
    decos = [ast.unparse(d) for d in fn.decorator_list]
    if "staticmethod" in decos:
        return None
    a = fn.args.posonlyargs + fn.args.args
    return a[0].arg if a else None


def clean_docstrings(tree):
    for n in ast.walk(tree):
        if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)
        ):
            b = n.body
            if (
                b
                and isinstance(b[0], ast.Expr)
                and isinstance(b[0].value, ast.Constant)
                and isinstance(b[0].value.value, str)
            ):
                b[0].value.value = inspect.cleandoc(b[0].value.value)
    return tree


class Ctx:
    """Everything the canonicaliser needs, derived from the spec and the BASE tree."""

    def __init__(self, spec, base_cls, base_members, base_consts):
        self.owner = spec["class"]
        self.members = set(base_members)
        self.consts = set(base_consts)
        self.home = {m: None for m in base_members}
        self.group_of = {}
        for g in spec["groups"]:
            for m in g["members"]:
                self.home[m] = g
        self.collab_by_attr = {
            g["attr"]: g for g in spec["groups"] if g["kind"] == "collaborator"
        }
        self.collab_by_class = {
            g["class"]: g for g in spec["groups"] if g["kind"] == "collaborator"
        }
        self.hoisted = {c for g in spec["groups"] for c in g.get("hoist_constants", [])}
        self.fn_members = {
            m for m, g in self.home.items() if g and g["kind"] == "functions"
        }
        # expected public names, from the BASE call graph
        callers = {m: set() for m in base_members}
        for name, fn in base_members.items():
            fp = first_param(fn)
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.attr in callers
                ):
                    if n.value.id == self.owner or (fp and n.value.id == fp):
                        callers[n.attr].add(name)
        self.expected_name = {}
        for m, g in self.home.items():
            if g and g["kind"] == "collaborator":
                external = any(self.home[c] is not g for c in callers[m])
                self.expected_name[m] = (
                    m.lstrip("_")
                    if (external and m.startswith("_") and not m.startswith("__"))
                    else m
                )
        self.name_to_member = (
            {}
        )  # collaborator (class/attr, public name) -> base member
        for m, new in self.expected_name.items():
            g = self.home[m]
            self.name_to_member[(g["class"], new)] = m
        self.fn_deps: dict[str, list[str]] = {}
        self.properties = {
            n
            for n, fn in base_members.items()
            if any(
                ast.unparse(d)
                in ("property", "cached_property", "functools.cached_property")
                for d in fn.decorator_list
            )
        }
        h = spec.get("holder") or {}
        self.holder = h.get("attr")
        self.holder_fields = set(h.get("fields", []))
        self.flat_base = bool(h.get("flat_base"))
        # parameter name -> the expression an owner/collaborator passes for it
        self.param_expr: dict[str, str] = {a: f"self.{a}" for a in self.collab_by_attr}

    def base_dep(self, n, fp):
        """If BASE node ``n`` reads a dependency through ``fp``, return its parameter name."""
        if not (isinstance(n, ast.Attribute) and fp):
            return None
        v = n.value
        if (
            self.holder
            and isinstance(v, ast.Attribute)
            and isinstance(v.value, ast.Name)
            and v.value.id == fp
            and v.attr == self.holder
        ):
            self.param_expr.setdefault(n.attr, f"self.{self.holder}.{n.attr}")
            return n.attr
        if isinstance(v, ast.Name) and v.id == fp:
            a = n.attr
            if a in self.consts or (a in self.members and a not in self.properties):
                return None
            if self.flat_base and a in self.holder_fields:
                self.param_expr.setdefault(a, f"self.{self.holder}.{a}")
                return a
            self.param_expr.setdefault(pname(a), f"self.{a}")
            return pname(a)
        return None


def canon_base(fn, ctx: Ctx, base_deps: set[str]):
    """Canonicalise a BASE method body."""
    fn = copy.deepcopy(fn)
    fp = first_param(fn)

    class T(ast.NodeTransformer):
        def visit_Attribute(self, n):
            d = ctx.base_dep(n, fp)
            if d is not None:
                return ast.Name(id=f"DEP__{d}", ctx=n.ctx)
            self.generic_visit(n)
            if isinstance(n.value, ast.Name) and (
                n.value.id == ctx.owner or (fp and n.value.id == fp)
            ):
                if n.attr in ctx.members:
                    return ast.Name(id=f"CALL__{n.attr}", ctx=ast.Load())
                if n.attr in ctx.consts:
                    return ast.Name(id=f"CONST__{n.attr}", ctx=ast.Load())
            return n

    fn = T().visit(fn)
    return fn


def canon_head(fn, ctx: Ctx, kind: str, group, errs: list[str], label: str):
    """Canonicalise a HEAD function/method. kind in {'owner','function','collab'}."""
    fn = copy.deepcopy(fn)
    fp = first_param(fn) if kind != "function" else None
    dep_params: set[str] = set()
    if kind == "function":
        dep_params = set(ctx.fn_deps.get(label, []))

    def expect_args(callee, args, ctx_kind):
        deps = ctx.fn_deps.get(callee, [])
        got = args[: len(deps)]
        want = []
        for d in deps:
            if ctx_kind == "function":
                want.append(d)
            else:
                want.append(ctx.param_expr.get(d, f"<no base read for {d}>"))
        gotu = [ast.unparse(a) for a in got]
        if gotu != want:
            errs.append(
                f"{label}: call to {callee} passes {gotu}, expected dependency args {want}"
            )
        return args[len(deps) :]

    class T(ast.NodeTransformer):
        def visit_Call(self, n):
            # function-member calls: strip dependency args, THEN canonicalise
            if isinstance(n.func, ast.Name) and n.func.id in ctx.fn_members:
                callee = n.func.id
                n.args = expect_args(
                    callee, n.args, kind
                )  # check RAW dependency args first
                n.args = [self.visit(a) for a in n.args]
                n.keywords = [self.visit(k) for k in n.keywords]
                n.func = ast.Name(id=f"CALL__{callee}", ctx=ast.Load())
                return n
            self.generic_visit(n)
            return n

        def visit_Name(self, n):
            if kind == "function" and n.id in dep_params:
                return ast.Name(id=f"DEP__{n.id}", ctx=n.ctx)
            if n.id in ctx.hoisted:
                return ast.Name(id=f"CONST__{n.id}", ctx=n.ctx)
            if n.id in ctx.fn_members:
                return ast.Name(id=f"CALL__{n.id}", ctx=n.ctx)
            return n

        def visit_Attribute(self, n):
            v = n.value
            # self.<holder>.X  -> DEP__X (owner and collaborators read the shared holder)
            if (
                ctx.holder
                and fp
                and isinstance(v, ast.Attribute)
                and isinstance(v.value, ast.Name)
                and v.value.id == fp
                and v.attr == ctx.holder
                and kind in ("owner", "collab")
            ):
                return ast.Name(id=f"DEP__{n.attr}", ctx=n.ctx)
            # self.<attr>.m / <attr>.m (collaborator member via its owner attribute)
            if (
                isinstance(v, ast.Attribute)
                and isinstance(v.value, ast.Name)
                and v.value.id == "self"
                and v.attr in ctx.collab_by_attr
            ):
                g = ctx.collab_by_attr[v.attr]
                m = ctx.name_to_member.get((g["class"], n.attr))
                if m:
                    return ast.Name(id=f"CALL__{m}", ctx=ast.Load())
            if (
                kind == "function"
                and isinstance(v, ast.Name)
                and v.id in ctx.collab_by_attr
                and v.id in dep_params
            ):
                g = ctx.collab_by_attr[v.id]
                m = ctx.name_to_member.get((g["class"], n.attr))
                if m:
                    return ast.Name(id=f"CALL__{m}", ctx=ast.Load())
            if isinstance(v, ast.Name) and v.id in ctx.collab_by_class:
                g = ctx.collab_by_class[v.id]
                m = ctx.name_to_member.get((g["class"], n.attr))
                if m:
                    return ast.Name(id=f"CALL__{m}", ctx=ast.Load())
                if n.attr in ctx.consts:
                    return ast.Name(id=f"CONST__{n.attr}", ctx=ast.Load())
            self.generic_visit(n)
            if isinstance(n.value, ast.Name) and fp and n.value.id == fp:
                if kind == "collab":
                    m = ctx.name_to_member.get((group["class"], n.attr))
                    if m:
                        return ast.Name(id=f"CALL__{m}", ctx=ast.Load())
                    if n.attr in ctx.consts:
                        return ast.Name(id=f"CONST__{n.attr}", ctx=ast.Load())
                    return ast.Name(id=f"DEP__{pname(n.attr)}", ctx=n.ctx)
                if kind == "owner":
                    if n.attr in ctx.properties:
                        return ast.Name(id=f"DEP__{pname(n.attr)}", ctx=n.ctx)
                    if n.attr in ctx.members and ctx.home.get(n.attr) is None:
                        return ast.Name(id=f"CALL__{n.attr}", ctx=ast.Load())
                    if n.attr in ctx.consts:
                        return ast.Name(id=f"CONST__{n.attr}", ctx=ast.Load())
                    if n.attr in ctx.collab_by_attr:
                        return n
                    return ast.Name(id=f"DEP__{pname(n.attr)}", ctx=n.ctx)
            if (
                isinstance(n.value, ast.Name)
                and n.value.id == ctx.owner
                and n.attr in ctx.consts
            ):
                return ast.Name(id=f"CONST__{n.attr}", ctx=ast.Load())
            # Owner._m for a member that stays on the owner (static helpers call siblings this way)
            if (
                isinstance(n.value, ast.Name)
                and n.value.id == ctx.owner
                and n.attr in ctx.members
                and ctx.home.get(n.attr) is None
            ):
                return ast.Name(id=f"CALL__{n.attr}", ctx=ast.Load())
            return n

    return T().visit(fn)


def strip_self_and_decos(fn, is_function: bool, deps: list[str]):
    fn = copy.deepcopy(fn)
    if is_function:
        fn.decorator_list = [
            d
            for d in fn.decorator_list
            if ast.unparse(d) not in ("staticmethod", "classmethod")
        ]
    return fn


def args_sig(fn, drop_first: bool, drop_leading: int = 0):
    a = copy.deepcopy(fn.args)
    pos = a.posonlyargs + a.args
    if drop_first:
        pos = pos[1:]
    pos = pos[drop_leading:]
    for x in pos + a.kwonlyargs:
        x.annotation = x.annotation  # keep annotations
    return ast.dump(
        ast.arguments(
            posonlyargs=[],
            args=pos,
            vararg=a.vararg,
            kwonlyargs=a.kwonlyargs,
            kw_defaults=a.kw_defaults,
            kwarg=a.kwarg,
            defaults=a.defaults,
        )
    )


def body_dump(fn):
    return ast.dump(ast.Module(body=fn.body, type_ignores=[]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--spec", required=True)
    a = ap.parse_args()
    base, head = Path(a.base), Path(a.head)
    spec = json.loads(Path(a.spec).read_text())
    src = spec["source"]
    btree = clean_docstrings(parse(base / src))
    bcls = find_class(btree, spec["class"])
    bmem = funcs_of(bcls)
    bconst = consts_of(bcls)
    ctx = Ctx(spec, bcls, bmem, bconst)
    for (
        _fn
    ) in (
        bmem.values()
    ):  # learn every dependency's expression before any call site is checked
        _fp = first_param(_fn)
        for _n in ast.walk(_fn):
            ctx.base_dep(_n, _fp)

    htree = clean_docstrings(parse(head / src))
    hcls = find_class(htree, spec["class"])
    if hcls is None:
        print(f"FAIL: owner class {spec['class']} missing at head")
        return 1
    hmem = funcs_of(hcls)
    mods = {}
    for g in spec["groups"]:
        p = head / g["module"]
        if not p.exists():
            fail(f"{g['module']}: missing at head")
            continue
        mods[g["module"]] = clean_docstrings(parse(p))

    # --- dependency params of function members (from HEAD signatures) --------
    for m, g in ctx.home.items():
        if g and g["kind"] == "functions" and g["module"] in mods:
            fn = funcs_of(mods[g["module"]]).get(m)
            if fn is None:
                continue
            base_pos = bmem[m].args.posonlyargs + bmem[m].args.args
            fp = first_param(bmem[m])
            nbase = len(base_pos) - (1 if fp else 0)
            head_pos = fn.args.posonlyargs + fn.args.args
            k = len(head_pos) - nbase
            ctx.fn_deps[m] = [x.arg for x in head_pos[:k]] if k > 0 else []

    # every dependency parameter must be justified by a base read or a callee's need
    for m, deps in ctx.fn_deps.items():
        fp = first_param(bmem[m])
        direct = set()
        inner = set()
        for n in ast.walk(bmem[m]):
            if id(n) in inner:
                continue
            d = ctx.base_dep(n, fp)
            if d is not None:
                direct.add(d)
                if isinstance(n.value, ast.Attribute):
                    inner.add(id(n.value))
        callees = {
            n.attr
            for n in ast.walk(bmem[m])
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.attr in ctx.members
            and (n.value.id == ctx.owner or (fp and n.value.id == fp))
        }
        needed = set(direct)
        for c in callees:
            if c in ctx.fn_members:
                needed |= set(ctx.fn_deps.get(c, []))
            elif ctx.home.get(c) and ctx.home[c]["kind"] == "collaborator":
                needed.add(ctx.home[c]["attr"])
        if set(deps) != needed:
            fail(f"{m}: dependency params {deps} != needed {sorted(needed)}")
        # shadowing: a dependency parameter the base body already used as a plain name
        base_names = {n.id for n in ast.walk(bmem[m]) if isinstance(n, ast.Name)}
        base_names |= {
            x.arg
            for x in bmem[m].args.posonlyargs
            + bmem[m].args.args
            + bmem[m].args.kwonlyargs
        }
        for d in deps:
            if d in base_names:
                fail(
                    f"{m}: dependency parameter {d!r} collides with a name the base body already uses"
                )

    seen = {}
    # --- owner ---------------------------------------------------------------
    for m, g in ctx.home.items():
        if g is None:
            if m not in hmem:
                fail(f"{m}: should stay on {spec['class']} but is missing")
                continue
            seen[m] = "owner"
            if m == "__init__":
                continue
            b = canon_base(bmem[m], ctx, set())
            h = canon_head(hmem[m], ctx, "owner", None, FAIL, m)
            if args_sig(b, False) != args_sig(h, False) or [
                ast.dump(d) for d in b.decorator_list
            ] != [ast.dump(d) for d in h.decorator_list]:
                fail(f"{m} (owner): signature or decorators changed")
            if body_dump(b) != body_dump(h):
                fail(f"{m} (owner): body differs beyond permitted rewrites")
    extra = set(hmem) - {m for m, g in ctx.home.items() if g is None}
    if extra:
        fail(f"owner gained methods not in base: {sorted(extra)}")

    # --- moved members --------------------------------------------------------
    for g in spec["groups"]:
        tree = mods.get(g["module"])
        if tree is None:
            continue
        if g["kind"] == "functions":
            hf = funcs_of(tree)
            for m in g["members"]:
                fn = hf.get(m)
                if fn is None:
                    fail(f"{m}: not defined as a function in {g['module']}")
                    continue
                seen[m] = g["module"]
                bfn = bmem[m]
                fp = first_param(bfn)
                b = canon_base(bfn, ctx, set())
                h = canon_head(fn, ctx, "function", g, FAIL, m)
                bdec = [
                    ast.dump(d)
                    for d in b.decorator_list
                    if ast.unparse(d) not in ("staticmethod", "classmethod")
                ]
                if bdec != [ast.dump(d) for d in h.decorator_list]:
                    fail(f"{m}: decorators changed")
                if args_sig(b, drop_first=bool(fp)) != args_sig(
                    h, False, drop_leading=len(ctx.fn_deps.get(m, []))
                ):
                    fail(
                        f"{m}: signature changed beyond self removal and leading dependency params"
                    )
                if body_dump(b) != body_dump(h):
                    fail(f"{m} ({g['module']}): body differs beyond permitted rewrites")
                if isinstance(bfn, ast.AsyncFunctionDef) != isinstance(
                    fn, ast.AsyncFunctionDef
                ):
                    fail(f"{m}: async-ness changed")
        else:
            kc = find_class(tree, g["class"])
            if kc is None:
                fail(f"{g['class']}: missing from {g['module']}")
                continue
            km = funcs_of(kc)
            for m in g["members"]:
                new = ctx.expected_name[m]
                fn = km.get(new)
                if fn is None:
                    fail(
                        f"{m}: expected as {g['class']}.{new} (public iff called from outside the collaborator)"
                    )
                    continue
                seen[m] = f"{g['module']}:{g['class']}"
                b = canon_base(bmem[m], ctx, set())
                h = canon_head(fn, ctx, "collab", g, FAIL, m)
                if args_sig(b, False) != args_sig(h, False) or [
                    ast.dump(d) for d in b.decorator_list
                ] != [ast.dump(d) for d in h.decorator_list]:
                    fail(f"{m} ({g['class']}): signature or decorators changed")
                if body_dump(b) != body_dump(h):
                    fail(f"{m} ({g['class']}): body differs beyond permitted rewrites")
                if isinstance(bmem[m], ast.AsyncFunctionDef) != isinstance(
                    fn, ast.AsyncFunctionDef
                ):
                    fail(f"{m}: async-ness changed")
            expected_methods = {ctx.expected_name[m] for m in g["members"]} | {
                "__init__"
            }
            if set(km) != expected_methods:
                fail(
                    f"{g['class']}: methods {sorted(set(km) - expected_methods)} extra / {sorted(expected_methods - set(km))} missing"
                )
            kconst = consts_of(kc)
            for c in g.get("class_constants", []):
                if c not in kconst or ast.dump(kconst[c]) != ast.dump(bconst[c]):
                    fail(f"{g['class']}.{c}: constant missing or changed")
            # constructor: kw-only params, each assigned verbatim; owned state from base initialiser
            init = km.get("__init__")
            if init is not None:
                params = [x.arg for x in init.args.kwonlyargs]
                if init.args.args[1:] or init.args.posonlyargs:
                    fail(f"{g['class']}.__init__: dependencies must be keyword-only")
                binit = bmem.get("__init__")
                base_state = {}
                if binit is not None:
                    for st in binit.body:
                        if isinstance(st, (ast.Assign, ast.AnnAssign)):
                            tg = (
                                st.targets[0]
                                if isinstance(st, ast.Assign)
                                else st.target
                            )
                            if isinstance(tg, ast.Attribute):
                                base_state[tg.attr] = ast.dump(st.value)
                for st in init.body:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)):
                        tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
                        if isinstance(tg, ast.Attribute) and tg.attr in g.get(
                            "owned_state", []
                        ):
                            if base_state.get(tg.attr) != ast.dump(st.value):
                                fail(
                                    f"{g['class']}.__init__: owned state {tg.attr} initialised differently from the base"
                                )
                        elif isinstance(tg, ast.Attribute):
                            if not (
                                isinstance(st.value, ast.Name)
                                and st.value.id == pname(tg.attr)
                                and pname(tg.attr) in params
                            ):
                                fail(
                                    f"{g['class']}.__init__: unexpected statement {ast.unparse(st)}"
                                )
                # the owner constructs it with exactly these keywords
                hinit = hmem.get("__init__")
                built = [
                    st
                    for st in (hinit.body if hinit else [])
                    if isinstance(st, ast.Assign)
                    and isinstance(st.targets[0], ast.Attribute)
                    and st.targets[0].attr == g["attr"]
                    and isinstance(st.value, ast.Call)
                    and isinstance(st.value.func, ast.Name)
                    and st.value.func.id == g["class"]
                ]
                if len(built) != 1:
                    fail(
                        f"owner __init__ must build self.{g['attr']} = {g['class']}(...) exactly once (found {len(built)})"
                    )
                else:
                    kws = {k.arg: ast.unparse(k.value) for k in built[0].value.keywords}
                    if set(kws) != set(params):
                        fail(
                            f"self.{g['attr']}: built with {sorted(kws)}, constructor takes {sorted(params)}"
                        )
                    attr_of = {}
                    for st in init.body:
                        if (
                            isinstance(st, ast.Assign)
                            and isinstance(st.targets[0], ast.Attribute)
                            and isinstance(st.value, ast.Name)
                        ):
                            attr_of[st.value.id] = st.targets[0].attr
                    for k, val in kws.items():
                        want = f"self.{attr_of.get(k, k)}"
                        if val != want:
                            fail(
                                f"self.{g['attr']}: keyword {k}={val}, expected {want}"
                            )

    # --- owner __init__: base statements preserved except owned state -----------
    binit, hinit = bmem.get("__init__"), hmem.get("__init__")
    owned = {st for g in spec["groups"] for st in g.get("owned_state", [])}
    collab_attrs = set(ctx.collab_by_attr)
    if binit and hinit:

        def keep(st, drop):
            if isinstance(st, (ast.Assign, ast.AnnAssign)):
                tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
                if isinstance(tg, ast.Attribute) and tg.attr in drop:
                    return False
            return True

        base_drop, head_drop = set(owned), set(collab_attrs)
        if ctx.holder and ctx.flat_base:
            # E0: the flat self.X = <value> assignments become ONE holder construction
            base_drop |= ctx.holder_fields
            head_drop.add(ctx.holder)
            flat = {}
            for st in binit.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
                    if isinstance(tg, ast.Attribute) and tg.attr in ctx.holder_fields:
                        flat[tg.attr] = ast.dump(st.value)
            built = [
                st
                for st in hinit.body
                if isinstance(st, ast.Assign)
                and isinstance(st.targets[0], ast.Attribute)
                and st.targets[0].attr == ctx.holder
                and isinstance(st.value, ast.Call)
            ]
            if len(built) != 1:
                fail(f"owner __init__ must build self.{ctx.holder} exactly once")
            else:
                kws = {k.arg: ast.dump(k.value) for k in built[0].value.keywords}
                if kws != flat:
                    fail(
                        f"self.{ctx.holder}: fields {sorted(kws)} / values differ from the base assignments {sorted(flat)}"
                    )
            if set(flat) != ctx.holder_fields:
                fail(
                    f"holder fields {sorted(ctx.holder_fields)} != attributes the base __init__ set {sorted(flat)}"
                )
        bb = [ast.dump(s) for s in binit.body if keep(s, base_drop)]
        hb = [ast.dump(s) for s in hinit.body if keep(s, head_drop)]
        if bb != hb:
            fail(
                "owner __init__: statements changed beyond removing owned state and adding collaborators"
            )

    # --- coverage --------------------------------------------------------------
    missing = sorted(set(bmem) - set(seen))
    if missing:
        fail(f"members lost: {missing}")

    # --- moved module-level statements ----------------------------------------------
    bmodtop = {}
    for n in btree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bmodtop[n.name] = n
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets[0] if isinstance(n, ast.Assign) else n.target
            if isinstance(tg, ast.Name):
                bmodtop[tg.id] = n
    for g in spec["groups"]:
        tree = mods.get(g["module"])
        if tree is None:
            continue
        top = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top[n.name] = n
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                tg = n.targets[0] if isinstance(n, ast.Assign) else n.target
                if isinstance(tg, ast.Name):
                    top[tg.id] = n
        for nm in g.get("module_names", []):
            if nm not in top or ast.dump(top[nm]) != ast.dump(bmodtop[nm]):
                fail(
                    f"{nm}: module-level statement missing from {g['module']} or changed"
                )
            htop_owner = {
                n.name
                for n in htree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
            if nm in htop_owner:
                fail(f"{nm}: still defined in {src} as well (defined twice)")
        for c in g.get("hoist_constants", []):
            if c not in top or ast.dump(top[c].value) != ast.dump(bconst[c].value):
                fail(f"{c}: hoisted constant missing from {g['module']} or changed")

    if FAIL:
        print(f"FAIL ({len(FAIL)})")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print(
        f"PASS: {len(seen)} members accounted for ({sum(1 for v in seen.values() if v == 'owner')} on owner)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

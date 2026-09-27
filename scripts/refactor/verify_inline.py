#!/usr/bin/env python3
"""Prove a function-body split preserved the function: re-inline, then compare.

PROVES, for one owner method split into phase methods of the same class:

  1. RE-INLINE EQUALS BASE. Substituting every phase method's body back at its
     call site reproduces the base method's AST exactly. Allowed call sites:
       STRAIGHT  ``a, b = [await] self._p(x=x, y=y)``  (method ends ``return a, b``)
                 ``x = ...``  /  ``[await] self._p(...)``  (one / no output)
       TAIL      ``return [await] self._p(x=x, ...)``
     A leading docstring in a phase method is ignored.
  2. CALL CONTRACT. Every argument is ``name=name``; the keywords are exactly
     the method's parameters (keyword-only, after ``self``); a STRAIGHT call's
     targets are exactly the names the method returns, in order; the call is
     awaited if and only if the method is ``async``; a STRAIGHT method has no
     ``return`` other than its final one.
  3. SCOPE. No name that was LOCAL to the base method resolves differently now:
     a name a phase reads that was a base local must be a parameter of the
     phase or assigned in it (else it would silently read a GLOBAL); and every
     base local the owner still reads must still be assigned or a parameter in
     the owner (else a missing output would read a global or raise).
  3a. BINDING. A base local a phase reads BEFORE binding it (``x += a``
     included) must be a parameter; a STRAIGHT phase's returned names must be
     bound on every path through it, or be parameters. A split can re-inline
     to an equal AST and still raise UnboundLocalError at run time: these
     checks catch it.
  4. No phase uses ``super()``, ``__class__``, ``locals()``, ``vars()``,
     ``nonlocal`` or ``global`` (their meaning changes inside a new method).

Independent of extract_phase.py by design: it shares no code with the codemod.

USAGE: verify_inline.py --base BASE_FILE --head HEAD_FILE --func Class.method
EXIT: 0 PASS, 1 FAIL.
"""

from __future__ import annotations

import argparse
import ast
import copy
import sys

FAIL: list[str] = []


def fail(m):
    FAIL.append(m)


def method(tree, cls, name):
    C = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return C, next((m for m in C.body if getattr(m, "name", None) == name), None)


def strip_doc(body):
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def local_names(fn) -> set[str]:
    """Names local to ``fn`` (params + anything it binds), excluding nested scopes."""
    out = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    out |= {x.arg for x in (fn.args.vararg, fn.args.kwarg) if x}

    def walk(node):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(c.name)
                for d in c.decorator_list:
                    walk(d)
                continue
            if isinstance(
                c,
                (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
            ):
                # comprehension targets are local to the comprehension; a walrus inside binds outside
                for x in ast.walk(c):
                    if isinstance(x, ast.NamedExpr):
                        out.add(x.target.id)
                continue
            if isinstance(c, ast.Name) and not isinstance(c.ctx, ast.Load):
                out.add(c.id)
            if isinstance(c, ast.ExceptHandler) and c.name:
                out.add(c.name)
            if isinstance(c, ast.alias):
                out.add((c.asname or c.name).split(".")[0])
            walk(c)

    walk(fn)
    return out


def reads(nodes) -> set[str]:
    out = set()

    def walk(node, shadow):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = {
                    a.arg for a in c.args.posonlyargs + c.args.args + c.args.kwonlyargs
                }
                inner |= {x.arg for x in (c.args.vararg, c.args.kwarg) if x}
                body_locals = local_names(c) if not isinstance(c, ast.Lambda) else set()
                for d in (
                    getattr(c, "decorator_list", [])
                    + c.args.defaults
                    + [k for k in c.args.kw_defaults if k]
                ):
                    walk(d, shadow)
                sub = c.body if isinstance(c.body, list) else [c.body]
                for s in sub:
                    walk(s, shadow | inner | body_locals)
                continue
            if isinstance(
                c, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                tg = {
                    x.id
                    for g in c.generators
                    for x in ast.walk(g.target)
                    if isinstance(x, ast.Name)
                }
                walk(c, shadow | tg)
                continue
            if (
                isinstance(c, ast.Name)
                and isinstance(c.ctx, ast.Load)
                and c.id not in shadow
            ):
                out.add(c.id)
            walk(c, shadow)

    for n in nodes:
        walk(n, set())
    return out


def bound_by(stmts) -> set[str]:
    """Names certainly bound when ``stmts`` falls through (independent of phase_flow)."""
    out: set[str] = set()
    for s in stmts:
        if isinstance(s, (ast.Return, ast.Raise)):
            return out | {"<dead>"}
        if isinstance(s, (ast.Assign, ast.AugAssign)) or (
            isinstance(s, ast.AnnAssign) and s.value is not None
        ):
            tg = s.targets if isinstance(s, ast.Assign) else [s.target]
            out |= {x.id for t in tg for x in ast.walk(t) if isinstance(x, ast.Name)}
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in s.names}
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(s.name)
        elif isinstance(s, ast.If) and s.orelse:
            b, e = bound_by(s.body), bound_by(s.orelse)
            if "<dead>" in b and "<dead>" in e:
                return out | {"<dead>"}
            out |= (e if "<dead>" in b else b if "<dead>" in e else b & e) - {"<dead>"}
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            out |= bound_by(s.body) - {"<dead>"}
            out |= {
                x.id
                for i in s.items
                if i.optional_vars is not None
                for x in ast.walk(i.optional_vars)
                if isinstance(x, ast.Name)
            }
        for x in ast.walk(s):
            if isinstance(x, ast.NamedExpr) and isinstance(s, (ast.Assign, ast.Expr)):
                out.add(x.target.id)
    return out


def read_first(stmts, known=frozenset()) -> set[str]:
    """Names read before a binding in ``stmts`` reaches them. ``x += 1`` reads x."""
    known = set(known)
    exposed: set[str] = set()
    for s in stmts:
        if isinstance(s, ast.If):
            exposed |= reads([s.test]) - known
            exposed |= read_first(s.body, known) | read_first(s.orelse, known)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            exposed |= reads([s.iter]) - known
            tgt = {x.id for x in ast.walk(s.target) if isinstance(x, ast.Name)}
            exposed |= read_first(s.body, known | tgt) | read_first(s.orelse, known)
        elif isinstance(s, ast.While):
            exposed |= reads([s.test]) - known
            exposed |= read_first(s.body, known) | read_first(s.orelse, known)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            exposed |= reads([i.context_expr for i in s.items]) - known
            tgt = {
                x.id
                for i in s.items
                if i.optional_vars is not None
                for x in ast.walk(i.optional_vars)
                if isinstance(x, ast.Name)
            }
            exposed |= read_first(s.body, known | tgt)
        elif isinstance(s, ast.Try):
            exposed |= read_first(s.body, known)
            for h in s.handlers:
                exposed |= read_first(h.body, known | ({h.name} if h.name else set()))
            exposed |= read_first(s.orelse, known | bound_by(s.body)) | read_first(
                s.finalbody, known
            )
        else:
            r = reads([s])
            if isinstance(s, ast.AugAssign) and isinstance(s.target, ast.Name):
                r = r | {s.target.id}
            exposed |= r - known
        known |= bound_by([s]) - {"<dead>"}
    return exposed


def phase_call(stmt, phases):
    """Classify ``stmt`` as a phase call site: (kind, name, call_node, targets, awaited) or None."""

    def unwrap(v):
        aw = isinstance(v, ast.Await)
        v = v.value if aw else v
        if (
            isinstance(v, ast.Call)
            and isinstance(v.func, ast.Attribute)
            and isinstance(v.func.value, ast.Name)
            and v.func.value.id == "self"
            and v.func.attr in phases
        ):
            return v, aw
        return None, aw

    if isinstance(stmt, ast.Return) and stmt.value is not None:
        c, aw = unwrap(stmt.value)
        if c is not None:
            return "TAIL", c.func.attr, c, [], aw
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        c, aw = unwrap(stmt.value)
        if c is not None:
            t = stmt.targets[0]
            names = (
                [e.id for e in t.elts]
                if isinstance(t, ast.Tuple)
                and all(isinstance(e, ast.Name) for e in t.elts)
                else ([t.id] if isinstance(t, ast.Name) else None)
            )
            if names is None:
                fail(f"call to {c.func.attr}: targets must be plain names")
                return None
            return "STRAIGHT", c.func.attr, c, names, aw
    if isinstance(stmt, ast.Expr):
        c, aw = unwrap(stmt.value)
        if c is not None:
            return "STRAIGHT", c.func.attr, c, [], aw
    return None


def inline(stmts, phases, used):
    out = []
    for s in stmts:
        pc = phase_call(s, phases)
        if pc:
            kind, name, call, targets, awaited = pc
            used.add(name)
            m = phases[name]
            is_async = isinstance(m, ast.AsyncFunctionDef)
            if is_async != awaited:
                fail(
                    f"{name}: {'async' if is_async else 'sync'} method called {'with' if awaited else 'without'} await"
                )
            if call.args:
                fail(f"{name}: positional arguments at the call site (use name=name)")
            kws = {}
            for k in call.keywords:
                if k.arg is None or not (
                    isinstance(k.value, ast.Name) and k.value.id == k.arg
                ):
                    fail(f"{name}: argument {ast.unparse(k)} is not name=name")
                kws[k.arg] = True
            params = [a.arg for a in m.args.kwonlyargs]
            if (
                m.args.args[1:]
                or m.args.posonlyargs
                or m.args.vararg
                or m.args.kwarg
                or [a.arg for a in m.args.args[:1]] != ["self"]
            ):
                fail(f"{name}: parameters must be (self, *, inputs)")
            if set(kws) != set(params):
                fail(
                    f"{name}: call passes {sorted(kws)} but the method takes {sorted(params)}"
                )
            body = strip_doc(list(m.body))
            if kind == "STRAIGHT":
                if targets:
                    last = body[-1] if body else None
                    ret = last.value if isinstance(last, ast.Return) else None
                    rn = (
                        (
                            [e.id for e in ret.elts]
                            if isinstance(ret, ast.Tuple)
                            else [ret.id] if isinstance(ret, ast.Name) else None
                        )
                        if ret is not None
                        else None
                    )
                    if rn != targets:
                        fail(
                            f"{name}: call assigns {targets} but the method returns {rn}"
                        )
                    body = body[:-1]
                if any(
                    isinstance(n, ast.Return)
                    for st in body
                    for n in ast.walk(st)
                    if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ):
                    # returns inside nested defs are fine; walk() includes them, so re-check shallowly
                    shallow = []
                    for st in body:
                        stack = [st]
                        while stack:
                            x = stack.pop()
                            if isinstance(
                                x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                            ):
                                continue
                            if isinstance(x, ast.Return):
                                shallow.append(x)
                            stack.extend(ast.iter_child_nodes(x))
                    if shallow:
                        fail(
                            f"{name}: a STRAIGHT phase returns early at L{shallow[0].lineno}"
                        )
            out.extend(inline(copy.deepcopy(body), phases, used))
            continue
        s = copy.deepcopy(s)
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(s, field, None)
            if (
                isinstance(lst, list)
                and lst
                and isinstance(lst[0], ast.stmt)
                and not isinstance(
                    s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            ):
                setattr(s, field, inline(lst, phases, used))
        for h in getattr(s, "handlers", []) or []:
            h.body = inline(h.body, phases, used)
        out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--func", required=True)
    a = ap.parse_args()
    cls, name = a.func.split(".")
    btree, htree = ast.parse(open(a.base).read()), ast.parse(open(a.head).read())
    bC, bf = method(btree, cls, name)
    hC, hf = method(htree, cls, name)
    if bf is None or hf is None:
        print("FAIL: owner method missing")
        return 1
    base_members = {
        m.name
        for m in bC.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    phases = {
        m.name: m
        for m in hC.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        and m.name not in base_members
    }
    used: set[str] = set()
    rebuilt = copy.deepcopy(hf)
    rebuilt.body = inline(hf.body, phases, used)
    unused = sorted(set(phases) - used)
    if unused:
        fail(
            f"new methods not called from {name}: {unused} (a phase must have exactly this one caller)"
        )
    for pname, m in phases.items():
        for n in ast.walk(m):
            if isinstance(n, ast.Name) and n.id in ("__class__",):
                fail(f"{pname}: uses __class__")
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in ("super", "locals", "vars")
            ):
                fail(f"{pname}: calls {n.func.id}()")
            if isinstance(n, (ast.Nonlocal, ast.Global)):
                fail(f"{pname}: {type(n).__name__.lower()} statement")
    # the rest of the class is untouched
    for m in bC.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name != name:
            hm = next((x for x in hC.body if getattr(x, "name", None) == m.name), None)
            if hm is None or ast.dump(hm) != ast.dump(m):
                fail(f"{m.name}: another member of {cls} changed")
    # 1. re-inline equals base
    b_doc, r_doc = copy.deepcopy(bf), rebuilt
    if ast.dump(b_doc) != ast.dump(r_doc):
        bu, ru = ast.unparse(b_doc).splitlines(), ast.unparse(r_doc).splitlines()
        import difflib

        diff = list(
            difflib.unified_diff(bu, ru, "base", "re-inlined", lineterm="", n=1)
        )[:40]
        fail("re-inlined method differs from the base:\n      " + "\n      ".join(diff))
    # 3. scope
    base_locals = local_names(bf)
    for pname, m in phases.items():
        pbody = strip_doc(list(m.body))
        params = {a.arg for a in m.args.kwonlyargs}
        # (a) every base local the phase reads before binding it must arrive as a parameter
        first = (read_first(pbody) & base_locals) - params - {"self"}
        if first:
            fail(
                f"{pname}: reads {sorted(first)} (base locals) before binding them, and they are not parameters"
            )
        # (b) a STRAIGHT phase's returned names must be bound on every path, or come in as parameters
        last = pbody[-1] if pbody else None
        if (
            isinstance(last, ast.Return)
            and last.value is not None
            and not any(
                isinstance(x, ast.Return) for st in pbody[:-1] for x in ast.walk(st)
            )
        ):
            rv = last.value
            names = (
                {e.id for e in rv.elts}
                if isinstance(rv, ast.Tuple)
                else ({rv.id} if isinstance(rv, ast.Name) else set())
            )
            unbound = names - bound_by(pbody[:-1]) - params
            if unbound:
                fail(
                    f"{pname}: returns {sorted(unbound)}, not bound on every path and not parameters"
                )
        mine = local_names(m)
        leaked = sorted((reads(strip_doc(m.body)) & base_locals) - mine - {"self"})
        if leaked:
            fail(
                f"{pname}: reads {leaked}, locals of the base method that are neither its parameters nor assigned in it (they would resolve as globals)"
            )
    owner_locals = local_names(hf)
    lost = sorted((reads(hf.body) & base_locals) - owner_locals - {"self"})
    if lost:
        fail(
            f"{name}: still reads {lost}, which are no longer bound in it (a missing phase output)"
        )
    if FAIL:
        print(f"FAIL ({len(FAIL)})")
        for x in FAIL:
            print(f"  - {x}")
        return 1
    print(f"PASS: {len(phases)} phase(s) re-inline to the base {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

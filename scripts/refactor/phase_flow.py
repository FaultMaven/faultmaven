#!/usr/bin/env python3
"""Plan an Extract Method over one long function: the data flow of candidate phases.

A PHASE is a run of consecutive statements of ONE statement list anywhere in
the function (the function body, a try body, an if/else branch, a loop body).
For each phase this reports:

  shape     STRAIGHT  no return inside; call site ``a, b = self._p(...)``
            TAIL      a SUFFIX of its statement list (possibly the whole body of
                      an if/elif/else) on which every path ends in return or
                      raise; nothing after it in that list can run, so the call
                      site is ``return self._p(...)`` where the suffix started
            INVALID   with the reason (a return inside that is not a TAIL)
  inputs    locals the phase reads before binding them (``x += a`` reads x)
            that are bound before it; one not bound on EVERY path into the
            phase is flagged MAYBE-UNBOUND
  outputs   locals assigned in the phase that are LIVE after it (read before
            any definite re-assignment on the way out); STRAIGHT only. An
            output the phase re-binds on only SOME paths, and that is bound
            before it, is also an input, so its entry value survives
  undefined outputs that are not definitely assigned on every path through
            the phase and are not bound before it (the phase's ``return``
            would raise UnboundLocalError) -> a boundary to move
  conflicts names the phase assigns that an except/finally handler or a
            closure defined outside the phase reads; nonlocal/global/yield;
            break/continue escaping the phase; a nested def the phase defines
            and hands out whose free names are re-bound after the phase;
            a read of a name bound only LATER in the function
  self      ``self.<attr>`` the phase touches

USAGE:
  phase_flow.py --file F --func Class.method --outline [--depth N]
  phase_flow.py --file F --func Class.method --phase NAME:FIRST-LAST [...]
     FIRST/LAST are the first line of the phase's first statement and the last
     line of its last statement (as --outline prints them).
Overlapping --phase ranges are reported as OVERLAP.

EXIT: 0 every phase valid, 1 some phase INVALID, with undefined/conflicts, or
overlapping another.
"""

from __future__ import annotations

import argparse
import ast
import sys


class Names(ast.NodeVisitor):
    def __init__(self):
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.nested_frees: set[str] = set()
        self.nested_defs: dict[str, set[str]] = {}
        self.flags: list[str] = []
        self.returns = 0
        self.awaits = False
        self.selfattrs: set[str] = set()
        self.handler_names: set[str] = set()

    def visit_Name(self, n):
        (self.reads if isinstance(n.ctx, ast.Load) else self.writes).add(n.id)

    def visit_AugAssign(self, n):
        # ``x += a`` reads x before it writes it
        if isinstance(n.target, ast.Name):
            self.reads.add(n.target.id)
        self.generic_visit(n)

    def visit_Attribute(self, n):
        if isinstance(n.value, ast.Name) and n.value.id == "self":
            self.selfattrs.add(n.attr)
        self.generic_visit(n)

    def visit_Await(self, n):
        self.awaits = True
        self.generic_visit(n)

    def visit_Return(self, n):
        self.returns += 1
        self.generic_visit(n)

    def visit_Nonlocal(self, n):
        self.flags.append(f"nonlocal {n.names} at L{n.lineno}")

    def visit_Global(self, n):
        self.flags.append(f"global {n.names} at L{n.lineno}")

    def visit_Yield(self, n):
        self.flags.append(f"yield at L{n.lineno}")

    visit_YieldFrom = visit_Yield

    def visit_Call(self, n):
        if (
            isinstance(n.func, ast.Name)
            and n.func.id in ("locals", "vars")
            and not n.args
        ):
            self.flags.append(f"{n.func.id}() at L{n.lineno}")
        self.generic_visit(n)

    def _comp(self, n):
        inner = {
            x.id
            for g in n.generators
            for x in ast.walk(g.target)
            if isinstance(x, ast.Name)
        }
        sub = Names()
        for g in n.generators:
            sub.visit(g.iter)
            for c in g.ifs:
                sub.visit(c)
        for part in ("elt", "key", "value"):
            if hasattr(n, part):
                sub.visit(getattr(n, part))
        self.reads |= sub.reads - inner
        self.writes |= sub.writes - inner
        self.awaits |= sub.awaits
        self.selfattrs |= sub.selfattrs

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comp

    def _func(self, n):
        if hasattr(n, "name"):
            self.writes.add(n.name)
        for d in getattr(n, "decorator_list", []):
            self.visit(d)
        args = n.args
        for d in args.defaults + [x for x in args.kw_defaults if x is not None]:
            self.visit(d)
        params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        params |= {x.arg for x in (args.vararg, args.kwarg) if x}
        sub = Names()
        for s in (n.body if isinstance(n.body, list) else [n.body]):
            sub.visit(s)
        frees = sub.reads - sub.writes - params
        self.reads |= frees
        self.nested_frees |= frees
        if hasattr(n, "name"):
            self.nested_defs[n.name] = frees
        self.selfattrs |= sub.selfattrs

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def visit_ClassDef(self, n):
        self.writes.add(n.name)
        self.flags.append(f"class definition {n.name} at L{n.lineno}")

    def visit_ExceptHandler(self, n):
        if n.name:
            self.writes.add(n.name)
            self.handler_names.add(n.name)
        self.generic_visit(n)

    def visit_alias(self, n):
        self.writes.add((n.asname or n.name).split(".")[0])

    def visit_Delete(self, n):
        self.flags.append(f"del at L{n.lineno}")
        self.generic_visit(n)


def names_of(nodes):
    v = Names()
    for s in nodes:
        v.visit(s)
    return v


def terminates(stmts) -> bool:
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If):
        return terminates(last.body) and bool(last.orelse) and terminates(last.orelse)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return terminates(last.body)
    if isinstance(last, ast.Try):
        main = last.orelse if last.orelse else last.body
        return terminates(main) and all(terminates(h.body) for h in last.handlers)
    return False


def definite(stmts) -> set[str]:
    """Names definitely bound after running ``stmts`` to completion (fallthrough)."""
    out: set[str] = set()
    for s in stmts:
        if terminates([s]):
            return out | {"<unreachable>"}
        if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(s, ast.AnnAssign) and s.value is None:
                continue
            tgts = s.targets if isinstance(s, ast.Assign) else [s.target]
            for t in tgts:
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
            out |= {x.target.id for x in ast.walk(s) if isinstance(x, ast.NamedExpr)}
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in s.names}
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(s.name)
        elif isinstance(s, ast.If):
            if not s.orelse:
                continue  # the test can be false: the body binds nothing definitely
            b, e = definite(s.body), definite(s.orelse)
            bu, eu = "<unreachable>" in b, "<unreachable>" in e
            if bu and eu:
                return out | {"<unreachable>"}
            out |= (e if bu else b if eu else (b & e)) - {"<unreachable>"}
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            out |= definite(s.body) - {"<unreachable>"}
            for item in s.items:
                if item.optional_vars is not None:
                    out |= {
                        x.id
                        for x in ast.walk(item.optional_vars)
                        if isinstance(x, ast.Name)
                    }
        elif isinstance(s, ast.Try):
            # conservatively: only what the body AND every non-terminating handler bind
            body = definite(s.body + s.orelse)
            hs = [definite(h.body) for h in s.handlers if not terminates(h.body)]
            common = body
            for h in hs:
                common &= h
            out |= common - {"<unreachable>"}
            out |= definite(s.finalbody) - {"<unreachable>"}
        elif isinstance(s, ast.Expr):
            out |= {x.target.id for x in ast.walk(s) if isinstance(x, ast.NamedExpr)}
        # for/while bodies may run zero times: bind nothing definitely
    return out


def upward_exposed(stmts, seen=None) -> set[str]:
    """Names read by ``stmts`` before a definite assignment reaching the read.

    Recurses into compound statements, so ``if c: x = f(); g(x)`` does not
    expose ``x``.
    """
    seen = set(seen or ())
    exposed: set[str] = set()

    def expr_reads(nodes):
        return names_of([ast.Expr(value=n) for n in nodes if n is not None]).reads

    for s in stmts:
        if isinstance(s, ast.If):
            exposed |= expr_reads([s.test]) - seen
            exposed |= upward_exposed(s.body, seen) | upward_exposed(s.orelse, seen)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            exposed |= expr_reads([s.iter]) - seen
            inner = seen | {x.id for x in ast.walk(s.target) if isinstance(x, ast.Name)}
            exposed |= upward_exposed(s.body, inner) | upward_exposed(s.orelse, seen)
        elif isinstance(s, ast.While):
            exposed |= expr_reads([s.test]) - seen
            exposed |= upward_exposed(s.body, seen) | upward_exposed(s.orelse, seen)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            exposed |= expr_reads([i.context_expr for i in s.items]) - seen
            inner = seen | {
                x.id
                for i in s.items
                if i.optional_vars is not None
                for x in ast.walk(i.optional_vars)
                if isinstance(x, ast.Name)
            }
            exposed |= upward_exposed(s.body, inner)
        elif isinstance(s, ast.Try):
            exposed |= upward_exposed(s.body, seen)
            for h in s.handlers:
                exposed |= expr_reads([h.type]) - seen
                exposed |= upward_exposed(
                    h.body, seen | ({h.name} if h.name else set())
                )
            exposed |= upward_exposed(
                s.orelse, seen | definite(s.body)
            ) | upward_exposed(s.finalbody, seen)
        else:
            exposed |= names_of([s]).reads - seen
        seen |= definite([s]) - {"<unreachable>"}
    return exposed


def escaping_loop_control(stmts) -> list[str]:
    out = []

    def walk(node, in_loop):
        for c in ast.iter_child_nodes(node):
            if isinstance(
                c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            if isinstance(c, (ast.Break, ast.Continue)) and not in_loop:
                out.append(
                    f"{type(c).__name__.lower()} at L{c.lineno} escapes the phase"
                )
            walk(c, in_loop or isinstance(c, (ast.For, ast.AsyncFor, ast.While)))

    for s in stmts:
        if isinstance(s, (ast.Break, ast.Continue)):
            out.append(f"{type(s).__name__.lower()} at L{s.lineno} escapes the phase")
        walk(s, isinstance(s, (ast.For, ast.AsyncFor, ast.While)))
    return out


def statement_lists(node, parents=()):
    """Yield (list, owner_stmt, field, parents) for every statement list in ``node``."""
    for field in ("body", "orelse", "finalbody"):
        lst = getattr(node, field, None)
        if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
            yield lst, node, field, parents
            for s in lst:
                if not isinstance(
                    s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    yield from statement_lists(s, parents + ((lst, s),))
    for h in getattr(node, "handlers", []) or []:
        yield h.body, h, "body", parents
        for s in h.body:
            yield from statement_lists(s, parents + ((h.body, s),))


def analyse_phase(f, lo, hi):
    """The data flow of the phase at L``lo``-``hi`` of method ``f``.

    Returns a dict: found, shape, inputs, outputs, awaits, selfattrs, where,
    size, stmts, problems (a non-empty list refuses the phase).
    """
    params = {x.arg for x in f.args.posonlyargs + f.args.args + f.args.kwonlyargs}
    params |= {x.arg for x in (f.args.vararg, f.args.kwarg) if x}
    all_lists = list(statement_lists(f))
    found = None
    for lst, owner, field, parents in all_lists:
        idx = [i for i, s in enumerate(lst) if s.lineno >= lo and s.end_lineno <= hi]
        if (
            idx
            and lst[idx[0]].lineno == lo
            and lst[idx[-1]].end_lineno == hi
            and idx == list(range(idx[0], idx[-1] + 1))
        ):
            found = (lst, owner, field, parents, idx[0], idx[-1])
            break
    if not found:
        return {
            "found": False,
            "problems": [
                f"L{lo}-{hi} is not a run of whole statements of one statement list"
            ],
        }
    lst, owner, field, parents, i0, i1 = found
    body = lst[i0 : i1 + 1]
    all_stmts = [s for l, *_ in all_lists for s in l]
    before = [
        s
        for s in all_stmts
        if s.end_lineno < body[0].lineno and not any(s is p for _, p in parents)
    ]
    nb, nv = names_of(before), names_of(body)
    bound_before = params | nb.writes
    fn_locals = params | names_of(f.body).writes
    exposed = upward_exposed(body)
    problems = list(nv.flags) + escaping_loop_control(body)
    # a read that no earlier binding can reach, of a name the function binds LATER:
    # the base raises UnboundLocalError there; a phase would read a global instead
    later = sorted((exposed & fn_locals) - bound_before - {"self"})
    if later:
        problems.append(
            f"reads {later} before any binding reaches it (bound only later in the function)"
        )
    inputs = set((exposed & bound_before) - {"self"})
    # handlers/finally OUTSIDE the phase
    handler_reads = set()
    for n in ast.walk(f):
        if isinstance(n, ast.ExceptHandler) and not (lo <= n.lineno <= hi):
            handler_reads |= names_of(n.body).reads
        if (
            isinstance(n, ast.Try)
            and n.finalbody
            and not (lo <= n.finalbody[0].lineno <= hi)
        ):
            handler_reads |= names_of(n.finalbody).reads
    chain = [lst[i1 + 1 :]]
    for plist, pstmt in reversed(parents):
        if isinstance(pstmt, (ast.For, ast.AsyncFor, ast.While)):
            chain.append([pstmt])
        k = next(i for i, s_ in enumerate(plist) if s_ is pstmt)
        chain.append(plist[k + 1 :])
    live, dead = set(), set()
    for seg in chain:
        for s in seg:
            live |= (names_of([s]).reads & nv.writes) - dead
            dead |= definite([s]) - {"<unreachable>"}
    live |= nv.writes & handler_reads
    live -= nv.handler_names
    caught = (nv.writes - nv.handler_names) & handler_reads
    if caught:
        problems.append(
            f"assigns {sorted(caught)}, which an except/finally handler reads"
        )
    outside = names_of(
        [
            s
            for s in all_stmts
            if s.end_lineno < body[0].lineno or s.lineno > body[-1].end_lineno
        ]
    )
    clos = nv.writes & outside.nested_frees
    if clos:
        problems.append(
            f"assigns {sorted(clos)}, which a closure defined outside the phase reads"
        )
    after_writes = set()
    for seg in chain:
        after_writes |= names_of(seg).writes
    for dname, frees in nv.nested_defs.items():
        if dname in live and frees & after_writes:
            problems.append(
                f"nested def {dname} leaves the phase and captures {sorted(frees & after_writes)}, re-bound after it"
            )
    outputs = []
    if nv.returns == 0:
        shape = "STRAIGHT"
        outputs = sorted(live)
        d = definite(body)
        # an output the phase re-binds only on SOME paths keeps its entry value on
        # the others: it must come in as an input, or ``return`` raises
        inputs |= {n for n in outputs if n not in d and n in bound_before}
        undefined = sorted(n for n in outputs if n not in d and n not in bound_before)
        if undefined:
            problems.append(f"UNDEFINED outputs (not bound on every path): {undefined}")
    elif terminates(body) and i1 == len(lst) - 1:
        shape = "TAIL"
    else:
        shape = "INVALID"
        problems.append(
            "a return inside, but the phase is not a terminating suffix of its statement list"
        )
    entry = set(params)
    for plist, pstmt in parents:
        k = next(i for i, s_ in enumerate(plist) if s_ is pstmt)
        entry |= definite(plist[:k]) - {"<unreachable>"}
        if isinstance(pstmt, (ast.With, ast.AsyncWith)):
            for item in pstmt.items:
                if item.optional_vars is not None:
                    entry |= {
                        x.id
                        for x in ast.walk(item.optional_vars)
                        if isinstance(x, ast.Name)
                    }
        if isinstance(pstmt, (ast.For, ast.AsyncFor)):
            entry |= {x.id for x in ast.walk(pstmt.target) if isinstance(x, ast.Name)}
    entry |= definite(lst[:i0]) - {"<unreachable>"}
    maybe = sorted(inputs - entry)
    if maybe:
        problems.append(
            f"MAYBE-UNBOUND inputs (not bound on every path into the phase): {maybe}"
        )
    return {
        "found": True,
        "shape": shape,
        "inputs": sorted(inputs),
        "outputs": outputs,
        "awaits": nv.awaits,
        "selfattrs": sorted(nv.selfattrs),
        "where": "/".join(type(p).__name__ for _, p in parents) or "function",
        "size": body[-1].end_lineno - body[0].lineno + 1,
        "stmts": len(body),
        "span": (body[0].lineno, body[-1].end_lineno),
        "problems": problems,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--func", required=True)
    ap.add_argument("--phase", action="append", default=[])
    ap.add_argument("--outline", action="store_true")
    ap.add_argument("--depth", type=int, default=1)
    a = ap.parse_args()
    tree = ast.parse(open(a.file).read())
    cls, meth = a.func.split(".")
    C = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    f = next(m for m in C.body if getattr(m, "name", None) == meth)
    if a.outline:
        for lst, owner, field, parents in statement_lists(f):
            if len(parents) >= a.depth:
                continue
            for s in lst:
                ind = "  " * len(parents)
                print(
                    f"{ind}L{s.lineno:>5}-{s.end_lineno:<5} {type(s).__name__:<9} {ast.unparse(s).splitlines()[0][:80]}"
                )
        return 0
    bad = False
    spans = []
    for spec in a.phase:
        name, rng = spec.split(":")
        lo, hi = (int(x) for x in rng.split("-"))
        r = analyse_phase(f, lo, hi)
        if not r["found"]:
            print(f"\n== {name}: {r['problems'][0]}")
            bad = True
            continue
        spans.append((r["span"], name))
        print(
            f"\n== {name}: L{r['span'][0]}-{r['span'][1]} ({r['size']} lines, {r['stmts']} stmts, in {r['where']}) shape={r['shape']} {'async' if r['awaits'] else 'sync'}"
        )
        print(f"   inputs  ({len(r['inputs'])}): {', '.join(r['inputs'])}")
        if r["shape"] == "STRAIGHT":
            print(f"   outputs ({len(r['outputs'])}): {', '.join(r['outputs'])}")
        print(f"   self    : {', '.join(r['selfattrs'])}")
        if r["problems"]:
            bad = True
            print("   PROBLEMS:")
            for c in r["problems"]:
                print(f"     - {c}")
    spans.sort()
    for ((a1, b1), n1), ((a2, b2), n2) in zip(spans, spans[1:]):
        if a2 <= b1:
            print(f"\nOVERLAP: {n1} (L{a1}-{b1}) and {n2} (L{a2}-{b2})")
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

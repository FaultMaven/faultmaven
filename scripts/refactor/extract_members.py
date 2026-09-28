#!/usr/bin/env python3
"""Extract methods of one class into module functions or collaborator classes.

DOES: the production-side transformation of an Extract Function / Extract Class
refactor, driven by a JSON spec, with method bodies preserved except for the
rewrites a refactor of this kind needs. WHY: a hand-typed extraction of
thousands of lines drifts; the equivalence checker (verify_extract.py) then has
something exact to compare against.

A member's HOME is one of:
  * the owner class (not listed in any group);
  * a ``functions`` group: the method becomes a module-level function. Its
    ``self``/``cls`` parameter is removed and every injected dependency it
    reads (``self.dep``), directly or through functions it calls, becomes a
    LEADING positional parameter named like the attribute. Calls become
    ``fn(<deps>, args)``;
  * a ``collaborator`` group: the method moves verbatim into a new class
    whose constructor takes every dependency the group reads as a keyword
    argument; the owner builds it in ``__init__`` and holds it as
    ``self.<attr>``. A collaborator method called from outside the
    collaborator is part of its interface and loses its leading underscore.

A function may MUTATE an object the owner passes it (``self._cache[k] = v``
becomes ``cache[k] = v`` on the very dict the owner holds), but may not REBIND
an attribute (``self._last = k``): that is refused.

Permitted body rewrites (and nothing else): ``self``/``cls``/decorator removal
for functions; ``self.dep`` -> ``dep`` inside functions; member references
re-pointed at their new home; references to moved class constants re-pointed;
dedent. A reference from moved code back into the owner is an ERROR: it
means the boundary is wrong.

SPEC (JSON):
{
  "source": "faultmaven/.../engine.py",
  "class": "MilestoneEngine",
  "groups": [
    {"module": "faultmaven/.../structured_output.py", "kind": "functions",
     "members": ["_a", "_b"],
     "module_names": ["_SENTINEL"],          # top-level statements of source that move here
     "hoist_constants": []                   # class constants that become module constants here
    },
    {"module": "faultmaven/.../generation.py", "kind": "collaborator",
     "class": "StructuredOutputGenerator", "attr": "generator",
     "members": ["_generate", "_tool_loop"],
     "class_constants": ["MAX_TOOL_ITERATIONS"],
     "owned_state": ["_inflight"],           # attrs the collaborator initialises (from owner __init__)
     "module_names": [],
     "docstring": "..."}
  ]
}

USAGE: extract_members.py --repo DIR --spec spec.json [--apply] [--report out.json]
Without --apply it only analyses and prints the plan / errors.
Exit: 0 ok, 1 errors (nothing written), 2 usage.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections import defaultdict
from pathlib import Path


class SpecError(Exception):
    pass


def pname(attr: str) -> str:
    """Parameter / keyword name for an injected attribute: leading underscores dropped.

    A field of the shared dependency holder (``deps.X``) is passed as ``X``.
    """
    return attr.split(".")[-1].lstrip("_")


def collab_dep(attr: str) -> str:
    """What a collaborator must hold to read ``attr``: the holder itself for a holder field."""
    return attr.split(".")[0]


HOLDER: list[str] = (
    []
)  # set from the spec: ["deps"] when the owner keeps a shared holder


def string_interior_lines(src: str) -> set[int]:
    """1-based line numbers that lie INSIDE a multi-line string token (not its first line)."""
    out: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
            out.update(range(tok.start[0] + 1, tok.end[0] + 1))
    return out


class Source:
    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text()
        self.lines = self.text.splitlines(keepends=True)
        self.tree = ast.parse(self.text)
        self.line_starts = [0]
        for ln in self.lines:
            self.line_starts.append(self.line_starts[-1] + len(ln))
        self.interior = string_interior_lines(self.text)

    def off(self, lineno: int, col: int) -> int:
        # ast col offsets are UTF-8 byte offsets
        line = self.lines[lineno - 1]
        return self.line_starts[lineno - 1] + len(line.encode()[:col].decode())

    def node_span(self, node) -> tuple[int, int]:
        return self.off(node.lineno, node.col_offset), self.off(
            node.end_lineno, node.end_col_offset
        )


def leading_comment_start(src: Source, first_line: int) -> int:
    """Extend a statement upward over a contiguous comment block directly above it."""
    ln = first_line
    while ln > 1 and src.lines[ln - 2].strip().startswith("#"):
        ln -= 1
    return ln


def stmt_first_line(node) -> int:
    decos = getattr(node, "decorator_list", [])
    return min([node.lineno] + [d.lineno for d in decos])


class Member:
    def __init__(self, node, src: Source, cls_name: str):
        self.node = node
        self.name = node.name
        decos = [ast.unparse(d) for d in node.decorator_list]
        self.is_static = "staticmethod" in decos
        self.is_class = "classmethod" in decos
        args = node.args.posonlyargs + node.args.args
        self.first = None if self.is_static or not args else args[0].arg
        self.first_arg_node = None if self.first is None else args[0]
        self.start_line = leading_comment_start(src, stmt_first_line(node))
        self.end_line = node.end_lineno
        self.member_refs: list[tuple[ast.Attribute, str]] = []  # (node, member name)
        self.const_refs: list[tuple[ast.Attribute, str]] = []
        self.dep_reads: dict[str, list[ast.Attribute]] = defaultdict(list)
        self.state_writes: set[str] = set()
        self.dynamic_reads: list[str] = []
        self.global_names: set[str] = set()
        self.is_property = False


def analyse(src: Source, cls_name: str):
    cls = next(
        (
            n
            for n in src.tree.body
            if isinstance(n, ast.ClassDef) and n.name == cls_name
        ),
        None,
    )
    if cls is None:
        raise SpecError(f"class {cls_name} not found in {src.path}")
    members = {
        n.name: Member(n, src, cls_name)
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    consts = {}
    for n in cls.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            consts[n.target.id] = n
    properties = {
        n
        for n, m in members.items()
        if any(
            ast.unparse(d)
            in ("property", "functools.cached_property", "cached_property")
            or ast.unparse(d).endswith(".setter")
            for d in m.node.decorator_list
        )
    }
    for m in members.values():
        skip: set[int] = set()
        for node in ast.walk(m.node):
            if id(node) in skip:
                continue
            if (
                HOLDER
                and m.first
                and isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == m.first
                and node.value.attr == HOLDER[0]
                and isinstance(node.ctx, ast.Load)
            ):
                m.dep_reads[f"{HOLDER[0]}.{node.attr}"].append(
                    node
                )  # a holder field, read per call
                skip.add(id(node.value))
                continue
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                recv = node.value.id
                if recv == cls_name or (m.first and recv == m.first):
                    if (
                        node.attr in properties
                        and recv != cls_name
                        and isinstance(node.ctx, ast.Load)
                    ):
                        m.dep_reads[node.attr].append(
                            node
                        )  # read through the owner at call time
                    elif node.attr in members:
                        m.member_refs.append((node, node.attr))
                    elif node.attr in consts:
                        m.const_refs.append((node, node.attr))
                    elif recv == cls_name:
                        raise SpecError(
                            f"{m.name}: {cls_name}.{node.attr} is neither member nor constant"
                        )
                    elif isinstance(node.ctx, ast.Store):
                        m.state_writes.add(node.attr)
                    else:
                        m.dep_reads[node.attr].append(node)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "hasattr", "setattr")
                and node.args
                and isinstance(node.args[0], ast.Name)
                and m.first
                and node.args[0].id == m.first
            ):
                m.dynamic_reads.append(ast.unparse(node))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                m.global_names.add(node.id)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "super"
            ):
                raise SpecError(f"{m.name}: uses super(); not supported")
    for n in properties:
        members[n].is_property = True
    return cls, members, consts


def parent_map(tree):
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    return parents


def module_level_defs(src: Source):
    """name -> top-level statement defining it (functions, classes, assignments)."""
    out = {}
    for n in src.tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for nm in ast.walk(t):
                    if isinstance(nm, ast.Name):
                        out[nm.id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out[n.target.id] = n
    return out


def _import_chunks(src: Source) -> list[str]:
    chunks = []
    for n in src.tree.body:
        is_tc = (
            isinstance(n, ast.If)
            and "TYPE_CHECKING" in ast.unparse(n.test)
            and all(isinstance(b, (ast.Import, ast.ImportFrom)) for b in n.body)
        )
        if isinstance(n, (ast.Import, ast.ImportFrom)) or is_tc:
            chunks.append("".join(src.lines[n.lineno - 1 : n.end_lineno]))
    return chunks


def import_block(src: Source) -> str:
    """Every top-level import statement (and `if TYPE_CHECKING:` import block), verbatim."""
    chunks = []
    for n in src.tree.body:
        is_tc = (
            isinstance(n, ast.If)
            and "TYPE_CHECKING" in ast.unparse(n.test)
            and all(isinstance(b, (ast.Import, ast.ImportFrom)) for b in n.body)
        )
        if isinstance(n, (ast.Import, ast.ImportFrom)) or is_tc:
            chunks.append("".join(src.lines[n.lineno - 1 : n.end_lineno]))
    return "".join(chunks)


def _is_type_checking_block(n: ast.stmt) -> bool:
    return (
        isinstance(n, ast.If)
        and "TYPE_CHECKING" in ast.unparse(n.test)
        and all(isinstance(b, (ast.Import, ast.ImportFrom)) for b in n.body)
    )


def _import_bindings(n: ast.stmt) -> dict[str, str]:
    """name -> what it is bound to, for one import statement (dotted source)."""
    if isinstance(n, ast.Import):
        return {a.asname or a.name: a.name for a in n.names}
    if isinstance(n, ast.ImportFrom):
        mod = "." * n.level + (n.module or "")
        return {a.asname or a.name: f"{mod}:{a.name}" for a in n.names}
    return {}


def append_to_module(
    existing: str, import_chunks: list[str], add_logger: bool, body: str
) -> str:
    """Append ``body`` to an existing module, merging its imports into the header.

    The new imports go after the module's leading run of imports, never at the
    append point: ruff's ``I`` rules sort one contiguous block, so an import
    written below existing code stays there. A name the module already imports
    from the same source is dropped rather than imported twice; the same name
    bound from a DIFFERENT source is refused, since dropping either binding
    would silently change what the moved code or the existing code reads.
    A ``from __future__`` import goes first, where Python requires it.
    """
    tree = ast.parse(existing)
    lines = existing.splitlines(keepends=True)
    bound: dict[str, str] = {}
    for n in tree.body:
        for s in n.body if _is_type_checking_block(n) else [n]:
            bound.update(_import_bindings(s))
    docstring_end = 0
    header_end = 0
    for i, n in enumerate(tree.body):
        if (
            i == 0
            and isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        ):
            docstring_end = header_end = n.end_lineno
        elif isinstance(n, (ast.Import, ast.ImportFrom)) or _is_type_checking_block(n):
            header_end = n.end_lineno
        else:
            break
    future, rest = [], []
    for chunk in import_chunks:
        n = ast.parse(chunk).body[0]
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            new = _import_bindings(n)
            clash = sorted(k for k, v in new.items() if k in bound and bound[k] != v)
            if clash:
                raise ValueError(
                    f"appending would re-bind {clash}, which the module already "
                    "imports from a different source"
                )
            keep = [a for a in n.names if (a.asname or a.name) not in bound]
            if not keep:
                continue
            if len(keep) < len(n.names):
                n.names = keep
                chunk = ast.unparse(n) + "\n"
            bound.update(_import_bindings(n))
        elif chunk in existing:
            continue
        is_future = isinstance(n, ast.ImportFrom) and n.module == "__future__"
        (future if is_future else rest).append(chunk)
    logger_line = "\nlogger = logging.getLogger(__name__)\n" if add_logger else ""
    return (
        "".join(lines[:docstring_end])
        + "".join(future)
        + "".join(lines[docstring_end:header_end])
        + "".join(rest)
        + logger_line
        + "".join(lines[header_end:]).rstrip("\n")
        + "\n\n\n"
        + body
    )


def dotted(repo: Path, path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        p = p.relative_to(repo)
    return ".".join(p.with_suffix("").parts)


def plan(repo: Path, spec: dict):
    src = Source(repo / spec["source"])
    cls_name = spec["class"]
    cls, members, consts = analyse(src, cls_name)
    PARENTS.clear()
    PARENTS.update(parent_map(src.tree))
    toplevel = module_level_defs(src)
    home: dict[str, dict | None] = {m: None for m in members}
    groups = spec["groups"]
    const_home: dict[str, dict] = {}
    name_home: dict[str, dict] = {}
    for g in groups:
        g.setdefault("module_names", [])
        g.setdefault("hoist_constants", [])
        g.setdefault("class_constants", [])
        g.setdefault("owned_state", [])
        for m in g["members"]:
            if m not in members:
                raise SpecError(f"{m}: not a method of {cls_name}")
            if home[m] is not None:
                raise SpecError(f"{m}: listed in two groups")
            if m == "__init__":
                raise SpecError("__init__ cannot move")
            home[m] = g
        for c in g["class_constants"] + g["hoist_constants"]:
            if c not in consts:
                raise SpecError(f"{c}: not a class constant of {cls_name}")
            if c in const_home:
                raise SpecError(f"{c}: constant homed twice")
            const_home[c] = g
        if g["kind"] == "functions" and g["class_constants"]:
            raise SpecError(
                f"{g['module']}: a functions group cannot hold class_constants (use hoist_constants)"
            )
        if g["kind"] == "collaborator" and g["hoist_constants"]:
            raise SpecError(
                f"{g['module']}: a collaborator holds class_constants, not hoist_constants"
            )
        for nm in g["module_names"]:
            if nm not in toplevel:
                raise SpecError(f"{nm}: not a top-level name of {spec['source']}")
            name_home[nm] = g

    errors: list[str] = []
    collab_attrs = {g["attr"]: g for g in groups if g["kind"] == "collaborator"}
    all_deps_attrs = set()
    for m in members.values():
        all_deps_attrs |= set(m.dep_reads)

    # ---- dependency closure ------------------------------------------------
    deps: dict[str, list[str]] = {}  # function member -> ordered deps
    cdeps: dict[str, set[str]] = {g["attr"]: set() for g in collab_attrs.values()}

    def direct_deps(m: Member) -> set[str]:
        return set(m.dep_reads)

    fdeps: dict[str, set[str]] = {
        n: direct_deps(members[n])
        for n, g in home.items()
        if g and g["kind"] == "functions"
    }
    for n, g in home.items():
        if g and g["kind"] == "collaborator":
            cdeps[g["attr"]] |= {collab_dep(d) for d in direct_deps(members[n])} - set(
                g["owned_state"]
            )
    changed = True
    while changed:
        changed = False
        for n, g in home.items():
            if not g:
                continue
            m = members[n]
            for _, r in m.member_refs:
                rg = home[r]
                need: set[str] = set()
                if rg is None:
                    continue
                if rg["kind"] == "functions":
                    need = fdeps[r]
                elif rg is not g:
                    need = {rg["attr"]}
                if g["kind"] == "functions":
                    if not need <= fdeps[n]:
                        fdeps[n] |= need
                        changed = True
                else:
                    add = (
                        {collab_dep(d) for d in need}
                        - set(g["owned_state"])
                        - {g["attr"]}
                    )
                    if not add <= cdeps[g["attr"]]:
                        cdeps[g["attr"]] |= add
                        changed = True
            for _, c in m.const_refs:
                cg = const_home.get(c)
                if cg is not None and cg["kind"] == "collaborator" and cg is not g:
                    pass  # referenced by class name, no instance dependency
    # owner members calling functions need nothing extra (they pass self.dep)
    for n, s in fdeps.items():
        deps[n] = sorted(s)

    # ---- validation ----------------------------------------------------------
    for n, g in home.items():
        m = members[n]
        where = "owner" if g is None else g["module"]
        for _, r in m.member_refs:
            if g is not None and home[r] is None:
                errors.append(
                    f"{n} ({where}) references {r}, which stays on the owner: boundary is wrong"
                )
        for _, c in m.const_refs:
            if g is not None and c not in const_home:
                errors.append(
                    f"{n} ({where}) reads class constant {c}, which stays on the owner"
                )
        if g is not None and m.is_property:
            errors.append(
                f"{n}: is a property; properties stay on the owner and are passed as values"
            )
        if g is not None and g["kind"] == "functions":
            used = {x.id for x in ast.walk(m.node) if isinstance(x, ast.Name)}
            used |= {
                x.arg
                for x in m.node.args.posonlyargs
                + m.node.args.args
                + m.node.args.kwonlyargs
            }
            pn = [pname(d) for d in deps.get(n, [])]
            if len(set(pn)) != len(pn):
                errors.append(
                    f"{n}: two dependencies map to one parameter name: {deps.get(n)}"
                )
            for d in pn:
                if d in used:
                    errors.append(
                        f"{n}: dependency parameter {d!r} would shadow a name the body already uses"
                    )
            if m.state_writes:
                errors.append(
                    f"{n}: writes instance state {sorted(m.state_writes)}; cannot be a function"
                )
            if m.dynamic_reads:
                errors.append(
                    f"{n}: dynamic self access {m.dynamic_reads}; cannot be a function"
                )
        if g is not None and g["kind"] == "collaborator":
            bad = m.state_writes - set(g["owned_state"])
            if bad:
                errors.append(
                    f"{n}: writes {sorted(bad)} not declared owned_state of {g['class']}"
                )
            if m.dynamic_reads:
                errors.append(
                    f"{n}: dynamic self access {m.dynamic_reads}; declare the attribute a dependency by hand"
                )
        # callbacks to functions with deps
        for node, r in m.member_refs:
            rg = home[r]
            if rg is not None and rg["kind"] == "functions" and deps[r]:
                par = PARENTS.get(node)
                if not (isinstance(par, ast.Call) and par.func is node):
                    errors.append(
                        f"{n}: passes {r} as a value but {r} needs deps {deps[r]}"
                    )
        if g is not None:
            for nm in m.global_names:
                if (
                    nm in toplevel
                    and nm not in name_home
                    and nm != cls_name
                    and nm != "logger"
                ):
                    errors.append(
                        f"{n} ({where}) reads module-level {nm!r} of {spec['source']}; home it via module_names"
                    )
    for a, ds in cdeps.items():
        for d in ds:
            if d in members and members[d].is_property:
                errors.append(
                    f"collaborator {collab_attrs[a]['class']} reads owner property {d}: it would snapshot a value the owner computes per call"
                )
    # collaborator dependency cycle
    graph = {a: {d for d in cdeps[a] if d in collab_attrs} for a in cdeps}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(a, stack):
        if state.get(a) == 2:
            return
        if state.get(a) == 1:
            errors.append(f"collaborator cycle: {' -> '.join(stack + [a])}")
            return
        state[a] = 1
        for d in sorted(graph[a]):
            visit(d, stack + [a])
        state[a] = 2
        order.append(a)

    for a in sorted(graph):
        visit(a, [])

    # ---- exposure (renames) --------------------------------------------------
    rename: dict[str, str] = {}
    for n, g in home.items():
        if g and g["kind"] == "collaborator":
            callers = {
                c for c, cg in home.items() for _, r in members[c].member_refs if r == n
            }
            external = any(home[c] is not g for c in callers)
            if external and n.startswith("_") and not n.startswith("__"):
                rename[n] = n.lstrip("_")
            else:
                rename[n] = n
    for n, g in home.items():
        if g and g["kind"] == "collaborator":
            for other, og in home.items():
                if og is g and other != n and rename.get(other) == rename[n]:
                    errors.append(
                        f"rename collision in {g['class']}: {n} and {other} -> {rename[n]}"
                    )

    # owner attributes no longer read by the owner
    owner_reads = set()
    for n, g in home.items():
        if g is None and n != "__init__":
            owner_reads |= {collab_dep(d) for d in members[n].dep_reads}
            # the owner also reads every dependency it now PASSES to a moved function
            for _, r in members[n].member_refs:
                if home[r] is not None and home[r]["kind"] == "functions":
                    owner_reads |= {collab_dep(d) for d in deps.get(r, [])}
    init_sets = members["__init__"].state_writes if "__init__" in members else set()
    unread = sorted(a for a in init_sets if a not in owner_reads)

    return dict(
        src=src,
        cls=cls,
        members=members,
        consts=consts,
        home=home,
        deps=deps,
        cdeps=cdeps,
        collab_order=order,
        rename=rename,
        errors=errors,
        const_home=const_home,
        name_home=name_home,
        toplevel=toplevel,
        collab_attrs=collab_attrs,
        unread=unread,
    )


PARENTS: dict = {}


def dep_expr(ctx_home, d: str) -> str:
    if ctx_home is None or ctx_home["kind"] == "collaborator":
        return f"self.{d}"
    return pname(d)


def member_edits(P, n: str):
    """Text edits (start, end, replacement) over the SOURCE for member n's segment."""
    src, members, home = P["src"], P["members"], P["home"]
    m, g = members[n], home[n]
    edits = []
    cls_name = P["cls"].name
    for node, r in m.member_refs:
        rg = home[r]
        if rg is None:
            continue
        s, e = src.node_span(node)
        par = PARENTS.get(node)
        is_call = isinstance(par, ast.Call) and par.func is node
        if rg["kind"] == "functions":
            edits.append((s, e, r))
            dl = [dep_expr(g, d) for d in P["deps"][r]]
            if is_call and dl:
                ins = ", ".join(dl) + (", " if (par.args or par.keywords) else "")
                edits.append((e + 1, e + 1, ins))  # just after "("
        else:
            new = P["rename"][r]
            if g is rg:
                recv = node.value.id
                if recv == cls_name:
                    edits.append((s, e, f"{rg['class']}.{new}"))
                else:
                    edits.append((s, e, f"{recv}.{new}"))
            elif g is None or g["kind"] == "collaborator":
                edits.append((s, e, f"self.{rg['attr']}.{new}"))
            else:
                edits.append((s, e, f"{rg['attr']}.{new}"))
    for node, c in m.const_refs:
        cg = P["const_home"].get(c)
        if cg is None:
            continue
        s, e = src.node_span(node)
        if cg["kind"] == "functions":  # hoisted to module constant
            edits.append((s, e, c))
        elif cg is g:
            if node.value.id == cls_name:
                edits.append((s, e, f"{cg['class']}.{c}"))
        else:
            edits.append((s, e, f"{cg['class']}.{c}"))
    if g is not None and g["kind"] == "functions":
        for d, nodes in m.dep_reads.items():
            for node in nodes:
                s, e = src.node_span(node)
                edits.append((s, e, pname(d)))
        # signature: drop self/cls, prepend deps
        dl = [pname(d) for d in P["deps"][n]]
        if m.first_arg_node is not None:
            s, e = src.node_span(m.first_arg_node)
            tail = src.text[e:]
            mt = re.match(
                r"[ \t]*,[ \t]*", tail
            )  # never crosses a newline: edits keep line count
            if mt:
                repl = ", ".join(dl) + (", " if dl else "")
                edits.append((s, e + mt.end(), repl))
            else:
                edits.append((s, e, ", ".join(dl)))
        elif dl:
            a = m.node.args.posonlyargs + m.node.args.args + m.node.args.kwonlyargs
            if a:
                s, _ = src.node_span(a[0])
                edits.append((s, s, ", ".join(dl) + ", "))
            else:
                # def f(): -> def f(deps): ; locate "(" after the name
                s = src.off(m.node.lineno, m.node.col_offset)
                p = src.text.index("(", s)
                edits.append((p + 1, p + 1, ", ".join(dl)))
        for d in m.node.decorator_list:
            if ast.unparse(d) in ("staticmethod", "classmethod"):
                s = src.line_starts[d.lineno - 1]
                e2 = (
                    src.line_starts[d.end_lineno] - 1
                )  # keep the newline: edits keep line count
                edits.append((s, e2, ""))
    return edits


def apply_edits(text: str, base: int, edits) -> str:
    out = text
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        out = out[: s - base] + r + out[e - base :]
    return out


def segment(src: Source, first_line: int, last_line: int):
    s = src.line_starts[first_line - 1]
    e = src.line_starts[last_line]
    return s, e


def dedent_segment(src: Source, text: str, first_line: int, amount: int) -> str:
    lines = text.splitlines(keepends=True)
    out = []
    for i, ln in enumerate(lines):
        lineno = first_line + i
        if lineno in src.interior:
            out.append(ln)
        elif ln[:amount].strip() == "":
            out.append(ln[amount:] if len(ln) > amount else ln.lstrip(" "))
        else:
            out.append(ln)
    return "".join(out)


def render(P, repo: Path, spec: dict):
    src, members, home = P["src"], P["members"], P["home"]
    files: dict[str, str] = {}
    owner_mod = dotted(repo, spec["source"])
    by_group: dict[int, list[str]] = defaultdict(list)
    for n, g in home.items():
        if g is not None:
            by_group[id(g)].append(n)
    removals = []  # (start, end) over source

    def member_text(n):
        m = members[n]
        s, e = segment(src, m.start_line, m.end_line)
        txt = apply_edits(
            src.text[s:e], s, [x for x in member_edits(P, n) if s <= x[0] <= e]
        )
        return s, e, txt

    group_imports: dict[str, set[str]] = defaultdict(
        set
    )  # module -> names needed from other groups

    def need_names(n):
        """Names from OTHER modules the rewritten member n needs imported."""
        m, g = members[n], home[n]
        out = set()
        for _, r in m.member_refs:
            rg = home[r]
            if rg is None or rg is g:
                continue
            if rg["kind"] == "functions":
                out.add((rg["module"], r))
        for _, c in m.const_refs:
            cg = P["const_home"].get(c)
            if cg is None or cg is g:
                continue
            out.add((cg["module"], cg["class"] if cg["kind"] == "collaborator" else c))
        for nm in m.global_names:
            ng = P["name_home"].get(nm)
            if ng is not None and ng is not g:
                out.add((ng["module"], nm))
        return out

    header_imports = import_block(src)
    for g in spec["groups"]:
        body = []
        for nm in g["module_names"]:
            node = P["toplevel"][nm]
            s, e = segment(
                src, leading_comment_start(src, stmt_first_line(node)), node.end_lineno
            )
            if (s, e) not in removals:
                removals.append((s, e))
                body.append(src.text[s:e])
        for c in g["hoist_constants"]:
            node = P["consts"][c]
            s, e = segment(
                src, leading_comment_start(src, stmt_first_line(node)), node.end_lineno
            )
            removals.append((s, e))
            body.append(
                dedent_segment(
                    src,
                    src.text[s:e],
                    leading_comment_start(src, stmt_first_line(node)),
                    4,
                )
            )
        needs = set()
        members_in = [n for n in members if home[n] is g]
        if g["kind"] == "functions":
            for n in members_in:
                s, e, txt = member_text(n)
                removals.append((s, e))
                body.append(
                    dedent_segment(
                        src, txt, members[n].start_line, members[n].node.col_offset
                    )
                )
                needs |= need_names(n)
        else:
            cls_lines = [
                f"class {g['class']}:\n",
                f'    """{g.get("docstring", "TODO: docstring")}"""\n\n',
            ]
            for c in g["class_constants"]:
                node = P["consts"][c]
                s, e = segment(
                    src,
                    leading_comment_start(src, stmt_first_line(node)),
                    node.end_lineno,
                )
                removals.append((s, e))
                cls_lines.append(src.text[s:e])
            dl = sorted(P["cdeps"][g["attr"]])
            params = ", ".join(pname(d) for d in dl)
            init = [
                (
                    f"\n    def __init__(self, *, {params}) -> None:\n"
                    if dl
                    else "\n    def __init__(self) -> None:\n"
                )
            ]
            for d in dl:
                init.append(f"        self.{d} = {pname(d)}\n")
            for st in g["owned_state"]:
                init.append(
                    f"        self.{st} = {g.get('state_init', {}).get(st, 'TODO')}\n"
                )
            cls_lines += init
            for n in members_in:
                s, e, txt = member_text(n)
                removals.append((s, e))
                new = P["rename"][n]
                if new != n:
                    txt = re.sub(
                        rf"(\bdef\s+){re.escape(n)}\b", rf"\g<1>{new}", txt, count=1
                    )
                cls_lines.append("\n" + txt)
                needs |= need_names(n)
            body.append("".join(cls_lines))
        imp = []
        for mod, nm in sorted(needs):
            if mod != g["module"]:
                imp.append(f"from {dotted(repo, mod)} import {nm}\n")
        target = repo / g["module"]
        existing = target.read_text() if target.exists() and g.get("append") else None
        self_mod = dotted(repo, g["module"])
        self_rel = "." + Path(g["module"]).stem
        own_chunks = [
            chunk
            for chunk in _import_chunks(src)
            if not re.search(
                rf"^\s*from\s+({re.escape(self_mod)}|{re.escape(self_rel)})\s+import",
                chunk,
                re.M,
            )
        ]
        uses_logger = "logger" in "".join(body)
        if existing:
            has_logger = re.search(r"^logger\s*=", existing, re.M) is not None
            try:
                text = append_to_module(
                    existing,
                    own_chunks + imp,
                    uses_logger and not has_logger,
                    "\n\n".join(body),
                )
            except ValueError as e:
                raise SystemExit(f"{g['module']}: {e}")
        else:
            doc = g.get("module_docstring", "TODO: module docstring.")
            text = (
                f'"""{doc}"""\n\n'
                + "".join(own_chunks)
                + "".join(imp)
                + ("\nlogger = logging.getLogger(__name__)\n" if uses_logger else "")
                + "\n\n"
                + "\n\n".join(body)
            )
        files[g["module"]] = text

    # owner
    owner_edits = []
    for n, g in home.items():
        if g is None:
            owner_edits += member_edits(P, n)
    text = src.text
    # collaborator construction appended to __init__
    init = members.get("__init__")
    if init is not None and P["collab_order"]:
        lines = []
        for a in P["collab_order"]:
            g = P["collab_attrs"][a]
            kw = ", ".join(
                f"{pname(d)}={dep_expr(None, d)}" for d in sorted(P["cdeps"][a])
            )
            lines.append(f"        self.{a} = {g['class']}({kw})\n")
        pos = src.line_starts[init.node.end_lineno]
        owner_edits.append((pos, pos, "".join(lines)))
    # owned state initialisers leave the owner __init__
    owned = {st for g in spec["groups"] for st in g.get("owned_state", [])}
    if init is not None:
        for stmt in init.node.body:
            tgts = []
            if isinstance(stmt, ast.Assign):
                tgts = stmt.targets
            elif isinstance(stmt, ast.AnnAssign):
                tgts = [stmt.target]
            for t in tgts:
                if isinstance(t, ast.Attribute) and t.attr in owned:
                    s, e = segment(
                        src, leading_comment_start(src, stmt.lineno), stmt.end_lineno
                    )
                    owner_edits.append((s, e, ""))
    for s, e in removals:
        owner_edits.append((s, e, ""))
    # imports for the owner
    owner_needs = set()
    for n, g in home.items():
        if g is None:
            owner_needs |= need_names(n)
    for a in P["collab_order"]:
        g = P["collab_attrs"][a]
        owner_needs.add((g["module"], g["class"]))
    last_import = max(
        (
            n.end_lineno
            for n in src.tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ),
        default=0,
    )
    pos = src.line_starts[last_import]
    owner_edits.append(
        (
            pos,
            pos,
            "".join(
                f"from {dotted(repo, mod)} import {nm}\n"
                for mod, nm in sorted(owner_needs)
            ),
        )
    )
    files[spec["source"]] = apply_edits(text, 0, owner_edits)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    spec = json.loads(Path(a.spec).read_text())
    HOLDER.clear()
    if spec.get("holder"):
        HOLDER.append(spec["holder"]["attr"])
    try:
        P = plan(repo, spec)
    except SpecError as ex:
        print(f"SPEC ERROR: {ex}")
        return 1
    for g in spec["groups"]:
        ms = [n for n in P["members"] if P["home"][n] is g]
        if g["kind"] == "functions":
            print(f"[functions] {g['module']}")
            for n in ms:
                print(f"    {n}({', '.join(P['deps'][n])})")
        else:
            print(f"[collaborator] {g['class']} as self.{g['attr']} in {g['module']}")
            print(f"    deps: {sorted(P['cdeps'][g['attr']])}")
            for n in ms:
                r = P["rename"][n]
                print(f"    {n}" + (f" -> {r}" if r != n else ""))
    print(f"collaborator construction order: {P['collab_order']}")
    print(
        f"owner attributes the owner no longer reads (R5: drop unless read outside): {P['unread']}"
    )
    rep = dict(
        deps=P["deps"],
        collab_deps={k: sorted(v) for k, v in P["cdeps"].items()},
        rename=P["rename"],
        homes={n: (None if g is None else g["module"]) for n, g in P["home"].items()},
        collab_attr={
            n: g["attr"]
            for n, g in P["home"].items()
            if g and g["kind"] == "collaborator"
        },
        collab_class={
            n: g["class"]
            for n, g in P["home"].items()
            if g and g["kind"] == "collaborator"
        },
        unread=P["unread"],
        errors=P["errors"],
    )
    if a.report:
        Path(a.report).write_text(json.dumps(rep, indent=1))
    if P["errors"]:
        print(f"\n{len(P['errors'])} ERROR(S):")
        for e in P["errors"]:
            print(f"  - {e}")
        return 1
    if a.apply:
        files = render(P, repo, spec)
        for rel, text in files.items():
            (repo / rel).write_text(text)
            print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

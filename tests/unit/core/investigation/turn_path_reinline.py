"""Rebuild the pre-#1707-wave-3 statement order of ``_process_turn_impl``.

Wave 3 split ``MilestoneEngine._process_turn_impl`` into private phase
methods of the same class (``_confirm_pending_transition``,
``_decline_bare_reply``, ``_represent_pending_transition``,
``_close_on_explicit_intent``, ``_generate_turn_response``,
``_apply_turn_response``, ``_persist_turn``, ``_compose_turn_reply``). A
source-pinning test that asserted one call precedes another across what are
now two different phase methods can no longer answer that from either
method's own ``inspect.getsource`` in isolation.

This module answers it the way ``verify_inline.py`` proves the split itself:
by substituting each phase call back into the owner's body — an AST inline,
never a source concatenation, because concatenation would not preserve how
the phases actually interleave with the owner's own dispatch statements (the
``if``/``elif`` skeleton the split left behind). Call
``reinlined_process_turn_impl_source()`` and run the same ``.index()``/``in``
assertions against its return value that used to run against
``inspect.getsource(MilestoneEngine._process_turn_impl)`` directly.
"""

from __future__ import annotations

import ast
import copy
import inspect

import faultmaven.core.investigation.milestone_engine.engine as _engine_module

#: Every phase method wave 3 extracted from ``_process_turn_impl``. Kept
#: here rather than discovered, so a future phase split (or un-split) is a
#: deliberate edit to this list rather than a silent change in what "the
#: turn path" means to these tests.
PHASE_METHOD_NAMES = (
    "_confirm_pending_transition",
    "_decline_bare_reply",
    "_represent_pending_transition",
    "_close_on_explicit_intent",
    "_generate_turn_response",
    "_apply_turn_response",
    "_persist_turn",
    "_compose_turn_reply",
)


def _strip_doc(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _phase_call(
    stmt: ast.stmt, phase_names: set[str]
) -> tuple[str, str] | tuple[None, None]:
    """Classify ``stmt`` as a phase call site: (kind, phase name) or (None, None).

    Mirrors the call-site shapes ``extract_phase.py`` writes: TAIL is
    ``return [await] self._p(...)``; STRAIGHT is ``<targets> = [await]
    self._p(...)`` or a bare ``[await] self._p(...)``.
    """

    def unwrap(v: ast.expr) -> ast.Call | None:
        v = v.value if isinstance(v, ast.Await) else v
        if (
            isinstance(v, ast.Call)
            and isinstance(v.func, ast.Attribute)
            and isinstance(v.func.value, ast.Name)
            and v.func.value.id == "self"
            and v.func.attr in phase_names
        ):
            return v
        return None

    if isinstance(stmt, ast.Return) and stmt.value is not None:
        c = unwrap(stmt.value)
        if c is not None:
            return "TAIL", c.func.attr
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        c = unwrap(stmt.value)
        if c is not None:
            return "STRAIGHT", c.func.attr
    if isinstance(stmt, ast.Expr):
        c = unwrap(stmt.value)
        if c is not None:
            return "STRAIGHT", c.func.attr
    return None, None


def _inline(stmts: list[ast.stmt], phases: dict[str, ast.AST]) -> list[ast.stmt]:
    phase_names = set(phases)
    out: list[ast.stmt] = []
    for s in stmts:
        kind, name = _phase_call(s, phase_names)
        if name is not None:
            body = _strip_doc(list(phases[name].body))
            # extract_phase.py appends a synthetic ``return <outputs>`` only
            # to a STRAIGHT phase that has outputs; a TAIL phase's trailing
            # return is its own real statement and must be kept.
            if kind == "STRAIGHT" and body and isinstance(body[-1], ast.Return):
                body = body[:-1]
            out.extend(_inline(copy.deepcopy(body), phases))
            continue
        s = copy.deepcopy(s)
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(s, field, None)
            if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
                setattr(s, field, _inline(lst, phases))
        for h in getattr(s, "handlers", []) or []:
            h.body = _inline(h.body, phases)
        out.append(s)
    return out


def reinlined_process_turn_impl_source() -> str:
    """The turn path's statements in their original (pre-split) order.

    Never concatenate ``inspect.getsource`` of the phase methods to answer an
    order question — that reflects textual definition order, not the actual
    call-site interleaving. This rebuilds the real order via AST inline.
    """
    src = inspect.getsource(_engine_module)
    tree = ast.parse(src)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "MilestoneEngine"
    )
    owner = next(
        m for m in cls.body if getattr(m, "name", None) == "_process_turn_impl"
    )
    phases = {
        m.name: m for m in cls.body if getattr(m, "name", None) in PHASE_METHOD_NAMES
    }
    missing = set(PHASE_METHOD_NAMES) - set(phases)
    if missing:
        raise AssertionError(
            f"expected phase method(s) not found on MilestoneEngine: {sorted(missing)}"
        )
    rebuilt = copy.deepcopy(owner)
    rebuilt.body = _inline(owner.body, phases)
    return ast.unparse(rebuilt)

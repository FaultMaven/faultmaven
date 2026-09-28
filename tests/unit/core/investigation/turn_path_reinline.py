"""Rebuild ``_process_turn_impl``'s turn path in execution order, every phase inlined.

Wave 3 step A split ``MilestoneEngine._process_turn_impl`` into private
phase methods of the same class; step B moved those phase methods out as
module-level functions of sibling modules (``transition_turns.py``,
``turn_generation.py``, ``turn_application.py``, ``turn_completion.py``). A
source-pinning test that asserted one call precedes another across what are
now separate functions in separate files can no longer answer that from any
one function's own ``inspect.getsource`` in isolation.

This module answers it the way ``verify_inline.py``/``verify_extract.py``
prove the split itself: by substituting each phase call back into the
owner's body — an AST inline, never a source concatenation, because
concatenation would not preserve how the phases actually interleave with the
owner's own dispatch statements (the ``if``/``elif`` skeleton the split left
behind). Call ``reinlined_process_turn_impl_source()`` and run the same
``.index()``/``in`` assertions against its return value that used to run
against ``inspect.getsource(MilestoneEngine._process_turn_impl)`` directly.
"""

from __future__ import annotations

import ast
import copy
import inspect

import faultmaven.core.investigation.milestone_engine.engine as _engine_module
import faultmaven.core.investigation.milestone_engine.transition_turns as _transition_turns
import faultmaven.core.investigation.milestone_engine.turn_application as _turn_application
import faultmaven.core.investigation.milestone_engine.turn_completion as _turn_completion
import faultmaven.core.investigation.milestone_engine.turn_generation as _turn_generation

#: Every phase wave 3 extracted from ``_process_turn_impl``, and the module
#: each now lives in (step B). Kept here rather than discovered, so a future
#: phase split (or un-split) is a deliberate edit to this list rather than a
#: silent change in what "the turn path" means to these tests.
PHASE_MODULES: dict[str, object] = {
    "_confirm_pending_transition": _transition_turns,
    "_decline_bare_reply": _transition_turns,
    "_represent_pending_transition": _transition_turns,
    "_close_on_explicit_intent": _transition_turns,
    "_generate_turn_response": _turn_generation,
    "_apply_turn_response": _turn_application,
    "_persist_turn": _turn_completion,
    "_compose_turn_reply": _turn_completion,
}


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

    Mirrors the call-site shapes ``extract_members.py`` writes for a
    ``functions`` group: TAIL is ``return [await] _p(<deps>, ...)``; STRAIGHT
    is ``<targets> = [await] _p(<deps>, ...)`` or a bare ``[await] _p(...)``
    — a direct call to the (module-level, imported) phase name, not a
    ``self.`` attribute call as it was in step A.
    """

    def unwrap(v: ast.expr) -> ast.Call | None:
        v = v.value if isinstance(v, ast.Await) else v
        if (
            isinstance(v, ast.Call)
            and isinstance(v.func, ast.Name)
            and v.func.id in phase_names
        ):
            return v
        return None

    if isinstance(stmt, ast.Return) and stmt.value is not None:
        c = unwrap(stmt.value)
        if c is not None:
            return "TAIL", c.func.id
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        c = unwrap(stmt.value)
        if c is not None:
            return "STRAIGHT", c.func.id
    if isinstance(stmt, ast.Expr):
        c = unwrap(stmt.value)
        if c is not None:
            return "STRAIGHT", c.func.id
    return None, None


def _inline(stmts: list[ast.stmt], phases: dict[str, ast.AST]) -> list[ast.stmt]:
    phase_names = set(phases)
    out: list[ast.stmt] = []
    for s in stmts:
        kind, name = _phase_call(s, phase_names)
        if name is not None:
            body = _strip_doc(list(phases[name].body))
            # extract_phase.py (step A) appended a synthetic ``return
            # <outputs>`` only to a STRAIGHT phase that has outputs, and
            # extract_members.py (step B) preserved that trailing return
            # verbatim; a TAIL phase's trailing return is its own real
            # statement and must be kept.
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

    Never concatenate ``inspect.getsource`` of the phase functions to answer
    an order question — that reflects textual definition order (and, across
    files, nothing at all), not the actual call-site interleaving. This
    rebuilds the real order via AST inline.
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
    phases: dict[str, ast.AST] = {}
    missing: list[str] = []
    for name, module in PHASE_MODULES.items():
        mod_tree = ast.parse(inspect.getsource(module))
        node = next(
            (m for m in mod_tree.body if getattr(m, "name", None) == name), None
        )
        if node is None:
            missing.append(name)
        else:
            phases[name] = node
    if missing:
        raise AssertionError(
            f"expected phase function(s) not found in their step-B module: {sorted(missing)}"
        )
    rebuilt = copy.deepcopy(owner)
    rebuilt.body = _inline(owner.body, phases)
    return ast.unparse(rebuilt)

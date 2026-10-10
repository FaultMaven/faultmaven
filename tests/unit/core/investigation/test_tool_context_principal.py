"""The turn's principal reaches every tool context build.

``ToolContext.user_id`` is the turn's authenticated principal, for the tools
that record who acted (evidence reclassification). ``build_tool_context`` used
to read it off ``intent_data``, which no caller populates, so every live turn
resolved to ``"system"``. These cases pin the delivery of the principal from the
authenticated entry point, because a test that hands ``build_tool_context`` a
principal directly cannot tell whether anything upstream supplies one.

The KB scope is NOT keyed on this principal: it is the case driver's knowledge
(``case_retrieval_scope``, #1919), pinned in ``test_case_retrieval_scope.py``.
"""

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _engine():
    """A MilestoneEngine with just enough wiring to build a tool context."""
    from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
    from faultmaven.core.investigation.milestone_engine.generation import (
        StructuredOutputGenerator,
    )

    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.deps = EngineDeps()
    engine.deps.repository = MagicMock()
    engine.deps.investigation_tools = None
    engine.deps.team_service = None
    engine.deps.share_repository = None
    engine.generator = StructuredOutputGenerator(deps=engine.deps, vectorizer=None)
    return engine


def _case():
    case = MagicMock()
    case.case_id = "case_1"
    case.user_id = "case_creator"
    case.enterprise_id = "ent_1"
    case.progress = None
    return case


# ---------------------------------------------------------------------------
# Delivery of the principal.
#
# These pin the two hops the principal has to survive, as properties over
# every call site rather than as one sampled instance, so a newly added dispatch
# branch that forgets to thread it fails here.
# ---------------------------------------------------------------------------


def _module_source(module) -> str:
    """``inspect.getsource``, widened for a package (fm#1707).

    ``milestone_engine`` is now a package: ``inspect.getsource`` on the
    package object returns only ``__init__.py``, silently dropping any call
    site that lives in one of its submodules.
    """
    path = getattr(module, "__file__", None)
    if path and Path(path).name == "__init__.py":
        pkg_dir = Path(path).parent
        return "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(pkg_dir.glob("*.py"))
        )
    return inspect.getsource(module)


def _call_sites(module, attr_suffix: tuple[str, ...]) -> list[ast.Call]:
    """Every ``ast.Call`` in ``module`` whose callee ENDS in ``attr_suffix``.

    A suffix match, not an exact one: #1707 wave 3 step B moved
    ``build_tool_context``'s caller out as a module function, where the
    collaborator arrives as a bare parameter (``generator.build_tool_context``)
    rather than through ``self`` (``self.generator.build_tool_context``, still
    the shape at the surviving method call sites). Both name the same call.
    """
    tree = ast.parse(_module_source(module))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts, cur = [], node.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        callee = tuple(reversed(parts))
        if callee[-len(attr_suffix) :] == attr_suffix:
            found.append(node)
    return found


def test_the_principal_reaches_every_tool_context_build():
    """Hop 2: engine → ``build_tool_context``.

    A call site that omits ``user_id`` silently gets the ``"system"`` sentinel,
    and a tool that records who acted attributes the act to nobody, with no
    error anywhere.

    #1707: the method moved to ``StructuredOutputGenerator`` and lost its
    leading underscore (called from outside the collaborator), so every call
    site is now two hops — ``generator.build_tool_context`` — rather than
    one. Wave 3 step B moved one such caller (``_generate_turn_response``)
    out of the engine class as a module function, where the collaborator
    arrives as the bare parameter ``generator`` rather than ``self.generator``
    — ``_call_sites`` matches the ``("generator", "build_tool_context")``
    suffix so both shapes count as the same call.
    """
    from faultmaven.core.investigation import milestone_engine

    calls = _call_sites(milestone_engine, ("generator", "build_tool_context"))
    assert calls, "no build_tool_context call sites found — did it get renamed?"
    for call in calls:
        assert any(kw.arg == "user_id" for kw in call.keywords), (
            f"milestone_engine builds a ToolContext at line {call.lineno} without "
            "passing user_id; its tools would attribute acts to 'system'"
        )


def test_the_principal_reaches_every_engine_turn():
    """Hop 1: ``InvestigationService`` → ``engine.process_turn``.

    The service is the only holder of the authenticated principal (the route
    passes ``current_user.user_id``); the engine cannot recover it from the
    case, which names the *owner*, not this turn's reader.
    """
    from faultmaven.modules.agent.domain.services import investigation_service

    calls = _call_sites(investigation_service, ("self", "engine", "process_turn"))
    assert calls, "no engine.process_turn call sites found — did it get renamed?"
    for call in calls:
        assert any(kw.arg == "user_id" for kw in call.keywords), (
            f"investigation_service.py:{call.lineno} dispatches a turn without "
            "the authenticated principal; its tools lose who acted"
        )


async def test_the_principal_is_not_taken_from_the_client_intent_payload():
    """``intent_data`` is built from the client-supplied intent; it must not be
    able to name the principal a tool acts for."""
    engine = _engine()

    # The shape a client could forge if the principal came from the payload.
    context = await engine.generator.build_tool_context(_case(), user_id=None)

    assert context.user_id == "system"
    assert (
        "intent_data"
        not in inspect.signature(engine.generator.build_tool_context).parameters
    )

"""The tool path's KB read allowlist must carry its team arm (ADR-013 §D4).

``kb_qa`` builds its vector-store filter from two things it reads off the
``ToolContext``: ``user_id`` for the owner arm and ``shared_kb_ids`` for the team
arm. The engine populated the first and not the second, so ``build_kb_scope_filter``
dropped the team arm on every turn and a team-shared runbook was invisible to the
agent — while ``_prefetch_kb_context``, resolving the same allowlist for the KB
cause-seeder, could see it. Two KB paths, two different answers to "what may this
principal read".

That failed *closed*, so this is a lost-capability bug rather than an exposure one.
The direction still matters, which is why the narrowing cases below are asserted as
hard as the widening one: a wrong turn here is a cross-tenant read.

These assert at the surface that renders the value — ``build_kb_scope_filter``'s
actual output, the dict handed to ChromaDB — rather than on ``context.shared_kb_ids``
alone. Asserting the field is set would pass even if nothing downstream consumed it,
which is exactly how the gap survived.

Both arms are keyed on one principal, and the whole allowlist is worthless if that
principal never arrives: ``_build_tool_context`` used to read ``user_id`` off
``intent_data``, which no caller populates, so every live turn resolved to
``"system"`` and *both* arms collapsed to the global corpus. The
``test_the_principal_reaches_*`` cases below pin the delivery of the principal from
the authenticated entry point, because a test that hands ``_build_tool_context`` a
principal directly cannot tell whether anything upstream supplies one.
"""

import ast
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    build_kb_scope_filter,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _engine(team_ids=None, shared_ids=None, *, wired=True):
    """A MilestoneEngine with just enough wiring to build a tool context."""
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.repository = MagicMock()
    engine.investigation_tools = None

    if wired:
        engine.team_service = MagicMock()
        engine.team_service.list_all_user_team_ids = AsyncMock(
            return_value=list(team_ids or [])
        )
        engine.share_repository = MagicMock()
        engine.share_repository.list_resource_ids = AsyncMock(
            return_value=list(shared_ids or [])
        )
    else:
        engine.team_service = None
        engine.share_repository = None
    return engine


def _case(enterprise_id="ent_1"):
    case = MagicMock()
    case.case_id = "case_1"
    case.enterprise_id = enterprise_id
    case.progress = None
    return case


def _team_arm(scope_filter):
    """The ``parent_document_id`` arm of the filter, or None if absent."""
    arms = scope_filter.get("$or", [scope_filter])
    for arm in arms:
        if "parent_document_id" in arm:
            return arm["parent_document_id"]["$in"]
    return None


async def test_team_shared_ids_reach_the_scope_filter():
    """The widening case: a shared item must be readable by the tool."""
    engine = _engine(team_ids=["team_a"], shared_ids=["kb_shared_1", "kb_shared_2"])

    context = await engine._build_tool_context(_case(), user_id="user_a")

    # The filter kb_qa will build from this context, exactly as kb_tool_adapter
    # passes it through.
    scope_filter = build_kb_scope_filter(context.user_id, context.shared_kb_ids)

    assert _team_arm(scope_filter) == ["kb_shared_1", "kb_shared_2"], (
        "team-shared KB items are not reaching the vector-store filter, so the "
        "agent's kb_qa tool cannot read them"
    )


async def test_the_team_arm_is_keyed_on_the_session_user():
    """Not the case owner — both arms of one filter describe one principal.

    ``kb_tool_adapter`` passes ``ToolContext.user_id`` as the owner arm. If the
    team arm resolved the *case owner's* teams, a collaborator's turn would read
    items shared to teams they may not belong to.
    """
    engine = _engine(team_ids=["team_of_session_user"], shared_ids=["kb_1"])
    case = _case()
    case.user_id = "case_owner"  # deliberately different from the session user

    await engine._build_tool_context(case, user_id="session_user")

    engine.team_service.list_all_user_team_ids.assert_awaited_once_with("session_user")


async def test_the_enterprise_is_threaded_into_the_share_lookup():
    """The share resolution must stay tenant-scoped — on the ENTERPRISE, which
    is what isolates (ADR-017 D1). A team may span two organizations inside one
    enterprise, so scoping the lookup by organization would hide a share the
    requester is entitled to; scoping it by nothing would cross the wall."""
    engine = _engine(team_ids=["team_a"], shared_ids=["kb_1"])

    await engine._build_tool_context(_case(enterprise_id="ent_xyz"), user_id="user_a")

    _, kwargs = engine.share_repository.list_resource_ids.await_args
    assert (
        kwargs.get("enterprise_id") == "ent_xyz"
    ), f"share lookup was not scoped to the case's enterprise: {kwargs}"
    assert kwargs.get("resource_type") == "knowledge_item"
    assert kwargs.get("scope_type") == "team"


@pytest.mark.parametrize("user_id", ["system", "", None])
async def test_no_principal_means_no_team_arm(user_id):
    """``system`` is not a user; it must not resolve anyone's team allowlist."""
    engine = _engine(team_ids=["team_a"], shared_ids=["kb_1"])

    context = await engine._build_tool_context(_case(), user_id=user_id)

    assert context.shared_kb_ids == []
    engine.team_service.list_all_user_team_ids.assert_not_awaited()
    assert _team_arm(build_kb_scope_filter(context.user_id, context.shared_kb_ids)) is (
        None
    )


async def test_standalone_without_team_services_collapses_to_owned_and_global():
    """Missing collaborators narrow the allowlist; they never widen it."""
    engine = _engine(wired=False)

    context = await engine._build_tool_context(_case(), user_id="user_a")

    assert context.shared_kb_ids == []
    assert _team_arm(build_kb_scope_filter(context.user_id, context.shared_kb_ids)) is (
        None
    )


async def test_a_failed_resolution_narrows_rather_than_raising():
    """A share-table fault must not fail the turn, and must not widen the read."""
    engine = _engine(team_ids=["team_a"])
    engine.share_repository.list_resource_ids = AsyncMock(
        side_effect=RuntimeError("share table unavailable")
    )

    context = await engine._build_tool_context(_case(), user_id="user_a")

    assert context.shared_kb_ids == []
    assert _team_arm(build_kb_scope_filter(context.user_id, context.shared_kb_ids)) is (
        None
    )


# ---------------------------------------------------------------------------
# Delivery of the principal.
#
# Everything above hands ``_build_tool_context`` a principal. That proves the
# allowlist is built correctly *given* one, and proves nothing about whether a
# real turn ever supplies one — the failure that made the original team-arm fix
# inert. These pin the two hops the principal has to survive, as properties over
# every call site rather than as one sampled instance, so a newly added dispatch
# branch that forgets to thread it fails here.
# ---------------------------------------------------------------------------


def _call_sites(module, attr_path: tuple[str, ...]) -> list[ast.Call]:
    """Every ``ast.Call`` in ``module`` whose callee is ``attr_path``."""
    tree = ast.parse(inspect.getsource(module))
    wanted = ".".join(attr_path)
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
        if ".".join(reversed(parts)) == wanted:
            found.append(node)
    return found


def test_the_principal_reaches_every_tool_context_build():
    """Hop 2: engine → ``_build_tool_context``.

    A call site that omits ``user_id`` silently gets the ``"system"`` sentinel,
    which owns nothing and belongs to no team — the KB tool then reads the
    global corpus only, with no error anywhere.
    """
    from faultmaven.core.investigation import milestone_engine

    calls = _call_sites(milestone_engine, ("self", "_build_tool_context"))
    assert calls, "no _build_tool_context call sites found — did it get renamed?"
    for call in calls:
        assert any(kw.arg == "user_id" for kw in call.keywords), (
            f"milestone_engine.py:{call.lineno} builds a ToolContext without "
            "passing user_id; its KB tool would read global-only"
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
            "the authenticated principal; the agent's KB tool loses its "
            "owner and team arms"
        )


async def test_the_principal_is_not_taken_from_the_client_intent_payload():
    """``intent_data`` is built from the client-supplied intent; it must not be
    able to name the principal whose KB the agent reads."""
    engine = _engine(team_ids=["team_a"], shared_ids=["kb_1"])

    # The shape a client could forge if the principal came from the payload.
    context = await engine._build_tool_context(_case(), user_id=None)

    assert context.user_id == "system"
    assert context.shared_kb_ids == []
    assert "intent_data" not in inspect.signature(engine._build_tool_context).parameters

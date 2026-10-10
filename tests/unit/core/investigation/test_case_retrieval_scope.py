"""A case's KB retrieval scope is the case driver's knowledge (#1919).

Owner ruling on #1919 (2026-10-10): retrieval applies the knowledge of the
person driving the investigation, global ∪ the driver's personal KB ∪ the
runbooks shared to the driver's teams. Until #1898 adds a driver the driver is
the creator, ``cases.user_id``. ``case_retrieval_scope(case)`` decides it, and
the pre-fetch (push), the ``kb_qa`` tool context (pull) and the runbook dedup
all take their scope from it. Stored copies of what it retrieved are gated per
viewer when read back (``test_kb_source_gate.py``).

The first half pins the function's arms and failure modes. The second half
drives each call site: each of those tests fails if its call site keys on
anything but the case, notably on the turn's user.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.retrieval_scope import (
    case_retrieval_scope,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENT = "ent-1"
DRIVER = "user-driver"
TEAMMATE = "user-teammate"

T_DRIVER = "team-of-driver"
T_TEAMMATE = "team-of-teammate"

KB_DRIVER_TEAM = "kb-of-driver-team"
KB_TEAMMATE_TEAM = "kb-of-teammate-team"

GLOBAL = {"scope": "global"}
DRIVER_SCOPE = {
    "$or": [
        GLOBAL,
        {"owner_id": DRIVER},
        {"parent_document_id": {"$in": [KB_DRIVER_TEAM]}},
    ]
}
DRIVER_PERSONAL_ONLY = {"$or": [GLOBAL, {"owner_id": DRIVER}]}


class _Shares:
    """``IShareRepository.list_resource_ids`` double: one runbook per team."""

    def __init__(self, *, fail: bool = False) -> None:
        self.kb_by_team = {T_DRIVER: KB_DRIVER_TEAM, T_TEAMMATE: KB_TEAMMATE_TEAM}
        self.fail = fail
        self.lookups: List[Dict[str, Any]] = []

    async def list_resource_ids(
        self,
        *,
        resource_type: str,
        scope_type: str,
        scope_ids: List[str],
        enterprise_id: str,
    ) -> List[str]:
        self.lookups.append(
            {
                "resource_type": resource_type,
                "scope_type": scope_type,
                "scope_ids": list(scope_ids),
                "enterprise_id": enterprise_id,
            }
        )
        if self.fail:
            raise RuntimeError("share table unavailable")
        if enterprise_id != ENT:
            return []
        return [self.kb_by_team[t] for t in scope_ids if t in self.kb_by_team]


class _Teams:
    def __init__(self, *, fail: bool = False) -> None:
        self.memberships = {DRIVER: [T_DRIVER], TEAMMATE: [T_TEAMMATE]}
        self.fail = fail
        self.lookups: List[str] = []

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        self.lookups.append(user_id)
        if self.fail:
            raise RuntimeError("team lookup failed")
        return list(self.memberships.get(user_id, []))


def _case(user_id: Optional[str] = DRIVER) -> Any:
    return SimpleNamespace(
        case_id="case-1",
        user_id=user_id,
        enterprise_id=ENT,
        progress=None,
        kb_context=None,
    )


# ---------------------------------------------------------------------------
# The function's arms
# ---------------------------------------------------------------------------


async def test_the_scope_is_the_drivers_personal_and_team_knowledge():
    teams, shares = _Teams(), _Shares()

    scope = await case_retrieval_scope(
        _case(), team_service=teams, share_repository=shares
    )

    assert scope == DRIVER_SCOPE
    assert teams.lookups == [DRIVER]
    assert shares.lookups == [
        {
            "resource_type": "knowledge_item",
            "scope_type": "team",
            "scope_ids": [T_DRIVER],
            "enterprise_id": ENT,
        }
    ]


async def test_standalone_reads_global_and_the_drivers_personal_kb():
    """``team_service`` is None only in standalone."""
    scope = await case_retrieval_scope(
        _case(), team_service=None, share_repository=_Shares()
    )

    assert scope == DRIVER_PERSONAL_ONLY


async def test_a_case_without_a_driver_reads_global_only():
    """``cases.user_id`` is set NULL when the creator's account is deleted."""
    teams = _Teams()

    scope = await case_retrieval_scope(
        _case(user_id=None), team_service=teams, share_repository=_Shares()
    )

    assert scope == GLOBAL
    assert teams.lookups == []


@pytest.mark.parametrize("failing", ["teams", "shares"])
async def test_a_failed_team_lookup_narrows_to_the_drivers_personal_kb(failing):
    scope = await case_retrieval_scope(
        _case(),
        team_service=_Teams(fail=failing == "teams"),
        share_repository=_Shares(fail=failing == "shares"),
    )

    assert scope == DRIVER_PERSONAL_ONLY


@pytest.mark.parametrize("failing", ["teams", "shares"])
async def test_raise_on_failure_raises_instead_of_narrowing(failing):
    with pytest.raises(RuntimeError):
        await case_retrieval_scope(
            _case(),
            team_service=_Teams(fail=failing == "teams"),
            share_repository=_Shares(fail=failing == "shares"),
            raise_on_failure=True,
        )


# ---------------------------------------------------------------------------
# Each call site takes the case's scope
# ---------------------------------------------------------------------------


def _deps(teams: _Teams, shares: _Shares):
    from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps

    deps = EngineDeps()
    deps.team_service = teams
    deps.share_repository = shares
    deps.repository = MagicMock()
    deps.investigation_tools = None
    return deps


class _SearchRecorder:
    def __init__(self) -> None:
        self.filters_seen: List[Optional[Dict[str, Any]]] = []

    async def search_knowledge(
        self, query, limit=10, filters=None, use_hybrid=False, min_score=None
    ):
        self.filters_seen.append(filters)
        return [
            SimpleNamespace(
                title="t",
                snippet="s",
                score=0.9,
                document_type="runbook",
                parent_document_id="rb1",
            )
        ]


async def test_push_the_prefetch_searches_the_drivers_scope():
    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _deps(_Teams(), _Shares())
    deps.knowledge_service = _SearchRecorder()

    await KbPrefetcher(deps=deps).prefetch_kb_context(_case(), "X fails", "symptom")

    assert deps.knowledge_service.filters_seen == [DRIVER_SCOPE]


async def test_push_a_failed_team_lookup_narrows_rather_than_widens():
    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _deps(_Teams(fail=True), _Shares())
    deps.knowledge_service = _SearchRecorder()

    await KbPrefetcher(deps=deps).prefetch_kb_context(_case(), "X fails", "symptom")

    assert deps.knowledge_service.filters_seen == [DRIVER_PERSONAL_ONLY]


def _generator(teams: _Teams, shares: _Shares):
    from faultmaven.core.investigation.milestone_engine.generation import (
        StructuredOutputGenerator,
    )

    return StructuredOutputGenerator(deps=_deps(teams, shares), vectorizer=None)


async def test_pull_the_tool_context_carries_the_cases_scope_not_the_turn_users():
    """Keyed on the case: a turn submitted by anyone else (an engine-internal
    turn, or after #1898 a driver who is not the creator through a code path
    that forgot to re-key) still reads the case's scope, never the user's."""
    generator = _generator(_Teams(), _Shares())

    context = await generator.build_tool_context(_case(), user_id=TEAMMATE)

    assert context.kb_scope_filter == DRIVER_SCOPE
    assert context.user_id == TEAMMATE


async def test_pull_an_engine_internal_turn_still_gets_the_cases_scope():
    generator = _generator(_Teams(), _Shares())

    context = await generator.build_tool_context(_case(), user_id=None)

    assert context.user_id == "system"
    assert context.kb_scope_filter == DRIVER_SCOPE


async def test_pull_the_kb_tool_searches_exactly_the_context_scope():
    """The adapter forwards the context's scope; it builds none of its own."""
    from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter

    context = await _generator(_Teams(), _Shares()).build_tool_context(
        _case(), user_id=DRIVER
    )
    wrapped = MagicMock()
    wrapped._arun = AsyncMock(return_value="answer")

    result = await KBToolAdapter(wrapped).execute_with_context(
        {"question": "q"}, context
    )

    assert result.success, result.error
    wrapped._arun.assert_awaited_once_with(question="q", scope_filter=DRIVER_SCOPE, k=5)


def _runbook_creator(teams: _Teams, shares: _Shares):
    from faultmaven.core.investigation.milestone_engine.runbook_creation import (
        RunbookCreator,
    )

    return RunbookCreator(case_locks={}, deps=_deps(teams, shares))


async def test_dedup_the_resolver_returns_the_drivers_scope():
    scope = await _runbook_creator(_Teams(), _Shares())._runbook_dedup_scope_resolver(
        _case()
    )()

    assert scope == DRIVER_SCOPE


async def test_dedup_the_resolver_raises_rather_than_narrowing():
    """A narrowed dedup would back a "nothing similar" claim it never checked."""
    resolver = _runbook_creator(
        _Teams(fail=True), _Shares()
    )._runbook_dedup_scope_resolver(_case())

    with pytest.raises(RuntimeError, match="team lookup failed"):
        await resolver()

"""A case's KB retrieval scope is the case driver's knowledge (#1919).

Owner ruling on #1919 (2026-10-10): retrieval applies the knowledge of the
person driving the investigation, global ∪ the driver's personal KB ∪ the
runbooks shared to the driver's teams. The driver is the case's EFFECTIVE
driver (ADR-020): ``COALESCE(driver_id, user_id)``, the creator unless the case
was handed to someone. ``case_retrieval_scope(case)`` decides it, and
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

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.core.investigation.milestone_engine.retrieval_scope import (
    case_retrieval_scope,
)
from faultmaven.core.investigation.prompts.templates.tool_text import NO_TOOLS
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI
from tests.model_content import disabled_redaction
from tests.utils import admit_every_parent

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENT = "ent-1"
DRIVER = "user-driver"
TEAMMATE = "user-teammate"

T_DRIVER = "team-of-driver"
T_TEAMMATE = "team-of-teammate"

KB_DRIVER_TEAM = "kb-of-driver-team"
KB_TEAMMATE_TEAM = "kb-of-teammate-team"

# Every arm carries the case's enterprise (#1775); the global arm also admits
# the platform value the global tier is stamped with.
GLOBAL = {
    "$and": [
        {"scope": "global"},
        {"enterprise_id": {"$in": [ENT, STANDALONE_ENTERPRISE_ID]}},
    ]
}
DRIVER_SCOPE = {
    "$or": [
        GLOBAL,
        {"$and": [{"owner_id": DRIVER}, {"enterprise_id": ENT}]},
        {
            "$and": [
                {"parent_document_id": {"$in": [KB_DRIVER_TEAM]}},
                {"enterprise_id": ENT},
            ]
        },
    ]
}
DRIVER_PERSONAL_ONLY = {
    "$or": [GLOBAL, {"$and": [{"owner_id": DRIVER}, {"enterprise_id": ENT}]}]
}
#: A case with no usable enterprise: global chunks stamped with the platform
#: value, and nothing keyed on the driver.
PLATFORM_TIER = {
    "$and": [{"scope": "global"}, {"enterprise_id": STANDALONE_ENTERPRISE_ID}]
}


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


def _case(user_id: Optional[str] = DRIVER, enterprise_id: Optional[str] = ENT) -> Any:
    return SimpleNamespace(
        case_id="case-1",
        user_id=user_id,
        enterprise_id=enterprise_id,
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


def _unusable_enterprises():
    """A case enterprise ``usable_tenant_id`` refuses: none, or the Standalone
    sentinel under ``TENANT_PROVIDER=multi``."""
    return [
        pytest.param(None, None, id="none"),
        pytest.param(STANDALONE_ENTERPRISE_ID, BUILTIN_MULTI, id="sentinel-multi"),
    ]


@pytest.mark.parametrize("enterprise_id,provider", _unusable_enterprises())
async def test_no_usable_enterprise_reads_the_platform_tier_alone(
    monkeypatch, enterprise_id, provider
):
    """The pre-fetch degrades (#1775): never the driver's own chunks without a
    tenant to pin them to, and no team lookup that would be thrown away."""
    if provider:
        monkeypatch.setattr(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            lambda: provider,
        )
    teams, shares = _Teams(), _Shares()

    scope = await case_retrieval_scope(
        _case(enterprise_id=enterprise_id), team_service=teams, share_repository=shares
    )

    assert scope == PLATFORM_TIER
    assert teams.lookups == [] and shares.lookups == []


@pytest.mark.parametrize("enterprise_id,provider", _unusable_enterprises())
async def test_raise_on_failure_refuses_a_case_with_no_usable_enterprise(
    monkeypatch, enterprise_id, provider
):
    """P1 of the #1775 critique: the dedup and the KB-question lookup refuse
    rather than search a platform tier that never saw the driver's runbooks."""
    if provider:
        monkeypatch.setattr(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            lambda: provider,
        )

    teams = _Teams()

    with pytest.raises(RuntimeError, match="no usable enterprise"):
        await case_retrieval_scope(
            _case(enterprise_id=enterprise_id),
            team_service=teams,
            share_repository=None,
            raise_on_failure=True,
        )
    assert teams.lookups == []


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

    async def search_knowledge(self, query, limit=10, filters=None, min_score=None):
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

    context = await generator.build_tool_context(
        _case(),
        user_id=TEAMMATE,
        redaction_ctx=disabled_redaction(),
        given_tools=NO_TOOLS,
    )

    assert context.kb_scope_filter == DRIVER_SCOPE
    assert context.user_id == TEAMMATE


async def test_pull_an_engine_internal_turn_still_gets_the_cases_scope():
    generator = _generator(_Teams(), _Shares())

    context = await generator.build_tool_context(
        _case(), user_id=None, redaction_ctx=disabled_redaction(), given_tools=NO_TOOLS
    )

    assert context.user_id == "system"
    assert context.kb_scope_filter == DRIVER_SCOPE


async def test_pull_the_kb_tool_searches_exactly_the_context_scope():
    """The adapter forwards the context's scope; it builds none of its own."""
    from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter

    redaction = disabled_redaction()
    context = await _generator(_Teams(), _Shares()).build_tool_context(
        _case(),
        user_id=DRIVER,
        redaction_ctx=redaction,
        given_tools=NO_TOOLS,
    )
    wrapped = MagicMock()
    wrapped.answer = AsyncMock(return_value=("answer", []))

    result = await KBToolAdapter(
        wrapped, admit=admit_every_parent
    ).execute_with_context({"question": "q"}, context)

    assert result.success, result.error
    wrapped.answer.assert_awaited_once_with(
        question="q",
        scope_filter=DRIVER_SCOPE,
        k=5,
        redaction_ctx=redaction,
        admit=admit_every_parent,
    )


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


# ---------------------------------------------------------------------------
# After a hand-off (ADR-020 D9): every arm keys on the DRIVER, not the creator
# ---------------------------------------------------------------------------


def _handed_off_case() -> Any:
    """TEAMMATE created the case and handed it to DRIVER."""
    case = _case(user_id=TEAMMATE)
    case.driver_id = DRIVER
    case.effective_driver_id = DRIVER
    return case


async def test_the_scope_follows_the_driver_not_the_creator():
    assert (
        await case_retrieval_scope(
            _handed_off_case(), team_service=_Teams(), share_repository=_Shares()
        )
        == DRIVER_SCOPE
    )


async def test_push_after_a_hand_off_searches_the_drivers_scope_and_stamps_it():
    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _deps(_Teams(), _Shares())
    deps.knowledge_service = _SearchRecorder()
    case = _handed_off_case()

    await KbPrefetcher(deps=deps).prefetch_kb_context(case, "X fails", "symptom")

    assert deps.knowledge_service.filters_seen == [DRIVER_SCOPE]
    assert case.kb_context_origin["driver_id"] == DRIVER, "stamp and fetch agree"


async def test_pull_after_a_hand_off_carries_the_drivers_scope():
    context = await _generator(_Teams(), _Shares()).build_tool_context(
        _handed_off_case(),
        user_id=DRIVER,
        redaction_ctx=disabled_redaction(),
        given_tools=NO_TOOLS,
    )

    assert context.kb_scope_filter == DRIVER_SCOPE


async def test_pull_kb_qa_refuses_for_a_case_with_no_usable_enterprise():
    """#1775: ``kb_qa`` refuses rather than answer from the platform tier as if
    it were the driver's knowledge. The context carries no scope, so the
    adapter's refusal answers and no search runs; the pre-fetch for the same
    case still reads the platform tier."""
    from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter

    case = _case(enterprise_id=None)
    context = await _generator(_Teams(), _Shares()).build_tool_context(
        case, user_id=DRIVER, redaction_ctx=disabled_redaction(), given_tools=NO_TOOLS
    )
    wrapped = MagicMock()
    wrapped.answer = AsyncMock(return_value=("answer", []))

    result = await KBToolAdapter(
        wrapped, admit=admit_every_parent
    ).execute_with_context({"question": "q"}, context)

    assert context.kb_scope_filter is None
    assert not result.success
    assert "Knowledge base query refused" in result.error
    wrapped.answer.assert_not_awaited()

    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _deps(_Teams(), _Shares())
    deps.knowledge_service = _SearchRecorder()
    await KbPrefetcher(deps=deps).prefetch_kb_context(case, "X fails", "symptom")
    assert deps.knowledge_service.filters_seen == [PLATFORM_TIER]


async def test_dedup_the_resolver_refuses_a_case_with_no_usable_enterprise():
    resolver = _runbook_creator(_Teams(), _Shares())._runbook_dedup_scope_resolver(
        _case(enterprise_id=None)
    )

    with pytest.raises(RuntimeError, match="no usable enterprise"):
        await resolver()


async def test_dedup_after_a_hand_off_resolves_the_drivers_scope():
    scope = await _runbook_creator(_Teams(), _Shares())._runbook_dedup_scope_resolver(
        _handed_off_case()
    )()

    assert scope == DRIVER_SCOPE

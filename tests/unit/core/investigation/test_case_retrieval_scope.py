"""A case's KB retrieval scope is the case's audience, not a user's (#1919).

What a turn retrieves from the knowledge base reaches the case transcript, and
every reader of the case reads that transcript (ADR-013 D4). The rule (owner
ruling on #1898, 2026-10-09):

- an UNSHARED case searches global ∪ the creator's personal KB ∪ the runbooks
  shared to the creator's teams (unchanged);
- a SHARED case searches global ∪ the runbooks shared to the teams the case is
  shared with, and nobody's personal KB.

``case_retrieval_scope`` decides it, and three call sites take their scope from
it: the pre-fetch (push), the ``kb_qa`` tool context (pull) and the runbook
dedup. The first half of this file pins the function's arms and its failure
modes. The second half drives each call site with a shared case: each of those
tests fails if its call site goes back to keying on a user.

The scenario: the creator belongs to ``T_SHARED`` (the case is shared to it)
and ``T_OTHER`` (it is not). The case is also shared to ``T_RETIRED``, a team
whose last member left. Each team has one runbook shared to it.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.retrieval_scope import (
    case_retrieval_scope,
)
from faultmaven.models.interfaces_sharing import ResourceShare

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENT = "ent-1"
CREATOR = "user-creator"
CASE_ID = "case-1"

T_SHARED = "team-shared"
T_OTHER = "team-other"
T_RETIRED = "team-retired"

KB_SHARED = "kb-of-shared-team"
KB_OTHER = "kb-of-other-team"
KB_RETIRED = "kb-of-retired-team"

GLOBAL = {"scope": "global"}
CREATOR_ARM = {"owner_id": CREATOR}


class _Shares:
    """An ``IShareRepository`` double holding case shares and KB shares."""

    def __init__(
        self,
        case_teams: List[str],
        *,
        fail_case_lookup: bool = False,
        fail_kb_lookup: bool = False,
    ) -> None:
        self.case_teams = list(case_teams)
        self.kb_by_team = {
            T_SHARED: KB_SHARED,
            T_OTHER: KB_OTHER,
            T_RETIRED: KB_RETIRED,
        }
        self.fail_case_lookup = fail_case_lookup
        self.fail_kb_lookup = fail_kb_lookup
        self.kb_lookups: List[Dict[str, Any]] = []

    async def list_scopes_for_resource(
        self, resource_type: str, resource_id: str
    ) -> List[ResourceShare]:
        if self.fail_case_lookup:
            raise RuntimeError("share table unavailable")
        assert (resource_type, resource_id) == ("case", CASE_ID)
        return [
            ResourceShare(
                share_id=f"s-{team}",
                resource_type="case",
                resource_id=CASE_ID,
                scope_type="team",
                scope_id=team,
                enterprise_id=ENT,
            )
            for team in self.case_teams
        ]

    async def list_resource_ids(
        self,
        *,
        resource_type: str,
        scope_type: str,
        scope_ids: List[str],
        enterprise_id: str,
    ) -> List[str]:
        self.kb_lookups.append(
            {
                "resource_type": resource_type,
                "scope_type": scope_type,
                "scope_ids": list(scope_ids),
                "enterprise_id": enterprise_id,
            }
        )
        if self.fail_kb_lookup:
            raise RuntimeError("share table unavailable")
        if enterprise_id != ENT:
            return []
        return [self.kb_by_team[t] for t in scope_ids if t in self.kb_by_team]


class _Teams:
    """A team-service double: the creator's memberships and the live teams."""

    def __init__(self, *, fail_membership: bool = False) -> None:
        self.memberships = {CREATOR: [T_SHARED, T_OTHER]}
        self.live = {T_SHARED, T_OTHER}
        self.fail_membership = fail_membership
        self.membership_lookups: List[str] = []

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        self.membership_lookups.append(user_id)
        if self.fail_membership:
            raise RuntimeError("team lookup failed")
        return list(self.memberships.get(user_id, []))

    async def name_teams(self, *, enterprise_id: str, team_ids: List[str]) -> dict:
        if enterprise_id != ENT:
            return {}
        return {t: f"Team {t}" for t in team_ids if t in self.live}


def _case(user_id: Optional[str] = CREATOR, enterprise_id: str = ENT) -> Any:
    return SimpleNamespace(
        case_id=CASE_ID,
        user_id=user_id,
        enterprise_id=enterprise_id,
        progress=None,
        kb_context=None,
    )


def _arms(scope_filter: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(scope_filter.get("$or", [scope_filter]))


def _team_arm(scope_filter: Dict[str, Any]) -> Optional[List[str]]:
    for arm in _arms(scope_filter):
        if "parent_document_id" in arm:
            return list(arm["parent_document_id"]["$in"])
    return None


SHARED_CASE_SCOPE = {
    "$or": [GLOBAL, {"parent_document_id": {"$in": [KB_SHARED]}}],
}
UNSHARED_CASE_SCOPE = {
    "$or": [
        GLOBAL,
        CREATOR_ARM,
        {"parent_document_id": {"$in": [KB_SHARED, KB_OTHER]}},
    ],
}


def _assert_shared_case_scope(scope_filter: Dict[str, Any]) -> None:
    """The shared-case scope, with a reason for each way it can be wrong."""
    owner_arms = [arm for arm in _arms(scope_filter) if "owner_id" in arm]
    assert owner_arms == [], (
        f"a shared case searched a personal KB ({owner_arms}): its excerpts "
        "reach every team the case is shared with"
    )
    team_arm = _team_arm(scope_filter) or []
    assert KB_OTHER not in team_arm, (
        "a shared case searched a team the creator is in but the case is not "
        "shared with"
    )
    assert KB_RETIRED not in team_arm, "a retired team's runbooks reached the case"
    assert team_arm == [KB_SHARED], (
        "a shared case did not search the runbooks of the team it is shared "
        f"with: {scope_filter}"
    )
    assert scope_filter == SHARED_CASE_SCOPE


# ---------------------------------------------------------------------------
# The function's arms
# ---------------------------------------------------------------------------


async def test_a_shared_case_searches_global_and_its_teams_runbooks_only():
    teams = _Teams()
    scope = await case_retrieval_scope(
        _case(),
        team_service=teams,
        share_repository=_Shares([T_SHARED, T_RETIRED]),
    )

    _assert_shared_case_scope(scope)
    assert (
        teams.membership_lookups == []
    ), "a shared case's scope must not be derived from anyone's memberships"


async def test_an_unshared_case_keeps_the_creators_scope():
    teams = _Teams()
    shares = _Shares([])

    scope = await case_retrieval_scope(
        _case(), team_service=teams, share_repository=shares
    )

    assert scope == UNSHARED_CASE_SCOPE
    assert teams.membership_lookups == [CREATOR]
    assert shares.kb_lookups == [
        {
            "resource_type": "knowledge_item",
            "scope_type": "team",
            "scope_ids": [T_SHARED, T_OTHER],
            "enterprise_id": ENT,
        }
    ]


async def test_standalone_searches_global_and_the_creators_personal_kb():
    """``team_service`` is None only in standalone, where nothing is shared."""
    shares = MagicMock()
    scope = await case_retrieval_scope(
        _case(), team_service=None, share_repository=shares
    )

    assert scope == {"$or": [GLOBAL, CREATOR_ARM]}
    shares.list_scopes_for_resource.assert_not_called()


async def test_an_unshared_case_without_a_creator_searches_global_only():
    """``cases.user_id`` is set NULL when the creator's account is deleted."""
    teams = _Teams()
    scope = await case_retrieval_scope(
        _case(user_id=None), team_service=teams, share_repository=_Shares([])
    )

    assert scope == GLOBAL
    assert teams.membership_lookups == []


async def test_a_case_shared_only_to_a_retired_team_reads_no_team_and_no_person():
    scope = await case_retrieval_scope(
        _case(), team_service=_Teams(), share_repository=_Shares([T_RETIRED])
    )

    assert scope == GLOBAL


async def test_the_case_team_arm_is_scoped_to_the_cases_enterprise():
    shares = _Shares([T_SHARED])
    await case_retrieval_scope(_case(), team_service=_Teams(), share_repository=shares)

    assert shares.kb_lookups == [
        {
            "resource_type": "knowledge_item",
            "scope_type": "team",
            "scope_ids": [T_SHARED],
            "enterprise_id": ENT,
        }
    ]


# ---------------------------------------------------------------------------
# Failure narrows; it never widens to a personal KB
# ---------------------------------------------------------------------------


async def test_a_failed_case_share_lookup_falls_back_to_global_only():
    """Whether the case is shared is unknown, so no personal arm is safe."""
    teams = _Teams()
    scope = await case_retrieval_scope(
        _case(),
        team_service=teams,
        share_repository=_Shares([T_SHARED], fail_case_lookup=True),
    )

    assert scope == GLOBAL
    assert teams.membership_lookups == []


async def test_a_failed_team_arm_on_a_shared_case_falls_back_to_global_only():
    scope = await case_retrieval_scope(
        _case(),
        team_service=_Teams(),
        share_repository=_Shares([T_SHARED], fail_kb_lookup=True),
    )

    assert scope == GLOBAL


async def test_a_failed_team_arm_on_an_unshared_case_keeps_the_creators_personal_kb():
    """Known unshared: the creator is the one reader, so personal stays safe."""
    scope = await case_retrieval_scope(
        _case(),
        team_service=_Teams(fail_membership=True),
        share_repository=_Shares([]),
    )

    assert scope == {"$or": [GLOBAL, CREATOR_ARM]}


@pytest.mark.parametrize(
    "teams,shares",
    [
        (_Teams(), _Shares([T_SHARED], fail_case_lookup=True)),
        (_Teams(), _Shares([T_SHARED], fail_kb_lookup=True)),
        (_Teams(fail_membership=True), _Shares([])),
    ],
    ids=["case-share-lookup", "shared-case-team-arm", "unshared-case-team-arm"],
)
async def test_raise_on_failure_raises_instead_of_degrading(teams, shares):
    with pytest.raises(RuntimeError):
        await case_retrieval_scope(
            _case(),
            team_service=teams,
            share_repository=shares,
            raise_on_failure=True,
        )


# ---------------------------------------------------------------------------
# Each call site takes the case's scope
# ---------------------------------------------------------------------------


def _wired_deps(shares: _Shares, teams: _Teams):
    from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps

    deps = EngineDeps()
    deps.team_service = teams
    deps.share_repository = shares
    return deps


class _SearchRecorder:
    """``search_knowledge`` double: records the filter, returns one hit."""

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


async def test_push_the_prefetch_searches_the_shared_cases_scope():
    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _wired_deps(_Shares([T_SHARED, T_RETIRED]), _Teams())
    deps.knowledge_service = _SearchRecorder()

    await KbPrefetcher(deps=deps).prefetch_kb_context(_case(), "X fails", "symptom")

    assert len(deps.knowledge_service.filters_seen) == 1
    _assert_shared_case_scope(deps.knowledge_service.filters_seen[0])


async def test_push_the_prefetch_keeps_the_unshared_cases_scope():
    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _wired_deps(_Shares([]), _Teams())
    deps.knowledge_service = _SearchRecorder()

    await KbPrefetcher(deps=deps).prefetch_kb_context(_case(), "X fails", "symptom")

    assert deps.knowledge_service.filters_seen == [UNSHARED_CASE_SCOPE]


async def test_push_the_prefetch_falls_back_to_global_when_shares_cannot_be_read():
    from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher

    deps = _wired_deps(_Shares([T_SHARED], fail_case_lookup=True), _Teams())
    deps.knowledge_service = _SearchRecorder()

    await KbPrefetcher(deps=deps).prefetch_kb_context(_case(), "X fails", "symptom")

    assert deps.knowledge_service.filters_seen == [GLOBAL]


def _generator(shares: _Shares, teams: _Teams):
    from faultmaven.core.investigation.milestone_engine.generation import (
        StructuredOutputGenerator,
    )

    deps = _wired_deps(shares, teams)
    deps.repository = MagicMock()
    deps.investigation_tools = None
    return StructuredOutputGenerator(deps=deps, vectorizer=None)


async def test_pull_the_tool_context_carries_the_shared_cases_scope_whoever_drives():
    """The turn's user is a teammate here; the scope still comes from the case."""
    teams = _Teams()
    teams.memberships["user-teammate"] = [T_OTHER]
    generator = _generator(_Shares([T_SHARED, T_RETIRED]), teams)

    context = await generator.build_tool_context(_case(), user_id="user-teammate")

    _assert_shared_case_scope(context.kb_scope_filter)
    assert context.user_id == "user-teammate"


async def test_pull_an_engine_internal_turn_still_gets_the_cases_scope():
    """No principal (the ``"system"`` sentinel) changes nothing about the scope."""
    generator = _generator(_Shares([T_SHARED]), _Teams())

    context = await generator.build_tool_context(_case(), user_id=None)

    assert context.user_id == "system"
    assert context.kb_scope_filter == SHARED_CASE_SCOPE


async def test_pull_the_tool_context_keeps_the_unshared_cases_scope():
    generator = _generator(_Shares([]), _Teams())

    context = await generator.build_tool_context(_case(), user_id=CREATOR)

    assert context.kb_scope_filter == UNSHARED_CASE_SCOPE


async def test_pull_the_kb_tool_searches_exactly_the_context_scope():
    """The adapter forwards the context's scope; it builds none of its own."""
    from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter

    generator = _generator(_Shares([T_SHARED]), _Teams())
    context = await generator.build_tool_context(_case(), user_id=CREATOR)
    wrapped = MagicMock()
    wrapped._arun = AsyncMock(return_value="answer")

    result = await KBToolAdapter(wrapped).execute_with_context(
        {"question": "q"}, context
    )

    assert result.success, result.error
    wrapped._arun.assert_awaited_once_with(
        question="q", scope_filter=SHARED_CASE_SCOPE, k=5
    )


def _runbook_creator(shares: _Shares, teams: _Teams):
    from faultmaven.core.investigation.milestone_engine.runbook_creation import (
        RunbookCreator,
    )

    return RunbookCreator(case_locks={}, deps=_wired_deps(shares, teams))


async def test_dedup_the_resolver_returns_the_shared_cases_scope():
    creator = _runbook_creator(_Shares([T_SHARED, T_RETIRED]), _Teams())

    scope = await creator._runbook_dedup_scope_resolver(_case())()

    _assert_shared_case_scope(scope)


async def test_dedup_the_resolver_keeps_the_unshared_cases_scope():
    creator = _runbook_creator(_Shares([]), _Teams())

    scope = await creator._runbook_dedup_scope_resolver(_case())()

    assert scope == UNSHARED_CASE_SCOPE


async def test_dedup_the_resolver_raises_rather_than_narrowing():
    """A narrowed dedup would back a "nothing similar" claim it never checked."""
    creator = _runbook_creator(_Shares([T_SHARED], fail_case_lookup=True), _Teams())

    with pytest.raises(RuntimeError, match="share table unavailable"):
        await creator._runbook_dedup_scope_resolver(_case())()

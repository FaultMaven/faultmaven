"""The report-recommendation runbook search is scoped to the REQUESTER (fm#1030).

``ReportRecommendationService._find_similar_runbooks`` resolves by
*similarity* — no id, no owner — so the caller-supplied KB scope filter is the
only isolation on the path. The recommendation answers "should *you* generate
a runbook?", so the requester's read scope governs (global ∪ their own items ∪
items shared to their teams); the engine's terminal-turn dedup scopes on the
case owner, and the two may legitimately disagree on the same case (owner
decision, fm#1030).

Also pinned: a requester scope that cannot be RESOLVED is a typed refusal,
never a silently narrowed search — a search over a narrower scope than the
requester can read would underpin a "no similar runbooks" claim it did not
establish.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
)
from faultmaven.modules.report.domain.services.report_recommendation_service import (
    ReportRecommendationService,
)

pytestmark = [pytest.mark.unit]

REQUESTER = "user-requester-1"
ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _RecordingRunbookKB(RunbookKnowledgeBase):
    """Records the scope filter of each query, over the REAL class.

    Subclasses ``RunbookKnowledgeBase`` and overrides only the vector-level
    search rather than reimplementing the interface, so the production
    text -> embedding -> scope-refusal path runs for real and this test keeps
    measuring the thing it claims to measure (#944's dedup rotted precisely
    because a hand-rolled double outlived the interface it mimicked).
    """

    def __init__(self) -> None:
        super().__init__(vector_store=MagicMock())
        self.scopes: List[Optional[Dict[str, Any]]] = []

    async def search_runbooks(
        self,
        query_embedding: Any,
        *,
        scope_filter: Optional[Dict[str, Any]],
        top_k: int = 5,
        min_similarity: float = 0.65,
    ) -> List[Any]:
        self.scopes.append(scope_filter)
        return []


@pytest.fixture(autouse=True)
def _available_embedder():
    """These tests are about the scope predicate, not embedding availability."""
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        new=AsyncMock(return_value=[0.1] * 1024),
    ):
        yield


def _case() -> Case:
    return Case(
        user_id="case-owner-9",  # deliberately NOT the requester
        organization_id=ORG,
        title="Connection pool exhausted under load",
        description="DB queries timing out",
        state=CaseState.INVESTIGATING,
        problem_verification=ProblemVerification(
            symptom_statement="Timeout errors",
            severity="HIGH",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Timeout",
        ),
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_requesters_scope_governs_not_the_case_owners():
    """The filter is keyed on the REQUESTER's ids — the case owner's personal
    corpus must not widen a stranger's search."""
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    await service._find_similar_runbooks(
        _case(), requester_user_id=REQUESTER, requester_organization_id=ORG
    )

    assert kb.scopes == [{"$or": [{"scope": "global"}, {"owner_id": REQUESTER}]}]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_requesters_team_shared_items_widen_the_scope():
    kb = _RecordingRunbookKB()
    team_service = MagicMock()
    team_service.list_all_user_team_ids = AsyncMock(return_value=["team-1"])
    share_repository = MagicMock()
    share_repository.list_resource_ids = AsyncMock(return_value=["kb-shared-7"])
    service = ReportRecommendationService(
        runbook_kb=kb, team_service=team_service, share_repository=share_repository
    )

    await service._find_similar_runbooks(
        _case(), requester_user_id=REQUESTER, requester_organization_id=ORG
    )

    (scope,) = kb.scopes
    assert {"parent_document_id": {"$in": ["kb-shared-7"]}} in scope["$or"]
    team_service.list_all_user_team_ids.assert_awaited_once_with(REQUESTER)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_scope_resolution_failure_is_a_typed_refusal_not_a_narrower_search():
    """The team arm failing must refuse, not degrade to global ∪ personal:
    the route renders the refusal as its 503, and no query is ever issued
    under a scope the requester did not get."""
    kb = _RecordingRunbookKB()
    team_service = MagicMock()
    team_service.list_all_user_team_ids = AsyncMock(
        side_effect=RuntimeError("team lookup failed")
    )
    service = ReportRecommendationService(
        runbook_kb=kb, team_service=team_service, share_repository=MagicMock()
    )

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await service._find_similar_runbooks(
            _case(), requester_user_id=REQUESTER, requester_organization_id=ORG
        )

    assert excinfo.value.error_code == "RUNBOOK_SCOPE_RESOLUTION_FAILED"
    assert kb.scopes == [], "a query was issued under a scope that failed to resolve"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standalone_without_team_deps_is_a_clean_search_not_a_failure():
    """No team service means no teams EXIST — the empty team arm is the
    correct scope, not a degraded one, so the search proceeds."""
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    await service._find_similar_runbooks(
        _case(), requester_user_id=REQUESTER, requester_organization_id=None
    )

    assert len(kb.scopes) == 1, "standalone must still search"

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

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.case.contracts import (
    Case,
)
from faultmaven.modules.report.domain.services.report_recommendation_service import (
    ReportRecommendationService,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE
from tests.case_builders import investigating_case
from tests.utils import admit_every_parent

pytestmark = [pytest.mark.unit]

REQUESTER = "user-requester-1"
ENTERPRISE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _RecordingRunbookKB(RunbookKnowledgeBase):
    """Records the scope filter of each query, over the REAL class.

    Subclasses ``RunbookKnowledgeBase`` and overrides only the vector-level
    search rather than reimplementing the interface, so the production
    text -> embedding -> scope-refusal path runs for real and this test keeps
    measuring the thing it claims to measure (#944's dedup rotted precisely
    because a hand-rolled double outlived the interface it mimicked).
    """

    def __init__(self) -> None:
        super().__init__(vector_store=MagicMock(), admit=admit_every_parent)
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
    return investigating_case(
        user_id="case-owner-9",  # deliberately NOT the requester
        enterprise_id=ENTERPRISE,
        title="Connection pool exhausted under load",
        description="DB queries timing out",
        symptom="Timeout errors",
        statement="Timeout",
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
        _case(), requester_user_id=REQUESTER, requester_enterprise_id=ENTERPRISE
    )

    assert kb.scopes == [
        {
            "$or": [
                {
                    "$and": [
                        {"scope": "global"},
                        {
                            "enterprise_id": {
                                "$in": [ENTERPRISE, STANDALONE_ENTERPRISE_ID]
                            }
                        },
                    ]
                },
                {"$and": [{"owner_id": REQUESTER}, {"enterprise_id": ENTERPRISE}]},
            ]
        }
    ]


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
        _case(), requester_user_id=REQUESTER, requester_enterprise_id=ENTERPRISE
    )

    (scope,) = kb.scopes
    assert {
        "$and": [
            {"parent_document_id": {"$in": ["kb-shared-7"]}},
            {"enterprise_id": ENTERPRISE},
        ]
    } in scope["$or"]
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
            _case(), requester_user_id=REQUESTER, requester_enterprise_id=ENTERPRISE
        )

    assert excinfo.value.error_code == "RUNBOOK_SCOPE_RESOLUTION_FAILED"
    assert kb.scopes == [], "a query was issued under a scope that failed to resolve"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_standalone_without_team_deps_is_a_clean_search_not_a_failure():
    """No team service means no teams EXIST — the empty team arm is the
    correct scope, not a degraded one, so the search proceeds. Under
    single-tenant the Standalone enterprise is the one real tenant (the value
    the route binds), so the real ``usable_tenant_id`` admits it."""
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    with patch(
        "faultmaven.providers.tenancy.factory.requested_tenant_provider",
        return_value=BUILTIN_SINGLE,
    ):
        await service._find_similar_runbooks(
            _case(),
            requester_user_id=REQUESTER,
            requester_enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

    (scope,) = kb.scopes
    assert {
        "$and": [{"owner_id": REQUESTER}, {"enterprise_id": STANDALONE_ENTERPRISE_ID}]
    } in scope["$or"], "standalone must still search, under the requester's own scope"


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enterprise_id,provider",
    [
        pytest.param(None, BUILTIN_SINGLE, id="none"),
        pytest.param(STANDALONE_ENTERPRISE_ID, BUILTIN_MULTI, id="sentinel-multi"),
    ],
)
async def test_a_requester_with_no_usable_enterprise_is_refused_not_narrowed(
    enterprise_id, provider
):
    """P1 of the #1775 critique: every arm carries the enterprise conjunct, so
    with none usable the only scope left is the platform tier — which never saw
    the requester's own runbooks. The search is refused, not run on it."""
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    with patch(
        "faultmaven.providers.tenancy.factory.requested_tenant_provider",
        return_value=provider,
    ):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await service._find_similar_runbooks(
                _case(),
                requester_user_id=REQUESTER,
                requester_enterprise_id=enterprise_id,
            )

    assert excinfo.value.error_code == "RUNBOOK_SCOPE_RESOLUTION_FAILED"
    assert kb.scopes == [], "a query was issued under the platform tier alone"

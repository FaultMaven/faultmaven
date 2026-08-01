"""The runbook-similarity search behind report recommendation is tenant-scoped.

``ReportRecommendationService._find_similar_runbooks`` is the second consumer of
``RunbookKnowledgeBase.search_runbooks``, and like its sibling in
``terminal_transitions`` it resolves by *similarity* — no id, no owner — so the
``organization_id`` metadata predicate is the only isolation on the path
(``docs/architecture/security/rbac.md`` → "Tenant-Scoped Resolution").

The tenant comes from ``case.organization_id``, which ``CaseService.create_case``
stamps from the **total** ``get_current_org_id`` — so under
``TENANT_PROVIDER=multi`` it can be the Standalone sentinel, which is not a
tenant there. These tests pin that the service resolves it through
``usable_tenant_id`` rather than handing the raw value to the query.
"""

from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
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
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

_PROVIDER_TARGET = "faultmaven.providers.tenancy.factory.requested_tenant_provider"

TENANT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _RecordingRunbookKB(RunbookKnowledgeBase):
    """Records the tenant of each query, over the REAL class.

    Subclasses ``RunbookKnowledgeBase`` and overrides only the terminal query
    rather than reimplementing the interface. A free-standing stub used to
    stand in here, and it silently stopped exercising the production path when
    the service moved to ``search_by_text`` (#944) — a hand-rolled double
    asserts against an interface that no longer exists. Inheriting means the
    real text -> embedding -> tenant-predicate path runs, so this test keeps
    measuring the thing it claims to measure.
    """

    def __init__(self) -> None:
        super().__init__(vector_store=MagicMock())
        self.tenants: List[Optional[str]] = []

    async def search_runbooks(
        self,
        query_embedding: Any,
        *,
        organization_id: Optional[str],
        filters: Any = None,
        top_k: int = 5,
        min_similarity: float = 0.65,
    ) -> List[Any]:
        self.tenants.append(organization_id)
        return []


@pytest.fixture(autouse=True)
def _available_embedder():
    """These tests are about the tenant predicate, not embedding availability."""
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        new=AsyncMock(return_value=[0.1] * 1024),
    ):
        yield


def _case(organization_id: str) -> Case:
    return Case(
        user_id="u1",
        organization_id=organization_id,
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
@pytest.mark.parametrize("provider", [BUILTIN_SINGLE, BUILTIN_MULTI])
async def test_a_real_tenant_is_threaded_into_the_similarity_query(
    monkeypatch, provider
):
    """The positive arm — without it, "refuse the sentinel" and "refuse
    everything" would be indistinguishable."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: provider)
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    await service._find_similar_runbooks(_case(TENANT_A))

    assert kb.tenants == [TENANT_A]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_sentinel_is_not_a_tenant_under_multi(monkeypatch):
    """A sentinel-stamped case must not query with the sentinel as the predicate.

    The mechanism tightened in #944 review. It used to pass ``None`` down to
    ``search_runbooks``, whose falsy-org guard returned ``[]`` without
    querying — fail-closed, but SILENTLY, and the recommendation service reads
    ``[]`` as "no similar runbooks exist" and answers ``generate``. A refused
    search must not be reportable as a finding, so ``search_by_text`` now
    refuses up front and loudly.

    The tenancy property is unchanged and asserted directly: no query is ever
    issued carrying a predicate that is not a real tenant.
    """
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    with pytest.raises(KnowledgeBaseError) as excinfo:
        await service._find_similar_runbooks(_case(STANDALONE_ORG_ID))

    assert excinfo.value.error_code == "RUNBOOK_SEARCH_UNSCOPED"
    assert kb.tenants == [], "a similarity query was issued without a real tenant"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_sentinel_is_the_tenant_under_single(monkeypatch):
    """Under single the sentinel is the deployment's one legitimate tenant."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_SINGLE)
    kb = _RecordingRunbookKB()
    service = ReportRecommendationService(runbook_kb=kb)

    await service._find_similar_runbooks(_case(STANDALONE_ORG_ID))

    assert kb.tenants == [STANDALONE_ORG_ID]

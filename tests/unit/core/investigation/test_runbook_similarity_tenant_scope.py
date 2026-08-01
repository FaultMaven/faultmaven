"""The tenant guard on the runbook-similarity path in ``terminal_transitions``.

``_find_similar_runbooks_for_case`` is an **id-free resolution path**: a
similarity query names no id and no owner, so the ``organization_id`` metadata
predicate is the only isolation available (``docs/architecture/security/rbac.md``
→ "Tenant-Scoped Resolution").

The guard in that function is what stands on the reachable failure, and the
downstream one cannot replace it: ``search_runbooks`` refuses only a **falsy**
org, while the Standalone sentinel is a perfectly truthy string. Under
``TENANT_PROVIDER=multi`` the sentinel is not a tenant, so it must be rejected
*here* — the downstream check would happily query with it as the predicate and
pool every deployment-defaulted case into one corpus.

Since #944 there is a single arm: ``_find_similar_runbooks_for_case`` calls
``RunbookKnowledgeBase.search_by_text`` directly. The two-arm
``hasattr``/fallback structure these tests used to cover is gone — the probe
was permanently False (no such method existed) and the fallback raised
``TypeError`` on a signature mismatch, so dedup never ran at all.

The reachable failure is **not** an org-less case. ``Case.organization_id`` is
``str`` with ``min_length=1`` under ``validate_assignment=True``, so a
well-formed case can never carry a falsy org. The reachable one is the
**Standalone sentinel**: ``CaseService.create_case`` stamps the org from the
*total* ``get_current_org_id()``, whose contextvar defaults to the sentinel, so
under ``TENANT_PROVIDER=multi`` a case written from an execution context that
never bound a tenant is a perfectly valid ``Case`` carrying a value that is not a
tenant. These tests pin that state, both directions of the ``single``/``multi``
split, and the positive arm that proves the guard is not simply "always refuse".
"""

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.core.investigation.terminal_transitions import (
    _find_similar_runbooks_for_case,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
    SimilarRunbook,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

#: The single override point for "which tenant provider is in force" —
#: ``usable_tenant_id`` resolves it from this module attribute.
_PROVIDER_TARGET = "faultmaven.providers.tenancy.factory.requested_tenant_provider"

TENANT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

#: What a KB would hand back if the query were issued. High enough to drive the
#: ``EXISTING_COVERS`` verdict, so a search that should not have happened is
#: visible as a *leaked runbook title*, not merely as a call count.
LEAKED_MATCH = {"similarity_score": 0.93, "title": "Another Tenant's Runbook"}


def _leaked_runbook() -> SimilarRunbook:
    """A real ``SimilarRunbook``, the type ``search_by_text`` actually returns.

    Built from the real models rather than a duck-typed stand-in: the previous
    doubles returned plain dicts, which matched the old raw-passthrough arm but
    not the production contract — so they would have kept passing against code
    that no longer worked (which is exactly how #944's dedup rotted unnoticed).
    """
    return SimilarRunbook(
        runbook=CaseReport(
            case_id="other-case",
            report_type=ReportType.RUNBOOK,
            title=LEAKED_MATCH["title"],
            content="steps",
            generation_status=ReportStatus.COMPLETED,
            generation_time_ms=0,
        ),
        similarity_score=LEAKED_MATCH["similarity_score"],
        case_title=LEAKED_MATCH["title"],
        case_id="other-case",
    )


class _RecordingKB(RunbookKnowledgeBase):
    """Records the tenant of every query that actually reaches the store.

    Subclasses the real ``RunbookKnowledgeBase`` and overrides only the
    terminal query, so the production ``search_by_text`` — including its
    embedding step — runs for real. Always returns a match, so "no query was
    issued" and "a query was issued but found nothing" cannot be confused.
    """

    def __init__(self) -> None:
        super().__init__(vector_store=MagicMock())
        self.tenants: list[Optional[str]] = []

    async def search_runbooks(
        self,
        query_embedding: Any = None,
        *,
        organization_id: Optional[str],
        filters: Any = None,
        top_k: int = 5,
        min_similarity: float = 0.65,
    ) -> list[Any]:
        self.tenants.append(organization_id)
        return [_leaked_runbook()]


@pytest.fixture(autouse=True)
def _available_embedder():
    """These tests are about the tenant predicate, not embedder availability."""
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        new=AsyncMock(return_value=[0.1] * 1024),
    ):
        yield


def _case(organization_id: str) -> Case:
    """A case carrying ``organization_id`` and enough content to build a query.

    A real ``Case``, not a stand-in: the guard reads ``organization_id`` off it,
    and a double could carry a value the model forbids (``str``/``min_length=1``
    under ``validate_assignment``), which would make the gate pass for the wrong
    reason. Title + root cause + solution title are exactly the three fields
    ``_find_similar_runbooks_for_case`` joins into its query text, so with the
    guard removed the function has a non-empty query and really does search.
    """
    case = Case(
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
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Misconfigured connection pool timeout",
        confidence_level="verified",
        likelihood=0.9,
        mechanism="Connection pool timeout set to 1s caused cascading failures",
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Increase connection pool timeout to 30s",
            longterm_fix="Update pool timeout in application config",
        )
    ]
    return case


# ---------------------------------------------------------------------------
# The reachable state: a case stamped with the Standalone sentinel
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_sentinel_stamped_case_issues_no_similarity_query_under_multi(
    monkeypatch,
):
    """Under multi the sentinel is not a tenant, so no query is issued at all.

    ``create_case`` stamps the org from the total ``get_current_org_id``, so this
    case shape is reachable in production. Querying with the sentinel as the
    predicate would pool every deployment-defaulted case into one corpus.
    """
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    kb = _RecordingKB()

    result = await _find_similar_runbooks_for_case(_case(STANDALONE_ORG_ID), kb)

    # None, not [] — "no search ran", which does NOT license a caller's
    # "nothing similar exists" verdict (#944 review). The tenancy property is
    # the second assertion and is unchanged.
    assert result is None
    assert kb.tenants == [], "a query was issued with the sentinel as the tenant"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinel_stamped_case_is_a_real_tenant_under_single(monkeypatch):
    """The positive half of the split: under single the sentinel *is* the tenant.

    Without this, "refuse the sentinel" and "refuse everything" are the same
    test, and a guard that always returned ``[]`` would pass.
    """
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_SINGLE)
    kb = _RecordingKB()

    result = await _find_similar_runbooks_for_case(_case(STANDALONE_ORG_ID), kb)

    assert kb.tenants == [STANDALONE_ORG_ID]
    assert result == [dict(LEAKED_MATCH)]  # mapped from the SimilarRunbook


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [BUILTIN_SINGLE, BUILTIN_MULTI])
async def test_a_real_tenant_reaches_the_similarity_search(monkeypatch, provider):
    """A genuine org is threaded into the query unchanged, under either provider."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: provider)
    kb = _RecordingKB()

    result = await _find_similar_runbooks_for_case(_case(TENANT_A), kb)

    assert kb.tenants == [TENANT_A]
    assert result == [dict(LEAKED_MATCH)]  # mapped from the SimilarRunbook


# ---------------------------------------------------------------------------
# The falsy arm — not reachable through the model, pinned so the guard is total
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_org", ["", None])
async def test_an_org_less_case_issues_no_similarity_query(monkeypatch, bad_org):
    """A case with no org at all searches nothing, under either provider.

    ``Case.organization_id`` is ``str``/``min_length=1`` with
    ``validate_assignment=True``, so this state cannot be produced through the
    model — it is written here with ``object.__setattr__``, the one bypass, to
    pin the guard as total rather than as a check that happens to hold for the
    values the model permits.
    """
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    case = _case(TENANT_A)
    object.__setattr__(case, "organization_id", bad_org)
    kb = _RecordingKB()

    result = await _find_similar_runbooks_for_case(case, kb)

    # None, not [] — see the sentinel test above. The tenancy property (no
    # query issued at all) is the second assertion and is unchanged.
    assert result is None
    assert kb.tenants == []

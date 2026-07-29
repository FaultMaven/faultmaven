"""The tenant guard on the runbook-similarity path in ``terminal_transitions``.

``_find_similar_runbooks_for_case`` is an **id-free resolution path**: a
similarity query names no id and no owner, so the ``organization_id`` metadata
predicate is the only isolation available (``docs/architecture/security/rbac.md``
→ "Tenant-Scoped Resolution"). Two things make the guard in that function the
*only* thing standing on the first arm:

* the ``search_by_text`` arm calls a method with no downstream fail-closed check
  of its own — unlike the ``search_runbooks`` arm beside it, which refuses a
  falsy org before querying;
* ``hasattr(runbook_kb, "search_by_text")`` is trivially true for any ``Mock``,
  so that arm is the one tests actually drive.

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

import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.core.investigation.terminal_transitions import (
    _find_similar_runbooks_for_case,
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


class _RecordingSearchByText:
    """KB double exposing only ``search_by_text`` — the arm with no downstream guard.

    Records the tenant of every query it is asked to run and always returns a
    match, so "no query was issued" and "a query was issued but found nothing"
    cannot be confused.
    """

    def __init__(self) -> None:
        self.tenants: list[Optional[str]] = []

    async def search_by_text(
        self,
        *,
        query_text: str,
        organization_id: Optional[str],
        top_k: int,
        min_similarity: float,
    ) -> list[dict]:
        self.tenants.append(organization_id)
        return [dict(LEAKED_MATCH)]


class _RecordingSearchRunbooks:
    """KB double exposing only ``search_runbooks`` — the ``elif`` fallback arm."""

    def __init__(self) -> None:
        self.tenants: list[Optional[str]] = []

    async def search_runbooks(
        self,
        *,
        query_text: str,
        organization_id: Optional[str],
        top_k: int,
        min_similarity: float,
    ) -> list[Any]:
        self.tenants.append(organization_id)

        class _Match:
            similarity_score = LEAKED_MATCH["similarity_score"]
            title = LEAKED_MATCH["title"]

        return [_Match()]


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
    kb = _RecordingSearchByText()

    result = await _find_similar_runbooks_for_case(_case(STANDALONE_ORG_ID), kb)

    assert result == []
    assert kb.tenants == [], "a query was issued with the sentinel as the tenant"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinel_stamped_case_is_a_real_tenant_under_single(monkeypatch):
    """The positive half of the split: under single the sentinel *is* the tenant.

    Without this, "refuse the sentinel" and "refuse everything" are the same
    test, and a guard that always returned ``[]`` would pass.
    """
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_SINGLE)
    kb = _RecordingSearchByText()

    result = await _find_similar_runbooks_for_case(_case(STANDALONE_ORG_ID), kb)

    assert kb.tenants == [STANDALONE_ORG_ID]
    assert result == [dict(LEAKED_MATCH)]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [BUILTIN_SINGLE, BUILTIN_MULTI])
async def test_a_real_tenant_reaches_the_similarity_search(monkeypatch, provider):
    """A genuine org is threaded into the query unchanged, under either provider."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: provider)
    kb = _RecordingSearchByText()

    result = await _find_similar_runbooks_for_case(_case(TENANT_A), kb)

    assert kb.tenants == [TENANT_A]
    assert result == [dict(LEAKED_MATCH)]


# ---------------------------------------------------------------------------
# The ``search_runbooks`` fallback arm carries the same predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_fallback_arm_also_refuses_the_sentinel_under_multi(monkeypatch):
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    kb = _RecordingSearchRunbooks()

    result = await _find_similar_runbooks_for_case(_case(STANDALONE_ORG_ID), kb)

    assert result == []
    assert kb.tenants == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_arm_threads_a_real_tenant(monkeypatch):
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    kb = _RecordingSearchRunbooks()

    result = await _find_similar_runbooks_for_case(_case(TENANT_A), kb)

    assert kb.tenants == [TENANT_A]
    assert result == [
        {
            "similarity_score": LEAKED_MATCH["similarity_score"],
            "title": LEAKED_MATCH["title"],
        }
    ]


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
    kb = _RecordingSearchByText()

    result = await _find_similar_runbooks_for_case(case, kb)

    assert result == []
    assert kb.tenants == []

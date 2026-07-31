"""Runbook dedup must never conclude a negative from a search that never ran (#944).

Two independent sites concluded "no similar runbook exists" without issuing a
valid query, and both were live with a completely healthy stack:

1. ``ReportRecommendationService`` searched with ``[]`` as the embedding.
   ChromaDB rejects a 0-dim vector; the error was swallowed; the service read
   the resulting ``[]`` as "none found" and answered ``action="generate"``
   every single time.
2. ``terminal_transitions`` probed ``hasattr(runbook_kb, "search_by_text")`` —
   permanently False, since no such method existed — then called
   ``search_runbooks(query_text=...)`` against a signature taking
   ``query_embedding``, raising ``TypeError`` into an ``except`` returning [].

The tests below therefore check two distinct properties:

- **The query is actually issued** — a real 1024-dim vector, scoped to the
  tenant. Asserting only on the returned recommendation would pass against
  both the broken and the fixed code, because both return "generate" when the
  KB is genuinely empty. That is the trap that let this rot silently, so the
  signature/arguments are pinned directly.
- **A search that could not run never reads as "none found"**.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import terminal_transitions as tt
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.report.domain.services.report_recommendation_service import (
    ReportRecommendationService,
)

pytestmark = [pytest.mark.unit]

_EMBED_QUERY = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"


def _kb(search_result=None) -> RunbookKnowledgeBase:
    kb = RunbookKnowledgeBase(vector_store=MagicMock())
    kb.search_runbooks = AsyncMock(return_value=search_result or [])
    return kb


def _case() -> MagicMock:
    case = MagicMock()
    case.case_id = "case-1"
    case.title = "Pods OOMKilled after deploy"
    case.description = "memory limit too low"
    case.organization_id = "org-1"
    case.domain = "kubernetes"
    case.tags = ["oom"]
    case.root_cause_conclusion = None
    case.solutions = []
    return case


# ---------------------------------------------------------------------------
# The search must actually be issued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_issues_a_real_embedding_scoped_to_the_tenant():
    """Pins the arguments, not just the verdict.

    The old code reached the same ``action="generate"`` while passing ``[]``
    as the embedding, so a verdict-only assertion cannot tell the fix from
    the defect.
    """
    kb = _kb()
    service = ReportRecommendationService(runbook_kb=kb)

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        await service.get_available_report_types(case=_case())

    kwargs = kb.search_runbooks.await_args.kwargs
    assert len(kwargs["query_embedding"]) == 1024, (
        "the runbook search is still being issued without a usable embedding"
    )
    assert kwargs["organization_id"] == "org-1", "tenant predicate lost"


@pytest.mark.asyncio
async def test_terminal_dedup_issues_a_real_query():
    """The terminal-resolution arm, which previously never queried at all."""
    kb = _kb()

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        await tt._find_similar_runbooks_for_case(_case(), kb)

    assert kb.search_runbooks.await_count == 1, "dedup still never queries"
    kwargs = kb.search_runbooks.await_args.kwargs
    assert len(kwargs["query_embedding"]) == 1024
    assert kwargs["organization_id"] == "org-1"


def test_search_by_text_exists_with_the_signature_both_callers_use():
    """The `hasattr` probe is gone, so a rename now breaks loudly. This pins
    the contract that made the probe silently False for so long."""
    sig = inspect.signature(RunbookKnowledgeBase.search_by_text)
    assert "query_text" in sig.parameters
    assert "organization_id" in sig.parameters
    assert sig.parameters["organization_id"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# A search that could not run is never "none found"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_embedder_refuses_instead_of_recommending_generate():
    service = ReportRecommendationService(runbook_kb=_kb())

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await service.get_available_report_types(case=_case())

    assert "No similar runbooks found" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_chromadb_failure_is_not_flattened_into_no_similar_runbooks():
    vector_store = MagicMock()
    vector_store.query_by_embedding = AsyncMock(side_effect=RuntimeError("unreachable"))
    kb = RunbookKnowledgeBase(vector_store=vector_store)

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        with pytest.raises(KnowledgeBaseError):
            await kb.search_by_text(query_text="oom", organization_id="org-1")


@pytest.mark.asyncio
async def test_failed_dedup_yields_a_caveat_not_a_clean_suggestion(ready_case):
    """``SUGGEST`` means "checked, nothing similar". An unchecked case must not
    reach it, or the verdict states a dedup result never obtained."""
    kb = _kb()

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=None)):
        suggestion = await tt.evaluate_runbook_suggestion(ready_case, kb)

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message


@pytest.mark.asyncio
async def test_absent_kb_also_yields_a_caveat(ready_case):
    suggestion = await tt.evaluate_runbook_suggestion(ready_case, runbook_kb=None)

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message


@pytest.mark.asyncio
async def test_a_clean_dedup_still_reaches_plain_suggest(ready_case):
    """The gate must be able to PASS.

    Without this, always returning the caveat would satisfy every assertion
    above while destroying the feature — "checked, nothing similar found" is a
    real and useful verdict that must stay reachable.
    """
    kb = _kb()

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        suggestion = await tt.evaluate_runbook_suggestion(ready_case, kb)

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST


@pytest.mark.asyncio
async def test_an_existing_similar_runbook_is_still_detected(ready_case):
    """The other direction of the same gate: dedup must still be able to FIRE."""
    match = MagicMock()
    match.similarity_score = 0.91
    match.runbook = MagicMock(title="OOMKilled recovery")
    kb = _kb(search_result=[match])

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        suggestion = await tt.evaluate_runbook_suggestion(ready_case, kb)

    assert suggestion.verdict == tt.RunbookSuggestion.EXISTING_COVERS
    assert "OOMKilled recovery" in suggestion.message


def _resolved_case() -> MagicMock:
    case = _case()
    case.root_cause_conclusion = MagicMock(root_cause="memory limit too low")
    solution = MagicMock()
    solution.title = "raise the limit"
    case.solutions = [solution]
    return case


@pytest.fixture
def ready_case(monkeypatch):
    """A case whose content readiness is READY.

    ``assess_runbook_readiness`` is pinned rather than satisfied for real: it
    requires a CONFIRMED cause-assurance grade backed by a causal graph, which
    is a large, unrelated fixture. These tests are about the *dedup* factor, so
    readiness is held constant to isolate it — the readiness logic has its own
    coverage elsewhere.
    """
    monkeypatch.setattr(
        tt,
        "assess_runbook_readiness",
        lambda case: MagicMock(verdict=tt.RunbookReadiness.READY, message="ready"),
    )
    return _resolved_case()

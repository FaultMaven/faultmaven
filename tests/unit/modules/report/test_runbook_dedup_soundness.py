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

- **The query is actually issued** — a real 1024-dim vector, under the
  searching principal's scope filter. Asserting only on the returned
  recommendation would pass against both the broken and the fixed code,
  because both return "generate" when the KB is genuinely empty. That is the
  trap that let this rot silently, so the signature/arguments are pinned
  directly.
- **A search that could not run never reads as "none found"**.

fm#1030 additions: dedup reads the LIVE writer's rows under the KB scope
model, and the auto-"reuse"/EXISTING_COVERS verdict is withheld (owner
decision) — a strong match is surfaced by title and score for the user to
judge. Both are pinned here.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import terminal_transitions as tt
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.report import RunbookMatch
from faultmaven.modules.report.domain.services.report_recommendation_service import (
    ReportRecommendationService,
)

pytestmark = [pytest.mark.unit]

_EMBED_QUERY = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"

_SCOPE = {"$or": [{"scope": "global"}, {"owner_id": "user-1"}]}


async def _scope_resolver():
    return dict(_SCOPE)


def _kb(search_result=None) -> RunbookKnowledgeBase:
    kb = RunbookKnowledgeBase(vector_store=MagicMock())
    kb.search_runbooks = AsyncMock(return_value=search_result or [])
    return kb


def _case() -> MagicMock:
    case = MagicMock()
    case.case_id = "case-1"
    case.title = "Pods OOMKilled after deploy"
    case.description = "memory limit too low"
    case.user_id = "user-1"
    case.organization_id = "org-1"
    case.tags = ["oom"]
    case.root_cause_conclusion = None
    case.solutions = []
    return case


# ---------------------------------------------------------------------------
# The search must actually be issued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_issues_a_real_embedding_under_the_requesters_scope():
    """Pins the arguments, not just the verdict.

    The old code reached the same ``action="generate"`` while passing ``[]``
    as the embedding, so a verdict-only assertion cannot tell the fix from
    the defect.
    """
    kb = _kb()
    service = ReportRecommendationService(runbook_kb=kb)

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        await service.get_available_report_types(
            case=_case(),
            requester_user_id="user-1",
            requester_organization_id="org-1",
        )

    kwargs = kb.search_runbooks.await_args.kwargs
    assert (
        len(kwargs["query_embedding"]) == 1024
    ), "the runbook search is still being issued without a usable embedding"
    assert kwargs["scope_filter"] == _SCOPE, "requester scope predicate lost"


@pytest.mark.asyncio
async def test_terminal_dedup_issues_a_real_query():
    """The terminal-resolution arm, which previously never queried at all."""
    kb = _kb()

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        await tt._find_similar_runbooks_for_case(_case(), kb, dict(_SCOPE))

    assert kb.search_runbooks.await_count == 1, "dedup still never queries"
    kwargs = kb.search_runbooks.await_args.kwargs
    assert len(kwargs["query_embedding"]) == 1024
    assert kwargs["scope_filter"] == _SCOPE


def test_search_by_text_exists_with_the_signature_both_callers_use():
    """The `hasattr` probe is gone, so a rename now breaks loudly. This pins
    the contract that made the probe silently False for so long."""
    sig = inspect.signature(RunbookKnowledgeBase.search_by_text)
    assert "query_text" in sig.parameters
    assert "scope_filter" in sig.parameters
    assert sig.parameters["scope_filter"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# A search that could not run is never "none found"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_embedder_refuses_instead_of_recommending_generate():
    service = ReportRecommendationService(runbook_kb=_kb())

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=None)):
        with pytest.raises(KnowledgeBaseError) as excinfo:
            await service.get_available_report_types(
                case=_case(), requester_user_id="user-1"
            )

    assert "No similar runbooks found" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_chromadb_failure_is_not_flattened_into_no_similar_runbooks():
    vector_store = MagicMock()
    vector_store.query_by_embedding = AsyncMock(side_effect=RuntimeError("unreachable"))
    kb = RunbookKnowledgeBase(vector_store=vector_store)

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        with pytest.raises(KnowledgeBaseError):
            await kb.search_by_text(query_text="oom", scope_filter=dict(_SCOPE))


@pytest.mark.asyncio
async def test_failed_dedup_yields_a_caveat_not_a_clean_suggestion(ready_case):
    """``SUGGEST`` means "checked, nothing similar". An unchecked case must not
    reach it, or the verdict states a dedup result never obtained."""
    kb = _kb()

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=None)):
        suggestion = await tt.evaluate_runbook_suggestion(
            ready_case, kb, scope_resolver=_scope_resolver
        )

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message


@pytest.mark.asyncio
async def test_an_unreadable_result_set_caveat_names_the_right_remedy(ready_case):
    """The caveat must not tell the user to wait for something already working.

    Every other dedup failure is transient and clears on retry.
    ``RUNBOOK_RESULTS_UNREADABLE`` never does: the knowledge base answered, and
    the closest runbooks stay unreadable until someone re-indexes them. Saying
    "the knowledge base search is unavailable" points at a subsystem that is
    not broken.

    This is the agent-facing half of the fix made for the HTTP 503. It was
    missed the first time and nothing failed — the message had no test.
    """
    kb = _kb()
    kb.search_by_text = AsyncMock(
        side_effect=KnowledgeBaseError(
            "matched rows could not be read",
            error_code="RUNBOOK_RESULTS_UNREADABLE",
        )
    )

    suggestion = await tt.evaluate_runbook_suggestion(
        ready_case, kb, scope_resolver=_scope_resolver
    )

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message
    assert "need re-indexing" in suggestion.message
    assert "knowledge base search is unavailable" not in suggestion.message


@pytest.mark.asyncio
async def test_a_skipped_dedup_blames_nothing(ready_case):
    """No knowledge base wired: the search did not run, nothing is broken.

    The remedy flags are only ever ASSIGNED in the exception handler, so every
    path that never raises — no KB, no scope resolver, too little case content —
    keeps their initial values. Flipping either initialiser is invisible to a
    test that exercises only the raising path: with ``dedup_unreadable``
    initialised ``True`` this caveat claimed runbooks "need re-indexing" for a
    knowledge base that does not exist, and 129 tests passed.
    """
    suggestion = await tt.evaluate_runbook_suggestion(ready_case, runbook_kb=None)

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "did not run" in suggestion.message
    assert "re-indexing" not in suggestion.message
    assert "unavailable" not in suggestion.message


@pytest.mark.asyncio
async def test_a_transient_dedup_failure_keeps_the_unavailable_wording(ready_case):
    """The transient causes keep the wording that fits them.

    Pins the other side of the branch: without it, always emitting the
    re-indexing text would satisfy the test above while misdescribing every
    genuine outage.
    """
    kb = _kb()
    kb.search_by_text = AsyncMock(
        side_effect=KnowledgeBaseError(
            "chromadb unreachable", error_code="RUNBOOK_SEARCH_FAILED"
        )
    )

    suggestion = await tt.evaluate_runbook_suggestion(
        ready_case, kb, scope_resolver=_scope_resolver
    )

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "knowledge base search is unavailable" in suggestion.message
    assert "re-indexing" not in suggestion.message


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
        suggestion = await tt.evaluate_runbook_suggestion(
            ready_case, kb, scope_resolver=_scope_resolver
        )

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST


@pytest.mark.asyncio
async def test_an_existing_similar_runbook_is_still_detected(ready_case):
    """The other direction of the same gate: dedup must still be able to FIRE."""
    match = RunbookMatch(
        item_id="kb-1",
        title="OOMKilled recovery",
        scope="global",
        similarity_score=0.91,
    )
    kb = _kb(search_result=[match])

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        suggestion = await tt.evaluate_runbook_suggestion(
            ready_case, kb, scope_resolver=_scope_resolver
        )

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "OOMKilled recovery" in suggestion.message
    assert "91%" in suggestion.message


# ---------------------------------------------------------------------------
# The "reuse" verdict is withheld (owner decision, fm#1030)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_even_a_near_perfect_match_never_auto_suppresses_generation(ready_case):
    """Best-chunk-max detects OVERLAP, not whole-runbook equivalence, and the
    old ≥0.85 auto-suppression threshold never fired against any real
    distribution. However strong the match, the verdict surfaces it for the
    USER to judge and generation stays available — there is no
    EXISTING_COVERS verdict left to return."""
    match = RunbookMatch(
        item_id="kb-1",
        title="OOMKilled recovery",
        scope="global",
        similarity_score=0.99,
    )
    kb = _kb(search_result=[match])

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        suggestion = await tt.evaluate_runbook_suggestion(
            ready_case, kb, scope_resolver=_scope_resolver
        )

    assert not hasattr(tt.RunbookSuggestion, "EXISTING_COVERS")
    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "generate a new one" in suggestion.message


@pytest.mark.asyncio
async def test_the_recommendation_service_never_answers_reuse():
    """The route-side half of the same decision: a 0.99 best match yields
    ``review_or_generate`` with an honest KB-item ref, never ``reuse`` — and
    the action Literal no longer admits "reuse" at all."""
    from faultmaven.modules.case.contracts import RunbookRecommendation

    match = RunbookMatch(
        item_id="kb-1",
        title="OOMKilled recovery",
        scope="global",
        similarity_score=0.99,
    )
    service = ReportRecommendationService(runbook_kb=_kb())

    rec = service._generate_runbook_recommendation([match])

    assert rec.action == "review_or_generate"
    assert rec.existing_runbook.item_id == "kb-1"
    assert rec.existing_runbook.title == "OOMKilled recovery"
    assert rec.existing_runbook.scope == "global"
    assert rec.similarity_score == pytest.approx(0.99)
    with pytest.raises(Exception):
        RunbookRecommendation(action="reuse", reason="should not validate")


# ---------------------------------------------------------------------------
# The caveat must reach the USER, not just the verdict object
# ---------------------------------------------------------------------------


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


def _engine_for_creation() -> "object":
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.knowledge_service = MagicMock()
    engine.runbook_kb = _kb()
    engine.team_service = None
    engine.share_repository = None
    engine.conversion_service = MagicMock()
    engine.conversion_service.get_conversion_by_case = AsyncMock(return_value=None)
    engine._run_runbook_conversion = AsyncMock()
    engine._remaining_regens_for = AsyncMock(return_value=3)
    return engine


@pytest.mark.asyncio
async def test_the_dedup_caveat_reaches_the_user_visible_turn(ready_case, monkeypatch):
    """Asserts on ``agent_response`` — the text the user actually reads.

    The tests above assert on ``suggestion.message``, an intermediate return
    value. That is insufficient on its own: ``_handle_runbook_creation``
    surfaces ``suggestion.message`` for NOT_READY only, so a
    SUGGEST_WITH_CAVEATS message can be produced correctly and still never be
    shown — the verdict-object assertions would pass while the user is told,
    unqualified, that a draft is being created. That is the same
    assert-on-an-intermediate mistake this campaign exists to close, so the
    property is pinned at the surface.
    """
    engine = _engine_for_creation()

    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin",
        lambda case: None,
    )
    monkeypatch.setattr(
        "faultmaven.modules.knowledge.domain.models.conversion."
        "CaseConversionRequest.from_case",
        classmethod(lambda cls, case, scope="personal": MagicMock()),
    )

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=None)):
        result = await engine._handle_runbook_creation(ready_case, metadata={})

    assert "could not check" in result["agent_response"], (
        "the dedup caveat never reached the user — the turn claims a draft is "
        "being created with no mention that duplicates were not ruled out"
    )


@pytest.mark.asyncio
async def test_a_clean_dedup_turn_carries_no_caveat(ready_case, monkeypatch):
    """The gate must be able to pass: a real, empty dedup adds no warning."""
    engine = _engine_for_creation()

    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin",
        lambda case: None,
    )
    monkeypatch.setattr(
        "faultmaven.modules.knowledge.domain.models.conversion."
        "CaseConversionRequest.from_case",
        classmethod(lambda cls, case, scope="personal": MagicMock()),
    )

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        result = await engine._handle_runbook_creation(ready_case, metadata={})

    assert "could not check" not in result["agent_response"]


# ---------------------------------------------------------------------------
# A search that was never ISSUED is also not "nothing found"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_case_with_no_query_content_did_not_check_for_duplicates(ready_case):
    """``_find_similar_runbooks_for_case`` returns without searching when the
    case yields no query text. That is "unchecked", not "checked and clean", so
    it must not reach the plain SUGGEST verdict."""
    ready_case.title = None
    ready_case.root_cause_conclusion = None
    ready_case.solutions = []
    kb = _kb()

    with patch(_EMBED_QUERY, new=AsyncMock(return_value=[0.1] * 1024)):
        suggestion = await tt.evaluate_runbook_suggestion(
            ready_case, kb, scope_resolver=_scope_resolver
        )

    assert kb.search_runbooks.await_count == 0, "expected no search to be issued"
    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message

"""The scope contract on the engine's runbook-dedup path (fm#1030).

``_find_similar_runbooks_for_case`` is an **id-free resolution path**: a
similarity query names no id and no owner, so the caller-supplied KB scope
filter is the only isolation available. The engine scopes dedup on the CASE
OWNER (the principal who will act on the suggestion) and resolves that scope
through ``MilestoneEngine._runbook_dedup_scope_resolver``.

Two properties are pinned here that a green "no duplicate found" cannot prove:

- **The resolver's failure surfaces.** Unlike the KB seeder pre-fetch, which
  degrades a team-arm failure to global ∪ personal (correct for seeding), a
  dedup search that silently narrowed would underpin a "checked, nothing
  similar" claim it did not establish. A resolver failure must take
  ``evaluate_runbook_suggestion``'s caveat branch, with NO search issued.
- **No resolver, no dedup.** A KB with no scope resolver cannot run an
  honestly scoped search; the skip takes the "did not run" caveat rather than
  quietly searching a narrower scope.
"""

import logging
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import terminal_transitions as tt
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.report import RunbookMatch
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)

pytestmark = [pytest.mark.unit]

OWNER = "user-owner-1"
ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _RecordingKB(RunbookKnowledgeBase):
    """Records the scope filter of every query that reaches the store.

    Subclasses the real ``RunbookKnowledgeBase`` and overrides only the
    vector-level search, so the production ``search_by_text`` — its unscoped
    refusal and embedding step included — runs for real. Always returns a
    match, so "no query was issued" and "a query found nothing" cannot be
    confused.
    """

    def __init__(self) -> None:
        super().__init__(vector_store=MagicMock())
        self.scopes: List[Optional[Dict[str, Any]]] = []

    async def search_runbooks(
        self,
        query_embedding: Any = None,
        *,
        scope_filter: Optional[Dict[str, Any]],
        top_k: int = 5,
        min_similarity: float = 0.65,
    ) -> list:
        self.scopes.append(scope_filter)
        return [
            RunbookMatch(
                item_id="kb-existing",
                title="Existing Pool Runbook",
                scope="personal",
                similarity_score=0.93,
            )
        ]


@pytest.fixture(autouse=True)
def _available_embedder():
    """These tests are about the scope contract, not embedder availability."""
    with patch(
        "faultmaven.infrastructure.model_cache.model_cache.aembed_query",
        new=AsyncMock(return_value=[0.1] * 1024),
    ):
        yield


@pytest.fixture(autouse=True)
def _ready_readiness(monkeypatch):
    """Hold content readiness constant — these tests isolate the dedup factor."""
    monkeypatch.setattr(
        tt,
        "assess_runbook_readiness",
        lambda case: MagicMock(verdict=tt.RunbookReadiness.READY, message="ready"),
    )


def _case() -> Case:
    """A real ``Case`` with enough content to build a similarity query."""
    case = Case(
        user_id=OWNER,
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
# The resolver's scope is what the search runs under
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_resolved_scope_filter_is_threaded_into_the_search():
    kb = _RecordingKB()
    scope = {"$or": [{"scope": "global"}, {"owner_id": OWNER}]}

    async def resolver():
        return scope

    suggestion = await tt.evaluate_runbook_suggestion(
        _case(), kb, scope_resolver=resolver
    )

    assert kb.scopes == [scope]
    # The 0.93 match stops the turn and is surfaced for the USER to decide —
    # never asserted to cover the case (no "existing covers" verdict), and
    # never silently created past (owner decision + fm#1030 review CORE 2).
    assert suggestion.verdict == tt.RunbookSuggestion.SIMILAR_FOUND
    assert "Existing Pool Runbook" in suggestion.message
    assert not hasattr(tt.RunbookSuggestion, "EXISTING_COVERS")


# ---------------------------------------------------------------------------
# Resolver failure surfaces — it must not narrow and answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_scope_resolver_takes_the_caveat_branch_without_searching():
    """The deliberate divergence from the seeder pre-fetch: a team-arm failure
    is a FAILED dedup with a caveat, never a quietly narrowed search."""
    kb = _RecordingKB()

    async def resolver():
        raise RuntimeError("team membership lookup failed")

    suggestion = await tt.evaluate_runbook_suggestion(
        _case(), kb, scope_resolver=resolver
    )

    assert kb.scopes == [], "a search ran under a scope that failed to resolve"
    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "could not check" in suggestion.message
    assert "unavailable" in suggestion.message


@pytest.mark.asyncio
async def test_a_kb_without_a_scope_resolver_skips_dedup_with_the_did_not_run_caveat(
    caplog,
):
    """No resolver ⇒ the search cannot be honestly scoped ⇒ skipped, loudly.

    "Did not run", not "unavailable" — nothing is broken, and blaming a
    healthy subsystem sends the user after the wrong remedy (#912)."""
    kb = _RecordingKB()

    with caplog.at_level(
        logging.WARNING, logger="faultmaven.core.investigation.terminal_transitions"
    ):
        suggestion = await tt.evaluate_runbook_suggestion(
            _case(), kb, scope_resolver=None
        )

    assert kb.scopes == [], "an unscoped dedup search was issued"
    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "did not run" in suggestion.message
    assert "unavailable" not in suggestion.message
    assert any(
        "Runbook deduplication skipped" in r.message for r in caplog.records
    ), "the skip must be observable — a silent skip masks misconfiguration"


# ---------------------------------------------------------------------------
# The engine's resolver: case-owner scope, team arm NOT swallowed
# ---------------------------------------------------------------------------


def _engine(**attrs) -> MilestoneEngine:
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.team_service = attrs.get("team_service")
    engine.share_repository = attrs.get("share_repository")
    return engine


@pytest.mark.asyncio
async def test_the_engine_resolver_builds_the_owner_scope_in_standalone():
    """No team service (standalone): global ∪ the OWNER's personal items —
    keyed on ``case.user_id``, so user B's case can never widen into user A's
    personal runbooks."""
    resolver = _engine()._runbook_dedup_scope_resolver(_case())

    scope = await resolver()

    assert scope == {"$or": [{"scope": "global"}, {"owner_id": OWNER}]}


@pytest.mark.asyncio
async def test_the_engine_resolver_includes_the_owners_team_shared_items():
    team_service = MagicMock()
    team_service.list_all_user_team_ids = AsyncMock(return_value=["team-1"])
    share_repository = MagicMock()
    share_repository.list_resource_ids = AsyncMock(return_value=["kb-shared-1"])

    resolver = _engine(
        team_service=team_service, share_repository=share_repository
    )._runbook_dedup_scope_resolver(_case())

    scope = await resolver()

    assert {"parent_document_id": {"$in": ["kb-shared-1"]}} in scope["$or"]
    team_service.list_all_user_team_ids.assert_awaited_once_with(OWNER)


@pytest.mark.asyncio
async def test_the_engine_resolver_does_not_swallow_a_team_arm_failure():
    """The pre-fetch's ``except: degrade`` is deliberately ABSENT here. If the
    team arm cannot be resolved, the resolver raises, and the dedup caller
    takes its caveat branch — pinned above. A resolver that caught this and
    returned global ∪ personal would make dedup claim a scope it never
    searched."""
    team_service = MagicMock()
    team_service.list_all_user_team_ids = AsyncMock(
        side_effect=RuntimeError("team lookup failed")
    )
    share_repository = MagicMock()

    resolver = _engine(
        team_service=team_service, share_repository=share_repository
    )._runbook_dedup_scope_resolver(_case())

    with pytest.raises(RuntimeError, match="team lookup failed"):
        await resolver()


@pytest.mark.asyncio
async def test_the_engine_passes_its_injected_kb_and_owner_resolver_to_dedup(
    monkeypatch,
):
    """``_handle_runbook_creation`` wires ``self.runbook_kb`` (the explicit
    constructor injection that replaced the permanently-False
    ``hasattr(knowledge_service, "runbook_kb")`` probe) and the case-owner
    resolver into ``evaluate_runbook_suggestion``."""
    engine = _engine()
    engine.knowledge_service = MagicMock()
    engine.runbook_kb = _RecordingKB()

    captured = {}

    async def fake_evaluate(case, runbook_kb=None, scope_resolver=None):
        captured["runbook_kb"] = runbook_kb
        captured["scope_resolver"] = scope_resolver
        return tt.RunbookSuggestion(
            verdict=tt.RunbookSuggestion.NOT_READY, message="stop here"
        )

    monkeypatch.setattr(
        "faultmaven.core.investigation.terminal_transitions."
        "evaluate_runbook_suggestion",
        fake_evaluate,
    )
    monkeypatch.setattr(
        "faultmaven.core.investigation.kb_cause_seeder.confirmed_root_seed_origin",
        lambda case: None,
    )

    await engine._handle_runbook_creation(_case(), metadata={})

    assert captured["runbook_kb"] is engine.runbook_kb
    assert captured["scope_resolver"] is not None
    assert (await captured["scope_resolver"]())["$or"][1] == {"owner_id": OWNER}

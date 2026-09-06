"""``seeded_provenance`` — the readers of provenance the removed seeder wrote.

The KB cause seeder ran on by default from 2026-07-16 to 2026-09-02 and was
removed in fm#1295. Cases opened in that window persist ``seeded_from_runbook``
(and ``seeded_interventions``) in ``causal_nodes`` metadata, and three
behaviours keyed on those markers are still correct for them: the seeded
directive in the diagnosis prompt (pinned in
``test_kb_resolution_prompt_characterization``), the R9 candidate-solutions
handoff, and the sync provenance tier of runbook-generation dedup. Nothing
writes the markers any more, so every seed here is PLANTED the way the seeder
left it. Delete this file with the module at its sunset.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.seeded_provenance import (
    SEEDED_FROM_RUNBOOK_KEY,
    SEEDED_INTERVENTIONS_KEY,
    case_has_seeded_candidates,
    confirmed_cause_interventions,
    confirmed_root_seed_origin,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = pytest.mark.unit

_INTERVENTIONS = [{"quadrant": "remediation", "ref": "root", "text": "fix the root"}]


def _case() -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="X fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
        current_turn=1,
    )


def _seed_case_with_root(
    item_id: str = "rb_seed_1", root_stmt: str = "root A: the underlying fault"
) -> "tuple[Case, str]":
    """A case carrying one CANDIDATE root exactly as the seeder left it."""
    case = _case()
    node = CausalNode(
        statement=root_stmt,
        node_type=NodeType.ROOT,
        generated_at_turn=1,
        metadata={
            SEEDED_FROM_RUNBOOK_KEY: item_id,
            SEEDED_INTERVENTIONS_KEY: list(_INTERVENTIONS),
        },
    )
    case.causal_nodes[node.node_id] = node
    return case, node.node_id


def _add_unmarked_root(case: Case, statement: str) -> str:
    """A self-discovered ROOT node — no seed provenance marker."""
    node = CausalNode(statement=statement, node_type=NodeType.ROOT, generated_at_turn=1)
    case.causal_nodes[node.node_id] = node
    return node.node_id


def _confirm_root(case: Case, node_id: str) -> None:
    """Make a root counterfactually CONFIRMED: VALIDATED + a SUPPORTS link to a
    causal_absence row (the gone⇒gone proof), mirroring the resolution stamp."""
    row = Evidence(
        summary="post-fix verification: cause no longer present",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_at=datetime.now(UTC),
        collected_by="u",
        primary_purpose="confirm root cause eliminated",
        preprocessed_content="cause absent after fix",
        content_size_bytes=40,
        preprocessing_method="manual",
        collected_at_turn=2,
    )
    case.evidence.append(row)
    node = case.causal_nodes[node_id]
    node.node_state = NodeState.VALIDATED
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="removing the cause removed the problem",
            linked_at_turn=2,
        )
    )


# ---------------------------------------------------------------------------
# case_has_seeded_candidates — the prompt-swap predicate
# ---------------------------------------------------------------------------


def test_has_seeded_candidates_reads_the_marker():
    case, _ = _seed_case_with_root()
    assert case_has_seeded_candidates(case)


def test_has_seeded_candidates_false_without_the_marker():
    case = _case()
    _add_unmarked_root(case, "a self-discovered cause")
    assert not case_has_seeded_candidates(case)


# ---------------------------------------------------------------------------
# confirmed_root_seed_origin — provenance-based runbook uniqueness (Phase 5.2b)
# ---------------------------------------------------------------------------


def test_origin_returned_when_confirmed_root_was_seeded():
    case, root_id = _seed_case_with_root("rb_argocd")
    _confirm_root(case, root_id)
    assert confirmed_root_seed_origin(case) == "rb_argocd"


def test_none_when_no_confirmed_root():
    """A seeded candidate that never validated is not a resolution — no origin
    (candidate-only seeds must never suppress a future offer)."""
    case, _ = _seed_case_with_root("rb_argocd")
    assert confirmed_root_seed_origin(case) is None


def test_none_when_confirmed_cause_was_self_discovered():
    """A case can carry seeded candidates the model refuted while resolving a
    DIFFERENT, self-discovered cause. That case must still be offered a runbook."""
    case, _ = _seed_case_with_root("rb_unrelated")
    own = _add_unmarked_root(case, "east-region network partition dropped traffic")
    _confirm_root(case, own)
    assert confirmed_root_seed_origin(case) is None


def test_origin_returned_via_cluster_when_seeded_duplicate_confirmed():
    """Clustering ranges over ALL roots: a self-discovered confirmed root that
    is a DUPLICATE (mutual mirror) of a seeded candidate collapses onto it."""
    stmt = "database connection pool exhausted under load"
    case, _ = _seed_case_with_root("rb_dbpool", root_stmt=stmt)
    dup = _add_unmarked_root(case, stmt)
    _confirm_root(case, dup)
    assert confirmed_root_seed_origin(case) == "rb_dbpool"


def test_refuted_seeded_root_does_not_claim_resolution():
    """A seeded root REFUTED by a failed fix must never be the basis for a
    'resolved by applying X' signal, even when it would cluster with a
    later-confirmed root."""
    stmt = "database connection pool exhausted under load"
    case, seeded_root = _seed_case_with_root("rb_disproven", root_stmt=stmt)
    case.causal_nodes[seeded_root].node_state = NodeState.REFUTED
    real = _add_unmarked_root(case, stmt)
    _confirm_root(case, real)
    assert confirmed_root_seed_origin(case) is None


def test_none_on_empty_graph():
    assert confirmed_root_seed_origin(_case()) is None


# ---------------------------------------------------------------------------
# confirmed_cause_interventions — the R9 read half
# ---------------------------------------------------------------------------


def test_confirmed_cause_interventions_returned_when_confirmed():
    case, root_id = _seed_case_with_root("rb_iv")
    _confirm_root(case, root_id)
    assert confirmed_cause_interventions(case) == _INTERVENTIONS


def test_confirmed_cause_interventions_empty_when_only_candidate():
    case, _ = _seed_case_with_root("rb_iv")
    assert confirmed_cause_interventions(case) == []


def test_confirmed_cause_interventions_empty_when_self_discovered():
    case, _ = _seed_case_with_root("rb_unrelated")
    own = _add_unmarked_root(case, "east-region network partition dropped traffic")
    _confirm_root(case, own)
    assert confirmed_cause_interventions(case) == []


def test_confirmed_cause_interventions_via_cluster_duplicate():
    stmt = "database connection pool exhausted under load"
    case, _ = _seed_case_with_root("rb_dbpool", root_stmt=stmt)
    dup = _add_unmarked_root(case, stmt)
    _confirm_root(case, dup)
    assert confirmed_cause_interventions(case) == _INTERVENTIONS


def test_candidate_solutions_block_renders_only_when_confirmed():
    from faultmaven.core.investigation.prompts.context_builder import (
        _build_candidate_solutions_block,
    )

    case, root_id = _seed_case_with_root("rb_render")
    assert _build_candidate_solutions_block(case) == ""
    _confirm_root(case, root_id)
    block = _build_candidate_solutions_block(case)
    assert "<candidate_solutions>" in block
    assert "[remediation] fix the root" in block
    assert "quadrant" in block


def test_candidate_solutions_block_empty_off_investigating():
    from faultmaven.core.investigation.prompts.context_builder import (
        _build_candidate_solutions_block,
    )

    case, root_id = _seed_case_with_root("rb_render")
    _confirm_root(case, root_id)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    assert _build_candidate_solutions_block(case) == ""


# ---------------------------------------------------------------------------
# _handle_runbook_creation step 0 — the sync dedup tier, legacy rows
# ---------------------------------------------------------------------------


class _TitleKnowledgeStub:
    runbook_kb = None

    def __init__(self, titles=None):
        self.titles = titles or {}
        self.title_calls = []

    async def get_runbook_title(self, item_id):
        self.title_calls.append(item_id)
        return self.titles.get(item_id)


def _engine(knowledge_service) -> MilestoneEngine:
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.knowledge_service = knowledge_service
    engine.hypothesis_manager = create_hypothesis_manager()
    engine.runbook_kb = None
    return engine


_ORIGIN = "faultmaven.core.investigation.seeded_provenance.confirmed_root_seed_origin"


@pytest.mark.asyncio
async def test_action_short_circuits_when_confirmed_cause_seeded(monkeypatch):
    """The sync provenance tier fires ABOVE the async similarity backstop: a
    case whose confirmed cause was seeded from runbook X returns the NAMED
    'already covered' message without creating a draft."""
    monkeypatch.setattr(_ORIGIN, lambda case: "rb_cover")
    ks = _TitleKnowledgeStub({"rb_cover": "ArgoCD sync failure"})
    engine = _engine(ks)
    engine.conversion_service = None  # must not be reached — no draft created
    result = await engine._handle_runbook_creation(_case(), {})
    assert ks.title_calls == ["rb_cover"]
    assert "ArgoCD sync failure" in result["agent_response"]
    assert result["suggested_follow_ups"] == []


@pytest.mark.asyncio
async def test_action_message_degrades_when_title_unavailable(monkeypatch):
    monkeypatch.setattr(_ORIGIN, lambda case: "rb_cover")
    ks = _TitleKnowledgeStub({})
    engine = _engine(ks)
    engine.conversion_service = None
    result = await engine._handle_runbook_creation(_case(), {})
    assert "an existing runbook" in result["agent_response"]


@pytest.mark.asyncio
async def test_action_proceeds_when_cause_self_discovered(monkeypatch):
    """No seed origin → the provenance tier is skipped and the normal
    readiness/dedup path runs (stubbed to NOT_READY); no title lookup."""
    from faultmaven.core.investigation import terminal_transitions

    monkeypatch.setattr(_ORIGIN, lambda case: None)

    async def _not_ready(case, runbook_kb=None, scope_resolver=None):
        return terminal_transitions.RunbookSuggestion(
            terminal_transitions.RunbookSuggestion.NOT_READY, "not ready"
        )

    monkeypatch.setattr(terminal_transitions, "evaluate_runbook_suggestion", _not_ready)
    ks = _TitleKnowledgeStub({"rb_cover": "should not be used"})
    engine = _engine(ks)
    engine.conversion_service = None
    result = await engine._handle_runbook_creation(_case(), {})
    assert result["agent_response"] == "not ready"
    assert ks.title_calls == []


# ---------------------------------------------------------------------------
# KnowledgeService.get_runbook_title — names the covering runbook
# ---------------------------------------------------------------------------


class _FakeSessionCM:
    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return False


def _service_with_repo(
    monkeypatch, *, item="__unset__", raises=None
) -> KnowledgeService:
    class _FakeRepo:
        def __init__(self, session):
            pass

        async def get_by_id(self, item_id):
            if raises is not None:
                raise raises
            return None if item == "__unset__" else item

    monkeypatch.setattr(
        "faultmaven.modules.knowledge.infrastructure.persistence."
        "knowledge_item_repository.DatabaseKnowledgeItemRepository",
        _FakeRepo,
    )
    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = lambda: _FakeSessionCM()
    return svc


@pytest.mark.asyncio
async def test_get_runbook_title_returns_title(monkeypatch):
    svc = _service_with_repo(
        monkeypatch, item=SimpleNamespace(title="ArgoCD sync failure")
    )
    assert await svc.get_runbook_title("rb1") == "ArgoCD sync failure"


@pytest.mark.asyncio
async def test_get_runbook_title_none_when_falsy_id(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=SimpleNamespace(title="x"))
    assert await svc.get_runbook_title("") is None


@pytest.mark.asyncio
async def test_get_runbook_title_none_when_item_missing(monkeypatch):
    svc = _service_with_repo(monkeypatch, item=None)
    assert await svc.get_runbook_title("rb1") is None


@pytest.mark.asyncio
async def test_get_runbook_title_none_on_lookup_error(monkeypatch):
    svc = _service_with_repo(monkeypatch, raises=RuntimeError("db down"))
    assert await svc.get_runbook_title("rb1") is None

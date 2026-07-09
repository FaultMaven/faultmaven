"""Anti-anchoring is an ENGINE action, not just a prompt nudge.

When the differential fixates (e.g. 4+ active hypotheses piled into one category),
``_perform_hypothesis_housekeeping`` retires the STALLED hypotheses the anchoring
detector flagged (``force_alternative_generation``), then tells the LLM to
diversify. It fires only on a genuine stall:
  - it stands down while the investigation RECENTLY asked for still-outstanding
    data (and a *stale* unanswered need does not permanently disable it);
  - it acts at most once per cooldown window, tracked by an explicit
    ``progress.last_anti_anchoring_turn`` marker (so the cooldown holds even on a
    turn that retires nothing);
  - it never retires a hypothesis whose chain root is validated (that is the
    grounded cause, not a fixation).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import (
    _ANTI_ANCHORING_COOLDOWN_TURNS,
    MilestoneEngine,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    EvidenceNeed,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NeedPriority,
    NeedPurpose,
    NeedState,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _case(current_turn: int = 10) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="db slow",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="db slow", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = current_turn
    return case


def _hyp(
    hyp_id: str,
    *,
    iters: int = 0,
    state: HypothesisState = HypothesisState.ACTIVE,
    category: HypothesisCategory = HypothesisCategory.DATABASE,
    likelihood: float = 0.4,
    root_node_id: str | None = None,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hyp_id,
        statement="a database theory",
        category=category,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        likelihood=likelihood,
        initial_likelihood=likelihood,
        root_node_id=root_node_id,
        generated_at_turn=1,
        last_updated_turn=1,
        iterations_without_progress=iters,
    )


def _flooded_case(current_turn: int = 10) -> Case:
    """4 ACTIVE hypotheses in one category (trips category anchoring); two have
    stalled (iterations >= 2), two are fresh."""
    case = _case(current_turn)
    hyps = [
        _hyp("hyp_0000000000a1", iters=3),
        _hyp("hyp_0000000000a2", iters=3),
        _hyp("hyp_0000000000a3", iters=0),
        _hyp("hyp_0000000000a4", iters=0),
    ]
    case.hypotheses = {h.hypothesis_id: h for h in hyps}
    return case


def _pending_need(case: Case, *, created_at_turn: int) -> EvidenceNeed:
    return EvidenceNeed(
        case_id=case.case_id,
        purpose=NeedPurpose.SYMPTOM_VERIFICATION,
        request_text="please attach the slow-query log",
        rationale="needed to confirm the symptom",
        priority=NeedPriority.MEDIUM,
        state=NeedState.PENDING,
        created_at_turn=created_at_turn,
    )


def test_anchoring_retires_flagged_stalled_hypotheses_and_marks_the_turn():
    eng, case = _engine(), _flooded_case()
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    retired = {
        h.hypothesis_id
        for h in case.hypotheses.values()
        if h.state == HypothesisState.RETIRED
    }
    # The two stalled flagged hypotheses are retired; the two fresh ones survive.
    assert retired == {"hyp_0000000000a1", "hyp_0000000000a2"}
    # The intervention recorded the turn it fired (drives the cooldown).
    assert case.progress.last_anti_anchoring_turn == case.current_turn
    # The LLM is told to diversify, with an honest retirement count.
    fb = meta.get("system_feedback") or ""
    assert "CRITICAL" in fb and "Retired 2" in fb


def test_recent_outstanding_need_suppresses():
    """A need asked THIS turn that's still outstanding → waiting on the user, not
    fixated → anti-anchoring stands down."""
    eng, case = _engine(), _flooded_case()
    case.evidence_needs = [_pending_need(case, created_at_turn=case.current_turn)]
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    assert all(h.state == HypothesisState.ACTIVE for h in case.hypotheses.values())
    assert case.progress.last_anti_anchoring_turn == 0  # never fired
    assert not meta.get("system_feedback")


def test_stale_outstanding_need_does_not_permanently_suppress():
    """A need asked long ago and never answered must NOT disable anti-anchoring
    forever — once it is no longer 'recent', a genuine fixation is acted on."""
    eng, case = _engine(), _flooded_case(current_turn=10)
    case.evidence_needs = [
        _pending_need(case, created_at_turn=2)  # stale (8 turns old)
    ]
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    retired = {
        h.hypothesis_id
        for h in case.hypotheses.values()
        if h.state == HypothesisState.RETIRED
    }
    assert retired == {"hyp_0000000000a1", "hyp_0000000000a2"}
    assert "CRITICAL" in (meta.get("system_feedback") or "")


def test_cooldown_marker_suppresses_then_expires():
    # Fired last turn → on cooldown → no action.
    eng, case = _engine(), _flooded_case(current_turn=10)
    case.progress.last_anti_anchoring_turn = case.current_turn - 1
    meta: dict = {}
    eng._perform_hypothesis_housekeeping(case, meta)
    assert all(h.state == HypothesisState.ACTIVE for h in case.hypotheses.values())
    assert not meta.get("system_feedback")

    # Window expired → fires again.
    eng2, case2 = _engine(), _flooded_case(current_turn=10)
    case2.progress.last_anti_anchoring_turn = (
        case2.current_turn - _ANTI_ANCHORING_COOLDOWN_TURNS
    )
    meta2: dict = {}
    eng2._perform_hypothesis_housekeeping(case2, meta2)
    assert any(h.state == HypothesisState.RETIRED for h in case2.hypotheses.values())


def test_retire_zero_still_marks_the_turn_so_it_does_not_renag_every_turn():
    """When anchoring trips but every flagged hypothesis is still fresh (nothing
    to retire), the intervention still records the turn — so the cooldown holds
    and it does not re-fire/re-nag the next turn."""
    eng, case = _engine(), _case(current_turn=10)
    # 4 FRESH same-category hypotheses: category anchoring trips, but none stalled.
    case.hypotheses = {
        h.hypothesis_id: h
        for h in [_hyp(f"hyp_00000000000{i}", iters=0) for i in range(4)]
    }
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    assert all(h.state == HypothesisState.ACTIVE for h in case.hypotheses.values())
    # Nothing retired, but the turn is marked and the message claims no retirement.
    assert case.progress.last_anti_anchoring_turn == case.current_turn
    fb = meta.get("system_feedback") or ""
    assert "CRITICAL" in fb and "Retired" not in fb


def test_grounding_validated_root_hypothesis_is_not_retired():
    """A flagged, stalled hypothesis whose chain root is VALIDATED is the grounded
    cause — it must NOT be retired for anchoring (retiring it would discard the
    answer)."""
    eng, case = _engine(), _case(current_turn=10)
    root = CausalNode(
        node_id="cn_000000000001",
        statement="the root cause",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        belief=0.8,
        actionable=True,
        generated_at_turn=1,
    )
    case.causal_nodes = {root.node_id: root}
    grounded = _hyp("hyp_0000000000f0", iters=3, root_node_id=root.node_id)
    others = [_hyp(f"hyp_0000000000a{i}", iters=3) for i in range(3)]
    case.hypotheses = {h.hypothesis_id: h for h in [grounded, *others]}
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    # The grounded (validated-root) hypothesis survives; the others are retired.
    assert case.hypotheses["hyp_0000000000f0"].state == HypothesisState.ACTIVE
    assert all(
        case.hypotheses[h.hypothesis_id].state == HypothesisState.RETIRED
        for h in others
    )


def test_count_held_root_hypothesis_is_not_retired():
    """INV-29: a flagged, stalled hypothesis whose chain root is COUNT-HELD
    (really causally supported, blocked only by the §7.1 independent-support
    bar) is likely the true cause awaiting its second observation — pre-bar it
    would have been VALIDATED and protected, so the raised bar must not feed
    it to forced retirement."""
    from datetime import datetime, timezone

    from faultmaven.modules.case.contracts import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        EvidenceStance,
        NodeEvidenceLink,
    )

    eng, case = _engine(), _case(current_turn=10)
    ev = Evidence(
        evidence_id="ev_" + "a" * 12,
        summary="config diff shows pool max_size dropped to 5",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )
    case.evidence = [ev]
    root = CausalNode(
        node_id="cn_000000000001",
        statement="undersized connection pool exhausts under load",
        node_type=NodeType.ROOT,
        node_state=NodeState.INCONCLUSIVE,  # held by the count bar
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=ev.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the root",
                linked_at_turn=2,
            )
        ],
        generated_at_turn=1,
    )
    case.causal_nodes = {root.node_id: root}
    held = _hyp("hyp_0000000000f1", iters=3, root_node_id=root.node_id)
    others = [_hyp(f"hyp_0000000000b{i}", iters=3) for i in range(3)]
    case.hypotheses = {h.hypothesis_id: h for h in [held, *others]}
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    assert case.hypotheses["hyp_0000000000f1"].state == HypothesisState.ACTIVE
    assert all(
        case.hypotheses[h.hypothesis_id].state == HypothesisState.RETIRED
        for h in others
    )

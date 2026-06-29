"""Anti-anchoring is an ENGINE action, not just a prompt nudge.

When the differential fixates (e.g. 4+ active hypotheses piled into one category),
``_perform_hypothesis_housekeeping`` calls ``force_alternative_generation`` to
actually retire the stalled dominant-category hypotheses, then tells the LLM to
diversify. It fires only on a genuine stall — it stands down while the
investigation is waiting on an outstanding evidence need, and it does not churn
the differential every turn (a cooldown derived from recent retirement state).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    EvidenceNeed,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NeedPriority,
    NeedPurpose,
    NeedState,
    ProblemVerification,
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
        generated_at_turn=1,
        last_updated_turn=1,
        iterations_without_progress=iters,
    )


def _flooded_case(current_turn: int = 10) -> Case:
    """4 ACTIVE hypotheses in one category (trips category anchoring); two have
    stalled (iterations >= 2) so are eligible for forced retirement."""
    case = _case(current_turn)
    hyps = [
        _hyp("hyp_0000000000a1", iters=3),
        _hyp("hyp_0000000000a2", iters=3),
        _hyp("hyp_0000000000a3", iters=0),
        _hyp("hyp_0000000000a4", iters=0),
    ]
    case.hypotheses = {h.hypothesis_id: h for h in hyps}
    return case


def test_anchoring_retires_stalled_dominant_category_and_feeds_back():
    eng, case = _engine(), _flooded_case()
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    retired = [
        h for h in case.hypotheses.values() if h.state == HypothesisState.RETIRED
    ]
    # The two stalled (iterations >= 2) dominant-category hypotheses are retired;
    # the two fresh ones are left alone (never sweep out a new theory).
    assert {h.hypothesis_id for h in retired} == {
        "hyp_0000000000a1",
        "hyp_0000000000a2",
    }
    assert all(
        (h.retirement_reason or "").startswith("Anchoring prevention") for h in retired
    )
    # The LLM is told to diversify.
    assert "CRITICAL" in (meta.get("system_feedback") or "")


def test_anchoring_suppressed_while_evidence_need_outstanding():
    """A pending evidence need means the agent is waiting on requested data —
    progress, not fixation — so anti-anchoring stands down."""
    eng, case = _engine(), _flooded_case()
    case.evidence_needs = [
        EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="please attach the slow-query log",
            rationale="needed to confirm the symptom",
            priority=NeedPriority.MEDIUM,
            state=NeedState.PENDING,
            created_at_turn=case.current_turn,
        )
    ]
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    assert all(
        h.state == HypothesisState.ACTIVE for h in case.hypotheses.values()
    )  # nothing retired
    assert not meta.get("system_feedback")


def test_anchoring_on_cooldown_after_recent_forced_alternative():
    """If a forced-alternative retirement fired within the cooldown window, the
    engine does not retire/diversify again this turn (no per-turn churn)."""
    eng, case = _engine(), _flooded_case(current_turn=10)
    # A hypothesis retired for anchoring one turn ago (within the 2-turn cooldown).
    recent = _hyp(
        "hyp_0000000000b0",
        state=HypothesisState.RETIRED,
        category=HypothesisCategory.NETWORK,
    )
    recent.retirement_reason = "Anchoring prevention: retired to diversify from x"
    recent.last_updated_turn = case.current_turn - 1
    case.hypotheses[recent.hypothesis_id] = recent
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    # Still anchored, but on cooldown → no NEW retirement, no feedback.
    newly_retired = [
        h
        for h in case.hypotheses.values()
        if h.state == HypothesisState.RETIRED
        and h.hypothesis_id != recent.hypothesis_id
    ]
    assert newly_retired == []
    assert not meta.get("system_feedback")


def test_cooldown_expires_after_window():
    """Once the cooldown window has passed, anti-anchoring fires again."""
    eng, case = _engine(), _flooded_case(current_turn=10)
    stale = _hyp(
        "hyp_0000000000b0",
        state=HypothesisState.RETIRED,
        category=HypothesisCategory.NETWORK,
    )
    stale.retirement_reason = "Anchoring prevention: retired to diversify from x"
    stale.last_updated_turn = case.current_turn - 2  # outside the 2-turn window
    case.hypotheses[stale.hypothesis_id] = stale
    meta: dict = {}

    eng._perform_hypothesis_housekeeping(case, meta)

    assert any(
        h.state == HypothesisState.RETIRED
        and h.hypothesis_id.startswith("hyp_0000000000a")
        for h in case.hypotheses.values()
    )
    assert "CRITICAL" in (meta.get("system_feedback") or "")

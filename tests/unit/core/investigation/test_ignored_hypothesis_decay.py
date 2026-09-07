"""An IGNORED active hypothesis must age toward stagnation like a tested one.

``iterations_without_progress`` otherwise advances ONLY when a hypothesis is
engaged (evidence linked / a likelihood update that fails to move belief). A
hypothesis that no turn ever touches keeps the counter at 0 forever, so
``apply_likelihood_decay`` no-ops and stagnation-based anchoring never fires on
it — it lingers at its prior as a permanent unrefuted sibling (#713). The
housekeeping loop now runs an origin-BLIND, age-based sweep
(``advance_stagnation_if_ignored``) so an ignored hypothesis decays and can trip
anchoring the same as a repeatedly-tested one.

These are MECHANICAL engine-state assertions (no LLM output): the sweep decays
CONSERVATIVELY — it can only lower belief over time or stall the theory, and
never validates, refutes, or concludes on the basis of age. It is provenance-
blind: an ignored seeded hypothesis and an ignored self-generated hypothesis
decay identically.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import (
    IGNORED_STAGNATION_TURN_THRESHOLD,
    HypothesisManager,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


# A seeded hypothesis's rationale carries this literal prefix; a self-generated
# one does not. The sweep must behave identically for both — asserting on the
# behavior, not by importing the marker into the engine (which is banned).
_SEEDED_RATIONALE = "Seeded from runbook rb1 (Cause A: undersized pool)"
_SELF_RATIONALE = "Model-proposed theory: undersized pool"


def _engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _case(current_turn: int) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
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
    *,
    likelihood: float = 0.3,
    created_turn: int = 0,
    rationale: str = _SELF_RATIONALE,
    state: HypothesisState = HypothesisState.ACTIVE,
) -> Hypothesis:
    """A hypothesis born at ``created_turn`` and never touched since (its
    last-progress/last-updated turns both sit at creation)."""
    return Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement="undersized connection pool exhausts under load",
        category=HypothesisCategory.OTHER,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale=rationale,
        likelihood=likelihood,
        initial_likelihood=likelihood,
        generated_at_turn=created_turn,
        last_updated_turn=created_turn,
        last_progress_at_turn=created_turn,
        iterations_without_progress=0,
    )


# ---------------------------------------------------------------------------
# advance_stagnation_if_ignored — the sweep itself
# ---------------------------------------------------------------------------


def test_ignored_active_hypothesis_ages_after_threshold_turns():
    hm = HypothesisManager()
    h = _hyp(created_turn=0)
    # Just below the threshold: no aging yet.
    hm.advance_stagnation_if_ignored(h, IGNORED_STAGNATION_TURN_THRESHOLD - 1)
    assert h.iterations_without_progress == 0

    # At the threshold: the counter advances by one.
    hm.advance_stagnation_if_ignored(h, IGNORED_STAGNATION_TURN_THRESHOLD)
    assert h.iterations_without_progress == 1
    assert h.last_updated_turn == IGNORED_STAGNATION_TURN_THRESHOLD


def test_recently_touched_hypothesis_is_not_swept_this_turn():
    """A hypothesis the current turn already touched (evidence path or creation)
    must not be double-advanced by the sweep in the same turn."""
    hm = HypothesisManager()
    current = 8
    # last_updated_turn == current_turn simulates "touched this turn".
    h = _hyp(created_turn=0)
    h.last_progress_at_turn = 0  # stale progress (age well past threshold)
    h.last_updated_turn = current
    hm.advance_stagnation_if_ignored(h, current)
    assert h.iterations_without_progress == 0


def test_sweep_does_not_touch_non_active_hypotheses():
    hm = HypothesisManager()
    for state in (
        HypothesisState.RETIRED,
        HypothesisState.CAPTURED,
        HypothesisState.INCONCLUSIVE,
    ):
        h = _hyp(created_turn=0, state=state)
        hm.advance_stagnation_if_ignored(h, 50)
        assert h.iterations_without_progress == 0


def test_sweep_never_raises_likelihood():
    """The sweep only advances a counter — it never touches belief upward."""
    hm = HypothesisManager()
    h = _hyp(likelihood=0.3, created_turn=0)
    before = h.likelihood
    hm.advance_stagnation_if_ignored(h, 20)
    assert h.likelihood == before  # counter-only; decay is a separate step


# ---------------------------------------------------------------------------
# housekeeping — decay actually happens for an ignored hypothesis
# ---------------------------------------------------------------------------


def test_ignored_hypothesis_decays_through_housekeeping():
    """An ignored ACTIVE hypothesis loses belief over successive stagnant turns
    (the #713 defect: previously it never moved)."""
    eng = _engine()
    h = _hyp(likelihood=0.3, created_turn=0)
    prior = h.likelihood

    # Turns before the threshold: no decay yet.
    for turn in range(1, IGNORED_STAGNATION_TURN_THRESHOLD):
        case = _case(turn)
        case.hypotheses = {h.hypothesis_id: h}
        eng._perform_hypothesis_housekeeping(case, {})
        assert h.likelihood == prior
        assert h.iterations_without_progress == 0

    # From the threshold on, belief strictly decays each turn while the theory
    # is ignored — and never rises above where it started.
    last = h.likelihood
    for turn in range(
        IGNORED_STAGNATION_TURN_THRESHOLD, IGNORED_STAGNATION_TURN_THRESHOLD + 2
    ):
        case = _case(turn)
        case.hypotheses = {h.hypothesis_id: h}
        eng._perform_hypothesis_housekeeping(case, {})
        assert h.likelihood < last
        assert h.likelihood <= prior
        last = h.likelihood


def test_recently_touched_hypothesis_does_not_decay():
    """A hypothesis engaged THIS turn (fresh progress) keeps its belief — decay
    is for stagnation, not for every turn."""
    eng = _engine()
    turn = 10
    h = _hyp(likelihood=0.3, created_turn=0)
    # Fresh progress THIS turn: age is zero and it was touched this turn.
    h.last_progress_at_turn = turn
    h.last_updated_turn = turn
    before = h.likelihood
    case = _case(turn)
    case.hypotheses = {h.hypothesis_id: h}
    eng._perform_hypothesis_housekeeping(case, {})
    assert h.likelihood == before
    assert h.iterations_without_progress == 0


def test_age_decay_never_validates_refutes_or_concludes():
    """Even after many ignored turns, age-decay only stalls/soft-retires — it
    never marks the hypothesis VALIDATED or REFUTED, never fabricates a
    refutation_reason, and floors belief rather than driving it negative."""
    eng = _engine()
    h = _hyp(likelihood=0.3, created_turn=0)
    for turn in range(1, 30):
        case = _case(turn)
        case.hypotheses = {h.hypothesis_id: h}
        eng._perform_hypothesis_housekeeping(case, {})
        # NO INCORRECT CONCLUSION: age alone never validates or refutes.
        assert h.state not in (HypothesisState.VALIDATED, HypothesisState.REFUTED)
        assert h.refutation_reason is None
        # CONSERVATIVE: belief only ever falls, floored at the decay floor.
        assert 0.1 <= h.likelihood <= 0.3


def test_ignored_hypothesis_eventually_trips_stagnation_anchoring():
    """The other half of #713: an ignored hypothesis must be able to TRIP
    stagnation-based anchoring. After enough ignored turns its stagnation counter
    crosses the anchoring horizon and the anti-anchoring intervention soft-retires
    it (a stall, not a wrong answer)."""
    eng = _engine()
    h = _hyp(likelihood=0.3, created_turn=0)
    retired = False
    for turn in range(1, 30):
        case = _case(turn)
        case.hypotheses = {h.hypothesis_id: h}
        meta: dict = {}
        eng._perform_hypothesis_housekeeping(case, meta)
        if h.state == HypothesisState.RETIRED:
            retired = True
            # Retirement is an anti-anchoring soft-retire, never a refutation.
            assert h.refutation_reason is None
            break
    assert retired, "an ignored hypothesis never tripped stagnation anchoring"


def test_captured_promotion_starts_a_fresh_stagnation_clock():
    """A hypothesis queued CAPTURED before symptom verification must not bank the
    turns it spent queued: on promotion to ACTIVE its stagnation clock restarts,
    so it gets the same grace as a freshly-created ACTIVE candidate and is NOT
    instantly aged/decayed on its first active turn."""
    hm = HypothesisManager()
    eng = _engine()

    # Created CAPTURED at turn 0; the case then spends several turns verifying the
    # symptom before promotion at turn 5.
    h = _hyp(created_turn=0, state=HypothesisState.CAPTURED)
    promote_turn = 5
    case = _case(promote_turn)
    case.hypotheses = {h.hypothesis_id: h}

    promoted = hm.activate_queued_hypotheses(case)
    assert promoted == [h.hypothesis_id]
    # Clock restarted at activation — not left at the creation turn.
    assert h.last_progress_at_turn == promote_turn
    assert h.last_updated_turn == promote_turn

    prior = h.likelihood
    # First ACTIVE housekeeping turn (same turn as promotion): no aging yet — the
    # age since (re)start is 0, well under the threshold.
    eng._perform_hypothesis_housekeeping(case, {})
    assert h.iterations_without_progress == 0
    assert h.likelihood == prior

    # It only ages after the SAME threshold a fresh ACTIVE candidate would.
    grace_turn = promote_turn + IGNORED_STAGNATION_TURN_THRESHOLD - 1
    case2 = _case(grace_turn)
    case2.hypotheses = {h.hypothesis_id: h}
    eng._perform_hypothesis_housekeeping(case2, {})
    assert h.iterations_without_progress == 0
    assert h.likelihood == prior


# ---------------------------------------------------------------------------
# provenance-blindness — a seed decays exactly like a self-generated hypothesis
# ---------------------------------------------------------------------------


def test_ignored_seed_and_self_generated_decay_identically():
    """The sweep must be origin-blind: two hypotheses identical except that one
    carries a seeded-runbook rationale prefix must decay in lockstep across every
    housekeeping turn — same counter, same belief, same state."""
    eng = _engine()
    seeded = _hyp(likelihood=0.3, created_turn=0, rationale=_SEEDED_RATIONALE)
    self_gen = _hyp(likelihood=0.3, created_turn=0, rationale=_SELF_RATIONALE)

    for turn in range(1, 12):
        # Drive each in its own single-hypothesis case so anchoring dynamics are
        # identical and isolated; only the rationale differs.
        for h in (seeded, self_gen):
            case = _case(turn)
            case.hypotheses = {h.hypothesis_id: h}
            eng._perform_hypothesis_housekeeping(case, {})

        assert (
            seeded.iterations_without_progress == self_gen.iterations_without_progress
        )
        assert seeded.likelihood == self_gen.likelihood
        assert seeded.state == self_gen.state

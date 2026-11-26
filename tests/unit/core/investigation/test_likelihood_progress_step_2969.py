"""A likelihood move of exactly 0.05 is a step, not a restatement (#2969).

Float subtraction puts an exact two-decimal 0.05 move just under 0.05
(``abs(0.25 - 0.30) == 0.04999999999999999``), so a raw ``>= 0.05`` read it as
a restatement on the model path, the evidence path and the ``lowered`` test.
The tolerance is pinned from both sides: a move of 0.05 - 1e-6 is a
restatement, and a move within the 1e-9 product tolerance (0.05 - 1e-10) is a
step.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import (
    STAGNATION_HORIZON_ITERATIONS,
    HypothesisManager,
)
from faultmaven.modules.case.contracts import (
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
)

pytestmark = pytest.mark.unit

_GRID = [round(i * 0.05, 2) for i in range(21)]
_STEPS = [
    (old, new)
    for old in _GRID
    for new in (round(old - 0.05, 2), round(old + 0.05, 2))
    if 0.0 <= new <= 1.0
]


def _moves(*deltas: float) -> list[tuple[float, float]]:
    """Every (old, new) with old on the grid and new = old ± delta in [0, 1]."""
    return [
        (old, old + sign * delta)
        for old in _GRID
        for delta in deltas
        for sign in (-1, 1)
        if 0.0 <= old + sign * delta <= 1.0
    ]


_RESTATEMENTS = _moves(0.01, 0.04, 0.049, 0.0499, 0.04999, 0.05 - 1e-6)
_IN_BAND = _moves(0.05 - 1e-10)
_HEDGED_OLDS = [old for old in _GRID if 0.05 <= old <= 0.3]
_TURN = 7


def _link(stance: EvidenceStance, confidence: float) -> HypothesisEvidenceLink:
    return HypothesisEvidenceLink(
        hypothesis_id="hyp",
        evidence_id=f"ev_{uuid4().hex[:8]}",
        stance=stance,
        reasoning="r",
        stance_confidence=confidence,
    )


def _hypothesis(likelihood: float, initial: float, *links) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement="the connection pool is exhausted",
        category=HypothesisCategory.OTHER,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="pool metrics",
        likelihood=likelihood,
        initial_likelihood=initial,
        evidence_links=list(links),
        generated_at_turn=0,
        last_updated_turn=0,
        last_progress_at_turn=0,
        iterations_without_progress=2,
    )


def _supported(likelihood: float) -> Hypothesis:
    """A qualifying SUPPORTS link, so a raise is not capped."""
    return _hypothesis(likelihood, 0.5, _link(EvidenceStance.SUPPORTS, 0.9))


@pytest.mark.parametrize(("old", "new"), _STEPS + _IN_BAND)
def test_a_model_move_of_exactly_one_step_is_progress(old, new):
    h = _supported(old)

    HypothesisManager().update_hypothesis_likelihood(h, new, _TURN, "test result")

    assert h.likelihood == new
    assert h.iterations_without_progress == 0
    assert h.last_updated_turn == _TURN
    assert h.last_progress_at_turn == _TURN


@pytest.mark.parametrize(("old", "new"), _RESTATEMENTS)
def test_a_model_move_below_a_step_is_a_restatement(old, new):
    h = _supported(old)

    HypothesisManager().update_hypothesis_likelihood(h, new, _TURN, "test result")

    assert h.likelihood == new
    assert h.iterations_without_progress == 2
    assert h.last_updated_turn == 0
    assert h.last_progress_at_turn == 0


@pytest.mark.parametrize(
    ("stored", "initial", "stance", "expected"),
    [
        (0.35, 0.5, EvidenceStance.REFUTES, 0.3),
        (0.7, 0.5, EvidenceStance.SUPPORTS, 0.65),
        (0.45, 0.35, EvidenceStance.SUPPORTS, 0.5),
    ],
)
def test_an_evidence_move_of_exactly_one_step_is_progress(
    stored, initial, stance, expected
):
    h = _hypothesis(stored, initial)

    HypothesisManager().link_evidence(h, "ev_new", stance, _TURN)

    assert h.likelihood == pytest.approx(expected)
    assert h.iterations_without_progress == 0
    assert h.last_progress_at_turn == _TURN
    assert h.state == HypothesisState.ACTIVE


@pytest.mark.parametrize(("stored", "initial"), _STEPS)
def test_an_evidence_update_of_exactly_one_step_is_progress(stored, initial):
    """No links, so the evidence formula returns ``initial_likelihood``."""
    h = _hypothesis(stored, initial)

    HypothesisManager().update_likelihood_from_evidence(h, _TURN)

    assert h.likelihood == initial
    assert h.iterations_without_progress == 0
    assert h.last_progress_at_turn == _TURN


@pytest.mark.parametrize(
    ("delta", "state"),
    [(0.05, HypothesisState.RETIRED), (0.04, HypothesisState.ACTIVE)],
)
@pytest.mark.parametrize("old", _HEDGED_OLDS)
def test_a_model_lowering_of_one_step_retires_past_a_hedged_refutes(old, delta, state):
    """The hedged REFUTES stands and the confident-only formula gives 0.50, so
    only the model's own lowering of 0.05 or more retires."""
    h = _hypothesis(old, 0.5, _link(EvidenceStance.REFUTES, 0.3))
    new = round(old - delta, 2)

    HypothesisManager().update_hypothesis_likelihood(h, new, _TURN, "test result")

    assert h.state == state


def _refuted_twice(likelihood: float) -> Hypothesis:
    refutes = [_link(EvidenceStance.REFUTES, 0.9) for _ in range(2)]
    return _hypothesis(likelihood, 0.7, *refutes)


def test_the_evidence_formula_lands_on_the_grid_value_3103():
    """0.7 - 2 x 0.20 is 0.29999999999999993 in floats, under the 0.30 bar."""
    h = _hypothesis(0.7, 0.7)
    manager = HypothesisManager()

    manager.link_evidence(h, "ev_1", EvidenceStance.REFUTES, 1)
    manager.link_evidence(h, "ev_2", EvidenceStance.REFUTES, 2)

    assert h.likelihood == 0.3
    assert h.state == HypothesisState.ACTIVE


def test_a_stagnant_hypothesis_at_the_band_edge_is_inconclusive_3103():
    h = _refuted_twice(0.3)
    h.iterations_without_progress = STAGNATION_HORIZON_ITERATIONS

    HypothesisManager().update_likelihood_from_evidence(h, _TURN)

    assert h.likelihood == 0.3
    assert h.state == HypothesisState.INCONCLUSIVE

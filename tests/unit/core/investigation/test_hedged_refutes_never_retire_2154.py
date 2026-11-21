"""A self-hedged REFUTES link never retires a hypothesis by itself (#2154).

Owner ruling 2026-10-22, option (b): a REFUTES link below
``CAUSAL_STANCE_CONFIDENCE_MIN`` still lowers likelihood by the same step, but
the low-confidence retirement fires through an evidence link, a model update
that restates or raises the likelihood, or a link crossing the hedge bar only
when the evidence likelihood with the hedged refutes left out is also below
0.30. A model lowering by 0.05 or more, and a hypothesis with no hedged
REFUTES link, transition as before; the age-out is unchanged.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.modules.case.contracts import (
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _DSU,
    _apply,
    _case,
    _engine,
    _link,
    _on_record,
    _refute,
    _standing,
)

pytestmark = pytest.mark.unit

_SUPPORTS, _REFUTES = EvidenceStance.SUPPORTS, EvidenceStance.REFUTES
_HEDGED, _CONFIDENT = 0.3, 0.9


def _hypothesis(likelihood: float = 0.5) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement="the connection pool is exhausted",
        category=HypothesisCategory.OTHER,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="pool metrics",
        likelihood=likelihood,
        initial_likelihood=likelihood,
        generated_at_turn=0,
        last_updated_turn=0,
        last_progress_at_turn=0,
        iterations_without_progress=0,
    )


def _refutes(manager, hypothesis, *confidences, turn: int = 1) -> list[tuple]:
    """Link one REFUTES per confidence, a turn apart; return (likelihood, state)
    after each."""
    seen = []
    for i, confidence in enumerate(confidences):
        manager.link_evidence(
            hypothesis,
            f"ev_{i}",
            _REFUTES,
            turn + i,
            stance_confidence=confidence,
        )
        seen.append((round(hypothesis.likelihood, 6), hypothesis.state))
    return seen


def _held() -> tuple[HypothesisManager, Hypothesis]:
    """The reviewer's reproduction: 0.5, two hedged REFUTES, held at 0.1."""
    manager, h = HypothesisManager(), _hypothesis()
    _refutes(manager, h, _HEDGED, _HEDGED)
    return manager, h


def test_the_reproduction_two_hedged_refutes_lower_but_never_retire():
    manager, h = HypothesisManager(), _hypothesis()

    seen = _refutes(manager, h, _HEDGED, _HEDGED)

    assert seen == [
        (0.3, HypothesisState.ACTIVE),
        (0.1, HypothesisState.ACTIVE),
    ]


def test_confident_refutation_still_retires():
    manager, h = HypothesisManager(), _hypothesis()

    seen = _refutes(manager, h, _CONFIDENT, _CONFIDENT)

    assert seen[-1] == (0.1, HypothesisState.RETIRED)


def test_hedged_then_confident_retires_only_at_the_fourth_link():
    """Likelihood reaches the 0.0 clamp at the third link and is held; the
    confident-only formula falls below the bar only with the second confident
    link."""
    manager, h = HypothesisManager(), _hypothesis()

    seen = _refutes(manager, h, _HEDGED, _HEDGED, _CONFIDENT, _CONFIDENT)

    assert [state for _, state in seen] == [HypothesisState.ACTIVE] * 3 + [
        HypothesisState.RETIRED
    ]
    assert seen[2][0] == 0.0


def test_two_confident_and_two_hedged_retire_at_the_clamp():
    manager, h = HypothesisManager(), _hypothesis()

    seen = _refutes(manager, h, _CONFIDENT, _HEDGED, _HEDGED, _CONFIDENT)

    assert seen[-1] == (0.0, HypothesisState.RETIRED)


def test_one_confident_and_two_hedged_stay_active():
    """Without the hedged links the formula reads 0.3: not below the bar."""
    manager, h = HypothesisManager(), _hypothesis()

    seen = _refutes(manager, h, _CONFIDENT, _HEDGED, _HEDGED)

    assert seen[-1] == (0.0, HypothesisState.ACTIVE)


def test_a_prior_below_the_bar_still_retires_on_a_hedged_refutes():
    """The confident-only formula is the 0.25 prior itself, already below the
    bar, so the low prior is the cause (a NEUTRAL link retires it the same)."""
    manager, h = HypothesisManager(), _hypothesis(0.25)

    seen = _refutes(manager, h, _HEDGED)

    assert seen == [(0.05, HypothesisState.RETIRED)]


def test_the_float_edge_reads_as_on_main():
    """0.4 + 2 × 0.15 − 2 × 0.20 is 0.29999999999999993 in floats: below the
    bar, with or without a hedged REFUTES beside the confident ones."""
    for extra in ((), (_HEDGED,)):
        manager, h = HypothesisManager(), _hypothesis(0.4)
        manager.link_evidence(h, "s1", _SUPPORTS, 1)
        manager.link_evidence(h, "s2", _SUPPORTS, 1)

        _refutes(manager, h, _CONFIDENT, _CONFIDENT, *extra, turn=2)

        assert h.state == HypothesisState.RETIRED


def test_hedged_links_re_emitted_confident_retire():
    """Crossing the hedge bar at an unchanged stance re-checks the transition."""
    manager, h = _held()

    for evidence_id in ("ev_0", "ev_1"):
        manager.link_evidence(h, evidence_id, _REFUTES, 3, stance_confidence=0.9)

    assert h.state == HypothesisState.RETIRED


def test_firming_re_checks_only_the_retirement():
    """Firming a hedged REFUTES leaves the formula where it was: no recompute,
    so no stagnant iteration and no INCONCLUSIVE, and 0.4 does not retire."""
    manager, h = HypothesisManager(), _hypothesis(0.6)
    _refutes(manager, h, _HEDGED)
    h.iterations_without_progress = 2
    before = (h.likelihood, h.iterations_without_progress, h.last_updated_turn)

    manager.link_evidence(h, "ev_0", _REFUTES, 5, stance_confidence=_CONFIDENT)

    assert h.state == HypothesisState.ACTIVE
    assert (h.likelihood, h.iterations_without_progress, h.last_updated_turn) == (
        before
    )


@pytest.mark.parametrize("confidence, material", [(_CONFIDENT, False), (_HEDGED, True)])
def test_a_stored_unset_confidence_reads_confident_for_materiality(
    confidence, material
):
    """``None`` reads as full confidence (``stance_declared_confident``):
    re-sent confident it is restatement, re-sent hedged it crosses the bar."""
    manager, h = HypothesisManager(), _hypothesis()
    manager.link_evidence(h, "ev_0", _REFUTES, 1)
    h.evidence_links[0].stance_confidence = None

    assert (
        manager.link_evidence(h, "ev_0", _REFUTES, 2, stance_confidence=confidence)
        is material
    )


def test_a_crossing_under_hold_state_leaves_the_transition_to_the_caller():
    manager, h = _held()

    for evidence_id in ("ev_0", "ev_1"):
        manager.link_evidence(
            h, evidence_id, _REFUTES, 3, stance_confidence=0.9, hold_state=True
        )

    assert h.state == HypothesisState.ACTIVE


@pytest.mark.parametrize("restated", [0.1, 0.12])
def test_a_restated_model_likelihood_does_not_retire_a_held_hypothesis(restated):
    manager, h = _held()

    manager.update_hypothesis_likelihood(h, restated, 3, "restated")

    assert h.state == HypothesisState.ACTIVE


def test_a_model_raise_still_below_the_bar_keeps_the_hold():
    manager, h = _held()

    manager.update_hypothesis_likelihood(h, 0.2, 3, "raised")

    assert (h.likelihood, h.state) == (0.2, HypothesisState.ACTIVE)


def test_a_real_model_lowering_retires_as_on_main():
    """0.5 with one hedged REFUTES reads 0.3; the model's own 0.2 retires it."""
    manager, h = HypothesisManager(), _hypothesis()
    _refutes(manager, h, _HEDGED)

    manager.update_hypothesis_likelihood(h, 0.2, 2, "lowered")

    assert (h.likelihood, h.state) == (0.2, HypothesisState.RETIRED)


def test_a_model_lowering_without_hedged_links_retires_as_on_main():
    manager, h = HypothesisManager(), _hypothesis()

    manager.update_hypothesis_likelihood(h, 0.2, 2, "lowered")

    assert h.state == HypothesisState.RETIRED


def test_the_held_hypothesis_ages_out_at_the_horizon():
    """Ignored after its last link (turn 2), the held hypothesis ages out at
    turn 7, as the unchanged age-out decides."""
    manager, h = _held()
    case = investigating_case(statement="x", symptom="x", current_turn=2)
    case.hypotheses[h.hypothesis_id] = h

    aged_out_at = None
    for turn in range(3, 12):
        manager.advance_stagnation_if_ignored(h, turn, case, turn_counts=True)
        manager.apply_likelihood_decay(h, turn)
        if manager.retire_if_aged_out(h, case, turn):
            aged_out_at = turn
            break

    assert aged_out_at == 7
    assert h.state == HypothesisState.RETIRED


async def test_a_refused_refutation_on_hedged_links_leaves_it_open():
    """The hold-state path (#1911): two hedged REFUTES links and a same-emission
    refutation. The refutation is refused, and the held transition then finds
    the retirement unsupported."""
    eng, case = _engine(), _case()
    h_id = _standing(case, likelihood=0.5)
    rows = [_on_record(case, f"gateway rejects request {i}") for i in range(2)]

    meta = await _apply(
        eng,
        case,
        _DSU(
            hypothesis_evidence_links=[_link(h_id, r, confidence=0.4) for r in rows],
            hypotheses_to_update=[_refute(h_id)],
        ),
    )

    h = case.hypotheses[h_id]
    assert (round(h.likelihood, 6), h.state) == (0.1, HypothesisState.ACTIVE)
    assert "It stays open." in "\n".join(meta["system_notices"])

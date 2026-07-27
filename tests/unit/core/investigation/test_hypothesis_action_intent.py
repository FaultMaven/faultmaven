"""Unit tests for ``_apply_hypothesis_action_intent`` — the explicit user
``hypothesis_action`` intent path (frontend/IntentResolver).

Pins the terminal-state guard (#843): terminal is immutable from EVERY write
path, not just the LLM apply layer. Before the guard, ``action == "retire"``
wrote unconditionally, so retiring an already-REFUTED hypothesis stranded
``refutation_reason`` on ``state=RETIRED`` — a pair the domain model rejects.
Because ``validate_assignment`` is off, the in-place write succeeded silently
and the corruption surfaced as a 500 at the next Case reconstruction, far
from its cause.

The guard is pinned for BOTH terminal states and every action (retire /
refute / validate), plus the reconstruction round-trip that was the original
failure.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
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


def _make_engine() -> MilestoneEngine:
    """Bare engine — only the intent helper is exercised. __init__ takes many
    deps, so bypass it and wire just the hypothesis manager the helper uses."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _make_case() -> Case:
    inquiry = InquiryData(
        proposed_problem_statement="Deploy to on-prem job fails",
        problem_statement_confirmed=True,
        decided_to_investigate=True,
    )
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        organization_id="org_test",
        title="Deploy fails",
        description="The 'Deploy to on-prem' job is failing",
        state=CaseState.INVESTIGATING,
        inquiry=inquiry,
        problem_verification=ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 7
    return case


def _hyp(state: HypothesisState, **overrides) -> Hypothesis:
    fields = dict(
        statement="NetworkPolicy blocks the connection",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="initial",
        state=state,
        likelihood=0.7,
    )
    fields.update(overrides)
    return Hypothesis(**fields)


def _refuted_hyp() -> Hypothesis:
    return _hyp(
        HypothesisState.REFUTED,
        refutation_reason="disproved by evidence",
        likelihood=0.0,
    )


def _retired_hyp() -> Hypothesis:
    return _hyp(
        HypothesisState.RETIRED,
        retirement_reason="investigation moved on",
        likelihood=0.2,
    )


def _apply(eng, case, hypothesis_id, action, user_message="user says so"):
    metadata: dict = {}
    eng._apply_hypothesis_action_intent(
        case,
        {"hypothesis_id": hypothesis_id, "action": action},
        user_message,
        metadata,
    )
    return metadata


# ---------------------------------------------------------------------------
# The terminal guard (#843): both terminal states x every action.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_terminal", [_refuted_hyp, _retired_hyp])
@pytest.mark.parametrize("action", ["retire", "refute", "validate"])
def test_terminal_hypothesis_refuses_every_action(make_terminal, action):
    eng = _make_engine()
    case = _make_case()
    h = make_terminal()
    case.hypotheses = {h.hypothesis_id: h}
    before = h.model_dump()

    metadata = _apply(eng, case, h.hypothesis_id, action)

    # Nothing on the hypothesis moved — state, reasons, likelihood all intact.
    assert h.model_dump() == before
    # The action was refused, not silently swallowed as applied.
    assert "hypothesis_action_applied" not in metadata
    fb = metadata.get("system_feedback", "")
    assert "terminal" in fb.lower()
    assert h.state.value in fb
    # The aggregate still reconstructs — the original #843 failure was a
    # ValidationError here (stranded refutation_reason on state=RETIRED),
    # surfacing as a 500 at the next Case save round-trip.
    Hypothesis(**h.model_dump())  # must not raise


def test_retire_on_refuted_does_not_strand_refutation_reason():
    """The exact #843 repro: user clicks 'retire' on a hypothesis the engine
    already refuted. Pre-guard this wrote state=RETIRED while
    refutation_reason stayed populated — rejected by the domain model on
    reconstruction."""
    eng = _make_engine()
    case = _make_case()
    h = _refuted_hyp()
    case.hypotheses = {h.hypothesis_id: h}

    _apply(eng, case, h.hypothesis_id, "retire")

    assert h.state == HypothesisState.REFUTED
    assert h.refutation_reason == "disproved by evidence"
    assert h.retirement_reason is None
    Hypothesis(**h.model_dump())  # must not raise


def test_refute_on_retired_is_refused():
    """The mirror-image case: 'refute' on an already-RETIRED hypothesis is a
    terminal->terminal mutation and is refused the same way."""
    eng = _make_engine()
    case = _make_case()
    h = _retired_hyp()
    case.hypotheses = {h.hypothesis_id: h}

    _apply(eng, case, h.hypothesis_id, "refute")

    assert h.state == HypothesisState.RETIRED
    assert h.refutation_reason is None
    assert h.retirement_reason == "investigation moved on"


def test_validate_on_terminal_does_not_touch_likelihood():
    """'validate' writes likelihood=1.0 on a live hypothesis; on a REFUTED
    one that would assert full belief in a disproven cause."""
    eng = _make_engine()
    case = _make_case()
    h = _refuted_hyp()  # likelihood 0.0
    case.hypotheses = {h.hypothesis_id: h}

    _apply(eng, case, h.hypothesis_id, "validate")

    assert h.likelihood == 0.0


# ---------------------------------------------------------------------------
# Live hypotheses: the three actions still work.
# ---------------------------------------------------------------------------


def test_retire_on_active_applies():
    eng = _make_engine()
    case = _make_case()
    h = _hyp(HypothesisState.ACTIVE)
    case.hypotheses = {h.hypothesis_id: h}

    metadata = _apply(eng, case, h.hypothesis_id, "retire", "not it after all")

    assert h.state == HypothesisState.RETIRED
    assert h.retirement_reason == "not it after all"
    assert h.last_updated_turn == case.current_turn
    assert metadata["hypothesis_action_applied"] is True


def test_refute_on_active_applies_via_canonical_path():
    eng = _make_engine()
    case = _make_case()
    h = _hyp(HypothesisState.ACTIVE)
    case.hypotheses = {h.hypothesis_id: h}

    metadata = _apply(eng, case, h.hypothesis_id, "refute", "logs disprove it")

    assert h.state == HypothesisState.REFUTED
    assert h.refutation_reason == "logs disprove it"
    assert h.likelihood == 0.0  # canonical refute zeroes it
    assert metadata["hypothesis_action_applied"] is True


def test_validate_on_active_records_strong_prior():
    eng = _make_engine()
    case = _make_case()
    h = _hyp(HypothesisState.ACTIVE)
    case.hypotheses = {h.hypothesis_id: h}

    metadata = _apply(eng, case, h.hypothesis_id, "validate")

    # #695 Defect A: a strong PRIOR, not validation-by-assertion.
    assert h.state == HypothesisState.ACTIVE
    assert h.likelihood == 1.0
    assert metadata["hypothesis_action_applied"] is True
    assert "validated once" in metadata.get("system_feedback", "")


def test_unknown_hypothesis_id_is_a_noop():
    eng = _make_engine()
    case = _make_case()
    h = _hyp(HypothesisState.ACTIVE)
    case.hypotheses = {h.hypothesis_id: h}

    metadata = _apply(eng, case, "hyp_doesnotexist", "retire")

    assert h.state == HypothesisState.ACTIVE
    assert "hypothesis_action_applied" not in metadata

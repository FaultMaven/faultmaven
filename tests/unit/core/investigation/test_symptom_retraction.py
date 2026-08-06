"""The symptom claim must be able to fall when it no longer holds.

``symptom_verified`` was a one-way latch: nothing could lower it once set, so a
verification that later proved not to hold stayed on the case and kept the
investigation pointed at a cause for a condition that was not occurring.

The downstream layers were already built for withdrawal — the backstop legs in
``cause_identification_leg`` are gated on a verified symptom precisely so a
conclusion left behind after a withdrawn claim stops counting. It was simply
unreachable.
"""

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import _apply_symptom_retraction
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


def _case(verified=True) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout 500s", severity=CaseSeverity.HIGH
        ),
    )
    case.progress.symptom_verified = verified
    return case


# The engine deliberately does not judge the CONTENT of a justification, so any
# non-blank string exercises the guard. The exemplar is still a claim-was-wrong
# rationale rather than "it is not happening right now": the latter is exactly
# what the prompt forbids retracting on (a problem is investigable while it
# EXISTS), and a canonical example should not model the banned case.
def _response(justification=None):
    reasoning = SimpleNamespace(
        milestone_justifications=(
            {"symptom_verified": justification} if justification is not None else {}
        )
    )
    return SimpleNamespace(internal_reasoning=reasoning)


def _apply(case, claimed, justification=None):
    meta: dict = {}
    applied = _apply_symptom_retraction(
        case,
        SimpleNamespace(symptom_verified=claimed),
        _response(justification),
        meta,
    )
    return applied, meta


# -- the retraction ----------------------------------------------------------
def test_justified_false_retracts_the_claim():
    case = _case()
    applied, meta = _apply(
        case,
        False,
        "Misread the dashboard — those 500s were the staging cluster, not prod.",
    )
    assert applied is True
    assert case.progress.symptom_verified is False
    assert meta["milestones_retracted"] == ["symptom_verified"]


# -- guard 1: only an EXPLICIT false ------------------------------------------
def test_absent_field_changes_nothing():
    """The overwhelmingly common shape. ``Optional[bool]`` makes "absent" and
    "false" distinguishable, and only the latter is a decision."""

    case = _case()
    applied, meta = _apply(case, None, "irrelevant")
    assert applied is False
    assert case.progress.symptom_verified is True
    assert "milestones_retracted" not in meta


def test_true_is_not_a_retraction():
    case = _case()
    applied, _ = _apply(case, True, "still failing")
    assert applied is False
    assert case.progress.symptom_verified is True


# -- guard 2: a justification separates judgement from a provider default -----
def test_unjustified_false_is_refused():
    """Providers differ in how eagerly they populate optional booleans, and
    some emit ``false`` by habit. A retraction discards real progress, so an
    unexplained one is treated as a default rather than a decision."""

    case = _case()
    applied, meta = _apply(case, False, justification=None)
    assert applied is False
    assert case.progress.symptom_verified is True
    assert "milestones_retracted" not in meta


def test_blank_justification_is_refused():
    case = _case()
    applied, _ = _apply(case, False, "   ")
    assert applied is False
    assert case.progress.symptom_verified is True


# -- idempotence / no-op safety ----------------------------------------------
def test_retracting_an_unset_claim_is_a_no_op():
    case = _case(verified=False)
    applied, meta = _apply(case, False, "already not verified")
    assert applied is False
    assert "milestones_retracted" not in meta


# -- the downstream layers that were waiting for this -------------------------
def test_retraction_withdraws_the_backstop_cause_legs():
    """``cause_identification_leg``'s rcc / working_conclusion legs are gated on
    a verified symptom so a conclusion "left behind after a symptom claim is
    withdrawn" stops counting. That gate could never fire before."""

    from faultmaven.core.investigation.terminal_transitions import (
        cause_identification_leg,
    )
    from faultmaven.modules.case.contracts import ConfidenceLevel, RootCauseConclusion

    case = _case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="connection pool exhausted",
        mechanism="m",
        evidence_basis=[],
        likelihood=0.8,
        confidence_level=ConfidenceLevel.from_score(0.8),
    )
    assert cause_identification_leg(case) == "rcc"

    _apply(
        case,
        False,
        "Misread the dashboard — those 500s were the staging cluster, not prod.",
    )
    assert cause_identification_leg(case) is None


def test_retraction_makes_the_case_not_grounded():
    """``verification_status._is_grounded`` reads the same anchor."""

    from faultmaven.core.investigation.verification_status import _is_grounded

    case = _case()
    _apply(
        case,
        False,
        "Misread the dashboard — those 500s were the staging cluster, not prod.",
    )
    assert _is_grounded(case) is False

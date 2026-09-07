"""INV-41 (#673 retirement gate) — cause-identification leg + backstop-reliance.

#673 (retire the LLM free-text conclusion, derive it from the validated chain) is
gated on "reliable chain-grounding". ``cause_identification_leg`` makes that gate
measurable: at each RESOLVED transition it reports which leg licensed the
resolution — the validated ``chain`` (cause_state=IDENTIFIED), or the ``rcc`` /
``working_conclusion`` backstop, or ``None``. The per-provider backstop-reliance
rate ``(rcc + working_conclusion) / all`` (``resolution_cause_leg_total``) is the
retirement gate; it must clear at the INV-39 provider floor.

These tests pin the leg matrix mechanically (engine state → leg, no LLM judge) and
pin that ``_cause_identified`` agrees with ``leg is not None`` — the single-source-
of-truth invariant that keeps the metric from ever disagreeing with the gate it
observes.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.core.investigation.terminal_transitions import (
    CAUSE_IDENTIFIED_LIKELIHOOD,
    _cause_identified,
    cause_identification_leg,
    finalize_resolution_truth_surface,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
)
from faultmaven.modules.case.domain.models import (
    CauseState,
    ConfidenceLevel,
    InvestigationProgress,
    RootCauseConclusion,
    WorkingConclusion,
)

pytestmark = pytest.mark.unit


def _case() -> Case:
    case = Case(
        case_id="case_ba0c05704e10",
        title="backstop-reliance test",
        state=CaseState.INVESTIGATING,
        user_id="u",
        enterprise_id="o",
        description="d",
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="p",
        ),
    )
    case.progress = InvestigationProgress()
    return case


def _rcc() -> RootCauseConclusion:
    return RootCauseConclusion(
        root_cause="Connection pool exhaustion under load",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
        mechanism="Pool capped at 5; traffic saturated it",
    )


def _working(likelihood: float) -> WorkingConclusion:
    return WorkingConclusion(
        statement="Pool exhaustion is the likely cause",
        likelihood=likelihood,
        reasoning="latency correlates with saturation",
    )


# ---------------------------------------------------------------------------
# Leg matrix
# ---------------------------------------------------------------------------


def test_leg_chain_when_cause_state_identified():
    case = _case()
    case.progress = InvestigationProgress(
        cause_state=CauseState.IDENTIFIED,
        symptom_verified=True,
        root_cause_likelihood=0.7,
        root_cause_method="hypothesis_validation",
    )
    assert cause_identification_leg(case) == "chain"


def test_leg_rcc_backstop_when_symptom_verified_and_no_chain():
    case = _case()
    case.progress.symptom_verified = True  # cause_state stays UNKNOWN
    case.root_cause_conclusion = _rcc()
    assert cause_identification_leg(case) == "rcc"


def test_leg_working_conclusion_backstop_above_threshold():
    case = _case()
    case.progress.symptom_verified = True
    case.working_conclusion = _working(CAUSE_IDENTIFIED_LIKELIHOOD)  # exactly the bar
    assert cause_identification_leg(case) == "working_conclusion"


def test_leg_none_on_empty_case():
    assert cause_identification_leg(_case()) is None


def test_leg_none_when_symptom_unverified_blocks_backstops():
    """The backstops are anchored on a verified symptom — an unanchored RCC does
    not count as a known cause (so a resolution licensed by it is impossible)."""
    case = _case()
    case.progress.symptom_verified = False
    case.root_cause_conclusion = _rcc()
    assert cause_identification_leg(case) is None


def test_leg_none_when_contested_suppresses_backstops():
    """While identification is MECE-contested, no backstop proxy counts."""
    case = _case()
    case.progress.symptom_verified = True
    case.progress.cause_identification_contested = True
    case.root_cause_conclusion = _rcc()
    assert cause_identification_leg(case) is None


def test_working_conclusion_below_threshold_is_none():
    case = _case()
    case.progress.symptom_verified = True
    case.working_conclusion = _working(CAUSE_IDENTIFIED_LIKELIHOOD - 0.1)
    assert cause_identification_leg(case) is None


def test_chain_takes_precedence_over_rcc():
    """When the chain validated AND an RCC is present, the healthy chain leg wins
    — a resolution here is NOT backstop-reliant."""
    case = _case()
    case.progress = InvestigationProgress(
        cause_state=CauseState.IDENTIFIED,
        symptom_verified=True,
        root_cause_likelihood=0.7,
        root_cause_method="hypothesis_validation",
    )
    case.root_cause_conclusion = _rcc()
    assert cause_identification_leg(case) == "chain"


def test_rcc_takes_precedence_over_working_conclusion():
    case = _case()
    case.progress.symptom_verified = True
    case.root_cause_conclusion = _rcc()
    case.working_conclusion = _working(0.9)
    assert cause_identification_leg(case) == "rcc"


# ---------------------------------------------------------------------------
# Single-source-of-truth invariant: _cause_identified == (leg is not None)
# ---------------------------------------------------------------------------


def _matrix_cases():
    # chain
    c1 = _case()
    c1.progress = InvestigationProgress(
        cause_state=CauseState.IDENTIFIED,
        symptom_verified=True,
        root_cause_likelihood=0.7,
        root_cause_method="hypothesis_validation",
    )
    # rcc
    c2 = _case()
    c2.progress.symptom_verified = True
    c2.root_cause_conclusion = _rcc()
    # working_conclusion
    c3 = _case()
    c3.progress.symptom_verified = True
    c3.working_conclusion = _working(0.8)
    # none — empty
    c4 = _case()
    # none — unverified symptom blocks RCC
    c5 = _case()
    c5.root_cause_conclusion = _rcc()
    # none — contested
    c6 = _case()
    c6.progress.symptom_verified = True
    c6.progress.cause_identification_contested = True
    c6.root_cause_conclusion = _rcc()
    return [c1, c2, c3, c4, c5, c6]


@pytest.mark.parametrize("case", _matrix_cases())
def test_cause_identified_agrees_with_leg(case):
    """The gate predicate and the metric's leg reader must never disagree —
    _cause_identified delegates to cause_identification_leg."""
    assert _cause_identified(case) is (cause_identification_leg(case) is not None)


# ---------------------------------------------------------------------------
# Emission wiring — the metric fires at the shared resolution chokepoint,
# ``finalize_resolution_truth_surface`` (every RESOLVED executor calls it), and
# reads the leg PRE-stamp so a backstop-licensed resolution isn't relabeled
# "chain" by the confirm-stamp. See test_chain_cause_state.py for the decisive
# pre/post-stamp regression that exercises the real confirm-stamp promotion.
# ---------------------------------------------------------------------------


def _leg_calls(counter: MagicMock, leg: str) -> int:
    return sum(1 for c in counter.labels.call_args_list if c.kwargs.get("leg") == leg)


def test_finalize_emits_resolution_cause_leg_once():
    """The finalizer — the one chokepoint every resolve surface shares — emits
    the metric exactly once, labeled by the pre-stamp leg. A case licensed by the
    RCC backstop (verified symptom, RCC present, no validated chain) records
    ``rcc``. No qualifying absence here, so the stamp doesn't promote — this test
    pins coverage + labeling; the pre/post-stamp regression lives with the real
    stamp machinery in test_chain_cause_state.py."""
    case = _case()
    case.progress.symptom_verified = True
    case.root_cause_conclusion = _rcc()
    assert cause_identification_leg(case) == "rcc"

    with patch(
        "faultmaven.core.investigation.terminal_transitions.resolution_cause_leg_total",
        new=MagicMock(),
    ) as counter:
        finalize_resolution_truth_surface(case)

    assert counter.labels.call_count == 1
    assert _leg_calls(counter, "rcc") == 1
    assert _leg_calls(counter, "chain") == 0

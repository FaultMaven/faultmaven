"""#2039 — a relapse fails the fix window, and disproves nothing about the cause.

``fix_windows`` reads two different records as a FAILED window:

- a DISCONFIRMATION: the cause observed removed after the fix, then the
  problem observed present, never observed gone in between (the cause went,
  the problem stayed);
- a RELAPSE: the problem observed gone after the fix, then observed again —
  the problem went with the cause, and its return may be the cause back, the
  effect not lasting, another cause, or a misread check.

The readiness gate reads both as a fix that did not hold: only a new fix earns
RESOLVED (#1927). The destructive readers — M6's record trigger and the
failed-fix ground a refutation may stand on (``failed_fix_of``), and M6's
counterfactual basis — act on a disconfirmation only
(``FixWindow.disconfirmation``, the pair of observations it rests on), which
is what INV-42 and the provenance row M6 writes already said. In the incident a diagnostic curl run without the client
certificate, recorded after two reliefs, refuted the correct root at belief 0.
"""

from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    m6_disconfirmation_basis,
)
from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_SUCCEEDED,
    fix_windows,
    has_resolution_confirmation,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _flag_absence_inside_a_failed_fix_window,
    _solution_cause_validated,
)
from faultmaven.core.investigation.prompts.templates.treatment import (
    treatment_instructions,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CauseState,
    EvidenceCategory,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _add_sibling_chain,
    _assert_demoted,
    _cause_gone,
    _engine_rows,
    _failed_fix,
    _identified_case,
    _problem_gone,
    _recompute_at,
    _row,
    _still_failing,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _judge

pytestmark = pytest.mark.unit


def _probe_failure(turn=13) -> object:
    """A check that failed for its own reason, in the problem's nouns: what
    the incident's curl without ``--cert`` was."""
    return _row(
        "probe_failure",
        EvidenceCategory.SYMPTOM_EVIDENCE,
        turn,
        "curl to checkout returned 503 because the probe omitted the session cookie",
    )


def _relapsed_case():
    """The incident's shape: the fix runs at 6; at 7 the cause and the problem
    are both seen gone, at 12 the problem again; at 13 a symptom row."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 6)
    case.evidence += [
        _cause_gone(7),
        _problem_gone(7, label="relief_7"),
        _problem_gone(12, label="relief_12"),
        _probe_failure(13),
    ]
    return case, root, hyp


def _assert_standing(case, root, hyp):
    assert hyp.state != HypothesisState.REFUTED
    assert root.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_the_incident_fails_the_window_as_a_relapse_and_keeps_the_cause():
    case, root, hyp = _relapsed_case()
    window = fix_windows(case)[-1]
    assert (window.fix_turn, window.state) == (6, FIX_FAILED)
    assert (window.relief_turn, window.failure_turn) == (7, 13)
    assert window.disconfirmation is None

    _recompute_at(case, 13)

    _assert_standing(case, root, hyp)
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == "a new fix after the one that did not hold"
    assert has_resolution_confirmation(case) is False


def test_a_relapse_is_metered_as_its_own_refusal():
    case, _root, _hyp = _relapsed_case()
    with patch(
        "faultmaven.core.investigation.causal_graph.disconfirmation."
        "m6_demotion_refused_total"
    ) as counter:
        assert m6_disconfirmation_basis(case) is None
    counter.labels.assert_called_once_with(reason="relapse")


def test_a_relapse_backs_no_refutation_of_the_cause():
    """'The fix had no effect' is false after a relief: the failed-fix ground
    (#1911) is a persistence's, so a refutation needs evidence it links."""
    case, _root, hyp = _relapsed_case()
    case.current_turn = 13

    _judge(case, hyp.hypothesis_id)

    assert hyp.state != HypothesisState.REFUTED


def test_a_disconfirmation_still_demotes_and_a_later_relief_does_not_undo_it():
    """The relief comes after the failure: the cause was seen gone and the
    problem present, and FAILED is final for the window."""
    case, root, hyp = _identified_case()
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    case.evidence.append(_problem_gone(10, label="quiet_later"))
    window = fix_windows(case)[-1]
    assert (window.state, window.disconfirmation) == (FIX_FAILED, (9, 9))

    _recompute_at(case, 10)

    _assert_demoted(case, root, hyp)
    assert len(_engine_rows(case)) == 1
    assert "not observed gone in between" in _engine_rows(case)[0].summary


def test_the_cause_seen_still_gone_after_a_relapse_disconfirms_it():
    """After the relapse the cause is re-checked: still removed, and the
    problem is present after that, with nothing gone in between. The relief
    at 7 does not shield it, and M6's row names the pair it read."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 6)
    case.evidence += [
        _cause_gone(7),
        _problem_gone(7, label="relief_7"),
        _still_failing(12, label="back_at_12"),
        _row(
            "cause_still_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            13,
            "max_connections still reads 100",
        ),
        _still_failing(14, label="still_503_at_14"),
    ]
    window = fix_windows(case)[-1]
    assert (window.state, window.failure_turn) == (FIX_FAILED, 12)
    assert window.disconfirmation == (13, 14)

    _recompute_at(case, 14)

    _assert_demoted(case, root, hyp)
    summary = _engine_rows(case)[0].summary
    assert "observed removed at turn 13" in summary
    assert "at turn 14 observes the problem still present" in summary


def test_a_relief_before_the_cause_was_seen_gone_does_not_shield_it():
    """Design call, pinned: the problem seen gone at 7, the cause first seen
    removed at 9, the problem back at 12. The record never observed the cause
    and the problem gone together; it observed the cause gone and the problem
    present after it with nothing gone in between, so it disconfirms."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 6)
    case.evidence += [
        _problem_gone(7, label="relief_7"),
        _cause_gone(9),
        _still_failing(12, label="back_at_12"),
    ]
    window = fix_windows(case)[-1]
    assert (window.relief_turn, window.disconfirmation) == (7, (9, 12))

    _recompute_at(case, 12)

    _assert_demoted(case, root, hyp)


def test_a_same_turn_relief_then_a_later_persistence_is_a_relapse():
    """A turn holding both reads as gone (#1927 ruling), so the problem seen
    at a later turn came back: a relapse."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _problem_gone(9, label="same_turn_relief"),
        _still_failing(9, label="same_turn_persistence"),
        _still_failing(11, label="back_at_11"),
    ]
    window = fix_windows(case)[-1]
    assert (window.state, window.relief_turn, window.failure_turn) == (
        FIX_FAILED,
        9,
        11,
    )
    assert window.disconfirmation is None

    _recompute_at(case, 11)

    _assert_standing(case, root, hyp)


def test_a_relapse_does_not_shield_the_cause_from_a_later_fix_that_fails():
    """The relapsed window asks for a new fix; when the cause is seen gone
    after it and the problem present, the cause is disconfirmed."""
    case, root, hyp = _relapsed_case()
    _recompute_at(case, 13)
    _assert_standing(case, root, hyp)

    _accept(case, InvestigationActionType.SOLUTION, 14)
    case.evidence += [
        _cause_gone(15),
        _still_failing(16, label="still_503_after_second_fix"),
    ]
    _recompute_at(case, 16)

    _assert_demoted(case, root, hyp)


def test_a_relief_at_turn_zero_still_counts():
    """``relief_turn`` is a turn number, and 0 is one: an out-of-band fix
    confirmed in the opening turn is window 0's success."""
    case, _root, _hyp = _identified_case()
    case.evidence += [_cause_gone(0), _problem_gone(0, label="relief_0")]

    window = fix_windows(case)[0]
    assert (window.state, window.relief_turn) == (FIX_SUCCEEDED, 0)


# ---------------------------------------------------------------------------
# The model notice states the shape it read.
# ---------------------------------------------------------------------------


def _notice(case, turn):
    case.current_turn = turn
    case.evidence.append(_problem_gone(turn, label=f"fine_now_{turn}"))
    metadata = {}
    assert _flag_absence_inside_a_failed_fix_window(case, metadata) is True
    return metadata["system_feedback"]


def test_after_a_relapse_the_notice_keeps_the_fix_under_a_standing_cause():
    case, _root, _hyp = _relapsed_case()
    _recompute_at(case, 13)

    feedback = _notice(case, 14)

    assert "did not hold: the problem was seen gone at turn 7, then seen again" in (
        feedback
    )
    assert "does not by itself disprove the cause the fix addressed" in feedback
    assert "if that cause still stands, no new hypothesis is needed" in feedback
    assert "SolutionToAdd" in feedback and "solution_accepted" in feedback


def test_after_a_relapse_of_a_cause_that_already_fell_the_notice_offers_the_re_assertion():
    """Review finding: M6 demoted the cause on the first fix; a second fix of
    the same cause then relapses. The notice must not tell the model the
    cause stands: it leaves "no new hypothesis" to a cause that still stands
    and names the re-assertion for one that fell."""
    case, root, hyp = _identified_case()
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    _recompute_at(case, 9)
    _assert_demoted(case, root, hyp)
    _accept(case, InvestigationActionType.SOLUTION, 12)
    case.evidence += [
        _row(
            "second_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            13,
            "max_connections now reads 100 after the second change",
        ),
        _problem_gone(13, label="second_relief"),
        _still_failing(15, label="back_at_15"),
    ]
    _recompute_at(case, 15)
    window = fix_windows(case)[-1]
    assert (window.state, window.disconfirmation) == (FIX_FAILED, None)

    feedback = _notice(case, 16)

    assert "the problem was seen gone at turn 13, then seen again" in feedback
    assert "if it no longer stands, re-assert it with a root_cause_conclusion" in (
        feedback
    )


def test_after_a_persistence_the_notice_asks_to_re_assert_the_cause():
    case, _root, _hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)

    feedback = _notice(case, 10)

    assert "the cause was shown removed, then the problem was seen again" in feedback
    assert "root_cause_conclusion on a new hypothesis" in feedback
    assert "does not by itself disprove" not in feedback
    assert "no new hypothesis is needed" not in feedback


# ---------------------------------------------------------------------------
# The treatment prompt asks for the label the engine reads.
# ---------------------------------------------------------------------------


def test_treatment_keeps_symptom_evidence_for_the_reported_signature():
    text = treatment_instructions(DEFAULT_GIVEN)

    assert "New symptoms that emerge after a failed fix" not in text
    assert "ONLY the reported problem's own signature" in text
    assert "do not record it as symptom_evidence" in text
    # Review finding: "Revisit the Diagnosis" still asks for the
    # symptom_evidence a mis-stated problem's revision cites.
    assert "the symptom_evidence a revised_problem_statement cites" in text
    assert "with the symptom_evidence that shows it" in text


def test_treatment_says_a_relapse_disproves_nothing():
    text = treatment_instructions(DEFAULT_GIVEN)

    assert "never seen gone in between" in text
    assert "is a relapse, which by itself" in text
    assert (
        "if it is still gone while the problem\n     is present, that disproves it"
        in (text)
    )

"""Regression tests for the investigation-flow redesign trap fix (Phase 2).

Covers the two engine mechanisms that dissolve the S1 (k8s-pvc-pending)
mitigation_first trap documented in
docs/architecture/investigation-engine/investigation-flow-redesign.md §1.1:

1. Surgical reasoning-validation strip — a single unjustified milestone no
   longer wipes co-emitted VALID milestones (the literal collateral-wipe bug).
2. cause_state derivation — the engine-owned assessment variable derives
   CANDIDATES from >=2 ACTIVE hypotheses, is UNKNOWN otherwise, and IDENTIFIED
   is forward-only (sticky).
"""

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
    validate_reasoning_first,
)
from faultmaven.core.investigation.schemas import InternalReasoning, MilestoneUpdates
from faultmaven.modules.case.contracts import (
    CaseState,
    CauseState,
    InvestigationProgress,
    SolutionState,
)


def _response(milestones: MilestoneUpdates, justifications: dict):
    """Minimal duck-typed investigation response (not Inquiry/Terminal)."""
    ir = InternalReasoning(milestone_justifications=justifications, evidence_analyzed=[])
    state_updates = SimpleNamespace(milestones=milestones, evidence_to_add=[])
    return SimpleNamespace(internal_reasoning=ir, state_updates=state_updates)


def _case(*, evidence=True):
    return SimpleNamespace(
        state=CaseState.INVESTIGATING,
        is_terminal=False,
        pending_transition=None,
        progress=SimpleNamespace(solution_verified=False),
        evidence=["ev_1"] if evidence else [],
        current_turn=5,
        case_id="case_test",
    )


class TestSurgicalStrip:
    def test_unjustified_milestone_is_offending_justified_one_is_preserved(self):
        """The S1 mechanism: root_cause_identified (unjustified) must not drag
        a justified mitigation_accepted into the strip."""
        milestones = MilestoneUpdates(
            root_cause_identified=True, mitigation_accepted=True
        )
        # Only mitigation_accepted is justified; root_cause_identified is not.
        justifications = {"mitigation_accepted": "User applied the fix (ev_1)"}

        is_valid, errors, offending = validate_reasoning_first(
            _response(milestones, justifications), _case()
        )

        assert is_valid is False
        assert offending == {"root_cause_identified"}
        # mitigation_accepted was justified -> NOT stripped.
        assert "mitigation_accepted" not in offending

    def test_no_internal_reasoning_strips_all_completed(self):
        milestones = MilestoneUpdates(symptom_verified=True, mitigation_accepted=True)
        resp = _response(milestones, {})
        resp.internal_reasoning = None

        is_valid, errors, offending = validate_reasoning_first(resp, _case())

        assert is_valid is False
        assert offending == {"symptom_verified", "mitigation_accepted"}

    def test_no_actionable_evidence_strips_all_completed(self):
        milestones = MilestoneUpdates(symptom_verified=True)
        justifications = {"symptom_verified": "confirmed"}

        is_valid, errors, offending = validate_reasoning_first(
            _response(milestones, justifications), _case(evidence=False)
        )

        assert is_valid is False
        assert offending == {"symptom_verified"}

    def test_all_justified_passes_clean(self):
        milestones = MilestoneUpdates(symptom_verified=True)
        justifications = {"symptom_verified": "confirmed via ev_1"}

        is_valid, errors, offending = validate_reasoning_first(
            _response(milestones, justifications), _case()
        )

        assert is_valid is True
        assert offending == set()


class TestCauseStateDerivation:
    def _case_with_hyps(self, n_active: int):
        hm = create_hypothesis_manager()
        hyps = {}
        for i in range(n_active):
            h = hm.create_hypothesis(
                statement=f"hypothesis {i}",
                category="config",
                initial_likelihood=0.5,
                current_turn=1,
            )
            hyps[h.hypothesis_id] = h
        progress = InvestigationProgress()
        return SimpleNamespace(progress=progress, hypotheses=hyps, solutions=[])

    def test_unknown_with_fewer_than_two_active(self):
        case = self._case_with_hyps(1)
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.UNKNOWN

    def test_candidates_with_two_or_more_active(self):
        case = self._case_with_hyps(2)
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.CANDIDATES

    def test_identified_is_sticky(self):
        case = self._case_with_hyps(3)  # would be CANDIDATES
        case.progress.cause_state = CauseState.IDENTIFIED
        case.progress.root_cause_likelihood = 0.9
        case.progress.root_cause_method = "direct_analysis"
        _recompute_assessment_state(case)
        # forward-only: IDENTIFIED is not downgraded to CANDIDATES
        assert case.progress.cause_state == CauseState.IDENTIFIED

    def test_solution_state_selected_when_proposed(self):
        case = self._case_with_hyps(0)
        case.progress.solution_proposed = True
        _recompute_assessment_state(case)
        assert case.progress.solution_state == SolutionState.SELECTED

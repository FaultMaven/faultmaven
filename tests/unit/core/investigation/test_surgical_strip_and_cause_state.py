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
    EvidenceCategory,
    InvestigationProgress,
    SolutionFeasible,
    SolutionState,
)


def _response(milestones: MilestoneUpdates, justifications: dict):
    """Minimal duck-typed investigation response (not Inquiry/Terminal)."""
    ir = InternalReasoning(
        milestone_justifications=justifications, evidence_analyzed=[]
    )
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
    def _case_with_hyps(self, n_active: int, *, causal_evidence=0, conclusion=False):
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
        evidence = [
            SimpleNamespace(category=EvidenceCategory.CAUSAL_EVIDENCE)
            for _ in range(causal_evidence)
        ]
        rcc = SimpleNamespace(root_cause="the cause") if conclusion else None
        return SimpleNamespace(
            progress=progress,
            hypotheses=hyps,
            solutions=[],
            evidence=evidence,
            root_cause_conclusion=rcc,
        )

    def test_unknown_with_fewer_than_two_active(self):
        case = self._case_with_hyps(1)
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.UNKNOWN

    def test_candidates_with_two_or_more_active(self):
        case = self._case_with_hyps(2)
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.CANDIDATES

    def test_identified_derived_from_likelihood_plus_causal_evidence(self):
        # Follow-on B: high confidence + causal evidence -> IDENTIFIED even
        # though the LLM never set the root_cause_identified milestone.
        case = self._case_with_hyps(2, causal_evidence=1)  # would be CANDIDATES
        case.progress.root_cause_likelihood = 0.8
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.IDENTIFIED

    def test_high_likelihood_without_causal_evidence_is_not_identified(self):
        case = self._case_with_hyps(0, causal_evidence=0)
        case.progress.root_cause_likelihood = 0.9
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.UNKNOWN

    def test_identified_derived_from_root_cause_conclusion(self):
        case = self._case_with_hyps(0, conclusion=True)
        _recompute_assessment_state(case)
        assert case.progress.cause_state == CauseState.IDENTIFIED

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


class TestDeferredImplementationClose:
    """Follow-on A: solution_feasible=DEFERRED proposes CLOSE-with-documented-solution."""

    def _case(self, *, feasible, solution_proposed, pending=None, terminal=False):
        progress = InvestigationProgress()
        progress.solution_feasible = feasible
        progress.solution_proposed = solution_proposed
        case = SimpleNamespace(
            progress=progress,
            solutions=[],
            pending_transition=pending,
            is_terminal=terminal,
            state=CaseState.INVESTIGATING,
            case_id="case_test",
        )
        return case

    def test_no_proposal_when_feasible_now(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.NOW, solution_proposed=True)
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed" not in meta
        assert case.pending_transition is None

    def test_no_proposal_when_deferred_but_no_solution(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=False)
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed" not in meta

    def test_proposes_close_when_deferred_with_solution(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert meta.get("transition_proposed") is True
        assert case.pending_transition is not None
        assert case.pending_transition["to_state"] == "closed"
        assert meta.get("override_suggestions")

    def test_no_proposal_when_handshake_in_flight(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            pending={"to_state": "resolved"},
        )
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        # existing pending transition must not be clobbered
        assert case.pending_transition == {"to_state": "resolved"}
        assert "transition_proposed" not in meta


class TestStructuredOutputDegradation:
    """Follow-on C: parse-time cross-field validation errors degrade gracefully
    (prune the offending sub-record / conversational fallback) instead of 500ing.
    """

    def _engine(self):
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        return MilestoneEngine.__new__(MilestoneEngine)

    def test_prunes_invalid_evidence_keeps_valid(self):
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            "agent_response": "Here is my analysis.",
            "state_updates": {
                "evidence_to_add": [
                    # invalid: source_type=text without source_file_id (the S4 trigger)
                    {
                        "summary": "bad",
                        "extract": "x",
                        "category": "solution_evidence",
                        "source_type": "text",
                    },
                    # valid: source_file_id present
                    {
                        "summary": "good",
                        "extract": "y",
                        "category": "symptom_evidence",
                        "source_type": "text",
                        "source_file_id": "file_123",
                    },
                ]
            },
        }
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response == "Here is my analysis."
        # the invalid entry is pruned; the valid one survives
        summaries = [e.summary for e in parsed.state_updates.evidence_to_add]
        assert summaries == ["good"]

    def test_valid_response_passes_unchanged(self):
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {"agent_response": "All good.", "state_updates": {}}
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response == "All good."

    def test_conversational_fallback_drops_state_updates(self):
        # All evidence entries invalid -> after pruning, state_updates still has
        # the (now empty) list; the turn survives with the response text intact.
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            "agent_response": "Survives as conversation.",
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": "bad",
                        "extract": "x",
                        "category": "solution_evidence",
                        "source_type": "text",
                    }
                ]
            },
        }
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response == "Survives as conversation."
        assert parsed.state_updates.evidence_to_add == []

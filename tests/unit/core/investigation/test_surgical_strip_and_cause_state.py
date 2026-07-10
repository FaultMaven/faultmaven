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


class TestSolutionStateDerivation:
    """``solution_state`` / ``solution_proposed`` derivation in
    ``_recompute_assessment_state`` (INV-32; cause_state derivation itself is
    exercised by test_chain_cause_state.py, offer liveness by
    test_solution_offer_liveness.py)."""

    def _case(self):
        return SimpleNamespace(
            case_id="case_test",
            current_turn=3,
            progress=InvestigationProgress(),
            hypotheses={},
            solutions=[],
            evidence=[],
            causal_nodes={},
            causal_edges={},
            root_cause_conclusion=None,
            proposed_actions=[],
        )

    def test_solution_state_selected_when_ladder_advanced(self):
        # The gate ladder is a forward-only fact: solution_accepted keeps the
        # derived pair True/SELECTED even with no live offer standing.
        case = self._case()
        case.progress.solution_accepted = True
        _recompute_assessment_state(case)
        assert case.progress.solution_state == SolutionState.SELECTED
        assert case.progress.solution_proposed is True

    def test_solution_state_unknown_when_no_live_offer(self):
        # A stale persisted True with no live offer and no ladder derives OFF —
        # the write-once latch is gone (INV-32).
        case = self._case()
        case.progress.solution_proposed = True
        _recompute_assessment_state(case)
        assert case.progress.solution_state == SolutionState.UNKNOWN
        assert case.progress.solution_proposed is False


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
                        "category": "causal_evidence",
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
                        "category": "causal_evidence",
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

    def test_synthesizes_missing_agent_response_keeps_state(self, caplog):
        # gemini-3.5-flash sometimes omits the required agent_response ITSELF on
        # resolution turns. The rungs above preserve agent_response, so they
        # cannot help; this rung synthesizes a placeholder and KEEPS the model's
        # otherwise-valid state_updates instead of 500ing.
        import logging

        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            # no agent_response at all
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": "good",
                        "extract": "y",
                        "category": "symptom_evidence",
                        "source_type": "text",
                        "source_file_id": "file_123",
                    }
                ]
            },
        }
        with caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ):
            parsed = eng._validate_with_degradation(
                content, InvestigationResponse_Diagnosis
            )
        assert parsed.agent_response  # a non-empty placeholder was synthesized
        # the model's valid work survives — state_updates are NOT dropped here
        summaries = [e.summary for e in parsed.state_updates.evidence_to_add]
        assert summaries == ["good"]
        assert any(
            "synthesized missing agent_response" in r.getMessage()
            for r in caplog.records
        )

    def test_synthesizes_missing_agent_response_drops_invalid_state(self):
        # Both gaps at once (the turn-7 500 shape): no agent_response AND an
        # unrepairable state_updates error. The turn must still survive — the
        # placeholder is synthesized and the bad state is dropped, never a 500.
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {"state_updates": {"evidence_to_add": "should-be-a-list"}}
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response  # placeholder synthesized, no 500

    def test_fallback_logs_non_prunable_errors(self, caplog):
        # A NON-prunable error (loc has no list index) forces the conversational
        # fallback; that branch must log the offending loc/msg so each fallback is
        # self-diagnosing (S4 observability). Here evidence_to_add is the wrong
        # TYPE (a string, not a list) -> loc ('state_updates','evidence_to_add')
        # has no int index -> not prunable.
        import logging

        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            "agent_response": "hi",
            "state_updates": {"evidence_to_add": "should-be-a-list"},
        }
        with caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ):
            parsed = eng._validate_with_degradation(
                content, InvestigationResponse_Diagnosis
            )
        assert parsed.agent_response == "hi"  # turn survives, no 500
        degraded = [
            r for r in caplog.records if "structured_output_degraded" in r.getMessage()
        ]
        assert degraded, "fallback must emit a degraded warning"
        non_prunable = getattr(degraded[0], "non_prunable_errors", None)
        assert non_prunable, "fallback must log the non-prunable errors"
        assert any("evidence_to_add" in loc for loc, _msg in non_prunable)

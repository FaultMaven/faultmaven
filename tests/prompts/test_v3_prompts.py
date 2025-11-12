"""Tests for v3.0 Prompt Engineering System

Tests loop-back prompts, degraded mode prompts, Phase 5 entry modes,
Phase 1 routing, and Phase 3 structured output.
"""

import pytest
from unittest.mock import Mock

from faultmaven.prompts.investigation.loopback_prompts import (
    get_hypothesis_refutation_loopback_prompt,
    get_scope_change_loopback_prompt,
    get_timeline_wrong_loopback_prompt,
)
from faultmaven.prompts.investigation.degraded_mode_prompts import (
    get_degraded_mode_prompt,
)
from faultmaven.prompts.investigation.phase5_entry_modes import (
    get_phase5_entry_mode_context,
)
from faultmaven.prompts.investigation.phase1_routing_prompts import (
    get_routing_confirmation_prompt,
    parse_user_routing_response,
)
from faultmaven.prompts.investigation.phase3_structured_output import (
    get_structured_output_schema_prompt,
    get_structured_output_example,
)
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationLifecycle,
    InvestigationPhase,
    InvestigationMetadata,
    OODAEngine,
    Hypothesis,
    HypothesisStatus,
    DegradedModeType,
    WorkingConclusion,
    ConfidenceLevel,
)


@pytest.fixture
def base_investigation_state():
    """Create base investigation state"""
    state = InvestigationState(
        session_id="test-session",
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.VALIDATION,
            investigation_strategy="root_cause_analysis",
        ),
        metadata=InvestigationMetadata(current_turn=5),
        ooda_engine=OODAEngine(
            hypotheses=[],
            iterations=[],
        ),
    )
    return state


class TestLoopBackPrompts:
    """Test loop-back prompt generation"""

    def test_hypothesis_refutation_loopback(self, base_investigation_state):
        """Test hypothesis refutation loop-back prompt"""
        working_conclusion = {
            "statement": "All network hypotheses ruled out",
            "confidence": 0.35,
            "confidence_level": "speculation",
        }

        prompt = get_hypothesis_refutation_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=1,
            working_conclusion=working_conclusion,
            refutation_reason="All hypotheses tested negative",
        )

        assert prompt is not None
        assert len(prompt) > 100
        assert "hypothesis" in prompt.lower()
        assert "refut" in prompt.lower() or "ruled out" in prompt.lower()
        assert "Phase 3" in prompt  # Should loop to Phase 3
        assert "1" in prompt  # Loop count

    def test_scope_change_loopback(self, base_investigation_state):
        """Test scope change loop-back prompt"""
        prompt = get_scope_change_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=2,
            scope_expansion="Additional systems affected: database cluster",
            new_evidence_summary="Database logs show cascading failures",
        )

        assert prompt is not None
        assert "scope" in prompt.lower()
        assert "Phase 1" in prompt  # Should loop to Phase 1
        assert "database" in prompt.lower()
        assert "2" in prompt  # Loop count

    def test_timeline_wrong_loopback(self, base_investigation_state):
        """Test timeline wrong loop-back prompt"""
        prompt = get_timeline_wrong_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=1,
            timeline_correction="Problem started 2 hours earlier than believed",
            new_correlation="Deployment happened at 2pm, not 4pm",
        )

        assert prompt is not None
        assert "timeline" in prompt.lower()
        assert "Phase 2" in prompt  # Should loop to Phase 2
        assert "2pm" in prompt or "4pm" in prompt
        assert "1" in prompt  # Loop count

    def test_max_loop_warning(self, base_investigation_state):
        """Test that max loop count triggers escalation warning"""
        working_conclusion = {
            "statement": "Unable to identify root cause",
            "confidence": 0.30,
        }

        prompt = get_hypothesis_refutation_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=3,  # Max loops
            working_conclusion=working_conclusion,
            refutation_reason="All approaches exhausted",
        )

        assert "escalat" in prompt.lower() or "degrad" in prompt.lower() or "limit" in prompt.lower()


class TestDegradedModePrompts:
    """Test degraded mode prompt generation"""

    def test_critical_evidence_missing_prompt(self):
        """Test CRITICAL_EVIDENCE_MISSING degraded mode prompt"""
        prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.CRITICAL_EVIDENCE_MISSING,
            degraded_mode_explanation="Cannot access production logs",
            current_turn=10,
            entered_at_turn=8,
        )

        assert prompt is not None
        assert "evidence" in prompt.lower()
        assert "50%" in prompt  # Confidence cap
        assert "degraded" in prompt.lower()
        assert "limitation" in prompt.lower() or "constraint" in prompt.lower()

    def test_expertise_required_prompt(self):
        """Test EXPERTISE_REQUIRED degraded mode prompt"""
        prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.EXPERTISE_REQUIRED,
            degraded_mode_explanation="Requires kernel debugging expertise",
            current_turn=12,
            entered_at_turn=10,
        )

        assert prompt is not None
        assert "expertise" in prompt.lower() or "specialist" in prompt.lower()
        assert "40%" in prompt  # Confidence cap
        assert "kernel" in prompt.lower()

    def test_systemic_issue_prompt(self):
        """Test SYSTEMIC_ISSUE degraded mode prompt"""
        prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.SYSTEMIC_ISSUE,
            degraded_mode_explanation="Architectural design flaw",
            current_turn=15,
            entered_at_turn=12,
        )

        assert prompt is not None
        assert "systemic" in prompt.lower() or "architectural" in prompt.lower()
        assert "30%" in prompt  # Confidence cap

    def test_hypothesis_space_exhausted_prompt(self):
        """Test HYPOTHESIS_SPACE_EXHAUSTED degraded mode prompt"""
        prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.HYPOTHESIS_SPACE_EXHAUSTED,
            degraded_mode_explanation="All reasonable hypotheses tested",
            current_turn=20,
            entered_at_turn=18,
        )

        assert prompt is not None
        assert "hypothesis" in prompt.lower() or "exhaust" in prompt.lower()
        assert "0%" in prompt or "cannot" in prompt.lower()  # 0% cap

    def test_general_limitation_prompt(self):
        """Test GENERAL_LIMITATION degraded mode prompt"""
        prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.GENERAL_LIMITATION,
            degraded_mode_explanation="Multiple constraints",
            current_turn=10,
            entered_at_turn=8,
        )

        assert prompt is not None
        assert "50%" in prompt  # Confidence cap

    def test_re_escalation_after_turns(self):
        """Test re-escalation message after 3 turns"""
        prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.CRITICAL_EVIDENCE_MISSING,
            degraded_mode_explanation="Still no logs",
            current_turn=14,
            entered_at_turn=11,  # 3 turns ago
        )

        assert "3 turns" in prompt or "re-escalat" in prompt.lower()


class TestPhase5EntryModes:
    """Test Phase 5 entry mode contexts"""

    def test_normal_entry_mode(self, base_investigation_state):
        """Test normal entry mode context"""
        # Add validated hypothesis
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Database connection pool exhausted",
            likelihood=0.75,
            status=HypothesisStatus.VALIDATED,
            category="configuration",
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Root cause: connection pool exhausted",
            confidence=0.75,
            confidence_level=ConfidenceLevel.CONFIDENT,
            supporting_evidence_count=4,
            caveats=[],
            alternative_explanations=[],
            can_proceed_with_solution=True,
        )

        context = get_phase5_entry_mode_context(
            entry_mode="normal",
            investigation_state=base_investigation_state,
        )

        assert context is not None
        assert "normal" in context.lower() or "validated" in context.lower()
        assert "root cause" in context.lower() or "hypothesis" in context.lower()
        assert "75%" in context or "confident" in context.lower()

    def test_fast_recovery_entry_mode(self, base_investigation_state):
        """Test fast recovery entry mode context"""
        base_investigation_state.lifecycle.phase_entry_history = [
            InvestigationPhase.INTAKE,
            InvestigationPhase.BLAST_RADIUS,
        ]

        context = get_phase5_entry_mode_context(
            entry_mode="fast_recovery",
            investigation_state=base_investigation_state,
        )

        assert context is not None
        assert "fast" in context.lower() or "recovery" in context.lower() or "mitigation" in context.lower()
        assert "critical" in context.lower() or "urgent" in context.lower()
        assert "Phase 1" in context  # Came from Phase 1

    def test_degraded_entry_mode(self, base_investigation_state):
        """Test degraded mode entry context"""
        base_investigation_state.lifecycle.escalation_state.operating_in_degraded_mode = True
        base_investigation_state.lifecycle.escalation_state.degraded_mode_type = (
            DegradedModeType.EXPERTISE_REQUIRED
        )
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Suspected kernel issue",
            confidence=0.40,  # At cap
            confidence_level=ConfidenceLevel.SPECULATION,
            supporting_evidence_count=2,
            caveats=["Requires kernel debugging"],
            alternative_explanations=[],
            can_proceed_with_solution=True,  # Can proceed at cap
        )

        context = get_phase5_entry_mode_context(
            entry_mode="degraded",
            investigation_state=base_investigation_state,
        )

        assert context is not None
        assert "degraded" in context.lower() or "limitation" in context.lower()
        assert "40%" in context  # Confidence cap
        assert "best effort" in context.lower() or "mitigation" in context.lower()


class TestPhase1RoutingPrompts:
    """Test Phase 1 routing confirmation prompts"""

    def test_critical_urgency_routing_prompt(self, base_investigation_state):
        """Test routing prompt for CRITICAL urgency"""
        base_investigation_state.lifecycle.urgency_level = "critical"

        prompt = get_routing_confirmation_prompt(
            investigation_state=base_investigation_state,
            urgency_level="critical",
        )

        assert prompt is not None
        assert "critical" in prompt.lower()
        assert "fast recovery" in prompt.lower() or "mitigation" in prompt.lower()
        assert "option" in prompt.lower() or "choice" in prompt.lower()
        # Should present two options
        assert "1" in prompt and "2" in prompt

    def test_high_urgency_routing_prompt(self, base_investigation_state):
        """Test routing prompt for HIGH urgency"""
        prompt = get_routing_confirmation_prompt(
            investigation_state=base_investigation_state,
            urgency_level="high",
        )

        assert prompt is not None
        assert "high" in prompt.lower() or "urgent" in prompt.lower()
        assert "investigation" in prompt.lower()
        assert "mitigation" in prompt.lower()

    def test_parse_fast_recovery_response(self):
        """Test parsing fast recovery choice"""
        responses = [
            "fast recovery",
            "option 1",
            "skip to mitigation",
            "I choose fast recovery",
        ]

        for response in responses:
            decision, is_ambiguous = parse_user_routing_response(response)
            assert decision == "fast_recovery"
            assert is_ambiguous is False

    def test_parse_full_investigation_response(self):
        """Test parsing full investigation choice"""
        responses = [
            "full investigation",
            "option 2",
            "do complete investigation",
            "I want the full investigation",
        ]

        for response in responses:
            decision, is_ambiguous = parse_user_routing_response(response)
            assert decision == "full_investigation"
            assert is_ambiguous is False

    def test_parse_ambiguous_response(self):
        """Test parsing ambiguous response"""
        responses = [
            "not sure",
            "what do you think?",
            "both",
            "neither",
        ]

        for response in responses:
            decision, is_ambiguous = parse_user_routing_response(response)
            assert is_ambiguous is True


class TestPhase3StructuredOutput:
    """Test Phase 3 structured output prompts"""

    def test_structured_output_schema_prompt(self):
        """Test structured output schema prompt"""
        prompt = get_structured_output_schema_prompt()

        assert prompt is not None
        assert "json" in prompt.lower()
        assert "required_evidence" in prompt
        assert "priority" in prompt.lower()
        assert "acquisition_guidance" in prompt.lower()
        assert "2-4 hypotheses" in prompt or "2-5 evidence items" in prompt

    def test_structured_output_example(self):
        """Test structured output example"""
        example = get_structured_output_example()

        assert example is not None
        assert "hypotheses" in example
        # Should be valid JSON-like structure
        assert "{" in example and "}" in example
        assert "required_evidence" in example
        assert "priority" in example
        assert "critical" in example.lower() or "important" in example.lower()

    def test_example_has_all_required_fields(self):
        """Test example contains all required fields"""
        example = get_structured_output_example()

        required_fields = [
            "hypothesis_id",
            "statement",
            "likelihood",
            "category",
            "required_evidence",
            "priority",
            "source_type",
            "query_pattern",
            "interpretation_guidance",
        ]

        for field in required_fields:
            assert field in example, f"Missing required field: {field}"


class TestPromptIntegration:
    """Integration tests for prompt system"""

    def test_loop_back_to_degraded_mode_transition(self, base_investigation_state):
        """Test transition from loop-back to degraded mode"""
        # First loop-back
        prompt1 = get_hypothesis_refutation_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=1,
            working_conclusion={"statement": "Test", "confidence": 0.30},
            refutation_reason="All tested negative",
        )
        assert "Phase 3" in prompt1

        # Second loop-back
        prompt2 = get_hypothesis_refutation_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=2,
            working_conclusion={"statement": "Test", "confidence": 0.25},
            refutation_reason="Still no viable hypotheses",
        )
        assert "Phase 3" in prompt2

        # Third loop-back - should suggest escalation
        prompt3 = get_hypothesis_refutation_loopback_prompt(
            investigation_state=base_investigation_state,
            loop_count=3,
            working_conclusion={"statement": "Test", "confidence": 0.20},
            refutation_reason="Exhausted hypothesis space",
        )
        assert "escalat" in prompt3.lower() or "degrad" in prompt3.lower()

        # Now in degraded mode
        degraded_prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.HYPOTHESIS_SPACE_EXHAUSTED,
            degraded_mode_explanation="Loop-back limit reached",
            current_turn=15,
            entered_at_turn=13,
        )
        assert "0%" in degraded_prompt  # Can't proceed with solution

    def test_fast_recovery_to_degraded_mode(self, base_investigation_state):
        """Test fast recovery path entering degraded mode"""
        # Phase 1 routing - choose fast recovery
        routing_prompt = get_routing_confirmation_prompt(
            investigation_state=base_investigation_state,
            urgency_level="critical",
        )
        assert "fast recovery" in routing_prompt.lower()

        # Phase 5 entry in fast recovery mode
        phase5_context = get_phase5_entry_mode_context(
            entry_mode="fast_recovery",
            investigation_state=base_investigation_state,
        )
        assert "fast" in phase5_context.lower() or "mitigation" in phase5_context.lower()

        # But hit limitation - enter degraded mode
        base_investigation_state.lifecycle.escalation_state.operating_in_degraded_mode = True
        base_investigation_state.lifecycle.escalation_state.degraded_mode_type = (
            DegradedModeType.CRITICAL_EVIDENCE_MISSING
        )

        degraded_prompt = get_degraded_mode_prompt(
            degraded_mode_type=DegradedModeType.CRITICAL_EVIDENCE_MISSING,
            degraded_mode_explanation="Cannot access production",
            current_turn=8,
            entered_at_turn=6,
        )
        assert "50%" in degraded_prompt  # Can still provide mitigation


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

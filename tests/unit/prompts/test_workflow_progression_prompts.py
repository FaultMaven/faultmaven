"""Tests for Workflow Progression Prompts (v3.0)

Tests agent-initiated workflow progression confirmations:
1. Start Investigation (CONSULTING → INVESTIGATING)
2. Mark Complete (INVESTIGATING → RESOLVED)
3. Suggest Escalation (INVESTIGATING → CLOSED)

Design Reference: WORKFLOW_PROGRESSION_IMPLEMENTATION_STATUS.md
"""

import pytest
from faultmaven.prompts.investigation.workflow_progression_prompts import (
    # Start Investigation
    get_start_investigation_prompt,
    parse_start_investigation_response,
    get_start_investigation_clarification,
    # Mark Complete
    get_mark_complete_prompt,
    parse_mark_complete_response,
    get_mark_complete_clarification,
    # Suggest Escalation
    get_suggest_escalation_prompt,
    parse_suggest_escalation_response,
    get_suggest_escalation_clarification,
    # Workflow Transition
    get_workflow_transition_confirmation,
)


# =============================================================================
# Start Investigation Tests
# =============================================================================

class TestStartInvestigationPrompt:
    """Test start investigation prompt generation"""

    def test_basic_prompt_generation(self):
        """Test basic prompt generation with required parameters"""
        prompt = get_start_investigation_prompt(
            problem_summary="Database connection timeout",
            complexity_indicators=["multi_turn_conversation", "multiple_symptoms"],
        )

        assert "Database connection timeout" in prompt
        assert "Ready to Start Systematic Investigation" in prompt
        assert "start investigation" in prompt.lower()
        assert "not yet" in prompt.lower()

    def test_prompt_includes_complexity_indicators(self):
        """Test that complexity indicators are included in prompt"""
        prompt = get_start_investigation_prompt(
            problem_summary="Test problem",
            complexity_indicators=["multi_turn_conversation", "unclear_scope"],
        )

        # Should include human-readable explanations
        assert "multiple turns" in prompt.lower()
        assert "scope" in prompt.lower()

    def test_custom_time_estimate(self):
        """Test custom time estimate is included"""
        prompt = get_start_investigation_prompt(
            problem_summary="Test problem",
            complexity_indicators=["complexity_detected"],
            estimated_time_range="30-45 minutes",
        )

        assert "30-45 minutes" in prompt


class TestStartInvestigationResponseParsing:
    """Test parsing of user responses to start investigation prompt"""

    @pytest.mark.parametrize("response,expected_decision", [
        ("start investigation", "start"),
        ("Start Investigation", "start"),
        ("let's do it", "start"),
        ("yes", "start"),
        ("ok", "start"),
        ("sure", "start"),
        ("investigate", "start"),
        ("go ahead", "start"),
        ("proceed", "start"),
    ])
    def test_start_keywords(self, response, expected_decision):
        """Test various ways of confirming investigation start"""
        decision, is_ambiguous = parse_start_investigation_response(response)
        assert decision == expected_decision
        assert not is_ambiguous

    @pytest.mark.parametrize("response,expected_decision", [
        ("not yet", "decline"),
        ("keep consulting", "decline"),
        ("no", "decline"),
        ("continue", "decline"),
        ("just questions", "decline"),
        ("not now", "decline"),
        ("maybe later", "decline"),
    ])
    def test_decline_keywords(self, response, expected_decision):
        """Test various ways of declining investigation"""
        decision, is_ambiguous = parse_start_investigation_response(response)
        assert decision == expected_decision
        assert not is_ambiguous

    @pytest.mark.parametrize("response,expected_decision", [
        ("tell me more", "more_info"),
        ("explain", "more_info"),
        ("what does this mean", "more_info"),
        ("how long", "more_info"),
        ("what happens", "more_info"),
    ])
    def test_more_info_keywords(self, response, expected_decision):
        """Test requests for more information"""
        decision, is_ambiguous = parse_start_investigation_response(response)
        assert decision == expected_decision
        assert not is_ambiguous

    @pytest.mark.parametrize("response", [
        "what is the database name?",  # Question, not confirmation
        "yes, but what about...",  # Question despite "yes"
        "random text here",
        "",
        "maybe",
    ])
    def test_ambiguous_responses(self, response):
        """Test responses that are ambiguous"""
        decision, is_ambiguous = parse_start_investigation_response(response)
        assert is_ambiguous
        assert decision == "ambiguous"


class TestStartInvestigationClarification:
    """Test clarification prompts for start investigation"""

    def test_first_attempt_clarification(self):
        """Test first clarification attempt is polite"""
        clarification = get_start_investigation_clarification(attempt=1)
        assert "not sure" in clarification.lower()
        assert "start investigation" in clarification.lower()
        assert "keep consulting" in clarification.lower()

    def test_second_attempt_more_explicit(self):
        """Test second attempt is more explicit"""
        clarification = get_start_investigation_clarification(attempt=2)
        assert "need a clear answer" in clarification.lower()
        assert "type" in clarification.lower()

    def test_third_attempt_graceful_fallback(self):
        """Test third attempt gives up gracefully"""
        clarification = get_start_investigation_clarification(attempt=3)
        assert "continue in consulting mode" in clarification.lower()
        assert "haven't received" in clarification.lower()


# =============================================================================
# Mark Complete Tests
# =============================================================================

class TestMarkCompletePrompt:
    """Test mark complete prompt generation"""

    def test_basic_prompt_generation(self):
        """Test basic prompt with all required fields"""
        prompt = get_mark_complete_prompt(
            root_cause="Memory leak in cache module",
            solution_summary="Implemented cache cleanup",
            verification_details="Memory usage stable after 24h",
            confidence_level=0.85,
            completeness_assessment="FULL",
        )

        assert "Memory leak in cache module" in prompt
        assert "Implemented cache cleanup" in prompt
        assert "85%" in prompt
        assert "FULL" in prompt
        assert "mark as complete" in prompt.lower()

    def test_prompt_includes_warnings(self):
        """Test prompt includes warning about terminal state"""
        prompt = get_mark_complete_prompt(
            root_cause="Test",
            solution_summary="Test",
            verification_details="Test",
            confidence_level=0.75,
            completeness_assessment="HIGH",
        )

        # Should warn about finality
        assert "marked as RESOLVED" in prompt or "complete" in prompt.lower()


class TestMarkCompleteResponseParsing:
    """Test parsing of mark complete responses"""

    @pytest.mark.parametrize("response,expected_decision", [
        ("mark as complete", "complete"),
        ("we're done", "complete"),
        ("yes", "complete"),
        ("close it", "complete"),
        ("done", "complete"),
        ("resolved", "complete"),
    ])
    def test_complete_keywords(self, response, expected_decision):
        """Test various ways of confirming completion"""
        decision, is_ambiguous = parse_mark_complete_response(response)
        assert decision == expected_decision
        assert not is_ambiguous

    @pytest.mark.parametrize("response,expected_decision", [
        ("not yet", "more_verification"),
        ("more verification", "more_verification"),
        ("keep monitoring", "more_verification"),
        ("wait", "more_verification"),
        ("not ready", "more_verification"),
    ])
    def test_more_verification_keywords(self, response, expected_decision):
        """Test requests for more verification"""
        decision, is_ambiguous = parse_mark_complete_response(response)
        assert decision == expected_decision
        assert not is_ambiguous

    def test_question_mark_makes_ambiguous(self):
        """Test that question marks indicate ambiguity"""
        decision, is_ambiguous = parse_mark_complete_response("complete?")
        assert is_ambiguous


# =============================================================================
# Suggest Escalation Tests
# =============================================================================

class TestSuggestEscalationPrompt:
    """Test escalation suggestion prompt generation"""

    def test_basic_prompt_generation(self):
        """Test basic escalation prompt"""
        prompt = get_suggest_escalation_prompt(
            limitation_type="Hypothesis Space Exhausted",
            limitation_explanation="All hypotheses tested",
            findings_summary="Investigated 5 hypotheses",
            confidence_level=0.45,
            next_steps_recommendations=[
                "Escalate to senior engineer",
                "Request additional tools",
            ],
        )

        assert "Hypothesis Space Exhausted" in prompt
        assert "45%" in prompt
        assert "Escalate to senior engineer" in prompt
        assert "close and escalate" in prompt.lower()
        assert "keep trying" in prompt.lower()

    def test_prompt_explains_options(self):
        """Test prompt explains all options clearly"""
        prompt = get_suggest_escalation_prompt(
            limitation_type="Critical Evidence Missing",
            limitation_explanation="Cannot access logs",
            findings_summary="Limited investigation",
            confidence_level=0.3,
            next_steps_recommendations=["Request log access"],
        )

        # Should present 3 options
        assert "Option 1" in prompt
        assert "Option 2" in prompt
        assert "Option 3" in prompt


class TestSuggestEscalationResponseParsing:
    """Test parsing of escalation responses"""

    @pytest.mark.parametrize("response,expected_decision", [
        ("close and escalate", "escalate"),
        ("escalate", "escalate"),
        ("close", "escalate"),
        ("get help", "escalate"),
        ("need expert", "escalate"),
    ])
    def test_escalate_keywords(self, response, expected_decision):
        """Test various ways of confirming escalation"""
        decision, is_ambiguous = parse_suggest_escalation_response(response)
        assert decision == expected_decision
        assert not is_ambiguous

    @pytest.mark.parametrize("response,expected_decision", [
        ("keep trying", "continue"),
        ("continue", "continue"),
        ("keep going", "continue"),
        ("don't give up", "continue"),
        ("try anyway", "continue"),
    ])
    def test_continue_keywords(self, response, expected_decision):
        """Test various ways of continuing despite limitations"""
        decision, is_ambiguous = parse_suggest_escalation_response(response)
        assert decision == expected_decision
        assert not is_ambiguous


# =============================================================================
# Workflow Transition Confirmation Tests
# =============================================================================

class TestWorkflowTransitionConfirmation:
    """Test confirmation messages after transitions"""

    def test_start_investigation_confirmation(self):
        """Test confirmation for starting investigation"""
        confirmation = get_workflow_transition_confirmation(
            "start_investigation",
            {},
        )

        assert "Starting Systematic Investigation" in confirmation
        assert "Phase 1" in confirmation or "Blast Radius" in confirmation

    def test_mark_complete_confirmation(self):
        """Test confirmation for marking complete"""
        confirmation = get_workflow_transition_confirmation(
            "mark_complete",
            {"root_cause": "Memory leak"},
        )

        assert "Investigation Marked as Complete" in confirmation
        assert "Memory leak" in confirmation

    def test_escalate_confirmation(self):
        """Test confirmation for escalation"""
        confirmation = get_workflow_transition_confirmation(
            "escalate",
            {"recommendations": ["Escalate to expert", "Request tools"]},
        )

        assert "Investigation Closed" in confirmation or "Escalation" in confirmation
        assert "Escalate to expert" in confirmation

    def test_declined_investigation_confirmation(self):
        """Test confirmation for declined investigation"""
        confirmation = get_workflow_transition_confirmation(
            "declined_investigation",
            {},
        )

        assert "Continuing in Consulting Mode" in confirmation

    def test_more_verification_confirmation(self):
        """Test confirmation for more verification"""
        confirmation = get_workflow_transition_confirmation(
            "more_verification",
            {},
        )

        assert "Continuing Verification" in confirmation


# =============================================================================
# Edge Cases and Integration Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_empty_complexity_indicators(self):
        """Test prompt generation with empty indicators list"""
        prompt = get_start_investigation_prompt(
            problem_summary="Test problem",
            complexity_indicators=[],
        )

        # Should still generate valid prompt
        assert len(prompt) > 100
        assert "Ready to Start" in prompt

    def test_very_long_problem_summary(self):
        """Test with very long problem summary"""
        long_summary = "A" * 1000
        prompt = get_start_investigation_prompt(
            problem_summary=long_summary,
            complexity_indicators=["complexity_detected"],
        )

        # Should include the summary (truncated or full)
        assert len(prompt) > 100

    def test_case_insensitivity(self):
        """Test that parsing is case-insensitive"""
        decision1, _ = parse_start_investigation_response("START INVESTIGATION")
        decision2, _ = parse_start_investigation_response("start investigation")
        decision3, _ = parse_start_investigation_response("Start Investigation")

        assert decision1 == decision2 == decision3 == "start"

    def test_whitespace_handling(self):
        """Test that extra whitespace is handled"""
        decision, is_ambiguous = parse_start_investigation_response("  start investigation  ")
        assert decision == "start"
        assert not is_ambiguous

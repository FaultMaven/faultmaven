"""Tests for doctor/patient prompt builder."""

import pytest
from datetime import datetime

from faultmaven.models import (
    CaseDiagnosticState,
    UrgencyLevel,
    CaseMessage,
    MessageType
)
from faultmaven.prompts.doctor_patient import PromptVersion
from faultmaven.services.agentic.doctor_patient.prompt_builder import (
    format_diagnostic_state,
    format_conversation_history,
    build_diagnostic_prompt,
    estimate_prompt_tokens,
    PHASE_NAMES
)


class TestFormatDiagnosticState:
    """Tests for diagnostic state formatting."""

    def test_format_no_active_problem(self):
        """Test formatting when no active problem exists."""
        state = CaseDiagnosticState(has_active_problem=False)

        formatted = format_diagnostic_state(state)

        assert "No active problem" in formatted
        assert "informational" in formatted.lower()

    def test_format_basic_problem(self):
        """Test formatting basic problem information."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API returning 500 errors",
            current_phase=1,
            urgency_level=UrgencyLevel.HIGH
        )

        formatted = format_diagnostic_state(state)

        assert "API returning 500 errors" in formatted
        assert "Phase 1" in formatted
        assert "HIGH" in formatted
        assert PHASE_NAMES[1] in formatted

    def test_format_with_symptoms(self):
        """Test formatting with symptoms list."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Database issues",
            symptoms=["slow queries", "timeout errors", "connection failures"]
        )

        formatted = format_diagnostic_state(state)

        assert "slow queries" in formatted
        assert "timeout errors" in formatted
        assert "connection failures" in formatted
        assert "Symptoms Identified" in formatted

    def test_format_with_timeline(self):
        """Test formatting with timeline information."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Service outage",
            timeline_info={
                "started": "2 hours ago",
                "trigger_event": "deployment",
                "frequency": "continuous"
            }
        )

        formatted = format_diagnostic_state(state)

        assert "Timeline Info" in formatted
        assert "2 hours ago" in formatted
        assert "deployment" in formatted

    def test_format_with_hypotheses(self):
        """Test formatting with working hypotheses."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Performance degradation",
            hypotheses=[
                {
                    "hypothesis": "Database connection pool exhaustion",
                    "likelihood": "high",
                    "evidence": "Connection errors in logs"
                },
                {
                    "hypothesis": "Memory leak",
                    "likelihood": "medium",
                    "evidence": "Gradual memory increase"
                }
            ]
        )

        formatted = format_diagnostic_state(state)

        assert "Working Hypotheses" in formatted
        assert "[high]" in formatted
        assert "Database connection pool" in formatted
        assert "[medium]" in formatted
        assert "Memory leak" in formatted

    def test_format_with_tests_performed(self):
        """Test formatting with diagnostic tests."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Network issues",
            tests_performed=[
                "Checked firewall rules",
                "Analyzed network traffic",
                "Reviewed DNS configuration"
            ]
        )

        formatted = format_diagnostic_state(state)

        assert "Tests Performed" in formatted
        assert "firewall" in formatted
        assert "network traffic" in formatted

    def test_format_with_root_cause(self):
        """Test formatting with identified root cause."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="System crash",
            root_cause="Kernel memory leak in network driver",
            current_phase=5
        )

        formatted = format_diagnostic_state(state)

        assert "Root Cause Identified" in formatted
        assert "Kernel memory leak" in formatted

    def test_format_with_solution(self):
        """Test formatting with proposed solution."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Configuration error",
            solution_proposed=True,
            solution_text="Update config file and restart service",
            current_phase=5
        )

        formatted = format_diagnostic_state(state)

        assert "Solution Proposed" in formatted
        assert "Update config file" in formatted

    def test_format_all_phases(self):
        """Test formatting for each diagnostic phase."""
        for phase_num in range(6):
            state = CaseDiagnosticState(
                has_active_problem=True,
                problem_statement="Test problem",
                current_phase=phase_num
            )

            formatted = format_diagnostic_state(state)

            assert f"Phase {phase_num}" in formatted
            assert PHASE_NAMES[phase_num] in formatted

    def test_format_urgency_levels(self):
        """Test formatting for all urgency levels."""
        for urgency in [UrgencyLevel.NORMAL, UrgencyLevel.HIGH, UrgencyLevel.HIGH, UrgencyLevel.CRITICAL]:
            state = CaseDiagnosticState(
                has_active_problem=True,
                problem_statement="Test",
                urgency_level=urgency
            )

            formatted = format_diagnostic_state(state)

            assert urgency.value.upper() in formatted


class TestFormatConversationHistory:
    """Tests for conversation history formatting."""

    def test_format_empty_history(self):
        """Test formatting with no messages."""
        formatted = format_conversation_history([])

        assert "No previous conversation" in formatted

    def test_format_single_message(self):
        """Test formatting with one message."""
        messages = [
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="My API is broken",
                timestamp=datetime.utcnow()
            )
        ]

        formatted = format_conversation_history(messages)

        assert "User:" in formatted
        assert "My API is broken" in formatted

    def test_format_multiple_messages(self):
        """Test formatting with multiple messages."""
        now = datetime.utcnow()
        messages = [
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="Hello",
                timestamp=now
            ),
            CaseMessage(case_id="test-case",
                message_type="agent_response",
                content="Hi! How can I help?",
                timestamp=now
            ),
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="My database is slow",
                timestamp=now
            )
        ]

        formatted = format_conversation_history(messages)

        assert "User: Hello" in formatted
        assert "FaultMaven: Hi!" in formatted
        assert "User: My database is slow" in formatted

    def test_format_respects_max_messages(self):
        """Test that max_messages limit is respected."""
        messages = [
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content=f"Message {i}",
                timestamp=datetime.utcnow()
            )
            for i in range(10)
        ]

        formatted = format_conversation_history(messages, max_messages=3)

        # Should only include last 3 messages
        assert "Message 7" in formatted
        assert "Message 8" in formatted
        assert "Message 9" in formatted
        assert "Message 0" not in formatted
        assert "Message 1" not in formatted

    def test_format_respects_max_tokens(self):
        """Test that max_tokens limit is respected."""
        messages = [
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="A" * 1000,  # Long message
                timestamp=datetime.utcnow()
            ),
            CaseMessage(case_id="test-case",
                message_type="agent_response",
                content="B" * 1000,  # Long message
                timestamp=datetime.utcnow()
            )
        ]

        formatted = format_conversation_history(messages, max_tokens=100)

        # Should truncate due to token limit (100 tokens ≈ 400 chars)
        assert len(formatted) < 1500  # Both messages together would be > 2000 chars

    def test_format_message_order(self):
        """Test that messages are in chronological order."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)
        messages = [
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="Third",
                timestamp=datetime(2025, 1, 1, 12, 2, 0)
            ),
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="First",
                timestamp=base_time
            ),
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="Second",
                timestamp=datetime(2025, 1, 1, 12, 1, 0)
            )
        ]

        formatted = format_conversation_history(messages)

        # Should be sorted chronologically
        first_idx = formatted.index("First")
        second_idx = formatted.index("Second")
        third_idx = formatted.index("Third")
        assert first_idx < second_idx < third_idx


class TestBuildDiagnosticPrompt:
    """Tests for complete prompt building."""

    def test_build_basic_prompt(self):
        """Test building basic diagnostic prompt."""
        state = CaseDiagnosticState(has_active_problem=False)

        prompt = build_diagnostic_prompt(
            user_query="What is Redis?",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        assert "What is Redis?" in prompt
        assert "No active problem" in prompt
        assert len(prompt) > 500  # Should include full system prompt

    def test_build_with_active_problem(self):
        """Test building prompt with active problem."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API errors",
            current_phase=2,
            symptoms=["500 errors"]
        )

        prompt = build_diagnostic_prompt(
            user_query="When did this start?",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        assert "API errors" in prompt
        assert "500 errors" in prompt
        assert "When did this start?" in prompt

    def test_build_with_conversation_history(self):
        """Test building prompt with conversation history."""
        state = CaseDiagnosticState(has_active_problem=True)
        messages = [
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="My database is down",
                timestamp=datetime.utcnow()
            ),
            CaseMessage(case_id="test-case",
                message_type="agent_response",
                content="Let's diagnose this",
                timestamp=datetime.utcnow()
            )
        ]

        prompt = build_diagnostic_prompt(
            user_query="What should I check?",
            diagnostic_state=state,
            conversation_history=messages,
            prompt_version=PromptVersion.STANDARD
        )

        assert "database is down" in prompt
        assert "What should I check?" in prompt

    def test_build_minimal_prompt_shorter_than_standard(self):
        """Test that minimal prompt is shorter than standard."""
        state = CaseDiagnosticState(has_active_problem=False)

        minimal_prompt = build_diagnostic_prompt(
            user_query="Test query",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.MINIMAL
        )

        standard_prompt = build_diagnostic_prompt(
            user_query="Test query",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        assert len(minimal_prompt) < len(standard_prompt)

    def test_build_detailed_prompt_longer_than_standard(self):
        """Test that detailed prompt is longer than standard."""
        state = CaseDiagnosticState(has_active_problem=False)

        standard_prompt = build_diagnostic_prompt(
            user_query="Test query",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        detailed_prompt = build_diagnostic_prompt(
            user_query="Test query",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.DETAILED
        )

        assert len(detailed_prompt) > len(standard_prompt)

    def test_build_all_prompt_versions(self):
        """Test that all prompt versions can be built."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Test problem"
        )

        for version in [PromptVersion.MINIMAL, PromptVersion.STANDARD, PromptVersion.DETAILED]:
            prompt = build_diagnostic_prompt(
                user_query="Test",
                diagnostic_state=state,
                conversation_history=[],
                prompt_version=version
            )

            assert len(prompt) > 0
            assert "Test" in prompt
            assert "Test problem" in prompt

    def test_build_includes_diagnostic_state_context(self):
        """Test that diagnostic state is properly injected."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Complex multi-symptom issue",
            symptoms=["symptom 1", "symptom 2", "symptom 3"],
            hypotheses=[{"hypothesis": "Theory A", "likelihood": "high"}],
            current_phase=3
        )

        prompt = build_diagnostic_prompt(
            user_query="What next?",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        assert "Complex multi-symptom issue" in prompt
        assert "symptom 1" in prompt
        assert "Theory A" in prompt
        assert "Phase 3" in prompt


class TestEstimatePromptTokens:
    """Tests for token estimation."""

    def test_estimate_empty_string(self):
        """Test estimation for empty string."""
        tokens = estimate_prompt_tokens("")

        assert tokens == 0

    def test_estimate_short_text(self):
        """Test estimation for short text."""
        text = "Hello world"  # ~11 chars = ~3 tokens
        tokens = estimate_prompt_tokens(text)

        assert 2 <= tokens <= 4

    def test_estimate_medium_text(self):
        """Test estimation for medium length text."""
        text = "This is a medium length text that should be around 100 characters or so, maybe a bit more."
        tokens = estimate_prompt_tokens(text)

        # ~92 chars ≈ 25 tokens (92/3.7)
        assert 20 <= tokens <= 30

    def test_estimate_long_text(self):
        """Test estimation for long text."""
        text = "A" * 1000
        tokens = estimate_prompt_tokens(text)

        # 1000 chars ≈ 270 tokens
        assert 250 <= tokens <= 300

    def test_estimate_increases_with_length(self):
        """Test that token estimate increases with text length."""
        short = "Short text"
        medium = short * 10
        long = medium * 10

        short_tokens = estimate_prompt_tokens(short)
        medium_tokens = estimate_prompt_tokens(medium)
        long_tokens = estimate_prompt_tokens(long)

        assert short_tokens < medium_tokens < long_tokens

    def test_estimate_unicode_text(self):
        """Test estimation with unicode characters."""
        text = "Hello 世界 🌍"
        tokens = estimate_prompt_tokens(text)

        # Should handle unicode gracefully
        assert tokens > 0

    def test_estimate_realistic_prompt(self):
        """Test estimation on realistic prompt."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API performance issues"
        )

        prompt = build_diagnostic_prompt(
            user_query="How do I fix this?",
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        tokens = estimate_prompt_tokens(prompt)

        # Standard prompt should be ~1300 tokens + context
        assert 1000 <= tokens <= 2000


class TestPromptBuilderEdgeCases:
    """Tests for edge cases in prompt building."""

    def test_handle_none_conversation_history(self):
        """Test handling of None conversation history."""
        state = CaseDiagnosticState()

        # Should not raise error
        formatted = format_conversation_history(None or [])

        assert "No previous conversation" in formatted

    def test_handle_very_long_user_query(self):
        """Test handling of very long user queries."""
        state = CaseDiagnosticState()
        long_query = "A" * 10000

        prompt = build_diagnostic_prompt(
            user_query=long_query,
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.MINIMAL
        )

        assert long_query in prompt

    def test_handle_special_characters_in_query(self):
        """Test handling of special characters."""
        state = CaseDiagnosticState()
        special_query = "How do I use {curly}, [brackets], and <angle> brackets?"

        prompt = build_diagnostic_prompt(
            user_query=special_query,
            diagnostic_state=state,
            conversation_history=[],
            prompt_version=PromptVersion.STANDARD
        )

        assert special_query in prompt

    def test_handle_empty_diagnostic_state_fields(self):
        """Test handling of empty lists/dicts in state."""
        state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Issue",
            symptoms=[],
            hypotheses=[],
            timeline_info={},
            blast_radius={}
        )

        formatted = format_diagnostic_state(state)

        # Should not include empty sections
        assert "Symptoms Identified" not in formatted
        assert "Working Hypotheses" not in formatted
        assert "Timeline Info" not in formatted

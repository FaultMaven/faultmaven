"""Unit tests for SolutionAgent (Phase 5)."""

import pytest

from faultmaven.models import CaseDiagnosticState
from faultmaven.services.agentic.doctor_patient.sub_agents import SolutionAgent
from faultmaven.services.agentic.doctor_patient.sub_agents.base import PhaseContext, PhaseAgentResponse


class MockLLMClient:
    """Mock LLM client for testing."""

    def __init__(self, mock_response: str):
        self.mock_response = mock_response

    async def route(self, prompt: str, temperature: float = 0.7, max_tokens: int = 1000):
        class MockResponse:
            def __init__(self, content):
                self.content = content
        return MockResponse(self.mock_response)


class TestSolutionAgentExtractPhaseContext:
    """Tests for phase context extraction."""

    def test_extract_context_with_root_cause(self):
        """Test context extraction with identified root cause."""
        agent = SolutionAgent(llm_client=None)

        full_state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API errors",
            root_cause="Database connection pool exhausted",
            symptoms=["timeout", "500 errors"],
            blast_radius={"severity": "high"},
            timeline_info={"problem_started_at": "2h ago"},
            hypotheses=[
                {"hypothesis": "Connection pool exhausted", "likelihood": "high"}
            ],
            current_phase=5
        )

        context = agent.extract_phase_context(
            full_diagnostic_state=full_state,
            conversation_history=[],
            user_query="How do I fix this?",
            case_id="test-1"
        )

        assert context.phase == 5
        assert "connection pool" in context.phase_state["root_cause"].lower()


class TestSolutionAgentProcessing:
    """Tests for agent processing logic."""

    @pytest.mark.asyncio
    async def test_process_provides_complete_solution(self):
        """Test that agent provides comprehensive solution."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Here's how to fix the connection pool issue",
            "solution": {
                "fix_description": "Increase database connection pool size and add monitoring",
                "why_this_works": "Root cause is pool exhaustion; larger pool will handle load",
                "implementation_steps": [
                    {
                        "step": 1,
                        "action": "Update database configuration",
                        "command": "SET max_connections = 200",
                        "expected_result": "Pool size increased",
                        "estimated_time": "5 minutes"
                    },
                    {
                        "step": 2,
                        "action": "Restart database service",
                        "command": "systemctl restart postgresql",
                        "expected_result": "Service restarted with new config",
                        "estimated_time": "2 minutes"
                    }
                ],
                "risk_level": "low",
                "risks": ["Brief connection interruption during restart"],
                "rollback_procedure": "Revert config and restart",
                "verification_steps": ["Check connection count < 200", "Monitor error rate"],
                "preventive_measures": ["Add connection pool monitoring", "Set up alerts"]
            },
            "phase_complete": true,
            "confidence": 0.90
        }
        """)

        agent = SolutionAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=5,
            user_query="How do I fix this?",
            phase_state={
                "problem_statement": "API errors",
                "root_cause": "Connection pool exhausted"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        assert isinstance(response, PhaseAgentResponse)
        solution = response.state_updates.get("solution_details", {})
        assert "fix_description" in solution
        assert len(solution.get("implementation_steps", [])) >= 2
        assert "verification_steps" in solution
        assert "preventive_measures" in solution
        assert response.state_updates.get("solution_proposed") == True
        assert response.phase_complete == True

    @pytest.mark.asyncio
    async def test_process_includes_risk_assessment(self):
        """Test that solution includes risk assessment."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "This fix requires careful consideration",
            "solution": {
                "fix_description": "Rollback to previous version",
                "why_this_works": "Previous version was stable",
                "implementation_steps": [{"step": 1, "action": "Deploy v2.2.0"}],
                "risk_level": "medium",
                "risks": ["Losing new features", "Requires deployment window"],
                "rollback_procedure": "Deploy v2.3.1 again if needed"
            },
            "phase_complete": true,
            "confidence": 0.85
        }
        """)

        agent = SolutionAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=5,
            user_query="What's the solution?",
            phase_state={"root_cause": "Bug in v2.3.1"},
            recent_context=[],
            case_id="test-2",
            urgency_level="high"
        )

        response = await agent.process(context)

        solution = response.state_updates.get("solution_details", {})
        assert solution.get("risk_level") == "medium"
        assert len(solution.get("risks", [])) > 0
        assert "rollback_procedure" in solution


class TestSolutionAgentPhaseAdvancement:
    """Tests for phase advancement logic."""

    def test_should_not_advance_from_solution_phase(self):
        """Test that solution phase is terminal - doesn't advance."""
        agent = SolutionAgent(llm_client=None)

        context = PhaseContext(
            phase=5,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Solution provided",
            state_updates={"solution_proposed": True},
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=True,
            confidence=0.90,
            recommended_next_phase=5  # Stays in phase 5
        )

        should_advance = agent.should_advance_phase(context, response)
        # Solution is final phase - should not advance
        assert should_advance == False


class TestSolutionAgentPromptBuilding:
    """Tests for prompt construction."""

    def test_build_prompt_includes_root_cause(self):
        """Test that prompt includes root cause and validation evidence."""
        agent = SolutionAgent(llm_client=None)

        context = PhaseContext(
            phase=5,
            user_query="How to fix?",
            phase_state={
                "problem_statement": "Database errors",
                "root_cause": "Connection pool exhausted",
                "validation_evidence": "[HIGH] Pool at 100% capacity"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        prompt = agent.build_prompt(context)

        assert "Connection pool exhausted" in prompt
        assert "100% capacity" in prompt

    def test_build_prompt_size_approximately_650_tokens(self):
        """Test that prompt is approximately 650 tokens."""
        agent = SolutionAgent(llm_client=None)

        context = PhaseContext(
            phase=5,
            user_query="Solution?",
            phase_state={
                "problem_statement": "Issue",
                "root_cause": "Known cause"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        prompt = agent.build_prompt(context)

        estimated_tokens = len(prompt) / 3.7
        assert 400 <= estimated_tokens <= 900


class TestSolutionAgentImplementationSteps:
    """Tests for implementation step formatting."""

    @pytest.mark.asyncio
    async def test_provides_sequential_steps(self):
        """Test that solution has clear sequential steps."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Follow these steps to resolve the issue",
            "solution": {
                "fix_description": "Increase pool and restart",
                "implementation_steps": [
                    {"step": 1, "action": "Update config", "command": "edit config.yml"},
                    {"step": 2, "action": "Restart service", "command": "restart app"},
                    {"step": 3, "action": "Verify fix", "command": "check logs"}
                ]
            },
            "phase_complete": true,
            "confidence": 0.88
        }
        """)

        agent = SolutionAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=5,
            user_query="Walk me through the fix",
            phase_state={"root_cause": "Config issue"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        steps = response.state_updates.get("solution_details", {}).get("implementation_steps", [])
        assert len(steps) >= 3
        assert steps[0]["step"] == 1
        assert steps[1]["step"] == 2


class TestSolutionAgentPreventiveMeasures:
    """Tests for preventive recommendations."""

    @pytest.mark.asyncio
    async def test_includes_preventive_measures(self):
        """Test that solution includes prevention recommendations."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Here's the fix and how to prevent recurrence",
            "solution": {
                "fix_description": "Restart service",
                "implementation_steps": [{"step": 1, "action": "Restart"}],
                "preventive_measures": [
                    "Add monitoring for connection pool usage",
                    "Set up alerts at 80% capacity",
                    "Implement connection recycling"
                ]
            },
            "phase_complete": true,
            "confidence": 0.87
        }
        """)

        agent = SolutionAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=5,
            user_query="How do I prevent this?",
            phase_state={"root_cause": "Pool exhaustion"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        preventive = response.state_updates.get("solution_details", {}).get("preventive_measures", [])
        assert len(preventive) >= 2
        assert any("monitor" in m.lower() for m in preventive)


class TestSolutionAgentEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_handle_no_root_cause(self):
        """Test handling when root cause not clearly identified."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Without a confirmed root cause, here's a general troubleshooting approach",
            "solution": {
                "fix_description": "General troubleshooting steps",
                "implementation_steps": [
                    {"step": 1, "action": "Gather more diagnostics"}
                ],
                "risk_level": "low"
            },
            "phase_complete": true,
            "confidence": 0.60
        }
        """)

        agent = SolutionAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=5,
            user_query="What should I do?",
            phase_state={"root_cause": "Not yet identified"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        # Should still provide solution even if confidence is lower
        assert response.state_updates.get("solution_proposed") == True
        assert response.confidence < 0.8

    @pytest.mark.asyncio
    async def test_handle_heuristic_extraction(self):
        """Test heuristic solution extraction from text."""
        mock_llm = MockLLMClient(mock_response="""
        To fix this issue, follow these steps:

        1. First, increase the database connection pool from 100 to 200
        2. Then restart the API service to pick up the new settings
        3. Monitor the connection count to ensure it stays below the new limit

        This should resolve the 500 errors. To prevent this in the future,
        set up monitoring and alerts for connection pool utilization.
        """)

        agent = SolutionAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=5,
            user_query="How to fix?",
            phase_state={"root_cause": "Connection pool"},
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        # Heuristic should still mark solution as proposed
        assert response.state_updates.get("solution_proposed") == True
        assert "solution_text" in response.state_updates

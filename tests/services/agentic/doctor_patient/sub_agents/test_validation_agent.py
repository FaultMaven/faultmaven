"""Unit tests for ValidationAgent (Phase 4)."""

import pytest

from faultmaven.models import CaseDiagnosticState
from faultmaven.services.agentic.doctor_patient.sub_agents import ValidationAgent
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


class TestValidationAgentExtractPhaseContext:
    """Tests for phase context extraction."""

    def test_extract_context_with_hypotheses(self):
        """Test context extraction with hypotheses to validate."""
        agent = ValidationAgent(llm_client=None)

        full_state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API errors",
            symptoms=["timeout", "500 errors"],
            blast_radius={"severity": "high"},
            timeline_info={"problem_started_at": "2h ago"},
            hypotheses=[
                {"hypothesis": "Connection pool exhausted", "likelihood": "high"},
                {"hypothesis": "Memory leak", "likelihood": "medium"}
            ],
            tests_performed=["checked logs"],
            current_phase=4
        )

        context = agent.extract_phase_context(
            full_diagnostic_state=full_state,
            conversation_history=[],
            user_query="Let's test the connection pool theory",
            case_id="test-1"
        )

        assert context.phase == 4
        assert "Connection pool" in context.phase_state["hypotheses"]


class TestValidationAgentProcessing:
    """Tests for agent processing logic."""

    @pytest.mark.asyncio
    async def test_process_confirms_hypothesis(self):
        """Test hypothesis confirmation through validation."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "The connection pool theory is confirmed by the metrics",
            "validation_results": [
                {
                    "hypothesis_index": 0,
                    "status": "confirmed",
                    "evidence": ["Pool at 100% capacity", "Connections timing out"],
                    "updated_likelihood": "high"
                }
            ],
            "root_cause_confidence": 0.92,
            "phase_complete": true,
            "confidence": 0.92
        }
        """)

        agent = ValidationAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=4,
            user_query="Connection pool shows 100% utilization",
            phase_state={
                "problem_statement": "API errors",
                "hypotheses": "[HIGH] Connection pool exhausted"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        assert isinstance(response, PhaseAgentResponse)
        results = response.state_updates.get("validation_results", [])
        assert len(results) > 0
        assert results[0]["status"] == "confirmed"
        assert response.confidence >= 0.8
        assert response.phase_complete == True
        assert response.recommended_next_phase == 5

    @pytest.mark.asyncio
    async def test_process_refutes_hypothesis(self):
        """Test hypothesis refutation."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "The data refutes the memory leak theory",
            "validation_results": [
                {
                    "hypothesis_index": 1,
                    "status": "refuted",
                    "evidence": ["Memory usage stable", "No gradual increase"],
                    "updated_likelihood": "low"
                }
            ],
            "root_cause_confidence": 0.3,
            "phase_complete": false,
            "confidence": 0.65
        }
        """)

        agent = ValidationAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=4,
            user_query="Memory metrics are stable",
            phase_state={"hypotheses": "[MEDIUM] Memory leak"},
            recent_context=[],
            case_id="test-2",
            urgency_level="normal"
        )

        response = await agent.process(context)

        results = response.state_updates.get("validation_results", [])
        assert results[0]["status"] == "refuted"
        assert response.phase_complete == False


class TestValidationAgentPhaseAdvancement:
    """Tests for phase advancement logic."""

    def test_should_advance_with_high_confidence(self):
        """Test advancement when root cause identified with confidence > 0.8."""
        agent = ValidationAgent(llm_client=None)

        context = PhaseContext(
            phase=4,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Root cause confirmed",
            state_updates={
                "validation_results": [{"status": "confirmed"}],
                "root_cause": "Connection pool exhausted"
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=True,
            confidence=0.90,
            recommended_next_phase=5
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == True

    def test_should_not_advance_low_confidence(self):
        """Test no advancement when confidence < 0.8."""
        agent = ValidationAgent(llm_client=None)

        context = PhaseContext(
            phase=4,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Still uncertain",
            state_updates={"validation_results": []},
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.60,
            recommended_next_phase=4
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == False


class TestValidationAgentPromptBuilding:
    """Tests for prompt construction."""

    def test_build_prompt_includes_hypotheses(self):
        """Test that prompt includes hypotheses to validate."""
        agent = ValidationAgent(llm_client=None)

        context = PhaseContext(
            phase=4,
            user_query="Test the theory",
            phase_state={
                "problem_statement": "Database slow",
                "hypotheses": "[HIGH] Connection pool\n[MEDIUM] Query inefficiency"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        prompt = agent.build_prompt(context)

        assert "Database slow" in prompt
        assert "Connection pool" in prompt

    def test_build_prompt_size_approximately_700_tokens(self):
        """Test that prompt is approximately 700 tokens."""
        agent = ValidationAgent(llm_client=None)

        context = PhaseContext(
            phase=4,
            user_query="Validate",
            phase_state={
                "problem_statement": "Issue",
                "hypotheses": "Theory 1, Theory 2"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        prompt = agent.build_prompt(context)

        estimated_tokens = len(prompt) / 3.7
        assert 450 <= estimated_tokens <= 950


class TestValidationAgentSystematicTesting:
    """Tests for systematic validation approach."""

    @pytest.mark.asyncio
    async def test_prioritizes_high_likelihood_hypotheses(self):
        """Test that agent prioritizes high-likelihood theories first."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Let's test the high-likelihood theory first",
            "validation_results": [
                {
                    "hypothesis_index": 0,
                    "status": "confirmed",
                    "evidence": ["Metrics confirm this theory"]
                }
            ],
            "recommended_test": {
                "hypothesis_index": 1,
                "test_description": "Test medium likelihood theory next"
            },
            "root_cause_confidence": 0.85,
            "phase_complete": true,
            "confidence": 0.85
        }
        """)

        agent = ValidationAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=4,
            user_query="Start validation",
            phase_state={"hypotheses": "[HIGH] Theory A\n[MEDIUM] Theory B"},
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        # Should validate high-likelihood first
        assert response.confidence >= 0.8


class TestValidationAgentEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_handle_inconclusive_evidence(self):
        """Test handling of inconclusive validation results."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "The evidence is inconclusive",
            "validation_results": [
                {
                    "hypothesis_index": 0,
                    "status": "inconclusive",
                    "evidence": ["Some supporting data", "Some contradictory data"]
                }
            ],
            "root_cause_confidence": 0.5,
            "phase_complete": false,
            "confidence": 0.50
        }
        """)

        agent = ValidationAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=4,
            user_query="Results are mixed",
            phase_state={"hypotheses": "Theory"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.phase_complete == False
        assert response.confidence < 0.8

    @pytest.mark.asyncio
    async def test_handle_no_hypotheses(self):
        """Test handling when no hypotheses available to validate."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "We need to formulate hypotheses first",
            "validation_results": [],
            "root_cause_confidence": 0.0,
            "phase_complete": false,
            "confidence": 0.3
        }
        """)

        agent = ValidationAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=4,
            user_query="Validate",
            phase_state={"hypotheses": "No hypotheses"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.phase_complete == False
        assert len(response.state_updates.get("validation_results", [])) == 0

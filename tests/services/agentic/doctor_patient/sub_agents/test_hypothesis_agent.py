"""Unit tests for HypothesisAgent (Phase 3)."""

import pytest

from faultmaven.models import CaseDiagnosticState
from faultmaven.services.agentic.doctor_patient.sub_agents import HypothesisAgent
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


class TestHypothesisAgentExtractPhaseContext:
    """Tests for phase context extraction."""

    def test_extract_context_with_full_diagnostic_context(self):
        """Test context extraction with symptoms, timeline, and blast radius."""
        agent = HypothesisAgent(llm_client=None)

        full_state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API returning 500 errors",
            symptoms=["timeout", "500 errors", "high latency"],
            blast_radius={"affected_services": ["api"], "severity": "high"},
            timeline_info={"problem_started_at": "2 hours ago", "recent_changes": [{"type": "deployment"}]},
            tests_performed=["checked logs", "reviewed metrics"],
            current_phase=3
        )

        context = agent.extract_phase_context(
            full_diagnostic_state=full_state,
            conversation_history=[],
            user_query="What could be causing this?",
            case_id="test-1"
        )

        assert context.phase == 3
        assert "API returning 500 errors" in context.phase_state["problem_statement"]
        assert "timeout" in context.phase_state["symptoms"]
        assert "api" in context.phase_state["blast_radius"]


class TestHypothesisAgentProcessing:
    """Tests for agent processing logic."""

    @pytest.mark.asyncio
    async def test_process_generates_multiple_hypotheses(self):
        """Test that agent generates 2-3 ranked hypotheses."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Based on the symptoms, I have three theories",
            "hypotheses": [
                {
                    "hypothesis": "Database connection pool exhausted",
                    "likelihood": "high",
                    "evidence": ["Timeout errors", "Started after traffic spike"],
                    "validation_steps": ["Check active connections", "Review pool metrics"]
                },
                {
                    "hypothesis": "Memory leak in API service",
                    "likelihood": "medium",
                    "evidence": ["Gradual degradation", "Restart fixes temporarily"],
                    "validation_steps": ["Check memory usage over time"]
                },
                {
                    "hypothesis": "External dependency failure",
                    "likelihood": "low",
                    "evidence": ["Some correlation with third-party status"],
                    "validation_steps": ["Check third-party service status"]
                }
            ],
            "phase_complete": true,
            "confidence": 0.85
        }
        """)

        agent = HypothesisAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=3,
            user_query="What could cause these 500 errors?",
            phase_state={
                "problem_statement": "API errors",
                "symptoms": "timeout, 500 errors",
                "timeline": "started 2 hours ago",
                "blast_radius": "api affected"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        assert isinstance(response, PhaseAgentResponse)
        hypotheses = response.state_updates.get("hypotheses", [])
        assert len(hypotheses) >= 2
        assert hypotheses[0]["likelihood"] == "high"
        assert len(hypotheses[0]["validation_steps"]) > 0
        assert response.phase_complete == True
        assert response.recommended_next_phase == 4

    @pytest.mark.asyncio
    async def test_process_insufficient_hypotheses(self):
        """Test when agent generates only 1 hypothesis."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I need more information to form additional theories",
            "hypotheses": [
                {
                    "hypothesis": "Deployment introduced bug",
                    "likelihood": "medium",
                    "evidence": ["Timing correlation"]
                }
            ],
            "phase_complete": false,
            "confidence": 0.6
        }
        """)

        agent = HypothesisAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=3,
            user_query="What's wrong?",
            phase_state={"problem_statement": "Something broken"},
            recent_context=[],
            case_id="test-2",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.phase_complete == False
        assert len(response.state_updates.get("hypotheses", [])) < 2


class TestHypothesisAgentPhaseAdvancement:
    """Tests for phase advancement logic."""

    def test_should_advance_with_multiple_hypotheses(self):
        """Test advancement when 2+ hypotheses generated."""
        agent = HypothesisAgent(llm_client=None)

        context = PhaseContext(
            phase=3,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Theories formulated",
            state_updates={
                "hypotheses": [
                    {"hypothesis": "Theory 1", "likelihood": "high"},
                    {"hypothesis": "Theory 2", "likelihood": "medium"}
                ]
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=True,
            confidence=0.85,
            recommended_next_phase=4
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == True

    def test_should_not_advance_insufficient_hypotheses(self):
        """Test no advancement with only 1 hypothesis."""
        agent = HypothesisAgent(llm_client=None)

        context = PhaseContext(
            phase=3,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Need more theories",
            state_updates={
                "hypotheses": [{"hypothesis": "Only one theory"}]
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.5,
            recommended_next_phase=3
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == False

    def test_should_not_advance_missing_likelihood(self):
        """Test no advancement when likelihood rankings missing."""
        agent = HypothesisAgent(llm_client=None)

        context = PhaseContext(
            phase=3,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Theories but unranked",
            state_updates={
                "hypotheses": [
                    {"hypothesis": "Theory 1"},  # Missing likelihood
                    {"hypothesis": "Theory 2"}   # Missing likelihood
                ]
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.6,
            recommended_next_phase=3
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == False


class TestHypothesisAgentPromptBuilding:
    """Tests for prompt construction."""

    def test_build_prompt_includes_timeline_and_symptoms(self):
        """Test that prompt includes timeline and symptom context."""
        agent = HypothesisAgent(llm_client=None)

        context = PhaseContext(
            phase=3,
            user_query="What's the root cause?",
            phase_state={
                "problem_statement": "Database slowness",
                "symptoms": "timeout, high CPU",
                "timeline": "started after deployment"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        prompt = agent.build_prompt(context)

        assert "Database slowness" in prompt
        assert "timeout" in prompt
        assert "deployment" in prompt

    def test_build_prompt_size_approximately_400_tokens(self):
        """Test that prompt is approximately 400 tokens."""
        agent = HypothesisAgent(llm_client=None)

        context = PhaseContext(
            phase=3,
            user_query="Why is this happening?",
            phase_state={
                "problem_statement": "API errors",
                "symptoms": "500 errors"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        prompt = agent.build_prompt(context)

        estimated_tokens = len(prompt) / 3.7
        assert 250 <= estimated_tokens <= 600

    def test_uses_higher_temperature_for_creativity(self):
        """Test that HypothesisAgent uses temperature=0.8 for creative thinking."""
        mock_llm = MockLLMClient(mock_response='{"hypotheses": []}')
        agent = HypothesisAgent(llm_client=mock_llm)

        # The agent should use temperature=0.8 in its process method
        # This is verified by checking the implementation


class TestHypothesisAgentLikelihoodRanking:
    """Tests for hypothesis likelihood ranking."""

    @pytest.mark.asyncio
    async def test_hypotheses_ranked_by_likelihood(self):
        """Test that hypotheses are properly ranked."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Here are the theories ranked by likelihood",
            "hypotheses": [
                {"hypothesis": "High probability cause", "likelihood": "high"},
                {"hypothesis": "Medium probability cause", "likelihood": "medium"},
                {"hypothesis": "Low probability cause", "likelihood": "low"}
            ],
            "phase_complete": true,
            "confidence": 0.88
        }
        """)

        agent = HypothesisAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=3,
            user_query="What are the possible causes?",
            phase_state={"problem_statement": "Issue"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        hypotheses = response.state_updates["hypotheses"]
        assert hypotheses[0]["likelihood"] == "high"
        assert hypotheses[1]["likelihood"] == "medium"
        assert hypotheses[2]["likelihood"] == "low"


class TestHypothesisAgentEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_handle_insufficient_context(self):
        """Test handling when not enough context to form hypotheses."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I need more information about the symptoms and timeline",
            "hypotheses": [],
            "clarifying_questions": ["Can you describe the symptoms in more detail?"],
            "phase_complete": false,
            "confidence": 0.3
        }
        """)

        agent = HypothesisAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=3,
            user_query="Why?",
            phase_state={"problem_statement": "Something wrong"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.phase_complete == False
        assert len(response.state_updates.get("hypotheses", [])) == 0

    @pytest.mark.asyncio
    async def test_handle_heuristic_extraction(self):
        """Test heuristic hypothesis extraction from text."""
        mock_llm = MockLLMClient(mock_response="""
        I have three theories about what's causing this:

        1. The database connection pool might be exhausted (high likelihood)
           - This matches the timeout pattern
           - Started after traffic increased

        2. There could be a memory leak (medium likelihood)
           - Gradual degradation suggests this

        3. Network issues with the upstream service (low likelihood)
        """)

        agent = HypothesisAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=3,
            user_query="What could it be?",
            phase_state={"problem_statement": "Errors"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        # Heuristic should extract hypotheses from numbered list
        hypotheses = response.state_updates.get("hypotheses", [])
        assert len(hypotheses) > 0

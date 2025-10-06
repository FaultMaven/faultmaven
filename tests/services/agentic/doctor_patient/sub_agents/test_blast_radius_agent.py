"""Unit tests for BlastRadiusAgent (Phase 1)."""

import pytest
from datetime import datetime

from faultmaven.models import CaseDiagnosticState, UrgencyLevel
from faultmaven.services.agentic.doctor_patient.sub_agents import BlastRadiusAgent
from faultmaven.services.agentic.doctor_patient.sub_agents.base import PhaseContext, PhaseAgentResponse


class MockLLMClient:
    """Mock LLM client for testing."""

    def __init__(self, mock_response: str):
        self.mock_response = mock_response
        self.last_prompt = None
        self.last_temperature = None

    async def route(self, prompt: str, temperature: float = 0.7, max_tokens: int = 1000):
        """Mock LLM route call."""
        self.last_prompt = prompt
        self.last_temperature = temperature

        class MockResponse:
            def __init__(self, content):
                self.content = content

        return MockResponse(self.mock_response)


class TestBlastRadiusAgentExtractPhaseContext:
    """Tests for phase context extraction."""

    def test_extract_context_with_problem_and_symptoms(self):
        """Test context extraction with problem statement and symptoms."""
        agent = BlastRadiusAgent(llm_client=None)

        full_state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API returning 500 errors",
            symptoms=["timeout errors", "high latency", "connection failures"],
            current_phase=1,
            urgency_level=UrgencyLevel.HIGH
        )

        context = agent.extract_phase_context(
            full_diagnostic_state=full_state,
            conversation_history=[],
            user_query="Which services are affected?",
            case_id="test-case-1"
        )

        assert context.phase == 1
        assert context.phase_state["problem_statement"] == "API returning 500 errors"
        assert "timeout errors" in context.phase_state["symptoms"]
        assert context.urgency_level == "high"

    def test_extract_context_with_existing_blast_radius(self):
        """Test context extraction when blast radius partially defined."""
        agent = BlastRadiusAgent(llm_client=None)

        full_state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="Database slow",
            blast_radius={
                "affected_services": ["api", "worker"],
                "severity": "medium"
            },
            current_phase=1
        )

        context = agent.extract_phase_context(
            full_diagnostic_state=full_state,
            conversation_history=[],
            user_query="Any other systems affected?",
            case_id="test-case-2"
        )

        assert "api" in context.phase_state["existing_blast_radius"]
        assert "medium" in context.phase_state["existing_blast_radius"]


class TestBlastRadiusAgentProcessing:
    """Tests for agent processing logic."""

    @pytest.mark.asyncio
    async def test_process_defines_scope_json(self):
        """Test that agent defines blast radius from JSON response."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Based on the symptoms, this affects the API and database services",
            "blast_radius": {
                "affected_services": ["api", "database", "cache"],
                "affected_users": "all",
                "environments": ["production"],
                "severity": "critical",
                "error_patterns": ["500 Internal Server Error", "Connection timeout"],
                "dependencies_impacted": ["payment-service", "notification-service"]
            },
            "clarifying_questions": [],
            "suggested_commands": [],
            "phase_complete": true,
            "confidence": 0.85
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="All users are seeing 500 errors",
            phase_state={
                "problem_statement": "API errors",
                "symptoms": "500 errors",
                "urgency_level": "high"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        assert isinstance(response, PhaseAgentResponse)
        blast_radius = response.state_updates["blast_radius"]
        assert "api" in blast_radius["affected_services"]
        assert blast_radius["severity"] == "critical"
        assert blast_radius["affected_users"] == "all"
        assert response.phase_complete == True
        assert response.recommended_next_phase == 2

    @pytest.mark.asyncio
    async def test_process_incomplete_scope(self):
        """Test that agent requests more info when scope unclear."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I need more information to determine the blast radius",
            "blast_radius": {
                "affected_services": [],
                "severity": "unknown"
            },
            "clarifying_questions": [
                "Which specific services are showing errors?",
                "Is this affecting all users or specific ones?"
            ],
            "phase_complete": false,
            "confidence": 0.4
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Something is broken",
            phase_state={"problem_statement": "Unknown issue"},
            recent_context=[],
            case_id="test-2",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.phase_complete == False
        assert response.recommended_next_phase == 1  # Stay in phase 1

    @pytest.mark.asyncio
    async def test_process_heuristic_fallback(self):
        """Test heuristic extraction when JSON parsing fails."""
        mock_llm = MockLLMClient(mock_response="""
        This is a critical production outage affecting all users.
        The API service and database are both impacted with 500 errors.
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Production is down",
            phase_state={"problem_statement": "Outage"},
            recent_context=[],
            case_id="test-3",
            urgency_level="critical"
        )

        response = await agent.process(context)

        blast_radius = response.state_updates["blast_radius"]
        # Heuristic should detect critical severity
        assert blast_radius.get("severity") == "critical"
        # Should detect "all users" mention
        assert blast_radius.get("affected_users") in ["all", "subset"]


class TestBlastRadiusAgentPhaseAdvancement:
    """Tests for phase advancement logic."""

    def test_should_advance_with_complete_blast_radius(self):
        """Test advancement when scope and severity defined."""
        agent = BlastRadiusAgent(llm_client=None)

        context = PhaseContext(
            phase=1,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Blast radius defined",
            state_updates={
                "blast_radius": {
                    "affected_services": ["api", "database"],
                    "severity": "high",
                    "affected_users": "all"
                }
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=True,
            confidence=0.85,
            recommended_next_phase=2
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == True

    def test_should_not_advance_missing_severity(self):
        """Test no advancement when severity missing."""
        agent = BlastRadiusAgent(llm_client=None)

        context = PhaseContext(
            phase=1,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Partial info",
            state_updates={
                "blast_radius": {
                    "affected_services": ["api"]
                    # Missing severity
                }
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.6,
            recommended_next_phase=1
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == False

    def test_should_not_advance_missing_scope(self):
        """Test no advancement when scope missing."""
        agent = BlastRadiusAgent(llm_client=None)

        context = PhaseContext(
            phase=1,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = PhaseAgentResponse(
            answer="Partial info",
            state_updates={
                "blast_radius": {
                    "severity": "high"
                    # Missing affected_services or affected_users
                }
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.6,
            recommended_next_phase=1
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == False


class TestBlastRadiusAgentPromptBuilding:
    """Tests for prompt construction."""

    def test_build_prompt_includes_problem_statement(self):
        """Test that prompt includes problem statement."""
        agent = BlastRadiusAgent(llm_client=None)

        context = PhaseContext(
            phase=1,
            user_query="Which services affected?",
            phase_state={
                "problem_statement": "API returning 500 errors",
                "symptoms": "timeout, errors",
                "urgency_level": "high"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        prompt = agent.build_prompt(context)

        assert "API returning 500 errors" in prompt
        assert "timeout" in prompt

    def test_build_prompt_size_approximately_500_tokens(self):
        """Test that prompt is approximately 500 tokens as designed."""
        agent = BlastRadiusAgent(llm_client=None)

        context = PhaseContext(
            phase=1,
            user_query="What's the scope?",
            phase_state={
                "problem_statement": "Database issues",
                "symptoms": "slow queries",
                "urgency_level": "medium"
            },
            recent_context=["Previous message"],
            case_id="test-1",
            urgency_level="medium"
        )

        prompt = agent.build_prompt(context)

        # Estimate tokens (1 token ≈ 3.7 chars)
        estimated_tokens = len(prompt) / 3.7

        # Should be around 500 tokens (allow ±200 margin)
        assert 300 <= estimated_tokens <= 700


class TestBlastRadiusAgentSeverityDetection:
    """Tests for severity classification."""

    @pytest.mark.asyncio
    async def test_detect_critical_severity(self):
        """Test detection of critical severity indicators."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "This is a complete production outage",
            "blast_radius": {
                "affected_services": ["all"],
                "severity": "critical",
                "affected_users": "all"
            },
            "phase_complete": true,
            "confidence": 0.95
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Production is completely down!",
            phase_state={"problem_statement": "Complete outage"},
            recent_context=[],
            case_id="test-1",
            urgency_level="critical"
        )

        response = await agent.process(context)

        assert response.state_updates["blast_radius"]["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_detect_medium_severity(self):
        """Test detection of medium severity."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Some users experiencing intermittent slowness",
            "blast_radius": {
                "affected_services": ["api"],
                "severity": "medium",
                "affected_users": "subset"
            },
            "phase_complete": true,
            "confidence": 0.80
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Some users report slowness",
            phase_state={"problem_statement": "Performance degradation"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.state_updates["blast_radius"]["severity"] in ["medium", "low"]


class TestBlastRadiusAgentPatternDetection:
    """Tests for error pattern identification."""

    @pytest.mark.asyncio
    async def test_detect_error_patterns(self):
        """Test identification of error patterns."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I see a pattern of 500 and 503 errors",
            "blast_radius": {
                "affected_services": ["api"],
                "severity": "high",
                "error_patterns": ["500 Internal Server Error", "503 Service Unavailable"],
                "affected_users": "all"
            },
            "phase_complete": true,
            "confidence": 0.88
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Users seeing 500 and 503 errors",
            phase_state={
                "problem_statement": "HTTP errors",
                "symptoms": "500, 503"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        patterns = response.state_updates["blast_radius"]["error_patterns"]
        assert "500" in str(patterns)
        assert "503" in str(patterns)


class TestBlastRadiusAgentEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_handle_vague_scope_description(self):
        """Test handling of vague user description."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I need more specific information about which systems are affected",
            "blast_radius": {},
            "clarifying_questions": [
                "Can you specify which services are showing errors?",
                "Are you seeing this in production or staging?"
            ],
            "phase_complete": false,
            "confidence": 0.3
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Everything is broken",
            phase_state={"problem_statement": "Something wrong"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        assert response.phase_complete == False
        assert response.confidence < 0.5

    @pytest.mark.asyncio
    async def test_handle_empty_response(self):
        """Test handling of empty LLM response."""
        mock_llm = MockLLMClient(mock_response="")

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="Test",
            phase_state={"problem_statement": "Test"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        # Should provide fallback response
        assert isinstance(response, PhaseAgentResponse)
        assert "blast_radius" in response.state_updates

    @pytest.mark.asyncio
    async def test_handle_partial_blast_radius_info(self):
        """Test handling when only partial blast radius provided."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I can see the API is affected but need more info",
            "blast_radius": {
                "affected_services": ["api"]
            },
            "clarifying_questions": ["What's the error rate?"],
            "phase_complete": false,
            "confidence": 0.65
        }
        """)

        agent = BlastRadiusAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=1,
            user_query="API has issues",
            phase_state={"problem_statement": "API problem"},
            recent_context=[],
            case_id="test-1",
            urgency_level="normal"
        )

        response = await agent.process(context)

        # Should accept partial info but not advance
        assert "api" in response.state_updates["blast_radius"]["affected_services"]
        assert response.phase_complete == False

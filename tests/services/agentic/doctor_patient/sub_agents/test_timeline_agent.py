"""Unit tests for TimelineAgent (Phase 2)."""

import pytest
from datetime import datetime

from faultmaven.models import CaseDiagnosticState, UrgencyLevel
from faultmaven.services.agentic.doctor_patient.sub_agents import TimelineAgent
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


class TestTimelineAgentExtractPhaseContext:
    """Tests for phase context extraction."""

    def test_extract_context_with_problem_and_blast_radius(self):
        """Test context extraction with problem and blast radius info."""
        agent = TimelineAgent(llm_client=None)

        full_state = CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API returning 500 errors",
            symptoms=["timeout", "500 errors"],
            blast_radius={"affected_services": ["api", "database"], "severity": "high"},
            current_phase=2
        )

        context = agent.extract_phase_context(
            full_diagnostic_state=full_state,
            conversation_history=[],
            user_query="When did this start?",
            case_id="test-1"
        )

        assert context.phase == 2
        assert "API returning 500 errors" in context.phase_state["problem_statement"]
        assert "api" in context.phase_state["blast_radius"]


class TestTimelineAgentProcessing:
    """Tests for agent processing logic."""

    @pytest.mark.asyncio
    async def test_process_establishes_timeline_json(self):
        """Test that agent establishes timeline from JSON response."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "The issue started after the v2.3.1 deployment",
            "timeline_info": {
                "problem_started_at": "2024-01-15 14:30 UTC",
                "problem_duration": "2 hours",
                "last_known_good": "2024-01-15 14:00 UTC",
                "recent_changes": [
                    {
                        "type": "deployment",
                        "description": "Deployed API v2.3.1",
                        "timestamp": "2024-01-15 14:25 UTC",
                        "correlation": "high"
                    }
                ],
                "pattern": "sudden"
            },
            "phase_complete": true,
            "confidence": 0.90
        }
        """)

        agent = TimelineAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=2,
            user_query="Started after we deployed v2.3.1",
            phase_state={
                "problem_statement": "API errors",
                "blast_radius": "api, database"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        assert isinstance(response, PhaseAgentResponse)
        timeline = response.state_updates["timeline_info"]
        assert "2024-01-15" in timeline["problem_started_at"]
        assert len(timeline["recent_changes"]) > 0
        assert timeline["recent_changes"][0]["type"] == "deployment"
        assert response.phase_complete == True
        assert response.recommended_next_phase == 3

    @pytest.mark.asyncio
    async def test_process_incomplete_timeline(self):
        """Test when timeline is unclear."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "I need more information about when this started",
            "timeline_info": {
                "problem_started_at": "unknown"
            },
            "clarifying_questions": ["When exactly did you first notice the errors?"],
            "phase_complete": false,
            "confidence": 0.5
        }
        """)

        agent = TimelineAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=2,
            user_query="Not sure when it started",
            phase_state={"problem_statement": "Something broken"},
            recent_context=[],
            case_id="test-2",
            urgency_level="medium"
        )

        response = await agent.process(context)

        # Agent determines phase_complete based on _is_timeline_complete() logic
        # "unknown" start time + no changes = incomplete
        assert response.phase_complete == False
        assert response.recommended_next_phase == 2


class TestTimelineAgentPhaseAdvancement:
    """Tests for phase advancement logic."""

    def test_should_advance_with_start_time(self):
        """Test advancement when start time identified."""
        agent = TimelineAgent(llm_client=None)

        context = PhaseContext(
            phase=2,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        response = PhaseAgentResponse(
            answer="Timeline established",
            state_updates={
                "timeline_info": {
                    "problem_started_at": "2 hours ago",
                    "recent_changes": []
                }
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=True,
            confidence=0.85,
            recommended_next_phase=3
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == True

    def test_should_advance_with_changes_identified(self):
        """Test advancement when changes identified even without exact start time."""
        agent = TimelineAgent(llm_client=None)

        context = PhaseContext(
            phase=2,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        response = PhaseAgentResponse(
            answer="Found deployment",
            state_updates={
                "timeline_info": {
                    "problem_started_at": "unknown",
                    "recent_changes": [{"type": "deployment", "description": "v2.0"}]
                }
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=True,
            confidence=0.80,
            recommended_next_phase=3
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == True

    def test_should_not_advance_no_timeline_data(self):
        """Test no advancement when timeline missing."""
        agent = TimelineAgent(llm_client=None)

        context = PhaseContext(
            phase=2,
            user_query="Test",
            phase_state={},
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        response = PhaseAgentResponse(
            answer="Need more info",
            state_updates={
                "timeline_info": {
                    "problem_started_at": "unknown",
                    "recent_changes": []
                }
            },
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.4,
            recommended_next_phase=2
        )

        should_advance = agent.should_advance_phase(context, response)
        assert should_advance == False


class TestTimelineAgentPromptBuilding:
    """Tests for prompt construction."""

    def test_build_prompt_includes_blast_radius(self):
        """Test that prompt includes blast radius context."""
        agent = TimelineAgent(llm_client=None)

        context = PhaseContext(
            phase=2,
            user_query="When did this start?",
            phase_state={
                "problem_statement": "API errors",
                "blast_radius": "affected_services=api, severity=high"
            },
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        prompt = agent.build_prompt(context)

        assert "API errors" in prompt
        assert "api" in prompt.lower()

    def test_build_prompt_size_approximately_550_tokens(self):
        """Test that prompt is approximately 550 tokens."""
        agent = TimelineAgent(llm_client=None)

        context = PhaseContext(
            phase=2,
            user_query="What changed?",
            phase_state={
                "problem_statement": "Database slow",
                "blast_radius": "database affected"
            },
            recent_context=["Previous context"],
            case_id="test-1",
            urgency_level="medium"
        )

        prompt = agent.build_prompt(context)

        estimated_tokens = len(prompt) / 3.7
        assert 350 <= estimated_tokens <= 750


class TestTimelineAgentChangeDetection:
    """Tests for change detection."""

    @pytest.mark.asyncio
    async def test_detect_deployment_correlation(self):
        """Test detection of deployment correlation."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "The deployment timing correlates with the issue",
            "timeline_info": {
                "problem_started_at": "14:30",
                "recent_changes": [{
                    "type": "deployment",
                    "description": "API v2.3.1",
                    "timestamp": "14:25",
                    "correlation": "high"
                }]
            },
            "phase_complete": true,
            "confidence": 0.92
        }
        """)

        agent = TimelineAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=2,
            user_query="Started right after deployment",
            phase_state={"problem_statement": "API errors"},
            recent_context=[],
            case_id="test-1",
            urgency_level="high"
        )

        response = await agent.process(context)

        changes = response.state_updates["timeline_info"]["recent_changes"]
        assert len(changes) > 0
        assert changes[0]["correlation"] == "high"


class TestTimelineAgentEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_handle_vague_time_reference(self):
        """Test handling of vague time descriptions."""
        mock_llm = MockLLMClient(mock_response="""
        {
            "answer": "Can you be more specific about when this started?",
            "timeline_info": {
                "problem_started_at": "unknown"
            },
            "clarifying_questions": ["Was this today, yesterday, or last week?"],
            "phase_complete": false,
            "confidence": 0.4
        }
        """)

        agent = TimelineAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=2,
            user_query="It started recently",
            phase_state={"problem_statement": "Issue"},
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        response = await agent.process(context)

        # Vague time ("unknown") should result in incomplete phase
        assert response.phase_complete == False
        assert response.confidence < 0.6

    @pytest.mark.asyncio
    async def test_handle_heuristic_extraction(self):
        """Test heuristic timeline extraction from text."""
        mock_llm = MockLLMClient(mock_response="""
        Based on what you've said, the issue started about 2 hours ago,
        which was shortly after you deployed the new version.
        This is a gradual degradation pattern.
        """)

        agent = TimelineAgent(llm_client=mock_llm)

        context = PhaseContext(
            phase=2,
            user_query="Started 2 hours ago after deployment",
            phase_state={"problem_statement": "Slowness"},
            recent_context=[],
            case_id="test-1",
            urgency_level="medium"
        )

        response = await agent.process(context)

        timeline = response.state_updates["timeline_info"]
        # Heuristic should extract "2 hours ago" or deployment mention
        assert "problem_started_at" in timeline or "recent_changes" in timeline

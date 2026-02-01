import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationStage,
    ProblemVerification,
)


class MockLLMProvider(ILLMProvider):
    async def generate(self, prompt, **kwargs):
        # Default mock response - should be overridden in tests if needed
        return "{}"

    async def generate_stream(self, prompt, **kwargs):
        yield "mock"

    async def generate_with_history(self, messages, **kwargs):
        return "{}"


@pytest.fixture
def mock_llm():
    llm = MockLLMProvider()
    llm.generate = AsyncMock()
    return llm


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock()
    return repo


@pytest.fixture
def base_case():
    return Case(
        case_id="case_1234567890ab",
        title="Test Case",
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Test description",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )


class TestMilestoneEngine:

    @pytest.mark.asyncio
    async def test_process_turn_investigating(self, mock_llm, mock_repo, base_case):
        """Test processing a turn in INVESTIGATING status"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response with structured output
        mock_response_content = json.dumps(
            {
                "agent_response": "I have verified the symptom.",
                "state_updates": {
                    "milestones": {"symptom_verified": True},
                    "evidence_to_add": [],
                    "hypotheses_to_add": [],
                    "outcome": "milestone_completed",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(base_case, "Verify the symptom")

        # Verify LLM called
        mock_llm.generate.assert_called_once()

        # Verify metadata
        metadata = result["metadata"]
        assert "symptom_verified" in metadata["milestones_completed"]
        assert metadata["progress_made"] is True

        # Verify case updated
        updated_case = result["case_updated"]
        assert updated_case.progress.symptom_verified is True

    @pytest.mark.asyncio
    async def test_process_turn_inquiry(self, mock_llm, mock_repo):
        """Test processing a turn in INQUIRY status"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        case = Case(
            case_id="case_1234567890ab",
            title="Inquiry",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
        )

        # Mock LLM response
        mock_response_content = json.dumps(
            {
                "agent_response": "How can I help?",
                "state_updates": {
                    "proposed_problem_statement": "Proposed issue",
                    "quick_suggestions": ["Reboot"],
                    "outcome": "conversation",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(case, "Help me")

        # Verify
        updated_case = result["case_updated"]
        assert updated_case.inquiry.proposed_problem_statement == "Proposed issue"

    @pytest.mark.asyncio
    async def test_no_progress_detection(self, mock_llm, mock_repo, base_case):
        """Test no-op detection when no milestones/evidence added"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response with NO state updates
        mock_response_content = json.dumps(
            {
                "agent_response": "Just chatting.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(base_case, "Chat")

        metadata = result["metadata"]
        assert metadata["progress_made"] is False
        assert metadata["milestones_completed"] == []

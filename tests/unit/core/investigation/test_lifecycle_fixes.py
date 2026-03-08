import json
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone
import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    ProblemVerification,
    Evidence,
    EvidenceCategory,
    InquiryData,
    EvidenceSourceType,
    EvidenceForm,
)

# ... imports ...
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputStrategy,
    StructuredOutputCapability,
    StructuredOutputMode,
)


class MockLLMProvider(ILLMProvider):
    async def generate(self, prompt, **kwargs):
        return "{}"

    async def generate_stream(self, prompt, **kwargs):
        yield "mock"

    async def generate_with_history(self, messages, **kwargs):
        return "{}"

    def get_structured_output_strategy(self, schema):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "Test", "schema": schema},
            },
        )


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


@pytest.mark.asyncio
async def test_path_selection_triggered_after_verification(
    mock_llm, mock_repo, base_case
):
    """Test Bug #3: Path selection triggers only after symptom verification"""
    engine = MilestoneEngine(
        mock_llm,
        mock_repo,
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )

    # Setup: Case has None path_selection
    base_case.path_selection = None
    base_case.progress.symptom_verified = False

    # Add existing evidence for validation
    base_case.evidence.append(
        Evidence(
            evidence_id="ev_001122334455",
            summary="s",
            content_ref="r",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(),
            collected_by="u",
            collected_at_turn=0,
            form=EvidenceForm.USER_TEXT,
            primary_purpose="Test validation",
            preprocessed_content="Raw stuff",
            content_size_bytes=100,
            preprocessing_method="manual",
        )
    )

    # Mock response: Complete symptom_verified
    mock_response = json.dumps(
        {
            "agent_response": "Verified.",
            "internal_reasoning": {
                "evidence_analyzed": ["ev_001122334455"],
                "conclusions": [],
                "milestone_justifications": {"symptom_verified": "done"},
            },
            "state_updates": {
                "milestones": {"symptom_verified": True},
                "outcome": "milestone_completed",
            },
        }
    )
    mock_llm.generate.return_value = mock_response

    result = await engine.process_turn(base_case, "Verify symptom")
    case = result["case_updated"]

    # Verify path selection triggered
    assert case.path_selection is not None
    assert (
        case.path_selection.path == "mitigation_first"
    )  # ongoing + high -> mitigation


@pytest.mark.asyncio
async def test_evidence_linking_to_milestones(mock_llm, mock_repo, base_case):
    """Test Bug #4: Evidence added in same turn as milestone is linked"""
    engine = MilestoneEngine(
        mock_llm,
        mock_repo,
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )

    # Add existing evidence for validation
    base_case.evidence.append(
        Evidence(
            evidence_id="ev_001122334455",
            summary="s",
            content_ref="r",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(),
            collected_by="u",
            collected_at_turn=0,
            form=EvidenceForm.USER_TEXT,
            primary_purpose="Test validation",
            preprocessed_content="Raw stuff",
            content_size_bytes=100,
            preprocessing_method="manual",
        )
    )

    # Mock response: Add evidence AND complete milestone
    mock_response = json.dumps(
        {
            "agent_response": "Found logs.",
            "internal_reasoning": {
                "evidence_analyzed": ["ev_001122334455"],
                "conclusions": [],
                "milestone_justifications": {"symptom_verified": "See evidence"},
            },
            "state_updates": {
                "milestones": {"symptom_verified": True},
                "evidence_to_add": [
                    {
                        "summary": "Log file",
                        "content_ref": "log.txt",
                        "category": "symptom_evidence",
                        "source_type": "logs",
                        "likelihood": 0.9,
                    }
                ],
                "outcome": "milestone_completed",
            },
        }
    )
    mock_llm.generate.return_value = mock_response

    result = await engine.process_turn(base_case, "Check logs")
    case = result["case_updated"]

    # Evidence count: 1 initial + 1 from LLM response = 2
    # Note: User messages NO LONGER create automatic UNCLASSIFIED evidence
    assert len(case.evidence) == 2

    # The LLM response evidence is the second one (last appended)
    ev = case.evidence[1]

    # Verify it was linked to the milestone
    assert "symptom_verified" in ev.advances_milestones


@pytest.mark.asyncio
async def test_turn_outcome_logic(mock_llm, mock_repo, base_case):
    """Test Bug #8: Robust turn outcome logic"""
    engine = MilestoneEngine(
        mock_llm,
        mock_repo,
        investigation_tools=MagicMock(),
        evidence_service=MagicMock(),
    )

    # Add existing evidence for validation
    base_case.evidence.append(
        Evidence(
            evidence_id="ev_001122334455",
            summary="s",
            content_ref="r",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            collected_at=datetime.now(),
            collected_by="u",
            collected_at_turn=0,
            form=EvidenceForm.USER_TEXT,
            primary_purpose="Test validation",
            preprocessed_content="Raw stuff",
            content_size_bytes=100,
            preprocessing_method="manual",
        )
    )

    # Scenarios to test:

    # 1. Milestone Completed
    mock_llm.generate.return_value = json.dumps(
        {
            "agent_response": "Done",
            "internal_reasoning": {
                "evidence_analyzed": ["ev_001122334455"],
                "conclusions": [],
                "milestone_justifications": {"symptom_verified": "ok"},
            },
            "state_updates": {
                "milestones": {"symptom_verified": True},
                "outcome": "conversation",
            },  # outcome overridden
        }
    )
    result = await engine.process_turn(base_case, "msg")
    assert result["metadata"]["outcome"] == "milestone_completed"

    # 2. Hypothesis Validated
    # (Requires setting up hypothesis first, but we can iterate)
    # Just check metadata outcome logic from method by mocking metadata
    # ... actually integration test is cleaner:

    # 3. Data Provided (Evidence Added)
    mock_llm.generate.return_value = json.dumps(
        {
            "agent_response": "Data",
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": "ev",
                        "content_ref": "ref",
                        "category": "symptom_evidence",
                        "source_type": "logs",
                    }
                ],
                "outcome": "conversation",
            },
        }
    )
    result = await engine.process_turn(base_case, "msg")
    assert result["metadata"]["outcome"] == "data_provided"

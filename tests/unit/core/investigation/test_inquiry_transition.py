"""Tests for INQUIRY → INVESTIGATING transition logic (Option 4: Two-Stage Smart Heuristic)

This test suite validates the revised transition logic that prevents premature transitions
while enabling auto-transitions for urgent production issues.

Test Coverage:
1. Critical outage → Auto-transition (CRITICAL + ongoing)
2. Vague query → No transition (no problem detected)
3. Informational query → No transition (no problem)
4. Post-mortem → No transition (not ongoing)
5. Medium urgency → No transition (not CRITICAL/HIGH)
6. Multi-turn escalation → Transition on Turn 2 when urgency emerges
7. Original bug scenario → No transition (development context)
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    InquiryData,
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
                "json_schema": {
                    "name": "TestSchema",
                    "strict": True,
                    "schema": schema,
                },
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
def inquiry_case():
    """Base case in INQUIRY status"""
    return Case(
        case_id="case_1234567890ab",
        title="Test Inquiry",
        status=CaseStatus.INQUIRY,
        user_id="user_123",
        organization_id="org_123",
        description="",
        inquiry=InquiryData(thread_id="thread_123"),
    )


class TestInquiryTransitionLogic:
    """Test suite for INQUIRY → INVESTIGATING transition logic"""

    @pytest.mark.asyncio
    async def test_critical_outage_auto_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 1: Critical outage → Auto-transition (CRITICAL + ongoing)"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response for critical production outage
        mock_response = json.dumps(
            {
                "agent_response": "I understand - production API is completely down. Let me start investigating immediately.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "critical",
                        "preliminary_guidance": "Production API unavailable - all requests failing with 500 errors",
                    },
                    "preliminary_urgency": {
                        "level": "CRITICAL",
                        "is_ongoing": True,
                        "impact_assessment": "All users blocked from accessing production",
                        "mitigation_hint": "Consider rollback or service restart",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case,
            "Production API is completely down. All requests returning 500 errors. Users can't log in.",
        )

        # Verify auto-transition to INVESTIGATING
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.inquiry.decided_to_investigate is True
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Production API unavailable - all requests failing with 500 errors"
        )
        assert result["metadata"]["status_transitioned"] is True

    @pytest.mark.asyncio
    async def test_vague_query_no_transition(self, mock_llm, mock_repo, inquiry_case):
        """Scenario 2: Vague query → No transition (no problem detected)"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response for vague query
        mock_response = json.dumps(
            {
                "agent_response": "I'd be happy to help! Can you tell me more about what you're experiencing?",
                "state_updates": {},
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case, "Hey, I have a question about our API performance."
        )

        # Verify stays in INQUIRY
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False
        assert result["metadata"].get("status_transitioned", False) is False

    @pytest.mark.asyncio
    async def test_informational_query_no_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 3: Informational query → No transition (no problem)"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response for informational query
        mock_response = json.dumps(
            {
                "agent_response": "To upload evidence, you can use the /api/v1/cases/{case_id}/evidence endpoint...",
                "state_updates": {},
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case, "How do I upload evidence to a case?"
        )

        # Verify stays in INQUIRY
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_postmortem_no_auto_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 4: Post-mortem → No auto-transition (not ongoing)"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response for historical post-mortem
        mock_response = json.dumps(
            {
                "agent_response": "I can help you understand what happened. Let's review the timeline and evidence.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "high",
                        "preliminary_guidance": "Historical outage occurred last Tuesday requiring root cause analysis",
                    },
                    "preliminary_urgency": {
                        "level": "LOW",
                        "is_ongoing": False,
                        "impact_assessment": "Historical issue, not currently affecting users",
                    },
                    "proposed_problem_statement": "Historical outage occurred last Tuesday requiring root cause analysis",
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case,
            "We had an outage last Tuesday. Want to understand what happened.",
        )

        # Verify stays in INQUIRY (not auto-confirmed because not ongoing)
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INQUIRY
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Historical outage occurred last Tuesday requiring root cause analysis"
        )
        # Should NOT auto-confirm because is_ongoing=False
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_medium_urgency_no_auto_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 5: Medium urgency → No auto-transition (not CRITICAL/HIGH)"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response for medium urgency issue
        mock_response = json.dumps(
            {
                "agent_response": "I understand the checkout is experiencing intermittent slowness. Let me help diagnose this.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "slowness",
                        "severity_guess": "medium",
                        "preliminary_guidance": None,  # LLM may not always provide this
                    },
                    "preliminary_urgency": {
                        "level": "MEDIUM",
                        "is_ongoing": True,
                        "impact_assessment": "Intermittent slowness affecting some users",
                    },
                    "proposed_problem_statement": "Checkout performance degradation observed intermittently",
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case, "Our checkout is slow sometimes."
        )

        # Verify stays in INQUIRY (MEDIUM urgency doesn't trigger auto-confirm)
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INQUIRY
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Checkout performance degradation observed intermittently"
        )
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_multiturn_urgency_escalation(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 6: Multi-turn escalation → Transition on Turn 2 when urgency emerges"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Turn 1: Vague initial query
        mock_response_turn1 = json.dumps(
            {
                "agent_response": "Can you provide more details about the API behavior?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "other",
                        "severity_guess": "unknown",
                        "preliminary_guidance": None,
                    },
                    "proposed_problem_statement": "API behavior anomaly - details unclear",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1

        result1 = await engine.process_turn(inquiry_case, "Our API is acting weird.")

        # Verify Turn 1: stays in INQUIRY
        case_after_turn1 = result1["case_updated"]
        assert case_after_turn1.status == CaseStatus.INQUIRY
        assert (
            case_after_turn1.inquiry.proposed_problem_statement
            == "API behavior anomaly - details unclear"
        )
        assert case_after_turn1.inquiry.problem_statement_confirmed is False

        # Turn 2: User clarifies it's a critical production issue
        mock_response_turn2 = json.dumps(
            {
                "agent_response": "Understood - this is critical. All users getting 403 errors. Let me investigate immediately.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "critical",
                        "preliminary_guidance": "All users receiving 403 forbidden errors in production",
                    },
                    "preliminary_urgency": {
                        "level": "CRITICAL",
                        "is_ongoing": True,
                        "impact_assessment": "All users blocked from accessing production API",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn2

        result2 = await engine.process_turn(
            case_after_turn1,
            "Actually, all users are getting 403 errors right now. This is production!",
        )

        # Verify Turn 2: auto-transitions to INVESTIGATING
        case_after_turn2 = result2["case_updated"]
        assert case_after_turn2.status == CaseStatus.INVESTIGATING
        assert case_after_turn2.inquiry.problem_statement_confirmed is True
        assert case_after_turn2.inquiry.decided_to_investigate is True
        # Keeps the problem statement from Turn 1 (Stage 1 only runs if no proposed_problem_statement)
        assert (
            case_after_turn2.inquiry.proposed_problem_statement
            == "API behavior anomaly - details unclear"
        )

    @pytest.mark.asyncio
    async def test_original_bug_scenario_no_premature_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 7: Original bug → No premature transition (development context)"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response matching the original bug report
        mock_response = json.dumps(
            {
                "agent_response": "I can help you debug this. Can you share the error logs or stack trace?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "medium",
                        "preliminary_guidance": "Development environment errors suspected in agent workflow",
                    },
                    "preliminary_urgency": {
                        "level": "LOW",
                        "is_ongoing": False,
                        "impact_assessment": "Development/debugging context, not production impact",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case,
            "I started seeing errors. I was debugging locally. I suspect the issue is in agent workflow.",
        )

        # Verify NO premature transition (this is the original bug fix)
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INQUIRY
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Development environment errors suspected in agent workflow"
        )
        # Should NOT auto-confirm because is_ongoing=False and level=LOW
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False
        assert result["metadata"].get("status_transitioned", False) is False

    @pytest.mark.asyncio
    async def test_high_urgency_ongoing_auto_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Test HIGH urgency + ongoing also triggers auto-transition"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response for HIGH urgency ongoing issue
        mock_response = json.dumps(
            {
                "agent_response": "I understand - payment processing is failing. Let me investigate this urgently.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "high",
                        "preliminary_guidance": "Payment processing failures affecting customers",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "impact_assessment": "Customer payments failing, revenue impact",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case,
            "Our payment processing is failing. Customers can't complete purchases.",
        )

        # Verify auto-transition (HIGH + ongoing should work)
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.inquiry.decided_to_investigate is True

    @pytest.mark.asyncio
    async def test_fallback_to_proposed_problem_statement(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Test Stage 1 fallback when preliminary_guidance is None"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response with proposed_problem_statement but no preliminary_guidance
        mock_response = json.dumps(
            {
                "agent_response": "Let me help investigate this latency issue.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "slowness",
                        "severity_guess": "high",
                        "preliminary_guidance": None,  # Explicitly None
                    },
                    "proposed_problem_statement": "API latency spike to 8 seconds affecting dashboards",
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "impact_assessment": "Users experiencing slow dashboard loads",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case,
            "API latency spiked from 200ms to 8 seconds. Customers complaining.",
        )

        # Verify uses proposed_problem_statement (fallback works)
        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "API latency spike to 8 seconds affecting dashboards"
        )
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.inquiry.decided_to_investigate is True

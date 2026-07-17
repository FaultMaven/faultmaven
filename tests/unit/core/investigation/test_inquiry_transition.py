"""Tests for INQUIRY → INVESTIGATING transition logic (Two-Step Confirmation)

This test suite validates the two-step confirmation flow where ALL transitions from
INQUIRY → INVESTIGATING require explicit user confirmation (Design Doc Section 1.2).

Test Coverage:
1. Critical outage → Stays INQUIRY on Turn 1 (waits for user confirmation)
2. Vague query → No transition (no problem detected)
3. Informational query → No transition (no problem)
4. Post-mortem → No transition (not ongoing)
5. Medium urgency → No transition (not CRITICAL/HIGH)
6. Multi-turn escalation → Stays INQUIRY until user confirms
7. Original bug scenario → No transition (development context)
8. HIGH + ongoing → Stays INQUIRY (waits for confirmation)
9. Proposed problem statement fallback → Stays INQUIRY (waits for confirmation)
10. User confirms → Transition to INVESTIGATING
11. Multi-turn confirmation flow → Turn 1 present, Turn 2 confirm, transition fires
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
from faultmaven.modules.case.contracts import Case, CaseState, InquiryData


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
    """Base case in INQUIRY state"""
    return Case(
        case_id="case_1234567890ab",
        title="Test Inquiry",
        state=CaseState.INQUIRY,
        user_id="user_123",
        organization_id="org_123",
        description="",
        inquiry=InquiryData(thread_id="thread_123"),
    )


class TestInquiryTransitionLogic:
    """Test suite for INQUIRY → INVESTIGATING transition logic"""

    @pytest.mark.asyncio
    async def test_critical_outage_stays_inquiry_until_confirmed(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 1: Critical outage → Stays INQUIRY (waits for user confirmation)

        Even for CRITICAL + ongoing issues, the design requires two-step confirmation:
        Turn N: Agent presents problem statement + asks for confirmation
        Turn N+1: User confirms → transition fires
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Mock LLM response for critical production outage
        mock_response = json.dumps(
            {
                "agent_response": "I understand - production API is completely down. Let me confirm: all requests returning 500 errors. Is this accurate?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "critical",
                        "preliminary_guidance": "Production API unavailable - all requests failing with 500 errors",
                    },
                    "preliminary_urgency": {
                        "level": "CRITICAL",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "All users blocked from accessing production",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response

        result = await engine.process_turn(
            inquiry_case,
            "Production API is completely down. All requests returning 500 errors. Users can't log in.",
        )

        # Verify stays in INQUIRY — agent should ask for confirmation in response
        updated_case = result["case_updated"]
        assert updated_case.state == CaseState.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Production API unavailable - all requests failing with 500 errors"
        )
        assert result["metadata"].get("status_transitioned", False) is False

    @pytest.mark.asyncio
    async def test_vague_query_no_transition(self, mock_llm, mock_repo, inquiry_case):
        """Scenario 2: Vague query → No transition (no problem detected)"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

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
        assert updated_case.state == CaseState.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False
        assert result["metadata"].get("status_transitioned", False) is False

    @pytest.mark.asyncio
    async def test_informational_query_no_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 3: Informational query → No transition (no problem)"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

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
        assert updated_case.state == CaseState.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_postmortem_no_auto_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 4: Post-mortem → No auto-transition (not ongoing)"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

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
        assert updated_case.state == CaseState.INQUIRY
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
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

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
        assert updated_case.state == CaseState.INQUIRY
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Checkout performance degradation observed intermittently"
        )
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_multiturn_urgency_escalation_stays_inquiry(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 6: Multi-turn escalation → Stays INQUIRY until user explicitly confirms"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

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
        assert case_after_turn1.state == CaseState.INQUIRY
        assert (
            case_after_turn1.inquiry.proposed_problem_statement
            == "API behavior anomaly - details unclear"
        )
        assert case_after_turn1.inquiry.problem_statement_confirmed is False

        # Turn 2: User clarifies it's a critical production issue
        # Agent should present updated problem statement and ask for confirmation
        mock_response_turn2 = json.dumps(
            {
                "agent_response": "This sounds critical. Let me confirm: all users are receiving 403 errors in production. Is this accurate? Should we investigate?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "critical",
                        "preliminary_guidance": "All users receiving 403 forbidden errors in production",
                    },
                    "preliminary_urgency": {
                        "level": "CRITICAL",
                        "is_ongoing": True,
                        "is_incident_report": True,
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

        # Verify Turn 2: STILL in INQUIRY — user hasn't confirmed yet
        case_after_turn2 = result2["case_updated"]
        assert case_after_turn2.state == CaseState.INQUIRY
        assert case_after_turn2.inquiry.problem_statement_confirmed is False
        assert case_after_turn2.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_original_bug_scenario_no_premature_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Scenario 7: Original bug → No premature transition (development context)"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

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
        assert updated_case.state == CaseState.INQUIRY
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "Development environment errors suspected in agent workflow"
        )
        # Should NOT auto-confirm because is_ongoing=False and level=LOW
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False
        assert result["metadata"].get("status_transitioned", False) is False

    @pytest.mark.asyncio
    async def test_high_urgency_ongoing_stays_inquiry(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Test HIGH urgency + ongoing stays in INQUIRY (waits for user confirmation)"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Mock LLM response for HIGH urgency ongoing issue
        mock_response = json.dumps(
            {
                "agent_response": "I understand - payment processing is failing. Let me confirm: customers can't complete purchases due to payment failures. Is this accurate?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "high",
                        "preliminary_guidance": "Payment processing failures affecting customers",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
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

        # Verify stays in INQUIRY (user hasn't confirmed yet)
        updated_case = result["case_updated"]
        assert updated_case.state == CaseState.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_fallback_to_proposed_problem_statement(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Test Stage 1 fallback when preliminary_guidance is None — stays INQUIRY"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Mock LLM response with proposed_problem_statement but no preliminary_guidance
        mock_response = json.dumps(
            {
                "agent_response": "Let me confirm: API latency has spiked to 8 seconds affecting dashboards. Is this accurate?",
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
                        "is_incident_report": True,
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

        # Verify uses proposed_problem_statement (fallback works) but stays in INQUIRY
        updated_case = result["case_updated"]
        assert updated_case.state == CaseState.INQUIRY
        assert (
            updated_case.inquiry.proposed_problem_statement
            == "API latency spike to 8 seconds affecting dashboards"
        )
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_user_confirmation_triggers_transition(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Post-redesign INV-19: INQUIRY → INVESTIGATING requires Gate 1
        only. Gate 2 (path commit) fires later in INVESTIGATING after
        ``symptom_verified``.

        Turn 1: Agent presents problem statement (stays INQUIRY, Gate 1 open)
        Turn 2: User confirms problem → Gate 1 closes → transition to
                INVESTIGATING. path_selection is None at this point;
                Gate 2 will fire after symptom_verified in subsequent
                turns.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Turn 1: Agent detects incident and presents problem statement
        mock_response_turn1 = json.dumps(
            {
                "agent_response": "Let me confirm: production database is returning connection timeout errors. Is this accurate?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "critical",
                        "preliminary_guidance": "Database connection timeouts causing service failures",
                    },
                    "preliminary_urgency": {
                        "level": "CRITICAL",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "All services failing due to database connection timeouts",
                    },
                    "proposed_problem_statement": "Database connection timeouts affecting all services",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1

        result1 = await engine.process_turn(
            inquiry_case,
            "Production database is timing out. All services are failing.",
        )

        # Turn 1: stays in INQUIRY
        case_after_turn1 = result1["case_updated"]
        assert case_after_turn1.state == CaseState.INQUIRY
        assert case_after_turn1.inquiry.problem_statement_confirmed is False

        # Turn 2: User confirms → Gate 1 closes → case transitions to
        # INVESTIGATING (post-redesign: Gate 2 no longer gates the
        # transition; it fires later in INVESTIGATING after
        # symptom_verified). path_selection stays None.
        mock_response_turn2 = json.dumps(
            {
                "agent_response": "Confirmed. Starting investigation.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn2

        result2 = await engine.process_turn(
            case_after_turn1,
            "Yes, that's correct. Please investigate.",
        )

        # Turn 2: Gate 1 closed → transition to INVESTIGATING.
        # Post-redesign there is no path fork to commit.
        case_after_turn2 = result2["case_updated"]
        assert case_after_turn2.state == CaseState.INVESTIGATING
        assert case_after_turn2.inquiry.problem_statement_confirmed is True
        assert case_after_turn2.inquiry.decided_to_investigate is True
        assert result2["metadata"]["status_transitioned"] is True

    @pytest.mark.asyncio
    async def test_user_declines_investigation_stays_inquiry(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Test: User declines investigation → stays in INQUIRY

        When user says "No" or provides corrections, user_confirmed_investigation
        stays False and case remains in INQUIRY.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Turn 1: Agent detects incident and presents problem statement
        mock_response_turn1 = json.dumps(
            {
                "agent_response": "Let me confirm: API is returning 503 errors. Is this accurate?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "high",
                        "preliminary_guidance": "API 503 errors affecting users",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Users getting 503 errors",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1

        result1 = await engine.process_turn(
            inquiry_case, "Our API is returning 503 errors."
        )
        case_after_turn1 = result1["case_updated"]
        assert case_after_turn1.state == CaseState.INQUIRY

        # Turn 2: User corrects the problem statement
        mock_response_turn2 = json.dumps(
            {
                "agent_response": "I see, it's actually 504 timeout errors, not 503. Let me update: API is returning 504 timeout errors. Is this accurate?",
                "state_updates": {
                    "user_confirmed_investigation": False,
                    "proposed_problem_statement": "API returning 504 timeout errors affecting users",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn2

        result2 = await engine.process_turn(
            case_after_turn1,
            "No, it's actually 504 timeout errors, not 503.",
        )

        # Turn 2: stays in INQUIRY (user corrected, didn't confirm)
        case_after_turn2 = result2["case_updated"]
        assert case_after_turn2.state == CaseState.INQUIRY
        assert case_after_turn2.inquiry.problem_statement_confirmed is False
        assert case_after_turn2.inquiry.decided_to_investigate is False
        assert (
            case_after_turn2.inquiry.proposed_problem_statement
            == "API returning 504 timeout errors affecting users"
        )

    @pytest.mark.asyncio
    async def test_same_turn_confirmation_is_rejected(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Regression: LLM must not collapse the User-Agent Handshake into one turn.

        The design (INQUIRY_TEMPLATE: "Never set user_confirmed_investigation=True
        on the same turn you first present the problem statement") requires the
        user to see the proposed_problem_statement on turn N before confirming
        on turn N+1.

        Before the same-turn-confirmation guard in _apply_inquiry_updates, the
        engine accepted any turn that carried BOTH a new proposed_problem_statement
        AND user_confirmed_investigation=True — collapsing the two-step handshake.
        Observed on first-turn cases with explicit "please investigate" phrasing,
        which prompts the LLM to write the statement and signal confirmation in
        the same response.

        Asserts that on turn 1, even if the LLM sets both fields, the case
        stays in INQUIRY (transition deferred to turn 2).
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Turn 1: LLM tries to set the statement AND confirm in one shot.
        # This is the anti-case the prompt forbids but the LLM may still emit.
        mock_response_turn1 = json.dumps(
            {
                "agent_response": "Starting investigation into API 503 errors.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "high",
                        "preliminary_guidance": "API returning 503 errors",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Users seeing errors",
                    },
                    "proposed_problem_statement": "API returning 503 errors affecting users",
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1

        result1 = await engine.process_turn(
            inquiry_case,
            "My API is returning 503s, please investigate.",
        )

        # The guard must refuse the same-turn confirmation. The statement
        # is captured (so it can be presented to the user) but the
        # transition does NOT fire — case stays in INQUIRY for the user
        # to confirm explicitly on a subsequent turn.
        case_after_turn1 = result1["case_updated"]
        assert case_after_turn1.state == CaseState.INQUIRY, (
            "Same-turn confirmation collapsed the handshake — INQUIRY → INVESTIGATING "
            "fired without giving the user a chance to confirm. This is the "
            "regression the same-turn guard prevents."
        )
        assert case_after_turn1.inquiry.problem_statement_confirmed is False
        assert case_after_turn1.inquiry.decided_to_investigate is False
        # The statement IS persisted — the agent presents it on the
        # next turn and the user confirms then.
        assert (
            case_after_turn1.inquiry.proposed_problem_statement
            == "API returning 503 errors affecting users"
        )

    @pytest.mark.asyncio
    async def test_confirmation_accepted_when_statement_persisted_across_turns(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Confirmation IS accepted when the statement existed on a prior turn.

        Complement to test_same_turn_confirmation_is_rejected: ensures the
        guard is precise. A confirmation must be accepted when the
        proposed_problem_statement was set on a previous turn (i.e., the
        user actually saw it before confirming). This is the normal
        two-turn handshake flow.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Turn 1: agent proposes the statement; user has not yet confirmed.
        mock_response_turn1 = json.dumps(
            {
                "agent_response": "Let me confirm: API returning 503 errors. Is this right?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "high",
                        "preliminary_guidance": "API returning 503 errors",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Users seeing errors",
                    },
                    "proposed_problem_statement": "API returning 503 errors affecting users",
                    "user_confirmed_investigation": False,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1
        result1 = await engine.process_turn(inquiry_case, "API is returning 503s")
        case_after_turn1 = result1["case_updated"]
        assert case_after_turn1.state == CaseState.INQUIRY

        # Turn 2: user confirms; LLM only emits user_confirmed_investigation=True
        # (no new proposed_problem_statement). Statement existed before this
        # turn → INV-01 guard passes → Gate 1 closes → case transitions to
        # INVESTIGATING (post-redesign: Gate 2 no longer gates the transition;
        # it fires later in INVESTIGATING after symptom_verified).
        mock_response_turn2 = json.dumps(
            {
                "agent_response": "Confirmed. Starting investigation.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn2
        result2 = await engine.process_turn(case_after_turn1, "yes")

        case_after_turn2 = result2["case_updated"]
        # INV-01 (Gate 1) passes → transition fires. Post-redesign there is
        # no path fork to commit.
        assert case_after_turn2.state == CaseState.INVESTIGATING
        assert case_after_turn2.inquiry.problem_statement_confirmed is True
        assert case_after_turn2.inquiry.decided_to_investigate is True


class TestContextBuilderConfirmationInjection:
    """Tests for Fix 2: AWAITING_CONFIRMATION replaced with NOT_YET_CONFIRMED.

    When a proposed_problem_statement exists but is not confirmed, the context
    builder should inject NOT_YET_CONFIRMED (not AWAITING_CONFIRMATION) to prevent
    the LLM from re-evaluating confirmation every turn.
    """

    def test_not_yet_confirmed_injected_when_unconfirmed(self):
        """Context builder outputs NOT_YET_CONFIRMED for unconfirmed problem statement."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors",
                problem_statement_confirmed=False,
            ),
        )

        context = build_investigation_context(case, user_message="test message")

        # Check all values in the returned dict for the markers
        context_str = str(context.values())
        assert "NOT_YET_CONFIRMED" in context_str
        assert "AWAITING_CONFIRMATION" not in context_str

    def test_no_injection_when_confirmed(self):
        """No NOT_YET_CONFIRMED injection when problem statement is confirmed."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors",
                problem_statement_confirmed=True,
            ),
        )

        context = build_investigation_context(case, user_message="test message")

        context_str = str(context.values())
        assert "NOT_YET_CONFIRMED" not in context_str
        assert "AWAITING_CONFIRMATION" not in context_str


class TestHandshakeDeferredRecovery:
    """Pinning tests for the recovery path after the same-turn-confirmation guard fires.

    The guard at _apply_inquiry_updates rejects the same-turn collapse and sets
    case.inquiry.handshake_deferred_at_turn. On the NEXT turn, two things must
    happen so the user has a deterministic recovery path:

    1. context_builder switches the inquiry_state block from NOT_YET_CONFIRMED
       ("don't re-propose") to HANDSHAKE_DEFERRED (re-present + ask).
    2. The engine emits confirmation suggestions deterministically, even if the
       LLM disobeys the prompt and fails to surface its own confirmation pair.
    """

    def test_handshake_deferred_block_injected_on_recovery_turn(self):
        """Context builder injects HANDSHAKE_DEFERRED on turn following guard fire."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        # Guard fired on turn 3; we're now processing turn 4.
        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
            current_turn=4,
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors affecting users",
                problem_statement_confirmed=False,
                handshake_deferred_at_turn=3,
            ),
        )

        context = build_investigation_context(case, user_message="anything")

        context_str = str(context.values())
        assert "HANDSHAKE_DEFERRED" in context_str, (
            "Recovery-turn prompt did not include HANDSHAKE_DEFERRED — LLM has "
            "no signal to re-present the statement."
        )
        assert "NOT_YET_CONFIRMED" not in context_str, (
            "Recovery-turn prompt still includes NOT_YET_CONFIRMED — the two "
            "blocks are mutually exclusive and the guard's deferral assumption "
            "depends on the LLM being told to re-present, not to stay quiet."
        )

    def test_not_yet_confirmed_block_used_when_flag_is_stale(self):
        """Stale flag (older than one turn) falls back to NOT_YET_CONFIRMED."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        # Guard fired on turn 3; recovery turn 4 has come and gone; we're on turn 5.
        # The flag is stale and the default NOT_YET_CONFIRMED behavior resumes.
        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
            current_turn=5,
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors affecting users",
                problem_statement_confirmed=False,
                handshake_deferred_at_turn=3,
            ),
        )

        context = build_investigation_context(case, user_message="anything")

        context_str = str(context.values())
        assert "NOT_YET_CONFIRMED" in context_str
        assert "HANDSHAKE_DEFERRED" not in context_str

    @pytest.mark.asyncio
    async def test_deterministic_confirmation_suggestions_on_recovery_turn(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Engine emits confirmation suggestions on the recovery turn even when
        the LLM ignores HANDSHAKE_DEFERRED and emits no suggestions of its own.

        This is the Code-guarded backstop — the user must have a clickable path
        regardless of LLM compliance with the re-present instruction.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # The engine assumes current_turn was incremented by investigation_service
        # before each process_turn call (see milestone_engine.py:1574). Simulate that.
        inquiry_case.current_turn = 1

        # Turn 1: LLM one-shots — guard will fire.
        mock_response_turn1 = json.dumps(
            {
                "agent_response": "Starting investigation into API 503 errors.",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "high",
                        "preliminary_guidance": "API returning 503 errors",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Users seeing errors",
                    },
                    "proposed_problem_statement": "API returning 503 errors affecting users",
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1
        result1 = await engine.process_turn(
            inquiry_case,
            "My API is returning 503s, please investigate.",
        )
        case_after_turn1 = result1["case_updated"]

        # Guard fired: flag set to this turn, no transition.
        assert case_after_turn1.state == CaseState.INQUIRY
        assert case_after_turn1.inquiry.handshake_deferred_at_turn == 1

        # Simulate investigation_service incrementing current_turn for turn 2.
        case_after_turn1.current_turn = 2

        # Turn 2 (recovery): LLM disobeys HANDSHAKE_DEFERRED and emits no
        # follow-up suggestions. Engine must still emit them deterministically.
        mock_response_turn2 = json.dumps(
            {
                "agent_response": "Looking at this more...",
                "state_updates": {
                    "user_confirmed_investigation": False,
                },
                "suggested_follow_ups": [],
            }
        )
        mock_llm.generate.return_value = mock_response_turn2
        result2 = await engine.process_turn(case_after_turn1, "ok")

        follow_ups = result2["suggested_follow_ups"]
        confirmation_labels = {f.get("label") for f in follow_ups}
        assert any(
            "investigate" in (lbl or "").lower() for lbl in confirmation_labels
        ), (
            f"Recovery turn did not emit a confirmation suggestion. "
            f"follow_ups={follow_ups}"
        )
        # The positive suggestion carries the deterministic confirmation intent,
        # so clicking it hits the engine's CONFIRMATION intent path and transitions
        # without further LLM involvement.
        positive = next(
            (
                f
                for f in follow_ups
                if f.get("intent", {}).get("confirmation_value") is True
            ),
            None,
        )
        assert positive is not None, (
            "Recovery turn emitted suggestions but none carried "
            "intent.confirmation_value=True — clicking provides no deterministic "
            "transition path."
        )


class TestEngineOwnedGate1OnFirstDetect:
    """Pins the architectural completion of INV-01: Gate 1 fires on *every*
    Gate-1-pending turn, not only on the handshake-deferred recovery turn.

    This is the fix shape for the Run 4 (2026-05-19) regression where the LLM
    silently dropped the optional ``intent`` field, leaving the persona with
    no clickable confirmation path. Under the old code the engine emitted
    deterministic confirmation suggestions only after the same-turn-confirmation
    guard fired; with the engine_owned_affordances consolidator, Gate 1 is
    code-guarded on every turn that has a proposed_problem_statement awaiting
    confirmation — same pattern as Gate 2 and Gate 3.
    """

    @pytest.mark.asyncio
    async def test_first_detect_turn_emits_deterministic_confirmation_pair(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """LLM proposes a problem statement on turn 1 and emits arbitrary
        (intent-less) suggestions; engine must REPLACE them with the canonical
        confirmation pair carrying intent metadata. On a gate-pending turn the
        engine owns the suggestion list — the LLM's own (often confirm-shaped)
        suggestions must not pass through, or they render as duplicate buttons
        beside the engine's authoritative pair (case_d22ebbd63784).
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )
        inquiry_case.current_turn = 1

        # LLM emits a problem statement (Gate 1 pending) plus some intent-less
        # suggestions of its own. This is exactly the Run 4 shape.
        llm_response = json.dumps(
            {
                "agent_response": (
                    "I want to make sure I understand: API returning 500s. "
                    "Is that accurate?"
                ),
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "unavailability",
                        "severity_guess": "high",
                        "preliminary_guidance": "API down",
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "users seeing errors",
                    },
                    "proposed_problem_statement": (
                        "API returning 500 errors affecting users"
                    ),
                    "user_confirmed_investigation": False,
                },
                "suggested_follow_ups": [
                    {
                        "label": "LLM Suggestion A",
                        "action_type": "DECIDE",
                        "payload": "LLM payload A",
                    },
                    {
                        "label": "LLM Suggestion B",
                        "action_type": "DECIDE",
                        "payload": "LLM payload B",
                    },
                ],
            }
        )
        mock_llm.generate.return_value = llm_response

        result = await engine.process_turn(
            inquiry_case,
            "Production API is returning 500s",
        )

        follow_ups = result["suggested_follow_ups"]

        # Engine-owned: the LLM's suggestions must NOT have passed through —
        # the gate-pending turn's suggestion list is owned by the engine.
        llm_labels = {f.get("label") for f in follow_ups}
        assert "LLM Suggestion A" not in llm_labels
        assert "LLM Suggestion B" not in llm_labels

        # Engine must have substituted the canonical confirmation pair with
        # intent metadata that hits the deterministic CONFIRMATION routing.
        positive = next(
            (
                f
                for f in follow_ups
                if (f.get("intent") or {}).get("type") == "confirmation"
                and (f.get("intent") or {}).get("confirmation_value") is True
            ),
            None,
        )
        assert positive is not None, (
            f"Gate-1-pending first-detect turn did not produce an "
            f"engine-owned confirmation suggestion. follow_ups={follow_ups}"
        )


class TestInquiryConfirmationSchemaContract:
    """Problem-statement confirmation (INQUIRY→INVESTIGATING) is direction-setting,
    so per the confirmation model it requires EXPLICIT confirmation. Pin that the
    ``user_confirmed_investigation`` schema field instructs explicit-only and does
    NOT permit implicit confirmation (diagnostic questions / engagement / urgency).

    The implicit clause was a stranded remnant of a reverted design (05a4347a):
    the engine (no keyword fallback, auto-confirm disabled even for urgency), the
    INQUIRY prompt prose, and the transition tests all require explicit — the
    schema field was the lone outlier. This test keeps the field aligned so the
    contradiction cannot silently return.
    """

    def _field_description(self) -> str:
        from faultmaven.core.investigation.schemas import InquiryResponse

        field = InquiryResponse.InquiryStateUpdate.model_fields[
            "user_confirmed_investigation"
        ]
        return field.description or ""

    def test_requires_explicit_directive(self):
        desc = self._field_description()
        assert "EXPLICIT" in desc, (
            "user_confirmed_investigation must require an explicit directive — "
            "confirming the problem statement sets the investigation's direction."
        )

    def test_does_not_permit_implicit_engagement_as_confirmation(self):
        desc = self._field_description().lower()
        # The reverted implicit clause must not return: diagnostic questions or
        # urgency framed as a confirmation trigger.
        assert "implicit" not in desc, (
            "The 'Implicit (…): user asks diagnostic questions or expresses "
            "urgency' clause contradicts the engine/prose/tests and must not "
            "reappear — engagement is not confirmation of a direction-setting step."
        )
        assert "engagement is not confirmation" in desc

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
9. LLM-proposed statement is the one used → Stays INQUIRY (waits for confirmation)
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
        enterprise_id="org_123",
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
                    },
                    "preliminary_urgency": {
                        "level": "CRITICAL",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "All users blocked from accessing production",
                    },
                    "proposed_problem_statement": "Production API unavailable - all requests failing with 500 errors",
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
                    },
                    "preliminary_urgency": {
                        "level": "LOW",
                        "is_ongoing": False,
                        "impact_assessment": "Development/debugging context, not production impact",
                    },
                    "proposed_problem_statement": "Development environment errors suspected in agent workflow",
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
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Customer payments failing, revenue impact",
                    },
                    "proposed_problem_statement": (
                        "Payment processing is failing, blocking customer "
                        "purchases (ongoing)"
                    ),
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
        # The case must be AWAITING confirmation of a presented statement —
        # without this the assertions below pass for the trivial reason that
        # nothing was ever proposed, and the scenario goes unexercised.
        assert updated_case.inquiry.proposed_problem_statement is not None
        assert updated_case.inquiry.problem_statement_confirmed is False
        assert updated_case.inquiry.decided_to_investigate is False

    @pytest.mark.asyncio
    async def test_llm_proposed_statement_is_used(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """The statement the LLM deliberately wrote is the one used — stays INQUIRY"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Mock LLM response carrying an explicit proposed_problem_statement
        mock_response = json.dumps(
            {
                "agent_response": "Let me confirm: API latency has spiked to 8 seconds affecting dashboards. Is this accurate?",
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "slowness",
                        "severity_guess": "high",
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
                    },
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Users getting 503 errors",
                    },
                    "proposed_problem_statement": (
                        "API returning 503 errors affecting users (ongoing)"
                    ),
                },
            }
        )
        mock_llm.generate.return_value = mock_response_turn1

        result1 = await engine.process_turn(
            inquiry_case, "Our API is returning 503 errors."
        )
        case_after_turn1 = result1["case_updated"]
        assert case_after_turn1.state == CaseState.INQUIRY
        # Turn 2 is a CORRECTION, so turn 1 must have produced something to
        # correct; otherwise the decline path below is never exercised.
        assert (
            case_after_turn1.inquiry.proposed_problem_statement
            == "API returning 503 errors affecting users (ongoing)"
        )

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
    """The ``<inquiry_state>`` block carries ONE rule, not a fork.

    It used to alternate between NOT_YET_CONFIRMED ("do NOT re-propose it")
    and HANDSHAKE_DEFERRED ("RE-PRESENT it verbatim") because PRESENTING the
    statement was the LLM's job and the prompt had to say, turn by turn,
    whether this was a presenting turn. The engine presents now (#1607), so
    the LLM is told once: the statement is already on screen, don't restate it.
    """

    def test_engine_presents_rule_injected_when_unconfirmed(self):
        """An unconfirmed statement gets the engine-presents directive."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            enterprise_id="org_123",
            description="",
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors",
                problem_statement_confirmed=False,
            ),
        )

        context = build_investigation_context(case, user_message="test message")
        context_str = str(context.values())

        assert "ENGINE_PRESENTS_THIS" in context_str
        # The retired fork must not come back in either direction.
        assert "HANDSHAKE_DEFERRED" not in context_str
        assert "AWAITING_CONFIRMATION" not in context_str

    def test_block_states_prior_turn_fact_not_present_tense(self):
        """The block describes the case as it ENTERED the turn.

        A present-tense "the user has not confirmed it yet" is false on the
        very turn they do confirm, and mis-primes the model to keep waiting.
        Confirmation DETECTION lives in the static TWO-STEP CONFIRMATION prose
        and the ``user_confirmed_investigation`` schema field, not here.
        """
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            enterprise_id="org_123",
            description="",
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors",
                problem_statement_confirmed=False,
            ),
        )

        context = build_investigation_context(case, user_message="test message")
        normalized = " ".join(str(context.values()).split())

        assert "unconfirmed going into this turn" in normalized
        assert "has not confirmed it yet" not in normalized, (
            "the block re-asserts the present-tense 'has not confirmed it yet' "
            "fact that is false on the confirming turn."
        )

    def test_no_injection_when_confirmed(self):
        """A confirmed statement gets no directive — Gate 1 is closed."""
        from faultmaven.core.investigation.prompts.context_builder import (
            build_investigation_context,
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            state=CaseState.INQUIRY,
            user_id="user_123",
            enterprise_id="org_123",
            description="",
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors",
                problem_statement_confirmed=True,
            ),
        )

        context = build_investigation_context(case, user_message="test message")
        context_str = str(context.values())

        assert "ENGINE_PRESENTS_THIS" not in context_str
        assert "AWAITING_CONFIRMATION" not in context_str


@pytest.mark.unit
class TestGate1PresentsItsStatement:
    """INV-01: a Gate-1 turn ships its statement with its buttons.

    The affordances ask the user to confirm a problem statement, so the
    statement has to be on screen on the same turn. Two production cases
    proved the prompt cannot be relied on to put it there, so the engine
    composes it — on EVERY pending turn, which is also what makes the old
    deferral/recovery flag unnecessary.
    """

    @pytest.mark.asyncio
    async def test_statement_is_composed_into_a_pending_turn(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """The standing statement appears verbatim beside the confirm pair.

        The LLM answers something unrelated and never mentions the statement —
        the exact shape of the production defect. The engine supplies it.

        Mutation check: delete the gate1 composition block in
        ``_process_turn_impl`` and this goes red.
        """
        statement = "Checkout API returns 503 for all users since 14:00 UTC"
        inquiry_case.inquiry.proposed_problem_statement = statement
        inquiry_case.inquiry.problem_statement_confirmed = False

        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": "Kubernetes RBAC denies a request when no rule matches.",
                "state_updates": {},
            }
        )

        result = await engine.process_turn(inquiry_case, "What does a 403 mean?")

        assert statement in result["agent_response"], (
            "Gate 1 served its confirm/refine pair without the statement the "
            "pair refers to."
        )
        labels = {
            (f or {}).get("label") for f in (result.get("suggested_follow_ups") or [])
        }
        assert "Yes, let's investigate" in labels

    @pytest.mark.asyncio
    async def test_revising_and_confirming_in_one_turn_is_refused(
        self, mock_llm, mock_repo, inquiry_case
    ):
        """Consent applies only to wording the user has already seen.

        A statement existed at turn start, so the old guard (which asked only
        "did something stand?") admitted this. The user never saw the revision.
        """
        inquiry_case.inquiry.proposed_problem_statement = "API is slow"
        inquiry_case.inquiry.problem_statement_confirmed = False

        engine = MilestoneEngine(mock_llm, mock_repo, investigation_tools=MagicMock())
        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": "Updated and confirmed.",
                "state_updates": {
                    "proposed_problem_statement": "Checkout API returns 503 for all users",
                    "user_confirmed_investigation": True,
                },
            }
        )

        result = await engine.process_turn(inquiry_case, "yes that's right")
        updated = result["case_updated"]

        assert updated.state == CaseState.INQUIRY
        assert updated.inquiry.problem_statement_confirmed is False
        # The revised wording stands and is presented, so the user can answer it.
        assert (
            updated.inquiry.proposed_problem_statement
            == "Checkout API returns 503 for all users"
        )
        assert "Checkout API returns 503 for all users" in result["agent_response"]


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


class TestProblemStatementSingleWriter:
    """``proposed_problem_statement`` has exactly ONE writer (#1606).

    A second writer used to promote ``problem_confirmation.preliminary_guidance``
    into the statement whenever none existed yet. That field carried no
    description on the LLM-facing schema and was named nowhere in the INQUIRY
    prompt, so a model filled it from its name alone — with guidance — and the
    guidance became the problem statement, then ``case.description``, then the
    frame for the whole investigation.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "urgency",
        [
            pytest.param(
                {
                    "level": "MEDIUM",
                    "is_ongoing": True,
                    "is_incident_report": False,
                    "impact_assessment": "Not yet established.",
                },
                id="benign",
            ),
            pytest.param(
                {
                    "level": "CRITICAL",
                    "is_ongoing": True,
                    "is_incident_report": True,
                    "impact_assessment": "All users blocked.",
                },
                id="incident",
            ),
        ],
    )
    async def test_problem_confirmation_alone_mints_no_statement(
        self, mock_llm, mock_repo, inquiry_case, urgency
    ):
        """Classifying the problem does NOT propose a statement.

        The engine may only carry a statement the model deliberately wrote as
        one. A turn that classifies (``problem_confirmation`` +
        ``preliminary_urgency``) without proposing leaves the statement unset,
        so Gate 1 stays shut.

        Both urgency shapes are exercised because they take different arms in
        ``_apply_inquiry_updates``: the benign one only logs, while the
        CRITICAL/ongoing/incident one is the branch a future change is most
        likely to touch — and the branch where a statement-less case would be
        presented to the user as a confirmable incident.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": (
                    "Which service is returning the errors, and when did they start?"
                ),
                "state_updates": {
                    "problem_confirmation": {
                        "problem_type": "error",
                        "severity_guess": "unknown",
                    },
                    "preliminary_urgency": urgency,
                },
            }
        )

        result = await engine.process_turn(inquiry_case, "Something is erroring.")
        updated_case = result["case_updated"]

        assert updated_case.inquiry.proposed_problem_statement is None
        assert updated_case.state == CaseState.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False

        # Surface check, not a restatement of the line above: this pins the
        # wiring from state to affordance, so a Gate 1 keyed off anything
        # other than the statement would still be caught here.
        labels = {
            (f or {}).get("label") for f in (result.get("suggested_follow_ups") or [])
        }
        assert "Yes, let's investigate" not in labels

    def test_apply_inquiry_updates_assigns_the_statement_exactly_once(self):
        """Source-level pin: one write, whatever shape it takes.

        The behavioural test above only catches a promotion that fires on the
        shapes it exercises. This one catches any second WRITE, including ones
        that reach the attribute through a local alias
        (``_inq = case.inquiry; _inq.proposed_problem_statement = ...``), a
        tuple unpack, an augmented assignment, or ``setattr``.

        Matching on ``ast.Store`` context rather than on the base expression is
        what makes that true: every assignment form marks its target attribute
        Store, so none of them can slip past by renaming the base.

        Mutation check: add any second write of
        ``proposed_problem_statement`` to ``_apply_inquiry_updates`` and this
        goes red.
        """
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(MilestoneEngine._apply_inquiry_updates))
        tree = ast.parse(src)

        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "proposed_problem_statement"
            and isinstance(node.ctx, ast.Store)
        ]

        setattr_writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "proposed_problem_statement"
        ]

        assert not setattr_writes, (
            f"setattr write of proposed_problem_statement at lines "
            f"{[n.lineno for n in setattr_writes]}"
        )
        assert len(writes) == 1, (
            f"expected exactly 1 write of proposed_problem_statement in "
            f"_apply_inquiry_updates, found {len(writes)} "
            f"at lines {[w.lineno for w in writes]}"
        )

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import MilestoneUpdates
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

    def get_structured_output_strategy(self, schema):
        """Mock implementation of capability system method"""
        # Default to STRICT mode with json_schema for testing
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,  # STRICT mode doesn't need schema in prompt
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


def _make_resolution_ready(case):
    """Add root cause and solution to a case so it passes resolution readiness check."""
    from faultmaven.modules.case.contracts import (
        RootCauseConclusion,
        Solution,
        SolutionType,
    )

    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Misconfigured connection pool timeout",
        confidence_level="verified",
        likelihood=0.9,
        mechanism="Connection pool timeout set to 1s caused cascading failures",
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Increase connection pool timeout to 30s",
            longterm_fix="Update pool timeout in application config",
        )
    ]
    return case


class TestMilestoneEngine:

    @pytest.mark.asyncio
    async def test_process_turn_investigating(self, mock_llm, mock_repo, base_case):
        """Test processing a turn in INVESTIGATING status"""
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceForm,
            EvidenceSourceType,
        )

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Add evidence to the case (required for milestone completion)
        base_case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test evidence",
                content_ref="test.log",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                collected_at=datetime.now(timezone.utc),
                collected_by="user_123",
                primary_purpose="Testing",
                preprocessed_content="Log content",
                content_size_bytes=100,
                preprocessing_method="manual",
                form=EvidenceForm.USER_TEXT,
                collected_at_turn=1,
            )
        )

        # Mock LLM response with structured output (including internal_reasoning)
        mock_response_content = json.dumps(
            {
                "agent_response": "I have verified the symptom.",
                "internal_reasoning": {
                    "evidence_analyzed": ["ev_001122334455"],
                    "conclusions": [
                        {
                            "observation": "Symptom observed",
                            "inference": "Problem confirmed",
                            "confidence": 0.9,
                        }
                    ],
                    "milestone_justifications": {
                        "symptom_verified": "Verified based on ev_001122334455 showing errors"
                    },
                    "uncertainties": [],
                },
                "state_updates": {
                    "milestones": {"symptom_verified": True},
                    "evidence_to_add": [
                        {
                            "summary": "Error logs showing 500 errors",
                            "category": "symptom_evidence",
                            "source_type": "logs",
                            "content_ref": "ERROR 500 at /api/checkout",
                        }
                    ],
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
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

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
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

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

    @pytest.mark.asyncio
    async def test_reasoning_validation_success(self, mock_llm, mock_repo, base_case):
        """Test successful reasoning validation when milestone completed with justification"""
        from faultmaven.core.investigation.milestone_engine import (
            validate_reasoning_first,
        )
        from faultmaven.core.investigation.schemas import (
            InternalReasoning,
            InvestigationResponse_Diagnosis,
            ReasoningConclusion,
        )
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceForm,
            EvidenceSourceType,
        )

        # Add evidence to case
        base_case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test evidence",
                content_ref="test.log",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                collected_at=datetime.now(timezone.utc),
                collected_by="user_123",
                primary_purpose="Testing",
                preprocessed_content="Log content",
                content_size_bytes=100,
                preprocessing_method="manual",
                form=EvidenceForm.USER_TEXT,
                collected_at_turn=1,
            )
        )

        # Create response with proper internal reasoning
        response = InvestigationResponse_Diagnosis(
            agent_response="Symptom verified",
            internal_reasoning=InternalReasoning(
                evidence_analyzed=["ev_001122334455"],
                conclusions=[
                    ReasoningConclusion(
                        observation="Errors in logs",
                        inference="System is failing",
                        confidence=0.9,
                    )
                ],
                milestone_justifications={
                    "symptom_verified": "Confirmed via ev_001122334455 showing errors"
                },
                uncertainties=[],
            ),
            state_updates=InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
                milestones=MilestoneUpdates(symptom_verified=True),
                outcome="milestone_completed",
            ),
        )

        is_valid, errors = validate_reasoning_first(response, base_case)
        assert is_valid
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_reasoning_validation_failure_no_justification(self, base_case):
        """Test reasoning validation fails when milestone completed without justification"""
        from faultmaven.core.investigation.milestone_engine import (
            validate_reasoning_first,
        )
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
            MilestoneUpdates,
        )

        # Create response with milestone but NO internal reasoning
        response = InvestigationResponse_Diagnosis(
            agent_response="Symptom verified",
            state_updates=InvestigationResponse_Diagnosis.DiagnosisStateUpdate(
                milestones=MilestoneUpdates(symptom_verified=True),
                outcome="milestone_completed",
            ),
        )

        is_valid, errors = validate_reasoning_first(response, base_case)
        assert not is_valid
        assert len(errors) > 0
        assert "internal_reasoning" in errors[0].lower()

    @pytest.mark.asyncio
    async def test_blocker_detection_surfaces_system_feedback(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that missing_critical_data surfaces as system_feedback, not degraded mode"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Mock LLM response with blocker detection
        mock_response_content = json.dumps(
            {
                "agent_response": "Investigation limitations: Critical data is corrupted",
                "state_updates": {
                    "missing_critical_data": {
                        "blocker_type": "data_corrupted",
                        "description": "Logs missing timestamps",
                        "what_was_expected": "Complete logs with timestamps",
                        "what_was_found": "Logs without timestamps",
                        "impact": "Cannot establish timeline",
                        "suggested_alternatives": [
                            "Request logs from different source"
                        ],
                    },
                    "outcome": "conversation",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(base_case, "Check logs")

        # Verify blocker surfaced as system_feedback in the turn record
        updated_case = result["case_updated"]
        last_turn = updated_case.turn_history[-1]
        assert last_turn.system_feedback is not None
        assert "Logs missing timestamps" in last_turn.system_feedback
        assert "Cannot establish timeline" in last_turn.system_feedback

    @pytest.mark.asyncio
    async def test_evidence_quality_issues_logged(self, mock_llm, mock_repo, base_case):
        """Test that evidence quality issues are processed without error"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Mock LLM response with quality issues
        mock_response_content = json.dumps(
            {
                "agent_response": "Found evidence but quality is limited",
                "state_updates": {
                    "evidence_quality_issues": [
                        {
                            "evidence_id": "ev_001122334455",
                            "issue_type": "incomplete",
                            "severity": "limiting",
                            "description": "Partial log data",
                            "workaround": "Use metrics as supplement",
                        }
                    ],
                    "outcome": "conversation",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # Should process without error
        result = await engine.process_turn(base_case, "Analyze evidence")

        # Verify no exception raised and case is returned
        assert result["case_updated"] is not None
        assert result["agent_response"] == "Found evidence but quality is limited"

    @pytest.mark.asyncio
    async def test_user_intent_detection_proposes_transition(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that NLP-detected resolution intent proposes a transition (User-Agent Handshake)

        Design Decision B: Terminal transitions require explicit user confirmation.
        NLP-detected intent sets pending_transition, NOT an immediate state change.
        The user must confirm in the next turn for the transition to execute.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Set case to INVESTIGATING status with resolution-ready data
        base_case.status = CaseStatus.INVESTIGATING
        _make_resolution_ready(base_case)

        # Test various resolution phrases — all should PROPOSE, not execute.
        # Note: "close this case" is ambiguous and handled separately
        # (asks for clarification without setting pending_transition).
        resolution_phrases = [
            "mark as resolved",
            "case is resolved",
            "problem solved",
            "issue fixed",
            "solution worked",
        ]

        for phrase in resolution_phrases:
            # Reset case status
            base_case.atomic_update(
                status=CaseStatus.INVESTIGATING,
                resolved_at=None,
                closed_at=None,
            )
            base_case.pending_transition = None
            base_case.progress.solution_verified = False

            result = await engine.process_turn(base_case, phrase)

            updated_case = result["case_updated"]

            # Verify case did NOT transition — pending_transition is set instead
            assert (
                updated_case.status == CaseStatus.INVESTIGATING
            ), f"Case should remain INVESTIGATING for phrase: '{phrase}'"
            assert (
                updated_case.progress.solution_verified is False
            ), f"solution_verified should not be set for phrase: '{phrase}'"
            assert (
                hasattr(updated_case, "pending_transition")
                and updated_case.pending_transition is not None
            ), f"pending_transition should be set for phrase: '{phrase}'"
            assert (
                updated_case.pending_transition["to_status"] == "resolved"
            ), f"pending_transition target should be 'resolved' for phrase: '{phrase}'"

    @pytest.mark.asyncio
    async def test_user_intent_detection_case_insensitive(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that user intent detection is case-insensitive (proposes transition)"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        base_case.status = CaseStatus.INVESTIGATING
        _make_resolution_ready(base_case)

        # Test uppercase, mixed case, with punctuation
        result = await engine.process_turn(
            base_case, "CLOSE THIS CASE! The solution worked perfectly."
        )

        updated_case = result["case_updated"]
        # User-Agent Handshake: NLP detection proposes, does not execute
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.progress.solution_verified is False
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "resolved"

    @pytest.mark.asyncio
    async def test_user_intent_detection_no_false_positives(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that user intent detection doesn't trigger on unrelated phrases"""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        base_case.status = CaseStatus.INVESTIGATING

        # Mock LLM response
        mock_response_content = json.dumps(
            {
                "agent_response": "Continuing investigation.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # Test phrases that should NOT trigger resolution or propose transition
        non_resolution_phrases = [
            "I want to resolve this issue",
            "How do I close cases?",
            "The problem is not solved yet",
            "Please help me solve this",
        ]

        for phrase in non_resolution_phrases:
            base_case.status = CaseStatus.INVESTIGATING
            base_case.progress.solution_verified = False
            base_case.pending_transition = None

            result = await engine.process_turn(base_case, phrase)

            updated_case = result["case_updated"]

            # Verify case did NOT transition and no pending_transition proposed
            assert (
                updated_case.status == CaseStatus.INVESTIGATING
            ), f"False positive for phrase: '{phrase}'"
            assert (
                updated_case.progress.solution_verified is False
            ), f"False positive for phrase: '{phrase}'"
            assert (
                not hasattr(updated_case, "pending_transition")
                or updated_case.pending_transition is None
            ), f"False positive pending_transition for phrase: '{phrase}'"

    @pytest.mark.asyncio
    async def test_reasoning_validation_skipped_when_pending_transition_exists(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that reasoning validation is skipped when a pending_transition exists

        User-Agent Handshake: When a transition has been proposed (pending_transition set),
        the LLM may still attempt milestone updates. Reasoning validation should be
        skipped since the case is in the process of transitioning.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        base_case.status = CaseStatus.INVESTIGATING
        # Simulate a pending transition from a previous turn's propose_transition()
        base_case.pending_transition = {
            "to_status": "resolved",
            "reason": "User indicated the problem is resolved",
            "summary": "Based on your message, the issue appears to be resolved.",
            "evidence_ids": [],
            "proposed_at": "2026-02-09T00:00:00+00:00",
            "proposed_by": "agent",
        }

        # Mock LLM response trying to complete OTHER milestones without justification
        mock_response_content = json.dumps(
            {
                "agent_response": "Case is being closed.",
                "state_updates": {
                    "milestones": {"symptom_verified": True},  # No justification!
                    "outcome": "milestone_completed",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User confirms the pending transition
        # This should NOT fail with reasoning validation error
        # because pending_transition exists (case transitioning to terminal)
        result = await engine.process_turn(base_case, "yes, go ahead")

        # Verify transition executed via handshake confirmation
        assert result["case_updated"] is not None
        assert result["case_updated"].status == CaseStatus.RESOLVED
        assert result["case_updated"].progress.solution_verified is True

    @pytest.mark.asyncio
    async def test_complete_user_agent_handshake_flow(
        self, mock_llm, mock_repo, base_case
    ):
        """Integration test: Complete User-Agent Handshake flow for terminal transition

        This test exercises the complete two-step handshake:
        1. Turn N: User says "the fix worked" → readiness check passes → system proposes transition
        2. Turn N+1: User says "yes" → system confirms and executes transition

        Design Decision B: Terminal transitions are irreversible, so the agent
        proposes and the user explicitly confirms.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Start in INVESTIGATING with resolution-ready case
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True
        base_case.progress.scope_assessed = True
        _make_resolution_ready(base_case)

        # ===== TURN N: User says "the fix worked" → proposes transition =====

        result_turn_n = await engine.process_turn(base_case, "the fix worked")

        updated_case = result_turn_n["case_updated"]

        # Verify: Case stays INVESTIGATING, pending_transition proposed
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.progress.solution_verified is False
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "resolved"

        # ===== TURN N+1: User confirms the transition =====

        mock_llm.generate.reset_mock()
        mock_response_content_confirm = json.dumps(
            {
                "agent_response": "Case resolved.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content_confirm

        # User explicitly confirms
        result_turn_n1 = await engine.process_turn(updated_case, "yes, go ahead")

        final_case = result_turn_n1["case_updated"]

        # ===== VERIFY COMPLETE TERMINAL TRANSITION =====

        # 1. Case transitioned to RESOLVED terminal state
        assert final_case.status == CaseStatus.RESOLVED
        assert final_case.is_terminal is True

        # 2. Terminal state timestamps set
        assert final_case.resolved_at is not None
        assert final_case.closed_at is not None

        # 3. Correct closure reason
        assert final_case.closure_reason == "resolved"

        # 4. Solution milestone set via handshake confirmation
        assert final_case.progress.solution_verified is True

        # 5. Pending transition cleared after execution
        assert final_case.pending_transition is None

        # 6. Status history recorded transition with user as trigger
        assert len(final_case.action_history) > 0
        last_transition = final_case.action_history[-1]
        assert last_transition.from_status == CaseStatus.INVESTIGATING
        assert last_transition.to_status == CaseStatus.RESOLVED

    @pytest.mark.asyncio
    async def test_user_intent_close_as_unresolved_proposes_closed(
        self, mock_llm, mock_repo, base_case
    ):
        """Test user intent: 'Close as unresolved' should propose CLOSED via handshake.

        NLP abandonment patterns now propose a pending transition instead of
        immediately executing the CLOSED transition. The user must confirm.

        Pattern matching order:
        1. Abandonment patterns (highest priority) → propose CLOSED
        2. Resolution patterns (medium priority) → propose RESOLVED
        3. Ambiguous close patterns (lowest priority) → ask clarification
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Start in INVESTIGATING with some progress
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True
        base_case.progress.scope_assessed = True

        # Mock LLM response (still goes through LLM after proposing)
        mock_response_content = json.dumps(
            {
                "agent_response": "Understood. I've noted your intent to close.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User explicitly says "close as unresolved" - should propose CLOSED, not execute immediately
        result = await engine.process_turn(base_case, "Close this case as unresolved")

        updated_case = result["case_updated"]

        # Case should still be INVESTIGATING with a pending transition
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "closed"

        # Solution milestone NOT completed
        assert updated_case.progress.solution_verified is False

    @pytest.mark.asyncio
    async def test_user_intent_ambiguous_close_asks_for_clarification(
        self, mock_llm, mock_repo, base_case
    ):
        """Ambiguous 'close this case' asks for clarification without setting pending_transition.

        When user says "close this case" without indicating resolved vs closed,
        the system asks for clarification. No pending_transition is set because
        we don't know the user's intent yet. Their next message will route
        through resolve_patterns or abandonment_patterns.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True

        result = await engine.process_turn(base_case, "close this case")

        updated_case = result["case_updated"]

        # Case stays INVESTIGATING — no transition proposed
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.progress.solution_verified is False
        assert updated_case.pending_transition is None  # NOT set — clarification needed

        # Response asks for clarification
        assert "resolved" in result["agent_response"].lower()
        assert "closed" in result["agent_response"].lower()

        # LLM is NOT called
        assert not mock_llm.generate.called

    @pytest.mark.asyncio
    async def test_explicit_status_transition_inquiry_to_closed(
        self, mock_llm, mock_repo
    ):
        """Test explicit status_transition intent: INQUIRY → CLOSED via dropdown.

        Dropdown CLOSED now proposes a pending transition (handshake) instead
        of executing immediately.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Create case in INQUIRY status
        inquiry_case = Case(
            case_id="case_1234567890ab",  # 17 chars
            title="Test Inquiry Close",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="Test description",
            problem_verification=ProblemVerification(
                symptom_statement="Test symptom",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
        )

        # User clicks "Close" in dropdown - frontend sends status_transition intent
        result = await engine.process_turn(
            case=inquiry_case,
            user_message="Close this case. I don't need further investigation.",
            intent_type="status_transition",
            intent_data={
                "from_status": "inquiry",
                "to_status": "closed",
                "user_confirmed": True,
            },
        )

        updated_case = result["case_updated"]

        # 1. Status should still be INQUIRY — pending transition proposed
        assert updated_case.status == CaseStatus.INQUIRY

        # 2. Pending transition should be set to "closed"
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "closed"

        # 3. Should NOT have gone through investigation
        assert updated_case.progress.symptom_verified is False

        # 4. LLM should NOT be called (deterministic response)
        assert not mock_llm.generate.called

    @pytest.mark.asyncio
    async def test_explicit_status_transition_investigating_to_closed(
        self, mock_llm, mock_repo, base_case
    ):
        """Test explicit status_transition intent: INVESTIGATING → CLOSED via dropdown.

        Dropdown CLOSED now proposes a pending transition (handshake) instead
        of executing immediately.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Start in INVESTIGATING with some progress
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True

        # User clicks "Close" in dropdown - frontend sends status_transition intent
        result = await engine.process_turn(
            case=base_case,
            user_message="Close this case as unresolved.",
            intent_type="status_transition",
            intent_data={
                "from_status": "investigating",
                "to_status": "closed",
                "user_confirmed": True,
            },
        )

        updated_case = result["case_updated"]

        # 1. Status should still be INVESTIGATING — pending transition proposed
        assert updated_case.status == CaseStatus.INVESTIGATING

        # 2. Pending transition should be set to "closed"
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "closed"

        # 3. Solution should NOT be verified
        assert updated_case.progress.solution_verified is False

        # 4. LLM should NOT be called (deterministic response)
        assert not mock_llm.generate.called

    @pytest.mark.asyncio
    async def test_explicit_status_transition_inquiry_to_investigating(
        self, mock_llm, mock_repo
    ):
        """Test explicit status_transition intent: INQUIRY → INVESTIGATING via dropdown.

        Design: Dropdown = message. The dropdown does NOT bypass the agent.
        Instead, it injects a pre-composed message and lets the LLM handle
        the multi-turn problem statement flow. When the LLM sets
        user_confirmed_investigation=True, the transition fires automatically.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Create case in INQUIRY status with a proposed problem statement
        inquiry_case = Case(
            case_id="case_0987654321ab",  # 17 chars
            title="Test Inquiry to Investigating",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="Test description",
        )
        inquiry_case.inquiry.proposed_problem_statement = "Test symptom"

        # Mock LLM response: InquiryResponse with user_confirmed_investigation=True
        mock_response_content = json.dumps(
            {
                "agent_response": "Confirmed. Starting investigation into the reported issue.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User clicks "Start Investigation" in dropdown
        result = await engine.process_turn(
            case=inquiry_case,
            user_message="I want to start a formal investigation to find the root cause.",
            intent_type="status_transition",
            intent_data={
                "from_status": "inquiry",
                "to_status": "investigating",
                "user_confirmed": True,
            },
        )

        updated_case = result["case_updated"]

        # 1. Status should be INVESTIGATING (transition via _check_automatic_transitions)
        assert updated_case.status == CaseStatus.INVESTIGATING

        # 2. Inquiry data should be updated
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.inquiry.decided_to_investigate is True

        # 3. Status history should record the transition
        assert len(updated_case.action_history) > 0
        last_transition = updated_case.action_history[-1]
        assert last_transition.from_status == CaseStatus.INQUIRY
        assert last_transition.to_status == CaseStatus.INVESTIGATING

        # 4. Should have called LLM (not bypassed)
        assert mock_llm.generate.called

    @pytest.mark.asyncio
    async def test_investigating_dropdown_without_problem_statement_calls_llm(
        self, mock_llm, mock_repo
    ):
        """Dropdown INQUIRY→INVESTIGATING with no problem statement routes through LLM.

        Design: Dropdown = message. Without a problem statement, the LLM should
        ask the user to describe the problem rather than silently transitioning.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        inquiry_case = Case(
            case_id="case_0987654321cd",
            title="API issue",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
        )
        # No proposed_problem_statement set — agent hasn't formulated one yet

        # LLM asks user to describe the problem (does NOT confirm investigation)
        mock_response_content = json.dumps(
            {
                "agent_response": "I'd like to help investigate. Could you describe the problem you're seeing?",
                "state_updates": {},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(
            case=inquiry_case,
            user_message="",  # Empty message — dropdown click only
            intent_type="status_transition",
            intent_data={"from_status": "inquiry", "to_status": "investigating"},
        )

        updated_case = result["case_updated"]

        # Case should stay in INQUIRY (no problem statement to confirm)
        assert updated_case.status == CaseStatus.INQUIRY
        # LLM was called (not bypassed)
        assert mock_llm.generate.called

    @pytest.mark.asyncio
    async def test_resolved_dropdown_proposes_transition(
        self, mock_llm, mock_repo, base_case
    ):
        """Dropdown INVESTIGATING→RESOLVED proposes transition when case is ready.

        Design: The first click checks resolution readiness. If the case has
        root cause + solution, it proposes the transition and returns immediately
        with a confirmation prompt (skips the full LLM pipeline to avoid timeout).
        The transition does NOT execute until the user confirms on the next turn.
        """
        from faultmaven.modules.case.contracts import (
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        # Set up a case that meets resolution criteria
        base_case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Misconfigured connection pool timeout",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Connection pool timeout set to 1s caused cascading failures under load",
        )
        base_case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Increase connection pool timeout to 30s",
                longterm_fix="Update pool timeout in application config",
            )
        ]

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        result = await engine.process_turn(
            case=base_case,
            user_message="The issue is resolved.",
            intent_type="status_transition",
            intent_data={"from_status": "investigating", "to_status": "resolved"},
        )

        updated_case = result["case_updated"]

        # Case should still be INVESTIGATING (transition proposed, not executed)
        assert updated_case.status == CaseStatus.INVESTIGATING

        # Pending transition should be set
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "resolved"

        # LLM is NOT called — response is returned immediately with proposal message
        assert not mock_llm.generate.called

        # Response contains confirmation prompt with root cause and solution
        assert "resolved" in result["agent_response"].lower()
        assert "root cause" in result["agent_response"].lower()

    @pytest.mark.asyncio
    async def test_resolved_dropdown_suggests_close_when_not_ready(
        self, mock_llm, mock_repo, base_case
    ):
        """Dropdown RESOLVED with missing info sets pending_transition with needs_info.

        The system remembers the user's resolve intent so that when they provide
        the missing info, the next turn shows a confirmation prompt without
        requiring another dropdown click or LLM call.
        """
        # base_case has no root_cause_conclusion, no solutions, no evidence
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        result = await engine.process_turn(
            case=base_case,
            user_message="The issue is resolved.",
            intent_type="status_transition",
            intent_data={"from_status": "investigating", "to_status": "resolved"},
        )

        # Case should still be INVESTIGATING with pending transition + needs_info
        assert result["case_updated"].status == CaseStatus.INVESTIGATING
        assert result["case_updated"].pending_transition is not None
        assert result["case_updated"].pending_transition["to_status"] == "resolved"
        assert result["case_updated"].pending_transition["needs_info"] is True

        # Response suggests closing instead
        assert "close" in result["agent_response"].lower()

        # LLM is NOT called
        assert not mock_llm.generate.called

    @pytest.mark.asyncio
    async def test_resolved_dropdown_with_pending_confirms(
        self, mock_llm, mock_repo, base_case
    ):
        """Dropdown RESOLVED with existing pending transition confirms it.

        If the user clicks Resolve again when a pending transition already
        exists, it acts as confirmation and executes the transition.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        # Set up pending transition from a previous turn
        base_case.pending_transition = {
            "to_status": "resolved",
            "reason": "User indicated resolution",
            "summary": "Issue resolved",
            "evidence_ids": [],
            "proposed_at": "2026-03-01T00:00:00Z",
            "proposed_by": "agent",
        }

        result = await engine.process_turn(
            case=base_case,
            user_message="yes",
            intent_type="status_transition",
            intent_data={"from_status": "investigating", "to_status": "resolved"},
        )

        updated_case = result["case_updated"]

        # Transition should be executed (confirmed the pending)
        assert updated_case.status == CaseStatus.RESOLVED
        assert updated_case.pending_transition is None
        assert updated_case.progress.solution_verified is True

    @pytest.mark.asyncio
    async def test_resolved_dropdown_injects_precomposed_message(
        self, mock_llm, mock_repo, base_case
    ):
        """Dropdown RESOLVED with empty message returns proposal immediately.

        When user clicks the dropdown without typing a message, the system
        checks readiness and returns a confirmation prompt directly (no LLM call).
        """
        from faultmaven.modules.case.contracts import (
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        # Set up a case that meets resolution criteria
        base_case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Misconfigured connection pool timeout",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Timeout too low for production load",
        )
        base_case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Increase pool timeout",
                longterm_fix="Set timeout to 30s",
            )
        ]

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        result = await engine.process_turn(
            case=base_case,
            user_message="",  # Empty — dropdown click only
            intent_type="status_transition",
            intent_data={"from_status": "investigating", "to_status": "resolved"},
        )

        # LLM is NOT called — returns immediately with proposal
        assert not mock_llm.generate.called
        # Pending transition proposed
        assert result["case_updated"].pending_transition is not None
        # Response asks for confirmation
        assert "resolved" in result["agent_response"].lower()

    @pytest.mark.asyncio
    async def test_closed_transitions_use_handshake(
        self, mock_llm, mock_repo, base_case
    ):
        """CLOSED transitions now use the handshake pattern (pending transition).

        Design: CLOSED transitions propose a pending transition with a closure
        readiness summary. The user must confirm before the transition executes.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        result = await engine.process_turn(
            case=base_case,
            user_message="Close without resolution",
            intent_type="status_transition",
            intent_data={"from_status": "investigating", "to_status": "closed"},
        )

        updated_case = result["case_updated"]

        # Should propose transition (not execute immediately)
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.pending_transition is not None
        assert updated_case.pending_transition["to_status"] == "closed"
        # LLM should NOT be called (deterministic response)
        assert not mock_llm.generate.called


class TestInquiryConfirmation:
    """Test that INQUIRY→INVESTIGATING transition relies on the LLM setting
    user_confirmed_investigation=True. No mechanical keyword fallback."""

    @pytest.mark.asyncio
    async def test_stays_in_inquiry_when_llm_misses_confirmation(
        self, mock_llm, mock_repo
    ):
        """LLM doesn't set user_confirmed_investigation → stays in INQUIRY,
        even if user message contains 'yes'. The LLM is solely responsible."""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test INQUIRY stuck",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
        )
        case.inquiry.proposed_problem_statement = "API returning 503 errors"

        # LLM response: does NOT set user_confirmed_investigation
        mock_response_content = json.dumps(
            {
                "agent_response": "Let me confirm: your API is returning 503 errors?",
                "state_updates": {},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(case, "yes, that's correct")

        updated_case = result["case_updated"]
        # No fallback — LLM is the sole decision-maker for confirmation
        assert updated_case.status == CaseStatus.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False

    @pytest.mark.asyncio
    async def test_fallback_does_not_fire_without_proposed_statement(
        self, mock_llm, mock_repo
    ):
        """Fallback should not fire if there's no proposed_problem_statement."""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test no statement",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
        )
        # No proposed_problem_statement set

        mock_response_content = json.dumps(
            {
                "agent_response": "What issue are you experiencing?",
                "state_updates": {},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(case, "yes")

        updated_case = result["case_updated"]
        # Should stay in INQUIRY — no statement to confirm
        assert updated_case.status == CaseStatus.INQUIRY
        assert updated_case.inquiry.problem_statement_confirmed is False

    @pytest.mark.asyncio
    async def test_llm_path_takes_priority_over_fallback(self, mock_llm, mock_repo):
        """When LLM sets user_confirmed_investigation=True, the LLM path fires (not fallback)."""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test LLM priority",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="",
        )
        case.inquiry.proposed_problem_statement = "Database connection drops"

        # LLM correctly detects confirmation
        mock_response_content = json.dumps(
            {
                "agent_response": "Starting investigation.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(case, "yes, proceed")

        updated_case = result["case_updated"]
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.status == CaseStatus.INVESTIGATING


# =============================================================================
# Tests: Resolution & Runbook Readiness, Terminal Summary Guardrail
# =============================================================================


class TestReadinessAssessments:
    """Test resolution readiness, runbook readiness, and terminal summary guardrail."""

    def _make_case(self, **overrides):
        defaults = {
            "user_id": "user_123",
            "organization_id": "org_123",
            "title": "Test Case",
            "description": "Database queries timing out",
            "status": CaseStatus.INVESTIGATING,
            "problem_verification": ProblemVerification(
                symptom_statement="Database queries timing out",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            "inquiry": InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Database queries timing out",
            ),
        }
        defaults.update(overrides)
        return Case(**defaults)

    def test_resolution_readiness_suggest_close_when_empty(self):
        """No root cause, no solution, no evidence → suggest close."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_resolution_readiness,
        )

        case = self._make_case()
        result = assess_resolution_readiness(case)
        assert result.verdict == result.SUGGEST_CLOSE
        assert "root cause" in result.missing
        assert "solution" in result.missing

    def test_resolution_readiness_needs_info_when_partial(self):
        """Has root cause but no solution → needs info."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_resolution_readiness,
        )
        from faultmaven.modules.case.contracts import RootCauseConclusion

        case = self._make_case()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Connection pool exhaustion",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Pool limit too low for concurrent requests",
        )
        result = assess_resolution_readiness(case)
        assert result.verdict == result.NEEDS_INFO
        assert "solution" in result.missing
        assert "root cause" not in result.missing

    def test_resolution_readiness_ready_when_complete(self):
        """Has root cause + solution → ready."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_resolution_readiness,
        )
        from faultmaven.modules.case.contracts import (
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        case = self._make_case()
        _make_resolution_ready(case)
        result = assess_resolution_readiness(case)
        assert result.verdict == result.READY

    def test_runbook_readiness_not_suitable_without_root_cause(self):
        """Has solution but no root cause → not suitable for runbook."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_runbook_readiness,
        )
        from faultmaven.modules.case.contracts import (
            Solution,
            SolutionType,
        )

        case = self._make_case()
        # Solution exists but no root_cause_conclusion
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Fix config",
                longterm_fix="Set timeout to 30s",
            )
        ]
        result = assess_runbook_readiness(case)
        assert result.verdict == result.NOT_SUITABLE

    def test_runbook_readiness_ready_with_rich_solution(self):
        """Root cause + solution with commands + evidence → ready."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_runbook_readiness,
        )
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        case = self._make_case()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Connection pool timeout too low",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="1s timeout causes cascading failures under load",
        )
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Increase pool timeout",
                longterm_fix="Set pool.timeout=30s in application.yaml",
                commands=["kubectl edit configmap app-config"],
                implementation_steps=["Edit configmap", "Restart pods"],
                verification_method="Check p99 latency < 500ms for 30 min",
            )
        ]
        case.evidence = [
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="Timeout errors in application logs at 14:03 UTC",
                preprocessed_content="Error: connection timeout after 1000ms",
                content_size_bytes=1024,
                preprocessing_method="crime_scene_extraction",
                source_type="logs",
                form="document",
                collected_by="user_123",
                collected_at_turn=1,
            )
        ]
        result = assess_runbook_readiness(case)
        assert result.verdict == result.READY

    def test_runbook_readiness_needs_enrichment(self):
        """Root cause + commands but no evidence, no mitigation, no verification → needs enrichment."""
        from faultmaven.core.investigation.terminal_transitions import (
            assess_runbook_readiness,
        )
        from faultmaven.modules.case.contracts import (
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        case = self._make_case()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Config error",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Wrong pool timeout",
        )
        case.solutions = [
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Fix config",
                longterm_fix="Set timeout to 30s",
                commands=["kubectl edit configmap"],
            )
        ]
        # No evidence, no mitigation, no verification
        result = assess_runbook_readiness(case)
        assert result.verdict == result.NEEDS_ENRICHMENT

    def test_summary_guardrail_skips_too_few_messages(self):
        """Case with <4 messages → skip summary regardless of substance."""
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
        )

        case = MagicMock()
        case.case_id = "case_short"
        case.closure_reason = "inquiry_only"
        case.evidence = [MagicMock()]  # Has substance but too few messages
        case.hypotheses = {}
        case.description = "A real problem"
        case.progress.completed_milestones = []
        case.message_count = 2
        assert should_generate_terminal_summary(case) is False

    def test_summary_guardrail_skips_no_substance(self):
        """Case with enough messages but no substance → skip summary."""
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
        )

        case = MagicMock()
        case.case_id = "case_trivial"
        case.closure_reason = "abandoned"
        case.evidence = []
        case.hypotheses = {}
        case.description = ""
        case.progress.completed_milestones = []
        case.message_count = 8
        assert should_generate_terminal_summary(case) is False

    def test_summary_guardrail_generates_for_real_investigations(self):
        """Case with evidence and enough messages → generate summary."""
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
        )

        case = MagicMock()
        case.case_id = "case_real"
        case.closure_reason = "resolved"
        case.evidence = [MagicMock()]  # Has at least one evidence item
        case.hypotheses = {}
        case.description = "API latency issue"
        case.progress.completed_milestones = ["symptom_verified"]
        case.message_count = 8
        assert should_generate_terminal_summary(case) is True

    def test_summary_guardrail_skips_duplicates(self):
        """Duplicate cases → always skip regardless of content."""
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
        )

        case = MagicMock()
        case.case_id = "case_dup"
        case.closure_reason = "duplicate"
        case.evidence = [MagicMock(), MagicMock()]
        case.hypotheses = {"h1": MagicMock()}
        case.message_count = 10
        assert should_generate_terminal_summary(case) is False


@pytest.mark.asyncio
class TestRunbookSuggestion:
    """Test the combined runbook suggestion logic (content + dedup)."""

    async def test_suggest_when_ready_and_no_kb(self):
        """Content ready, no KB available → suggest (skip dedup)."""
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
        )
        from faultmaven.modules.case.contracts import (
            Case,
            InquiryData,
            ProblemVerification,
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        case = Case(
            user_id="u1",
            organization_id="o1",
            title="Pool timeout issue",
            description="DB queries timing out",
            status=CaseStatus.INVESTIGATING,
            problem_verification=ProblemVerification(
                symptom_statement="Timeout errors",
                severity="HIGH",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Timeout",
            ),
        )
        _make_resolution_ready(case)
        # Add commands + evidence so runbook readiness is fully READY
        case.solutions[0].commands = ["kubectl edit configmap"]
        case.solutions[0].verification_method = "Check p99 < 500ms for 30 min"
        from faultmaven.modules.case.contracts import Evidence, EvidenceCategory

        case.evidence = [
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="Timeout errors in logs",
                preprocessed_content="Error: timeout",
                content_size_bytes=100,
                preprocessing_method="crime_scene_extraction",
                source_type="logs",
                form="document",
                collected_by="u1",
                collected_at_turn=1,
            )
        ]

        result = await evaluate_runbook_suggestion(case, runbook_kb=None)
        assert result.verdict == RunbookSuggestion.SUGGEST

    async def test_existing_covers_when_high_similarity(self):
        """KB returns ≥85% match → existing covers."""
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
        )
        from faultmaven.modules.case.contracts import (
            Case,
            InquiryData,
            ProblemVerification,
            RootCauseConclusion,
            Solution,
            SolutionType,
        )

        case = Case(
            user_id="u1",
            organization_id="o1",
            title="Pool timeout issue",
            description="DB queries timing out",
            status=CaseStatus.INVESTIGATING,
            problem_verification=ProblemVerification(
                symptom_statement="Timeout errors",
                severity="HIGH",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Timeout",
            ),
        )
        _make_resolution_ready(case)
        case.solutions[0].commands = ["kubectl edit configmap"]

        # Mock runbook_kb that returns a high-similarity match
        mock_kb = AsyncMock()
        mock_kb.search_by_text = AsyncMock(
            return_value=[
                {"similarity_score": 0.92, "title": "Connection Pool Timeout Runbook"},
            ]
        )

        result = await evaluate_runbook_suggestion(case, runbook_kb=mock_kb)
        assert result.verdict == RunbookSuggestion.EXISTING_COVERS
        assert "Connection Pool Timeout Runbook" in result.message

    async def test_not_ready_when_content_insufficient(self):
        """No root cause, no solution → not ready (skip dedup entirely)."""
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
        )
        from faultmaven.modules.case.contracts import (
            Case,
            InquiryData,
            ProblemVerification,
        )

        case = Case(
            user_id="u1",
            organization_id="o1",
            title="Mystery issue",
            description="Something is wrong",
            status=CaseStatus.INVESTIGATING,
            problem_verification=ProblemVerification(
                symptom_statement="Unknown",
                severity="LOW",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Unknown",
            ),
        )
        # No root cause, no solution → NOT_SUITABLE → NOT_READY

        result = await evaluate_runbook_suggestion(case, runbook_kb=None)
        assert result.verdict == RunbookSuggestion.NOT_READY


class TestContradictingIntentCancelsPendingTransition:
    """Tests for Fix 1: Contradicting status_transition cancels pending_transition.

    When a pending_transition exists (e.g., CLOSED) and the user submits a different
    status_transition intent (e.g., INVESTIGATING), the pending transition should be
    cancelled and the new intent processed normally.
    """

    @pytest.mark.asyncio
    async def test_contradicting_intent_cancels_pending_close(
        self, mock_llm, mock_repo
    ):
        """User has pending CLOSE, then clicks 'Investigating' → pending cancelled."""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            description="Test",
            inquiry=InquiryData(
                thread_id="thread_123",
                proposed_problem_statement="API timeout errors",
                problem_statement_confirmed=True,
                decided_to_investigate=False,
            ),
        )

        # Set up a pending CLOSE transition
        case.pending_transition = {
            "to_status": "closed",
            "reason": "User wants to close",
            "summary": "Close without resolution",
            "evidence_ids": [],
            "proposed_at": "2026-04-23T00:00:00+00:00",
            "proposed_by": "agent",
        }

        # Mock LLM response for the new intent processing
        mock_response_content = json.dumps(
            {
                "agent_response": "Starting investigation.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User submits a contradicting status_transition intent
        result = await engine.process_turn(
            case,
            "I want to investigate this",
            intent_type="status_transition",
            intent_data={"to_status": "investigating"},
        )

        updated_case = result["case_updated"]

        # Pending transition should be cancelled
        assert updated_case.pending_transition is None

        # Case should NOT have transitioned to CLOSED
        assert updated_case.status != CaseStatus.CLOSED

    @pytest.mark.asyncio
    async def test_same_intent_still_confirms(self, mock_llm, mock_repo):
        """User has pending CLOSE, then clicks 'Close' again → treated as confirmation."""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
            evidence_service=MagicMock(),
        )

        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Test",
            problem_verification=ProblemVerification(
                symptom_statement="Test symptom",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Test symptom",
            ),
        )

        # Set up a pending CLOSE transition
        case.pending_transition = {
            "to_status": "closed",
            "reason": "User wants to close",
            "summary": "Close without resolution",
            "evidence_ids": [],
            "proposed_at": "2026-04-23T00:00:00+00:00",
            "proposed_by": "agent",
        }

        # User submits SAME status_transition intent → confirmation
        result = await engine.process_turn(
            case,
            "Close this case",
            intent_type="status_transition",
            intent_data={"to_status": "closed"},
        )

        updated_case = result["case_updated"]

        # Case should have transitioned to CLOSED (same intent = confirmation)
        assert updated_case.status == CaseStatus.CLOSED
        assert updated_case.pending_transition is None

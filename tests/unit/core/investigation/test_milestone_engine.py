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
from faultmaven.core.investigation.schemas import MilestoneUpdates
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


class TestMilestoneEngine:

    @pytest.mark.asyncio
    async def test_process_turn_investigating(self, mock_llm, mock_repo, base_case):
        """Test processing a turn in INVESTIGATING status"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response with structured output (including internal_reasoning)
        mock_response_content = json.dumps(
            {
                "agent_response": "I have verified the symptom.",
                "internal_reasoning": {
                    "evidence_analyzed": ["new_index_1"],
                    "conclusions": [
                        {
                            "observation": "Symptom observed",
                            "inference": "Problem confirmed",
                            "confidence": 0.9,
                        }
                    ],
                    "milestone_justifications": {
                        "symptom_verified": "Verified based on observed symptoms"
                    },
                    "uncertainties": [],
                },
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

    @pytest.mark.asyncio
    async def test_reasoning_validation_success(self, mock_llm, mock_repo, base_case):
        """Test successful reasoning validation when milestone completed with justification"""
        from faultmaven.core.investigation.milestone_engine import (
            validate_reasoning_first,
        )
        from faultmaven.core.investigation.schemas import (
            InternalReasoning,
            InvestigationResponse_Verification,
            ReasoningConclusion,
        )
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceSourceType,
            EvidenceForm,
        )

        # Add evidence to case
        base_case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test evidence",
                content_ref="test.log",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOG_FILE,
                collected_at=datetime.now(timezone.utc),
                collected_by="user_123",
                primary_purpose="Testing",
                preprocessed_content="Log content",
                content_size_bytes=100,
                preprocessing_method="manual",
                form=EvidenceForm.USER_INPUT,
                collected_at_turn=1,
            )
        )

        # Create response with proper internal reasoning
        response = InvestigationResponse_Verification(
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
            state_updates=InvestigationResponse_Verification.VerificationStateUpdate(
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
            InvestigationResponse_Verification,
            MilestoneUpdates,
        )

        # Create response with milestone but NO internal reasoning
        response = InvestigationResponse_Verification(
            agent_response="Symptom verified",
            state_updates=InvestigationResponse_Verification.VerificationStateUpdate(
                milestones=MilestoneUpdates(symptom_verified=True),
                outcome="milestone_completed",
            ),
        )

        is_valid, errors = validate_reasoning_first(response, base_case)
        assert not is_valid
        assert len(errors) > 0
        assert "internal_reasoning" in errors[0].lower()

    @pytest.mark.asyncio
    async def test_blocker_detection_triggers_degraded_mode(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that missing_critical_data triggers immediate degraded mode"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Mock LLM response with blocker detection
        mock_response_content = json.dumps(
            {
                "agent_response": "⚠️ Investigation limitations: Critical data is corrupted",
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
                        "triggers_degraded_mode": True,
                    },
                    "outcome": "conversation",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(base_case, "Check logs")

        # Verify degraded mode entered
        updated_case = result["case_updated"]
        assert updated_case.degraded_mode is not None
        assert updated_case.degraded_mode.is_active
        assert updated_case.degraded_mode.mode_type.value == "data_blocker"
        assert "Logs missing timestamps" in updated_case.degraded_mode.reason

        metadata = result["metadata"]
        # Degraded mode prevents progress
        assert metadata["progress_made"] is False

    @pytest.mark.asyncio
    async def test_evidence_quality_issues_logged(self, mock_llm, mock_repo, base_case):
        """Test that evidence quality issues are processed without error"""
        engine = MilestoneEngine(mock_llm, mock_repo)

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
    async def test_user_intent_detection_resolves_case(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that explicit user intent to resolve case triggers automatic transition"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Set case to INVESTIGATING status
        base_case.status = CaseStatus.INVESTIGATING

        # Mock LLM response (doesn't matter much, user intent happens before LLM call)
        mock_response_content = json.dumps(
            {
                "agent_response": "Case marked as resolved.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # Test various resolution phrases
        resolution_phrases = [
            "mark as resolved",
            "close this case",
            "case is resolved",
            "problem solved",
            "issue fixed",
            "solution worked",
        ]

        for phrase in resolution_phrases:
            # Reset case status using atomic_update to avoid validation Catch-22
            base_case.atomic_update(
                status=CaseStatus.INVESTIGATING,
                resolved_at=None,
                closed_at=None,
            )
            base_case.progress.solution_verified = False

            result = await engine.process_turn(base_case, phrase)

            updated_case = result["case_updated"]

            # Verify case transitioned to RESOLVED
            assert (
                updated_case.status == CaseStatus.RESOLVED
            ), f"Failed for phrase: '{phrase}'"
            assert (
                updated_case.progress.solution_verified is True
            ), f"Failed for phrase: '{phrase}'"
            assert (
                updated_case.resolved_at is not None
            ), f"Failed for phrase: '{phrase}'"
            assert updated_case.closed_at is not None, f"Failed for phrase: '{phrase}'"
            assert (
                updated_case.closure_reason == "resolved"
            ), f"Failed for phrase: '{phrase}'"

            # Verify status history updated
            assert (
                len(updated_case.status_history) > 0
            ), f"Failed for phrase: '{phrase}'"
            last_transition = updated_case.status_history[-1]
            assert (
                last_transition.from_status == CaseStatus.INVESTIGATING
            ), f"Failed for phrase: '{phrase}'"
            assert (
                last_transition.to_status == CaseStatus.RESOLVED
            ), f"Failed for phrase: '{phrase}'"

    @pytest.mark.asyncio
    async def test_user_intent_detection_case_insensitive(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that user intent detection is case-insensitive"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        base_case.status = CaseStatus.INVESTIGATING

        # Mock LLM response
        mock_response_content = json.dumps(
            {
                "agent_response": "Case marked as resolved.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # Test uppercase, mixed case, with punctuation
        result = await engine.process_turn(
            base_case, "CLOSE THIS CASE! The solution worked perfectly."
        )

        updated_case = result["case_updated"]
        assert updated_case.status == CaseStatus.RESOLVED
        assert updated_case.progress.solution_verified is True

    @pytest.mark.asyncio
    async def test_user_intent_detection_no_false_positives(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that user intent detection doesn't trigger on unrelated phrases"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        base_case.status = CaseStatus.INVESTIGATING

        # Mock LLM response
        mock_response_content = json.dumps(
            {
                "agent_response": "Continuing investigation.",
                "state_updates": {"outcome": "conversation"},
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # Test phrases that should NOT trigger resolution
        non_resolution_phrases = [
            "I want to resolve this issue",
            "How do I close cases?",
            "The problem is not solved yet",
            "Please help me solve this",
        ]

        for phrase in non_resolution_phrases:
            base_case.status = CaseStatus.INVESTIGATING
            base_case.progress.solution_verified = False

            result = await engine.process_turn(base_case, phrase)

            updated_case = result["case_updated"]

            # Verify case did NOT transition to RESOLVED
            assert (
                updated_case.status == CaseStatus.INVESTIGATING
            ), f"False positive for phrase: '{phrase}'"
            assert (
                updated_case.progress.solution_verified is False
            ), f"False positive for phrase: '{phrase}'"

    @pytest.mark.asyncio
    async def test_reasoning_validation_skipped_when_solution_verified_already_set(
        self, mock_llm, mock_repo, base_case
    ):
        """Test that reasoning validation is skipped when solution_verified was already set by user intent"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        base_case.status = CaseStatus.INVESTIGATING
        # Simulate user intent detection having already set solution_verified
        base_case.progress.solution_verified = True

        # Mock LLM response trying to complete OTHER milestones without justification
        # This simulates the Turn 4 error scenario reported by user
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

        # This should NOT fail with reasoning validation error
        # because solution_verified is already True (case transitioning to terminal)
        result = await engine.process_turn(base_case, "Closing the case")

        # Verify no validation error occurred
        assert result["case_updated"] is not None
        assert result["case_updated"].status == CaseStatus.RESOLVED

    @pytest.mark.asyncio
    async def test_complete_user_intent_to_terminal_transition_flow(
        self, mock_llm, mock_repo, base_case
    ):
        """Integration test: Complete flow from user intent → terminal state with LLM milestone attempts

        This test exercises the complete integration of:
        1. User intent detection (detects "close case" and sets solution_verified=True)
        2. LLM tries to complete OTHER milestones without justification
        3. Reasoning validation correctly skips validation (case.progress.solution_verified already True)
        4. Terminal transition executes (INVESTIGATING → RESOLVED)
        5. Case ends in correct terminal state with closure_reason="resolved"
        """
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Start in INVESTIGATING with some progress
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True
        base_case.progress.scope_assessed = True

        # Mock LLM response that tries to complete milestones without justification
        # This would normally fail reasoning validation, but should be skipped
        # because user intent detection sets solution_verified=True
        mock_response_content = json.dumps(
            {
                "agent_response": "Understood. I'll mark this case as resolved.",
                "state_updates": {
                    "milestones": {
                        "timeline_established": True,  # No justification provided!
                        "changes_identified": True,  # No justification provided!
                    },
                    "outcome": "milestone_completed",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User explicitly says "close this case" - triggers user intent detection
        result = await engine.process_turn(base_case, "close this case")

        updated_case = result["case_updated"]

        # ===== VERIFY COMPLETE TERMINAL TRANSITION =====

        # 1. Case transitioned to RESOLVED terminal state
        assert updated_case.status == CaseStatus.RESOLVED
        assert updated_case.is_terminal is True

        # 2. Terminal state timestamps set
        assert updated_case.resolved_at is not None
        assert updated_case.closed_at is not None

        # 3. Correct closure reason (resolved, not fast_track_kb_match)
        assert updated_case.closure_reason == "resolved"

        # 4. Solution milestone progression completed
        assert updated_case.progress.solution_verified is True

        # 5. Status history recorded transition
        assert len(updated_case.status_history) > 0
        last_transition = updated_case.status_history[-1]
        assert last_transition.from_status == CaseStatus.INVESTIGATING
        assert last_transition.to_status == CaseStatus.RESOLVED
        assert last_transition.triggered_by == "system"
        assert "automatic transition" in last_transition.reason.lower()

        # 6. No reasoning validation error (even though LLM didn't justify milestones)
        # This proves the fix at milestone_engine.py:144-159 works correctly
        assert "error" not in result["agent_response"].lower()

        # 7. LLM's milestone attempts were processed (best-effort)
        # Even though case is closing, we don't block the LLM's state updates
        assert updated_case.progress.timeline_established is True
        assert updated_case.progress.changes_identified is True

    @pytest.mark.asyncio
    async def test_user_intent_close_as_unresolved_transitions_to_closed(
        self, mock_llm, mock_repo, base_case
    ):
        """Test user intent: 'Close as unresolved' should transition to CLOSED, not RESOLVED

        This tests the fix for the bug where:
        - User says "Close this case as unresolved"
        - System was incorrectly transitioning to RESOLVED (with solution)
        - Should transition to CLOSED (without solution, abandoned)

        Pattern matching order:
        1. Abandonment patterns (highest priority) → CLOSED
        2. Resolution patterns (medium priority) → RESOLVED
        3. Ambiguous close patterns (lowest priority) → RESOLVED (backward compatible)
        """
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Start in INVESTIGATING with some progress
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True
        base_case.progress.scope_assessed = True

        # Mock LLM response using TerminalResponse schema
        # (case will be CLOSED after user intent detection, so TerminalResponse is used)
        mock_response_content = json.dumps(
            {
                "agent_response": "Understood. This case has been closed without resolution as requested.",
                "state_updates": {
                    "final_summary_update": "Case closed by user request without finding resolution.",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User explicitly says "close as unresolved" - should trigger CLOSED, not RESOLVED
        result = await engine.process_turn(base_case, "Close this case as unresolved")

        updated_case = result["case_updated"]

        # ===== VERIFY CORRECT TERMINAL STATE =====

        # 1. Case transitioned to CLOSED (NOT RESOLVED)
        assert updated_case.status == CaseStatus.CLOSED
        assert updated_case.status != CaseStatus.RESOLVED
        assert updated_case.is_terminal is True

        # 2. Terminal state timestamps set
        assert updated_case.closed_at is not None

        # 3. Correct closure reason (abandoned, not resolved)
        assert updated_case.closure_reason == "abandoned"

        # 4. Solution milestone NOT completed (no solution verified)
        assert updated_case.progress.solution_verified is False

        # 5. resolved_at should be None (not a resolution)
        assert updated_case.resolved_at is None

        # 6. Status history recorded transition to CLOSED
        assert len(updated_case.status_history) > 0
        last_transition = updated_case.status_history[-1]
        assert last_transition.from_status == CaseStatus.INVESTIGATING
        assert last_transition.to_status == CaseStatus.CLOSED
        assert "abandoned" in last_transition.reason.lower()

    @pytest.mark.asyncio
    async def test_user_intent_ambiguous_close_defaults_to_resolved(
        self, mock_llm, mock_repo, base_case
    ):
        """Test user intent: Ambiguous 'close this case' defaults to RESOLVED (backward compatible)

        When user says just "close this case" without clarification:
        - Defaults to RESOLVED (backward compatible behavior)
        - User must explicitly say "unresolved" or "without solution" to get CLOSED
        """
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Start in INVESTIGATING with some progress
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True

        # Mock LLM response
        mock_response_content = json.dumps(
            {
                "agent_response": "Case marked as resolved.",
                "state_updates": {
                    "outcome": "milestone_completed",
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        # User says ambiguous "close this case" - should default to RESOLVED
        result = await engine.process_turn(base_case, "close this case")

        updated_case = result["case_updated"]

        # Should transition to RESOLVED (backward compatible default)
        assert updated_case.status == CaseStatus.RESOLVED
        assert updated_case.closure_reason == "resolved"
        assert updated_case.progress.solution_verified is True
        assert updated_case.resolved_at is not None

    @pytest.mark.asyncio
    async def test_explicit_status_transition_inquiry_to_closed(
        self, mock_llm, mock_repo
    ):
        """Test explicit status_transition intent: INQUIRY → CLOSED via dropdown"""
        engine = MilestoneEngine(mock_llm, mock_repo)

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

        # 1. Status should be CLOSED
        assert updated_case.status == CaseStatus.CLOSED

        # 2. Closure reason should be "inquiry_only"
        assert updated_case.closure_reason == "inquiry_only"

        # 3. closed_at should be set
        assert updated_case.closed_at is not None

        # 4. Should NOT have gone through investigation
        assert updated_case.progress.symptom_verified is False

        # 5. Status history should record the transition
        assert len(updated_case.status_history) > 0
        last_transition = updated_case.status_history[-1]
        assert last_transition.from_status == CaseStatus.INQUIRY
        assert last_transition.to_status == CaseStatus.CLOSED

        # 6. Agent response should acknowledge closure
        assert "closed" in result["agent_response"].lower()

    @pytest.mark.asyncio
    async def test_explicit_status_transition_investigating_to_closed(
        self, mock_llm, mock_repo, base_case
    ):
        """Test explicit status_transition intent: INVESTIGATING → CLOSED via dropdown"""
        engine = MilestoneEngine(mock_llm, mock_repo)

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

        # 1. Status should be CLOSED
        assert updated_case.status == CaseStatus.CLOSED

        # 2. Closure reason should be "user_closed"
        assert updated_case.closure_reason == "user_closed"

        # 3. closed_at should be set
        assert updated_case.closed_at is not None

        # 4. Solution should NOT be verified
        assert updated_case.progress.solution_verified is False

        # 5. Status history should record the transition
        assert len(updated_case.status_history) > 0
        last_transition = updated_case.status_history[-1]
        assert last_transition.from_status == CaseStatus.INVESTIGATING
        assert last_transition.to_status == CaseStatus.CLOSED

        # 6. Agent response should acknowledge closure
        assert "closed" in result["agent_response"].lower()

    @pytest.mark.asyncio
    async def test_explicit_status_transition_inquiry_to_investigating(
        self, mock_llm, mock_repo
    ):
        """Test explicit status_transition intent: INQUIRY → INVESTIGATING via dropdown"""
        engine = MilestoneEngine(mock_llm, mock_repo)

        # Create case in INQUIRY status
        inquiry_case = Case(
            case_id="case_0987654321ab",  # 17 chars
            title="Test Inquiry to Investigating",
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

        # Mock LLM response for investigation kickoff
        mock_response_content = json.dumps(
            {
                "agent_response": "Let's start the investigation. I'll begin by verifying the symptom.",
                "state_updates": {
                    "outcome": "milestone_completed",
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

        # 1. Status should be INVESTIGATING
        assert updated_case.status == CaseStatus.INVESTIGATING

        # 2. Inquiry data should be updated
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.inquiry.decided_to_investigate is True

        # 3. Status history should record the transition
        assert len(updated_case.status_history) > 0
        last_transition = updated_case.status_history[-1]
        assert last_transition.from_status == CaseStatus.INQUIRY
        assert last_transition.to_status == CaseStatus.INVESTIGATING

        # 4. Should continue to LLM for kickoff message
        assert mock_llm.generate.called
        assert "investigation" in result["agent_response"].lower()

import json
from datetime import UTC, datetime, timezone
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
    InvestigationPath,
    InvestigationStage,
    PathSelection,
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
    # Fully-pathed INVESTIGATING case (Gate 2 already committed) so tests
    # exercising post-path behavior don't need to walk the pre-path
    # window themselves. Post-INV-19 redesign, an INVESTIGATING case CAN
    # legitimately have ``path_selection=None`` — that's the pre-path
    # window between INQUIRY transition and Gate 2 click. Tests that
    # need that shape construct it explicitly.
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
        path_selection=PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="ongoing high impact",
            alternate_path=InvestigationPath.ROOT_CAUSE,
            selected_by="user_123",
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
            EvidenceSourceType,
        )

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Add evidence to the case (required for milestone completion)
        base_case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test evidence",
                content_ref="test.log",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_at=datetime.now(UTC),
                collected_by="user_123",
                primary_purpose="Testing",
                preprocessed_content="Log content",
                content_size_bytes=100,
                preprocessing_method="manual",
                source_file_id=None,
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
                            "extract": "ERROR 500 at /api/checkout",
                            # Post-010: source_file_id required unless
                            # source_type=USER_DESCRIPTION.
                            "source_file_id": "file_aabb12345678",
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
            EvidenceSourceType,
        )

        # Add evidence to case
        base_case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test evidence",
                content_ref="test.log",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_at=datetime.now(UTC),
                collected_by="user_123",
                primary_purpose="Testing",
                preprocessed_content="Log content",
                content_size_bytes=100,
                preprocessing_method="manual",
                source_file_id=None,
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

    # NL transition detection (resolve/close phrases, case insensitivity,
    # false-positive guards) moved to InvestigationService._detect_transition_intent.
    # See tests/unit/modules/agent/test_transition_intent_detection.py for
    # the layer-correct coverage. The engine now sees only structured
    # status_transition intents — exercised by the
    # test_explicit_status_transition_* tests below.

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

        Drives the engine via structured status_transition intents (the
        same shape produced by both UI clicks and
        InvestigationService._detect_transition_intent for typed text):
        1. Turn N: status_transition → resolved → engine proposes transition
        2. Turn N+1: status_transition → resolved with pending exists →
           engine confirms and executes the transition.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Start in INVESTIGATING with resolution-ready case
        base_case.status = CaseStatus.INVESTIGATING
        base_case.progress.symptom_verified = True
        _make_resolution_ready(base_case)

        # ===== TURN N: structured RESOLVED request → proposes transition =====

        result_turn_n = await engine.process_turn(
            base_case,
            "the fix worked",
            intent_type="status_transition",
            intent_data={
                "from_status": CaseStatus.INVESTIGATING,
                "to_status": "resolved",
                "user_confirmed": False,
            },
        )

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

        # User explicitly confirms via structured status_transition
        # (same shape a UI confirm button would produce).
        result_turn_n1 = await engine.process_turn(
            updated_case,
            "yes, go ahead",
            intent_type="status_transition",
            intent_data={
                "from_status": CaseStatus.INVESTIGATING,
                "to_status": "resolved",
                "user_confirmed": True,
            },
        )

        final_case = result_turn_n1["case_updated"]

        # ===== VERIFY COMPLETE TERMINAL TRANSITION =====

        # 1. Case transitioned to RESOLVED terminal state
        assert final_case.status == CaseStatus.RESOLVED
        assert final_case.is_terminal is True

        # 2. Terminal state timestamps set
        assert final_case.resolved_at is not None
        assert final_case.closed_at is not None

        # 3. Correct closure reason — None for RESOLVED
        # (resolution itself is the categorization; closure_reason is a
        # sub-categorization of CLOSED only).
        assert final_case.closure_reason is None

        # 4. Solution milestone set via handshake confirmation
        assert final_case.progress.solution_verified is True

        # 5. Pending transition cleared after execution
        assert final_case.pending_transition is None

        # 6. Status history recorded transition with user as trigger
        assert len(final_case.action_history) > 0
        last_transition = final_case.action_history[-1]
        assert last_transition.from_status == CaseStatus.INVESTIGATING
        assert last_transition.to_status == CaseStatus.RESOLVED

    # Engine-level NL pattern matching for CLOSED-from-INVESTIGATING and the
    # "ambiguous close" clarification dialog were removed. NL transition
    # detection now lives in InvestigationService._detect_transition_intent
    # and structured CLOSED proposals are exercised by
    # test_explicit_status_transition_investigating_to_closed below.

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

        # 5. Canonical CLOSE confirm/decline pair emitted (alignment with
        #    agent-initiated path: every propose_transition surface offers
        #    deterministic COOPERATIVE confirmation suggestions).
        suggestions = result["suggested_follow_ups"]
        assert len(suggestions) == 2
        assert suggestions[0]["intent"] == {
            "type": "confirmation",
            "confirmation_value": True,
        }
        assert suggestions[1]["intent"] == {
            "type": "confirmation",
            "confirmation_value": False,
        }
        assert all(s["action_type"] == "COOPERATIVE" for s in suggestions)

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

        # 5. Canonical CLOSE confirm/decline pair emitted (alignment).
        suggestions = result["suggested_follow_ups"]
        assert len(suggestions) == 2
        assert suggestions[0]["intent"] == {
            "type": "confirmation",
            "confirmation_value": True,
        }
        assert suggestions[1]["intent"] == {
            "type": "confirmation",
            "confirmation_value": False,
        }
        assert all(s["action_type"] == "COOPERATIVE" for s in suggestions)

    @pytest.mark.asyncio
    async def test_explicit_status_transition_inquiry_to_investigating(
        self, mock_llm, mock_repo
    ):
        """Test explicit status_transition intent: INQUIRY → INVESTIGATING via dropdown.

        Post-redesign: INQUIRY → INVESTIGATING requires Gate 1 only.
        Gate 2 (path commit) fires later in INVESTIGATING after
        ``symptom_verified``. The dropdown injects a pre-composed
        message; the LLM emits confirmation; engine transitions.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
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

        # Turn 1 mock: LLM confirms problem AND emits urgency signals (which
        # the engine needs to compute the path recommendation).
        mock_response_content = json.dumps(
            {
                "agent_response": "Confirmed. Recommend root-cause analysis.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                    "preliminary_urgency": {
                        "level": "MEDIUM",
                        "is_ongoing": False,
                        "is_incident_report": False,
                        "impact_assessment": "Historical symptom",
                    },
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

        # Gate 1 closes → case transitions to INVESTIGATING immediately
        # (post-redesign: Gate 2 no longer gates the transition).
        # path_selection is None; Gate 2 will fire later after
        # symptom_verified.
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.inquiry.decided_to_investigate is True
        assert updated_case.path_selection is None

        # Should have called LLM (not bypassed)
        assert mock_llm.generate.called

        # Action history records the INQUIRY → INVESTIGATING transition.
        assert len(updated_case.action_history) > 0
        last_transition = updated_case.action_history[-1]
        assert last_transition.from_status == CaseStatus.INQUIRY
        assert last_transition.to_status == CaseStatus.INVESTIGATING

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
        """Dropdown RESOLVED on a thin case pivots to CLOSED.

        When the case lacks root cause / solution / evidence, the readiness
        verdict is SUGGEST_CLOSE. The engine pivots to a CLOSED proposal so
        the prompt the user sees and the COOPERATIVE confirmation pair both
        align with what they're actually being asked to do.
        """
        # base_case has no root_cause_conclusion, no solutions, no evidence
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        result = await engine.process_turn(
            case=base_case,
            user_message="The issue is resolved.",
            intent_type="status_transition",
            intent_data={"from_status": "investigating", "to_status": "resolved"},
        )

        # Case stays INVESTIGATING; pending transition pivots to CLOSED
        # (not RESOLVED) so the user's confirm click closes the case.
        assert result["case_updated"].status == CaseStatus.INVESTIGATING
        assert result["case_updated"].pending_transition is not None
        assert result["case_updated"].pending_transition["to_status"] == "closed"
        # No needs_info flag — pivot path doesn't carry resolve intent forward.
        assert not result["case_updated"].pending_transition.get("needs_info")

        # Response suggests closing
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


class TestGate2SetOnceSemantic:
    """Pin the behavioral promise: once ``case.path_selection`` is committed,
    a subsequent ``path_selection`` intent is silently ignored — the committed
    path is NOT overwritten. This is the only enforcement of "click once"
    semantics; without this test, a future maintainer could rewrite the
    warning-log path in ``milestone_engine`` to apply the re-commit, silently
    changing the semantic. Companion to INV-19 (existence == commit) pinned
    in test_investigation_gates.py::TestINV19PathSelectionAsCommitSignal.
    """

    @pytest.mark.asyncio
    async def test_gate2_re_commitment_is_ignored_after_first_click(
        self, mock_llm, mock_repo
    ):
        """A second path_selection intent on a case with a committed
        path_selection must NOT overwrite the first commit. The handler logs
        a warning and skips; the case retains the original path.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        # Construct an INVESTIGATING case past symptom_verified with a
        # path_selection ALREADY committed (e.g., user clicked Gate 2 on a
        # prior turn). Post-redesign, Gate 2 commits inside INVESTIGATING
        # after symptom_verified — INQUIRY never carries a path_selection.
        from faultmaven.modules.case.contracts import InvestigationProgress

        original_commit = PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale="historical low urgency",
            alternate_path=InvestigationPath.MITIGATION_FIRST,
            selected_by="user_123",
        )
        case = Case(
            case_id="case_aaaaaaaaaaab",
            title="Re-commit test",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Test description",
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                thread_id="thread_123",
                proposed_problem_statement="Test symptom",
            ),
            progress=InvestigationProgress(symptom_verified=True),
            path_selection=original_commit,
        )
        original_selected_at = case.path_selection.selected_at

        mock_llm.generate.return_value = json.dumps(
            {"agent_response": "noop", "state_updates": {}}
        )

        # Fire a second path_selection intent picking the OTHER path.
        result = await engine.process_turn(
            case=case,
            user_message="Actually let's do mitigation-first.",
            intent_type="path_selection",
            intent_data={"investigation_path": "mitigation_first"},
        )

        updated = result["case_updated"]

        # The original commit must survive — neither path nor metadata flipped.
        assert updated.path_selection is not None
        assert updated.path_selection.path == InvestigationPath.ROOT_CAUSE
        assert updated.path_selection.selected_at == original_selected_at


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
        """When LLM sets user_confirmed_investigation=True, Gate 1 closes via
        the LLM-emitted path (not a regex fallback). Slice 2 adds Gate 2 as
        a second confirmation; this test verifies Gate 1 fires correctly on
        the LLM path and that the case is then in the Gate-2-pending state.
        """
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
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

        # LLM correctly detects confirmation and supplies urgency signals
        mock_response_content = json.dumps(
            {
                "agent_response": "Starting investigation.",
                "state_updates": {
                    "user_confirmed_investigation": True,
                    "preliminary_urgency": {
                        "level": "HIGH",
                        "is_ongoing": True,
                        "is_incident_report": True,
                        "impact_assessment": "Users seeing connection drops",
                    },
                },
            }
        )
        mock_llm.generate.return_value = mock_response_content

        result = await engine.process_turn(case, "yes, proceed")

        updated_case = result["case_updated"]
        # Gate 1 closed via the LLM path (problem_statement_confirmed=True)
        # → case transitions to INVESTIGATING (post-redesign: Gate 2 fires
        # later in INVESTIGATING after symptom_verified, so it no longer
        # gates the INQUIRY → INVESTIGATING transition).
        assert updated_case.inquiry.problem_statement_confirmed is True
        assert updated_case.status == CaseStatus.INVESTIGATING
        assert updated_case.path_selection is None


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
            EvidenceSourceType,
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
                extract="Error: connection timeout after 1000ms",
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_aabb12345678",
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

    def test_summary_guardrail_passes_with_evidence_even_for_short_chat(self):
        """The gate is now substance-only: evidence is enough.

        Conversation depth (message_count) is intentionally NOT a gate
        signal — terminal Q&A turns inflate message_count, so including
        it would let the verdict flip after closure. Substance signals
        (evidence / hypotheses / milestones) are naturally frozen in
        CLOSED state and carry the meaningful "is there something to
        summarize?" signal on their own.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
        )

        case = MagicMock()
        case.case_id = "case_short"
        case.closure_reason = "inquiry_only"
        case.evidence = [MagicMock()]  # Substance present
        case.hypotheses = {}
        case.description = "A real problem"
        case.progress.completed_milestones = []
        case.message_count = 2  # No longer part of the gate
        assert should_generate_terminal_summary(case) is True

    def test_summary_guardrail_skips_no_substance(self):
        """Case with enough messages but no substance → skip summary."""
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
        )

        case = MagicMock()
        case.case_id = "case_trivial"
        case.closure_reason = "closed_after_investigation"
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
        # RESOLVED cases have closure_reason=None (resolution itself is
        # the categorization). The substance check below is what matters.
        case.closure_reason = None
        case.evidence = [MagicMock()]  # Has at least one evidence item
        case.hypotheses = {}
        case.description = "API latency issue"
        case.progress.completed_milestones = ["symptom_verified"]
        case.message_count = 8
        assert should_generate_terminal_summary(case) is True

    # test_summary_guardrail_skips_duplicates removed — the 'duplicate'
    # short-circuit was deleted when closure_reason was simplified to 3
    # engine-derived values. Duplicate-tracking, if reintroduced, will
    # use a separate field rather than an enum value the engine can't
    # reliably assign.

    def test_skip_reason_resolved_case_returns_none(self):
        """Resolved cases always generate a summary — skip reason is None
        regardless of conversation length or substance."""
        from faultmaven.core.investigation.terminal_transitions import (
            terminal_summary_skip_reason,
        )

        case = MagicMock()
        case.case_id = "case_resolved"
        case.status = CaseStatus.RESOLVED
        # Even with thin content, RESOLVED never skips
        case.evidence = []
        case.hypotheses = {}
        case.description = ""
        case.progress.completed_milestones = []
        case.message_count = 1
        assert terminal_summary_skip_reason(case) is None

    def test_skip_reason_closed_short_chat_with_evidence_returns_none(self):
        """Closed case with evidence — no skip note even if chat is short.

        message_count is no longer a gate signal (it's inflated by
        terminal Q&A), so a short conversation with real investigation
        substance still produces a summary.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            terminal_summary_skip_reason,
        )

        case = MagicMock()
        case.case_id = "case_short"
        case.status = CaseStatus.CLOSED
        case.evidence = [MagicMock()]
        case.hypotheses = {}
        case.description = "A real problem"
        case.progress.completed_milestones = []
        case.message_count = 2
        assert terminal_summary_skip_reason(case) is None

    def test_skip_reason_closed_no_substance(self):
        """Closed case with enough messages but no substance → skip reason
        mentions the missing substance signals."""
        from faultmaven.core.investigation.terminal_transitions import (
            terminal_summary_skip_reason,
        )

        case = MagicMock()
        case.case_id = "case_trivial"
        case.status = CaseStatus.CLOSED
        case.evidence = []
        case.hypotheses = {}
        case.description = ""
        case.progress.completed_milestones = []
        case.message_count = 8
        reason = terminal_summary_skip_reason(case)
        assert reason is not None
        assert "evidence" in reason
        assert "milestones" in reason

    def test_skip_reason_closed_with_substance_returns_none(self):
        """Closed case that meets the heuristic — no skip note (summary will
        be auto-generated)."""
        from faultmaven.core.investigation.terminal_transitions import (
            terminal_summary_skip_reason,
        )

        case = MagicMock()
        case.case_id = "case_real"
        case.status = CaseStatus.CLOSED
        case.evidence = [MagicMock()]
        case.hypotheses = {}
        case.description = "API latency issue"
        case.progress.completed_milestones = ["symptom_verified"]
        case.message_count = 8
        assert terminal_summary_skip_reason(case) is None


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
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceSourceType,
        )

        case.evidence = [
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="Timeout errors in logs",
                extract="Error: timeout",
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_aabb12345678",
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

        # Set up a pending CLOSE transition (post-simplification shape:
        # closure_reason is engine-derived enum; reason/proposed_by removed).
        case.pending_transition = {
            "to_status": "closed",
            "summary": "Close without resolution",
            "evidence_ids": [],
            "proposed_at": "2026-04-23T00:00:00+00:00",
            "closure_reason": "closed_after_investigation",
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

        # Set up a pending CLOSE transition (post-simplification shape:
        # closure_reason is engine-derived enum; reason/proposed_by removed).
        case.pending_transition = {
            "to_status": "closed",
            "summary": "Close without resolution",
            "evidence_ids": [],
            "proposed_at": "2026-04-23T00:00:00+00:00",
            "closure_reason": "closed_after_investigation",
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


class TestRootCauseConclusionPersistence:
    """Verify that root_cause_conclusion from LLM output is saved to the case.

    Added when _apply_investigation_updates gained step 1a: the LLM can now
    populate root_cause_conclusion in state_updates and the backend must save
    it with correct field-name mapping (evidence_ids → evidence_basis) and
    must derive confidence_level from likelihood via ConfidenceLevel.from_score.
    """

    @pytest.mark.asyncio
    async def test_root_cause_conclusion_saved_from_llm_output(
        self, mock_llm, mock_repo, base_case
    ):
        """LLM-provided root_cause_conclusion is persisted with correct field mapping."""
        from faultmaven.modules.case.contracts import ConfidenceLevel

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        base_case.progress.symptom_verified = True
        base_case.progress.root_cause_identified = True

        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": "Root cause identified.",
                "internal_reasoning": {
                    "evidence_analyzed": [],
                    "conclusions": [
                        {
                            "observation": "Config mismatch found",
                            "inference": "Root cause confirmed",
                            "confidence": 0.85,
                        }
                    ],
                    "milestone_justifications": {},
                    "uncertainties": [],
                },
                "state_updates": {
                    "root_cause_conclusion": {
                        "root_cause": "max_connections set to 10 instead of 100",
                        "mechanism": "Connection pool exhaustion caused cascading timeouts",
                        "evidence_ids": ["ev_aabbccdd1122"],
                        "likelihood": 0.85,
                    },
                    "outcome": "conversation",
                },
            }
        )

        result = await engine.process_turn(base_case, "What is the root cause?")
        updated_case = result["case_updated"]

        rcc = updated_case.root_cause_conclusion
        assert rcc is not None
        assert rcc.root_cause == "max_connections set to 10 instead of 100"
        assert rcc.mechanism == "Connection pool exhaustion caused cascading timeouts"
        # Schema uses evidence_ids; domain model uses evidence_basis
        assert rcc.evidence_basis == ["ev_aabbccdd1122"]
        assert rcc.likelihood == 0.85
        # confidence_level must be derived from likelihood (0.85 → CONFIDENT)
        assert rcc.confidence_level == ConfidenceLevel.CONFIDENT

    @pytest.mark.asyncio
    async def test_root_cause_conclusion_absent_is_noop(
        self, mock_llm, mock_repo, base_case
    ):
        """When LLM omits root_cause_conclusion, case.root_cause_conclusion is unchanged."""
        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        assert base_case.root_cause_conclusion is None

        mock_llm.generate.return_value = json.dumps(
            {
                "agent_response": "Still investigating.",
                "state_updates": {"outcome": "conversation"},
            }
        )

        result = await engine.process_turn(base_case, "Any progress?")
        updated_case = result["case_updated"]

        assert updated_case.root_cause_conclusion is None


class TestTerminalTransitionPendingActionCleanup:
    """Verify that _execute_resolved_transition cleans up orphaned pending actions.

    Added when terminal_transitions.py gained pending-action cleanup to close
    the audit gap on the TREATMENT failure path: when solution_accepted is already
    True and a revised fix is proposed, no stage-gate fires for the new
    ProposedAction, so it must be marked accepted at resolution time.
    """

    def _make_case(self):
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
                proposed_problem_statement="Test symptom",
            ),
        )

    def test_pending_action_marked_accepted_on_resolution(self):
        """A pending ProposedAction is accepted and audited when the case resolves."""
        from faultmaven.core.investigation.terminal_transitions import (
            _execute_resolved_transition,
        )
        from faultmaven.modules.case.contracts import (
            InvestigationActionType,
            ProposedAction,
        )

        case = self._make_case()
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Revised fix after first attempt failed",
                proposed_in_turn=3,
                status="pending",
            )
        )

        _execute_resolved_transition(case, "user_123")

        assert case.proposed_actions[0].status == "accepted"
        assert len(case.action_attempts) == 1
        attempt = case.action_attempts[0]
        assert attempt.compliance_detected is True
        assert attempt.compliance_confidence == 1.0

    def test_already_accepted_actions_not_reprocessed(self):
        """Actions already in accepted state are untouched during resolution."""
        from faultmaven.core.investigation.terminal_transitions import (
            _execute_resolved_transition,
        )
        from faultmaven.modules.case.contracts import (
            InvestigationActionType,
            ProposedAction,
        )

        case = self._make_case()
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Original fix — already accepted when solution_accepted fired",
                proposed_in_turn=2,
                status="accepted",
            )
        )

        _execute_resolved_transition(case, "user_123")

        # Status unchanged, and no ActionAttempt created for it
        assert case.proposed_actions[0].status == "accepted"
        assert len(case.action_attempts) == 0

    def test_mixed_actions_only_pending_cleaned_up(self):
        """With one accepted and one pending action, only the pending one is processed."""
        from faultmaven.core.investigation.terminal_transitions import (
            _execute_resolved_transition,
        )
        from faultmaven.modules.case.contracts import (
            InvestigationActionType,
            ProposedAction,
        )

        case = self._make_case()
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Original fix",
                proposed_in_turn=2,
                status="accepted",
            )
        )
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Revised fix from failure path",
                proposed_in_turn=4,
                status="pending",
            )
        )

        _execute_resolved_transition(case, "user_123")

        assert case.proposed_actions[0].status == "accepted"  # unchanged
        assert case.proposed_actions[1].status == "accepted"  # cleaned up
        assert len(case.action_attempts) == 1
        assert case.action_attempts[0].action_id == case.proposed_actions[1].action_id


# =============================================================================
# Tests: needs_info-followup pivot proposes CLOSED (loop-breaker fix)
# Surfaced 2026-05-24 in Run 25 (case_e766aa6b658f T12-T17): when readiness
# fails on a follow-up evaluation, the engine used to emit a close-suggestion
# message but never propose the close transition itself — so the user's
# "yes" had nothing to confirm, producing a 5-turn stuck loop. The fix:
# both SUGGEST_CLOSE and NEEDS_INFO-second-pass branches now propose CLOSED
# alongside the message, mirroring the dropdown path's behavior.
# =============================================================================


class TestNeedsInfoFollowupProposesClose:
    """Verifies the needs_info-followup path proposes a CLOSED transition
    in both pivot branches (SUGGEST_CLOSE and NEEDS_INFO second-pass),
    so subsequent user confirmations actually fire instead of looping."""

    def _make_engine(self):
        mock_llm = MockLLMProvider()
        mock_llm.generate = AsyncMock()
        mock_repo = MagicMock()
        mock_repo.save = AsyncMock(side_effect=lambda c: c)
        return MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

    def _make_case_with_pending_resolve_needs_info(self):
        """Case in INVESTIGATING with a pending RESOLVED transition
        marked needs_info=True (the state the followup path re-evaluates)."""
        case = Case(
            case_id="case_aabbccdd1122",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Test",
            problem_verification=ProblemVerification(
                symptom_statement="Issue ongoing",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                proposed_problem_statement="Issue ongoing",
            ),
        )
        case.pending_transition = {
            "to_status": "resolved",
            "summary": "Awaiting user-provided detail",
            "needs_info": True,
            "evidence_ids": [],
            "proposed_at": datetime.now(UTC).isoformat(),
        }
        return case

    @pytest.mark.asyncio
    async def test_suggest_close_branch_proposes_close(self):
        """Re-evaluation returns SUGGEST_CLOSE → engine proposes CLOSED so
        the user's next confirmation fires. Pre-fix this branch only emitted
        the close suggestions without a pending transition to confirm."""
        engine = self._make_engine()
        case = self._make_case_with_pending_resolve_needs_info()
        # No root_cause, no solutions, no evidence → SUGGEST_CLOSE on
        # re-eval (critical_missing >= 2 AND not has_evidence).
        metadata = {}
        await engine._check_automatic_transitions(case, metadata, user_message="ok")

        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "closed"
        # closure_reason is auto-derived; no mitigation path → closed_after_investigation
        assert case.pending_transition["closure_reason"] == "closed_after_investigation"
        assert metadata.get("resolution_suggest_close") is True
        assert metadata.get("transition_proposed_this_turn") is True

    @pytest.mark.asyncio
    async def test_needs_info_second_pass_proposes_close(self):
        """Re-evaluation returns NEEDS_INFO (root cause present, solution
        missing, evidence present) → engine proposes CLOSED on second ask
        with the hardcoded close-suggestion message."""
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceSourceType,
            RootCauseConclusion,
        )

        engine = self._make_engine()
        case = self._make_case_with_pending_resolve_needs_info()
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Misconfigured connection pool",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Pool timeout too short",
        )
        case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_aabbccdd1122",
                primary_purpose="Test",
                collected_by="user_123",
                collected_at_turn=1,
                collected_at=datetime.now(UTC),
            )
        )
        # case.solutions stays empty → readiness verdict is NEEDS_INFO
        # (one critical missing — "solution")
        metadata = {}
        await engine._check_automatic_transitions(case, metadata, user_message="ok")

        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "closed"
        assert case.pending_transition["closure_reason"] == "closed_after_investigation"
        assert metadata.get("resolution_suggest_close") is True
        assert metadata.get("transition_proposed_this_turn") is True
        # The hardcoded second-ask message is what the user sees
        assert (
            "Without a documented solution" in metadata["resolution_readiness_message"]
        )

    @pytest.mark.asyncio
    async def test_ready_branch_unchanged(self):
        """Control: if re-eval is READY, the path clears needs_info and
        keeps the pending RESOLVED — must not have been broken by the
        SUGGEST_CLOSE/NEEDS_INFO branch changes."""
        engine = self._make_engine()
        case = self._make_case_with_pending_resolve_needs_info()
        _make_resolution_ready(case)  # adds root_cause + Solution row
        # Also need evidence to fully satisfy readiness
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceSourceType,
        )

        case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_aabbccdd1122",
                primary_purpose="Test",
                collected_by="user_123",
                collected_at_turn=1,
                collected_at=datetime.now(UTC),
            )
        )
        metadata = {}
        await engine._check_automatic_transitions(case, metadata, user_message="ok")

        # Pending transition stays as RESOLVED, needs_info cleared
        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "resolved"
        assert case.pending_transition.get("needs_info") is False
        assert metadata.get("resolution_ready_for_confirmation") is True
        # The propose-close path did NOT fire
        assert metadata.get("resolution_suggest_close") is not True

    @pytest.mark.asyncio
    async def test_closure_reason_mitigation_sufficient_on_gate3(self):
        """When the case is at Gate 3 (mitigation_first path, mitigation
        verified, RCA not yet committed), the auto-derived closure_reason
        is 'mitigation_sufficient' rather than 'closed_after_investigation'."""
        from faultmaven.modules.case.contracts import (
            Evidence,
            EvidenceCategory,
            EvidenceSourceType,
            InvestigationPath,
            PathSelection,
            RootCauseConclusion,
        )

        engine = self._make_engine()
        case = self._make_case_with_pending_resolve_needs_info()
        case.path_selection = PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale="ongoing + high",
            selected_by="u1",  # path existence = Gate 2 committed
            mitigation_completed_at_turn=5,
            rca_after_mitigation_confirmed=False,  # Gate 3 still open
        )
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Misconfigured connection pool",
            confidence_level="verified",
            likelihood=0.9,
            mechanism="Pool timeout too short",
        )
        case.evidence.append(
            Evidence(
                evidence_id="ev_001122334455",
                summary="Test",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                source_file_id="file_aabbccdd1122",
                primary_purpose="Test",
                collected_by="user_123",
                collected_at_turn=1,
                collected_at=datetime.now(UTC),
            )
        )
        # No Solution row → readiness verdict NEEDS_INFO
        metadata = {}
        await engine._check_automatic_transitions(case, metadata, user_message="ok")

        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "closed"
        # Gate 3 open → closure_reason is mitigation_sufficient
        assert case.pending_transition["closure_reason"] == "mitigation_sufficient"


class TestCreateTurnRecordSystemFeedbackTruncation:
    """Regression: ``TurnProgress.system_feedback`` has a 1000-char Pydantic
    cap. Multiple backstops (path-conditional emission rejection, milestone
    ordering guards, data-quality blockers, etc.) each append independently
    to ``metadata["system_feedback"]``. When 4+ backstops fire in one turn
    the accumulated text overflowed the cap and crashed the turn save with
    a 500. ``_create_turn_record`` is the single chokepoint where all
    system_feedback values are written into the TurnProgress, so truncation
    happens there."""

    def _make_engine(self):
        mock_llm = MockLLMProvider()
        mock_llm.generate = AsyncMock()
        mock_repo = MagicMock()
        mock_repo.save = AsyncMock(side_effect=lambda c: c)
        return MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

    def test_short_system_feedback_passes_through_unchanged(self):
        from faultmaven.modules.case.contracts import TurnOutcome

        engine = self._make_engine()
        short_feedback = (
            "MILESTONE ORDER ERROR: mitigation_verified without acceptance."
        )
        record = engine._create_turn_record(
            turn_number=1,
            milestones_completed=[],
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            user_message="hi",
            agent_response="ok",
            system_feedback=short_feedback,
        )
        assert record.system_feedback == short_feedback

    def test_oversized_system_feedback_is_truncated_with_marker(self):
        """Simulate the multi-backstop pile-up from the eval failure:
        four path-conditional rejection messages concatenated push past
        1000 chars. Without truncation, TurnProgress validation crashes."""
        from faultmaven.modules.case.contracts import TurnOutcome

        engine = self._make_engine()
        # Each path-conditional rejection message is ~300-400 chars.
        # Four firing in one turn (rejected RCA milestone + causal_evidence
        # + hypotheses_to_add + solutions_to_add in pre_path_investigating)
        # easily exceeds 1000.
        oversized_feedback = (
            "PATH-CONDITIONAL MILESTONE ERROR: You set ``root_cause_identified=True`` "
            "in a state where the _PRE_PATH_DIAGNOSIS_BLOCK prompt directive forbids "
            "RCA-side milestones. Root-cause work is deferred until the user commits "
            "an investigation path (Gate 2). Do not re-emit ``root_cause_identified=True`` "
            "until the case reaches a state where RCA is in scope.\n"
            "PATH-CONDITIONAL EMISSION ERROR: You classified evidence as "
            "``causal_evidence`` in a state where the _PRE_PATH_DIAGNOSIS_BLOCK "
            "prompt directive explicitly forbids this category. Causal claims "
            "presuppose a hypothesis to attach to (INV-17), and hypothesis formation "
            "is gated in this state. Use ``symptom_evidence`` instead.\n"
            "PATH-CONDITIONAL EMISSION ERROR: You emitted ``hypotheses_to_add`` "
            "(2 record(s)) in a state where the _PRE_PATH_DIAGNOSIS_BLOCK prompt "
            "directive forbids structured hypothesis emission. Hypothesis formation "
            "is deferred until the case reaches a state where it is in scope.\n"
            "PATH-CONDITIONAL EMISSION ERROR: You emitted ``solutions_to_add`` "
            "(1 record(s)) before the user committed an investigation path (Gate 2). "
            "Mitigation and solution proposals only belong inside the chosen path."
        )
        assert len(oversized_feedback) > 1000  # sanity: the test setup is real

        record = engine._create_turn_record(
            turn_number=6,
            milestones_completed=[],
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            user_message="please continue",
            agent_response="working on it",
            system_feedback=oversized_feedback,
        )
        # Truncated to fit within the Pydantic max_length=1000 cap.
        assert record.system_feedback is not None
        assert len(record.system_feedback) <= 1000
        assert "[truncated]" in record.system_feedback
        # First backstop's content is preserved (truncation is tail-cut).
        assert "PATH-CONDITIONAL MILESTONE ERROR" in record.system_feedback

    def test_none_system_feedback_remains_none(self):
        from faultmaven.modules.case.contracts import TurnOutcome

        engine = self._make_engine()
        record = engine._create_turn_record(
            turn_number=1,
            milestones_completed=[],
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            user_message="hi",
            agent_response="ok",
            system_feedback=None,
        )
        assert record.system_feedback is None

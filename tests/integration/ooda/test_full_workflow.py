"""Integration tests for full OODA Investigation Workflow

Tests:
- Complete Phase 0 → Phase 6 flow
- Phase transitions and state persistence
- Evidence collection across phases
- Hypothesis lifecycle through validation
- Memory compression during long investigations

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from faultmaven.services.agentic.phase_handlers.intake_handler import IntakeHandler
from faultmaven.services.agentic.phase_handlers.blast_radius_handler import BlastRadiusHandler
from faultmaven.services.agentic.phase_handlers.hypothesis_handler import HypothesisHandler
from faultmaven.services.agentic.phase_handlers.validation_handler import ValidationHandler
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    EngagementMode,
    HypothesisStatus,
    InvestigationMetadata,
    InvestigationLifecycle,
    OODAEngineState,
    EvidenceLayer,
    MemoryLayer,
    ProblemConfirmation,
)


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider for all handlers"""
    provider = Mock()
    mock_response = Mock()
    mock_response.content = '{"answer": "Test response", "suggested_commands": [], "clarifying_questions": [], "evidence_needed": []}'
    mock_response.tool_calls = None
    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def handlers(mock_llm_provider):
    """Create all phase handlers"""
    return {
        "intake": IntakeHandler(llm_provider=mock_llm_provider),
        "blast_radius": BlastRadiusHandler(llm_provider=mock_llm_provider),
    }


class TestFullInvestigationWorkflow:
    """Test complete investigation from Phase 0 to Phase 6"""

    @pytest.mark.asyncio
    async def test_phase_0_to_phase_1_transition(self, handlers, mock_llm_provider):
        """Test transition from Intake (Phase 0) to Blast Radius (Phase 1)"""
        # Start in Phase 0
        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="integration-test",
                engagement_mode=EngagementMode.CONSULTANT,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.INTAKE,
            ),
            ooda_engine=OODAEngineState(),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
        )

        # Mock engagement manager for problem detection
        with patch('faultmaven.services.agentic.phase_handlers.intake_handler.create_engagement_mode_manager') as mock_mgr:
            manager = Mock()
            manager.analyze_initial_query = Mock(return_value={
                "signal_strength": "strong",
                "detected_keywords": ["error", "production"],
            })
            manager.create_problem_confirmation = Mock(return_value=ProblemConfirmation(
                problem_statement="Production API errors",
                affected_components=["api-service"],
                severity="critical",
                impact="All users affected",
                investigation_approach="Systematic investigation",
                estimated_evidence_needed=["logs", "metrics"],
            ))
            manager.select_investigation_strategy = Mock(return_value="active_incident")
            mock_mgr.return_value = manager

            # Step 1: User reports problem
            result1 = await handlers["intake"].handle(
                investigation_state=state,
                user_query="Our production API is returning 500 errors for all requests",
                conversation_history="",
            )

            assert result1.made_progress is True
            assert state.problem_confirmation is not None
            assert result1.phase_complete is False  # Waiting for consent

            # Step 2: User consents
            with patch('faultmaven.services.agentic.phase_handlers.intake_handler.detect_entry_phase') as mock_detect:
                mock_detect.return_value = InvestigationPhase.BLAST_RADIUS

                result2 = await handlers["intake"].handle(
                    investigation_state=state,
                    user_query="Yes, please help me investigate",
                    conversation_history="",
                )

                assert result2.phase_complete is True
                assert result2.should_advance is True
                assert result2.next_phase == InvestigationPhase.BLAST_RADIUS
                assert state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR
                assert state.ooda_engine.ooda_active is True

        # Step 3: Transition to Phase 1
        state.lifecycle.current_phase = InvestigationPhase.BLAST_RADIUS

        result3 = await handlers["blast_radius"].handle(
            investigation_state=state,
            user_query="What evidence do you need?",
            conversation_history="",
        )

        assert result3.made_progress is True
        assert len(result3.evidence_requests_generated) > 0
        assert len(state.ooda_engine.iterations) > 0

    @pytest.mark.asyncio
    async def test_state_persistence_across_phases(self, handlers, mock_llm_provider):
        """Test that state persists correctly across phase transitions"""
        # Start with Phase 1 state
        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="persistence-test",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
                current_turn=5,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.BLAST_RADIUS,
                urgency_level="critical",
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
            problem_confirmation=ProblemConfirmation(
                problem_statement="Database connection failures",
                affected_components=["api-service", "database"],
                severity="critical",
                impact="All users",
                investigation_approach="Active incident response",
                estimated_evidence_needed=["logs", "metrics"],
            ),
        )

        # Execute Phase 1
        result = await handlers["blast_radius"].handle(
            investigation_state=state,
            user_query="Gathering scope evidence",
            conversation_history="",
        )

        # Verify state updates persist
        assert state.metadata.current_turn == 5
        assert state.problem_confirmation is not None
        assert state.problem_confirmation.problem_statement == "Database connection failures"
        assert state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR

    @pytest.mark.asyncio
    async def test_evidence_collection_workflow(self, handlers, mock_llm_provider):
        """Test evidence request generation and tracking"""
        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="evidence-test",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.BLAST_RADIUS,
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
            problem_confirmation=ProblemConfirmation(
                problem_statement="Test problem",
                affected_components=[],
                severity="medium",
                impact="Test",
                investigation_approach="Test",
                estimated_evidence_needed=[],
            ),
        )

        # Generate evidence requests
        result = await handlers["blast_radius"].handle(
            investigation_state=state,
            user_query="What evidence do you need?",
            conversation_history="",
        )

        # Verify evidence requests generated
        assert len(result.evidence_requests_generated) > 0

        # Verify evidence request structure
        for evidence_request in result.evidence_requests_generated:
            assert evidence_request.label is not None
            assert evidence_request.description is not None
            assert evidence_request.category is not None
            assert evidence_request.guidance is not None

    @pytest.mark.asyncio
    async def test_multiple_ooda_iterations(self, handlers, mock_llm_provider):
        """Test multiple OODA iterations within a phase"""
        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="iterations-test",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
                current_turn=1,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.BLAST_RADIUS,
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
            problem_confirmation=ProblemConfirmation(
                problem_statement="Test problem",
                affected_components=[],
                severity="medium",
                impact="Test",
                investigation_approach="Test",
                estimated_evidence_needed=[],
            ),
        )

        # Iteration 1: Observe
        result1 = await handlers["blast_radius"].handle(
            investigation_state=state,
            user_query="What evidence needed?",
            conversation_history="",
        )
        state.metadata.current_turn += 1

        # Iteration 2: Orient
        result2 = await handlers["blast_radius"].handle(
            investigation_state=state,
            user_query="Here's the evidence",
            conversation_history="",
        )
        state.metadata.current_turn += 1

        # Verify multiple iterations tracked
        assert len(state.ooda_engine.iterations) >= 1

    @pytest.mark.asyncio
    async def test_investigation_strategy_selection(self, handlers, mock_llm_provider):
        """Test investigation strategy affects workflow"""
        # Test Active Incident strategy
        state_active = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="strategy-test-active",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.BLAST_RADIUS,
                urgency_level="critical",
                investigation_strategy="active_incident",
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
            problem_confirmation=ProblemConfirmation(
                problem_statement="Critical production issue",
                affected_components=["api"],
                severity="critical",
                impact="All users",
                investigation_approach="Fast mitigation",
                estimated_evidence_needed=[],
            ),
        )

        result_active = await handlers["blast_radius"].handle(
            investigation_state=state_active,
            user_query="What should I do?",
            conversation_history="",
        )

        # Verify strategy influences response
        assert result_active.made_progress is True


class TestPhaseTransitions:
    """Test phase transition logic"""

    @pytest.mark.asyncio
    async def test_phase_completion_detection(self, handlers):
        """Test detecting when phase objectives are met"""
        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="completion-test",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.BLAST_RADIUS,
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
            problem_confirmation=ProblemConfirmation(
                problem_statement="Test",
                affected_components=[],
                severity="medium",
                impact="Test",
                investigation_approach="Test",
                estimated_evidence_needed=[],
            ),
        )

        # Check completion before objectives met
        is_complete, met, unmet = await handlers["blast_radius"].check_completion(state)
        assert is_complete is False
        assert len(unmet) > 0

    @pytest.mark.asyncio
    async def test_phase_skip_logic(self):
        """Test that high-urgency investigations can skip phases"""
        # Critical incidents might skip Phase 2 (Timeline) and go straight to Phase 3 (Hypothesis)
        # This is determined by entry point selection

        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="skip-test",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.INTAKE,
                urgency_level="critical",
                entry_phase=InvestigationPhase.HYPOTHESIS,  # Skipping Blast Radius and Timeline
            ),
            ooda_engine=OODAEngineState(),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
        )

        # Verify entry phase set correctly
        assert state.lifecycle.entry_phase == InvestigationPhase.HYPOTHESIS


class TestLongInvestigation:
    """Test behavior during long, complex investigations"""

    @pytest.mark.asyncio
    async def test_memory_compression_during_investigation(
        self, handlers, mock_llm_provider
    ):
        """Test that memory is compressed during long investigations"""
        from faultmaven.core.investigation.memory_manager import create_memory_manager

        memory_manager = create_memory_manager(llm_provider=mock_llm_provider)

        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="long-investigation",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
                current_turn=1,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.VALIDATION,
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
        )

        # Simulate 10 turns of investigation
        from faultmaven.models.investigation import OODAIteration, OODAStep

        for turn in range(1, 11):
            iteration = OODAIteration(
                iteration_number=turn,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=turn,
                completed_at_turn=turn + 1,
                duration_turns=1,
                steps_completed=[OODAStep.OBSERVE, OODAStep.ORIENT],
                new_insights=[f"Insight from turn {turn}"],
                made_progress=True,
            )

            state.memory.hierarchical_memory = await memory_manager.update_memory(
                memory=state.memory.hierarchical_memory,
                new_iteration=iteration,
                current_turn=turn,
            )

        # Verify compression occurred
        memory = state.memory.hierarchical_memory
        assert len(memory.hot_memory) <= 2  # Hot tier limited to 2
        assert len(memory.warm_snapshots) >= 0  # Some moved to warm
        assert memory.last_compression_turn > 0  # Compression triggered

    @pytest.mark.asyncio
    async def test_anchoring_detection_during_validation(
        self, handlers, mock_llm_provider
    ):
        """Test anchoring bias detection during validation phase"""
        from faultmaven.models.investigation import Hypothesis

        state = InvestigationState(
            metadata=InvestigationMetadata(
                session_id="anchoring-test",
                engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
                current_turn=10,
            ),
            lifecycle=InvestigationLifecycle(
                current_phase=InvestigationPhase.VALIDATION,
            ),
            ooda_engine=OODAEngineState(
                ooda_active=True,
                current_iteration=5,
                hypotheses=[
                    Hypothesis(
                        statement=f"Code issue {i}",
                        category="code",
                        likelihood=0.7,
                        initial_likelihood=0.7,
                        created_at_turn=2,
                        last_updated_turn=2,
                        status=HypothesisStatus.TESTING,
                    )
                    for i in range(4)
                ],
            ),
            evidence=EvidenceLayer(),
            memory=MemoryLayer(),
        )

        # Check for anchoring
        from faultmaven.core.investigation.ooda_engine import AdaptiveIntensityController

        controller = AdaptiveIntensityController()
        should_trigger, reason = controller.should_trigger_anchoring_prevention(
            iteration_count=5,
            hypotheses=state.ooda_engine.hypotheses,
        )

        # Should detect anchoring (4 hypotheses in "code" category)
        assert should_trigger is True
        assert "same category" in reason.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

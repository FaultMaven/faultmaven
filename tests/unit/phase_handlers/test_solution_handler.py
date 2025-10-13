"""Unit tests for SolutionHandler (Phase 5)

Tests:
- Solution proposal from validated hypothesis
- Fix implementation guidance
- Solution verification and rollback planning
- Full OODA cycle execution (Decide, Act, Orient)
- Multiple solution scenarios
- Error handling for LLM failures

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from faultmaven.services.agentic.phase_handlers.solution_handler import SolutionHandler
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    InvestigationStrategy,
    EngagementMode,
    Hypothesis,
    HypothesisStatus,
    InvestigationMetadata,
    InvestigationLifecycle,
    OODAEngineState,
    OODAStep,
    OODAIteration,
    EvidenceLayer,
    MemoryLayer,
    AnomalyFrame,
)
from datetime import datetime


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with realistic solution responses"""
    provider = Mock()
    mock_response = Mock()
    mock_response.content = '{"answer": "Solution proposed", "solution_proposal": {}, "suggested_commands": []}'
    mock_response.tool_calls = None
    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def solution_handler(mock_llm_provider):
    """Create SolutionHandler instance"""
    return SolutionHandler(llm_provider=mock_llm_provider)


@pytest.fixture
def investigation_state_phase5():
    """Create investigation state in Phase 5 with validated hypothesis"""
    return InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.SOLUTION,
            case_status="investigating",
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
            anomaly_frame=AnomalyFrame(
                statement="API timeout errors",
                affected_components=["api-service"],
                affected_scope="All users",
                started_at=datetime.utcnow(),
                severity="high",
                confidence=0.85,
                framed_at_turn=3,
            ),
            hypotheses=[
                Hypothesis(
                    statement="Database connection pool exhausted",
                    category="infrastructure",
                    likelihood=0.85,
                    initial_likelihood=0.70,
                    created_at_turn=5,
                    last_updated_turn=7,
                    status=HypothesisStatus.VALIDATED,
                ),
            ],
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )


@pytest.fixture
def investigation_state_with_iteration():
    """Investigation state with existing OODA iteration"""
    state = InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.SOLUTION,
            case_status="investigating",
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
            current_iteration=1,
            anomaly_frame=AnomalyFrame(
                statement="Service crashes",
                affected_components=["service"],
                affected_scope="All",
                started_at=datetime.utcnow(),
                severity="critical",
                confidence=0.9,
                framed_at_turn=2,
            ),
            hypotheses=[
                Hypothesis(
                    statement="Memory leak in service",
                    category="code",
                    likelihood=0.90,
                    initial_likelihood=0.80,
                    created_at_turn=4,
                    last_updated_turn=6,
                    status=HypothesisStatus.VALIDATED,
                ),
            ],
            iterations=[
                OODAIteration(
                    iteration_number=1,
                    phase=InvestigationPhase.SOLUTION,
                    started_at_turn=8,
                    steps_completed=[OODAStep.DECIDE],
                    new_insights=["Solution proposed"],
                )
            ],
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )
    return state


# =============================================================================
# Basic Functionality Tests
# =============================================================================


class TestSolutionHandlerBasics:
    """Test basic SolutionHandler functionality"""

    def test_get_phase(self, solution_handler):
        """Test handler returns Phase 5 (SOLUTION)"""
        assert solution_handler.get_phase() == InvestigationPhase.SOLUTION

    @pytest.mark.asyncio
    async def test_handle_executes_solution_logic(
        self, solution_handler, investigation_state_phase5
    ):
        """Test handle() executes solution implementation logic"""
        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="What's the fix?",
            conversation_history="",
        )

        assert result.made_progress is True
        assert result.response_text is not None
        assert result.updated_state is not None

    @pytest.mark.asyncio
    async def test_handle_updates_state(
        self, solution_handler, investigation_state_phase5
    ):
        """Test handler properly updates investigation state"""
        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="How do I fix this?",
            conversation_history="",
        )

        # State should be updated
        assert result.updated_state == investigation_state_phase5
        # Should track OODA step execution
        assert result.ooda_step_executed in [OODAStep.DECIDE, OODAStep.ACT, OODAStep.ORIENT]


# =============================================================================
# OODA Step Tests (Phase 5: Decide, Act, Orient)
# =============================================================================


class TestOODAStepDecide:
    """Test OODA Decide step: Solution selection from validated hypothesis"""

    @pytest.mark.asyncio
    async def test_decide_proposes_solution(
        self, solution_handler, investigation_state_phase5
    ):
        """Test Decide step proposes solution based on validated hypothesis"""
        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="What should I do to fix this?",
            conversation_history="",
        )

        # Should execute Decide step
        assert result.ooda_step_executed == OODAStep.DECIDE
        assert result.made_progress is True
        # Should create new OODA iteration
        assert len(investigation_state_phase5.ooda_engine.iterations) >= 1

    @pytest.mark.asyncio
    async def test_decide_uses_validated_hypothesis(
        self, solution_handler, investigation_state_phase5, mock_llm_provider
    ):
        """Test Decide step uses validated hypothesis for solution"""
        with patch(
            "faultmaven.core.investigation.hypothesis_manager.create_hypothesis_manager"
        ) as mock_manager_factory:
            # Mock hypothesis manager
            mock_manager = Mock()
            mock_manager.get_validated_hypothesis.return_value = (
                investigation_state_phase5.ooda_engine.hypotheses[0]
            )
            mock_manager_factory.return_value = mock_manager

            result = await solution_handler.handle(
                investigation_state=investigation_state_phase5,
                user_query="What's the solution?",
                conversation_history="",
            )

            # Verify hypothesis manager was called
            mock_manager.get_validated_hypothesis.assert_called_once()
            assert result.ooda_step_executed == OODAStep.DECIDE

    @pytest.mark.asyncio
    async def test_decide_marks_step_complete(
        self, solution_handler, investigation_state_phase5
    ):
        """Test Decide step marks itself as completed"""
        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="Propose a fix",
            conversation_history="",
        )

        # Check iteration has Decide marked complete
        current_iteration = investigation_state_phase5.ooda_engine.iterations[-1]
        assert OODAStep.DECIDE in current_iteration.steps_completed
        assert "Solution proposed" in current_iteration.new_insights


class TestOODAStepAct:
    """Test OODA Act step: Solution implementation guidance"""

    @pytest.mark.asyncio
    async def test_act_provides_implementation_guidance(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test Act step provides solution implementation guidance"""
        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="I'll implement the fix now",
            conversation_history="",
        )

        # Should execute Act step
        assert result.ooda_step_executed == OODAStep.ACT
        assert result.made_progress is True

    @pytest.mark.asyncio
    async def test_act_marks_step_complete(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test Act step marks itself as completed"""
        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="Applying the solution",
            conversation_history="",
        )

        # Check iteration has Act marked complete
        current_iteration = investigation_state_with_iteration.ooda_engine.iterations[-1]
        assert OODAStep.ACT in current_iteration.steps_completed


class TestOODAStepOrient:
    """Test OODA Orient step: Verification of fix effectiveness"""

    @pytest.mark.asyncio
    async def test_orient_requests_verification(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test Orient step requests solution verification"""
        # Mark Decide and Act as complete
        investigation_state_with_iteration.ooda_engine.iterations[-1].steps_completed = [
            OODAStep.DECIDE,
            OODAStep.ACT,
        ]

        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="The fix is deployed",
            conversation_history="",
        )

        # Should execute Orient step
        assert result.ooda_step_executed == OODAStep.ORIENT
        # Should generate evidence requests for verification
        assert len(result.evidence_requests_generated) > 0

    @pytest.mark.asyncio
    async def test_orient_updates_case_status(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test Orient step updates case status to resolved"""
        # Mark Decide and Act as complete
        investigation_state_with_iteration.ooda_engine.iterations[-1].steps_completed = [
            OODAStep.DECIDE,
            OODAStep.ACT,
        ]

        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="Verification looks good",
            conversation_history="",
        )

        # Case status should be updated
        assert investigation_state_with_iteration.lifecycle.case_status == "resolved"

    @pytest.mark.asyncio
    async def test_orient_marks_iteration_complete(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test Orient step marks iteration as complete"""
        # Mark Decide and Act as complete
        investigation_state_with_iteration.ooda_engine.iterations[-1].steps_completed = [
            OODAStep.DECIDE,
            OODAStep.ACT,
        ]

        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="Everything is working now",
            conversation_history="",
        )

        # Iteration should be marked complete
        assert result.iteration_complete is True
        current_iteration = investigation_state_with_iteration.ooda_engine.iterations[-1]
        assert OODAStep.ORIENT in current_iteration.steps_completed

    @pytest.mark.asyncio
    async def test_orient_signals_phase_advancement(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test Orient step signals phase completion and advancement"""
        # Mark Decide and Act as complete
        investigation_state_with_iteration.ooda_engine.iterations[-1].steps_completed = [
            OODAStep.DECIDE,
            OODAStep.ACT,
        ]
        # Increase iteration count to meet completion criteria
        investigation_state_with_iteration.ooda_engine.current_iteration = 2

        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="Confirmed fixed",
            conversation_history="",
        )

        # Should signal phase completion
        assert result.phase_complete is True
        assert result.should_advance is True
        assert result.next_phase == InvestigationPhase.DOCUMENT


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestSolutionHappyPath:
    """Test successful solution implementation workflow"""

    @pytest.mark.asyncio
    async def test_solution_proposal_with_commands(
        self, solution_handler, investigation_state_phase5, mock_llm_provider
    ):
        """Test solution proposal includes implementation commands"""
        # Mock response with commands
        mock_response = Mock()
        mock_response.content = '''{
            "answer": "Increase connection pool size to 50",
            "suggested_commands": [
                {
                    "command": "UPDATE config SET pool_size=50",
                    "description": "Update connection pool configuration",
                    "safety": "caution"
                }
            ]
        }'''
        mock_response.tool_calls = None
        mock_llm_provider.generate.return_value = mock_response

        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="What should I do?",
            conversation_history="",
        )

        assert result.made_progress is True
        # Verify LLM was called
        assert mock_llm_provider.generate.called

    @pytest.mark.asyncio
    async def test_fix_verification_with_rollback(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test solution verification includes rollback planning"""
        # Complete previous steps
        investigation_state_with_iteration.ooda_engine.iterations[-1].steps_completed = [
            OODAStep.DECIDE,
            OODAStep.ACT,
        ]

        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="How do I verify this worked?",
            conversation_history="",
        )

        # Should provide verification guidance
        assert result.ooda_step_executed == OODAStep.ORIENT
        assert result.made_progress is True


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestSolutionEdgeCases:
    """Test edge cases and special scenarios"""

    @pytest.mark.asyncio
    async def test_multiple_solution_options(
        self, solution_handler, investigation_state_phase5
    ):
        """Test handling multiple solution options (A/B choice)"""
        # Add multiple validated hypotheses
        investigation_state_phase5.ooda_engine.hypotheses.append(
            Hypothesis(
                statement="Insufficient server resources",
                category="infrastructure",
                likelihood=0.80,
                initial_likelihood=0.65,
                created_at_turn=5,
                last_updated_turn=7,
                status=HypothesisStatus.VALIDATED,
            )
        )

        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="What are the options?",
            conversation_history="",
        )

        # Should still make progress with multiple hypotheses
        assert result.made_progress is True

    @pytest.mark.asyncio
    async def test_partial_solution_mitigation(
        self, solution_handler, investigation_state_phase5
    ):
        """Test partial solution (mitigation vs full fix)"""
        investigation_state_phase5.lifecycle.case_status = "mitigated"

        # Complete all OODA steps
        investigation_state_phase5.ooda_engine.iterations = [
            OODAIteration(
                iteration_number=1,
                phase=InvestigationPhase.SOLUTION,
                started_at_turn=8,
                steps_completed=[OODAStep.DECIDE, OODAStep.ACT, OODAStep.ORIENT],
                new_insights=["Mitigation applied"],
                completed_at_turn=10,
            )
        ]
        investigation_state_phase5.ooda_engine.current_iteration = 2

        is_complete, met, unmet = await solution_handler.check_completion(
            investigation_state_phase5
        )

        # Mitigation should count as completion
        assert is_complete is True
        assert "resolved/mitigated" in " ".join(met).lower()

    @pytest.mark.asyncio
    async def test_solution_requiring_multiple_steps(
        self, solution_handler, investigation_state_phase5
    ):
        """Test solution requiring multiple implementation steps"""
        # Simulate multiple iterations
        investigation_state_phase5.ooda_engine.current_iteration = 3

        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="Continue with next step",
            conversation_history="",
        )

        # Should still make progress on multi-step solutions
        assert result.made_progress is True


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestSolutionErrorHandling:
    """Test error handling scenarios"""

    @pytest.mark.asyncio
    async def test_llm_provider_failure(
        self, solution_handler, investigation_state_phase5, mock_llm_provider
    ):
        """Test handling LLM provider failure during solution generation"""
        # Make LLM provider fail
        mock_llm_provider.generate.side_effect = Exception("LLM service unavailable")

        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="What should I do?",
            conversation_history="",
        )

        # Should handle error gracefully
        assert result.response_text is not None
        assert "error" in result.response_text.lower()

    @pytest.mark.asyncio
    async def test_malformed_solution_response(
        self, solution_handler, investigation_state_phase5, mock_llm_provider
    ):
        """Test handling malformed solution response from LLM"""
        # Return invalid JSON
        mock_response = Mock()
        mock_response.content = "This is not valid JSON at all"
        mock_response.tool_calls = None
        mock_llm_provider.generate.return_value = mock_response

        result = await solution_handler.handle(
            investigation_state=investigation_state_phase5,
            user_query="What's the fix?",
            conversation_history="",
        )

        # Should handle parsing error gracefully
        assert result.response_text is not None

    @pytest.mark.asyncio
    async def test_no_validated_hypothesis(
        self, solution_handler, investigation_state_phase5
    ):
        """Test handling case with no validated hypothesis"""
        # Mark hypothesis as pending
        investigation_state_phase5.ooda_engine.hypotheses[0].status = HypothesisStatus.PENDING

        with patch(
            "faultmaven.core.investigation.hypothesis_manager.create_hypothesis_manager"
        ) as mock_manager_factory:
            # Mock hypothesis manager to return None
            mock_manager = Mock()
            mock_manager.get_validated_hypothesis.return_value = None
            mock_manager_factory.return_value = mock_manager

            result = await solution_handler.handle(
                investigation_state=investigation_state_phase5,
                user_query="What's the solution?",
                conversation_history="",
            )

            # Should still attempt to provide guidance
            assert result.made_progress is True


# =============================================================================
# Completion Criteria Tests
# =============================================================================


class TestCompletionCriteria:
    """Test phase completion detection"""

    @pytest.mark.asyncio
    async def test_completion_requires_resolved_status(
        self, solution_handler, investigation_state_phase5
    ):
        """Test completion requires problem resolved/mitigated status"""
        is_complete, met, unmet = await solution_handler.check_completion(
            investigation_state_phase5
        )

        # Should not be complete without resolved status
        assert is_complete is False
        assert any("verified" in criterion.lower() for criterion in unmet)

    @pytest.mark.asyncio
    async def test_completion_with_resolved_status(
        self, solution_handler, investigation_state_phase5
    ):
        """Test completion when problem is resolved"""
        # Set resolved status
        investigation_state_phase5.lifecycle.case_status = "resolved"
        investigation_state_phase5.ooda_engine.current_iteration = 2

        is_complete, met, unmet = await solution_handler.check_completion(
            investigation_state_phase5
        )

        # Should be complete
        assert is_complete is True
        assert len(unmet) == 0

    @pytest.mark.asyncio
    async def test_completion_checks_iteration_count(
        self, solution_handler, investigation_state_phase5
    ):
        """Test completion considers iteration count"""
        investigation_state_phase5.lifecycle.case_status = "resolved"
        investigation_state_phase5.ooda_engine.current_iteration = 3

        is_complete, met, unmet = await solution_handler.check_completion(
            investigation_state_phase5
        )

        # Should recognize multiple iterations
        assert "implementation attempted" in " ".join(met).lower()


# =============================================================================
# Integration Tests
# =============================================================================


class TestSolutionIntegration:
    """Test integration with other components"""

    @pytest.mark.asyncio
    async def test_evidence_request_generation(
        self, solution_handler, investigation_state_with_iteration
    ):
        """Test generation of verification evidence requests"""
        # Complete previous steps
        investigation_state_with_iteration.ooda_engine.iterations[-1].steps_completed = [
            OODAStep.DECIDE,
            OODAStep.ACT,
        ]

        result = await solution_handler.handle(
            investigation_state=investigation_state_with_iteration,
            user_query="Verify the fix",
            conversation_history="",
        )

        # Should generate evidence requests
        assert len(result.evidence_requests_generated) > 0
        evidence_request = result.evidence_requests_generated[0]
        assert evidence_request.label == "Solution verification"
        assert evidence_request.priority == 1

    @pytest.mark.asyncio
    async def test_state_format_for_prompt(
        self, solution_handler, investigation_state_phase5
    ):
        """Test state formatting for prompt includes necessary context"""
        validated_hypothesis = investigation_state_phase5.ooda_engine.hypotheses[0]

        formatted_state = solution_handler._format_state_for_prompt(
            investigation_state_phase5, validated_hypothesis
        )

        # Should include relevant context
        assert "case_status" in formatted_state
        assert "current_iteration" in formatted_state
        assert "root_cause" in formatted_state
        assert formatted_state["root_cause"] == validated_hypothesis.statement


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Unit tests for the TroubleshootingDoctrine class.

This module tests the five-phase troubleshooting methodology including:
- Phase execution with LLM integration
- Fallback mechanisms when LLM is unavailable
- Response parsing and structured output
- Phase transitions and validation
- Error handling and edge cases
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.agent.doctrine import Phase, TroubleshootingDoctrine
from faultmaven.models import AgentState


class TestTroubleshootingDoctrine:
    """Test cases for TroubleshootingDoctrine class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.doctrine = TroubleshootingDoctrine()
        self.sample_agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database timeout in production",
            findings=[
                {
                    "finding": "Connection pool exhausted",
                    "timestamp": "2024-01-01T10:00:00Z",
                },
                {
                    "finding": "High memory usage detected",
                    "timestamp": "2024-01-01T10:01:00Z",
                },
            ],
            investigation_context={},
        )
        self.sample_context = {
            "uploaded_data": [
                {"data_type": "log_file", "file_name": "app.log"},
                {"data_type": "error_message", "file_name": "error.txt"},
            ]
        }

    def test_phase_enum_values(self):
        """Test that Phase enum has correct values."""
        assert Phase.DEFINE_BLAST_RADIUS.value == "define_blast_radius"
        assert Phase.ESTABLISH_TIMELINE.value == "establish_timeline"
        assert Phase.FORMULATE_HYPOTHESIS.value == "formulate_hypothesis"
        assert Phase.VALIDATE_HYPOTHESIS.value == "validate_hypothesis"
        assert Phase.PROPOSE_SOLUTION.value == "propose_solution"

    def test_doctrine_initialization(self):
        """Test TroubleshootingDoctrine initialization."""
        assert self.doctrine.PHASES == [phase.value for phase in Phase]
        assert len(self.doctrine.phase_guidance) == 5
        assert Phase.DEFINE_BLAST_RADIUS in self.doctrine.phase_guidance
        assert Phase.PROPOSE_SOLUTION in self.doctrine.phase_guidance

    def test_get_phase_objective(self):
        """Test get_phase_objective method."""
        objective = self.doctrine.get_phase_objective(Phase.DEFINE_BLAST_RADIUS)
        assert "scope and impact" in objective.lower()
        assert "assess" in objective.lower()

        objective = self.doctrine.get_phase_objective(Phase.PROPOSE_SOLUTION)
        assert "solution" in objective.lower()

    def test_create_phase_prompt(self):
        """Test _create_phase_prompt method."""
        guidance = self.doctrine.phase_guidance[Phase.DEFINE_BLAST_RADIUS]
        prompt = self.doctrine._create_phase_prompt(
            Phase.DEFINE_BLAST_RADIUS,
            guidance,
            self.sample_agent_state,
            self.sample_context,
        )

        # Check prompt structure
        assert "Phase: Define Blast Radius" in prompt
        assert "Objective:" in prompt
        assert "Key Questions to Address:" in prompt
        assert "Available Tools:" in prompt
        assert "Current Context:" in prompt
        assert "Single Insight, Single Question" in prompt
        assert "test-session-123" in prompt
        assert "Database timeout in production" in prompt

    def test_parse_llm_response_basic(self):
        """Test _parse_llm_response with basic content."""
        content = """
        Key insight: The database connection pool is exhausted.
        
        This suggests we need to look at connection management.
        Should we check the connection pool configuration?
        """

        parsed = self.doctrine._parse_llm_response(content)
        assert "database connection pool" in parsed["key_insight"].lower()
        assert "?" in parsed["follow_up_question"]

    def test_parse_llm_response_with_tools(self):
        """Test _parse_llm_response with tool recommendations."""
        content = """
        Key insight: Need to search knowledge base for similar issues.
        
        I recommend using knowledge base search for connection timeout patterns.
        Should we proceed with this approach?
        """

        parsed = self.doctrine._parse_llm_response(content)
        assert parsed["tool_to_use"] == "knowledge_base_search"
        assert "connection timeout" in parsed["tool_query"]

    def test_parse_llm_response_with_hypotheses(self):
        """Test _parse_llm_response with hypothesis lists."""
        content = """
        Key insight: Multiple potential causes identified.
        
        1. Connection pool exhaustion hypothesis
        - Database connection limits reached
        
        Which hypothesis should we test first?
        """

        parsed = self.doctrine._parse_llm_response(content)
        assert len(parsed["hypotheses"]) > 0
        assert "connection pool" in parsed["hypotheses"][0].lower()

    @pytest.mark.asyncio
    async def test_execute_phase_blast_radius_with_llm(self):
        """Test execute_phase for blast radius with LLM."""
        # Mock LLM router
        mock_llm_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = """
        Key insight: The issue affects the entire user authentication system.
        
        This suggests a system-wide impact rather than isolated failures.
        Are multiple users reporting authentication failures?
        """
        mock_llm_router.route.return_value = mock_response

        context = {**self.sample_context, "llm_router": mock_llm_router}

        result = await self.doctrine.execute_phase(
            Phase.DEFINE_BLAST_RADIUS, self.sample_agent_state, context
        )

        # Verify LLM was called
        mock_llm_router.route.assert_called_once()

        # Check result structure
        assert result["phase"] == Phase.DEFINE_BLAST_RADIUS.value
        assert result["status"] == "completed"
        assert "key_insight" in result
        assert "follow_up_question" in result
        assert result["requires_user_input"] is True
        assert result["next_phase"] == Phase.ESTABLISH_TIMELINE.value

    @pytest.mark.asyncio
    async def test_execute_phase_timeline_with_llm(self):
        """Test execute_phase for timeline with LLM."""
        mock_llm_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = """
        Key insight: The issue started after the 2 PM deployment.
        
        Timeline analysis shows correlation with system changes.
        Were there any database schema changes in that deployment?
        """
        mock_llm_router.route.return_value = mock_response

        context = {**self.sample_context, "llm_router": mock_llm_router}

        result = await self.doctrine.execute_phase(
            Phase.ESTABLISH_TIMELINE, self.sample_agent_state, context
        )

        assert result["phase"] == Phase.ESTABLISH_TIMELINE.value
        assert result["status"] == "completed"
        assert (
            "timeline" in result["key_insight"].lower()
            or "deployment" in result["key_insight"].lower()
        )
        assert result["next_phase"] == Phase.FORMULATE_HYPOTHESIS.value

    @pytest.mark.asyncio
    async def test_execute_phase_hypothesis_with_llm(self):
        """Test execute_phase for hypothesis with LLM."""
        mock_llm_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = """
        Key insight: Three potential hypotheses identified.
        
        1. Database connection pool exhaustion
        - Connection limits reached during peak load
        
        Which hypothesis should we validate first?
        """
        mock_llm_router.route.return_value = mock_response

        context = {**self.sample_context, "llm_router": mock_llm_router}

        result = await self.doctrine.execute_phase(
            Phase.FORMULATE_HYPOTHESIS, self.sample_agent_state, context
        )

        assert result["phase"] == Phase.FORMULATE_HYPOTHESIS.value
        assert result["status"] == "completed"
        assert "hypothesis_list" in result
        assert len(result["hypothesis_list"]) > 0
        assert result["next_phase"] == Phase.VALIDATE_HYPOTHESIS.value

    @pytest.mark.asyncio
    async def test_execute_phase_validation_with_llm(self):
        """Test execute_phase for validation with LLM."""
        # Set up agent state with hypothesis from previous phase
        agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database connection timeout",
            findings=[],
            investigation_context={
                "formulate_hypothesis_results": {
                    "hypothesis_list": ["Connection pool exhaustion", "Network timeout"]
                }
            },
        )

        mock_llm_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = """
        Key insight: Evidence strongly supports the connection pool exhaustion hypothesis.
        
        Validated hypothesis: Connection pool exhaustion
        Does this match your observations?
        """
        mock_llm_router.route.return_value = mock_response

        context = {**self.sample_context, "llm_router": mock_llm_router}

        result = await self.doctrine.execute_phase(
            Phase.VALIDATE_HYPOTHESIS, agent_state, context
        )

        assert result["phase"] == Phase.VALIDATE_HYPOTHESIS.value
        assert result["status"] == "completed"
        assert "validated_hypothesis" in result
        assert result["next_phase"] == Phase.PROPOSE_SOLUTION.value

    @pytest.mark.asyncio
    async def test_execute_phase_solution_with_llm(self):
        """Test execute_phase for solution with LLM."""
        # Set up agent state with validated hypothesis
        agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database connection timeout",
            findings=[],
            investigation_context={
                "validate_hypothesis_results": {
                    "validated_hypothesis": "Connection pool exhaustion"
                }
            },
        )

        mock_llm_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = """
        Key insight: Multiple solution approaches available.
        
        1. Increase connection pool size
        2. Implement connection pooling optimization
        
        Which solution would you like to implement first?
        """
        mock_llm_router.route.return_value = mock_response

        context = {**self.sample_context, "llm_router": mock_llm_router}

        result = await self.doctrine.execute_phase(
            Phase.PROPOSE_SOLUTION, agent_state, context
        )

        assert result["phase"] == Phase.PROPOSE_SOLUTION.value
        assert result["status"] == "completed"
        assert "solution_options" in result
        assert len(result["solution_options"]) > 0
        assert result["next_phase"] is None  # Final phase

    @pytest.mark.asyncio
    async def test_execute_phase_fallback_no_llm(self):
        """Test execute_phase fallback when LLM is not available."""
        context = self.sample_context  # No llm_router

        result = await self.doctrine.execute_phase(
            Phase.DEFINE_BLAST_RADIUS, self.sample_agent_state, context
        )

        # Should use fallback method
        assert result["phase"] == Phase.DEFINE_BLAST_RADIUS.value
        assert result["status"] == "completed"
        assert "key_insight" in result
        assert "follow_up_question" in result
        assert result["confidence_score"] == 0.6  # Lower confidence for fallback

    @pytest.mark.asyncio
    async def test_execute_phase_llm_error_fallback(self):
        """Test execute_phase fallback when LLM call fails."""
        # Mock LLM router that raises exception
        mock_llm_router = AsyncMock()
        mock_llm_router.route.side_effect = Exception("LLM service unavailable")

        context = {**self.sample_context, "llm_router": mock_llm_router}

        result = await self.doctrine.execute_phase(
            Phase.DEFINE_BLAST_RADIUS, self.sample_agent_state, context
        )

        # Should gracefully fall back
        assert result["phase"] == Phase.DEFINE_BLAST_RADIUS.value
        assert result["status"] == "completed"
        assert result["confidence_score"] == 0.6  # Fallback confidence

    @pytest.mark.asyncio
    async def test_execute_phase_with_knowledge_base_tool(self):
        """Test execute_phase with knowledge base tool integration."""
        # Mock knowledge base tool
        mock_kb_tool = MagicMock()
        mock_kb_tool.search.return_value = (
            "**Result 1**\nRelevant information\n**Result 2**\nMore info"
        )

        mock_llm_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = """
        Key insight: Need to search knowledge base for similar patterns.
        
        I recommend searching knowledge base for connection timeout issues.
        Should we proceed with this search?
        """
        mock_llm_router.route.return_value = mock_response

        context = {
            **self.sample_context,
            "llm_router": mock_llm_router,
            "knowledge_base_tool": mock_kb_tool,
        }

        result = await self.doctrine.execute_phase(
            Phase.DEFINE_BLAST_RADIUS, self.sample_agent_state, context
        )

        # Verify knowledge base tool was called
        mock_kb_tool.search.assert_called_once()

        # Check that search result was incorporated
        assert any(
            "Knowledge base search completed" in finding
            for finding in result["findings"]
        )

    def test_get_next_phase(self):
        """Test get_next_phase method."""
        assert (
            self.doctrine.get_next_phase(Phase.DEFINE_BLAST_RADIUS)
            == Phase.ESTABLISH_TIMELINE
        )
        assert (
            self.doctrine.get_next_phase(Phase.ESTABLISH_TIMELINE)
            == Phase.FORMULATE_HYPOTHESIS
        )
        assert (
            self.doctrine.get_next_phase(Phase.FORMULATE_HYPOTHESIS)
            == Phase.VALIDATE_HYPOTHESIS
        )
        assert (
            self.doctrine.get_next_phase(Phase.VALIDATE_HYPOTHESIS)
            == Phase.PROPOSE_SOLUTION
        )
        assert (
            self.doctrine.get_next_phase(Phase.PROPOSE_SOLUTION) is None
        )  # Final phase

    def test_validate_phase_transition(self):
        """Test validate_phase_transition method."""
        # Valid transitions
        assert (
            self.doctrine.validate_phase_transition(
                Phase.DEFINE_BLAST_RADIUS, Phase.ESTABLISH_TIMELINE
            )
            is True
        )

        assert (
            self.doctrine.validate_phase_transition(
                Phase.VALIDATE_HYPOTHESIS, Phase.PROPOSE_SOLUTION
            )
            is True
        )

        # Invalid transitions (skipping phases)
        assert (
            self.doctrine.validate_phase_transition(
                Phase.DEFINE_BLAST_RADIUS, Phase.FORMULATE_HYPOTHESIS
            )
            is False
        )

        assert (
            self.doctrine.validate_phase_transition(
                Phase.ESTABLISH_TIMELINE, Phase.PROPOSE_SOLUTION
            )
            is False
        )

    def test_should_request_user_input(self):
        """Test should_request_user_input method."""
        result_with_input = {"requires_user_input": True}
        result_without_input = {"requires_user_input": False}
        result_no_field = {}

        assert self.doctrine.should_request_user_input(result_with_input) is True
        assert self.doctrine.should_request_user_input(result_without_input) is False
        assert self.doctrine.should_request_user_input(result_no_field) is False

    def test_get_phase_guidance(self):
        """Test get_phase_guidance method."""
        guidance = self.doctrine.get_phase_guidance(Phase.DEFINE_BLAST_RADIUS)

        assert "objective" in guidance
        assert "key_questions" in guidance
        assert "tools_needed" in guidance
        assert "outputs" in guidance
        assert "success_criteria" in guidance

        # Test unknown phase
        fake_phase = MagicMock()
        fake_phase.value = "unknown_phase"
        empty_guidance = self.doctrine.get_phase_guidance(fake_phase)
        assert empty_guidance == {}

    @pytest.mark.asyncio
    async def test_execute_phase_invalid_phase(self):
        """Test execute_phase with invalid phase."""
        # Create a mock phase that's not in the enum
        mock_phase = MagicMock()
        mock_phase.value = "invalid_phase"

        with pytest.raises(KeyError):
            await self.doctrine.execute_phase(
                mock_phase, self.sample_agent_state, self.sample_context
            )

    def test_fallback_methods_with_data(self):
        """Test fallback methods with different data types."""
        context_with_logs = {
            "uploaded_data": [
                {"data_type": "log_file", "file_name": "app.log"},
                {"data_type": "metrics_data", "file_name": "metrics.json"},
            ]
        }

        # Test blast radius fallback
        result = self.doctrine._fallback_blast_radius_analysis(
            self.sample_agent_state, context_with_logs
        )
        assert result["phase"] == Phase.DEFINE_BLAST_RADIUS.value
        assert any("Log files" in finding for finding in result["findings"])

        # Test timeline fallback
        result = self.doctrine._fallback_timeline_analysis(
            self.sample_agent_state, context_with_logs
        )
        assert result["phase"] == Phase.ESTABLISH_TIMELINE.value
        assert any("Log timestamps" in finding for finding in result["findings"])

    def test_fallback_methods_with_previous_findings(self):
        """Test fallback methods using previous findings."""
        agent_state_with_findings = AgentState(
            session_id="test-session-123",
            user_query="Database error",
            findings=[
                {
                    "finding": "Connection error detected in logs",
                    "timestamp": "2024-01-01T10:00:00Z",
                },
                {
                    "finding": "Performance degradation observed",
                    "timestamp": "2024-01-01T10:01:00Z",
                },
            ],
            investigation_context={},
        )

        result = self.doctrine._fallback_hypothesis_analysis(
            agent_state_with_findings, {}
        )

        assert result["phase"] == Phase.FORMULATE_HYPOTHESIS.value
        assert len(result["hypothesis_list"]) > 0
        assert any(
            "Error-based failure hypothesis" in h for h in result["hypothesis_list"]
        )
        assert any(
            "Performance degradation hypothesis" in h for h in result["hypothesis_list"]
        )

    def test_fallback_validation_with_hypothesis(self):
        """Test validation fallback with existing hypothesis."""
        agent_state_with_hypothesis = AgentState(
            session_id="test-session-123",
            user_query="Database error",
            findings=[],
            investigation_context={
                "formulate_hypothesis_results": {
                    "hypothesis_list": [
                        "Database connection failure",
                        "Network timeout",
                    ]
                }
            },
        )

        result = self.doctrine._fallback_validation_analysis(
            agent_state_with_hypothesis, {}
        )

        assert result["phase"] == Phase.VALIDATE_HYPOTHESIS.value
        assert result["validated_hypothesis"] == "Database connection failure"
        assert "Evidence supports" in result["key_insight"]

    def test_fallback_solution_with_validated_hypothesis(self):
        """Test solution fallback with validated hypothesis."""
        agent_state_with_validation = AgentState(
            session_id="test-session-123",
            user_query="Database error",
            findings=[],
            investigation_context={
                "validate_hypothesis_results": {
                    "validated_hypothesis": "Connection pool exhaustion"
                }
            },
        )

        result = self.doctrine._fallback_solution_analysis(
            agent_state_with_validation, {}
        )

        assert result["phase"] == Phase.PROPOSE_SOLUTION.value
        assert len(result["solution_options"]) > 0
        assert any(
            "Connection pool exhaustion" in solution
            for solution in result["solution_options"]
        )
        assert "validated hypothesis" in result["key_insight"].lower()

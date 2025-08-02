from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.agent.agent import FaultMavenAgent
from faultmaven.core.agent.doctrine import Phase
from faultmaven.models import AgentState


class TestCoreAgentErrors:
    """Test error handling scenarios for FaultMavenAgent."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_llm_router = AsyncMock()
        self.mock_kb_tool = MagicMock()
        self.mock_web_search_tool = MagicMock()

        # Configure web search tool to be available
        self.mock_web_search_tool.is_available.return_value = True

        self.agent = FaultMavenAgent(
            llm_router=self.mock_llm_router,
            knowledge_base_tool=self.mock_kb_tool,
            web_search_tool=self.mock_web_search_tool,
        )

        self.sample_state = AgentState(
            session_id="test-session-123",
            user_query="Database connection timeout",
            findings=[],
            investigation_context={},
            awaiting_user_input=False,
            current_phase="triage",
            user_feedback="",
        )

    @pytest.mark.asyncio
    async def test_triage_node_llm_failure(self):
        """Test triage node when LLM router fails."""
        # Use an error message that triggers the LLM fallback
        self.mock_llm_router.route.side_effect = Exception("All LLM providers failed")

        result = await self.agent._triage_node(self.sample_state)

        # Should fall back to basic triage and complete the phase
        assert result["current_phase"] == "triage_completed"
        assert "triage_error" in result["investigation_context"]
        assert "triage_assessment" in result["investigation_context"]
        assert "severity" in result["investigation_context"]
        assert "agent_response" in result["investigation_context"]

    @pytest.mark.asyncio
    async def test_triage_node_kb_tool_failure(self):
        """Test triage node when knowledge base tool fails."""
        # Use an error message that doesn't trigger LLM fallback
        self.mock_llm_router.route.side_effect = Exception("LLM service unavailable")
        self.mock_kb_tool.arun.side_effect = Exception("Knowledge base unavailable")

        result = await self.agent._triage_node(self.sample_state)

        # Should handle the error but not complete the phase (remains "triage")
        assert result["current_phase"] == "triage"
        assert "triage_error" in result["investigation_context"]
        assert "agent_response" in result["investigation_context"]

    @pytest.mark.asyncio
    async def test_define_blast_radius_doctrine_failure(self):
        """Test define_blast_radius when doctrine execution fails."""
        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase.side_effect = Exception(
                "Doctrine execution failed"
            )

            result = await self.agent._define_blast_radius_node(self.sample_state)

            # Should handle the error gracefully
            assert result["current_phase"] == "triage"
            assert "blast_radius_error" in result["investigation_context"]

    @pytest.mark.asyncio
    async def test_establish_timeline_doctrine_failure(self):
        """Test establish_timeline when doctrine execution fails."""
        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase.side_effect = Exception(
                "Doctrine execution failed"
            )

            result = await self.agent._establish_timeline_node(self.sample_state)

            # Should handle the error gracefully
            assert result["current_phase"] == "triage"
            assert "timeline_error" in result["investigation_context"]

    @pytest.mark.asyncio
    async def test_formulate_hypothesis_doctrine_failure(self):
        """Test formulate_hypothesis when doctrine execution fails."""
        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase.side_effect = Exception(
                "Doctrine execution failed"
            )

            result = await self.agent._formulate_hypothesis_node(self.sample_state)

            # Should handle the error gracefully
            assert result["current_phase"] == "triage"
            assert "hypothesis_error" in result["investigation_context"]

    @pytest.mark.asyncio
    async def test_validate_hypothesis_doctrine_failure(self):
        """Test validate_hypothesis when doctrine execution fails."""
        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase.side_effect = Exception(
                "Doctrine execution failed"
            )

            result = await self.agent._validate_hypothesis_node(self.sample_state)

            # Should handle the error gracefully
            assert result["current_phase"] == "triage"
            assert "validation_error" in result["investigation_context"]

    @pytest.mark.asyncio
    async def test_propose_solution_doctrine_failure(self):
        """Test propose_solution when doctrine execution fails."""
        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase.side_effect = Exception(
                "Doctrine execution failed"
            )

            result = await self.agent._propose_solution_node(self.sample_state)

            # Should handle the error gracefully
            assert result["current_phase"] == "triage"
            assert "solution_error" in result["investigation_context"]

    def test_should_start_investigation_with_error(self):
        """Test _should_start_investigation with error in state."""
        # State with error condition
        error_state = AgentState(
            session_id="test-session",
            user_query="",
            findings=[],
            investigation_context={"error": "Invalid query"},
            awaiting_user_input=False,
            current_phase="triage",
            user_feedback="",
        )

        result = self.agent._should_start_investigation(error_state)
        assert (
            result == "respond_to_user"
        )  # Should respond to user instead of continuing

    def test_decide_if_user_update_needed_with_error(self):
        """Test _decide_if_user_update_is_needed with error in investigation context."""
        error_state = AgentState(
            session_id="test-session",
            user_query="test query",
            findings=[],
            investigation_context={
                "error": "Processing failed",
                "waiting_for_input": False,
            },
            awaiting_user_input=False,
            current_phase="define_blast_radius_completed",
            user_feedback="",
        )

        result = self.agent._decide_if_user_update_is_needed(error_state)
        assert (
            result == "respond_to_user"
        )  # Should respond to user when there's an error

    def test_process_user_input_invalid_phase(self):
        """Test _process_user_input with invalid phase transition."""
        state = AgentState(
            session_id="test-session",
            user_query="test query",
            findings=[],
            investigation_context={"current_phase": "invalid_phase"},
            awaiting_user_input=True,
            current_phase="await_user_input",
            user_feedback="continue",
        )

        result = self.agent._process_user_input(state)
        assert result == "END"  # Should return END for invalid phase

    def test_check_tool_safety_dangerous_tool(self):
        """Test _check_tool_safety with dangerous tool."""
        # Test dangerous tool
        assert self.agent._check_tool_safety("system_restart") is True
        assert self.agent._check_tool_safety("database_reset") is True

        # Test safe tool
        assert self.agent._check_tool_safety("knowledge_base_search") is False

    def test_extract_severity_edge_cases(self):
        """Test _extract_severity with edge cases."""
        # Test empty string
        assert self.agent._extract_severity("") == "low"

        # Test None
        assert self.agent._extract_severity(None) == "low"

        # Test with no severity indicators
        assert self.agent._extract_severity("This is a normal message") == "low"

    @pytest.mark.asyncio
    async def test_run_method_with_uploaded_data_error(self):
        """Test run method when processing uploaded data fails."""
        # Mock uploaded data that causes an error
        uploaded_data = [{"invalid": "data", "causes": "error"}]

        with patch.object(self.agent, "compiled_graph") as mock_graph:
            mock_graph.ainvoke.side_effect = Exception("Graph execution failed")

            result = await self.agent.run("test-session", "test query", uploaded_data)

            # Should handle the error and return a state with error information
            assert result["investigation_context"].get("error") is not None

    @pytest.mark.asyncio
    async def test_resume_method_with_invalid_session(self):
        """Test resume method with invalid session."""
        with patch.object(self.agent, "compiled_graph") as mock_graph:
            mock_graph.ainvoke.side_effect = Exception("Session not found")

            with pytest.raises(Exception) as exc_info:
                await self.agent.resume("invalid-session", "user input")
            assert "Session not found" in str(exc_info.value)

    def test_get_agent_status_with_error(self):
        """Test get_agent_status when there's an error retrieving status."""
        with patch.object(self.agent, "compiled_graph") as mock_graph:
            mock_graph.get_state.side_effect = Exception("Status retrieval failed")

            result = self.agent.get_agent_status("test-session")

            # Should return None or error information
            assert result is None or "error" in result

    @pytest.mark.asyncio
    async def test_process_query_with_priority_error(self):
        """Test process_query with invalid priority."""
        result = await self.agent.process_query(
            query="test query", session_id="test-session", priority="invalid_priority"
        )
        # Should surface an error message in recommendations
        assert any(
            "error" in rec.lower() or "issue" in rec.lower()
            for rec in result.get("recommendations", [])
        )

    def test_estimate_mttr_edge_cases(self):
        """Test _estimate_mttr with edge cases."""
        # Test with very low confidence
        assert self.agent._estimate_mttr("high", 0.1) == "1h 30m"

        # Test with very high confidence
        assert self.agent._estimate_mttr("low", 0.95) == "2 hours"

        # Test with invalid priority
        assert self.agent._estimate_mttr("invalid", 0.5) == "2 hours"

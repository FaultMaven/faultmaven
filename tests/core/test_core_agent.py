"""
Unit tests for the FaultMavenAgent class.

This module tests the agent orchestration system including:
- Agent graph construction and execution
- Human-in-the-loop interruption mechanisms
- Tool safety protocols
- State management and phase transitions
- Error handling and recovery
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from faultmaven.core.agent.agent import FaultMavenAgent
from faultmaven.core.agent.doctrine import Phase
from faultmaven.models import AgentStateDict


class TestCoreAgent:
    """Test cases for FaultMavenAgent class."""

    def setup_method(self):
        """Set up test fixtures."""
        # Mock dependencies
        self.mock_doctrine = MagicMock()
        self.mock_llm_router = AsyncMock()
        self.mock_kb_tool = MagicMock()

        # Initialize agent with mocked dependencies
        self.agent = FaultMavenAgent(
            llm_router=self.mock_llm_router, knowledge_base_tool=self.mock_kb_tool
        )

        self.sample_agent_state = AgentStateDict(
            session_id="test-session-123",
            user_query="Database connection timeout in production",
            findings=[],
            case_context={},
            awaiting_user_input=False,
            current_phase="define_blast_radius",
            user_feedback="",
        )

    def test_agent_initialization(self):
        """Test FaultMavenAgent initialization."""
        assert self.agent.doctrine is not None
        assert self.agent.graph is not None
        assert hasattr(self.agent, "logger")

    def test_graph_construction(self):
        """Test that the agent graph is properly constructed."""
        # Check that all required nodes are present
        expected_nodes = [
            "define_blast_radius",
            "establish_timeline",
            "formulate_hypothesis",
            "validate_hypothesis",
            "propose_solution",
            "respond_to_user",
            "await_user_input",
        ]

        graph_nodes = list(self.agent.graph.nodes.keys())
        for node in expected_nodes:
            assert node in graph_nodes

    def test_decide_if_user_update_needed_basic(self):
        """Test _decide_if_user_update_is_needed with basic phase results."""
        # Create a mock state with required fields
        state = {
            "case_context": {"waiting_for_input": False},
            "confidence_score": 0.9,
            "findings": [],
            "current_phase": "",
        }

        # High confidence, no waiting - should proceed to next phase
        decision = self.agent._decide_if_user_update_is_needed(state)
        assert decision == "respond_to_user"  # Default behavior when no phase set

        # Test with waiting for input
        state["case_context"]["waiting_for_input"] = True
        decision = self.agent._decide_if_user_update_is_needed(state)
        assert decision == "respond_to_user"

    def test_decide_if_user_update_needed_low_confidence(self):
        """Test _decide_if_user_update_is_needed with low confidence scores."""
        state = {
            "case_context": {"waiting_for_input": False},
            "confidence_score": 0.3,  # Low confidence
            "findings": [],
            "current_phase": "",
        }

        decision = self.agent._decide_if_user_update_is_needed(state)
        assert (
            decision == "respond_to_user"
        )  # Should require user input due to low confidence

    def test_decide_if_user_update_needed_with_phases(self):
        """Test _decide_if_user_update_is_needed with different completed phases."""
        # Test blast radius completed -> establish timeline
        state = {
            "case_context": {"waiting_for_input": False},
            "confidence_score": 0.8,
            "findings": [],
            "current_phase": "define_blast_radius_completed",
        }

        decision = self.agent._decide_if_user_update_is_needed(state)
        assert decision == "establish_timeline"

        # Test timeline completed -> formulate hypothesis
        state["current_phase"] = "establish_timeline_completed"
        decision = self.agent._decide_if_user_update_is_needed(state)
        assert decision == "formulate_hypothesis"

    def test_decide_if_user_update_needed_findings(self):
        """Test _decide_if_user_update_is_needed with findings that need user response."""
        state = {
            "case_context": {"waiting_for_input": False},
            "confidence_score": 0.7,
            "findings": [{"requires_user_response": True}],
            "current_phase": "validate_hypothesis_completed",
        }

        decision = self.agent._decide_if_user_update_is_needed(state)
        assert (
            decision == "respond_to_user"
        )  # Should respond to user when findings require response

    @pytest.mark.asyncio
    async def test_define_blast_radius_node_success(self):
        """Test define_blast_radius node execution."""
        # Mock doctrine execution
        mock_phase_result = {
            "phase": "define_blast_radius",
            "status": "completed",
            "key_insight": "Issue affects authentication system",
            "follow_up_question": "Are multiple users affected?",
            "requires_user_input": True,
            "next_phase": "establish_timeline",
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            # Use AsyncMock for async method
            mock_doctrine.execute_phase = AsyncMock(return_value=mock_phase_result)

            # Execute node
            result = await self.agent.define_blast_radius(self.sample_agent_state)

            # Verify doctrine was called correctly with expected context
            expected_context = {
                "llm_router": self.agent.llm_router,
                "knowledge_base_tool": self.agent.knowledge_base_tool,
                "web_search_tool": self.agent.web_search_tool,
                "uploaded_data": [],
            }

            mock_doctrine.execute_phase.assert_called_once_with(
                Phase.DEFINE_BLAST_RADIUS, self.sample_agent_state, expected_context
            )

            # Check result - the method returns an AgentState object, not a dict
            assert result["current_phase"] == "define_blast_radius_completed"
            assert "define_blast_radius_results" in result["case_context"]

    @pytest.mark.asyncio
    async def test_establish_timeline_node_success(self):
        """Test establish_timeline node execution."""
        mock_phase_result = {
            "phase": "establish_timeline",
            "status": "completed",
            "key_insight": "Issue started after 2 PM deployment",
            "follow_up_question": "Were there database changes?",
            "requires_user_input": True,
            "next_phase": "formulate_hypothesis",
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase = AsyncMock(return_value=mock_phase_result)

            result = await self.agent.establish_timeline(self.sample_agent_state)

            expected_context = {
                "knowledge_base_tool": self.agent.knowledge_base_tool,
                "uploaded_data": [],
            }

            mock_doctrine.execute_phase.assert_called_once_with(
                Phase.ESTABLISH_TIMELINE, self.sample_agent_state, expected_context
            )

            assert result["current_phase"] == "establish_timeline_completed"
            assert "establish_timeline_results" in result["case_context"]

    @pytest.mark.asyncio
    async def test_formulate_hypothesis_node_success(self):
        """Test formulate_hypothesis node execution."""
        mock_phase_result = {
            "phase": "formulate_hypothesis",
            "status": "completed",
            "key_insight": "Three potential hypotheses identified",
            "follow_up_question": "Which hypothesis should we test?",
            "hypothesis_list": [
                "Connection pool exhaustion",
                "Network timeout",
                "Database lock",
            ],
            "requires_user_input": True,
            "next_phase": "validate_hypothesis",
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase = AsyncMock(return_value=mock_phase_result)

            result = await self.agent.formulate_hypothesis(self.sample_agent_state)

            expected_context = {
                "llm_router": self.agent.llm_router,
                "knowledge_base_tool": self.agent.knowledge_base_tool,
                "web_search_tool": self.agent.web_search_tool,
                "uploaded_data": [],
            }

            mock_doctrine.execute_phase.assert_called_once_with(
                Phase.FORMULATE_HYPOTHESIS, self.sample_agent_state, expected_context
            )

            assert result["current_phase"] == "formulate_hypothesis_completed"
            assert "formulate_hypothesis_results" in result["case_context"]

    @pytest.mark.asyncio
    async def test_validate_hypothesis_node_success(self):
        """Test validate_hypothesis node execution."""
        mock_phase_result = {
            "phase": "validate_hypothesis",
            "status": "completed",
            "key_insight": "Evidence supports connection pool exhaustion",
            "follow_up_question": "Does this match your observations?",
            "validated_hypothesis": "Connection pool exhaustion",
            "requires_user_input": True,
            "next_phase": "propose_solution",
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase = AsyncMock(return_value=mock_phase_result)

            result = await self.agent.validate_hypothesis(self.sample_agent_state)

            expected_context = {
                "llm_router": self.agent.llm_router,
                "knowledge_base_tool": self.agent.knowledge_base_tool,
                "web_search_tool": self.agent.web_search_tool,
                "uploaded_data": [],
            }

            mock_doctrine.execute_phase.assert_called_once_with(
                Phase.VALIDATE_HYPOTHESIS, self.sample_agent_state, expected_context
            )

            assert result["current_phase"] == "validate_hypothesis_completed"
            assert "validate_hypothesis_results" in result["case_context"]

    @pytest.mark.asyncio
    async def test_propose_solution_node_success(self):
        """Test propose_solution node execution."""
        mock_phase_result = {
            "phase": "propose_solution",
            "status": "completed",
            "key_insight": "Multiple solution approaches available",
            "follow_up_question": "Which solution should we implement?",
            "solution_options": [
                "Increase connection pool size",
                "Optimize queries",
                "Add connection monitoring",
            ],
            "requires_user_input": True,
            "next_phase": None,
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase = AsyncMock(return_value=mock_phase_result)

            result = await self.agent.propose_solution(self.sample_agent_state)

            expected_context = {
                "llm_router": self.agent.llm_router,
                "knowledge_base_tool": self.agent.knowledge_base_tool,
                "web_search_tool": self.agent.web_search_tool,
                "uploaded_data": [],
            }

            mock_doctrine.execute_phase.assert_called_once_with(
                Phase.PROPOSE_SOLUTION, self.sample_agent_state, expected_context
            )

            assert result["current_phase"] == "propose_solution_completed"
            assert "propose_solution_results" in result["case_context"]

    @pytest.mark.asyncio
    async def test_respond_to_user_node(self):
        """Test respond_to_user node execution."""
        agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database timeout",
            findings=[],
            case_context={
                "define_blast_radius_results": {
                    "key_insight": "System-wide authentication issue",
                    "follow_up_question": "Are multiple users affected?",
                },
                "agent_response": "System-wide authentication issue\n\nAre multiple users affected?",
            },
            awaiting_user_input=False,
            current_phase="define_blast_radius",
            user_feedback="",
        )

        context = {}

        result = await self.agent.respond_to_user(agent_state, context)

        # Should format response from phase results
        assert "last_agent_response" in result["case_context"]
        assert (
            "System-wide authentication issue"
            in result["case_context"]["last_agent_response"]
        )
        assert (
            "Are multiple users affected?"
            in result["case_context"]["last_agent_response"]
        )

    @pytest.mark.asyncio
    async def test_await_user_input_node(self):
        """Test await_user_input node execution."""
        agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database timeout",
            findings=[],
            case_context={},
            awaiting_user_input=False,
            current_phase="define_blast_radius",
            user_feedback="",
        )

        context = {}

        result = await self.agent.await_user_input(agent_state, context)

        # Should set status in case_context
        assert result["case_context"]["status"] == "awaiting_user_input"
        assert "pause_timestamp" in result["case_context"]

    @pytest.mark.asyncio
    async def test_node_execution_with_error(self):
        """Test node execution with error handling."""
        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase = AsyncMock(
                side_effect=Exception("LLM service error")
            )

            result = await self.agent.define_blast_radius(self.sample_agent_state)

            # Should handle error gracefully
            assert "blast_radius_error" in result["case_context"]
            assert (
                "LLM service error"
                in result["case_context"]["blast_radius_error"]
            )
            assert (
                "agent_response" in result["case_context"]
            )  # Should set error response

    @pytest.mark.asyncio
    async def test_resume_method(self):
        """Test the resume method for continuing after user feedback."""
        agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database timeout",
            findings=[],
            case_context={},
            awaiting_user_input=True,
            current_phase="define_blast_radius",
            user_feedback="Yes, multiple users are affected",
        )

        # Create a mock state object that mimics LangGraph's StateSnapshot structure
        mock_state_snapshot = Mock()
        mock_state_snapshot.values = agent_state

        # Mock the compiled graph execution
        with patch.object(self.agent.compiled_graph, "get_state") as mock_get_state:
            with patch.object(self.agent.compiled_graph, "ainvoke") as mock_ainvoke:
                mock_get_state.return_value = mock_state_snapshot
                mock_ainvoke.return_value = {"final_result": "success"}

                result = await self.agent.resume(
                    "test-session-123", "Yes, multiple users are affected"
                )

                # Should call compiled_graph.ainvoke with updated state
                mock_ainvoke.assert_called_once()
                assert result["final_result"] == "success"

    def test_dangerous_operation_detection(self):
        """Test detection of dangerous operations."""
        dangerous_phrases = [
            "restart database",
            "delete records",
            "truncate table",
            "drop table",
            "shutdown system",
            "kill process",
            "format disk",
            "remove files",
        ]

        for phrase in dangerous_phrases:
            phase_result = {
                "key_insight": f"We need to {phrase} to fix the issue",
                "follow_up_question": "Should we proceed?",
                "requires_user_input": False,
                "confidence_score": 0.8,
            }

            decision = self.agent._decide_if_user_update_is_needed(phase_result)
            assert (
                decision == "respond_to_user"
            ), f"Should detect '{phrase}' as dangerous"

    def test_uncertainty_detection(self):
        """Test detection of uncertainty in responses."""
        uncertain_phrases = [
            "unable to determine",
            "not sure",
            "unclear",
            "insufficient information",
            "need more data",
            "uncertain about",
        ]

        for phrase in uncertain_phrases:
            phase_result = {
                "key_insight": f"We are {phrase} the root cause",
                "follow_up_question": "What should we check next?",
                "requires_user_input": False,
                "confidence_score": 0.7,
            }

            decision = self.agent._decide_if_user_update_is_needed(phase_result)
            assert (
                decision == "respond_to_user"
            ), f"Should detect '{phrase}' as uncertain"

    @pytest.mark.asyncio
    async def test_state_management_across_phases(self):
        """Test state management across different phases."""
        # Start with initial state
        agent_state = AgentState(
            session_id="test-session-123",
            user_query="Database timeout",
            findings=[],
            case_context={},
            awaiting_user_input=False,
            current_phase="define_blast_radius",
            user_feedback="",
        )

        # Mock phase results
        blast_radius_result = {
            "phase": "define_blast_radius",
            "status": "completed",
            "key_insight": "System-wide issue identified",
            "follow_up_question": "Are multiple services affected?",
            "requires_user_input": True,
            "next_phase": "establish_timeline",
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            mock_doctrine.execute_phase = AsyncMock(return_value=blast_radius_result)

            # Execute blast radius phase
            result = await self.agent.define_blast_radius(agent_state)

            # Verify state was updated with phase results
            assert result["current_phase"] == "define_blast_radius_completed"
            assert "define_blast_radius_results" in result["case_context"]
            assert (
                result["case_context"]["define_blast_radius_results"]
                == blast_radius_result
            )

    def test_graph_edge_configuration(self):
        """Test that graph edges are configured correctly."""
        # This tests the routing logic between nodes
        edges = self.agent.graph.edges

        # Should have proper connections between phase nodes
        # The exact structure depends on the graph implementation
        assert len(edges) > 0  # Should have edges defined

        # Test that decision points exist
        # The graph should route based on needs_user_input

    @pytest.mark.asyncio
    async def test_full_workflow_simulation(self):
        """Test a full workflow simulation through multiple phases."""
        # This is an integration test that simulates going through phases
        initial_state = AgentState(
            session_id="test-session-123",
            user_query="Database connection issues",
            findings=[],
            case_context={},
            awaiting_user_input=False,
            current_phase="define_blast_radius",
            user_feedback="",
        )

        # Mock all phase results
        phase_results = {
            "define_blast_radius": {
                "phase": "define_blast_radius",
                "status": "completed",
                "key_insight": "Authentication system affected",
                "follow_up_question": "Are login services down?",
                "requires_user_input": True,
                "next_phase": "establish_timeline",
            },
            "establish_timeline": {
                "phase": "establish_timeline",
                "status": "completed",
                "key_insight": "Started after deployment",
                "follow_up_question": "What was deployed?",
                "requires_user_input": True,
                "next_phase": "formulate_hypothesis",
            },
        }

        with patch.object(self.agent, "doctrine") as mock_doctrine:
            # Configure mock to return different results based on phase
            async def mock_execute_phase(phase, state, context):
                return phase_results.get(phase.value, {})

            mock_doctrine.execute_phase = AsyncMock(side_effect=mock_execute_phase)

            # Execute blast radius phase
            result1 = await self.agent.define_blast_radius(initial_state)
            assert result1["current_phase"] == "define_blast_radius_completed"
            assert "define_blast_radius_results" in result1["case_context"]

            # Simulate moving to next phase
            updated_state = AgentState(
                session_id="test-session-123",
                user_query="Database connection issues",
                findings=[],
                case_context={
                    "define_blast_radius_results": result1["case_context"][
                        "define_blast_radius_results"
                    ]
                },
                awaiting_user_input=False,
                current_phase="establish_timeline",
                user_feedback="Yes, login services are down",
            )

            # Execute timeline phase
            result2 = await self.agent.establish_timeline(updated_state)
            assert result2["current_phase"] == "establish_timeline_completed"
            assert "establish_timeline_results" in result2["case_context"]

            # Verify progression - check the actual stored results
            blast_results = result1["case_context"][
                "define_blast_radius_results"
            ]
            timeline_results = result2["case_context"][
                "establish_timeline_results"
            ]
            assert blast_results["next_phase"] == "establish_timeline"
            assert timeline_results["next_phase"] == "formulate_hypothesis"

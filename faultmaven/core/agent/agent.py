"""core_agent.py

Purpose: Enhanced LangGraph-based agent orchestrator with human-in-the-loop

Requirements:
--------------------------------------------------------------------------------
• Implement FaultMavenAgent class with granular state machine
• Build StateGraph with phase-specific nodes and conditional edges
• Implement interruption mechanism for user feedback
• Add tool safety protocols for dangerous operations
• Follow "Single Insight, Single Question" rule

Key Components:
--------------------------------------------------------------------------------
  class FaultMavenAgent: ...
  def _build_agent_graph() -> CompiledGraph

Technology Stack:
--------------------------------------------------------------------------------
LangGraph, LangChain

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
• Human-in-the-Loop: Pause for user feedback at critical points
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.models import AgentState
from faultmaven.models.interfaces import ILLMProvider, BaseTool
from faultmaven.infrastructure.observability.tracing import trace
from .doctrine import Phase, TroubleshootingDoctrine
from faultmaven.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.tools.web_search import WebSearchTool


class FaultMavenAgent:
    """Enhanced agent orchestrator with human-in-the-loop capability"""

    def __init__(
        self,
        llm_interface: Optional[ILLMProvider] = None,
        tools: Optional[List[BaseTool]] = None,
        # Backward compatibility parameters
        llm_router: Optional[LLMRouter] = None,
        knowledge_base_tool: Optional[KnowledgeBaseTool] = None,
        web_search_tool: Optional[WebSearchTool] = None,
    ):
        self.logger = logging.getLogger(__name__)
        
        # Handle interface vs concrete implementation
        if llm_interface:
            self.llm_router = llm_interface
        elif llm_router:
            self.llm_router = llm_router
        else:
            # Default to LLMRouter if nothing provided
            self.llm_router = LLMRouter()
            
        # Handle tools - prefer interface-based tools
        if tools:
            self.tools = tools
        else:
            # Backward compatibility - create tools from concrete implementations
            self.tools = []
            if knowledge_base_tool:
                self.tools.append(knowledge_base_tool)
            if web_search_tool and web_search_tool.is_available():
                self.tools.append(web_search_tool)
                
        # Store concrete implementations for backward compatibility
        self.knowledge_base_tool = knowledge_base_tool
        self.web_search_tool = web_search_tool
        self.doctrine = TroubleshootingDoctrine()

        # Build the agent graph
        self.graph = self._build_agent_graph()
        self.compiled_graph = self.graph.compile(
            checkpointer=MemorySaver(), interrupt_before=["await_user_input"]
        )

        # Tool safety configuration
        self.dangerous_tools = {
            "system_restart": True,
            "database_reset": True,
            "network_config_change": True,
            "permission_change": True,
        }

    def _build_agent_graph(self) -> StateGraph:
        """
        Build the enhanced LangGraph state machine with granular nodes

        Returns:
            Configured StateGraph
        """
        # Create the state graph
        workflow = StateGraph(AgentState)

        # Add granular nodes for each phase
        workflow.add_node("triage", self._triage_node)
        workflow.add_node("define_blast_radius", self._define_blast_radius_node)
        workflow.add_node("establish_timeline", self._establish_timeline_node)
        workflow.add_node("formulate_hypothesis", self._formulate_hypothesis_node)
        workflow.add_node("validate_hypothesis", self._validate_hypothesis_node)
        workflow.add_node("propose_solution", self._propose_solution_node)
        workflow.add_node("respond_to_user", self._respond_to_user_node)
        workflow.add_node("get_user_confirmation", self._get_user_confirmation_node)
        workflow.add_node("await_user_input", self._await_user_input_node)

        # Set entry point
        workflow.set_entry_point("triage")

        # Add conditional edges with interruption mechanism
        workflow.add_conditional_edges(
            "triage",
            self._should_start_investigation,
            {
                "define_blast_radius": "define_blast_radius",
                "respond_to_user": "respond_to_user",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "define_blast_radius",
            self._decide_if_user_update_is_needed,
            {
                "establish_timeline": "establish_timeline",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "establish_timeline",
            self._decide_if_user_update_is_needed,
            {
                "formulate_hypothesis": "formulate_hypothesis",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "formulate_hypothesis",
            self._decide_if_user_update_is_needed,
            {
                "validate_hypothesis": "validate_hypothesis",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "validate_hypothesis",
            self._decide_if_user_update_is_needed,
            {
                "propose_solution": "propose_solution",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "propose_solution",
            self._decide_if_user_update_is_needed,
            {
                "respond_to_user": "respond_to_user",
                "get_user_confirmation": "get_user_confirmation",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "respond_to_user",
            self._should_continue_after_response,
            {"await_user_input": "await_user_input", END: END},
        )

        workflow.add_conditional_edges(
            "get_user_confirmation",
            self._should_continue_after_confirmation,
            {
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END,
            },
        )

        workflow.add_conditional_edges(
            "await_user_input",
            self._process_user_input,
            {
                "define_blast_radius": "define_blast_radius",
                "establish_timeline": "establish_timeline",
                "formulate_hypothesis": "formulate_hypothesis",
                "validate_hypothesis": "validate_hypothesis",
                "propose_solution": "propose_solution",
                "respond_to_user": "respond_to_user",
                END: END,
            },
        )

        return workflow

    @trace("agent_triage_node")
    async def _triage_node(self, state: AgentState) -> AgentState:
        """
        Enhanced triage node with better state initialization

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing triage node")

        # Initialize state if needed
        if not state.get("investigation_context"):
            state["investigation_context"] = {}

        if not state.get("findings"):
            state["findings"] = []

        if not state.get("recommendations"):
            state["recommendations"] = []

        if not state.get("tools_used"):
            state["tools_used"] = []

        # Initialize interaction tracking
        state["investigation_context"]["interaction_count"] = 0
        state["investigation_context"]["last_user_input"] = None
        state["investigation_context"]["waiting_for_input"] = False

        # Analyze the user query
        triage_prompt = f"""
        Analyze this troubleshooting request and provide an initial assessment:
        
        User Query: {state.get('user_query', '')}
        
        Follow the "Single Insight, Single Question" rule:
        1. Present ONE key finding about the severity/urgency
        2. Ask ONE clear question to proceed
        
        Keep your response concise and actionable.
        """

        try:
            response = await self.llm_router.route(
                prompt=triage_prompt, max_tokens=300, temperature=0.3
            )

            # Extract key insight and question
            content = response.content
            severity = self._extract_severity(content)

            # Update state with triage results
            state["investigation_context"]["triage_assessment"] = content
            state["investigation_context"]["severity"] = severity
            state["current_phase"] = "triage_completed"
            state["confidence_score"] = 0.6

            # Add to findings
            state["findings"].append(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "phase": "triage",
                    "finding": f"Initial assessment: {severity} severity",
                    "details": content,
                    "requires_user_response": True,
                }
            )

            # Set response for user
            state["investigation_context"]["agent_response"] = content
            state["investigation_context"]["waiting_for_input"] = True

        except Exception as e:
            self.logger.error(f"Triage failed: {e}")
            state["investigation_context"]["triage_error"] = str(e)
            state["confidence_score"] = 0.3

            # Check if this is an LLM provider failure
            if "All LLM providers failed" in str(
                e
            ) or "failed or returned low confidence" in str(e):
                # Provide a basic triage assessment without LLM
                basic_assessment = self._basic_triage_fallback(
                    state.get("user_query", "")
                )
                state["investigation_context"]["triage_assessment"] = basic_assessment
                state["investigation_context"]["severity"] = "medium"
                state["current_phase"] = "triage_completed"

                # Add basic finding
                state["findings"].append(
                    {
                        "timestamp": datetime.utcnow().isoformat(),
                        "phase": "triage",
                        "finding": "Basic triage assessment (LLM providers unavailable)",
                        "details": basic_assessment,
                        "requires_user_response": False,
                    }
                )

                # Set a helpful response
                state["investigation_context"]["agent_response"] = basic_assessment
                state["investigation_context"]["waiting_for_input"] = False
            else:
                # For other errors, request more info
                state["investigation_context"][
                    "agent_response"
                ] = f"I encountered an issue during triage: {str(e)}. Can you provide more details about the problem?"
                state["investigation_context"]["waiting_for_input"] = True

        return state

    async def _define_blast_radius_node(self, state: AgentState) -> AgentState:
        """
        Execute the blast radius definition phase

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing define blast radius node")

        # Execute the doctrine phase
        context = {
            "llm_router": self.llm_router,
            "knowledge_base_tool": self.knowledge_base_tool,
            "web_search_tool": self.web_search_tool,
            "uploaded_data": state.get("investigation_context", {}).get(
                "uploaded_data", []
            ),
        }

        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.DEFINE_BLAST_RADIUS, state, context
            )

            # Update state with phase results
            state["current_phase"] = "define_blast_radius_completed"
            state["confidence_score"] = phase_result.get("confidence_score", 0.5)

            # Add findings
            if phase_result.get("findings"):
                for finding in phase_result["findings"]:
                    state["findings"].append(
                        {
                            "timestamp": datetime.utcnow().isoformat(),
                            "phase": "define_blast_radius",
                            "finding": finding,
                            "details": phase_result,
                        }
                    )

            # Store phase results
            state["investigation_context"]["define_blast_radius_results"] = phase_result

            # Set response following "Single Insight, Single Question" rule
            key_insight = phase_result.get(
                "key_insight", "Blast radius assessment completed"
            )
            follow_up_question = phase_result.get(
                "follow_up_question", "Should we proceed to establish the timeline?"
            )

            state["investigation_context"][
                "agent_response"
            ] = f"{key_insight}\n\n{follow_up_question}"
            state["investigation_context"]["waiting_for_input"] = phase_result.get(
                "requires_user_input", False
            )

        except Exception as e:
            self.logger.error(f"Blast radius phase failed: {e}")
            state["investigation_context"]["blast_radius_error"] = str(e)
            state["investigation_context"][
                "agent_response"
            ] = f"I encountered an issue assessing the blast radius: {str(e)}. Can you provide more information about the affected systems?"

        return state

    async def _establish_timeline_node(self, state: AgentState) -> AgentState:
        """
        Execute the timeline establishment phase

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing establish timeline node")

        context = {
            "knowledge_base_tool": self.knowledge_base_tool,
            "uploaded_data": state.get("investigation_context", {}).get(
                "uploaded_data", []
            ),
        }

        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.ESTABLISH_TIMELINE, state, context
            )

            # Update state
            state["current_phase"] = "establish_timeline_completed"
            state["confidence_score"] = phase_result.get("confidence_score", 0.5)

            # Add findings
            if phase_result.get("findings"):
                for finding in phase_result["findings"]:
                    state["findings"].append(
                        {
                            "timestamp": datetime.utcnow().isoformat(),
                            "phase": "establish_timeline",
                            "finding": finding,
                            "details": phase_result,
                        }
                    )

            # Store phase results
            state["investigation_context"]["establish_timeline_results"] = phase_result

            # Set response
            key_insight = phase_result.get("key_insight", "Timeline analysis completed")
            follow_up_question = phase_result.get(
                "follow_up_question", "Should we proceed to formulate hypotheses?"
            )

            state["investigation_context"][
                "agent_response"
            ] = f"{key_insight}\n\n{follow_up_question}"
            state["investigation_context"]["waiting_for_input"] = phase_result.get(
                "requires_user_input", False
            )

        except Exception as e:
            self.logger.error(f"Timeline phase failed: {e}")
            state["investigation_context"]["timeline_error"] = str(e)
            state["investigation_context"][
                "agent_response"
            ] = f"I encountered an issue establishing the timeline: {str(e)}. Can you provide more details about when the issue started?"

        return state

    @trace("agent_formulate_hypothesis_node")
    async def _formulate_hypothesis_node(self, state: AgentState) -> AgentState:
        """
        Execute the hypothesis formulation phase

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing formulate hypothesis node")

        context = {
            "llm_router": self.llm_router,
            "knowledge_base_tool": self.knowledge_base_tool,
            "web_search_tool": self.web_search_tool,
            "uploaded_data": state.get("investigation_context", {}).get(
                "uploaded_data", []
            ),
        }

        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.FORMULATE_HYPOTHESIS, state, context
            )

            # Update state
            state["current_phase"] = "formulate_hypothesis_completed"
            state["confidence_score"] = phase_result.get("confidence_score", 0.5)

            # Add findings
            if phase_result.get("findings"):
                for finding in phase_result["findings"]:
                    state["findings"].append(
                        {
                            "timestamp": datetime.utcnow().isoformat(),
                            "phase": "formulate_hypothesis",
                            "finding": finding,
                            "details": phase_result,
                        }
                    )

            # Store phase results
            state["investigation_context"][
                "formulate_hypothesis_results"
            ] = phase_result

            # Set response
            key_insight = phase_result.get("key_insight", "Hypotheses formulated")
            follow_up_question = phase_result.get(
                "follow_up_question", "Should we validate these hypotheses?"
            )

            state["investigation_context"][
                "agent_response"
            ] = f"{key_insight}\n\n{follow_up_question}"
            state["investigation_context"]["waiting_for_input"] = phase_result.get(
                "requires_user_input", False
            )

        except Exception as e:
            self.logger.error(f"Hypothesis formulation phase failed: {e}")
            state["investigation_context"]["hypothesis_error"] = str(e)
            state["investigation_context"][
                "agent_response"
            ] = f"I encountered an issue formulating hypotheses: {str(e)}. Can you provide more symptoms or error details?"

        return state

    @trace("agent_validate_hypothesis_node")
    async def _validate_hypothesis_node(self, state: AgentState) -> AgentState:
        """
        Execute the hypothesis validation phase

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing validate hypothesis node")

        context = {
            "llm_router": self.llm_router,
            "knowledge_base_tool": self.knowledge_base_tool,
            "web_search_tool": self.web_search_tool,
            "uploaded_data": state.get("investigation_context", {}).get(
                "uploaded_data", []
            ),
        }

        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.VALIDATE_HYPOTHESIS, state, context
            )

            # Update state
            state["current_phase"] = "validate_hypothesis_completed"
            state["confidence_score"] = phase_result.get("confidence_score", 0.5)

            # Add findings
            if phase_result.get("findings"):
                for finding in phase_result["findings"]:
                    state["findings"].append(
                        {
                            "timestamp": datetime.utcnow().isoformat(),
                            "phase": "validate_hypothesis",
                            "finding": finding,
                            "details": phase_result,
                        }
                    )

            # Store phase results
            state["investigation_context"]["validate_hypothesis_results"] = phase_result

            # Set response
            key_insight = phase_result.get(
                "key_insight", "Hypothesis validation completed"
            )
            follow_up_question = phase_result.get(
                "follow_up_question", "Should we proceed to propose solutions?"
            )

            state["investigation_context"][
                "agent_response"
            ] = f"{key_insight}\n\n{follow_up_question}"
            state["investigation_context"]["waiting_for_input"] = phase_result.get(
                "requires_user_input", False
            )

        except Exception as e:
            self.logger.error(f"Hypothesis validation phase failed: {e}")
            state["investigation_context"]["validation_error"] = str(e)
            state["investigation_context"][
                "agent_response"
            ] = f"I encountered an issue validating hypotheses: {str(e)}. Can you provide additional data to test our theories?"

        return state

    @trace("agent_propose_solution_node")
    async def _propose_solution_node(self, state: AgentState) -> AgentState:
        """
        Execute the solution proposal phase

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing propose solution node")

        context = {
            "llm_router": self.llm_router,
            "knowledge_base_tool": self.knowledge_base_tool,
            "web_search_tool": self.web_search_tool,
            "uploaded_data": state.get("investigation_context", {}).get(
                "uploaded_data", []
            ),
        }

        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.PROPOSE_SOLUTION, state, context
            )

            # Update state
            state["current_phase"] = "propose_solution_completed"
            state["confidence_score"] = phase_result.get("confidence_score", 0.5)

            # Add findings
            if phase_result.get("findings"):
                for finding in phase_result["findings"]:
                    state["findings"].append(
                        {
                            "timestamp": datetime.utcnow().isoformat(),
                            "phase": "propose_solution",
                            "finding": finding,
                            "details": phase_result,
                        }
                    )

            # Store phase results
            state["investigation_context"]["propose_solution_results"] = phase_result

            # Set response
            key_insight = phase_result.get("key_insight", "Solution proposal completed")
            follow_up_question = phase_result.get(
                "follow_up_question",
                "Would you like to proceed with implementing this solution?",
            )

            state["investigation_context"][
                "agent_response"
            ] = f"{key_insight}\n\n{follow_up_question}"
            state["investigation_context"]["waiting_for_input"] = phase_result.get(
                "requires_user_input", False
            )

        except Exception as e:
            self.logger.error(f"Solution proposal phase failed: {e}")
            state["investigation_context"]["solution_error"] = str(e)
            state["investigation_context"][
                "agent_response"
            ] = f"I encountered an issue proposing solutions: {str(e)}. Can you provide more context about your constraints or requirements?"

        return state

    async def _respond_to_user_node(self, state: AgentState) -> AgentState:
        """
        Respond to user with current findings and question

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing respond to user node")

        # Get the prepared response
        response = state.get("investigation_context", {}).get(
            "agent_response", "I need more information to proceed."
        )

        # Update interaction count
        interaction_count = (
            state.get("investigation_context", {}).get("interaction_count", 0) + 1
        )
        state["investigation_context"]["interaction_count"] = interaction_count

        # Mark that we've responded
        state["investigation_context"]["last_agent_response"] = response
        state["investigation_context"][
            "response_timestamp"
        ] = datetime.utcnow().isoformat()

        # Set waiting for input
        state["investigation_context"]["waiting_for_input"] = True

        self.logger.info(f"Agent response: {response}")

        return state

    async def _get_user_confirmation_node(self, state: AgentState) -> AgentState:
        """
        Request user confirmation for potentially dangerous operations

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing get user confirmation node")

        # Generate confirmation request
        confirmation_prompt = """
        Based on the current investigation, I need to confirm before proceeding with potentially impactful actions.
        
        What I found:
        - The solution involves system changes that could affect operations
        - I recommend proceeding with caution
        
        Do you want me to proceed with the recommended solution? (yes/no)
        """

        state["investigation_context"]["agent_response"] = confirmation_prompt
        state["investigation_context"]["waiting_for_input"] = True
        state["investigation_context"]["confirmation_required"] = True

        return state

    async def _await_user_input_node(self, state: AgentState) -> AgentState:
        """
        Wait for user input - this is where the graph pauses

        Args:
            state: Current agent state

        Returns:
            Updated agent state
        """
        self.logger.info("Executing await user input node")

        # This node represents a pause in the graph
        # The graph will wait here until new input is provided
        state["investigation_context"]["status"] = "awaiting_user_input"
        state["investigation_context"][
            "pause_timestamp"
        ] = datetime.utcnow().isoformat()

        return state

    def _should_start_investigation(self, state: AgentState) -> str:
        """Determine if we should start the investigation"""
        severity = state.get("investigation_context", {}).get("severity", "low")

        if severity in ["high", "critical"]:
            return "define_blast_radius"
        elif severity == "medium":
            return "respond_to_user"
        else:
            return "respond_to_user"

    def _decide_if_user_update_is_needed(self, state: AgentState) -> str:
        """
        Critical decision point: determine if user input is needed

        This implements the interruption mechanism for human-in-the-loop capability
        """
        # Check if the phase explicitly requires user input
        if state.get("investigation_context", {}).get("waiting_for_input", False):
            return "respond_to_user"

        # Check confidence score - if low, ask for user input
        confidence = state.get("confidence_score", 0.0)
        if confidence < 0.4:
            return "respond_to_user"

        # Check if we found significant findings that need user confirmation
        findings = state.get("findings", [])
        if findings:
            last_finding = findings[-1]
            if last_finding.get("requires_user_response", False):
                return "respond_to_user"

        # Check phase completion and proceed to next phase
        current_phase = state.get("current_phase", "")

        if current_phase == "define_blast_radius_completed":
            return "establish_timeline"
        elif current_phase == "establish_timeline_completed":
            return "formulate_hypothesis"
        elif current_phase == "formulate_hypothesis_completed":
            return "validate_hypothesis"
        elif current_phase == "validate_hypothesis_completed":
            return "propose_solution"
        elif current_phase == "propose_solution_completed":
            return "respond_to_user"
        else:
            return "respond_to_user"

    def _should_continue_after_response(self, state: AgentState) -> str:
        """Determine next step after responding to user"""
        # Always wait for user input after responding
        return "await_user_input"

    def _should_continue_after_confirmation(self, state: AgentState) -> str:
        """Determine next step after user confirmation"""
        # Check if user confirmed
        last_input = (
            state.get("investigation_context", {}).get("last_user_input", "") or ""
        )
        if "yes" in last_input.lower():
            return "respond_to_user"
        else:
            return "await_user_input"

    def _process_user_input(self, state: AgentState) -> str:
        """
        Process user input and determine next phase

        This is called when the graph resumes after user input
        """
        # Get user input with multiple fallbacks to prevent None
        investigation_context = state.get("investigation_context", {})
        last_user_input = investigation_context.get("last_user_input")

        # Ensure user_input is never None
        user_input = ""
        if last_user_input is not None:
            user_input = str(last_user_input)

        current_phase = state.get("current_phase", "")

        # Update state with user input
        state["investigation_context"]["waiting_for_input"] = False

        # If no user input provided yet (first run), end execution to await input
        if not user_input or user_input.strip() == "":
            return "END"

        # If user wants to continue, proceed to next phase
        if any(word in user_input.lower() for word in ["yes", "continue", "proceed"]):
            if current_phase == "triage_completed":
                return "define_blast_radius"
            elif current_phase == "define_blast_radius_completed":
                return "establish_timeline"
            elif current_phase == "establish_timeline_completed":
                return "formulate_hypothesis"
            elif current_phase == "formulate_hypothesis_completed":
                return "validate_hypothesis"
            elif current_phase == "validate_hypothesis_completed":
                return "propose_solution"
            else:
                return "respond_to_user"

        # If user provides more information, stay in current phase or respond
        elif len(user_input.strip()) > 10:  # Substantial input
            return "respond_to_user"

        # Default: respond to user
        return "respond_to_user"

    def _check_tool_safety(self, tool_name: str) -> bool:
        """
        Check if a tool requires user confirmation before execution

        Args:
            tool_name: Name of the tool to check

        Returns:
            True if tool is considered dangerous
        """
        return self.dangerous_tools.get(tool_name, False)

    def _extract_severity(self, content: str) -> str:
        """Extract severity from response content"""
        if not content:
            return "low"

        content_lower = content.lower()

        if "critical" in content_lower:
            return "critical"
        elif "high" in content_lower:
            return "high"
        elif "medium" in content_lower:
            return "medium"
        else:
            return "low"

    def _basic_triage_fallback(self, user_query: str) -> str:
        """
        Provide basic triage assessment when LLM providers are unavailable

        Args:
            user_query: The user's query

        Returns:
            Basic assessment string
        """
        if not user_query:
            return "Unable to perform detailed analysis. Please provide more information about the issue."

        query_lower = user_query.lower()

        # Try to search knowledge base first, even without LLM
        kb_results = ""
        if self.knowledge_base_tool:
            try:
                # Use the working synchronous search from the doctrine approach
                kb_results = self._simple_kb_search(user_query)

                # If we found meaningful results from KB, use them
                if (
                    kb_results
                    and len(kb_results) > 100
                    and "Knowledge Base Results:" in kb_results
                ):
                    # Extract the actual content and include it in the response
                    return f"**Knowledge Base Search Results**\n\n{kb_results}\n\n**Note**: Detailed LLM analysis is currently unavailable, but I found relevant information in the knowledge base above."

            except Exception as e:
                self.logger.error(f"Knowledge base search failed in fallback: {e}")

        # Basic keyword-based assessment if KB search didn't yield meaningful results
        if any(
            word in query_lower
            for word in [
                "down",
                "outage",
                "critical",
                "production",
                "crash",
                "error",
                "timeout",
            ]
        ):
            severity = "high"
            assessment = f"**Initial Assessment**: This appears to be a {severity} severity issue based on the keywords in your query. "
        elif any(
            word in query_lower
            for word in ["slow", "performance", "degraded", "issues"]
        ):
            severity = "medium"
            assessment = f"**Initial Assessment**: This appears to be a {severity} severity performance-related issue. "
        else:
            severity = "low"
            assessment = f"**Initial Assessment**: This appears to be a {severity} severity issue that requires investigation. "

        # Add basic recommendations
        assessment += (
            "Since detailed LLM analysis is currently unavailable, I recommend:\n"
        )
        assessment += "1. Check system status and logs\n"
        assessment += "2. Verify if this affects multiple users or systems\n"
        assessment += "3. Check recent deployments or changes\n"
        assessment += "4. Review monitoring dashboards\n\n"
        assessment += "Please provide more details about the issue including error messages, affected systems, and timeline."

        return assessment

    def _simple_kb_search(self, query: str) -> str:
        """
        Simple synchronous knowledge base search for fallback scenarios

        Args:
            query: Search query

        Returns:
            Search results or empty string if no results
        """
        try:
            # Get the knowledge base ingester directly
            if not hasattr(self.knowledge_base_tool, "_knowledge_ingester"):
                return ""

            knowledge_ingester = self.knowledge_base_tool._knowledge_ingester

            # Generate query embedding using the same model as the ingester
            query_embedding = knowledge_ingester.embedding_model.encode(query).tolist()

            # Search in ChromaDB collection directly (this is synchronous)
            results = knowledge_ingester.collection.query(
                query_embeddings=[query_embedding],
                n_results=5,  # Get more results to increase relevance
                include=["documents", "metadatas", "distances"],
            )

            # Format results
            if results["documents"] and results["documents"][0]:
                formatted_results = []
                for i, doc in enumerate(results["documents"][0]):
                    content = doc.strip()
                    if content:
                        # Increase truncation limit and be smarter about truncation
                        if len(content) > 800:  # Increased from 300 to 800
                            # Try to find a good breaking point around 700 chars
                            break_point = content.rfind(".", 600, 700)
                            if break_point == -1:
                                break_point = content.rfind(" ", 600, 700)
                            if break_point == -1:
                                break_point = 700
                            content = content[:break_point] + "..."
                        formatted_results.append(content)

                if formatted_results:
                    return "Knowledge Base Results:\n" + "\n".join(
                        f"• {res}" for res in formatted_results
                    )

            return ""

        except Exception as e:
            self.logger.error(f"Simple KB search failed: {e}")
            return ""

    @trace("agent_run")
    async def run(
        self,
        query: str,
        session_id: str,
        tools: List[BaseTool],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Run the agent with interface-based parameters (new interface method)

        Args:
            query: User's troubleshooting query
            session_id: Session identifier
            tools: List of tools available to the agent
            context: Optional context information

        Returns:
            Dictionary with agent results
        """
        # Use the existing process_query method which has the right return format
        return await self.process_query(
            query=query,
            session_id=session_id,
            context=context,
            priority="medium"
        )

    @trace("agent_run")
    async def run_legacy(
        self,
        session_id: str,
        user_query: str,
        uploaded_data: Optional[List[Dict[str, Any]]] = None,
    ) -> AgentState:
        """
        Run the agent for a given session and query

        Args:
            session_id: Session identifier
            user_query: User's troubleshooting query
            uploaded_data: Optional uploaded data for analysis

        Returns:
            Final agent state
        """
        self.logger.info(f"Starting agent run for session {session_id}")

        # Initialize state
        initial_state = AgentState(
            session_id=session_id,
            user_query=user_query,
            current_phase="triage",
            investigation_context={
                "uploaded_data": uploaded_data or [],
                "start_time": datetime.utcnow().isoformat(),
                "interaction_count": 0,
                "waiting_for_input": False,
            },
            findings=[],
            recommendations=[],
            confidence_score=0.0,
            tools_used=[],
        )

        try:
            # Run the graph with proper thread configuration for checkpointer
            config = {"configurable": {"thread_id": session_id}}
            final_state = await self.compiled_graph.ainvoke(
                initial_state, config=config
            )

            # Add completion timestamp
            final_state["investigation_context"][
                "end_time"
            ] = datetime.utcnow().isoformat()

            self.logger.info(f"Agent run completed for session {session_id}")
            return final_state

        except Exception as e:
            self.logger.error(f"Agent run failed for session {session_id}: {e}")

            # Return error state
            error_state = initial_state.copy()
            error_state["investigation_context"]["error"] = str(e)
            error_state["confidence_score"] = 0.0
            error_state["investigation_context"][
                "agent_response"
            ] = f"I encountered an error: {str(e)}. Please try again or provide more details."
            return error_state

    @trace("agent_resume")
    async def resume(self, session_id: str, user_input: str) -> AgentState:
        """
        Resume an agent session with user input

        Args:
            session_id: Session identifier
            user_input: User's input to resume with

        Returns:
            Updated agent state
        """
        self.logger.info(
            f"Resuming agent session {session_id} with input: {user_input}"
        )

        try:
            # Get current state with proper thread configuration
            config = {"configurable": {"thread_id": session_id}}
            current_state = self.compiled_graph.get_state(config)

            if not current_state:
                raise ValueError(f"No active session found for {session_id}")

            # Update state with user input
            state_values = current_state.values
            state_values["investigation_context"]["last_user_input"] = user_input
            state_values["investigation_context"][
                "input_timestamp"
            ] = datetime.utcnow().isoformat()

            # Resume the graph
            final_state = await self.compiled_graph.ainvoke(state_values, config=config)

            return final_state

        except Exception as e:
            self.logger.error(f"Failed to resume session {session_id}: {e}")
            raise

    def get_agent_status(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current status of an agent run

        Args:
            session_id: Session identifier

        Returns:
            Agent status information
        """
        try:
            # Get the latest state from the checkpointer with proper thread configuration
            config = {"configurable": {"thread_id": session_id}}
            latest_state = self.compiled_graph.get_state(config)

            if latest_state and latest_state.values:
                state_values = latest_state.values
                return {
                    "session_id": session_id,
                    "current_phase": state_values.get("current_phase"),
                    "confidence_score": state_values.get("confidence_score"),
                    "findings_count": len(state_values.get("findings", [])),
                    "tools_used": state_values.get("tools_used", []),
                    "waiting_for_input": state_values.get(
                        "investigation_context", {}
                    ).get("waiting_for_input", False),
                    "interaction_count": state_values.get(
                        "investigation_context", {}
                    ).get("interaction_count", 0),
                    "last_response": state_values.get("investigation_context", {}).get(
                        "last_agent_response", ""
                    ),
                }

            return None

        except Exception as e:
            self.logger.error(
                f"Failed to get agent status for session {session_id}: {e}"
            )
            return None

    # Public methods for testing - these delegate to the internal node methods
    async def define_blast_radius(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _define_blast_radius_node"""
        return await self._define_blast_radius_node(state)

    async def establish_timeline(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _establish_timeline_node"""
        return await self._establish_timeline_node(state)

    async def formulate_hypothesis(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _formulate_hypothesis_node"""
        return await self._formulate_hypothesis_node(state)

    async def validate_hypothesis(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _validate_hypothesis_node"""
        return await self._validate_hypothesis_node(state)

    async def propose_solution(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _propose_solution_node"""
        return await self._propose_solution_node(state)

    async def respond_to_user(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _respond_to_user_node"""
        return await self._respond_to_user_node(state)

    async def await_user_input(
        self, state: AgentState, context: Dict[str, Any] = None
    ) -> AgentState:
        """Public method for testing - delegates to _await_user_input_node"""
        return await self._await_user_input_node(state)

    async def process_query(
        self,
        query: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
        priority: str = "medium",
    ) -> Dict[str, Any]:
        """
        Process a troubleshooting query and return structured response

        Args:
            query: User's troubleshooting query
            session_id: Session identifier
            context: Optional context information
            priority: Query priority level

        Returns:
            Dictionary with findings, root_cause, recommendations, etc.
        """
        self.logger.info(f"Processing query for session {session_id}: {query}")

        try:
            # Run the agent using legacy method
            final_state = await self.run_legacy(
                session_id=session_id,
                user_query=query,
                uploaded_data=context.get("uploaded_data", []) if context else [],
            )

            # Extract findings and convert to expected format
            findings = []
            for finding in final_state.get("findings", []):
                findings.append(
                    {
                        "type": finding.get("phase", "general"),
                        "message": finding.get("finding", ""),
                        "details": finding.get("details", ""),
                        "timestamp": finding.get(
                            "timestamp", datetime.utcnow().isoformat()
                        ),
                        "severity": "info",
                    }
                )

            # Extract recommendations from the investigation context
            recommendations = final_state.get("recommendations", [])
            if not recommendations:
                # Fallback to extracting from agent response
                agent_response = final_state.get("investigation_context", {}).get(
                    "agent_response", ""
                )
                if agent_response:
                    recommendations = [agent_response]

            # Determine root cause from findings
            root_cause = "Under investigation"
            if findings:
                latest_finding = findings[-1]
                root_cause = latest_finding.get("message", "Under investigation")

            # Calculate confidence score
            confidence_score = final_state.get("confidence_score", 0.5)

            # Generate next steps
            next_steps = ["Continue investigation", "Gather more data"]
            if final_state.get("investigation_context", {}).get(
                "waiting_for_input", False
            ):
                next_steps = ["Awaiting user input", "Provide additional details"]

            return {
                "findings": findings,
                "root_cause": root_cause,
                "recommendations": recommendations,
                "confidence_score": confidence_score,
                "estimated_mttr": self._estimate_mttr(priority, confidence_score),
                "next_steps": next_steps,
            }

        except Exception as e:
            self.logger.error(f"Query processing failed: {e}")
            # Return error response in expected format
            return {
                "findings": [
                    {
                        "type": "error",
                        "message": f"Processing error: {str(e)}",
                        "details": "Please try again or provide more details",
                        "timestamp": datetime.utcnow().isoformat(),
                        "severity": "error",
                    }
                ],
                "root_cause": "Processing error occurred",
                "recommendations": [
                    "Please try again",
                    "Provide more details about the issue",
                ],
                "confidence_score": 0.0,
                "estimated_mttr": "Unknown",
                "next_steps": [
                    "Retry the request",
                    "Contact support if issue persists",
                ],
            }

    def _estimate_mttr(self, priority: str, confidence_score: float) -> str:
        """
        Estimate Mean Time To Resolution based on priority and confidence

        Args:
            priority: Query priority level
            confidence_score: Confidence in the analysis

        Returns:
            Estimated MTTR as string
        """
        base_times = {"critical": 30, "high": 60, "medium": 120, "low": 240}  # minutes

        base_time = base_times.get(priority, 120)

        # Adjust based on confidence - higher confidence means faster resolution
        if confidence_score > 0.8:
            multiplier = 0.5
        elif confidence_score > 0.6:
            multiplier = 0.7
        elif confidence_score > 0.4:
            multiplier = 1.0
        else:
            multiplier = 1.5

        estimated_minutes = int(base_time * multiplier)

        if estimated_minutes < 60:
            return f"{estimated_minutes} minutes"
        else:
            hours = estimated_minutes // 60
            minutes = estimated_minutes % 60
            if minutes == 0:
                return f"{hours} hours"
            else:
                return f"{hours}h {minutes}m"

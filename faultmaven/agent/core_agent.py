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
from typing import Dict, Any, List, Optional
from datetime import datetime

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from ..models import AgentState
from ..llm.router import LLMRouter
from .doctrine import TroubleshootingDoctrine, Phase
from .tools.knowledge_base import KnowledgeBaseTool
from .tools.web_search import WebSearchTool


class FaultMavenAgent:
    """Enhanced agent orchestrator with human-in-the-loop capability"""
    
    def __init__(
        self, 
        llm_router: LLMRouter, 
        knowledge_base_tool: KnowledgeBaseTool,
        web_search_tool: Optional[WebSearchTool] = None
    ):
        self.logger = logging.getLogger(__name__)
        self.llm_router = llm_router
        self.knowledge_base_tool = knowledge_base_tool
        self.web_search_tool = web_search_tool
        self.doctrine = TroubleshootingDoctrine()
        
        # Create tools list for easy access
        self.tools = [knowledge_base_tool]
        if web_search_tool and web_search_tool.is_available():
            self.tools.append(web_search_tool)
        
        # Build the agent graph
        self.graph = self._build_agent_graph()
        self.compiled_graph = self.graph.compile(checkpointer=MemorySaver())
        
        # Tool safety configuration
        self.dangerous_tools = {
            'system_restart': True,
            'database_reset': True,
            'network_config_change': True,
            'permission_change': True
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
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "define_blast_radius",
            self._decide_if_user_update_is_needed,
            {
                "establish_timeline": "establish_timeline",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "establish_timeline",
            self._decide_if_user_update_is_needed,
            {
                "formulate_hypothesis": "formulate_hypothesis",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "formulate_hypothesis",
            self._decide_if_user_update_is_needed,
            {
                "validate_hypothesis": "validate_hypothesis",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "validate_hypothesis",
            self._decide_if_user_update_is_needed,
            {
                "propose_solution": "propose_solution",
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "propose_solution",
            self._decide_if_user_update_is_needed,
            {
                "respond_to_user": "respond_to_user",
                "get_user_confirmation": "get_user_confirmation",
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "respond_to_user",
            self._should_continue_after_response,
            {
                "await_user_input": "await_user_input",
                END: END
            }
        )
        
        workflow.add_conditional_edges(
            "get_user_confirmation",
            self._should_continue_after_confirmation,
            {
                "respond_to_user": "respond_to_user",
                "await_user_input": "await_user_input",
                END: END
            }
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
                END: END
            }
        )
        
        return workflow
    
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
        if not state.get('investigation_context'):
            state['investigation_context'] = {}
        
        if not state.get('findings'):
            state['findings'] = []
        
        if not state.get('recommendations'):
            state['recommendations'] = []
        
        if not state.get('tools_used'):
            state['tools_used'] = []
        
        # Initialize interaction tracking
        state['investigation_context']['interaction_count'] = 0
        state['investigation_context']['last_user_input'] = None
        state['investigation_context']['waiting_for_input'] = False
        
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
                prompt=triage_prompt,
                max_tokens=300,
                temperature=0.3
            )
            
            # Extract key insight and question
            content = response.content
            severity = self._extract_severity(content)
            
            # Update state with triage results
            state['investigation_context']['triage_assessment'] = content
            state['investigation_context']['severity'] = severity
            state['current_phase'] = 'triage_completed'
            state['confidence_score'] = 0.6
            
            # Add to findings
            state['findings'].append({
                'timestamp': datetime.utcnow().isoformat(),
                'phase': 'triage',
                'finding': f'Initial assessment: {severity} severity',
                'details': content,
                'requires_user_response': True
            })
            
            # Set response for user
            state['investigation_context']['agent_response'] = content
            state['investigation_context']['waiting_for_input'] = True
            
        except Exception as e:
            self.logger.error(f"Triage failed: {e}")
            state['investigation_context']['triage_error'] = str(e)
            state['confidence_score'] = 0.3
            state['investigation_context']['agent_response'] = f"I encountered an issue during triage: {str(e)}. Can you provide more details about the problem?"
        
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
            'llm_router': self.llm_router,
            'knowledge_base_tool': self.knowledge_base_tool,
            'web_search_tool': self.web_search_tool,
            'uploaded_data': state.get('investigation_context', {}).get('uploaded_data', [])
        }
        
        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.DEFINE_BLAST_RADIUS, state, context
            )
            
            # Update state with phase results
            state['current_phase'] = 'define_blast_radius_completed'
            state['confidence_score'] = phase_result.get('confidence_score', 0.5)
            
            # Add findings
            if phase_result.get('findings'):
                for finding in phase_result['findings']:
                    state['findings'].append({
                        'timestamp': datetime.utcnow().isoformat(),
                        'phase': 'define_blast_radius',
                        'finding': finding,
                        'details': phase_result
                    })
            
            # Store phase results
            state['investigation_context']['define_blast_radius_results'] = phase_result
            
            # Set response following "Single Insight, Single Question" rule
            key_insight = phase_result.get('key_insight', 'Blast radius assessment completed')
            follow_up_question = phase_result.get('follow_up_question', 'Should we proceed to establish the timeline?')
            
            state['investigation_context']['agent_response'] = f"{key_insight}\n\n{follow_up_question}"
            state['investigation_context']['waiting_for_input'] = phase_result.get('requires_user_input', False)
            
        except Exception as e:
            self.logger.error(f"Blast radius phase failed: {e}")
            state['investigation_context']['blast_radius_error'] = str(e)
            state['investigation_context']['agent_response'] = f"I encountered an issue assessing the blast radius: {str(e)}. Can you provide more information about the affected systems?"
        
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
            'knowledge_base_tool': self.knowledge_base_tool,
            'uploaded_data': state.get('investigation_context', {}).get('uploaded_data', [])
        }
        
        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.ESTABLISH_TIMELINE, state, context
            )
            
            # Update state
            state['current_phase'] = 'establish_timeline_completed'
            state['confidence_score'] = phase_result.get('confidence_score', 0.5)
            
            # Add findings
            if phase_result.get('findings'):
                for finding in phase_result['findings']:
                    state['findings'].append({
                        'timestamp': datetime.utcnow().isoformat(),
                        'phase': 'establish_timeline',
                        'finding': finding,
                        'details': phase_result
                    })
            
            # Store phase results
            state['investigation_context']['establish_timeline_results'] = phase_result
            
            # Set response
            key_insight = phase_result.get('key_insight', 'Timeline analysis completed')
            follow_up_question = phase_result.get('follow_up_question', 'Should we proceed to formulate hypotheses?')
            
            state['investigation_context']['agent_response'] = f"{key_insight}\n\n{follow_up_question}"
            state['investigation_context']['waiting_for_input'] = phase_result.get('requires_user_input', False)
            
        except Exception as e:
            self.logger.error(f"Timeline phase failed: {e}")
            state['investigation_context']['timeline_error'] = str(e)
            state['investigation_context']['agent_response'] = f"I encountered an issue establishing the timeline: {str(e)}. Can you provide more details about when the issue started?"
        
        return state
    
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
            'llm_router': self.llm_router,
            'knowledge_base_tool': self.knowledge_base_tool,
            'web_search_tool': self.web_search_tool,
            'uploaded_data': state.get('investigation_context', {}).get('uploaded_data', [])
        }
        
        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.FORMULATE_HYPOTHESIS, state, context
            )
            
            # Update state
            state['current_phase'] = 'formulate_hypothesis_completed'
            state['confidence_score'] = phase_result.get('confidence_score', 0.5)
            
            # Add findings
            if phase_result.get('findings'):
                for finding in phase_result['findings']:
                    state['findings'].append({
                        'timestamp': datetime.utcnow().isoformat(),
                        'phase': 'formulate_hypothesis',
                        'finding': finding,
                        'details': phase_result
                    })
            
            # Store phase results
            state['investigation_context']['formulate_hypothesis_results'] = phase_result
            
            # Set response
            key_insight = phase_result.get('key_insight', 'Hypotheses formulated')
            follow_up_question = phase_result.get('follow_up_question', 'Should we validate these hypotheses?')
            
            state['investigation_context']['agent_response'] = f"{key_insight}\n\n{follow_up_question}"
            state['investigation_context']['waiting_for_input'] = phase_result.get('requires_user_input', False)
            
        except Exception as e:
            self.logger.error(f"Hypothesis formulation phase failed: {e}")
            state['investigation_context']['hypothesis_error'] = str(e)
            state['investigation_context']['agent_response'] = f"I encountered an issue formulating hypotheses: {str(e)}. Can you provide more symptoms or error details?"
        
        return state
    
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
            'llm_router': self.llm_router,
            'knowledge_base_tool': self.knowledge_base_tool,
            'web_search_tool': self.web_search_tool,
            'uploaded_data': state.get('investigation_context', {}).get('uploaded_data', [])
        }
        
        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.VALIDATE_HYPOTHESIS, state, context
            )
            
            # Update state
            state['current_phase'] = 'validate_hypothesis_completed'
            state['confidence_score'] = phase_result.get('confidence_score', 0.5)
            
            # Add findings
            if phase_result.get('findings'):
                for finding in phase_result['findings']:
                    state['findings'].append({
                        'timestamp': datetime.utcnow().isoformat(),
                        'phase': 'validate_hypothesis',
                        'finding': finding,
                        'details': phase_result
                    })
            
            # Store phase results
            state['investigation_context']['validate_hypothesis_results'] = phase_result
            
            # Set response
            key_insight = phase_result.get('key_insight', 'Hypothesis validation completed')
            follow_up_question = phase_result.get('follow_up_question', 'Should we proceed to propose solutions?')
            
            state['investigation_context']['agent_response'] = f"{key_insight}\n\n{follow_up_question}"
            state['investigation_context']['waiting_for_input'] = phase_result.get('requires_user_input', False)
            
        except Exception as e:
            self.logger.error(f"Hypothesis validation phase failed: {e}")
            state['investigation_context']['validation_error'] = str(e)
            state['investigation_context']['agent_response'] = f"I encountered an issue validating hypotheses: {str(e)}. Can you provide additional data to test our theories?"
        
        return state
    
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
            'llm_router': self.llm_router,
            'knowledge_base_tool': self.knowledge_base_tool,
            'web_search_tool': self.web_search_tool,
            'uploaded_data': state.get('investigation_context', {}).get('uploaded_data', [])
        }
        
        try:
            phase_result = await self.doctrine.execute_phase(
                Phase.PROPOSE_SOLUTION, state, context
            )
            
            # Update state
            state['current_phase'] = 'propose_solution_completed'
            state['confidence_score'] = phase_result.get('confidence_score', 0.5)
            
            # Add findings
            if phase_result.get('findings'):
                for finding in phase_result['findings']:
                    state['findings'].append({
                        'timestamp': datetime.utcnow().isoformat(),
                        'phase': 'propose_solution',
                        'finding': finding,
                        'details': phase_result
                    })
            
            # Store phase results
            state['investigation_context']['propose_solution_results'] = phase_result
            
            # Set response
            key_insight = phase_result.get('key_insight', 'Solution proposal completed')
            follow_up_question = phase_result.get('follow_up_question', 'Would you like to proceed with implementing this solution?')
            
            state['investigation_context']['agent_response'] = f"{key_insight}\n\n{follow_up_question}"
            state['investigation_context']['waiting_for_input'] = phase_result.get('requires_user_input', False)
            
        except Exception as e:
            self.logger.error(f"Solution proposal phase failed: {e}")
            state['investigation_context']['solution_error'] = str(e)
            state['investigation_context']['agent_response'] = f"I encountered an issue proposing solutions: {str(e)}. Can you provide more context about your constraints or requirements?"
        
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
        response = state.get('investigation_context', {}).get('agent_response', 'I need more information to proceed.')
        
        # Update interaction count
        interaction_count = state.get('investigation_context', {}).get('interaction_count', 0) + 1
        state['investigation_context']['interaction_count'] = interaction_count
        
        # Mark that we've responded
        state['investigation_context']['last_agent_response'] = response
        state['investigation_context']['response_timestamp'] = datetime.utcnow().isoformat()
        
        # Set waiting for input
        state['investigation_context']['waiting_for_input'] = True
        
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
        
        state['investigation_context']['agent_response'] = confirmation_prompt
        state['investigation_context']['waiting_for_input'] = True
        state['investigation_context']['confirmation_required'] = True
        
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
        state['investigation_context']['status'] = 'awaiting_user_input'
        state['investigation_context']['pause_timestamp'] = datetime.utcnow().isoformat()
        
        return state
    
    def _should_start_investigation(self, state: AgentState) -> str:
        """Determine if we should start the investigation"""
        severity = state.get('investigation_context', {}).get('severity', 'low')
        
        if severity in ['high', 'critical']:
            return "define_blast_radius"
        elif severity == 'medium':
            return "respond_to_user"
        else:
            return "respond_to_user"
    
    def _decide_if_user_update_is_needed(self, state: AgentState) -> str:
        """
        Critical decision point: determine if user input is needed
        
        This implements the interruption mechanism for human-in-the-loop capability
        """
        # Check if the phase explicitly requires user input
        if state.get('investigation_context', {}).get('waiting_for_input', False):
            return "respond_to_user"
        
        # Check confidence score - if low, ask for user input
        confidence = state.get('confidence_score', 0.0)
        if confidence < 0.4:
            return "respond_to_user"
        
        # Check if we found significant findings that need user confirmation
        findings = state.get('findings', [])
        if findings:
            last_finding = findings[-1]
            if last_finding.get('requires_user_response', False):
                return "respond_to_user"
        
        # Check phase completion and proceed to next phase
        current_phase = state.get('current_phase', '')
        
        if current_phase == 'define_blast_radius_completed':
            return "establish_timeline"
        elif current_phase == 'establish_timeline_completed':
            return "formulate_hypothesis"
        elif current_phase == 'formulate_hypothesis_completed':
            return "validate_hypothesis"
        elif current_phase == 'validate_hypothesis_completed':
            return "propose_solution"
        elif current_phase == 'propose_solution_completed':
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
        last_input = state.get('investigation_context', {}).get('last_user_input', '')
        if 'yes' in last_input.lower():
            return "respond_to_user"
        else:
            return "await_user_input"
    
    def _process_user_input(self, state: AgentState) -> str:
        """
        Process user input and determine next phase
        
        This is called when the graph resumes after user input
        """
        user_input = state.get('investigation_context', {}).get('last_user_input', '')
        current_phase = state.get('current_phase', '')
        
        # Update state with user input
        state['investigation_context']['waiting_for_input'] = False
        
        # If user wants to continue, proceed to next phase
        if any(word in user_input.lower() for word in ['yes', 'continue', 'proceed']):
            if current_phase == 'triage_completed':
                return "define_blast_radius"
            elif current_phase == 'define_blast_radius_completed':
                return "establish_timeline"
            elif current_phase == 'establish_timeline_completed':
                return "formulate_hypothesis"
            elif current_phase == 'formulate_hypothesis_completed':
                return "validate_hypothesis"
            elif current_phase == 'validate_hypothesis_completed':
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
        content_lower = content.lower()
        
        if 'critical' in content_lower:
            return 'critical'
        elif 'high' in content_lower:
            return 'high'
        elif 'medium' in content_lower:
            return 'medium'
        else:
            return 'low'
    
    async def run(self, session_id: str, user_query: str, uploaded_data: Optional[List[Dict[str, Any]]] = None) -> AgentState:
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
            current_phase='triage',
            investigation_context={
                'uploaded_data': uploaded_data or [],
                'start_time': datetime.utcnow().isoformat(),
                'interaction_count': 0,
                'waiting_for_input': False
            },
            findings=[],
            recommendations=[],
            confidence_score=0.0,
            tools_used=[]
        )
        
        try:
            # Run the graph
            final_state = await self.compiled_graph.ainvoke(initial_state)
            
            # Add completion timestamp
            final_state['investigation_context']['end_time'] = datetime.utcnow().isoformat()
            
            self.logger.info(f"Agent run completed for session {session_id}")
            return final_state
            
        except Exception as e:
            self.logger.error(f"Agent run failed for session {session_id}: {e}")
            
            # Return error state
            error_state = initial_state.copy()
            error_state['investigation_context']['error'] = str(e)
            error_state['confidence_score'] = 0.0
            error_state['investigation_context']['agent_response'] = f"I encountered an error: {str(e)}. Please try again or provide more details."
            return error_state
    
    async def resume(self, session_id: str, user_input: str) -> AgentState:
        """
        Resume an agent session with user input
        
        Args:
            session_id: Session identifier
            user_input: User's input to resume with
            
        Returns:
            Updated agent state
        """
        self.logger.info(f"Resuming agent session {session_id} with input: {user_input}")
        
        try:
            # Get current state
            current_state = self.compiled_graph.get_state({"session_id": session_id})
            
            if not current_state:
                raise ValueError(f"No active session found for {session_id}")
            
            # Update state with user input
            current_state['investigation_context']['last_user_input'] = user_input
            current_state['investigation_context']['input_timestamp'] = datetime.utcnow().isoformat()
            
            # Resume the graph
            final_state = await self.compiled_graph.ainvoke(current_state)
            
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
            # Get the latest state from the checkpointer
            latest_state = self.compiled_graph.get_state({"session_id": session_id})
            
            if latest_state:
                return {
                    'session_id': session_id,
                    'current_phase': latest_state.get('current_phase'),
                    'confidence_score': latest_state.get('confidence_score'),
                    'findings_count': len(latest_state.get('findings', [])),
                    'tools_used': latest_state.get('tools_used', []),
                    'waiting_for_input': latest_state.get('investigation_context', {}).get('waiting_for_input', False),
                    'interaction_count': latest_state.get('investigation_context', {}).get('interaction_count', 0),
                    'last_response': latest_state.get('investigation_context', {}).get('last_agent_response', '')
                }
            
            return None
            
        except Exception as e:
            self.logger.error(f"Failed to get agent status for session {session_id}: {e}")
            return None
    
    # Public methods for testing - these delegate to the internal node methods
    async def define_blast_radius(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _define_blast_radius_node"""
        return await self._define_blast_radius_node(state)
    
    async def establish_timeline(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _establish_timeline_node"""
        return await self._establish_timeline_node(state)
    
    async def formulate_hypothesis(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _formulate_hypothesis_node"""
        return await self._formulate_hypothesis_node(state)
    
    async def validate_hypothesis(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _validate_hypothesis_node"""
        return await self._validate_hypothesis_node(state)
    
    async def propose_solution(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _propose_solution_node"""
        return await self._propose_solution_node(state)
    
    async def respond_to_user(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _respond_to_user_node"""
        return await self._respond_to_user_node(state)
    
    async def await_user_input(self, state: AgentState, context: Dict[str, Any] = None) -> AgentState:
        """Public method for testing - delegates to _await_user_input_node"""
        return await self._await_user_input_node(state)


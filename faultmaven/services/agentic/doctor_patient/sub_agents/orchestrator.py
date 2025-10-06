"""Diagnostic Orchestrator - Routes queries to appropriate phase agents.

This orchestrator implements the routing layer that:
1. Determines which phase agent should handle the current query
2. Extracts minimal context for that phase
3. Calls the specialized agent
4. Merges agent response with global diagnostic state
5. Determines if phase should advance

Implements Anthropic's sub-agent architecture pattern:
- Main orchestrator with minimal routing logic
- Delegates to specialized sub-agents
- Each sub-agent has optimized context (30-50% smaller)
"""

from typing import Any, Dict, List, Optional
import logging

from faultmaven.models import CaseDiagnosticState, CaseMessage
from .base import PhaseAgent, PhaseContext, PhaseAgentResponse
from .intake_agent import IntakeAgent
from .blast_radius_agent import BlastRadiusAgent
from .timeline_agent import TimelineAgent
from .hypothesis_agent import HypothesisAgent
from .validation_agent import ValidationAgent
from .solution_agent import SolutionAgent

logger = logging.getLogger(__name__)


class DiagnosticOrchestrator:
    """Routes diagnostic queries to appropriate phase-specific agents.

    Architecture:
        User Query → Orchestrator → Phase Agent → Specialized Processing
                                  ↓
                          Global State Update

    Key Principles:
    - Agent selection based on current_phase in diagnostic state
    - Each agent receives minimal, phase-specific context
    - Agents return delta state updates (not full state)
    - Goal-oriented phase advancement (not turn-based)
    """

    def __init__(self, llm_client: Any):
        """Initialize orchestrator with all phase agents.

        Args:
            llm_client: LLM client for agent calls (shared across all agents)
        """
        self.llm_client = llm_client

        # Initialize all phase agents
        self.agents: Dict[int, PhaseAgent] = {
            0: IntakeAgent(llm_client),
            1: BlastRadiusAgent(llm_client),
            2: TimelineAgent(llm_client),
            3: HypothesisAgent(llm_client),
            4: ValidationAgent(llm_client),
            5: SolutionAgent(llm_client),
        }

        logger.info(f"Initialized DiagnosticOrchestrator with {len(self.agents)} phase agents (complete 0-5)")

    async def process_query(
        self,
        user_query: str,
        diagnostic_state: CaseDiagnosticState,
        conversation_history: List[CaseMessage],
        case_id: str
    ) -> PhaseAgentResponse:
        """Process user query by routing to appropriate phase agent.

        Workflow:
        1. Determine current phase from diagnostic state
        2. Get appropriate phase agent
        3. Extract minimal context for that phase
        4. Call agent to process query
        5. Return agent response (caller handles state merge)

        Args:
            user_query: Current user question/statement
            diagnostic_state: Current diagnostic state
            conversation_history: Recent messages for context
            case_id: Case identifier

        Returns:
            PhaseAgentResponse with answer and state updates

        Example:
            >>> orchestrator = DiagnosticOrchestrator(llm_client)
            >>> response = await orchestrator.process_query(
            ...     user_query="API is returning 500 errors",
            ...     diagnostic_state=current_state,
            ...     conversation_history=messages,
            ...     case_id="case-123"
            ... )
            >>> response.answer
            "I see you're experiencing API errors. Let me help diagnose this..."
            >>> response.state_updates
            {"has_active_problem": True, "problem_statement": "API returning 500 errors"}
        """
        current_phase = diagnostic_state.current_phase

        logger.info(
            f"Processing query for case {case_id}, current_phase={current_phase}, "
            f"query_preview='{user_query[:50]}...'"
        )

        # Get appropriate agent
        agent = self._get_agent_for_phase(current_phase)

        if not agent:
            logger.error(f"No agent available for phase {current_phase}")
            return self._create_fallback_response(
                f"Phase {current_phase} agent not yet implemented",
                current_phase
            )

        try:
            # Extract minimal phase-specific context
            context = agent.extract_phase_context(
                full_diagnostic_state=diagnostic_state,
                conversation_history=conversation_history,
                user_query=user_query,
                case_id=case_id
            )

            logger.debug(
                f"Extracted phase context: phase={context.phase}, "
                f"phase_state_keys={list(context.phase_state.keys())}, "
                f"recent_context_count={len(context.recent_context)}"
            )

            # Process with specialized agent
            response = await agent.process(context)

            logger.info(
                f"Agent response: phase_complete={response.phase_complete}, "
                f"confidence={response.confidence:.2f}, "
                f"recommended_next_phase={response.recommended_next_phase}"
            )

            # Check if phase should advance
            if agent.should_advance_phase(context, response):
                logger.info(
                    f"Phase advancement triggered: {current_phase} → {response.recommended_next_phase}"
                )
                # Ensure recommended_next_phase is set in state_updates
                if "current_phase" not in response.state_updates:
                    response.state_updates["current_phase"] = response.recommended_next_phase

            return response

        except Exception as e:
            logger.error(f"Error processing query with {agent.__class__.__name__}: {e}", exc_info=True)
            return self._create_fallback_response(
                f"I encountered an error processing your request. Please try rephrasing.",
                current_phase,
                error=str(e)
            )

    def _get_agent_for_phase(self, phase: int) -> Optional[PhaseAgent]:
        """Get the appropriate agent for the given phase.

        Args:
            phase: Current diagnostic phase (0-5)

        Returns:
            PhaseAgent instance or None if not implemented
        """
        agent = self.agents.get(phase)

        if not agent:
            logger.warning(f"Phase {phase} agent not yet implemented")
            # Fallback: Try to use Intake agent if nothing else available
            if phase == 0:
                return None
            return self.agents.get(0)  # Intake can handle general questions

        return agent

    def _create_fallback_response(
        self,
        message: str,
        current_phase: int,
        error: Optional[str] = None
    ) -> PhaseAgentResponse:
        """Create a fallback response when agent processing fails.

        Args:
            message: Fallback message to user
            current_phase: Current phase number
            error: Optional error details

        Returns:
            PhaseAgentResponse with error handling
        """
        state_updates = {}
        if error:
            logger.error(f"Fallback response due to error: {error}")

        return PhaseAgentResponse(
            answer=message,
            state_updates=state_updates,
            suggested_actions=[],
            suggested_commands=[],
            phase_complete=False,
            confidence=0.5,  # Low confidence for fallback
            recommended_next_phase=current_phase  # Stay in current phase
        )

    def get_phase_info(self, phase: int) -> Dict[str, Any]:
        """Get metadata about a specific phase.

        Args:
            phase: Phase number (0-5)

        Returns:
            Dictionary with phase name, description, goals
        """
        phase_metadata = {
            0: {
                "name": "Intake",
                "description": "Problem identification and triage",
                "goals": [
                    "Determine if user has active problem",
                    "Capture concise problem statement",
                    "Assess urgency level"
                ],
                "typical_questions": [
                    "What seems to be the problem?",
                    "When did this start?",
                    "Is this affecting production?"
                ]
            },
            1: {
                "name": "Blast Radius",
                "description": "Impact assessment and scope definition",
                "goals": [
                    "Identify affected systems/services",
                    "Determine user impact",
                    "Map dependencies"
                ],
                "typical_questions": [
                    "Which services are affected?",
                    "How many users are impacted?",
                    "Are there any error patterns?"
                ]
            },
            2: {
                "name": "Timeline",
                "description": "Change analysis and temporal context",
                "goals": [
                    "Establish when problem started",
                    "Identify recent changes",
                    "Find last known good state"
                ],
                "typical_questions": [
                    "What changed recently?",
                    "When was it last working?",
                    "Were there any deployments?"
                ]
            },
            3: {
                "name": "Hypothesis",
                "description": "Root cause theory formation",
                "goals": [
                    "Generate 2-3 ranked hypotheses",
                    "Provide supporting evidence",
                    "Suggest validation steps"
                ],
                "typical_questions": [
                    "What could cause these symptoms?",
                    "How can we test this theory?",
                    "What evidence supports this?"
                ]
            },
            4: {
                "name": "Validation",
                "description": "Hypothesis testing and evidence gathering",
                "goals": [
                    "Execute validation tests",
                    "Analyze results",
                    "Narrow down root cause"
                ],
                "typical_questions": [
                    "Can you run this diagnostic command?",
                    "What do the logs show?",
                    "Does this metric confirm the theory?"
                ]
            },
            5: {
                "name": "Solution",
                "description": "Resolution and remediation",
                "goals": [
                    "Propose specific fix",
                    "Provide implementation steps",
                    "Suggest preventive measures"
                ],
                "typical_questions": [
                    "Here's how to fix it...",
                    "What should we do to prevent this?",
                    "Are there any risks to this fix?"
                ]
            }
        }

        return phase_metadata.get(phase, {
            "name": f"Phase {phase}",
            "description": "Unknown phase",
            "goals": [],
            "typical_questions": []
        })

    def get_available_agents(self) -> List[Dict[str, Any]]:
        """Get list of currently available phase agents.

        Returns:
            List of dicts with agent info (phase, name, status)
        """
        available = []

        for phase in range(6):
            agent = self.agents.get(phase)
            phase_info = self.get_phase_info(phase)

            available.append({
                "phase": phase,
                "name": phase_info["name"],
                "implemented": agent is not None,
                "agent_class": agent.__class__.__name__ if agent else None,
                "description": phase_info["description"]
            })

        return available

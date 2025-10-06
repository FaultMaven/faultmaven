"""Base classes for phase-specific sub-agents.

Implements the interface that all phase agents must follow.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime

from faultmaven.models import CaseDiagnosticState, LLMResponse as DoctorPatientLLMResponse


@dataclass
class PhaseContext:
    """Minimal context needed for a specific phase.

    Instead of passing entire conversation history and full diagnostic state,
    we extract only what's relevant for the current phase.

    This implements Anthropic's principle: "smallest possible set of high-signal tokens"
    """
    # Current phase number (0-5)
    phase: int

    # User's current query
    user_query: str

    # Phase-specific state (extracted from full diagnostic state)
    phase_state: Dict[str, Any]

    # Recent relevant messages (max 3, not entire history)
    recent_context: List[str]

    # Case metadata (minimal)
    case_id: str
    urgency_level: str

    # Optional: Existing summary if available
    summary: Optional[str] = None


@dataclass
class PhaseAgentResponse:
    """Response from a phase-specific agent.

    Contains the agent's answer plus any state updates relevant to this phase.
    """
    # Natural language response to user
    answer: str

    # Phase-specific state updates (delta, not full state)
    state_updates: Dict[str, Any]

    # Suggested actions (if any)
    suggested_actions: List[Dict[str, Any]]

    # Suggested commands (if any)
    suggested_commands: List[Dict[str, Any]]

    # Whether phase is complete and ready to advance
    phase_complete: bool

    # Confidence in this response (0.0-1.0)
    confidence: float

    # Next recommended phase (usually current_phase or current_phase+1)
    recommended_next_phase: int


class PhaseAgent(ABC):
    """Base class for all phase-specific agents.

    Each agent implements specialized logic for its diagnostic phase:
    - Custom prompts optimized for phase goals
    - Phase-specific state extraction
    - Targeted questions and guidance

    Benefits vs monolithic agent:
    - 30-50% smaller context window
    - More focused prompts (200-400 tokens vs 1300)
    - Better performance on phase-specific tasks
    """

    def __init__(
        self,
        llm_client: Any,
        phase_number: int,
        phase_name: str,
        prompt_template: str
    ):
        """Initialize phase agent.

        Args:
            llm_client: LLM provider for this agent
            phase_number: Which phase this agent handles (0-5)
            phase_name: Human-readable phase name
            prompt_template: Phase-specific system prompt
        """
        self.llm_client = llm_client
        self.phase_number = phase_number
        self.phase_name = phase_name
        self.prompt_template = prompt_template

    @abstractmethod
    def extract_phase_context(
        self,
        full_diagnostic_state: CaseDiagnosticState,
        conversation_history: List[Any],
        user_query: str,
        case_id: str
    ) -> PhaseContext:
        """Extract minimal context needed for this phase.

        This is where we implement Anthropic's "context as precious resource":
        - Only include what's needed for THIS phase
        - Summarize or exclude irrelevant information
        - Keep context small and focused

        Args:
            full_diagnostic_state: Complete state (we'll extract from this)
            conversation_history: Full history (we'll select relevant parts)
            user_query: Current user question
            case_id: Case identifier

        Returns:
            PhaseContext with only what this phase needs
        """
        pass

    @abstractmethod
    async def process(
        self,
        context: PhaseContext
    ) -> PhaseAgentResponse:
        """Process user query for this specific phase.

        This is the core processing method. Each agent implements
        phase-specific logic here.

        Args:
            context: Minimal phase-specific context

        Returns:
            PhaseAgentResponse with answer and state updates
        """
        pass

    @abstractmethod
    def build_prompt(self, context: PhaseContext) -> str:
        """Build phase-specific prompt.

        Much smaller than monolithic prompt (200-400 tokens vs 1300):
        - Only phase-relevant instructions
        - Targeted examples
        - Focused goals

        Args:
            context: Phase context

        Returns:
            Complete prompt for LLM
        """
        pass

    def format_state_updates(
        self,
        raw_updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Convert raw LLM output to structured state updates.

        Each phase agent knows which state fields it can update.

        Args:
            raw_updates: Raw updates from LLM (function calling or JSON)

        Returns:
            Validated state updates
        """
        # Base implementation - override for phase-specific validation
        return raw_updates

    def should_advance_phase(
        self,
        context: PhaseContext,
        response: PhaseAgentResponse
    ) -> bool:
        """Determine if this phase is complete.

        Goal-oriented assessment (Anthropic principle):
        - Don't advance just because user answered a question
        - Advance when phase GOALS are met
        - Validate required information is present

        Args:
            context: Current phase context
            response: Agent's response

        Returns:
            True if ready to advance to next phase
        """
        # Base implementation - override with phase-specific logic
        return response.phase_complete


class MinimalPhaseAgent(PhaseAgent):
    """Simplified base for agents that don't need complex logic.

    Provides default implementations of common patterns.
    """

    def extract_phase_context(
        self,
        full_diagnostic_state: CaseDiagnosticState,
        conversation_history: List[Any],
        user_query: str,
        case_id: str
    ) -> PhaseContext:
        """Default context extraction: recent messages + phase-specific state."""

        # Get last 3 messages (or fewer if not available)
        recent_messages = conversation_history[-3:] if conversation_history else []
        recent_context = [
            f"{msg.message_type}: {msg.content}"
            for msg in recent_messages
        ]

        # Extract phase-specific state (override this method for custom extraction)
        phase_state = self._extract_phase_state(full_diagnostic_state)

        return PhaseContext(
            phase=self.phase_number,
            user_query=user_query,
            phase_state=phase_state,
            recent_context=recent_context,
            case_id=case_id,
            urgency_level=full_diagnostic_state.urgency_level.value,
            summary=None  # Could load from case metadata if available
        )

    def _extract_phase_state(
        self,
        full_state: CaseDiagnosticState
    ) -> Dict[str, Any]:
        """Extract relevant state for this phase.

        Override this in each agent to select phase-specific fields.
        """
        return {
            "has_active_problem": full_state.has_active_problem,
            "problem_statement": full_state.problem_statement,
            "current_phase": full_state.current_phase
        }

    def build_prompt(self, context: PhaseContext) -> str:
        """Default prompt builder using template + context."""

        recent_conversation = "\n".join(context.recent_context) if context.recent_context else "No recent conversation"

        phase_state_formatted = "\n".join([
            f"  {key}: {value}"
            for key, value in context.phase_state.items()
        ])

        return self.prompt_template.format(
            user_query=context.user_query,
            phase_state=phase_state_formatted,
            recent_conversation=recent_conversation,
            urgency_level=context.urgency_level.upper()
        )

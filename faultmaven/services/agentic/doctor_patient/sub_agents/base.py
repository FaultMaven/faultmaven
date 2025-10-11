"""Base classes for phase-specific sub-agents.

Implements the interface that all phase agents must follow.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime

from faultmaven.models import CaseDiagnosticState, LLMResponse as DoctorPatientLLMResponse
from faultmaven.models.doctor_patient import SuggestedAction, CommandSuggestion


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

    Type-safe: Uses Pydantic models for suggested_actions and suggested_commands
    instead of raw dicts to ensure schema compliance.
    """
    # Natural language response to user
    answer: str

    # Phase-specific state updates (delta, not full state)
    state_updates: Dict[str, Any]

    # Suggested actions (if any) - Properly typed Pydantic models
    suggested_actions: List[SuggestedAction]

    # Suggested commands (if any) - Properly typed Pydantic models
    suggested_commands: List[CommandSuggestion]

    # Whether phase is complete and ready to advance
    phase_complete: bool

    # Confidence in this response (0.0-1.0)
    confidence: float

    # Next recommended phase (usually current_phase or current_phase+1)
    recommended_next_phase: int


def generate_fallback_actions(
    phase: int,
    phase_state: Dict[str, Any],
    user_query: str,
    phase_complete: bool
) -> List[SuggestedAction]:
    """Generate contextual suggested_actions when LLM doesn't provide them.

    This is a defensive fallback - only generates actions when we can infer
    meaningful next steps from the phase state. Returns empty list if unsure.

    Args:
        phase: Current phase number (0-5)
        phase_state: Phase-specific state dict
        user_query: User's query
        phase_complete: Whether phase is marked complete

    Returns:
        List of contextually relevant SuggestedAction objects, or empty list
    """
    from faultmaven.models.doctor_patient import ActionType
    actions = []

    # Don't generate actions if phase is complete - let natural advancement happen
    if phase_complete:
        return []

    # Phase 0: Intake - only if we don't have problem statement
    if phase == 0:
        has_problem = phase_state.get("has_active_problem")
        if has_problem is None:  # Unclear if there's a problem
            actions.append(SuggestedAction(
                label="🔴 Yes, I have a problem",
                type=ActionType.QUESTION_TEMPLATE,
                payload="Yes, I'm experiencing a technical issue that needs troubleshooting"
            ))
            actions.append(SuggestedAction(
                label="📚 Just have a question",
                type=ActionType.QUESTION_TEMPLATE,
                payload="I just have a question about best practices"
            ))

    # Phase 1: Blast Radius - only if missing scope/severity
    elif phase == 1:
        blast_radius = phase_state.get("blast_radius", {})
        if not blast_radius or not blast_radius.get("severity"):
            actions.append(SuggestedAction(
                label="🔴 All users affected",
                type=ActionType.QUESTION_TEMPLATE,
                payload="This is affecting all users"
            ))
            actions.append(SuggestedAction(
                label="🟡 Some users affected",
                type=ActionType.QUESTION_TEMPLATE,
                payload="Only some users are experiencing this issue"
            ))

    # Phase 2: Timeline - only if missing when/what-changed
    elif phase == 2:
        timeline = phase_state.get("timeline_info", {})
        if not timeline or not timeline.get("problem_started_at"):
            actions.append(SuggestedAction(
                label="📅 I know when it started",
                type=ActionType.QUESTION_TEMPLATE,
                payload="The problem started at [time] on [date]"
            ))
            actions.append(SuggestedAction(
                label="🔄 Recent deployment",
                type=ActionType.QUESTION_TEMPLATE,
                payload="We deployed a change at [time]"
            ))

    # Phase 3: Hypothesis - only if we have < 2 hypotheses
    elif phase == 3:
        num_hypotheses = phase_state.get("num_hypotheses", 0)
        if num_hypotheses < 2:
            actions.append(SuggestedAction(
                label="✅ Theory makes sense",
                type=ActionType.QUESTION_TEMPLATE,
                payload="That hypothesis fits what I'm seeing"
            ))
            actions.append(SuggestedAction(
                label="🤔 I have another idea",
                type=ActionType.QUESTION_TEMPLATE,
                payload="I think the root cause might be something else: "
            ))

    # Phase 4: Validation - only if missing validation results
    elif phase == 4:
        # Only suggest if we haven't validated yet
        tests_performed = phase_state.get("tests_performed")
        if not tests_performed or tests_performed == "None yet":
            actions.append(SuggestedAction(
                label="📊 I've checked",
                type=ActionType.QUESTION_TEMPLATE,
                payload="Here's what I found when I checked: "
            ))

    # Phase 5: Solution - only if solution not confirmed
    elif phase == 5:
        solution_proposed = phase_state.get("solution_proposed")
        if not solution_proposed:
            actions.append(SuggestedAction(
                label="✅ I'll proceed",
                type=ActionType.QUESTION_TEMPLATE,
                payload="I'm implementing the recommended solution"
            ))
            actions.append(SuggestedAction(
                label="⚠️ I have constraints",
                type=ActionType.QUESTION_TEMPLATE,
                payload="Before I proceed, here are environment constraints: "
            ))

    return actions[:3]  # Maximum 3 actions


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

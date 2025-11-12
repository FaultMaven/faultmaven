"""Base Phase Handler - Abstract Interface for Investigation Phase Execution

This module defines the abstract base class for all phase handlers in the
OODA investigation framework. Each phase handler implements phase-specific
logic while following a common interface.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from datetime import datetime

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    InvestigationStrategy,
    OODAStep,
    OODAIteration,
)
from faultmaven.models.evidence import EvidenceRequest


logger = logging.getLogger(__name__)


# =============================================================================
# Phase Handler Result
# =============================================================================


@dataclass
class PhaseHandlerResult:
    """Result of phase handler execution

    Represents the outcome of processing a user query within a specific
    investigation phase, including updated state, response, and control flow.
    """

    # Response to user
    response_text: str

    # State updates
    updated_state: InvestigationState

    # Structured response (OODA v3.2.0)
    structured_response: Optional[Any] = None  # OODAResponse subclass

    # Phase control
    phase_complete: bool = False
    should_advance: bool = False
    next_phase: Optional[InvestigationPhase] = None

    # OODA tracking
    ooda_step_executed: Optional[OODAStep] = None
    iteration_complete: bool = False

    # Evidence management
    evidence_requests_generated: List[EvidenceRequest] = None
    evidence_received: List[str] = None  # Evidence IDs

    # Progress tracking
    made_progress: bool = True
    stall_detected: bool = False
    stall_reason: Optional[str] = None

    # Metadata
    processing_time_ms: Optional[float] = None
    confidence_change: float = 0.0

    def __post_init__(self):
        """Initialize mutable defaults"""
        if self.evidence_requests_generated is None:
            self.evidence_requests_generated = []
        if self.evidence_received is None:
            self.evidence_received = []


# =============================================================================
# Base Phase Handler
# =============================================================================


class BasePhaseHandler(ABC):
    """Abstract base class for investigation phase handlers

    Each phase handler implements:
    - Phase entry logic (when entering this phase)
    - OODA step execution (Observe, Orient, Decide, Act)
    - Phase completion detection
    - Evidence request generation
    - State updates

    Subclasses must implement:
    - get_phase(): Return the InvestigationPhase this handler manages
    - handle(): Process user query and return result
    - check_completion(): Determine if phase objectives met
    """

    def __init__(
        self,
        llm_provider=None,
        tools: List[Any] = None,
        tracer=None,
    ):
        """Initialize phase handler

        Args:
            llm_provider: LLM provider for generating responses
            tools: Available tools for this phase
            tracer: Observability tracer
        """
        self.llm_provider = llm_provider
        self.tools = tools or []
        self.tracer = tracer
        self.logger = logging.getLogger(self.__class__.__name__)

        # Access settings for configurable values
        from faultmaven.config.settings import get_settings
        self._settings = get_settings()

    @abstractmethod
    def get_phase(self) -> InvestigationPhase:
        """Get the investigation phase this handler manages

        Returns:
            InvestigationPhase enum value
        """
        pass

    @abstractmethod
    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Handle user query within this phase

        Main entry point for phase execution. Determines current OODA step,
        executes appropriate logic, and returns result.

        Args:
            investigation_state: Current investigation state
            user_query: User's input query
            conversation_history: Recent conversation context
            context: Optional context dict (e.g., file upload metadata)

        Returns:
            PhaseHandlerResult with response and state updates
        """
        pass

    @abstractmethod
    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if phase completion criteria are met

        Args:
            investigation_state: Current investigation state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        pass

    # =========================================================================
    # Common Helper Methods (Implemented in Base)
    # =========================================================================

    def get_phase_config(
        self,
        investigation_state: InvestigationState,
    ) -> Dict[str, Any]:
        """Get configuration for current phase and strategy

        Args:
            investigation_state: Current investigation state

        Returns:
            Configuration dictionary
        """
        from faultmaven.core.investigation.strategy_selector import StrategyConfig
        from faultmaven.core.investigation.phases import get_phase_definition

        phase = self.get_phase()
        strategy = investigation_state.lifecycle.investigation_strategy

        # Get strategy config
        strategy_config = StrategyConfig.get_config(strategy) if strategy else {}

        # Get phase definition
        phase_def = get_phase_definition(phase)

        # Merge configs
        return {
            "phase_name": phase_def.name,
            "ooda_steps": phase_def.ooda_steps,
            "intensity": phase_def.intensity,
            "expected_iterations": phase_def.expected_iterations,
            "strategy": strategy.value if strategy else None,
            "max_iterations": strategy_config.get("max_iterations_per_phase", {}).get(phase, 5),
            "confidence_threshold": strategy_config.get("min_hypothesis_confidence", 0.7),
        }

    def determine_ooda_step(
        self,
        investigation_state: InvestigationState,
    ) -> Optional[OODAStep]:
        """Determine which OODA step to execute next

        Logic:
        - If no current iteration, start new one (Observe)
        - Follow step sequence: Observe → Orient → Decide → Act
        - Some phases skip certain steps (defined in phase config)

        Args:
            investigation_state: Current investigation state

        Returns:
            Next OODAStep to execute, or None if phase has no OODA
        """
        from faultmaven.core.investigation.phases import get_ooda_steps_for_phase

        phase = self.get_phase()
        active_steps = get_ooda_steps_for_phase(phase)

        if not active_steps:
            return None  # Phase 0 has no OODA

        # Get current iteration
        if not investigation_state.ooda_engine.iterations:
            # Start with first active step
            return active_steps[0]

        current_iteration = investigation_state.ooda_engine.iterations[-1]
        completed_steps = current_iteration.steps_completed

        # Find next uncompleted step
        for step in active_steps:
            if step not in completed_steps:
                return step

        # All steps completed - start new iteration
        return active_steps[0]

    async def generate_llm_response(
        self,
        system_prompt: str,
        user_query: str,
        context: Dict[str, Any] = None,
        max_tokens: int = None,
        expected_schema: type = None,
    ) -> "OODAResponse":
        """Generate structured LLM response for phase

        Uses function calling (Tier 1) when available, falls back to three-tier parsing.

        Args:
            system_prompt: System prompt with phase-specific guidance
            user_query: User's query
            context: Additional context for response generation
            max_tokens: Maximum response tokens
            expected_schema: Expected response schema (ConsultantResponse, LeadInvestigatorResponse)

        Returns:
            Structured OODAResponse object (parsed and validated)
        """
        # Import here to avoid circular dependency
        from faultmaven.models.responses import OODAResponse
        from faultmaven.core.response_parser import parse_ooda_response
        from faultmaven.utils.schema_converter import pydantic_to_openai_tools

        if not self.llm_provider:
            raise ValueError("LLM provider not configured")

        # Use configured max_tokens if not explicitly provided
        if max_tokens is None:
            max_tokens = self._settings.llm.phase_response_max_tokens

        # Default to base OODAResponse if no schema specified
        if expected_schema is None:
            expected_schema = OODAResponse

        try:
            # Build full prompt
            full_prompt = system_prompt

            if context:
                # Format context dictionary as readable text
                context_parts = []
                for key, value in context.items():
                    context_parts.append(f"## {key.replace('_', ' ').title()}\n\n{value}")
                full_prompt += f"\n\n# Context\n\n" + "\n\n".join(context_parts)

            full_prompt += f"\n\n# User Query\n\n{user_query}"

            # Try Tier 1: Function calling (if provider supports it)
            tools = None
            tool_choice = None
            try:
                tools = pydantic_to_openai_tools(
                    expected_schema,
                    name=f"respond_{expected_schema.__name__.lower()}",
                    description=f"Respond with structured {expected_schema.__name__}",
                )
                tool_choice = "required"  # Force tool use for structured output
            except Exception as e:
                self.logger.warning(f"Could not create function schema: {e}")

            # Add fallback JSON formatting instruction (for when function calling fails)
            # This ensures consistent response format across all LLM providers
            # IMPORTANT: Use the actual schema fields, not a hardcoded example
            try:
                # Get the actual schema to show correct fields
                schema = expected_schema.model_json_schema()
                schema_example = {}
                for field_name, field_info in schema.get("properties", {}).items():
                    field_type = field_info.get("type", "string")
                    if field_type == "string":
                        if field_name == "answer":
                            schema_example[field_name] = "Your natural language response here"
                        else:
                            schema_example[field_name] = "appropriate value"
                    elif field_type == "boolean":
                        schema_example[field_name] = False
                    elif field_type == "array":
                        schema_example[field_name] = []
                    elif field_type == "object":
                        schema_example[field_name] = {}
                    else:
                        schema_example[field_name] = None

                import json
                schema_json = json.dumps(schema_example, indent=2)

                full_prompt += f"""

# Response Format

CRITICAL: If function calling is not available, respond with a JSON object matching this EXACT structure:

{schema_json}

IMPORTANT INSTRUCTIONS:
- The "answer" field should contain your natural language response as a PLAIN STRING
- Do NOT put JSON inside the "answer" field
- Do NOT nest the entire response structure inside the "answer" field
- Return ONLY the JSON object above
- Do not wrap it in markdown code blocks or any other formatting"""
            except Exception as e:
                # Fallback to basic instruction if schema parsing fails
                self.logger.warning(f"Could not generate schema example: {e}")
                full_prompt += f"""

# Response Format

If function calling is not available, respond with a JSON object with at minimum an "answer" field containing your natural language response as a plain string. Do not put JSON inside the answer field."""

            # Generate response
            llm_response = await self.llm_provider.generate(
                prompt=full_prompt,
                max_tokens=max_tokens,
                temperature=0.7,
                tools=tools,
                tool_choice=tool_choice,
            )

            # Handle case where provider returns string instead of LLMResponse object
            if isinstance(llm_response, str):
                self.logger.warning("LLM provider returned string instead of LLMResponse object")
                raw_response = llm_response
                llm_response = None  # Mark as None for tool_calls check below
            else:
                # Parse response using three-tier fallback
                # If tool_calls present, raw_response will be the function arguments JSON
                # Otherwise, it's the text content
                try:
                    raw_response = llm_response.content
                except AttributeError:
                    # Provider returned unexpected type - convert to string
                    self.logger.error(f"LLM provider returned unexpected type: {type(llm_response)}")
                    raw_response = str(llm_response)
                    llm_response = None

            # If tool_calls present, extract arguments as dict (Tier 1 success)
            if llm_response and llm_response.tool_calls:
                try:
                    import json
                    tool_call = llm_response.tool_calls[0]
                    arguments_json = tool_call.function.get("arguments", "{}")
                    raw_response = json.loads(arguments_json)
                    self.logger.debug("Using Tier 1 (function calling) response")
                except Exception as e:
                    self.logger.warning(f"Tool call parsing failed: {e}, falling back to text")

            # Parse into structured response using three-tier fallback
            # The parser handles double-encoding detection and fixes it automatically
            structured_response = parse_ooda_response(
                raw_response=raw_response,
                expected_schema=expected_schema,
            )

            return structured_response

        except Exception as e:
            self.logger.error(f"LLM generation failed: {e}")
            # Return minimal fallback response
            from faultmaven.models.responses import create_minimal_response
            return create_minimal_response(
                "I encountered an error processing your query. Please try again."
            )

    def create_evidence_request(
        self,
        label: str,
        description: str,
        category: str,
        commands: List[str] = None,
        file_locations: List[str] = None,
        ui_locations: List[str] = None,
        for_hypothesis_id: str = None,
        priority: int = 2,
    ) -> EvidenceRequest:
        """Create evidence request for user

        Args:
            label: Brief title
            description: What evidence is needed and why
            category: Evidence category (symptoms, timeline, etc.)
            commands: Shell commands to run
            file_locations: File paths to check
            ui_locations: UI navigation paths
            for_hypothesis_id: Hypothesis this tests (if any)
            priority: 1=critical, 2=important, 3=nice-to-have

        Returns:
            EvidenceRequest object
        """
        from faultmaven.models.evidence import (
            EvidenceRequest,
            EvidenceCategory,
            AcquisitionGuidance,
        )

        guidance = AcquisitionGuidance(
            commands=commands or [],
            file_locations=file_locations or [],
            ui_locations=ui_locations or [],
        )

        return EvidenceRequest(
            label=label,
            description=description,
            category=EvidenceCategory(category),
            guidance=guidance,
            created_at_turn=0,  # Will be updated by caller
            for_hypothesis_id=for_hypothesis_id,
            priority=priority,
        )

    def start_new_ooda_iteration(
        self,
        investigation_state: InvestigationState,
    ) -> OODAIteration:
        """Start a new OODA iteration

        Args:
            investigation_state: Current investigation state

        Returns:
            New OODAIteration object
        """
        from faultmaven.core.investigation.ooda_engine import create_ooda_engine

        engine = create_ooda_engine()
        return engine.start_new_iteration(investigation_state)

    def should_continue_phase(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, str]:
        """Determine if should continue in current phase

        Args:
            investigation_state: Current investigation state

        Returns:
            Tuple of (should_continue, reason)
        """
        from faultmaven.core.investigation.phases import should_advance_phase

        phase = self.get_phase()
        should_advance, reason = should_advance_phase(
            phase,
            investigation_state,
            max_stall_turns=5,
        )

        return not should_advance, reason

    def log_phase_action(
        self,
        action: str,
        details: Dict[str, Any] = None,
    ):
        """Log phase handler action

        Args:
            action: Action description
            details: Additional details
        """
        phase = self.get_phase()
        log_msg = f"[{phase.name}] {action}"

        if details:
            log_msg += f" - {details}"

        self.logger.info(log_msg)

"""OODA Integration Layer

Connects the OODA PhaseOrchestrator to the existing AgentService infrastructure.
Provides migration path from legacy doctor/patient system to OODA framework.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
from typing import Tuple, Optional, Any
from datetime import datetime

from faultmaven.models.case import Case, CaseDiagnosticState
from faultmaven.models.investigation import InvestigationState
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.models.agentic import StructuredLLMResponse

from faultmaven.services.agentic.orchestration.phase_orchestrator import PhaseOrchestrator
from faultmaven.services.agentic.management.state_manager import StateManager


async def process_turn_with_ooda(
    user_query: str,
    case: Case,
    llm_client: ILLMProvider,
    session_id: str,
    state_manager: Optional[StateManager] = None,
) -> Tuple[StructuredLLMResponse, CaseDiagnosticState]:
    """Process conversation turn using OODA framework

    This is the main entry point that replaces process_turn_with_orchestrator
    from the legacy doctor/patient system.

    Args:
        user_query: User's query
        case: Case object
        llm_client: LLM provider
        session_id: Session identifier
        state_manager: Optional state manager for persistence

    Returns:
        Tuple of (StructuredLLMResponse, updated CaseDiagnosticState)
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Processing turn with OODA framework for case {case.case_id}")

    # Initialize state manager if not provided
    if state_manager is None:
        from faultmaven.container import get_container
        container = get_container()
        state_manager = container.get_state_manager()

    # Get or initialize investigation state
    investigation_state = await _get_or_initialize_investigation_state(
        case=case,
        session_id=session_id,
        user_query=user_query,
        llm_client=llm_client,
        state_manager=state_manager,
    )

    # Initialize orchestrator
    orchestrator = PhaseOrchestrator(
        llm_provider=llm_client,
        session_id=session_id,
        logger=logger,
    )

    # Build conversation history
    conversation_history = await _build_conversation_history(case, session_id, state_manager)

    # Process turn through orchestrator
    response_text, updated_investigation_state = await orchestrator.process_turn(
        user_query=user_query,
        investigation_state=investigation_state,
        conversation_history=conversation_history,
    )

    # Persist updated investigation state
    await state_manager.update_investigation_state(
        session_id=session_id,
        state=updated_investigation_state,
    )

    # Convert to StructuredLLMResponse for compatibility
    structured_response = _convert_to_structured_response(
        response_text=response_text,
        investigation_state=updated_investigation_state,
    )

    # Update case diagnostic state with OODA data
    updated_diagnostic_state = _update_diagnostic_state_from_investigation(
        case_diagnostic_state=case.diagnostic_state,
        investigation_state=updated_investigation_state,
    )

    logger.info(
        f"OODA turn processed: phase={updated_investigation_state.lifecycle.current_phase.name}, "
        f"turn={updated_investigation_state.metadata.current_turn}"
    )

    return structured_response, updated_diagnostic_state


async def _get_or_initialize_investigation_state(
    case: Case,
    session_id: str,
    user_query: str,
    llm_client: ILLMProvider,
    state_manager: StateManager,
) -> InvestigationState:
    """Get existing or initialize new investigation state

    Args:
        case: Case object
        session_id: Session ID
        user_query: User's query
        llm_client: LLM provider
        state_manager: State manager

    Returns:
        InvestigationState object
    """
    logger = logging.getLogger(__name__)

    # Try to get existing investigation state
    investigation_state = await state_manager.get_investigation_state(session_id)

    if investigation_state:
        logger.info(f"Retrieved existing investigation state for session {session_id}")
        return investigation_state

    # Initialize new investigation state
    logger.info(f"Initializing new investigation state for session {session_id}")

    orchestrator = PhaseOrchestrator(
        llm_provider=llm_client,
        session_id=session_id,
        logger=logger,
    )

    investigation_state = await orchestrator.initialize_investigation(
        user_query=user_query,
        case_diagnostic_state=case.diagnostic_state,
    )

    # Persist new state
    await state_manager.update_investigation_state(
        session_id=session_id,
        state=investigation_state,
    )

    return investigation_state


async def _build_conversation_history(
    case: Case,
    session_id: str,
    state_manager: StateManager,
    max_messages: int = 5,
) -> str:
    """Build conversation history for context

    Args:
        case: Case object
        session_id: Session ID
        state_manager: State manager
        max_messages: Maximum messages to include

    Returns:
        Formatted conversation history
    """
    # Get recent messages from case
    if not case.messages:
        return ""

    # Take last N messages
    recent_messages = case.messages[-max_messages:]

    # Format as conversation
    history_parts = []
    for msg in recent_messages:
        role = "User" if msg.message_type.value == "user" else "Assistant"
        history_parts.append(f"{role}: {msg.content}")

    return "\n".join(history_parts)


def _convert_to_structured_response(
    response_text: str,
    investigation_state: InvestigationState,
) -> StructuredLLMResponse:
    """Convert OODA response to StructuredLLMResponse format

    Maintains compatibility with existing API contracts.

    Args:
        response_text: Response text from phase handler
        investigation_state: Current investigation state

    Returns:
        StructuredLLMResponse object
    """
    from faultmaven.models.agentic import SuggestedAction

    # Extract evidence requests as suggested actions
    suggested_actions = []
    if investigation_state.evidence.evidence_requests:
        for req in investigation_state.evidence.evidence_requests[-3:]:  # Last 3 requests
            action = SuggestedAction(
                action_type="evidence_request",
                description=req.description,
                commands=req.acquisition_guidance.suggested_commands if req.acquisition_guidance else [],
                rationale=req.acquisition_guidance.rationale if req.acquisition_guidance else "",
                priority=req.priority,
            )
            suggested_actions.append(action)

    # Build structured response
    return StructuredLLMResponse(
        answer=response_text,
        reasoning=_extract_reasoning_from_state(investigation_state),
        suggested_actions=suggested_actions if suggested_actions else None,
        clarifying_questions=None,  # OODA uses evidence requests instead
        command_validation=None,
        suggested_commands=None,
    )


def _extract_reasoning_from_state(investigation_state: InvestigationState) -> Optional[str]:
    """Extract reasoning from investigation state

    Args:
        investigation_state: Investigation state

    Returns:
        Reasoning string if available
    """
    reasoning_parts = []

    # Current phase reasoning
    current_phase = investigation_state.lifecycle.current_phase
    reasoning_parts.append(f"Current phase: {current_phase.name}")

    # OODA iteration info
    if investigation_state.ooda_engine.current_iteration > 0:
        reasoning_parts.append(
            f"OODA iteration {investigation_state.ooda_engine.current_iteration}"
        )

    # Hypothesis reasoning
    if investigation_state.ooda_engine.hypotheses:
        top_hypothesis = max(
            investigation_state.ooda_engine.hypotheses,
            key=lambda h: h.likelihood,
        )
        reasoning_parts.append(
            f"Leading hypothesis: {top_hypothesis.statement} ({top_hypothesis.likelihood:.0%})"
        )

    # Memory insights
    if investigation_state.memory.persistent_insights:
        reasoning_parts.append(
            f"Key insights: {len(investigation_state.memory.persistent_insights)}"
        )

    return " | ".join(reasoning_parts) if reasoning_parts else None


def _update_diagnostic_state_from_investigation(
    case_diagnostic_state: CaseDiagnosticState,
    investigation_state: InvestigationState,
) -> CaseDiagnosticState:
    """Update case diagnostic state with OODA investigation data

    Maintains both legacy fields and new OODA fields during transition.

    Args:
        case_diagnostic_state: Current case diagnostic state
        investigation_state: OODA investigation state

    Returns:
        Updated CaseDiagnosticState
    """
    # Update OODA-specific fields
    case_diagnostic_state.investigation_state_id = investigation_state.metadata.investigation_id

    # Map OODA phase to legacy current_phase if needed
    # Phase mapping: INTAKE->0, BLAST_RADIUS->1, etc.
    case_diagnostic_state.current_phase = investigation_state.lifecycle.current_phase.value

    # Update urgency level if available
    if investigation_state.problem_confirmation:
        urgency_map = {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "critical": "critical",
        }
        urgency = investigation_state.problem_confirmation.urgency_level
        if urgency in urgency_map:
            from faultmaven.models.case import UrgencyLevel
            case_diagnostic_state.urgency_level = UrgencyLevel(urgency_map[urgency])

    # Update has_active_problem based on engagement mode
    from faultmaven.models.investigation import EngagementMode
    case_diagnostic_state.has_active_problem = (
        investigation_state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR
    )

    # Update timestamps
    case_diagnostic_state.last_assistant_message_at = datetime.utcnow()

    return case_diagnostic_state


def get_investigation_progress_summary(
    investigation_state: InvestigationState,
) -> dict:
    """Get investigation progress summary for API responses

    Args:
        investigation_state: Investigation state

    Returns:
        Dictionary with progress information
    """
    from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager

    hypothesis_manager = create_hypothesis_manager()
    validated = hypothesis_manager.get_validated_hypothesis(
        investigation_state.ooda_engine.hypotheses
    )

    summary = {
        "phase": {
            "current": investigation_state.lifecycle.current_phase.name,
            "number": investigation_state.lifecycle.current_phase.value,
        },
        "engagement_mode": investigation_state.metadata.engagement_mode.value,
        "ooda_iteration": investigation_state.ooda_engine.current_iteration,
        "turn_count": investigation_state.metadata.current_turn,
        "case_status": investigation_state.lifecycle.case_status,
        "hypotheses": {
            "total": len(investigation_state.ooda_engine.hypotheses),
            "validated": validated.statement if validated else None,
            "validated_confidence": validated.likelihood if validated else None,
        },
        "evidence_collected": len(investigation_state.evidence.evidence_items),
        "evidence_requested": len(investigation_state.evidence.evidence_requests),
    }

    # Add anomaly frame if available
    if investigation_state.ooda_engine.anomaly_frame:
        summary["anomaly_frame"] = {
            "statement": investigation_state.ooda_engine.anomaly_frame.statement,
            "severity": investigation_state.ooda_engine.anomaly_frame.severity,
            "affected_components": investigation_state.ooda_engine.anomaly_frame.affected_components,
        }

    return summary


async def process_turn_with_framework_selection(
    user_query: str,
    case: Case,
    llm_client: ILLMProvider,
    session_id: str,
    state_manager: Optional[StateManager] = None,
    use_legacy: bool = False,
) -> Tuple[StructuredLLMResponse, CaseDiagnosticState]:
    """Process turn with OODA framework (default) or legacy fallback

    OODA is the primary framework. Legacy doctor/patient available for testing only.

    Args:
        user_query: User's query
        case: Case object
        llm_client: LLM provider
        session_id: Session ID
        state_manager: Optional state manager
        use_legacy: Use legacy doctor/patient (testing only)

    Returns:
        Tuple of (StructuredLLMResponse, updated CaseDiagnosticState)
    """
    logger = logging.getLogger(__name__)

    if use_legacy:
        logger.warning("Using legacy doctor/patient framework (testing only)")
        from faultmaven.services.agentic.doctor_patient.orchestrator_integration import (
            process_turn_with_orchestrator,
        )
        return await process_turn_with_orchestrator(
            user_query=user_query,
            case=case,
            llm_client=llm_client,
            session_id=session_id,
        )

    # Default: Use OODA framework
    logger.info("Using OODA framework for turn processing")
    return await process_turn_with_ooda(
        user_query=user_query,
        case=case,
        llm_client=llm_client,
        session_id=session_id,
        state_manager=state_manager,
    )

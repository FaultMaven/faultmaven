"""Phase Orchestrator - Manages OODA investigation lifecycle

The orchestrator coordinates phase handlers, manages transitions,
and provides the main entry point for investigation processing.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timezone

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    EngagementMode,
)
from faultmaven.models.case import CaseDiagnosticState
from faultmaven.models.interfaces import ILLMProvider

from faultmaven.services.agentic.phase_handlers.base import PhaseHandlerResult
from faultmaven.services.agentic.phase_handlers.intake_handler import IntakeHandler
from faultmaven.services.agentic.phase_handlers.blast_radius_handler import BlastRadiusHandler
from faultmaven.services.agentic.phase_handlers.timeline_handler import TimelineHandler
from faultmaven.services.agentic.phase_handlers.hypothesis_handler import HypothesisHandler
from faultmaven.services.agentic.phase_handlers.validation_handler import ValidationHandler
from faultmaven.services.agentic.phase_handlers.solution_handler import SolutionHandler
from faultmaven.services.agentic.phase_handlers.document_handler import DocumentHandler

from faultmaven.core.investigation.phases import can_transition, get_phase_definition
from faultmaven.utils.serialization import to_json_compatible


class PhaseOrchestrator:
    """Orchestrates OODA investigation phases

    Responsibilities:
    - Route user queries to appropriate phase handler
    - Manage phase transitions
    - Coordinate investigation state updates
    - Handle cross-phase concerns (memory, evidence)

    Pattern: Command pattern + Strategy pattern
    - Phase handlers are strategies
    - Orchestrator dispatches to appropriate handler
    """

    def __init__(
        self,
        llm_provider: ILLMProvider,
        case_id: str,
        session_id: str = None,
        logger: Optional[logging.Logger] = None,
        tools: Optional[List[Any]] = None,
    ):
        """Initialize phase orchestrator (v3.3.0 - FIXED: require case_id, not just session_id)

        Investigation state belongs to CASE (permanent), not SESSION (temporary).
        See case-and-session-concepts.md for architectural principles.

        Args:
            llm_provider: LLM provider for generating responses
            case_id: Case identifier (investigation state belongs to case)
            session_id: Session identifier (optional, for audit/metadata only)
            logger: Optional logger instance
            tools: Optional list of tools available to phase handlers (e.g., QA sub-agents)
        """
        self.llm_provider = llm_provider
        self.case_id = case_id
        self.session_id = session_id  # Keep for backward compatibility and audit trail
        self.logger = logger or logging.getLogger(__name__)
        self.tools = tools or []

        # Initialize all phase handlers with tools for QA sub-agent access
        # Using keyword arguments to match BasePhaseHandler(llm_provider, tools, tracer) signature
        self.handlers: Dict[InvestigationPhase, Any] = {
            InvestigationPhase.INTAKE: IntakeHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
            InvestigationPhase.BLAST_RADIUS: BlastRadiusHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
            InvestigationPhase.TIMELINE: TimelineHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
            InvestigationPhase.HYPOTHESIS: HypothesisHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
            InvestigationPhase.VALIDATION: ValidationHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
            InvestigationPhase.SOLUTION: SolutionHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
            InvestigationPhase.DOCUMENT: DocumentHandler(llm_provider=llm_provider, tools=self.tools, tracer=None),
        }

        # Log tool availability
        if self.tools:
            tool_names = [getattr(t, 'name', str(t)) for t in self.tools]
            self.logger.info(f"PhaseOrchestrator initialized for session {session_id} with {len(self.tools)} tools: {tool_names}")
        else:
            self.logger.warning(f"PhaseOrchestrator initialized for session {session_id} with NO TOOLS - QA sub-agents will not be available!")

    async def process_turn(
        self,
        user_query: str,
        investigation_state: InvestigationState,
        conversation_history: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, InvestigationState]:
        """Process a single conversation turn

        Main entry point for investigation processing. Routes to appropriate
        phase handler, manages transitions, and updates state.

        Args:
            user_query: User's query
            investigation_state: Current investigation state
            conversation_history: Recent conversation context
            context: Optional context dict (e.g., file upload data, source metadata)

        Returns:
            Tuple of (response_text, updated_investigation_state)
        """
        self.logger.info(
            f"Processing turn {investigation_state.metadata.current_turn + 1} "
            f"in phase {investigation_state.lifecycle.current_phase.name}"
        )

        # Increment turn counter
        investigation_state.metadata.current_turn += 1
        investigation_state.metadata.last_updated = datetime.now(timezone.utc)

        # Get current phase handler
        current_phase = investigation_state.lifecycle.current_phase
        handler = self.handlers.get(current_phase)

        if not handler:
            error_msg = f"No handler found for phase {current_phase}"
            self.logger.error(error_msg)
            return f"Internal error: {error_msg}", investigation_state

        # Execute phase handler
        try:
            result: PhaseHandlerResult = await handler.handle(
                investigation_state=investigation_state,
                user_query=user_query,
                conversation_history=conversation_history,
                context=context,  # Pass file upload context to phase handler
            )

            # Handle phase transitions
            if result.should_advance and result.next_phase is not None:
                investigation_state = await self._handle_phase_transition(
                    investigation_state=result.updated_state,
                    from_phase=current_phase,
                    to_phase=result.next_phase,
                )
            else:
                investigation_state = result.updated_state

            # Log phase progress
            self._log_phase_progress(current_phase, result)

            return result.response_text, investigation_state

        except Exception as e:
            self.logger.error(f"Error processing turn in phase {current_phase}: {e}", exc_info=True)
            error_response = (
                f"I encountered an error while processing your request in "
                f"{current_phase.name} phase. Error: {str(e)[:100]}"
            )
            return error_response, investigation_state

    async def _handle_phase_transition(
        self,
        investigation_state: InvestigationState,
        from_phase: InvestigationPhase,
        to_phase: InvestigationPhase,
    ) -> InvestigationState:
        """Handle transition between phases

        Validates transition, updates state, and performs any cleanup.

        Args:
            investigation_state: Current state
            from_phase: Phase transitioning from
            to_phase: Phase transitioning to

        Returns:
            Updated investigation state
        """
        # Validate transition
        can_proceed, reason = can_transition(from_phase, to_phase, investigation_state)

        if not can_proceed:
            self.logger.warning(
                f"Invalid transition from {from_phase.name} to {to_phase.name}: {reason}"
            )
            # Don't transition - keep current phase
            return investigation_state

        # Log transition
        self.logger.info(
            f"Phase transition: {from_phase.name} -> {to_phase.name}. Reason: {reason}"
        )

        # Update investigation state
        investigation_state.lifecycle.current_phase = to_phase
        investigation_state.lifecycle.phase_history.append({
            "from_phase": from_phase.value,
            "to_phase": to_phase.value,
            "transitioned_at": to_json_compatible(datetime.now(timezone.utc)),
            "reason": reason,
        })

        # Get new phase definition for context
        new_phase_def = get_phase_definition(to_phase)
        investigation_state.metadata.engagement_mode = new_phase_def.engagement_mode

        # Reset OODA iteration for new phase
        investigation_state.ooda_engine.current_iteration = 0
        investigation_state.ooda_engine.iterations = []

        return investigation_state

    def _log_phase_progress(
        self,
        phase: InvestigationPhase,
        result: PhaseHandlerResult,
    ) -> None:
        """Log phase handler execution results

        Args:
            phase: Phase that was executed
            result: Result from handler
        """
        log_data = {
            "phase": phase.name,
            "phase_complete": result.phase_complete,
            "should_advance": result.should_advance,
            "next_phase": result.next_phase.name if result.next_phase else None,
            "ooda_step_executed": result.ooda_step_executed.value if result.ooda_step_executed else None,
            "iteration_complete": result.iteration_complete,
            "made_progress": result.made_progress,
            "stall_detected": result.stall_detected,
            "evidence_requests": len(result.evidence_requests_generated) if result.evidence_requests_generated else 0,
        }

        self.logger.info(f"Phase {phase.name} execution complete", extra=log_data)

    async def check_phase_completion(
        self,
        investigation_state: InvestigationState,
    ) -> Tuple[bool, list, list]:
        """Check if current phase completion criteria are met

        Args:
            investigation_state: Current investigation state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        current_phase = investigation_state.lifecycle.current_phase
        handler = self.handlers.get(current_phase)

        if not handler:
            return False, [], [f"No handler for phase {current_phase}"]

        return await handler.check_completion(investigation_state)

    def get_phase_status(
        self,
        investigation_state: InvestigationState,
    ) -> Dict[str, Any]:
        """Get current phase status and progress

        Args:
            investigation_state: Current investigation state

        Returns:
            Dictionary with phase status information
        """
        current_phase = investigation_state.lifecycle.current_phase
        phase_def = get_phase_definition(current_phase)

        return {
            "current_phase": current_phase.name,
            "phase_number": current_phase.value,
            "engagement_mode": investigation_state.metadata.engagement_mode.value,
            "ooda_iteration": investigation_state.ooda_engine.current_iteration,
            "active_ooda_steps": [step.value for step in phase_def.ooda_steps],
            "expected_iterations": f"{phase_def.expected_iterations[0]}-{phase_def.expected_iterations[1]}",
            "case_status": investigation_state.lifecycle.case_status,
            "total_turns": investigation_state.metadata.current_turn,
            "phase_history_count": len(investigation_state.lifecycle.phase_history),
        }

    async def initialize_investigation(
        self,
        user_query: str,
        case_diagnostic_state: Optional[CaseDiagnosticState] = None,
    ) -> InvestigationState:
        """Initialize new investigation from user query

        Creates investigation state and determines entry phase.

        Args:
            user_query: Initial user query
            case_diagnostic_state: Optional existing case state for migration

        Returns:
            Initialized investigation state
        """
        from faultmaven.models.investigation import InvestigationMetadata, InvestigationLifecycle
        from faultmaven.core.investigation.engagement_modes import EngagementModeManager

        # Initialize engagement mode manager
        engagement_manager = EngagementModeManager()

        # Analyze initial query
        analysis = engagement_manager.analyze_initial_query(user_query)

        # Create investigation metadata
        # FIXED: Use case_id for investigation_id (not session_id!)
        # Investigation state belongs to CASE (permanent), not SESSION (temporary)
        metadata = InvestigationMetadata(
            investigation_id=f"inv_{self.case_id}",
            session_id=self.session_id or "unknown",  # session_id now optional (for audit only)
            engagement_mode=EngagementMode.CONSULTANT,  # Always start in Consultant mode
            started_at=datetime.now(timezone.utc),
            last_updated=datetime.now(timezone.utc),
            current_turn=0,
        )

        # Create investigation lifecycle
        lifecycle = InvestigationLifecycle(
            current_phase=InvestigationPhase.INTAKE,  # Always start at Phase 0
            case_status="open",
            phase_history=[],
            investigation_strategy=None,  # Set after user consent
        )

        # Create investigation state
        from faultmaven.models.investigation import (
            InvestigationState,
            OODAEngineState,
            EvidenceLayer,
            MemoryLayer,
        )

        investigation_state = InvestigationState(
            metadata=metadata,
            lifecycle=lifecycle,
            ooda_engine=OODAEngineState(
                current_iteration=0,
                iterations=[],
                hypotheses=[],
                anomaly_frame=None,
                anchoring_detected=False,
            ),
            evidence=EvidenceLayer(),  # Uses default_factory
            memory=MemoryLayer(),  # Uses default_factory for hierarchical_memory
        )

        # Store problem confirmation if detected
        if analysis.get("problem_detected"):
            problem_confirmation = engagement_manager.create_problem_confirmation(
                user_query, ""
            )
            investigation_state.problem_confirmation = problem_confirmation

        self.logger.info(
            f"Investigation initialized: {metadata.investigation_id}, "
            f"Problem detected: {analysis.get('problem_detected', False)}"
        )

        return investigation_state

    def get_investigation_summary(
        self,
        investigation_state: InvestigationState,
    ) -> str:
        """Get human-readable investigation summary

        Args:
            investigation_state: Investigation state

        Returns:
            Formatted summary string
        """
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager

        hypothesis_manager = create_hypothesis_manager()
        validated = hypothesis_manager.get_validated_hypothesis(
            investigation_state.ooda_engine.hypotheses
        )

        summary_parts = [
            f"**Investigation Summary**",
            f"",
            f"**Phase:** {investigation_state.lifecycle.current_phase.name} "
            f"(Phase {investigation_state.lifecycle.current_phase.value}/6)",
            f"**Mode:** {investigation_state.metadata.engagement_mode.value}",
            f"**Status:** {investigation_state.lifecycle.case_status}",
            f"**Turns:** {investigation_state.metadata.current_turn}",
            f"**OODA Iterations:** {investigation_state.ooda_engine.current_iteration}",
        ]

        if investigation_state.ooda_engine.anomaly_frame:
            summary_parts.extend([
                f"",
                f"**Problem:** {investigation_state.ooda_engine.anomaly_frame.statement}",
                f"**Severity:** {investigation_state.ooda_engine.anomaly_frame.severity}",
            ])

        if investigation_state.ooda_engine.hypotheses:
            summary_parts.append(f"**Hypotheses:** {len(investigation_state.ooda_engine.hypotheses)}")
            if validated:
                summary_parts.append(f"**Validated Hypothesis:** {validated.statement} ({validated.likelihood:.0%})")

        return "\n".join(summary_parts)

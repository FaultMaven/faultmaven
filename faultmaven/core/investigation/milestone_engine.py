"""Data-Driven and Opportunistic Investigation Engine

This module implements the data-driven investigation system that replaces
legacy process-based frameworks. Instead of rigid phase orchestration, this engine
completes milestones opportunistically based on data availability.

Key Design Principles:
- Process-Agnostic: No rigid phase transitions - milestones complete when data is available
- Opportunistic: Multiple milestones can complete in one turn
- Data-Driven Context: Status-based prompt generation based on available data
- Progress tracked via InvestigationProgress

Design Reference:
- docs/architecture/milestone-based-investigation-framework.md


Architecture:
- Process turn → Generate status-based prompt → Invoke LLM → Process response
- Update milestones based on LLM state_updates
- Track turn progress for analytics
- Automatic status transitions (INVESTIGATING → RESOLVED)
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.llm_error_handler import (
    ErrorAction,
    LLMErrorHandler,
)
from faultmaven.core.investigation.prompts.templates import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
    InquiryResponse,
    TerminalResponse,
    get_schema_for_stage,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputMode,
)
from faultmaven.core.investigation.stagnation_detector import (
    StagnationBreaker,
    StagnationDetector,
    StagnationType,
)
from faultmaven.core.investigation.state_validator import (
    StateValidator,
    ValidationSeverity,
)
from faultmaven.core.investigation.working_conclusion_generator import (
    ProgressMetrics,
    calculate_progress_metrics,
    generate_working_conclusion,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    CaseStatusTransition,
    DegradedMode,
    DegradedModeType,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisStatus,
    InvestigationMomentum,
    InvestigationProgress,
    InvestigationStage,
    KnowledgeResolution,
    ProblemVerification,
    Solution,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.domain.services.investigation_router import (
    determine_investigation_path,
)
from faultmaven.modules.knowledge.contracts import IKnowledgeService

logger = logging.getLogger(__name__)


# =============================================================================
# Reasoning Validation
# =============================================================================


def validate_reasoning_first(
    response_obj: BaseInteractionResponse, case: Case
) -> tuple[bool, list[str]]:
    """
    Validate that milestone completions are justified with internal reasoning.

    This function enforces the "Reasoning-First" pattern where the LLM must provide
    justifications for milestone completions BEFORE setting state updates. This prevents
    the LLM from arbitrarily completing milestones without evidence-based reasoning.

    EXCEPTION: Validation is skipped during terminal state transitions to allow graceful
    case closure without forcing justifications. This handles the scenario where:
    - User explicitly says "close this case" (user intent detection sets solution_verified=True)
    - Case automatically transitions INVESTIGATING → RESOLVED
    - LLM may try to complete other milestones during closure
    - We skip validation because the case is already transitioning to terminal state

    Reference: Prompt Engineering Guide Section 13 (lines 3236-3281)

    Args:
        response_obj: LLM's structured response (InquiryResponse, InvestigationResponse_*, or TerminalResponse)
        case: Current case state

    Returns:
        (is_valid, error_messages): Tuple of validation result and list of error messages

    Skip Conditions (validation bypassed):
        1. Response is InquiryResponse or TerminalResponse (no investigation milestones)
        2. Case is already in terminal state (RESOLVED or CLOSED)
        3. Case is transitioning to terminal state:
           - LLM is setting solution_verified=True in this response, OR
           - solution_verified was already set by user intent detection
    """
    errors = []

    # Debug logging for Turn 2 issue
    logger.debug(
        f"🔍 validate_reasoning_first: response_type={type(response_obj).__name__}, "
        f"case_status={case.status.value}, "
        f"is_InquiryResponse={isinstance(response_obj, InquiryResponse)}, "
        f"is_TerminalResponse={isinstance(response_obj, TerminalResponse)}"
    )

    # Only validate investigation responses (not INQUIRY or TERMINAL)
    if isinstance(response_obj, (InquiryResponse, TerminalResponse)):
        logger.debug("🔍 Skipping reasoning validation (INQUIRY or TERMINAL response)")
        return True, []

    # Skip validation if case is already in terminal state
    if case.is_terminal:
        logger.debug(
            "🔍 Skipping reasoning validation (case already in terminal state)"
        )
        return True, []

    # Check if response has internal_reasoning field
    internal_reasoning = getattr(response_obj, "internal_reasoning", None)
    milestones = getattr(response_obj.state_updates, "milestones", None)

    if not milestones:
        # No milestones being completed, no validation needed
        return True, []

    # Get list of milestone fields being completed (set to True)
    completed_milestones = []
    milestone_dict = milestones.model_dump(exclude_none=True)
    for milestone_name, value in milestone_dict.items():
        if isinstance(value, bool) and value is True:
            completed_milestones.append(milestone_name)

    if not completed_milestones:
        # No milestones actually completed, no validation needed
        return True, []

    # ===== TERMINAL TRANSITION EXCEPTION =====
    # Skip validation if case is transitioning to terminal state.
    #
    # SCENARIO: User says "close this case"
    #   1. User intent detection (milestone_engine.py:361-401) sets case.progress.solution_verified = True
    #   2. Case still in INVESTIGATING status (transition happens AFTER LLM call)
    #   3. LLM generates response, may try to complete milestones (e.g., symptom_verified=True)
    #   4. validate_reasoning_first() called
    #   5. CRITICAL FIX: We detect case.progress.solution_verified is already True
    #   6. Skip validation → Allow LLM response without justifications
    #   7. Terminal transition executes (terminal_transitions.py:47-79)
    #   8. Case becomes RESOLVED with closure_reason="resolved"
    #
    # WHY THIS FIX IS NEEDED:
    # - Original logic only checked if LLM was CURRENTLY setting solution_verified
    # - Missed the case where solution_verified was ALREADY set by user intent detection
    # - Caused reasoning validation errors during case closure (Turn 4 bug)
    #
    # FIX: Check BOTH conditions:
    # 1. LLM is setting solution_verified in THIS response (completed_milestones contains "solution_verified")
    # 2. solution_verified was ALREADY set (case.progress.solution_verified == True)
    if case.status == CaseStatus.INVESTIGATING:
        # Check if LLM is setting solution_verified in this response
        llm_setting_solution_verified = "solution_verified" in completed_milestones
        # Check if solution_verified was already set (e.g., by user intent detection)
        already_solution_verified = case.progress.solution_verified

        if llm_setting_solution_verified or already_solution_verified:
            logger.debug(
                f"🔍 Skipping reasoning validation (case transitioning to RESOLVED: "
                f"llm_setting={llm_setting_solution_verified}, already_set={already_solution_verified})"
            )
            return True, []

    # If milestones are being completed, internal_reasoning is REQUIRED
    if not internal_reasoning:
        errors.append(
            f"Milestones {completed_milestones} completed without internal_reasoning. "
            "You MUST provide internal_reasoning with justifications when completing milestones."
        )
        return False, errors

    # Check 1: All completed milestones must have justifications
    for milestone in completed_milestones:
        if milestone not in internal_reasoning.milestone_justifications:
            errors.append(
                f"Milestone '{milestone}' completed without justification. "
                "Add justification to internal_reasoning.milestone_justifications."
            )

    # Check 2: Justifications must reference analyzed evidence
    if (
        internal_reasoning.milestone_justifications
        and not internal_reasoning.evidence_analyzed
    ):
        errors.append(
            "Milestone justifications provided but no evidence_analyzed listed. "
            "Specify which evidence IDs were considered."
        )

    # Check 3: Evidence IDs must exist in case
    case_evidence_ids = {e.evidence_id for e in case.evidence}
    for evidence_id in internal_reasoning.evidence_analyzed:
        # Skip "new_index_N" references (evidence being added this turn)
        if evidence_id.startswith("new_index_"):
            continue

        # STRICT VALIDATION: All evidence IDs must be real
        # No more USER_MESSAGE_* pseudo-IDs - user messages are now auto-created as Evidence
        if evidence_id not in case_evidence_ids:
            errors.append(
                f"internal_reasoning references evidence_id '{evidence_id}' which doesn't exist in case. "
                f"Available evidence IDs: {list(case_evidence_ids)}"
            )

    # Check 4: Enforce evidence ID format (must start with "ev_")
    for evidence_id in internal_reasoning.evidence_analyzed:
        if evidence_id.startswith("new_index_"):
            continue  # These are valid temporary references
        if not evidence_id.startswith("ev_"):
            errors.append(
                f"Invalid evidence ID format: '{evidence_id}'. "
                f"Evidence IDs must start with 'ev_' prefix (e.g., 'ev_a1b2c3d4e5f6'). "
                f"Do not use placeholder IDs or descriptive labels."
            )

    # Check 5: Cannot complete milestones without evidence
    if internal_reasoning.milestone_justifications:
        if not internal_reasoning.evidence_analyzed:
            errors.append(
                "Cannot complete milestones without analyzing evidence. "
                "The evidence_analyzed list is empty, but milestone_justifications are provided. "
                "You must cite specific evidence IDs to support milestone completion."
            )

    return len(errors) == 0, errors


# =============================================================================
# Milestone Engine - Main Implementation
# =============================================================================


class MilestoneEngine:
    """
    Data-Driven and Opportunistic Investigation Engine.

    Replaces legacy process-based engines with a simpler, more flexible approach where
    the agent completes milestones opportunistically based on available data.

    Responsibilities:
    - Generate prompts based on case status (INQUIRY, INVESTIGATING, RESOLVED)
    - Invoke LLM with appropriate schema
    - Process LLM responses and update case state
    - Track milestone completion and turn progress
    - Automatic status transitions when milestones complete

    Key Design Principles:
    - No phase orchestration - milestones complete when data is available
    - Status-based prompts instead of phase-based
    - Multiple milestones can complete in single turn
    - Repository abstraction for persistence (no direct DB access)
    """

    def __init__(
        self,
        llm_provider: ILLMProvider,
        repository: Any,  # Case repository abstraction (duck typing)
        knowledge_service: IKnowledgeService | None = None,
        trace_enabled: bool = True,
    ):
        """Initialize milestone engine.

        Args:
            llm_provider: LLM provider implementation (ILLMProvider interface)
            repository: Case repository with save/get methods
            knowledge_service: Optional knowledge service for KB searches
            trace_enabled: Enable observability tracing
        """
        self.llm_provider = llm_provider
        self.repository = repository
        self.knowledge_service = knowledge_service
        self.trace_enabled = trace_enabled
        self.hypothesis_manager = create_hypothesis_manager()
        self.state_validator = StateValidator()
        self.stagnation_detector = StagnationDetector()
        self.stagnation_breaker = StagnationBreaker()
        self.llm_error_handler = LLMErrorHandler()

        logger.info("MilestoneEngine initialized with structured output engine")

    async def process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        intent_type: str | None = None,
        intent_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process a single conversation turn with optional structured intent.

        This is the main entry point for the milestone engine. It:
        1. Routes based on intent_type (if provided) or processes normally
        2. Generates status-appropriate prompt
        3. Invokes LLM with structured output
        4. Processes response and updates case state
        5. Records turn progress
        6. Checks for automatic status transitions

        Args:
            case: Current case
            user_message: User's message this turn
            attachments: Optional file attachments
            intent_type: Optional structured intent type (status_transition, confirmation, etc.)
            intent_data: Optional intent-specific data

        Returns:
            {
                "agent_response": str,        # Natural language response to user
                "case_updated": Case,         # Updated case object
                "metadata": {
                    "turn_number": int,
                    "milestones_completed": List[str],
                    "progress_made": bool,
                    "status_transitioned": bool,
                    "outcome": TurnOutcome
                }
            }

        Raises:
            MilestoneEngineError: If processing fails
        """
        # Add intent information to logger for tracing
        intent_info = f" [intent={intent_type}]" if intent_type else ""
        logger.info(
            f"Processing turn {case.current_turn} for case {case.case_id} "
            f"(status: {case.status}){intent_info}"
        )

        # Debug logging for Turn 2 issue
        if case.status == CaseStatus.INQUIRY:
            logger.info(
                f"🔍 Turn {case.current_turn + 1} starting: status={case.status.value}, "
                f"confirmed={case.inquiry.problem_statement_confirmed}, "
                f"decided_to_investigate={case.inquiry.decided_to_investigate}"
            )
        else:
            logger.info(
                f"🔍 Turn {case.current_turn + 1} starting: status={case.status.value}, "
                f"stage={case.current_stage}"
            )

        try:
            # 0. CRITICAL: Auto-create Evidence from user message BEFORE LLM prompt
            # This ensures ALL data has concrete Evidence IDs that the LLM can reference
            # Eliminates the need for USER_MESSAGE_* pseudo-ID workarounds
            user_message_evidence_id = None
            if user_message and user_message.strip():
                from uuid import uuid4
                from faultmaven.modules.case.domain.models import (
                    Evidence,
                    EvidenceCategory,
                    EvidenceForm,
                    EvidenceSourceType,
                )

                # Create Evidence object from user message
                user_evidence = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=user_message[:200]
                    + ("..." if len(user_message) > 200 else ""),
                    content_ref=f"turn_{case.current_turn + 1}_user_message",
                    category=EvidenceCategory.SYMPTOM_EVIDENCE,  # User input is symptom evidence
                    source_type=EvidenceSourceType.USER_REPORT,  # User-provided information
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn + 1,
                    form=EvidenceForm.USER_INPUT,
                    primary_purpose="User-provided context and information",
                    preprocessed_content=user_message,
                    content_size_bytes=len(user_message),
                    preprocessing_method="none",
                )

                # Add to case BEFORE LLM sees it
                case.evidence.append(user_evidence)
                user_message_evidence_id = user_evidence.evidence_id

                logger.info(
                    f"📝 Auto-created Evidence from user message: {user_evidence.evidence_id} "
                    f"(turn {case.current_turn + 1}, {len(user_message)} bytes)"
                )

            # 0b. Detect explicit user intent to close/resolve case
            # This handles cases where user explicitly says "close this case" or "mark as resolved"
            # without relying on LLM to set solution_verified=True
            #
            # CRITICAL DISTINCTION:
            # - CLOSED (without solution): User abandons investigation without finding solution
            # - RESOLVED (with solution): User confirms problem is fixed/resolved
            #
            # TWO COMPLEMENTARY PATHS (Intent-Based Routing Design):
            # 1. EXPLICIT INTENT (frontend buttons/actions) → Skip pattern matching, use intent_data
            # 2. NATURAL LANGUAGE (user types in chat) → Pattern matching fallback (below)
            #
            # Pattern matching order matters: Check abandonment FIRST, then resolution.
            # This prevents "close as unresolved" from matching resolution patterns.
            #
            # BUG FIX (2026-02-08): User said "Close this case as unresolved" but system went to RESOLVED
            # ROOT CAUSE: Patterns were too specific ("close as unresolved" exact match)
            # FIX: Use key phrases that work with variations:
            #   - "as unresolved" matches: "close as unresolved", "close this case as unresolved"
            #   - "without solution" matches: "close without solution", "close this without solution"
            # This handles natural language variations while maintaining correct intent detection.
            # ============================================================
            # USER INTENT DETECTION - EXPLICIT STATUS TRANSITION (Frontend Buttons)
            # ============================================================
            # BUG FIX (2026-02-09): Status dropdown transitions not working
            # ROOT CAUSE: intent_type="status_transition" skipped pattern matching but had no handler
            # FIX: Add explicit handler before pattern matching section
            if intent_type == "status_transition" and intent_data:
                to_status_str = intent_data.get("to_status")
                from_status_str = intent_data.get("from_status")

                if not to_status_str:
                    raise ValueError(
                        "to_status is required for status_transition intent"
                    )

                logger.info(
                    f"🎯 Explicit status_transition intent: {from_status_str} → {to_status_str} "
                    f"for case {case.case_id}"
                )

                # Import terminal transition functions
                from faultmaven.core.investigation.terminal_transitions import (
                    close_from_inquiry,
                    force_close_investigation,
                )

                # Handle each status transition
                if to_status_str == "closed":
                    if case.status == CaseStatus.INQUIRY:
                        close_from_inquiry(case=case, user_id=case.user_id)
                        agent_response = "Case closed without investigation."
                    elif case.status == CaseStatus.INVESTIGATING:
                        force_close_investigation(
                            case=case, user_id=case.user_id, reason="user_closed"
                        )
                        agent_response = "Investigation closed without resolution."
                    else:
                        raise ValueError(
                            f"Cannot transition to CLOSED from {case.status.value}"
                        )

                    logger.info(
                        f"✅ Case {case.case_id} transitioned to CLOSED via explicit intent"
                    )

                    # Save and return immediately (skip LLM)
                    await self.repository.save(case)
                    return {
                        "agent_response": agent_response,
                        "case_updated": case,
                        "metadata": {
                            "turn_number": case.current_turn,
                            "milestones_completed": [],
                            "progress_made": False,
                        },
                    }

                elif to_status_str == "resolved":
                    # Set milestones for automatic RESOLVED transition
                    if not case.progress.solution_proposed:
                        case.progress.solution_proposed = True
                    if not case.progress.solution_applied:
                        case.progress.solution_applied = True
                    case.progress.solution_verified = True

                    logger.info(
                        f"✅ Set solution milestones for case {case.case_id} via explicit intent "
                        f"(will auto-transition to RESOLVED)"
                    )

                    # Continue to normal LLM flow - terminal transition will happen in check_terminal_transitions()
                    # Don't return early - let LLM generate proper resolution summary

                elif to_status_str == "investigating":
                    if case.status != CaseStatus.INQUIRY:
                        raise ValueError(
                            f"Cannot transition to INVESTIGATING from {case.status.value}"
                        )

                    # Update inquiry state
                    case.inquiry.problem_statement_confirmed = True
                    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
                    case.inquiry.decided_to_investigate = True
                    case.inquiry.decision_made_at = datetime.now(UTC)

                    # Transition to INVESTIGATING
                    case.status = CaseStatus.INVESTIGATING
                    case.status_history.append(
                        CaseStatusTransition(
                            from_status=CaseStatus.INQUIRY,
                            to_status=CaseStatus.INVESTIGATING,
                            triggered_at=datetime.now(UTC),
                            triggered_by=case.user_id,
                            reason="User initiated formal investigation",
                        )
                    )

                    logger.info(
                        f"✅ Case {case.case_id} transitioned to INVESTIGATING via explicit intent"
                    )

                    # Continue to normal LLM flow for investigation kickoff message

                else:
                    raise ValueError(f"Unknown to_status: {to_status_str}")

            # ============================================================
            # USER INTENT DETECTION - PATTERN MATCHING (Natural Language)
            # ============================================================
            # Two complementary paths:
            # 1. Explicit intent (frontend buttons) → Handled above with intent_data
            # 2. Natural language (user types) → Pattern matching below
            #
            # SKIP PATTERN MATCHING if explicit intent provided by frontend
            if user_message and not intent_type:
                user_msg_lower = user_message.lower().strip()

                # ========================================
                # INQUIRY STATUS - CLOSE FROM INQUIRY
                # ========================================
                if case.status == CaseStatus.INQUIRY:
                    # User wants to close from INQUIRY without starting investigation
                    close_inquiry_patterns = [
                        "close this",
                        "close the case",
                        "don't need",
                        "don't want",
                        "no need",
                        "not needed",
                        "cancel this",
                        "never mind",
                        "nevermind",
                    ]

                    if any(
                        pattern in user_msg_lower for pattern in close_inquiry_patterns
                    ):
                        logger.info(
                            f"🎯 Detected user intent to CLOSE from INQUIRY for case {case.case_id}: '{user_message[:50]}...'"
                        )
                        from faultmaven.core.investigation.terminal_transitions import (
                            close_from_inquiry,
                        )

                        close_from_inquiry(
                            case=case,
                            user_id=case.user_id,
                        )

                        logger.info(
                            f"✅ Case {case.case_id} transitioned to CLOSED (inquiry_only) based on user intent"
                        )

                # ========================================
                # INVESTIGATING STATUS - ABANDONMENT / RESOLUTION
                # ========================================
                elif case.status == CaseStatus.INVESTIGATING:
                    # Pattern matching for ABANDONMENT intent (CLOSED without solution)
                    # These patterns indicate user wants to close WITHOUT confirming solution
                    # Note: Use key phrases that can appear anywhere in the message
                    abandonment_patterns = [
                        "as unresolved",  # "close as unresolved", "close this case as unresolved"
                        "without solution",  # "close without solution", "close this without solution"
                        "without resolving",  # "close without resolving"
                        "abandon this case",
                        "abandon the case",
                        "abandon",  # Generic abandonment
                        "give up",
                        "can't solve",
                        "cannot solve",
                        "unable to resolve",
                        "escalate this",
                        "escalate to",
                    ]

                    # Pattern matching for RESOLUTION intent (RESOLVED with solution)
                    # These patterns indicate user confirms problem is fixed
                    # Use specific phrases that clearly indicate solution/resolution
                    resolve_patterns = [
                        "mark as resolved",
                        "mark this resolved",
                        "case is resolved",
                        "case resolved",
                        "this is resolved",
                        "problem is solved",
                        "problem solved",
                        "issue is fixed",
                        "issue fixed",
                        "solution worked",
                        "solution works",
                        "fixed now",
                        "working now",
                    ]

                    # Ambiguous patterns that need context from abandonment keywords
                    # "close this case" could mean either CLOSED or RESOLVED
                    # Check if combined with abandonment indicators
                    close_patterns = [
                        "close this case",
                        "close the case",
                    ]

                    # CHECK ABANDONMENT FIRST (highest priority)
                    if any(
                        pattern in user_msg_lower for pattern in abandonment_patterns
                    ):
                        logger.info(
                            f"🎯 Detected explicit user intent to ABANDON case {case.case_id} (CLOSED): '{user_message[:50]}...'"
                        )
                        # Import here to avoid circular dependency
                        from faultmaven.core.investigation.terminal_transitions import (
                            force_close_investigation,
                        )

                        # Force close with "abandoned" reason
                        force_close_investigation(
                            case=case,
                            user_id=case.user_id,
                            reason="abandoned",
                        )

                        logger.info(
                            f"✅ Case {case.case_id} transitioned to CLOSED (abandoned) based on user intent"
                        )

                    # CHECK RESOLUTION (medium priority)
                    elif any(pattern in user_msg_lower for pattern in resolve_patterns):
                        logger.info(
                            f"🎯 Detected explicit user intent to RESOLVE case {case.case_id}: '{user_message[:50]}...'"
                        )
                        # Set milestone progression to allow solution_verified=True
                        # Milestone ordering: proposed → applied → verified
                        if not case.progress.solution_proposed:
                            case.progress.solution_proposed = True
                        if not case.progress.solution_applied:
                            case.progress.solution_applied = True

                        # This will trigger automatic transition in check_terminal_transitions()
                        case.progress.solution_verified = True

                        logger.info(
                            f"✅ Set solution milestones for case {case.case_id} based on user intent"
                        )

                        logger.info(
                            f"✅ Case {case.case_id} will transition to RESOLVED based on user intent"
                        )

                    # CHECK AMBIGUOUS CLOSE PATTERNS (lowest priority)
                    # Only trigger resolution if no abandonment indicators present
                    elif any(pattern in user_msg_lower for pattern in close_patterns):
                        # Default ambiguous "close" to RESOLVED (backward compatible)
                        logger.info(
                            f"🎯 Detected ambiguous close intent for case {case.case_id}: '{user_message[:50]}...' (defaulting to RESOLVED)"
                        )
                        # Set milestone progression to allow solution_verified=True
                        if not case.progress.solution_proposed:
                            case.progress.solution_proposed = True
                        if not case.progress.solution_applied:
                            case.progress.solution_applied = True

                        case.progress.solution_verified = True

                        logger.info(
                            f"✅ Case {case.case_id} will transition to RESOLVED (ambiguous close defaulted to resolution)"
                        )

            # 1. Gather Context & Build Prompt
            # Phase 3: Inquiry KB Search (Fast-track)
            kb_results = None
            if case.status == CaseStatus.INQUIRY and self.knowledge_service:
                try:
                    kb_results = await self.knowledge_service.search(user_message, k=3)
                    logger.info(
                        f"KB Search found {len(kb_results) if kb_results else 0} results"
                    )
                except Exception as e:
                    logger.warning(f"KB Search failed: {e}")

            # Build prompt using the adaptive template system
            # Gap #6: Pass provider info for dynamic token budget calculation
            provider_name = getattr(self.llm_provider, "provider_name", None)
            model_name = (
                getattr(self.llm_provider.config, "default_model", None)
                if hasattr(self.llm_provider, "config")
                else None
            )
            prompt = get_prompt_for_case(
                case,
                user_message,
                kb_results,
                provider_name=provider_name,
                model_name=model_name,
            )

            # Determine schema based on status/stage
            if case.status == CaseStatus.INQUIRY:
                schema_model = InquiryResponse
                logger.info(
                    f"🔍 Turn {case.current_turn + 1} schema selection: "
                    f"status={case.status.value}, schema=InquiryResponse"
                )
            elif case.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
                schema_model = TerminalResponse
                logger.info(
                    f"🔍 Turn {case.current_turn + 1} schema selection: "
                    f"status={case.status.value}, schema=TerminalResponse"
                )
            else:
                schema_model = get_schema_for_stage(
                    case.current_stage or InvestigationStage.SYMPTOM_VERIFICATION
                )
                logger.info(
                    f"🔍 Turn {case.current_turn + 1} schema selection: "
                    f"status={case.status.value}, stage={case.current_stage}, "
                    f"schema={schema_model.__name__}"
                )

            # 2. Invoke LLM with structured output
            response_obj = await self._generate_structured_output(prompt, schema_model)

            # Debug: Log what type was actually returned
            logger.info(
                f"🔍 Turn {case.current_turn + 1} response type: {type(response_obj).__name__}"
            )

            # 3. Process response and update case state
            case_updated, metadata = await self._process_response_structured(
                case, user_message, response_obj, attachments
            )

            # 3b. Validate Diagnostic Reasoning (Section 3.3)
            # Ensure agent provides context-specific reasoning before suggestions
            from faultmaven.core.investigation.diagnostic_reasoning_validator import (
                validate_diagnostic_reasoning,
            )

            is_valid_reasoning, violations = validate_diagnostic_reasoning(
                case=case_updated,
                agent_response=response_obj.agent_response,
                contains_suggestion=None,  # Auto-detect
            )
            if not is_valid_reasoning:
                logger.warning(
                    f"Diagnostic reasoning validation failed: {violations}. "
                    "Agent response may contain generic advice without case-specific reasoning."
                )
                # Add violations to metadata for observability
                metadata["diagnostic_reasoning_violations"] = violations

            # Phase 1: No-Op Detection
            progress_made = self._check_if_progress_made(metadata)
            metadata["progress_made"] = progress_made
            # Outcome is already set by _process_response_structured (default) or applied updates (LLM choice)

            # 4. Check for automatic status transitions
            case_updated = await self._check_automatic_transitions(
                case_updated, metadata
            )

            # 5. Phase 4: Hypothesis Housekeeping (Decay & Anchoring)
            # This happens after transitions but before recording the turn
            self._perform_hypothesis_housekeeping(case_updated, metadata)

            # Step 5.5: Calculate progress metrics
            progress_metrics = calculate_progress_metrics(
                case=case_updated, current_turn=case_updated.current_turn
            )
            metadata["momentum"] = progress_metrics.investigation_momentum
            metadata["blocked_reasons"] = progress_metrics.blocked_reasons
            metadata["next_steps"] = progress_metrics.next_steps

            # Step 5.6: Generate working conclusion EVERY turn during INVESTIGATING
            # Gap #7: Working Conclusion Every Turn
            # Reference: Prompt Engineering Guide Section 11.7
            # Why: Provides consistent context tracking, prevents "lost context" issues
            if case_updated.status == CaseStatus.INVESTIGATING:
                working_conclusion = generate_working_conclusion(
                    case=case_updated, current_turn=case_updated.current_turn
                )
                case_updated.working_conclusion = working_conclusion
                logger.debug(
                    f"Working conclusion updated: likelihood={working_conclusion.likelihood:.2f}"
                )

            # Step 5.7: Validate state consistency
            is_valid, validation_issues = self.state_validator.is_valid(case_updated)
            validation_repairs: list[str] = []
            if validation_issues:
                # Log validation issues and collect repairs
                for issue in validation_issues:
                    if issue.severity == ValidationSeverity.ERROR:
                        logger.warning(
                            f"State validation error: {issue.code} - {issue.message}"
                        )
                        if issue.suggested_fix:
                            validation_repairs.append(
                                f"{issue.code}: {issue.suggested_fix}"
                            )
                    elif issue.severity == ValidationSeverity.WARNING:
                        logger.debug(
                            f"State validation warning: {issue.code} - {issue.message}"
                        )
                metadata["validation_issues"] = [
                    {"code": i.code, "message": i.message, "severity": i.severity.value}
                    for i in validation_issues
                ]

            # Step 5.8: Update progress tracking (before stagnation check)
            if metadata.get("progress_made", False):
                case_updated.turns_without_progress = 0
            else:
                case_updated.turns_without_progress += 1

            # Step 5.9: Check for stagnation (before recording turn)
            stagnation_type = self.stagnation_detector.detect_stagnation(case_updated)
            stagnation_str: str | None = None
            if stagnation_type and case_updated.degraded_mode is None:
                stagnation_str = stagnation_type.value
                # Get breakout action and apply it
                breakout_action = self.stagnation_breaker.break_stagnation(
                    case_updated, stagnation_type
                )
                metadata["stagnation_type"] = stagnation_type.value
                metadata["breakout_action"] = breakout_action.action
                metadata["breakout_prompt_injection"] = breakout_action.prompt_injection
                logger.info(
                    f"Stagnation detected: {stagnation_type.value}. "
                    f"Action: {breakout_action.action}"
                )

            # Step 6: Record turn progress
            turn_record = self._create_turn_record(
                turn_number=case_updated.current_turn,
                milestones_completed=metadata.get("milestones_completed", []),
                evidence_added=metadata.get("evidence_added", []),
                hypotheses_generated=metadata.get("hypotheses_generated", []),
                hypotheses_validated=metadata.get("hypotheses_validated", []),
                solutions_proposed=metadata.get("solutions_proposed", []),
                progress_made=metadata.get("progress_made", False),
                outcome=metadata.get("outcome", TurnOutcome.CONVERSATION),
                user_message=user_message,
                agent_response=response_obj.agent_response,
                system_feedback=metadata.get("system_feedback"),
                momentum=progress_metrics.investigation_momentum,
                blocked_reasons=progress_metrics.blocked_reasons,
                next_steps=progress_metrics.next_steps,
                stagnation_detected=stagnation_str,
                validation_repairs=validation_repairs,
            )
            case_updated.turn_history.append(turn_record)

            # Step 7: Save case (only if changes made, but turn history always updates)
            case_updated.updated_at = datetime.now(UTC)
            case_updated.last_activity_at = datetime.now(UTC)
            await self.repository.save(case_updated)

            logger.info(
                f"Turn {case_updated.current_turn} processed successfully. "
                f"Status: {case_updated.status}, "
                f"Progress made: {metadata.get('progress_made', False)}"
            )

            return {
                "agent_response": response_obj.agent_response,
                "case_updated": case_updated,
                "metadata": {
                    "turn_number": case_updated.current_turn,
                    "milestones_completed": metadata.get("milestones_completed", []),
                    "progress_made": metadata.get("progress_made", False),
                    "status_transitioned": metadata.get("status_transitioned", False),
                    "outcome": metadata.get("outcome", TurnOutcome.CONVERSATION),
                    "momentum": metadata.get("momentum"),
                    "next_steps": metadata.get("next_steps", []),
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            }

        except Exception as e:
            error_str = str(e)

            # Classify error types to determine logging level
            is_external_service_error = any(
                indicator in error_str.lower()
                for indicator in [
                    "over capacity",
                    "503",
                    "rate limit",
                    "429",
                    "timeout",
                    "all providers failed",
                    "api error",
                ]
            )

            if is_external_service_error:
                # External service errors are expected - log without stack trace
                logger.warning(
                    f"External service error for case {case.case_id}: {error_str[:200]}"
                )
            else:
                # Unexpected errors - log with full stack trace for debugging
                logger.error(
                    f"Error processing turn for case {case.case_id}: {e}", exc_info=True
                )

            raise MilestoneEngineError(f"Turn processing failed: {e}") from e

    # =========================================================================
    # Prompt Generation
    # =========================================================================

    async def _generate_structured_output(
        self, prompt: str, schema_model: Any
    ) -> BaseInteractionResponse:
        """
        Generate structured output from LLM using provider-agnostic capability system.

        This method automatically detects the provider's structured output capabilities
        and adjusts the prompt and response format accordingly:
        - STRICT mode: Uses json_schema with strict:true (OpenAI GPT-4o, Groq gpt-oss)
        - BEST_EFFORT mode: Uses json_object with schema in prompt (most models)
        - FUNCTION_CALLING mode: Uses tool calling pattern (Anthropic Claude)
        - NONE mode: Schema only in prompt, no API support (legacy models)

        Args:
            prompt: User prompt
            schema_model: Pydantic model class for expected output

        Returns:
            Instantiated Pydantic model
        """
        # Get provider-specific structured output strategy
        schema = schema_model.model_json_schema()
        strategy = self.llm_provider.get_structured_output_strategy(schema)

        # Conditionally include schema in prompt based on provider capability
        if strategy.include_schema_in_prompt:
            # Provider requires schema in prompt text (json_object or prompt_only modes)
            from faultmaven.core.investigation.prompts.templates import (
                SCHEMA_INSTRUCTIONS,
            )

            schema_json = json.dumps(schema, indent=2)
            json_instruction = (
                f"\n\n{SCHEMA_INSTRUCTIONS}\n"
                "You MUST respond with valid JSON matching this exact schema:\n\n"
                f"```json\n{schema_json}\n```\n\n"
                "IMPORTANT:\n"
                "- Use the exact field names shown in the schema\n"
                "- Do not add extra fields not in the schema\n"
                "- Do not include any text before or after the JSON\n"
                "- Ensure all required fields are present\n"
            )
            final_prompt = f"{prompt}{json_instruction}"
        else:
            # Provider supports strict json_schema - no need for schema in prompt
            final_prompt = prompt

        # Define the LLM operation for retry
        async def llm_operation():
            # Build generate parameters based on strategy mode
            generate_params = {
                "prompt": final_prompt,
                "max_tokens": 4000,
                "temperature": 0.2,  # Lower temperature for structured output
            }

            # Apply strategy-specific parameters
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Use tools/function calling for structured output (Anthropic, etc.)
                from faultmaven.utils.schema_converter import pydantic_to_openai_tools

                generate_params["tools"] = pydantic_to_openai_tools(schema_model)
                generate_params["tool_choice"] = "required"  # Force tool use
                # Don't include response_format for function calling
            else:
                # Use response_format for JSON modes (STRICT, BEST_EFFORT, NONE)
                if strategy.response_format:
                    generate_params["response_format"] = strategy.response_format

            response = await self.llm_provider.generate(**generate_params)
            content = response if isinstance(response, str) else response.content

            # For function calling, extract from tool_calls
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Parse response to handle tool_calls format
                if hasattr(response, "tool_calls") and response.tool_calls:
                    # Extract arguments from first tool call
                    args = response.tool_calls[0].function.get("arguments", "{}")

                    # arguments may be a string (most providers) or dict (some providers)
                    if isinstance(args, dict):
                        # Convert dict to JSON string for model_validate_json
                        content = json.dumps(args)
                    else:
                        # Already a string
                        content = args
            else:
                # For non-function-calling modes, strip markdown code blocks if present
                # Some LLMs return: ```json\n{...}\n``` instead of raw JSON
                # Or even worse: "Here's the response:\n```json\n{...}\n```"
                if isinstance(content, str):
                    content = content.strip()

                    # Check if content contains a markdown code block
                    if "```" in content:
                        # Extract JSON from markdown code block
                        # Handle both cases:
                        # 1. ```json\n{...}\n```
                        # 2. Some text\n```json\n{...}\n```\nMore text
                        import re

                        # Match ```json (or ```JSON or just ```) followed by content until closing ```
                        pattern = r"```(?:json|JSON)?\s*\n(.*?)\n```"
                        match = re.search(pattern, content, re.DOTALL)
                        if match:
                            content = match.group(1).strip()
                        elif content.startswith("```"):
                            # Fallback to old logic if regex fails
                            lines = content.split("\n")
                            if lines and lines[0].startswith("```"):
                                lines = lines[1:]
                            if lines and lines[-1].strip() == "```":
                                lines = lines[:-1]
                            content = "\n".join(lines).strip()

            return schema_model.model_validate_json(content)

        # Execute with retry and error handling
        result, error_result = await self.llm_error_handler.with_retry(
            operation=llm_operation
        )

        if result is not None:
            return result

        # All retries exhausted or non-retryable error
        if error_result:
            error_msg = error_result.message
            logger.error(f"Structured generation failed after retries: {error_msg}")
            raise MilestoneEngineError(
                f"Structured output generation failed: {error_msg}"
            )
        else:
            raise MilestoneEngineError(
                "Structured output generation failed with unknown error"
            )

    # =========================================================================
    # Response Processing
    # =========================================================================

    async def _process_response_structured(
        self,
        case: Case,
        user_message: str,
        response_obj: BaseInteractionResponse,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[Case, dict[str, Any]]:
        """Process structured response and update case state."""

        # Validate reasoning-first requirement
        is_valid, validation_errors = validate_reasoning_first(response_obj, case)
        if not is_valid:
            error_msg = "Reasoning validation failed:\n" + "\n".join(validation_errors)
            logger.warning(
                f"Reasoning validation failed for case {case.case_id}: {error_msg}"
            )
            raise ValueError(error_msg)

        # Initialize metadata
        metadata = {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
            "outcome": TurnOutcome.CONVERSATION,
        }

        # Handle file uploads (common across all states)
        if attachments:
            for attachment in attachments:
                uploaded_file = self._create_uploaded_file_from_attachment(
                    case=case, attachment=attachment, turn_number=case.current_turn
                )
                case.uploaded_files.append(uploaded_file)
                metadata["files_uploaded"] = metadata.get("files_uploaded", []) + [
                    uploaded_file.file_id
                ]

        # Dispatch based on response type
        if isinstance(response_obj, InquiryResponse):
            await self._apply_inquiry_updates(
                case, response_obj.state_updates, metadata
            )
        elif isinstance(response_obj, TerminalResponse):
            # Terminal updates typically just documentation, no deep state change
            pass
        else:
            # Investigation updates (Verification, Hypothesis, Resolution, General)
            # All check 'state_updates' which matches InvestigationStateUpdate structure
            await self._apply_investigation_updates(
                case, response_obj.state_updates, metadata, attachments
            )

        return case, metadata

    async def _apply_inquiry_updates(
        self, case: Case, updates: Any, metadata: dict[str, Any]
    ) -> None:
        """Apply updates during INQUIRY phase."""
        if updates.proposed_problem_statement:
            case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

        # Convert and store problem_confirmation from LLM schema to domain model
        if updates.problem_confirmation:
            from faultmaven.modules.case.domain.models import (
                ProblemConfirmation as DomainProblemConfirmation,
            )

            case.inquiry.problem_confirmation = DomainProblemConfirmation(
                problem_type=updates.problem_confirmation.problem_type,
                severity_guess=updates.problem_confirmation.severity_guess,
                preliminary_guidance=updates.problem_confirmation.preliminary_guidance
                or "",  # Convert None to empty string
            )

        # Convert and store preliminary_urgency from LLM schema to domain model
        if updates.preliminary_urgency:
            from faultmaven.modules.case.domain.models import (
                PreliminaryUrgency as DomainPreliminaryUrgency,
                UrgencyLevel,
            )

            case.inquiry.preliminary_urgency = DomainPreliminaryUrgency(
                level=UrgencyLevel(
                    updates.preliminary_urgency.level.lower()
                ),  # Convert uppercase to lowercase enum
                impact_assessment=updates.preliminary_urgency.impact_assessment,
                assessed_at_turn=case.current_turn,  # Use current turn number
            )

        # STAGE 1: Extract problem statement from LLM (first turn only)
        # Extract problem statement but DON'T auto-confirm yet
        if updates.problem_confirmation and not case.inquiry.proposed_problem_statement:
            if updates.problem_confirmation.preliminary_guidance:
                case.inquiry.proposed_problem_statement = (
                    updates.problem_confirmation.preliminary_guidance
                )
                logger.info(
                    f"Problem statement extracted from preliminary_guidance: {updates.problem_confirmation.problem_type}"
                )
            # If no preliminary_guidance but proposed_problem_statement exists in updates,
            # it was already set above at line 685-686

        # STAGE 2: Conditional auto-confirm for urgent ongoing issues
        # Only auto-confirm if ALL conditions are met:
        # - Urgency is CRITICAL or HIGH
        # - Problem is ongoing (not historical)
        # - We have a proposed problem statement (non-empty)
        # - Not already confirmed
        if (
            updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and case.inquiry.proposed_problem_statement
            and case.inquiry.proposed_problem_statement.strip()  # Ensure non-empty
            and not case.inquiry.problem_statement_confirmed
        ):
            # Auto-confirm for urgent ongoing production issues
            case.inquiry.problem_statement_confirmed = True
            case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
            case.inquiry.decided_to_investigate = True
            case.inquiry.decision_made_at = datetime.now(UTC)
            logger.info(
                f"Auto-confirmed urgent issue: {updates.preliminary_urgency.level} + ongoing, "
                f"problem_type={updates.problem_confirmation.problem_type if updates.problem_confirmation else 'unknown'}"
            )
        elif (
            updates.preliminary_urgency
            and updates.preliminary_urgency.level in ["CRITICAL", "HIGH"]
            and updates.preliminary_urgency.is_ongoing
            and not case.inquiry.proposed_problem_statement
        ):
            # Log info if we can't auto-confirm due to missing problem statement
            # This is EXPECTED behavior when user query is urgent but vague - LLM will
            # ask clarifying questions first, then auto-confirm on subsequent turn once
            # it has enough information to formulate a clear problem statement
            logger.info(
                f"Urgent issue detected but cannot auto-confirm yet: proposed_problem_statement is missing. "
                f"Will auto-confirm on next turn once LLM extracts clear problem statement from user responses."
            )

        # Handle explicit user confirmation/corrections logic (Bug #1)
        # If the LLM has refined the statement based on user corrections, it will come through updates.proposed_problem_statement
        # If the user explicitly confirmed (e.g. "Yes"), the LLM should have updated problem_confirmation or proposed_problem_statement

        # If we have a problem statement but it's not confirmed yet, the Agent should ask.
        # This is handled by the Prompt (asking for confirmation).
        # We ensure metadata reflects the state.

        # Check for KB Resolution
        if updates.knowledge_resolution:
            case.inquiry.knowledge_resolution = KnowledgeResolution(
                match_id=updates.knowledge_resolution.match_id,
                match_type=updates.knowledge_resolution.match_type,
                solution_applied=updates.knowledge_resolution.solution_applied,
                user_confirmation=updates.knowledge_resolution.user_confirmation,
            )
            # This triggers Fast-Track in _check_fast_track_resolution

    async def _apply_investigation_updates(
        self,
        case: Case,
        updates: Any,
        metadata: dict[str, Any],
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Apply updates during INVESTIGATING phase."""
        from faultmaven.modules.case.domain.services.investigation_router import (
            determine_investigation_path,
        )

        # 0. Check for Proactive Blocker Detection
        if hasattr(updates, "missing_critical_data") and updates.missing_critical_data:
            blocker = updates.missing_critical_data
            if blocker.triggers_degraded_mode and not case.degraded_mode:
                # Enter degraded mode immediately
                from faultmaven.modules.case.domain.models import DegradedModeType

                case.degraded_mode = DegradedMode(
                    mode_type=DegradedModeType.DATA_BLOCKER,
                    reason=blocker.description,
                    attempted_actions=[
                        f"Expected: {blocker.what_was_expected}, Found: {blocker.what_was_found}"
                    ],
                    fallback_offered=(
                        ", ".join(blocker.suggested_alternatives)
                        if blocker.suggested_alternatives
                        else None
                    ),
                )
                logger.warning(
                    f"Case {case.case_id} entered degraded mode immediately due to data blocker: {blocker.description}"
                )
                metadata["degraded_mode_entered"] = True
                metadata["progress_made"] = False  # Blocker prevents progress

        # Track evidence quality issues (non-blocking)
        if (
            hasattr(updates, "evidence_quality_issues")
            and updates.evidence_quality_issues
        ):
            for issue in updates.evidence_quality_issues:
                logger.info(
                    f"Evidence quality issue detected: {issue.evidence_id} - {issue.issue_type} ({issue.severity})"
                )
                # Could store these in case metadata for future reference
                metadata.setdefault("evidence_quality_issues", []).append(
                    {
                        "evidence_id": issue.evidence_id,
                        "issue_type": issue.issue_type,
                        "severity": issue.severity,
                    }
                )

        # 1. Update Milestones
        if updates.milestones:
            m = updates.milestones
            p = case.progress
            # Only set to True (never revert)
            milestone_fields = [
                "symptom_verified",
                "scope_assessed",
                "timeline_established",
                "changes_identified",
                "root_cause_identified",
                "mitigation_applied",
                "solution_proposed",
                "solution_applied",
                "solution_verified",
            ]
            for field in milestone_fields:
                if getattr(m, field, False):
                    # Only append if transitioning from False to True
                    if not getattr(p, field, False):
                        setattr(p, field, True)
                        metadata["milestones_completed"].append(field)

            # Bug #3: Path Selection Trigger
            # Check if symptom_verified was just completed
            if (
                "symptom_verified" in metadata["milestones_completed"]
                and not case.path_selection
            ):
                case.path_selection = determine_investigation_path(
                    case.problem_verification
                )
                logger.info(
                    f"Path Selection Triggered: {case.path_selection.path} "
                    f"(reason: {case.path_selection.rationale})"
                )
                # If MITIGATION_FIRST selected and confirmed, agent prompts will adapt automatically next turn

            if m.root_cause_likelihood is not None:
                p.root_cause_likelihood = m.root_cause_likelihood
            if m.root_cause_method:
                p.root_cause_method = m.root_cause_method

        # 2. Add Evidence
        # a) From attachments (explicit linking)
        if attachments:
            for attachment in attachments:
                # Check if this attachment was referenced in evidence_to_add
                # This is tricky matching. For now, create generic evidence for attachments
                # UNLESS the LLM explicitly created evidence for it?
                # The design says: "uploaded files AND create evidence...".
                # Let's keep the existing logic: create evidence for all attachments,
                # BUT we can update their metadata if LLM provides specific evidence_to_add?
                # Simplification: Create evidence for all attachments.
                evidence = self._create_evidence_from_attachment(
                    case, attachment, case.current_turn
                )
                case.evidence.append(evidence)
                metadata["evidence_added"].append(evidence.evidence_id)

        # b) From 'evidence_to_add' (text snippets, logs, non-file evidence)
        if hasattr(updates, "evidence_to_add") and updates.evidence_to_add:
            for ev_item in updates.evidence_to_add:
                # Deduplication logic (basic)
                ev = Evidence(
                    evidence_id=f"ev_{uuid4().hex[:12]}",
                    summary=ev_item.summary,
                    content_ref=ev_item.content_ref,
                    category=ev_item.category,
                    source_type=ev_item.source_type,
                    collected_at=datetime.now(UTC),
                    collected_by=case.user_id,
                    collected_at_turn=case.current_turn,
                    form=EvidenceForm.USER_INPUT,  # Default
                    # Mandatory fields
                    primary_purpose="Investigation context",
                    preprocessed_content=ev_item.summary,  # Use summary as content for text evidence
                    content_size_bytes=len(ev_item.summary),
                    preprocessing_method="none",
                )
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)

        # 2b. Process Evidence for Milestone Advancement
        # After creating evidence, check if it advances any milestones opportunistically
        if metadata["evidence_added"]:
            from faultmaven.core.investigation.evidence_processor import (
                process_evidence,
            )

            for ev_id in metadata["evidence_added"]:
                ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
                if ev:
                    milestones_advanced = await process_evidence(case, ev)
                    # Track milestones advanced by this evidence
                    for milestone_name in milestones_advanced:
                        if milestone_name not in metadata["milestones_completed"]:
                            metadata["milestones_completed"].append(milestone_name)
                    logger.info(
                        f"Evidence {ev_id} advanced {len(milestones_advanced)} milestone(s): {milestones_advanced}"
                    )

        # 2c. Trigger Path Selection if symptom_verified milestone completed
        if "symptom_verified" in metadata.get("milestones_completed", []):
            if not case.path_selection:
                case.path_selection = determine_investigation_path(
                    case.problem_verification
                )
                logger.info(
                    f"Path selection triggered: {case.path_selection.path.value} "
                    f"(auto={case.path_selection.auto_selected})"
                )

        # 3. Add/Update Hypotheses
        if hasattr(updates, "hypotheses_to_add") and updates.hypotheses_to_add:
            for h_item in updates.hypotheses_to_add:
                h = self.hypothesis_manager.create_hypothesis(
                    statement=h_item.statement,
                    category=h_item.category,
                    initial_likelihood=h_item.likelihood,
                    current_turn=case.current_turn,
                    status=HypothesisStatus.ACTIVE,
                )
                case.hypotheses[h.hypothesis_id] = h
                metadata["hypotheses_generated"].append(h.hypothesis_id)

        # 4. Link Evidence (Partial Application Check)
        if (
            hasattr(updates, "hypothesis_evidence_links")
            and updates.hypothesis_evidence_links
        ):
            feedback = []
            for link in updates.hypothesis_evidence_links:
                # Resolve partial IDs like 'new_index_0' to actual IDs if we just created them
                h_id = self._resolve_id_ref(
                    link.hypothesis_id_ref,
                    metadata.get("hypotheses_generated", []),
                    "hyp",
                )
                e_id = self._resolve_id_ref(
                    link.evidence_id_ref, metadata.get("evidence_added", []), "ev"
                )

                # Check existence
                if h_id not in case.hypotheses:
                    msg = f"Validation Error: Hypothesis ID '{h_id}' not found (resolved from '{link.hypothesis_id_ref}'). Cannot link evidence."
                    feedback.append(msg)
                    logger.warning(msg)
                    continue

                # Check evidence existence (scan list)
                ev_exists = any(e.evidence_id == e_id for e in case.evidence)
                if not ev_exists:
                    msg = f"Validation Error: Evidence ID '{e_id}' not found (resolved from '{link.evidence_id_ref}'). Cannot link to hypothesis."
                    feedback.append(msg)
                    logger.warning(msg)
                    continue

                self.hypothesis_manager.link_evidence(
                    case.hypotheses[h_id],
                    e_id,
                    link.stance == EvidenceStance.SUPPORTS,
                    case.current_turn,
                    reasoning=link.reasoning,
                    stance_confidence=link.stance_confidence,
                )

            if feedback:
                current_feedback = metadata.get("system_feedback", "")
                metadata["system_feedback"] = (
                    (current_feedback + "\n" + "\n".join(feedback))
                    if current_feedback
                    else "\n".join(feedback)
                )

        # 5. Solutions
        if hasattr(updates, "solutions_to_add") and updates.solutions_to_add:
            for s_item in updates.solutions_to_add:
                sol = Solution(
                    solution_id=f"sol_{uuid4().hex[:12]}",
                    description=s_item.description,
                    title=f"Solution: {s_item.solution_type}",  # Added missing title
                    type=s_item.solution_type,
                    status="proposed",
                    proposed_at=datetime.now(UTC),
                )
                case.solutions.append(sol)
                metadata["solutions_proposed"].append(sol.solution_id)

        # Bug #4: Evidence-Milestone Linking (Moved here to ensure evidence exists)
        if metadata["milestones_completed"] and metadata["evidence_added"]:
            for ev_id in metadata["evidence_added"]:
                ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
                if ev:
                    ev.advances_milestones.extend(metadata["milestones_completed"])

        # Bug #8: Robust Turn Outcome Determination
        metadata["outcome"] = self._determine_turn_outcome(
            case, metadata, updates.outcome
        )

    # =========================================================================
    # State Management
    # =========================================================================

    async def _transition_to_investigating(self, case: Case) -> None:
        """
        Transition case from INQUIRY to INVESTIGATING.

        This creates the initial investigation structures and copies the
        confirmed problem statement to the case description.
        """
        logger.info(f"Transitioning case {case.case_id} to INVESTIGATING")

        # Copy confirmed problem statement to description BEFORE changing status
        # (Pydantic validation requires description to be set before INVESTIGATING status)
        if case.inquiry.proposed_problem_statement:
            case.description = case.inquiry.proposed_problem_statement

        # Change status (Pydantic validation happens here)
        case.status = CaseStatus.INVESTIGATING

        # Initialize investigation progress
        case.progress = InvestigationProgress()

        # Initialize problem verification with confirmed statement
        verification_kwargs = {
            "symptom_statement": case.description or "Unspecified issue",
            "severity": "UNKNOWN",  # Default
        }

        # Hydrate from problem confirmation if available
        if case.inquiry.problem_confirmation:
            pc = case.inquiry.problem_confirmation
            verification_kwargs["severity"] = pc.severity_guess.upper()

        # Hydrate from preliminary urgency if available
        if case.inquiry.preliminary_urgency:
            pu = case.inquiry.preliminary_urgency
            if pu.level:
                verification_kwargs["urgency_level"] = (
                    pu.level.lower()
                )  # Convert to lowercase for enum
                # If severity still unknown, use urgency level as severity (keep uppercase for severity)
                if verification_kwargs["severity"] == "UNKNOWN":
                    verification_kwargs["severity"] = (
                        pu.level
                    )  # Keep uppercase for severity field

        case.problem_verification = ProblemVerification(**verification_kwargs)

        # Determine path selection
        case.path_selection = determine_investigation_path(case.problem_verification)
        logger.info(
            f"Selected investigation path: {case.path_selection.path} (reason: {case.path_selection.rationale})"
        )

        # Initialize empty collections (already defaults in Case model)
        # case.evidence, case.hypotheses, case.solutions, case.turn_history are defaults

    async def _check_automatic_transitions(
        self, case: Case, metadata: dict[str, Any]
    ) -> Case:
        """
        Check if case should automatically transition status.

        Automatic Transitions:
        - INQUIRY -> INVESTIGATING when decided_to_investigate=True
        - INQUIRY -> RESOLVED/CLOSED for fast-track
        - INVESTIGATING -> RESOLVED when solution_verified=True
        """
        old_status = case.status

        # 1. INQUIRY transitions
        if case.status == CaseStatus.INQUIRY:
            # Check for fast-track resolution via KB
            if self._check_fast_track_resolution(case):
                # Status changed in _check_fast_track_resolution
                metadata["status_transitioned"] = True
                case.status_history.append(
                    CaseStatusTransition(
                        from_status=old_status,
                        to_status=CaseStatus.RESOLVED,
                        triggered_by="system",
                        reason="Fast-track resolution via KB match",
                    )
                )
                return case

            # Check for transition to investigation
            if case.inquiry.decided_to_investigate or (
                case.inquiry.problem_statement_confirmed
                and case.inquiry.problem_confirmation
            ):
                await self._transition_to_investigating(case)
                metadata["status_transitioned"] = True
                case.status_history.append(
                    CaseStatusTransition(
                        from_status=old_status,
                        to_status=CaseStatus.INVESTIGATING,
                        triggered_by="system",
                        reason="Problem confirmed and investigation triggered",
                    )
                )
                return case

        # 2. INVESTIGATING -> RESOLVED (using terminal_transitions module)
        if case.status == CaseStatus.INVESTIGATING and case.progress.solution_verified:
            from faultmaven.core.investigation.terminal_transitions import (
                check_terminal_transitions,
            )

            await check_terminal_transitions(case)
            metadata["status_transitioned"] = True

        return case

    def _check_fast_track_resolution(self, case: Case) -> bool:
        """Check if case can be Fast-Track resolved via KB match.

        Fast-track resolution uses closure_reason="resolved" (same as normal resolution)
        because a solution WAS found (via knowledge base). The distinction between
        fast-track and normal resolution is captured in case.inquiry.knowledge_resolution
        and status transition history.
        """
        if case.inquiry.knowledge_resolution:
            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(UTC)
            case.closed_at = datetime.now(UTC)
            case.closure_reason = "resolved"  # KB match = solution found = resolved

            # Log transition
            logger.info(
                f"Case {case.case_id} Fast-Track resolved via KB match: "
                f"{case.inquiry.knowledge_resolution.match_id}"
            )
            return True
        return False

    def _determine_turn_outcome(
        self, case: Case, metadata: dict[str, Any], reported_outcome: TurnOutcome
    ) -> TurnOutcome:
        """
        Determine turn outcome classification (Bug #8).
        Checked AFTER milestone detection and evidence processing.
        """
        from faultmaven.core.investigation.turn_outcome import determine_turn_outcome

        return determine_turn_outcome(
            case=case,
            progress_made=metadata.get("progress_made", False),
            milestones_completed=metadata.get("milestones_completed", []),
            evidence_added=metadata.get("evidence_added", []),
            hypotheses_generated=len(metadata.get("hypotheses_generated", [])),
            solutions_proposed=len(metadata.get("solutions_proposed", [])),
        )

    def _enter_degraded_mode(
        self, case: Case, mode_type: str, reason: str | None = None
    ) -> None:
        """
        Enter degraded mode when investigation is stuck.

        Args:
            case: Current case
            mode_type: Type of degradation (no_progress, limited_data, etc.)
            reason: Optional detailed reason
        """
        if case.degraded_mode:
            logger.warning(f"Case {case.case_id} already in degraded mode")
            return

        # Determine reason if not provided
        if not reason:
            if mode_type == "no_progress":
                reason = (
                    f"No progress for {case.turns_without_progress} consecutive turns"
                )
            else:
                reason = "Investigation limitations encountered"

        case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType(mode_type),
            reason=reason,
            entered_at=datetime.now(UTC),
            attempted_actions=[],  # TODO: Track attempted actions
        )

        logger.info(
            f"Case {case.case_id} entered degraded mode: {mode_type} - {reason}"
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _create_uploaded_file_from_attachment(
        self, case: Case, attachment: dict[str, Any], turn_number: int
    ) -> "UploadedFile":  # noqa: F821
        """
        Create uploaded file record from attachment.

        Args:
            case: Current case
            attachment: Attachment metadata with file_id, filename, data_type, etc.
            turn_number: Current turn number

        Returns:
            UploadedFile object
        """
        from faultmaven.modules.case.contracts import UploadedFile

        uploaded_file = UploadedFile(
            file_id=attachment.get("file_id", f"file_{uuid4().hex[:12]}"),
            filename=attachment.get("filename", "unknown"),
            size_bytes=attachment.get("size", 0),
            data_type=attachment.get("data_type", "unknown"),
            uploaded_at_turn=turn_number,
            uploaded_at=datetime.now(UTC),
            source_type=attachment.get("source_type", "file_upload"),
            preprocessing_summary=attachment.get("summary", None),
            content_ref=attachment.get("s3_uri", attachment.get("file_id", "unknown")),
        )

        return uploaded_file

    def _create_evidence_from_attachment(
        self, case: Case, attachment: dict[str, Any], turn_number: int
    ) -> Evidence:
        """
        Create evidence object from file attachment.

        Args:
            case: Current case
            attachment: Attachment metadata
            turn_number: Current turn number

        Returns:
            Evidence object
        """
        # Infer category based on investigation state
        category = self._infer_evidence_category(case)

        # Create evidence
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            summary=f"Uploaded file: {attachment.get('filename', 'unknown')}",
            preprocessed_content="[Content to be preprocessed]",  # Placeholder
            content_ref=attachment.get("s3_uri", "unknown"),
            content_size_bytes=attachment.get("size", 0),
            preprocessing_method="pending",
            category=category,
            source_type=EvidenceSourceType.LOG_FILE,  # Default
            form=EvidenceForm.DOCUMENT,
            advances_milestones=[],  # Calculated later
            collected_at=datetime.now(UTC),
            collected_by=case.user_id,
            collected_at_turn=turn_number,
            primary_purpose="File Analysis",  # Mandatory
        )

        return evidence

    def _infer_evidence_category(self, case: Case) -> EvidenceCategory:
        """
        Infer evidence category from investigation state.

        Rules:
        - If verification incomplete → SYMPTOM_EVIDENCE
        - If solution proposed → RESOLUTION_EVIDENCE
        - Otherwise → CAUSAL_EVIDENCE
        """
        if not case.progress.verification_complete:
            return EvidenceCategory.SYMPTOM_EVIDENCE

        if case.progress.solution_proposed:
            return EvidenceCategory.RESOLUTION_EVIDENCE

        return EvidenceCategory.CAUSAL_EVIDENCE

    def _create_turn_record(
        self,
        turn_number: int,
        milestones_completed: list[str],
        evidence_added: list[str],
        hypotheses_generated: list[str],
        hypotheses_validated: list[str],
        solutions_proposed: list[str],
        progress_made: bool,
        outcome: TurnOutcome,
        user_message: str,
        agent_response: str,
        system_feedback: str | None = None,
        momentum: InvestigationMomentum | None = None,
        blocked_reasons: list[str] | None = None,
        next_steps: list[str] | None = None,
        stagnation_detected: str | None = None,
        validation_repairs: list[str] | None = None,
    ) -> TurnProgress:
        """Create turn progress record."""
        return TurnProgress(
            turn_number=turn_number,
            timestamp=datetime.now(UTC),
            milestones_completed=milestones_completed,
            evidence_added=evidence_added,
            hypotheses_generated=hypotheses_generated,
            hypotheses_validated=hypotheses_validated,
            solutions_proposed=solutions_proposed,
            progress_made=progress_made,
            actions_taken=self._extract_actions(agent_response),
            outcome=outcome,
            user_message_summary=self._summarize_text(user_message, 200),
            agent_response_summary=self._summarize_text(agent_response, 500),
            system_feedback=system_feedback,
            momentum=momentum,
            blocked_reasons=blocked_reasons or [],
            next_steps=next_steps or [],
            stagnation_detected=stagnation_detected,
            validation_repairs=validation_repairs or [],
        )

    def _extract_actions(self, agent_response: str) -> list[str]:
        """Extract action keywords from agent response."""
        action_keywords = [
            "verified",
            "identified",
            "proposed",
            "tested",
            "confirmed",
            "analyzed",
        ]
        actions = []

        response_lower = agent_response.lower()
        for keyword in action_keywords:
            if keyword in response_lower:
                actions.append(keyword)

        return actions[:5]  # Limit to 5

    def _check_if_progress_made(self, metadata: dict[str, Any]) -> bool:
        """Check if any meaningful status update was made."""
        keys_to_check = [
            "milestones_completed",
            "evidence_added",
            "hypotheses_generated",
            "hypotheses_validated",
            "solutions_proposed",
            "files_uploaded",
        ]
        for key in keys_to_check:
            if metadata.get(key):
                return True

        if metadata.get("status_transitioned"):
            return True

        return False

    def _summarize_text(self, text: str, max_length: int = 200) -> str:
        """Summarize long text for storage."""
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    # =============================================================================
    # Phase 4 Housekeeping & Helpers
    # =============================================================================

    def _perform_hypothesis_housekeeping(
        self, case: Case, metadata: dict[str, Any]
    ) -> None:
        """Apply confidence decay and anchoring detection."""
        active_hypotheses = [
            h for h in case.hypotheses.values() if h.status == HypothesisStatus.ACTIVE
        ]

        if not active_hypotheses:
            return

        # 1. Apply confidence decay to stagnant hypotheses
        for h in active_hypotheses:
            # We decay if NO progress was made this turn for this specific hypothesis
            # (Note: link_evidence resets iterations_without_progress to 0)
            self.hypothesis_manager.apply_likelihood_decay(h, case.current_turn)

        # 2. Detect anchoring and add system feedback if necessary
        is_anchored, reason, hypothesis_ids = self.hypothesis_manager.detect_anchoring(
            active_hypotheses, case.current_turn
        )

        if is_anchored:
            logger.warning(f"Anchoring detected for case {case.case_id}: {reason}")
            # Add to system feedback for next turn
            anchoring_msg = f"CRITICAL: {reason}. You are stalled on these theories. Diversify your approach and generate alternative hypotheses from different categories."

            current_feedback = metadata.get("system_feedback", "")
            metadata["system_feedback"] = (
                (current_feedback + "\n" + anchoring_msg)
                if current_feedback
                else anchoring_msg
            )

    def _resolve_id_ref(self, ref: str, created_ids: list[str], prefix: str) -> str:
        """Resolve 'new_index_N' to actual ID from created_ids list or return ref as-is."""
        if ref and ref.startswith("new_index_"):
            try:
                idx_str = ref.replace("new_index_", "")
                idx = int(idx_str)
                if 0 <= idx < len(created_ids):
                    return created_ids[idx]
            except (ValueError, IndexError):
                pass
        return ref


# =============================================================================
# Exceptions
# =============================================================================


class MilestoneEngineError(Exception):
    """Base exception for milestone engine errors."""

    pass

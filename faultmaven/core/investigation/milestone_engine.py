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
from faultmaven.utils.schema_converter import create_response_format_json_schema
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
    ) -> dict[str, Any]:
        """
        Process a single conversation turn.

        This is the main entry point for the milestone engine. It:
        1. Generates status-appropriate prompt
        2. Invokes LLM with structured output
        3. Processes response and updates case state
        4. Records turn progress
        5. Checks for automatic status transitions

        Args:
            case: Current case
            user_message: User's message this turn
            attachments: Optional file attachments

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
        logger.info(
            f"Processing turn {case.current_turn} for case {case.case_id} "
            f"(status: {case.status})"
        )

        try:
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
            prompt = get_prompt_for_case(case, user_message, kb_results)

            # Determine schema based on status/stage
            if case.status == CaseStatus.INQUIRY:
                schema_model = InquiryResponse
            elif case.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
                schema_model = TerminalResponse
            else:
                schema_model = get_schema_for_stage(
                    case.current_stage or InvestigationStage.SYMPTOM_VERIFICATION
                )

            # 2. Invoke LLM with structured output
            response_obj = await self._generate_structured_output(prompt, schema_model)

            # 3. Process response and update case state
            case_updated, metadata = await self._process_response_structured(
                case, user_message, response_obj, attachments
            )

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

            # Step 5.6: Generate working conclusion if significant progress
            if (
                metadata.get("milestones_completed")
                or progress_metrics.investigation_momentum == InvestigationMomentum.HIGH
            ):
                working_conclusion = generate_working_conclusion(
                    case=case_updated, current_turn=case_updated.current_turn
                )
                case_updated.working_conclusion = working_conclusion
                logger.info(
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
        Generate structured output from LLM.

        Args:
            prompt: User prompt
            schema_model: Pydantic model class for expected output

        Returns:
            Instantiated Pydantic model
        """
        # Create proper json_schema response format
        response_format = create_response_format_json_schema(schema_model)

        # CRITICAL: Include schema in prompt for json_object fallback mode
        # When providers don't support strict json_schema (e.g., Groq Llama-3.3),
        # they fall back to json_object which DISCARDS the schema from response_format.
        # We must include the schema in the prompt text itself to ensure the model
        # knows what structure to generate.
        schema_json = json.dumps(schema_model.model_json_schema(), indent=2)
        json_instruction = (
            "\n\n## RESPONSE FORMAT\n"
            "You MUST respond with valid JSON matching this exact schema:\n\n"
            f"```json\n{schema_json}\n```\n\n"
            "IMPORTANT:\n"
            "- Use the exact field names shown in the schema\n"
            "- Do not add extra fields not in the schema\n"
            "- Do not include any text before or after the JSON\n"
            "- Ensure all required fields are present\n"
        )
        prompt_with_schema = f"{prompt}{json_instruction}"

        # Define the LLM operation for retry
        async def llm_operation():
            response = await self.llm_provider.generate(
                prompt=prompt_with_schema,
                max_tokens=4000,
                temperature=0.2,  # Lower temperature for structured output
                response_format=response_format,
            )
            content = response if isinstance(response, str) else response.content
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

        if updates.problem_confirmation:
            # Transfer guess to inquiry data
            case.inquiry.problem_statement_confirmed = (
                True  # Heuristic: if agent has guidance, it means it's confirmed
            )
            case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
            logger.info(
                f"Problem confirmed during inquiry: {updates.problem_confirmation.problem_type}"
            )

        # Check for user confirmation (still simple text check? or should LLM signal it?)
        # Design guide says: "Step C: User confirms... System detects and sets"
        # Ideally, we want the LLM to signal 'problem_confirmed' in the schema,
        # but InquiryStateUpdate only has 'problem_confirmation' (Agent's understanding).

        # Wait, the Design says: "User says 'yes' -> System detects".
        # So we KEEP the text heuristic for confirmation? Or add it to schema?
        # Schema has `problem_statement_confirmed`? No, InquiryStateUpdate doesn't.
        # Let's keep the logic: If LLM sees user confirmed, it should PROPOSE the FINAL statement again?
        # Actually, let's keep the heuristic for "Yes" -> confirm for now as per design 3.4
        pass

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
                    form=EvidenceForm.TEXT,  # Default
                )
                case.evidence.append(ev)
                metadata["evidence_added"].append(ev.evidence_id)

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

        # Determine Outcome
        if metadata["milestones_completed"]:
            metadata["outcome"] = TurnOutcome.MILESTONE_COMPLETED
        elif updates.outcome:
            metadata["outcome"] = updates.outcome

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

        # Change status
        case.status = CaseStatus.INVESTIGATING

        # Copy confirmed problem statement to description
        if case.inquiry.proposed_problem_statement:
            case.description = case.inquiry.proposed_problem_statement

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
                verification_kwargs["urgency_level"] = pu.level
                if verification_kwargs["severity"] == "UNKNOWN":
                    verification_kwargs["severity"] = pu.level.value.upper()

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

        # 2. INVESTIGATING -> RESOLVED
        if case.status == CaseStatus.INVESTIGATING and case.progress.solution_verified:
            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(UTC)
            case.closed_at = datetime.now(UTC)
            case.closure_reason = "resolved"
            metadata["status_transitioned"] = True

            case.status_history.append(
                CaseStatusTransition(
                    from_status=old_status,
                    to_status=CaseStatus.RESOLVED,
                    triggered_by="system",
                    reason="Solution verified via milestones",
                )
            )

            logger.info(
                f"Case {case.case_id} automatically transitioned to RESOLVED "
                f"(solution verified)"
            )

        return case

    def _check_fast_track_resolution(self, case: Case) -> bool:
        """Check if case can be Fast-Track resolved via KB match."""
        if case.inquiry.knowledge_resolution:
            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(UTC)
            case.closed_at = datetime.now(UTC)
            case.closure_reason = "fast_track_kb_match"

            # Log transition
            logger.info(
                f"Case {case.case_id} Fast-Track resolved via KB match: "
                f"{case.inquiry.knowledge_resolution.match_id}"
            )
            return True
        return False

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
            active_hypotheses
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

"""Investigation Service - Manages milestone-based troubleshooting workflow

Purpose: Orchestrate investigation turns and milestone progress tracking

This service wraps the MilestoneEngine and provides:
- Access control for investigations
- Case retrieval and persistence
- Turn creation and processing
- Progress tracking and reporting
- Integration with session management
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.models.api_models import CaseQueryRequest, CaseQueryResponse, IntentType

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import Case, CaseStatus
from faultmaven.services.base import BaseService
from faultmaven.utils.serialization import to_json_compatible


class InvestigationService(BaseService):
    """
    Service for managing investigation turns and milestone progress.

    Coordinates between:
    - MilestoneEngine (core investigation logic)
    - CaseRepository (persistence)
    - Access control (user permissions)
    """

    def __init__(
        self, milestone_engine: MilestoneEngine, case_repository: CaseRepository
    ):
        """
        Initialize investigation service.

        Args:
            milestone_engine: Core investigation engine with LLM integration
            case_repository: Case persistence layer
        """
        super().__init__("investigation_service")
        self.engine = milestone_engine
        self.repository = case_repository

    @trace("investigation_service_process_turn")
    async def process_turn(
        self, case_id: str, user_id: str, request: CaseQueryRequest
    ) -> CaseQueryResponse:
        """
        Process a user message and update investigation.

        Workflow:
        1. Retrieve case from repository
        2. Verify user has access
        3. Process turn via MilestoneEngine
        4. Return response with progress updates

        Args:
            case_id: Case identifier
            user_id: User making the request
            request: Turn request with message and optional attachments

        Returns:
            CaseQueryResponse with agent response, milestones, and progress

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If turn processing fails
        """
        try:
            # 1. Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # 2. Check permissions (simple owner check)
            if case.user_id != user_id:
                self.logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # 3. Save user message to conversation history BEFORE processing
            from datetime import datetime, timezone
            from uuid import uuid4

            # Calculate next turn number (don't commit yet - only after successful processing)
            next_turn = case.current_turn + 1

            # Per case-storage-design.md Section 4.7, use "timestamp" not "created_at"
            # Preserve intent metadata for debugging, analytics, and audit trail
            user_message_obj = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": next_turn,
                "role": "user",
                "message_type": "user_query",
                "content": request.message,
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": user_id,
                "token_count": None,
                "metadata": {
                    "has_attachments": bool(request.attachments),
                    "attachment_count": (
                        len(request.attachments) if request.attachments else 0
                    ),
                    # Intent metadata for tracing and analytics
                    "intent_type": request.intent.type.value,
                    "intent_metadata": request.intent.model_dump(
                        exclude_unset=True, exclude={"type"}
                    ),
                },
            }
            case.messages.append(user_message_obj)
            case.message_count += 1

            # Persist user message (but NOT turn increment yet - that happens after success)
            await self.repository.save(case)

            # 4. Route based on structured intent (clean, no keyword matching)
            # Intent-based routing eliminates ambiguity and provides single code path
            # for all interactions (conversation history unified).
            intent_type = request.intent.type

            # Heuristic check for greetings if intent is CONVERSATION (default)
            if intent_type == IntentType.CONVERSATION and request.message:
                heuristic_intent = self._detect_intent_heuristic(request.message)
                if heuristic_intent:
                    intent_type = heuristic_intent
                    self.logger.info(
                        f"Heuristic detected intent {intent_type.value} for message: '{request.message}'"
                    )

            if intent_type == IntentType.STATUS_TRANSITION:
                # Explicit state transition (resolve/close) via UI button
                result = await self._handle_status_transition(
                    case=case,
                    user_message=request.message,
                    from_status=request.intent.from_status,
                    to_status=request.intent.to_status,
                    user_confirmed=request.intent.user_confirmed or False,
                )
            elif intent_type == IntentType.CONFIRMATION:
                # Yes/No confirmation response
                result = await self._handle_confirmation(
                    case=case,
                    user_message=request.message,
                    confirmation_value=request.intent.confirmation_value,
                )
            elif intent_type == IntentType.HYPOTHESIS_ACTION:
                # Validate/refute/retire hypothesis
                result = await self._handle_hypothesis_action(
                    case=case,
                    user_message=request.message,
                    hypothesis_id=request.intent.hypothesis_id,
                    action=request.intent.action,
                )
            elif intent_type == IntentType.CONVERSATION:
                # Normal conversation - process via MilestoneEngine
                # Engine handles:
                # - Generating status-based prompt
                # - Invoking LLM with structured output
                # - Updating case state (milestones, evidence, hypotheses)
                # - Pattern matching fallback for natural language (e.g., "close as unresolved")
                # - Automatic status transitions
                # - Saving case via repository
                result = await self.engine.process_turn(
                    case=case,
                    user_message=request.message,
                    attachments=request.attachments,
                    intent_type=intent_type.value,  # Pass intent for logging/tracing
                    intent_data=request.intent.model_dump(exclude_unset=True),
                )
            elif intent_type == IntentType.GREETING:
                # Heuristic greeting response (no LLM)
                result = await self._handle_greeting(case=case)
            else:
                raise ValueError(f"Unknown intent type: {intent_type}")

            # 5. Processing succeeded - commit turn increment
            # Only commit after successful processing to avoid gaps in audit trail on crashes
            result["case_updated"].current_turn = next_turn

            # 6. Build response
            updated_case = result["case_updated"]
            agent_response_text = result["agent_response"]

            # 7. Save agent response to conversation history
            from datetime import datetime, timezone
            from uuid import uuid4

            # Per case-storage-design.md Section 4.7, use "created_at"
            agent_message = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": updated_case.current_turn,
                "role": "agent",
                "message_type": "agent_response",
                "content": agent_response_text,
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": None,  # System/agent has no user_id
                "token_count": None,
                "metadata": {},
            }

            updated_case.messages.append(agent_message)
            updated_case.message_count += 1

            # Save case with agent message
            await self.repository.save(updated_case)

            response = CaseQueryResponse(
                agent_response=agent_response_text,
                turn_number=updated_case.current_turn,
                milestones_completed=result.get("metadata", {}).get(
                    "milestones_completed", []
                ),
                case_status=updated_case.status,
                progress_made=result.get("metadata", {}).get("progress_made", False),
                is_stuck=(
                    updated_case.is_stuck
                    if hasattr(updated_case, "is_stuck")
                    else False
                ),
            )

            self.logger.info(
                f"Processed turn {response.turn_number} for case {case_id}, "
                f"status={response.case_status}, milestones={len(response.milestones_completed)}, "
                f"messages={updated_case.message_count}"
            )

            return response

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            self.logger.error(f"Failed to process turn for case {case_id}: {e}")
            raise ServiceException(f"Turn processing failed: {str(e)}") from e

    @trace("investigation_service_get_progress")
    async def get_progress(self, case_id: str, user_id: str) -> Dict[str, Any]:
        """
        Get current investigation progress.

        Args:
            case_id: Case identifier
            user_id: User making the request

        Returns:
            Progress summary with:
            - case_id, status, current_stage
            - milestones_completed, pending_milestones
            - current_turn, is_stuck, degraded_mode

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                self.logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Return progress summary
            return {
                "case_id": case.case_id,
                "status": case.status.value,
                "current_stage": (
                    case.current_stage.value if case.current_stage else None
                ),
                "milestones_completed": case.progress.completed_milestones,
                "pending_milestones": case.progress.pending_milestones,
                "current_turn": case.current_turn,
                "is_stuck": case.is_stuck if hasattr(case, "is_stuck") else False,
                "degraded_mode": (
                    case.degraded_mode.is_active
                    if hasattr(case, "degraded_mode") and case.degraded_mode
                    else False
                ),
            }

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            self.logger.error(f"Failed to get progress for case {case_id}: {e}")
            raise ServiceException(f"Progress retrieval failed: {str(e)}") from e

    # ============================================================
    # Intent-Based Query Handlers
    # ============================================================

    async def _handle_status_transition(
        self,
        case: "Case",
        user_message: str,
        from_status: Optional[str],
        to_status: Optional[str],
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        """Handle status transition intent with validation.

        Args:
            case: Case entity
            user_message: User's message explaining the transition
            from_status: Expected current status
            to_status: Requested new status
            user_confirmed: Whether user confirmed the transition

        Returns:
            Result dict with agent response and updated case
        """
        self.logger.info(
            f"Processing status transition: {from_status} → {to_status} "
            f"(confirmed={user_confirmed}) for case {case.case_id}"
        )

        # Validate transition request
        if not to_status:
            raise ValueError("to_status is required for status_transition intent")

        # Delegate to milestone engine with structured intent
        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
            intent_type="status_transition",
            intent_data={
                "from_status": from_status,
                "to_status": to_status,
                "user_confirmed": user_confirmed,
            },
        )

        return result

    async def _handle_confirmation(
        self, case: "Case", user_message: str, confirmation_value: Optional[bool]
    ) -> Dict[str, Any]:
        """Handle yes/no confirmation intent.

        Args:
            case: Case entity
            user_message: User's confirmation message
            confirmation_value: True for yes, False for no

        Returns:
            Result dict with agent response and updated case
        """
        self.logger.info(
            f"Processing confirmation: {confirmation_value} for case {case.case_id}"
        )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
            intent_type="confirmation",
            intent_data={"value": confirmation_value},
        )

        return result

    async def _handle_hypothesis_action(
        self,
        case: "Case",
        user_message: str,
        hypothesis_id: Optional[str],
        action: Optional[str],
    ) -> Dict[str, Any]:
        """Handle hypothesis action intent (validate/refute/retire).

        Args:
            case: Case entity
            user_message: User's message about the hypothesis
            hypothesis_id: Target hypothesis ID
            action: Action to perform

        Returns:
            Result dict with agent response and updated case
        """
        self.logger.info(
            f"Processing hypothesis action: {action} on {hypothesis_id} for case {case.case_id}"
        )

        if not hypothesis_id or not action:
            raise ValueError(
                "hypothesis_id and action required for hypothesis_action intent"
            )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
            intent_type="hypothesis_action",
            intent_data={"hypothesis_id": hypothesis_id, "action": action},
        )

        return result

    async def _handle_greeting(self, case: "Case") -> Dict[str, Any]:
        """Handle greeting intent without LLM.

        Args:
            case: Case entity

        Returns:
            Result dict with static agent response and updated case
        """
        self.logger.info(f"Processing greeting for case {case.case_id}")

        # Static response (saving tokens and latency)
        agent_response = (
            "Hello! I'm FaultMaven, an expert SRE troubleshooting copilot. "
            "I can help you diagnose issues, analyze logs, and verify solutions. "
            "Please describe the problem you're observing."
        )

        # No engine call needed - manually construct result
        return {
            "agent_response": agent_response,
            "case_updated": case,
            "metadata": {
                "progress_made": False,
                "milestones_completed": [],
            },
        }

    def _detect_intent_heuristic(self, message: str) -> Optional[IntentType]:
        """Detect intent from message content using simple heuristics.

        Args:
            message: User message text

        Returns:
            Detected IntentType or None
        """
        import re

        clean_msg = message.strip().lower()

        # Greeting patterns (case-insensitive)
        # Matches: "Hi", "Hello", "Hi FaultMaven", "Greetings", "Help"
        # Does NOT match: "Hi, the db is down", "Hello, I have an error"
        greeting_pattern = r"^(hi|hello|hey|greetings|help)( faultmaven)?[\.!]*$"

        if re.match(greeting_pattern, clean_msg):
            return IntentType.GREETING

        return None

    @trace("investigation_service_transition_to_investigating")
    async def transition_to_investigating(
        self, case_id: str, user_id: str, confirmed_description: str
    ) -> Case:
        """
        Transition case from INQUIRY to INVESTIGATING.

        Called when user confirms the problem statement during inquiry phase.

        Args:
            case_id: Case identifier
            user_id: User making the request
            confirmed_description: Confirmed problem description

        Returns:
            Updated case

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If transition fails or invalid state
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Validate current status
            if case.status != CaseStatus.INQUIRY:
                raise ServiceException(
                    f"Cannot transition to INVESTIGATING: case is in {case.status.value} status"
                )

            # Ensure inquiry data is properly set for INVESTIGATING transition
            if not case.inquiry.proposed_problem_statement:
                # Use confirmed_description as the problem statement
                case.inquiry.proposed_problem_statement = confirmed_description

            if not case.inquiry.problem_statement_confirmed:
                case.inquiry.problem_statement_confirmed = True
                case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)

            if not case.inquiry.decided_to_investigate:
                case.inquiry.decided_to_investigate = True
                case.inquiry.decision_made_at = datetime.now(timezone.utc)

            # Update case
            case.description = confirmed_description
            case.status = CaseStatus.INVESTIGATING

            # Path selection is now DEFERRED until symptom verification (Bug #3 fix)
            # Logic moved to MilestoneEngine._process_response_structured via automatic check
            case.path_selection = None

            # Save
            updated_case = await self.repository.save(case)

            self.logger.info(
                f"Transitioned case {case_id} to INVESTIGATING with description: "
                f"{confirmed_description[:100]}..."
            )

            return updated_case

        except (NotFoundError, PermissionDeniedException, ServiceException):
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to transition case {case_id} to INVESTIGATING: {e}"
            )
            raise ServiceException(f"Status transition failed: {str(e)}") from e

    @trace("investigation_service_close_case")
    async def close_case(self, case_id: str, user_id: str, closure_reason: str) -> Case:
        """
        Close a case.

        Args:
            case_id: Case identifier
            user_id: User making the request
            closure_reason: Why the case is being closed
                (resolved | abandoned | escalated | inquiry_only | duplicate | other)

        Returns:
            Updated case

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Update status and timestamps (use model_copy to bypass field-by-field validation)
            now = datetime.now(timezone.utc)
            updated_case_data = case.model_copy(
                update={
                    "status": CaseStatus.CLOSED,
                    "closure_reason": closure_reason,
                    "closed_at": now,
                },
                deep=True,
            )

            # Save
            updated_case = await self.repository.save(updated_case_data)

            self.logger.info(f"Closed case {case_id}, reason: {closure_reason}")

            return updated_case

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            self.logger.error(f"Failed to close case {case_id}: {e}")
            raise ServiceException(f"Case closure failed: {str(e)}") from e

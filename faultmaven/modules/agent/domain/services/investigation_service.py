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
from faultmaven.utils.serialization import to_json_compatible

from faultmaven.services.base import BaseService
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.models.case import Case, CaseStatus
from faultmaven.models.api_models import CaseQueryRequest, CaseQueryResponse
from faultmaven.exceptions import NotFoundException, PermissionDeniedException, ServiceException
from faultmaven.infrastructure.observability.tracing import trace


class InvestigationService(BaseService):
    """
    Service for managing investigation turns and milestone progress.

    Coordinates between:
    - MilestoneEngine (core investigation logic)
    - CaseRepository (persistence)
    - Access control (user permissions)
    """

    def __init__(
        self,
        milestone_engine: MilestoneEngine,
        case_repository: CaseRepository
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
        self,
        case_id: str,
        user_id: str,
        request: CaseQueryRequest
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
            NotFoundException: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If turn processing fails
        """
        try:
            # 1. Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundException(f"Case {case_id} not found")

            # 2. Check permissions (simple owner check)
            if case.user_id != user_id:
                self.logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # 3. Save user message to conversation history BEFORE processing
            from uuid import uuid4
            from datetime import datetime, timezone
            # Per case-storage-design.md Section 4.7, use "timestamp" not "created_at"
            user_message_obj = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": case.current_turn + 1,  # Next turn
                "role": "user",
                "message_type": "user_query",
                "content": request.message,
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": user_id,
                "token_count": None,
                "metadata": {
                    "has_attachments": bool(request.attachments),
                    "attachment_count": len(request.attachments) if request.attachments else 0
                }
            }
            case.messages.append(user_message_obj)
            case.message_count += 1

            # 4. Process turn via MilestoneEngine
            # Engine handles:
            # - Generating status-based prompt
            # - Invoking LLM
            # - Updating case state (milestones, evidence, hypotheses)
            # - Saving case via repository
            result = await self.engine.process_turn(
                case=case,
                user_message=request.message,
                attachments=request.attachments
            )

            # 5. Build response
            updated_case = result["case_updated"]
            agent_response_text = result["agent_response"]

            # 6. Save agent response to conversation history
            from uuid import uuid4
            from datetime import datetime, timezone
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
                "metadata": {}
            }

            updated_case.messages.append(agent_message)
            updated_case.message_count += 1

            # Save case with agent message
            await self.repository.save(updated_case)

            response = CaseQueryResponse(
                agent_response=agent_response_text,
                turn_number=updated_case.current_turn,
                milestones_completed=result.get("metadata", {}).get("milestones_completed", []),
                case_status=updated_case.status,
                progress_made=result.get("metadata", {}).get("progress_made", False),
                is_stuck=updated_case.is_stuck if hasattr(updated_case, 'is_stuck') else False
            )

            self.logger.info(
                f"Processed turn {response.turn_number} for case {case_id}, "
                f"status={response.case_status}, milestones={len(response.milestones_completed)}, "
                f"messages={updated_case.message_count}"
            )

            return response

        except (NotFoundException, PermissionDeniedException):
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
            - completion_percentage
            - current_turn, is_stuck, degraded_mode

        Raises:
            NotFoundException: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundException(f"Case {case_id} not found")

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
                "current_stage": case.current_stage.value if case.current_stage else None,
                "milestones_completed": case.progress.completed_milestones,
                "pending_milestones": case.progress.pending_milestones,
                "completion_percentage": case.progress.completion_percentage,
                "current_turn": case.current_turn,
                "is_stuck": case.is_stuck if hasattr(case, 'is_stuck') else False,
                "degraded_mode": (
                    case.degraded_mode.is_active
                    if hasattr(case, 'degraded_mode') and case.degraded_mode
                    else False
                )
            }

        except (NotFoundException, PermissionDeniedException):
            raise
        except Exception as e:
            self.logger.error(f"Failed to get progress for case {case_id}: {e}")
            raise ServiceException(f"Progress retrieval failed: {str(e)}") from e

    @trace("investigation_service_transition_to_investigating")
    async def transition_to_investigating(
        self,
        case_id: str,
        user_id: str,
        confirmed_description: str
    ) -> Case:
        """
        Transition case from CONSULTING to INVESTIGATING.

        Called when user confirms the problem statement during consulting phase.

        Args:
            case_id: Case identifier
            user_id: User making the request
            confirmed_description: Confirmed problem description

        Returns:
            Updated case

        Raises:
            NotFoundException: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If transition fails or invalid state
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundException(f"Case {case_id} not found")

            # Check permissions
            if case.user_id != user_id:
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Validate current status
            if case.status != CaseStatus.CONSULTING:
                raise ServiceException(
                    f"Cannot transition to INVESTIGATING: case is in {case.status} status"
                )

            # Ensure consulting data is properly set for INVESTIGATING transition
            if not case.consulting.proposed_problem_statement:
                # Use confirmed_description as the problem statement
                case.consulting.proposed_problem_statement = confirmed_description

            if not case.consulting.problem_statement_confirmed:
                case.consulting.problem_statement_confirmed = True
                case.consulting.problem_statement_confirmed_at = datetime.now(timezone.utc)

            if not case.consulting.decided_to_investigate:
                case.consulting.decided_to_investigate = True
                case.consulting.decision_made_at = datetime.now(timezone.utc)

            # Update case
            case.description = confirmed_description
            case.status = CaseStatus.INVESTIGATING

            # Save
            updated_case = await self.repository.save(case)

            self.logger.info(
                f"Transitioned case {case_id} to INVESTIGATING with description: "
                f"{confirmed_description[:100]}..."
            )

            return updated_case

        except (NotFoundException, PermissionDeniedException, ServiceException):
            raise
        except Exception as e:
            self.logger.error(f"Failed to transition case {case_id} to INVESTIGATING: {e}")
            raise ServiceException(f"Status transition failed: {str(e)}") from e

    @trace("investigation_service_close_case")
    async def close_case(
        self,
        case_id: str,
        user_id: str,
        closure_reason: str
    ) -> Case:
        """
        Close a case.

        Args:
            case_id: Case identifier
            user_id: User making the request
            closure_reason: Why the case is being closed
                (resolved | abandoned | escalated | consulting_only | duplicate | other)

        Returns:
            Updated case

        Raises:
            NotFoundException: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundException(f"Case {case_id} not found")

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
                    "closed_at": now
                },
                deep=True
            )

            # Save
            updated_case = await self.repository.save(updated_case_data)

            self.logger.info(f"Closed case {case_id}, reason: {closure_reason}")

            return updated_case

        except (NotFoundException, PermissionDeniedException):
            raise
        except Exception as e:
            self.logger.error(f"Failed to close case {case_id}: {e}")
            raise ServiceException(f"Case closure failed: {str(e)}") from e

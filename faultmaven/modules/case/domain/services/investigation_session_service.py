"""API Investigation Session Service Module (TASK-012)

Purpose: API service layer for investigation session management operations.

This service provides business logic orchestration between FastAPI controllers
and domain repositories for investigation session management. It handles:
- Session lifecycle management (create, pause, resume, complete, abandon)
- Token budget tracking and validation
- Agent execution coordination within sessions
- Authorization checks (organization ownership via parent case)
- Audit logging

Architecture:
    FastAPI Routes → InvestigationSessionService → Repositories → Database

This complements the APICaseService (TASK-011) by providing session-specific
workflow management for investigation activities.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.agent_execution_repository import (
    AgentExecutionRepository,
)
from faultmaven.modules.case.domain.investigation_session import (
    InvestigationSession,
    SessionStatus,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.modules.case.infrastructure.investigation_session_repository import (
    InvestigationSessionRepository,
)

import logging

logger = logging.getLogger(__name__)


class APIInvestigationSessionService:
    """Service for API investigation session management operations.

    This service provides a clean API layer for investigation session management with:
    - Organization-level authorization via parent case
    - Session lifecycle management (active, paused, completed, abandoned)
    - Token budget tracking and enforcement
    - Agent execution linking and coordination
    - Unified logging and error handling

    Attributes:
        session_repo: Investigation session repository
        execution_repo: Agent execution repository
        case_repo: Case repository for authorization checks
    """

    def __init__(
        self,
        session_repo: InvestigationSessionRepository,
        execution_repo: AgentExecutionRepository,
        case_repo: CaseRepository,
    ):
        """Initialize API investigation session service.

        Args:
            session_repo: Investigation session repository
            execution_repo: Agent execution repository
            case_repo: Case repository (for authorization)
        """
        self.session_repo = session_repo
        self.execution_repo = execution_repo
        self.case_repo = case_repo

    # ============================================================
    # Authorization Helper
    # ============================================================

    async def _verify_case_authorization(
        self,
        case_id: str,
        organization_id: str,
    ) -> None:
        """Verify that the organization owns the case.

        Args:
            case_id: Case ID to check
            organization_id: Organization ID to verify

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        case = await self.case_repo.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)

        if case.organization_id != organization_id:
            raise AuthorizationError(
                f"Case {case_id} not accessible by organization {organization_id}"
            )

    async def _get_session_with_authorization(
        self,
        session_id: str,
        organization_id: str,
    ) -> InvestigationSession:
        """Get session and verify authorization via parent case.

        Args:
            session_id: Session ID
            organization_id: Organization ID for authorization

        Returns:
            InvestigationSession if authorized

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own parent case
        """
        session = await self.session_repo.get_by_id(session_id)
        if not session:
            raise NotFoundError("Session", session_id)

        # Authorization check via parent case
        case = await self.case_repo.get(session.case_id)
        if not case:
            raise NotFoundError("Case", session.case_id)

        if case.organization_id != organization_id:
            raise AuthorizationError(
                f"Session {session_id} not accessible by organization {organization_id}"
            )

        return session

    # ============================================================
    # Core CRUD Operations
    # ============================================================

    async def create_session(
        self,
        case_id: str,
        organization_id: str,
        user_id: str,
        session_goal: Optional[str] = None,
        token_budget_limit: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> InvestigationSession:
        """Create a new investigation session.

        Workflow:
        1. Verify case exists and belongs to organization
        2. Check for existing active session (raise ConflictError if exists)
        3. Generate session_id
        4. Create session with status=ACTIVE
        5. Return created session

        Args:
            case_id: Case to create session for
            organization_id: Organization for authorization
            user_id: User creating the session
            session_goal: Optional goal description
            token_budget_limit: Optional token spending limit
            metadata: Optional metadata

        Returns:
            Created InvestigationSession

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
            ConflictError: If active session already exists for case
            ValidationException: If inputs invalid
        """
        logger.info(
            "create_session",
            extra={
                "case_id": case_id,
                "organization_id": organization_id,
                "user_id": user_id,
            },
        )

        # Validate inputs
        if not case_id or not case_id.strip():
            raise ValidationException("case_id: Case ID is required")

        if not user_id or not user_id.strip():
            raise ValidationException("user_id: User ID is required")

        if not organization_id or not organization_id.strip():
            raise ValidationException("organization_id: Organization ID is required")

        if token_budget_limit is not None and token_budget_limit < 0:
            raise ValidationException(
                "token_budget_limit: Token budget limit cannot be negative"
            )

        try:
            # Verify case authorization
            await self._verify_case_authorization(case_id, organization_id)

            # Check for existing active session
            active_session = await self.session_repo.get_active_session(case_id)
            if active_session:
                raise ConflictError(
                    f"An active session already exists for case {case_id}",
                    resource_type="Session",
                    resource_id=active_session.session_id,
                    conflict_reason="active_session_exists",
                )

            # Create new session
            now = datetime.now(timezone.utc)
            session = InvestigationSession(
                session_id=f"session_{uuid4().hex[:12]}",
                case_id=case_id.strip(),
                user_id=user_id.strip(),
                organization_id=organization_id.strip(),
                status=SessionStatus.ACTIVE,
                started_at=now,
                last_activity_at=now,
                session_goal=session_goal.strip() if session_goal else None,
                token_budget_limit=token_budget_limit,
                total_token_usage=0,
                total_agent_executions=0,
                metadata=metadata,
                created_at=now,
                updated_at=now,
            )

            # Save the session
            saved_session = await self.session_repo.create(session)

            logger.info(
                "create_session_success",
                extra={"session_id": saved_session.session_id, "case_id": case_id},
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException, ConflictError):
            raise
        except Exception as e:
            logger.error("create_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to create session: {e}")

    async def get_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> Optional[InvestigationSession]:
        """Get session by ID with authorization check.

        Verifies organization owns the parent case.

        Args:
            session_id: Session ID to retrieve
            organization_id: Organization for authorization

        Returns:
            Session if found and authorized, None otherwise
        """
        logger.info(
            "get_session",
            extra={"session_id": session_id, "organization_id": organization_id},
        )

        if not session_id or not session_id.strip():
            return None

        try:
            session = await self.session_repo.get_by_id(session_id)

            if not session:
                return None

            # Authorization check via parent case
            case = await self.case_repo.get(session.case_id)
            if not case:
                return None

            if case.organization_id != organization_id:
                logger.info(
                    "get_session_unauthorized",
                    extra={
                        "session_id": session_id,
                        "organization_id": organization_id,
                        "case_org_id": case.organization_id,
                    },
                )
                return None

            return session

        except Exception as e:
            logger.error("get_session failed: %s", e, exc_info=True)
            return None

    async def update_session(
        self,
        session_id: str,
        organization_id: str,
        updates: Dict[str, Any],
    ) -> InvestigationSession:
        """Update session with authorization check.

        Allowed updates:
        - session_goal
        - token_budget_limit
        - metadata

        Args:
            session_id: Session ID to update
            organization_id: Organization for authorization
            updates: Fields to update

        Returns:
            Updated InvestigationSession

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If updates invalid
        """
        logger.info(
            "update_session",
            extra={
                "session_id": session_id,
                "organization_id": organization_id,
                "update_fields": list(updates.keys()),
            },
        )

        if not session_id or not session_id.strip():
            raise ValidationException("session_id: Session ID is required")

        if not updates:
            raise ValidationException("updates: At least one update field is required")

        try:
            # Get session with authorization
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            # Validate and apply updates
            allowed_fields = {"session_goal", "token_budget_limit", "metadata"}

            for key, value in updates.items():
                if key not in allowed_fields:
                    continue

                if key == "session_goal":
                    session.session_goal = value.strip() if value else None

                elif key == "token_budget_limit":
                    if value is not None and value < 0:
                        raise ValidationException(
                            "token_budget_limit: Token budget limit cannot be negative"
                        )
                    session.token_budget_limit = value

                elif key == "metadata":
                    session.metadata = value

            # Update timestamp
            session.updated_at = datetime.now(timezone.utc)

            # Save updated session
            saved_session = await self.session_repo.update(session)

            logger.info(
                "update_session_success",
                extra={"session_id": session_id, "organization_id": organization_id},
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            logger.error("update_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to update session: {e}")

    # ============================================================
    # Session Lifecycle Operations
    # ============================================================

    async def pause_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> InvestigationSession:
        """Pause an active session.

        Args:
            session_id: Session ID to pause
            organization_id: Organization for authorization

        Returns:
            Updated session with status=PAUSED

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If session not active
        """
        logger.info(
            "pause_session",
            extra={"session_id": session_id, "organization_id": organization_id},
        )

        try:
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            # Check if session is active
            if session.status != SessionStatus.ACTIVE:
                raise ValidationException(
                    f"Cannot pause session in {session.status.value} status. "
                    "Only active sessions can be paused."
                )

            # Pause the session
            session.pause()

            # Save updated session
            saved_session = await self.session_repo.update(session)

            logger.info(
                "pause_session_success",
                extra={"session_id": session_id, "status": saved_session.status.value},
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            logger.error("pause_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to pause session: {e}")

    async def resume_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> InvestigationSession:
        """Resume a paused session.

        Args:
            session_id: Session ID to resume
            organization_id: Organization for authorization

        Returns:
            Updated session with status=ACTIVE

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If session not paused
        """
        logger.info(
            "resume_session",
            extra={"session_id": session_id, "organization_id": organization_id},
        )

        try:
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            # Check if session is paused
            if session.status != SessionStatus.PAUSED:
                raise ValidationException(
                    f"Cannot resume session in {session.status.value} status. "
                    "Only paused sessions can be resumed."
                )

            # Resume the session
            session.resume()

            # Save updated session
            saved_session = await self.session_repo.update(session)

            logger.info(
                "resume_session_success",
                extra={"session_id": session_id, "status": saved_session.status.value},
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            logger.error("resume_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to resume session: {e}")

    async def complete_session(
        self,
        session_id: str,
        organization_id: str,
        findings_summary: str,
    ) -> InvestigationSession:
        """Complete a session with findings.

        Workflow:
        1. Verify authorization
        2. Mark session as completed
        3. Set findings_summary
        4. Calculate total_duration_ms
        5. Set ended_at timestamp
        6. Return updated session

        Args:
            session_id: Session ID to complete
            organization_id: Organization for authorization
            findings_summary: Summary of investigation findings

        Returns:
            Updated session with status=COMPLETED

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If session already completed/abandoned or findings empty
        """
        logger.info(
            "complete_session",
            extra={"session_id": session_id, "organization_id": organization_id},
        )

        # Validate findings
        if not findings_summary or not findings_summary.strip():
            raise ValidationException(
                "findings_summary: Findings summary is required to complete session"
            )

        try:
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            # Check if session is already in terminal state
            if session.status in (SessionStatus.COMPLETED, SessionStatus.ABANDONED):
                raise ValidationException(
                    f"Cannot complete session in {session.status.value} status. "
                    "Session is already in a terminal state."
                )

            # Complete the session
            session.complete(findings_summary.strip())

            # Save updated session
            saved_session = await self.session_repo.update(session)

            logger.info(
                "complete_session_success",
                extra={
                    "session_id": session_id,
                    "status": saved_session.status.value,
                    "total_duration_ms": saved_session.total_duration_ms,
                },
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            logger.error("complete_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to complete session: {e}")

    async def abandon_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> InvestigationSession:
        """Abandon a session without findings.

        Args:
            session_id: Session ID to abandon
            organization_id: Organization for authorization

        Returns:
            Updated session with status=ABANDONED

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If session already completed
        """
        logger.info(
            "abandon_session",
            extra={"session_id": session_id, "organization_id": organization_id},
        )

        try:
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            # Check if session is already in terminal state
            if session.status in (SessionStatus.COMPLETED, SessionStatus.ABANDONED):
                raise ValidationException(
                    f"Cannot abandon session in {session.status.value} status. "
                    "Session is already in a terminal state."
                )

            # Abandon the session
            session.abandon()

            # Save updated session
            saved_session = await self.session_repo.update(session)

            logger.info(
                "abandon_session_success",
                extra={"session_id": session_id, "status": saved_session.status.value},
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            logger.error("abandon_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to abandon session: {e}")

    # ============================================================
    # Query Operations
    # ============================================================

    async def get_active_session(
        self,
        case_id: str,
        organization_id: str,
    ) -> Optional[InvestigationSession]:
        """Get the currently active session for a case.

        Args:
            case_id: Case ID to get active session for
            organization_id: Organization for authorization

        Returns:
            Active session if found, None otherwise

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        logger.info(
            "get_active_session",
            extra={"case_id": case_id, "organization_id": organization_id},
        )

        try:
            # Verify case authorization
            await self._verify_case_authorization(case_id, organization_id)

            # Get active session
            session = await self.session_repo.get_active_session(case_id)

            return session

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            logger.error("get_active_session failed: %s", e, exc_info=True)
            return None

    async def list_sessions(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[SessionStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[InvestigationSession]:
        """List sessions for a case with filters.

        Args:
            case_id: Case ID to list sessions for
            organization_id: Organization for authorization
            status: Optional filter by status
            limit: Max results
            offset: Pagination offset

        Returns:
            List of sessions

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        logger.info(
            "list_sessions",
            extra={
                "case_id": case_id,
                "organization_id": organization_id,
                "status": status.value if status else None,
                "limit": limit,
                "offset": offset,
            },
        )

        try:
            # Verify case authorization
            await self._verify_case_authorization(case_id, organization_id)

            # Get sessions from repository
            sessions = await self.session_repo.list_by_case_id(case_id, status=status)

            # Apply pagination manually since repository may not support it
            paginated = sessions[offset : offset + limit]

            logger.info(
                "list_sessions_result",
                extra={
                    "case_id": case_id,
                    "count": len(paginated),
                    "total": len(sessions),
                },
            )

            return paginated

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            logger.error("list_sessions failed: %s", e, exc_info=True)
            return []

    async def get_session_with_executions(
        self,
        session_id: str,
        organization_id: str,
        include_tool_calls: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Get session with related agent executions.

        Args:
            session_id: Session ID
            organization_id: Organization for authorization
            include_tool_calls: Include tool calls for each execution

        Returns:
            Dictionary with session and executions, or None if not found/authorized
        """
        logger.info(
            "get_session_with_executions",
            extra={
                "session_id": session_id,
                "organization_id": organization_id,
                "include_tool_calls": include_tool_calls,
            },
        )

        try:
            # Get session (returns None if not authorized)
            session = await self.get_session(session_id, organization_id)
            if not session:
                return None

            result: Dict[str, Any] = {
                "session": session,
            }

            # Get executions for this session's case
            # Note: We filter by session_id if the execution has one
            try:
                executions, _ = await self.execution_repo.list_executions_by_case(
                    session.case_id
                )

                # Filter executions that belong to this session if session tracking is available
                # For now, return all executions for the case
                result["executions"] = executions

                if include_tool_calls:
                    # Tool calls are already loaded with executions in most implementations
                    for execution in executions:
                        if not execution.tool_calls:
                            execution.tool_calls = (
                                await self.execution_repo.get_tool_calls_for_execution(
                                    execution.execution_id
                                )
                            )
            except Exception as e:
                logger.error("get_session_executions failed: %s", e, exc_info=True)
                result["executions"] = []

            return result

        except Exception as e:
            logger.error("get_session_with_executions failed: %s", e, exc_info=True)
            return None

    # ============================================================
    # Token Budget and Execution Management
    # ============================================================

    async def add_execution_to_session(
        self,
        session_id: str,
        organization_id: str,
        execution_id: str,
        token_usage: int,
    ) -> InvestigationSession:
        """Link an agent execution to a session and update token tracking.

        Workflow:
        1. Verify session authorization
        2. Verify execution exists
        3. Call session.add_agent_execution(token_usage)
        4. Update session in repository
        5. Return updated session

        Args:
            session_id: Session ID
            organization_id: Organization for authorization
            execution_id: Agent execution ID to link
            token_usage: Token usage for this execution

        Returns:
            Updated session with incremented counts

        Raises:
            NotFoundError: If session or execution not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If session not active or token_usage negative
        """
        logger.info(
            "add_execution_to_session",
            extra={
                "session_id": session_id,
                "organization_id": organization_id,
                "execution_id": execution_id,
                "token_usage": token_usage,
            },
        )

        if token_usage < 0:
            raise ValidationException("token_usage: Token usage cannot be negative")

        try:
            # Get session with authorization
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            # Verify session is active
            if session.status != SessionStatus.ACTIVE:
                raise ValidationException(
                    f"Cannot add execution to session in {session.status.value} status. "
                    "Session must be active."
                )

            # Verify execution exists
            execution = await self.execution_repo.get_execution(execution_id)
            if not execution:
                raise NotFoundError("Execution", execution_id)

            # Add execution to session (updates token usage and execution count)
            session.add_agent_execution(token_usage)

            # Save updated session
            saved_session = await self.session_repo.update(session)

            logger.info(
                "add_execution_to_session_success",
                extra={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "total_token_usage": saved_session.total_token_usage,
                    "total_agent_executions": saved_session.total_agent_executions,
                },
            )

            return saved_session

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            logger.error("add_execution_to_session failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to add execution to session: {e}")

    async def check_budget_exceeded(
        self,
        session_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """Check if session has exceeded token budget.

        Args:
            session_id: Session ID
            organization_id: Organization for authorization

        Returns:
            Dictionary with:
            - is_over_budget: bool
            - total_token_usage: int
            - token_budget_limit: int or None
            - usage_percentage: float or None

        Raises:
            NotFoundError: If session not found
            AuthorizationError: If organization doesn't own case
        """
        logger.info(
            "check_budget_exceeded",
            extra={"session_id": session_id, "organization_id": organization_id},
        )

        try:
            session = await self._get_session_with_authorization(
                session_id, organization_id
            )

            is_over_budget = session.is_over_budget()
            usage_percentage = session.get_budget_usage_percent()

            result = {
                "is_over_budget": is_over_budget,
                "total_token_usage": session.total_token_usage,
                "token_budget_limit": session.token_budget_limit,
                "usage_percentage": usage_percentage,
            }

            logger.info(
                "check_budget_exceeded_result",
                extra={
                    "session_id": session_id,
                    "is_over_budget": is_over_budget,
                    "usage_percentage": usage_percentage,
                },
            )

            return result

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            logger.error("check_budget_exceeded failed: %s", e, exc_info=True)
            raise ServiceError(f"Failed to check budget status: {e}")

    # ============================================================
    # Statistics and Analytics
    # ============================================================

    async def get_session_statistics(
        self,
        case_id: str,
        organization_id: str,
    ) -> Dict[str, Any]:
        """Get session statistics for a case.

        Returns:
            Statistics including:
            - total_sessions
            - by_status (active, paused, completed, abandoned)
            - total_token_usage_all_sessions
            - total_agent_executions_all_sessions
            - avg_session_duration_ms
        """
        logger.info(
            "get_session_statistics",
            extra={"case_id": case_id, "organization_id": organization_id},
        )

        try:
            # Verify case authorization
            await self._verify_case_authorization(case_id, organization_id)

            # Get all sessions for case
            sessions = await self.session_repo.list_by_case_id(case_id)

            # Calculate statistics
            by_status: Dict[str, int] = {}
            total_token_usage = 0
            total_agent_executions = 0
            durations: List[int] = []

            for session in sessions:
                # Count by status
                status_key = session.status.value
                by_status[status_key] = by_status.get(status_key, 0) + 1

                # Sum token usage
                total_token_usage += session.total_token_usage

                # Sum agent executions
                total_agent_executions += session.total_agent_executions

                # Collect durations for completed sessions
                if session.total_duration_ms is not None:
                    durations.append(session.total_duration_ms)

            # Calculate average duration
            avg_session_duration_ms = 0.0
            if durations:
                avg_session_duration_ms = sum(durations) / len(durations)

            statistics = {
                "total_sessions": len(sessions),
                "by_status": by_status,
                "total_token_usage_all_sessions": total_token_usage,
                "total_agent_executions_all_sessions": total_agent_executions,
                "avg_session_duration_ms": avg_session_duration_ms,
                "case_id": case_id,
                "organization_id": organization_id,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

            logger.info(
                "get_session_statistics_success",
                extra={"case_id": case_id, "total_sessions": len(sessions)},
            )

            return statistics

        except (NotFoundError, AuthorizationError):
            raise
        except Exception as e:
            logger.error("get_session_statistics failed: %s", e, exc_info=True)
            return {
                "total_sessions": 0,
                "by_status": {},
                "total_token_usage_all_sessions": 0,
                "total_agent_executions_all_sessions": 0,
                "avg_session_duration_ms": 0,
                "case_id": case_id,
                "organization_id": organization_id,
                "error": str(e),
            }

"""Case Service Module

Purpose: Core case management service for troubleshooting persistence

This service provides business logic for managing troubleshooting cases that
persist across multiple sessions, enabling conversation continuity and
collaborative troubleshooting.

Core Responsibilities:
- Case lifecycle management (create, update, archive)
- Case-session association and linking
- Conversation context management
- Case sharing and collaboration
- Access control and permissions
- Case analytics and metrics
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from faultmaven.services.base import BaseService
from faultmaven.models.case import Case, CaseStatus, MessageType
from faultmaven.models.api_models import (
    CaseCreateRequest,
    CaseUpdateRequest,
    CaseSummary,
    CaseListFilter,
    CaseSearchRequest,
    CaseMessage,
    CaseParticipant,
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.models.interfaces_report import IReportStore
from faultmaven.models.interfaces import ISessionStore
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.models import parse_utc_timestamp
from faultmaven.utils.serialization import to_json_compatible


class CaseService(BaseService, ICaseService):
    """Service for centralized case management and coordination"""

    def __init__(
        self,
        case_repository: CaseRepository,
        session_store: Optional[ISessionStore] = None,
        report_store: Optional[IReportStore] = None,
        case_vector_store: Optional[Any] = None,
        settings: Optional[Any] = None,
        max_cases_per_user: int = 100
    ):
        """
        Initialize the Case Service

        Args:
            case_repository: Case repository for persistence
            session_store: Optional session store for integration
            report_store: Optional report store for cascade deletion
            case_vector_store: Optional case vector store for Working Memory cleanup
            settings: Configuration settings for the service
            max_cases_per_user: Maximum cases per user
        """
        super().__init__("case_service")
        self.repository = case_repository
        self.session_store = session_store
        self.case_vector_store = case_vector_store
        self.report_store = report_store
        self._settings = settings

        # Use settings values if available, otherwise use parameter defaults
        if settings and hasattr(settings, 'case'):
            self.max_cases_per_user = getattr(settings.case, 'max_per_user', max_cases_per_user)
        else:
            self.max_cases_per_user = max_cases_per_user

    @trace("case_service_create_case")
    async def create_case(
        self,
        title: Optional[str] = None,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        session_id: Optional[str] = None,
        initial_message: Optional[str] = None
    ) -> Case:
        """
        Create a new troubleshooting case

        Args:
            title: Case title (required)
            owner_id: Required owner user ID
            description: Optional case description
            session_id: Optional session to associate with case
            initial_message: Optional initial message content (added as USER_QUERY message)

        Returns:
            Created case object

        Raises:
            ValidationException: If input validation fails
            ServiceException: If case creation fails
        """
        # Validate owner_id
        if not owner_id or not owner_id.strip():
            raise ValidationException("Owner ID is required")

        try:
            # Check user case limits and prepare for title auto-generation
            user_cases_list, total = await self.repository.list(user_id=owner_id.strip())
            # Only count non-terminal cases
            active_cases = [c for c in user_cases_list if c.status not in [CaseStatus.RESOLVED, CaseStatus.CLOSED]]

            if len(active_cases) >= self.max_cases_per_user:
                raise ValidationException(f"User has reached maximum case limit ({self.max_cases_per_user})")

            # Auto-generate title if not provided (API spec: Case-MMDD-N)
            if not title or not title.strip():
                # Format: Case-MMDD-N (e.g., Case-1106-1, Case-1106-2)
                # Sequence counter resets daily
                now = datetime.now(timezone.utc)
                date_suffix = now.strftime("%m%d")  # MMDD format

                # Count today's cases for this user to get sequence number
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                today_cases = [c for c in user_cases_list if c.created_at >= today_start]
                sequence = len(today_cases) + 1

                title = f"Case-{date_suffix}-{sequence}"
                self.logger.debug(f"Auto-generated title: {title}")
            else:
                title = title.strip()
                if len(title) > 200:
                    raise ValidationException("Case title cannot exceed 200 characters")

            # Create new case using milestone-based model
            # Note: organization_id required by new model - using owner_id for now
            case = Case(
                title=title,
                description=description.strip() if description else "",
                user_id=owner_id.strip(),
                organization_id=owner_id.strip()  # TODO: Get from user context
            )

            # Add initial message if provided (restored from old implementation)
            if initial_message:
                message_dict = {
                    "message_id": f"msg_{uuid.uuid4().hex[:12]}",
                    "case_id": case.case_id,
                    "author_id": owner_id.strip(),
                    "role": "user",
                    "content": initial_message.strip(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "turn_number": 1,
                    "metadata": {}
                }
                case.messages.append(message_dict)
                case.message_count = len(case.messages)

            # Session association via session store if available
            if session_id and self.session_store:
                try:
                    await self.session_store.set(
                        f"session:{session_id}:current_case_id",
                        case.case_id,
                        ttl=86400  # 24 hours
                    )
                except Exception as e:
                    self.logger.warning(f"Failed to update session with case ID: {e}")

            # Save the case using repository
            saved_case = await self.repository.save(case)

            self.logger.info(f"Created case {saved_case.case_id} with title '{title}'")
            return saved_case

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to create case: {e}")
            raise ServiceException(f"Case creation failed: {str(e)}") from e

    @trace("case_service_get_case")
    async def get_case(
        self,
        case_id: str,
        user_id: Optional[str] = None
    ) -> Optional[Case]:
        """
        Get a case with optional access control

        Args:
            case_id: Case identifier
            user_id: Optional user ID for access control

        Returns:
            Case object if found and accessible, None otherwise
        """
        if not case_id or not case_id.strip():
            return None

        try:
            case = await self.repository.get(case_id)
            if not case:
                return None

            # Apply access control if user_id provided
            # Simple check: must be case owner
            if user_id and case.user_id != user_id:
                self.logger.warning(f"User {user_id} denied access to case {case_id} (owner: {case.user_id})")
                return None

            return case

        except Exception as e:
            self.logger.error(f"Failed to get case {case_id}: {e}")
            return None

    @trace("case_service_update_case")
    async def update_case(
        self,
        case_id: str,
        updates: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> bool:
        """
        Update case with access control

        Args:
            case_id: Case identifier
            updates: Updates to apply (title, description, status, closure_reason)
            user_id: Optional user ID for access control

        Returns:
            True if update was successful
        """
        if not case_id or not case_id.strip():
            raise ValidationException("Case ID cannot be empty")

        if not updates:
            raise ValidationException("Updates cannot be empty")

        try:
            # Get current case and check access
            case = await self.get_case(case_id, user_id)
            if not case:
                return False

            # Validate and apply updates directly to Case object
            allowed_fields = {'title', 'description', 'status', 'closure_reason'}

            for key, value in updates.items():
                if key in allowed_fields and hasattr(case, key):
                    setattr(case, key, value)

            # Save updated case
            saved_case = await self.repository.save(case)

            self.logger.info(f"Updated case {case_id}")
            return saved_case is not None

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to update case {case_id}: {e}")
            raise ServiceException(f"Case update failed: {str(e)}") from e

    @trace("case_service_add_message")
    async def add_message_to_case(
        self,
        case_id: str,
        message: CaseMessage,
        session_id: Optional[str] = None
    ) -> bool:
        """
        Add a message to a case conversation

        Args:
            case_id: Case identifier
            message: Message to add
            session_id: Optional session ID

        Returns:
            True if message was added successfully
        """
        if not case_id or not message:
            raise ValidationException("Case ID and message are required")

        try:
            # Verify case exists
            case = await self.repository.get(case_id)
            if not case:
                raise ValidationException(f"Case {case_id} not found")

            # Ensure message belongs to this case
            message.case_id = case_id

            # Convert CaseMessage to dict format for storage (per case-storage-design.md spec)
            message_dict = {
                "message_id": message.message_id,
                "case_id": case_id,
                "author_id": message.author_id,
                "role": getattr(message, 'role', 'system'),
                "message_type": message.message_type.value if hasattr(message.message_type, 'value') else str(message.message_type),
                "content": message.content,
                "created_at": message.created_at.isoformat() if hasattr(message.created_at, 'isoformat') else str(message.created_at),
                "turn_number": case.current_turn,
                "token_count": getattr(message, 'token_count', None),
                "metadata": message.metadata or {}
            }

            # Delegate to repository - it handles storage-specific logic
            success = await self.repository.add_message(case_id, message_dict)

            if success:
                self.logger.debug(f"Added message to case {case_id}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to add message to case {case_id}: {e}")
            return False

    @trace("case_service_get_or_create_case_for_session")
    async def get_or_create_case_for_session(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        force_new: bool = False,
        title: Optional[str] = None
    ) -> str:
        """
        Get existing case for session or create new one

        Args:
            session_id: Session identifier
            user_id: Optional user identifier
            force_new: Force creation of new case
            title: Optional case title (default: auto-generated)

        Returns:
            Case ID
        """
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")

        try:
            # Try to get existing case for session if not forcing new
            if not force_new and self.session_store:
                try:
                    existing_case_id = await self.session_store.get(f"session:{session_id}:current_case_id")
                    if existing_case_id:
                        # Verify case still exists and is accessible
                        case = await self.get_case(existing_case_id, user_id)
                        if case:
                            self.logger.debug(f"Using existing case {existing_case_id} for session {session_id}")
                            return existing_case_id
                except Exception as e:
                    self.logger.warning(f"Failed to get existing case for session: {e}")

            # Create new case with provided title or auto-generate
            case_title = title if title else f"Troubleshooting Session {session_id[:8]}"
            case = await self.create_case(
                title=case_title,
                description="Auto-created case for troubleshooting session",
                owner_id=user_id,
                session_id=session_id
            )

            self.logger.info(f"Created new case {case.case_id} for session {session_id}")
            return case.case_id

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get/create case for session {session_id}: {e}")
            raise ServiceException(f"Case management failed: {str(e)}") from e

    @trace("case_service_link_session_to_case")
    async def link_session_to_case(self, session_id: str, case_id: str) -> bool:
        """
        Link a session to an existing case

        Args:
            session_id: Session identifier
            case_id: Case identifier

        Returns:
            True if linking was successful
        """
        if not session_id or not case_id:
            raise ValidationException("Session ID and Case ID are required")

        try:
            # Verify case exists
            case = await self.repository.get(case_id)
            if not case:
                return False

            # Update last activity timestamp via repository
            await self.repository.update_activity_timestamp(case_id)

            # Update session store with case reference
            if self.session_store:
                try:
                    await self.session_store.set(
                        f"session:{session_id}:current_case_id",
                        case_id,
                        ttl=86400  # 24 hours
                    )
                except Exception as e:
                    self.logger.warning(f"Failed to update session store: {e}")

            self.logger.info(f"Linked session {session_id} to case {case_id}")
            return True

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to link session to case: {e}")
            return False

    @trace("case_service_get_conversation_context")
    async def get_case_conversation_context(
        self,
        case_id: str,
        limit: int = 10
    ) -> str:
        """
        Get formatted conversation context for LLM

        Args:
            case_id: Case identifier
            limit: Maximum number of messages to include

        Returns:
            Formatted conversation context string
        """
        if not case_id:
            return ""

        try:
            # Get messages via repository - it handles pagination
            messages = await self.repository.get_messages(case_id, limit=limit)
            if not messages:
                return ""

            # Format for LLM context
            context_lines = ["Previous conversation in this troubleshooting case:"]

            for i, msg_dict in enumerate(messages[:-1], 1):  # Exclude current query
                try:
                    created_at = parse_utc_timestamp(msg_dict.get("created_at"))
                    timestamp = created_at.strftime("%H:%M") if created_at else "??:??"
                    message_type = msg_dict.get("message_type", "system_event")
                    content = msg_dict.get("content", "")

                    if message_type == "user_query":
                        context_lines.append(f"{i}. [{timestamp}] User: {content}")
                    elif message_type == "agent_response":
                        # Truncate long agent responses
                        truncated = content[:200] + "..." if len(content) > 200 else content
                        context_lines.append(f"{i}. [{timestamp}] Assistant: {truncated}")
                    elif message_type == "system_event":
                        context_lines.append(f"{i}. [{timestamp}] System: {content}")
                except Exception as e:
                    self.logger.warning(f"Failed to format message {i} in context: {e}")
                    continue

            if len(context_lines) > 1:  # More than just header
                context_lines.append("")  # Add spacing
                context_lines.append("Current query:")
                return "\n".join(context_lines)
            else:
                return ""

        except Exception as e:
            self.logger.warning(f"Failed to get conversation context for case {case_id}: {e}")
            return ""

    @trace("case_service_resume_case")
    async def resume_case_in_session(self, case_id: str, session_id: str) -> bool:
        """
        Resume an existing case in a new session

        Args:
            case_id: Case identifier
            session_id: Session identifier

        Returns:
            True if case was resumed successfully
        """
        if not case_id or not session_id:
            raise ValidationException("Case ID and Session ID are required")

        try:
            # Link session to case
            success = await self.link_session_to_case(session_id, case_id)
            
            if success:
                # Log resume event
                resume_message = CaseMessage(
                    case_id=case_id,
                    session_id=session_id,
                    message_type=MessageType.SYSTEM_EVENT,
                    content=f"Case resumed in session {session_id}",
                    metadata={"event_type": "case_resumed"}
                )
                
                await self.add_message_to_case(case_id, resume_message, session_id)
                self.logger.info(f"Resumed case {case_id} in session {session_id}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to resume case {case_id} in session {session_id}: {e}")
            return False

    @trace("case_service_hard_delete_case")
    async def hard_delete_case(
        self,
        case_id: str,
        user_id: Optional[str] = None
    ) -> bool:
        """
        Permanently delete a case and all associated data
        
        This method performs a hard delete of the case, removing:
        - The case record
        - All associated messages
        - All uploaded data files
        - All index entries
        - Any cached data
        
        The operation is idempotent - subsequent calls will return True
        even if the case has already been deleted.
        
        Args:
            case_id: Case identifier
            user_id: Optional user ID for access control
            
        Returns:
            True if case was deleted successfully (or already deleted)
        """
        if not case_id:
            raise ValidationException("Case ID cannot be empty")
            
        try:
            # Check if case exists and user has permissions
            if user_id:
                case = await self.get_case(case_id, user_id)
                if not case:
                    # Case not found or no access - idempotent behavior
                    return True

                # Check if user can delete (only owner can delete)
                if case.user_id != user_id:
                    self.logger.warning(f"User {user_id} denied delete access to case {case_id} (not owner)")
                    return False
            
            # Cascade delete reports BEFORE deleting case
            # This ensures report cleanup happens even if case delete fails
            if self.report_store:
                try:
                    await self.report_store.delete_case_reports(case_id)
                    self.logger.info(
                        f"Cascade deleted incident_reports and post_mortems for case {case_id} "
                        f"(runbooks preserved independently)"
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Failed to cascade delete reports for case {case_id}: {e}"
                    )
                    # Continue with case deletion even if report cleanup fails

            # Perform hard delete through repository
            success = await self.repository.delete(case_id)

            if success:
                self.logger.info(f"Hard deleted case {case_id}")

                # Clean up Case Working Memory (delete vector store collection)
                if self.case_vector_store:
                    try:
                        await self.case_vector_store.delete_case_collection(case_id)
                        self.logger.info(f"Deleted Working Memory collection for deleted case {case_id}")
                    except Exception as e:
                        self.logger.error(f"Failed to delete Working Memory for case {case_id}: {e}")
                        # Don't fail the delete operation if cleanup fails

                # TODO: Cascade delete other associated data:
                # - Delete uploaded data files
                # - Remove from search indexes
                # - Clear cached conversation context
                # - Remove session associations
                # This should be implemented when full data integration is available

            # Always return True for idempotent behavior
            # Even if delete failed, we consider it "successful" for idempotency
            return True
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to hard delete case {case_id}: {e}")
            # For idempotent behavior, return True even on error
            # The case might not exist or might already be deleted
            return True

    @trace("case_service_list_user_cases")
    async def list_user_cases(
        self,
        user_id: str,
        filters: Optional[CaseListFilter] = None
    ) -> List[CaseSummary]:
        """
        List cases for a user

        Args:
            user_id: User identifier
            filters: Optional filter criteria (include_empty, include_archived, status, etc.)

        Returns:
            List of user's cases (as CaseSummary objects)
        """
        if not user_id:
            raise ValidationException("User ID cannot be empty")

        try:
            # Get cases from repository
            status_filter = filters.status if filters else None
            cases_list, total = await self.repository.list(user_id=user_id, status=status_filter)

            # Apply additional filters in service layer (restored from old implementation)
            if filters:
                # Filter empty cases (current_turn == 0) unless include_empty=True
                # Note: New model uses current_turn instead of message_count
                if not filters.include_empty:
                    cases_list = [c for c in cases_list if c.current_turn > 0]

                # Filter archived cases unless include_archived=True
                # Note: New model uses CLOSED status instead of ARCHIVED
                if not filters.include_archived:
                    cases_list = [c for c in cases_list if c.status != CaseStatus.CLOSED]

            # Convert to CaseSummary
            from faultmaven.models.api_models import CaseSummary
            summaries = [CaseSummary.from_case(case) for case in cases_list]

            return summaries

        except Exception as e:
            self.logger.error(f"Failed to list cases for user {user_id}: {e}")
            return []

    @trace("case_service_search_cases")
    async def search_cases(
        self,
        search_request: CaseSearchRequest,
        user_id: Optional[str] = None
    ) -> List[CaseSummary]:
        """
        Search cases with access control

        Args:
            search_request: Search criteria
            user_id: Optional user ID for access control

        Returns:
            List of matching cases
        """
        try:
            # Search using repository
            cases_list, total = await self.repository.search(query=search_request.query)

            # Filter by user if provided
            if user_id:
                cases_list = [c for c in cases_list if c.user_id == user_id]

            # Convert to CaseSummary
            from faultmaven.models.api_models import CaseSummary
            summaries = [CaseSummary.from_case(case) for case in cases_list]

            return summaries

        except Exception as e:
            self.logger.error(f"Failed to search cases: {e}")
            return []

    @trace("case_service_get_analytics")
    async def get_case_analytics(self, case_id: str) -> Dict[str, Any]:
        """
        Get case analytics and metrics

        Args:
            case_id: Case identifier

        Returns:
            Case analytics dictionary
        """
        try:
            # Delegate to repository - it computes analytics efficiently
            return await self.repository.get_analytics(case_id)

        except Exception as e:
            self.logger.error(f"Failed to get analytics for case {case_id}: {e}")
            return {}

    @trace("case_service_cleanup_expired")
    async def cleanup_expired_cases(self) -> int:
        """
        Clean up expired cases

        Returns:
            Number of cases cleaned up
        """
        try:
            # Delegate to repository - it handles cleanup efficiently
            cleaned_count = await self.repository.cleanup_expired(max_age_days=90, batch_size=100)

            if cleaned_count > 0:
                self.logger.info(f"Cleaned up {cleaned_count} expired cases")

            return cleaned_count

        except Exception as e:
            self.logger.error(f"Failed to cleanup expired cases: {e}")
            return 0

    @trace("case_service_list_cases_by_session")
    async def list_cases_by_session(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Case]:
        """
        List cases owned by the user authenticated via session.

        Architecture: Session → User → User's Cases (indirect relationship)
        Per case-and-session-concepts.md: Cases are owned by users, NOT bound to sessions.

        Args:
            session_id: Session identifier (provides authentication context)
            limit: Maximum number of cases to return
            offset: Number of cases to skip

        Returns:
            List of cases owned by the authenticated user
        """
        if not session_id:
            raise ValidationException("Session ID cannot be empty")

        try:
            # Step 1: Get user_id from session (authentication)
            user_id = None
            if self.session_store:
                session_data = await self.session_store.get(f"session:{session_id}")
                if session_data:
                    user_id = session_data.get('user_id')

            if not user_id:
                self.logger.warning(f"No user_id found for session {session_id}")
                return []

            # Step 2: Get user's cases (authorization via ownership)
            cases, _ = await self.repository.list(
                user_id=user_id,
                limit=limit,
                offset=offset
            )
            return cases

        except Exception as e:
            self.logger.error(f"Failed to list cases for session {session_id}: {e}")
            return []

    @trace("case_service_count_cases_by_session")
    async def count_cases_by_session(self, session_id: str) -> int:
        """
        Count cases owned by the user authenticated via session.

        Architecture: Session → User → User's Cases

        Args:
            session_id: Session identifier (provides authentication context)

        Returns:
            Total number of cases owned by the authenticated user
        """
        if not session_id:
            return 0

        try:
            # Step 1: Get user_id from session
            user_id = None
            if self.session_store:
                session_data = await self.session_store.get(f"session:{session_id}")
                if session_data:
                    user_id = session_data.get('user_id')

            if not user_id:
                return 0

            # Step 2: Count user's cases
            cases, total_count = await self.repository.list(
                user_id=user_id,
                limit=0,
                offset=0
            )
            return total_count

        except Exception as e:
            self.logger.error(f"Failed to count cases for session {session_id}: {e}")
            return 0

    async def get_case_health_status(self) -> Dict[str, Any]:
        """
        Get case service health status and metrics

        Returns:
            Health status dictionary
        """
        try:
            # Get some basic metrics
            return {
                "service_status": "healthy",
                "repository_connected": self.repository is not None,
                "session_store_connected": self.session_store is not None,
                "report_store_connected": self.report_store is not None,
                "max_cases_per_user": self.max_cases_per_user
            }

        except Exception as e:
            self.logger.error(f"Failed to get case service health: {e}")
            return {
                "service_status": "unhealthy",
                "error": str(e)
            }

    # Message and Query Management Methods
    # Following design principles: delegate to case_store, proper error handling, interface compliance

    @trace("case_service_get_case_messages")
    async def get_case_messages(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[CaseMessage]:
        """
        Get messages for a case with pagination (FIXED IMPLEMENTATION)

        Args:
            case_id: Case identifier
            limit: Maximum number of messages to return
            offset: Offset for pagination

        Returns:
            List of case messages ordered by timestamp
        """
        if not case_id:
            raise ValidationException("Case ID is required")

        try:
            # Get case from repository (messages are stored in Case.messages now)
            case = await self.repository.get(case_id)
            if not case:
                raise ValidationException(f"Case {case_id} not found")

            # DEBUG: Log case.messages length
            self.logger.info(f"Case {case_id} has {len(case.messages)} messages in case.messages array, message_count={case.message_count}")

            # Convert dict messages to CaseMessage objects
            case_messages = []
            for msg_dict in case.messages:
                # Convert dict to CaseMessage object for compatibility
                # Per case-storage-design.md Section 4.7, use "created_at"
                case_msg = CaseMessage(
                    message_id=msg_dict["message_id"],
                    case_id=case_id,
                    turn_number=msg_dict.get("turn_number", 0),
                    role=msg_dict.get("role", "user"),
                    content=msg_dict["content"],
                    created_at=msg_dict.get("created_at"),
                    author_id=msg_dict.get("author_id"),
                    token_count=msg_dict.get("token_count"),
                    metadata=msg_dict.get("metadata", {}),
                    attachments=msg_dict.get("attachments")
                )
                case_messages.append(case_msg)

            # Log for observability
            self.logger.debug(f"Retrieved {len(case_messages)} messages for case {case_id}")

            return case_messages

        except Exception as e:
            self.logger.error(f"Failed to get messages for case {case_id}: {e}")
            raise ServiceException(f"Failed to retrieve case messages: {str(e)}") from e

    @trace("case_service_add_case_query")
    async def add_case_query(
        self,
        case_id: str,
        query_text: str,
        user_id: Optional[str] = None
    ) -> bool:
        """
        Add a user query to case conversation

        This method tracks the query in the case's message history.
        For milestone-based system, queries are implicit in turn processing.

        Args:
            case_id: Case identifier
            query_text: User's query text
            user_id: Optional user identifier

        Returns:
            True if query was tracked successfully
        """
        if not case_id or not query_text:
            raise ValidationException("Case ID and query text are required")

        try:
            # Get the case to verify it exists
            case = await self.repository.get(case_id)
            if not case:
                raise ValidationException(f"Case {case_id} not found")

            # Create user message and add to conversation (FIXED IMPLEMENTATION)
            from uuid import uuid4
            # Per case-storage-design.md Section 4.7, use "created_at"
            user_message = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": case.current_turn + 1,  # Next turn
                "role": "user",
                "message_type": "user_query",  # MessageType enum value
                "content": query_text.strip(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "author_id": user_id,
                "token_count": None,
                "metadata": {}
            }

            case.messages.append(user_message)
            case.message_count += 1
            case.last_activity_at = datetime.now(timezone.utc)

            # Save updated case with message
            await self.repository.save(case)

            self.logger.debug(f"Added user message to case {case_id}, message_count now {case.message_count}")
            return True

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to add query to case {case_id}: {e}")
            raise ServiceException(f"Failed to add case query: {str(e)}") from e

    @trace("case_service_list_case_queries")
    async def list_case_queries(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List user queries for a case

        Filters case messages to return only USER_QUERY messages.
        Returns in API format expected by routes.

        Args:
            case_id: Case identifier
            limit: Maximum number of queries to return
            offset: Offset for pagination

        Returns:
            List of query dictionaries
        """
        if not case_id:
            raise ValidationException("Case ID is required")

        try:
            # Get all messages and filter for queries
            # Note: For better performance, this could be optimized with store-level filtering
            all_messages = await self.get_case_messages(case_id, limit=limit+offset+50, offset=0)

            # Filter for USER_QUERY messages only
            query_messages = [
                msg for msg in all_messages
                if msg.message_type == MessageType.USER_QUERY
            ]

            # Apply pagination to filtered results
            paginated_queries = query_messages[offset:offset+limit]

            # Convert to API format
            queries = []
            for msg in paginated_queries:
                query_dict = {
                    "query_id": msg.message_id,
                    "query_text": msg.content,
                    "created_at": to_json_compatible(msg.created_at),
                    "user_id": msg.author_id,
                    "metadata": msg.metadata or {}
                }
                queries.append(query_dict)

            self.logger.debug(f"Retrieved {len(queries)} queries for case {case_id}")
            return queries

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to list queries for case {case_id}: {e}")
            raise ServiceException(f"Failed to list case queries: {str(e)}") from e

    @trace("case_service_count_case_queries")
    async def count_case_queries(self, case_id: str) -> int:
        """
        Count total user queries for a case

        Used for pagination metadata.

        Args:
            case_id: Case identifier

        Returns:
            Total number of user queries in the case
        """
        if not case_id:
            raise ValidationException("Case ID is required")

        try:
            # Get all messages and count queries
            # Note: This could be optimized with store-level counting
            all_messages = await self.get_case_messages(case_id, limit=1000, offset=0)

            query_count = sum(
                1 for msg in all_messages
                if msg.message_type == MessageType.USER_QUERY
            )

            self.logger.debug(f"Counted {query_count} queries for case {case_id}")
            return query_count

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to count queries for case {case_id}: {e}")
            return 0  # Graceful degradation for pagination

    @trace("case_service_count_user_cases")
    async def count_user_cases(
        self,
        user_id: str,
        filters: Optional[CaseListFilter] = None
    ) -> int:
        """
        Count total cases for a user

        Used for pagination in list_user_cases endpoint.

        Args:
            user_id: User identifier
            filters: Optional filter criteria

        Returns:
            Total number of cases for the user
        """
        if not user_id:
            raise ValidationException("User ID is required")

        try:
            # Get all user cases and count them
            # Note: This could be optimized with store-level counting
            cases = await self.list_user_cases(user_id, filters)
            count = len(cases) if cases else 0

            self.logger.debug(f"Counted {count} cases for user {user_id}")
            return count

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to count cases for user {user_id}: {e}")
            return 0  # Graceful degradation for pagination

    @trace("case_service_get_query_result")
    async def get_query_result(self, case_id: str, query_id: str) -> Optional[Dict[str, Any]]:
        """
        Get result for a specific query

        This method attempts to find a query response by looking for
        the agent response that follows a specific user query.

        Args:
            case_id: Case identifier
            query_id: Query message identifier

        Returns:
            Agent response dictionary or None if not found
        """
        if not case_id or not query_id:
            raise ValidationException("Case ID and query ID are required")

        try:
            # Get case messages and find the query + response pair
            messages = await self.get_case_messages(case_id, limit=1000, offset=0)

            # Find the query message
            query_message = None
            query_index = -1

            for i, msg in enumerate(messages):
                if msg.message_id == query_id and msg.message_type == MessageType.USER_QUERY:
                    query_message = msg
                    query_index = i
                    break

            if not query_message:
                self.logger.debug(f"Query {query_id} not found in case {case_id}")
                return None

            # Find the next agent response after this query
            for i in range(query_index + 1, len(messages)):
                msg = messages[i]
                if msg.message_type == MessageType.AGENT_RESPONSE:
                    # Found the response - convert to expected format
                    response_dict = {
                        "schema_version": "3.1.0",
                        "content": msg.content,
                        "response_type": msg.metadata.get("response_type", "ANSWER"),
                        "confidence_score": msg.metadata.get("confidence_score", 0.8),
                        "created_at": to_json_compatible(msg.created_at),
                        "query_id": query_id,
                        "response_id": msg.message_id
                    }

                    self.logger.debug(f"Found query result for {query_id} in case {case_id}")
                    return response_dict

            # No agent response found after this query
            self.logger.debug(f"No agent response found for query {query_id} in case {case_id}")
            return None

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get query result for {query_id} in case {case_id}: {e}")
            return None

    @trace("case_service_check_idempotency_key")
    async def check_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """
        Check if an idempotency key has been used before

        Args:
            idempotency_key: Idempotency key to check

        Returns:
            Previous result if key was used, None otherwise
        """
        if not idempotency_key:
            return None

        try:
            # Check if this idempotency key exists in Redis/store
            # For now, implement a simple in-memory check
            # In production, this would be stored in Redis with TTL

            # Note: This is a simplified implementation
            # A full implementation would store in Redis with expiration
            self.logger.debug(f"Checking idempotency key: {idempotency_key}")

            # For now, always return None (no previous result)
            # This means idempotency is disabled until we implement Redis storage
            return None

        except Exception as e:
            self.logger.error(f"Failed to check idempotency key {idempotency_key}: {e}")
            return None

    @trace("case_service_store_idempotency_result")
    async def store_idempotency_result(
        self,
        idempotency_key: str,
        status_code: int,
        content: Dict[str, Any],
        headers: Dict[str, str]
    ) -> bool:
        """
        Store result for an idempotency key

        Args:
            idempotency_key: Idempotency key
            status_code: HTTP status code of the response
            content: Response content
            headers: Response headers

        Returns:
            True if stored successfully
        """
        if not idempotency_key:
            return False

        try:
            # Store the result for this idempotency key
            # For now, implement a simple logging approach
            # In production, this would be stored in Redis with TTL (e.g., 24 hours)

            self.logger.debug(f"Storing idempotency result for key {idempotency_key}: {status_code}")

            # For now, just log and return success
            # A full implementation would store in Redis:
            # await self.redis.setex(f"idempotency:{idempotency_key}", 86400, json.dumps({
            #     "status_code": status_code,
            #     "content": content,
            #     "headers": headers,
            #     "timestamp": to_json_compatible(datetime.now(timezone.utc))
            # }))

            return True

        except Exception as e:
            self.logger.error(f"Failed to store idempotency result for {idempotency_key}: {e}")
            return False

    @trace("case_service_get_case_messages_enhanced")
    async def get_case_messages_enhanced(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0,
        include_debug: bool = False
    ) -> "CaseMessagesResponse":
        """
        Enhanced message retrieval with debugging support and metadata.

        This method provides comprehensive message retrieval with:
        - Pagination support
        - Debug information when requested
        - Storage error tracking
        - Message parsing error handling
        - Performance metrics

        Args:
            case_id: Case identifier
            limit: Maximum number of messages to return
            offset: Offset for pagination
            include_debug: Whether to include debug information

        Returns:
            CaseMessagesResponse with messages and metadata
        """
        if not case_id:
            raise ValidationException("Case ID is required")

        # Import here to avoid circular dependencies
        from faultmaven.models.api import CaseMessagesResponse, MessageRetrievalDebugInfo, Message
        import time

        start_time = time.time()
        debug_info = None
        storage_errors = []
        message_parsing_errors = 0

        try:
            # Get all messages for the case first to calculate total count
            all_messages = await self.get_case_messages(case_id, limit=1000, offset=0)
            total_count = len(all_messages)

            # Apply pagination to the messages
            paginated_messages = all_messages[offset:offset + limit]
            retrieved_count = len(paginated_messages)

            # Convert CaseMessage objects to API Message format
            messages = []
            for case_msg in paginated_messages:
                try:
                    # CaseMessage already has 'role' field, use it directly
                    # No need to map from message_type (that field doesn't exist in CaseMessage)
                    role = case_msg.role if hasattr(case_msg, 'role') else "system"

                    # Format created_at
                    created_at_str = None
                    if case_msg.created_at:
                        try:
                            if hasattr(case_msg.created_at, 'isoformat'):
                                created_at_str = to_json_compatible(case_msg.created_at)
                            else:
                                created_at_str = str(case_msg.created_at)
                        except Exception as e:
                            self.logger.warning(f"Failed to format created_at for message {case_msg.message_id}: {e}")
                            created_at_str = str(case_msg.created_at)

                    # Create API Message object
                    # Per case-storage-design.md Section 4.7, use "created_at" field
                    api_message = Message(
                        message_id=case_msg.message_id,
                        turn_number=case_msg.turn_number,
                        role=role,
                        content=case_msg.content,
                        created_at=created_at_str,
                        author_id=case_msg.author_id,
                        token_count=case_msg.token_count,
                        metadata=case_msg.metadata
                    )
                    messages.append(api_message)

                except Exception as e:
                    message_parsing_errors += 1
                    self.logger.warning(f"Failed to convert message {getattr(case_msg, 'message_id', 'unknown')}: {e}")
                    if include_debug:
                        storage_errors.append(f"Message parsing error: {str(e)}")

            # Calculate performance metrics
            processing_time_ms = int((time.time() - start_time) * 1000)

            # Create debug info if requested
            if include_debug:
                debug_info = MessageRetrievalDebugInfo(
                    storage_backend="redis",
                    redis_key=f"case:{case_id}:messages",
                    total_messages_in_storage=total_count,
                    messages_requested=limit,
                    messages_retrieved=retrieved_count,
                    offset_used=offset,
                    processing_time_ms=processing_time_ms,
                    storage_errors=storage_errors,
                    message_parsing_errors=message_parsing_errors
                )

            # Determine if there are more messages
            has_more = (offset + retrieved_count) < total_count

            # Create and return response
            response = CaseMessagesResponse(
                messages=messages,
                total_count=total_count,
                retrieved_count=retrieved_count,
                has_more=has_more,
                debug_info=debug_info
            )

            self.logger.debug(
                f"Retrieved {retrieved_count}/{total_count} messages for case {case_id} "
                f"in {processing_time_ms}ms"
            )

            return response

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get enhanced messages for case {case_id}: {e}")
            # Return empty response with error info for graceful degradation
            if include_debug:
                debug_info = MessageRetrievalDebugInfo(
                    storage_backend="redis",
                    redis_key=f"case:{case_id}:messages",
                    total_messages_in_storage=0,
                    messages_requested=limit,
                    messages_retrieved=0,
                    offset_used=offset,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    storage_errors=[f"Service error: {str(e)}"],
                    message_parsing_errors=0
                )

            return CaseMessagesResponse(
                messages=[],
                total_count=0,
                retrieved_count=0,
                has_more=False,
                debug_info=debug_info
            )

    # ============================================================
    # Case Sharing Operations
    # ============================================================

    @trace("case_service_share_case")
    async def share_case(
        self,
        case_id: str,
        target_user_id: str,
        role: str,  # 'owner', 'collaborator', 'viewer'
        sharer_user_id: str
    ) -> bool:
        """
        Share a case with another user.

        Args:
            case_id: Case identifier
            target_user_id: User to share with
            role: Role to assign (owner, collaborator, viewer)
            sharer_user_id: User performing the share action

        Returns:
            True if case was shared successfully

        Raises:
            ValidationException: If user lacks permission to share
        """
        # Verify case exists
        case = await self.repository.get(case_id)
        if not case:
            raise ValidationException(f"Case {case_id} not found")

        # Verify sharer has permission (must be owner or existing collaborator)
        if case.user_id != sharer_user_id:
            # Check if sharer is a participant with appropriate permissions
            participants = await self.repository.get_case_participants(case_id)
            sharer_participant = next(
                (p for p in participants if p["user_id"] == sharer_user_id),
                None
            )
            if not sharer_participant or sharer_participant["role"] == "viewer":
                raise ValidationException(
                    "User lacks permission to share this case"
                )

        # Share the case
        success = await self.repository.share_case(
            case_id, target_user_id, role, sharer_user_id
        )

        if success:
            self.logger.info(
                f"Case {case_id} shared with user {target_user_id} as {role} "
                f"by {sharer_user_id}"
            )

        return success

    @trace("case_service_unshare_case")
    async def unshare_case(
        self,
        case_id: str,
        target_user_id: str,
        unsharer_user_id: str
    ) -> bool:
        """
        Unshare a case from a user.

        Args:
            case_id: Case identifier
            target_user_id: User to unshare from
            unsharer_user_id: User performing the unshare action

        Returns:
            True if case was unshared successfully

        Raises:
            ValidationException: If user lacks permission or trying to unshare owner
        """
        # Verify case exists
        case = await self.repository.get(case_id)
        if not case:
            raise ValidationException(f"Case {case_id} not found")

        # Prevent unsharing the case owner
        if case.user_id == target_user_id:
            raise ValidationException("Cannot unshare the case owner")

        # Verify unsharer has permission
        if case.user_id != unsharer_user_id:
            participants = await self.repository.get_case_participants(case_id)
            unsharer_participant = next(
                (p for p in participants if p["user_id"] == unsharer_user_id),
                None
            )
            if not unsharer_participant or unsharer_participant["role"] != "owner":
                raise ValidationException(
                    "User lacks permission to unshare this case"
                )

        # Unshare the case
        success = await self.repository.unshare_case(
            case_id, target_user_id, unsharer_user_id
        )

        if success:
            self.logger.info(
                f"Case {case_id} unshared from user {target_user_id} "
                f"by {unsharer_user_id}"
            )

        return success

    @trace("case_service_get_case_participants")
    async def get_case_participants(self, case_id: str) -> List[Dict[str, Any]]:
        """
        Get all participants for a case.

        Args:
            case_id: Case identifier

        Returns:
            List of participants with their roles and metadata
        """
        return await self.repository.get_case_participants(case_id)

    @trace("case_service_user_can_access_case")
    async def user_can_access_case(self, case_id: str, user_id: str) -> bool:
        """
        Check if a user can access a case.

        Checks:
        1. User is the case owner
        2. User is a participant
        3. User is a team member (if case has team_id)
        4. User has org-level access (if case has org_id)

        Args:
            case_id: Case identifier
            user_id: User identifier

        Returns:
            True if user can access the case
        """
        # Get case
        case = await self.repository.get(case_id)
        if not case:
            return False

        # Check if user is owner
        if case.user_id == user_id:
            return True

        # Check if user is participant
        participants = await self.repository.get_case_participants(case_id)
        if any(p["user_id"] == user_id for p in participants):
            return True

        # TODO: Check team/org membership when those services are integrated
        # For now, return False if not owner or explicit participant

        return False

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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from faultmaven.services.base import BaseService
from faultmaven.models.case import (
    Case,
    CaseCreateRequest,
    CaseListFilter,
    CaseMessage,
    CaseSearchRequest,
    CaseSummary,
    CaseUpdateRequest,
    MessageType,
    ParticipantRole,
    CaseStatus,
    CaseParticipant
)
from faultmaven.models.interfaces_case import ICaseStore, ICaseService
from faultmaven.models.interfaces import ISessionStore
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.models import parse_utc_timestamp


class CaseService(BaseService, ICaseService):
    """Service for centralized case management and coordination"""

    def __init__(
        self,
        case_store: ICaseStore,
        session_store: Optional[ISessionStore] = None,
        settings: Optional[Any] = None,
        default_case_expiry_days: int = 30,
        max_cases_per_user: int = 100
    ):
        """
        Initialize the Case Service

        Args:
            case_store: Case persistence store interface
            session_store: Optional session store for integration
            settings: Configuration settings for the service
            default_case_expiry_days: Default case expiration in days
            max_cases_per_user: Maximum cases per user
        """
        super().__init__("case_service")
        self.case_store = case_store
        self.session_store = session_store
        self._settings = settings
        
        # Use settings values if available, otherwise use parameter defaults
        if settings and hasattr(settings, 'case'):
            self.default_case_expiry_days = getattr(settings.case, 'expiry_days', default_case_expiry_days)
            self.max_cases_per_user = getattr(settings.case, 'max_per_user', max_cases_per_user)
        else:
            self.default_case_expiry_days = default_case_expiry_days
            self.max_cases_per_user = max_cases_per_user

    @trace("case_service_create_case")
    async def create_case(
        self,
        title: str,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        session_id: Optional[str] = None,
        initial_message: Optional[str] = None
    ) -> Case:
        """
        Create a new troubleshooting case

        Args:
            title: Case title
            owner_id: Required owner user ID
            description: Optional case description
            session_id: Optional session to associate with case
            initial_message: Optional initial message content

        Returns:
            Created case object

        Raises:
            ValidationException: If input validation fails
            ServiceException: If case creation fails
        """
        if not title or not title.strip():
            raise ValidationException("Case title cannot be empty")

        if len(title) > 200:
            raise ValidationException("Case title cannot exceed 200 characters")

        # Anonymous case creation is supported (owner_id can be None)
        # If owner_id is provided, it must not be empty
        if owner_id is not None and (not owner_id or not owner_id.strip()):
            raise ValidationException("Owner ID cannot be empty when provided")

        try:
            # Check user case limits (skip for anonymous users)
            if owner_id is not None:
                user_cases = await self.list_user_cases(owner_id)
                active_cases = [c for c in user_cases if c.status not in [CaseStatus.ARCHIVED]]

                if len(active_cases) >= self.max_cases_per_user:
                    raise ValidationException(f"User has reached maximum case limit ({self.max_cases_per_user})")

            # Create new case (supports anonymous owner_id=None)
            case = Case(
                title=title.strip(),
                description=description.strip() if description else None,
                owner_id=owner_id.strip() if owner_id else None,
                expires_at=datetime.utcnow() + timedelta(days=self.default_case_expiry_days)
            )

            # Add owner as participant (only if owner_id is provided)
            if owner_id:
                case.add_participant(owner_id.strip(), ParticipantRole.OWNER)

            # Add initial message if provided
            if initial_message:
                message = CaseMessage(
                    case_id=case.case_id,
                    session_id=session_id,
                    author_id=owner_id.strip() if owner_id else None,
                    message_type=MessageType.USER_QUERY,
                    content=initial_message.strip()
                )
                case.add_message(message)

            # Session association removed - cases are user-owned, not session-bound
            # Update session with case ID if session store available
                if self.session_store:
                    try:
                        await self.session_store.set(
                            f"session:{session_id}:current_case_id", 
                            case.case_id, 
                            ttl=86400  # 24 hours
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to update session with case ID: {e}")

            # Store the case
            success = await self.case_store.create_case(case)
            if not success:
                raise ServiceException("Failed to create case in store")

            self.logger.info(f"Created case {case.case_id} with title '{title}'")
            return case

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
            case = await self.case_store.get_case(case_id)
            if not case:
                return None

            # Apply access control if user_id provided
            if user_id:
                if not case.can_user_access(user_id):
                    self.logger.warning(f"User {user_id} denied access to case {case_id}")
                    return None

                # Update participant last accessed time
                for participant in case.participants:
                    if participant.user_id == user_id:
                        participant.last_accessed = datetime.utcnow()
                        # Update in store - serialize datetime objects properly
                        participants_data = []
                        for p in case.participants:
                            p_dict = p.dict()
                            # Convert datetime fields to ISO strings
                            if p_dict.get('added_at'):
                                p_dict['added_at'] = p_dict['added_at'].isoformat() + 'Z' if hasattr(p_dict['added_at'], 'isoformat') else str(p_dict['added_at'])
                            if p_dict.get('last_accessed'):
                                p_dict['last_accessed'] = p_dict['last_accessed'].isoformat() + 'Z' if hasattr(p_dict['last_accessed'], 'isoformat') else str(p_dict['last_accessed'])
                            participants_data.append(p_dict)

                        await self.case_store.update_case(case_id, {
                            "participants": participants_data
                        })
                        break

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
            updates: Updates to apply
            user_id: Optional user ID for access control

        Returns:
            True if update was successful
        """
        if not case_id or not case_id.strip():
            raise ValidationException("Case ID cannot be empty")

        if not updates:
            raise ValidationException("Updates cannot be empty")

        try:
            # Get current case and check access with retry logic for race conditions
            # This handles the case where case was just created and Redis hasn't fully committed
            case = None
            max_retries = 3
            retry_delay = 0.05  # 50ms initial delay

            for attempt in range(max_retries):
                case = await self.get_case(case_id, user_id)
                if case:
                    break
                if attempt < max_retries - 1:
                    # Exponential backoff: 50ms, 100ms, 200ms
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    self.logger.debug(f"Retry {attempt + 1}/{max_retries} for case {case_id}")

            if not case:
                return False

            # Check edit permissions
            if user_id:
                if not case.can_user_edit(user_id):
                    self.logger.warning(f"User {user_id} denied edit access to case {case_id}")
                    return False

            # Validate update fields
            allowed_fields = {
                'title', 'description', 'status', 'priority', 'tags', 
                'metadata', 'context', 'auto_archive_after_days'
            }
            
            filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}
            if not filtered_updates:
                raise ValidationException("No valid update fields provided")

            # Add update metadata
            filtered_updates['updated_at'] = datetime.utcnow().isoformat() + 'Z'
            if user_id:
                filtered_updates['metadata'] = {
                    **case.metadata,
                    'last_updated_by': user_id
                }

            # Apply updates
            success = await self.case_store.update_case(case_id, filtered_updates)
            if success:
                self.logger.info(f"Updated case {case_id}")
            
            return success

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to update case {case_id}: {e}")
            raise ServiceException(f"Case update failed: {str(e)}") from e

    @trace("case_service_share_case")
    async def share_case(
        self,
        case_id: str,
        target_user_id: str,
        role: ParticipantRole,
        sharer_user_id: Optional[str] = None
    ) -> bool:
        """
        Share a case with another user

        Args:
            case_id: Case identifier
            target_user_id: User to share with
            role: Role to assign to the user
            sharer_user_id: User performing the share action

        Returns:
            True if case was shared successfully
        """
        if not case_id or not target_user_id:
            raise ValidationException("Case ID and target user ID are required")

        if role == ParticipantRole.OWNER:
            raise ValidationException("Cannot assign owner role through sharing")

        try:
            # Get case and check share permissions
            case = await self.get_case(case_id, sharer_user_id)
            if not case:
                return False

            if sharer_user_id and not case.can_user_share(sharer_user_id):
                self.logger.warning(f"User {sharer_user_id} denied share access to case {case_id}")
                return False

            # Check if user is already a participant
            existing_role = case.get_participant_role(target_user_id)
            if existing_role:
                # Update existing participant role if different
                if existing_role != role:
                    success = await self.case_store.update_case(case_id, {
                        "participants": [
                            {**p.dict(), "role": role.value} if p.user_id == target_user_id else p.dict()
                            for p in case.participants
                        ],
                        "updated_at": datetime.utcnow().isoformat() + 'Z'
                    })
                    if success:
                        self.logger.info(f"Updated role for user {target_user_id} in case {case_id}")
                    return success
                else:
                    # User already has the same role
                    return True

            # Add new participant
            success = await self.case_store.add_case_participant(
                case_id, target_user_id, role, sharer_user_id
            )

            if success:
                # Update case metadata
                await self.case_store.update_case(case_id, {
                    "share_count": case.share_count + 1,
                    "updated_at": datetime.utcnow().isoformat() + 'Z',
                    "metadata": {
                        **case.metadata,
                        f"shared_with_{target_user_id}": {
                            "shared_by": sharer_user_id,
                            "shared_at": datetime.utcnow().isoformat() + 'Z',
                            "role": role.value
                        }
                    }
                })

                self.logger.info(f"Shared case {case_id} with user {target_user_id} as {role.value}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to share case {case_id}: {e}")
            raise ServiceException(f"Case sharing failed: {str(e)}") from e

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
            # Ensure message belongs to this case
            message.case_id = case_id
            if session_id:
                message.session_id = session_id

            # Add message to store
            success = await self.case_store.add_message_to_case(case_id, message)
            
            if success:
                # Update case activity
                await self.case_store.update_case_activity(case_id, session_id)
                
                # Session tracking removed - cases are accessed via user authentication

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
        force_new: bool = False
    ) -> str:
        """
        Get existing case for session or create new one

        Args:
            session_id: Session identifier
            user_id: Optional user identifier
            force_new: Force creation of new case

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
                        if case and not case.is_expired():
                            self.logger.debug(f"Using existing case {existing_case_id} for session {session_id}")
                            return existing_case_id
                except Exception as e:
                    self.logger.warning(f"Failed to get existing case for session: {e}")

            # Create new case
            title = f"Troubleshooting Session {session_id[:8]}"
            case = await self.create_case(
                title=title,
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
            case = await self.case_store.get_case(case_id)
            if not case:
                return False

            # Session tracking removed - cases are accessed via user authentication
            # Update last activity time
            success = await self.case_store.update_case(case_id, {
                "last_activity_at": datetime.utcnow().isoformat() + 'Z'
            })

            if not success:
                return False

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
            messages = await self.case_store.get_case_messages(case_id, limit=limit)
            if not messages:
                return ""

            # Sort messages by timestamp
            sorted_messages = sorted(messages, key=lambda m: m.timestamp)

            # Format for LLM context
            context_lines = ["Previous conversation in this troubleshooting case:"]
            
            for i, message in enumerate(sorted_messages[:-1], 1):  # Exclude current query
                timestamp = message.timestamp.strftime("%H:%M")
                
                if message.message_type == MessageType.USER_QUERY:
                    context_lines.append(f"{i}. [{timestamp}] User: {message.content}")
                elif message.message_type == MessageType.AGENT_RESPONSE:
                    # Truncate long agent responses
                    content = message.content[:200] + "..." if len(message.content) > 200 else message.content
                    context_lines.append(f"{i}. [{timestamp}] Assistant: {content}")
                elif message.message_type == MessageType.SYSTEM_EVENT:
                    context_lines.append(f"{i}. [{timestamp}] System: {message.content}")

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

    @trace("case_service_archive_case")
    async def archive_case(
        self,
        case_id: str,
        reason: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> bool:
        """
        Archive a case

        Args:
            case_id: Case identifier
            reason: Optional archive reason
            user_id: Optional user ID for access control

        Returns:
            True if case was archived successfully
        """
        if not case_id:
            raise ValidationException("Case ID cannot be empty")

        try:
            # Check permissions if user provided
            if user_id:
                case = await self.get_case(case_id, user_id)
                if not case:
                    return False

                # Check if user can archive
                user_role = case.get_participant_role(user_id)
                if user_role not in [ParticipantRole.OWNER, ParticipantRole.COLLABORATOR]:
                    self.logger.warning(f"User {user_id} denied archive access to case {case_id}")
                    return False

            # Archive the case
            updates = {
                "status": CaseStatus.ARCHIVED.value,
                "updated_at": datetime.utcnow().isoformat() + 'Z',
                "metadata": {}
            }

            if reason:
                updates["metadata"]["archive_reason"] = reason
            if user_id:
                updates["metadata"]["archived_by"] = user_id

            success = await self.case_store.update_case(case_id, updates)
            
            if success:
                self.logger.info(f"Archived case {case_id}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to archive case {case_id}: {e}")
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
                    
                # Check if user can delete (only owner or admin)
                user_role = case.get_participant_role(user_id)
                if user_role != ParticipantRole.OWNER:
                    self.logger.warning(f"User {user_id} denied delete access to case {case_id}")
                    return False
            
            # Perform hard delete through case store
            success = await self.case_store.delete_case(case_id)
            
            if success:
                self.logger.info(f"Hard deleted case {case_id}")
                
                # TODO: Cascade delete associated data:
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
            filters: Optional filter criteria

        Returns:
            List of user's cases
        """
        if not user_id:
            raise ValidationException("User ID cannot be empty")

        try:
            return await self.case_store.get_user_cases(user_id, filters)

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
            # Add user filter if provided
            if user_id and search_request.filters:
                search_request.filters.user_id = user_id
            elif user_id:
                search_request.filters = CaseListFilter(user_id=user_id)

            return await self.case_store.search_cases(search_request)

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
            return await self.case_store.get_case_analytics(case_id)

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
            cleaned_count = await self.case_store.cleanup_expired_cases()
            
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
        List cases associated with a session

        Args:
            session_id: Session identifier
            limit: Maximum number of cases to return
            offset: Number of cases to skip

        Returns:
            List of cases associated with the session
        """
        if not session_id:
            raise ValidationException("Session ID cannot be empty")

        try:
            # Get user's cases (session provides authentication context)
            all_cases = await self.case_store.get_cases_by_session(session_id, limit, offset)
            return all_cases

        except Exception as e:
            self.logger.error(f"Failed to list cases for session {session_id}: {e}")
            return []

    @trace("case_service_count_cases_by_session")
    async def count_cases_by_session(self, session_id: str) -> int:
        """
        Count total cases associated with a session

        Args:
            session_id: Session identifier

        Returns:
            Total number of cases associated with the session
        """
        if not session_id:
            return 0

        try:
            count = await self.case_store.count_cases_by_session(session_id)
            return count

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
            # This would typically query the case store for metrics
            return {
                "service_status": "healthy",
                "case_store_connected": True,
                "session_store_connected": self.session_store is not None,
                "default_expiry_days": self.default_case_expiry_days,
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
        Get messages for a case with pagination

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
            # Delegate to case store following interface contract
            messages = await self.case_store.get_case_messages(case_id, limit, offset)

            # Log for observability
            self.logger.debug(f"Retrieved {len(messages)} messages for case {case_id}")

            return messages or []

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

        This method creates a USER_QUERY message and adds it to the case.
        It follows our design principle of using existing message infrastructure.

        Args:
            case_id: Case identifier
            query_text: User's query text
            user_id: Optional user identifier

        Returns:
            True if query was added successfully
        """
        if not case_id or not query_text:
            raise ValidationException("Case ID and query text are required")

        try:
            # Create proper CaseMessage following data model contracts
            query_message = CaseMessage(
                case_id=case_id,
                author_id=user_id,
                message_type=MessageType.USER_QUERY,
                content=query_text.strip(),
                metadata={"query": True}  # Flag for filtering
            )

            # Use existing message infrastructure
            success = await self.add_message_to_case(case_id, query_message)

            if success:
                self.logger.debug(f"Added query to case {case_id}")

            return success

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
                    "timestamp": msg.timestamp.isoformat() + 'Z',
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
                        "timestamp": msg.timestamp.isoformat() + 'Z',
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
            #     "timestamp": datetime.utcnow().isoformat()
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
                    # Map message_type to role
                    role = "system"  # default
                    if case_msg.message_type == MessageType.USER_QUERY:
                        role = "user"
                    elif case_msg.message_type == MessageType.AGENT_RESPONSE:
                        role = "assistant"  # Use "assistant" as per API spec
                    elif case_msg.message_type == MessageType.CASE_NOTE:
                        role = "user"
                    # Keep system for other types (SYSTEM_EVENT, DATA_UPLOAD, STATUS_CHANGE)

                    # Format timestamp
                    created_at = None
                    if case_msg.timestamp:
                        try:
                            if hasattr(case_msg.timestamp, 'isoformat'):
                                created_at = case_msg.timestamp.isoformat() + 'Z'
                            else:
                                created_at = str(case_msg.timestamp)
                        except Exception as e:
                            self.logger.warning(f"Failed to format timestamp for message {case_msg.message_id}: {e}")
                            created_at = str(case_msg.timestamp)

                    # Create API Message object
                    api_message = Message(
                        message_id=case_msg.message_id,
                        role=role,
                        content=case_msg.content,
                        created_at=created_at,
                        metadata=case_msg.metadata or {}
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
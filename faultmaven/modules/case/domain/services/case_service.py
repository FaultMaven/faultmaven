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
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from faultmaven.config.tenant_context import get_current_org_id
from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api_models import (
    CaseCreateRequest,
    CaseListFilter,
    CaseMessage,
    CaseParticipant,
    CaseSearchRequest,
    CaseSummary,
    CaseUpdateRequest,
)
from faultmaven.models.interfaces import ISessionStore
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import is_team_member
from faultmaven.modules.case.domain.models import Case, CaseState, MessageType
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.utils.datetime import parse_utc_timestamp
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class CaseService(ICaseService):
    """Service for centralized case management and coordination"""

    def __init__(
        self,
        case_repository: CaseRepository,
        session_store: Optional[ISessionStore] = None,
        case_vector_store: Optional[Any] = None,
        settings: Optional[Any] = None,
        max_cases_per_user: int = 100,
        team_service: Optional[Any] = None,
        share_repository: Optional[Any] = None,
    ):
        """
        Initialize the Case Service

        Args:
            case_repository: Case repository for persistence (reports are handled via repository)
            session_store: Optional session store for integration
            case_vector_store: Optional case vector store for Working Memory cleanup
            settings: Configuration settings for the service
            max_cases_per_user: Maximum cases per user
            team_service: Optional team-membership resolver (``list_all_user_team_ids``).
                Unwired in standalone — the shared-case arm then resolves empty.
            share_repository: Optional ``IShareRepository`` for the case read
                allowlist (``owned ∪ shared-to-my-teams``, ADR-013 §D4). Both
                degrade gracefully to owner-only when absent.
        """
        self.repository = case_repository
        self.session_store = session_store
        self.case_vector_store = case_vector_store
        self._settings = settings
        self.team_service = team_service
        self.share_repository = share_repository

        # Use settings values if available, otherwise use parameter defaults
        if settings and hasattr(settings, "case"):
            self.max_cases_per_user = getattr(
                settings.case, "max_per_user", max_cases_per_user
            )
        else:
            self.max_cases_per_user = max_cases_per_user

    @trace("case_service_create_case")
    async def create_case(
        self,
        title: Optional[str] = None,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        session_id: Optional[str] = None,
        initial_message: Optional[str] = None,
        source: str = "copilot",
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
            user_cases_list, total = await self.repository.list(
                user_id=owner_id.strip()
            )
            # Only count non-terminal cases
            active_cases = [
                c
                for c in user_cases_list
                if c.state not in [CaseState.RESOLVED, CaseState.CLOSED]
            ]

            if len(active_cases) >= self.max_cases_per_user:
                raise ValidationException(
                    f"User has reached maximum case limit ({self.max_cases_per_user})"
                )

            # Auto-generate title if not provided (Format: Case-YYMMDD-N)
            if not title or not title.strip():
                # Format: Case-YYMMDD-N (e.g., Case-260128-1)
                # Sequence counter resets daily via key expiration/change
                now = datetime.now(timezone.utc)
                date_str = now.strftime("%y%m%d")  # YYMMDD (Year-safe)

                # 1. Get Redis Counter (Atomic, High Performance)
                redis_seq = 0
                if self.session_store:
                    try:
                        # Key format: case_seq:{user_id}:{YYMMDD}
                        seq_key = f"case_seq:{owner_id.strip()}:{date_str}"
                        redis_seq = await self.session_store.increment_counter(
                            seq_key, ttl=172800
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to increment atomic counter (Redis): {e}"
                        )

                # 2. Get Database Count (Persistence, Robustness)
                # Always check DB to ensure we don't duplicate if Redis was reset (e.g. restart)
                db_count = 0
                try:
                    db_count = await self.repository.count_user_cases_on_date(
                        owner_id.strip(), now.date()
                    )
                except Exception as e:
                    logger.error(f"Failed to count cases in DB: {e}")

                # 3. Hybrid Strategy: Max of both
                # - If Redis is fresh (0) but DB has 5 cases, we start at 6.
                # - If Redis is ahead (10) but DB has 5 (lag), we use 10.
                sequence = max(redis_seq, db_count + 1)

                # Fallback safeguard
                if sequence == 0:
                    sequence = 1

                title = f"Case-{date_str}-{sequence}"
                logger.debug(f"Auto-generated title: {title}")
            else:
                title = title.strip()
                if len(title) > 200:
                    raise ValidationException("Case title cannot exceed 200 characters")

            # Stamp the case with the request's resolved organization. The
            # request->org middleware (api/middleware/tenant_scope.py) binds this
            # once per request: the Standalone org in single-tenant mode, or the
            # caller's verified JWT org in multi-tenant mode. It is the same value
            # RLS scopes every query to, so the write stamp and the read-isolation
            # boundary can never diverge.
            resolved_org_id = get_current_org_id()

            # Create new case using milestone-based model
            case = Case(
                title=title,
                description=description.strip() if description else "",
                user_id=owner_id.strip(),
                organization_id=resolved_org_id,  # Deployment-agnostic org resolution
                source=source if source in ("copilot", "slack", "api") else "copilot",
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
                    "metadata": {},
                }
                case.messages.append(message_dict)
                case.message_count = len(case.messages)

            # Session association via session store if available
            if session_id and self.session_store:
                try:
                    await self.session_store.set(
                        f"session:{session_id}:current_case_id",
                        case.case_id,
                        ttl=86400,  # 24 hours
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session with case ID: {e}")

            # Save the case using repository
            saved_case = await self.repository.save(case)

            # Share-creation defaults (ADR-013 §D3). A Slack-originated case is
            # auto-shared to the workspace's Team at creation, so every Team
            # member sees it (the "visible to the whole team via Slack" promise);
            # a Copilot case stays personal-until-shared (no share row). Inert in
            # standalone — see ``_auto_share_slack_case``.
            if saved_case.source == "slack":
                await self._auto_share_slack_case(saved_case)

            logger.info(f"Created case {saved_case.case_id} with title '{title}'")
            return saved_case

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to create case: {e}")
            raise ServiceException(f"Case creation failed: {str(e)}") from e

    @trace("case_service_get_case")
    async def get_case(
        self, case_id: str, user_id: Optional[str] = None
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

            # Access control: the requester must OWN the case or have it SHARED
            # to one of their teams (owned ∪ shared-to-my-teams, ADR-013 §D4) —
            # the single-case gate transitively guarding reports, exports,
            # analytics, and messages. The shared arm is inert until case shares
            # exist (U10); in standalone it always resolves empty (owner-only).
            if user_id and case.user_id != user_id:
                shared_case_ids = await self._resolve_shared_case_ids(user_id)
                if case_id not in shared_case_ids:
                    logger.warning(
                        f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                    )
                    return None

            return case

        except Exception as e:
            logger.error(f"Failed to get case {case_id}: {e}")
            return None

    @trace("case_service_update_case")
    async def update_case(
        self, case_id: str, updates: Dict[str, Any], user_id: Optional[str] = None
    ) -> bool:
        """
        Update case with access control.

        Splits writes into two channels based on field semantics:

        - **Metadata only** (title, description): goes through the scoped
          ``update_metadata_fields`` repo method. Does NOT bump
          ``cases.version`` and therefore cannot stale-conflict with an
          in-flight turn save. This is the common path for title
          generation and dashboard renames.
        - **Investigation state** (status, closure_reason, or mixed with
          metadata): goes through ``update_case_with_retry``, which uses
          the versioned ``save`` path with OCC retry on conflict. Status
          transitions are real investigation events; concurrent writers
          must coordinate.

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

        # Validate and apply updates directly to Case object
        metadata_fields = {"title", "description"}
        state_fields = {"state", "closure_reason"}
        allowed_fields = metadata_fields | state_fields
        safe_updates = {k: v for k, v in updates.items() if k in allowed_fields}

        try:
            # Access check happens by loading via get_case first. Then the
            # retry helper (state path) reloads on conflict via
            # repository.get(), which skips the access check. We enforce
            # the check once up front; no privilege escalation window
            # exists because the user_id doesn't change between attempts.
            existing = await self.get_case(case_id, user_id)
            if not existing:
                return False

            touches_state = any(k in state_fields for k in safe_updates)
            touches_metadata = any(k in metadata_fields for k in safe_updates)

            if not touches_state and touches_metadata:
                # Metadata-only path: scoped UPDATE, no version bump.
                await self.repository.update_metadata_fields(
                    case_id,
                    title=safe_updates.get("title"),
                    description=safe_updates.get("description"),
                )
            elif touches_state:
                # Investigation-state path: versioned save with OCC retry.
                # Any metadata fields in the same call ride along on the
                # versioned save — the OCC has to fire for the state
                # change anyway, so there's no value in splitting them.
                from faultmaven.modules.case.utils import update_case_with_retry

                async def apply(case: Case) -> None:
                    for key, value in safe_updates.items():
                        if hasattr(case, key):
                            setattr(case, key, value)

                await update_case_with_retry(self.repository, case_id, apply)
            # else: no recognized fields — treat as no-op success (matches
            # pre-split behavior for forward compatibility with callers
            # that pass unrelated keys).

            logger.info(f"Updated case {case_id}")
            return True

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to update case {case_id}: {e}")
            raise ServiceException(f"Case update failed: {str(e)}") from e

    @trace("case_service_add_message")
    async def add_message_to_case(
        self, case_id: str, message: CaseMessage, session_id: Optional[str] = None
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

            message_role = getattr(message, "role", "system")

            # Deduplication: Check if identical to last message
            # This fixes Issue 1: Duplicate Questions When Chat History is Reloaded
            # A turn is a duplicate only if the SAME principal resubmits the same
            # content — on team-shared cases two members can legitimately post
            # identical adjacent turns ("still broken", "+1"), which must both
            # persist (#855).
            if case.messages and len(case.messages) > 0:
                last_msg = case.messages[-1]
                # Check for identical content, role, and author
                if (
                    last_msg.get("role") == message_role
                    and last_msg.get("content") == message.content
                    and last_msg.get("author_id") == message.author_id
                ):
                    logger.warning(
                        f"Skipping duplicate message for case {case_id} (content hash match)",
                        extra={"case_id": case_id, "message_id": message.message_id},
                    )
                    return True

            # Increment turn number for new user messages
            # This fixes Issue 3: Turn number for each turn is always 1
            if message_role == "user":
                case.current_turn += 1
                # Persist the new turn number
                # Note: We must save the case to persist the turn update before adding the message
                await self.repository.save(case)

            # Convert CaseMessage to dict format for storage (per case-storage-design.md spec)
            message_dict = {
                "message_id": message.message_id,
                "case_id": case_id,
                "author_id": message.author_id,
                "role": message_role,
                "message_type": (
                    getattr(message, "message_type", None).value
                    if getattr(message, "message_type", None)
                    and hasattr(getattr(message, "message_type", None), "value")
                    else str(
                        getattr(
                            message,
                            "message_type",
                            "user_query" if message_role == "user" else "system_event",
                        )
                    )
                ),
                "content": message.content,
                "created_at": (
                    message.created_at.isoformat()
                    if hasattr(message.created_at, "isoformat")
                    else str(message.created_at)
                ),
                "turn_number": case.current_turn,
                "token_count": getattr(message, "token_count", None),
                "metadata": message.metadata or {},
            }

            # Delegate to repository - it handles storage-specific logic
            success = await self.repository.add_message(case_id, message_dict)

            if success:
                logger.debug(f"Added message to case {case_id}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to add message to case {case_id}: {e}")
            return False

    @trace("case_service_get_or_create_case_for_session")
    async def get_or_create_case_for_session(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        force_new: bool = False,
        title: Optional[str] = None,
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
                    existing_case_id = await self.session_store.get(
                        f"session:{session_id}:current_case_id"
                    )
                    if existing_case_id:
                        # Verify case still exists and is accessible
                        case = await self.get_case(existing_case_id, user_id)
                        if case:
                            logger.debug(
                                f"Using existing case {existing_case_id} for session {session_id}"
                            )
                            return existing_case_id
                except Exception as e:
                    logger.warning(f"Failed to get existing case for session: {e}")

            # Create new case - pass title as-is to trigger auto-generation when None
            case = await self.create_case(
                title=title,  # None triggers Case-MMDD-N auto-generation
                description="Auto-created case for troubleshooting session",
                owner_id=user_id,
                session_id=session_id,
            )

            logger.info(f"Created new case {case.case_id} for session {session_id}")
            return case.case_id

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to get/create case for session {session_id}: {e}")
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
                        ttl=86400,  # 24 hours
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session store: {e}")

            logger.info(f"Linked session {session_id} to case {case_id}")
            return True

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to link session to case: {e}")
            return False

    @trace("case_service_get_conversation_context")
    async def get_case_conversation_context(self, case_id: str, limit: int = 10) -> str:
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
                    # Use 'role' field from database schema.
                    # Values: "user" | "assistant" | "system" (enforced by the
                    # case_messages_role_check DB CHECK and MessageRole enum).
                    role = msg_dict.get("role", "system")
                    content = msg_dict.get("content", "")

                    if role == "user":
                        context_lines.append(f"{i}. [{timestamp}] User: {content}")
                    elif role == "assistant":
                        # Truncate long assistant responses
                        truncated = (
                            content[:200] + "..." if len(content) > 200 else content
                        )
                        context_lines.append(
                            f"{i}. [{timestamp}] Assistant: {truncated}"
                        )
                    elif role == "system":
                        context_lines.append(f"{i}. [{timestamp}] System: {content}")
                except Exception as e:
                    logger.warning(f"Failed to format message {i} in context: {e}")
                    continue

            if len(context_lines) > 1:  # More than just header
                context_lines.append("")  # Add spacing
                context_lines.append("Current query:")
                return "\n".join(context_lines)
            else:
                return ""

        except Exception as e:
            logger.warning(
                f"Failed to get conversation context for case {case_id}: {e}"
            )
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
                    metadata={"event_type": "case_resumed"},
                )

                await self.add_message_to_case(case_id, resume_message, session_id)
                logger.info(f"Resumed case {case_id} in session {session_id}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            logger.error(
                f"Failed to resume case {case_id} in session {session_id}: {e}"
            )
            return False

    @trace("case_service_hard_delete_case")
    async def hard_delete_case(
        self, case_id: str, user_id: Optional[str] = None
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
                    logger.warning(
                        f"User {user_id} denied delete access to case {case_id} (not owner)"
                    )
                    return False

            # Perform hard delete through repository
            # Note: Reports are automatically deleted via CASCADE FK constraint (TD-001: reports stored in PostgreSQL)
            success = await self.repository.delete(case_id)

            if success:
                logger.info(f"Hard deleted case {case_id}")

                # Cascade the polymorphic team-share rows (ADR-013 §D4). The
                # share table has no FK on its polymorphic target columns, so the
                # cascade is app-enforced here, mirroring the KB delete path. An
                # orphan share row is harmless to reads (the allowlist resolves
                # ids that then match no case), but leaving them is untidy and
                # would resurface if a case_id were ever reused.
                if self.share_repository:
                    try:
                        await self.share_repository.delete_for_resource("case", case_id)
                    except Exception as e:
                        logger.error(f"Failed to delete shares for case {case_id}: {e}")

                # Clean up Case Working Memory (delete vector store collection)
                if self.case_vector_store:
                    try:
                        await self.case_vector_store.delete_case_collection(case_id)
                        logger.info(
                            f"Deleted Working Memory collection for deleted case {case_id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to delete Working Memory for case {case_id}: {e}"
                        )
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
            logger.error(f"Failed to hard delete case {case_id}: {e}")
            # For idempotent behavior, return True even on error
            # The case might not exist or might already be deleted
            return True

    @trace("case_service_close_case")
    async def close_case(self, case_id: str, user_id: str) -> Case:
        """Close a case from the API surface (user-initiated, no chat gate).

        Routes through the engine's terminal executor
        (``execute_user_closure`` → ``_execute_closed_transition``) so the
        REST close and the chat-confirmed close share ONE closure rule:
        engine-derived closure_reason, ``closed_at`` stamped, action-history
        entry — all set atomically. The previous route mutated
        ``case.state`` directly, which the terminal-state validator rejects
        (#915).

        Owner-only write: shared-to-team readers can view a case but not
        close it, mirroring the delete posture. Denial surfaces as
        NotFoundError (404-not-403 — existence is not disclosed to
        non-owners).

        Wrapped in ``update_case_with_retry`` so a concurrent save reloads
        and re-applies the closure; the mutator re-checks terminal state on
        each fresh load, so a close that lost the race to another terminal
        transition surfaces as a conflict instead of silently re-closing.

        Raises:
            NotFoundError: Unknown case, or the caller is not the owner.
            ConflictError: Case is already resolved/closed.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            execute_user_closure,
        )
        from faultmaven.exceptions import ConflictError, NotFoundError
        from faultmaven.modules.case.exceptions import (
            CaseNotFoundError,
            StaleCaseException,
        )
        from faultmaven.modules.case.utils import update_case_with_retry

        case = await self.get_case(case_id, user_id)
        if not case or case.user_id != user_id:
            raise NotFoundError("Case", case_id)

        def _already_terminal(state: CaseState) -> ConflictError:
            return ConflictError(
                f"Case {case_id} is already {state.value}",
                resource_type="Case",
                resource_id=case_id,
                conflict_reason="already_closed",
            )

        if case.state.is_terminal:
            raise _already_terminal(case.state)

        async def apply(fresh: Case) -> None:
            if fresh.state.is_terminal:
                raise _already_terminal(fresh.state)
            execute_user_closure(fresh, user_id)

        # The retry helper's own exceptions subclass CaseException, which no
        # global handler maps — translate the two rare races to handled
        # types so they surface as 404/409, not 500.
        try:
            updated_case = await update_case_with_retry(self.repository, case_id, apply)
        except CaseNotFoundError:
            # Deleted between the pre-check and the retry's fresh load.
            raise NotFoundError("Case", case_id)
        except StaleCaseException:
            # Lost the version race max_attempts times; caller should
            # reload and re-decide (mirrors the turn route's OCC posture).
            raise ConflictError(
                f"Case {case_id} changed while closing; reload and retry",
                resource_type="Case",
                resource_id=case_id,
                conflict_reason="concurrent_update",
            )
        logger.info(
            f"Closed case {case_id} via API, " f"reason: {updated_case.closure_reason}"
        )
        return updated_case

    async def _resolve_user_team_ids(self, user_id: Optional[str]) -> List[str]:
        """The team ids a principal belongs to; ``[]`` when unwired or on error.

        Gateway for the read paths (allowlist + team facet) so a request that
        needs both resolves membership once. The share path checks membership
        via the shared :func:`~faultmaven.modules.auth.contracts.is_team_member`
        predicate instead, keeping it in lockstep with the KB team-publish
        surface.
        The resolver JOINs ``team_members`` through the RLS-tenanted ``teams``
        table (see ``PostgreSQLTeamRepository``), so under the caller's org RLS
        context it returns only teams in that org — the case/team org boundary is
        enforced here, not left to a distant policy. Fail-safe: an error yields
        ``[]`` (owner-only reads / no facet) rather than leaking or crashing.
        """
        if not user_id or not self.team_service:
            return []
        try:
            return await self.team_service.list_all_user_team_ids(user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to resolve team ids for {user_id}: {e}")
            return []

    @trace("case_service_list_user_cases")
    async def _resolve_shared_case_ids(
        self, user_id: Optional[str], team_ids: Optional[List[str]] = None
    ) -> List[str]:
        """Resolve the case ids shared to any of ``user_id``'s teams.

        The "shared-to-my-teams" arm of the case read allowlist (ADR-013 §D4 /
        ADR-011 D3), the case analogue of ``resolve_shared_kb_ids``. Returns
        ``[]`` — collapsing the allowlist to owner-only — when there is no share
        repository or the principal belongs to no teams. ``team_ids`` may be
        passed pre-resolved by a caller that already fetched membership (the
        team-filter path) to avoid a second lookup. Degrades gracefully on any
        resolution error: the owner arm still works, so a share-lookup failure
        narrows visibility (fail-closed) rather than leaking.
        """
        if not self.share_repository:
            return []
        if team_ids is None:
            team_ids = await self._resolve_user_team_ids(user_id)
        if not team_ids:
            return []
        try:
            return await self.share_repository.list_resource_ids(
                resource_type="case",
                scope_type="team",
                scope_ids=team_ids,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to resolve shared case ids for {user_id}: {e}")
            return []

    async def _share_case_with_team(
        self,
        *,
        case_id: str,
        team_id: str,
        organization_id: str,
        created_by: Optional[str] = None,
    ) -> None:
        """Record a team share for a case (idempotent) — the write counterpart of
        the U9 read allowlist and the source of truth for team visibility
        (ADR-013 §D4), mirroring the KB write path
        (``KnowledgeService._create_team_share``).

        No-ops when no share repository is wired. Cross-org sharing is
        structurally impossible: the share carries the case's own
        ``organization_id`` and the read allowlist only ever resolves teams the
        requester belongs to (within their RLS-isolated org), so a share to a
        foreign team is unreachable; RLS is the backstop.
        """
        if not self.share_repository:
            return
        await self.share_repository.share(
            resource_type="case",
            resource_id=case_id,
            scope_type="team",
            scope_id=team_id,
            organization_id=organization_id,
            created_by=created_by,
        )

    async def _auto_share_slack_case(self, case: Case) -> None:
        """Auto-share a Slack-originated case to its workspace Team (ADR-013 §D3).

        A Slack workspace maps to a Team, and the workspace's ``slack`` service
        account (the case owner) is a nominal member of that Team; sharing the
        case to every Team the owner belongs to makes the "visible to the whole
        team via Slack" promise real. Copilot cases don't take this path — they
        stay personal-until-shared.

        Inert in standalone: ``team_service`` is Cloud-only (unwired in
        single-tenant), so there is no workspace Team to resolve and the single
        local user already sees their own cases via the owner arm. Fail-safe — an
        auto-share error never blocks case creation, and *not* sharing is the safe
        direction (owner-only, never over-exposed); the share can be re-created.
        """
        if not self.team_service or not self.share_repository:
            return
        try:
            team_ids = await self.team_service.list_all_user_team_ids(case.user_id)
            if not team_ids:
                # Reached only in cloud (team_service is unwired in standalone),
                # where a Slack service account is expected to be a member of its
                # workspace Team. Zero teams means the case silently stays
                # owner-only instead of team-visible — surface it rather than
                # swallow, matching the account_kind path's posture.
                logger.warning(
                    "Slack case %s resolved no workspace Team; it stays "
                    "owner-only (Team membership misconfigured for owner %s)",
                    case.case_id,
                    case.user_id,
                )
                return
            for team_id in team_ids:
                await self._share_case_with_team(
                    case_id=case.case_id,
                    team_id=team_id,
                    organization_id=case.organization_id,
                    created_by=case.user_id,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Slack case auto-share failed for {case.case_id}: {e}")

    async def list_user_cases(
        self, user_id: str, filters: Optional[CaseListFilter] = None
    ) -> Tuple[List[CaseSummary], int]:
        """
        List cases for a user

        Args:
            user_id: User identifier
            filters: Optional filter criteria (include_empty, status, limit, offset, etc.)

        Returns:
            Tuple of (case summaries for the requested page, total match count).
            The total reflects every filter the repository applies (state,
            source, ``include_empty``, team scope) so the API can compute
            ``has_more`` soundly — it is NOT the length of the returned page.
        """
        if not user_id:
            raise ValidationException("User ID cannot be empty")

        try:
            # Get cases from repository
            status_filter = filters.state if filters else None
            source_filter = filters.source if filters else None
            team_filter = filters.team_id if filters else None
            # Pagination + include_empty are pushed into the repository query so
            # the returned page and the total count stay consistent (sound
            # pagination). Defaults mirror CaseListFilter when no filters given.
            limit = filters.limit if filters else 50
            offset = filters.offset if filters else 0
            include_empty = filters.include_empty if filters else True
            # Resolve team membership once for both the facet and the allowlist.
            team_ids = await self._resolve_user_team_ids(user_id)
            # Filter-by-team facet (ADR-013 §D4): narrow to one Team's shares.
            # Resolving to an empty set (not a member / standalone) means "no
            # matches" — short-circuit rather than issue a query that can't match.
            restrict_case_ids: Optional[List[str]] = None
            if team_filter:
                restrict_case_ids = await self._resolve_team_filter_case_ids(
                    user_id, team_filter, team_ids=team_ids
                )
                if not restrict_case_ids:
                    return [], 0
            # owned ∪ shared-to-my-teams (ADR-013 §D4). Resolved in SQL, not
            # post-filter: paginating the owner-only set and then adding shares
            # in Python would break the page/total contract.
            shared_case_ids = await self._resolve_shared_case_ids(
                user_id, team_ids=team_ids
            )
            cases_list, total = await self.repository.list(
                user_id=user_id,
                state=status_filter,
                source=source_filter,
                limit=limit,
                offset=offset,
                shared_case_ids=shared_case_ids,
                restrict_case_ids=restrict_case_ids,
                include_empty=include_empty,
            )

            # NOTE: include_empty is applied in the repository query (above), not
            # as a Python post-filter — a post-slice filter would drop rows from
            # an already-paginated page and disagree with ``total``.

            # Convert to CaseSummary
            from faultmaven.models.api_models import CaseSummary

            # DEBUG: Log the types we are working with
            if cases_list:
                logger.debug(
                    f"DEBUG_CASE_LIST: Found {len(cases_list)} cases. First type: {type(cases_list[0])}"
                )

            summaries = []
            for case in cases_list:
                try:
                    summaries.append(CaseSummary.from_case(case))
                except Exception as e:
                    logger.error(
                        f"Failed to convert case {case.case_id} to summary: {e}"
                    )
                    # Continue best effort? Or fail? Best effort for list

            # Enrich with team shares (ADR-013 §D4) in one batched query — empty
            # in standalone (team sharing unwired).
            await self._enrich_summaries_with_team_shares(summaries)

            if summaries:
                logger.debug(
                    f"DEBUG_CASE_SUMMARIES: Returning {len(summaries)} summaries. First type: {type(summaries[0])}"
                )

            return summaries, total

        except Exception as e:
            logger.error(f"Failed to list cases for user {user_id}: {e}")
            return [], 0

    async def list_all_cases(
        self, filters: Optional[CaseListFilter] = None
    ) -> Tuple[List[CaseSummary], int]:
        """List cases across ALL users/orgs (platform-admin cross-tenant read).

        Backs the platform-admin case view (ADR-012 D9). Unlike
        ``list_user_cases`` this passes ``user_id=None`` so the repository
        drops its per-user WHERE clause and returns every user's cases for the
        requested page, plus the total match count for pagination.
        Authorization, the metadata/content projection and the tenancy gate are
        all enforced at the API layer; this method must only be reached for an
        admin, and returns full summaries (titles included) in every deployment.
        In cloud/Postgres, Row-Level Security still scopes the result to the
        caller's org, which is why the API layer refuses this path under
        ``TENANT_PROVIDER=multi`` rather than serving a partial list.

        Repository errors propagate so the API surfaces a 5xx rather than
        masking a failure as an empty list (this is a diagnostic admin view).
        Per-case summary conversion is best-effort. ``include_empty`` is not
        honored here: filtering after the repository already paginated would
        make ``total_count`` and the page disagree, so the admin view lists
        every case (empties included) — the operator wants full visibility.

        Returns:
            Tuple of (case summaries for the page, total match count).
        """
        from faultmaven.models.api_models import CaseSummary

        status_filter = filters.state if filters else None
        source_filter = filters.source if filters else None
        limit = filters.limit if filters else 50
        offset = filters.offset if filters else 0

        cases_list, total = await self.repository.list(
            user_id=None,
            state=status_filter,
            limit=limit,
            offset=offset,
            source=source_filter,
        )

        summaries: List[CaseSummary] = []
        for case in cases_list:
            try:
                summaries.append(CaseSummary.from_case(case))
            except Exception as e:
                logger.error(f"Failed to convert case {case.case_id} to summary: {e}")

        return summaries, total

    @trace("case_service_search_cases")
    async def search_cases(
        self, search_request: CaseSearchRequest, user_id: Optional[str] = None
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
            # Resolve team membership once for both the facet and the allowlist.
            team_ids = await self._resolve_user_team_ids(user_id)
            # Filter-by-team facet (ADR-013 §D4): narrow to one Team's shares.
            # Empty resolution (not a member / standalone) → no matches.
            restrict_case_ids: Optional[List[str]] = None
            if search_request.team_id:
                restrict_case_ids = await self._resolve_team_filter_case_ids(
                    user_id, search_request.team_id, team_ids=team_ids
                )
                if not restrict_case_ids:
                    return []
            # Scope in the SQL query, not in Python: filtering after the
            # repository already applied its LIMIT would (a) drop the caller's
            # own matches when other users' cases fill the page, and (b) leak
            # every user's cases if user_id were ever falsy. Passing user_id
            # down adds `AND c.user_id = :user_id` to the WHERE clause; the
            # shared-case allowlist widens it to owned ∪ shared-to-my-teams.
            shared_case_ids = await self._resolve_shared_case_ids(
                user_id, team_ids=team_ids
            )
            cases_list, total = await self.repository.search(
                query=search_request.query,
                user_id=user_id,
                limit=search_request.limit,
                shared_case_ids=shared_case_ids,
                restrict_case_ids=restrict_case_ids,
            )

            # Convert to CaseSummary
            from faultmaven.models.api_models import CaseSummary

            summaries = [CaseSummary.from_case(case) for case in cases_list]

            # Enrich with team shares (ADR-013 §D4); empty in standalone.
            await self._enrich_summaries_with_team_shares(summaries)

            return summaries

        except Exception as e:
            logger.error(f"Failed to search cases: {e}")
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
            logger.error(f"Failed to get analytics for case {case_id}: {e}")
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
            cleaned_count = await self.repository.cleanup_expired(
                max_age_days=90, batch_size=100
            )

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} expired cases")

            return cleaned_count

        except Exception as e:
            logger.error(f"Failed to cleanup expired cases: {e}")
            return 0

    @trace("case_service_list_cases_by_session")
    async def list_cases_by_session(
        self, session_id: str, limit: int = 50, offset: int = 0
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
                    user_id = session_data.get("user_id")

            if not user_id:
                logger.warning(f"No user_id found for session {session_id}")
                return []

            # Step 2: Get user's cases (authorization via ownership)
            cases, _ = await self.repository.list(
                user_id=user_id, limit=limit, offset=offset
            )
            return cases

        except Exception as e:
            logger.error(f"Failed to list cases for session {session_id}: {e}")
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
                    user_id = session_data.get("user_id")

            if not user_id:
                return 0

            # Step 2: Count user's cases
            cases, total_count = await self.repository.list(
                user_id=user_id, limit=0, offset=0
            )
            return total_count

        except Exception as e:
            logger.error(f"Failed to count cases for session {session_id}: {e}")
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
                "max_cases_per_user": self.max_cases_per_user,
            }

        except Exception as e:
            logger.error(f"Failed to get case service health: {e}")
            return {"service_status": "unhealthy", "error": str(e)}

    # Message and Query Management Methods
    # Following design principles: delegate to case_store, proper error handling, interface compliance

    @trace("case_service_get_case_messages")
    async def get_case_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
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
            logger.info(
                f"Case {case_id} has {len(case.messages)} messages in case.messages array, message_count={case.message_count}"
            )

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
                    attachments=msg_dict.get("attachments"),
                )
                case_messages.append(case_msg)

            # Log for observability
            logger.debug(f"Retrieved {len(case_messages)} messages for case {case_id}")

            return case_messages

        except Exception as e:
            logger.error(f"Failed to get messages for case {case_id}: {e}")
            raise ServiceException(f"Failed to retrieve case messages: {str(e)}") from e

    @trace("case_service_add_case_query")
    async def add_case_query(
        self, case_id: str, query_text: str, user_id: Optional[str] = None
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
            # Create CaseMessage object
            # Per case-storage-design.md Section 4.7, use "created_at"
            msg = CaseMessage(
                message_id=f"msg_{uuid.uuid4().hex[:12]}",
                case_id=case_id,
                turn_number=0,  # Will be set correctly by add_message_to_case based on current turn
                role="user",
                # Note: message_type relies on dynamic assignment or fallback in add_message_to_case
                content=query_text.strip(),
                created_at=datetime.now(timezone.utc),
                author_id=user_id,
                metadata={},
            )
            # Explicitly set message_type attribute for add_message_to_case logic
            msg.message_type = "user_query"

            # Use centralized method which handles turn numbering, deduplication, and persistence
            success = await self.add_message_to_case(case_id, msg)

            if success:
                logger.debug(f"Added user query to case {case_id}")

            return success

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to add query to case {case_id}: {e}")
            raise ServiceException(f"Failed to add case query: {str(e)}") from e

    @trace("case_service_list_case_queries")
    async def list_case_queries(
        self, case_id: str, limit: int = 50, offset: int = 0
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
            all_messages = await self.get_case_messages(
                case_id, limit=limit + offset + 50, offset=0
            )

            # Filter for USER_QUERY messages only
            query_messages = [
                msg
                for msg in all_messages
                if msg.message_type == MessageType.USER_QUERY
            ]

            # Apply pagination to filtered results
            paginated_queries = query_messages[offset : offset + limit]

            # Convert to API format
            queries = []
            for msg in paginated_queries:
                query_dict = {
                    "query_id": msg.message_id,
                    "query_text": msg.content,
                    "created_at": to_json_compatible(msg.created_at),
                    "user_id": msg.author_id,
                    "metadata": msg.metadata or {},
                }
                queries.append(query_dict)

            logger.debug(f"Retrieved {len(queries)} queries for case {case_id}")
            return queries

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to list queries for case {case_id}: {e}")
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
                1 for msg in all_messages if msg.message_type == MessageType.USER_QUERY
            )

            logger.debug(f"Counted {query_count} queries for case {case_id}")
            return query_count

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to count queries for case {case_id}: {e}")
            return 0  # Graceful degradation for pagination

    @trace("case_service_count_user_cases")
    async def count_user_cases(
        self, user_id: str, filters: Optional[CaseListFilter] = None
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
            # list_user_cases returns the repository's true total match count
            # (all filters applied, before pagination) — use it directly rather
            # than counting a single page's rows.
            _, count = await self.list_user_cases(user_id, filters)

            logger.debug(f"Counted {count} cases for user {user_id}")
            return count

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to count cases for user {user_id}: {e}")
            return 0  # Graceful degradation for pagination

    @trace("case_service_get_query_result")
    async def get_query_result(
        self, case_id: str, query_id: str
    ) -> Optional[Dict[str, Any]]:
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
                if (
                    msg.message_id == query_id
                    and msg.message_type == MessageType.USER_QUERY
                ):
                    query_message = msg
                    query_index = i
                    break

            if not query_message:
                logger.debug(f"Query {query_id} not found in case {case_id}")
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
                        "response_id": msg.message_id,
                    }

                    logger.debug(f"Found query result for {query_id} in case {case_id}")
                    return response_dict

            # No agent response found after this query
            logger.debug(
                f"No agent response found for query {query_id} in case {case_id}"
            )
            return None

        except ValidationException:
            raise
        except Exception as e:
            logger.error(
                f"Failed to get query result for {query_id} in case {case_id}: {e}"
            )
            return None

    @trace("case_service_check_idempotency_key")
    async def check_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
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
            logger.debug(f"Checking idempotency key: {idempotency_key}")

            # For now, always return None (no previous result)
            # This means idempotency is disabled until we implement Redis storage
            return None

        except Exception as e:
            logger.error(f"Failed to check idempotency key {idempotency_key}: {e}")
            return None

    @trace("case_service_store_idempotency_result")
    async def store_idempotency_result(
        self,
        idempotency_key: str,
        status_code: int,
        content: Dict[str, Any],
        headers: Dict[str, str],
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

            logger.debug(
                f"Storing idempotency result for key {idempotency_key}: {status_code}"
            )

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
            logger.error(
                f"Failed to store idempotency result for {idempotency_key}: {e}"
            )
            return False

    @trace("case_service_get_case_messages_enhanced")
    async def get_case_messages_enhanced(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0,
        include_debug: bool = False,
    ) -> "CaseMessagesResponse":  # noqa: F821
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
        import time

        from faultmaven.models.api import (
            CaseMessagesResponse,
            Message,
            MessageRetrievalDebugInfo,
        )

        start_time = time.time()
        debug_info = None
        storage_errors = []
        message_parsing_errors = 0

        try:
            # Get all messages for the case first to calculate total count
            all_messages = await self.get_case_messages(case_id, limit=1000, offset=0)
            total_count = len(all_messages)

            # Apply pagination to the messages
            paginated_messages = all_messages[offset : offset + limit]
            retrieved_count = len(paginated_messages)

            # Convert CaseMessage objects to API Message format
            messages = []
            for case_msg in paginated_messages:
                try:
                    # CaseMessage already has 'role' field, use it directly
                    # No need to map from message_type (that field doesn't exist in CaseMessage)
                    role = case_msg.role if hasattr(case_msg, "role") else "system"

                    # Format created_at
                    created_at_str = None
                    if case_msg.created_at:
                        try:
                            if hasattr(case_msg.created_at, "isoformat"):
                                created_at_str = to_json_compatible(case_msg.created_at)
                            else:
                                created_at_str = str(case_msg.created_at)
                        except Exception as e:
                            logger.warning(
                                f"Failed to format created_at for message {case_msg.message_id}: {e}"
                            )
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
                        metadata=case_msg.metadata,
                    )
                    messages.append(api_message)

                except Exception as e:
                    message_parsing_errors += 1
                    logger.warning(
                        f"Failed to convert message {getattr(case_msg, 'message_id', 'unknown')}: {e}"
                    )
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
                    message_parsing_errors=message_parsing_errors,
                )

            # Determine if there are more messages
            has_more = (offset + retrieved_count) < total_count

            # Create and return response
            response = CaseMessagesResponse(
                messages=messages,
                total_count=total_count,
                retrieved_count=retrieved_count,
                has_more=has_more,
                debug_info=debug_info,
            )

            logger.debug(
                f"Retrieved {retrieved_count}/{total_count} messages for case {case_id} "
                f"in {processing_time_ms}ms"
            )

            return response

        except ValidationException:
            raise
        except Exception as e:
            logger.error(f"Failed to get enhanced messages for case {case_id}: {e}")
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
                    message_parsing_errors=0,
                )

            return CaseMessagesResponse(
                messages=[],
                total_count=0,
                retrieved_count=0,
                has_more=False,
                debug_info=debug_info,
            )

    # ============================================================
    # Case Team-Sharing Operations (ADR-013 §D4)
    # ============================================================
    #
    # Team sharing is the canonical case-visibility model: a case is visible to
    # its owner and to members of any Team it is shared to via ``resource_shares``
    # (the read counterpart is ``_resolve_shared_case_ids`` /
    # ``case_scope_where``). It replaced the pre-ADR-013 per-user participant
    # model (``share_case``/``unshare_case``/``get_case_participants``), which was
    # unreachable (no client), non-functional (its ``case_participants`` table +
    # ``upsert_case_participant`` SQL function never existed in the clean
    # baseline), and contradicted this model — retired here.
    #
    # All operations are Cloud-only: ``team_service`` is unwired in standalone
    # (single implicit team), so they raise a clear "not available" rather than
    # silently no-op.

    @trace("case_service_get_case_team_ids")
    async def get_case_team_ids(self, case_id: str) -> List[str]:
        """Team ids a single case is shared to (``[]`` when sharing is unwired).

        Used by the case-detail read path to populate ``CaseDetail.shared_team_ids``;
        delegates to the batched resolver (single-id map) so the team-scope
        projection lives in exactly one place.
        """
        team_map = await self._resolve_case_team_ids_map([case_id])
        return team_map.get(case_id, [])

    async def _resolve_case_team_ids_map(
        self, case_ids: List[str]
    ) -> Dict[str, List[str]]:
        """Batch ``case_id -> team ids`` for a page of cases (one query, no N+1).

        Gated on ``team_service`` — team visibility is a Cloud feature, so this
        skips the query entirely in standalone and every case gets ``[]``.
        """
        if not self.team_service or not self.share_repository or not case_ids:
            return {}
        shares_map = await self.share_repository.list_scopes_for_resources(
            "case", case_ids
        )
        return {
            cid: [s.scope_id for s in shares if s.scope_type == "team"]
            for cid, shares in shares_map.items()
        }

    async def _enrich_summaries_with_team_shares(
        self, summaries: List["CaseSummary"]
    ) -> None:
        """Populate ``shared_team_ids`` on a page of summaries in place.

        One batched share lookup for the whole page (no N+1); a no-op in
        standalone (``_resolve_case_team_ids_map`` returns ``{}``). Best-effort:
        an enrichment failure leaves ``shared_team_ids`` empty rather than
        failing the list.
        """
        if not summaries:
            return
        try:
            team_map = await self._resolve_case_team_ids_map(
                [s.case_id for s in summaries]
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to enrich cases with team shares: {e}")
            return
        for summary in summaries:
            summary.shared_team_ids = team_map.get(summary.case_id, [])

    async def _resolve_team_filter_case_ids(
        self,
        user_id: Optional[str],
        team_id: str,
        team_ids: Optional[List[str]] = None,
    ) -> List[str]:
        """Case ids shared to ``team_id`` for the filter-by-team facet.

        Returns ``[]`` (→ empty result) unless the caller is a member of
        ``team_id``: you can only filter by a Team you belong to, so probing an
        arbitrary team id surfaces nothing. Empty in standalone (no team_service).
        ``team_ids`` may be passed pre-resolved to avoid re-fetching membership.
        """
        if not user_id or not self.share_repository:
            return []
        if team_ids is None:
            team_ids = await self._resolve_user_team_ids(user_id)
        if team_id not in team_ids:
            return []
        return await self.share_repository.list_resource_ids(
            resource_type="case",
            scope_type="team",
            scope_ids=[team_id],
        )

    @trace("case_service_share_case_with_team")
    async def share_case_with_team(
        self, case_id: str, team_id: str, actor_user_id: str
    ) -> None:
        """Share a case with a Team (ADR-013 §D4), user-initiated.

        Only the case owner may share, and only with a Team they belong to. The
        membership check is what keeps the share within the case's org: it
        resolves through the RLS-tenanted ``teams`` table (via the shared
        ``is_team_member`` predicate), so under the owner's org RLS context
        membership contains only teams in that org — a foreign-org team is
        never a member and is rejected here, not merely masked at read time. The
        share row then carries the case's own ``organization_id``. Idempotent
        (re-sharing is a no-op).

        Raises:
            ValidationException: sharing unavailable (standalone), case missing,
                caller is not the owner, or caller is not a member of ``team_id``.
        """
        if not self.team_service or not self.share_repository:
            raise ValidationException(
                "Team sharing is not available in this deployment"
            )
        case = await self.repository.get(case_id)
        if not case:
            raise ValidationException(f"Case {case_id} not found")
        if case.user_id != actor_user_id:
            raise ValidationException("Only the case owner can share it with a team")
        if not await is_team_member(self.team_service, actor_user_id, team_id):
            raise ValidationException(
                "You can only share a case with a team you belong to"
            )
        await self._share_case_with_team(
            case_id=case_id,
            team_id=team_id,
            organization_id=case.organization_id,
            created_by=actor_user_id,
        )
        logger.info(
            "Case %s shared with team %s by %s", case_id, team_id, actor_user_id
        )

    @trace("case_service_unshare_case_from_team")
    async def unshare_case_from_team(
        self, case_id: str, team_id: str, actor_user_id: str
    ) -> bool:
        """Remove a case's share to a Team (ADR-013 §D4). Owner-only.

        Returns True if a share row was removed (False if it wasn't shared).

        Raises:
            ValidationException: sharing unavailable, case missing, or caller is
                not the owner.
        """
        if not self.team_service or not self.share_repository:
            raise ValidationException(
                "Team sharing is not available in this deployment"
            )
        case = await self.repository.get(case_id)
        if not case:
            raise ValidationException(f"Case {case_id} not found")
        if case.user_id != actor_user_id:
            raise ValidationException("Only the case owner can unshare it from a team")
        removed = await self.share_repository.unshare(
            resource_type="case",
            resource_id=case_id,
            scope_type="team",
            scope_id=team_id,
        )
        if removed:
            logger.info(
                "Case %s unshared from team %s by %s",
                case_id,
                team_id,
                actor_user_id,
            )
        return removed

"""Auth Session Service Module - Spec-Compliant (v2.0)

Purpose: Authentication session management ONLY (per case-and-session-concepts.md)

Core Responsibilities (Spec Lines 388-423):
- Session lifecycle management (create, validate, expire, delete)
- Multi-device support via client_id (spec lines 263-269)
- Session resumption for same client_id (spec lines 83-86)
- Session authentication and authorization
- Per-user session limit (oldest evicted); expiry is the store's TTL,
  re-armed by every write (SESSION_TIMEOUT_MINUTES, #2301)

NOT Responsible For (Spec Lines 102-107):
- Case management (cases are independent resources)
- Case history (cases manage their own history)
- Current case tracking (frontend UI state)
- Data uploads (belongs to cases)

Version: 2.0 (Spec-Compliant Refactor - 2025-10-23)
Reference: docs/architecture/case-and-session-concepts.md
"""

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import ValidationError

from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models import SessionContext
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)

#: The only ``SessionContext`` fields a client may set through
#: ``update_session`` (#1834). An allow-list, because the deny-list it replaced
#: left ``session_id`` writable, and ``save()`` keys its write on that field: a
#: caller could overwrite another session's record. ``expires_at`` is the
#: store's to set, on every write; ``client_id`` belongs to the resumption index.
CLIENT_UPDATABLE_SESSION_FIELDS = frozenset({"metadata"})

#: The 400 for a value of the wrong type, one fixed message per allowed field.
#: Keyed by field name, never by caller input, and never carrying pydantic's
#: text or the submitted value. Every allowed field needs an entry.
_FIELD_MESSAGES: Dict[str, str] = {
    "metadata": "metadata must be a JSON object",
}

#: The refusal names at most this many keys, each clipped to this many
#: characters: the keys are caller input, and unbounded they made the 400 as
#: large as the request body.
_MAX_NAMED_REFUSALS = 5
_MAX_REFUSED_NAME_CHARS = 64


def _refusal_message(refused: List[str]) -> str:
    """Name the first few refused keys, clipped, then how many there were."""
    named = []
    for key in refused[:_MAX_NAMED_REFUSALS]:
        shown = repr(key)
        if len(shown) > _MAX_REFUSED_NAME_CHARS:
            shown = shown[: _MAX_REFUSED_NAME_CHARS - 1] + "…"
        named.append(shown)
    if len(refused) > len(named):
        named.append("…")
    allowed = ", ".join(
        repr(field) for field in sorted(CLIENT_UPDATABLE_SESSION_FIELDS)
    )
    return (
        f"Session fields cannot be updated: {', '.join(named)} "
        f"({len(refused)} refused). Only {allowed} may be updated."
    )


class AuthSessionService:
    """Service for authentication session management (spec-compliant)

    Implements multi-device session support per spec lines 88-92, 213-246.
    Sessions provide AUTHENTICATION ONLY - no case management.
    """

    def __init__(
        self,
        session_store: Optional[Any] = None,
        settings: Optional[Any] = None,
        max_sessions_per_user: int = 10,
    ):
        """Initialize Auth Session Service

        A session's lifetime is the store's: every write through ``_persist``
        gives it the store's TTL (``SESSION_TIMEOUT_MINUTES``) and stamps its
        ``expires_at`` with the instant that lapses (#2301). This service holds
        no clock of its own, so nothing here can disagree with the TTL.

        Args:
            session_store: Session persistence store (ISessionStore interface)
            settings: Configuration settings
            max_sessions_per_user: Maximum concurrent sessions per user (multi-device)
        """
        self.session_store = session_store
        self._settings = settings

        # Use settings if available, otherwise use defaults
        if settings and hasattr(settings, "session"):
            self.max_sessions_per_user = getattr(
                settings.session, "max_sessions_per_user", max_sessions_per_user
            )
        else:
            self.max_sessions_per_user = max_sessions_per_user

        # Compatibility alias
        self.session_manager = self.session_store

    @property
    def session_ttl_seconds(self) -> Optional[int]:
        """The TTL the store gives every session write; None without a store."""
        return getattr(self.session_store, "session_ttl_seconds", None)

    # =========================================================================
    # Core Session Lifecycle Management
    # =========================================================================

    @trace("session_service_create_session")
    async def create_session(
        self,
        user_id: str,
        client_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        provider_session_id: Optional[str] = None,
    ) -> Tuple[SessionContext, bool]:
        """Create new session or resume existing session for (user, client) pair

        Implements session resumption per spec lines 83-86, 394-416.

        Args:
            user_id: User identifier (REQUIRED per spec)
            client_id: Client/device identifier for multi-device support (spec line 264)
            metadata: Additional session metadata (authentication context only)
            provider_session_id: The IdP session an SSO login opened, which
                logout ends. Server-set: no route passes a client's value.

        Returns:
            Tuple of (SessionContext, resumed: bool)

        Raises:
            ValidationException: If user_id is invalid
            ServiceException: If session creation fails
        """
        if not user_id or not user_id.strip():
            raise ValidationException("user_id is required for session creation")

        # Check for existing session for this (user_id, client_id) pair
        # Implements session resumption (spec lines 397-402)
        if client_id:
            existing_session = await self._find_session_by_user_and_client(
                user_id, client_id
            )
            if existing_session:
                # Resuming is activity: the write re-arms the session's TTL.
                now = datetime.now(timezone.utc)
                existing_session.last_activity = now
                existing_session.updated_at = now
                await self._persist(existing_session)
                existing_session.session_resumed = True
                logger.info(
                    f"Session resumed for user {user_id}, client {client_id}: {existing_session.session_id}"
                )
                return existing_session, True

        # Enforce max sessions per user (multi-device limit)
        await self._enforce_session_limit(user_id)

        # Create new session
        now = datetime.now(timezone.utc)
        session = SessionContext(
            session_id=str(uuid4()),
            user_id=user_id,
            client_id=client_id,
            session_resumed=False,
            created_at=now,
            last_activity=now,
            updated_at=now,
            metadata=metadata or {},
            provider_session_id=provider_session_id,
        )

        await self._persist(session)

        logger.info(f"Created new session for user {user_id}: {session.session_id}")
        return session, False

    @trace("session_service_get_session")
    async def get_session(
        self, session_id: str, validate: bool = True
    ) -> Optional[SessionContext]:
        """Get session by ID, optionally enforcing expiry (ISessionService contract)

        Args:
            session_id: Session identifier
            validate: When True (default), enforce expiry — an expired session is
                deleted and None is returned. When False, read the stored session
                as-is: no expiry check and no delete side effect. Readers that only
                want to observe session data (e.g. the heartbeat endpoint reporting
                last_activity) must not destroy a session as a side effect of
                reading it.

        Returns:
            SessionContext if found (and unexpired when validate=True), None otherwise
        """
        if not session_id or not session_id.strip():
            return None

        if not self.session_store:
            raise ServiceException("Session store not configured")

        session = await self.session_store.get_session(session_id)
        if not session:
            return None

        if not validate:
            # Non-validating read: return the stored session untouched.
            return session

        # Check if session is expired
        if session.expires_at and datetime.now(timezone.utc) > session.expires_at:
            logger.info(f"Session {session_id} has expired, deleting")
            await self.delete_session(session_id)
            return None

        return session

    @trace("session_service_validate_session")
    async def validate_session(self, session_id: str) -> bool:
        """Validate session is active and not expired

        Args:
            session_id: Session identifier

        Returns:
            True if session is valid and active
        """
        session = await self.get_session(session_id)
        return session is not None

    @trace("session_service_update_session")
    async def update_session(
        self,
        session_id: str,
        updates: Dict[str, Any],
    ) -> bool:
        """Update session metadata (authentication context only)

        Args:
            session_id: Session identifier
            updates: Fields to set; only those in CLIENT_UPDATABLE_SESSION_FIELDS

        Returns:
            True if update successful

        Raises:
            ValidationException: If updates name any other field, or a value
                fails SessionContext validation or is not representable as
                JSON. Nothing is applied either way.
            ServiceException: If the stored record names a session id other
                than the one it was read by. Nothing is written.
        """
        if not session_id or not session_id.strip():
            raise ValidationException("session_id cannot be empty")

        if not updates:
            raise ValidationException("updates cannot be empty")

        # An allow-list, checked before anything is read or applied: a body
        # with one permitted key and one refused key changes nothing.
        refused = sorted(set(updates) - CLIENT_UPDATABLE_SESSION_FIELDS)
        if refused:
            raise ValidationException(_refusal_message(refused))

        # NaN, ±Infinity (``1e400`` parses as inf) and lone surrogates pass
        # ``Dict[str, Any]``, are saved, and then every read of the session
        # answers 500 because no response can render them. Refuse them here.
        if "metadata" in updates:
            try:
                json.dumps(
                    updates["metadata"], allow_nan=False, ensure_ascii=False
                ).encode("utf-8")
            except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
                raise ValidationException(
                    "metadata must hold only JSON values"
                ) from None

        session = await self.get_session(session_id)
        if not session:
            return False

        # Re-validate the whole record so a wrong-typed value is refused, not
        # stored. model_validate, never model_copy(update=...): a copy skips
        # validation of the values it is handed. The message comes from
        # _FIELD_MESSAGES by the failing field's name, so neither pydantic's
        # text nor the submitted value reaches the caller.
        try:
            updated = SessionContext.model_validate({**session.model_dump(), **updates})
        except ValidationError as exc:
            errors = exc.errors()
            field = errors[0]["loc"][0] if errors and errors[0]["loc"] else None
            raise ValidationException(
                _FIELD_MESSAGES.get(field, "session update is invalid")
            ) from None

        # save() keys its write on the record's own session_id. A stored record
        # claiming another id would redirect this write onto that session: the
        # #1834 mechanism, reached from the stored side instead of the body.
        if updated.session_id != session_id:
            raise ServiceException(
                "Stored session record does not match the id it was read by; "
                "not updated"
            )

        updated.updated_at = datetime.now(timezone.utc)

        await self._persist(updated)

        return True

    @trace("session_service_update_last_activity")
    async def update_last_activity(self, session_id: str) -> bool:
        """Update session last activity timestamp

        Args:
            session_id: Session identifier

        Returns:
            True if updated successfully
        """
        session = await self.get_session(session_id)
        if not session:
            return False

        session.last_activity = datetime.now(timezone.utc)
        session.updated_at = session.last_activity

        await self._persist(session)

        return True

    @trace("session_service_delete_session")
    async def delete_session(self, session_id: str) -> bool:
        """Delete session (logout)

        Args:
            session_id: Session identifier

        Returns:
            True if deleted successfully
        """
        if not session_id or not session_id.strip():
            return False

        if not self.session_store:
            return False

        success = await self.session_store.delete_session(session_id)

        if success:
            logger.info(f"Deleted session {session_id}")

        return success

    # =========================================================================
    # Multi-User Session Management
    # =========================================================================

    @trace("session_service_get_user_sessions")
    async def get_user_sessions(self, user_id: str) -> List[SessionContext]:
        """Get all active sessions for a user (multi-device support)

        Implements spec lines 88-92: "Multiple concurrent sessions per user"

        Args:
            user_id: User identifier

        Returns:
            List of active sessions for user
        """
        if not user_id or not user_id.strip():
            return []

        if not self.session_store:
            return []

        return [
            s
            for s in await self.session_store.list_sessions(user_id)
            if await self._is_active(s)
        ]

    @trace("session_service_list_sessions")
    async def list_sessions(self, user_id: str) -> List[SessionContext]:
        """List one user's sessions

        There is no listing of every user's sessions: the store indexes
        sessions per user, and enumerating them across users is what #1447
        closed. A blank ``user_id`` names nobody and lists nothing.

        Args:
            user_id: User identifier

        Returns:
            The user's stored sessions, oldest first
        """
        if not user_id or not user_id.strip():
            return []

        if not self.session_store:
            return []

        return await self.session_store.list_sessions(user_id)

    # =========================================================================
    # Session Analytics and Monitoring
    # =========================================================================

    @trace("session_service_get_user_session_analytics")
    async def get_user_session_analytics(self, user_id: str) -> Dict[str, Any]:
        """Get session analytics for specific user

        Args:
            user_id: User identifier

        Returns:
            Analytics data for user's sessions
        """
        user_sessions = await self.get_user_sessions(user_id)

        return {
            "user_id": user_id,
            "active_sessions": len(user_sessions),
            "devices": [s.client_id for s in user_sessions if s.client_id],
            "sessions": [
                {
                    "session_id": s.session_id,
                    "client_id": s.client_id,
                    "created_at": to_json_compatible(s.created_at),
                    "last_activity": to_json_compatible(s.last_activity),
                    "expires_at": (
                        to_json_compatible(s.expires_at) if s.expires_at else None
                    ),
                }
                for s in user_sessions
            ],
        }

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    async def _find_session_by_user_and_client(
        self, user_id: str, client_id: str
    ) -> Optional[SessionContext]:
        """Find existing session for (user, client) pair for resumption

        Args:
            user_id: User identifier
            client_id: Client identifier

        Returns:
            Existing session if found and valid, None otherwise
        """
        if not self.session_store:
            return None

        # The (user, client) index is the one lookup; ISessionStore requires it.
        session_id = await self.session_store.find_by_user_and_client(
            user_id, client_id
        )
        if not session_id:
            return None
        return await self.get_session(session_id)

    async def _enforce_session_limit(self, user_id: str) -> None:
        """Make room for one more session within the per-user limit

        Deletes the user's least recently active sessions until fewer than
        ``max_sessions_per_user`` remain. Usually that is one session, but a
        user can already be over the limit: concurrent creates (nothing locks
        the count), a lowered limit, or older sessions that joined the index on
        a later write. Deleting one per create would never bring them back
        under it; this converges on the next create.

        Args:
            user_id: User identifier
        """
        user_sessions = await self.get_user_sessions(user_id)
        excess = len(user_sessions) - self.max_sessions_per_user + 1
        if excess <= 0:
            return

        by_activity = sorted(user_sessions, key=lambda s: s.last_activity)
        for session in by_activity[:excess]:
            await self.delete_session(session.session_id)
            logger.info(
                f"Deleted oldest session for user {user_id} to enforce limit: {session.session_id}"
            )

    async def _is_active(self, session: SessionContext) -> bool:
        """Whether ``session`` has not expired

        The TTL is the inactivity clock: a session with no write for
        ``SESSION_TIMEOUT_MINUTES`` is gone, so every session left is active.
        A second threshold on ``last_activity`` could only drop a live session
        whose TTL a metadata update or an archive re-armed, and leave it out of
        the per-user limit's count while it stays alive (#2301).

        Args:
            session: Session to check

        Returns:
            True if session is active
        """
        return not (
            session.expires_at and datetime.now(timezone.utc) > session.expires_at
        )

    async def _persist(self, session: SessionContext) -> None:
        """Write ``session``, which re-arms its TTL

        The store gives every session write its TTL (``SESSION_TIMEOUT_MINUTES``)
        and sets ``session.expires_at`` to the instant it lapses, so a session
        lives that long past its last write: a heartbeat, a resume, an update.
        The (user, client) resumption index is re-armed to the same instant:
        set once at creation, it lapsed under a session kept alive past it, and
        the next resume minted a second session instead.

        Args:
            session: The session to write; its ``expires_at`` is set
        """
        if not self.session_store:
            return
        await self.session_store.save(session)
        if session.client_id and session.expires_at:
            remaining = session.expires_at - datetime.now(timezone.utc)
            await self.session_store.index_session_by_client(
                session.user_id,
                session.client_id,
                session.session_id,
                max(1, math.ceil(remaining.total_seconds())),
            )

    # =========================================================================
    # Enhanced Session Operations (Microservices Parity)
    # =========================================================================

    @trace("session_service_search_sessions")
    async def search_sessions(
        self,
        user_id: str,
        query: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[SessionContext]:
        """Search user's sessions with filters

        Implements microservices parity endpoint from fm-session-service.

        Args:
            user_id: User identifier
            query: Optional text query (searches metadata)
            status: Optional status filter (active, archived, etc.)
            limit: Maximum number of results (default 50)

        Returns:
            List of matching sessions
        """
        # Get all user sessions
        all_sessions = await self.list_sessions(user_id=user_id)

        # Apply filters
        filtered = all_sessions

        # Status filter
        if status:
            filtered = [
                s for s in filtered if s.metadata.get("status", "active") == status
            ]

        # Text query filter (searches in metadata)
        if query:
            query_lower = query.lower()
            filtered = [
                s
                for s in filtered
                if any(
                    query_lower in str(v).lower()
                    for v in s.metadata.values()
                    if v is not None
                )
            ]

        # Apply limit
        return filtered[:limit]

    @trace("session_service_archive_session")
    async def archive_session(self, session_id: str) -> bool:
        """Archive a session by setting status to 'archived'

        Implements microservices parity endpoint from fm-session-service.
        Preserves all session data but marks as archived.

        Args:
            session_id: Session identifier

        Returns:
            True if archived successfully, False if session not found
        """
        session = await self.get_session(session_id)
        if not session:
            return False

        # Update status in metadata
        if not session.metadata:
            session.metadata = {}
        session.metadata["status"] = "archived"
        session.updated_at = datetime.now(timezone.utc)

        await self._persist(session)

        logger.info(f"Session {session_id} archived")
        return True

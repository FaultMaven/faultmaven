"""Domain model for user sessions.

This module defines the Session domain model for tracking user sessions
and their association with cases.

Usage:
    from faultmaven.modules.auth.domain.models.session import Session

    session = Session(
        session_id="550e8400-e29b-41d4-a716-446655440000",
        user_id="user_123",
        created_at=datetime.now(timezone.utc),
        last_accessed=datetime.now(timezone.utc),
    )
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class Session:
    """Domain model for user session.

    Represents a user's session in the system, enabling session-based
    case creation, retrieval, and context continuity across interactions.

    Attributes:
        session_id: Unique identifier for the session (UUID format)
        user_id: Identifier of the user who owns the session
        organization_id: Organization ID for multi-tenant isolation
        created_at: Timestamp when the session was created
        last_accessed: Timestamp of the last session activity
        expires_at: Optional expiration timestamp for the session
        metadata: Optional dictionary for session context data
    """

    session_id: str
    user_id: str
    organization_id: str = (
        "00000000-0000-0000-0000-000000000001"  # Default org for single-tenant mode
    )
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    def is_expired(self) -> bool:
        """Check if session has expired.

        Returns:
            True if session has expired, False otherwise.
            Sessions without expires_at never expire.
        """
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    def is_active(self) -> bool:
        """Check if session is active.

        Returns:
            True if session is active (not expired), False otherwise.
        """
        return not self.is_expired()

    def touch(self) -> None:
        """Update last_accessed to current time."""
        self.last_accessed = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return (
            f"Session(session_id={self.session_id!r}, "
            f"user_id={self.user_id!r}, "
            f"is_active={self.is_active()})"
        )

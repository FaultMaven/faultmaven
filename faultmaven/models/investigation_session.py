"""Investigation Session domain model.

Represents a continuous investigation period within a case, including
multiple agent executions, user interactions, and state management.
Sessions provide temporal structure to case investigations.

Design Reference: TASK-008 Investigation Session Repository Pattern
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class SessionStatus(str, Enum):
    """Investigation session status.

    Tracks the lifecycle of an investigation session from active to completion.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


@dataclass
class InvestigationSession:
    """Investigation session within a case.

    Represents a continuous investigation period with multiple agent
    executions, user interactions, and state management.

    Attributes:
        session_id: Unique identifier for the session
        case_id: Case this session belongs to
        user_id: User who owns this session
        organization_id: Organization that owns this session
        status: Current session status (active, paused, completed, abandoned)
        started_at: When the session started
        ended_at: When the session ended (for completed/abandoned sessions)
        last_activity_at: Timestamp of last activity in the session
        total_duration_ms: Total session duration in milliseconds
        session_goal: Optional goal for this investigation session
        findings_summary: Summary of findings (for completed sessions)
        total_token_usage: Total tokens used across all executions
        total_agent_executions: Count of agent executions in this session
        token_budget_limit: Optional spending limit for tokens
        metadata: Additional session-specific metadata (JSON)
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    session_id: str
    case_id: str
    user_id: str
    organization_id: str
    status: SessionStatus = SessionStatus.ACTIVE
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_duration_ms: Optional[int] = None
    session_goal: Optional[str] = None
    findings_summary: Optional[str] = None
    total_token_usage: int = 0
    total_agent_executions: int = 0
    token_budget_limit: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate investigation session data."""
        if not self.session_id:
            raise ValueError("session_id is required")
        if not self.case_id:
            raise ValueError("case_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.organization_id:
            raise ValueError("organization_id is required")
        if self.total_token_usage < 0:
            raise ValueError("total_token_usage cannot be negative")
        if self.total_agent_executions < 0:
            raise ValueError("total_agent_executions cannot be negative")
        if self.token_budget_limit is not None and self.token_budget_limit < 0:
            raise ValueError("token_budget_limit cannot be negative")
        if self.total_duration_ms is not None and self.total_duration_ms < 0:
            raise ValueError("total_duration_ms cannot be negative")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at must be after started_at")

    def pause(self) -> None:
        """Pause the session.

        Transitions from ACTIVE to PAUSED status and updates duration.

        Raises:
            ValueError: If session is not in ACTIVE status.
        """
        if self.status != SessionStatus.ACTIVE:
            raise ValueError(
                f"Cannot pause session in {self.status.value} status. "
                "Only active sessions can be paused."
            )
        self.status = SessionStatus.PAUSED
        self._update_duration()
        self.updated_at = datetime.now(timezone.utc)

    def resume(self) -> None:
        """Resume a paused session.

        Transitions from PAUSED to ACTIVE status.

        Raises:
            ValueError: If session is not in PAUSED status.
        """
        if self.status != SessionStatus.PAUSED:
            raise ValueError(
                f"Cannot resume session in {self.status.value} status. "
                "Only paused sessions can be resumed."
            )
        self.status = SessionStatus.ACTIVE
        self.last_activity_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def complete(self, findings_summary: str) -> None:
        """Mark session as completed with findings.

        Transitions to COMPLETED status and records findings.

        Args:
            findings_summary: Summary of investigation findings.

        Raises:
            ValueError: If session is already in a terminal status.
        """
        if self.status in (SessionStatus.COMPLETED, SessionStatus.ABANDONED):
            raise ValueError(
                f"Cannot complete session in {self.status.value} status. "
                "Session is already in a terminal state."
            )
        self.status = SessionStatus.COMPLETED
        self.findings_summary = findings_summary
        self.ended_at = datetime.now(timezone.utc)
        self._update_duration()
        self.updated_at = datetime.now(timezone.utc)

    def abandon(self) -> None:
        """Mark session as abandoned.

        Transitions to ABANDONED status without requiring findings.

        Raises:
            ValueError: If session is already in a terminal status.
        """
        if self.status in (SessionStatus.COMPLETED, SessionStatus.ABANDONED):
            raise ValueError(
                f"Cannot abandon session in {self.status.value} status. "
                "Session is already in a terminal state."
            )
        self.status = SessionStatus.ABANDONED
        self.ended_at = datetime.now(timezone.utc)
        self._update_duration()
        self.updated_at = datetime.now(timezone.utc)

    def add_agent_execution(self, token_usage: int) -> None:
        """Record an agent execution and update token usage.

        Args:
            token_usage: Number of tokens used in the execution.

        Raises:
            ValueError: If token_usage is negative.
            ValueError: If session is not active.
        """
        if token_usage < 0:
            raise ValueError("token_usage cannot be negative")
        if not self.is_active():
            raise ValueError(
                f"Cannot add execution to session in {self.status.value} status. "
                "Session must be active."
            )
        self.total_token_usage += token_usage
        self.total_agent_executions += 1
        self.last_activity_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def is_active(self) -> bool:
        """Check if session is currently active.

        Returns:
            True if session status is ACTIVE.
        """
        return self.status == SessionStatus.ACTIVE

    def is_paused(self) -> bool:
        """Check if session is currently paused.

        Returns:
            True if session status is PAUSED.
        """
        return self.status == SessionStatus.PAUSED

    def is_completed(self) -> bool:
        """Check if session is in a terminal state.

        Returns:
            True if session status is COMPLETED or ABANDONED.
        """
        return self.status in (SessionStatus.COMPLETED, SessionStatus.ABANDONED)

    def is_over_budget(self) -> bool:
        """Check if session has exceeded token budget.

        Returns:
            True if token_budget_limit is set and total_token_usage exceeds it.
            False if no budget limit is set or usage is within budget.
        """
        if self.token_budget_limit is None:
            return False
        return self.total_token_usage > self.token_budget_limit

    def get_remaining_budget(self) -> Optional[int]:
        """Get remaining token budget.

        Returns:
            Remaining tokens if budget is set, None otherwise.
            Can be negative if over budget.
        """
        if self.token_budget_limit is None:
            return None
        return self.token_budget_limit - self.total_token_usage

    def get_budget_usage_percent(self) -> Optional[float]:
        """Get budget usage as a percentage.

        Returns:
            Percentage of budget used (0-100+), None if no budget set.
        """
        if self.token_budget_limit is None or self.token_budget_limit == 0:
            return None
        return (self.total_token_usage / self.token_budget_limit) * 100

    def get_duration_display(self) -> str:
        """Get human-readable session duration.

        Returns:
            Formatted duration string (e.g., "2h 15m 30s" or "45m 12s").
        """
        if self.total_duration_ms is None:
            # Calculate current duration for active sessions
            now = datetime.now(timezone.utc)
            duration_ms = int((now - self.started_at).total_seconds() * 1000)
        else:
            duration_ms = self.total_duration_ms

        if duration_ms < 0:
            return "0s"

        total_seconds = duration_ms // 1000
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0 or not parts:
            parts.append(f"{seconds}s")

        return " ".join(parts)

    def _update_duration(self) -> None:
        """Update total_duration_ms based on timestamps."""
        if self.ended_at is not None:
            end_time = self.ended_at
        else:
            end_time = datetime.now(timezone.utc)

        duration = end_time - self.started_at
        self.total_duration_ms = int(duration.total_seconds() * 1000)

    def touch(self) -> None:
        """Update the updated_at timestamp to current time."""
        self.updated_at = datetime.now(timezone.utc)

    def update_last_activity(self) -> None:
        """Update the last_activity_at timestamp to current time."""
        self.last_activity_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return (
            f"InvestigationSession(session_id={self.session_id!r}, "
            f"case_id={self.case_id!r}, "
            f"status={self.status.value!r}, "
            f"total_agent_executions={self.total_agent_executions})"
        )

"""Agent module exceptions.

This module defines exceptions specific to the agent module,
providing a hierarchical exception structure for agent operations.
"""

from typing import Any, Dict, Optional

from faultmaven.exceptions import AgentException, ServiceError


class AgentOrchestrationError(AgentException):
    """Raised when agent orchestration fails.

    This exception is raised when the agent cannot coordinate
    multiple tools or services to complete a task.
    """

    def __init__(
        self,
        message: str,
        phase: Optional[str] = None,
        tool_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.phase = phase
        self.tool_name = tool_name
        super().__init__(
            message, details={**(details or {}), "phase": phase, "tool_name": tool_name}
        )


class ToolExecutionError(AgentException):
    """Raised when a tool execution fails.

    This exception is raised when a specific tool (e.g., knowledge search,
    web search) fails to execute properly.
    """

    def __init__(
        self,
        message: str,
        tool_name: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.tool_name = tool_name
        self.error_code = error_code
        super().__init__(
            message,
            details={
                **(details or {}),
                "tool_name": tool_name,
                "error_code": error_code,
            },
        )


class InvestigationError(AgentException):
    """Raised when an investigation operation fails.

    This exception is raised when the investigation workflow
    encounters an unrecoverable error.
    """

    def __init__(
        self,
        message: str,
        session_id: Optional[str] = None,
        phase: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.session_id = session_id
        self.phase = phase
        super().__init__(
            message,
            details={**(details or {}), "session_id": session_id, "phase": phase},
        )


class CaseActionError(AgentException):
    """Raised when a case action (phase transition or disposition) validation fails.

    This exception is raised when the investigation cannot
    perform a case action due to validation failures.
    """

    def __init__(
        self,
        message: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        super().__init__(
            message,
            details={
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
            },
        )


# Backward compatibility alias
PhaseTransitionError = CaseActionError


class AgentTimeoutError(AgentException):
    """Raised when agent operation times out."""

    def __init__(
        self,
        message: str = "Agent operation timed out",
        timeout_seconds: Optional[float] = None,
    ):
        self.timeout_seconds = timeout_seconds
        super().__init__(message, details={"timeout_seconds": timeout_seconds})

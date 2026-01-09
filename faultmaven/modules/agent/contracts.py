"""
Agent Module Public Contracts

This file defines the public interface of the agent module.
Other modules MUST only import from this file, never from
domain/ or infrastructure/ directories.

Contracts include:
- DTOs: Data transfer objects for cross-module communication
- Protocols: Interface definitions for dependency injection
- Exceptions: Re-exported domain exceptions
- Enums: Public enumerations needed by other modules
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ============================================
# Public Enumerations
# ============================================


class ExecutionStatusDTO(str, Enum):
    """Public representation of agent execution status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class AgentTypeDTO(str, Enum):
    """Public representation of agent types."""

    INVESTIGATOR = "investigator"
    DEBUGGER = "debugger"
    RESEARCHER = "researcher"
    VALIDATOR = "validator"
    REPORTER = "reporter"
    CUSTOM = "custom"


# ============================================
# Data Transfer Objects (DTOs)
# ============================================


@dataclass(frozen=True)
class ToolCallDTO:
    """Public representation of a tool call.

    Represents a single tool invocation during agent execution.
    """

    tool_call_id: str
    execution_id: str
    tool_name: str
    status: str  # "pending", "running", "success", "failed"
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


@dataclass(frozen=True)
class AgentExecutionDTO:
    """Public representation of an agent execution.

    This is the cross-module representation of an agent execution.
    Contains execution metadata and results.
    """

    execution_id: str
    case_id: str
    agent_type: AgentTypeDTO
    agent_model: str
    status: ExecutionStatusDTO
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_duration_ms: Optional[int] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    error_message: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None
    tool_call_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class AgentExecutionSummaryDTO:
    """Lightweight execution summary for list views.

    Contains minimal information for efficient listing.
    """

    execution_id: str
    case_id: str
    agent_type: AgentTypeDTO
    status: ExecutionStatusDTO
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_duration_ms: Optional[int] = None


@dataclass(frozen=True)
class AgentExecutionRequestDTO:
    """Request to execute an agent.

    Contains parameters for starting an agent execution.
    """

    case_id: str
    agent_type: AgentTypeDTO
    prompt: str
    agent_model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class AgentExecutionResultDTO:
    """Result of agent execution.

    Contains the final result of an agent execution.
    """

    execution_id: str
    status: ExecutionStatusDTO
    response: Optional[str] = None
    error_message: Optional[str] = None
    tool_calls: tuple[ToolCallDTO, ...] = field(default_factory=tuple)
    execution_duration_ms: Optional[int] = None
    token_usage: Optional[Dict[str, int]] = None


# ============================================
# Service Protocols (Interfaces)
# ============================================


@runtime_checkable
class IAgentQuery(Protocol):
    """Read-only agent operations for cross-module use.

    Provides read access to agent execution data for other modules.
    """

    async def get_execution(self, execution_id: str) -> Optional[AgentExecutionDTO]:
        """Get agent execution by ID.

        Args:
            execution_id: The execution's unique identifier

        Returns:
            AgentExecutionDTO if found, None otherwise
        """
        ...

    async def get_executions_by_ids(
        self, execution_ids: list[str]
    ) -> list[AgentExecutionDTO]:
        """Bulk execution lookup - prevents N+1 queries.

        Args:
            execution_ids: List of execution IDs to fetch

        Returns:
            List of AgentExecutionDTO objects (may be fewer than requested)
        """
        ...

    async def list_executions_by_case(
        self,
        case_id: str,
        agent_type: Optional[AgentTypeDTO] = None,
        status: Optional[ExecutionStatusDTO] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentExecutionSummaryDTO]:
        """List agent executions for a case with optional filtering.

        Args:
            case_id: The case's unique identifier
            agent_type: Optional agent type filter
            status: Optional status filter
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of AgentExecutionSummaryDTO objects
        """
        ...

    async def get_tool_calls(self, execution_id: str) -> list[ToolCallDTO]:
        """Get tool calls for an execution.

        Args:
            execution_id: The execution's unique identifier

        Returns:
            List of ToolCallDTO objects
        """
        ...

    async def get_latest_execution(
        self, case_id: str, agent_type: Optional[AgentTypeDTO] = None
    ) -> Optional[AgentExecutionDTO]:
        """Get the latest execution for a case.

        Args:
            case_id: The case's unique identifier
            agent_type: Optional agent type filter

        Returns:
            AgentExecutionDTO if found, None otherwise
        """
        ...


@runtime_checkable
class IAgentCommand(Protocol):
    """Write operations for agent execution.

    Provides write access to agent execution. Use with caution from other modules.
    """

    async def execute(
        self, request: AgentExecutionRequestDTO
    ) -> AgentExecutionResultDTO:
        """Execute an agent.

        Args:
            request: Execution request parameters

        Returns:
            AgentExecutionResultDTO with execution results
        """
        ...

    async def cancel(self, execution_id: str) -> bool:
        """Cancel a running execution.

        Args:
            execution_id: The execution's unique identifier

        Returns:
            True if cancelled, False if not found or already completed
        """
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from module exceptions
from faultmaven.modules.agent.exceptions import (
    AgentOrchestrationError,
    AgentTimeoutError,
    InvestigationError,
    PhaseTransitionError,
    ToolExecutionError,
)

# Import base AgentException from central exceptions
from faultmaven.exceptions import AgentException

__all__ = [
    # Enums
    "ExecutionStatusDTO",
    "AgentTypeDTO",
    # DTOs
    "ToolCallDTO",
    "AgentExecutionDTO",
    "AgentExecutionSummaryDTO",
    "AgentExecutionRequestDTO",
    "AgentExecutionResultDTO",
    # Protocols
    "IAgentQuery",
    "IAgentCommand",
    # Exceptions
    "AgentException",
    "AgentOrchestrationError",
    "ToolExecutionError",
    "InvestigationError",
    "PhaseTransitionError",
    "AgentTimeoutError",
]

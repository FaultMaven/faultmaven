"""Agent Execution Domain Models - Owned by Case Module.

Per module-organization-design.md:
- Case module owns agent execution audit data (agent_tool_calls is case audit data)
- Agent module is a Domain Service that operates on Case-owned data
- These models are canonical and should be imported from Case contracts

Represents a single execution of an AI agent for a case, including
tool calls made during execution. Enables debugging, transparency,
and audit compliance for agent behavior.

Design Reference: TASK-007 Agent Execution Repository Pattern
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ExecutionStatus(str, Enum):
    """Agent execution status.

    Tracks the lifecycle of an agent execution from queued to completion.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class AgentType(str, Enum):
    """Types of AI agents.

    Categorizes agents by their primary function in the investigation process.
    """

    INVESTIGATOR = "investigator"  # Root cause analysis
    DEBUGGER = "debugger"  # Code debugging
    RESEARCHER = "researcher"  # Information gathering
    VALIDATOR = "validator"  # Hypothesis validation
    REPORTER = "reporter"  # Report generation
    CUSTOM = "custom"  # User-defined agents


@dataclass
class AgentToolCall:
    """Tool call made during agent execution.

    Represents a single tool invocation (e.g., web_search, code_exec, file_read).

    Attributes:
        tool_call_id: Unique identifier
        execution_id: Parent execution this tool call belongs to
        tool_name: Name of the tool (web_search, code_exec, etc.)
        tool_input: Input parameters passed to the tool (JSON)
        tool_output: Output returned by the tool (JSON)
        status: Tool call status (pending, running, success, failed)
        error_message: Error message if tool call failed
        started_at: When tool call started
        completed_at: When tool call completed
        duration_ms: Tool call duration in milliseconds
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    tool_call_id: str
    execution_id: str
    tool_name: str
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[Dict[str, Any]] = None
    status: str = "pending"  # pending, running, success, failed
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate tool call data."""
        if not self.tool_call_id:
            raise ValueError("tool_call_id is required")
        if not self.execution_id:
            raise ValueError("execution_id is required")
        if not self.tool_name:
            raise ValueError("tool_name is required")
        if self.status not in ("pending", "running", "success", "failed"):
            raise ValueError(
                f"Invalid status: {self.status}. "
                "Must be one of: pending, running, success, failed"
            )
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms cannot be negative")

    def mark_started(self) -> None:
        """Mark tool call as started."""
        self.status = "running"
        self.started_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def mark_success(self, output: Dict[str, Any]) -> None:
        """Mark tool call as successful."""
        self.status = "success"
        self.tool_output = output
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_failed(self, error_message: str) -> None:
        """Mark tool call as failed."""
        self.status = "failed"
        self.error_message = error_message
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def _calculate_duration(self) -> None:
        """Calculate tool call duration."""
        if self.started_at and self.completed_at:
            duration = self.completed_at - self.started_at
            self.duration_ms = int(duration.total_seconds() * 1000)

    def is_completed(self) -> bool:
        """Check if tool call is in a terminal state."""
        return self.status in ("success", "failed")

    def is_running(self) -> bool:
        """Check if tool call is currently running."""
        return self.status == "running"

    def __repr__(self) -> str:
        return (
            f"AgentToolCall(tool_call_id={self.tool_call_id!r}, "
            f"tool_name={self.tool_name!r}, "
            f"status={self.status!r})"
        )


@dataclass
class AgentExecution:
    """Agent execution for a case.

    Represents a single execution of an AI agent, tracking the full lifecycle
    from queued to completion or failure.

    Attributes:
        execution_id: Unique identifier
        case_id: Case this execution belongs to
        agent_type: Type of agent (investigator, debugger, etc.)
        agent_model: LLM model used (e.g., "gpt-4", "claude-3-opus")
        status: Current execution status
        started_at: When execution started
        completed_at: When execution completed (success or failure)
        execution_duration_ms: Total execution time in milliseconds
        prompt: Prompt sent to the agent
        response: Agent's response
        error_message: Error message if execution failed
        token_usage: Token usage statistics (prompt_tokens, completion_tokens, total_tokens)
        tool_calls: List of tool calls made during execution
        metadata: Additional execution-specific metadata (JSON)
        created_at: Record creation timestamp
        updated_at: Record update timestamp
    """

    execution_id: str
    case_id: str
    agent_type: AgentType
    agent_model: str
    status: ExecutionStatus = ExecutionStatus.QUEUED
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_duration_ms: Optional[int] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    error_message: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None
    tool_calls: List[AgentToolCall] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate agent execution data."""
        if not self.execution_id:
            raise ValueError("execution_id is required")
        if not self.case_id:
            raise ValueError("case_id is required")
        if not self.agent_model:
            raise ValueError("agent_model is required")
        if self.execution_duration_ms is not None and self.execution_duration_ms < 0:
            raise ValueError("execution_duration_ms cannot be negative")

    def is_completed(self) -> bool:
        """Check if execution is in a terminal state."""
        return self.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        )

    def is_running(self) -> bool:
        """Check if execution is currently running."""
        return self.status == ExecutionStatus.RUNNING

    def is_queued(self) -> bool:
        """Check if execution is queued."""
        return self.status == ExecutionStatus.QUEUED

    def mark_started(self) -> None:
        """Mark execution as started."""
        self.status = ExecutionStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def mark_completed(self, response: str) -> None:
        """Mark execution as successfully completed."""
        self.status = ExecutionStatus.COMPLETED
        self.completed_at = datetime.now(timezone.utc)
        self.response = response
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_failed(self, error_message: str) -> None:
        """Mark execution as failed."""
        self.status = ExecutionStatus.FAILED
        self.completed_at = datetime.now(timezone.utc)
        self.error_message = error_message
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_cancelled(self) -> None:
        """Mark execution as cancelled."""
        self.status = ExecutionStatus.CANCELLED
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def mark_timeout(self) -> None:
        """Mark execution as timed out."""
        self.status = ExecutionStatus.TIMEOUT
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self._calculate_duration()

    def _calculate_duration(self) -> None:
        """Calculate execution duration if both timestamps are set."""
        if self.started_at and self.completed_at:
            duration = self.completed_at - self.started_at
            self.execution_duration_ms = int(duration.total_seconds() * 1000)

    def add_tool_call(self, tool_call: AgentToolCall) -> None:
        """Add a tool call to this execution."""
        self.tool_calls.append(tool_call)
        self.updated_at = datetime.now(timezone.utc)

    def get_total_tokens(self) -> int:
        """Get total token usage."""
        if self.token_usage:
            return self.token_usage.get("total_tokens", 0)
        return 0

    def get_prompt_tokens(self) -> int:
        """Get prompt token usage."""
        if self.token_usage:
            return self.token_usage.get("prompt_tokens", 0)
        return 0

    def get_completion_tokens(self) -> int:
        """Get completion token usage."""
        if self.token_usage:
            return self.token_usage.get("completion_tokens", 0)
        return 0

    def set_token_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: Optional[int] = None,
    ) -> None:
        """Set token usage statistics."""
        self.token_usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
        }
        self.updated_at = datetime.now(timezone.utc)

    def get_failed_tool_calls(self) -> List[AgentToolCall]:
        """Get list of failed tool calls."""
        return [tc for tc in self.tool_calls if tc.status == "failed"]

    def get_successful_tool_calls(self) -> List[AgentToolCall]:
        """Get list of successful tool calls."""
        return [tc for tc in self.tool_calls if tc.status == "success"]

    def get_tool_call_count(self) -> int:
        """Get total number of tool calls."""
        return len(self.tool_calls)

    def get_tool_call_duration_total_ms(self) -> int:
        """Get total duration of all tool calls in milliseconds."""
        total = 0
        for tc in self.tool_calls:
            if tc.duration_ms:
                total += tc.duration_ms
        return total

    def touch(self) -> None:
        """Update the updated_at timestamp to current time."""
        self.updated_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return (
            f"AgentExecution(execution_id={self.execution_id!r}, "
            f"case_id={self.case_id!r}, "
            f"agent_type={self.agent_type.value!r}, "
            f"status={self.status.value!r})"
        )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Enumerations
    "ExecutionStatus",
    "AgentType",
    # Domain Models
    "AgentToolCall",
    "AgentExecution",
]

"""Domain Events for Agent Orchestration (TASK-015)

This module defines the event types and data structures used during
agent execution for streaming responses and event-driven communication.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ExecutionEventType(str, Enum):
    """Types of execution events emitted during agent processing.

    These events are used for streaming real-time updates to clients
    during agent execution workflows.
    """

    STARTED = "started"  # Execution has started
    THINKING = "thinking"  # Agent is reasoning/processing
    TOOL_CALL = "tool_call"  # Tool invocation requested
    TOOL_RESULT = "tool_result"  # Tool execution completed
    RESPONSE = "response"  # Incremental response chunk
    ERROR = "error"  # Error occurred during execution
    COMPLETED = "completed"  # Execution finished successfully


class LLMEventType(str, Enum):
    """Types of events from LLM streaming responses.

    These low-level events map to LLM API streaming chunks.
    """

    TEXT_CHUNK = "text_chunk"  # Incremental text from LLM
    TOOL_USE = "tool_use"  # Tool call requested by LLM
    TOOL_RESULT = "tool_result"  # Tool result to send back
    THINKING = "thinking"  # Extended thinking/reasoning content
    COMPLETION = "completion"  # Stream completed
    ERROR = "error"  # LLM error occurred


class MessageRole(str, Enum):
    """Roles for conversation messages.

    Standard roles for LLM conversation history.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ExecutionEvent:
    """Event emitted during agent execution for streaming.

    These events are yielded by the agent orchestration service to provide
    real-time updates about execution progress, including reasoning,
    tool calls, and response generation.

    Attributes:
        event_type: Type of execution event
        content: Text content of the event (may be partial for streaming)
        metadata: Additional event metadata (tool info, error details, etc.)
        timestamp: When the event was created
        event_id: Optional unique identifier for the event
        execution_id: ID of the parent agent execution
    """

    event_type: ExecutionEventType
    content: str
    metadata: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str | None = None
    execution_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "event_type": self.event_type.value,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
            "event_id": self.event_id,
            "execution_id": self.execution_id,
        }

    @classmethod
    def started(
        cls,
        execution_id: str,
        message: str = "Execution started",
        metadata: dict[str, Any] | None = None,
    ) -> "ExecutionEvent":
        """Factory method for started event."""
        return cls(
            event_type=ExecutionEventType.STARTED,
            content=message,
            metadata=metadata,
            execution_id=execution_id,
        )

    @classmethod
    def thinking(
        cls,
        content: str,
        execution_id: str | None = None,
    ) -> "ExecutionEvent":
        """Factory method for thinking event."""
        return cls(
            event_type=ExecutionEventType.THINKING,
            content=content,
            execution_id=execution_id,
        )

    @classmethod
    def tool_call(
        cls,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_call_id: str,
        execution_id: str | None = None,
    ) -> "ExecutionEvent":
        """Factory method for tool call event."""
        return cls(
            event_type=ExecutionEventType.TOOL_CALL,
            content=f"Calling tool: {tool_name}",
            metadata={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_call_id": tool_call_id,
            },
            execution_id=execution_id,
        )

    @classmethod
    def tool_result(
        cls,
        tool_name: str,
        result: Any,
        success: bool,
        tool_call_id: str,
        execution_id: str | None = None,
    ) -> "ExecutionEvent":
        """Factory method for tool result event."""
        return cls(
            event_type=ExecutionEventType.TOOL_RESULT,
            content=f"Tool {tool_name} {'succeeded' if success else 'failed'}",
            metadata={
                "tool_name": tool_name,
                "result": result,
                "success": success,
                "tool_call_id": tool_call_id,
            },
            execution_id=execution_id,
        )

    @classmethod
    def response(
        cls,
        content: str,
        execution_id: str | None = None,
        is_final: bool = False,
    ) -> "ExecutionEvent":
        """Factory method for response event."""
        return cls(
            event_type=ExecutionEventType.RESPONSE,
            content=content,
            metadata={"is_final": is_final},
            execution_id=execution_id,
        )

    @classmethod
    def error(
        cls,
        error_message: str,
        error_type: str | None = None,
        execution_id: str | None = None,
    ) -> "ExecutionEvent":
        """Factory method for error event."""
        return cls(
            event_type=ExecutionEventType.ERROR,
            content=error_message,
            metadata={"error_type": error_type},
            execution_id=execution_id,
        )

    @classmethod
    def completed(
        cls,
        execution_id: str,
        total_tokens: int | None = None,
        duration_ms: int | None = None,
    ) -> "ExecutionEvent":
        """Factory method for completed event."""
        return cls(
            event_type=ExecutionEventType.COMPLETED,
            content="Execution completed",
            metadata={
                "total_tokens": total_tokens,
                "duration_ms": duration_ms,
            },
            execution_id=execution_id,
        )


@dataclass
class LLMEvent:
    """Event from LLM streaming response.

    Low-level events representing chunks from the LLM API stream.
    These are processed and converted to ExecutionEvents by the
    agent orchestration service.

    Attributes:
        event_type: Type of LLM event
        content: Content data (text chunk, tool call, etc.)
        metadata: Additional event metadata
        index: Stream index for ordering
    """

    event_type: LLMEventType
    content: Any
    metadata: dict[str, Any] | None = None
    index: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "event_type": self.event_type.value,
            "content": self.content,
            "metadata": self.metadata,
            "index": self.index,
        }


@dataclass
class ToolCall:
    """Represents a tool call from the LLM.

    Contains all information needed to execute a tool and
    return the result to the LLM.

    Attributes:
        id: Unique identifier for this tool call
        name: Name of the tool to invoke
        arguments: Arguments to pass to the tool
    """

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }


@dataclass
class ToolResult:
    """Result of a tool execution.

    Contains the outcome of a tool invocation to be
    passed back to the LLM.

    Attributes:
        tool_call_id: ID of the tool call this result is for
        tool_name: Name of the tool that was called
        success: Whether the tool execution succeeded
        content: Result content (string for LLM)
        error: Error message if execution failed
    """

    tool_call_id: str
    tool_name: str
    success: bool
    content: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "success": self.success,
            "content": self.content,
            "error": self.error,
        }


@dataclass
class Message:
    """A message in the conversation history.

    Represents a single message in the LLM conversation context.

    Attributes:
        role: Role of the message sender
        content: Text content of the message
        name: Optional name for tool messages
        tool_calls: Optional list of tool calls (for assistant messages)
        tool_call_id: Optional tool call ID (for tool messages)
    """

    role: MessageRole
    content: str
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for LLM API."""
        result: dict[str, Any] = {
            "role": self.role.value,
            "content": self.content,
        }
        if self.name:
            result["name"] = self.name
        if self.tool_calls:
            result["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        return result

    @classmethod
    def system(cls, content: str) -> "Message":
        """Create a system message."""
        return cls(role=MessageRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        """Create a user message."""
        return cls(role=MessageRole.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str,
        tool_calls: list[ToolCall] | None = None,
    ) -> "Message":
        """Create an assistant message."""
        return cls(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, content: str, tool_call_id: str, name: str) -> "Message":
        """Create a tool result message."""
        return cls(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )


@dataclass
class Tool:
    """Tool definition for LLM function calling.

    Represents a tool that can be invoked by the LLM during
    agent execution.

    Attributes:
        name: Unique name of the tool
        description: Human-readable description
        parameters: JSON Schema for tool parameters
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for LLM API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class AgentContext:
    """Context for agent execution.

    Contains all the information needed for an agent to process
    a request, including conversation history, available tools,
    and system instructions.

    Attributes:
        system_prompt: System instructions for the agent
        messages: Conversation history
        tools: Available tools for the agent
        context_data: Additional context (case details, evidence, etc.)
        agent_type: Type of agent for specialized behavior
        max_tokens: Maximum tokens for response
    """

    system_prompt: str
    messages: list[Message]
    tools: list[Tool]
    context_data: dict[str, Any] = field(default_factory=dict)
    agent_type: str = "investigator"
    max_tokens: int = 4096

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "system_prompt": self.system_prompt,
            "messages": [m.to_dict() for m in self.messages],
            "tools": [t.to_dict() for t in self.tools],
            "context_data": self.context_data,
            "agent_type": self.agent_type,
            "max_tokens": self.max_tokens,
        }

    def add_user_message(self, content: str) -> None:
        """Add a user message to the conversation."""
        self.messages.append(Message.user(content))

    def add_assistant_message(
        self,
        content: str,
        tool_calls: list[ToolCall] | None = None,
    ) -> None:
        """Add an assistant message to the conversation."""
        self.messages.append(Message.assistant(content, tool_calls))

    def add_tool_result(
        self,
        content: str,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        """Add a tool result message to the conversation."""
        self.messages.append(Message.tool(content, tool_call_id, tool_name))

    def get_tool_by_name(self, name: str) -> Tool | None:
        """Get a tool by its name."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

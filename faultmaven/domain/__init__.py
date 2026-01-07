"""FaultMaven domain module.

This module contains domain entities, events, and value objects for the
agent orchestration and investigation workflows.
"""

# Backward compatibility - Events moved to modules/agent/domain/events/
# TODO: Remove after full migration complete (Phase 4)
from faultmaven.modules.agent.domain.events.execution_events import (
    ExecutionEvent,
    ExecutionEventType,
    LLMEvent,
    LLMEventType,
    AgentContext,
    Message,
    MessageRole,
    ToolCall,
    ToolResult,
    Tool,
)

__all__ = [
    "ExecutionEvent",
    "ExecutionEventType",
    "LLMEvent",
    "LLMEventType",
    "AgentContext",
    "Message",
    "MessageRole",
]

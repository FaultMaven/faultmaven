"""API Request/Response Models for Session Messages & Agent Chat (TASK-027).

This module defines Pydantic models for the Session Messages and Agent Chat API endpoints.
These models provide clear API contracts with validation, examples, and documentation.

Endpoints:
- GET /api/v1/sessions/{session_id}/messages - Retrieve conversation history
- POST /api/v1/agent/chat - Simplified agent chat with auto-session creation

Design: Follows TASK-024 (Report Module) and TASK-026 (Hypothesis) API model patterns.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ============================================================
# Message API Models
# ============================================================


class MessageMetadata(BaseModel):
    """Metadata for a message within a conversation."""

    execution_id: str = Field(..., description="ID of the agent execution that created this message")
    token_usage: Optional[Dict[str, int]] = Field(
        None, description="Token usage for assistant messages (input, output, total)"
    )
    agent_type: Optional[str] = Field(
        None, description="Type of agent that generated this message"
    )
    tool_calls: List[str] = Field(
        default_factory=list, description="List of tool names called during this execution"
    )


class MessageResponse(BaseModel):
    """A single message in a conversation."""

    message_id: str = Field(..., description="Unique message identifier (execution_id)")
    role: Literal["user", "assistant"] = Field(..., description="Message role (user or assistant)")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")
    metadata: Optional[MessageMetadata] = Field(
        None, description="Additional message metadata"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "message_id": "exec-abc123",
                "role": "assistant",
                "content": "Based on the logs, the 500 error was caused by a database connection timeout...",
                "timestamp": "2026-01-01T10:00:05Z",
                "metadata": {
                    "execution_id": "exec-abc123",
                    "token_usage": {"input": 150, "output": 320, "total": 470},
                    "agent_type": "investigator",
                    "tool_calls": ["list_evidence", "read_file"],
                },
            }
        }


class PaginationInfo(BaseModel):
    """Pagination information for list responses."""

    limit: int = Field(..., description="Number of items requested")
    offset: int = Field(..., description="Offset from start of list")
    next_offset: Optional[int] = Field(
        None, description="Offset for next page, null if no more items"
    )


class MessageListResponse(BaseModel):
    """Response model for listing session messages."""

    session_id: str = Field(..., description="Session ID")
    messages: List[MessageResponse] = Field(
        default_factory=list, description="List of messages in chronological order"
    )
    total_count: int = Field(..., description="Total number of messages in the session")
    has_more: bool = Field(..., description="Whether there are more messages to fetch")
    pagination: PaginationInfo = Field(..., description="Pagination information")

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "sess-abc123",
                "messages": [
                    {
                        "message_id": "exec-001",
                        "role": "user",
                        "content": "What caused the 500 error in the logs?",
                        "timestamp": "2026-01-01T10:00:00Z",
                        "metadata": {"execution_id": "exec-001"},
                    },
                    {
                        "message_id": "exec-001",
                        "role": "assistant",
                        "content": "Based on the logs, the 500 error was caused by...",
                        "timestamp": "2026-01-01T10:00:05Z",
                        "metadata": {
                            "execution_id": "exec-001",
                            "token_usage": {"input": 150, "output": 320, "total": 470},
                            "agent_type": "investigator",
                            "tool_calls": ["list_evidence", "read_file"],
                        },
                    },
                ],
                "total_count": 24,
                "has_more": False,
                "pagination": {"limit": 50, "offset": 0, "next_offset": None},
            }
        }


# ============================================================
# Chat API Models
# ============================================================


class ChatRequest(BaseModel):
    """Request model for simplified agent chat."""

    case_id: str = Field(..., description="Case ID to chat about")
    session_id: Optional[str] = Field(
        None, description="Existing session ID (auto-creates if null)"
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="User message/prompt",
    )
    agent_type: str = Field(
        default="investigator",
        description="Type of agent (investigator, debugger, researcher, validator, reporter)",
    )
    stream: bool = Field(default=True, description="Enable SSE streaming")

    class Config:
        json_schema_extra = {
            "example": {
                "case_id": "CASE-2026010100001",
                "session_id": None,
                "message": "What caused the 500 error in the logs?",
                "agent_type": "investigator",
                "stream": True,
            }
        }


class ChatMessageResponse(BaseModel):
    """Message response for non-streaming chat."""

    role: Literal["assistant"] = Field(
        default="assistant", description="Message role (always assistant)"
    )
    content: str = Field(..., description="Assistant's response content")
    timestamp: datetime = Field(..., description="Response timestamp")


class ChatResponse(BaseModel):
    """Response model for non-streaming chat completion."""

    execution_id: str = Field(..., description="Agent execution ID")
    session_id: str = Field(..., description="Session ID (may be newly created)")
    message: ChatMessageResponse = Field(..., description="Assistant's message")
    token_usage: Dict[str, int] = Field(
        default_factory=dict, description="Token usage (input, output, total)"
    )
    tool_calls: List[str] = Field(
        default_factory=list, description="List of tools called during execution"
    )
    duration_ms: int = Field(..., description="Execution duration in milliseconds")

    class Config:
        json_schema_extra = {
            "example": {
                "execution_id": "exec-abc123",
                "session_id": "sess-xyz789",
                "message": {
                    "role": "assistant",
                    "content": "Based on the logs, the 500 error was caused by...",
                    "timestamp": "2026-01-01T10:00:05Z",
                },
                "token_usage": {"input": 150, "output": 320, "total": 470},
                "tool_calls": ["list_evidence", "read_file"],
                "duration_ms": 5234,
            }
        }

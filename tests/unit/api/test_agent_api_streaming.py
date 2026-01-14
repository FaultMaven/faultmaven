"""Unit tests for Agent API SSE Streaming (TASK-016).

Tests:
- SSE Event format validation
- ExecutionEventSSE model
- Event conversion from domain models
- Error event generation
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from faultmaven.api.models import ExecutionEventSSE
from faultmaven.modules.agent.domain.events.execution_events import (
    ExecutionEvent,
    ExecutionEventType,
)

# ============================================================
# ExecutionEventSSE Model Tests
# ============================================================


class TestExecutionEventSSE:
    """Tests for ExecutionEventSSE model."""

    def test_to_sse_returns_valid_format(self):
        """Test to_sse() returns valid SSE format."""
        event = ExecutionEventSSE(
            event="started",
            data={"content": "Execution started", "metadata": {}},
        )

        sse_str = event.to_sse()

        assert sse_str.startswith("event: started\n")
        assert "data: " in sse_str
        assert sse_str.endswith("\n\n")

    def test_to_sse_event_line(self):
        """Test event line starts with 'event: '."""
        event = ExecutionEventSSE(
            event="thinking",
            data={"content": "Processing..."},
        )

        sse_str = event.to_sse()
        lines = sse_str.split("\n")

        assert lines[0] == "event: thinking"

    def test_to_sse_data_line(self):
        """Test data line starts with 'data: '."""
        event = ExecutionEventSSE(
            event="response",
            data={"content": "Hello"},
        )

        sse_str = event.to_sse()
        lines = sse_str.split("\n")

        assert lines[1].startswith("data: ")

    def test_to_sse_ends_with_double_newline(self):
        """Test SSE ends with double newline."""
        event = ExecutionEventSSE(
            event="completed",
            data={"content": "Done"},
        )

        sse_str = event.to_sse()

        assert sse_str.endswith("\n\n")

    def test_to_sse_data_is_valid_json(self):
        """Test data line contains valid JSON."""
        event = ExecutionEventSSE(
            event="response",
            data={
                "content": "Analysis complete",
                "metadata": {"execution_id": "exec-123"},
            },
        )

        sse_str = event.to_sse()
        lines = sse_str.split("\n")
        data_line = lines[1]
        data_json = data_line[6:]  # Remove "data: " prefix

        parsed = json.loads(data_json)
        assert parsed["content"] == "Analysis complete"
        assert parsed["metadata"]["execution_id"] == "exec-123"

    def test_to_sse_handles_special_characters(self):
        """Test SSE handles special characters in data."""
        event = ExecutionEventSSE(
            event="response",
            data={"content": 'Line1\nLine2 "quoted" special: < > &'},
        )

        sse_str = event.to_sse()
        lines = sse_str.split("\n")
        data_line = lines[1]
        data_json = data_line[6:]

        # Should be valid JSON despite special characters
        parsed = json.loads(data_json)
        assert "Line1\nLine2" in parsed["content"]

    def test_to_sse_handles_unicode(self):
        """Test SSE handles unicode characters."""
        event = ExecutionEventSSE(
            event="response",
            data={"content": "Japanese: 日本語 Emoji: 🚀"},
        )

        sse_str = event.to_sse()
        lines = sse_str.split("\n")
        data_line = lines[1]
        data_json = data_line[6:]

        parsed = json.loads(data_json)
        assert "日本語" in parsed["content"]
        assert "🚀" in parsed["content"]


# ============================================================
# Event Conversion Tests
# ============================================================


class TestEventConversion:
    """Tests for ExecutionEvent to ExecutionEventSSE conversion."""

    def test_from_execution_event_started(self):
        """Test conversion of started event."""
        domain_event = ExecutionEvent.started(
            execution_id="exec-123",
            metadata={"agent_type": "investigator"},
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "started"
        assert sse_event.data["content"] == "Execution started"
        assert sse_event.data["execution_id"] == "exec-123"
        assert "timestamp" in sse_event.data

    def test_from_execution_event_thinking(self):
        """Test conversion of thinking event."""
        domain_event = ExecutionEvent.thinking(
            content="Analyzing the problem...",
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "thinking"
        assert sse_event.data["content"] == "Analyzing the problem..."

    def test_from_execution_event_tool_call(self):
        """Test conversion of tool_call event."""
        domain_event = ExecutionEvent.tool_call(
            tool_name="read_file",
            tool_input={"evidence_id": "ev-123"},
            tool_call_id="tc-456",
            execution_id="exec-789",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "tool_call"
        assert sse_event.data["metadata"]["tool_name"] == "read_file"
        assert sse_event.data["metadata"]["tool_input"]["evidence_id"] == "ev-123"
        assert sse_event.data["metadata"]["tool_call_id"] == "tc-456"

    def test_from_execution_event_tool_result(self):
        """Test conversion of tool_result event."""
        domain_event = ExecutionEvent.tool_result(
            tool_name="read_file",
            result="File contents here...",
            success=True,
            tool_call_id="tc-456",
            execution_id="exec-789",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "tool_result"
        assert "succeeded" in sse_event.data["content"]
        assert sse_event.data["metadata"]["tool_name"] == "read_file"
        assert sse_event.data["metadata"]["success"] is True

    def test_from_execution_event_response(self):
        """Test conversion of response event."""
        domain_event = ExecutionEvent.response(
            content="Based on my analysis...",
            execution_id="exec-123",
            is_final=False,
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "response"
        assert sse_event.data["content"] == "Based on my analysis..."

    def test_from_execution_event_error(self):
        """Test conversion of error event."""
        domain_event = ExecutionEvent.error(
            error_message="LLM rate limit exceeded",
            error_type="LLMException",
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "error"
        assert "rate limit" in sse_event.data["content"]
        assert sse_event.data["metadata"]["error_type"] == "LLMException"

    def test_from_execution_event_completed(self):
        """Test conversion of completed event."""
        domain_event = ExecutionEvent.completed(
            execution_id="exec-123",
            total_tokens=1500,
            duration_ms=5000,
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == "completed"
        assert sse_event.data["metadata"]["total_tokens"] == 1500
        assert sse_event.data["metadata"]["duration_ms"] == 5000

    def test_from_execution_event_preserves_metadata(self):
        """Test conversion preserves metadata."""
        domain_event = ExecutionEvent(
            event_type=ExecutionEventType.RESPONSE,
            content="Test content",
            metadata={
                "custom_field": "custom_value",
                "nested": {"key": "value"},
            },
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.data["metadata"]["custom_field"] == "custom_value"
        assert sse_event.data["metadata"]["nested"]["key"] == "value"

    def test_from_execution_event_timestamp_format(self):
        """Test timestamp is in ISO format."""
        domain_event = ExecutionEvent.started(
            execution_id="exec-123",
            metadata={},
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)
        timestamp = sse_event.data["timestamp"]

        # Should be parseable as ISO format
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed is not None


# ============================================================
# Error Event Tests
# ============================================================


class TestErrorEvents:
    """Tests for error event generation."""

    def test_error_event_not_found(self):
        """Test not_found error event generation."""
        event = ExecutionEventSSE.error_event(
            error_code="not_found",
            message="Session not found",
        )

        assert event.event == "error"
        assert event.data["error"] == "not_found"
        assert event.data["message"] == "Session not found"

    def test_error_event_forbidden(self):
        """Test forbidden error event generation."""
        event = ExecutionEventSSE.error_event(
            error_code="forbidden",
            message="Session does not belong to organization",
        )

        assert event.event == "error"
        assert event.data["error"] == "forbidden"
        assert event.data["message"] == "Session does not belong to organization"

    def test_error_event_conflict(self):
        """Test conflict error event generation."""
        event = ExecutionEventSSE.error_event(
            error_code="conflict",
            message="Session is not active",
        )

        assert event.event == "error"
        assert event.data["error"] == "conflict"

    def test_error_event_llm_error(self):
        """Test llm_error event generation."""
        event = ExecutionEventSSE.error_event(
            error_code="llm_error",
            message="LLM rate limit exceeded",
        )

        assert event.event == "error"
        assert event.data["error"] == "llm_error"

    def test_error_event_internal_error(self):
        """Test internal_error event generation."""
        event = ExecutionEventSSE.error_event(
            error_code="internal_error",
            message="An unexpected error occurred",
        )

        assert event.event == "error"
        assert event.data["error"] == "internal_error"

    def test_error_event_to_sse_format(self):
        """Test error event SSE format."""
        event = ExecutionEventSSE.error_event(
            error_code="not_found",
            message="Resource not found",
        )

        sse_str = event.to_sse()

        assert "event: error" in sse_str
        assert "not_found" in sse_str
        assert "Resource not found" in sse_str


# ============================================================
# SSE Event Type Tests
# ============================================================


class TestSSEEventTypes:
    """Tests for all SSE event types."""

    @pytest.mark.parametrize(
        "event_type,expected_event",
        [
            (ExecutionEventType.STARTED, "started"),
            (ExecutionEventType.THINKING, "thinking"),
            (ExecutionEventType.TOOL_CALL, "tool_call"),
            (ExecutionEventType.TOOL_RESULT, "tool_result"),
            (ExecutionEventType.RESPONSE, "response"),
            (ExecutionEventType.ERROR, "error"),
            (ExecutionEventType.COMPLETED, "completed"),
        ],
    )
    def test_all_event_types_supported(self, event_type, expected_event):
        """Test all event types are properly converted."""
        domain_event = ExecutionEvent(
            event_type=event_type,
            content="Test content",
            metadata={},
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)

        assert sse_event.event == expected_event

    def test_event_type_to_sse_roundtrip(self):
        """Test event type is preserved through conversion."""
        domain_event = ExecutionEvent.response(
            content="Test response",
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)
        sse_str = sse_event.to_sse()

        assert "event: response" in sse_str


# ============================================================
# Edge Cases
# ============================================================


class TestSSEEdgeCases:
    """Tests for SSE edge cases."""

    def test_empty_content(self):
        """Test SSE with empty content."""
        event = ExecutionEventSSE(
            event="response",
            data={"content": ""},
        )

        sse_str = event.to_sse()
        assert "event: response" in sse_str

    def test_empty_metadata(self):
        """Test SSE with empty metadata."""
        domain_event = ExecutionEvent(
            event_type=ExecutionEventType.RESPONSE,
            content="Test",
            metadata=None,
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)
        assert sse_event.data["metadata"] == {}

    def test_large_content(self):
        """Test SSE with large content."""
        large_content = "x" * 100000

        event = ExecutionEventSSE(
            event="response",
            data={"content": large_content},
        )

        sse_str = event.to_sse()
        lines = sse_str.split("\n")
        data_json = lines[1][6:]
        parsed = json.loads(data_json)

        assert len(parsed["content"]) == 100000

    def test_nested_metadata(self):
        """Test SSE with deeply nested metadata."""
        domain_event = ExecutionEvent(
            event_type=ExecutionEventType.RESPONSE,
            content="Test",
            metadata={
                "level1": {
                    "level2": {
                        "level3": {
                            "level4": "deep value",
                        },
                    },
                },
            },
            execution_id="exec-123",
        )

        sse_event = ExecutionEventSSE.from_execution_event(domain_event)
        sse_str = sse_event.to_sse()

        # Should still be valid JSON
        lines = sse_str.split("\n")
        data_json = lines[1][6:]
        parsed = json.loads(data_json)
        assert (
            parsed["metadata"]["level1"]["level2"]["level3"]["level4"] == "deep value"
        )

    def test_special_event_name(self):
        """Test SSE with various event names."""
        event = ExecutionEventSSE(
            event="custom_event",
            data={"content": "test"},
        )

        sse_str = event.to_sse()
        assert "event: custom_event" in sse_str

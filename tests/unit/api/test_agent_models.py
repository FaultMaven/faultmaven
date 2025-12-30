"""Unit tests for Agent API Models (TASK-016).

Tests:
- AgentExecutionRequest validation
- AgentExecutionResponse model
- ToolCallResponse model
- ExecutionEventSSE model
- from_domain conversion methods
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from faultmaven.api.models import (
    AgentExecutionRequest,
    AgentExecutionResponse,
    ExecutionEventSSE,
    ToolCallResponse,
)
from faultmaven.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)


# ============================================================
# AgentExecutionRequest Tests
# ============================================================


class TestAgentExecutionRequest:
    """Tests for AgentExecutionRequest model."""

    def test_valid_request_with_all_fields(self):
        """Test valid request with all fields."""
        request = AgentExecutionRequest(
            user_message="What is causing the 500 errors?",
            agent_type="investigator",
            stream=True,
        )

        assert request.user_message == "What is causing the 500 errors?"
        assert request.agent_type == "investigator"
        assert request.stream is True

    def test_valid_request_minimal(self):
        """Test valid request with minimal fields."""
        request = AgentExecutionRequest(
            user_message="Help me debug this issue",
        )

        assert request.user_message == "Help me debug this issue"
        assert request.agent_type == "investigator"  # Default
        assert request.stream is True  # Default

    def test_user_message_required(self):
        """Test user_message is required."""
        with pytest.raises(ValidationError) as exc_info:
            AgentExecutionRequest()

        assert "user_message" in str(exc_info.value)

    def test_user_message_min_length(self):
        """Test user_message minimum length validation."""
        with pytest.raises(ValidationError) as exc_info:
            AgentExecutionRequest(user_message="")

        errors = exc_info.value.errors()
        assert any("min_length" in str(e).lower() or "at least 1" in str(e).lower() for e in errors)

    def test_user_message_max_length(self):
        """Test user_message maximum length validation."""
        with pytest.raises(ValidationError) as exc_info:
            AgentExecutionRequest(user_message="x" * 10001)

        errors = exc_info.value.errors()
        assert any("max_length" in str(e).lower() or "at most 10000" in str(e).lower() for e in errors)

    def test_user_message_at_max_length(self):
        """Test user_message at exactly max length is valid."""
        request = AgentExecutionRequest(user_message="x" * 10000)
        assert len(request.user_message) == 10000

    def test_agent_type_defaults_to_investigator(self):
        """Test agent_type defaults to investigator."""
        request = AgentExecutionRequest(user_message="Test")
        assert request.agent_type == "investigator"

    def test_agent_type_valid_values(self):
        """Test valid agent_type values."""
        for agent_type in ["investigator", "debugger", "researcher", "validator", "reporter"]:
            request = AgentExecutionRequest(
                user_message="Test",
                agent_type=agent_type,
            )
            assert request.agent_type == agent_type

    def test_stream_defaults_to_true(self):
        """Test stream defaults to True."""
        request = AgentExecutionRequest(user_message="Test")
        assert request.stream is True

    def test_stream_false(self):
        """Test stream can be set to False."""
        request = AgentExecutionRequest(
            user_message="Test",
            stream=False,
        )
        assert request.stream is False

    def test_json_schema_example(self):
        """Test model has json_schema_extra with example."""
        schema = AgentExecutionRequest.model_json_schema()
        # Verify example exists
        assert "example" in schema.get("examples", [{}])[0] or "example" in str(schema)


# ============================================================
# ToolCallResponse Tests
# ============================================================


class TestToolCallResponse:
    """Tests for ToolCallResponse model."""

    def test_valid_tool_call_response(self):
        """Test valid tool call response."""
        response = ToolCallResponse(
            tool_call_id="tc-123",
            tool_name="read_file",
            arguments={"evidence_id": "ev-456"},
            result="File contents...",
            status="success",
        )

        assert response.tool_call_id == "tc-123"
        assert response.tool_name == "read_file"
        assert response.arguments["evidence_id"] == "ev-456"
        assert response.result == "File contents..."
        assert response.status == "success"

    def test_tool_call_response_optional_result(self):
        """Test tool call response with optional result."""
        response = ToolCallResponse(
            tool_call_id="tc-123",
            tool_name="search_knowledge",
            arguments={"query": "error handling"},
            status="pending",
        )

        assert response.result is None
        assert response.status == "pending"

    def test_from_domain_success(self):
        """Test from_domain with successful tool call."""
        tool_call = MagicMock()
        tool_call.tool_call_id = "tc-123"
        tool_call.tool_name = "read_file"
        tool_call.tool_input = {"evidence_id": "ev-456"}
        tool_call.tool_output = {"content": "File contents..."}
        tool_call.status = "success"

        response = ToolCallResponse.from_domain(tool_call)

        assert response.tool_call_id == "tc-123"
        assert response.tool_name == "read_file"
        assert response.arguments == {"evidence_id": "ev-456"}
        assert response.status == "success"

    def test_from_domain_with_none_input(self):
        """Test from_domain with None tool_input."""
        tool_call = MagicMock()
        tool_call.tool_call_id = "tc-123"
        tool_call.tool_name = "list_evidence"
        tool_call.tool_input = None
        tool_call.tool_output = None
        tool_call.status = "pending"

        response = ToolCallResponse.from_domain(tool_call)

        assert response.arguments == {}
        assert response.result is None

    def test_from_domain_with_complex_output(self):
        """Test from_domain with complex output."""
        tool_call = MagicMock()
        tool_call.tool_call_id = "tc-123"
        tool_call.tool_name = "search_knowledge"
        tool_call.tool_input = {"query": "test"}
        tool_call.tool_output = {"results": [{"id": 1}, {"id": 2}]}
        tool_call.status = "success"

        response = ToolCallResponse.from_domain(tool_call)

        assert response.result is not None
        assert "results" in response.result


# ============================================================
# AgentExecutionResponse Tests
# ============================================================


class TestAgentExecutionResponse:
    """Tests for AgentExecutionResponse model."""

    def test_valid_execution_response(self):
        """Test valid execution response."""
        now = datetime.now(timezone.utc)
        response = AgentExecutionResponse(
            execution_id="exec-123",
            status="completed",
            agent_response="The error is caused by...",
            tokens_used=300,
            started_at=now,
            completed_at=now,
            tool_calls=[],
        )

        assert response.execution_id == "exec-123"
        assert response.status == "completed"
        assert response.agent_response == "The error is caused by..."
        assert response.tokens_used == 300

    def test_execution_response_with_tool_calls(self):
        """Test execution response with tool calls."""
        now = datetime.now(timezone.utc)
        tool_call = ToolCallResponse(
            tool_call_id="tc-123",
            tool_name="read_file",
            arguments={"evidence_id": "ev-456"},
            result="contents",
            status="success",
        )
        response = AgentExecutionResponse(
            execution_id="exec-123",
            status="completed",
            agent_response="Analysis complete",
            tokens_used=500,
            started_at=now,
            tool_calls=[tool_call],
        )

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].tool_name == "read_file"

    def test_execution_response_optional_completed_at(self):
        """Test execution response with optional completed_at."""
        now = datetime.now(timezone.utc)
        response = AgentExecutionResponse(
            execution_id="exec-123",
            status="running",
            agent_response="",
            tokens_used=0,
            started_at=now,
        )

        assert response.completed_at is None

    def test_from_domain_completed_execution(self):
        """Test from_domain with completed execution."""
        execution = MagicMock()
        execution.execution_id = "exec-123"
        execution.status = ExecutionStatus.COMPLETED
        execution.response = "The root cause is..."
        execution.get_total_tokens.return_value = 450
        execution.started_at = datetime.now(timezone.utc)
        execution.completed_at = datetime.now(timezone.utc)
        execution.created_at = datetime.now(timezone.utc)
        execution.tool_calls = []

        response = AgentExecutionResponse.from_domain(execution)

        assert response.execution_id == "exec-123"
        assert response.status == "completed"
        assert response.agent_response == "The root cause is..."
        assert response.tokens_used == 450

    def test_from_domain_running_execution(self):
        """Test from_domain with running execution."""
        execution = MagicMock()
        execution.execution_id = "exec-123"
        execution.status = ExecutionStatus.RUNNING
        execution.response = None
        execution.get_total_tokens.return_value = 0
        execution.started_at = datetime.now(timezone.utc)
        execution.completed_at = None
        execution.created_at = datetime.now(timezone.utc)
        execution.tool_calls = []

        response = AgentExecutionResponse.from_domain(execution)

        assert response.status == "running"
        assert response.agent_response == ""
        assert response.completed_at is None

    def test_from_domain_with_tool_calls(self):
        """Test from_domain preserves tool calls."""
        tool_call = MagicMock()
        tool_call.tool_call_id = "tc-123"
        tool_call.tool_name = "read_file"
        tool_call.tool_input = {"evidence_id": "ev-456"}
        tool_call.tool_output = {"content": "data"}
        tool_call.status = "success"

        execution = MagicMock()
        execution.execution_id = "exec-123"
        execution.status = ExecutionStatus.COMPLETED
        execution.response = "Analysis complete"
        execution.get_total_tokens.return_value = 300
        execution.started_at = datetime.now(timezone.utc)
        execution.completed_at = datetime.now(timezone.utc)
        execution.created_at = datetime.now(timezone.utc)
        execution.tool_calls = [tool_call]

        response = AgentExecutionResponse.from_domain(execution)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].tool_name == "read_file"

    def test_from_domain_with_string_status(self):
        """Test from_domain handles string status."""
        execution = MagicMock()
        execution.execution_id = "exec-123"
        execution.status = "completed"  # String instead of enum
        execution.response = "Done"
        execution.get_total_tokens.return_value = 100
        execution.started_at = datetime.now(timezone.utc)
        execution.completed_at = datetime.now(timezone.utc)
        execution.created_at = datetime.now(timezone.utc)
        execution.tool_calls = []

        response = AgentExecutionResponse.from_domain(execution)

        assert response.status == "completed"

    def test_from_domain_uses_created_at_fallback(self):
        """Test from_domain uses created_at when started_at is None."""
        created = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        execution = MagicMock()
        execution.execution_id = "exec-123"
        execution.status = ExecutionStatus.QUEUED
        execution.response = None
        execution.get_total_tokens.return_value = 0
        execution.started_at = None
        execution.completed_at = None
        execution.created_at = created
        execution.tool_calls = []

        response = AgentExecutionResponse.from_domain(execution)

        assert response.started_at == created

    def test_from_domain_empty_tool_calls(self):
        """Test from_domain handles None tool_calls."""
        execution = MagicMock()
        execution.execution_id = "exec-123"
        execution.status = ExecutionStatus.COMPLETED
        execution.response = "Done"
        execution.get_total_tokens.return_value = 100
        execution.started_at = datetime.now(timezone.utc)
        execution.completed_at = datetime.now(timezone.utc)
        execution.created_at = datetime.now(timezone.utc)
        execution.tool_calls = None

        response = AgentExecutionResponse.from_domain(execution)

        assert response.tool_calls == []


# ============================================================
# ExecutionEventSSE Model Tests
# ============================================================


class TestExecutionEventSSEModel:
    """Tests for ExecutionEventSSE model."""

    def test_valid_event(self):
        """Test valid SSE event."""
        event = ExecutionEventSSE(
            event="started",
            data={"content": "Execution started", "metadata": {}},
        )

        assert event.event == "started"
        assert event.data["content"] == "Execution started"

    def test_event_default_data(self):
        """Test SSE event with default empty data."""
        event = ExecutionEventSSE(event="thinking")

        assert event.data == {}

    def test_event_types(self):
        """Test various event types."""
        for event_type in ["started", "thinking", "tool_call", "tool_result", "response", "error", "completed"]:
            event = ExecutionEventSSE(event=event_type, data={})
            assert event.event == event_type

    def test_data_can_be_complex(self):
        """Test data can contain complex structures."""
        event = ExecutionEventSSE(
            event="tool_call",
            data={
                "content": "Calling tool",
                "metadata": {
                    "tool_name": "read_file",
                    "arguments": {"path": "/var/log/app.log"},
                    "nested": {"deep": {"value": 123}},
                },
                "list_data": [1, 2, 3],
            },
        )

        assert event.data["metadata"]["tool_name"] == "read_file"
        assert event.data["metadata"]["nested"]["deep"]["value"] == 123
        assert event.data["list_data"] == [1, 2, 3]


# ============================================================
# Model Validation Edge Cases
# ============================================================


class TestModelEdgeCases:
    """Tests for model edge cases and validation."""

    def test_request_with_unicode_message(self):
        """Test request with unicode characters."""
        request = AgentExecutionRequest(
            user_message="日本語のエラーメッセージ 🚀",
        )
        assert "日本語" in request.user_message

    def test_request_with_newlines(self):
        """Test request with newlines in message."""
        request = AgentExecutionRequest(
            user_message="Line 1\nLine 2\nLine 3",
        )
        assert "\n" in request.user_message

    def test_request_with_special_characters(self):
        """Test request with special characters."""
        request = AgentExecutionRequest(
            user_message='Error: "Connection refused" <socket> & timeout',
        )
        assert '"' in request.user_message
        assert '<' in request.user_message

    def test_response_serialization(self):
        """Test response can be serialized to JSON."""
        now = datetime.now(timezone.utc)
        response = AgentExecutionResponse(
            execution_id="exec-123",
            status="completed",
            agent_response="Done",
            tokens_used=100,
            started_at=now,
            tool_calls=[],
        )

        json_data = response.model_dump_json()
        assert "exec-123" in json_data
        assert "completed" in json_data

    def test_tool_call_response_serialization(self):
        """Test tool call response can be serialized."""
        response = ToolCallResponse(
            tool_call_id="tc-123",
            tool_name="search",
            arguments={"query": "test"},
            status="success",
        )

        json_data = response.model_dump_json()
        assert "tc-123" in json_data
        assert "search" in json_data


# ============================================================
# Model Config Tests
# ============================================================


class TestModelConfig:
    """Tests for model configuration."""

    def test_agent_execution_response_from_attributes(self):
        """Test AgentExecutionResponse has from_attributes config."""
        assert AgentExecutionResponse.model_config.get("from_attributes", False) is True

    def test_tool_call_response_from_attributes(self):
        """Test ToolCallResponse has from_attributes config."""
        assert ToolCallResponse.model_config.get("from_attributes", False) is True

"""Unit tests for Agent Execution domain models.

Tests the AgentExecution and AgentToolCall dataclasses and related enums.
"""

import pytest
from datetime import datetime, timezone, timedelta
import time

from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)


class TestExecutionStatus:
    """Tests for ExecutionStatus enum."""

    def test_all_statuses_defined(self):
        """Verify all expected execution statuses are defined."""
        expected_statuses = [
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "timeout",
        ]
        actual_statuses = [s.value for s in ExecutionStatus]
        assert set(expected_statuses) == set(actual_statuses)

    def test_enum_is_string(self):
        """Verify enum values are strings."""
        for s in ExecutionStatus:
            assert isinstance(s.value, str)

    def test_enum_lookup_by_value(self):
        """Test looking up enum by value."""
        assert ExecutionStatus("queued") == ExecutionStatus.QUEUED
        assert ExecutionStatus("completed") == ExecutionStatus.COMPLETED
        assert ExecutionStatus("failed") == ExecutionStatus.FAILED


class TestAgentType:
    """Tests for AgentType enum."""

    def test_all_types_defined(self):
        """Verify all expected agent types are defined."""
        expected_types = [
            "investigator",
            "debugger",
            "researcher",
            "validator",
            "reporter",
            "custom",
        ]
        actual_types = [t.value for t in AgentType]
        assert set(expected_types) == set(actual_types)

    def test_enum_lookup_by_value(self):
        """Test looking up enum by value."""
        assert AgentType("investigator") == AgentType.INVESTIGATOR
        assert AgentType("debugger") == AgentType.DEBUGGER


class TestAgentToolCallCreation:
    """Tests for AgentToolCall creation and validation."""

    def test_create_minimal_tool_call(self):
        """Test creating tool call with minimal required fields."""
        tool_call = AgentToolCall(
            tool_call_id="tc_123abc",
            execution_id="exec_456def",
            tool_name="web_search",
        )

        assert tool_call.tool_call_id == "tc_123abc"
        assert tool_call.execution_id == "exec_456def"
        assert tool_call.tool_name == "web_search"
        assert tool_call.status == "pending"
        assert tool_call.tool_input is None
        assert tool_call.tool_output is None
        assert tool_call.error_message is None
        assert tool_call.started_at is None
        assert tool_call.completed_at is None
        assert tool_call.duration_ms is None

    def test_create_full_tool_call(self):
        """Test creating tool call with all fields."""
        created = datetime(2025, 12, 29, 10, 0, 0, tzinfo=timezone.utc)
        started = datetime(2025, 12, 29, 10, 0, 1, tzinfo=timezone.utc)
        completed = datetime(2025, 12, 29, 10, 0, 3, tzinfo=timezone.utc)

        tool_call = AgentToolCall(
            tool_call_id="tc_full123",
            execution_id="exec_456def",
            tool_name="code_exec",
            tool_input={"code": "print('hello')"},
            tool_output={"stdout": "hello", "exit_code": 0},
            status="success",
            started_at=started,
            completed_at=completed,
            duration_ms=2000,
            created_at=created,
            updated_at=created,
        )

        assert tool_call.tool_input == {"code": "print('hello')"}
        assert tool_call.tool_output == {"stdout": "hello", "exit_code": 0}
        assert tool_call.status == "success"
        assert tool_call.duration_ms == 2000

    def test_default_timestamps(self):
        """Test that timestamps are auto-generated if not provided."""
        before = datetime.now(timezone.utc)

        tool_call = AgentToolCall(
            tool_call_id="tc_timestamps",
            execution_id="exec_123",
            tool_name="test_tool",
        )

        after = datetime.now(timezone.utc)

        assert before <= tool_call.created_at <= after
        assert before <= tool_call.updated_at <= after


class TestAgentToolCallValidation:
    """Tests for AgentToolCall validation."""

    def test_empty_tool_call_id_fails(self):
        """Test that empty tool_call_id raises ValueError."""
        with pytest.raises(ValueError, match="tool_call_id is required"):
            AgentToolCall(
                tool_call_id="",
                execution_id="exec_123",
                tool_name="test_tool",
            )

    def test_empty_execution_id_fails(self):
        """Test that empty execution_id raises ValueError."""
        with pytest.raises(ValueError, match="execution_id is required"):
            AgentToolCall(
                tool_call_id="tc_123",
                execution_id="",
                tool_name="test_tool",
            )

    def test_empty_tool_name_fails(self):
        """Test that empty tool_name raises ValueError."""
        with pytest.raises(ValueError, match="tool_name is required"):
            AgentToolCall(
                tool_call_id="tc_123",
                execution_id="exec_123",
                tool_name="",
            )

    def test_invalid_status_fails(self):
        """Test that invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Invalid status"):
            AgentToolCall(
                tool_call_id="tc_123",
                execution_id="exec_123",
                tool_name="test_tool",
                status="invalid_status",
            )

    def test_negative_duration_fails(self):
        """Test that negative duration_ms raises ValueError."""
        with pytest.raises(ValueError, match="duration_ms cannot be negative"):
            AgentToolCall(
                tool_call_id="tc_123",
                execution_id="exec_123",
                tool_name="test_tool",
                duration_ms=-100,
            )

    def test_zero_duration_allowed(self):
        """Test that zero duration_ms is allowed."""
        tool_call = AgentToolCall(
            tool_call_id="tc_123",
            execution_id="exec_123",
            tool_name="test_tool",
            duration_ms=0,
        )
        assert tool_call.duration_ms == 0


class TestAgentToolCallMethods:
    """Tests for AgentToolCall lifecycle methods."""

    @pytest.fixture
    def sample_tool_call(self):
        """Create sample tool call for testing."""
        return AgentToolCall(
            tool_call_id="tc_sample123",
            execution_id="exec_456",
            tool_name="web_search",
            tool_input={"query": "test query"},
        )

    def test_mark_started(self, sample_tool_call):
        """Test mark_started method."""
        before = datetime.now(timezone.utc)
        sample_tool_call.mark_started()
        after = datetime.now(timezone.utc)

        assert sample_tool_call.status == "running"
        assert before <= sample_tool_call.started_at <= after
        assert before <= sample_tool_call.updated_at <= after

    def test_mark_success(self, sample_tool_call):
        """Test mark_success method."""
        sample_tool_call.mark_started()
        time.sleep(0.001)  # Ensure some duration

        output = {"result": "success", "data": [1, 2, 3]}
        sample_tool_call.mark_success(output)

        assert sample_tool_call.status == "success"
        assert sample_tool_call.tool_output == output
        assert sample_tool_call.completed_at is not None
        assert sample_tool_call.duration_ms is not None
        assert sample_tool_call.duration_ms >= 0

    def test_mark_failed(self, sample_tool_call):
        """Test mark_failed method."""
        sample_tool_call.mark_started()
        time.sleep(0.001)

        sample_tool_call.mark_failed("Connection timeout")

        assert sample_tool_call.status == "failed"
        assert sample_tool_call.error_message == "Connection timeout"
        assert sample_tool_call.completed_at is not None
        assert sample_tool_call.duration_ms is not None

    def test_is_completed(self, sample_tool_call):
        """Test is_completed method."""
        assert sample_tool_call.is_completed() is False

        sample_tool_call.status = "success"
        assert sample_tool_call.is_completed() is True

        sample_tool_call.status = "failed"
        assert sample_tool_call.is_completed() is True

        sample_tool_call.status = "running"
        assert sample_tool_call.is_completed() is False

    def test_is_running(self, sample_tool_call):
        """Test is_running method."""
        assert sample_tool_call.is_running() is False

        sample_tool_call.mark_started()
        assert sample_tool_call.is_running() is True

        sample_tool_call.mark_success({})
        assert sample_tool_call.is_running() is False

    def test_repr(self, sample_tool_call):
        """Test string representation."""
        repr_str = repr(sample_tool_call)
        assert "AgentToolCall" in repr_str
        assert "tc_sample123" in repr_str
        assert "web_search" in repr_str
        assert "pending" in repr_str


class TestAgentExecutionCreation:
    """Tests for AgentExecution creation and validation."""

    def test_create_minimal_execution(self):
        """Test creating execution with minimal required fields."""
        execution = AgentExecution(
            execution_id="exec_123abc",
            case_id="case_456def",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
        )

        assert execution.execution_id == "exec_123abc"
        assert execution.case_id == "case_456def"
        assert execution.agent_type == AgentType.INVESTIGATOR
        assert execution.agent_model == "gpt-4"
        assert execution.status == ExecutionStatus.QUEUED
        assert execution.tool_calls == []
        assert execution.token_usage is None
        assert execution.prompt is None
        assert execution.response is None

    def test_create_full_execution(self):
        """Test creating execution with all fields."""
        created = datetime(2025, 12, 29, 10, 0, 0, tzinfo=timezone.utc)
        started = datetime(2025, 12, 29, 10, 0, 1, tzinfo=timezone.utc)
        completed = datetime(2025, 12, 29, 10, 1, 0, tzinfo=timezone.utc)

        execution = AgentExecution(
            execution_id="exec_full123",
            case_id="case_456def",
            agent_type=AgentType.DEBUGGER,
            agent_model="claude-3-opus",
            status=ExecutionStatus.COMPLETED,
            started_at=started,
            completed_at=completed,
            execution_duration_ms=59000,
            prompt="Debug this code",
            response="Here is the fix...",
            token_usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
            metadata={"session_id": "sess_123"},
            created_at=created,
            updated_at=created,
        )

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.execution_duration_ms == 59000
        assert execution.prompt == "Debug this code"
        assert execution.response == "Here is the fix..."
        assert execution.token_usage == {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}

    def test_default_timestamps(self):
        """Test that timestamps are auto-generated if not provided."""
        before = datetime.now(timezone.utc)

        execution = AgentExecution(
            execution_id="exec_timestamps",
            case_id="case_123",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
        )

        after = datetime.now(timezone.utc)

        assert before <= execution.created_at <= after
        assert before <= execution.updated_at <= after


class TestAgentExecutionValidation:
    """Tests for AgentExecution validation."""

    def test_empty_execution_id_fails(self):
        """Test that empty execution_id raises ValueError."""
        with pytest.raises(ValueError, match="execution_id is required"):
            AgentExecution(
                execution_id="",
                case_id="case_123",
                agent_type=AgentType.INVESTIGATOR,
                agent_model="gpt-4",
            )

    def test_empty_case_id_fails(self):
        """Test that empty case_id raises ValueError."""
        with pytest.raises(ValueError, match="case_id is required"):
            AgentExecution(
                execution_id="exec_123",
                case_id="",
                agent_type=AgentType.INVESTIGATOR,
                agent_model="gpt-4",
            )

    def test_empty_agent_model_fails(self):
        """Test that empty agent_model raises ValueError."""
        with pytest.raises(ValueError, match="agent_model is required"):
            AgentExecution(
                execution_id="exec_123",
                case_id="case_123",
                agent_type=AgentType.INVESTIGATOR,
                agent_model="",
            )

    def test_negative_duration_fails(self):
        """Test that negative execution_duration_ms raises ValueError."""
        with pytest.raises(ValueError, match="execution_duration_ms cannot be negative"):
            AgentExecution(
                execution_id="exec_123",
                case_id="case_123",
                agent_type=AgentType.INVESTIGATOR,
                agent_model="gpt-4",
                execution_duration_ms=-100,
            )

    def test_zero_duration_allowed(self):
        """Test that zero execution_duration_ms is allowed."""
        execution = AgentExecution(
            execution_id="exec_123",
            case_id="case_123",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
            execution_duration_ms=0,
        )
        assert execution.execution_duration_ms == 0


class TestAgentExecutionMethods:
    """Tests for AgentExecution lifecycle methods."""

    @pytest.fixture
    def sample_execution(self):
        """Create sample execution for testing."""
        return AgentExecution(
            execution_id="exec_sample123",
            case_id="case_456",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
            prompt="Investigate this issue",
        )

    def test_is_queued(self, sample_execution):
        """Test is_queued method."""
        assert sample_execution.is_queued() is True

        sample_execution.mark_started()
        assert sample_execution.is_queued() is False

    def test_is_running(self, sample_execution):
        """Test is_running method."""
        assert sample_execution.is_running() is False

        sample_execution.mark_started()
        assert sample_execution.is_running() is True

        sample_execution.mark_completed("Done")
        assert sample_execution.is_running() is False

    def test_is_completed(self, sample_execution):
        """Test is_completed method for all terminal states."""
        assert sample_execution.is_completed() is False

        sample_execution.status = ExecutionStatus.COMPLETED
        assert sample_execution.is_completed() is True

        sample_execution.status = ExecutionStatus.FAILED
        assert sample_execution.is_completed() is True

        sample_execution.status = ExecutionStatus.CANCELLED
        assert sample_execution.is_completed() is True

        sample_execution.status = ExecutionStatus.TIMEOUT
        assert sample_execution.is_completed() is True

        sample_execution.status = ExecutionStatus.RUNNING
        assert sample_execution.is_completed() is False

    def test_mark_started(self, sample_execution):
        """Test mark_started method."""
        before = datetime.now(timezone.utc)
        sample_execution.mark_started()
        after = datetime.now(timezone.utc)

        assert sample_execution.status == ExecutionStatus.RUNNING
        assert before <= sample_execution.started_at <= after
        assert before <= sample_execution.updated_at <= after

    def test_mark_completed(self, sample_execution):
        """Test mark_completed method."""
        sample_execution.mark_started()
        time.sleep(0.001)

        sample_execution.mark_completed("Investigation complete. Root cause is...")

        assert sample_execution.status == ExecutionStatus.COMPLETED
        assert sample_execution.response == "Investigation complete. Root cause is..."
        assert sample_execution.completed_at is not None
        assert sample_execution.execution_duration_ms is not None
        assert sample_execution.execution_duration_ms >= 0

    def test_mark_failed(self, sample_execution):
        """Test mark_failed method."""
        sample_execution.mark_started()
        time.sleep(0.001)

        sample_execution.mark_failed("API rate limit exceeded")

        assert sample_execution.status == ExecutionStatus.FAILED
        assert sample_execution.error_message == "API rate limit exceeded"
        assert sample_execution.completed_at is not None
        assert sample_execution.execution_duration_ms is not None

    def test_mark_cancelled(self, sample_execution):
        """Test mark_cancelled method."""
        sample_execution.mark_started()
        time.sleep(0.001)

        sample_execution.mark_cancelled()

        assert sample_execution.status == ExecutionStatus.CANCELLED
        assert sample_execution.completed_at is not None

    def test_mark_timeout(self, sample_execution):
        """Test mark_timeout method."""
        sample_execution.mark_started()
        time.sleep(0.001)

        sample_execution.mark_timeout()

        assert sample_execution.status == ExecutionStatus.TIMEOUT
        assert sample_execution.completed_at is not None

    def test_add_tool_call(self, sample_execution):
        """Test add_tool_call method."""
        tool_call = AgentToolCall(
            tool_call_id="tc_123",
            execution_id=sample_execution.execution_id,
            tool_name="web_search",
        )

        sample_execution.add_tool_call(tool_call)

        assert len(sample_execution.tool_calls) == 1
        assert sample_execution.tool_calls[0] == tool_call

    def test_get_tool_call_count(self, sample_execution):
        """Test get_tool_call_count method."""
        assert sample_execution.get_tool_call_count() == 0

        for i in range(3):
            sample_execution.add_tool_call(
                AgentToolCall(
                    tool_call_id=f"tc_{i}",
                    execution_id=sample_execution.execution_id,
                    tool_name="test_tool",
                )
            )

        assert sample_execution.get_tool_call_count() == 3

    def test_get_failed_tool_calls(self, sample_execution):
        """Test get_failed_tool_calls method."""
        tc1 = AgentToolCall(
            tool_call_id="tc_1",
            execution_id=sample_execution.execution_id,
            tool_name="web_search",
            status="success",
        )
        tc2 = AgentToolCall(
            tool_call_id="tc_2",
            execution_id=sample_execution.execution_id,
            tool_name="code_exec",
            status="failed",
        )
        tc3 = AgentToolCall(
            tool_call_id="tc_3",
            execution_id=sample_execution.execution_id,
            tool_name="file_read",
            status="failed",
        )

        sample_execution.tool_calls = [tc1, tc2, tc3]

        failed = sample_execution.get_failed_tool_calls()
        assert len(failed) == 2
        assert all(tc.status == "failed" for tc in failed)

    def test_get_successful_tool_calls(self, sample_execution):
        """Test get_successful_tool_calls method."""
        tc1 = AgentToolCall(
            tool_call_id="tc_1",
            execution_id=sample_execution.execution_id,
            tool_name="web_search",
            status="success",
        )
        tc2 = AgentToolCall(
            tool_call_id="tc_2",
            execution_id=sample_execution.execution_id,
            tool_name="code_exec",
            status="failed",
        )

        sample_execution.tool_calls = [tc1, tc2]

        successful = sample_execution.get_successful_tool_calls()
        assert len(successful) == 1
        assert successful[0].status == "success"


class TestAgentExecutionTokenUsage:
    """Tests for AgentExecution token usage methods."""

    @pytest.fixture
    def sample_execution(self):
        """Create sample execution for testing."""
        return AgentExecution(
            execution_id="exec_tokens",
            case_id="case_123",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
        )

    def test_get_total_tokens_none(self, sample_execution):
        """Test get_total_tokens when no token usage set."""
        assert sample_execution.get_total_tokens() == 0

    def test_get_total_tokens(self, sample_execution):
        """Test get_total_tokens with token usage."""
        sample_execution.token_usage = {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }
        assert sample_execution.get_total_tokens() == 300

    def test_get_prompt_tokens(self, sample_execution):
        """Test get_prompt_tokens method."""
        sample_execution.token_usage = {
            "prompt_tokens": 150,
            "completion_tokens": 250,
            "total_tokens": 400,
        }
        assert sample_execution.get_prompt_tokens() == 150

    def test_get_completion_tokens(self, sample_execution):
        """Test get_completion_tokens method."""
        sample_execution.token_usage = {
            "prompt_tokens": 150,
            "completion_tokens": 250,
            "total_tokens": 400,
        }
        assert sample_execution.get_completion_tokens() == 250

    def test_set_token_usage(self, sample_execution):
        """Test set_token_usage method."""
        sample_execution.set_token_usage(
            prompt_tokens=100,
            completion_tokens=200,
        )

        assert sample_execution.token_usage == {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }

    def test_set_token_usage_with_total(self, sample_execution):
        """Test set_token_usage with explicit total."""
        sample_execution.set_token_usage(
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=350,  # Different from sum
        )

        assert sample_execution.token_usage["total_tokens"] == 350


class TestAgentExecutionToolCallDuration:
    """Tests for tool call duration aggregation."""

    @pytest.fixture
    def sample_execution(self):
        """Create sample execution with tool calls."""
        execution = AgentExecution(
            execution_id="exec_duration",
            case_id="case_123",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
        )

        tc1 = AgentToolCall(
            tool_call_id="tc_1",
            execution_id="exec_duration",
            tool_name="web_search",
            duration_ms=500,
            status="success",
        )
        tc2 = AgentToolCall(
            tool_call_id="tc_2",
            execution_id="exec_duration",
            tool_name="code_exec",
            duration_ms=1500,
            status="success",
        )
        tc3 = AgentToolCall(
            tool_call_id="tc_3",
            execution_id="exec_duration",
            tool_name="file_read",
            duration_ms=None,  # No duration
            status="pending",
        )

        execution.tool_calls = [tc1, tc2, tc3]
        return execution

    def test_get_tool_call_duration_total_ms(self, sample_execution):
        """Test get_tool_call_duration_total_ms method."""
        total = sample_execution.get_tool_call_duration_total_ms()
        assert total == 2000  # 500 + 1500, None is ignored

    def test_get_tool_call_duration_total_ms_empty(self):
        """Test duration total with no tool calls."""
        execution = AgentExecution(
            execution_id="exec_empty",
            case_id="case_123",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
        )
        assert execution.get_tool_call_duration_total_ms() == 0


class TestAgentExecutionTouch:
    """Tests for touch method."""

    def test_touch_updates_timestamp(self):
        """Test that touch() updates updated_at timestamp."""
        execution = AgentExecution(
            execution_id="exec_touch",
            case_id="case_123",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="gpt-4",
        )

        original = execution.updated_at
        time.sleep(0.001)

        execution.touch()

        assert execution.updated_at > original


class TestAgentExecutionRepr:
    """Tests for string representation."""

    def test_repr(self):
        """Test string representation."""
        execution = AgentExecution(
            execution_id="exec_repr123",
            case_id="case_456",
            agent_type=AgentType.DEBUGGER,
            agent_model="gpt-4",
        )

        repr_str = repr(execution)
        assert "AgentExecution" in repr_str
        assert "exec_repr123" in repr_str
        assert "case_456" in repr_str
        assert "debugger" in repr_str
        assert "queued" in repr_str

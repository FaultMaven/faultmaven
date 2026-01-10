"""Unit tests for Agent Execution Repository.

Tests the InMemoryAgentExecutionRepository implementation.
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4
import time

from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.agent.infrastructure.persistence.agent_execution_repository import (
    InMemoryAgentExecutionRepository,
)
from tests.utils import generate_case_id


def generate_execution_id() -> str:
    """Generate a valid execution ID."""
    return f"exec_{uuid4().hex[:12]}"


def generate_tool_call_id() -> str:
    """Generate a valid tool call ID."""
    return f"tc_{uuid4().hex[:12]}"


def create_sample_execution(
    case_id: str = None,
    agent_type: AgentType = AgentType.INVESTIGATOR,
    status: ExecutionStatus = ExecutionStatus.QUEUED,
) -> AgentExecution:
    """Create a sample agent execution for testing."""
    return AgentExecution(
        execution_id=generate_execution_id(),
        case_id=case_id or generate_case_id(),
        agent_type=agent_type,
        agent_model="gpt-4",
        status=status,
        prompt="Investigate this issue",
    )


def create_sample_tool_call(
    execution_id: str,
    tool_name: str = "web_search",
    status: str = "pending",
) -> AgentToolCall:
    """Create a sample tool call for testing."""
    return AgentToolCall(
        tool_call_id=generate_tool_call_id(),
        execution_id=execution_id,
        tool_name=tool_name,
        tool_input={"query": "test query"},
        status=status,
    )


@pytest.fixture
def repository():
    """Create a fresh in-memory repository for each test."""
    return InMemoryAgentExecutionRepository()


class TestExecutionCreation:
    """Tests for agent execution creation."""

    @pytest.mark.asyncio
    async def test_create_execution_success(self, repository):
        """Test creating an agent execution successfully."""
        execution = create_sample_execution()

        result = await repository.create_execution(execution)

        assert result is not None
        assert result.execution_id == execution.execution_id
        assert result.case_id == execution.case_id
        assert result.agent_type == AgentType.INVESTIGATOR
        assert result.agent_model == "gpt-4"

    @pytest.mark.asyncio
    async def test_create_execution_duplicate_id_fails(self, repository):
        """Test that creating execution with duplicate ID fails."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        # Try to create with same ID
        duplicate = create_sample_execution()
        duplicate.execution_id = execution.execution_id

        with pytest.raises(ValueError, match="already exists"):
            await repository.create_execution(duplicate)

    @pytest.mark.asyncio
    async def test_create_execution_preserves_all_fields(self, repository):
        """Test that all fields are preserved when creating execution."""
        execution = AgentExecution(
            execution_id="exec_fullfields123",
            case_id="case_123",
            agent_type=AgentType.DEBUGGER,
            agent_model="claude-3-opus",
            status=ExecutionStatus.RUNNING,
            prompt="Debug this code",
            token_usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
            metadata={"session_id": "sess_123"},
        )

        result = await repository.create_execution(execution)

        assert result.agent_type == AgentType.DEBUGGER
        assert result.agent_model == "claude-3-opus"
        assert result.prompt == "Debug this code"
        assert result.token_usage == {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
        assert result.metadata == {"session_id": "sess_123"}


class TestExecutionRetrieval:
    """Tests for agent execution retrieval."""

    @pytest.mark.asyncio
    async def test_get_execution_found(self, repository):
        """Test retrieving an existing agent execution."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        result = await repository.get_execution(execution.execution_id)

        assert result is not None
        assert result.execution_id == execution.execution_id

    @pytest.mark.asyncio
    async def test_get_execution_not_found(self, repository):
        """Test retrieving a non-existent agent execution."""
        result = await repository.get_execution("exec_nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_execution_returns_copy(self, repository):
        """Test that get_execution returns a copy, not the original."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        result1 = await repository.get_execution(execution.execution_id)
        result1.prompt = "Modified"

        result2 = await repository.get_execution(execution.execution_id)

        assert result2.prompt == "Investigate this issue"  # Original unchanged

    @pytest.mark.asyncio
    async def test_get_execution_includes_tool_calls(self, repository):
        """Test that get_execution includes associated tool calls."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        # Create tool calls
        tc1 = create_sample_tool_call(execution.execution_id, "web_search")
        tc2 = create_sample_tool_call(execution.execution_id, "code_exec")
        await repository.create_tool_call(tc1)
        await repository.create_tool_call(tc2)

        result = await repository.get_execution(execution.execution_id)

        assert len(result.tool_calls) == 2
        tool_names = {tc.tool_name for tc in result.tool_calls}
        assert tool_names == {"web_search", "code_exec"}


class TestExecutionUpdate:
    """Tests for agent execution updates."""

    @pytest.mark.asyncio
    async def test_update_execution_success(self, repository):
        """Test updating an agent execution successfully."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        execution.status = ExecutionStatus.RUNNING
        execution.mark_started()

        result = await repository.update_execution(execution)

        assert result.status == ExecutionStatus.RUNNING
        assert result.started_at is not None

    @pytest.mark.asyncio
    async def test_update_execution_not_found_fails(self, repository):
        """Test that updating non-existent execution fails."""
        execution = create_sample_execution()

        with pytest.raises(ValueError, match="not found"):
            await repository.update_execution(execution)

    @pytest.mark.asyncio
    async def test_update_execution_updates_timestamp(self, repository):
        """Test that update modifies updated_at timestamp."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        original_updated = execution.updated_at

        time.sleep(0.001)

        execution.status = ExecutionStatus.RUNNING
        result = await repository.update_execution(execution)

        assert result.updated_at > original_updated

    @pytest.mark.asyncio
    async def test_update_execution_preserves_tool_calls(self, repository):
        """Test that update preserves associated tool calls."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        # Create tool call
        tc = create_sample_tool_call(execution.execution_id)
        await repository.create_tool_call(tc)

        # Update execution
        execution.status = ExecutionStatus.RUNNING
        result = await repository.update_execution(execution)

        # Verify tool call is still associated
        assert len(result.tool_calls) == 1


class TestExecutionDeletion:
    """Tests for agent execution deletion."""

    @pytest.mark.asyncio
    async def test_delete_execution_success(self, repository):
        """Test deleting an agent execution successfully."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        result = await repository.delete_execution(execution.execution_id)

        assert result is True

        # Verify deletion
        retrieved = await repository.get_execution(execution.execution_id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_execution_not_found(self, repository):
        """Test deleting a non-existent agent execution."""
        result = await repository.delete_execution("exec_nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_execution_cascades_tool_calls(self, repository):
        """Test that deleting execution also deletes its tool calls."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        # Create tool calls
        tc1 = create_sample_tool_call(execution.execution_id)
        tc2 = create_sample_tool_call(execution.execution_id)
        await repository.create_tool_call(tc1)
        await repository.create_tool_call(tc2)

        # Delete execution
        await repository.delete_execution(execution.execution_id)

        # Verify tool calls are also deleted
        tool_calls = await repository.get_tool_calls_for_execution(execution.execution_id)
        assert len(tool_calls) == 0


class TestExecutionListByCase:
    """Tests for listing executions by case."""

    @pytest.mark.asyncio
    async def test_list_executions_by_case_empty(self, repository):
        """Test listing executions for a case with no executions."""
        result, total = await repository.list_executions_by_case("case_empty")

        assert result == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_executions_by_case_multiple(self, repository):
        """Test listing multiple executions for a case."""
        case_id = generate_case_id()

        # Create 5 executions
        for i in range(5):
            execution = create_sample_execution(case_id=case_id)
            await repository.create_execution(execution)

        result, total = await repository.list_executions_by_case(case_id)

        assert len(result) == 5
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_executions_by_case_with_status_filter(self, repository):
        """Test listing executions filtered by status."""
        case_id = generate_case_id()

        # Create executions with different statuses
        for i in range(3):
            execution = create_sample_execution(
                case_id=case_id,
                status=ExecutionStatus.COMPLETED,
            )
            await repository.create_execution(execution)

        for i in range(2):
            execution = create_sample_execution(
                case_id=case_id,
                status=ExecutionStatus.FAILED,
            )
            await repository.create_execution(execution)

        # Filter by completed
        completed, total = await repository.list_executions_by_case(
            case_id,
            status=ExecutionStatus.COMPLETED,
        )

        assert len(completed) == 3
        assert total == 3
        assert all(e.status == ExecutionStatus.COMPLETED for e in completed)

    @pytest.mark.asyncio
    async def test_list_executions_by_case_with_agent_type_filter(self, repository):
        """Test listing executions filtered by agent type."""
        case_id = generate_case_id()

        # Create executions with different agent types
        for i in range(3):
            execution = create_sample_execution(
                case_id=case_id,
                agent_type=AgentType.INVESTIGATOR,
            )
            await repository.create_execution(execution)

        for i in range(2):
            execution = create_sample_execution(
                case_id=case_id,
                agent_type=AgentType.DEBUGGER,
            )
            await repository.create_execution(execution)

        # Filter by investigator
        investigators, total = await repository.list_executions_by_case(
            case_id,
            agent_type=AgentType.INVESTIGATOR,
        )

        assert len(investigators) == 3
        assert total == 3
        assert all(e.agent_type == AgentType.INVESTIGATOR for e in investigators)

    @pytest.mark.asyncio
    async def test_list_executions_by_case_pagination(self, repository):
        """Test pagination of execution listing."""
        case_id = generate_case_id()

        # Create 10 executions
        for i in range(10):
            execution = create_sample_execution(case_id=case_id)
            await repository.create_execution(execution)

        # Get first page (5 items)
        page1, total = await repository.list_executions_by_case(
            case_id,
            limit=5,
            offset=0,
        )

        assert len(page1) == 5
        assert total == 10

        # Get second page (5 items)
        page2, total = await repository.list_executions_by_case(
            case_id,
            limit=5,
            offset=5,
        )

        assert len(page2) == 5
        assert total == 10

        # Ensure no overlap
        page1_ids = {e.execution_id for e in page1}
        page2_ids = {e.execution_id for e in page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_list_executions_ordered_by_created_at(self, repository):
        """Test that executions are ordered by created_at descending."""
        case_id = generate_case_id()

        execution_ids = []
        for i in range(3):
            execution = create_sample_execution(case_id=case_id)
            await repository.create_execution(execution)
            execution_ids.append(execution.execution_id)
            time.sleep(0.001)

        result, _ = await repository.list_executions_by_case(case_id)

        # Most recent should be first
        assert result[0].execution_id == execution_ids[-1]
        assert result[-1].execution_id == execution_ids[0]

    @pytest.mark.asyncio
    async def test_list_executions_excludes_other_cases(self, repository):
        """Test that listing only returns executions for specified case."""
        case_id_1 = generate_case_id()
        case_id_2 = generate_case_id()

        # Create executions for case 1
        for i in range(3):
            execution = create_sample_execution(case_id=case_id_1)
            await repository.create_execution(execution)

        # Create executions for case 2
        for i in range(2):
            execution = create_sample_execution(case_id=case_id_2)
            await repository.create_execution(execution)

        result1, total1 = await repository.list_executions_by_case(case_id_1)
        result2, total2 = await repository.list_executions_by_case(case_id_2)

        assert len(result1) == 3
        assert total1 == 3
        assert len(result2) == 2
        assert total2 == 2


class TestToolCallCreation:
    """Tests for tool call creation."""

    @pytest.mark.asyncio
    async def test_create_tool_call_success(self, repository):
        """Test creating a tool call successfully."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        tool_call = create_sample_tool_call(execution.execution_id)

        result = await repository.create_tool_call(tool_call)

        assert result is not None
        assert result.tool_call_id == tool_call.tool_call_id
        assert result.tool_name == "web_search"

    @pytest.mark.asyncio
    async def test_create_tool_call_duplicate_id_fails(self, repository):
        """Test that creating tool call with duplicate ID fails."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        tool_call = create_sample_tool_call(execution.execution_id)
        await repository.create_tool_call(tool_call)

        # Try to create with same ID
        duplicate = create_sample_tool_call(execution.execution_id)
        duplicate.tool_call_id = tool_call.tool_call_id

        with pytest.raises(ValueError, match="already exists"):
            await repository.create_tool_call(duplicate)

    @pytest.mark.asyncio
    async def test_create_tool_call_invalid_execution_fails(self, repository):
        """Test that creating tool call with invalid execution ID fails."""
        tool_call = create_sample_tool_call("exec_nonexistent")

        with pytest.raises(ValueError, match="not found"):
            await repository.create_tool_call(tool_call)


class TestToolCallUpdate:
    """Tests for tool call updates."""

    @pytest.mark.asyncio
    async def test_update_tool_call_success(self, repository):
        """Test updating a tool call successfully."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        tool_call = create_sample_tool_call(execution.execution_id)
        await repository.create_tool_call(tool_call)

        tool_call.status = "success"
        tool_call.tool_output = {"result": "found"}

        result = await repository.update_tool_call(tool_call)

        assert result.status == "success"
        assert result.tool_output == {"result": "found"}

    @pytest.mark.asyncio
    async def test_update_tool_call_not_found_fails(self, repository):
        """Test that updating non-existent tool call fails."""
        tool_call = AgentToolCall(
            tool_call_id="tc_nonexistent",
            execution_id="exec_123",
            tool_name="test",
        )

        with pytest.raises(ValueError, match="not found"):
            await repository.update_tool_call(tool_call)


class TestGetToolCallsForExecution:
    """Tests for getting tool calls for an execution."""

    @pytest.mark.asyncio
    async def test_get_tool_calls_empty(self, repository):
        """Test getting tool calls when none exist."""
        result = await repository.get_tool_calls_for_execution("exec_empty")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_tool_calls_multiple(self, repository):
        """Test getting multiple tool calls for an execution."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        # Create 3 tool calls
        for i in range(3):
            tc = create_sample_tool_call(execution.execution_id, f"tool_{i}")
            await repository.create_tool_call(tc)

        result = await repository.get_tool_calls_for_execution(execution.execution_id)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_tool_calls_ordered_by_created_at(self, repository):
        """Test that tool calls are ordered by created_at ascending."""
        execution = create_sample_execution()
        await repository.create_execution(execution)

        tc_ids = []
        for i in range(3):
            tc = create_sample_tool_call(execution.execution_id, f"tool_{i}")
            await repository.create_tool_call(tc)
            tc_ids.append(tc.tool_call_id)
            time.sleep(0.001)

        result = await repository.get_tool_calls_for_execution(execution.execution_id)

        # Oldest should be first
        assert result[0].tool_call_id == tc_ids[0]
        assert result[-1].tool_call_id == tc_ids[-1]


class TestExecutionCount:
    """Tests for counting executions by case."""

    @pytest.mark.asyncio
    async def test_count_executions_empty(self, repository):
        """Test counting executions for case with no executions."""
        count = await repository.count_executions_by_case("case_empty")

        assert count == 0

    @pytest.mark.asyncio
    async def test_count_executions_multiple(self, repository):
        """Test counting executions for case with multiple items."""
        case_id = generate_case_id()

        # Create 5 executions
        for i in range(5):
            execution = create_sample_execution(case_id=case_id)
            await repository.create_execution(execution)

        count = await repository.count_executions_by_case(case_id)

        assert count == 5

    @pytest.mark.asyncio
    async def test_count_excludes_other_cases(self, repository):
        """Test that count only includes executions for specified case."""
        case_id_1 = generate_case_id()
        case_id_2 = generate_case_id()

        # Create executions for case 1
        for i in range(3):
            execution = create_sample_execution(case_id=case_id_1)
            await repository.create_execution(execution)

        # Create executions for case 2
        for i in range(2):
            execution = create_sample_execution(case_id=case_id_2)
            await repository.create_execution(execution)

        count1 = await repository.count_executions_by_case(case_id_1)
        count2 = await repository.count_executions_by_case(case_id_2)

        assert count1 == 3
        assert count2 == 2


class TestLatestExecution:
    """Tests for getting the latest execution."""

    @pytest.mark.asyncio
    async def test_get_latest_execution_none(self, repository):
        """Test getting latest execution when none exist."""
        result = await repository.get_latest_execution("case_empty")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_latest_execution_success(self, repository):
        """Test getting the most recent execution."""
        case_id = generate_case_id()

        execution_ids = []
        for i in range(3):
            execution = create_sample_execution(case_id=case_id)
            await repository.create_execution(execution)
            execution_ids.append(execution.execution_id)
            time.sleep(0.001)

        result = await repository.get_latest_execution(case_id)

        assert result is not None
        assert result.execution_id == execution_ids[-1]  # Most recent

    @pytest.mark.asyncio
    async def test_get_latest_execution_with_agent_type(self, repository):
        """Test getting latest execution filtered by agent type."""
        case_id = generate_case_id()

        # Create investigator execution (older)
        exec1 = create_sample_execution(case_id=case_id, agent_type=AgentType.INVESTIGATOR)
        await repository.create_execution(exec1)
        time.sleep(0.001)

        # Create debugger execution (newer)
        exec2 = create_sample_execution(case_id=case_id, agent_type=AgentType.DEBUGGER)
        await repository.create_execution(exec2)
        time.sleep(0.001)

        # Create another investigator execution (newest)
        exec3 = create_sample_execution(case_id=case_id, agent_type=AgentType.INVESTIGATOR)
        await repository.create_execution(exec3)

        # Get latest investigator
        result = await repository.get_latest_execution(case_id, AgentType.INVESTIGATOR)

        assert result is not None
        assert result.execution_id == exec3.execution_id

    @pytest.mark.asyncio
    async def test_get_latest_execution_includes_tool_calls(self, repository):
        """Test that latest execution includes its tool calls."""
        case_id = generate_case_id()

        execution = create_sample_execution(case_id=case_id)
        await repository.create_execution(execution)

        tc = create_sample_tool_call(execution.execution_id)
        await repository.create_tool_call(tc)

        result = await repository.get_latest_execution(case_id)

        assert len(result.tool_calls) == 1


class TestRepositoryClear:
    """Tests for repository clearing."""

    @pytest.mark.asyncio
    async def test_clear_removes_all_data(self, repository):
        """Test that clear removes all executions and tool calls."""
        # Create executions with tool calls
        for i in range(3):
            execution = create_sample_execution()
            await repository.create_execution(execution)
            tc = create_sample_tool_call(execution.execution_id)
            await repository.create_tool_call(tc)

        # Clear repository
        repository.clear()

        # Verify all removed
        count = await repository.count_executions_by_case("case_any")
        assert count == 0


class TestCascadeDeleteForCase:
    """Tests for cascade deletion when case is deleted."""

    @pytest.mark.asyncio
    async def test_delete_executions_for_case(self, repository):
        """Test deleting all executions for a case (simulates cascade)."""
        case_id = generate_case_id()

        # Create 3 executions with tool calls
        for i in range(3):
            execution = create_sample_execution(case_id=case_id)
            await repository.create_execution(execution)
            tc = create_sample_tool_call(execution.execution_id)
            await repository.create_tool_call(tc)

        # Also create execution for another case
        other_execution = create_sample_execution()
        await repository.create_execution(other_execution)

        # Delete executions for case
        deleted_count = repository.delete_executions_for_case(case_id)

        assert deleted_count == 3

        # Verify case executions are gone
        count = await repository.count_executions_by_case(case_id)
        assert count == 0

        # Verify other case execution remains
        other = await repository.get_execution(other_execution.execution_id)
        assert other is not None

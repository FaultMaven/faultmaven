"""Unit tests for CaseRepository agent execution methods.

Tests the new agent execution persistence methods added to CaseRepository:
- create_agent_execution
- get_agent_execution
- list_agent_executions_by_case
- list_agent_executions_by_session
- update_agent_execution
- delete_agent_execution
- create_agent_tool_call
- update_agent_tool_call
- get_agent_tool_calls_for_execution
- count_agent_executions_by_case
- get_latest_agent_execution

Tests InMemoryCaseRepository implementation.

Run with:
    pytest tests/unit/infrastructure/persistence/test_case_repository_agent_executions.py -v
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
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
    execution_id: str = None,
) -> AgentExecution:
    """Create a sample agent execution for testing."""
    return AgentExecution(
        execution_id=execution_id or generate_execution_id(),
        case_id=case_id or generate_case_id(),
        agent_type=agent_type,
        agent_model="gpt-4",
        status=status,
        prompt="Investigate this issue",
        tool_calls=[],  # Tool calls stored separately
    )


def create_sample_tool_call(
    execution_id: str,
    tool_name: str = "web_search",
    status: str = "pending",
    tool_call_id: str = None,
) -> AgentToolCall:
    """Create a sample tool call for testing."""
    return AgentToolCall(
        tool_call_id=tool_call_id or generate_tool_call_id(),
        execution_id=execution_id,
        tool_name=tool_name,
        tool_input={"query": "test query"},
        status=status,
    )


@pytest.fixture
def inmemory_repository() -> InMemoryCaseRepository:
    """Create InMemoryCaseRepository instance."""
    return InMemoryCaseRepository()


# ============================================================
# Test Agent Execution Creation
# ============================================================

@pytest.mark.asyncio
async def test_create_agent_execution_success(inmemory_repository: InMemoryCaseRepository):
    """Test creating agent execution successfully."""
    execution = create_sample_execution()
    
    result = await inmemory_repository.create_agent_execution(execution)
    
    assert result is not None
    assert result.execution_id == execution.execution_id
    assert result.case_id == execution.case_id
    assert result.agent_type == AgentType.INVESTIGATOR
    assert result.agent_model == "gpt-4"
    assert result.tool_calls == []  # Tool calls stored separately, empty initially


@pytest.mark.asyncio
async def test_create_agent_execution_duplicate_id_fails(inmemory_repository: InMemoryCaseRepository):
    """Test that creating execution with duplicate ID fails."""
    execution = create_sample_execution()
    await inmemory_repository.create_agent_execution(execution)
    
    # Try to create with same ID
    duplicate = create_sample_execution()
    duplicate.execution_id = execution.execution_id
    
    with pytest.raises(ValueError, match="already exists"):
        await inmemory_repository.create_agent_execution(duplicate)


@pytest.mark.asyncio
async def test_create_agent_execution_preserves_fields(inmemory_repository: InMemoryCaseRepository):
    """Test that all execution fields are preserved."""
    execution = create_sample_execution(
        agent_type=AgentType.DEBUGGER,
        status=ExecutionStatus.RUNNING,
    )
    
    result = await inmemory_repository.create_agent_execution(execution)
    
    assert result.agent_type == AgentType.DEBUGGER
    assert result.status == ExecutionStatus.RUNNING
    assert result.prompt == "Investigate this issue"


# ============================================================
# Test Get Agent Execution
# ============================================================

@pytest.mark.asyncio
async def test_get_agent_execution_success(inmemory_repository: InMemoryCaseRepository):
    """Test getting agent execution by ID."""
    execution = create_sample_execution()
    created = await inmemory_repository.create_agent_execution(execution)
    
    retrieved = await inmemory_repository.get_agent_execution(created.execution_id)
    
    assert retrieved is not None
    assert retrieved.execution_id == created.execution_id
    assert retrieved.case_id == created.case_id
    assert retrieved.tool_calls == []  # No tool calls added yet


@pytest.mark.asyncio
async def test_get_agent_execution_with_tool_calls(inmemory_repository: InMemoryCaseRepository):
    """Test getting agent execution with tool calls attached."""
    execution = create_sample_execution()
    created = await inmemory_repository.create_agent_execution(execution)
    
    # Add tool calls
    tool_call1 = create_sample_tool_call(created.execution_id, "web_search", "success")
    tool_call2 = create_sample_tool_call(created.execution_id, "code_exec", "pending")
    await inmemory_repository.create_agent_tool_call(tool_call1)
    await inmemory_repository.create_agent_tool_call(tool_call2)
    
    # Retrieve execution - should have tool calls attached
    retrieved = await inmemory_repository.get_agent_execution(created.execution_id)
    
    assert retrieved is not None
    assert len(retrieved.tool_calls) == 2
    tool_call_ids = {tc.tool_call_id for tc in retrieved.tool_calls}
    assert tool_call1.tool_call_id in tool_call_ids
    assert tool_call2.tool_call_id in tool_call_ids
    
    # Tool calls should be sorted by created_at
    assert retrieved.tool_calls[0].created_at <= retrieved.tool_calls[1].created_at


@pytest.mark.asyncio
async def test_get_agent_execution_not_found(inmemory_repository: InMemoryCaseRepository):
    """Test getting non-existent agent execution."""
    result = await inmemory_repository.get_agent_execution("nonexistent-id")
    assert result is None


# ============================================================
# Test List Agent Executions by Case
# ============================================================

@pytest.mark.asyncio
async def test_list_agent_executions_by_case_all(inmemory_repository: InMemoryCaseRepository):
    """Test listing all agent executions for a case."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id, agent_type=AgentType.INVESTIGATOR)
    execution2 = create_sample_execution(case_id=case_id, agent_type=AgentType.DEBUGGER)
    execution3 = create_sample_execution(case_id=generate_case_id())  # Different case
    
    await inmemory_repository.create_agent_execution(execution1)
    await inmemory_repository.create_agent_execution(execution2)
    await inmemory_repository.create_agent_execution(execution3)
    
    results, total = await inmemory_repository.list_agent_executions_by_case(case_id)
    
    assert total == 2
    execution_ids = {e.execution_id for e in results}
    assert execution1.execution_id in execution_ids
    assert execution2.execution_id in execution_ids
    assert execution3.execution_id not in execution_ids


@pytest.mark.asyncio
async def test_list_agent_executions_by_case_filter_by_status(inmemory_repository: InMemoryCaseRepository):
    """Test filtering agent executions by status."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id, status=ExecutionStatus.COMPLETED)
    execution2 = create_sample_execution(case_id=case_id, status=ExecutionStatus.FAILED)
    execution3 = create_sample_execution(case_id=case_id, status=ExecutionStatus.QUEUED)
    
    await inmemory_repository.create_agent_execution(execution1)
    await inmemory_repository.create_agent_execution(execution2)
    await inmemory_repository.create_agent_execution(execution3)
    
    # ExecutionStatus is str, Enum - can pass enum directly (comparison works)
    results, total = await inmemory_repository.list_agent_executions_by_case(
        case_id, status=ExecutionStatus.COMPLETED
    )
    
    assert total == 1
    assert results[0].execution_id == execution1.execution_id
    assert results[0].status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_list_agent_executions_by_case_filter_by_agent_type(inmemory_repository: InMemoryCaseRepository):
    """Test filtering agent executions by agent type."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id, agent_type=AgentType.INVESTIGATOR)
    execution2 = create_sample_execution(case_id=case_id, agent_type=AgentType.DEBUGGER)
    
    await inmemory_repository.create_agent_execution(execution1)
    await inmemory_repository.create_agent_execution(execution2)
    
    # AgentType is str, Enum - can pass enum directly (comparison works)
    results, total = await inmemory_repository.list_agent_executions_by_case(
        case_id, agent_type=AgentType.INVESTIGATOR
    )
    
    assert total == 1
    assert results[0].execution_id == execution1.execution_id
    assert results[0].agent_type == AgentType.INVESTIGATOR


@pytest.mark.asyncio
async def test_list_agent_executions_by_case_pagination(inmemory_repository: InMemoryCaseRepository):
    """Test pagination for listing agent executions."""
    case_id = generate_case_id()
    # Create 5 executions
    executions = []
    for i in range(5):
        exec = create_sample_execution(case_id=case_id)
        await inmemory_repository.create_agent_execution(exec)
        executions.append(exec)
    
    # First page
    results1, total = await inmemory_repository.list_agent_executions_by_case(
        case_id, limit=2, offset=0
    )
    assert len(results1) == 2
    assert total == 5
    
    # Second page
    results2, total2 = await inmemory_repository.list_agent_executions_by_case(
        case_id, limit=2, offset=2
    )
    assert len(results2) == 2
    assert total2 == total


@pytest.mark.asyncio
async def test_list_agent_executions_by_case_sorted_by_created_at(inmemory_repository: InMemoryCaseRepository):
    """Test that executions are sorted by created_at descending."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id)
    await inmemory_repository.create_agent_execution(execution1)
    # Small delay to ensure different timestamps
    import asyncio
    await asyncio.sleep(0.01)
    
    execution2 = create_sample_execution(case_id=case_id)
    await inmemory_repository.create_agent_execution(execution2)
    
    results, total = await inmemory_repository.list_agent_executions_by_case(case_id)
    
    # Most recent should be first
    assert results[0].execution_id == execution2.execution_id
    assert results[1].execution_id == execution1.execution_id


# ============================================================
# Test List Agent Executions by Session
# ============================================================

@pytest.mark.asyncio
async def test_list_agent_executions_by_session(inmemory_repository: InMemoryCaseRepository):
    """Test listing agent executions by session_id."""
    session_id1 = f"sess_{uuid4().hex[:12]}"
    session_id2 = f"sess_{uuid4().hex[:12]}"
    
    # Create executions with session_id in metadata
    execution1 = create_sample_execution()
    execution1.metadata = {"session_id": session_id1}
    execution2 = create_sample_execution()
    execution2.metadata = {"session_id": session_id1}
    execution3 = create_sample_execution()
    execution3.metadata = {"session_id": session_id2}
    
    await inmemory_repository.create_agent_execution(execution1)
    await inmemory_repository.create_agent_execution(execution2)
    await inmemory_repository.create_agent_execution(execution3)
    
    results, total = await inmemory_repository.list_agent_executions_by_session(session_id1)
    
    assert total == 2
    execution_ids = {e.execution_id for e in results}
    assert execution1.execution_id in execution_ids
    assert execution2.execution_id in execution_ids
    assert execution3.execution_id not in execution_ids
    
    # Verify metadata is preserved
    for exec in results:
        assert exec.metadata is not None
        assert exec.metadata.get("session_id") == session_id1


# ============================================================
# Test Update Agent Execution
# ============================================================

@pytest.mark.asyncio
async def test_update_agent_execution_success(inmemory_repository: InMemoryCaseRepository):
    """Test updating agent execution."""
    execution = create_sample_execution(status=ExecutionStatus.QUEUED)
    created = await inmemory_repository.create_agent_execution(execution)
    
    # Update status and response
    created.status = ExecutionStatus.COMPLETED
    created.response = "Investigation complete"
    
    updated = await inmemory_repository.update_agent_execution(created)
    
    assert updated.status == ExecutionStatus.COMPLETED
    assert updated.response == "Investigation complete"
    assert updated.updated_at is not None
    
    # Verify stored
    retrieved = await inmemory_repository.get_agent_execution(created.execution_id)
    assert retrieved.status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_update_agent_execution_preserves_tool_calls(inmemory_repository: InMemoryCaseRepository):
    """Test that updating execution preserves tool calls."""
    execution = create_sample_execution()
    created = await inmemory_repository.create_agent_execution(execution)
    
    # Add tool call
    tool_call = create_sample_tool_call(created.execution_id)
    await inmemory_repository.create_agent_tool_call(tool_call)
    
    # Update execution
    created.status = ExecutionStatus.COMPLETED
    updated = await inmemory_repository.update_agent_execution(created)
    
    # Tool calls should still be attached
    assert len(updated.tool_calls) == 1
    assert updated.tool_calls[0].tool_call_id == tool_call.tool_call_id


@pytest.mark.asyncio
async def test_update_agent_execution_not_found(inmemory_repository: InMemoryCaseRepository):
    """Test updating non-existent agent execution."""
    execution = create_sample_execution()
    execution.execution_id = "nonexistent-id"
    
    with pytest.raises(ValueError, match="not found"):
        await inmemory_repository.update_agent_execution(execution)


# ============================================================
# Test Delete Agent Execution
# ============================================================

@pytest.mark.asyncio
async def test_delete_agent_execution_success(inmemory_repository: InMemoryCaseRepository):
    """Test deleting agent execution (should cascade to tool calls)."""
    execution = create_sample_execution()
    created = await inmemory_repository.create_agent_execution(execution)
    
    # Add tool calls
    tool_call = create_sample_tool_call(created.execution_id)
    await inmemory_repository.create_agent_tool_call(tool_call)
    
    # Delete execution
    deleted = await inmemory_repository.delete_agent_execution(created.execution_id)
    assert deleted is True
    
    # Execution should be gone
    retrieved = await inmemory_repository.get_agent_execution(created.execution_id)
    assert retrieved is None
    
    # Tool calls should also be deleted (cascaded)
    tool_calls = await inmemory_repository.get_agent_tool_calls_for_execution(created.execution_id)
    assert len(tool_calls) == 0


@pytest.mark.asyncio
async def test_delete_agent_execution_not_found(inmemory_repository: InMemoryCaseRepository):
    """Test deleting non-existent agent execution."""
    deleted = await inmemory_repository.delete_agent_execution("nonexistent-id")
    assert deleted is False


# ============================================================
# Test Agent Tool Call Operations
# ============================================================

@pytest.mark.asyncio
async def test_create_agent_tool_call_success(inmemory_repository: InMemoryCaseRepository):
    """Test creating agent tool call."""
    execution = create_sample_execution()
    await inmemory_repository.create_agent_execution(execution)
    
    tool_call = create_sample_tool_call(execution.execution_id, "web_search")
    
    result = await inmemory_repository.create_agent_tool_call(tool_call)
    
    assert result is not None
    assert result.tool_call_id == tool_call.tool_call_id
    assert result.execution_id == execution.execution_id
    assert result.tool_name == "web_search"
    assert result.status == "pending"


@pytest.mark.asyncio
async def test_create_agent_tool_call_duplicate_id_fails(inmemory_repository: InMemoryCaseRepository):
    """Test that creating tool call with duplicate ID fails."""
    execution = create_sample_execution()
    await inmemory_repository.create_agent_execution(execution)
    
    tool_call = create_sample_tool_call(execution.execution_id)
    await inmemory_repository.create_agent_tool_call(tool_call)
    
    # Try to create with same ID
    duplicate = create_sample_tool_call(execution.execution_id)
    duplicate.tool_call_id = tool_call.tool_call_id
    
    with pytest.raises(ValueError, match="already exists"):
        await inmemory_repository.create_agent_tool_call(duplicate)


@pytest.mark.asyncio
async def test_create_agent_tool_call_invalid_execution_fails(inmemory_repository: InMemoryCaseRepository):
    """Test that creating tool call with invalid execution ID fails."""
    tool_call = create_sample_tool_call("nonexistent-execution-id")
    
    with pytest.raises(ValueError, match="Execution .* not found"):
        await inmemory_repository.create_agent_tool_call(tool_call)


@pytest.mark.asyncio
async def test_update_agent_tool_call_success(inmemory_repository: InMemoryCaseRepository):
    """Test updating agent tool call."""
    execution = create_sample_execution()
    await inmemory_repository.create_agent_execution(execution)
    
    tool_call = create_sample_tool_call(execution.execution_id, status="pending")
    created = await inmemory_repository.create_agent_tool_call(tool_call)
    
    # Update tool call
    created.status = "success"
    created.tool_output = {"result": "Search completed"}
    
    updated = await inmemory_repository.update_agent_tool_call(created)
    
    assert updated.status == "success"
    assert updated.tool_output == {"result": "Search completed"}
    assert updated.updated_at is not None


@pytest.mark.asyncio
async def test_update_agent_tool_call_not_found(inmemory_repository: InMemoryCaseRepository):
    """Test updating non-existent tool call."""
    tool_call = create_sample_tool_call("exec-123")
    tool_call.tool_call_id = "nonexistent-id"
    
    with pytest.raises(ValueError, match="not found"):
        await inmemory_repository.update_agent_tool_call(tool_call)


@pytest.mark.asyncio
async def test_get_agent_tool_calls_for_execution(inmemory_repository: InMemoryCaseRepository):
    """Test getting all tool calls for an execution."""
    execution = create_sample_execution()
    await inmemory_repository.create_agent_execution(execution)
    
    # Create another execution for tool_call3
    other_execution = create_sample_execution()
    await inmemory_repository.create_agent_execution(other_execution)
    
    tool_call1 = create_sample_tool_call(execution.execution_id, "web_search")
    tool_call2 = create_sample_tool_call(execution.execution_id, "code_exec")
    tool_call3 = create_sample_tool_call(other_execution.execution_id, "web_search")  # Different execution
    
    await inmemory_repository.create_agent_tool_call(tool_call1)
    await inmemory_repository.create_agent_tool_call(tool_call2)
    await inmemory_repository.create_agent_tool_call(tool_call3)
    
    tool_calls = await inmemory_repository.get_agent_tool_calls_for_execution(execution.execution_id)
    
    assert len(tool_calls) == 2
    tool_call_ids = {tc.tool_call_id for tc in tool_calls}
    assert tool_call1.tool_call_id in tool_call_ids
    assert tool_call2.tool_call_id in tool_call_ids
    assert tool_call3.tool_call_id not in tool_call_ids
    
    # Verify tool_call3 is associated with the other execution
    other_tool_calls = await inmemory_repository.get_agent_tool_calls_for_execution(other_execution.execution_id)
    assert len(other_tool_calls) == 1
    assert other_tool_calls[0].tool_call_id == tool_call3.tool_call_id


# ============================================================
# Test Count and Latest Operations
# ============================================================

@pytest.mark.asyncio
async def test_count_agent_executions_by_case(inmemory_repository: InMemoryCaseRepository):
    """Test counting agent executions for a case."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id)
    execution2 = create_sample_execution(case_id=case_id)
    execution3 = create_sample_execution(case_id=generate_case_id())  # Different case
    
    await inmemory_repository.create_agent_execution(execution1)
    await inmemory_repository.create_agent_execution(execution2)
    await inmemory_repository.create_agent_execution(execution3)
    
    count = await inmemory_repository.count_agent_executions_by_case(case_id)
    assert count == 2


@pytest.mark.asyncio
async def test_get_latest_agent_execution(inmemory_repository: InMemoryCaseRepository):
    """Test getting the latest agent execution for a case."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id)
    await inmemory_repository.create_agent_execution(execution1)
    
    import asyncio
    await asyncio.sleep(0.01)  # Small delay to ensure different timestamps
    
    execution2 = create_sample_execution(case_id=case_id)
    await inmemory_repository.create_agent_execution(execution2)
    
    latest = await inmemory_repository.get_latest_agent_execution(case_id)
    
    assert latest is not None
    assert latest.execution_id == execution2.execution_id  # Most recent


@pytest.mark.asyncio
async def test_get_latest_agent_execution_filter_by_type(inmemory_repository: InMemoryCaseRepository):
    """Test getting latest agent execution filtered by agent type."""
    case_id = generate_case_id()
    execution1 = create_sample_execution(case_id=case_id, agent_type=AgentType.INVESTIGATOR)
    execution2 = create_sample_execution(case_id=case_id, agent_type=AgentType.DEBUGGER)
    
    await inmemory_repository.create_agent_execution(execution1)
    await inmemory_repository.create_agent_execution(execution2)
    
    # AgentType is str, Enum - can pass enum directly (comparison works)
    latest_investigator = await inmemory_repository.get_latest_agent_execution(
        case_id, agent_type=AgentType.INVESTIGATOR
    )
    latest_debugger = await inmemory_repository.get_latest_agent_execution(
        case_id, agent_type=AgentType.DEBUGGER
    )
    
    assert latest_investigator.agent_type == AgentType.INVESTIGATOR
    assert latest_debugger.agent_type == AgentType.DEBUGGER


@pytest.mark.asyncio
async def test_get_latest_agent_execution_not_found(inmemory_repository: InMemoryCaseRepository):
    """Test getting latest agent execution when none exist."""
    case_id = generate_case_id()
    result = await inmemory_repository.get_latest_agent_execution(case_id)
    assert result is None

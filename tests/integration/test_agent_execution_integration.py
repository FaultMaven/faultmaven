"""Integration tests for Agent Execution Repository.

Tests the full case-execution-tool_call relationship with real database operations.

Run with:
    pytest tests/integration/test_agent_execution_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - PostgreSQL for production testing (optional, requires DATABASE_URL)
"""

import os
import pytest
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import uuid4
import time

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database import reset_engine
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.modules.agent.infrastructure.persistence.agent_execution_repository import (
    DatabaseAgentExecutionRepository,
    InMemoryAgentExecutionRepository,
)
from faultmaven.infrastructure.persistence.repository_factory import (
    get_agent_execution_repository_async,
    reset_inmemory_agent_execution_repository,
    STORAGE_TYPE_INMEMORY,
    STORAGE_TYPE_DATABASE,
)
from faultmaven.modules.case.domain.models import Case, CaseStatus, InvestigationStrategy
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)


# ============================================================
# Test Fixtures
# ============================================================


def generate_case_id() -> str:
    """Generate a valid case ID."""
    return f"case_{uuid4().hex[:12]}"


def generate_execution_id() -> str:
    """Generate a valid execution ID."""
    return f"exec_{uuid4().hex[:12]}"


def generate_tool_call_id() -> str:
    """Generate a valid tool call ID."""
    return f"tc_{uuid4().hex[:12]}"


@pytest.fixture(scope="function")
async def test_engine():
    """Create test engine with in-memory SQLite."""
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    reset_engine()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()
    reset_engine()


@pytest.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test session."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
async def case_repository(test_session) -> DatabaseCaseRepository:
    """Create DatabaseCaseRepository with test session."""
    return DatabaseCaseRepository(test_session)


@pytest.fixture(scope="function")
async def execution_repository(test_session) -> DatabaseAgentExecutionRepository:
    """Create DatabaseAgentExecutionRepository with test session."""
    return DatabaseAgentExecutionRepository(test_session)


@pytest.fixture(scope="function")
def inmemory_repository() -> InMemoryAgentExecutionRepository:
    """Create fresh InMemoryAgentExecutionRepository."""
    reset_inmemory_agent_execution_repository()
    return InMemoryAgentExecutionRepository()


@pytest.fixture
async def sample_case(case_repository: DatabaseCaseRepository) -> Case:
    """Create a sample case for testing executions."""
    case = Case(
        case_id=generate_case_id(),
        user_id="integration-test-user",
        organization_id="integration-test-org",
        title="Agent Execution Integration Test Case",
        description="Testing agent execution management",
        status=CaseStatus.INVESTIGATING,
    )
    return await case_repository.save(case)


def create_sample_execution(
    case_id: str,
    agent_type: AgentType = AgentType.INVESTIGATOR,
    status: ExecutionStatus = ExecutionStatus.QUEUED,
) -> AgentExecution:
    """Create a sample agent execution for testing."""
    return AgentExecution(
        execution_id=generate_execution_id(),
        case_id=case_id,
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


# ============================================================
# Full Execution Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_execution_lifecycle(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test create -> update -> retrieve -> delete flow."""
    # Step 1: Create execution
    execution = create_sample_execution(sample_case.case_id)
    created = await execution_repository.create_execution(execution)

    assert created.execution_id == execution.execution_id
    assert created.case_id == sample_case.case_id
    assert created.status == ExecutionStatus.QUEUED

    # Step 2: Retrieve execution
    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert retrieved is not None
    assert retrieved.prompt == "Investigate this issue"

    # Step 3: Update execution (start)
    execution.mark_started()
    updated = await execution_repository.update_execution(execution)
    assert updated.status == ExecutionStatus.RUNNING
    assert updated.started_at is not None

    # Step 4: Update execution (complete)
    execution.mark_completed("Root cause identified: memory leak")
    execution.set_token_usage(100, 200)
    updated = await execution_repository.update_execution(execution)
    assert updated.status == ExecutionStatus.COMPLETED
    assert updated.response == "Root cause identified: memory leak"
    assert updated.token_usage["total_tokens"] == 300

    # Step 5: Delete execution
    deleted = await execution_repository.delete_execution(execution.execution_id)
    assert deleted is True

    # Verify deletion
    final = await execution_repository.get_execution(execution.execution_id)
    assert final is None


# ============================================================
# Case-Execution Relationship Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_preserves_case_foreign_key(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test that execution maintains foreign key to case."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert retrieved.case_id == sample_case.case_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_invalid_case_id_fails(
    execution_repository: DatabaseAgentExecutionRepository,
):
    """Test that creating execution with invalid case_id fails."""
    execution = create_sample_execution("case_nonexistent")

    with pytest.raises(ValueError, match="not found"):
        await execution_repository.create_execution(execution)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_executions_per_case(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test that a case can have multiple executions."""
    # Create 5 executions
    for i in range(5):
        execution = create_sample_execution(sample_case.case_id)
        await execution_repository.create_execution(execution)

    # Verify all created
    result, total = await execution_repository.list_executions_by_case(
        sample_case.case_id
    )
    assert len(result) == 5
    assert total == 5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_cascade_delete_on_case_delete(
    case_repository: DatabaseCaseRepository,
    execution_repository: DatabaseAgentExecutionRepository,
):
    """Test that executions are deleted when case is deleted (CASCADE)."""
    # Create case
    case = Case(
        case_id=generate_case_id(),
        user_id="cascade-test-user",
        organization_id="cascade-test-org",
        title="Cascade Delete Test",
    )
    await case_repository.save(case)

    # Create execution
    execution = create_sample_execution(case.case_id)
    await execution_repository.create_execution(execution)

    # Create tool call
    tool_call = create_sample_tool_call(execution.execution_id)
    await execution_repository.create_tool_call(tool_call)

    # Verify execution exists
    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert retrieved is not None
    assert len(retrieved.tool_calls) == 1

    # Delete case
    await case_repository.delete(case.case_id)

    # Verify execution is deleted (CASCADE)
    retrieved_after = await execution_repository.get_execution(execution.execution_id)
    assert retrieved_after is None

    # Verify tool calls are also deleted
    tool_calls = await execution_repository.get_tool_calls_for_execution(
        execution.execution_id
    )
    assert len(tool_calls) == 0


# ============================================================
# Execution-ToolCall Relationship Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_call_preserves_execution_foreign_key(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test that tool call maintains foreign key to execution."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    tool_call = create_sample_tool_call(execution.execution_id)
    await execution_repository.create_tool_call(tool_call)

    # Get execution with tool calls
    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert len(retrieved.tool_calls) == 1
    assert retrieved.tool_calls[0].execution_id == execution.execution_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_call_invalid_execution_id_fails(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test that creating tool call with invalid execution_id fails."""
    tool_call = create_sample_tool_call("exec_nonexistent")

    with pytest.raises(ValueError, match="not found"):
        await execution_repository.create_tool_call(tool_call)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_tool_calls_per_execution(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test that an execution can have multiple tool calls."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    # Create 5 tool calls
    for i in range(5):
        tool_call = create_sample_tool_call(execution.execution_id, f"tool_{i}")
        await execution_repository.create_tool_call(tool_call)

    # Verify all created
    tool_calls = await execution_repository.get_tool_calls_for_execution(
        execution.execution_id
    )
    assert len(tool_calls) == 5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_call_cascade_delete_on_execution_delete(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test that tool calls are deleted when execution is deleted (CASCADE)."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    # Create tool calls
    tc1 = create_sample_tool_call(execution.execution_id, "web_search")
    tc2 = create_sample_tool_call(execution.execution_id, "code_exec")
    await execution_repository.create_tool_call(tc1)
    await execution_repository.create_tool_call(tc2)

    # Verify tool calls exist
    tool_calls = await execution_repository.get_tool_calls_for_execution(
        execution.execution_id
    )
    assert len(tool_calls) == 2

    # Delete execution
    await execution_repository.delete_execution(execution.execution_id)

    # Verify tool calls are deleted (CASCADE)
    tool_calls_after = await execution_repository.get_tool_calls_for_execution(
        execution.execution_id
    )
    assert len(tool_calls_after) == 0


# ============================================================
# Three-Level CASCADE Delete Tests (Case -> Execution -> ToolCall)
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_three_level_cascade_delete(
    case_repository: DatabaseCaseRepository,
    execution_repository: DatabaseAgentExecutionRepository,
):
    """Test CASCADE delete works across all three levels."""
    # Create case
    case = Case(
        case_id=generate_case_id(),
        user_id="cascade-test-user",
        organization_id="cascade-test-org",
        title="Three Level Cascade Test",
    )
    await case_repository.save(case)

    # Create multiple executions with tool calls
    execution_ids = []
    tool_call_ids = []

    for i in range(3):
        execution = create_sample_execution(case.case_id)
        await execution_repository.create_execution(execution)
        execution_ids.append(execution.execution_id)

        # Create 2 tool calls per execution
        for j in range(2):
            tc = create_sample_tool_call(execution.execution_id, f"tool_{i}_{j}")
            await execution_repository.create_tool_call(tc)
            tool_call_ids.append(tc.tool_call_id)

    # Verify all created
    count = await execution_repository.count_executions_by_case(case.case_id)
    assert count == 3

    for exec_id in execution_ids:
        tool_calls = await execution_repository.get_tool_calls_for_execution(exec_id)
        assert len(tool_calls) == 2

    # Delete case - should cascade to executions and tool calls
    await case_repository.delete(case.case_id)

    # Verify all executions deleted
    for exec_id in execution_ids:
        execution = await execution_repository.get_execution(exec_id)
        assert execution is None

    # Verify execution count is 0
    count = await execution_repository.count_executions_by_case(case.case_id)
    assert count == 0


# ============================================================
# Execution Query Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_executions_with_status_filter(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test filtering executions by status."""
    # Create executions with different statuses
    statuses = [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.RUNNING,
    ]

    for status in statuses:
        execution = create_sample_execution(sample_case.case_id, status=status)
        await execution_repository.create_execution(execution)

    # Filter by completed
    completed, total = await execution_repository.list_executions_by_case(
        sample_case.case_id,
        status=ExecutionStatus.COMPLETED,
    )
    assert len(completed) == 2
    assert total == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_executions_with_agent_type_filter(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test filtering executions by agent type."""
    # Create executions with different agent types
    types = [
        AgentType.INVESTIGATOR,
        AgentType.INVESTIGATOR,
        AgentType.DEBUGGER,
        AgentType.RESEARCHER,
    ]

    for agent_type in types:
        execution = create_sample_execution(
            sample_case.case_id,
            agent_type=agent_type,
        )
        await execution_repository.create_execution(execution)

    # Filter by investigator
    investigators, total = await execution_repository.list_executions_by_case(
        sample_case.case_id,
        agent_type=AgentType.INVESTIGATOR,
    )
    assert len(investigators) == 2
    assert total == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_executions_pagination(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test execution list pagination."""
    # Create 20 executions
    for i in range(20):
        execution = create_sample_execution(sample_case.case_id)
        await execution_repository.create_execution(execution)

    # Get first page
    page1, total = await execution_repository.list_executions_by_case(
        sample_case.case_id,
        limit=5,
        offset=0,
    )
    assert len(page1) == 5
    assert total == 20

    # Get second page
    page2, total = await execution_repository.list_executions_by_case(
        sample_case.case_id,
        limit=5,
        offset=5,
    )
    assert len(page2) == 5
    assert total == 20

    # Verify no overlap
    page1_ids = {e.execution_id for e in page1}
    page2_ids = {e.execution_id for e in page2}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_latest_execution(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test getting the most recent execution."""
    execution_ids = []
    for i in range(3):
        execution = create_sample_execution(sample_case.case_id)
        await execution_repository.create_execution(execution)
        execution_ids.append(execution.execution_id)
        time.sleep(0.01)

    latest = await execution_repository.get_latest_execution(sample_case.case_id)

    assert latest is not None
    assert latest.execution_id == execution_ids[-1]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_latest_execution_with_agent_type(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test getting latest execution filtered by agent type."""
    # Create investigator (older)
    exec1 = create_sample_execution(
        sample_case.case_id,
        agent_type=AgentType.INVESTIGATOR,
    )
    await execution_repository.create_execution(exec1)
    time.sleep(0.01)

    # Create debugger (newer)
    exec2 = create_sample_execution(
        sample_case.case_id,
        agent_type=AgentType.DEBUGGER,
    )
    await execution_repository.create_execution(exec2)
    time.sleep(0.01)

    # Create investigator (newest)
    exec3 = create_sample_execution(
        sample_case.case_id,
        agent_type=AgentType.INVESTIGATOR,
    )
    await execution_repository.create_execution(exec3)

    # Get latest investigator
    latest = await execution_repository.get_latest_execution(
        sample_case.case_id,
        AgentType.INVESTIGATOR,
    )

    assert latest.execution_id == exec3.execution_id


# ============================================================
# Tool Call Update Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_call_lifecycle(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test tool call create -> start -> complete flow."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    tool_call = create_sample_tool_call(execution.execution_id)
    created = await execution_repository.create_tool_call(tool_call)
    assert created.status == "pending"

    # Start tool call
    tool_call.mark_started()
    updated = await execution_repository.update_tool_call(tool_call)
    assert updated.status == "running"
    assert updated.started_at is not None

    # Complete tool call
    tool_call.mark_success({"results": ["item1", "item2"]})
    completed = await execution_repository.update_tool_call(tool_call)
    assert completed.status == "success"
    assert completed.tool_output == {"results": ["item1", "item2"]}
    assert completed.completed_at is not None
    assert completed.duration_ms is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_call_failure(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test tool call failure scenario."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    tool_call = create_sample_tool_call(execution.execution_id)
    await execution_repository.create_tool_call(tool_call)

    # Start and fail
    tool_call.mark_started()
    tool_call.mark_failed("Connection timeout")

    updated = await execution_repository.update_tool_call(tool_call)
    assert updated.status == "failed"
    assert updated.error_message == "Connection timeout"


# ============================================================
# Data Persistence Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_with_large_prompt(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test execution with large prompt."""
    large_prompt = "Debug this issue:\n" + ("Error details here. " * 1000)

    execution = create_sample_execution(sample_case.case_id)
    execution.prompt = large_prompt
    await execution_repository.create_execution(execution)

    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert retrieved.prompt == large_prompt


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_with_large_response(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test execution with large response."""
    large_response = "Analysis results:\n" + ("Detailed analysis. " * 1000)

    execution = create_sample_execution(sample_case.case_id)
    execution.response = large_response
    execution.status = ExecutionStatus.COMPLETED
    await execution_repository.create_execution(execution)

    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert retrieved.response == large_response


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_with_complex_metadata(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test execution with complex metadata JSON."""
    metadata = {
        "session_id": "sess_12345",
        "user_preferences": {"language": "en", "format": "detailed"},
        "context": {
            "previous_executions": ["exec_001", "exec_002"],
            "related_cases": ["case_abc", "case_def"],
        },
        "nested": {"deep": {"values": [1, 2, 3, {"key": "value"}]}},
    }

    execution = create_sample_execution(sample_case.case_id)
    execution.metadata = metadata
    await execution_repository.create_execution(execution)

    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert retrieved.metadata == metadata


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tool_call_with_complex_input_output(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test tool call with complex input/output JSON."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    complex_input = {
        "query": "kubernetes pod crash",
        "filters": {"namespace": "production", "labels": {"app": "api"}},
        "options": {"max_results": 10, "include_related": True},
    }

    complex_output = {
        "results": [
            {"title": "Pod Crash Recovery", "score": 0.95, "content": "..."},
            {"title": "Memory Issues", "score": 0.87, "content": "..."},
        ],
        "metadata": {"total_results": 100, "time_ms": 45},
    }

    tool_call = AgentToolCall(
        tool_call_id=generate_tool_call_id(),
        execution_id=execution.execution_id,
        tool_name="knowledge_search",
        tool_input=complex_input,
        tool_output=complex_output,
        status="success",
    )
    await execution_repository.create_tool_call(tool_call)

    # Retrieve via execution
    retrieved = await execution_repository.get_execution(execution.execution_id)
    assert len(retrieved.tool_calls) == 1
    assert retrieved.tool_calls[0].tool_input == complex_input
    assert retrieved.tool_calls[0].tool_output == complex_output


# ============================================================
# Repository Factory Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_inmemory():
    """Test repository factory returns InMemoryAgentExecutionRepository."""
    os.environ["AGENT_EXECUTION_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY
    reset_inmemory_agent_execution_repository()

    async with get_agent_execution_repository_async() as repo:
        assert isinstance(repo, InMemoryAgentExecutionRepository)

    # Cleanup
    reset_inmemory_agent_execution_repository()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_invalid_type():
    """Test repository factory with invalid storage type."""
    os.environ["AGENT_EXECUTION_STORAGE_TYPE"] = "invalid_type"

    with pytest.raises(ValueError, match="Unknown storage type"):
        async with get_agent_execution_repository_async() as repo:
            pass

    # Reset
    os.environ["AGENT_EXECUTION_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_nonexistent_execution(
    execution_repository: DatabaseAgentExecutionRepository,
):
    """Test getting execution that doesn't exist."""
    result = await execution_repository.get_execution("exec_doesnotexist")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_nonexistent_execution(
    execution_repository: DatabaseAgentExecutionRepository,
):
    """Test deleting execution that doesn't exist."""
    result = await execution_repository.delete_execution("exec_doesnotexist")
    assert result is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_nonexistent_execution(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test updating execution that doesn't exist."""
    execution = create_sample_execution(sample_case.case_id)

    with pytest.raises(ValueError, match="not found"):
        await execution_repository.update_execution(execution)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_duplicate_execution_id_fails(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test creating execution with duplicate ID fails."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    duplicate = create_sample_execution(sample_case.case_id)
    duplicate.execution_id = execution.execution_id

    with pytest.raises(ValueError, match="already exists"):
        await execution_repository.create_execution(duplicate)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_duplicate_tool_call_id_fails(
    execution_repository: DatabaseAgentExecutionRepository,
    sample_case: Case,
):
    """Test creating tool call with duplicate ID fails."""
    execution = create_sample_execution(sample_case.case_id)
    await execution_repository.create_execution(execution)

    tool_call = create_sample_tool_call(execution.execution_id)
    await execution_repository.create_tool_call(tool_call)

    duplicate = create_sample_tool_call(execution.execution_id)
    duplicate.tool_call_id = tool_call.tool_call_id

    with pytest.raises(ValueError, match="already exists"):
        await execution_repository.create_tool_call(duplicate)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_nonexistent_tool_call(
    execution_repository: DatabaseAgentExecutionRepository,
):
    """Test updating tool call that doesn't exist."""
    tool_call = AgentToolCall(
        tool_call_id="tc_doesnotexist",
        execution_id="exec_123",
        tool_name="test",
    )

    with pytest.raises(ValueError, match="not found"):
        await execution_repository.update_tool_call(tool_call)

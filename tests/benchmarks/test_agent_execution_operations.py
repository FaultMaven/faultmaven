"""Benchmark agent execution management operations.

Establishes performance baselines for agent execution CRUD operations.

Performance Targets (p95 latency):
- Execution creation: < 150ms
- Execution retrieval (with tool calls): < 200ms
- List executions (20 items): < 150ms
- Tool call creation: < 100ms
- Update execution status: < 100ms

Run with:
    pytest tests/benchmarks/test_agent_execution_operations.py -m benchmark -v
"""

import pytest
import time
from datetime import datetime, timezone
from uuid import uuid4

from faultmaven.modules.case.domain.models import Case, CaseStatus, InvestigationStrategy
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.modules.agent.infrastructure.persistence.agent_execution_repository import (
    DatabaseAgentExecutionRepository,
)

from .conftest import generate_case_id


def generate_execution_id() -> str:
    """Generate a valid execution ID."""
    return f"exec_{uuid4().hex[:12]}"


def generate_tool_call_id() -> str:
    """Generate a valid tool call ID."""
    return f"tc_{uuid4().hex[:12]}"


def create_sample_execution(
    case_id: str,
    agent_type: AgentType = AgentType.INVESTIGATOR,
    status: ExecutionStatus = ExecutionStatus.QUEUED,
) -> AgentExecution:
    """Create a sample agent execution for benchmarking."""
    return AgentExecution(
        execution_id=generate_execution_id(),
        case_id=case_id,
        agent_type=agent_type,
        agent_model="gpt-4",
        status=status,
        prompt="Benchmark execution prompt for testing agent performance.",
    )


def create_sample_tool_call(
    execution_id: str,
    tool_name: str = "web_search",
    status: str = "pending",
) -> AgentToolCall:
    """Create a sample tool call for benchmarking."""
    return AgentToolCall(
        tool_call_id=generate_tool_call_id(),
        execution_id=execution_id,
        tool_name=tool_name,
        tool_input={"query": "benchmark query", "options": {"limit": 10}},
        status=status,
    )


@pytest.fixture
async def execution_repository(benchmark_session) -> DatabaseAgentExecutionRepository:
    """Create agent execution repository for benchmarks."""
    return DatabaseAgentExecutionRepository(benchmark_session)


@pytest.fixture
async def benchmark_case(case_repository: DatabaseCaseRepository) -> Case:
    """Create a case for benchmark execution operations."""
    case = Case(
        case_id=generate_case_id(),
        user_id="benchmark-user-001",
        organization_id="benchmark-org-001",
        title="Benchmark Execution Case",
        description="Case for benchmarking agent execution operations",
        status=CaseStatus.INVESTIGATING,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )
    return await case_repository.save(case)


@pytest.mark.benchmark
class TestExecutionCreationPerformance:
    """Benchmark execution creation operations."""

    @pytest.mark.asyncio
    async def test_single_execution_creation_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of creating a single agent execution.

        Target: p95 < 150ms
        """
        execution = create_sample_execution(benchmark_case.case_id)

        start = time.perf_counter()
        result = await execution_repository.create_execution(execution)
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.150, (
            f"Execution creation latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n  Execution creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_execution_creation_throughput(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure throughput of creating multiple agent executions.

        Target: > 30 executions/second
        """
        num_executions = 50

        executions = [
            create_sample_execution(benchmark_case.case_id) for _ in range(num_executions)
        ]

        start = time.perf_counter()
        for execution in executions:
            await execution_repository.create_execution(execution)
        duration = time.perf_counter() - start

        throughput = num_executions / duration
        assert throughput > 30, (
            f"Execution creation throughput {throughput:.1f}/sec below 30/sec target"
        )
        print(
            f"\n  Batch creation throughput: {throughput:.1f} executions/sec "
            f"({num_executions} items in {duration:.2f}s)"
        )


@pytest.mark.benchmark
class TestExecutionRetrievalPerformance:
    """Benchmark execution retrieval operations."""

    @pytest.mark.asyncio
    async def test_single_execution_retrieval_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of retrieving a single agent execution.

        Target: p95 < 100ms
        """
        # Setup - Create test execution
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        # Benchmark retrieval
        start = time.perf_counter()
        result = await execution_repository.get_execution(execution.execution_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.100, (
            f"Execution retrieval latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Execution retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_execution_with_tool_calls_retrieval_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of retrieving execution with tool calls.

        Target: p95 < 200ms for execution with 10 tool calls
        """
        # Setup - Create execution with 10 tool calls
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        for i in range(10):
            tc = create_sample_tool_call(execution.execution_id, f"tool_{i}")
            await execution_repository.create_tool_call(tc)

        # Benchmark retrieval (includes eager loading of tool calls)
        start = time.perf_counter()
        result = await execution_repository.get_execution(execution.execution_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert len(result.tool_calls) == 10
        assert latency < 0.200, (
            f"Execution+tools retrieval latency {latency*1000:.1f}ms exceeds 200ms target"
        )
        print(f"\n  Execution with tools retrieval latency: {latency*1000:.1f}ms ({len(result.tool_calls)} tool calls)")

    @pytest.mark.asyncio
    async def test_list_executions_by_case_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing executions for a case.

        Target: < 150ms for 20 executions
        """
        # Setup - Create 20 executions
        for i in range(20):
            execution = create_sample_execution(benchmark_case.case_id)
            await execution_repository.create_execution(execution)

        # Benchmark list operation
        start = time.perf_counter()
        result, total = await execution_repository.list_executions_by_case(
            case_id=benchmark_case.case_id,
            limit=50,
        )
        latency = time.perf_counter() - start

        assert len(result) == 20
        assert total == 20
        assert latency < 0.150, (
            f"List executions latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n  List executions latency: {latency*1000:.1f}ms ({len(result)} items)")

    @pytest.mark.asyncio
    async def test_list_executions_with_status_filter_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing executions with status filter.

        Target: < 150ms for filtered query
        """
        # Setup - Create mixed status executions
        for i in range(10):
            execution = create_sample_execution(
                benchmark_case.case_id,
                status=ExecutionStatus.COMPLETED,
            )
            await execution_repository.create_execution(execution)

        for i in range(10):
            execution = create_sample_execution(
                benchmark_case.case_id,
                status=ExecutionStatus.FAILED,
            )
            await execution_repository.create_execution(execution)

        # Benchmark filtered list
        start = time.perf_counter()
        result, total = await execution_repository.list_executions_by_case(
            case_id=benchmark_case.case_id,
            status=ExecutionStatus.COMPLETED,
            limit=50,
        )
        latency = time.perf_counter() - start

        assert len(result) == 10
        assert total == 10
        assert latency < 0.150, (
            f"Filtered list latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n  Filtered list latency: {latency*1000:.1f}ms ({len(result)} items)")


@pytest.mark.benchmark
class TestExecutionUpdatePerformance:
    """Benchmark execution update operations."""

    @pytest.mark.asyncio
    async def test_execution_status_update_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of updating execution status.

        Target: < 100ms
        """
        # Setup - Create test execution
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        # Update status
        execution.mark_started()

        # Benchmark update
        start = time.perf_counter()
        result = await execution_repository.update_execution(execution)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.status == ExecutionStatus.RUNNING
        assert latency < 0.100, (
            f"Status update latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Execution status update latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_execution_completion_update_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of completing an execution with response.

        Target: < 100ms
        """
        # Setup - Create and start execution
        execution = create_sample_execution(benchmark_case.case_id)
        execution.mark_started()
        await execution_repository.create_execution(execution)

        # Complete execution
        execution.mark_completed("Root cause identified: memory leak in production")
        execution.set_token_usage(500, 1000)

        # Benchmark update
        start = time.perf_counter()
        result = await execution_repository.update_execution(execution)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.status == ExecutionStatus.COMPLETED
        assert latency < 0.100, (
            f"Completion update latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Execution completion update latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestToolCallCreationPerformance:
    """Benchmark tool call creation operations."""

    @pytest.mark.asyncio
    async def test_single_tool_call_creation_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of creating a single tool call.

        Target: p95 < 100ms
        """
        # Setup - Create execution
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        tool_call = create_sample_tool_call(execution.execution_id)

        # Benchmark creation
        start = time.perf_counter()
        result = await execution_repository.create_tool_call(tool_call)
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.100, (
            f"Tool call creation latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Tool call creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_tool_call_creation_throughput(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure throughput of creating multiple tool calls.

        Target: > 50 tool calls/second
        """
        # Setup - Create execution
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        num_tool_calls = 30

        tool_calls = [
            create_sample_tool_call(execution.execution_id, f"tool_{i}")
            for i in range(num_tool_calls)
        ]

        start = time.perf_counter()
        for tc in tool_calls:
            await execution_repository.create_tool_call(tc)
        duration = time.perf_counter() - start

        throughput = num_tool_calls / duration
        assert throughput > 50, (
            f"Tool call creation throughput {throughput:.1f}/sec below 50/sec target"
        )
        print(
            f"\n  Tool call batch creation throughput: {throughput:.1f} calls/sec "
            f"({num_tool_calls} items in {duration:.2f}s)"
        )


@pytest.mark.benchmark
class TestToolCallUpdatePerformance:
    """Benchmark tool call update operations."""

    @pytest.mark.asyncio
    async def test_tool_call_update_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of updating a tool call.

        Target: < 100ms
        """
        # Setup - Create execution and tool call
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        tool_call = create_sample_tool_call(execution.execution_id)
        await execution_repository.create_tool_call(tool_call)

        # Update tool call
        tool_call.mark_started()
        tool_call.mark_success({"results": [1, 2, 3], "metadata": {"count": 3}})

        # Benchmark update
        start = time.perf_counter()
        result = await execution_repository.update_tool_call(tool_call)
        latency = time.perf_counter() - start

        assert result is not None
        assert result.status == "success"
        assert latency < 0.100, (
            f"Tool call update latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Tool call update latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestExecutionDeletePerformance:
    """Benchmark execution deletion operations."""

    @pytest.mark.asyncio
    async def test_execution_delete_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of deleting an agent execution.

        Target: < 100ms
        """
        # Setup - Create test execution
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        # Benchmark delete
        start = time.perf_counter()
        result = await execution_repository.delete_execution(execution.execution_id)
        latency = time.perf_counter() - start

        assert result is True
        assert latency < 0.100, (
            f"Execution delete latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Execution delete latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_execution_delete_with_tool_calls_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of deleting execution with tool calls (CASCADE).

        Target: < 150ms for execution with 10 tool calls
        """
        # Setup - Create execution with 10 tool calls
        execution = create_sample_execution(benchmark_case.case_id)
        await execution_repository.create_execution(execution)

        for i in range(10):
            tc = create_sample_tool_call(execution.execution_id, f"tool_{i}")
            await execution_repository.create_tool_call(tc)

        # Benchmark delete (should CASCADE to tool calls)
        start = time.perf_counter()
        result = await execution_repository.delete_execution(execution.execution_id)
        latency = time.perf_counter() - start

        assert result is True
        assert latency < 0.150, (
            f"Execution+tools delete latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n  Execution with tools delete latency: {latency*1000:.1f}ms (10 tool calls)")


@pytest.mark.benchmark
class TestExecutionCountPerformance:
    """Benchmark execution count operations."""

    @pytest.mark.asyncio
    async def test_count_executions_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of counting executions for a case.

        Target: < 50ms
        """
        # Setup - Create 50 executions
        for i in range(50):
            execution = create_sample_execution(benchmark_case.case_id)
            await execution_repository.create_execution(execution)

        # Benchmark count
        start = time.perf_counter()
        count = await execution_repository.count_executions_by_case(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert count == 50
        assert latency < 0.050, (
            f"Count latency {latency*1000:.1f}ms exceeds 50ms target"
        )
        print(f"\n  Count executions latency: {latency*1000:.1f}ms ({count} items)")


@pytest.mark.benchmark
class TestLatestExecutionPerformance:
    """Benchmark latest execution retrieval operations."""

    @pytest.mark.asyncio
    async def test_get_latest_execution_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of getting the latest execution.

        Target: < 100ms
        """
        # Setup - Create 20 executions
        for i in range(20):
            execution = create_sample_execution(benchmark_case.case_id)
            await execution_repository.create_execution(execution)

        # Benchmark get latest
        start = time.perf_counter()
        result = await execution_repository.get_latest_execution(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.100, (
            f"Get latest latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Get latest execution latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_get_latest_execution_with_agent_type_latency(
        self,
        execution_repository: DatabaseAgentExecutionRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of getting latest execution filtered by agent type.

        Target: < 100ms
        """
        # Setup - Create mixed agent type executions
        for i in range(10):
            execution = create_sample_execution(
                benchmark_case.case_id,
                agent_type=AgentType.INVESTIGATOR,
            )
            await execution_repository.create_execution(execution)

        for i in range(10):
            execution = create_sample_execution(
                benchmark_case.case_id,
                agent_type=AgentType.DEBUGGER,
            )
            await execution_repository.create_execution(execution)

        # Benchmark get latest with filter
        start = time.perf_counter()
        result = await execution_repository.get_latest_execution(
            benchmark_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
        )
        latency = time.perf_counter() - start

        assert result is not None
        assert result.agent_type == AgentType.INVESTIGATOR
        assert latency < 0.100, (
            f"Get latest filtered latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Get latest filtered execution latency: {latency*1000:.1f}ms")

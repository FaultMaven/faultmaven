"""Performance Benchmarks for Agent Orchestration (TASK-015)

This module provides performance benchmarks for the agent orchestration
service to ensure acceptable latency and throughput.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md

Target Performance (p95):
- Execute agent (simple query, no tools): <3000ms
- Execute agent (with 1 tool call): <5000ms
- Execute agent (with 3 tool calls): <8000ms
- Build agent context: <200ms
- Tool call execution (read_file): <300ms
- Parallel tool execution (3 tools): <500ms
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock
import pytest
import statistics

from faultmaven.modules.agent.domain.services.agent_orchestration_service import AgentOrchestrationService
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.case.domain.models import Case, CaseStatus, CaseSeverity
from faultmaven.modules.evidence.domain.models import EvidenceArtifact, EvidenceArtifactType
from faultmaven.modules.agent.domain.events.execution_events import (
    LLMEvent,
    LLMEventType,
    ToolCall,
)
from faultmaven.modules.agent.tools.agent_tools import AgentToolRegistry
from faultmaven.modules.agent.tools.read_file_tool import ReadFileTool
from faultmaven.modules.agent.tools.list_evidence_tool import ListEvidenceTool


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_session():
    """Create a sample session for benchmarks."""
    return InvestigationSession(
        session_id="session_bench",
        case_id="case_bench",
        user_id="user_bench",
        organization_id="org_bench",
        status=SessionStatus.ACTIVE,
        token_budget_limit=1000000,
    )


@pytest.fixture
def sample_case():
    """Create a sample case for benchmarks."""
    return Case(
        case_id="case_bench",
        organization_id="org_bench",
        title="Benchmark Test Case",
        description="Testing performance benchmarks",
        severity=CaseSeverity.MEDIUM,
        status=CaseStatus.INVESTIGATING,
    )


@pytest.fixture
def sample_evidence():
    """Create sample evidence for benchmarks."""
    return [
        EvidenceArtifact(
            evidence_id=f"ev_bench_{i}",
            case_id="case_bench",
            original_filename=f"file_{i}.log",
            storage_path=f"/data/file_{i}.log",
            file_size=1024,
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            description=f"Benchmark file {i}",
            is_primary=(i == 0),
        )
        for i in range(5)
    ]


@pytest.fixture
def mock_services(sample_session, sample_case, sample_evidence):
    """Create mock services for benchmarks."""
    session_service = AsyncMock()
    session_service.get_session.return_value = sample_session
    session_service.check_budget_exceeded.return_value = {"is_over_budget": False}
    session_service.add_execution_to_session.return_value = sample_session

    evidence_service = AsyncMock()
    evidence_service.get_evidence.return_value = sample_evidence[0]
    evidence_service.list_evidence_by_case.return_value = sample_evidence
    evidence_service.download_evidence.return_value = (
        b"Log content for benchmarking\n" * 100,
        "file.log",
        "text/plain",
    )

    execution_repo = AsyncMock()
    execution_repo.create_execution.side_effect = lambda e: e
    execution_repo.update_execution.return_value = None
    execution_repo.list_executions_by_case.return_value = ([], 0)
    execution_repo.save_tool_call.return_value = None

    case_repo = AsyncMock()
    case_repo.get.return_value = sample_case

    return {
        "session_service": session_service,
        "evidence_service": evidence_service,
        "execution_repo": execution_repo,
        "case_repo": case_repo,
    }


@pytest.fixture
def tool_registry():
    """Create tool registry for benchmarks."""
    registry = AgentToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListEvidenceTool())
    return registry


@pytest.fixture
def mock_llm_client():
    """Create mock LLM client for benchmarks."""
    client = MagicMock()
    client.model = "benchmark-model"
    return client


@pytest.fixture
def orchestration_service(mock_services, tool_registry, mock_llm_client):
    """Create orchestration service for benchmarks."""
    return AgentOrchestrationService(
        session_service=mock_services["session_service"],
        evidence_service=mock_services["evidence_service"],
        execution_repo=mock_services["execution_repo"],
        case_repo=mock_services["case_repo"],
        tool_registry=tool_registry,
        llm_client=mock_llm_client,
        max_retries=0,  # No retries for benchmarks
        retry_initial_delay=0.01,
        tool_timeout=5,
        max_parallel_tools=5,
    )


# =============================================================================
# Benchmark Helpers
# =============================================================================


async def measure_execution_time(coro):
    """Measure execution time of a coroutine."""
    start = time.perf_counter()
    result = await coro
    end = time.perf_counter()
    return result, (end - start) * 1000  # Return ms


def calculate_percentile(times: List[float], percentile: int) -> float:
    """Calculate percentile of a list of times."""
    if not times:
        return 0.0
    sorted_times = sorted(times)
    index = (len(sorted_times) - 1) * percentile / 100
    lower = int(index)
    upper = lower + 1
    if upper >= len(sorted_times):
        return sorted_times[lower]
    return sorted_times[lower] + (sorted_times[upper] - sorted_times[lower]) * (index - lower)


# =============================================================================
# Benchmark: Simple Query (No Tools)
# =============================================================================


class TestSimpleQueryPerformance:
    """Benchmarks for simple query execution without tools."""

    @pytest.mark.asyncio
    async def test_simple_query_latency(
        self,
        orchestration_service,
        sample_session,
    ):
        """Benchmark simple query latency (target: p95 < 3000ms)."""
        # Create fast mock LLM
        async def mock_stream():
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Response")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 100, "output_tokens": 50},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        times = []
        iterations = 10

        for _ in range(iterations):
            start = time.perf_counter()
            async for _ in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Simple test query",
            ):
                pass
            end = time.perf_counter()
            times.append((end - start) * 1000)

        p95 = calculate_percentile(times, 95)
        mean = statistics.mean(times)

        print(f"\nSimple Query Benchmark:")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  Min:  {min(times):.2f}ms")
        print(f"  Max:  {max(times):.2f}ms")

        # Assert p95 is under target (3000ms, relaxed for mocked tests)
        assert p95 < 3000, f"P95 latency {p95:.2f}ms exceeds 3000ms target"


# =============================================================================
# Benchmark: Single Tool Call
# =============================================================================


class TestSingleToolCallPerformance:
    """Benchmarks for execution with single tool call."""

    @pytest.mark.asyncio
    async def test_single_tool_call_latency(
        self,
        orchestration_service,
        sample_session,
        sample_evidence,
    ):
        """Benchmark single tool call latency (target: p95 < 5000ms)."""
        tool_call = ToolCall(
            id="tc_bench",
            name="read_file",
            arguments={"evidence_id": sample_evidence[0].evidence_id},
        )

        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] % 2 == 1:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Reading file")
                yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tool_call)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 50, "output_tokens": 20},
                )
            else:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Result processed")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 100, "output_tokens": 30},
                )

        orchestration_service._llm_client.stream_completion = mock_stream

        times = []
        iterations = 10

        for _ in range(iterations):
            call_count[0] = 0
            start = time.perf_counter()
            async for _ in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Read the file",
            ):
                pass
            end = time.perf_counter()
            times.append((end - start) * 1000)

        p95 = calculate_percentile(times, 95)
        mean = statistics.mean(times)

        print(f"\nSingle Tool Call Benchmark:")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  Min:  {min(times):.2f}ms")
        print(f"  Max:  {max(times):.2f}ms")

        assert p95 < 5000, f"P95 latency {p95:.2f}ms exceeds 5000ms target"


# =============================================================================
# Benchmark: Multiple Tool Calls
# =============================================================================


class TestMultipleToolCallsPerformance:
    """Benchmarks for execution with multiple tool calls."""

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_latency(
        self,
        orchestration_service,
        sample_session,
        sample_evidence,
    ):
        """Benchmark multiple tool calls latency (target: p95 < 8000ms)."""
        tool_calls = [
            ToolCall(id=f"tc_{i}", name="read_file", arguments={"evidence_id": f"ev_bench_{i}"})
            for i in range(3)
        ]

        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] % 2 == 1:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Reading files")
                for tc in tool_calls:
                    yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tc)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 50, "output_tokens": 30},
                )
            else:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Results processed")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 200, "output_tokens": 50},
                )

        orchestration_service._llm_client.stream_completion = mock_stream

        times = []
        iterations = 10

        for _ in range(iterations):
            call_count[0] = 0
            start = time.perf_counter()
            async for _ in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Read multiple files",
            ):
                pass
            end = time.perf_counter()
            times.append((end - start) * 1000)

        p95 = calculate_percentile(times, 95)
        mean = statistics.mean(times)

        print(f"\nMultiple Tool Calls (3) Benchmark:")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  Min:  {min(times):.2f}ms")
        print(f"  Max:  {max(times):.2f}ms")

        assert p95 < 8000, f"P95 latency {p95:.2f}ms exceeds 8000ms target"


# =============================================================================
# Benchmark: Build Agent Context
# =============================================================================


class TestBuildContextPerformance:
    """Benchmarks for building agent context."""

    @pytest.mark.asyncio
    async def test_build_context_latency(
        self,
        orchestration_service,
        sample_session,
        mock_services,
    ):
        """Benchmark context building latency (target: p95 < 200ms)."""
        # Add some conversation history
        previous_executions = [
            AgentExecution(
                execution_id=f"exec_{i}",
                case_id=sample_session.case_id,
                agent_type=AgentType.INVESTIGATOR,
                agent_model="test-model",
                status=ExecutionStatus.COMPLETED,
                prompt=f"Question {i}",
                response=f"Answer {i}",
            )
            for i in range(5)
        ]
        mock_services["execution_repo"].list_executions_by_case.return_value = (
            previous_executions,
            5,
        )

        times = []
        iterations = 20

        for _ in range(iterations):
            start = time.perf_counter()
            await orchestration_service._build_agent_context(
                session=sample_session,
                user_message="Test query",
                agent_type=AgentType.INVESTIGATOR,
            )
            end = time.perf_counter()
            times.append((end - start) * 1000)

        p95 = calculate_percentile(times, 95)
        mean = statistics.mean(times)

        print(f"\nBuild Context Benchmark:")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  Min:  {min(times):.2f}ms")
        print(f"  Max:  {max(times):.2f}ms")

        assert p95 < 200, f"P95 latency {p95:.2f}ms exceeds 200ms target"


# =============================================================================
# Benchmark: Tool Execution
# =============================================================================


class TestToolExecutionPerformance:
    """Benchmarks for individual tool execution."""

    @pytest.mark.asyncio
    async def test_read_file_tool_latency(
        self,
        tool_registry,
        mock_services,
        sample_evidence,
    ):
        """Benchmark read_file tool latency (target: p95 < 300ms)."""
        from faultmaven.modules.agent.tools.agent_tools import ToolContext

        context = ToolContext(
            session_id="session_bench",
            case_id="case_bench",
            organization_id="org_bench",
            user_id="user_bench",
            evidence_service=mock_services["evidence_service"],
        )

        times = []
        iterations = 20

        for _ in range(iterations):
            start = time.perf_counter()
            await tool_registry.execute_tool(
                tool_name="read_file",
                params={"evidence_id": sample_evidence[0].evidence_id},
                context=context,
            )
            end = time.perf_counter()
            times.append((end - start) * 1000)

        p95 = calculate_percentile(times, 95)
        mean = statistics.mean(times)

        print(f"\nRead File Tool Benchmark:")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  Min:  {min(times):.2f}ms")
        print(f"  Max:  {max(times):.2f}ms")

        assert p95 < 300, f"P95 latency {p95:.2f}ms exceeds 300ms target"


# =============================================================================
# Benchmark: Parallel Tool Execution
# =============================================================================


class TestParallelToolExecutionPerformance:
    """Benchmarks for parallel tool execution."""

    @pytest.mark.asyncio
    async def test_parallel_tool_execution_latency(
        self,
        orchestration_service,
        mock_services,
        sample_evidence,
        sample_session,
    ):
        """Benchmark parallel tool execution (target: p95 < 500ms for 3 tools)."""
        from faultmaven.modules.agent.domain.events.execution_events import ToolCall as DomainToolCall
        from faultmaven.modules.agent.tools.agent_tools import ToolContext

        tool_calls = [
            DomainToolCall(
                id=f"tc_{i}",
                name="read_file",
                arguments={"evidence_id": sample_evidence[i].evidence_id},
            )
            for i in range(3)
        ]

        execution = AgentExecution(
            execution_id="exec_bench",
            case_id=sample_session.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            evidence_service=mock_services["evidence_service"],
        )

        times = []
        iterations = 20

        for _ in range(iterations):
            start = time.perf_counter()
            await orchestration_service._handle_tool_calls(
                execution=execution,
                tool_calls=tool_calls,
                tool_context=tool_context,
            )
            end = time.perf_counter()
            times.append((end - start) * 1000)

        p95 = calculate_percentile(times, 95)
        mean = statistics.mean(times)

        print(f"\nParallel Tool Execution (3 tools) Benchmark:")
        print(f"  Mean: {mean:.2f}ms")
        print(f"  P95:  {p95:.2f}ms")
        print(f"  Min:  {min(times):.2f}ms")
        print(f"  Max:  {max(times):.2f}ms")

        assert p95 < 500, f"P95 latency {p95:.2f}ms exceeds 500ms target"


# =============================================================================
# Benchmark Summary
# =============================================================================


class TestBenchmarkSummary:
    """Summary of all benchmarks."""

    @pytest.mark.asyncio
    async def test_print_benchmark_summary(self):
        """Print benchmark targets for reference."""
        print("\n" + "=" * 60)
        print("BENCHMARK TARGETS (p95)")
        print("=" * 60)
        print("| Operation                          | Target    |")
        print("|------------------------------------+-----------|")
        print("| Execute agent (simple, no tools)   | <3000ms   |")
        print("| Execute agent (1 tool call)        | <5000ms   |")
        print("| Execute agent (3 tool calls)       | <8000ms   |")
        print("| Build agent context                | <200ms    |")
        print("| Tool call execution (read_file)    | <300ms    |")
        print("| Parallel tool execution (3 tools)  | <500ms    |")
        print("=" * 60)

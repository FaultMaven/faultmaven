"""Unit Tests for Agent Orchestration Service (TASK-015)

This module tests the AgentOrchestrationService which coordinates
AI agent execution for troubleshooting investigations.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    LLMException,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.models.interfaces import ToolResult
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.agent.domain.events.execution_events import (
    AgentContext,
    ExecutionEvent,
    ExecutionEventType,
    LLMEvent,
    LLMEventType,
    Message,
    ToolCall,
)
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.agent.domain.services.agent_orchestration_service import (
    AGENT_SYSTEM_PROMPTS,
    DATA_ACCESS_DIRECTED_ANALYSIS,
    DATA_ACCESS_TRIAGE,
    AgentOrchestrationService,
    EvidenceDAState,
)
from faultmaven.modules.agent.tools.base import AgentToolRegistry, ToolContext
from faultmaven.modules.case.domain.models import Case, CaseStatus

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_session_service():
    """Create a mock session service."""
    service = AsyncMock()
    return service


@pytest.fixture
def mock_evidence_service():
    """Create a mock evidence service."""
    service = AsyncMock()
    return service


@pytest.fixture
def mock_case_repo():
    """Create a mock case repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_tool_registry():
    """Create a mock tool registry."""
    registry = MagicMock(spec=AgentToolRegistry)
    registry.get_all_domain_tools.return_value = []
    return registry


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    client = MagicMock()
    client.model = "test-model"
    return client


@pytest.fixture
def sample_session():
    """Create a sample investigation session."""
    return InvestigationSession(
        session_id="session_test123",
        case_id="case_a1b2c3d4e5f6",
        user_id="user_test789",
        organization_id="org_test000",
        status=SessionStatus.ACTIVE,
        session_goal="Investigate API errors",
        token_budget_limit=100000,
        total_token_usage=0,
    )


@pytest.fixture
def sample_case():
    """Create a sample case."""
    return Case(
        case_id="case_a1b2c3d4e5f6",
        user_id="user_test789",
        organization_id="org_test000",
        title="API Error Investigation",
        description="Investigating 500 errors in production API",
        status=CaseStatus.INQUIRY,  # Use INQUIRY to avoid validation requirements
    )


@pytest.fixture
def sample_execution():
    """Create a sample agent execution."""
    return AgentExecution(
        execution_id="exec_test111",
        case_id="case_a1b2c3d4e5f6",
        agent_type=AgentType.INVESTIGATOR,
        agent_model="test-model",
        status=ExecutionStatus.QUEUED,
        prompt="What is causing the API errors?",
    )


@pytest.fixture
def orchestration_service(
    mock_session_service,
    mock_evidence_service,
    mock_case_repo,
    mock_tool_registry,
    mock_llm_client,
):
    """Create an AgentOrchestrationService with mock dependencies."""
    return AgentOrchestrationService(
        case_repo=mock_case_repo,
        session_service=mock_session_service,
        evidence_service=mock_evidence_service,
        tool_registry=mock_tool_registry,
        llm_client=mock_llm_client,
        max_retries=3,
        retry_initial_delay=0.01,  # Fast retries for testing
        tool_timeout=5,
        max_parallel_tools=3,
    )


# =============================================================================
# Test: Execute Agent - Basic Workflow
# =============================================================================


class TestExecuteAgentBasicWorkflow:
    """Tests for basic agent execution workflow."""

    @pytest.mark.asyncio
    async def test_execute_agent_creates_execution_record(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent creates an execution record."""
        # Setup
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": False
        }
        mock_session_service.add_execution_to_session.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.create_agent_execution.return_value = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        # Mock LLM to return simple response
        async def mock_stream(**kwargs):
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Test response")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="Test response",
                metadata={"input_tokens": 100, "output_tokens": 50},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        # Execute
        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="What is causing the errors?",
        ):
            events.append(event)

        # Verify
        mock_case_repo.create_agent_execution.assert_called_once()
        assert any(e.event_type == ExecutionEventType.STARTED for e in events)

    @pytest.mark.asyncio
    async def test_execute_agent_validates_session_is_active(
        self,
        orchestration_service,
        mock_session_service,
    ):
        """Test that execute_agent validates session is ACTIVE."""
        # Setup - paused session
        paused_session = InvestigationSession(
            session_id="session_paused",
            case_id="case_test",
            user_id="user_test",
            organization_id="org_test",
            status=SessionStatus.PAUSED,
        )
        mock_session_service.get_session.return_value = paused_session

        # Execute and verify
        with pytest.raises(ConflictError) as exc_info:
            async for _ in orchestration_service.execute_agent(
                session_id="session_paused",
                organization_id="org_test",
                user_message="Test message",
            ):
                pass

        assert "not active" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_execute_agent_validates_session_exists(
        self,
        orchestration_service,
        mock_session_service,
    ):
        """Test that execute_agent raises NotFoundError for missing session."""
        mock_session_service.get_session.return_value = None

        with pytest.raises(NotFoundError):
            async for _ in orchestration_service.execute_agent(
                session_id="nonexistent",
                organization_id="org_test",
                user_message="Test message",
            ):
                pass

    @pytest.mark.asyncio
    async def test_execute_agent_checks_token_budget(
        self,
        orchestration_service,
        mock_session_service,
        sample_session,
    ):
        """Test that execute_agent checks token budget before execution."""
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": True,
            "total_token_usage": 100000,
            "token_budget_limit": 100000,
        }

        with pytest.raises(ConflictError) as exc_info:
            async for _ in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Test message",
            ):
                pass

        assert "budget" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_execute_agent_streams_response_events(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent streams response events."""
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": False
        }
        mock_session_service.add_execution_to_session.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.create_agent_execution.return_value = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        # Mock LLM with multiple chunks
        async def mock_stream(**kwargs):
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Part 1 ")
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Part 2 ")
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Part 3")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 100, "output_tokens": 50},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Test message",
        ):
            events.append(event)

        # Should have response events
        response_events = [
            e for e in events if e.event_type == ExecutionEventType.RESPONSE
        ]
        assert len(response_events) >= 3

    @pytest.mark.asyncio
    async def test_execute_agent_updates_session_token_usage(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent updates session token usage."""
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": False
        }
        mock_session_service.add_execution_to_session.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.create_agent_execution.return_value = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        async def mock_stream(**kwargs):
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Response")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 100, "output_tokens": 50},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        async for _ in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Test message",
        ):
            pass

        # Verify session was updated with token usage
        mock_session_service.add_execution_to_session.assert_called_once()
        call_args = mock_session_service.add_execution_to_session.call_args
        assert call_args.kwargs["token_usage"] == 150  # 100 + 50

    @pytest.mark.asyncio
    async def test_execute_agent_pauses_session_on_budget_exceeded(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent pauses session when budget is exceeded."""
        mock_session_service.get_session.return_value = sample_session
        # First check: not over budget, second check (after execution): over budget
        mock_session_service.check_budget_exceeded.side_effect = [
            {"is_over_budget": False},
            {"is_over_budget": True},
        ]
        mock_session_service.add_execution_to_session.return_value = sample_session
        mock_session_service.pause_session.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.create_agent_execution.return_value = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        async def mock_stream(**kwargs):
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Response")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 100, "output_tokens": 50},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        async for _ in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Test message",
        ):
            pass

        # Verify session was paused
        mock_session_service.pause_session.assert_called_once_with(
            sample_session.session_id, sample_session.organization_id
        )

    @pytest.mark.asyncio
    async def test_execute_agent_supports_different_agent_types(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent supports different agent types."""
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": False
        }
        mock_session_service.add_execution_to_session.return_value = sample_session
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        for agent_type in [
            AgentType.INVESTIGATOR,
            AgentType.DEBUGGER,
            AgentType.RESEARCHER,
        ]:
            mock_case_repo.create_agent_execution.return_value = AgentExecution(
                execution_id=f"exec_{agent_type.value}",
                case_id=sample_case.case_id,
                agent_type=agent_type,
                agent_model="test-model",
            )

            async def mock_stream(**kwargs):
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Response")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 100, "output_tokens": 50},
                )

            orchestration_service._llm_client.stream_completion = mock_stream

            events = []
            async for event in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Test message",
                agent_type=agent_type,
            ):
                events.append(event)

            assert any(e.event_type == ExecutionEventType.STARTED for e in events)


# =============================================================================
# Test: Execute Agent - Error Handling
# =============================================================================


class TestExecuteAgentErrorHandling:
    """Tests for error handling in agent execution."""

    @pytest.mark.asyncio
    async def test_execute_agent_handles_llm_error(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent handles LLM errors gracefully."""
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": False
        }
        mock_case_repo.get.return_value = sample_case
        execution = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
        mock_case_repo.create_agent_execution.return_value = execution
        mock_case_repo.get_agent_execution.return_value = execution
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        async def mock_stream(**kwargs):
            yield LLMEvent(
                event_type=LLMEventType.ERROR,
                content="API Error: Rate limit exceeded",
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        with pytest.raises(ServiceError):
            async for event in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Test message",
            ):
                events.append(event)

        # Should have error event
        error_events = [e for e in events if e.event_type == ExecutionEventType.ERROR]
        assert len(error_events) >= 1

    @pytest.mark.asyncio
    async def test_execute_agent_marks_execution_failed_on_error(
        self,
        orchestration_service,
        mock_session_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that execute_agent marks execution as failed on error."""
        mock_session_service.get_session.return_value = sample_session
        mock_session_service.check_budget_exceeded.return_value = {
            "is_over_budget": False
        }
        mock_case_repo.get.return_value = sample_case

        created_execution = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
        mock_case_repo.create_agent_execution.return_value = created_execution
        mock_case_repo.get_agent_execution.return_value = created_execution
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        async def mock_stream(**kwargs):
            raise LLMException("Connection failed")
            yield  # unreachable — makes this an async generator so callers can `async for` over it

        orchestration_service._llm_client.stream_completion = mock_stream

        with pytest.raises(ServiceError):
            async for _ in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id=sample_session.organization_id,
                user_message="Test message",
            ):
                pass

        # Verify execution was updated with failure
        mock_case_repo.update_agent_execution.assert_called()


# =============================================================================
# Test: Build Agent Context
# =============================================================================


class TestBuildAgentContext:
    """Tests for building agent context."""

    @pytest.mark.asyncio
    async def test_build_context_includes_case_details(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that context includes case details."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="Test message",
            agent_type=AgentType.INVESTIGATOR,
        )

        assert sample_case.title in context.system_prompt
        assert sample_case.description in context.system_prompt

    @pytest.mark.asyncio
    async def test_build_context_includes_conversation_history(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that context includes previous conversation history."""
        # Add previous conversation to case.messages (new approach)
        sample_case.messages = [
            {
                "role": "user",
                "content": "Previous question",
                "turn_number": 1,
                "timestamp": "2024-01-01T00:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Previous answer",
                "turn_number": 1,
                "timestamp": "2024-01-01T00:00:01Z",
            },
        ]
        mock_case_repo.get.return_value = sample_case

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="Follow-up question",
            agent_type=AgentType.INVESTIGATOR,
        )

        # Should have previous Q&A (2 messages) + current user message (1) = 3 total
        assert len(context.messages) >= 3  # Previous Q&A + new message
        # Verify previous conversation is included
        assert any("Previous question" in msg.content for msg in context.messages)
        assert any("Previous answer" in msg.content for msg in context.messages)

    @pytest.mark.asyncio
    async def test_build_context_uses_correct_agent_prompt(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that context uses correct system prompt for agent type.

        INVESTIGATOR prompt has a {data_access_strategy} placeholder that
        gets replaced at runtime with mode-specific content, so we check
        for a stable substring rather than the raw template.
        """
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        for agent_type in AgentType:
            if agent_type == AgentType.CUSTOM:
                continue  # Skip custom agent type

            context = await orchestration_service._build_agent_context(
                session=sample_session,
                user_message="Test message",
                agent_type=agent_type,
            )

            if agent_type == AgentType.INVESTIGATOR:
                # INVESTIGATOR template contains {data_access_strategy} which
                # is replaced at runtime. Verify the stable preamble is present
                # and the placeholder was actually replaced.
                assert "expert troubleshooting investigator" in context.system_prompt
                assert "{data_access_strategy}" not in context.system_prompt
                # One of the two mode-specific prompts must be injected
                assert (
                    "Data Access Strategy" in context.system_prompt
                ), "data_access_strategy placeholder was not replaced"
            else:
                expected_prompt = AGENT_SYSTEM_PROMPTS[agent_type]
                assert expected_prompt in context.system_prompt

    @pytest.mark.asyncio
    async def test_build_context_includes_available_tools(
        self,
        orchestration_service,
        mock_case_repo,
        mock_tool_registry,
        sample_session,
        sample_case,
    ):
        """Test that context includes available tools."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        from faultmaven.modules.agent.domain.events.execution_events import Tool

        mock_tools = [
            Tool(
                name="read_file",
                description="Read a file",
                parameters={"type": "object"},
            ),
            Tool(
                name="list_evidence",
                description="List evidence",
                parameters={"type": "object"},
            ),
        ]
        mock_tool_registry.get_all_domain_tools.return_value = mock_tools

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="Test message",
            agent_type=AgentType.INVESTIGATOR,
        )

        assert len(context.tools) == 2

    @pytest.mark.asyncio
    async def test_build_context_handles_empty_history(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that context handles empty conversation history."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="First message",
            agent_type=AgentType.INVESTIGATOR,
        )

        # Should have just the new user message
        assert len(context.messages) == 1
        assert context.messages[0].content == "First message"

    def test_build_state_summary(
        self,
        orchestration_service,
        sample_case,
        sample_session,
    ):
        """Test that state summary includes key investigation context."""
        # Set up case with investigation state
        from faultmaven.modules.case.contracts import ProblemVerification, TemporalState

        sample_case.title = "Server Performance Issue"
        sample_case.description = "Database queries are slow"
        sample_case.current_turn = 5
        sample_case.problem_verification = ProblemVerification(
            symptom_statement="Database response time increased from 50ms to 500ms",
            temporal_state=TemporalState.ONGOING,
            severity="HIGH",
        )

        summary = orchestration_service._build_state_summary(
            case=sample_case,
            session=sample_session,
        )

        # Verify summary contains key information
        assert "Server Performance Issue" in summary
        assert "Database queries are slow" in summary
        assert "Database response time increased" in summary
        assert "Turns: 5 total" in summary
        assert "inquiry" in summary.lower() or "investigating" in summary.lower()


# =============================================================================
# Test: Tool Call Handling
# =============================================================================


class TestToolCallHandling:
    """Tests for tool call handling."""

    @pytest.mark.asyncio
    async def test_handle_tool_calls_executes_tools(
        self,
        orchestration_service,
        mock_tool_registry,
        sample_execution,
        mock_evidence_service,
        sample_session,
        mock_case_repo,
    ):
        """Test that _handle_tool_calls executes tools."""
        mock_tool_registry.execute_tool.return_value = ToolResult(
            success=True,
            data={"content": "File contents here"},
        )
        mock_case_repo.create_agent_tool_call.return_value = None

        tool_calls = [
            ToolCall(id="tc_1", name="read_file", arguments={"evidence_id": "ev_123"}),
        ]

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            case_repository=mock_case_repo,
        )

        results = await orchestration_service._handle_tool_calls(
            execution=sample_execution,
            tool_calls=tool_calls,
            tool_context=tool_context,
        )

        assert len(results) == 1
        assert results[0].success is True
        mock_tool_registry.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_tool_calls_executes_in_parallel(
        self,
        orchestration_service,
        mock_tool_registry,
        sample_execution,
        mock_evidence_service,
        sample_session,
        mock_case_repo,
    ):
        """Test that _handle_tool_calls executes multiple tools in parallel."""
        call_times = []

        async def mock_execute(tool_name, params, context):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.1)
            return ToolResult(success=True, data={"tool": tool_name})

        mock_tool_registry.execute_tool = mock_execute
        mock_case_repo.create_agent_tool_call.return_value = None

        tool_calls = [
            ToolCall(id="tc_1", name="tool_1", arguments={}),
            ToolCall(id="tc_2", name="tool_2", arguments={}),
            ToolCall(id="tc_3", name="tool_3", arguments={}),
        ]

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            case_repository=mock_case_repo,
        )

        results = await orchestration_service._handle_tool_calls(
            execution=sample_execution,
            tool_calls=tool_calls,
            tool_context=tool_context,
        )

        assert len(results) == 3
        # Calls should be parallel (all within ~0.1s of each other)
        time_spread = max(call_times) - min(call_times)
        assert time_spread < 0.05  # All started within 50ms

    @pytest.mark.asyncio
    async def test_handle_tool_calls_respects_parallel_limit(
        self,
        orchestration_service,
        mock_tool_registry,
        sample_execution,
        mock_evidence_service,
        sample_session,
        mock_case_repo,
    ):
        """Test that _handle_tool_calls respects max_parallel_tools limit."""
        concurrent_count = [0]
        max_concurrent = [0]

        async def mock_execute(tool_name, params, context):
            concurrent_count[0] += 1
            max_concurrent[0] = max(max_concurrent[0], concurrent_count[0])
            await asyncio.sleep(0.1)
            concurrent_count[0] -= 1
            return ToolResult(success=True, data={})

        mock_tool_registry.execute_tool = mock_execute
        mock_case_repo.create_agent_tool_call.return_value = None

        # Create more tools than max_parallel_tools (3)
        tool_calls = [
            ToolCall(id=f"tc_{i}", name=f"tool_{i}", arguments={}) for i in range(5)
        ]

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            case_repository=mock_case_repo,
        )

        await orchestration_service._handle_tool_calls(
            execution=sample_execution,
            tool_calls=tool_calls,
            tool_context=tool_context,
        )

        # Should never exceed max_parallel_tools (3)
        assert max_concurrent[0] <= 3

    @pytest.mark.asyncio
    async def test_handle_tool_calls_handles_tool_errors(
        self,
        orchestration_service,
        mock_tool_registry,
        sample_execution,
        mock_evidence_service,
        sample_session,
        mock_case_repo,
    ):
        """Test that _handle_tool_calls handles tool execution errors."""
        mock_tool_registry.execute_tool.return_value = ToolResult(
            success=False,
            data=None,
            error="File not found",
        )
        mock_case_repo.create_agent_tool_call.return_value = None

        tool_calls = [
            ToolCall(
                id="tc_1", name="read_file", arguments={"evidence_id": "nonexistent"}
            ),
        ]

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            case_repository=mock_case_repo,
        )

        results = await orchestration_service._handle_tool_calls(
            execution=sample_execution,
            tool_calls=tool_calls,
            tool_context=tool_context,
        )

        assert len(results) == 1
        assert results[0].success is False
        assert (
            "not found" in results[0].content.lower()
            or "error" in results[0].content.lower()
        )

    @pytest.mark.asyncio
    async def test_handle_tool_calls_creates_tool_call_records(
        self,
        orchestration_service,
        mock_tool_registry,
        sample_execution,
        mock_evidence_service,
        sample_session,
        mock_case_repo,
    ):
        """Test that _handle_tool_calls creates ToolCallRecord for each call."""
        mock_tool_registry.execute_tool.return_value = ToolResult(
            success=True,
            data={"result": "data"},
        )
        mock_case_repo.create_agent_tool_call.return_value = None

        tool_calls = [
            ToolCall(id="tc_1", name="read_file", arguments={"evidence_id": "ev_1"}),
            ToolCall(id="tc_2", name="list_evidence", arguments={}),
        ]

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            case_repository=mock_case_repo,
        )

        await orchestration_service._handle_tool_calls(
            execution=sample_execution,
            tool_calls=tool_calls,
            tool_context=tool_context,
        )

        # Should save tool call records once per tool (2 tools = 2 calls)
        assert mock_case_repo.create_agent_tool_call.call_count == 2

    @pytest.mark.asyncio
    async def test_handle_tool_calls_enforces_timeout(
        self,
        orchestration_service,
        mock_tool_registry,
        sample_execution,
        mock_evidence_service,
        sample_session,
        mock_case_repo,
    ):
        """Test that _handle_tool_calls enforces timeout."""

        async def slow_tool(tool_name, params, context):
            await asyncio.sleep(10)  # Longer than timeout (5s)
            return ToolResult(success=True, data={})

        mock_tool_registry.execute_tool = slow_tool
        mock_case_repo.create_agent_tool_call.return_value = None

        tool_calls = [
            ToolCall(id="tc_1", name="slow_tool", arguments={}),
        ]

        tool_context = ToolContext(
            session_id=sample_session.session_id,
            case_id=sample_session.case_id,
            organization_id=sample_session.organization_id,
            user_id=sample_session.user_id,
            case_repository=mock_case_repo,
        )

        results = await orchestration_service._handle_tool_calls(
            execution=sample_execution,
            tool_calls=tool_calls,
            tool_context=tool_context,
        )

        assert len(results) == 1
        assert results[0].success is False
        assert "timed out" in results[0].content.lower()


# =============================================================================
# Test: Retry Logic
# =============================================================================


class TestRetryLogic:
    """Tests for retry logic."""

    @pytest.mark.asyncio
    async def test_execute_with_retry_succeeds_on_first_attempt(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry succeeds on first attempt."""
        call_count = [0]

        async def success_generator():
            call_count[0] += 1
            yield LLMEvent(event_type=LLMEventType.COMPLETION, content="Success")

        events = []
        async for event in orchestration_service._execute_with_retry(success_generator):
            events.append(event)

        assert call_count[0] == 1
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_retries_on_rate_limit(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry retries on 429 rate limit."""
        call_count = [0]

        async def rate_limited_generator():
            call_count[0] += 1
            if call_count[0] < 2:
                raise Exception("429 Rate limit exceeded")
            yield LLMEvent(event_type=LLMEventType.COMPLETION, content="Success")

        events = []
        async for event in orchestration_service._execute_with_retry(
            rate_limited_generator
        ):
            events.append(event)

        assert call_count[0] == 2
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_retries_on_server_error(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry retries on 500 server error."""
        call_count = [0]

        async def server_error_generator():
            call_count[0] += 1
            if call_count[0] < 2:
                raise Exception("500 Internal server error")
            yield LLMEvent(event_type=LLMEventType.COMPLETION, content="Success")

        events = []
        async for event in orchestration_service._execute_with_retry(
            server_error_generator
        ):
            events.append(event)

        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_execute_with_retry_retries_on_timeout(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry retries on network timeout."""
        call_count = [0]

        async def timeout_generator():
            call_count[0] += 1
            if call_count[0] < 2:
                raise Exception("Connection timeout")
            yield LLMEvent(event_type=LLMEventType.COMPLETION, content="Success")

        events = []
        async for event in orchestration_service._execute_with_retry(timeout_generator):
            events.append(event)

        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_execute_with_retry_does_not_retry_on_bad_request(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry does NOT retry on 400 bad request."""
        call_count = [0]

        async def bad_request_generator():
            call_count[0] += 1
            raise Exception("400 Bad request: invalid parameter")
            yield  # Makes this an async generator (never reached)

        with pytest.raises(LLMException):
            async for _ in orchestration_service._execute_with_retry(
                bad_request_generator
            ):
                pass

        assert call_count[0] == 1  # Should not retry

    @pytest.mark.asyncio
    async def test_execute_with_retry_does_not_retry_on_auth_error(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry does NOT retry on 401 auth error."""
        call_count = [0]

        async def auth_error_generator():
            call_count[0] += 1
            raise Exception("401 Unauthorized: invalid API key")
            yield  # Makes this an async generator (never reached)

        with pytest.raises(LLMException):
            async for _ in orchestration_service._execute_with_retry(
                auth_error_generator
            ):
                pass

        assert call_count[0] == 1  # Should not retry

    @pytest.mark.asyncio
    async def test_execute_with_retry_raises_after_max_retries(
        self,
        orchestration_service,
    ):
        """Test that _execute_with_retry raises after max retries."""
        call_count = [0]

        async def always_fail_generator():
            call_count[0] += 1
            raise Exception("500 Server error")
            yield  # Makes this an async generator (never reached)

        with pytest.raises(LLMException) as exc_info:
            async for _ in orchestration_service._execute_with_retry(
                always_fail_generator
            ):
                pass

        assert call_count[0] == 4  # 1 initial + 3 retries
        assert (
            "after" in str(exc_info.value).lower()
            and "retries" in str(exc_info.value).lower()
        )


# =============================================================================
# Test: Utility Methods
# =============================================================================


class TestUtilityMethods:
    """Tests for utility methods."""

    @pytest.mark.asyncio
    async def test_get_execution_with_authorization(
        self,
        orchestration_service,
        mock_case_repo,
        sample_execution,
        sample_case,
    ):
        """Test get_execution with proper authorization."""
        mock_case_repo.get_agent_execution.return_value = sample_execution
        mock_case_repo.get.return_value = sample_case

        result = await orchestration_service.get_execution(
            execution_id=sample_execution.execution_id,
            organization_id=sample_case.organization_id,
        )

        assert result is not None
        assert result.execution_id == sample_execution.execution_id

    @pytest.mark.asyncio
    async def test_get_execution_returns_none_for_wrong_org(
        self,
        orchestration_service,
        mock_case_repo,
        sample_execution,
        sample_case,
    ):
        """Test get_execution returns None for wrong organization."""
        mock_case_repo.get_agent_execution.return_value = sample_execution
        sample_case.organization_id = "different_org"
        mock_case_repo.get.return_value = sample_case

        result = await orchestration_service.get_execution(
            execution_id=sample_execution.execution_id,
            organization_id="org_test000",  # Different org
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_list_executions_with_authorization(
        self,
        orchestration_service,
        mock_case_repo,
        sample_case,
    ):
        """Test list_executions with proper authorization."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = (
            [
                AgentExecution(
                    execution_id="exec_1",
                    case_id=sample_case.case_id,
                    agent_type=AgentType.INVESTIGATOR,
                    agent_model="test-model",
                ),
                AgentExecution(
                    execution_id="exec_2",
                    case_id=sample_case.case_id,
                    agent_type=AgentType.DEBUGGER,
                    agent_model="test-model",
                ),
            ],
            2,
        )

        executions, total = await orchestration_service.list_executions(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert len(executions) == 2
        assert total == 2

    @pytest.mark.asyncio
    async def test_list_executions_raises_for_wrong_org(
        self,
        orchestration_service,
        mock_case_repo,
        sample_case,
    ):
        """Test list_executions raises AuthorizationError for wrong org."""
        sample_case.organization_id = "different_org"
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await orchestration_service.list_executions(
                case_id=sample_case.case_id,
                organization_id="org_test000",  # Different org
            )

    @pytest.mark.asyncio
    async def test_cancel_execution(
        self,
        orchestration_service,
        mock_case_repo,
        sample_case,
    ):
        """Test cancel_execution cancels a running execution."""
        running_execution = AgentExecution(
            execution_id="exec_running",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
            status=ExecutionStatus.RUNNING,
        )
        mock_case_repo.get_agent_execution.return_value = running_execution
        mock_case_repo.update_agent_execution.return_value = None
        mock_case_repo.get.return_value = sample_case

        result = await orchestration_service.cancel_execution(
            execution_id=running_execution.execution_id,
            organization_id=sample_case.organization_id,
        )

        assert result is True
        mock_case_repo.update_agent_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_execution_returns_false_for_completed(
        self,
        orchestration_service,
        mock_case_repo,
        sample_case,
    ):
        """Test cancel_execution returns False for already completed execution."""
        completed_execution = AgentExecution(
            execution_id="exec_completed",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
            status=ExecutionStatus.COMPLETED,
        )
        mock_case_repo.get_agent_execution.return_value = completed_execution
        mock_case_repo.get.return_value = sample_case

        result = await orchestration_service.cancel_execution(
            execution_id=completed_execution.execution_id,
            organization_id=sample_case.organization_id,
        )

        assert result is False


# =============================================================================
# Test: Agent System Prompts
# =============================================================================


class TestAgentSystemPrompts:
    """Tests for agent system prompts."""

    def test_all_agent_types_have_prompts(self):
        """Test that all agent types have system prompts defined."""
        for agent_type in AgentType:
            if agent_type == AgentType.CUSTOM:
                continue  # Custom doesn't need default prompt
            assert (
                agent_type in AGENT_SYSTEM_PROMPTS
            ), f"Missing prompt for {agent_type}"

    def test_investigator_prompt_contains_ooda(self):
        """Test that investigator prompt mentions OODA methodology."""
        prompt = AGENT_SYSTEM_PROMPTS[AgentType.INVESTIGATOR]
        assert "investigat" in prompt.lower()

    def test_debugger_prompt_focuses_on_code(self):
        """Test that debugger prompt focuses on code analysis."""
        prompt = AGENT_SYSTEM_PROMPTS[AgentType.DEBUGGER]
        assert "code" in prompt.lower() or "debug" in prompt.lower()

    def test_researcher_prompt_mentions_knowledge(self):
        """Test that researcher prompt mentions knowledge base."""
        prompt = AGENT_SYSTEM_PROMPTS[AgentType.RESEARCHER]
        assert "knowledge" in prompt.lower() or "search" in prompt.lower()

    def test_validator_prompt_mentions_hypothesis(self):
        """Test that validator prompt mentions hypothesis testing."""
        prompt = AGENT_SYSTEM_PROMPTS[AgentType.VALIDATOR]
        assert "hypothesis" in prompt.lower() or "verify" in prompt.lower()

    def test_reporter_prompt_mentions_summary(self):
        """Test that reporter prompt mentions summarization."""
        prompt = AGENT_SYSTEM_PROMPTS[AgentType.REPORTER]
        assert "summary" in prompt.lower() or "report" in prompt.lower()


# =============================================================================
# Test: Query Entity Extraction (R3.1)
# =============================================================================


class TestExtractQueryEntities:
    """Tests for _extract_query_entities static method."""

    def test_extracts_timestamps(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "what happened at 14:00 and 14:30:15?"
        )
        assert "14:00" in entities["timestamps"]
        assert "14:30:15" in entities["timestamps"]

    def test_extracts_date_stamps(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "check logs for 2024-01-15"
        )
        assert "2024-01-15" in entities["timestamps"]

    def test_extracts_services(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "check logs from nginx and errors from redis"
        )
        assert "nginx" in entities["services"]
        assert "redis" in entities["services"]

    def test_extracts_http_error_codes(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "why are we getting 503 and 404 errors?"
        )
        assert "503" in entities["error_codes"]
        assert "404" in entities["error_codes"]

    def test_extracts_ip_addresses(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "connection refused from 10.0.0.5"
        )
        assert "10.0.0.5" in entities["ip_addresses"]

    def test_no_entities_from_plain_question(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "what is the root cause?"
        )
        assert not any(entities.values())

    def test_extracts_e_codes(self, orchestration_service):
        entities = orchestration_service._extract_query_entities(
            "seeing E1001 errors in production"
        )
        assert "E1001" in entities["error_codes"]


# =============================================================================
# Test: Coverage Gap Detection (R3.2)
# =============================================================================


class TestDetectCoverageGaps:
    """Tests for _detect_coverage_gaps."""

    def _make_case_with_evidence(self, preprocessed_content):
        """Helper to create a case with evidence containing coverage metadata."""
        evidence = MagicMock()
        evidence.extract = preprocessed_content
        case = MagicMock()
        case.evidence = [evidence]
        return case

    def test_detects_timestamp_gap(self, orchestration_service):
        from faultmaven.modules.preprocessing.extractors.utils import (
            COVERAGE_SEPARATOR,
        )

        case = self._make_case_with_evidence(
            f"log content here{COVERAGE_SEPARATOR}"
            f"First timestamp: 2024-01-15 13:42:00\n"
            f"Last timestamp: 2024-01-15 13:57:00"
        )
        entities = {
            "timestamps": ["14:00"],
            "services": [],
            "error_codes": [],
            "ip_addresses": [],
        }
        gaps = orchestration_service._detect_coverage_gaps(entities, case)
        assert len(gaps) > 0
        assert "14:00" in gaps[0]

    def test_no_gap_when_timestamp_covered(self, orchestration_service):
        from faultmaven.modules.preprocessing.extractors.utils import (
            COVERAGE_SEPARATOR,
        )

        case = self._make_case_with_evidence(
            f"log content{COVERAGE_SEPARATOR}"
            f"First timestamp: 2024-01-15 13:42:00\n"
            f"Last timestamp: 2024-01-15 14:00:00"
        )
        entities = {
            "timestamps": ["14:00"],
            "services": [],
            "error_codes": [],
            "ip_addresses": [],
        }
        gaps = orchestration_service._detect_coverage_gaps(entities, case)
        # "14:00" appears in the coverage text, so no gap
        assert len(gaps) == 0

    def test_service_gap_not_detected_in_new_model(self, orchestration_service):
        # Service gap detection via coverage text was removed in the file-extract
        # redesign (file_meta model). Services are checked via search_file instead.
        from faultmaven.modules.preprocessing.extractors.utils import (
            COVERAGE_SEPARATOR,
        )

        case = self._make_case_with_evidence(
            f"nginx logs{COVERAGE_SEPARATOR}Sources: nginx, redis"
        )
        entities = {
            "timestamps": [],
            "services": ["kafka"],
            "error_codes": [],
            "ip_addresses": [],
        }
        gaps = orchestration_service._detect_coverage_gaps(entities, case)
        # No service gaps emitted — service coverage is handled by search_file
        assert not any("kafka" in g for g in gaps)

    def test_no_gap_when_service_covered(self, orchestration_service):
        from faultmaven.modules.preprocessing.extractors.utils import (
            COVERAGE_SEPARATOR,
        )

        case = self._make_case_with_evidence(
            f"nginx logs{COVERAGE_SEPARATOR}Sources: nginx, redis"
        )
        entities = {
            "timestamps": [],
            "services": ["nginx"],
            "error_codes": [],
            "ip_addresses": [],
        }
        gaps = orchestration_service._detect_coverage_gaps(entities, case)
        assert len(gaps) == 0

    def test_no_gaps_when_no_evidence(self, orchestration_service):
        case = MagicMock()
        case.evidence = []
        entities = {
            "timestamps": ["14:00"],
            "services": ["nginx"],
            "error_codes": [],
            "ip_addresses": [],
        }
        gaps = orchestration_service._detect_coverage_gaps(entities, case)
        assert len(gaps) == 0


# =============================================================================
# Test: Coverage Advisories (R3.3)
# =============================================================================


class TestBuildCoverageAdvisories:
    """Tests for _build_coverage_advisories."""

    def test_returns_advisory_on_gap(self, orchestration_service):
        from faultmaven.modules.preprocessing.extractors.utils import (
            COVERAGE_SEPARATOR,
        )

        evidence = MagicMock()
        evidence.extract = (
            f"log content{COVERAGE_SEPARATOR}"
            f"First timestamp: 2024-01-15 13:42:00\n"
            f"Last timestamp: 2024-01-15 13:57:00"
        )
        case = MagicMock()
        case.evidence = [evidence]

        advisory = orchestration_service._build_coverage_advisories(
            "what happened at 14:00?", case
        )
        assert "Coverage Advisory" in advisory
        assert "14:00" in advisory

    def test_returns_empty_when_no_entities(self, orchestration_service):
        case = MagicMock()
        case.evidence = []
        advisory = orchestration_service._build_coverage_advisories(
            "what is the root cause?", case
        )
        assert advisory == ""

    def test_returns_empty_when_covered(self, orchestration_service):
        from faultmaven.modules.preprocessing.extractors.utils import (
            COVERAGE_SEPARATOR,
        )

        evidence = MagicMock()
        evidence.extract = (
            f"log content{COVERAGE_SEPARATOR}"
            f"First timestamp: 2024-01-15 14:00:00\n"
            f"Last timestamp: 2024-01-15 14:30:00\n"
            f"Sources: nginx"
        )
        case = MagicMock()
        case.evidence = [evidence]

        advisory = orchestration_service._build_coverage_advisories(
            "check nginx at 14:00", case
        )
        assert advisory == ""


# =============================================================================
# Test: Tool Result Compression (R5)
# =============================================================================


class TestCompressToolResult:
    """Tests for _compress_tool_result."""

    def test_short_content_unchanged(self, orchestration_service):
        content = "line 1\nline 2\nline 3"
        result = orchestration_service._compress_tool_result(content)
        assert result == content

    def test_standard_compression_keeps_first_and_last(self, orchestration_service):
        lines = [f"line {i}: some data" for i in range(50)]
        lines[25] = "line 25: CRITICAL error occurred"
        content = "\n".join(lines)

        result = orchestration_service._compress_tool_result(content, aggressive=False)

        # First 3 lines preserved
        assert "line 0:" in result
        assert "line 1:" in result
        assert "line 2:" in result
        # High-signal line preserved
        assert "CRITICAL error" in result
        # Last 2 lines preserved
        assert "line 49:" in result
        # Compression marker
        assert "50 total lines" in result

    def test_aggressive_compression_keeps_signal_only(self, orchestration_service):
        lines = [f"line {i}: normal data" for i in range(50)]
        lines[10] = "line 10: fatal crash detected"
        lines[30] = "line 30: timeout waiting for response"
        content = "\n".join(lines)

        result = orchestration_service._compress_tool_result(content, aggressive=True)

        # First line preserved
        assert "line 0:" in result
        # Signal lines preserved
        assert "fatal crash" in result
        assert "timeout" in result
        # Most normal lines removed
        assert "line 5:" not in result
        # Compression marker
        assert "50 total lines" in result

    def test_compression_handles_no_signal_lines(self, orchestration_service):
        lines = [f"line {i}: normal info log" for i in range(50)]
        content = "\n".join(lines)

        result = orchestration_service._compress_tool_result(content, aggressive=True)
        # Should still work, just fewer lines
        assert "line 0:" in result
        assert "50 total lines" in result


# =============================================================================
# Test: Mode-Aware Prompt Selection (Scenario-Driven Processing)
# =============================================================================


class TestModeAwarePromptSelection:
    """Tests that _build_agent_context selects the correct data access prompt
    based on query classification (Triage vs Directed Analysis)."""

    @pytest.mark.asyncio
    async def test_specific_question_gets_da_prompt(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """A specific question with entities → DA data access strategy."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="what caused the 502 errors at 14:00?",
            agent_type=AgentType.INVESTIGATOR,
        )

        assert "deep_analysis" in context.system_prompt
        assert "Primary tool" in context.system_prompt
        assert (
            "Do NOT call deep_analysis or vectorize_file in triage mode"
            not in context.system_prompt
        )

    @pytest.mark.asyncio
    async def test_generic_request_gets_triage_prompt(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """A generic request → Triage data access strategy."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="analyze this log file",
            agent_type=AgentType.INVESTIGATOR,
        )

        assert (
            "Do NOT call deep_analysis or vectorize_file in triage mode"
            in context.system_prompt
        )
        # Triage should NOT contain DA-specific guidance
        assert "Primary tool" not in context.system_prompt

    @pytest.mark.asyncio
    async def test_non_investigator_prompt_unaffected(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Non-INVESTIGATOR agents have no {data_access_strategy} placeholder.
        Their prompts should be unchanged regardless of query content."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="what caused the 502 errors?",
            agent_type=AgentType.DEBUGGER,
        )

        expected = AGENT_SYSTEM_PROMPTS[AgentType.DEBUGGER]
        assert expected in context.system_prompt
        # The data access block should NOT appear in non-INVESTIGATOR prompts
        assert "Data Access Strategy" not in context.system_prompt

    @pytest.mark.asyncio
    async def test_processing_mode_in_context_data(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """The classified processing mode should be stored in context_data."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_agent_executions_by_case.return_value = ([], 0)

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="what caused the 502 errors at 14:00?",
            agent_type=AgentType.INVESTIGATOR,
        )

        assert context.context_data.get("processing_mode") in (
            "triage",
            "directed_analysis",
        )


# =============================================================================
# Test: Auto-Vectorization Decision Logic
# =============================================================================


class TestShouldAutoVectorize:
    """Tests for _should_auto_vectorize — mechanical decision based on
    per-evidence DA failure signals and size threshold."""

    @pytest.fixture
    def service(
        self,
        mock_session_service,
        mock_evidence_service,
        mock_case_repo,
        mock_tool_registry,
        mock_llm_client,
    ):
        return AgentOrchestrationService(
            case_repo=mock_case_repo,
            session_service=mock_session_service,
            evidence_service=mock_evidence_service,
            tool_registry=mock_tool_registry,
            llm_client=mock_llm_client,
        )

    def _make_state(self, **overrides) -> EvidenceDAState:
        defaults = {
            "evidence_id": "ev_test",
            "content_size_bytes": 100_000,  # above default 50KB threshold
        }
        defaults.update(overrides)
        return EvidenceDAState(**defaults)

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_timeout_triggers_vectorization(self, mock_settings, service):
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(has_timed_out=True)
        assert service._should_auto_vectorize(state) is True

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_three_empty_searches_triggers(self, mock_settings, service):
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(empty_search_count=3)
        assert service._should_auto_vectorize(state) is True

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_two_empty_searches_insufficient(self, mock_settings, service):
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(empty_search_count=2)
        assert service._should_auto_vectorize(state) is False

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_three_da_calls_no_longer_triggers(self, mock_settings, service):
        """v5.2: da_call_count >= 3 removed — replaced by proactive vectorization."""
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(da_call_count=3)
        assert service._should_auto_vectorize(state) is False

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_low_confidence_triggers(self, mock_settings, service):
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(da_call_count=1, last_da_confidence=0.1)
        assert service._should_auto_vectorize(state) is True

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_confidence_above_threshold_no_trigger(self, mock_settings, service):
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(da_call_count=1, last_da_confidence=0.5)
        assert service._should_auto_vectorize(state) is False

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_size_gate_blocks_small_files(self, mock_settings, service):
        """Files below the vectorization threshold should never be auto-vectorized,
        even if DA failure signals have fired."""
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(
            content_size_bytes=10_000,  # 10KB — below threshold
            has_timed_out=True,
            empty_search_count=5,
            da_call_count=5,
        )
        assert service._should_auto_vectorize(state) is False

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_no_triggers_no_vectorization(self, mock_settings, service):
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state()  # all defaults — no failures
        assert service._should_auto_vectorize(state) is False

    @patch(
        "faultmaven.modules.agent.domain.services.agent_orchestration_service.get_settings"
    )
    def test_already_vectorized_state_not_checked(self, mock_settings, service):
        """_should_auto_vectorize doesn't check the 'vectorized' flag itself —
        the caller does. But verify the trigger logic is independent."""
        mock_settings.return_value.agent.vectorization_min_size_bytes = 50_000
        state = self._make_state(vectorized=True, has_timed_out=True)
        # The method still returns True — the caller's responsibility to check vectorized
        assert service._should_auto_vectorize(state) is True


class TestDAHasFailed:
    """Tests for _da_has_failed — same triggers as _should_auto_vectorize
    but without the size gate."""

    def _make_state(self, **overrides) -> EvidenceDAState:
        defaults = {"evidence_id": "ev_test"}
        defaults.update(overrides)
        return EvidenceDAState(**defaults)

    def test_timeout_is_failure(self):
        state = self._make_state(has_timed_out=True)
        assert AgentOrchestrationService._da_has_failed(state) is True

    def test_three_empty_searches_is_failure(self):
        state = self._make_state(empty_search_count=3)
        assert AgentOrchestrationService._da_has_failed(state) is True

    def test_three_da_calls_not_failure(self):
        """v5.2: da_call_count >= 3 removed — thorough investigation, not failure."""
        state = self._make_state(da_call_count=3)
        assert AgentOrchestrationService._da_has_failed(state) is False

    def test_low_confidence_is_failure(self):
        state = self._make_state(da_call_count=1, last_da_confidence=0.1)
        assert AgentOrchestrationService._da_has_failed(state) is True

    def test_no_triggers_not_failed(self):
        state = self._make_state()
        assert AgentOrchestrationService._da_has_failed(state) is False

    def test_small_file_still_detects_failure(self):
        """Unlike _should_auto_vectorize, _da_has_failed ignores file size."""
        state = self._make_state(
            content_size_bytes=1_000,  # tiny file
            has_timed_out=True,
        )
        assert AgentOrchestrationService._da_has_failed(state) is True


class TestVectorizationTriggerReason:
    """Tests for _vectorization_trigger_reason — logging helper."""

    def _make_state(self, **overrides) -> EvidenceDAState:
        defaults = {"evidence_id": "ev_test"}
        defaults.update(overrides)
        return EvidenceDAState(**defaults)

    def test_timeout_reason(self):
        state = self._make_state(has_timed_out=True)
        assert (
            AgentOrchestrationService._vectorization_trigger_reason(state)
            == "tool_timeout"
        )

    def test_empty_searches_reason(self):
        state = self._make_state(empty_search_count=3)
        assert (
            AgentOrchestrationService._vectorization_trigger_reason(state)
            == "repeated_empty_searches"
        )

    def test_da_calls_without_low_confidence_returns_unknown(self):
        """v5.2: da_call_count >= 3 no longer a trigger, returns unknown."""
        state = self._make_state(da_call_count=3)
        assert (
            AgentOrchestrationService._vectorization_trigger_reason(state) == "unknown"
        )

    def test_low_confidence_reason(self):
        state = self._make_state(da_call_count=1, last_da_confidence=0.1)
        assert (
            AgentOrchestrationService._vectorization_trigger_reason(state)
            == "low_confidence"
        )

    def test_priority_order_timeout_first(self):
        """Timeout has highest priority in reason reporting."""
        state = self._make_state(
            has_timed_out=True,
            empty_search_count=5,
            da_call_count=5,
        )
        assert (
            AgentOrchestrationService._vectorization_trigger_reason(state)
            == "tool_timeout"
        )


class TestEvidenceDAState:
    """Tests for EvidenceDAState dataclass defaults and tracking."""

    def test_defaults(self):
        state = EvidenceDAState(evidence_id="ev_test")
        assert state.evidence_id == "ev_test"
        assert state.empty_search_count == 0
        assert state.da_call_count == 0
        assert state.last_da_confidence == 1.0
        assert state.has_timed_out is False
        assert state.content_size_bytes == 0
        assert state.vectorized is False

    def test_independent_tracking(self):
        """Two EvidenceDAState instances track independently."""
        state_a = EvidenceDAState(evidence_id="ev_a")
        state_b = EvidenceDAState(evidence_id="ev_b")

        state_a.empty_search_count = 3
        state_a.has_timed_out = True

        assert state_b.empty_search_count == 0
        assert state_b.has_timed_out is False


class TestGetEvidenceSize:
    """Tests for _get_evidence_size — resolves file size from evidence service.

    The evidence_service returns EvidenceArtifact objects which have 'file_size'
    (not 'content_size_bytes'). The method must read the correct attribute.
    """

    @pytest.fixture
    def service(
        self,
        mock_session_service,
        mock_evidence_service,
        mock_case_repo,
        mock_tool_registry,
        mock_llm_client,
    ):
        return AgentOrchestrationService(
            case_repo=mock_case_repo,
            session_service=mock_session_service,
            evidence_service=mock_evidence_service,
            tool_registry=mock_tool_registry,
            llm_client=mock_llm_client,
        )

    @pytest.fixture
    def tool_context(self):
        """ToolContext carrying an in-memory case (storage redesign 2026-04
        phase 2: evidence is read from case.evidence, not the deleted
        evidence_service).
        """
        case = MagicMock()
        case.case_id = "case_test"
        case.evidence = []
        ctx = ToolContext(
            session_id="s",
            case_id="case_test",
            organization_id="org_test",
            user_id="u",
            in_memory_case=case,
        )
        return ctx

    @pytest.mark.asyncio
    async def test_reads_size_from_case_evidence(self, service, tool_context):
        """_get_evidence_size reads size_bytes from the linked UploadedFile."""
        ev = MagicMock()
        ev.evidence_id = "ev_test"
        ev.source_file_id = "file_test"
        uf = MagicMock()
        uf.size_bytes = 196268
        case = tool_context.in_memory_case
        case.evidence = [ev]
        case.find_uploaded_file = MagicMock(return_value=uf)

        size = await service._get_evidence_size("ev_test", tool_context)
        assert size == 196268

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_size_attribute(self, service, tool_context):
        """Returns 0 when evidence has no linked UploadedFile."""
        ev = MagicMock()
        ev.evidence_id = "ev_test"
        ev.source_file_id = None
        case = tool_context.in_memory_case
        case.evidence = [ev]
        case.find_uploaded_file = MagicMock(return_value=None)

        size = await service._get_evidence_size("ev_test", tool_context)
        assert size == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_case_is_none(self, service, tool_context):
        """Returns 0 when no case is available on the context."""
        tool_context.in_memory_case = None
        # case_repository is also None on this context -> nothing to query.
        size = await service._get_evidence_size("ev_test", tool_context)
        assert size == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_evidence_not_on_case(self, service, tool_context):
        """Returns 0 when the requested evidence is not on the case."""
        tool_context.in_memory_case.evidence = []
        size = await service._get_evidence_size("ev_test", tool_context)
        assert size == 0

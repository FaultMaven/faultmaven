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
    AgentOrchestrationService,
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
        status=CaseStatus.CONSULTING,  # Use CONSULTING to avoid validation requirements
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
        mock_case_repo.create_agent_execution.return_value = AgentExecution(
            execution_id="exec_new",
            case_id=sample_case.case_id,
            agent_type=AgentType.INVESTIGATOR,
            agent_model="test-model",
        )
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
        mock_case_repo.get.return_value = sample_case

        previous_executions = [
            AgentExecution(
                execution_id="exec_1",
                case_id=sample_case.case_id,
                agent_type=AgentType.INVESTIGATOR,
                agent_model="test-model",
                status=ExecutionStatus.COMPLETED,
                prompt="Previous question",
                response="Previous answer",
            ),
        ]
        mock_case_repo.list_agent_executions_by_case.return_value = (
            previous_executions,
            1,
        )

        context = await orchestration_service._build_agent_context(
            session=sample_session,
            user_message="Follow-up question",
            agent_type=AgentType.INVESTIGATOR,
        )

        # Should have user message plus previous conversation
        assert len(context.messages) >= 2  # At least previous Q&A + new message

    @pytest.mark.asyncio
    async def test_build_context_uses_correct_agent_prompt(
        self,
        orchestration_service,
        mock_case_repo,
        sample_session,
        sample_case,
    ):
        """Test that context uses correct system prompt for agent type."""
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

            # System prompt should contain agent-specific instructions
            expected_prompt = AGENT_SYSTEM_PROMPTS.get(
                agent_type, AGENT_SYSTEM_PROMPTS[AgentType.INVESTIGATOR]
            )
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
            evidence_service=mock_evidence_service,
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
            evidence_service=mock_evidence_service,
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
            evidence_service=mock_evidence_service,
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
            evidence_service=mock_evidence_service,
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
            evidence_service=mock_evidence_service,
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
            evidence_service=mock_evidence_service,
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
        assert "ooda" in prompt.lower()

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

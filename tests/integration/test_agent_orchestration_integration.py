"""Integration Tests for Agent Orchestration (TASK-015)

This module tests the full agent orchestration workflow including
case creation, session management, and agent execution with mocked LLM.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from faultmaven.services.agent_orchestration_service import AgentOrchestrationService
from faultmaven.services.investigation_session_service import APIInvestigationSessionService
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.models.case import Case, CaseStatus, CaseSeverity
from faultmaven.modules.evidence.domain.models import EvidenceArtifact, EvidenceArtifactType
from faultmaven.domain.events import (
    ExecutionEvent,
    ExecutionEventType,
    LLMEvent,
    LLMEventType,
    ToolCall,
)
from faultmaven.tools.agent_tools import AgentToolRegistry
from faultmaven.tools.read_file_tool import ReadFileTool
from faultmaven.tools.list_evidence_tool import ListEvidenceTool, SearchKnowledgeTool
from faultmaven.models.interfaces import ToolResult
from faultmaven.exceptions import (
    NotFoundError,
    AuthorizationError,
    ConflictError,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_organization_id():
    """Sample organization ID for tests."""
    return "org_integration_test"


@pytest.fixture
def sample_user_id():
    """Sample user ID for tests."""
    return "user_integration_test"


@pytest.fixture
def sample_case(sample_organization_id):
    """Create a sample case."""
    return Case(
        case_id="case_integration_test",
        organization_id=sample_organization_id,
        title="Integration Test Case",
        description="Testing agent orchestration integration",
        severity=CaseSeverity.MEDIUM,
        status=CaseStatus.INVESTIGATING,
    )


@pytest.fixture
def sample_session(sample_case, sample_organization_id, sample_user_id):
    """Create a sample session."""
    return InvestigationSession(
        session_id="session_integration_test",
        case_id=sample_case.case_id,
        user_id=sample_user_id,
        organization_id=sample_organization_id,
        status=SessionStatus.ACTIVE,
        session_goal="Integration testing",
        token_budget_limit=100000,
        total_token_usage=0,
    )


@pytest.fixture
def sample_evidence(sample_case):
    """Create sample evidence artifacts."""
    return [
        EvidenceArtifact(
            evidence_id="ev_log_1",
            case_id=sample_case.case_id,
            original_filename="error.log",
            storage_path="/data/ev_log_1",
            file_size=1024,
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            description="Application error log",
            is_primary=True,
        ),
        EvidenceArtifact(
            evidence_id="ev_config_1",
            case_id=sample_case.case_id,
            original_filename="config.json",
            storage_path="/data/ev_config_1",
            file_size=512,
            mime_type="application/json",
            evidence_type=EvidenceArtifactType.CONFIG_FILE,
            description="Application configuration",
            is_primary=False,
        ),
    ]


@pytest.fixture
def mock_repositories(sample_case, sample_session, sample_evidence):
    """Create mock repositories."""
    session_repo = AsyncMock()
    session_repo.get_by_id.return_value = sample_session
    session_repo.get_active_session.return_value = sample_session
    session_repo.create.return_value = sample_session
    session_repo.update.return_value = sample_session

    execution_repo = AsyncMock()
    execution_repo.create_execution.side_effect = lambda e: e
    execution_repo.update_execution.return_value = None
    execution_repo.get_execution.return_value = None
    execution_repo.list_executions_by_case.return_value = ([], 0)
    execution_repo.save_tool_call.return_value = None

    case_repo = AsyncMock()
    case_repo.get.return_value = sample_case

    evidence_repo = AsyncMock()

    return {
        "session_repo": session_repo,
        "execution_repo": execution_repo,
        "case_repo": case_repo,
        "evidence_repo": evidence_repo,
    }


@pytest.fixture
def mock_services(
    mock_repositories,
    sample_case,
    sample_session,
    sample_evidence,
    sample_organization_id,
):
    """Create mock services."""
    # Session service
    session_service = AsyncMock(spec=APIInvestigationSessionService)
    session_service.get_session.return_value = sample_session
    session_service.check_budget_exceeded.return_value = {"is_over_budget": False}
    session_service.add_execution_to_session.return_value = sample_session
    session_service.pause_session.return_value = sample_session

    # Evidence service
    evidence_service = AsyncMock(spec=APIEvidenceArtifactService)
    evidence_service.get_evidence.return_value = sample_evidence[0]
    evidence_service.list_evidence_by_case.return_value = sample_evidence
    evidence_service.download_evidence.return_value = (
        b"ERROR: Connection timeout at 2024-01-15 10:30:45\nWARNING: Retrying connection...\n",
        "error.log",
        "text/plain",
    )

    return {
        "session_service": session_service,
        "evidence_service": evidence_service,
    }


@pytest.fixture
def tool_registry(mock_services):
    """Create a tool registry with real tools."""
    registry = AgentToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListEvidenceTool())
    registry.register(SearchKnowledgeTool())
    return registry


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    client = MagicMock()
    client.model = "test-model"
    return client


@pytest.fixture
def orchestration_service(
    mock_services,
    mock_repositories,
    tool_registry,
    mock_llm_client,
):
    """Create an AgentOrchestrationService with mock dependencies."""
    return AgentOrchestrationService(
        session_service=mock_services["session_service"],
        evidence_service=mock_services["evidence_service"],
        execution_repo=mock_repositories["execution_repo"],
        case_repo=mock_repositories["case_repo"],
        tool_registry=tool_registry,
        llm_client=mock_llm_client,
        max_retries=2,
        retry_initial_delay=0.01,
        tool_timeout=5,
        max_parallel_tools=3,
    )


# =============================================================================
# Test: Complete Agent Workflow
# =============================================================================


class TestCompleteAgentWorkflow:
    """Tests for complete agent execution workflow."""

    @pytest.mark.asyncio
    async def test_simple_query_workflow(
        self,
        orchestration_service,
        sample_session,
        sample_case,
        mock_services,
    ):
        """Test complete workflow: simple query without tool calls."""
        # Setup LLM response
        async def mock_stream():
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Based on my analysis, ")
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="the issue appears to be ")
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="a connection timeout.")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 100, "output_tokens": 30},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        # Execute
        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="What is causing the API errors?",
            agent_type=AgentType.INVESTIGATOR,
        ):
            events.append(event)

        # Verify workflow
        event_types = [e.event_type for e in events]

        # Should have: STARTED, RESPONSE chunks, COMPLETED
        assert ExecutionEventType.STARTED in event_types
        assert ExecutionEventType.RESPONSE in event_types
        assert ExecutionEventType.COMPLETED in event_types

        # Response should be accumulated
        response_content = "".join(
            e.content for e in events if e.event_type == ExecutionEventType.RESPONSE
        )
        assert "connection timeout" in response_content

        # Session should be updated with token usage
        mock_services["session_service"].add_execution_to_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_workflow_with_tool_call(
        self,
        orchestration_service,
        sample_session,
        sample_case,
        sample_evidence,
        mock_services,
    ):
        """Test workflow with agent calling tools."""
        tool_call = ToolCall(
            id="tc_read_1",
            name="read_file",
            arguments={"evidence_id": sample_evidence[0].evidence_id},
        )

        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: request tool
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Let me check the log file.")
                yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tool_call)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 50, "output_tokens": 20},
                )
            else:
                # Second call: provide response after tool result
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="I found a connection timeout error in the log.")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 100, "output_tokens": 25},
                )

        orchestration_service._llm_client.stream_completion = mock_stream

        # Execute
        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Please analyze the log file",
            agent_type=AgentType.DEBUGGER,
        ):
            events.append(event)

        # Verify tool call workflow
        event_types = [e.event_type for e in events]

        assert ExecutionEventType.TOOL_CALL in event_types
        assert ExecutionEventType.TOOL_RESULT in event_types

        # Tool result should indicate success
        tool_result_events = [e for e in events if e.event_type == ExecutionEventType.TOOL_RESULT]
        assert len(tool_result_events) >= 1
        assert tool_result_events[0].metadata["success"] is True

    @pytest.mark.asyncio
    async def test_workflow_with_list_evidence_tool(
        self,
        orchestration_service,
        sample_session,
        sample_evidence,
        mock_services,
    ):
        """Test workflow using list_evidence tool."""
        tool_call = ToolCall(
            id="tc_list_1",
            name="list_evidence",
            arguments={},
        )

        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] == 1:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Let me list the evidence files.")
                yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tool_call)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 40, "output_tokens": 15},
                )
            else:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="I found 2 evidence files: error.log and config.json.")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 80, "output_tokens": 20},
                )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="What evidence do we have?",
        ):
            events.append(event)

        # Should have tool call and result
        tool_result_events = [e for e in events if e.event_type == ExecutionEventType.TOOL_RESULT]
        assert len(tool_result_events) >= 1

    @pytest.mark.asyncio
    async def test_workflow_with_multiple_tool_calls(
        self,
        orchestration_service,
        sample_session,
        sample_evidence,
        mock_services,
    ):
        """Test workflow with multiple tool calls in one response."""
        tool_calls = [
            ToolCall(id="tc_1", name="list_evidence", arguments={}),
            ToolCall(id="tc_2", name="read_file", arguments={"evidence_id": "ev_log_1"}),
        ]

        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] == 1:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Let me gather information.")
                for tc in tool_calls:
                    yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tc)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 60, "output_tokens": 25},
                )
            else:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Based on all the evidence, I found the issue.")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 150, "output_tokens": 30},
                )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Analyze all available evidence",
        ):
            events.append(event)

        # Should have multiple tool calls and results
        tool_call_events = [e for e in events if e.event_type == ExecutionEventType.TOOL_CALL]
        tool_result_events = [e for e in events if e.event_type == ExecutionEventType.TOOL_RESULT]

        assert len(tool_call_events) >= 2
        assert len(tool_result_events) >= 2


# =============================================================================
# Test: Token Budget Enforcement
# =============================================================================


class TestTokenBudgetEnforcement:
    """Tests for token budget tracking and enforcement."""

    @pytest.mark.asyncio
    async def test_session_auto_paused_on_budget_exceeded(
        self,
        orchestration_service,
        sample_session,
        mock_services,
    ):
        """Test session is automatically paused when budget is exceeded."""
        # First check: not over, second check: over budget
        mock_services["session_service"].check_budget_exceeded.side_effect = [
            {"is_over_budget": False},
            {"is_over_budget": True},
        ]

        async def mock_stream():
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Response")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 99000, "output_tokens": 1500},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        async for _ in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Test message",
        ):
            pass

        # Session should be paused
        mock_services["session_service"].pause_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_execution_blocked_when_already_over_budget(
        self,
        orchestration_service,
        sample_session,
        mock_services,
    ):
        """Test execution is blocked when budget already exceeded."""
        mock_services["session_service"].check_budget_exceeded.return_value = {
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


# =============================================================================
# Test: Multi-Turn Conversation
# =============================================================================


class TestMultiTurnConversation:
    """Tests for multi-turn conversation handling."""

    @pytest.mark.asyncio
    async def test_context_includes_previous_executions(
        self,
        orchestration_service,
        sample_session,
        mock_repositories,
        mock_services,
    ):
        """Test that context includes previous conversation history."""
        # Add previous executions
        previous_executions = [
            AgentExecution(
                execution_id="exec_prev_1",
                case_id=sample_session.case_id,
                agent_type=AgentType.INVESTIGATOR,
                agent_model="test-model",
                status=ExecutionStatus.COMPLETED,
                prompt="What is the error?",
                response="The error is a connection timeout.",
            ),
        ]
        mock_repositories["execution_repo"].list_executions_by_case.return_value = (
            previous_executions,
            1,
        )

        async def mock_stream():
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Building on my previous analysis...")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 200, "output_tokens": 30},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="What should I do about it?",
        ):
            events.append(event)

        # Execution should complete successfully with context
        assert any(e.event_type == ExecutionEventType.COMPLETED for e in events)


# =============================================================================
# Test: Authorization Enforcement
# =============================================================================


class TestAuthorizationEnforcement:
    """Tests for authorization checks."""

    @pytest.mark.asyncio
    async def test_execution_blocked_for_wrong_organization(
        self,
        orchestration_service,
        sample_session,
        mock_services,
    ):
        """Test execution is blocked for wrong organization."""
        mock_services["session_service"].get_session.return_value = None

        with pytest.raises(NotFoundError):
            async for _ in orchestration_service.execute_agent(
                session_id=sample_session.session_id,
                organization_id="wrong_org",
                user_message="Test message",
            ):
                pass

    @pytest.mark.asyncio
    async def test_list_executions_blocked_for_wrong_org(
        self,
        orchestration_service,
        sample_case,
        mock_repositories,
    ):
        """Test list_executions blocked for wrong organization."""
        mock_repositories["case_repo"].get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await orchestration_service.list_executions(
                case_id=sample_case.case_id,
                organization_id="wrong_org",
            )


# =============================================================================
# Test: Error Recovery
# =============================================================================


class TestErrorRecovery:
    """Tests for error handling and recovery."""

    @pytest.mark.asyncio
    async def test_tool_failure_continues_execution(
        self,
        orchestration_service,
        sample_session,
        sample_evidence,
        mock_services,
    ):
        """Test that tool failure doesn't stop execution."""
        # Make evidence service fail for read
        mock_services["evidence_service"].get_evidence.return_value = None

        tool_call = ToolCall(
            id="tc_1",
            name="read_file",
            arguments={"evidence_id": "nonexistent"},
        )

        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] == 1:
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Let me read the file.")
                yield LLMEvent(event_type=LLMEventType.TOOL_USE, content=tool_call)
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 40, "output_tokens": 15},
                )
            else:
                # Agent continues after tool failure
                yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="The file was not found. Let me try another approach.")
                yield LLMEvent(
                    event_type=LLMEventType.COMPLETION,
                    content="",
                    metadata={"input_tokens": 80, "output_tokens": 20},
                )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Read the file",
        ):
            events.append(event)

        # Should still complete
        assert any(e.event_type == ExecutionEventType.COMPLETED for e in events)

        # Tool result should indicate failure
        tool_result_events = [e for e in events if e.event_type == ExecutionEventType.TOOL_RESULT]
        assert len(tool_result_events) >= 1
        assert tool_result_events[0].metadata["success"] is False

    @pytest.mark.asyncio
    async def test_llm_retry_on_transient_error(
        self,
        orchestration_service,
        sample_session,
        mock_services,
    ):
        """Test LLM call is retried on transient errors."""
        call_count = [0]

        async def mock_stream():
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("500 Internal server error")
            yield LLMEvent(event_type=LLMEventType.TEXT_CHUNK, content="Success after retry")
            yield LLMEvent(
                event_type=LLMEventType.COMPLETION,
                content="",
                metadata={"input_tokens": 50, "output_tokens": 10},
            )

        orchestration_service._llm_client.stream_completion = mock_stream

        events = []
        async for event in orchestration_service.execute_agent(
            session_id=sample_session.session_id,
            organization_id=sample_session.organization_id,
            user_message="Test message",
        ):
            events.append(event)

        # Should succeed after retry
        assert any(e.event_type == ExecutionEventType.COMPLETED for e in events)
        assert call_count[0] == 2  # Initial + 1 retry

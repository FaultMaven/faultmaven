"""Agent Execution API Routes (TASK-016, TASK-017, TASK-020)

Purpose: FastAPI routes for AI agent execution with SSE streaming support.

Endpoints:
- POST /api/v1/cases/{case_id}/sessions/{session_id}/execute - Execute agent
- GET  /api/v1/cases/{case_id}/sessions/{session_id}/executions - List executions
- GET  /api/v1/cases/{case_id}/sessions/{session_id}/executions/{id} - Get execution
- POST /api/v1/cases/{case_id}/sessions/{session_id}/executions/{id}/cancel - Cancel

Authentication:
- JWT Bearer token: Authorization: Bearer <token>

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import logging
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Body, Depends, Path, status
from fastapi.responses import StreamingResponse

from faultmaven.api.dependencies import get_agent_orchestration_service
from faultmaven.api.middleware.auth import get_current_user
from faultmaven.api.models import (
    AgentExecutionRequest,
    AgentExecutionResponse,
    ExecutionEventSSE,
)
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    LLMException,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.modules.agent.domain.events.execution_events import ExecutionEventType
from faultmaven.modules.agent.domain.models.agent_execution import AgentType
from faultmaven.modules.agent.domain.services.agent_orchestration_service import (
    AgentOrchestrationService,
)
from faultmaven.modules.auth.contracts import AuthenticatedUser

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/cases/{case_id}/sessions/{session_id}",
    tags=["Agent Execution"],
)


# ============================================================
# Agent Execution Endpoints
# ============================================================


@router.post(
    "/execute",
    response_model=AgentExecutionResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Agent execution completed (non-streaming) or SSE stream (streaming)",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/AgentExecutionResponse"}
                },
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": 'event: started\ndata: {"content":"Execution started","metadata":{"execution_id":"exec-123"}}\n\n',
                },
            },
        },
        404: {"description": "Session not found"},
        403: {"description": "Forbidden - wrong organization"},
        409: {"description": "Conflict - session not active or budget exceeded"},
        422: {"description": "Validation error"},
        500: {"description": "LLM or tool execution error"},
    },
    summary="Execute AI agent for troubleshooting investigation",
    description="""
Execute an AI agent to analyze the case and generate recommendations.
Supports streaming (SSE) or non-streaming mode.

**Authentication:**
- JWT Bearer token: Authorization: Bearer <token>

**Streaming Mode (stream=true, default):**
Returns Server-Sent Events (SSE) with real-time updates including:
- `started`: Execution has begun
- `thinking`: Agent is reasoning/processing
- `tool_call`: Tool invocation requested
- `tool_result`: Tool execution completed
- `response`: Incremental response chunk
- `error`: Error occurred
- `completed`: Execution finished

**Non-Streaming Mode (stream=false):**
Returns complete AgentExecutionResponse when done.

The agent will:
- Analyze case context and previous conversation
- Use available tools (read evidence, search knowledge)
- Generate hypotheses and recommendations
- Stream thinking process in real-time

Token usage is tracked and the session will auto-pause if budget is exceeded.
""",
)
async def execute_agent(
    case_id: str = Path(..., description="Case ID"),
    session_id: str = Path(..., description="Investigation session ID"),
    request: AgentExecutionRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service),
) -> AgentExecutionResponse:
    """Execute AI agent for troubleshooting investigation.

    Supports two modes:
    1. Streaming (stream=true): Returns Server-Sent Events (SSE) with real-time updates
    2. Non-streaming (stream=false): Returns complete response when done

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case the session belongs to
        session_id: Unique session identifier
        request: Agent execution request with user message
        current_user: Authenticated user from JWT
        agent_service: Injected agent orchestration service

    Returns:
        Streaming: StreamingResponse with SSE events
        Non-streaming: AgentExecutionResponse with complete results

    Raises:
        401: Authentication required
        404: Session not found
        403: Not authorized (wrong organization)
        409: Session not active or budget exceeded
        422: Validation error (invalid agent_type, etc.)
        500: LLM or internal error
    """
    # Parse and validate agent type
    try:
        agent_type = AgentType(request.agent_type)
    except ValueError:
        valid_types = [t.value for t in AgentType]
        raise ValidationException(
            f"Invalid agent_type: '{request.agent_type}'. "
            f"Valid types are: {valid_types}"
        )

    if request.stream:
        # Streaming mode: Return SSE
        return StreamingResponse(
            _stream_agent_execution(
                agent_service=agent_service,
                session_id=session_id,
                organization_id=current_user.organization_id,
                user_message=request.user_message,
                agent_type=agent_type,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
                "Connection": "keep-alive",
            },
        )
    else:
        # Non-streaming mode: Return complete response
        return await _execute_non_streaming(
            agent_service=agent_service,
            session_id=session_id,
            organization_id=current_user.organization_id,
            user_message=request.user_message,
            agent_type=agent_type,
        )


async def _stream_agent_execution(
    agent_service: AgentOrchestrationService,
    session_id: str,
    organization_id: str,
    user_message: str,
    agent_type: AgentType,
) -> AsyncGenerator[str, None]:
    """Stream agent execution events as SSE.

    Yields SSE-formatted events for real-time streaming to clients.

    Args:
        agent_service: Agent orchestration service
        session_id: Investigation session ID
        organization_id: Organization ID for authorization
        user_message: User's question/request
        agent_type: Type of agent to execute

    Yields:
        SSE-formatted event strings
    """
    try:
        async for event in agent_service.execute_agent(
            session_id=session_id,
            organization_id=organization_id,
            user_message=user_message,
            agent_type=agent_type,
            stream=True,
        ):
            # Convert ExecutionEvent to SSE format
            sse_event = ExecutionEventSSE.from_execution_event(event)
            yield sse_event.to_sse()

    except NotFoundError as e:
        # Session not found
        error_event = ExecutionEventSSE.error_event(
            error_code="not_found",
            message=str(e),
        )
        yield error_event.to_sse()

    except AuthorizationError as e:
        # Wrong organization
        error_event = ExecutionEventSSE.error_event(
            error_code="forbidden",
            message=str(e),
        )
        yield error_event.to_sse()

    except ConflictError as e:
        # Session not active or budget exceeded
        error_event = ExecutionEventSSE.error_event(
            error_code="conflict",
            message=str(e),
        )
        yield error_event.to_sse()

    except LLMException as e:
        # LLM error
        error_event = ExecutionEventSSE.error_event(
            error_code="llm_error",
            message=str(e),
        )
        yield error_event.to_sse()

    except ServiceError as e:
        # Service error
        error_event = ExecutionEventSSE.error_event(
            error_code="service_error",
            message=str(e),
        )
        yield error_event.to_sse()

    except Exception as e:
        # Unexpected error
        logger.exception("Unexpected error during agent execution streaming")
        error_event = ExecutionEventSSE.error_event(
            error_code="internal_error",
            message="An unexpected error occurred",
        )
        yield error_event.to_sse()


async def _execute_non_streaming(
    agent_service: AgentOrchestrationService,
    session_id: str,
    organization_id: str,
    user_message: str,
    agent_type: AgentType,
) -> AgentExecutionResponse:
    """Execute agent in non-streaming mode.

    Collects all events and returns complete response when done.
    Exceptions from the service layer propagate naturally to FastAPI
    exception handlers.

    Args:
        agent_service: Agent orchestration service
        session_id: Investigation session ID
        organization_id: Organization ID for authorization
        user_message: User's question/request
        agent_type: Type of agent to execute

    Returns:
        AgentExecutionResponse with complete execution results

    Raises:
        NotFoundError: Session not found
        AuthorizationError: Wrong organization
        ConflictError: Session not active or budget exceeded
        ServiceError: LLM or internal error
    """
    execution_id: Optional[str] = None

    # Execute agent - exceptions propagate to FastAPI exception handlers
    async for event in agent_service.execute_agent(
        session_id=session_id,
        organization_id=organization_id,
        user_message=user_message,
        agent_type=agent_type,
        stream=False,
    ):
        if event.event_type == ExecutionEventType.STARTED:
            if event.metadata:
                execution_id = event.metadata.get("execution_id") or event.execution_id
            elif event.execution_id:
                execution_id = event.execution_id

        elif event.event_type == ExecutionEventType.COMPLETED:
            if not execution_id and event.execution_id:
                execution_id = event.execution_id

    # Execution must have completed with an ID
    if not execution_id:
        raise ServiceError(
            "Execution did not complete successfully - no execution ID received"
        )

    # Get the execution record from repository
    execution = await agent_service.get_execution(execution_id, organization_id)
    if not execution:
        raise NotFoundError("Execution", execution_id)

    return AgentExecutionResponse.from_domain(execution)


@router.get(
    "/executions",
    response_model=list,
    status_code=status.HTTP_200_OK,
    summary="List executions for case",
    description="""List all agent executions for the case.

**Note**: Executions are stored at the case level, not the session level.
The session_id in the path is for URL consistency with the execute endpoint,
but filtering is done by case_id. All executions for the case are returned
regardless of which session initiated them.""",
)
async def list_executions(
    case_id: str = Path(..., description="Case ID"),
    session_id: str = Path(
        ..., description="Session ID (for URL consistency, not used for filtering)"
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    limit: int = 50,
    offset: int = 0,
    agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service),
) -> list:
    """List all agent executions for a case.

    **Design Note**: Executions are associated with cases, not individual sessions.
    The session_id path parameter is included for URL consistency with the
    /execute endpoint, but executions are filtered by case_id only.
    This allows viewing all executions across multiple investigation sessions.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case ID to list executions for
        session_id: Session ID (included for URL consistency, not used for filtering)
        current_user: Authenticated user from JWT
        limit: Maximum number of results (default 50)
        offset: Pagination offset (default 0)
        agent_service: Injected agent orchestration service

    Returns:
        List of AgentExecutionResponse for all executions in the case

    Raises:
        401: Authentication required
        404: Case not found
    """
    # Note: session_id is not used for filtering - executions are per-case
    _ = session_id  # Explicitly mark as intentionally unused

    executions, total = await agent_service.list_executions(
        case_id=case_id,
        organization_id=current_user.organization_id,
        limit=limit,
        offset=offset,
    )

    return [AgentExecutionResponse.from_domain(e) for e in executions]


@router.get(
    "/executions/{execution_id}",
    response_model=AgentExecutionResponse,
    status_code=status.HTTP_200_OK,
    summary="Get execution by ID",
    description="Get details of a specific agent execution.",
)
async def get_execution(
    case_id: str = Path(..., description="Case ID"),
    session_id: str = Path(..., description="Session ID (for URL consistency)"),
    execution_id: str = Path(..., description="Execution ID"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service),
) -> AgentExecutionResponse:
    """Get details of a specific agent execution.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case ID the execution belongs to
        session_id: Session ID (for URL consistency, not used for lookup)
        execution_id: Execution ID to retrieve
        current_user: Authenticated user from JWT
        agent_service: Injected agent orchestration service

    Returns:
        AgentExecutionResponse with execution details

    Raises:
        401: Authentication required
        404: Execution not found or doesn't belong to case
    """
    _ = session_id  # Explicitly mark as intentionally unused

    execution = await agent_service.get_execution(
        execution_id, current_user.organization_id
    )

    if not execution:
        raise NotFoundError("Execution", execution_id)

    # Verify the execution belongs to the specified case
    if execution.case_id != case_id:
        raise NotFoundError("Execution", execution_id)

    return AgentExecutionResponse.from_domain(execution)


@router.post(
    "/executions/{execution_id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel running execution",
    description="Cancel a running agent execution.",
)
async def cancel_execution(
    case_id: str = Path(..., description="Case ID"),
    session_id: str = Path(..., description="Session ID (for URL consistency)"),
    execution_id: str = Path(..., description="Execution ID"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service),
) -> dict:
    """Cancel a running agent execution.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Args:
        case_id: Case ID (for URL consistency)
        session_id: Session ID (for URL consistency, not used)
        execution_id: Execution ID to cancel
        current_user: Authenticated user from JWT
        agent_service: Injected agent orchestration service

    Returns:
        Status message with cancelled execution ID

    Raises:
        401: Authentication required
        404: Execution not found
        409: Execution not running (cannot be cancelled)
    """
    _ = session_id  # Explicitly mark as intentionally unused
    _ = case_id  # Case ID verification done by cancel_execution

    cancelled = await agent_service.cancel_execution(
        execution_id, current_user.organization_id
    )

    if not cancelled:
        raise NotFoundError("Execution", execution_id)

    return {"status": "cancelled", "execution_id": execution_id}

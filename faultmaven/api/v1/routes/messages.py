"""Session Messages & Agent Chat API Routes (TASK-027)

Purpose: REST API endpoints for session message management and simplified agent chat.

This module provides 2 CRITICAL endpoints:

Message Endpoints:
1. GET /api/v1/sessions/{session_id}/messages - Retrieve conversation history

Chat Endpoints:
2. POST /api/v1/agent/chat - Simplified agent chat with auto-session creation

Authentication:
- JWT Bearer token: Authorization: Bearer <token>

Design Reference: docs/working/TASK-027.md
"""

import logging
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse

from faultmaven.api.dependencies import (
    get_agent_orchestration_service,
    get_investigation_session_service,
    get_service_factory,
)
from faultmaven.api.middleware.auth import get_current_user
from faultmaven.api.models import ExecutionEventSSE
from faultmaven.domain.events import ExecutionEventType
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    LLMException,
    NotFoundError,
    ServiceError,
    ValidationException,
)
from faultmaven.models.agent_execution import AgentType, ExecutionStatus
from faultmaven.models.api_messages import (
    ChatRequest,
    ChatMessageResponse,
    ChatResponse,
    MessageListResponse,
    MessageMetadata,
    MessageResponse,
    PaginationInfo,
)
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.services.agent_orchestration_service import AgentOrchestrationService
from faultmaven.services.investigation_session_service import APIInvestigationSessionService
from faultmaven.services.service_factory import ServiceFactory


logger = logging.getLogger(__name__)


# Create router for session messages
router = APIRouter(tags=["messages", "agent_chat"])


# ============================================================
# Message Retrieval Endpoints
# ============================================================


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    status_code=status.HTTP_200_OK,
    summary="Get session messages",
    description="""Retrieve conversation history for an investigation session.

Returns messages in chronological order, with both user prompts and assistant responses.
Each agent execution produces 2 messages (user prompt + assistant response).

**Authentication:**
- JWT Bearer token: Authorization: Bearer <token>

**Pagination:**
- Use limit/offset for pagination (default: limit=50, max=100)
- Response includes total_count and has_more for pagination control
""",
    responses={
        200: {"description": "Messages retrieved successfully"},
        401: {"description": "Authentication required"},
        403: {"description": "Session belongs to different organization"},
        404: {"description": "Session not found"},
    },
)
async def get_session_messages(
    session_id: str = Path(..., description="Investigation session ID"),
    limit: int = Query(
        default=50,
        ge=1,
        le=100,
        description="Maximum number of messages to return (max 100)",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Offset for pagination",
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    factory: ServiceFactory = Depends(get_service_factory),
) -> MessageListResponse:
    """Retrieve conversation history for a session.

    Fetches agent executions for the session and transforms them into
    a message format suitable for frontend chat UI display.

    Args:
        session_id: Investigation session ID
        limit: Maximum messages to return (default 50, max 100)
        offset: Pagination offset
        current_user: Authenticated user from JWT
        factory: Service factory for repository access

    Returns:
        MessageListResponse with paginated messages

    Raises:
        401: Authentication required
        403: Session belongs to different organization
        404: Session not found
    """
    # Get session to validate access
    session = await factory.session_repo.get_session(session_id)
    if session is None:
        raise NotFoundError("Session", session_id)

    # Verify organization access
    if session.organization_id != current_user.organization_id:
        raise AuthorizationError(
            f"Session {session_id} belongs to a different organization"
        )

    # Get executions for the session
    # Pagination strategy: each execution = 2 messages (user prompt + assistant response)
    # For correct pagination with odd offsets, we need to:
    # 1. Calculate which execution contains the first message at the offset
    # 2. Fetch enough executions to fill the requested limit

    # Calculate execution range needed for the requested message range
    # offset=0 -> exec 0, offset=1 -> exec 0, offset=2 -> exec 1, etc.
    first_execution_idx = offset // 2
    # For limit messages, we need at most (limit + 1) // 2 + 1 executions
    # +1 to handle odd offset starting mid-execution
    execution_limit = (limit + 1) // 2 + 1

    executions, total_executions = await factory.execution_repo.list_executions_by_session(
        session_id=session_id,
        status=ExecutionStatus.COMPLETED,
        limit=execution_limit,
        offset=first_execution_idx,
    )

    # Transform executions to messages
    all_messages: List[MessageResponse] = []
    for execution in executions:
        # Extract tool calls from tool_calls list - always return list, not None
        tool_names = [tc.tool_name for tc in execution.tool_calls] if execution.tool_calls else []

        # User message (prompt)
        if execution.prompt:
            all_messages.append(
                MessageResponse(
                    message_id=execution.execution_id,
                    role="user",
                    content=execution.prompt,
                    timestamp=execution.started_at or execution.created_at,
                    metadata=MessageMetadata(
                        execution_id=execution.execution_id,
                        token_usage=None,
                        agent_type=None,
                        tool_calls=[],  # Empty list instead of None for consistency
                    ),
                )
            )

        # Assistant message (response)
        if execution.response:
            all_messages.append(
                MessageResponse(
                    message_id=execution.execution_id,
                    role="assistant",
                    content=execution.response,
                    timestamp=execution.completed_at or execution.created_at,
                    metadata=MessageMetadata(
                        execution_id=execution.execution_id,
                        token_usage=execution.token_usage,
                        agent_type=execution.agent_type.value,
                        tool_calls=tool_names,  # Always a list, may be empty
                    ),
                )
            )

    # Calculate total messages (each execution = 2 messages)
    total_messages = total_executions * 2

    # Apply offset within the fetched messages for precise pagination
    # If offset is odd, skip the first message (which is the user message from the execution)
    start_idx = offset % 2  # 0 for even offset, 1 for odd offset
    messages = all_messages[start_idx:start_idx + limit]

    # Calculate pagination info
    has_more = offset + len(messages) < total_messages
    next_offset = offset + len(messages) if has_more else None

    return MessageListResponse(
        session_id=session_id,
        messages=messages,
        total_count=total_messages,
        has_more=has_more,
        pagination=PaginationInfo(
            limit=limit,
            offset=offset,
            next_offset=next_offset,
        ),
    )


# ============================================================
# Agent Chat Endpoints
# ============================================================


@router.post(
    "/agent/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Simplified agent chat",
    description="""Execute agent chat with simplified interface and auto-session creation.

This endpoint provides a simplified chat interface that:
- Auto-creates a session if session_id is not provided
- Routes to the existing agent execution infrastructure
- Supports both streaming (SSE) and non-streaming responses

**Authentication:**
- JWT Bearer token: Authorization: Bearer <token>

**Streaming Mode (stream=true, default):**
Returns Server-Sent Events (SSE) with real-time updates.

**Non-Streaming Mode (stream=false):**
Returns complete ChatResponse when done.
""",
    responses={
        200: {
            "description": "Chat completed (non-streaming) or SSE stream (streaming)",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ChatResponse"}
                },
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": 'event: started\ndata: {"content":"Execution started"}\n\n',
                },
            },
        },
        401: {"description": "Authentication required"},
        403: {"description": "Case belongs to different organization"},
        404: {"description": "Case not found"},
        422: {"description": "Validation error (invalid agent_type, etc.)"},
        500: {"description": "LLM or internal error"},
    },
)
async def agent_chat(
    request: ChatRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
    factory: ServiceFactory = Depends(get_service_factory),
):
    """Simplified agent chat with auto-session creation.

    Provides a simplified chat interface that auto-creates sessions
    and routes to the existing agent execution infrastructure.

    Args:
        request: Chat request with case_id, message, and options
        current_user: Authenticated user from JWT
        agent_service: Agent orchestration service
        session_service: Investigation session service
        factory: Service factory for case validation

    Returns:
        Streaming: StreamingResponse with SSE events
        Non-streaming: ChatResponse with complete results

    Raises:
        401: Authentication required
        403: Case belongs to different organization
        404: Case not found
        422: Invalid agent_type
        500: LLM or internal error
    """
    # Validate case exists and belongs to organization
    case = await factory.case_repo.get_case(request.case_id)
    if case is None:
        raise NotFoundError("Case", request.case_id)

    if case.organization_id != current_user.organization_id:
        raise AuthorizationError(
            f"Case {request.case_id} belongs to a different organization"
        )

    # Validate agent type
    try:
        agent_type = AgentType(request.agent_type)
    except ValueError:
        valid_types = [t.value for t in AgentType]
        raise ValidationException(
            f"Invalid agent_type: '{request.agent_type}'. "
            f"Valid types are: {valid_types}"
        )

    # Auto-create session if not provided
    session_id = request.session_id
    if not session_id:
        # Create new session for this chat
        session = await session_service.create_session(
            case_id=request.case_id,
            organization_id=current_user.organization_id,
            user_id=current_user.user_id,
            session_goal=f"Agent chat session",
            metadata={"created_via": "chat_endpoint"},
        )
        session_id = session.session_id
        logger.info(f"Auto-created session {session_id} for chat request")
    else:
        # Validate existing session
        session = await factory.session_repo.get_session(session_id)
        if session is None:
            raise NotFoundError("Session", session_id)
        if session.organization_id != current_user.organization_id:
            raise AuthorizationError(
                f"Session {session_id} belongs to a different organization"
            )
        # Validate session belongs to the specified case
        if session.case_id != request.case_id:
            raise ValidationException(
                f"Session {session_id} belongs to case {session.case_id}, "
                f"not {request.case_id}"
            )

    if request.stream:
        # Streaming mode: Return SSE
        return StreamingResponse(
            _stream_chat_execution(
                agent_service=agent_service,
                session_id=session_id,
                organization_id=current_user.organization_id,
                user_message=request.message,
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
        return await _execute_chat_non_streaming(
            agent_service=agent_service,
            session_id=session_id,
            organization_id=current_user.organization_id,
            user_message=request.message,
            agent_type=agent_type,
        )


async def _stream_chat_execution(
    agent_service: AgentOrchestrationService,
    session_id: str,
    organization_id: str,
    user_message: str,
    agent_type: AgentType,
) -> AsyncGenerator[str, None]:
    """Stream chat execution events as SSE.

    Wraps the agent execution streaming with session_id in response.

    Args:
        agent_service: Agent orchestration service
        session_id: Investigation session ID
        organization_id: Organization ID for authorization
        user_message: User's question/request
        agent_type: Type of agent to execute

    Yields:
        SSE-formatted event strings with session_id included
    """
    try:
        async for event in agent_service.execute_agent(
            session_id=session_id,
            organization_id=organization_id,
            user_message=user_message,
            agent_type=agent_type,
            stream=True,
        ):
            # Add session_id to metadata for started event
            if event.event_type == ExecutionEventType.STARTED:
                if event.metadata is None:
                    event.metadata = {}
                event.metadata["session_id"] = session_id

            # Convert ExecutionEvent to SSE format
            sse_event = ExecutionEventSSE.from_execution_event(event)
            yield sse_event.to_sse()

    except NotFoundError as e:
        error_event = ExecutionEventSSE.error_event(
            error_code="not_found",
            message=str(e),
        )
        yield error_event.to_sse()

    except AuthorizationError as e:
        error_event = ExecutionEventSSE.error_event(
            error_code="forbidden",
            message=str(e),
        )
        yield error_event.to_sse()

    except ConflictError as e:
        error_event = ExecutionEventSSE.error_event(
            error_code="conflict",
            message=str(e),
        )
        yield error_event.to_sse()

    except LLMException as e:
        error_event = ExecutionEventSSE.error_event(
            error_code="llm_error",
            message=str(e),
        )
        yield error_event.to_sse()

    except ServiceError as e:
        error_event = ExecutionEventSSE.error_event(
            error_code="service_error",
            message=str(e),
        )
        yield error_event.to_sse()

    except Exception as e:
        logger.exception("Unexpected error during chat execution streaming")
        error_event = ExecutionEventSSE.error_event(
            error_code="internal_error",
            message="An unexpected error occurred",
        )
        yield error_event.to_sse()


async def _execute_chat_non_streaming(
    agent_service: AgentOrchestrationService,
    session_id: str,
    organization_id: str,
    user_message: str,
    agent_type: AgentType,
) -> ChatResponse:
    """Execute chat in non-streaming mode.

    Collects all events and returns complete ChatResponse when done.

    Args:
        agent_service: Agent orchestration service
        session_id: Investigation session ID
        organization_id: Organization ID for authorization
        user_message: User's question/request
        agent_type: Type of agent to execute

    Returns:
        ChatResponse with complete execution results

    Raises:
        NotFoundError: Session not found
        AuthorizationError: Wrong organization
        ConflictError: Session not active or budget exceeded
        ServiceError: LLM or internal error
    """
    import time
    start_time = time.time()

    execution_id: Optional[str] = None
    final_response = ""
    token_usage = {}
    tool_calls: List[str] = []

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

        elif event.event_type == ExecutionEventType.TOOL_CALL:
            if event.metadata and "tool_name" in event.metadata:
                tool_calls.append(event.metadata["tool_name"])

        elif event.event_type == ExecutionEventType.RESPONSE:
            final_response += event.content

        elif event.event_type == ExecutionEventType.COMPLETED:
            if not execution_id and event.execution_id:
                execution_id = event.execution_id
            if event.metadata:
                token_usage = {
                    "input": event.metadata.get("input_tokens", 0),
                    "output": event.metadata.get("output_tokens", 0),
                    "total": event.metadata.get("total_tokens", 0),
                }

    # Calculate duration
    duration_ms = int((time.time() - start_time) * 1000)

    if not execution_id:
        raise ServiceError("Execution did not complete successfully - no execution ID received")

    # Get the execution to retrieve final response if needed
    if not final_response:
        execution = await agent_service.get_execution(execution_id, organization_id)
        if execution:
            final_response = execution.response or ""
            token_usage = execution.token_usage or {}
            tool_calls = [tc.tool_name for tc in execution.tool_calls] if execution.tool_calls else []

    from datetime import datetime, timezone

    return ChatResponse(
        execution_id=execution_id,
        session_id=session_id,
        message=ChatMessageResponse(
            role="assistant",
            content=final_response,
            timestamp=datetime.now(timezone.utc),
        ),
        token_usage=token_usage,
        tool_calls=tool_calls,
        duration_ms=duration_ms,
    )

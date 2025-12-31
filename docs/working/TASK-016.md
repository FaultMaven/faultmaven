# TASK-016: Agent Execution REST API Endpoint

## Task Metadata
- **Phase**: Week 6, Day 1-2 (Agent Execution API)
- **Priority**: P0 (Public API for agent execution)
- **Estimated Time**: 1-2 days
- **Dependencies**: TASK-015 (Agent Orchestration Service)
- **Assignee**: Developer
- **Reports To**: Solutions Architect

## Objective

**Implement REST API endpoint for executing AI agents** that exposes the agent orchestration service (TASK-015) via HTTP with streaming response support using Server-Sent Events (SSE).

This endpoint enables frontend applications to:
1. **Execute agents** with user messages and receive streaming responses
2. **Stream execution events** in real-time (thinking, tool_call, response, completed)
3. **Support multi-turn conversations** within investigation sessions
4. **Enforce authorization** via organization and user headers
5. **Handle errors gracefully** with proper HTTP status codes

---

## Context

### Evolution Path
```
TASK-011: Case Service ✅
TASK-012: Session Service ✅
TASK-013: Evidence Service ✅
TASK-014: FastAPI Controllers ✅
TASK-015: Agent Orchestration ✅
TASK-016: Agent Execution API ← Current
TASK-017: Authentication & Authorization (JWT, RBAC)
TASK-018: WebSocket API (alternative to SSE)
```

### Architectural Position

```
┌─────────────────────────────────────────────────────────┐
│ Frontend (Dashboard/Copilot)                            │
│ EventSource("/api/v1/sessions/{id}/execute")           │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP POST with SSE streaming
┌────────────────────▼────────────────────────────────────┐
│ FastAPI Agent Execution Endpoint (TASK-016) ← This Task│
│ POST /api/v1/cases/{case_id}/sessions/{id}/execute     │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│ Agent Orchestration Service (TASK-015)                 │
│ execute_agent() → AsyncGenerator[ExecutionEvent]       │
└─────────────────────────────────────────────────────────┘
```

---

## Implementation Requirements

### 1. Agent Execution Endpoint

**File**: `faultmaven/api/routes/agent.py`

**Endpoint**: `POST /api/v1/cases/{case_id}/sessions/{session_id}/execute`

**Request Model**:
```python
class AgentExecutionRequest(BaseModel):
    """Request to execute an AI agent."""

    user_message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="User's question or request for the agent"
    )
    agent_type: AgentType = Field(
        default=AgentType.INVESTIGATOR,
        description="Type of agent to execute"
    )
    stream: bool = Field(
        default=True,
        description="Whether to stream response events (SSE)"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_message": "What is causing the 500 errors in the API?",
                "agent_type": "investigator",
                "stream": True
            }
        }
    )
```

**Response Models**:

```python
class AgentExecutionResponse(BaseModel):
    """Response from agent execution (non-streaming)."""

    execution_id: str
    status: ExecutionStatus
    agent_response: str
    tokens_used: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    tool_calls: List[ToolCallResponse] = []

    @classmethod
    def from_domain(cls, execution: AgentExecution) -> "AgentExecutionResponse":
        """Convert domain model to response."""
        return cls(
            execution_id=execution.execution_id,
            status=execution.status,
            agent_response=execution.agent_response or "",
            tokens_used=execution.tokens_used or 0,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            tool_calls=[
                ToolCallResponse.from_domain(tc)
                for tc in execution.tool_calls
            ]
        )

class ToolCallResponse(BaseModel):
    """Tool call within an execution."""

    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    result: Optional[str]
    status: ExecutionStatus

    @classmethod
    def from_domain(cls, tool_call: AgentToolCall) -> "ToolCallResponse":
        """Convert domain model to response."""
        return cls(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments,
            result=tool_call.result,
            status=tool_call.status
        )

# SSE Event Format (streaming)
class ExecutionEventSSE(BaseModel):
    """Server-Sent Event for execution streaming."""

    event: str  # "started", "thinking", "tool_call", "response", "error", "completed"
    data: Dict[str, Any]

    def to_sse(self) -> str:
        """Convert to SSE format."""
        return f"event: {self.event}\ndata: {self.model_dump_json()}\n\n"
```

**Endpoint Implementation**:

```python
from fastapi import APIRouter, Depends, Header, Path
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/v1/cases", tags=["Agent Execution"])

@router.post(
    "/{case_id}/sessions/{session_id}/execute",
    response_model=AgentExecutionResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Agent execution completed (non-streaming)"},
        404: {"description": "Session not found"},
        403: {"description": "Forbidden - wrong organization"},
        409: {"description": "Conflict - session not active or budget exceeded"},
        422: {"description": "Validation error"},
        500: {"description": "LLM or tool execution error"}
    }
)
async def execute_agent(
    case_id: str = Path(..., description="Case ID"),
    session_id: str = Path(..., description="Investigation session ID"),
    request: AgentExecutionRequest = ...,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    user_id: str = Header(..., alias="X-User-ID"),
    agent_service: AgentOrchestrationService = Depends(get_agent_orchestration_service),
):
    """
    Execute AI agent for troubleshooting investigation.

    Supports two modes:
    1. Streaming (stream=true): Returns Server-Sent Events (SSE) with real-time updates
    2. Non-streaming (stream=false): Returns complete response when done

    The agent will:
    - Analyze the case context and previous conversation
    - Use available tools (read evidence files, search knowledge base)
    - Generate hypotheses and recommendations
    - Stream thinking process and tool calls in real-time

    Token usage is tracked and the session will auto-pause if budget is exceeded.
    """

    if request.stream:
        # Streaming mode: Return SSE
        return EventSourceResponse(
            _stream_agent_execution(
                agent_service=agent_service,
                session_id=session_id,
                organization_id=organization_id,
                user_message=request.user_message,
                agent_type=request.agent_type,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            }
        )
    else:
        # Non-streaming mode: Return complete response
        execution = None
        async for event in agent_service.execute_agent(
            session_id=session_id,
            organization_id=organization_id,
            user_message=request.user_message,
            agent_type=request.agent_type,
            stream=False,
        ):
            if event.event_type == ExecutionEventType.COMPLETED:
                execution_id = event.metadata.get("execution_id")
                execution = await agent_service._execution_repo.get_by_id(
                    execution_id, organization_id
                )

        if not execution:
            raise ServiceError("Execution did not complete")

        return AgentExecutionResponse.from_domain(execution)


async def _stream_agent_execution(
    agent_service: AgentOrchestrationService,
    session_id: str,
    organization_id: str,
    user_message: str,
    agent_type: AgentType,
) -> AsyncGenerator[str, None]:
    """
    Stream agent execution events as SSE.

    Yields:
        SSE-formatted events
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
            sse_event = ExecutionEventSSE(
                event=event.event_type.value,
                data={
                    "content": event.content,
                    "metadata": event.metadata or {},
                    "timestamp": event.timestamp.isoformat(),
                }
            )
            yield sse_event.to_sse()

    except NotFoundError as e:
        # Session not found
        error_event = ExecutionEventSSE(
            event="error",
            data={"error": "not_found", "message": str(e)}
        )
        yield error_event.to_sse()

    except AuthorizationError as e:
        # Wrong organization
        error_event = ExecutionEventSSE(
            event="error",
            data={"error": "forbidden", "message": str(e)}
        )
        yield error_event.to_sse()

    except ConflictError as e:
        # Session not active or budget exceeded
        error_event = ExecutionEventSSE(
            event="error",
            data={"error": "conflict", "message": str(e)}
        )
        yield error_event.to_sse()

    except LLMException as e:
        # LLM error
        error_event = ExecutionEventSSE(
            event="error",
            data={"error": "llm_error", "message": str(e)}
        )
        yield error_event.to_sse()

    except Exception as e:
        # Unexpected error
        logger.exception("Unexpected error during agent execution")
        error_event = ExecutionEventSSE(
            event="error",
            data={"error": "internal_error", "message": "An unexpected error occurred"}
        )
        yield error_event.to_sse()
```

---

### 2. Service Factory Extension

**File**: `faultmaven/services/service_factory.py`

**New Function**:
```python
def get_agent_orchestration_service() -> AgentOrchestrationService:
    """Get agent orchestration service instance."""
    return AgentOrchestrationService(
        execution_repo=get_agent_execution_repository(),
        session_service=get_investigation_session_service(),
        case_repo=get_case_repository(),
        evidence_service=get_evidence_artifact_service(),
        llm_client=create_llm_client(),
        tool_registry=agent_tool_registry,
    )
```

---

### 3. OpenAPI Documentation

**Update**: `faultmaven/api/app.py`

**Tags**:
```python
tags_metadata = [
    {
        "name": "Agent Execution",
        "description": "Execute AI agents for troubleshooting investigations with streaming support"
    },
    # ... existing tags
]
```

**Example**:
```python
# In AgentExecutionRequest schema
model_config = ConfigDict(
    json_schema_extra={
        "example": {
            "user_message": "What is causing the 500 errors in the API?",
            "agent_type": "investigator",
            "stream": True
        }
    }
)
```

---

## SSE (Server-Sent Events) Format

### Event Types

```
event: started
data: {"content":"Execution started","metadata":{"execution_id":"exec-123"},"timestamp":"2025-12-30T10:00:00Z"}

event: thinking
data: {"content":"Analyzing the case context and evidence...","metadata":{},"timestamp":"2025-12-30T10:00:01Z"}

event: tool_call
data: {"content":"Calling tool: read_file","metadata":{"tool_name":"read_file","arguments":{"evidence_id":"ev-456"}},"timestamp":"2025-12-30T10:00:02Z"}

event: tool_result
data: {"content":"File contents retrieved successfully","metadata":{"tool_name":"read_file","result":"..."},"timestamp":"2025-12-30T10:00:03Z"}

event: response
data: {"content":"Based on the error logs, I can see that...","metadata":{},"timestamp":"2025-12-30T10:00:04Z"}

event: completed
data: {"content":"Execution completed","metadata":{"execution_id":"exec-123","tokens_used":1523},"timestamp":"2025-12-30T10:00:10Z"}
```

### Error Events

```
event: error
data: {"error":"not_found","message":"Session not found"}

event: error
data: {"error":"forbidden","message":"Session does not belong to organization"}

event: error
data: {"error":"conflict","message":"Session is not active"}

event: error
data: {"error":"conflict","message":"Token budget exceeded for session"}

event: error
data: {"error":"llm_error","message":"LLM rate limit exceeded"}
```

---

## Client Usage Examples

### JavaScript (Browser)

```javascript
// Create EventSource connection
const eventSource = new EventSource('/api/v1/cases/case-123/sessions/sess-456/execute', {
    headers: {
        'X-Organization-ID': 'org-789',
        'X-User-ID': 'user-101'
    }
});

// Listen for different event types
eventSource.addEventListener('started', (e) => {
    const data = JSON.parse(e.data);
    console.log('Execution started:', data.metadata.execution_id);
});

eventSource.addEventListener('thinking', (e) => {
    const data = JSON.parse(e.data);
    updateUI('thinking', data.content);
});

eventSource.addEventListener('response', (e) => {
    const data = JSON.parse(e.data);
    appendMessage(data.content);
});

eventSource.addEventListener('completed', (e) => {
    const data = JSON.parse(e.data);
    console.log('Execution completed. Tokens used:', data.metadata.tokens_used);
    eventSource.close();
});

eventSource.addEventListener('error', (e) => {
    const data = JSON.parse(e.data);
    showError(data.error, data.message);
    eventSource.close();
});
```

### Python (Client)

```python
import httpx
from httpx_sse import connect_sse

async with httpx.AsyncClient() as client:
    async with connect_sse(
        client,
        'POST',
        'http://api.example.com/api/v1/cases/case-123/sessions/sess-456/execute',
        headers={
            'X-Organization-ID': 'org-789',
            'X-User-ID': 'user-101'
        },
        json={
            'user_message': 'What is causing the errors?',
            'agent_type': 'investigator',
            'stream': True
        }
    ) as event_source:
        async for event in event_source.aiter_sse():
            print(f"Event: {event.event}")
            print(f"Data: {event.data}")
```

---

## Testing Requirements

### 1. API Endpoint Tests

**File**: `tests/integration/api/test_agent_api.py`

**Coverage**: 90%+ for agent endpoint

**Test Categories**:

#### POST /api/v1/cases/{case_id}/sessions/{session_id}/execute (Streaming)
- [ ] 200 OK with SSE streaming
- [ ] Returns event: started
- [ ] Returns event: thinking
- [ ] Returns event: response (incremental chunks)
- [ ] Returns event: completed with execution_id and tokens_used
- [ ] Content-Type: text/event-stream
- [ ] Cache-Control: no-cache header
- [ ] Required headers (X-Organization-ID, X-User-ID)
- [ ] 422 Unprocessable Entity on missing headers
- [ ] 404 Not Found if session doesn't exist
- [ ] 403 Forbidden if wrong organization
- [ ] 409 Conflict if session not ACTIVE
- [ ] 409 Conflict if token budget exceeded
- [ ] Error events on LLM failure

#### POST /api/v1/cases/{case_id}/sessions/{session_id}/execute (Non-Streaming)
- [ ] 200 OK returns AgentExecutionResponse
- [ ] Returns complete execution with agent_response
- [ ] Returns tool_calls array
- [ ] Returns tokens_used
- [ ] Same error codes as streaming mode

#### Tool Call Events
- [ ] Returns event: tool_call when agent uses tool
- [ ] Returns event: tool_result after tool execution
- [ ] Tool metadata includes tool_name and arguments

#### Multi-Turn Conversation
- [ ] Second execution includes first execution in context
- [ ] Agent response references previous conversation

#### Authorization
- [ ] 403 Forbidden if organization_id doesn't match session's case
- [ ] Authorization checked before LLM call

**Expected Tests**: 30-40 tests

---

### 2. SSE Format Tests

**File**: `tests/unit/api/test_agent_api_streaming.py`

**Coverage**: 90%+

**Test Categories**:

#### SSE Event Format
- [ ] ExecutionEventSSE.to_sse() returns valid SSE format
- [ ] Event line starts with "event: "
- [ ] Data line starts with "data: "
- [ ] Ends with double newline "\n\n"
- [ ] Data is valid JSON

#### Event Conversion
- [ ] ExecutionEvent → ExecutionEventSSE conversion
- [ ] All event types supported (started, thinking, tool_call, response, completed)
- [ ] Metadata preserved
- [ ] Timestamp ISO format

#### Error Events
- [ ] NotFoundError → error event with error="not_found"
- [ ] AuthorizationError → error event with error="forbidden"
- [ ] ConflictError → error event with error="conflict"
- [ ] LLMException → error event with error="llm_error"
- [ ] Generic Exception → error event with error="internal_error"

**Expected Tests**: 15-20 tests

---

### 3. Integration Tests

**File**: `tests/integration/test_agent_api_integration.py`

**Coverage**: Critical workflows

**Test Categories**:

#### End-to-End Streaming
- [ ] **Complete workflow**:
  - [ ] Create case
  - [ ] Create session
  - [ ] POST execute endpoint with stream=true
  - [ ] Verify SSE events received in order
  - [ ] Verify started → thinking → response → completed
  - [ ] Verify execution record created
  - [ ] Verify tokens_used updated in session

#### Tool Call Streaming
- [ ] **Agent uses tool**:
  - [ ] Upload evidence file
  - [ ] POST execute asking about file
  - [ ] Verify tool_call event received
  - [ ] Verify tool_result event received
  - [ ] Verify agent response incorporates file data

#### Non-Streaming Mode
- [ ] **Complete workflow**:
  - [ ] POST execute with stream=false
  - [ ] Verify AgentExecutionResponse returned
  - [ ] Verify agent_response populated
  - [ ] Verify tool_calls array included

#### Error Handling
- [ ] **Session not found**:
  - [ ] POST execute with invalid session_id
  - [ ] Verify error event with error="not_found"

- [ ] **Session not active**:
  - [ ] Pause session
  - [ ] POST execute
  - [ ] Verify error event with error="conflict"

- [ ] **Token budget exceeded**:
  - [ ] Exceed token budget
  - [ ] POST execute
  - [ ] Verify error event with error="conflict"

**Expected Tests**: 15-20 tests

---

### 4. API Model Tests

**File**: `tests/unit/api/test_agent_models.py`

**Coverage**: 90%+

**Test Categories**:

#### AgentExecutionRequest
- [ ] Valid request with user_message
- [ ] agent_type defaults to INVESTIGATOR
- [ ] stream defaults to True
- [ ] ValidationError on empty user_message
- [ ] ValidationError on user_message > 10000 chars
- [ ] Valid agent_type enum values

#### AgentExecutionResponse
- [ ] from_domain() converts AgentExecution correctly
- [ ] Includes execution_id, status, agent_response
- [ ] Includes tokens_used, started_at, completed_at
- [ ] Includes tool_calls array

#### ToolCallResponse
- [ ] from_domain() converts AgentToolCall correctly
- [ ] Includes tool_call_id, tool_name, arguments, result, status

#### ExecutionEventSSE
- [ ] to_sse() returns valid SSE format
- [ ] Event and data lines formatted correctly

**Expected Tests**: 15-20 tests

---

## Expected Test Summary

| Category | Estimated Tests | Priority |
|----------|----------------|----------|
| API Endpoint | 30-40 | P0 |
| SSE Format | 15-20 | P0 |
| Integration | 15-20 | P0 |
| API Models | 15-20 | P0 |
| **TOTAL** | **~75-100 tests** | |

**Coverage Target**: 90%+

---

## Error Handling

### HTTP Status Codes

```python
# Success
200 OK - Agent execution streaming or completed (non-streaming)

# Client Errors
400 Bad Request - Invalid agent_type or parameters
403 Forbidden - Wrong organization
404 Not Found - Session not found
409 Conflict - Session not active or budget exceeded
422 Unprocessable Entity - Missing required headers or validation error

# Server Errors
500 Internal Server Error - LLM error or unexpected error
```

### Error Response Format (SSE)

```python
# SSE error events
event: error
data: {"error": "error_code", "message": "Human-readable message"}

# Error codes:
# - not_found: Session not found
# - forbidden: Wrong organization
# - conflict: Session not active or budget exceeded
# - validation_error: Invalid input
# - llm_error: LLM API error
# - internal_error: Unexpected error
```

---

## Configuration

**File**: `faultmaven/config/settings.py`

**New Settings**:

```python
# API Configuration
API_ENABLE_SSE: bool = True  # Enable Server-Sent Events
API_SSE_PING_INTERVAL: int = 30  # Send ping every 30s to keep connection alive
API_SSE_RETRY: int = 3000  # Client retry interval (ms)
```

---

## Dependencies

**External Libraries**:

```toml
[tool.poetry.dependencies]
sse-starlette = "^2.0.0"  # Server-Sent Events support for FastAPI
```

**Optional Client Libraries**:
```toml
# For Python clients
httpx = "^0.26.0"
httpx-sse = "^0.4.0"
```

---

## Deliverables

### Code Files
1. ✅ `faultmaven/api/routes/agent.py` - Agent execution endpoint (300-400 lines)
2. ✅ `faultmaven/api/models.py` - Request/response models (extend existing, 100-150 lines)
3. ✅ `faultmaven/services/service_factory.py` - Service factory extension (20-30 lines)
4. ✅ `faultmaven/api/app.py` - Router registration (10-20 lines)

### Test Files
1. ✅ `tests/integration/api/test_agent_api.py` (600-800 lines)
2. ✅ `tests/unit/api/test_agent_api_streaming.py` (300-400 lines)
3. ✅ `tests/integration/test_agent_api_integration.py` (400-600 lines)
4. ✅ `tests/unit/api/test_agent_models.py` (300-400 lines)

### Total Lines
- **Code**: ~450-620 lines
- **Tests**: ~1,600-2,200 lines
- **Total**: ~2,050-2,820 lines

---

## Success Criteria

**TASK-016 is complete when:**

1. ✅ **Agent execution endpoint** accepts POST requests with user_message
2. ✅ **Streaming mode** returns SSE events (started, thinking, tool_call, response, completed)
3. ✅ **Non-streaming mode** returns complete AgentExecutionResponse
4. ✅ **Tool call events** included in streaming (tool_call, tool_result)
5. ✅ **Error events** streamed for all error scenarios
6. ✅ **Authorization** enforced via organization_id header
7. ✅ **Token budget** tracking updates session after execution
8. ✅ **Multi-turn conversation** context preserved across executions
9. ✅ **HTTP status codes** correct (200, 404, 403, 409, 422, 500)
10. ✅ **75+ tests** with 90%+ coverage
11. ✅ **Integration tests** verify end-to-end streaming workflows
12. ✅ **OpenAPI docs** accessible at /api/docs with agent endpoint
13. ✅ **All tests pass** in CI/CD pipeline

---

## Notes

### SSE vs WebSocket

**SSE (Server-Sent Events)** chosen for TASK-016 because:
- ✅ Simpler implementation (HTTP-based)
- ✅ Native browser support (EventSource API)
- ✅ Automatic reconnection with last-event-id
- ✅ Works through proxies and firewalls
- ✅ One-way communication sufficient for agent execution

**WebSocket** (future TASK-018) offers:
- Bi-directional communication
- Lower latency
- Better for interactive sessions (human-in-the-loop)

### Future Enhancements (Out of Scope for TASK-016)

**TASK-017**: Authentication & Authorization
- JWT token validation
- RBAC (Role-Based Access Control)
- Replace header-based auth

**TASK-018**: WebSocket API
- Bi-directional streaming
- Interactive debugging sessions
- Human-in-the-loop confirmations

**TASK-019**: Agent Handoffs
- Multi-agent collaboration
- Agent type transitions (investigator → debugger)

**TASK-020**: Rate Limiting
- Per-user execution limits
- Organization-level quotas
- Token budget pooling

---

## OpenAPI Example

```yaml
/api/v1/cases/{case_id}/sessions/{session_id}/execute:
  post:
    summary: Execute AI agent for troubleshooting investigation
    description: |
      Execute an AI agent to analyze the case and generate recommendations.
      Supports streaming (SSE) or non-streaming mode.

      The agent will:
      - Analyze case context and previous conversation
      - Use available tools (read evidence, search knowledge)
      - Generate hypotheses and recommendations
      - Stream thinking process in real-time
    tags:
      - Agent Execution
    parameters:
      - name: case_id
        in: path
        required: true
        schema:
          type: string
      - name: session_id
        in: path
        required: true
        schema:
          type: string
      - name: X-Organization-ID
        in: header
        required: true
        schema:
          type: string
      - name: X-User-ID
        in: header
        required: true
        schema:
          type: string
    requestBody:
      required: true
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/AgentExecutionRequest'
          example:
            user_message: "What is causing the 500 errors in the API?"
            agent_type: "investigator"
            stream: true
    responses:
      200:
        description: Agent execution completed or streaming
        content:
          text/event-stream:
            schema:
              type: string
          application/json:
            schema:
              $ref: '#/components/schemas/AgentExecutionResponse'
      404:
        description: Session not found
      403:
        description: Forbidden - wrong organization
      409:
        description: Conflict - session not active or budget exceeded
      422:
        description: Validation error
```

---

## Approval Checklist

Before submitting PR:

- [ ] All 75+ tests written and passing
- [ ] Coverage 90%+
- [ ] SSE streaming works correctly
- [ ] Non-streaming mode works correctly
- [ ] Tool call events included in streaming
- [ ] Error events streamed for all error scenarios
- [ ] Authorization enforced
- [ ] Token budget tracking updates session
- [ ] Multi-turn conversation context preserved
- [ ] HTTP status codes correct
- [ ] Integration tests verify end-to-end workflows
- [ ] OpenAPI docs updated and accessible
- [ ] Code follows established patterns (TASK-014)
- [ ] No regressions in existing tests
- [ ] PR description includes test summary

---

**Created**: 2025-12-30
**Task**: TASK-016
**Status**: Ready for Development

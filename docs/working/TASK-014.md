# TASK-014: FastAPI REST API Controllers

**Phase:** Week 5, Day 1-3 (REST API Layer)
**Priority:** P1 (Public API foundation)
**Estimated Time:** 8-10 hours
**Dependencies:** TASK-011, TASK-012, TASK-013 (All service layers)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement FastAPI REST API controllers (routes) for case management, investigation sessions, and evidence artifacts. This creates the public HTTP API layer on top of the service layers built in TASK-011/012/013.

---

## Context

The API controllers translate HTTP requests into service layer calls, handle:
- Request/response models (Pydantic schemas)
- HTTP status codes and error responses
- OpenAPI documentation generation
- Request validation and parameter extraction
- Authentication/authorization middleware (future)

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. API Request/Response Models

**File:** `faultmaven/api/models.py`

Define Pydantic models for API requests and responses.

**Case Models:**
```python
class CaseCreateRequest(BaseModel):
    """Request model for creating a case."""
    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field(..., min_length=1)
    severity: CaseSeverity
    metadata: Optional[Dict[str, Any]] = None

class CaseUpdateRequest(BaseModel):
    """Request model for updating a case."""
    title: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = Field(None, min_length=1)
    severity: Optional[CaseSeverity] = None
    status: Optional[CaseStatus] = None
    assigned_to: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class CaseResponse(BaseModel):
    """Response model for a case."""
    case_id: str
    organization_id: str
    reporter_user_id: str
    title: str
    description: str
    severity: CaseSeverity
    status: CaseStatus
    assigned_to: Optional[str]
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime]
    resolution: Optional[str]
    metadata: Optional[Dict[str, Any]]

    class Config:
        from_attributes = True  # Allow ORM model conversion

class CaseListResponse(BaseModel):
    """Response model for case list."""
    items: List[CaseResponse]
    total: int
    limit: int
    offset: int
```

**Session Models:**
```python
class SessionCreateRequest(BaseModel):
    """Request model for creating investigation session."""
    session_goal: Optional[str] = None
    token_budget_limit: Optional[int] = Field(None, ge=0)
    metadata: Optional[Dict[str, Any]] = None

class SessionUpdateRequest(BaseModel):
    """Request model for updating session."""
    session_goal: Optional[str] = None
    token_budget_limit: Optional[int] = Field(None, ge=0)
    metadata: Optional[Dict[str, Any]] = None

class SessionResponse(BaseModel):
    """Response model for investigation session."""
    session_id: str
    case_id: str
    user_id: str
    organization_id: str
    status: SessionStatus
    started_at: datetime
    ended_at: Optional[datetime]
    last_activity_at: datetime
    total_duration_ms: Optional[int]
    session_goal: Optional[str]
    findings_summary: Optional[str]
    total_token_usage: int
    total_agent_executions: int
    token_budget_limit: Optional[int]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
```

**Evidence Models:**
```python
class EvidenceUploadRequest(BaseModel):
    """Request model for evidence upload (multipart form)."""
    evidence_type: EvidenceArtifactType
    description: Optional[str] = None
    is_primary: bool = False
    metadata: Optional[Dict[str, Any]] = None

class EvidenceUpdateRequest(BaseModel):
    """Request model for updating evidence."""
    description: Optional[str] = None
    is_primary: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

class EvidenceResponse(BaseModel):
    """Response model for evidence artifact."""
    evidence_id: str
    case_id: str
    user_id: str
    organization_id: str
    original_filename: str
    evidence_type: EvidenceArtifactType
    mime_type: str
    file_size: int
    description: Optional[str]
    is_primary: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
```

**Error Models:**
```python
class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None
    status_code: int
```

---

### 2. Case Management API

**File:** `faultmaven/api/routes/cases.py`

```python
router = APIRouter(prefix="/api/v1/cases", tags=["Cases"])

@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(
    request: CaseCreateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    user_id: str = Header(..., alias="X-User-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseResponse:
    """Create a new case.

    Headers:
        X-Organization-ID: Organization identifier
        X-User-ID: User identifier

    Returns:
        Created case
    """

@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseResponse:
    """Get case by ID.

    Raises:
        404: Case not found or not accessible
    """

@router.get("", response_model=CaseListResponse)
async def list_cases(
    organization_id: str = Header(..., alias="X-Organization-ID"),
    status: Optional[CaseStatus] = Query(None),
    severity: Optional[CaseSeverity] = Query(None),
    assigned_to: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseListResponse:
    """List cases for organization with filters."""

@router.patch("/{case_id}", response_model=CaseResponse)
async def update_case(
    case_id: str,
    request: CaseUpdateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseResponse:
    """Update case.

    Raises:
        404: Case not found
        403: Not authorized
    """

@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_case(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> None:
    """Delete case (CASCADE deletes sessions, executions, evidence)."""

@router.post("/{case_id}/assign", response_model=CaseResponse)
async def assign_case(
    case_id: str,
    assigned_to: str = Body(..., embed=True),
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseResponse:
    """Assign case to user."""

@router.post("/{case_id}/close", response_model=CaseResponse)
async def close_case(
    case_id: str,
    resolution: str = Body(..., embed=True),
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseResponse:
    """Close case with resolution."""

@router.post("/{case_id}/reopen", response_model=CaseResponse)
async def reopen_case(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> CaseResponse:
    """Reopen closed case."""

@router.get("/{case_id}/statistics")
async def get_case_statistics(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    case_service: APICaseService = Depends(get_case_service),
) -> Dict[str, Any]:
    """Get case-specific statistics."""
```

---

### 3. Investigation Session API

**File:** `faultmaven/api/routes/sessions.py`

```python
router = APIRouter(prefix="/api/v1/cases/{case_id}/sessions", tags=["Sessions"])

@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    case_id: str,
    request: SessionCreateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    user_id: str = Header(..., alias="X-User-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> SessionResponse:
    """Create investigation session for case."""

@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    case_id: str,
    session_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> SessionResponse:
    """Get session by ID."""

@router.get("", response_model=List[SessionResponse])
async def list_sessions(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    status: Optional[SessionStatus] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> List[SessionResponse]:
    """List sessions for case."""

@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    case_id: str,
    session_id: str,
    request: SessionUpdateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> SessionResponse:
    """Update session."""

@router.post("/{session_id}/pause", response_model=SessionResponse)
async def pause_session(
    case_id: str,
    session_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> SessionResponse:
    """Pause active session."""

@router.post("/{session_id}/resume", response_model=SessionResponse)
async def resume_session(
    case_id: str,
    session_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> SessionResponse:
    """Resume paused session."""

@router.post("/{session_id}/complete", response_model=SessionResponse)
async def complete_session(
    case_id: str,
    session_id: str,
    findings_summary: str = Body(..., embed=True),
    organization_id: str = Header(..., alias="X-Organization-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> SessionResponse:
    """Complete session with findings."""

@router.get("/active", response_model=Optional[SessionResponse])
async def get_active_session(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    session_service: APIInvestigationSessionService = Depends(get_investigation_session_service),
) -> Optional[SessionResponse]:
    """Get currently active session for case."""
```

---

### 4. Evidence Artifact API

**File:** `faultmaven/api/routes/evidence.py`

```python
router = APIRouter(prefix="/api/v1/cases/{case_id}/evidence", tags=["Evidence"])

@router.post("", response_model=EvidenceResponse, status_code=status.HTTP_201_CREATED)
async def upload_evidence(
    case_id: str,
    file: UploadFile = File(...),
    evidence_type: EvidenceArtifactType = Form(...),
    description: Optional[str] = Form(None),
    is_primary: bool = Form(False),
    organization_id: str = Header(..., alias="X-Organization-ID"),
    user_id: str = Header(..., alias="X-User-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Upload evidence artifact for case.

    Multipart form upload with file and metadata.
    """

@router.get("/{evidence_id}", response_model=EvidenceResponse)
async def get_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Get evidence metadata by ID."""

@router.get("/{evidence_id}/download")
async def download_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> StreamingResponse:
    """Download evidence file.

    Returns:
        Streaming response with file content
    """

@router.get("", response_model=List[EvidenceResponse])
async def list_evidence(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_type: Optional[EvidenceArtifactType] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> List[EvidenceResponse]:
    """List evidence for case."""

@router.patch("/{evidence_id}", response_model=EvidenceResponse)
async def update_evidence(
    case_id: str,
    evidence_id: str,
    request: EvidenceUpdateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Update evidence metadata."""

@router.delete("/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> None:
    """Delete evidence artifact and file."""

@router.post("/{evidence_id}/set-primary", response_model=EvidenceResponse)
async def set_primary_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Set artifact as primary evidence for case."""
```

---

### 5. Exception Handlers

**File:** `faultmaven/api/exception_handlers.py`

```python
async def not_found_exception_handler(
    request: Request,
    exc: NotFoundError
) -> JSONResponse:
    """Handle NotFoundError."""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "Not Found",
            "detail": str(exc),
            "status_code": 404,
        },
    )

async def authorization_exception_handler(
    request: Request,
    exc: AuthorizationError
) -> JSONResponse:
    """Handle AuthorizationError."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "error": "Forbidden",
            "detail": str(exc),
            "status_code": 403,
        },
    )

async def validation_exception_handler(
    request: Request,
    exc: ValidationException
) -> JSONResponse:
    """Handle ValidationException."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "Validation Error",
            "detail": str(exc),
            "status_code": 400,
        },
    )

async def conflict_exception_handler(
    request: Request,
    exc: ConflictError
) -> JSONResponse:
    """Handle ConflictError."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "Conflict",
            "detail": str(exc),
            "status_code": 409,
        },
    )

async def service_error_handler(
    request: Request,
    exc: ServiceError
) -> JSONResponse:
    """Handle ServiceError."""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal Server Error",
            "detail": "An unexpected error occurred",
            "status_code": 500,
        },
    )
```

---

### 6. Application Setup

**File:** `faultmaven/api/app.py`

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from faultmaven.api.routes import cases, sessions, evidence
from faultmaven.api.exception_handlers import (
    not_found_exception_handler,
    authorization_exception_handler,
    validation_exception_handler,
    conflict_exception_handler,
    service_error_handler,
)
from faultmaven.exceptions import (
    NotFoundError,
    AuthorizationError,
    ValidationException,
    ConflictError,
    ServiceError,
)

def create_app() -> FastAPI:
    """Create FastAPI application."""
    app = FastAPI(
        title="FaultMaven API",
        description="Evidence-centric troubleshooting platform",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure per environment
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register exception handlers
    app.add_exception_handler(NotFoundError, not_found_exception_handler)
    app.add_exception_handler(AuthorizationError, authorization_exception_handler)
    app.add_exception_handler(ValidationException, validation_exception_handler)
    app.add_exception_handler(ConflictError, conflict_exception_handler)
    app.add_exception_handler(ServiceError, service_error_handler)

    # Register routers
    app.include_router(cases.router)
    app.include_router(sessions.router)
    app.include_router(evidence.router)

    # Health check
    @app.get("/health")
    async def health_check():
        return {"status": "healthy"}

    return app

app = create_app()
```

---

## Testing Requirements

### 1. API Model Tests (20+ tests)

**File:** `tests/unit/api/test_models.py`

**Test Coverage:**
- ✅ CaseCreateRequest validation (required fields, constraints)
- ✅ CaseUpdateRequest optional fields
- ✅ CaseResponse serialization from domain model
- ✅ SessionCreateRequest validation
- ✅ SessionResponse serialization
- ✅ EvidenceResponse serialization
- ✅ Pydantic validation errors (min_length, ge constraints)

---

### 2. Case API Tests (40+ tests)

**File:** `tests/integration/api/test_cases_api.py`

**Test Coverage:**
- ✅ POST /api/v1/cases (201 Created)
- ✅ GET /api/v1/cases/{case_id} (200 OK)
- ✅ GET /api/v1/cases/{case_id} (404 Not Found)
- ✅ GET /api/v1/cases (200 OK, list)
- ✅ GET /api/v1/cases?status=OPEN (filter)
- ✅ PATCH /api/v1/cases/{case_id} (200 OK)
- ✅ PATCH /api/v1/cases/{case_id} (403 Forbidden, wrong org)
- ✅ DELETE /api/v1/cases/{case_id} (204 No Content)
- ✅ POST /api/v1/cases/{case_id}/assign (200 OK)
- ✅ POST /api/v1/cases/{case_id}/close (200 OK)
- ✅ POST /api/v1/cases/{case_id}/reopen (200 OK)
- ✅ Missing headers (X-Organization-ID, X-User-ID) returns 422

---

### 3. Session API Tests (35+ tests)

**File:** `tests/integration/api/test_sessions_api.py`

**Test Coverage:**
- ✅ POST /api/v1/cases/{case_id}/sessions (201 Created)
- ✅ GET /api/v1/cases/{case_id}/sessions/{session_id} (200 OK)
- ✅ GET /api/v1/cases/{case_id}/sessions (200 OK, list)
- ✅ PATCH /api/v1/cases/{case_id}/sessions/{session_id} (200 OK)
- ✅ POST /api/v1/cases/{case_id}/sessions/{session_id}/pause (200 OK)
- ✅ POST /api/v1/cases/{case_id}/sessions/{session_id}/resume (200 OK)
- ✅ POST /api/v1/cases/{case_id}/sessions/{session_id}/complete (200 OK)
- ✅ GET /api/v1/cases/{case_id}/sessions/active (200 OK)
- ✅ Authorization errors (403 Forbidden)

---

### 4. Evidence API Tests (40+ tests)

**File:** `tests/integration/api/test_evidence_api.py`

**Test Coverage:**
- ✅ POST /api/v1/cases/{case_id}/evidence (201 Created, multipart upload)
- ✅ GET /api/v1/cases/{case_id}/evidence/{evidence_id} (200 OK)
- ✅ GET /api/v1/cases/{case_id}/evidence/{evidence_id}/download (200 OK, file stream)
- ✅ GET /api/v1/cases/{case_id}/evidence (200 OK, list)
- ✅ PATCH /api/v1/cases/{case_id}/evidence/{evidence_id} (200 OK)
- ✅ DELETE /api/v1/cases/{case_id}/evidence/{evidence_id} (204 No Content)
- ✅ POST /api/v1/cases/{case_id}/evidence/{evidence_id}/set-primary (200 OK)
- ✅ File upload with actual binary data
- ✅ Download returns correct content-type and filename
- ✅ Authorization errors (403 Forbidden)

---

### 5. Exception Handler Tests (15+ tests)

**File:** `tests/unit/api/test_exception_handlers.py`

**Test Coverage:**
- ✅ NotFoundError → 404 JSON response
- ✅ AuthorizationError → 403 JSON response
- ✅ ValidationException → 400 JSON response
- ✅ ConflictError → 409 JSON response
- ✅ ServiceError → 500 JSON response
- ✅ Error response format (error, detail, status_code fields)

---

## Acceptance Criteria

- ✅ API request/response models (Pydantic)
- ✅ Case management API (9 endpoints)
- ✅ Investigation session API (8 endpoints)
- ✅ Evidence artifact API (7 endpoints)
- ✅ Exception handlers for all service exceptions
- ✅ FastAPI app with CORS, OpenAPI docs
- ✅ 150+ tests (20 models + 40 cases + 35 sessions + 40 evidence + 15 handlers)
- ✅ OpenAPI documentation auto-generated
- ✅ All tests pass

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] All tests passing (unit + integration)
- [ ] Test coverage ≥80%
- [ ] OpenAPI docs accessible at /api/docs
- [ ] Code follows established patterns
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests

---

## Notes

**Header-Based Auth (Temporary):**
- Use `X-Organization-ID` and `X-User-ID` headers
- Real authentication/JWT will be added later
- This allows testing without auth infrastructure

**OpenAPI Documentation:**
- Auto-generated from route decorators and Pydantic models
- Access at http://localhost:8000/api/docs (Swagger UI)
- Access at http://localhost:8000/api/redoc (ReDoc)

**Error Response Format:**
```json
{
  "error": "Not Found",
  "detail": "Case not found: CASE-123",
  "status_code": 404
}
```

**File Upload Pattern:**
```python
# Client uploads with multipart/form-data
curl -X POST "http://localhost:8000/api/v1/cases/CASE-123/evidence" \
  -H "X-Organization-ID: org-456" \
  -H "X-User-ID: user-789" \
  -F "file=@screenshot.png" \
  -F "evidence_type=screenshot" \
  -F "description=Login error screenshot"
```

**File Download Pattern:**
```python
# Server streams file with proper content-type
response = await client.get("/api/v1/cases/CASE-123/evidence/EVD-456/download")
assert response.status_code == 200
assert response.headers["content-type"] == "image/png"
assert response.headers["content-disposition"] == 'attachment; filename="screenshot.png"'
```

**CORS Configuration:**
- Currently allow all origins (`allow_origins=["*"]`)
- In production: Restrict to specific domains
- Configure per environment via settings

**Evolution Path:**
```
TASK-011: Case Service ✅
TASK-012: Session Service ✅
TASK-013: Evidence Service ✅
TASK-014: FastAPI Controllers ← Current
TASK-015: Agent Orchestration Service
TASK-016: Authentication & Authorization (JWT, RBAC)
```

# TASK-011: API Service Layer (Case Management)

**Phase:** Week 4, Day 1-3 (API Layer Evolution)
**Priority:** P1 (API foundation)
**Estimated Time:** 8-10 hours
**Dependencies:** TASK-010 (Vector Search Integration)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement the service layer for case management API, providing business logic orchestration between FastAPI controllers and domain repositories. This establishes the pattern for all API services.

---

## Context

The service layer sits between API controllers (FastAPI routes) and repositories, handling:
- Business logic and validation
- Transaction coordination across multiple repositories
- Error translation (domain errors → HTTP responses)
- Authorization checks (organization ownership)
- Audit logging

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. Base Service Class

**File:** `faultmaven/services/base.py`

```python
class BaseService:
    """Base class for all services.

    Provides common functionality:
    - Logging with service name prefix
    - Error handling utilities
    - Transaction context helpers
    """

    def __init__(self, service_name: str):
        """Initialize base service.

        Args:
            service_name: Name for logging (e.g., "case_service")
        """
        self.service_name = service_name
        self.logger = logging.getLogger(f"faultmaven.services.{service_name}")

    def log_operation(self, operation: str, **kwargs) -> None:
        """Log service operation with context."""
        self.logger.info(
            f"{operation}",
            extra={"service": self.service_name, **kwargs}
        )

    def log_error(self, operation: str, error: Exception, **kwargs) -> None:
        """Log service error with context."""
        self.logger.error(
            f"{operation} failed: {error}",
            extra={"service": self.service_name, "error_type": type(error).__name__, **kwargs},
            exc_info=True
        )
```

---

### 2. Case Service

**File:** `faultmaven/services/case_service.py`

```python
class CaseService(BaseService):
    """Service for case management operations."""

    def __init__(
        self,
        case_repo: CaseRepository,
        session_repo: InvestigationSessionRepository,
        evidence_repo: EvidenceArtifactRepository,
        execution_repo: AgentExecutionRepository,
    ):
        """Initialize case service.

        Args:
            case_repo: Case repository
            session_repo: Investigation session repository
            evidence_repo: Evidence artifact repository
            execution_repo: Agent execution repository
        """
        super().__init__("case_service")
        self.case_repo = case_repo
        self.session_repo = session_repo
        self.evidence_repo = evidence_repo
        self.execution_repo = execution_repo
```

**Core Methods:**

```python
async def create_case(
    self,
    user_id: str,
    organization_id: str,
    title: str,
    description: str,
    severity: CaseSeverity,
    metadata: Optional[Dict[str, Any]] = None
) -> Case:
    """Create a new case.

    Args:
        user_id: User creating the case
        organization_id: Organization owning the case
        title: Case title
        description: Case description
        severity: Case severity
        metadata: Optional metadata

    Returns:
        Created Case

    Raises:
        ValidationError: If inputs invalid
        ServiceError: If creation fails
    """

async def get_case(
    self,
    case_id: str,
    organization_id: str
) -> Optional[Case]:
    """Get case by ID with organization check.

    Args:
        case_id: Case ID to retrieve
        organization_id: Organization for authorization check

    Returns:
        Case if found and authorized, None otherwise
    """

async def update_case(
    self,
    case_id: str,
    organization_id: str,
    updates: Dict[str, Any]
) -> Case:
    """Update case with authorization check.

    Args:
        case_id: Case ID to update
        organization_id: Organization for authorization check
        updates: Fields to update (title, description, severity, status, etc.)

    Returns:
        Updated Case

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
        ValidationError: If updates invalid
    """

async def delete_case(
    self,
    case_id: str,
    organization_id: str
) -> bool:
    """Delete case with authorization check.

    This triggers CASCADE delete:
    - All investigation sessions
    - All agent executions
    - All tool calls
    - All evidence artifacts

    Args:
        case_id: Case ID to delete
        organization_id: Organization for authorization check

    Returns:
        True if deleted, False if not found

    Raises:
        AuthorizationError: If organization doesn't own case
    """

async def list_cases(
    self,
    organization_id: str,
    user_id: Optional[str] = None,
    status: Optional[CaseStatus] = None,
    severity: Optional[CaseSeverity] = None,
    assigned_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Case]:
    """List cases for organization with filters.

    Args:
        organization_id: Organization to list cases for
        user_id: Optional filter by reporter
        status: Optional filter by status
        severity: Optional filter by severity
        assigned_to: Optional filter by assignee
        limit: Max results
        offset: Pagination offset

    Returns:
        List of cases
    """

async def get_case_with_details(
    self,
    case_id: str,
    organization_id: str,
    include_sessions: bool = True,
    include_evidence: bool = True,
    include_executions: bool = False
) -> Optional[Dict[str, Any]]:
    """Get case with related entities.

    Args:
        case_id: Case ID
        organization_id: Organization for authorization
        include_sessions: Include investigation sessions
        include_evidence: Include evidence artifacts
        include_executions: Include agent executions

    Returns:
        Dictionary with case and related entities
    """

async def assign_case(
    self,
    case_id: str,
    organization_id: str,
    assigned_to: str
) -> Case:
    """Assign case to user.

    Args:
        case_id: Case ID
        organization_id: Organization for authorization
        assigned_to: User ID to assign to

    Returns:
        Updated case

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
    """

async def close_case(
    self,
    case_id: str,
    organization_id: str,
    resolution: str
) -> Case:
    """Close case with resolution.

    Args:
        case_id: Case ID
        organization_id: Organization for authorization
        resolution: Resolution description

    Returns:
        Updated case with status=CLOSED

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
        ValidationError: If case already closed
    """

async def reopen_case(
    self,
    case_id: str,
    organization_id: str
) -> Case:
    """Reopen a closed case.

    Args:
        case_id: Case ID
        organization_id: Organization for authorization

    Returns:
        Updated case with status=OPEN

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
        ValidationError: If case not closed
    """

async def get_case_statistics(
    self,
    organization_id: str
) -> Dict[str, Any]:
    """Get case statistics for organization.

    Returns:
        Statistics including:
        - total_cases
        - by_status (open, in_progress, closed, etc.)
        - by_severity (low, medium, high, critical)
        - avg_resolution_time
        - unassigned_count
    """
```

---

### 3. Service Exceptions

**File:** `faultmaven/exceptions.py`

Add service-level exceptions:

```python
class ServiceError(Exception):
    """Base exception for service layer errors."""
    pass

class NotFoundError(ServiceError):
    """Resource not found."""
    def __init__(self, resource_type: str, resource_id: str):
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(f"{resource_type} not found: {resource_id}")

class AuthorizationError(ServiceError):
    """Authorization check failed."""
    def __init__(self, message: str = "Not authorized"):
        super().__init__(message)

class ValidationError(ServiceError):
    """Input validation failed."""
    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"{field}: {message}")

class ConflictError(ServiceError):
    """Resource conflict (duplicate, state violation, etc.)."""
    pass
```

---

### 4. Service Factory

**File:** `faultmaven/services/service_factory.py`

```python
class ServiceFactory:
    """Factory for creating service instances with proper dependencies."""

    def __init__(self, db_session: AsyncSession):
        """Initialize service factory.

        Args:
            db_session: Database session for repositories
        """
        self.db_session = db_session

        # Create repositories
        self.case_repo = create_case_repository(db_session)
        self.session_repo = create_investigation_session_repository(db_session)
        self.evidence_repo = create_evidence_artifact_repository(db_session)
        self.execution_repo = create_agent_execution_repository(db_session)
        self.knowledge_repo = create_knowledge_item_repository(db_session)

    def create_case_service(self) -> CaseService:
        """Create case service with dependencies."""
        return CaseService(
            case_repo=self.case_repo,
            session_repo=self.session_repo,
            evidence_repo=self.evidence_repo,
            execution_repo=self.execution_repo,
        )

    # Future: create_session_service(), create_evidence_service(), etc.
```

---

### 5. FastAPI Dependency

**File:** `faultmaven/api/dependencies.py`

```python
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for request."""
    async with get_async_session() as session:
        yield session

async def get_service_factory(
    db_session: AsyncSession = Depends(get_db_session)
) -> ServiceFactory:
    """Get service factory for request."""
    return ServiceFactory(db_session)

async def get_case_service(
    factory: ServiceFactory = Depends(get_service_factory)
) -> CaseService:
    """Get case service for request."""
    return factory.create_case_service()
```

---

## Testing Requirements

### 1. Base Service Tests (10+ tests)

**File:** `tests/unit/services/test_base_service.py`

**Test Coverage:**
- ✅ Service initialization with name
- ✅ Logger created with correct name
- ✅ `log_operation()` logs with context
- ✅ `log_error()` logs with exception info
- ✅ Log extra fields included

---

### 2. Case Service Tests (50+ tests)

**File:** `tests/unit/services/test_case_service.py`

**Test Coverage:**

**Create Case:**
- ✅ `create_case()` success
- ✅ Returns Case with correct fields
- ✅ Case ID generated (UUID format)
- ✅ Timestamps set correctly
- ✅ Validation error on empty title
- ✅ Validation error on invalid severity

**Get Case:**
- ✅ `get_case()` success with authorization check
- ✅ Returns None if case not found
- ✅ Returns None if wrong organization (authorization)
- ✅ Correct organization returns case

**Update Case:**
- ✅ `update_case()` success
- ✅ Updates specific fields (title, description, severity)
- ✅ Authorization check (organization ownership)
- ✅ NotFoundError if case doesn't exist
- ✅ AuthorizationError if wrong organization
- ✅ ValidationError on invalid updates

**Delete Case:**
- ✅ `delete_case()` success
- ✅ Returns True if deleted
- ✅ Returns False if not found
- ✅ Authorization check (organization ownership)
- ✅ AuthorizationError if wrong organization

**List Cases:**
- ✅ `list_cases()` returns all cases for org
- ✅ Filter by user_id
- ✅ Filter by status
- ✅ Filter by severity
- ✅ Filter by assigned_to
- ✅ Pagination (limit/offset)
- ✅ Organization isolation (no cross-org leaks)

**Get Case with Details:**
- ✅ `get_case_with_details()` includes sessions
- ✅ Includes evidence artifacts
- ✅ Includes agent executions
- ✅ Selective inclusion (flags control what's included)
- ✅ Authorization check

**Assign Case:**
- ✅ `assign_case()` updates assigned_to
- ✅ Authorization check
- ✅ NotFoundError if case doesn't exist

**Close Case:**
- ✅ `close_case()` sets status to CLOSED
- ✅ Sets resolution field
- ✅ Sets closed_at timestamp
- ✅ ValidationError if already closed
- ✅ Authorization check

**Reopen Case:**
- ✅ `reopen_case()` sets status to OPEN
- ✅ Clears closed_at timestamp
- ✅ ValidationError if not closed
- ✅ Authorization check

**Get Statistics:**
- ✅ `get_case_statistics()` returns correct counts
- ✅ By status breakdown
- ✅ By severity breakdown
- ✅ Average resolution time calculation
- ✅ Unassigned count

---

### 3. Integration Tests (30+ tests)

**File:** `tests/integration/test_case_service_integration.py`

**Critical Tests:**

**End-to-End Case Lifecycle:**
```python
async def test_case_lifecycle():
    """Test complete case lifecycle."""
    # Create case
    # Assign case
    # Add evidence
    # Create investigation session
    # Close case with resolution
    # Verify all state transitions
```

**Authorization Enforcement:**
```python
async def test_authorization_prevents_cross_org_access():
    """Test organization-level authorization."""
    # Create case for org A
    # Attempt to access with org B
    # Verify AuthorizationError raised
    # Attempt to update with org B
    # Verify AuthorizationError raised
```

**CASCADE Delete Verification:**
```python
async def test_delete_case_cascades_to_related_entities():
    """Test CASCADE delete chain."""
    # Create case
    # Add evidence artifacts
    # Add investigation sessions
    # Add agent executions
    # Delete case
    # Verify all related entities deleted
```

**Transaction Rollback:**
```python
async def test_transaction_rollback_on_error():
    """Test transaction rollback on failure."""
    # Begin transaction
    # Create case
    # Trigger error (e.g., invalid update)
    # Verify case not persisted (rollback)
```

---

### 4. Service Factory Tests (15+ tests)

**File:** `tests/unit/services/test_service_factory.py`

**Test Coverage:**
- ✅ Factory initialization with db_session
- ✅ Creates all repositories
- ✅ `create_case_service()` returns CaseService
- ✅ Service has correct dependencies injected
- ✅ Multiple service creation (singleton vs new instance)

---

### 5. Performance Benchmarks (8+ benchmarks)

**File:** `tests/benchmarks/test_case_service_operations.py`

**Benchmarks:**
- Create case (target: <200ms p95)
- Get case (target: <100ms p95)
- Update case (target: <150ms p95)
- List cases (100 cases, target: <300ms p95)
- Get case with details (target: <250ms p95)
- Get statistics (1000 cases, target: <500ms p95)

---

## Acceptance Criteria

- ✅ BaseService implemented with logging utilities
- ✅ CaseService implemented with 12+ methods
- ✅ Service exceptions defined (NotFoundError, AuthorizationError, ValidationError, ConflictError)
- ✅ ServiceFactory for dependency injection
- ✅ FastAPI dependencies for service injection
- ✅ 110+ tests (10 base + 50 case service + 30 integration + 15 factory + 8 benchmarks)
- ✅ 80%+ test coverage
- ✅ All tests pass
- ✅ Performance benchmarks meet targets

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] All tests passing (unit + integration + benchmarks)
- [ ] Test coverage ≥80%
- [ ] Code follows established patterns
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests
- [ ] Authorization checks verified in all methods

---

## Notes

**Service Layer Principles:**
1. **Organization-level authorization** on all operations
2. **Transaction management** for multi-repository operations
3. **Domain error translation** to service exceptions
4. **Logging** for all operations and errors
5. **No direct database access** (use repositories only)

**Error Handling Pattern:**
```python
try:
    case = await self.case_repo.get_by_id(case_id)
    if not case:
        raise NotFoundError("Case", case_id)

    if case.organization_id != organization_id:
        raise AuthorizationError(f"Case {case_id} not accessible")

    return case
except RepositoryError as e:
    self.log_error("get_case", e, case_id=case_id)
    raise ServiceError(f"Failed to get case: {e}")
```

**No Database Migration:**
- Service layer is pure Python logic
- No schema changes required
- Uses existing repositories and domain models

**Evolution Path:**
```
TASK-011: Case Service (foundation) ← Current
TASK-012: Session Service (investigation workflow)
TASK-013: Evidence Service (file management)
TASK-014: FastAPI Controllers (REST API)
```

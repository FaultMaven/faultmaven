# TASK-012: API Service Layer (Investigation Session Management)

**Phase:** Week 4, Day 4-5 (API Layer Evolution)
**Priority:** P1 (Investigation workflow API)
**Estimated Time:** 6-8 hours
**Dependencies:** TASK-011 (Case Service)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement the service layer for investigation session management API, providing business logic for session lifecycle, token budget management, and agent execution coordination within sessions.

---

## Context

The Session Service builds on the Case Service pattern, adding workflow management for investigation sessions. Sessions are the temporal containers for investigation work, tracking agent executions, token usage, and findings.

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. Investigation Session Service

**File:** `faultmaven/services/investigation_session_service.py`

```python
class APIInvestigationSessionService(BaseService):
    """Service for API investigation session management operations."""

    def __init__(
        self,
        session_repo: InvestigationSessionRepository,
        execution_repo: AgentExecutionRepository,
        case_repo: CaseRepository,
    ):
        """Initialize API investigation session service.

        Args:
            session_repo: Investigation session repository
            execution_repo: Agent execution repository
            case_repo: Case repository (for authorization)
        """
        super().__init__("api_investigation_session_service")
        self.session_repo = session_repo
        self.execution_repo = execution_repo
        self.case_repo = case_repo
```

**Core Methods:**

```python
async def create_session(
    self,
    case_id: str,
    organization_id: str,
    user_id: str,
    session_goal: Optional[str] = None,
    token_budget_limit: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> InvestigationSession:
    """Create a new investigation session.

    Workflow:
    1. Verify case exists and belongs to organization
    2. Check for existing active session (warn if exists)
    3. Generate session_id
    4. Create session with status=ACTIVE
    5. Return created session

    Args:
        case_id: Case to create session for
        organization_id: Organization for authorization
        user_id: User creating the session
        session_goal: Optional goal description
        token_budget_limit: Optional token spending limit
        metadata: Optional metadata

    Returns:
        Created InvestigationSession

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
        ConflictError: If active session already exists for case
        ValidationException: If inputs invalid
    """

async def get_session(
    self,
    session_id: str,
    organization_id: str
) -> Optional[InvestigationSession]:
    """Get session by ID with authorization check.

    Verifies organization owns the parent case.

    Args:
        session_id: Session ID to retrieve
        organization_id: Organization for authorization

    Returns:
        Session if found and authorized, None otherwise
    """

async def update_session(
    self,
    session_id: str,
    organization_id: str,
    updates: Dict[str, Any]
) -> InvestigationSession:
    """Update session with authorization check.

    Allowed updates:
    - session_goal
    - token_budget_limit
    - metadata

    Args:
        session_id: Session ID to update
        organization_id: Organization for authorization
        updates: Fields to update

    Returns:
        Updated InvestigationSession

    Raises:
        NotFoundError: If session not found
        AuthorizationError: If organization doesn't own case
        ValidationException: If updates invalid
    """

async def pause_session(
    self,
    session_id: str,
    organization_id: str
) -> InvestigationSession:
    """Pause an active session.

    Args:
        session_id: Session ID to pause
        organization_id: Organization for authorization

    Returns:
        Updated session with status=PAUSED

    Raises:
        NotFoundError: If session not found
        AuthorizationError: If organization doesn't own case
        ValidationException: If session not active
    """

async def resume_session(
    self,
    session_id: str,
    organization_id: str
) -> InvestigationSession:
    """Resume a paused session.

    Args:
        session_id: Session ID to resume
        organization_id: Organization for authorization

    Returns:
        Updated session with status=ACTIVE

    Raises:
        NotFoundError: If session not found
        AuthorizationError: If organization doesn't own case
        ValidationException: If session not paused
    """

async def complete_session(
    self,
    session_id: str,
    organization_id: str,
    findings_summary: str
) -> InvestigationSession:
    """Complete a session with findings.

    Workflow:
    1. Verify authorization
    2. Mark session as completed
    3. Set findings_summary
    4. Calculate total_duration_ms
    5. Set ended_at timestamp
    6. Return updated session

    Args:
        session_id: Session ID to complete
        organization_id: Organization for authorization
        findings_summary: Summary of investigation findings

    Returns:
        Updated session with status=COMPLETED

    Raises:
        NotFoundError: If session not found
        AuthorizationError: If organization doesn't own case
        ValidationException: If session already completed/abandoned
    """

async def abandon_session(
    self,
    session_id: str,
    organization_id: str
) -> InvestigationSession:
    """Abandon a session without findings.

    Args:
        session_id: Session ID to abandon
        organization_id: Organization for authorization

    Returns:
        Updated session with status=ABANDONED

    Raises:
        NotFoundError: If session not found
        AuthorizationError: If organization doesn't own case
        ValidationException: If session already completed
    """

async def get_active_session(
    self,
    case_id: str,
    organization_id: str
) -> Optional[InvestigationSession]:
    """Get the currently active session for a case.

    Args:
        case_id: Case ID to get active session for
        organization_id: Organization for authorization

    Returns:
        Active session if found, None otherwise

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
    """

async def list_sessions(
    self,
    case_id: str,
    organization_id: str,
    status: Optional[SessionStatus] = None,
    limit: int = 50,
    offset: int = 0
) -> List[InvestigationSession]:
    """List sessions for a case with filters.

    Args:
        case_id: Case ID to list sessions for
        organization_id: Organization for authorization
        status: Optional filter by status
        limit: Max results
        offset: Pagination offset

    Returns:
        List of sessions

    Raises:
        NotFoundError: If case not found
        AuthorizationError: If organization doesn't own case
    """

async def get_session_with_executions(
    self,
    session_id: str,
    organization_id: str,
    include_tool_calls: bool = False
) -> Optional[Dict[str, Any]]:
    """Get session with related agent executions.

    Args:
        session_id: Session ID
        organization_id: Organization for authorization
        include_tool_calls: Include tool calls for each execution

    Returns:
        Dictionary with session and executions, or None if not found/authorized
    """

async def add_execution_to_session(
    self,
    session_id: str,
    organization_id: str,
    execution_id: str,
    token_usage: int
) -> InvestigationSession:
    """Link an agent execution to a session and update token tracking.

    Workflow:
    1. Verify session authorization
    2. Verify execution exists
    3. Update execution.session_id
    4. Call session.add_agent_execution(token_usage)
    5. Update session in repository
    6. Return updated session

    Args:
        session_id: Session ID
        organization_id: Organization for authorization
        execution_id: Agent execution ID to link
        token_usage: Token usage for this execution

    Returns:
        Updated session with incremented counts

    Raises:
        NotFoundError: If session or execution not found
        AuthorizationError: If organization doesn't own case
        ValidationException: If session not active
    """

async def check_budget_exceeded(
    self,
    session_id: str,
    organization_id: str
) -> Dict[str, Any]:
    """Check if session has exceeded token budget.

    Args:
        session_id: Session ID
        organization_id: Organization for authorization

    Returns:
        Dictionary with:
        - is_over_budget: bool
        - total_token_usage: int
        - token_budget_limit: int or None
        - usage_percentage: float or None

    Raises:
        NotFoundError: If session not found
        AuthorizationError: If organization doesn't own case
    """

async def get_session_statistics(
    self,
    case_id: str,
    organization_id: str
) -> Dict[str, Any]:
    """Get session statistics for a case.

    Returns:
        Statistics including:
        - total_sessions
        - by_status (active, paused, completed, abandoned)
        - total_token_usage_all_sessions
        - total_agent_executions_all_sessions
        - avg_session_duration_ms
    """
```

---

### 2. Service Factory Extension

**File:** `faultmaven/services/service_factory.py`

Add factory method:

```python
def create_investigation_session_service(self) -> APIInvestigationSessionService:
    """Create investigation session service with dependencies."""
    return APIInvestigationSessionService(
        session_repo=self.session_repo,
        execution_repo=self.execution_repo,
        case_repo=self.case_repo,
    )
```

---

### 3. FastAPI Dependency

**File:** `faultmaven/api/dependencies.py`

Add dependency:

```python
async def get_investigation_session_service(
    factory: ServiceFactory = Depends(get_service_factory)
) -> APIInvestigationSessionService:
    """Get investigation session service for request."""
    return factory.create_investigation_session_service()
```

---

## Testing Requirements

### 1. Session Service Tests (60+ tests)

**File:** `tests/unit/services/test_api_investigation_session_service.py`

**Test Coverage:**

**Create Session:**
- ✅ `create_session()` success
- ✅ Session ID generated (UUID format)
- ✅ Status set to ACTIVE
- ✅ Timestamps set correctly
- ✅ NotFoundError if case doesn't exist
- ✅ AuthorizationError if wrong organization
- ✅ ConflictError if active session already exists
- ✅ ValidationException on invalid inputs

**Get Session:**
- ✅ `get_session()` success with authorization
- ✅ Returns None if not found
- ✅ Returns None if wrong organization
- ✅ Authorization via parent case check

**Update Session:**
- ✅ `update_session()` updates fields
- ✅ Authorization check
- ✅ NotFoundError if not found
- ✅ ValidationException on invalid updates

**Pause/Resume Session:**
- ✅ `pause_session()` sets status to PAUSED
- ✅ ValidationException if not active
- ✅ `resume_session()` sets status to ACTIVE
- ✅ ValidationException if not paused
- ✅ Authorization checks

**Complete Session:**
- ✅ `complete_session()` sets status to COMPLETED
- ✅ Sets findings_summary
- ✅ Sets ended_at timestamp
- ✅ Calculates total_duration_ms
- ✅ ValidationException if already completed/abandoned
- ✅ Authorization check

**Abandon Session:**
- ✅ `abandon_session()` sets status to ABANDONED
- ✅ ValidationException if already completed
- ✅ Authorization check

**Get Active Session:**
- ✅ `get_active_session()` returns active session
- ✅ Returns None if no active session
- ✅ Authorization via case check
- ✅ NotFoundError if case doesn't exist

**List Sessions:**
- ✅ `list_sessions()` returns all sessions for case
- ✅ Filter by status
- ✅ Pagination (limit/offset)
- ✅ Authorization check
- ✅ Empty results handled

**Get Session with Executions:**
- ✅ `get_session_with_executions()` includes executions
- ✅ Optionally includes tool calls
- ✅ Authorization check
- ✅ Returns None if not found/unauthorized

**Add Execution to Session:**
- ✅ `add_execution_to_session()` links execution
- ✅ Updates execution.session_id
- ✅ Increments total_token_usage
- ✅ Increments total_agent_executions
- ✅ NotFoundError if session or execution not found
- ✅ ValidationException if session not active
- ✅ Authorization check

**Check Budget Exceeded:**
- ✅ `check_budget_exceeded()` returns correct status
- ✅ Calculates usage_percentage correctly
- ✅ Handles None budget (no limit)
- ✅ Authorization check

**Get Statistics:**
- ✅ `get_session_statistics()` returns correct counts
- ✅ By status breakdown
- ✅ Total token usage across all sessions
- ✅ Avg session duration calculation
- ✅ Authorization check

---

### 2. Integration Tests (35+ tests)

**File:** `tests/integration/test_investigation_session_service_integration.py`

**Critical Tests:**

**End-to-End Session Lifecycle:**
```python
async def test_session_full_lifecycle():
    """Test complete session lifecycle."""
    # Create case
    # Create session
    # Add agent executions to session
    # Pause session
    # Resume session
    # Complete session with findings
    # Verify all state transitions persisted
    # Verify token tracking correct
```

**Authorization Enforcement:**
```python
async def test_authorization_via_parent_case():
    """Test authorization checks via parent case."""
    # Create case for org A
    # Create session for case
    # Attempt to access session with org B
    # Verify AuthorizationError or None returned
```

**Active Session Enforcement:**
```python
async def test_only_one_active_session_per_case():
    """Test single active session per case enforcement."""
    # Create case
    # Create active session
    # Attempt to create another active session
    # Verify ConflictError raised
    # Complete first session
    # Create new active session (should succeed)
```

**Token Budget Tracking:**
```python
async def test_token_budget_tracking():
    """Test token usage and budget enforcement."""
    # Create session with budget limit
    # Add executions with token usage
    # Verify total_token_usage incremented
    # Check budget exceeded when over limit
    # Verify warning when approaching limit
```

**Execution Linking:**
```python
async def test_link_executions_to_session():
    """Test linking agent executions to session."""
    # Create session
    # Create agent executions
    # Link executions to session
    # Verify session.total_agent_executions incremented
    # Verify execution.session_id set
    # Get session with executions
    # Verify executions included
```

**SET NULL on Session Delete:**
```python
async def test_session_delete_preserves_executions():
    """Test SET NULL behavior when session deleted."""
    # Create session
    # Create executions linked to session
    # Delete session
    # Verify executions still exist
    # Verify execution.session_id is NULL
```

**Statistics Accuracy:**
```python
async def test_session_statistics_calculation():
    """Test session statistics calculation."""
    # Create multiple sessions with different statuses
    # Add executions to sessions
    # Complete some sessions
    # Get statistics
    # Verify counts, token totals, avg duration correct
```

---

### 3. Service Factory Tests (5+ tests)

**File:** `tests/unit/services/test_service_factory.py` (extend existing)

**Test Coverage:**
- ✅ `create_investigation_session_service()` returns service
- ✅ Service has correct dependencies
- ✅ session_repo, execution_repo, case_repo not None

---

### 4. Performance Benchmarks (10+ benchmarks)

**File:** `tests/benchmarks/test_investigation_session_service_operations.py`

**Benchmarks:**
- Create session (target: <200ms p95)
- Get session (target: <100ms p95)
- Update session (target: <150ms p95)
- Pause/resume session (target: <150ms p95)
- Complete session (target: <150ms p95)
- List sessions (50 sessions, target: <300ms p95)
- Get session with executions (10 executions, target: <250ms p95)
- Add execution to session (target: <150ms p95)
- Check budget exceeded (target: <100ms p95)
- Get statistics (100 sessions, target: <500ms p95)

---

## Acceptance Criteria

- ✅ APIInvestigationSessionService implemented with 14+ methods
- ✅ Service factory extended with session service creation
- ✅ FastAPI dependency added
- ✅ 110+ tests (60 session service + 35 integration + 5 factory + 10 benchmarks)
- ✅ 80%+ test coverage
- ✅ All tests pass
- ✅ Performance benchmarks meet targets

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] All tests passing (unit + integration + benchmarks)
- [ ] Test coverage ≥80%
- [ ] Code follows established patterns from TASK-011
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests
- [ ] Authorization checks verified in all methods

---

## Notes

**Authorization Pattern:**
- All session operations require organization authorization
- Authorization is checked via **parent case ownership**
- Get case first, verify organization_id matches

```python
# Authorization pattern
case = await self.case_repo.get_by_id(session.case_id)
if not case:
    raise NotFoundError("Case", session.case_id)

if case.organization_id != organization_id:
    raise AuthorizationError(f"Session {session_id} not accessible")
```

**Active Session Enforcement:**
- Only one active session per case at a time
- Creating new active session when one exists raises ConflictError
- Recommendation: Complete or pause existing session first

**Token Budget Pattern:**
- Budget is optional (token_budget_limit can be None)
- When set, track usage via `add_agent_execution()`
- Check with `check_budget_exceeded()` before expensive operations
- Frontend can warn users when approaching limit

**SET NULL Behavior:**
- When session deleted, executions remain (session_id set to NULL)
- This preserves historical execution records
- Verified in TASK-008 migration

**No Database Migration:**
- Service layer is pure Python logic
- No schema changes required
- Uses existing repositories and domain models

**Evolution Path:**
```
TASK-011: Case Service ✅
TASK-012: Session Service ← Current
TASK-013: Evidence Service
TASK-014: FastAPI Controllers (REST API)
```

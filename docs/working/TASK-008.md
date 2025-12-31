# TASK-008: Investigation Session Repository Pattern

**Phase:** Week 2, Day 6-7 (Repository Pattern Evolution)
**Priority:** P1 (Investigation workflow tracking)
**Estimated Time:** 6-8 hours
**Dependencies:** TASK-007 (Agent Execution Repository)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement the Investigation Session Repository Pattern to track investigation sessions within cases. Sessions represent continuous investigation periods with multiple agent executions, user interactions, and state management.

---

## Context

Investigation sessions provide temporal structure to case investigations. A single case may have multiple investigation sessions (initial triage, deep dive, follow-up). Each session contains:
- Multiple agent executions
- User interactions and annotations
- Session state (active, paused, completed, abandoned)
- Duration tracking and token budget management

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. Domain Model: InvestigationSession

**File:** `faultmaven/models/investigation_session.py`

```python
@dataclass
class InvestigationSession:
    """Investigation session within a case.

    Represents a continuous investigation period with multiple agent
    executions, user interactions, and state management.
    """
    session_id: str
    case_id: str
    user_id: str
    organization_id: str

    # Session state
    status: SessionStatus  # active, paused, completed, abandoned

    # Temporal tracking
    started_at: datetime
    ended_at: Optional[datetime] = None
    last_activity_at: datetime
    total_duration_ms: Optional[int] = None

    # Investigation context
    session_goal: Optional[str] = None  # "Identify root cause of timeout"
    findings_summary: Optional[str] = None

    # Resource tracking
    total_token_usage: int = 0
    total_agent_executions: int = 0
    token_budget_limit: Optional[int] = None  # Optional spending limit

    # Metadata
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
```

**SessionStatus Enum:**
```python
class SessionStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
```

**Lifecycle Methods:**
```python
def pause(self) -> None:
    """Pause the session."""

def resume(self) -> None:
    """Resume a paused session."""

def complete(self, findings_summary: str) -> None:
    """Mark session as completed with findings."""

def abandon(self) -> None:
    """Mark session as abandoned."""

def add_agent_execution(self, token_usage: int) -> None:
    """Record an agent execution and update token usage."""

def is_active(self) -> bool:
    """Check if session is currently active."""

def is_over_budget(self) -> bool:
    """Check if session has exceeded token budget."""

def get_duration_display(self) -> str:
    """Get human-readable session duration."""
```

**Validation:**
- Required: `session_id`, `case_id`, `user_id`, `organization_id`, `started_at`
- `total_token_usage` >= 0
- `total_agent_executions` >= 0
- `token_budget_limit` (if set) >= 0
- `ended_at` must be after `started_at` if set

---

### 2. Database Migration

**File:** `alembic/versions/20251229_2000_005_add_investigation_sessions.py`

**Table: investigation_sessions**

| Column | Type | Constraints |
|--------|------|-------------|
| session_id | VARCHAR(64) | PRIMARY KEY |
| case_id | VARCHAR(17) | NOT NULL, FK → cases.case_id ON DELETE CASCADE |
| user_id | VARCHAR(255) | NOT NULL |
| organization_id | VARCHAR(64) | NOT NULL |
| status | VARCHAR(32) | NOT NULL, DEFAULT 'active' |
| started_at | TIMESTAMPTZ | NOT NULL |
| ended_at | TIMESTAMPTZ | NULL |
| last_activity_at | TIMESTAMPTZ | NOT NULL |
| total_duration_ms | INTEGER | NULL, >= 0 |
| session_goal | TEXT | NULL |
| findings_summary | TEXT | NULL |
| total_token_usage | INTEGER | NOT NULL, DEFAULT 0, >= 0 |
| total_agent_executions | INTEGER | NOT NULL, DEFAULT 0, >= 0 |
| token_budget_limit | INTEGER | NULL, >= 0 |
| metadata | JSONB (PostgreSQL) / TEXT (SQLite) | DEFAULT '{}' |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() |

**Indexes:**
- `idx_investigation_sessions_case_id` (case_id)
- `idx_investigation_sessions_user_id` (user_id)
- `idx_investigation_sessions_organization_id` (organization_id)
- `idx_investigation_sessions_status` (status)
- `idx_investigation_sessions_started_at` (started_at DESC)
- `idx_investigation_sessions_last_activity_at` (last_activity_at DESC)

**Check Constraints (PostgreSQL):**
```sql
CONSTRAINT investigation_sessions_status_valid
    CHECK (status IN ('active', 'paused', 'completed', 'abandoned'))
CONSTRAINT investigation_sessions_duration_non_negative
    CHECK (total_duration_ms IS NULL OR total_duration_ms >= 0)
CONSTRAINT investigation_sessions_token_usage_non_negative
    CHECK (total_token_usage >= 0)
CONSTRAINT investigation_sessions_executions_non_negative
    CHECK (total_agent_executions >= 0)
CONSTRAINT investigation_sessions_budget_non_negative
    CHECK (token_budget_limit IS NULL OR token_budget_limit >= 0)
```

**Foreign Key:**
```sql
CONSTRAINT fk_investigation_sessions_case_id
    FOREIGN KEY (case_id) REFERENCES cases(case_id)
    ON DELETE CASCADE
```

**PostgreSQL Trigger:**
Auto-update `updated_at` timestamp on UPDATE.

**Note:** This creates a **four-level CASCADE delete chain**:
```
Case → Session → Execution → ToolCall
```

---

### 3. ORM Model

**File:** `faultmaven/infrastructure/persistence/models.py`

Add `InvestigationSessionModel` class:

```python
class InvestigationSessionModel(Base):
    """Investigation session ORM model."""
    __tablename__ = "investigation_sessions"

    session_id = Column(String(64), primary_key=True)
    case_id = Column(String(17), ForeignKey("cases.case_id", ondelete="CASCADE"))
    # ... all columns from migration

    # Relationships
    case = relationship("CaseModel", back_populates="investigation_sessions")
    agent_executions = relationship(
        "AgentExecutionModel",
        back_populates="session",
        cascade="all, delete-orphan"
    )
```

**Update AgentExecutionModel:**
Add optional `session_id` foreign key to link executions to sessions:

```python
class AgentExecutionModel(Base):
    # ... existing columns

    # Optional: link execution to session
    session_id = Column(
        String(64),
        ForeignKey("investigation_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Relationship
    session = relationship("InvestigationSessionModel", back_populates="agent_executions")
```

**Migration Note:** This requires a new migration to add `session_id` to `agent_executions` table. This should be done in the same migration (`005_add_investigation_sessions.py`) to maintain atomicity.

---

### 4. Repository Interface

**File:** `faultmaven/infrastructure/persistence/investigation_session_repository.py`

```python
class InvestigationSessionRepository(ABC):
    """Abstract repository for investigation sessions."""

    @abstractmethod
    async def create(self, session: InvestigationSession) -> InvestigationSession:
        """Create a new investigation session."""

    @abstractmethod
    async def get_by_id(self, session_id: str) -> Optional[InvestigationSession]:
        """Get session by ID."""

    @abstractmethod
    async def update(self, session: InvestigationSession) -> InvestigationSession:
        """Update existing session."""

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Delete session by ID."""

    @abstractmethod
    async def list_by_case_id(
        self,
        case_id: str,
        status: Optional[SessionStatus] = None
    ) -> List[InvestigationSession]:
        """List all sessions for a case, optionally filtered by status."""

    @abstractmethod
    async def get_active_session(self, case_id: str) -> Optional[InvestigationSession]:
        """Get the currently active session for a case (if any)."""

    @abstractmethod
    async def list_by_user_id(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[InvestigationSession]:
        """List sessions by user (paginated)."""

    @abstractmethod
    async def count_by_case_id(self, case_id: str) -> int:
        """Count sessions for a case."""
```

---

### 5. Database Implementation

**Class:** `DatabaseInvestigationSessionRepository`

**Implementation Requirements:**
- Use async SQLAlchemy with `AsyncSession`
- Map between `InvestigationSessionModel` (ORM) and `InvestigationSession` (domain)
- Handle JSONB serialization (PostgreSQL) vs TEXT JSON (SQLite)
- Proper error handling and logging
- Transaction management

**Key Methods:**

**`create()`:**
- Validate session doesn't already exist
- Convert domain model → ORM model
- Insert into database
- Return created domain model

**`get_by_id()`:**
- Query by session_id
- Return None if not found
- Convert ORM → domain model

**`update()`:**
- Fetch existing record
- Update fields
- Update `updated_at` timestamp
- Commit transaction
- Return updated domain model

**`list_by_case_id()`:**
- Query by case_id
- Optional status filter
- Order by started_at DESC
- Convert all ORM → domain models

**`get_active_session()`:**
- Query by case_id AND status='active'
- Return first result (should only be one active session per case)
- Return None if no active session

---

### 6. In-Memory Implementation

**Class:** `InMemoryInvestigationSessionRepository`

**Purpose:** Testing and local development

**Implementation:**
- Store sessions in `Dict[str, InvestigationSession]`
- Implement all interface methods
- Deep copy on read/write to prevent mutation
- Filter/sort operations in-memory

---

### 7. Factory Integration

**File:** `faultmaven/infrastructure/persistence/repository_factory.py`

Add factory method:

```python
def create_investigation_session_repository(
    db_session: Optional[AsyncSession] = None,
    use_in_memory: bool = False
) -> InvestigationSessionRepository:
    """Create investigation session repository."""
    if use_in_memory:
        return InMemoryInvestigationSessionRepository()
    return DatabaseInvestigationSessionRepository(db_session)
```

---

## Testing Requirements

### 1. Domain Model Tests (40+ tests)

**File:** `tests/unit/models/test_investigation_session.py`

**Test Coverage:**
- ✅ Model creation and validation
- ✅ Required field validation
- ✅ Lifecycle methods:
  - `pause()`, `resume()`, `complete()`, `abandon()`
  - State transitions (active → paused → active → completed)
- ✅ `add_agent_execution()` - token/execution counting
- ✅ `is_active()`, `is_over_budget()`
- ✅ `get_duration_display()` formatting
- ✅ Edge cases:
  - Negative token usage
  - Negative durations
  - Invalid status values
  - Budget limit enforcement

---

### 2. Repository Unit Tests (35+ tests)

**File:** `tests/unit/infrastructure/persistence/test_investigation_session_repository.py`

**Test Coverage:**
- ✅ CRUD operations (create, get, update, delete)
- ✅ `list_by_case_id()` with/without status filter
- ✅ `get_active_session()` - single active session per case
- ✅ `list_by_user_id()` with pagination
- ✅ `count_by_case_id()`
- ✅ Error handling (not found, duplicates, etc.)
- ✅ Both implementations (Database + InMemory)
- ✅ JSONB serialization (metadata field)

---

### 3. Integration Tests (25+ tests)

**File:** `tests/integration/test_investigation_session_integration.py`

**Critical Tests:**

**Four-level CASCADE Delete:**
```python
async def test_cascade_delete_case_to_sessions_to_executions_to_tool_calls():
    """Verify Case → Session → Execution → ToolCall CASCADE chain."""
    # Create case
    # Create session for case
    # Create execution for session
    # Create tool calls for execution
    # Delete case
    # Verify all sessions, executions, and tool calls CASCADE deleted
```

**Session Lifecycle:**
```python
async def test_session_full_lifecycle():
    """Test complete session lifecycle from creation to completion."""
    # Create active session
    # Add agent executions
    # Pause session
    # Resume session
    # Complete session with findings
    # Verify state transitions and timestamps
```

**Active Session Enforcement:**
```python
async def test_single_active_session_per_case():
    """Verify only one active session allowed per case."""
    # Create active session for case
    # Attempt to create another active session
    # Should complete first session before starting new one
```

**Link Executions to Session:**
```python
async def test_link_agent_executions_to_session():
    """Test linking agent executions to a session."""
    # Create session
    # Create execution with session_id
    # Verify execution.session relationship
    # Verify session.agent_executions relationship
```

---

### 4. Performance Benchmarks (10+ benchmarks)

**File:** `tests/benchmarks/test_investigation_session_operations.py`

**Benchmarks:**
- Create session (target: <200ms p95)
- Retrieve session (target: <100ms p95)
- Update session (target: <150ms p95)
- List sessions by case (100 sessions, target: <200ms p95)
- Get active session (target: <100ms p95)
- CASCADE delete (session → executions, target: <500ms p95)
- Bulk operations (if applicable)

---

## Migration Strategy

**File:** `alembic/versions/20251229_2000_005_add_investigation_sessions.py`

**Order of Operations:**

1. Create `investigation_sessions` table
2. Create indexes for `investigation_sessions`
3. Add foreign key to `cases` with CASCADE
4. Add `session_id` column to `agent_executions` (nullable)
5. Create index on `agent_executions.session_id`
6. Add foreign key to `investigation_sessions` with ON DELETE SET NULL
7. Create triggers for auto-update `updated_at`

**Downgrade:**
Reverse order - drop foreign keys, drop indexes, drop columns, drop table.

---

## Acceptance Criteria

- ✅ Domain model implemented with lifecycle methods
- ✅ Database migration with four-level CASCADE delete chain
- ✅ Repository interface with 8 methods
- ✅ Database implementation (async SQLAlchemy)
- ✅ In-memory implementation (testing)
- ✅ Factory integration
- ✅ 100+ tests (40 model + 35 repository + 25 integration + 10 benchmarks)
- ✅ 80%+ test coverage
- ✅ CASCADE delete chain verified in integration tests
- ✅ All tests pass
- ✅ Performance benchmarks meet targets

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] Database migration runs cleanly (PostgreSQL + SQLite)
- [ ] All tests passing (unit + integration + benchmarks)
- [ ] Test coverage ≥80%
- [ ] Code follows established patterns from TASK-002/003/006/007
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests
- [ ] Documentation updated (if needed)

---

## Notes

**Relationship to Agent Executions:**
- Sessions are optional containers for executions
- Not all executions need to be in a session (backward compatibility)
- `agent_executions.session_id` is nullable with ON DELETE SET NULL
- If session deleted, executions remain but lose session association

**Active Session Pattern:**
- Only one active session per case at a time
- New session should pause/complete previous session first
- Prevents concurrent investigation confusion

**Token Budget:**
- Optional spending limit per session
- Enables cost control for investigations
- Can warn/block when approaching limit

**Evolution Chain:**
This completes the four-level CASCADE delete chain:
```
Case (TASK-002)
  └─→ InvestigationSession (TASK-008)
        └─→ AgentExecution (TASK-007)
              └─→ AgentToolCall (TASK-007)
  └─→ EvidenceArtifact (TASK-006)
```

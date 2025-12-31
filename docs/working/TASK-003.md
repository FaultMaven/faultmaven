# TASK-003: Session Management Integration

## Task Metadata
- **Phase**: Week 1, Day 4-5 (Foundation)
- **Priority**: P0 (Required for case lifecycle tracking)
- **Estimated Time**: 2-3 hours implementation + tests
- **Dependencies**:
  - TASK-001 (Alembic setup) ✅ Complete
  - TASK-002 (Case Repository) ✅ Complete
- **Assignee**: Developer (implementation + tests)
- **Test Reviewer**: Test-Engineer (TASK-003-TEST-REVIEW)
- **Architect Reviewer**: Solutions Architect (final approval)

## Objective

Integrate session management with the case repository to track user sessions associated with cases. This enables session-based case creation, retrieval, and context continuity across user interactions.

## Context

Currently, cases are created and managed independently of user sessions. We need to:
1. Link cases to user sessions for context tracking
2. Add session metadata to case creation workflow
3. Support session-based case queries
4. Enable session cleanup that respects active cases
5. Prepare for future agent chat integration (Week 7-8)

## Acceptance Criteria

### Functional Requirements
- [ ] Cases linked to session IDs via foreign key
- [ ] Session metadata stored with case creation timestamp
- [ ] `get_cases_by_session()` method retrieves all cases for a session
- [ ] Session cleanup preserves case data (soft delete pattern)
- [ ] Case creation accepts optional `session_id` parameter
- [ ] Session-to-case relationship queryable bidirectionally

### Technical Requirements
- [ ] Alembic migration adds `session_id` to cases table
- [ ] Session model created with SQLAlchemy
- [ ] Repository methods updated to handle session context
- [ ] Foreign key constraint with `ON DELETE SET NULL` (preserve orphaned cases)
- [ ] Session storage type configurable (in-memory vs database)
- [ ] Type hints on all session-related methods

### Testing Requirements (Developer Must Implement)
- [ ] Unit tests for session-case linking (80%+ coverage) - **DEVELOPER WRITES THESE**
- [ ] Integration tests with session lifecycle - **DEVELOPER WRITES THESE**
- [ ] Test case creation with/without session
- [ ] Test session cleanup doesn't delete cases
- [ ] Test session-based case queries
- [ ] All tests pass locally before PR submission
- [ ] All tests pass in CI/CD
- [ ] Test code reviewed by test-engineer (TASK-003-TEST-REVIEW)

## Implementation Steps

### Step 1: Create Session Model

**File:** `faultmaven/infrastructure/persistence/models.py` (extend existing)

```python
from sqlalchemy import Column, String, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

class SessionModel(Base):
    __tablename__ = "sessions"

    session_id = Column(String(36), primary_key=True)  # UUID format
    user_id = Column(String(255), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    last_accessed = Column(DateTime, server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime, nullable=True)
    metadata = Column(JSON, nullable=True)  # Store session context

    # Relationship to cases
    cases = relationship("CaseModel", back_populates="session", foreign_keys="CaseModel.session_id")

# Update CaseModel to include session relationship
class CaseModel(Base):
    # ... existing columns ...
    session_id = Column(String(36), ForeignKey("sessions.session_id", ondelete="SET NULL"), nullable=True)

    # Relationship
    session = relationship("SessionModel", back_populates="cases")
```

### Step 2: Create Alembic Migration

**Command:**
```bash
alembic revision -m "002_add_session_management"
```

**File:** `alembic/versions/YYYYMMDD_HHMM_002_add_session_management.py`

```python
"""002_add_session_management

Add sessions table and link cases to sessions.

Revision ID: <generated>
Revises: da6856719b5f
Create Date: <timestamp>
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '<generated>'
down_revision = 'da6856719b5f'  # Previous migration
branch_labels = None
depends_on = None

def upgrade():
    # Create sessions table
    op.create_table(
        'sessions',
        sa.Column('session_id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('last_accessed', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('metadata', postgresql.JSONB() if op.get_bind().dialect.name == 'postgresql' else sa.JSON(), nullable=True)
    )

    # Add session_id to cases table
    op.add_column('cases',
        sa.Column('session_id', sa.String(36), nullable=True)
    )

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_cases_session_id',
        'cases', 'sessions',
        ['session_id'], ['session_id'],
        ondelete='SET NULL'
    )

    # Add index for session-based queries
    op.create_index('ix_cases_session_id', 'cases', ['session_id'])
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'])

def downgrade():
    # Drop foreign key and column
    op.drop_constraint('fk_cases_session_id', 'cases', type_='foreignkey')
    op.drop_index('ix_cases_session_id', 'cases')
    op.drop_column('cases', 'session_id')

    # Drop sessions table
    op.drop_index('ix_sessions_user_id', 'sessions')
    op.drop_table('sessions')
```

### Step 3: Implement Session Repository

**File:** `faultmaven/infrastructure/persistence/session_repository.py`

```python
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from datetime import datetime, timedelta

from faultmaven.domain.models.session import Session
from faultmaven.infrastructure.persistence.models import SessionModel

class DatabaseSessionRepository:
    """Database-backed session repository"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def create_session(self, session: Session) -> Session:
        """Create a new session"""
        session_model = SessionModel(
            session_id=session.session_id,
            user_id=session.user_id,
            created_at=session.created_at,
            expires_at=session.expires_at,
            metadata=session.metadata
        )
        self.db.add(session_model)
        await self.db.commit()
        await self.db.refresh(session_model)
        return self._to_domain(session_model)

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve session by ID"""
        stmt = select(SessionModel).where(SessionModel.session_id == session_id)
        result = await self.db.execute(stmt)
        session_model = result.scalar_one_or_none()
        return self._to_domain(session_model) if session_model else None

    async def update_last_accessed(self, session_id: str) -> None:
        """Update session last_accessed timestamp"""
        stmt = select(SessionModel).where(SessionModel.session_id == session_id)
        result = await self.db.execute(stmt)
        session_model = result.scalar_one_or_none()
        if session_model:
            session_model.last_accessed = datetime.utcnow()
            await self.db.commit()

    async def delete_session(self, session_id: str) -> bool:
        """Delete session (cases will be orphaned with session_id=NULL)"""
        stmt = delete(SessionModel).where(SessionModel.session_id == session_id)
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def cleanup_expired_sessions(self) -> int:
        """Delete expired sessions"""
        now = datetime.utcnow()
        stmt = delete(SessionModel).where(
            SessionModel.expires_at.isnot(None),
            SessionModel.expires_at < now
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount

    def _to_domain(self, model: SessionModel) -> Session:
        """Convert SQLAlchemy model to domain model"""
        return Session(
            session_id=model.session_id,
            user_id=model.user_id,
            created_at=model.created_at,
            last_accessed=model.last_accessed,
            expires_at=model.expires_at,
            metadata=model.metadata
        )
```

### Step 4: Update Case Repository

**File:** `faultmaven/infrastructure/persistence/database_case_repository.py` (extend)

Add session-aware methods:

```python
class DatabaseCaseRepository:
    # ... existing methods ...

    async def create_case(self, case: Case, session_id: Optional[str] = None) -> Case:
        """Create a new case, optionally linked to a session"""
        case_model = CaseModel(
            case_id=case.case_id,
            user_id=case.user_id,
            title=case.title,
            status=case.status,
            session_id=session_id,  # NEW: Link to session
            # ... other fields ...
        )
        self.db.add(case_model)
        await self.db.commit()
        await self.db.refresh(case_model)
        return self._to_domain(case_model)

    async def get_cases_by_session(self, session_id: str) -> List[Case]:
        """Get all cases associated with a session"""
        stmt = select(CaseModel).where(CaseModel.session_id == session_id)
        result = await self.db.execute(stmt)
        case_models = result.scalars().all()
        return [self._to_domain(model) for model in case_models]

    async def get_orphaned_cases(self, user_id: str) -> List[Case]:
        """Get cases with no session (session_id is NULL)"""
        stmt = select(CaseModel).where(
            CaseModel.user_id == user_id,
            CaseModel.session_id.is_(None)
        )
        result = await self.db.execute(stmt)
        case_models = result.scalars().all()
        return [self._to_domain(model) for model in case_models]
```

### Step 5: Create Domain Model

**File:** `faultmaven/domain/models/session.py`

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any

@dataclass
class Session:
    """Domain model for user session"""
    session_id: str
    user_id: str
    created_at: datetime
    last_accessed: datetime
    expires_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    def is_expired(self) -> bool:
        """Check if session has expired"""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    def is_active(self) -> bool:
        """Check if session is active"""
        return not self.is_expired()
```

### Step 6: Update Repository Factory

**File:** `faultmaven/infrastructure/persistence/repository_factory.py` (extend)

```python
from faultmaven.infrastructure.persistence.session_repository import DatabaseSessionRepository

def get_session_repository() -> SessionRepository:
    storage_type = os.getenv("SESSION_STORAGE_TYPE", "database")

    if storage_type == "inmemory":
        return InMemorySessionRepository()
    elif storage_type == "database":
        return DatabaseSessionRepository(get_db_session())
    else:
        raise ValueError(f"Unknown storage type: {storage_type}")
```

### Step 7: Write Unit Tests

**File:** `tests/unit/infrastructure/persistence/test_database_session_repository.py`

Required tests:
```python
@pytest.mark.unit
async def test_create_session():
    """Test creating a new session"""

@pytest.mark.unit
async def test_get_session():
    """Test retrieving session by ID"""

@pytest.mark.unit
async def test_get_session_not_found():
    """Test retrieving non-existent session returns None"""

@pytest.mark.unit
async def test_update_last_accessed():
    """Test updating session last_accessed timestamp"""

@pytest.mark.unit
async def test_delete_session():
    """Test deleting session"""

@pytest.mark.unit
async def test_cleanup_expired_sessions():
    """Test cleanup removes only expired sessions"""

@pytest.mark.unit
async def test_session_expiry_check():
    """Test session expiry logic"""
```

**File:** `tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py`

```python
@pytest.mark.unit
async def test_create_case_with_session():
    """Test creating case linked to session"""

@pytest.mark.unit
async def test_create_case_without_session():
    """Test creating case without session (session_id=NULL)"""

@pytest.mark.unit
async def test_get_cases_by_session():
    """Test retrieving all cases for a session"""

@pytest.mark.unit
async def test_get_cases_by_session_empty():
    """Test retrieving cases for session with no cases"""

@pytest.mark.unit
async def test_get_orphaned_cases():
    """Test retrieving cases with no session"""

@pytest.mark.unit
async def test_session_delete_orphans_cases():
    """Test deleting session sets case.session_id to NULL"""
```

### Step 8: Write Integration Tests

**File:** `tests/integration/test_session_case_integration.py`

```python
@pytest.mark.integration
async def test_session_case_lifecycle():
    """Test full lifecycle: create session → create case → retrieve → cleanup"""

@pytest.mark.integration
async def test_session_cleanup_preserves_cases():
    """Test deleting session doesn't delete cases"""

@pytest.mark.integration
async def test_multiple_cases_per_session():
    """Test multiple cases linked to same session"""

@pytest.mark.integration
async def test_session_expiry_workflow():
    """Test expired session cleanup with active cases"""

@pytest.mark.integration
async def test_repository_factory_session():
    """Test session repository factory returns correct implementation"""
```

## Files to Create/Modify

### Create
- `faultmaven/domain/models/session.py` (Domain model)
- `faultmaven/infrastructure/persistence/session_repository.py` (Repository)
- `alembic/versions/YYYYMMDD_HHMM_002_add_session_management.py` (Migration)
- `tests/unit/infrastructure/persistence/test_database_session_repository.py` (Unit tests)
- `tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py` (Unit tests)
- `tests/integration/test_session_case_integration.py` (Integration tests)

### Modify
- `faultmaven/infrastructure/persistence/models.py` (Add SessionModel, update CaseModel)
- `faultmaven/infrastructure/persistence/database_case_repository.py` (Add session methods)
- `faultmaven/infrastructure/persistence/repository_factory.py` (Add session factory)
- `.env.example` (Add SESSION_STORAGE_TYPE)

## Testing Requirements

### Coverage Target
- **Minimum:** 80% coverage for new code
- **Target:** 90% coverage for session logic
- **Critical paths:** 100% coverage (session-case linking, cleanup)

### Test Execution
```bash
# Run all tests
pytest

# Run unit tests only
pytest -m unit

# Run integration tests only
pytest -m integration

# Run with coverage
pytest --cov=faultmaven/infrastructure/persistence --cov-report=term-missing

# Verify coverage meets target
pytest --cov=faultmaven/infrastructure/persistence --cov-fail-under=80
```

### Test Data
- Use pytest fixtures for session and case data
- Test with both SQLite and PostgreSQL (if available)
- Cleanup after each test
- Test timezone handling (UTC timestamps)

## Success Metrics

### Definition of Done
- [ ] Session management fully integrated
- [ ] All unit tests pass (80%+ coverage)
- [ ] All integration tests pass
- [ ] Alembic migration tested (up and down)
- [ ] CI/CD pipeline passes
- [ ] Code reviewed and approved
- [ ] Documentation updated
- [ ] PR merged to main

### Performance
- Session creation: < 50ms (p95)
- Session retrieval: < 30ms (p95)
- Cases by session query: < 100ms for 50 cases (p95)

### Quality
- Zero flaky tests
- All async operations properly awaited
- No database connection leaks
- Proper error messages
- Foreign key constraints working correctly

## Environment Variables

Add to `.env.example`:
```bash
# Session Storage Configuration
SESSION_STORAGE_TYPE=database  # Options: database, inmemory
SESSION_EXPIRY_HOURS=24  # Default session expiration
```

## PR Template

**Title:** `[TASK-003] Implement Session Management Integration`

**Description:**
This PR integrates session management with the case repository, enabling session-based case tracking and context continuity.

**Changes:**
- Added SessionModel and updated CaseModel with session relationship
- Implemented DatabaseSessionRepository with full CRUD operations
- Extended DatabaseCaseRepository with session-aware methods
- Created Alembic migration for sessions table
- Added session repository factory
- Implemented 13 unit tests (90% coverage)
- Implemented 5 integration tests

**Testing:**
- [x] All unit tests pass (13/13) - **DEVELOPER MUST WRITE**
- [x] All integration tests pass (5/5) - **DEVELOPER MUST WRITE**
- [x] Coverage: 90% for session logic
- [x] Migration tested (upgrade and downgrade)
- [x] CI/CD pipeline passes
- [x] Manual testing: session lifecycle verified
- [x] Test-engineer reviewed tests (TASK-003-TEST-REVIEW)

**Checklist:**
- [ ] Session repository implements full interface
- [ ] Foreign key constraint works (ON DELETE SET NULL)
- [ ] Async/await used correctly
- [ ] **Unit tests written by developer (80%+ coverage)**
- [ ] **Integration tests written by developer**
- [ ] Session cleanup preserves cases
- [ ] Repository factory works
- [ ] Environment variables documented
- [ ] **Test-engineer approved test quality (TASK-003-TEST-REVIEW)**
- [ ] Solutions architect approved implementation

## Risks & Mitigation

### Risk 1: Session Cleanup Accidentally Deletes Cases
**Mitigation:** Use `ON DELETE SET NULL` constraint. Test extensively in integration tests.

### Risk 2: Orphaned Cases Accumulate
**Mitigation:** Implement `get_orphaned_cases()` method. Add monitoring for NULL session_id cases.

### Risk 3: Session Expiry Logic Errors
**Mitigation:** Use UTC timestamps consistently. Test timezone edge cases.

### Risk 4: In-Memory vs Database Session Mismatch
**Mitigation:** Define SessionRepository interface. Test both implementations.

## Next Steps After Completion

1. **TASK-004:** Minimal Shim Pattern Foundation (Week 1, Day 4-7)
   - Graceful degradation for Opik, Presidio
   - Enable community edition without heavy dependencies

2. **TASK-005:** Performance Baseline Suite (Week 1, Day 8-10)
   - Establish performance benchmarks
   - Monitor regression throughout evolution

## Questions?

Before starting:
- Understand session lifecycle management
- Review foreign key constraints (ON DELETE SET NULL)
- Know pytest async testing patterns
- Understand session expiry patterns

Ask solutions-architect if unclear.

---

**Ready to start?** Review this task, implement session management, write comprehensive tests, and submit PR when all tests pass.

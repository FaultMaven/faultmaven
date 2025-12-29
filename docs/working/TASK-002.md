# TASK-002: Case Repository Refactoring & Testing

## Task Metadata
- **Phase**: Week 1, Day 2-3 (Foundation)
- **Priority**: P0 (Required for session enhancement)
- **Estimated Time**: 3-4 hours
- **Dependencies**: TASK-001 (Alembic setup) ✅ Complete
- **Assignee**: Developer
- **Reviewer**: Solutions Architect

## Objective

Refactor the existing case repository from in-memory storage to use the Alembic-managed database schema. This enables persistent case storage and prepares for session enhancement in later tasks.

## Context

Currently, FaultMaven uses in-memory storage for cases (`InMemoryCaseRepository`). We need to:
1. Implement database-backed repository using SQLAlchemy
2. Maintain repository interface compatibility
3. Add comprehensive tests
4. Support both SQLite (dev) and PostgreSQL (prod)

## Acceptance Criteria

### Functional Requirements
- [ ] `DatabaseCaseRepository` implements same interface as `InMemoryCaseRepository`
- [ ] All CRUD operations work (create, read, update, delete)
- [ ] Case status transitions tracked in database
- [ ] Evidence, hypotheses, and solutions properly linked via foreign keys
- [ ] JSONB columns used for flexible data (consulting, metadata)
- [ ] Repository switchable via environment variable

### Technical Requirements
- [ ] Uses SQLAlchemy async session
- [ ] Proper transaction handling (rollback on errors)
- [ ] Connection pooling configured
- [ ] Database URL from environment variable
- [ ] Repository factory pattern for dependency injection
- [ ] Type hints on all methods

### Testing Requirements (Mandatory)
- [ ] Unit tests for repository methods (80%+ coverage)
- [ ] Integration tests with SQLite database
- [ ] Test case lifecycle (create → update → delete)
- [ ] Test concurrent operations (async safety)
- [ ] Test error handling (connection failures, constraint violations)
- [ ] All tests pass in CI/CD

## Implementation Steps

### Step 1: Create SQLAlchemy Models
**File:** `faultmaven/infrastructure/persistence/models.py`

```python
from sqlalchemy import Column, String, JSON, DateTime, ForeignKey, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import enum

Base = declarative_base()

class CaseStatus(str, enum.Enum):
    CONSULTING = "consulting"
    PROBLEM_VERIFICATION = "problem_verification"
    ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
    SOLUTION_IMPLEMENTATION = "solution_implementation"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ARCHIVED = "archived"

class CaseModel(Base):
    __tablename__ = "cases"

    case_id = Column(String(17), primary_key=True)
    user_id = Column(String(255), nullable=False)
    title = Column(String(200), nullable=False)
    status = Column(Enum(CaseStatus), nullable=False, default=CaseStatus.CONSULTING)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    consulting = Column(JSON, nullable=False)
    problem_verification = Column(JSON)
    working_conclusion = Column(JSON)
    # ... other JSONB columns
```

### Step 2: Implement DatabaseCaseRepository
**File:** `faultmaven/infrastructure/persistence/case_repository.py`

Key methods:
- `async def create_case(case: Case) -> Case`
- `async def get_case(case_id: str) -> Optional[Case]`
- `async def update_case(case: Case) -> Case`
- `async def delete_case(case_id: str) -> bool`
- `async def list_cases(user_id: str) -> List[Case]`
- `async def transition_status(case_id: str, from_status: str, to_status: str, reason: str)`

### Step 3: Create Repository Factory
**File:** `faultmaven/infrastructure/persistence/repository_factory.py`

```python
def get_case_repository() -> CaseRepository:
    storage_type = os.getenv("CASE_STORAGE_TYPE", "database")

    if storage_type == "inmemory":
        return InMemoryCaseRepository()
    elif storage_type == "database":
        return DatabaseCaseRepository(get_db_session())
    else:
        raise ValueError(f"Unknown storage type: {storage_type}")
```

### Step 4: Configure Database Session
**File:** `faultmaven/infrastructure/persistence/database.py`

```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./faultmaven.db")

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
```

### Step 5: Write Unit Tests
**File:** `tests/unit/infrastructure/persistence/test_database_case_repository.py`

Required tests:
```python
@pytest.mark.unit
async def test_create_case():
    """Test creating a new case in database"""

@pytest.mark.unit
async def test_get_case():
    """Test retrieving case by ID"""

@pytest.mark.unit
async def test_update_case():
    """Test updating case fields"""

@pytest.mark.unit
async def test_delete_case():
    """Test deleting case"""

@pytest.mark.unit
async def test_list_cases_by_user():
    """Test retrieving all cases for a user"""

@pytest.mark.unit
async def test_status_transition():
    """Test case status changes tracked"""

@pytest.mark.unit
async def test_concurrent_updates():
    """Test multiple updates don't corrupt data"""

@pytest.mark.unit
async def test_rollback_on_error():
    """Test transaction rollback on error"""
```

### Step 6: Write Integration Tests
**File:** `tests/integration/test_case_repository_integration.py`

```python
@pytest.mark.integration
async def test_full_case_lifecycle():
    """Test create → update → retrieve → delete flow"""

@pytest.mark.integration
async def test_case_with_evidence():
    """Test case with linked evidence"""

@pytest.mark.integration
async def test_case_with_hypotheses():
    """Test case with linked hypotheses"""

@pytest.mark.integration
async def test_repository_factory():
    """Test repository factory returns correct implementation"""
```

## Files to Create/Modify

### Create
- `faultmaven/infrastructure/persistence/models.py` (SQLAlchemy models)
- `faultmaven/infrastructure/persistence/case_repository.py` (DatabaseCaseRepository)
- `faultmaven/infrastructure/persistence/database.py` (Session management)
- `faultmaven/infrastructure/persistence/repository_factory.py` (Factory pattern)
- `tests/unit/infrastructure/persistence/test_database_case_repository.py` (Unit tests)
- `tests/integration/test_case_repository_integration.py` (Integration tests)
- `tests/conftest.py` (Pytest fixtures for DB setup)

### Modify
- `faultmaven/api/routes/cases.py` (Use factory instead of direct instantiation)
- `.env.example` (Add CASE_STORAGE_TYPE=database)
- `faultmaven/domain/repositories/case_repository.py` (Ensure interface compatibility)

## Testing Requirements

### Coverage Target
- **Minimum:** 80% coverage for new code
- **Target:** 90% coverage for repository logic
- **Critical paths:** 100% coverage (CRUD operations, transactions)

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
- Use factory pattern for test data (consider `factory_boy`)
- Fixtures in `tests/conftest.py`
- Database seeding for integration tests
- Cleanup after each test

## Success Metrics

### Definition of Done
- [ ] `DatabaseCaseRepository` fully implemented
- [ ] All unit tests pass (80%+ coverage)
- [ ] All integration tests pass
- [ ] CI/CD pipeline passes
- [ ] Code reviewed and approved
- [ ] Documentation updated
- [ ] PR merged to main

### Performance
- Case creation: < 100ms (p95)
- Case retrieval: < 50ms (p95)
- List cases: < 200ms for 100 cases (p95)

### Quality
- Zero flaky tests
- All async operations properly awaited
- No database connection leaks
- Proper error messages

## Environment Variables

Add to `.env.example`:
```bash
# Case Storage Configuration
CASE_STORAGE_TYPE=database  # Options: database, inmemory
DATABASE_URL=sqlite+aiosqlite:///./faultmaven.db  # SQLite for development
# DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/faultmaven  # PostgreSQL for production
```

## PR Template

**Title:** `[TASK-002] Implement Database-Backed Case Repository with Tests`

**Description:**
This PR refactors case storage from in-memory to database-backed using SQLAlchemy and the Alembic schema from TASK-001.

**Changes:**
- Implemented `DatabaseCaseRepository` with async SQLAlchemy
- Added SQLAlchemy models for cases table
- Created repository factory for storage type switching
- Implemented database session management with connection pooling
- Added 12 unit tests (90% coverage)
- Added 4 integration tests
- Updated API routes to use repository factory

**Testing:**
- [x] All unit tests pass (12/12)
- [x] All integration tests pass (4/4)
- [x] Coverage: 91% for persistence layer
- [x] CI/CD pipeline passes
- [x] Manual testing: full case lifecycle verified

**Checklist:**
- [ ] Repository implements full interface
- [ ] Async/await used correctly
- [ ] Transaction handling verified
- [ ] Tests have 80%+ coverage
- [ ] Integration tests pass on SQLite
- [ ] Error handling tested
- [ ] Repository factory works
- [ ] Environment variables documented

## Risks & Mitigation

### Risk 1: SQLAlchemy Async Compatibility
**Mitigation:** Use `asyncpg` for PostgreSQL, `aiosqlite` for SQLite. Test both.

### Risk 2: Transaction Deadlocks
**Mitigation:** Keep transactions short. Use proper isolation levels. Test concurrent operations.

### Risk 3: Migration Doesn't Match Models
**Mitigation:** Verify SQLAlchemy models match Alembic migration schema exactly.

### Risk 4: Test Database Cleanup
**Mitigation:** Use pytest fixtures with `autouse=True` for cleanup. Isolate test databases.

## Next Steps After Completion

1. **TASK-003:** Session Management Integration (Week 1, Day 4-5)
   - Link cases to user sessions
   - Add session metadata to case creation

2. **TASK-004:** Evidence Repository (Week 2)
   - Similar refactoring for evidence storage
   - Link to cases via foreign keys

## Questions?

Before starting:
- Understand SQLAlchemy async patterns
- Review Alembic migration schema (TASK-001)
- Understand repository pattern
- Know pytest async testing (`pytest-asyncio`)

Ask solutions-architect if unclear.

---

**Ready to start?** Review this task, implement the repository, write comprehensive tests, and submit PR when all tests pass.

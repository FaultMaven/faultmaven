# Developer Guide: Database Usage for Investigation Implementation

**Audience**: Developers implementing milestone-based investigation (Layers 1-5)  
**Purpose**: How to use repositories in your code (without worrying about database setup)  
**Status**: ✅ Production Guide  
**Version**: 1.0  
**Last Updated**: 2025-11-04

---

## TL;DR - What You Need to Know

```python
# ✅ DO THIS: Use repository abstraction
from faultmaven.dependencies import get_case_repository

class MilestoneEngine:
    def __init__(self, case_repository: CaseRepository):  # Injected
        self.repo = case_repository
    
    async def process_turn(self, case: Case):
        # Just use the repository - it handles storage
        updated = await self.repo.save(case)
        return updated

# ❌ DON'T DO THIS: Create database connections
import sqlite3
conn = sqlite3.connect("data/faultmaven.db")  # NO!
```

**Key Principle**: You write business logic. Infrastructure handles storage.

---

## Table of Contents

1. [What You DON'T Need to Do](#1-what-you-dont-need-to-do)
2. [What You DO Need to Know](#2-what-you-do-need-to-know)
3. [The CaseRepository Interface](#3-the-caserepository-interface)
4. [How to Use Repositories in Your Code](#4-how-to-use-repositories-in-your-code)
5. [Dependency Injection Patterns](#5-dependency-injection-patterns)
6. [Testing Your Code](#6-testing-your-code)
7. [Common Patterns](#7-common-patterns)
8. [What If I Need...](#8-what-if-i-need)
9. [Troubleshooting](#9-troubleshooting)
10. [FAQs](#10-faqs)

---

## 1. What You DON'T Need to Do

### ❌ Don't Create Databases

**You are NOT responsible for**:
- Creating SQLite files
- Creating PostgreSQL tables
- Running migrations
- Setting up connections
- Managing connection pools
- Handling database errors at the connection level

**Someone else handles this**: The infrastructure team (Layer 0) already did this.

### ❌ Don't Import Database Libraries

**Never import these in your investigation code**:
```python
# ❌ NO!
import sqlite3
import psycopg2
import asyncpg
from sqlalchemy import create_engine

# ❌ NO!
conn = sqlite3.connect(...)
cursor = conn.cursor()
cursor.execute("INSERT INTO ...")
```

**Why**: Your code should work with ANY storage backend (files, SQLite, PostgreSQL, or future options).

### ❌ Don't Write SQL

**Never write SQL in investigation logic**:
```python
# ❌ NO!
cursor.execute("""
    INSERT INTO cases (case_id, user_id, status)
    VALUES (?, ?, ?)
""", (case.case_id, case.user_id, case.status))
```

**Why**: The repository abstraction handles this. Your code stays clean and testable.

---

## 2. What You DO Need to Know

### ✅ You Work With Python Objects

```python
from faultmaven.models.case import Case, CaseStatus

# Create a case (just a Python object)
case = Case(
    user_id="user_123",
    organization_id="org_456",
    title="API Error Investigation",
    status=CaseStatus.INVESTIGATING
)

# That's it! No SQL, no database knowledge needed.
```

### ✅ You Use the Repository Interface

```python
from faultmaven.dependencies import get_case_repository

# Get repository (abstraction - you don't know if it's SQLite or PostgreSQL)
repo = get_case_repository()

# Save case (works with ANY backend)
saved_case = await repo.save(case)

# Get case (works with ANY backend)
retrieved = await repo.get(case.case_id)
```

### ✅ You Focus on Business Logic

**Your job is**:
- Implement milestone tracking
- Process user messages
- Generate LLM prompts
- Update investigation state
- Track evidence and hypotheses

**Not your job**:
- Database setup
- Connection management
- Query optimization
- Schema design

---

## 3. The CaseRepository Interface

### What Operations Are Available

```python
from faultmaven.infrastructure.database.repositories.base import CaseRepository

class CaseRepository(ABC):
    """All storage backends implement these methods"""
    
    # 1. Save or update a case
    async def save(self, case: Case) -> Case:
        """Save case (create or update). Returns saved case."""
        pass
    
    # 2. Get case by ID
    async def get(self, case_id: str) -> Optional[Case]:
        """Get case. Returns None if not found."""
        pass
    
    # 3. List user's cases
    async def list_by_user(
        self, 
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        status_filter: Optional[str] = None
    ) -> List[Case]:
        """List cases for user with pagination."""
        pass
    
    # 4. Delete case
    async def delete(self, case_id: str) -> bool:
        """Delete case. Returns True if deleted, False if not found."""
        pass
    
    # 5. Check if case exists
    async def exists(self, case_id: str) -> bool:
        """Check if case exists."""
        pass
    
    # 6. Count user's cases
    async def count_by_user(self, user_id: str) -> int:
        """Count total cases for user."""
        pass
    
    # 7. Search cases
    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 50
    ) -> List[Case]:
        """Search cases by text."""
        pass
```

**Important**: These 7 methods are ALL you need. If you need something else, ask the architecture team first!

---

## 4. How to Use Repositories in Your Code

### Pattern 1: In Classes (Dependency Injection)

```python
# faultmaven/services/agentic/orchestration/milestone_engine.py

from faultmaven.infrastructure.database.repositories.base import CaseRepository
from faultmaven.models.case import Case

class MilestoneInvestigationEngine:
    """
    Milestone-based investigation engine.
    
    Notice: Takes CaseRepository as parameter (dependency injection).
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        case_repository: CaseRepository,  # ← Injected (interface, not concrete)
        logger: Logger
    ):
        self.llm = llm_provider
        self.case_repo = case_repository  # ← Store for use
        self.logger = logger
    
    async def process_turn(
        self, 
        case: Case, 
        user_message: str
    ) -> Dict[str, Any]:
        """
        Process investigation turn.
        
        Notice: Uses repository without knowing implementation details.
        """
        
        # 1. Generate LLM response
        prompt = self._build_prompt(case, user_message)
        llm_response = await self.llm.generate(prompt)
        
        # 2. Update case based on response
        case.current_turn += 1
        case.progress.symptom_verified = True  # Example
        
        # 3. Save case (works with ANY backend - SQLite, PostgreSQL, etc.)
        updated_case = await self.case_repo.save(case)
        
        # 4. Return result
        return {
            "response": llm_response.content,
            "case": updated_case
        }
```

**Key Points**:
- ✅ Type hint: `CaseRepository` (interface, not `SQLiteCaseRepository`)
- ✅ Injected via constructor (testable)
- ✅ Just call methods - don't worry about storage

### Pattern 2: In Functions (Dependency Injection)

```python
# faultmaven/services/agentic/processors/investigating_processor.py

from faultmaven.infrastructure.database.repositories.base import CaseRepository
from faultmaven.models.case import Case
from faultmaven.models.llm_schemas import InvestigationResponse

async def process_investigation_response(
    case: Case,
    llm_response: InvestigationResponse,
    case_repository: CaseRepository  # ← Injected parameter
) -> Case:
    """
    Process LLM response and update case.
    
    Notice: Repository passed as parameter.
    """
    
    # 1. Apply milestone updates
    if llm_response.state_update.milestones:
        milestones = llm_response.state_update.milestones
        if milestones.symptom_verified:
            case.progress.symptom_verified = True
        if milestones.root_cause_identified:
            case.progress.root_cause_identified = True
    
    # 2. Save updated case
    updated_case = await case_repository.save(case)
    
    return updated_case
```

### Pattern 3: In API Endpoints (FastAPI Dependency)

```python
# faultmaven/api/v1/routes/cases.py

from fastapi import APIRouter, Depends
from faultmaven.dependencies import get_case_repository
from faultmaven.infrastructure.database.repositories.base import CaseRepository

router = APIRouter()

@router.get("/cases/{case_id}")
async def get_case(
    case_id: str,
    case_repo: CaseRepository = Depends(get_case_repository)  # ← FastAPI injection
):
    """
    Get case by ID.
    
    Notice: Repository injected by FastAPI.
    """
    
    # Just use it!
    case = await case_repo.get(case_id)
    
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    return case
```

---

## 5. Dependency Injection Patterns

### How Dependencies Are Wired

```python
# faultmaven/dependencies.py

"""
Dependency injection configuration.

THIS IS THE ONLY FILE that knows about concrete implementations!
"""

from faultmaven.infrastructure.database.config import DatabaseConfig
from faultmaven.infrastructure.database.repositories import (
    CaseRepository,
    FileSystemCaseRepository,
    SQLiteCaseRepository,
    PostgresCaseRepository
)

# Singleton configuration
_db_config = DatabaseConfig()
_case_repo_instance = None

def get_case_repository() -> CaseRepository:
    """
    Get case repository based on configuration.
    
    Returns appropriate implementation based on DATABASE_MODE env var.
    
    NOTE: You (investigation developer) just call this function.
          You don't need to know which implementation you get!
    """
    global _case_repo_instance
    
    if _case_repo_instance is None:
        if _db_config.mode == "file_based":
            _case_repo_instance = FileSystemCaseRepository(_db_config.storage_path)
        elif _db_config.mode == "standalone":
            _case_repo_instance = SQLiteCaseRepository(_db_config.database_url)
        elif _db_config.mode == "distributed":
            _case_repo_instance = PostgresCaseRepository(_db_config.database_url)
        else:
            raise ValueError(f"Unknown database mode: {_db_config.mode}")
    
    return _case_repo_instance
```

### Using in Your Code

```python
# YOUR CODE: faultmaven/services/agentic/orchestration/milestone_engine.py

from faultmaven.dependencies import get_case_repository  # ← Import this

# Option 1: Get at initialization
class MilestoneEngine:
    def __init__(self):
        self.case_repo = get_case_repository()  # ← Call this
    
    async def process_turn(self, case: Case):
        # Use self.case_repo
        await self.case_repo.save(case)


# Option 2: Better - accept as parameter (more testable)
class MilestoneEngine:
    def __init__(self, case_repository: CaseRepository):  # ← Inject
        self.case_repo = case_repository
    
    async def process_turn(self, case: Case):
        await self.case_repo.save(case)

# Wiring happens in main.py or FastAPI dependencies
```

---

## 6. Testing Your Code

### Why This Architecture Makes Testing Easy

**Without repository pattern** (bad):
```python
# Hard to test - needs real database
class Engine:
    def __init__(self):
        self.conn = sqlite3.connect("data/faultmaven.db")  # ❌ Needs real file
    
    async def process(self, case):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO ...")  # ❌ Needs real database

# Test has to set up real database!
```

**With repository pattern** (good):
```python
# Easy to test - use mock repository
class Engine:
    def __init__(self, case_repository: CaseRepository):  # ✅ Injected
        self.repo = case_repository
    
    async def process(self, case):
        await self.repo.save(case)  # ✅ Works with mock

# Test uses mock - no database needed!
```

### How to Write Tests

```python
# tests/unit/services/test_milestone_engine.py

import pytest
from unittest.mock import AsyncMock, MagicMock
from faultmaven.services.agentic.orchestration.milestone_engine import MilestoneEngine
from faultmaven.models.case import Case, CaseStatus

@pytest.fixture
def mock_repository():
    """Create mock repository for testing"""
    repo = AsyncMock()
    
    # Configure mock behavior
    repo.save.return_value = Case(
        user_id="test",
        organization_id="org",
        title="Test Case",
        status=CaseStatus.INVESTIGATING
    )
    
    return repo

@pytest.mark.asyncio
async def test_process_turn_updates_case(mock_repository):
    """Test that process_turn saves case"""
    
    # 1. Create engine with MOCK repository (no real database!)
    engine = MilestoneEngine(
        llm_provider=mock_llm,
        case_repository=mock_repository,  # ← Mock injected
        logger=mock_logger
    )
    
    # 2. Create test case
    case = Case(
        user_id="test",
        organization_id="org",
        title="Test",
        status=CaseStatus.INVESTIGATING
    )
    
    # 3. Process turn
    result = await engine.process_turn(case, "Test message")
    
    # 4. Verify repository.save() was called
    mock_repository.save.assert_called_once()
    
    # 5. Verify case was updated
    saved_case = mock_repository.save.call_args[0][0]
    assert saved_case.current_turn == 1

# ✅ Test runs instantly, no database setup needed!
```

### Integration Tests (Optional)

```python
# tests/integration/test_milestone_engine_integration.py

"""
Integration tests with REAL repository.

Only run these occasionally (slower).
"""

import pytest
from faultmaven.services.agentic.orchestration.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.database.repositories.sqlite import SQLiteCaseRepository

@pytest.mark.integration
@pytest.mark.asyncio
async def test_engine_with_real_database(tmp_path):
    """Test engine with real SQLite database"""
    
    # Create temporary database
    db_path = str(tmp_path / "test.db")
    real_repo = SQLiteCaseRepository(db_path)
    
    # Create engine with REAL repository
    engine = MilestoneEngine(
        llm_provider=mock_llm,
        case_repository=real_repo,  # ← Real repository
        logger=mock_logger
    )
    
    # Test with real storage
    case = Case(user_id="test", organization_id="org", title="Test")
    result = await engine.process_turn(case, "Test")
    
    # Verify case was actually saved to database
    retrieved = await real_repo.get(result['case'].case_id)
    assert retrieved is not None
    assert retrieved.current_turn == 1

# Run with: pytest -m integration
```

---

## 7. Common Patterns

### Pattern 1: Save After Every Change

```python
async def update_milestone(case: Case, repo: CaseRepository):
    """Update milestone and save"""
    
    # Update case
    case.progress.symptom_verified = True
    
    # Save immediately
    updated = await repo.save(case)
    
    return updated
```

### Pattern 2: Batch Updates

```python
async def process_evidence_batch(
    case: Case,
    evidence_list: List[Evidence],
    repo: CaseRepository
):
    """Add multiple pieces of evidence and save once"""
    
    # Update case multiple times
    for evidence in evidence_list:
        case.evidence.append(evidence)
    
    case.progress.scope_assessed = True
    case.current_turn += 1
    
    # Save once at the end
    updated = await repo.save(case)
    
    return updated
```

### Pattern 3: Error Handling

```python
from faultmaven.infrastructure.database.repositories.base import (
    RepositoryError,
    CaseNotFoundError
)

async def safe_case_update(case_id: str, repo: CaseRepository):
    """Update case with error handling"""
    
    try:
        # Get case
        case = await repo.get(case_id)
        
        if not case:
            raise CaseNotFoundError(f"Case {case_id} not found")
        
        # Update
        case.current_turn += 1
        
        # Save
        updated = await repo.save(case)
        return updated
        
    except RepositoryError as e:
        # Storage error (disk full, connection lost, etc.)
        logger.error(f"Failed to update case: {e}")
        raise
```

### Pattern 4: Pagination

```python
async def get_user_cases_paginated(
    user_id: str,
    page: int,
    page_size: int,
    repo: CaseRepository
):
    """Get paginated list of cases"""
    
    offset = (page - 1) * page_size
    
    cases = await repo.list_by_user(
        user_id=user_id,
        limit=page_size,
        offset=offset,
        status_filter="investigating"  # Optional filter
    )
    
    total = await repo.count_by_user(user_id)
    
    return {
        "cases": cases,
        "total": total,
        "page": page,
        "pages": (total + page_size - 1) // page_size
    }
```

---

## 8. What If I Need...

### Q: "I need to query cases by custom criteria"

**A**: Talk to the architecture team first. The repository interface might need extension.

**Example request**:
```python
# What you want to do:
cases = await repo.list_by_status_and_date(
    status="investigating",
    created_after=datetime.now() - timedelta(days=7)
)

# This is NOT in the current interface!
# → Request interface extension from architecture team
```

### Q: "I need transactions (save multiple things atomically)"

**A**: Use the repository's save method multiple times, or request transaction support.

**Current approach** (multiple saves):
```python
# Each save is atomic
await repo.save(case1)
await repo.save(case2)

# Note: If second save fails, first is already saved
# This is usually fine for investigation logic
```

**Future approach** (if transactions needed):
```python
# Architecture team can add transaction support
async with repo.transaction():
    await repo.save(case1)
    await repo.save(case2)
    # Both succeed or both fail
```

### Q: "I need better performance (caching)"

**A**: The infrastructure layer will handle this. Your code stays the same.

```python
# Your code (no changes):
case = await repo.get(case_id)

# Infrastructure adds caching layer:
# - First call: Hits database (slow)
# - Second call: Hits cache (fast)
# - You don't need to know!
```

### Q: "I need to search by vector similarity"

**A**: That's a different repository (KnowledgeBaseRepository). Ask architecture team.

---

## 9. Troubleshooting

### Issue 1: "Import Error - CaseRepository not found"

```python
# ❌ Wrong import
from faultmaven.models.case import CaseRepository  # NO!

# ✅ Correct import
from faultmaven.infrastructure.database.repositories.base import CaseRepository
```

### Issue 2: "get_case_repository() Returns None"

**Cause**: DATABASE_MODE environment variable not set

**Fix**:
```bash
# Set environment variable
export DATABASE_MODE=file_based

# Or in code/config
os.environ["DATABASE_MODE"] = "file_based"
```

### Issue 3: "Tests Fail With Database Errors"

**Cause**: Using real repository instead of mock in unit tests

**Fix**:
```python
# ❌ Wrong - uses real database
def test_engine():
    repo = SQLiteCaseRepository("test.db")  # Real DB!
    engine = MilestoneEngine(case_repository=repo)

# ✅ Correct - uses mock
def test_engine():
    repo = AsyncMock()  # Mock!
    engine = MilestoneEngine(case_repository=repo)
```

### Issue 4: "Case Not Saving"

**Debug checklist**:
```python
# 1. Check if save is actually called
await repo.save(case)  # Are you calling this?

# 2. Check if case has required fields
assert case.user_id is not None
assert case.case_id is not None

# 3. Check for exceptions
try:
    await repo.save(case)
except Exception as e:
    print(f"Save failed: {e}")  # What error?

# 4. Check database mode
from faultmaven.infrastructure.database.config import DatabaseConfig
config = DatabaseConfig()
print(f"Database mode: {config.mode}")  # What mode?
```

---

## 10. FAQs

### Q: Do I need to call `repo.commit()` or `repo.flush()`?

**A**: No! Each `save()` call is automatically committed. The repository handles this.

### Q: Can I access the database connection directly?

**A**: No, and you shouldn't want to. If you need something the repository doesn't provide, request an interface extension.

### Q: How do I know if I'm using SQLite or PostgreSQL?

**A**: You don't need to know! That's the whole point. Your code works with both.

### Q: What if the database is slow?

**A**: That's an infrastructure concern. The architecture team will add caching or optimize queries. Your code stays the same.

### Q: Can I use raw SQL for complex queries?

**A**: No. Complex queries should be added to the repository interface. This keeps SQL centralized and maintainable.

### Q: How do I run database migrations?

**A**: You don't. The infrastructure team handles migrations. You just use the repository interface.

### Q: What if I need to store something new (e.g., a new field)?

**A**: 
1. Add field to the `Case` model (Pydantic)
2. Infrastructure team updates database schema
3. You use the field immediately via `case.new_field`

```python
# Just use the new field
case.new_field = "value"
await repo.save(case)  # Infrastructure handles storage
```

### Q: Should I close the repository connection?

**A**: No. The repository manages its own lifecycle. Don't call `repo.close()` or similar.

---

## Summary: Your Checklist

When writing investigation code:

- [ ] ✅ Import `CaseRepository` from `infrastructure.database.repositories.base`
- [ ] ✅ Accept repository as constructor/function parameter (dependency injection)
- [ ] ✅ Use `await repo.save()`, `await repo.get()`, etc.
- [ ] ✅ Work with `Case` objects (Python), not SQL
- [ ] ✅ Write unit tests with mock repositories
- [ ] ❌ Don't import `sqlite3`, `psycopg2`, or any database library
- [ ] ❌ Don't write SQL
- [ ] ❌ Don't create database connections
- [ ] ❌ Don't worry about database setup

**Remember**: You focus on milestones, evidence, and hypotheses. Infrastructure handles storage.

---

## Quick Reference Card

```python
# ═══════════════════════════════════════════════════════════
#  INVESTIGATION DEVELOPER QUICK REFERENCE
# ═══════════════════════════════════════════════════════════

# 1️⃣ IMPORTS (Always these)
from faultmaven.dependencies import get_case_repository
from faultmaven.infrastructure.database.repositories.base import CaseRepository
from faultmaven.models.case import Case

# 2️⃣ GET REPOSITORY (Dependency injection)
repo = get_case_repository()

# 3️⃣ COMMON OPERATIONS
case = await repo.get(case_id)              # Get case
saved = await repo.save(case)               # Save case
cases = await repo.list_by_user(user_id)    # List cases
exists = await repo.exists(case_id)         # Check exists
deleted = await repo.delete(case_id)        # Delete case
count = await repo.count_by_user(user_id)   # Count cases
results = await repo.search("query")        # Search cases

# 4️⃣ ERROR HANDLING
from faultmaven.infrastructure.database.repositories.base import (
    RepositoryError,
    CaseNotFoundError
)

try:
    await repo.save(case)
except RepositoryError as e:
    logger.error(f"Storage error: {e}")

# 5️⃣ TESTING
from unittest.mock import AsyncMock

mock_repo = AsyncMock()
mock_repo.save.return_value = case
engine = MilestoneEngine(case_repository=mock_repo)

# ═══════════════════════════════════════════════════════════
```

---

**Questions?** Contact the architecture team or check `db-abstraction-layer-specification.md`

**Document End**


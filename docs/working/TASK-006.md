# TASK-006: Evidence Repository Pattern

## Task Metadata
- **Phase**: Week 2, Day 1-3 (Modular Foundation - Evidence Management)
- **Priority**: P1 (Core domain entity)
- **Estimated Time**: 3-4 hours
- **Dependencies**:
  - TASK-001 (Alembic migration infrastructure) ✅ Complete
  - TASK-002 (Case Repository pattern) ✅ Complete
  - TASK-003 (Session Management integration) ✅ Complete
  - TASK-004 (Minimal Shim Pattern) ✅ Complete
  - TASK-005 (Performance Baseline Suite) ✅ Complete
- **Assignee**: Developer
- **Reviewer**: Test-Engineer + Solutions Architect

## Objective

**Implement the Evidence Repository following the established repository pattern** to manage evidence artifacts (files, screenshots, logs, network traces) associated with cases.

### Success Criteria

1. ✅ Evidence domain model defined
2. ✅ Alembic migration creates evidence table
3. ✅ Database repository implementation (async SQLAlchemy)
4. ✅ In-memory repository implementation (testing)
5. ✅ Foreign key relationship to cases table
6. ✅ File metadata storage (path, type, size)
7. ✅ Integration with case lifecycle
8. ✅ Repository factory pattern
9. ✅ Comprehensive tests (unit + integration, 80%+ coverage)
10. ✅ Performance benchmarks added
11. ✅ Documentation updated

---

## Context

### Evolution Strategy Alignment

From the FaultMaven roadmap:

> **Week 2: Modular Foundation**
> - Evidence Repository (file management, storage abstraction)
> - Agent Repository (execution tracking, result storage)
> - Knowledge Base integration (vector search groundwork)

This task establishes the **Evidence Repository** following the same patterns proven in TASK-002 (Case Repository) and TASK-003 (Session Management).

### Why Evidence Management Matters

Evidence artifacts are critical to FaultMaven's value proposition:
- **Screenshots** - Visual proof of bugs
- **Logs** - Error traces and system output
- **Network traces** - HTTP/WebSocket request/response data
- **Configuration files** - Environment settings at time of error
- **Code snippets** - Relevant source code

Evidence must be:
1. **Linked to cases** - Foreign key relationship
2. **Metadata-rich** - Type, size, upload timestamp, original filename
3. **Storage-agnostic** - Local filesystem (community) or S3 (enterprise)
4. **Queryable** - List evidence by case, filter by type
5. **Performance-tested** - Benchmark upload/retrieval latency

---

## Domain Model

### Evidence Entity

**File**: `faultmaven/models/evidence.py`

```python
"""Evidence domain model.

Represents a piece of evidence (file, screenshot, log, etc.)
associated with a case.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from enum import Enum


class EvidenceType(str, Enum):
    """Types of evidence artifacts."""

    SCREENSHOT = "screenshot"
    LOG_FILE = "log_file"
    NETWORK_TRACE = "network_trace"
    CODE_SNIPPET = "code_snippet"
    CONFIGURATION = "configuration"
    VIDEO_RECORDING = "video_recording"
    HAR_FILE = "har_file"  # HTTP Archive
    OTHER = "other"


class StorageBackend(str, Enum):
    """Storage backend types."""

    LOCAL_FILESYSTEM = "local_filesystem"
    S3 = "s3"
    AZURE_BLOB = "azure_blob"
    GCS = "gcs"


@dataclass
class Evidence:
    """Evidence artifact associated with a case.

    Attributes:
        evidence_id: Unique identifier (UUID format recommended)
        case_id: Case this evidence belongs to
        user_id: User who uploaded the evidence
        organization_id: Organization that owns the evidence
        original_filename: Original filename when uploaded
        stored_filename: Filename as stored (may be renamed for uniqueness)
        file_path: Path to file (relative to storage root)
        evidence_type: Type of evidence (screenshot, log, etc.)
        mime_type: MIME type (e.g., image/png, text/plain)
        file_size: Size in bytes
        storage_backend: Where file is stored (local, s3, etc.)
        created_at: Upload timestamp
        updated_at: Last modification timestamp
        metadata: Additional evidence-specific metadata (JSON)
        description: Optional description of evidence
        is_primary: Whether this is primary/featured evidence for case
    """

    evidence_id: str
    case_id: str
    user_id: str
    organization_id: str
    original_filename: str
    stored_filename: str
    file_path: str
    evidence_type: EvidenceType
    mime_type: str
    file_size: int
    storage_backend: StorageBackend = StorageBackend.LOCAL_FILESYSTEM
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    is_primary: bool = False

    def __post_init__(self):
        """Validate evidence data."""
        if not self.evidence_id:
            raise ValueError("evidence_id is required")
        if not self.case_id:
            raise ValueError("case_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.organization_id:
            raise ValueError("organization_id is required")
        if not self.original_filename:
            raise ValueError("original_filename is required")
        if not self.file_path:
            raise ValueError("file_path is required")
        if self.file_size < 0:
            raise ValueError("file_size cannot be negative")

    def get_display_name(self) -> str:
        """Get user-friendly display name."""
        return self.description or self.original_filename

    def is_image(self) -> bool:
        """Check if evidence is an image."""
        return self.mime_type.startswith("image/")

    def is_text(self) -> bool:
        """Check if evidence is text-based."""
        return self.mime_type.startswith("text/") or self.mime_type == "application/json"
```

---

## Database Schema

### Alembic Migration

**File**: `alembic/versions/20251229_1600_003_add_evidence_management.py`

```python
"""Add evidence management.

Revision ID: 003
Revises: 002
Create Date: 2025-12-29 16:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create evidence table."""

    # Create evidence table
    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("stored_filename", sa.String(length=512), nullable=False),
        sa.Column("file_path", sa.String(length=2048), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=256), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False, server_default="local_filesystem"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Index("idx_evidence_case_id", "case_id"),
        sa.Index("idx_evidence_user_id", "user_id"),
        sa.Index("idx_evidence_organization_id", "organization_id"),
        sa.Index("idx_evidence_created_at", "created_at"),
        sa.Index("idx_evidence_type", "evidence_type"),
    )

    # Add foreign key to cases table
    op.create_foreign_key(
        "fk_evidence_case_id",
        "evidence",
        "cases",
        ["case_id"],
        ["case_id"],
        ondelete="CASCADE"  # Delete evidence when case is deleted
    )


def downgrade() -> None:
    """Drop evidence table."""

    op.drop_constraint("fk_evidence_case_id", "evidence", type_="foreignkey")
    op.drop_table("evidence")
```

**Migration Design Notes:**

1. **Foreign Key CASCADE**: Evidence deleted when case deleted (strong ownership)
2. **Indexes**: Optimized for common queries (by case, user, organization, type, date)
3. **JSONB metadata**: Flexible schema for evidence-specific data
4. **BigInteger file_size**: Supports files up to 8 exabytes
5. **Storage backend**: Enables multi-cloud storage strategy

---

## Repository Interface

### Abstract Repository

**File**: `faultmaven/infrastructure/persistence/evidence_repository.py` (interface section)

```python
"""Evidence repository implementations.

Provides storage and retrieval for evidence artifacts with metadata.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from datetime import datetime

from faultmaven.models.evidence import Evidence, EvidenceType


class EvidenceRepository(ABC):
    """Abstract repository for evidence management."""

    @abstractmethod
    async def create_evidence(self, evidence: Evidence) -> Evidence:
        """Create new evidence record.

        Args:
            evidence: Evidence to create

        Returns:
            Created evidence with timestamps

        Raises:
            ValueError: If evidence_id already exists
        """
        pass

    @abstractmethod
    async def get_evidence(self, evidence_id: str) -> Optional[Evidence]:
        """Get evidence by ID.

        Args:
            evidence_id: Evidence identifier

        Returns:
            Evidence if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Evidence], int]:
        """List evidence for a case.

        Args:
            case_id: Case identifier
            evidence_type: Optional filter by evidence type
            limit: Maximum results to return
            offset: Offset for pagination

        Returns:
            Tuple of (evidence list, total count)
        """
        pass

    @abstractmethod
    async def update_evidence(self, evidence: Evidence) -> Evidence:
        """Update existing evidence.

        Args:
            evidence: Evidence with updated fields

        Returns:
            Updated evidence

        Raises:
            ValueError: If evidence not found
        """
        pass

    @abstractmethod
    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence by ID.

        Args:
            evidence_id: Evidence identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def get_primary_evidence(self, case_id: str) -> Optional[Evidence]:
        """Get primary/featured evidence for case.

        Args:
            case_id: Case identifier

        Returns:
            Primary evidence if set, None otherwise
        """
        pass

    @abstractmethod
    async def set_primary_evidence(
        self,
        case_id: str,
        evidence_id: str
    ) -> bool:
        """Set primary/featured evidence for case.

        Only one evidence can be primary per case.

        Args:
            case_id: Case identifier
            evidence_id: Evidence to mark as primary

        Returns:
            True if set, False if evidence not found
        """
        pass
```

---

## Implementation

### Database Repository Implementation

**File**: `faultmaven/infrastructure/persistence/database_evidence_repository.py`

```python
"""Database-backed evidence repository implementation."""

from typing import List, Optional, Tuple
from datetime import datetime, timezone

from sqlalchemy import select, update, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from faultmaven.models.evidence import Evidence, EvidenceType
from faultmaven.infrastructure.persistence.evidence_repository import EvidenceRepository
from faultmaven.infrastructure.persistence.models import EvidenceModel


class DatabaseEvidenceRepository(EvidenceRepository):
    """Database implementation of evidence repository."""

    def __init__(self, session: AsyncSession):
        """Initialize repository.

        Args:
            session: Async database session
        """
        self.session = session

    async def create_evidence(self, evidence: Evidence) -> Evidence:
        """Create new evidence record."""

        # Convert domain model to ORM model
        evidence_model = EvidenceModel(
            evidence_id=evidence.evidence_id,
            case_id=evidence.case_id,
            user_id=evidence.user_id,
            organization_id=evidence.organization_id,
            original_filename=evidence.original_filename,
            stored_filename=evidence.stored_filename,
            file_path=evidence.file_path,
            evidence_type=evidence.evidence_type.value,
            mime_type=evidence.mime_type,
            file_size=evidence.file_size,
            storage_backend=evidence.storage_backend.value,
            created_at=evidence.created_at,
            updated_at=evidence.updated_at,
            metadata=evidence.metadata,
            description=evidence.description,
            is_primary=evidence.is_primary,
        )

        try:
            self.session.add(evidence_model)
            await self.session.flush()
            return evidence
        except IntegrityError as e:
            await self.session.rollback()
            if "foreign key constraint" in str(e).lower():
                raise ValueError(f"Case {evidence.case_id} not found")
            raise ValueError(f"Evidence {evidence.evidence_id} already exists")

    async def get_evidence(self, evidence_id: str) -> Optional[Evidence]:
        """Get evidence by ID."""

        stmt = select(EvidenceModel).where(EvidenceModel.evidence_id == evidence_id)
        result = await self.session.execute(stmt)
        evidence_model = result.scalar_one_or_none()

        if evidence_model is None:
            return None

        return self._to_domain_model(evidence_model)

    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Evidence], int]:
        """List evidence for a case."""

        # Build query
        conditions = [EvidenceModel.case_id == case_id]
        if evidence_type:
            conditions.append(EvidenceModel.evidence_type == evidence_type.value)

        where_clause = and_(*conditions)

        # Get total count
        count_stmt = select(func.count()).select_from(EvidenceModel).where(where_clause)
        count_result = await self.session.execute(count_stmt)
        total = count_result.scalar() or 0

        # Get paginated results
        stmt = (
            select(EvidenceModel)
            .where(where_clause)
            .order_by(EvidenceModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await self.session.execute(stmt)
        evidence_models = result.scalars().all()

        evidence_list = [self._to_domain_model(model) for model in evidence_models]

        return evidence_list, total

    async def update_evidence(self, evidence: Evidence) -> Evidence:
        """Update existing evidence."""

        evidence.updated_at = datetime.now(timezone.utc)

        stmt = (
            update(EvidenceModel)
            .where(EvidenceModel.evidence_id == evidence.evidence_id)
            .values(
                description=evidence.description,
                is_primary=evidence.is_primary,
                metadata=evidence.metadata,
                updated_at=evidence.updated_at,
            )
            .execution_options(synchronize_session=False)
        )

        result = await self.session.execute(stmt)

        if result.rowcount == 0:
            raise ValueError(f"Evidence {evidence.evidence_id} not found")

        await self.session.flush()
        return evidence

    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence by ID."""

        stmt = delete(EvidenceModel).where(EvidenceModel.evidence_id == evidence_id)
        result = await self.session.execute(stmt)
        await self.session.flush()

        return result.rowcount > 0

    async def get_primary_evidence(self, case_id: str) -> Optional[Evidence]:
        """Get primary/featured evidence for case."""

        stmt = select(EvidenceModel).where(
            and_(
                EvidenceModel.case_id == case_id,
                EvidenceModel.is_primary == True
            )
        )

        result = await self.session.execute(stmt)
        evidence_model = result.scalar_one_or_none()

        if evidence_model is None:
            return None

        return self._to_domain_model(evidence_model)

    async def set_primary_evidence(
        self,
        case_id: str,
        evidence_id: str
    ) -> bool:
        """Set primary/featured evidence for case."""

        # First, unset all primary flags for this case
        unset_stmt = (
            update(EvidenceModel)
            .where(EvidenceModel.case_id == case_id)
            .values(is_primary=False)
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(unset_stmt)

        # Then set the new primary evidence
        set_stmt = (
            update(EvidenceModel)
            .where(
                and_(
                    EvidenceModel.evidence_id == evidence_id,
                    EvidenceModel.case_id == case_id
                )
            )
            .values(is_primary=True)
            .execution_options(synchronize_session=False)
        )

        result = await self.session.execute(set_stmt)
        await self.session.flush()

        return result.rowcount > 0

    def _to_domain_model(self, model: EvidenceModel) -> Evidence:
        """Convert ORM model to domain model."""

        return Evidence(
            evidence_id=model.evidence_id,
            case_id=model.case_id,
            user_id=model.user_id,
            organization_id=model.organization_id,
            original_filename=model.original_filename,
            stored_filename=model.stored_filename,
            file_path=model.file_path,
            evidence_type=EvidenceType(model.evidence_type),
            mime_type=model.mime_type,
            file_size=model.file_size,
            storage_backend=StorageBackend(model.storage_backend),
            created_at=model.created_at,
            updated_at=model.updated_at,
            metadata=model.metadata,
            description=model.description,
            is_primary=model.is_primary,
        )
```

### In-Memory Repository Implementation

**File**: `faultmaven/infrastructure/persistence/in_memory_evidence_repository.py`

```python
"""In-memory evidence repository for testing."""

from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from copy import deepcopy

from faultmaven.models.evidence import Evidence, EvidenceType
from faultmaven.infrastructure.persistence.evidence_repository import EvidenceRepository


class InMemoryEvidenceRepository(EvidenceRepository):
    """In-memory implementation for testing."""

    def __init__(self):
        """Initialize in-memory storage."""
        self._evidence: Dict[str, Evidence] = {}

    async def create_evidence(self, evidence: Evidence) -> Evidence:
        """Create new evidence record."""

        if evidence.evidence_id in self._evidence:
            raise ValueError(f"Evidence {evidence.evidence_id} already exists")

        self._evidence[evidence.evidence_id] = deepcopy(evidence)
        return deepcopy(evidence)

    async def get_evidence(self, evidence_id: str) -> Optional[Evidence]:
        """Get evidence by ID."""

        evidence = self._evidence.get(evidence_id)
        return deepcopy(evidence) if evidence else None

    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Evidence], int]:
        """List evidence for a case."""

        # Filter by case
        evidence_list = [
            e for e in self._evidence.values()
            if e.case_id == case_id
        ]

        # Filter by type if specified
        if evidence_type:
            evidence_list = [
                e for e in evidence_list
                if e.evidence_type == evidence_type
            ]

        # Sort by created_at descending
        evidence_list.sort(key=lambda e: e.created_at, reverse=True)

        total = len(evidence_list)

        # Apply pagination
        paginated = evidence_list[offset:offset + limit]

        return [deepcopy(e) for e in paginated], total

    async def update_evidence(self, evidence: Evidence) -> Evidence:
        """Update existing evidence."""

        if evidence.evidence_id not in self._evidence:
            raise ValueError(f"Evidence {evidence.evidence_id} not found")

        evidence.updated_at = datetime.now(timezone.utc)
        self._evidence[evidence.evidence_id] = deepcopy(evidence)
        return deepcopy(evidence)

    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence by ID."""

        if evidence_id in self._evidence:
            del self._evidence[evidence_id]
            return True
        return False

    async def get_primary_evidence(self, case_id: str) -> Optional[Evidence]:
        """Get primary/featured evidence for case."""

        for evidence in self._evidence.values():
            if evidence.case_id == case_id and evidence.is_primary:
                return deepcopy(evidence)

        return None

    async def set_primary_evidence(
        self,
        case_id: str,
        evidence_id: str
    ) -> bool:
        """Set primary/featured evidence for case."""

        # Verify evidence exists and belongs to case
        evidence = self._evidence.get(evidence_id)
        if not evidence or evidence.case_id != case_id:
            return False

        # Unset all primary flags for this case
        for e in self._evidence.values():
            if e.case_id == case_id:
                e.is_primary = False

        # Set new primary
        self._evidence[evidence_id].is_primary = True
        self._evidence[evidence_id].updated_at = datetime.now(timezone.utc)

        return True
```

---

## Testing Requirements

### Unit Tests

**File**: `tests/unit/infrastructure/persistence/test_evidence_repository.py`

**Required Tests (80%+ coverage):**

1. **CRUD Operations:**
   - `test_create_evidence_success`
   - `test_create_evidence_duplicate_id_fails`
   - `test_create_evidence_invalid_case_id_fails`
   - `test_get_evidence_found`
   - `test_get_evidence_not_found`
   - `test_update_evidence_success`
   - `test_update_evidence_not_found_fails`
   - `test_delete_evidence_success`
   - `test_delete_evidence_not_found`

2. **Query Operations:**
   - `test_list_evidence_by_case_empty`
   - `test_list_evidence_by_case_multiple`
   - `test_list_evidence_by_case_with_type_filter`
   - `test_list_evidence_by_case_pagination`
   - `test_list_evidence_ordered_by_created_at`

3. **Primary Evidence:**
   - `test_get_primary_evidence_none_set`
   - `test_set_primary_evidence_success`
   - `test_set_primary_evidence_unsets_previous`
   - `test_set_primary_evidence_invalid_evidence_fails`

### Integration Tests

**File**: `tests/integration/test_evidence_integration.py`

**Required Tests:**

1. **Case-Evidence Relationship:**
   - `test_evidence_cascade_delete_on_case_delete`
   - `test_evidence_preserves_case_foreign_key`
   - `test_multiple_evidence_per_case`

2. **Database Transactions:**
   - `test_evidence_rollback_on_error`
   - `test_evidence_commit_persists_data`

3. **Real Database Operations:**
   - `test_evidence_with_large_metadata`
   - `test_evidence_with_unicode_filenames`
   - `test_evidence_concurrent_primary_updates`

---

## Performance Benchmarks

### Evidence Operation Benchmarks

**File**: `tests/benchmarks/test_evidence_operations.py`

```python
"""Benchmark evidence management operations."""

import pytest
import time
from datetime import datetime, timezone
from uuid import uuid4

from faultmaven.models.evidence import Evidence, EvidenceType, StorageBackend


@pytest.mark.benchmark
class TestEvidenceCreationPerformance:
    """Benchmark evidence creation operations."""

    @pytest.mark.asyncio
    async def test_single_evidence_creation_latency(
        self,
        evidence_repository,
        benchmark_session,
    ):
        """Measure latency of creating a single evidence record.

        Target: p95 < 150ms
        """
        evidence = Evidence(
            evidence_id=f"evidence_{uuid4().hex[:12]}",
            case_id="benchmark-case-001",
            user_id="benchmark-user-001",
            organization_id="benchmark-org-001",
            original_filename="screenshot.png",
            stored_filename=f"evidence_{uuid4().hex}.png",
            file_path="/evidence/2025/12/screenshot.png",
            evidence_type=EvidenceType.SCREENSHOT,
            mime_type="image/png",
            file_size=1024000,  # 1MB
        )

        start = time.perf_counter()
        result = await evidence_repository.create_evidence(evidence)
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.150, (
            f"Evidence creation latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n  Evidence creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_latency(
        self,
        evidence_repository,
        benchmark_session,
    ):
        """Measure latency of listing evidence for a case.

        Target: < 100ms for 20 evidence items
        """
        case_id = "benchmark-case-evidence-list"

        # Setup - Create 20 evidence records
        for i in range(20):
            evidence = Evidence(
                evidence_id=f"evidence_{uuid4().hex[:12]}",
                case_id=case_id,
                user_id="benchmark-user-001",
                organization_id="benchmark-org-001",
                original_filename=f"file_{i}.txt",
                stored_filename=f"evidence_{uuid4().hex}.txt",
                file_path=f"/evidence/2025/12/file_{i}.txt",
                evidence_type=EvidenceType.LOG_FILE,
                mime_type="text/plain",
                file_size=10000,
            )
            await evidence_repository.create_evidence(evidence)

        # Benchmark list operation
        start = time.perf_counter()
        result, total = await evidence_repository.list_evidence_by_case(
            case_id=case_id,
            limit=50,
        )
        latency = time.perf_counter() - start

        assert len(result) == 20
        assert total == 20
        assert latency < 0.100, (
            f"List evidence latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  List evidence latency: {latency*1000:.1f}ms ({len(result)} items)")
```

**Performance Targets:**
- Evidence creation: < 150ms p95
- Evidence retrieval: < 100ms p95
- List evidence by case: < 100ms for 20 items
- Delete evidence: < 100ms p95

---

## Deliverables

### Code Files

1. **Domain Model**
   - `faultmaven/models/evidence.py` - Evidence entity with EvidenceType/StorageBackend enums

2. **Database Migration**
   - `alembic/versions/20251229_1600_003_add_evidence_management.py` - Create evidence table + foreign key

3. **Repository Interface**
   - `faultmaven/infrastructure/persistence/evidence_repository.py` - Abstract repository interface

4. **Repository Implementations**
   - `faultmaven/infrastructure/persistence/database_evidence_repository.py` - SQLAlchemy implementation
   - `faultmaven/infrastructure/persistence/in_memory_evidence_repository.py` - In-memory for testing

5. **ORM Model**
   - Update `faultmaven/infrastructure/persistence/models.py` - Add EvidenceModel

6. **Repository Factory**
   - Update `faultmaven/infrastructure/persistence/factory.py` - Add evidence repository creation

### Test Files

7. **Unit Tests**
   - `tests/unit/infrastructure/persistence/test_evidence_repository.py` - Repository tests
   - `tests/unit/models/test_evidence.py` - Domain model tests

8. **Integration Tests**
   - `tests/integration/test_evidence_integration.py` - Database integration tests

9. **Performance Benchmarks**
   - `tests/benchmarks/test_evidence_operations.py` - Evidence operation benchmarks

### Documentation

10. **Update baseline file**
    - `.github/benchmark_baselines/baseline_v2.json` - Add evidence operation targets

11. **Update performance docs**
    - `docs/development/performance-testing.md` - Add evidence benchmarks section

---

## Acceptance Criteria

### Functional Requirements

- [x] Evidence domain model created with all required fields
- [x] EvidenceType enum supports all evidence types
- [x] StorageBackend enum supports multiple storage providers
- [x] Alembic migration creates evidence table successfully
- [x] Foreign key to cases table with CASCADE delete
- [x] Database repository implements all CRUD operations
- [x] In-memory repository implements all CRUD operations
- [x] Primary evidence management works correctly
- [x] Repository factory creates evidence repositories

### Testing Requirements

- [x] Unit tests achieve 80%+ coverage
- [x] Integration tests verify case-evidence relationship
- [x] Integration tests verify CASCADE delete
- [x] Performance benchmarks added
- [x] All tests pass locally
- [x] All tests pass in CI/CD

### Code Quality

- [x] Type hints on all public functions
- [x] Docstrings follow Google style
- [x] No hardcoded credentials or secrets
- [x] Error handling with specific exceptions
- [x] Transaction rollback on errors
- [x] Logging for debugging

---

## Migration Notes

**Migration Order:** 003 (depends on 002 - session management)

**Data Migration:** None required (new table)

**Rollback Plan:**
```bash
# Rollback evidence table
alembic downgrade -1
```

---

## Future Enhancements

After TASK-006:

1. **TASK-007**: File storage abstraction (local filesystem implementation)
2. **TASK-008**: S3 storage backend (enterprise edition)
3. **TASK-009**: Evidence search and filtering
4. **TASK-010**: Evidence thumbnail generation
5. **TASK-011**: Evidence virus scanning

---

## Questions?

- **Why CASCADE delete?** Evidence has no value without its case (strong ownership)
- **Why separate stored_filename?** Original filename may conflict, stored filename ensures uniqueness
- **Why storage_backend enum?** Enables future cloud storage (S3, Azure, GCS)
- **Why is_primary flag?** Allows featured evidence for case summary displays
- **Why BigInteger for file_size?** Supports large video/HAR files (> 2GB)

---

**Ready to implement?** Follow TASK-002 (Case Repository) as the reference implementation pattern. All repository patterns should be consistent across the codebase.

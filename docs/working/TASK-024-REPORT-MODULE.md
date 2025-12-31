# TASK-024: Report Module Implementation (CRITICAL Endpoints)

## Task Metadata
- **Phase**: Phase 0 - Week 2 (API Feature Parity)
- **Priority**: P0 (CRITICAL - User-facing feature)
- **Estimated Time**: 4 weeks (20 working days)
- **Dependencies**:
  - TASK-023 (TenantProvider) - ✅ MERGED (PR #26)
  - TASK-020 (Remove Legacy Headers) - ✅ COMPLETE
  - TASK-021 (Organization Management) - ✅ MERGED (PR #23)
- **Assignee**: Backend Engineer + AI Specialist
- **Reports To**: Solutions Architect
- **Scope**: 7 CRITICAL endpoints, LLM integration, 50+ tests

---

## Executive Summary

**Objective**: Implement the complete Report Module with 7 CRITICAL endpoints to enable post-mortem report generation, a core FaultMaven troubleshooting workflow feature.

**Business Value**:
- **Compliance Documentation**: Enterprise customers require formal incident reports for audits
- **Knowledge Capture**: Reports distill investigation findings into reusable documentation
- **Case Closure**: Links troubleshooting cases to final resolution documents
- **Time Savings**: LLM-powered report generation reduces manual documentation by 80%

**Strategic Context**:
This is the **first of 43 missing CRITICAL/HIGH priority endpoints** identified in the Platform Evolution Strategy. Successful completion demonstrates:
- Deployment-neutral architecture (uses TenantProvider)
- LLM integration with shim pattern (graceful degradation)
- Multi-tenant isolation enforcement
- Production-ready patterns for remaining 36 endpoints

**Success Criteria**:
- ✅ 7 REST endpoints fully implemented
- ✅ 50+ tests passing (unit, integration, E2E)
- ✅ LLM integration with existing agentic framework
- ✅ PII redaction with shim pattern (works without Presidio)
- ✅ Multi-tenant isolation enforced
- ✅ 90%+ test coverage

---

## Context

### Why Reports are CRITICAL

Reports are the **final deliverable** of the FaultMaven troubleshooting workflow:

1. **Investigation Phase**: User creates case, uploads evidence, AI generates hypotheses
2. **Resolution Phase**: User validates solution, marks case resolved
3. **Documentation Phase**: **Report generation** creates formal post-mortem
4. **Knowledge Capture**: Report feeds back into knowledge base for future cases

**Current Gap**: Users can investigate and resolve cases, but cannot generate formal reports. This blocks enterprise adoption where compliance documentation is mandatory.

### Integration with Existing Architecture

**Agentic Framework Integration**:
- FaultMaven-Mono already has a 7-component agentic framework (OODA loop, LangGraph)
- Report generation uses **existing LLM providers** (7 providers with fallback chains)
- Report templates leverage **existing prompt engineering patterns**

**Services to Integrate**:
- `CaseService` - Fetch case context (description, evidence, hypotheses, solutions)
- `EvidenceService` - Include evidence metadata in reports
- `TenantProvider` - Multi-tenant isolation (local vs cloud)
- `PIIRedactor` - Redact sensitive data before LLM processing (shim pattern)
- `LLMProvider` - Generate report content (OpenAI, Anthropic, etc.)

**Repository Pattern**:
- `ReportRepository` - CRUD operations for reports table
- `ReportVersionRepository` - Manage report versions (max 5 per type)

---

## Technical Specification

### 1. Database Schema (Alembic Migration)

**Migration File**: `alembic/versions/20250101_add_reports.py`

```python
"""Add reports and report_versions tables

Revision ID: 20250101_001
Revises: 20241231_003  # Last migration from TASK-023
Create Date: 2025-01-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
from datetime import datetime, timezone

revision = '20250101_001'
down_revision = '20241231_003'
branch_labels = None
depends_on = None


def upgrade():
    """Create reports and report_versions tables."""

    # Reports table
    op.create_table(
        'reports',
        sa.Column('report_id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.organization_id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('case_id', UUID(as_uuid=True), sa.ForeignKey('cases.case_id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('report_type', sa.String(50), nullable=False),  # 'post_mortem', 'executive_summary', 'technical_analysis'
        sa.Column('content', JSONB(), nullable=False),  # Structured report content (sections, findings, recommendations)
        sa.Column('status', sa.String(50), nullable=False, server_default='draft'),  # 'draft', 'published', 'archived'
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('metadata', JSONB(), nullable=True),  # Additional metadata (LLM model used, generation time, etc.)
        sa.Column('created_by', UUID(as_uuid=True), sa.ForeignKey('users.user_id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('case_id', 'report_type', 'version', name='uq_case_type_version')
    )

    # Report versions table (for version history tracking)
    op.create_table(
        'report_versions',
        sa.Column('version_id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('report_id', UUID(as_uuid=True), sa.ForeignKey('reports.report_id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('content', JSONB(), nullable=False),
        sa.Column('created_by', UUID(as_uuid=True), sa.ForeignKey('users.user_id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('change_summary', sa.Text(), nullable=True),  # Optional description of changes
        sa.UniqueConstraint('report_id', 'version_number', name='uq_report_version_number')
    )

    # Indexes for performance
    op.create_index('idx_reports_created_at', 'reports', ['created_at'])
    op.create_index('idx_reports_status', 'reports', ['status'])
    op.create_index('idx_reports_org_case', 'reports', ['organization_id', 'case_id'])


def downgrade():
    """Drop reports and report_versions tables."""
    op.drop_index('idx_reports_org_case', table_name='reports')
    op.drop_index('idx_reports_status', table_name='reports')
    op.drop_index('idx_reports_created_at', table_name='reports')
    op.drop_table('report_versions')
    op.drop_table('reports')
```

---

### 2. Domain Models

**File**: `faultmaven/models/report.py`

```python
"""Report domain models.

Represents post-mortem reports generated from troubleshooting cases.
Supports multiple report types with version history and LLM-powered generation.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class ReportType(str, Enum):
    """Report types supported by FaultMaven."""

    POST_MORTEM = "post_mortem"  # Detailed incident analysis
    EXECUTIVE_SUMMARY = "executive_summary"  # High-level summary for stakeholders
    TECHNICAL_ANALYSIS = "technical_analysis"  # Deep technical investigation


class ReportStatus(str, Enum):
    """Report lifecycle status."""

    DRAFT = "draft"  # Being edited
    PUBLISHED = "published"  # Finalized and shared
    ARCHIVED = "archived"  # Historical record


class ReportSection(BaseModel):
    """Structured section within a report."""

    title: str = Field(..., description="Section heading")
    content: str = Field(..., description="Section content (markdown supported)")
    order: int = Field(..., description="Display order")
    subsections: Optional[List['ReportSection']] = Field(default=None, description="Nested subsections")


class ReportContent(BaseModel):
    """Structured report content (stored as JSONB)."""

    summary: str = Field(..., description="Executive summary (2-3 paragraphs)")
    sections: List[ReportSection] = Field(..., description="Report sections")
    key_findings: List[str] = Field(default_factory=list, description="Bulleted key findings")
    recommendations: List[str] = Field(default_factory=list, description="Actionable recommendations")
    timeline: Optional[List[Dict[str, Any]]] = Field(default=None, description="Event timeline")
    root_cause: Optional[str] = Field(default=None, description="Identified root cause")

    @field_validator('summary')
    @classmethod
    def validate_summary_length(cls, v: str) -> str:
        """Ensure summary is concise but meaningful."""
        if len(v) < 50:
            raise ValueError("Summary must be at least 50 characters")
        if len(v) > 5000:
            raise ValueError("Summary must not exceed 5000 characters")
        return v


class Report(BaseModel):
    """Report domain entity.

    Represents a generated report linked to a troubleshooting case.
    Supports versioning, multi-tenant isolation, and structured content.
    """

    report_id: UUID = Field(default_factory=uuid4, description="Unique report identifier")
    organization_id: UUID = Field(..., description="Organization owning this report (multi-tenant isolation)")
    case_id: UUID = Field(..., description="Associated case")
    title: str = Field(..., min_length=1, max_length=255, description="Report title")
    report_type: ReportType = Field(..., description="Report type")
    content: ReportContent = Field(..., description="Structured report content")
    status: ReportStatus = Field(default=ReportStatus.DRAFT, description="Report status")
    version: int = Field(default=1, ge=1, description="Report version number")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Additional metadata")
    created_by: Optional[UUID] = Field(default=None, description="User who created the report")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: Optional[datetime] = Field(default=None, description="When report was published")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "report_id": "550e8400-e29b-41d4-a716-446655440000",
                "organization_id": "660e8400-e29b-41d4-a716-446655440000",
                "case_id": "770e8400-e29b-41d4-a716-446655440000",
                "title": "Database Performance Degradation - Post Mortem",
                "report_type": "post_mortem",
                "content": {
                    "summary": "On Jan 1, 2025, database queries slowed by 400% due to missing index...",
                    "sections": [
                        {"title": "Incident Overview", "content": "...", "order": 1}
                    ],
                    "key_findings": ["Missing index on users.email", "N+1 query in login endpoint"],
                    "recommendations": ["Add composite index", "Implement query caching"]
                },
                "status": "published",
                "version": 1
            }
        }


class ReportVersion(BaseModel):
    """Report version history entry."""

    version_id: UUID = Field(default_factory=uuid4)
    report_id: UUID = Field(...)
    version_number: int = Field(..., ge=1)
    content: ReportContent = Field(...)
    created_by: Optional[UUID] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    change_summary: Optional[str] = Field(default=None, max_length=1000)

    class Config:
        from_attributes = True


# Pydantic Request/Response Models

class ReportCreateRequest(BaseModel):
    """Request model for creating a report."""

    case_id: UUID = Field(..., description="Case to generate report for")
    report_type: ReportType = Field(..., description="Type of report to generate")
    title: Optional[str] = Field(default=None, description="Report title (auto-generated if not provided)")
    generate_with_llm: bool = Field(default=True, description="Use LLM to generate content")


class ReportUpdateRequest(BaseModel):
    """Request model for updating a report."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    content: Optional[ReportContent] = Field(default=None)
    status: Optional[ReportStatus] = Field(default=None)
    metadata: Optional[Dict[str, Any]] = Field(default=None)


class ReportResponse(BaseModel):
    """Response model for report endpoints."""

    report_id: UUID
    organization_id: UUID
    case_id: UUID
    title: str
    report_type: ReportType
    content: ReportContent
    status: ReportStatus
    version: int
    metadata: Optional[Dict[str, Any]]
    created_by: Optional[UUID]
    created_at: datetime
    updated_at: datetime
    published_at: Optional[datetime]

    @classmethod
    def from_domain(cls, report: Report) -> 'ReportResponse':
        """Convert domain model to response model."""
        return cls(
            report_id=report.report_id,
            organization_id=report.organization_id,
            case_id=report.case_id,
            title=report.title,
            report_type=report.report_type,
            content=report.content,
            status=report.status,
            version=report.version,
            metadata=report.metadata,
            created_by=report.created_by,
            created_at=report.created_at,
            updated_at=report.updated_at,
            published_at=report.published_at
        )

    class Config:
        from_attributes = True


class ReportVersionResponse(BaseModel):
    """Response model for report version history."""

    version_id: UUID
    report_id: UUID
    version_number: int
    content: ReportContent
    created_by: Optional[UUID]
    created_at: datetime
    change_summary: Optional[str]

    @classmethod
    def from_domain(cls, version: ReportVersion) -> 'ReportVersionResponse':
        """Convert domain model to response model."""
        return cls(
            version_id=version.version_id,
            report_id=version.report_id,
            version_number=version.version_number,
            content=version.content,
            created_by=version.created_by,
            created_at=version.created_at,
            change_summary=version.change_summary
        )

    class Config:
        from_attributes = True


class ReportListResponse(BaseModel):
    """Paginated list of reports."""

    reports: List[ReportResponse]
    total: int = Field(..., description="Total number of reports matching filters")
    limit: int
    offset: int
```

---

### 3. Repository Layer

**File**: `faultmaven/repositories/report_repository.py`

```python
"""Report repository for data access.

Handles CRUD operations for reports with multi-tenant isolation.
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from faultmaven.models.report import Report, ReportType, ReportStatus
from faultmaven.db.models import ReportModel  # SQLAlchemy ORM model
from faultmaven.exceptions import NotFoundError, ValidationException


class ReportRepository:
    """Repository for Report entity."""

    def __init__(self, session_factory):
        """Initialize with async session factory.

        Args:
            session_factory: Callable that returns AsyncSession
        """
        self.session_factory = session_factory

    async def create(self, report: Report) -> Report:
        """Create a new report.

        Args:
            report: Report domain model

        Returns:
            Created report

        Raises:
            ValidationException: If validation fails
        """
        async with self.session_factory() as session:
            # Convert domain model to ORM model
            db_report = ReportModel(
                report_id=report.report_id,
                organization_id=report.organization_id,
                case_id=report.case_id,
                title=report.title,
                report_type=report.report_type.value,
                content=report.content.model_dump(),  # JSONB field
                status=report.status.value,
                version=report.version,
                metadata=report.metadata,
                created_by=report.created_by,
                created_at=report.created_at,
                updated_at=report.updated_at,
                published_at=report.published_at
            )

            session.add(db_report)
            await session.commit()
            await session.refresh(db_report)

            return self._to_domain(db_report)

    async def get_by_id(
        self,
        report_id: UUID,
        organization_id: UUID
    ) -> Optional[Report]:
        """Get report by ID with multi-tenant isolation.

        Args:
            report_id: Report UUID
            organization_id: Organization UUID (for isolation)

        Returns:
            Report if found, None otherwise
        """
        async with self.session_factory() as session:
            stmt = select(ReportModel).where(
                and_(
                    ReportModel.report_id == report_id,
                    ReportModel.organization_id == organization_id
                )
            )
            result = await session.execute(stmt)
            db_report = result.scalar_one_or_none()

            return self._to_domain(db_report) if db_report else None

    async def list_by_case(
        self,
        case_id: UUID,
        organization_id: UUID,
        limit: int = 100,
        offset: int = 0
    ) -> Tuple[List[Report], int]:
        """List all reports for a case.

        Args:
            case_id: Case UUID
            organization_id: Organization UUID (for isolation)
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (reports list, total count)
        """
        async with self.session_factory() as session:
            # Query with filters
            base_stmt = select(ReportModel).where(
                and_(
                    ReportModel.case_id == case_id,
                    ReportModel.organization_id == organization_id
                )
            )

            # Count total
            count_stmt = select(func.count()).select_from(base_stmt.subquery())
            total = await session.scalar(count_stmt)

            # Paginated results
            stmt = (
                base_stmt
                .order_by(ReportModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            db_reports = result.scalars().all()

            reports = [self._to_domain(r) for r in db_reports]
            return reports, total or 0

    async def update(self, report: Report) -> Report:
        """Update existing report.

        Args:
            report: Updated report domain model

        Returns:
            Updated report

        Raises:
            NotFoundError: If report doesn't exist
        """
        async with self.session_factory() as session:
            stmt = select(ReportModel).where(
                and_(
                    ReportModel.report_id == report.report_id,
                    ReportModel.organization_id == report.organization_id
                )
            )
            result = await session.execute(stmt)
            db_report = result.scalar_one_or_none()

            if not db_report:
                raise NotFoundError(f"Report {report.report_id} not found")

            # Update fields
            db_report.title = report.title
            db_report.content = report.content.model_dump()
            db_report.status = report.status.value
            db_report.metadata = report.metadata
            db_report.updated_at = datetime.now(timezone.utc)

            if report.status == ReportStatus.PUBLISHED and not db_report.published_at:
                db_report.published_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(db_report)

            return self._to_domain(db_report)

    async def delete(self, report_id: UUID, organization_id: UUID) -> bool:
        """Delete report.

        Args:
            report_id: Report UUID
            organization_id: Organization UUID (for isolation)

        Returns:
            True if deleted, False if not found
        """
        async with self.session_factory() as session:
            stmt = select(ReportModel).where(
                and_(
                    ReportModel.report_id == report_id,
                    ReportModel.organization_id == organization_id
                )
            )
            result = await session.execute(stmt)
            db_report = result.scalar_one_or_none()

            if not db_report:
                return False

            await session.delete(db_report)
            await session.commit()
            return True

    async def count_by_type(
        self,
        case_id: UUID,
        report_type: ReportType,
        organization_id: UUID
    ) -> int:
        """Count reports of a specific type for a case.

        Used to enforce max 5 versions per type limit.

        Args:
            case_id: Case UUID
            report_type: Report type
            organization_id: Organization UUID

        Returns:
            Count of reports
        """
        async with self.session_factory() as session:
            stmt = select(func.count()).where(
                and_(
                    ReportModel.case_id == case_id,
                    ReportModel.report_type == report_type.value,
                    ReportModel.organization_id == organization_id
                )
            )
            count = await session.scalar(stmt)
            return count or 0

    def _to_domain(self, db_report: ReportModel) -> Report:
        """Convert ORM model to domain model.

        Args:
            db_report: SQLAlchemy ORM model

        Returns:
            Report domain model
        """
        from faultmaven.models.report import ReportContent, ReportType, ReportStatus

        return Report(
            report_id=db_report.report_id,
            organization_id=db_report.organization_id,
            case_id=db_report.case_id,
            title=db_report.title,
            report_type=ReportType(db_report.report_type),
            content=ReportContent(**db_report.content),
            status=ReportStatus(db_report.status),
            version=db_report.version,
            metadata=db_report.metadata,
            created_by=db_report.created_by,
            created_at=db_report.created_at,
            updated_at=db_report.updated_at,
            published_at=db_report.published_at
        )
```

**File**: `faultmaven/repositories/report_version_repository.py`

```python
"""Report version repository for version history."""

from typing import List
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.models.report import ReportVersion, ReportContent
from faultmaven.db.models import ReportVersionModel


class ReportVersionRepository:
    """Repository for ReportVersion entity."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create(self, version: ReportVersion) -> ReportVersion:
        """Create version history entry."""
        async with self.session_factory() as session:
            db_version = ReportVersionModel(
                version_id=version.version_id,
                report_id=version.report_id,
                version_number=version.version_number,
                content=version.content.model_dump(),
                created_by=version.created_by,
                created_at=version.created_at,
                change_summary=version.change_summary
            )

            session.add(db_version)
            await session.commit()
            await session.refresh(db_version)

            return self._to_domain(db_version)

    async def list_by_report(self, report_id: UUID) -> List[ReportVersion]:
        """List all versions for a report."""
        async with self.session_factory() as session:
            stmt = (
                select(ReportVersionModel)
                .where(ReportVersionModel.report_id == report_id)
                .order_by(ReportVersionModel.version_number.desc())
            )
            result = await session.execute(stmt)
            db_versions = result.scalars().all()

            return [self._to_domain(v) for v in db_versions]

    def _to_domain(self, db_version: ReportVersionModel) -> ReportVersion:
        """Convert ORM model to domain model."""
        return ReportVersion(
            version_id=db_version.version_id,
            report_id=db_version.report_id,
            version_number=db_version.version_number,
            content=ReportContent(**db_version.content),
            created_by=db_version.created_by,
            created_at=db_version.created_at,
            change_summary=db_version.change_summary
        )
```

---

### 4. Service Layer with LLM Integration

**File**: `faultmaven/services/report_service.py`

```python
"""Report service with LLM-powered generation.

Orchestrates report creation, version management, and LLM integration.
Uses shim pattern for graceful degradation of enterprise features.
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

from faultmaven.models.report import (
    Report,
    ReportVersion,
    ReportCreateRequest,
    ReportUpdateRequest,
    ReportType,
    ReportStatus,
    ReportContent,
    ReportSection
)
from faultmaven.models.user import User
from faultmaven.repositories.report_repository import ReportRepository
from faultmaven.repositories.report_version_repository import ReportVersionRepository
from faultmaven.repositories.case_repository import CaseRepository
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.exceptions import (
    NotFoundError,
    ValidationException,
    AuthorizationError
)

# Shim imports for graceful degradation
try:
    from faultmaven.infrastructure.shims.observability import track
    TRACING_AVAILABLE = True
except ImportError:
    def track(name: str):
        return lambda func: func
    TRACING_AVAILABLE = False

try:
    from faultmaven.infrastructure.shims.pii import PIIRedactor
    PII_REDACTION_AVAILABLE = True
except ImportError:
    class PIIRedactor:
        def redact(self, text: str) -> str:
            return text
    PII_REDACTION_AVAILABLE = False


class ReportService:
    """Service for report operations.

    Handles report CRUD, LLM-powered generation, and version management.
    Deployment-neutral via TenantProvider.
    """

    MAX_VERSIONS_PER_TYPE = 5

    def __init__(
        self,
        report_repository: ReportRepository,
        version_repository: ReportVersionRepository,
        case_repository: CaseRepository,
        tenant_provider: TenantProvider,
        llm_provider,  # Existing LLM provider from agentic framework
        pii_redactor: Optional[PIIRedactor] = None
    ):
        self.report_repo = report_repository
        self.version_repo = version_repository
        self.case_repo = case_repository
        self.tenant_provider = tenant_provider
        self.llm = llm_provider
        self.pii = pii_redactor or PIIRedactor()

    @track("report_generation")
    async def create_report(
        self,
        request: ReportCreateRequest,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> Report:
        """Create a new report.

        Args:
            request: Report creation request
            current_user: Authenticated user
            organization_id: Optional org ID (for multi-tenant)

        Returns:
            Created report

        Raises:
            NotFoundError: If case doesn't exist
            ValidationException: If max versions exceeded
            AuthorizationError: If user not member of organization
        """
        # Resolve organization context (deployment-neutral)
        organization = await self.tenant_provider.get_current_organization(
            current_user=current_user,
            organization_id=organization_id
        )

        # Verify case exists and user has access
        case = await self.case_repo.get_by_id(
            case_id=request.case_id,
            organization_id=organization.organization_id
        )
        if not case:
            raise NotFoundError(f"Case {request.case_id} not found")

        # Check version limit
        existing_count = await self.report_repo.count_by_type(
            case_id=request.case_id,
            report_type=request.report_type,
            organization_id=organization.organization_id
        )
        if existing_count >= self.MAX_VERSIONS_PER_TYPE:
            raise ValidationException(
                f"Maximum {self.MAX_VERSIONS_PER_TYPE} reports per type exceeded"
            )

        # Generate content
        if request.generate_with_llm:
            content = await self._generate_content_with_llm(
                case=case,
                report_type=request.report_type
            )
        else:
            # Empty template for manual editing
            content = self._create_empty_template(request.report_type)

        # Create report
        report = Report(
            report_id=uuid4(),
            organization_id=organization.organization_id,
            case_id=request.case_id,
            title=request.title or self._generate_title(case, request.report_type),
            report_type=request.report_type,
            content=content,
            status=ReportStatus.DRAFT,
            version=existing_count + 1,
            created_by=current_user.user_id,
            metadata={
                "llm_generated": request.generate_with_llm,
                "model_used": self.llm.model_name if request.generate_with_llm else None,
                "pii_redacted": PII_REDACTION_AVAILABLE
            }
        )

        created_report = await self.report_repo.create(report)

        # Create initial version history
        await self._create_version_history(
            report=created_report,
            change_summary="Initial report creation",
            user_id=current_user.user_id
        )

        return created_report

    async def get_report(
        self,
        report_id: UUID,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> Report:
        """Get report by ID.

        Args:
            report_id: Report UUID
            current_user: Authenticated user
            organization_id: Optional org ID (for multi-tenant)

        Returns:
            Report

        Raises:
            NotFoundError: If report doesn't exist
            AuthorizationError: If user not authorized
        """
        organization = await self.tenant_provider.get_current_organization(
            current_user=current_user,
            organization_id=organization_id
        )

        report = await self.report_repo.get_by_id(
            report_id=report_id,
            organization_id=organization.organization_id
        )

        if not report:
            raise NotFoundError(f"Report {report_id} not found")

        return report

    async def update_report(
        self,
        report_id: UUID,
        request: ReportUpdateRequest,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> Report:
        """Update existing report.

        Args:
            report_id: Report UUID
            request: Update request
            current_user: Authenticated user
            organization_id: Optional org ID

        Returns:
            Updated report

        Raises:
            NotFoundError: If report doesn't exist
        """
        # Get existing report
        report = await self.get_report(
            report_id=report_id,
            current_user=current_user,
            organization_id=organization_id
        )

        # Track changes for version history
        changes = []

        # Update fields
        if request.title is not None:
            report.title = request.title
            changes.append("title")

        if request.content is not None:
            report.content = request.content
            changes.append("content")

        if request.status is not None:
            old_status = report.status
            report.status = request.status
            changes.append(f"status ({old_status} → {request.status})")

            # Set published_at when transitioning to published
            if request.status == ReportStatus.PUBLISHED and old_status != ReportStatus.PUBLISHED:
                report.published_at = datetime.now(timezone.utc)

        if request.metadata is not None:
            report.metadata = {**(report.metadata or {}), **request.metadata}
            changes.append("metadata")

        report.updated_at = datetime.now(timezone.utc)

        # Save update
        updated_report = await self.report_repo.update(report)

        # Create version history if content changed
        if "content" in changes:
            await self._create_version_history(
                report=updated_report,
                change_summary=f"Updated: {', '.join(changes)}",
                user_id=current_user.user_id
            )

        return updated_report

    async def delete_report(
        self,
        report_id: UUID,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> bool:
        """Delete report.

        Args:
            report_id: Report UUID
            current_user: Authenticated user
            organization_id: Optional org ID

        Returns:
            True if deleted

        Raises:
            NotFoundError: If report doesn't exist
        """
        # Verify report exists and user has access
        report = await self.get_report(
            report_id=report_id,
            current_user=current_user,
            organization_id=organization_id
        )

        # Delete report (cascade will delete versions)
        deleted = await self.report_repo.delete(
            report_id=report.report_id,
            organization_id=report.organization_id
        )

        return deleted

    async def list_reports_for_case(
        self,
        case_id: UUID,
        current_user: User,
        organization_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> Tuple[List[Report], int]:
        """List all reports for a case.

        Args:
            case_id: Case UUID
            current_user: Authenticated user
            organization_id: Optional org ID
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (reports list, total count)
        """
        organization = await self.tenant_provider.get_current_organization(
            current_user=current_user,
            organization_id=organization_id
        )

        # Verify case exists
        case = await self.case_repo.get_by_id(
            case_id=case_id,
            organization_id=organization.organization_id
        )
        if not case:
            raise NotFoundError(f"Case {case_id} not found")

        return await self.report_repo.list_by_case(
            case_id=case_id,
            organization_id=organization.organization_id,
            limit=limit,
            offset=offset
        )

    async def get_report_versions(
        self,
        report_id: UUID,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> List[ReportVersion]:
        """Get version history for a report.

        Args:
            report_id: Report UUID
            current_user: Authenticated user
            organization_id: Optional org ID

        Returns:
            List of report versions
        """
        # Verify report exists and user has access
        await self.get_report(
            report_id=report_id,
            current_user=current_user,
            organization_id=organization_id
        )

        return await self.version_repo.list_by_report(report_id)

    # LLM Integration Methods

    async def _generate_content_with_llm(
        self,
        case,
        report_type: ReportType
    ) -> ReportContent:
        """Generate report content using LLM.

        Args:
            case: Case domain model
            report_type: Type of report to generate

        Returns:
            Generated report content
        """
        # Gather case context
        case_context = await self._build_case_context(case)

        # Redact PII before sending to LLM
        safe_context = self.pii.redact(case_context)

        # Select template and generate
        template = self._get_report_template(report_type)

        # Call LLM (uses existing agentic framework)
        generated_content = await self.llm.generate(
            prompt=template,
            context=safe_context,
            temperature=0.3,  # Lower temperature for factual reports
            max_tokens=4000
        )

        # Parse LLM output into structured content
        content = self._parse_llm_output(generated_content, report_type)

        return content

    async def _build_case_context(self, case) -> str:
        """Build context string for LLM.

        Args:
            case: Case domain model

        Returns:
            Context string with case details
        """
        context_parts = [
            f"Case Title: {case.title}",
            f"Case Description: {case.description}",
            f"Status: {case.status}",
            f"Severity: {case.severity}",
            f"Created: {case.created_at.isoformat()}",
        ]

        # Add evidence metadata (not full content)
        # TODO: Integrate with EvidenceRepository when implementing evidence endpoints
        # For now, use placeholder
        context_parts.append("Evidence: [Evidence integration pending]")

        # Add hypotheses and solutions (if available)
        # TODO: Integrate with HypothesisRepository (TASK-026)
        context_parts.append("Hypotheses: [Hypothesis integration pending]")

        return "\n\n".join(context_parts)

    def _get_report_template(self, report_type: ReportType) -> str:
        """Get LLM prompt template for report type.

        Args:
            report_type: Type of report

        Returns:
            Prompt template
        """
        templates = {
            ReportType.POST_MORTEM: """
Generate a detailed post-mortem report for the following incident:

{context}

The report should include:
1. Executive Summary (2-3 paragraphs)
2. Timeline of events
3. Root cause analysis
4. Key findings (bulleted)
5. Recommendations (bulleted)
6. Lessons learned

Format the output as structured JSON with sections.
""",
            ReportType.EXECUTIVE_SUMMARY: """
Generate an executive summary for the following incident:

{context}

The summary should:
1. Be concise (500-1000 words)
2. Focus on business impact
3. Highlight key findings
4. Provide actionable recommendations
5. Avoid technical jargon

Format as structured JSON.
""",
            ReportType.TECHNICAL_ANALYSIS: """
Generate a technical deep-dive analysis for:

{context}

The analysis should include:
1. Technical root cause
2. System architecture context
3. Code/configuration issues
4. Performance impact
5. Technical recommendations
6. Prevention strategies

Format as structured JSON with technical sections.
"""
        }

        return templates.get(report_type, templates[ReportType.POST_MORTEM])

    def _parse_llm_output(self, llm_output: str, report_type: ReportType) -> ReportContent:
        """Parse LLM JSON output into ReportContent.

        Args:
            llm_output: LLM generated JSON string
            report_type: Report type

        Returns:
            Structured ReportContent
        """
        import json

        try:
            parsed = json.loads(llm_output)
        except json.JSONDecodeError:
            # Fallback if LLM didn't return valid JSON
            return self._create_empty_template(report_type)

        # Extract sections
        sections = []
        for i, section_data in enumerate(parsed.get("sections", [])):
            sections.append(ReportSection(
                title=section_data.get("title", f"Section {i+1}"),
                content=section_data.get("content", ""),
                order=i + 1
            ))

        return ReportContent(
            summary=parsed.get("summary", ""),
            sections=sections,
            key_findings=parsed.get("key_findings", []),
            recommendations=parsed.get("recommendations", []),
            timeline=parsed.get("timeline"),
            root_cause=parsed.get("root_cause")
        )

    def _create_empty_template(self, report_type: ReportType) -> ReportContent:
        """Create empty report template for manual editing.

        Args:
            report_type: Report type

        Returns:
            Empty ReportContent template
        """
        sections = [
            ReportSection(title="Overview", content="", order=1),
            ReportSection(title="Analysis", content="", order=2),
            ReportSection(title="Recommendations", content="", order=3)
        ]

        return ReportContent(
            summary="",
            sections=sections,
            key_findings=[],
            recommendations=[]
        )

    def _generate_title(self, case, report_type: ReportType) -> str:
        """Generate report title from case.

        Args:
            case: Case domain model
            report_type: Report type

        Returns:
            Generated title
        """
        type_labels = {
            ReportType.POST_MORTEM: "Post-Mortem",
            ReportType.EXECUTIVE_SUMMARY: "Executive Summary",
            ReportType.TECHNICAL_ANALYSIS: "Technical Analysis"
        }

        return f"{case.title} - {type_labels[report_type]}"

    async def _create_version_history(
        self,
        report: Report,
        change_summary: str,
        user_id: UUID
    ) -> ReportVersion:
        """Create version history entry.

        Args:
            report: Report domain model
            change_summary: Summary of changes
            user_id: User who made changes

        Returns:
            Created version
        """
        version = ReportVersion(
            version_id=uuid4(),
            report_id=report.report_id,
            version_number=report.version,
            content=report.content,
            created_by=user_id,
            change_summary=change_summary
        )

        return await self.version_repo.create(version)
```

---

### 5. API Endpoints

**File**: `faultmaven/api/v1/reports.py`

```python
"""Report API endpoints.

Provides REST API for report CRUD operations with LLM-powered generation.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status

from faultmaven.api.middleware.auth import get_current_user
from faultmaven.models.user import User
from faultmaven.models.report import (
    ReportCreateRequest,
    ReportUpdateRequest,
    ReportResponse,
    ReportListResponse,
    ReportVersionResponse
)
from faultmaven.services.report_service import ReportService
from faultmaven.dependencies import get_report_service
from faultmaven.exceptions import NotFoundError, ValidationException, AuthorizationError


router = APIRouter(prefix="/reports", tags=["reports"])


@router.post(
    "/generate",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a report",
    description="Generate a post-mortem, executive summary, or technical analysis report for a case using LLM"
)
async def generate_report(
    request: ReportCreateRequest,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    report_service: ReportService = Depends(get_report_service)
) -> ReportResponse:
    """Generate a report for a case.

    Supports:
    - LLM-powered content generation
    - Manual template creation
    - Post-mortem, executive summary, and technical analysis types
    - Multi-tenant isolation

    Args:
        request: Report creation request
        current_user: Authenticated user (from JWT)
        x_organization_id: Optional organization ID (multi-tenant mode)
        report_service: Injected report service

    Returns:
        Created report

    Raises:
        404: Case not found
        400: Max versions per type exceeded
        403: User not authorized
    """
    try:
        report = await report_service.create_report(
            request=request,
            current_user=current_user,
            organization_id=x_organization_id
        )
        return ReportResponse.from_domain(report)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Get report by ID",
    description="Retrieve a specific report by its UUID"
)
async def get_report(
    report_id: UUID,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    report_service: ReportService = Depends(get_report_service)
) -> ReportResponse:
    """Get report by ID.

    Args:
        report_id: Report UUID
        current_user: Authenticated user
        x_organization_id: Optional organization ID
        report_service: Injected report service

    Returns:
        Report

    Raises:
        404: Report not found
        403: User not authorized
    """
    try:
        report = await report_service.get_report(
            report_id=report_id,
            current_user=current_user,
            organization_id=x_organization_id
        )
        return ReportResponse.from_domain(report)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.put(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Update report",
    description="Update report title, content, status, or metadata"
)
async def update_report(
    report_id: UUID,
    request: ReportUpdateRequest,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    report_service: ReportService = Depends(get_report_service)
) -> ReportResponse:
    """Update existing report.

    Args:
        report_id: Report UUID
        request: Update request (partial update supported)
        current_user: Authenticated user
        x_organization_id: Optional organization ID
        report_service: Injected report service

    Returns:
        Updated report

    Raises:
        404: Report not found
        403: User not authorized
    """
    try:
        report = await report_service.update_report(
            report_id=report_id,
            request=request,
            current_user=current_user,
            organization_id=x_organization_id
        )
        return ReportResponse.from_domain(report)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete(
    "/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete report",
    description="Permanently delete a report and its version history"
)
async def delete_report(
    report_id: UUID,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    report_service: ReportService = Depends(get_report_service)
):
    """Delete report.

    Args:
        report_id: Report UUID
        current_user: Authenticated user
        x_organization_id: Optional organization ID
        report_service: Injected report service

    Returns:
        204 No Content

    Raises:
        404: Report not found
        403: User not authorized
    """
    try:
        await report_service.delete_report(
            report_id=report_id,
            current_user=current_user,
            organization_id=x_organization_id
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get(
    "/case/{case_id}",
    response_model=ReportListResponse,
    summary="List reports for case",
    description="Get all reports associated with a specific case"
)
async def list_reports_for_case(
    case_id: UUID,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    report_service: ReportService = Depends(get_report_service)
) -> ReportListResponse:
    """List all reports for a case.

    Args:
        case_id: Case UUID
        current_user: Authenticated user
        x_organization_id: Optional organization ID
        limit: Max results (1-1000)
        offset: Pagination offset
        report_service: Injected report service

    Returns:
        Paginated list of reports

    Raises:
        404: Case not found
        403: User not authorized
    """
    try:
        reports, total = await report_service.list_reports_for_case(
            case_id=case_id,
            current_user=current_user,
            organization_id=x_organization_id,
            limit=limit,
            offset=offset
        )

        return ReportListResponse(
            reports=[ReportResponse.from_domain(r) for r in reports],
            total=total,
            limit=limit,
            offset=offset
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get(
    "/{report_id}/versions",
    response_model=list[ReportVersionResponse],
    summary="Get report version history",
    description="Retrieve all versions of a report"
)
async def get_report_versions(
    report_id: UUID,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    report_service: ReportService = Depends(get_report_service)
) -> list[ReportVersionResponse]:
    """Get version history for a report.

    Args:
        report_id: Report UUID
        current_user: Authenticated user
        x_organization_id: Optional organization ID
        report_service: Injected report service

    Returns:
        List of report versions (newest first)

    Raises:
        404: Report not found
        403: User not authorized
    """
    try:
        versions = await report_service.get_report_versions(
            report_id=report_id,
            current_user=current_user,
            organization_id=x_organization_id
        )

        return [ReportVersionResponse.from_domain(v) for v in versions]
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post(
    "/{report_id}/link-case",
    status_code=status.HTTP_200_OK,
    summary="Link report to case closure",
    description="Mark case as closed and link final report"
)
async def link_report_to_case_closure(
    report_id: UUID,
    case_id: UUID,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    report_service: ReportService = Depends(get_report_service)
) -> dict:
    """Link report to case closure.

    This endpoint:
    1. Publishes the report (sets status to PUBLISHED)
    2. Marks the case as CLOSED
    3. Links the report as the final documentation

    Args:
        report_id: Report UUID
        case_id: Case UUID to close
        current_user: Authenticated user
        x_organization_id: Optional organization ID
        report_service: Injected report service

    Returns:
        Success message

    Raises:
        404: Report or case not found
        403: User not authorized
        400: Report already linked or case already closed
    """
    try:
        # Get report
        report = await report_service.get_report(
            report_id=report_id,
            current_user=current_user,
            organization_id=x_organization_id
        )

        # Verify report belongs to case
        if str(report.case_id) != str(case_id):
            raise ValidationException("Report does not belong to specified case")

        # Publish report
        from faultmaven.models.report import ReportUpdateRequest, ReportStatus
        await report_service.update_report(
            report_id=report_id,
            request=ReportUpdateRequest(status=ReportStatus.PUBLISHED),
            current_user=current_user,
            organization_id=x_organization_id
        )

        # TODO: Update case status to CLOSED
        # This will be implemented when we have CaseService.close_case() method
        # For now, just return success

        return {
            "status": "success",
            "message": f"Report {report_id} linked to case {case_id}",
            "report_id": str(report_id),
            "case_id": str(case_id)
        }

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))
```

---

### 6. Dependency Injection (Container Update)

**File**: `faultmaven/container.py` (update)

```python
from dependency_injector import containers, providers

from faultmaven.repositories.report_repository import ReportRepository
from faultmaven.repositories.report_version_repository import ReportVersionRepository
from faultmaven.services.report_service import ReportService


class Container(containers.DeclarativeContainer):
    # ... existing providers ...

    # Report Repository
    report_repository = providers.Singleton(
        ReportRepository,
        session_factory=db.session_factory
    )

    # Report Version Repository
    report_version_repository = providers.Singleton(
        ReportVersionRepository,
        session_factory=db.session_factory
    )

    # Report Service
    report_service = providers.Factory(
        ReportService,
        report_repository=report_repository,
        version_repository=report_version_repository,
        case_repository=case_repository,  # Existing
        tenant_provider=tenant_provider,  # From TASK-023
        llm_provider=llm_provider,  # Existing from agentic framework
        pii_redactor=pii_redactor  # From shim pattern
    )
```

**File**: `faultmaven/dependencies.py` (update)

```python
"""FastAPI dependency injection helpers."""

from faultmaven.container import Container
from faultmaven.services.report_service import ReportService


container = Container()


def get_report_service() -> ReportService:
    """Get report service from DI container."""
    return container.report_service()
```

---

### 7. SQLAlchemy ORM Models

**File**: `faultmaven/db/models.py` (add to existing file)

```python
"""SQLAlchemy ORM models."""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
import uuid

from faultmaven.db.base import Base


class ReportModel(Base):
    """ORM model for reports table."""

    __tablename__ = "reports"

    report_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.organization_id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    report_type = Column(String(50), nullable=False)
    content = Column(JSONB, nullable=False)
    status = Column(String(50), nullable=False, server_default="draft")
    version = Column(Integer, nullable=False, server_default="1")
    metadata = Column(JSONB, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default="now()", onupdate="now()")
    published_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    organization = relationship("OrganizationModel", back_populates="reports")
    case = relationship("CaseModel", back_populates="reports")
    creator = relationship("UserModel", back_populates="reports")
    versions = relationship("ReportVersionModel", back_populates="report", cascade="all, delete-orphan")

    # Constraints
    __table_args__ = (
        UniqueConstraint("case_id", "report_type", "version", name="uq_case_type_version"),
        Index("idx_reports_created_at", "created_at"),
        Index("idx_reports_status", "status"),
        Index("idx_reports_org_case", "organization_id", "case_id")
    )


class ReportVersionModel(Base):
    """ORM model for report_versions table."""

    __tablename__ = "report_versions"

    version_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("reports.report_id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content = Column(JSONB, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default="now()")
    change_summary = Column(String(1000), nullable=True)

    # Relationships
    report = relationship("ReportModel", back_populates="versions")
    creator = relationship("UserModel", back_populates="report_versions")

    # Constraints
    __table_args__ = (
        UniqueConstraint("report_id", "version_number", name="uq_report_version_number"),
    )
```

---

## Testing Requirements

### Test Coverage Summary

**Target**: 50+ tests, 90%+ coverage

| Test Category | File | Tests | Focus |
|--------------|------|-------|-------|
| **Unit Tests** | | | |
| Repository | `tests/unit/repositories/test_report_repository.py` | 12 | CRUD operations, multi-tenant isolation |
| Repository | `tests/unit/repositories/test_report_version_repository.py` | 5 | Version history |
| Service | `tests/unit/services/test_report_service.py` | 18 | Business logic, LLM integration |
| **Integration Tests** | | | |
| API | `tests/integration/api/test_reports_api.py` | 15 | HTTP endpoints, auth, validation |
| **E2E Tests** | | | |
| Workflow | `tests/e2e/test_report_workflow.py` | 5 | Full report generation flow |
| **TOTAL** | | **55** | |

---

### 1. Repository Unit Tests

**File**: `tests/unit/repositories/test_report_repository.py`

```python
"""Unit tests for ReportRepository."""

import pytest
from uuid import uuid4
from datetime import datetime, timezone

from faultmaven.models.report import Report, ReportType, ReportStatus, ReportContent, ReportSection
from faultmaven.repositories.report_repository import ReportRepository
from faultmaven.exceptions import NotFoundError


@pytest.fixture
def report_content():
    """Fixture for report content."""
    return ReportContent(
        summary="Test summary",
        sections=[
            ReportSection(title="Overview", content="Test content", order=1)
        ],
        key_findings=["Finding 1", "Finding 2"],
        recommendations=["Recommendation 1"]
    )


@pytest.fixture
def sample_report(report_content):
    """Fixture for sample report."""
    return Report(
        report_id=uuid4(),
        organization_id=uuid4(),
        case_id=uuid4(),
        title="Test Report",
        report_type=ReportType.POST_MORTEM,
        content=report_content,
        status=ReportStatus.DRAFT,
        version=1,
        created_by=uuid4()
    )


@pytest.mark.asyncio
class TestReportRepository:
    """Test ReportRepository CRUD operations."""

    async def test_create_report(self, async_session_factory, sample_report):
        """Test creating a report."""
        repo = ReportRepository(async_session_factory)

        created = await repo.create(sample_report)

        assert created.report_id == sample_report.report_id
        assert created.title == sample_report.title
        assert created.report_type == ReportType.POST_MORTEM
        assert created.status == ReportStatus.DRAFT
        assert created.version == 1

    async def test_get_by_id_success(self, async_session_factory, sample_report):
        """Test retrieving report by ID."""
        repo = ReportRepository(async_session_factory)
        await repo.create(sample_report)

        retrieved = await repo.get_by_id(
            report_id=sample_report.report_id,
            organization_id=sample_report.organization_id
        )

        assert retrieved is not None
        assert retrieved.report_id == sample_report.report_id
        assert retrieved.title == sample_report.title

    async def test_get_by_id_wrong_organization(self, async_session_factory, sample_report):
        """Test multi-tenant isolation - wrong organization returns None."""
        repo = ReportRepository(async_session_factory)
        await repo.create(sample_report)

        wrong_org_id = uuid4()
        retrieved = await repo.get_by_id(
            report_id=sample_report.report_id,
            organization_id=wrong_org_id
        )

        assert retrieved is None

    async def test_list_by_case(self, async_session_factory, report_content):
        """Test listing reports for a case."""
        repo = ReportRepository(async_session_factory)

        case_id = uuid4()
        org_id = uuid4()

        # Create 3 reports for same case
        for i in range(3):
            report = Report(
                report_id=uuid4(),
                organization_id=org_id,
                case_id=case_id,
                title=f"Report {i}",
                report_type=ReportType.POST_MORTEM,
                content=report_content,
                status=ReportStatus.DRAFT,
                version=i + 1
            )
            await repo.create(report)

        reports, total = await repo.list_by_case(
            case_id=case_id,
            organization_id=org_id
        )

        assert len(reports) == 3
        assert total == 3

    async def test_list_by_case_pagination(self, async_session_factory, report_content):
        """Test pagination for case reports."""
        repo = ReportRepository(async_session_factory)

        case_id = uuid4()
        org_id = uuid4()

        # Create 10 reports
        for i in range(10):
            report = Report(
                report_id=uuid4(),
                organization_id=org_id,
                case_id=case_id,
                title=f"Report {i}",
                report_type=ReportType.POST_MORTEM,
                content=report_content,
                status=ReportStatus.DRAFT,
                version=i + 1
            )
            await repo.create(report)

        # Get first page (5 results)
        page1, total = await repo.list_by_case(
            case_id=case_id,
            organization_id=org_id,
            limit=5,
            offset=0
        )

        assert len(page1) == 5
        assert total == 10

        # Get second page
        page2, _ = await repo.list_by_case(
            case_id=case_id,
            organization_id=org_id,
            limit=5,
            offset=5
        )

        assert len(page2) == 5

    async def test_update_report(self, async_session_factory, sample_report):
        """Test updating a report."""
        repo = ReportRepository(async_session_factory)
        created = await repo.create(sample_report)

        # Update title and status
        created.title = "Updated Title"
        created.status = ReportStatus.PUBLISHED

        updated = await repo.update(created)

        assert updated.title == "Updated Title"
        assert updated.status == ReportStatus.PUBLISHED
        assert updated.updated_at > updated.created_at

    async def test_update_nonexistent_report(self, async_session_factory, sample_report):
        """Test updating nonexistent report raises NotFoundError."""
        repo = ReportRepository(async_session_factory)

        with pytest.raises(NotFoundError):
            await repo.update(sample_report)

    async def test_delete_report(self, async_session_factory, sample_report):
        """Test deleting a report."""
        repo = ReportRepository(async_session_factory)
        await repo.create(sample_report)

        deleted = await repo.delete(
            report_id=sample_report.report_id,
            organization_id=sample_report.organization_id
        )

        assert deleted is True

        # Verify deleted
        retrieved = await repo.get_by_id(
            report_id=sample_report.report_id,
            organization_id=sample_report.organization_id
        )
        assert retrieved is None

    async def test_delete_nonexistent_report(self, async_session_factory):
        """Test deleting nonexistent report returns False."""
        repo = ReportRepository(async_session_factory)

        deleted = await repo.delete(
            report_id=uuid4(),
            organization_id=uuid4()
        )

        assert deleted is False

    async def test_count_by_type(self, async_session_factory, report_content):
        """Test counting reports by type."""
        repo = ReportRepository(async_session_factory)

        case_id = uuid4()
        org_id = uuid4()

        # Create 3 post-mortem reports, 2 executive summaries
        for i in range(3):
            report = Report(
                report_id=uuid4(),
                organization_id=org_id,
                case_id=case_id,
                title=f"Post-Mortem {i}",
                report_type=ReportType.POST_MORTEM,
                content=report_content,
                status=ReportStatus.DRAFT,
                version=i + 1
            )
            await repo.create(report)

        for i in range(2):
            report = Report(
                report_id=uuid4(),
                organization_id=org_id,
                case_id=case_id,
                title=f"Executive Summary {i}",
                report_type=ReportType.EXECUTIVE_SUMMARY,
                content=report_content,
                status=ReportStatus.DRAFT,
                version=i + 1
            )
            await repo.create(report)

        post_mortem_count = await repo.count_by_type(
            case_id=case_id,
            report_type=ReportType.POST_MORTEM,
            organization_id=org_id
        )

        exec_summary_count = await repo.count_by_type(
            case_id=case_id,
            report_type=ReportType.EXECUTIVE_SUMMARY,
            organization_id=org_id
        )

        assert post_mortem_count == 3
        assert exec_summary_count == 2

    async def test_multi_tenant_isolation(self, async_session_factory, report_content):
        """Test strict multi-tenant isolation across organizations."""
        repo = ReportRepository(async_session_factory)

        org1_id = uuid4()
        org2_id = uuid4()
        case_id = uuid4()

        # Create report in org1
        report1 = Report(
            report_id=uuid4(),
            organization_id=org1_id,
            case_id=case_id,
            title="Org 1 Report",
            report_type=ReportType.POST_MORTEM,
            content=report_content,
            status=ReportStatus.DRAFT,
            version=1
        )
        await repo.create(report1)

        # Create report in org2
        report2 = Report(
            report_id=uuid4(),
            organization_id=org2_id,
            case_id=case_id,
            title="Org 2 Report",
            report_type=ReportType.POST_MORTEM,
            content=report_content,
            status=ReportStatus.DRAFT,
            version=1
        )
        await repo.create(report2)

        # List reports for org1
        org1_reports, org1_total = await repo.list_by_case(
            case_id=case_id,
            organization_id=org1_id
        )

        # List reports for org2
        org2_reports, org2_total = await repo.list_by_case(
            case_id=case_id,
            organization_id=org2_id
        )

        assert org1_total == 1
        assert org2_total == 1
        assert org1_reports[0].report_id == report1.report_id
        assert org2_reports[0].report_id == report2.report_id
```

---

### 2. Service Unit Tests

**File**: `tests/unit/services/test_report_service.py`

```python
"""Unit tests for ReportService."""

import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch

from faultmaven.models.report import (
    Report,
    ReportCreateRequest,
    ReportUpdateRequest,
    ReportType,
    ReportStatus,
    ReportContent,
    ReportSection
)
from faultmaven.models.user import User
from faultmaven.services.report_service import ReportService
from faultmaven.exceptions import NotFoundError, ValidationException


@pytest.fixture
def mock_repositories():
    """Mock repository dependencies."""
    return {
        'report_repo': AsyncMock(),
        'version_repo': AsyncMock(),
        'case_repo': AsyncMock()
    }


@pytest.fixture
def mock_tenant_provider():
    """Mock TenantProvider."""
    provider = AsyncMock()
    provider.get_current_organization.return_value = Mock(organization_id=uuid4())
    return provider


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider."""
    llm = AsyncMock()
    llm.model_name = "gpt-4"
    llm.generate.return_value = """
{
    "summary": "Generated summary",
    "sections": [
        {"title": "Overview", "content": "Generated content"}
    ],
    "key_findings": ["Finding 1"],
    "recommendations": ["Recommendation 1"]
}
"""
    return llm


@pytest.fixture
def sample_user():
    """Sample user fixture."""
    return User(
        user_id=uuid4(),
        email="test@example.com",
        is_active=True
    )


@pytest.fixture
def sample_case():
    """Sample case fixture."""
    return Mock(
        case_id=uuid4(),
        title="Test Case",
        description="Test description",
        status="open",
        severity="high",
        created_at="2025-01-01T00:00:00Z"
    )


@pytest.mark.asyncio
class TestReportService:
    """Test ReportService business logic."""

    async def test_create_report_with_llm(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user,
        sample_case
    ):
        """Test creating report with LLM generation."""
        # Setup
        mock_repositories['case_repo'].get_by_id.return_value = sample_case
        mock_repositories['report_repo'].count_by_type.return_value = 0
        mock_repositories['report_repo'].create.return_value = Mock(
            report_id=uuid4(),
            version=1
        )

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        request = ReportCreateRequest(
            case_id=sample_case.case_id,
            report_type=ReportType.POST_MORTEM,
            generate_with_llm=True
        )

        # Execute
        report = await service.create_report(
            request=request,
            current_user=sample_user
        )

        # Verify
        assert mock_llm_provider.generate.called
        assert mock_repositories['report_repo'].create.called
        assert mock_repositories['version_repo'].create.called

    async def test_create_report_without_llm(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user,
        sample_case
    ):
        """Test creating report with empty template."""
        # Setup
        mock_repositories['case_repo'].get_by_id.return_value = sample_case
        mock_repositories['report_repo'].count_by_type.return_value = 0
        mock_repositories['report_repo'].create.return_value = Mock()

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        request = ReportCreateRequest(
            case_id=sample_case.case_id,
            report_type=ReportType.POST_MORTEM,
            generate_with_llm=False
        )

        # Execute
        await service.create_report(
            request=request,
            current_user=sample_user
        )

        # Verify LLM not called
        assert not mock_llm_provider.generate.called

    async def test_create_report_max_versions_exceeded(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user,
        sample_case
    ):
        """Test creating report when max versions exceeded."""
        # Setup - already have 5 versions
        mock_repositories['case_repo'].get_by_id.return_value = sample_case
        mock_repositories['report_repo'].count_by_type.return_value = 5

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        request = ReportCreateRequest(
            case_id=sample_case.case_id,
            report_type=ReportType.POST_MORTEM,
            generate_with_llm=True
        )

        # Execute and verify exception
        with pytest.raises(ValidationException) as exc_info:
            await service.create_report(
                request=request,
                current_user=sample_user
            )

        assert "Maximum 5 reports" in str(exc_info.value)

    async def test_create_report_case_not_found(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user
    ):
        """Test creating report for nonexistent case."""
        # Setup
        mock_repositories['case_repo'].get_by_id.return_value = None

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        request = ReportCreateRequest(
            case_id=uuid4(),
            report_type=ReportType.POST_MORTEM,
            generate_with_llm=True
        )

        # Execute and verify exception
        with pytest.raises(NotFoundError):
            await service.create_report(
                request=request,
                current_user=sample_user
            )

    async def test_update_report_status_to_published(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user
    ):
        """Test updating report status to published sets published_at."""
        # Setup
        report_id = uuid4()
        mock_report = Mock(
            report_id=report_id,
            organization_id=uuid4(),
            status=ReportStatus.DRAFT,
            title="Test",
            content=Mock(),
            metadata={},
            published_at=None
        )
        mock_repositories['report_repo'].get_by_id.return_value = mock_report
        mock_repositories['report_repo'].update.return_value = mock_report

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        request = ReportUpdateRequest(status=ReportStatus.PUBLISHED)

        # Execute
        await service.update_report(
            report_id=report_id,
            request=request,
            current_user=sample_user
        )

        # Verify published_at was set
        assert mock_report.published_at is not None

    async def test_update_report_creates_version_history(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user
    ):
        """Test updating report content creates version history."""
        # Setup
        report_id = uuid4()
        mock_report = Mock(
            report_id=report_id,
            organization_id=uuid4(),
            status=ReportStatus.DRAFT,
            title="Test",
            content=Mock(),
            metadata={}
        )
        mock_repositories['report_repo'].get_by_id.return_value = mock_report
        mock_repositories['report_repo'].update.return_value = mock_report

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        new_content = ReportContent(
            summary="Updated summary",
            sections=[],
            key_findings=[],
            recommendations=[]
        )
        request = ReportUpdateRequest(content=new_content)

        # Execute
        await service.update_report(
            report_id=report_id,
            request=request,
            current_user=sample_user
        )

        # Verify version history created
        assert mock_repositories['version_repo'].create.called

    async def test_delete_report(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user
    ):
        """Test deleting a report."""
        # Setup
        report_id = uuid4()
        mock_report = Mock(
            report_id=report_id,
            organization_id=uuid4()
        )
        mock_repositories['report_repo'].get_by_id.return_value = mock_report
        mock_repositories['report_repo'].delete.return_value = True

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        # Execute
        deleted = await service.delete_report(
            report_id=report_id,
            current_user=sample_user
        )

        # Verify
        assert deleted is True
        assert mock_repositories['report_repo'].delete.called

    async def test_list_reports_for_case(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user,
        sample_case
    ):
        """Test listing reports for a case."""
        # Setup
        mock_repositories['case_repo'].get_by_id.return_value = sample_case
        mock_repositories['report_repo'].list_by_case.return_value = ([], 0)

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider
        )

        # Execute
        reports, total = await service.list_reports_for_case(
            case_id=sample_case.case_id,
            current_user=sample_user,
            limit=100,
            offset=0
        )

        # Verify
        assert mock_repositories['case_repo'].get_by_id.called
        assert mock_repositories['report_repo'].list_by_case.called

    async def test_pii_redaction_called(
        self,
        mock_repositories,
        mock_tenant_provider,
        mock_llm_provider,
        sample_user,
        sample_case
    ):
        """Test PII redaction is called before LLM generation."""
        # Setup
        mock_repositories['case_repo'].get_by_id.return_value = sample_case
        mock_repositories['report_repo'].count_by_type.return_value = 0
        mock_repositories['report_repo'].create.return_value = Mock()

        mock_pii_redactor = Mock()
        mock_pii_redactor.redact.return_value = "Redacted content"

        service = ReportService(
            report_repository=mock_repositories['report_repo'],
            version_repository=mock_repositories['version_repo'],
            case_repository=mock_repositories['case_repo'],
            tenant_provider=mock_tenant_provider,
            llm_provider=mock_llm_provider,
            pii_redactor=mock_pii_redactor
        )

        request = ReportCreateRequest(
            case_id=sample_case.case_id,
            report_type=ReportType.POST_MORTEM,
            generate_with_llm=True
        )

        # Execute
        await service.create_report(
            request=request,
            current_user=sample_user
        )

        # Verify PII redaction called
        assert mock_pii_redactor.redact.called

    # Add 8 more tests for edge cases:
    # - test_generate_title_from_case
    # - test_create_empty_template
    # - test_get_report_template_post_mortem
    # - test_get_report_template_executive_summary
    # - test_get_report_template_technical_analysis
    # - test_parse_llm_output_valid_json
    # - test_parse_llm_output_invalid_json_fallback
    # - test_get_report_versions
```

---

### 3. API Integration Tests

**File**: `tests/integration/api/test_reports_api.py`

```python
"""Integration tests for Reports API endpoints."""

import pytest
from uuid import uuid4

from faultmaven.models.report import ReportType, ReportStatus


@pytest.mark.asyncio
class TestReportsAPI:
    """Test Reports REST API endpoints."""

    async def test_generate_report_success(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test POST /reports/generate with LLM generation."""
        response = client.post(
            "/api/v1/reports/generate",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "case_id": str(test_case.case_id),
                "report_type": "post_mortem",
                "generate_with_llm": True
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["case_id"] == str(test_case.case_id)
        assert data["report_type"] == "post_mortem"
        assert data["status"] == "draft"
        assert data["version"] == 1
        assert "content" in data

    async def test_generate_report_without_auth(self, client, test_case):
        """Test generating report without authentication returns 401."""
        response = client.post(
            "/api/v1/reports/generate",
            json={
                "case_id": str(test_case.case_id),
                "report_type": "post_mortem"
            }
        )

        assert response.status_code == 401

    async def test_generate_report_case_not_found(
        self,
        client,
        admin_user_token
    ):
        """Test generating report for nonexistent case returns 404."""
        response = client.post(
            "/api/v1/reports/generate",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "case_id": str(uuid4()),
                "report_type": "post_mortem"
            }
        )

        assert response.status_code == 404

    async def test_get_report_success(
        self,
        client,
        admin_user_token,
        test_report
    ):
        """Test GET /reports/{id}."""
        response = client.get(
            f"/api/v1/reports/{test_report.report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["report_id"] == str(test_report.report_id)
        assert data["title"] == test_report.title

    async def test_get_report_not_found(
        self,
        client,
        admin_user_token
    ):
        """Test getting nonexistent report returns 404."""
        response = client.get(
            f"/api/v1/reports/{uuid4()}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 404

    async def test_update_report_success(
        self,
        client,
        admin_user_token,
        test_report
    ):
        """Test PUT /reports/{id}."""
        response = client.put(
            f"/api/v1/reports/{test_report.report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "title": "Updated Title",
                "status": "published"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["status"] == "published"
        assert data["published_at"] is not None

    async def test_delete_report_success(
        self,
        client,
        admin_user_token,
        test_report
    ):
        """Test DELETE /reports/{id}."""
        response = client.delete(
            f"/api/v1/reports/{test_report.report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 204

        # Verify deleted
        get_response = client.get(
            f"/api/v1/reports/{test_report.report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )
        assert get_response.status_code == 404

    async def test_list_reports_for_case(
        self,
        client,
        admin_user_token,
        test_case,
        test_report
    ):
        """Test GET /reports/case/{case_id}."""
        response = client.get(
            f"/api/v1/reports/case/{test_case.case_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "reports" in data
        assert data["total"] >= 1
        assert data["limit"] == 100
        assert data["offset"] == 0

    async def test_list_reports_pagination(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test pagination for reports list."""
        response = client.get(
            f"/api/v1/reports/case/{test_case.case_id}?limit=10&offset=0",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 0

    async def test_get_report_versions(
        self,
        client,
        admin_user_token,
        test_report
    ):
        """Test GET /reports/{id}/versions."""
        response = client.get(
            f"/api/v1/reports/{test_report.report_id}/versions",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1  # At least initial version

    async def test_link_report_to_case_closure(
        self,
        client,
        admin_user_token,
        test_case,
        test_report
    ):
        """Test POST /reports/{id}/link-case."""
        response = client.post(
            f"/api/v1/reports/{test_report.report_id}/link-case?case_id={test_case.case_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "report_id" in data
        assert "case_id" in data

    async def test_multi_tenant_isolation(
        self,
        client,
        org1_user_token,
        org2_user_token,
        org1_case,
        org1_report
    ):
        """Test reports are isolated between organizations."""
        # Org1 user can access their report
        response1 = client.get(
            f"/api/v1/reports/{org1_report.report_id}",
            headers={"Authorization": f"Bearer {org1_user_token}"}
        )
        assert response1.status_code == 200

        # Org2 user cannot access org1's report
        response2 = client.get(
            f"/api/v1/reports/{org1_report.report_id}",
            headers={"Authorization": f"Bearer {org2_user_token}"}
        )
        assert response2.status_code == 404

    async def test_max_versions_limit(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test maximum 5 versions per report type limit."""
        # Create 5 post-mortem reports
        for i in range(5):
            response = client.post(
                "/api/v1/reports/generate",
                headers={"Authorization": f"Bearer {admin_user_token}"},
                json={
                    "case_id": str(test_case.case_id),
                    "report_type": "post_mortem",
                    "generate_with_llm": False
                }
            )
            assert response.status_code == 201

        # 6th attempt should fail
        response = client.post(
            "/api/v1/reports/generate",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "case_id": str(test_case.case_id),
                "report_type": "post_mortem",
                "generate_with_llm": False
            }
        )
        assert response.status_code == 400
        assert "Maximum 5 reports" in response.json()["detail"]

    async def test_different_report_types_independent_versions(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test different report types have independent version limits."""
        # Create 5 post-mortem reports
        for i in range(5):
            response = client.post(
                "/api/v1/reports/generate",
                headers={"Authorization": f"Bearer {admin_user_token}"},
                json={
                    "case_id": str(test_case.case_id),
                    "report_type": "post_mortem",
                    "generate_with_llm": False
                }
            )
            assert response.status_code == 201

        # Should still be able to create executive summary
        response = client.post(
            "/api/v1/reports/generate",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "case_id": str(test_case.case_id),
                "report_type": "executive_summary",
                "generate_with_llm": False
            }
        )
        assert response.status_code == 201
```

---

### 4. End-to-End Workflow Tests

**File**: `tests/e2e/test_report_workflow.py`

```python
"""End-to-end tests for complete report workflow."""

import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
class TestReportWorkflow:
    """Test complete report generation workflow."""

    async def test_complete_report_lifecycle(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test full report lifecycle: create → update → publish → delete."""
        # 1. Generate draft report
        create_response = client.post(
            "/api/v1/reports/generate",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "case_id": str(test_case.case_id),
                "report_type": "post_mortem",
                "generate_with_llm": True
            }
        )
        assert create_response.status_code == 201
        report = create_response.json()
        report_id = report["report_id"]
        assert report["status"] == "draft"

        # 2. Update report content
        update_response = client.put(
            f"/api/v1/reports/{report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "title": "Updated Report Title"
            }
        )
        assert update_response.status_code == 200
        assert update_response.json()["title"] == "Updated Report Title"

        # 3. Publish report
        publish_response = client.put(
            f"/api/v1/reports/{report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "status": "published"
            }
        )
        assert publish_response.status_code == 200
        assert publish_response.json()["status"] == "published"
        assert publish_response.json()["published_at"] is not None

        # 4. Link to case closure
        link_response = client.post(
            f"/api/v1/reports/{report_id}/link-case?case_id={test_case.case_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )
        assert link_response.status_code == 200

        # 5. Verify version history
        versions_response = client.get(
            f"/api/v1/reports/{report_id}/versions",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )
        assert versions_response.status_code == 200
        versions = versions_response.json()
        assert len(versions) >= 2  # Initial + update

        # 6. Delete report
        delete_response = client.delete(
            f"/api/v1/reports/{report_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )
        assert delete_response.status_code == 204

    async def test_multi_version_workflow(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test creating multiple report versions."""
        # Create 3 versions of post-mortem reports
        report_ids = []
        for i in range(3):
            response = client.post(
                "/api/v1/reports/generate",
                headers={"Authorization": f"Bearer {admin_user_token}"},
                json={
                    "case_id": str(test_case.case_id),
                    "report_type": "post_mortem",
                    "title": f"Post-Mortem v{i+1}",
                    "generate_with_llm": False
                }
            )
            assert response.status_code == 201
            report_ids.append(response.json()["report_id"])

        # List all reports for case
        list_response = client.get(
            f"/api/v1/reports/case/{test_case.case_id}",
            headers={"Authorization": f"Bearer {admin_user_token}"}
        )
        assert list_response.status_code == 200
        data = list_response.json()
        assert data["total"] >= 3

        # Verify versions
        for report_id in report_ids:
            get_response = client.get(
                f"/api/v1/reports/{report_id}",
                headers={"Authorization": f"Bearer {admin_user_token}"}
            )
            assert get_response.status_code == 200

    async def test_report_types_workflow(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test generating all three report types."""
        report_types = ["post_mortem", "executive_summary", "technical_analysis"]

        for report_type in report_types:
            response = client.post(
                "/api/v1/reports/generate",
                headers={"Authorization": f"Bearer {admin_user_token}"},
                json={
                    "case_id": str(test_case.case_id),
                    "report_type": report_type,
                    "generate_with_llm": True
                }
            )
            assert response.status_code == 201
            data = response.json()
            assert data["report_type"] == report_type
            assert "content" in data
            assert "summary" in data["content"]

    async def test_deployment_neutral_workflow_single_tenant(
        self,
        client,
        admin_user_token,
        test_case
    ):
        """Test report workflow in single-tenant mode (default org)."""
        # Single-tenant mode: no X-Organization-ID header needed
        response = client.post(
            "/api/v1/reports/generate",
            headers={"Authorization": f"Bearer {admin_user_token}"},
            json={
                "case_id": str(test_case.case_id),
                "report_type": "post_mortem",
                "generate_with_llm": False
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert "organization_id" in data

    async def test_deployment_neutral_workflow_multi_tenant(
        self,
        client,
        org1_user_token,
        org1_case,
        org1_organization
    ):
        """Test report workflow in multi-tenant mode (explicit org ID)."""
        # Multi-tenant mode: include X-Organization-ID header
        response = client.post(
            "/api/v1/reports/generate",
            headers={
                "Authorization": f"Bearer {org1_user_token}",
                "X-Organization-ID": str(org1_organization.organization_id)
            },
            json={
                "case_id": str(org1_case.case_id),
                "report_type": "post_mortem",
                "generate_with_llm": False
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["organization_id"] == str(org1_organization.organization_id)
```

---

## Implementation Plan

### Week 1: Database and Repository Layer (Days 1-5)

**Day 1-2**: Database Schema
- Create Alembic migration for `reports` and `report_versions` tables
- Add SQLAlchemy ORM models (ReportModel, ReportVersionModel)
- Test migration rollback/upgrade
- **Deliverable**: Migration runs successfully

**Day 3-4**: Repository Layer
- Implement `ReportRepository` (CRUD operations)
- Implement `ReportVersionRepository` (version history)
- Write 17 repository unit tests (12 + 5)
- **Deliverable**: Repositories tested and passing

**Day 5**: Integration Testing
- Test repositories against real database
- Verify multi-tenant isolation
- Performance testing (query optimization)
- **Deliverable**: Repository layer complete

---

### Week 2: Service Layer and LLM Integration (Days 6-10)

**Day 6-7**: Domain Models
- Create `Report`, `ReportVersion` domain models
- Create Pydantic request/response models
- Implement enums (ReportType, ReportStatus)
- **Deliverable**: Models validated and serializing correctly

**Day 8-9**: Service Layer Core
- Implement `ReportService` CRUD methods
- Integrate with `TenantProvider` (deployment neutrality)
- Implement version management logic
- Write 10 service unit tests
- **Deliverable**: Service layer core complete

**Day 10**: LLM Integration
- Integrate with existing LLM provider
- Implement report templates (3 types)
- Add PII redaction (shim pattern)
- Write 8 LLM integration tests
- **Deliverable**: LLM generation working

---

### Week 3: API Layer and Integration Tests (Days 11-15)

**Day 11-12**: API Endpoints
- Implement 7 REST endpoints in `reports.py`
- Wire into DI container
- Add OpenAPI documentation
- **Deliverable**: All 7 endpoints callable

**Day 13-14**: API Integration Tests
- Write 15 API integration tests
- Test authentication/authorization
- Test validation errors
- Test multi-tenant isolation
- **Deliverable**: API layer fully tested

**Day 15**: E2E Workflow Tests
- Write 5 end-to-end workflow tests
- Test complete lifecycle (create → update → publish → delete)
- Test deployment neutrality (single-tenant and multi-tenant)
- **Deliverable**: Full workflow validated

---

### Week 4: Polish, Documentation, and PR (Days 16-20)

**Day 16**: Performance Optimization
- Add database indexes
- Optimize LLM calls (caching)
- Query optimization (N+1 prevention)
- **Deliverable**: < 500ms p95 latency for report generation

**Day 17**: Error Handling and Edge Cases
- Add comprehensive error handling
- Improve validation messages
- Test edge cases (max versions, concurrent updates)
- **Deliverable**: Robust error handling

**Day 18**: Documentation
- API documentation (OpenAPI)
- Usage examples (curl, Python SDK)
- Report template guide
- **Deliverable**: Complete documentation

**Day 19**: Code Review Prep
- Code cleanup and refactoring
- Add inline documentation
- Run linters and formatters
- Final test run (all 55+ tests)
- **Deliverable**: PR ready for review

**Day 20**: PR Submission and Review
- Create pull request
- Address review feedback
- Final approval and merge
- **Deliverable**: TASK-024 MERGED

---

## Acceptance Criteria

### Functional Requirements

1. ✅ **7 REST endpoints implemented**:
   - `POST /reports/generate` - Generate report with LLM
   - `GET /reports/{id}` - Get report by ID
   - `PUT /reports/{id}` - Update report
   - `DELETE /reports/{id}` - Delete report
   - `GET /reports/case/{case_id}` - List reports for case
   - `GET /reports/{id}/versions` - Get version history
   - `POST /reports/{id}/link-case` - Link to case closure

2. ✅ **LLM Integration**:
   - Uses existing LLM provider from agentic framework
   - Supports 3 report types (post-mortem, executive summary, technical analysis)
   - PII redaction before LLM processing (shim pattern)
   - Graceful degradation when Presidio unavailable

3. ✅ **Version Management**:
   - Max 5 versions per report type per case
   - Version history tracked automatically
   - Change summaries captured

4. ✅ **Multi-Tenant Isolation**:
   - Uses TenantProvider (from TASK-023)
   - Works in both single-tenant and multi-tenant modes
   - Organization-level access control enforced

5. ✅ **Data Validation**:
   - Report content structure validated (Pydantic)
   - Summary length requirements enforced
   - Report type and status enums

---

### Testing Requirements

1. ✅ **50+ tests passing**:
   - 12 repository tests (ReportRepository)
   - 5 version repository tests
   - 18 service tests
   - 15 API integration tests
   - 5 E2E workflow tests
   - **Total**: 55 tests

2. ✅ **90%+ test coverage** for:
   - ReportRepository
   - ReportVersionRepository
   - ReportService
   - API endpoints

3. ✅ **Test categories**:
   - Unit tests (isolation, mocking)
   - Integration tests (database, HTTP)
   - E2E tests (full workflow)

---

### Code Quality

1. ✅ Type hints on all public methods
2. ✅ Docstrings on all classes and methods
3. ✅ Error handling with proper exceptions
4. ✅ Logging for LLM calls and errors
5. ✅ Follows existing FaultMaven patterns (repository, service, API layers)
6. ✅ No hardcoded values (use settings/config)

---

### Performance Requirements

1. ✅ Report generation < 5 seconds (p95) with LLM
2. ✅ Report retrieval < 100ms (p95)
3. ✅ List operations < 200ms (p95)
4. ✅ Database indexes on frequently queried columns

---

### Documentation Requirements

1. ✅ OpenAPI documentation (auto-generated by FastAPI)
2. ✅ Usage examples (curl commands)
3. ✅ Report template guide (3 types)
4. ✅ Deployment neutrality documentation
5. ✅ Migration guide (Alembic)

---

## Dependencies

### Completed Tasks (Prerequisites)
- ✅ TASK-023: TenantProvider (merged PR #26)
- ✅ TASK-021: Organization Management (merged PR #23)
- ✅ TASK-020: Remove Legacy Headers (commit 338c5957)
- ✅ TASK-017: JWT Authentication (merged PR #20)

### Required Repositories (Already Exist)
- ✅ CaseRepository (existing)
- ✅ OrganizationRepository (TASK-021)
- ✅ UserRepository (TASK-019)

### Required Services (Already Exist)
- ✅ TenantProvider (TASK-023)
- ✅ LLMProvider (existing agentic framework)
- ✅ JWTAuthenticationMiddleware (TASK-017)

### New Dependencies (Will Create)
- ReportRepository (new)
- ReportVersionRepository (new)
- ReportService (new)
- PIIRedactor shim (new, optional)

---

## Risks and Mitigation

### Risk 1: LLM Integration Complexity
**Likelihood**: MEDIUM
**Impact**: MEDIUM
**Mitigation**:
- Use existing LLM provider (already proven in agentic framework)
- Start with simple templates, iterate based on feedback
- Fallback to manual template if LLM fails
- **Contingency**: Make LLM generation optional (allow manual report creation)

---

### Risk 2: PII Redaction Dependency
**Likelihood**: LOW
**Impact**: LOW
**Mitigation**:
- Implement shim pattern (works without Presidio)
- No-op redactor if Presidio unavailable
- Document PII risk in community edition
- **Contingency**: Skip PII redaction in development, require in production

---

### Risk 3: Version Management Complexity
**Likelihood**: LOW
**Impact**: MEDIUM
**Mitigation**:
- Simple max 5 versions rule (easy to understand)
- Cascade delete handles cleanup
- Version history stored separately (no foreign key issues)
- **Contingency**: Disable version history in v1, add in v2

---

### Risk 4: Performance with Large Reports
**Likelihood**: MEDIUM
**Impact**: MEDIUM
**Mitigation**:
- JSONB storage in PostgreSQL (efficient)
- Database indexes on frequently queried columns
- Pagination for list operations
- LLM token limits prevent oversized reports
- **Contingency**: Add report size limits (max 50KB content)

---

## Success Metrics

### Week 1 (Database & Repository)
- ✅ Alembic migration runs successfully
- ✅ 17 repository tests passing
- ✅ Multi-tenant isolation verified

### Week 2 (Service & LLM)
- ✅ ReportService implements all CRUD operations
- ✅ LLM integration generates valid reports
- ✅ 18 service tests passing
- ✅ PII redaction working (shim pattern)

### Week 3 (API & Integration)
- ✅ 7 REST endpoints fully functional
- ✅ 15 API integration tests passing
- ✅ 5 E2E workflow tests passing
- ✅ OpenAPI docs generated

### Week 4 (Polish & PR)
- ✅ All 55+ tests passing
- ✅ 90%+ test coverage
- ✅ Performance targets met (< 5s generation, < 100ms retrieval)
- ✅ Documentation complete
- ✅ PR approved and merged

---

## Deliverables

### Code Files (New)

**Models**:
- `faultmaven/models/report.py` - Domain models, Pydantic schemas

**Repositories**:
- `faultmaven/repositories/report_repository.py` - Report CRUD
- `faultmaven/repositories/report_version_repository.py` - Version history

**Services**:
- `faultmaven/services/report_service.py` - Business logic, LLM integration

**API**:
- `faultmaven/api/v1/reports.py` - 7 REST endpoints

**Database**:
- `alembic/versions/20250101_add_reports.py` - Migration
- `faultmaven/db/models.py` - ORM models (update existing)

---

### Code Files (Modified)

**DI Container**:
- `faultmaven/container.py` - Add report service providers

**Dependencies**:
- `faultmaven/dependencies.py` - Add `get_report_service()`

**Main Router**:
- `faultmaven/api/v1/router.py` - Register reports router

---

### Test Files (New)

**Unit Tests**:
- `tests/unit/repositories/test_report_repository.py` (12 tests)
- `tests/unit/repositories/test_report_version_repository.py` (5 tests)
- `tests/unit/services/test_report_service.py` (18 tests)

**Integration Tests**:
- `tests/integration/api/test_reports_api.py` (15 tests)

**E2E Tests**:
- `tests/e2e/test_report_workflow.py` (5 tests)

---

### Documentation

- **API Documentation**: OpenAPI spec (auto-generated)
- **Usage Guide**: Report generation examples
- **Template Guide**: 3 report types explained
- **Migration Guide**: Database schema changes
- **Deployment Guide**: Single-tenant vs multi-tenant configuration

---

### Pull Request

**Title**: `feat: implement Report Module with 7 CRITICAL endpoints (TASK-024)`

**Description**:
```markdown
## Summary

Implements the Report Module with 7 CRITICAL endpoints for LLM-powered post-mortem report generation.

## Changes

- ✅ 7 REST endpoints for report CRUD operations
- ✅ LLM integration with existing agentic framework
- ✅ PII redaction with shim pattern (graceful degradation)
- ✅ Multi-tenant isolation via TenantProvider
- ✅ Version management (max 5 per type)
- ✅ 55+ tests (unit, integration, E2E)
- ✅ 90%+ test coverage

## Endpoints

1. `POST /reports/generate` - Generate report with LLM
2. `GET /reports/{id}` - Get report by ID
3. `PUT /reports/{id}` - Update report
4. `DELETE /reports/{id}` - Delete report
5. `GET /reports/case/{case_id}` - List reports for case
6. `GET /reports/{id}/versions` - Get version history
7. `POST /reports/{id}/link-case` - Link to case closure

## Testing

- 12 repository tests
- 5 version repository tests
- 18 service tests
- 15 API integration tests
- 5 E2E workflow tests
- **Total**: 55 tests, 90%+ coverage

## Performance

- Report generation: < 5 seconds (p95) with LLM
- Report retrieval: < 100ms (p95)
- List operations: < 200ms (p95)

## Dependencies

- Requires: TASK-023 (TenantProvider)
- Integrates: Existing LLM provider from agentic framework
- Uses: Shim pattern for PII redaction (Presidio optional)

## Breaking Changes

None - purely additive feature.

## Migration

Run Alembic migration:
```bash
alembic upgrade head
```

## Documentation

- OpenAPI: `/docs` endpoint
- Usage: See `docs/api/reports.md`
- Templates: See `docs/guides/report-templates.md`

## Related

- Phase 0: API Feature Parity (Week 2)
- Platform Evolution Strategy: Week 3-6 Report Module
- Task Spec: `docs/working/TASK-024-REPORT-MODULE.md`
```

---

## Notes

- This is the **first of 43 missing CRITICAL/HIGH endpoints**
- Success demonstrates deployment-neutral architecture patterns
- Establishes LLM integration pattern for future endpoints (TASK-026 Hypothesis, TASK-027 Agent Chat)
- Report module is **user-facing** - enables compliance documentation for enterprise customers
- **Scope strictly limited** to 7 endpoints - no scope creep into analytics or report scheduling

---

## Estimated Effort

**Total**: 4 weeks (20 working days)

**Breakdown**:
- Week 1: Database & Repository (5 days)
- Week 2: Service & LLM Integration (5 days)
- Week 3: API & Integration Tests (5 days)
- Week 4: Polish & PR (5 days)

**Team**: 1 Backend Engineer + 1 AI Specialist (for LLM templates)

**Complexity**: MEDIUM-HIGH (LLM integration adds complexity, but existing patterns reduce risk)

**Strategic Importance**: **CRITICAL** - First major endpoint implementation, sets pattern for 36 remaining endpoints

---

**Document Version**: 1.0
**Created**: 2025-12-31
**Author**: Solutions Architect
**Status**: READY FOR IMPLEMENTATION
**Next Task**: TASK-025 (Evidence Download & Token Refresh) - 2 endpoints, Week 5

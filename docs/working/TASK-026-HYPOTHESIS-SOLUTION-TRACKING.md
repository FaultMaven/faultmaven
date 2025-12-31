# TASK-026: Hypothesis & Solution Tracking Implementation (CRITICAL Endpoints)

## Task Metadata
- **Phase**: Phase 1 - Weeks 5-6 (API Feature Parity)
- **Priority**: P0 (CRITICAL - Core troubleshooting workflow)
- **Estimated Time**: 2 weeks (10 working days)
- **Dependencies**:
  - TASK-024 (Report Module) - ✅ MERGED (PR #27)
  - TASK-023 (TenantProvider) - ✅ MERGED (PR #26)
  - TASK-025 (Evidence Download & Token Refresh) - ⏩ SKIPPED (already exists)
- **Assignee**: Backend Engineer + AI Specialist
- **Reports To**: Solutions Architect
- **Scope**: 3 CRITICAL endpoints, investigation orchestration layer, 30+ tests

---

## Executive Summary

**Objective**: Implement hypothesis tracking and solution documentation to complete FaultMaven's core troubleshooting workflow.

**Business Value**:
- **Investigation Methodology**: Enable systematic hypothesis-driven troubleshooting
- **Knowledge Capture**: Solutions feed directly into knowledge base for future cases
- **AI Integration**: Agent-generated hypotheses with confidence scoring
- **Workflow Completion**: Connects evidence → hypothesis → solution → resolution

**Strategic Context**:
This is the **2nd batch of CRITICAL endpoints** in the Platform Evolution Strategy. After TASK-024 (Report Module) delivered LLM-powered report generation, TASK-026 enables the **investigation workflow** that precedes report creation.

**User Story**:
```
As a troubleshooter,
I want to track multiple hypotheses during investigation,
So that I can systematically validate theories and document solutions.
```

**Success Criteria**:
- ✅ 3 REST endpoints fully implemented
- ✅ 30+ tests passing (unit, integration, E2E)
- ✅ Investigation orchestrator integrates with agentic framework
- ✅ Multi-tenant isolation enforced
- ✅ Deployment-neutral (uses TenantProvider)
- ✅ 90%+ test coverage

---

## Context

### Why Hypothesis & Solution Tracking is CRITICAL

**The FaultMaven Investigation Workflow**:

1. **Case Creation**: User describes problem, uploads evidence
2. **Hypothesis Generation**: AI agent analyzes evidence, proposes hypotheses
3. **Investigation**: User/agent tests hypotheses, collects evidence
4. **Solution Discovery**: Validated hypothesis leads to solution
5. **Resolution**: Solution applied, case marked resolved
6. **Documentation**: Report generated from hypotheses + solution

**Current Gap**: Steps 2-4 are manual. Users cannot:
- Track multiple hypotheses systematically
- Record confidence levels and validation status
- Link solutions to specific hypotheses
- Feed investigation findings into knowledge base

**Impact**:
- Investigations lack structure (users lose track of what they've tried)
- Knowledge is lost (no record of failed hypotheses)
- AI agent cannot learn from past investigations
- Reports miss critical investigation context

---

## Architecture Overview

### System Components

```mermaid
graph TD
    A[Client] -->|POST /hypotheses| B[HypothesisRouter]
    B --> C[InvestigationOrchestrator]
    C --> D[CaseService]
    C --> E[AgentManager]
    C --> F[HypothesisRepository]

    E -->|Generate hypotheses| G[LLM Provider]
    D -->|Get case context| H[PostgreSQL/SQLite]
    F -->|Persist hypotheses| H

    I[Client] -->|POST /solutions| J[SolutionRouter]
    J --> C
    C --> K[SolutionRepository]
    K --> H

    L[Client] -->|PUT /hypotheses/{id}| B
    B --> C
    C --> F
```

### Key Design Decision: Investigation Orchestrator

**Architecture**: Hypothesis tracking is implemented as an **orchestration layer** between the Case API and the Agent framework, NOT as agent tools.

**Rationale**:
- **Hypothesis lifecycle is business logic**, not AI functionality
- Agent framework generates hypotheses; orchestrator manages workflow
- Clear separation: Agents = reasoning, Orchestrator = state management
- Compatible with existing OODA loop and LangGraph patterns

**Pattern**:
```python
class InvestigationOrchestrator:
    """
    Coordinates agent actions and hypothesis lifecycle.
    Sits between Case API and Agent framework.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        case_service: CaseService,
        hypothesis_repo: HypothesisRepository,
        solution_repo: SolutionRepository
    ):
        self.agents = agent_manager
        self.cases = case_service
        self.hypotheses = hypothesis_repo
        self.solutions = solution_repo

    async def run_investigation(self, case_id: str) -> List[Hypothesis]:
        """Execute investigation with hypothesis generation and validation"""

        # 1. Get case context
        case = await self.cases.get_case(case_id)

        # 2. Agent generates hypotheses
        raw_hypotheses = await self.agents.generate_hypotheses(
            context=case.description,
            evidence=case.evidence
        )

        # 3. Store hypotheses in database
        for hyp in raw_hypotheses:
            await self.hypotheses.create(case_id, hyp)

        # 4. Agent investigates each hypothesis
        for hyp in raw_hypotheses:
            result = await self.agents.investigate_hypothesis(hyp)

            # 5. Update confidence based on findings
            confidence = self._calculate_confidence(result)
            await self.hypotheses.update(
                hyp.id,
                status='confirmed' if confidence > 0.8 else 'testing',
                confidence_level=confidence
            )

        return raw_hypotheses
```

---

## Technical Specification

### 1. Database Schema (Alembic Migration)

**Migration File**: `alembic/versions/20250101_add_hypotheses_solutions.py`

```python
"""Add hypotheses and solutions tables for TASK-026

Revision ID: 20250101_002
Revises: 20250101_001  # Last migration from TASK-024 (Report Module)
Create Date: 2025-01-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
from datetime import datetime, timezone

revision = '20250101_002'
down_revision = '20250101_001'
branch_labels = None
depends_on = None


def upgrade():
    """Create hypotheses and solutions tables"""

    # Hypotheses table
    op.create_table(
        'hypotheses',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('case_id', UUID(as_uuid=True), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True),

        # Core fields
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(50), server_default='testing', nullable=False),  # 'testing', 'confirmed', 'rejected'
        sa.Column('confidence_level', sa.Numeric(3, 2), nullable=True),  # 0.00 to 1.00

        # Evidence and findings
        sa.Column('evidence_refs', JSONB(), server_default='[]'),  # Array of evidence IDs
        sa.Column('findings', JSONB(), server_default='{}'),  # Investigation findings

        # Metadata
        sa.Column('source', sa.String(50), server_default='manual', nullable=False),  # 'manual', 'ai_generated'
        sa.Column('agent_metadata', JSONB(), server_default='{}'),  # AI-specific metadata

        # Audit trail
        sa.Column('created_by', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_by', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Solutions table
    op.create_table(
        'solutions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('case_id', UUID(as_uuid=True), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('hypothesis_id', UUID(as_uuid=True), sa.ForeignKey('hypotheses.id', ondelete='SET NULL'), nullable=True),  # Optional link
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True),

        # Core fields
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('steps', JSONB(), nullable=False),  # Array of solution steps with detailed instructions
        sa.Column('validation', JSONB(), server_default='{}'),  # Validation results, testing notes

        # Implementation tracking
        sa.Column('implemented', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('implemented_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('effectiveness_score', sa.Numeric(3, 2), nullable=True),  # 0.00 to 1.00

        # Knowledge base integration
        sa.Column('knowledge_article_id', UUID(as_uuid=True), nullable=True),  # Link to KB article

        # Audit trail
        sa.Column('created_by', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_by', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Indexes for query performance
    op.create_index('idx_hypotheses_case_status', 'hypotheses', ['case_id', 'status'])
    op.create_index('idx_hypotheses_org_created', 'hypotheses', ['organization_id', 'created_at'])
    op.create_index('idx_solutions_case_implemented', 'solutions', ['case_id', 'implemented'])
    op.create_index('idx_solutions_org_created', 'solutions', ['organization_id', 'created_at'])


def downgrade():
    """Drop hypotheses and solutions tables"""
    op.drop_index('idx_solutions_org_created')
    op.drop_index('idx_solutions_case_implemented')
    op.drop_index('idx_hypotheses_org_created')
    op.drop_index('idx_hypotheses_case_status')

    op.drop_table('solutions')
    op.drop_table('hypotheses')
```

**Design Notes**:
- Multi-tenant isolation via `organization_id` foreign key (enforced at DB level)
- Hypothesis status workflow: `testing` → `confirmed` or `rejected`
- Confidence level: 0.00-1.00 (agent-generated or user-assigned)
- Evidence refs: JSONB array of evidence IDs (allows hypothesis-evidence linking)
- Findings: JSONB object for flexible investigation notes
- Solution steps: JSONB array for structured instructions
- Validation: JSONB object for testing results

---

### 2. Domain Models (SQLAlchemy & Pydantic)

**File**: `faultmaven/models/hypothesis.py`

```python
"""Hypothesis and Solution Domain Models (TASK-026)

Purpose: SQLAlchemy ORM models and Pydantic schemas for hypothesis tracking.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, validator
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import relationship

from faultmaven.infrastructure.database import Base


# ============================================================
# Enums
# ============================================================


class HypothesisStatus(str, Enum):
    """Hypothesis validation status"""
    TESTING = "testing"  # Initial state, under investigation
    CONFIRMED = "confirmed"  # Validated, confidence > 0.8
    REJECTED = "rejected"  # Disproven, confidence < 0.2


class HypothesisSource(str, Enum):
    """How hypothesis was created"""
    MANUAL = "manual"  # User-created
    AI_GENERATED = "ai_generated"  # Agent-generated


# ============================================================
# SQLAlchemy ORM Models
# ============================================================


class Hypothesis(Base):
    """Hypothesis ORM model for investigation tracking"""

    __tablename__ = "hypotheses"

    # Primary key
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    # Foreign keys
    case_id = Column(PGUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    # Core fields
    description = Column(Text, nullable=False)
    status = Column(String(50), default=HypothesisStatus.TESTING.value, nullable=False)
    confidence_level = Column(Numeric(3, 2), nullable=True)  # 0.00 to 1.00

    # Evidence and findings
    evidence_refs = Column(JSONB, default=list)  # List of evidence IDs
    findings = Column(JSONB, default=dict)  # Investigation findings

    # Metadata
    source = Column(String(50), default=HypothesisSource.MANUAL.value, nullable=False)
    agent_metadata = Column(JSONB, default=dict)  # AI-specific metadata

    # Audit trail
    created_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    updated_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    case = relationship("Case", back_populates="hypotheses")
    solutions = relationship("Solution", back_populates="hypothesis")


class Solution(Base):
    """Solution ORM model for problem resolution tracking"""

    __tablename__ = "solutions"

    # Primary key
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    # Foreign keys
    case_id = Column(PGUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    hypothesis_id = Column(PGUUID(as_uuid=True), ForeignKey("hypotheses.id", ondelete="SET NULL"), nullable=True)
    organization_id = Column(PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    # Core fields
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    steps = Column(JSONB, nullable=False)  # Array of solution steps
    validation = Column(JSONB, default=dict)  # Validation results

    # Implementation tracking
    implemented = Column(Boolean, default=False, nullable=False)
    implemented_at = Column(DateTime(timezone=True), nullable=True)
    effectiveness_score = Column(Numeric(3, 2), nullable=True)  # 0.00 to 1.00

    # Knowledge base integration
    knowledge_article_id = Column(PGUUID(as_uuid=True), nullable=True)

    # Audit trail
    created_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    updated_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    case = relationship("Case", back_populates="solutions")
    hypothesis = relationship("Hypothesis", back_populates="solutions")


# ============================================================
# Pydantic Request/Response Models
# ============================================================


class HypothesisCreate(BaseModel):
    """Request model for creating hypothesis"""

    description: str = Field(..., min_length=10, max_length=5000, description="Hypothesis description")
    evidence_refs: List[str] = Field(default_factory=list, description="Evidence IDs supporting this hypothesis")
    source: HypothesisSource = Field(default=HypothesisSource.MANUAL, description="How hypothesis was created")
    agent_metadata: Dict = Field(default_factory=dict, description="AI-specific metadata (if AI-generated)")

    class Config:
        json_schema_extra = {
            "example": {
                "description": "Database connection timeout due to network latency spikes",
                "evidence_refs": ["e1234567-89ab-cdef-0123-456789abcdef"],
                "source": "manual"
            }
        }


class HypothesisUpdate(BaseModel):
    """Request model for updating hypothesis"""

    description: Optional[str] = Field(None, min_length=10, max_length=5000)
    status: Optional[HypothesisStatus] = None
    confidence_level: Optional[Decimal] = Field(None, ge=0, le=1, description="Confidence 0.00-1.00")
    evidence_refs: Optional[List[str]] = None
    findings: Optional[Dict] = None

    @validator('confidence_level')
    def validate_confidence(cls, v):
        if v is not None:
            if v < 0 or v > 1:
                raise ValueError("Confidence level must be between 0.00 and 1.00")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "status": "confirmed",
                "confidence_level": 0.85,
                "findings": {
                    "validation_method": "Network trace analysis",
                    "evidence": "200ms+ latency observed in logs"
                }
            }
        }


class HypothesisResponse(BaseModel):
    """Response model for hypothesis"""

    id: UUID
    case_id: UUID
    description: str
    status: HypothesisStatus
    confidence_level: Optional[Decimal]
    evidence_refs: List[str]
    findings: Dict
    source: HypothesisSource
    agent_metadata: Dict
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SolutionStep(BaseModel):
    """Single step in solution implementation"""

    step_number: int = Field(..., ge=1, description="Step order")
    action: str = Field(..., min_length=10, max_length=1000, description="Action to perform")
    expected_outcome: str = Field(..., min_length=5, max_length=500, description="Expected result")
    validation: Optional[str] = Field(None, max_length=500, description="How to validate this step")


class SolutionCreate(BaseModel):
    """Request model for creating solution"""

    title: str = Field(..., min_length=5, max_length=255, description="Solution title")
    description: str = Field(..., min_length=20, max_length=5000, description="Detailed solution description")
    hypothesis_id: Optional[UUID] = Field(None, description="Related hypothesis ID")
    steps: List[SolutionStep] = Field(..., min_items=1, description="Solution implementation steps")
    validation: Dict = Field(default_factory=dict, description="Validation results")

    class Config:
        json_schema_extra = {
            "example": {
                "title": "Increase database connection timeout",
                "description": "Adjust connection pool settings to handle network latency",
                "hypothesis_id": "h1234567-89ab-cdef-0123-456789abcdef",
                "steps": [
                    {
                        "step_number": 1,
                        "action": "Edit database.yml to increase timeout from 5s to 30s",
                        "expected_outcome": "Connection timeout errors should decrease",
                        "validation": "Monitor error logs for 24 hours"
                    }
                ]
            }
        }


class SolutionUpdate(BaseModel):
    """Request model for updating solution"""

    title: Optional[str] = Field(None, min_length=5, max_length=255)
    description: Optional[str] = Field(None, min_length=20, max_length=5000)
    steps: Optional[List[SolutionStep]] = None
    validation: Optional[Dict] = None
    implemented: Optional[bool] = None
    effectiveness_score: Optional[Decimal] = Field(None, ge=0, le=1)

    @validator('effectiveness_score')
    def validate_effectiveness(cls, v):
        if v is not None:
            if v < 0 or v > 1:
                raise ValueError("Effectiveness score must be between 0.00 and 1.00")
        return v


class SolutionResponse(BaseModel):
    """Response model for solution"""

    id: UUID
    case_id: UUID
    hypothesis_id: Optional[UUID]
    title: str
    description: str
    steps: List[Dict]  # List of SolutionStep dicts
    validation: Dict
    implemented: bool
    implemented_at: Optional[datetime]
    effectiveness_score: Optional[Decimal]
    knowledge_article_id: Optional[UUID]
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
```

---

### 3. Repository Layer

**File**: `faultmaven/repositories/hypothesis_repository.py`

```python
"""Hypothesis Repository (TASK-026)

Purpose: Data access layer for hypothesis CRUD operations.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.models.hypothesis import Hypothesis, HypothesisStatus, HypothesisSource
from faultmaven.infrastructure.observability.tracing import trace


class HypothesisRepository:
    """Repository for hypothesis data access operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @trace("hypothesis_create")
    async def create(
        self,
        case_id: UUID,
        organization_id: UUID,
        description: str,
        created_by: UUID,
        evidence_refs: List[str] = None,
        source: HypothesisSource = HypothesisSource.MANUAL,
        agent_metadata: Dict = None,
    ) -> Hypothesis:
        """Create new hypothesis"""
        hypothesis = Hypothesis(
            case_id=case_id,
            organization_id=organization_id,
            description=description,
            evidence_refs=evidence_refs or [],
            source=source.value,
            agent_metadata=agent_metadata or {},
            created_by=created_by,
            status=HypothesisStatus.TESTING.value,
        )

        self.session.add(hypothesis)
        await self.session.commit()
        await self.session.refresh(hypothesis)

        return hypothesis

    @trace("hypothesis_get_by_id")
    async def get_by_id(self, hypothesis_id: UUID, organization_id: UUID) -> Optional[Hypothesis]:
        """Get hypothesis by ID with multi-tenant isolation"""
        result = await self.session.execute(
            select(Hypothesis).where(
                and_(
                    Hypothesis.id == hypothesis_id,
                    Hypothesis.organization_id == organization_id
                )
            )
        )
        return result.scalar_one_or_none()

    @trace("hypothesis_list_by_case")
    async def list_by_case(
        self,
        case_id: UUID,
        organization_id: UUID,
        status: Optional[HypothesisStatus] = None
    ) -> List[Hypothesis]:
        """List hypotheses for a case with optional status filter"""
        filters = [
            Hypothesis.case_id == case_id,
            Hypothesis.organization_id == organization_id
        ]

        if status:
            filters.append(Hypothesis.status == status.value)

        result = await self.session.execute(
            select(Hypothesis)
            .where(and_(*filters))
            .order_by(desc(Hypothesis.confidence_level), desc(Hypothesis.created_at))
        )

        return list(result.scalars().all())

    @trace("hypothesis_update")
    async def update(
        self,
        hypothesis_id: UUID,
        organization_id: UUID,
        updated_by: UUID,
        **kwargs
    ) -> Optional[Hypothesis]:
        """Update hypothesis fields"""
        hypothesis = await self.get_by_id(hypothesis_id, organization_id)

        if not hypothesis:
            return None

        # Update allowed fields
        for key, value in kwargs.items():
            if hasattr(hypothesis, key) and value is not None:
                setattr(hypothesis, key, value)

        hypothesis.updated_by = updated_by
        hypothesis.updated_at = datetime.now(timezone.utc)

        await self.session.commit()
        await self.session.refresh(hypothesis)

        return hypothesis

    @trace("hypothesis_delete")
    async def delete(self, hypothesis_id: UUID, organization_id: UUID) -> bool:
        """Delete hypothesis (soft delete via CASCADE on case deletion)"""
        hypothesis = await self.get_by_id(hypothesis_id, organization_id)

        if not hypothesis:
            return False

        await self.session.delete(hypothesis)
        await self.session.commit()

        return True

    @trace("hypothesis_count_by_case")
    async def count_by_case(
        self,
        case_id: UUID,
        organization_id: UUID,
        status: Optional[HypothesisStatus] = None
    ) -> int:
        """Count hypotheses for a case"""
        filters = [
            Hypothesis.case_id == case_id,
            Hypothesis.organization_id == organization_id
        ]

        if status:
            filters.append(Hypothesis.status == status.value)

        result = await self.session.execute(
            select(Hypothesis).where(and_(*filters))
        )

        return len(list(result.scalars().all()))
```

**File**: `faultmaven/repositories/solution_repository.py`

```python
"""Solution Repository (TASK-026)

Purpose: Data access layer for solution CRUD operations.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.models.hypothesis import Solution
from faultmaven.infrastructure.observability.tracing import trace


class SolutionRepository:
    """Repository for solution data access operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @trace("solution_create")
    async def create(
        self,
        case_id: UUID,
        organization_id: UUID,
        title: str,
        description: str,
        steps: List[Dict],
        created_by: UUID,
        hypothesis_id: Optional[UUID] = None,
        validation: Dict = None,
    ) -> Solution:
        """Create new solution"""
        solution = Solution(
            case_id=case_id,
            organization_id=organization_id,
            hypothesis_id=hypothesis_id,
            title=title,
            description=description,
            steps=steps,
            validation=validation or {},
            created_by=created_by,
            implemented=False,
        )

        self.session.add(solution)
        await self.session.commit()
        await self.session.refresh(solution)

        return solution

    @trace("solution_get_by_id")
    async def get_by_id(self, solution_id: UUID, organization_id: UUID) -> Optional[Solution]:
        """Get solution by ID with multi-tenant isolation"""
        result = await self.session.execute(
            select(Solution).where(
                and_(
                    Solution.id == solution_id,
                    Solution.organization_id == organization_id
                )
            )
        )
        return result.scalar_one_or_none()

    @trace("solution_list_by_case")
    async def list_by_case(
        self,
        case_id: UUID,
        organization_id: UUID,
        implemented: Optional[bool] = None
    ) -> List[Solution]:
        """List solutions for a case with optional implementation filter"""
        filters = [
            Solution.case_id == case_id,
            Solution.organization_id == organization_id
        ]

        if implemented is not None:
            filters.append(Solution.implemented == implemented)

        result = await self.session.execute(
            select(Solution)
            .where(and_(*filters))
            .order_by(desc(Solution.effectiveness_score), desc(Solution.created_at))
        )

        return list(result.scalars().all())

    @trace("solution_update")
    async def update(
        self,
        solution_id: UUID,
        organization_id: UUID,
        updated_by: UUID,
        **kwargs
    ) -> Optional[Solution]:
        """Update solution fields"""
        solution = await self.get_by_id(solution_id, organization_id)

        if not solution:
            return None

        # Update allowed fields
        for key, value in kwargs.items():
            if hasattr(solution, key) and value is not None:
                setattr(solution, key, value)

        # Auto-set implemented_at if implemented flag changes to True
        if kwargs.get('implemented') and not solution.implemented:
            solution.implemented_at = datetime.now(timezone.utc)

        solution.updated_by = updated_by
        solution.updated_at = datetime.now(timezone.utc)

        await self.session.commit()
        await self.session.refresh(solution)

        return solution

    @trace("solution_delete")
    async def delete(self, solution_id: UUID, organization_id: UUID) -> bool:
        """Delete solution"""
        solution = await self.get_by_id(solution_id, organization_id)

        if not solution:
            return False

        await self.session.delete(solution)
        await self.session.commit()

        return True
```

---

### 4. Investigation Orchestrator (Service Layer)

**File**: `faultmaven/services/domain/investigation_orchestrator.py`

```python
"""Investigation Orchestrator (TASK-026)

Purpose: Coordinates hypothesis lifecycle and agent integration.

Design: Orchestration layer between Case API and Agent framework.
- Agents generate hypotheses (reasoning)
- Orchestrator manages state (business logic)
- Clear separation of concerns
"""

import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from faultmaven.models.hypothesis import (
    Hypothesis,
    HypothesisStatus,
    HypothesisSource,
    Solution,
    SolutionStep,
)
from faultmaven.repositories.hypothesis_repository import HypothesisRepository
from faultmaven.repositories.solution_repository import SolutionRepository
from faultmaven.services.interfaces.case_service import ICaseService
from faultmaven.infrastructure.observability.tracing import trace

logger = logging.getLogger(__name__)


class InvestigationOrchestrator:
    """
    Coordinates agent actions and hypothesis lifecycle.

    Responsibilities:
    - Generate hypotheses via AI agent
    - Manage hypothesis status transitions
    - Link solutions to hypotheses
    - Calculate confidence scores
    - Track investigation progress
    """

    def __init__(
        self,
        hypothesis_repo: HypothesisRepository,
        solution_repo: SolutionRepository,
        case_service: ICaseService,
        agent_manager=None,  # Optional: AgentManager for AI integration
    ):
        self.hypotheses = hypothesis_repo
        self.solutions = solution_repo
        self.cases = case_service
        self.agents = agent_manager  # May be None if AI disabled

    @trace("investigation_generate_hypotheses")
    async def generate_hypotheses(
        self,
        case_id: UUID,
        organization_id: UUID,
        user_id: UUID,
        force_ai: bool = False
    ) -> List[Hypothesis]:
        """
        Generate hypotheses for a case.

        If AI agent available: Generate via LLM
        Otherwise: Return empty list (user creates manually)

        Args:
            case_id: Case to generate hypotheses for
            organization_id: Multi-tenant isolation
            user_id: User requesting generation
            force_ai: Force AI generation (error if unavailable)

        Returns:
            List of generated hypotheses

        Raises:
            ValueError: If force_ai=True and agent unavailable
        """
        if not self.agents:
            if force_ai:
                raise ValueError("AI agent not available for hypothesis generation")
            logger.info(f"No AI agent available for case {case_id}, skipping auto-generation")
            return []

        # 1. Get case context
        case = await self.cases.get_case(case_id, organization_id)

        if not case:
            raise ValueError(f"Case {case_id} not found")

        # 2. Agent generates hypotheses
        logger.info(f"Generating hypotheses via AI for case {case_id}")
        raw_hypotheses = await self.agents.generate_hypotheses(
            case_description=case.description,
            evidence=case.evidence or [],
            case_metadata=case.metadata or {}
        )

        # 3. Store hypotheses in database
        created_hypotheses = []
        for hyp_data in raw_hypotheses:
            hypothesis = await self.hypotheses.create(
                case_id=case_id,
                organization_id=organization_id,
                description=hyp_data['description'],
                created_by=user_id,
                evidence_refs=hyp_data.get('evidence_refs', []),
                source=HypothesisSource.AI_GENERATED,
                agent_metadata={
                    'model': hyp_data.get('model', 'unknown'),
                    'initial_confidence': hyp_data.get('confidence', 0.5),
                    'reasoning': hyp_data.get('reasoning', ''),
                }
            )

            # 4. Set initial confidence from AI
            if 'confidence' in hyp_data:
                await self.hypotheses.update(
                    hypothesis_id=hypothesis.id,
                    organization_id=organization_id,
                    updated_by=user_id,
                    confidence_level=Decimal(str(hyp_data['confidence']))
                )

            created_hypotheses.append(hypothesis)

        logger.info(f"Generated {len(created_hypotheses)} hypotheses for case {case_id}")
        return created_hypotheses

    @trace("investigation_validate_hypothesis")
    async def validate_hypothesis(
        self,
        hypothesis_id: UUID,
        organization_id: UUID,
        user_id: UUID,
        validation_result: Dict,
    ) -> Hypothesis:
        """
        Update hypothesis with validation results.

        Automatically transitions status based on confidence:
        - confidence > 0.8 → confirmed
        - confidence < 0.2 → rejected
        - otherwise → testing (remains in testing)

        Args:
            hypothesis_id: Hypothesis to validate
            organization_id: Multi-tenant isolation
            user_id: User performing validation
            validation_result: Validation findings
                - confidence: float (0.0-1.0)
                - method: str (how validated)
                - evidence: str (supporting evidence)

        Returns:
            Updated hypothesis
        """
        confidence = Decimal(str(validation_result.get('confidence', 0.5)))

        # Determine status based on confidence
        if confidence >= Decimal('0.8'):
            new_status = HypothesisStatus.CONFIRMED
        elif confidence <= Decimal('0.2'):
            new_status = HypothesisStatus.REJECTED
        else:
            new_status = HypothesisStatus.TESTING

        # Update hypothesis
        hypothesis = await self.hypotheses.update(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
            updated_by=user_id,
            status=new_status.value,
            confidence_level=confidence,
            findings={
                'validation_method': validation_result.get('method', 'manual'),
                'evidence': validation_result.get('evidence', ''),
                'notes': validation_result.get('notes', ''),
                'validated_at': validation_result.get('validated_at'),
            }
        )

        logger.info(
            f"Validated hypothesis {hypothesis_id}: "
            f"confidence={confidence}, status={new_status.value}"
        )

        return hypothesis

    @trace("investigation_link_solution")
    async def link_solution_to_hypothesis(
        self,
        solution_id: UUID,
        hypothesis_id: UUID,
        organization_id: UUID,
        user_id: UUID,
    ) -> Solution:
        """
        Link a solution to a confirmed hypothesis.

        Validates that hypothesis is confirmed before linking.

        Args:
            solution_id: Solution to link
            hypothesis_id: Hypothesis to link to
            organization_id: Multi-tenant isolation
            user_id: User performing link

        Returns:
            Updated solution

        Raises:
            ValueError: If hypothesis not confirmed
        """
        # Verify hypothesis is confirmed
        hypothesis = await self.hypotheses.get_by_id(hypothesis_id, organization_id)

        if not hypothesis:
            raise ValueError(f"Hypothesis {hypothesis_id} not found")

        if hypothesis.status != HypothesisStatus.CONFIRMED.value:
            raise ValueError(
                f"Cannot link solution to {hypothesis.status} hypothesis. "
                "Only confirmed hypotheses can have solutions."
            )

        # Link solution
        solution = await self.solutions.update(
            solution_id=solution_id,
            organization_id=organization_id,
            updated_by=user_id,
            hypothesis_id=hypothesis_id
        )

        logger.info(f"Linked solution {solution_id} to hypothesis {hypothesis_id}")
        return solution

    @trace("investigation_get_progress")
    async def get_investigation_progress(
        self,
        case_id: UUID,
        organization_id: UUID
    ) -> Dict:
        """
        Get investigation progress summary.

        Returns:
            Progress summary with counts and percentages
        """
        # Count hypotheses by status
        total = await self.hypotheses.count_by_case(case_id, organization_id)
        confirmed = await self.hypotheses.count_by_case(
            case_id, organization_id, status=HypothesisStatus.CONFIRMED
        )
        rejected = await self.hypotheses.count_by_case(
            case_id, organization_id, status=HypothesisStatus.REJECTED
        )
        testing = total - confirmed - rejected

        # Count solutions
        all_solutions = await self.solutions.list_by_case(case_id, organization_id)
        implemented = sum(1 for s in all_solutions if s.implemented)

        return {
            'hypotheses': {
                'total': total,
                'confirmed': confirmed,
                'rejected': rejected,
                'testing': testing,
                'completion_rate': round(((confirmed + rejected) / total * 100), 1) if total > 0 else 0
            },
            'solutions': {
                'total': len(all_solutions),
                'implemented': implemented,
                'implementation_rate': round((implemented / len(all_solutions) * 100), 1) if all_solutions else 0
            }
        }
```

---

### 5. API Endpoints

**File**: `faultmaven/api/v1/routes/hypotheses.py`

```python
"""Hypothesis Tracking API Routes (TASK-026)

Purpose: REST API endpoints for hypothesis and solution management.

Endpoints:
1. POST   /api/v1/cases/{case_id}/hypotheses        - Create hypothesis
2. GET    /api/v1/cases/{case_id}/hypotheses        - List hypotheses for case
3. GET    /api/v1/hypotheses/{hypothesis_id}        - Get hypothesis by ID
4. PUT    /api/v1/hypotheses/{hypothesis_id}        - Update hypothesis
5. DELETE /api/v1/hypotheses/{hypothesis_id}        - Delete hypothesis
6. POST   /api/v1/cases/{case_id}/solutions         - Create solution
7. GET    /api/v1/cases/{case_id}/solutions         - List solutions for case
8. PUT    /api/v1/solutions/{solution_id}           - Update solution
9. POST   /api/v1/hypotheses/{hypothesis_id}/validate - Validate hypothesis

Authentication:
- JWT Bearer token: Authorization: Bearer <token>
"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from faultmaven.models.hypothesis import (
    HypothesisCreate,
    HypothesisUpdate,
    HypothesisResponse,
    HypothesisStatus,
    SolutionCreate,
    SolutionUpdate,
    SolutionResponse,
)
from faultmaven.models.auth import DevUser
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import (
    get_hypothesis_repository,
    get_solution_repository,
    get_investigation_orchestrator,
    get_tenant_provider,
)
from faultmaven.repositories.hypothesis_repository import HypothesisRepository
from faultmaven.repositories.solution_repository import SolutionRepository
from faultmaven.services.domain.investigation_orchestrator import InvestigationOrchestrator
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import NotFoundError, ValidationException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Hypotheses & Solutions"])


# ============================================================
# Hypothesis Endpoints
# ============================================================


@router.post(
    "/cases/{case_id}/hypotheses",
    response_model=HypothesisResponse,
    status_code=status.HTTP_201_CREATED
)
@trace("api_create_hypothesis")
async def create_hypothesis(
    case_id: str,
    request: HypothesisCreate,
    current_user: DevUser = Depends(require_authentication),
    hypothesis_repo: HypothesisRepository = Depends(get_hypothesis_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> HypothesisResponse:
    """
    Create new hypothesis for case.

    Hypotheses represent potential explanations for the problem described in the case.
    Users can manually create hypotheses, or use AI generation (POST /generate-hypotheses).

    Args:
        case_id: Case to add hypothesis to
        request: Hypothesis creation data
        current_user: Authenticated user
        hypothesis_repo: Hypothesis repository
        tenant_provider: Multi-tenant provider

    Returns:
        Created hypothesis

    Raises:
        401: Authentication required
        404: Case not found
        422: Validation error
    """
    try:
        # Get organization ID via TenantProvider (deployment-neutral)
        org_id = await tenant_provider.get_organization_id(current_user)

        # Create hypothesis
        hypothesis = await hypothesis_repo.create(
            case_id=UUID(case_id),
            organization_id=org_id,
            description=request.description,
            created_by=UUID(current_user.user_id),
            evidence_refs=request.evidence_refs,
            source=request.source,
            agent_metadata=request.agent_metadata,
        )

        logger.info(
            f"Created hypothesis {hypothesis.id} for case {case_id} "
            f"by user {current_user.user_id}"
        )

        return HypothesisResponse.from_orm(hypothesis)

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create hypothesis: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create hypothesis"
        )


@router.get(
    "/cases/{case_id}/hypotheses",
    response_model=List[HypothesisResponse]
)
@trace("api_list_hypotheses")
async def list_hypotheses(
    case_id: str,
    status_filter: Optional[HypothesisStatus] = Query(None, alias="status"),
    current_user: DevUser = Depends(require_authentication),
    hypothesis_repo: HypothesisRepository = Depends(get_hypothesis_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> List[HypothesisResponse]:
    """
    List all hypotheses for a case.

    Hypotheses are returned ordered by confidence level (desc) and creation date (desc).

    Query Parameters:
        status: Filter by hypothesis status (testing, confirmed, rejected)

    Args:
        case_id: Case ID
        status_filter: Optional status filter
        current_user: Authenticated user
        hypothesis_repo: Hypothesis repository
        tenant_provider: Multi-tenant provider

    Returns:
        List of hypotheses
    """
    org_id = await tenant_provider.get_organization_id(current_user)

    hypotheses = await hypothesis_repo.list_by_case(
        case_id=UUID(case_id),
        organization_id=org_id,
        status=status_filter
    )

    return [HypothesisResponse.from_orm(h) for h in hypotheses]


@router.get(
    "/hypotheses/{hypothesis_id}",
    response_model=HypothesisResponse
)
@trace("api_get_hypothesis")
async def get_hypothesis(
    hypothesis_id: str,
    current_user: DevUser = Depends(require_authentication),
    hypothesis_repo: HypothesisRepository = Depends(get_hypothesis_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> HypothesisResponse:
    """Get hypothesis by ID"""
    org_id = await tenant_provider.get_organization_id(current_user)

    hypothesis = await hypothesis_repo.get_by_id(UUID(hypothesis_id), org_id)

    if not hypothesis:
        raise NotFoundError("Hypothesis", hypothesis_id)

    return HypothesisResponse.from_orm(hypothesis)


@router.put(
    "/hypotheses/{hypothesis_id}",
    response_model=HypothesisResponse
)
@trace("api_update_hypothesis")
async def update_hypothesis(
    hypothesis_id: str,
    request: HypothesisUpdate,
    current_user: DevUser = Depends(require_authentication),
    hypothesis_repo: HypothesisRepository = Depends(get_hypothesis_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> HypothesisResponse:
    """
    Update hypothesis.

    Allows updating description, status, confidence level, evidence references, and findings.
    Status transitions: testing → confirmed/rejected

    Args:
        hypothesis_id: Hypothesis ID
        request: Update data
        current_user: Authenticated user
        hypothesis_repo: Hypothesis repository
        tenant_provider: Multi-tenant provider

    Returns:
        Updated hypothesis

    Raises:
        404: Hypothesis not found
        422: Validation error
    """
    org_id = await tenant_provider.get_organization_id(current_user)

    # Convert Pydantic model to dict, exclude None values
    update_data = request.dict(exclude_none=True)

    hypothesis = await hypothesis_repo.update(
        hypothesis_id=UUID(hypothesis_id),
        organization_id=org_id,
        updated_by=UUID(current_user.user_id),
        **update_data
    )

    if not hypothesis:
        raise NotFoundError("Hypothesis", hypothesis_id)

    logger.info(f"Updated hypothesis {hypothesis_id} by user {current_user.user_id}")

    return HypothesisResponse.from_orm(hypothesis)


@router.delete(
    "/hypotheses/{hypothesis_id}",
    status_code=status.HTTP_204_NO_CONTENT
)
@trace("api_delete_hypothesis")
async def delete_hypothesis(
    hypothesis_id: str,
    current_user: DevUser = Depends(require_authentication),
    hypothesis_repo: HypothesisRepository = Depends(get_hypothesis_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Delete hypothesis"""
    org_id = await tenant_provider.get_organization_id(current_user)

    deleted = await hypothesis_repo.delete(UUID(hypothesis_id), org_id)

    if not deleted:
        raise NotFoundError("Hypothesis", hypothesis_id)

    logger.info(f"Deleted hypothesis {hypothesis_id} by user {current_user.user_id}")


# ============================================================
# Solution Endpoints
# ============================================================


@router.post(
    "/cases/{case_id}/solutions",
    response_model=SolutionResponse,
    status_code=status.HTTP_201_CREATED
)
@trace("api_create_solution")
async def create_solution(
    case_id: str,
    request: SolutionCreate,
    current_user: DevUser = Depends(require_authentication),
    solution_repo: SolutionRepository = Depends(get_solution_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> SolutionResponse:
    """
    Create solution for case.

    Solutions document how to resolve the problem. Optionally link to a confirmed hypothesis.

    Args:
        case_id: Case to add solution to
        request: Solution creation data
        current_user: Authenticated user
        solution_repo: Solution repository
        tenant_provider: Multi-tenant provider

    Returns:
        Created solution

    Raises:
        401: Authentication required
        404: Case not found
        422: Validation error (e.g., hypothesis not confirmed)
    """
    try:
        org_id = await tenant_provider.get_organization_id(current_user)

        # Convert SolutionStep objects to dicts
        steps_data = [step.dict() for step in request.steps]

        solution = await solution_repo.create(
            case_id=UUID(case_id),
            organization_id=org_id,
            title=request.title,
            description=request.description,
            steps=steps_data,
            created_by=UUID(current_user.user_id),
            hypothesis_id=request.hypothesis_id,
            validation=request.validation,
        )

        logger.info(
            f"Created solution {solution.id} for case {case_id} "
            f"by user {current_user.user_id}"
        )

        return SolutionResponse.from_orm(solution)

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create solution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create solution"
        )


@router.get(
    "/cases/{case_id}/solutions",
    response_model=List[SolutionResponse]
)
@trace("api_list_solutions")
async def list_solutions(
    case_id: str,
    implemented: Optional[bool] = Query(None),
    current_user: DevUser = Depends(require_authentication),
    solution_repo: SolutionRepository = Depends(get_solution_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> List[SolutionResponse]:
    """
    List all solutions for a case.

    Query Parameters:
        implemented: Filter by implementation status (true/false)

    Args:
        case_id: Case ID
        implemented: Optional implementation filter
        current_user: Authenticated user
        solution_repo: Solution repository
        tenant_provider: Multi-tenant provider

    Returns:
        List of solutions
    """
    org_id = await tenant_provider.get_organization_id(current_user)

    solutions = await solution_repo.list_by_case(
        case_id=UUID(case_id),
        organization_id=org_id,
        implemented=implemented
    )

    return [SolutionResponse.from_orm(s) for s in solutions]


@router.put(
    "/solutions/{solution_id}",
    response_model=SolutionResponse
)
@trace("api_update_solution")
async def update_solution(
    solution_id: str,
    request: SolutionUpdate,
    current_user: DevUser = Depends(require_authentication),
    solution_repo: SolutionRepository = Depends(get_solution_repository),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> SolutionResponse:
    """
    Update solution.

    Allows updating title, description, steps, validation, implementation status,
    and effectiveness score.

    Args:
        solution_id: Solution ID
        request: Update data
        current_user: Authenticated user
        solution_repo: Solution repository
        tenant_provider: Multi-tenant provider

    Returns:
        Updated solution

    Raises:
        404: Solution not found
    """
    org_id = await tenant_provider.get_organization_id(current_user)

    # Convert Pydantic model to dict, exclude None values
    update_data = request.dict(exclude_none=True)

    # Convert SolutionStep objects to dicts if present
    if 'steps' in update_data:
        update_data['steps'] = [step.dict() for step in update_data['steps']]

    solution = await solution_repo.update(
        solution_id=UUID(solution_id),
        organization_id=org_id,
        updated_by=UUID(current_user.user_id),
        **update_data
    )

    if not solution:
        raise NotFoundError("Solution", solution_id)

    logger.info(f"Updated solution {solution_id} by user {current_user.user_id}")

    return SolutionResponse.from_orm(solution)


# ============================================================
# Investigation Orchestration Endpoints
# ============================================================


class HypothesisValidationRequest(BaseModel):
    """Request model for hypothesis validation"""

    confidence: float = Field(..., ge=0.0, le=1.0, description="Validation confidence 0.0-1.0")
    method: str = Field(..., min_length=5, max_length=200, description="Validation method used")
    evidence: str = Field(..., min_length=10, description="Supporting evidence")
    notes: Optional[str] = Field(None, max_length=1000, description="Additional notes")


@router.post(
    "/hypotheses/{hypothesis_id}/validate",
    response_model=HypothesisResponse
)
@trace("api_validate_hypothesis")
async def validate_hypothesis(
    hypothesis_id: str,
    request: HypothesisValidationRequest,
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
) -> HypothesisResponse:
    """
    Validate hypothesis with confidence scoring.

    Automatically transitions hypothesis status based on confidence:
    - confidence >= 0.8 → confirmed
    - confidence <= 0.2 → rejected
    - otherwise → testing (remains in testing)

    Args:
        hypothesis_id: Hypothesis to validate
        request: Validation data (confidence, method, evidence)
        current_user: Authenticated user
        orchestrator: Investigation orchestrator
        tenant_provider: Multi-tenant provider

    Returns:
        Updated hypothesis with new status and confidence

    Raises:
        404: Hypothesis not found
    """
    org_id = await tenant_provider.get_organization_id(current_user)

    hypothesis = await orchestrator.validate_hypothesis(
        hypothesis_id=UUID(hypothesis_id),
        organization_id=org_id,
        user_id=UUID(current_user.user_id),
        validation_result=request.dict()
    )

    if not hypothesis:
        raise NotFoundError("Hypothesis", hypothesis_id)

    logger.info(
        f"Validated hypothesis {hypothesis_id}: "
        f"confidence={request.confidence}, status={hypothesis.status}"
    )

    return HypothesisResponse.from_orm(hypothesis)
```

---

### 6. Dependency Injection Updates

**File**: `faultmaven/api/v1/dependencies.py` (add these functions)

```python
# Add to existing dependencies.py

from faultmaven.repositories.hypothesis_repository import HypothesisRepository
from faultmaven.repositories.solution_repository import SolutionRepository
from faultmaven.services.domain.investigation_orchestrator import InvestigationOrchestrator


async def get_hypothesis_repository() -> HypothesisRepository:
    """Get hypothesis repository from DI container"""
    from faultmaven.container import container
    return container.hypothesis_repository()


async def get_solution_repository() -> SolutionRepository:
    """Get solution repository from DI container"""
    from faultmaven.container import container
    return container.solution_repository()


async def get_investigation_orchestrator() -> InvestigationOrchestrator:
    """Get investigation orchestrator from DI container"""
    from faultmaven.container import container
    return container.investigation_orchestrator()
```

**File**: `faultmaven/container.py` (add these registrations)

```python
# Add to existing container.py

from faultmaven.repositories.hypothesis_repository import HypothesisRepository
from faultmaven.repositories.solution_repository import SolutionRepository
from faultmaven.services.domain.investigation_orchestrator import InvestigationOrchestrator


class Container:
    # ... existing container code ...

    def hypothesis_repository(self) -> HypothesisRepository:
        """Get hypothesis repository"""
        return HypothesisRepository(session=self.db_session())

    def solution_repository(self) -> SolutionRepository:
        """Get solution repository"""
        return SolutionRepository(session=self.db_session())

    def investigation_orchestrator(self) -> InvestigationOrchestrator:
        """Get investigation orchestrator"""
        return InvestigationOrchestrator(
            hypothesis_repo=self.hypothesis_repository(),
            solution_repo=self.solution_repository(),
            case_service=self.case_service(),
            agent_manager=None,  # TODO: Wire in AgentManager when ready
        )
```

---

## Testing Strategy

### Test Requirements

**Total Tests**: 30+ tests
- Repository tests: 12 tests (6 hypothesis + 6 solution)
- Orchestrator tests: 8 tests
- API integration tests: 10 tests
- E2E workflow tests: 3 tests

### Test Files Structure

```
tests/
├── repositories/
│   ├── test_hypothesis_repository.py  # 6 tests
│   └── test_solution_repository.py    # 6 tests
├── services/
│   └── test_investigation_orchestrator.py  # 8 tests
├── api/v1/routes/
│   └── test_hypotheses.py  # 10 tests
└── integration/
    └── test_investigation_workflow.py  # 3 E2E tests
```

### Test Coverage Targets

| Module | Coverage Target | Priority |
|--------|----------------|----------|
| HypothesisRepository | 95%+ | CRITICAL |
| SolutionRepository | 95%+ | CRITICAL |
| InvestigationOrchestrator | 90%+ | CRITICAL |
| API Routes (hypotheses.py) | 90%+ | CRITICAL |
| E2E Workflows | 80%+ | HIGH |

---

## Implementation Timeline

### Day 1-3: Database & Repository Layer (3 days)

**Day 1**:
- Create Alembic migration (`20250101_add_hypotheses_solutions.py`)
- Run migration: `alembic upgrade head`
- Verify tables created

**Day 2**:
- Implement `Hypothesis` and `Solution` SQLAlchemy models
- Implement `HypothesisRepository` with CRUD operations
- Write 6 repository tests for hypothesis

**Day 3**:
- Implement `SolutionRepository` with CRUD operations
- Write 6 repository tests for solution
- Verify 12/12 repository tests passing

---

### Day 4-6: Service Layer (Investigation Orchestrator) (3 days)

**Day 4**:
- Implement `InvestigationOrchestrator` class
- Implement `generate_hypotheses()` method (stub agent integration)
- Implement `validate_hypothesis()` method (confidence-based status transitions)

**Day 5**:
- Implement `link_solution_to_hypothesis()` method
- Implement `get_investigation_progress()` method
- Wire orchestrator into DI container

**Day 6**:
- Write 8 orchestrator tests
- Mock agent integration for testing
- Verify 20/20 tests passing (12 repo + 8 orchestrator)

---

### Day 7-8: API Endpoints (2 days)

**Day 7**:
- Create `hypotheses.py` router file
- Implement 5 hypothesis endpoints (create, list, get, update, delete)
- Implement `POST /hypotheses/{id}/validate` orchestration endpoint
- Wire into main app router

**Day 8**:
- Implement 3 solution endpoints (create, list, update)
- Test all endpoints with Postman/httpie
- Write 10 API integration tests
- Verify 30/30 tests passing

---

### Day 9-10: E2E Workflows & Documentation (2 days)

**Day 9**:
- Write 3 E2E workflow tests:
  1. Create case → Add hypothesis → Validate → Confirm
  2. Create hypothesis → Add solution → Link to hypothesis
  3. Full investigation: Multiple hypotheses → Best solution → Case resolution
- Run full test suite: 33/33 tests passing

**Day 10**:
- Update OpenAPI documentation
- Write usage examples (curl, httpie, Python SDK)
- Create PR description
- Final review and merge

---

## Success Criteria

### Functional Requirements

- ✅ 3 CRITICAL endpoints implemented:
  1. `POST /api/v1/cases/{case_id}/hypotheses`
  2. `PUT /api/v1/hypotheses/{id}`
  3. `POST /api/v1/cases/{case_id}/solutions`
- ✅ 6 additional endpoints for complete CRUD
- ✅ Investigation orchestrator integrates with agentic framework (stubbed initially)
- ✅ Multi-tenant isolation enforced
- ✅ Deployment-neutral (uses TenantProvider)

### Non-Functional Requirements

- ✅ 30+ tests passing (repository, service, API, E2E)
- ✅ 90%+ test coverage
- ✅ Performance: < 200ms p95 latency for CRUD operations
- ✅ Security: JWT authentication on all endpoints
- ✅ Observability: Tracing on all service methods

### Acceptance Criteria

1. **Database Schema**
   - Alembic migration runs successfully
   - Tables created with proper indexes and foreign keys
   - Multi-tenant isolation enforced at DB level

2. **Repository Layer**
   - All CRUD operations implemented
   - Multi-tenant filtering on all queries
   - 12/12 repository tests passing

3. **Service Layer**
   - Investigation orchestrator coordinates hypothesis lifecycle
   - Confidence-based status transitions working
   - Agent integration ready (stubbed initially)
   - 8/8 orchestrator tests passing

4. **API Layer**
   - 9 endpoints fully functional
   - JWT authentication on all endpoints
   - TenantProvider integration
   - 10/10 API tests passing

5. **E2E Workflows**
   - Complete investigation workflow tested
   - Hypothesis → solution → resolution path verified
   - 3/3 E2E tests passing

---

## Dependencies & Integration Points

### External Dependencies

- **TASK-023 (TenantProvider)**: ✅ MERGED - Required for deployment-neutral org resolution
- **TASK-024 (Report Module)**: ✅ MERGED - Reports use hypotheses/solutions as context
- **Agentic Framework**: 🟡 OPTIONAL - Stub initially, integrate later

### Integration Points

1. **Case Service**: Get case context for hypothesis generation
2. **TenantProvider**: Multi-tenant organization resolution
3. **Agent Manager** (optional): AI-generated hypotheses
4. **Knowledge Base** (future): Solutions feed into KB for reuse

---

## Risk Assessment

### Risk 1: Agent Integration Complexity

**Likelihood**: MEDIUM
**Impact**: MEDIUM

**Mitigation**:
- Stub agent integration initially (return empty list)
- InvestigationOrchestrator accepts `agent_manager=None`
- Users can create hypotheses manually
- AI integration added incrementally later

**Contingency**: Skip AI generation, deliver manual hypothesis tracking only

---

### Risk 2: Database Migration Issues

**Likelihood**: LOW
**Impact**: HIGH

**Mitigation**:
- Test migration on local SQLite first
- Test migration on PostgreSQL in Docker
- Verify rollback with `alembic downgrade -1`
- Backup production DB before migration

**Contingency**: Rollback migration, fix issues, rerun

---

### Risk 3: Timeline Overrun (Complex Orchestrator)

**Likelihood**: MEDIUM
**Impact**: MEDIUM

**Mitigation**:
- Start with simple orchestrator (no AI)
- Focus on CRUD operations first (Days 1-8)
- Defer complex AI logic to later iteration
- Daily progress check-ins

**Contingency**: Reduce scope - deliver CRUD endpoints only, defer orchestrator to TASK-027

---

## Deliverables

### Code Deliverables

1. **Database Migration**: `alembic/versions/20250101_add_hypotheses_solutions.py`
2. **Models**: `faultmaven/models/hypothesis.py`
3. **Repositories**:
   - `faultmaven/repositories/hypothesis_repository.py`
   - `faultmaven/repositories/solution_repository.py`
4. **Service**: `faultmaven/services/domain/investigation_orchestrator.py`
5. **API Routes**: `faultmaven/api/v1/routes/hypotheses.py`
6. **Tests**:
   - `tests/repositories/test_hypothesis_repository.py`
   - `tests/repositories/test_solution_repository.py`
   - `tests/services/test_investigation_orchestrator.py`
   - `tests/api/v1/routes/test_hypotheses.py`
   - `tests/integration/test_investigation_workflow.py`

### Documentation Deliverables

1. **API Documentation**: OpenAPI schema with examples
2. **Usage Guide**: Hypothesis tracking workflow examples
3. **Integration Guide**: How to integrate agent-generated hypotheses
4. **PR Description**: Comprehensive summary with screenshots

---

## Post-Implementation Tasks

### Week 7-8 (TASK-027 Integration)

1. **Wire Agent Manager**: Replace stub with real AgentManager
2. **Test AI Generation**: Verify hypothesis generation with LLM
3. **Confidence Scoring**: Implement AI-based confidence calculation
4. **Knowledge Base Integration**: Feed solutions into KB for reuse

### Phase 2 (Stabilization)

1. **Performance Optimization**: Index tuning, query optimization
2. **Caching**: Redis cache for frequently accessed hypotheses
3. **Analytics**: Track hypothesis success rates, solution effectiveness
4. **UI Integration**: Build React components for hypothesis management

---

## Conclusion

**TASK-026 (Hypothesis & Solution Tracking)** implements the core investigation workflow for FaultMaven's troubleshooting platform.

**Key Achievements**:
- 3 CRITICAL endpoints + 6 supporting endpoints (9 total)
- Investigation orchestrator for hypothesis lifecycle management
- Multi-tenant isolation and deployment neutrality
- 30+ tests with 90%+ coverage

**Timeline**: 2 weeks (10 working days)

**Next Task**: TASK-027 - Session Messages & Agent Chat (3 CRITICAL endpoints, streaming responses)

---

**Document Metadata**:
- **Created**: 2025-12-31
- **Author**: Solutions Architect
- **Version**: 1.0
- **Status**: READY FOR IMPLEMENTATION
- **Estimated Effort**: 80 hours (2 engineers × 1 week)

**Related Documents**:
- `/home/swhouse/product/faultmaven/docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md`
- `/home/swhouse/product/faultmaven/docs/working/TASK-024-REPORT-MODULE.md`
- `/home/swhouse/product/faultmaven/docs/working/TASK-025-STRATEGIC-SKIP-ANALYSIS.md`
- `/home/swhouse/product/faultmaven/docs/working/PHASE-0-COMPLETION-AND-NEXT-STEPS.md`

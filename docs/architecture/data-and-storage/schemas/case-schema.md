# FaultMaven Case Storage Design - Performant Production Standard

**Version**: 3.4
**Status**: Authoritative Standard
**Supersedes**: case-data-model-design.md, db-design-specifications.md
**Last Updated**: 2026-04-18

> **IMPORTANT**: `organization_id` is **ONLY** stored on the `cases` table (top-level entity).
> Child tables (evidence, hypotheses, solutions, etc.) do **NOT** have `organization_id` columns.
> Organization filtering is achieved via JOIN to the `cases` table for data integrity and simplicity.

---

## Implementation Status

**Current State** (as of 2026-04-18):

| Component | Status | Location |
|-----------|--------|----------|
| ✅ Design | Approved | This document |
| ✅ PostgreSQL Schema | Complete | `docs/reference/database/001_initial_hybrid_schema.sql` |
| ✅ SQLite Schema | Complete | Auto-created by `SQLiteCaseRepository` |
| ✅ Reports Migration | Complete | `docs/reference/database/005_add_reports_table.sql` (TD-001) |
| ✅ PostgreSQL Repository | Complete | `postgresql_hybrid_case_repository.py` |
| ✅ SQLite Repository | Complete | `sqlite_case_repository.py` (PR #120) |
| ✅ SQLite Integration Tests | Complete | 8 tests passing with real SQLite database |
| ⏳ PostgreSQL Tests | Pending | Not yet run against real PostgreSQL |
| ⏳ Performance Validation | Pending | Benchmarks needed |
| ⏳ Production Deploy | Pending | PostgreSQL not yet deployed to K8s |

**Active Implementations**:

- **Development**: `InMemoryCaseRepository` (Python dict, fast iteration)
- **Local Deployment**: `SQLiteCaseRepository` (single-file database, ✅ tested)
- **Production Target**: `PostgreSQLHybridCaseRepository` (distributed K8s, pending deployment)

**Multi-Dialect Support**:

The hybrid schema design supports **both** PostgreSQL and SQLite:

- **PostgreSQL**: Uses optimized PostgreSQL features (jsonb_build_object, FILTER, to_tsvector)
- **SQLite**: Uses SQLite-compatible SQL (JSON strings, CASE expressions, LIKE pattern matching)
- **Automatic Detection**: `SessionlessCaseRepository` detects dialect at runtime and instantiates appropriate repository

**Implementation Highlights**:

- ✅ 1,450 lines of SQLite-compatible SQL in `SQLiteCaseRepository`
- ✅ All 24 PostgreSQL-specific features replaced with SQLite equivalents
- ✅ Full feature parity between SQLite and PostgreSQL implementations
- ✅ Same hybrid normalized schema (cases + 6 related tables)
- ✅ Zero configuration - works automatically based on `DATABASE_URL`

---

## Executive Summary

This document defines the **authoritative storage design** for FaultMaven case data across all environments (development, testing, production).

**Key Principles**:
- **Pragmatic Hybrid Approach**: Normalize high-cardinality data, use JSONB for flexible low-cardinality data
- **Performance-First**: Optimize for FaultMaven's actual access patterns
- **Environment-Agnostic**: Same logical design, different physical implementations (InMemory vs PostgreSQL)
- **Production-Ready**: Designed for K8s PostgreSQL deployment at scale

**Design Philosophy**:
> "Normalize what you query, embed what you don't"

**Development Philosophy**:
> "Build it clean, build it right. No backward compatibility needed during development."

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Storage Implementations](#2-storage-implementations)
3. [Data Model](#3-data-model)
4. [PostgreSQL Schema](#4-postgresql-schema)
5. [Normalization Decisions](#5-normalization-decisions)
6. [Performance Characteristics](#6-performance-characteristics)
7. [Concurrency Model](#7-concurrency-model)
8. [Testing Requirements](#8-testing-requirements)
9. [Implementation Checklist](#9-implementation-checklist)

---

## 1. Architecture Overview

### 1.1 Logical vs Physical Design

**Logical Model** (Application Layer):
- Python Pydantic models
- Rich object graph with nested structures
- Defined in `faultmaven/models/case.py`

**Physical Storage** (Persistence Layer):
- **Development**: InMemory (Python dict)
- **Production**: PostgreSQL (Hybrid normalized + JSONB)

### 1.2 Access Pattern Analysis

FaultMaven's case data has predictable access patterns:

| Operation | Frequency | Pattern |
|-----------|-----------|---------|
| Load complete case | Very High | Always fetch ALL case data together |
| Update case state | High | Update entire case (turn-based) |
| Query evidence by type | Medium | Filter/search evidence within case |
| Query hypothesis by status | Medium | Track hypothesis testing progress |
| Search across cases | Low | Find cases by text/status/user |
| Analytics queries | Low | "Show all evidence type X across cases" |

**Key Insight**: Cases are loaded and updated as **complete units**, but evidence/hypotheses need **individual filtering**.

---

## 2. Storage Implementations

### 2.1 Repository Pattern

```python
# Abstract interface
class CaseRepository(ABC):
    async def save(self, case: Case) -> Case
    async def get(self, case_id: str) -> Optional[Case]
    async def list(...) -> tuple[List[Case], int]
    async def delete(self, case_id: str) -> bool
```

### 2.2 InMemory Implementation (Development/Testing)

**File**: `faultmaven/infrastructure/persistence/case_repository.py`

```python
class InMemoryCaseRepository(CaseRepository):
    """Stores Case objects directly in Python dictionary."""

    def __init__(self):
        self._cases: Dict[str, Case] = {}

    async def save(self, case: Case) -> Case:
        self._cases[case.case_id] = case
        return case
```

**Characteristics**:
- ✅ Simple, fast, no setup needed
- ✅ Perfect for unit tests
- ❌ Data lost on restart
- ❌ No persistence

**When to use**: Local development, unit tests, demos

### 2.3 SQLite Implementation (Local Deployment)

**File**: `faultmaven/modules/case/infrastructure/sqlite_case_repository.py`

```python
class SQLiteCaseRepository(CaseRepository):
    """SQLite repository using SQLite-compatible SQL."""

    def __init__(self, db_session):
        self.db = db_session

    async def save(self, case: Case) -> Case:
        # Uses SQLite-compatible SQL:
        # - No ::jsonb type casts
        # - No jsonb_build_object()
        # - No FILTER clauses
        # - LIKE instead of to_tsvector/ts_rank
```

**Characteristics**:

- ✅ Persistent across restarts (single file)
- ✅ ACID transactions
- ✅ No external dependencies (no PostgreSQL needed)
- ✅ Full feature parity with PostgreSQL
- ✅ Same hybrid normalized schema
- ⚠️ Limited concurrency (SQLite limitation)
- ❌ Not suitable for distributed systems

**When to use**: Local development, self-hosted single-node deployment, demos

**SQLite vs PostgreSQL SQL Differences**:

| Feature          | PostgreSQL                   | SQLite                          |
|------------------|------------------------------|---------------------------------|
| Type Casts       | `:inquiry::jsonb`         | Plain parameter binding         |
| JSON Functions   | `jsonb_build_object()`       | Separate queries + Python dict  |
| Aggregates       | `FILTER (WHERE ...)`         | `CASE WHEN ... END`             |
| Full-Text Search | `to_tsvector`, `ts_rank`     | `LIKE '%term%'`                 |
| Array Ops        | `= ALL`, `!= ALL`            | `IN (...)`                      |
| Timestamps       | Native datetime              | String → `fromisoformat()`      |

### 2.4 PostgreSQL Implementation (Production)

**File**: `faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py`

```python
class PostgreSQLHybridCaseRepository(CaseRepository):
    """Production repository using PostgreSQL-optimized SQL."""

    def __init__(self, db_session):
        self.db = db_session

    async def save(self, case: Case) -> Case:
        # Uses PostgreSQL-optimized SQL:
        # - ::jsonb type casts for performance
        # - jsonb_build_object() for efficiency
        # - FILTER clauses for aggregates
        # - to_tsvector/ts_rank for full-text search
```

> The legacy `PostgreSQLCaseRepository` class was replaced by `PostgreSQLHybridCaseRepository`; only the hybrid implementation exists in current code.

**Characteristics**:

- ✅ Persistent across restarts
- ✅ ACID transactions
- ✅ Optimized queries via indexes
- ✅ Concurrent access safe
- ✅ Production-grade performance
- ✅ Distributed system support
- ✅ Replication and HA

**When to use**: Production K8s deployment, staging, high-concurrency environments

---

## 3. Data Model

### 3.1 Core Case Structure

```python
# Illustrative subset — see faultmaven/modules/case/domain/models.py for the canonical model.
class Case(BaseModel):
    """Root case entity."""

    # ============================================================
    # Identity
    # ============================================================
    case_id: str                    # Primary key
    user_id: str                    # FK to users
    organization_id: str            # FK to organizations
    title: str                      # Max 200 chars
    description: str                # Max 2000 chars

    # ============================================================
    # Status & Lifecycle
    # ============================================================
    status: CaseStatus              # inquiry | investigating | resolved | closed
    # status_history is not an embedded column — transitions are persisted to the
    # case_actions table (Python alias: CaseStatusTransitionModel). When hydrating
    # a Case, the repository joins case_actions and projects the transitions.
    closure_reason: Optional[str]
    is_archived: bool = False       # Data-lifecycle flag, independent of status
    archived_at: Optional[datetime] # Set when is_archived flips to True

    # ============================================================
    # Turn Tracking
    # ============================================================
    current_turn: int
    turns_without_progress: int
    turn_history: List[TurnProgress]

    # ============================================================
    # Investigation Data (HIGH CARDINALITY - Separate Storage)
    # ============================================================
    evidence: List[Evidence]        # PostgreSQL: separate table
    hypotheses: Dict[str, Hypothesis]  # PostgreSQL: separate table
    solutions: List[Solution]       # PostgreSQL: separate table
    uploaded_files: List[UploadedFile]  # PostgreSQL: separate table

    # ============================================================
    # Context Data (LOW CARDINALITY - Embedded Storage)
    # ============================================================
    inquiry: InquiryData              # PostgreSQL: JSONB
    problem_verification: Optional[ProblemVerification]  # PostgreSQL: JSONB
    working_conclusion: Optional[WorkingConclusion]      # PostgreSQL: JSONB
    root_cause_conclusion: Optional[RootCauseConclusion]  # PostgreSQL: JSONB
    path_selection: Optional[PathSelection]  # PostgreSQL: JSONB
    escalation_state: Optional[EscalationState]  # PostgreSQL: JSONB
    documentation: DocumentationData         # PostgreSQL: JSONB
    # investigation_journal is carried on the domain model as an append-only list;
    # it is serialized into the cases.progress JSONB blob (field name: "journal").
    # There is no dedicated column or table.

    # ============================================================
    # Progress Tracking
    # ============================================================
    progress: InvestigationProgress  # PostgreSQL: JSONB
    investigation_strategy: InvestigationStrategy

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
```

---

## 4. PostgreSQL Schema

> **IMPORTANT - Distributed Architecture**: FaultMaven uses **separate PostgreSQL clusters**:
> - **`auth_db`**: Users, organizations, roles (managed by Auth module)
> - **`cases_db`**: Cases and investigation data (this schema)
>
> Foreign key constraints between clusters are **not possible**. The `user_id` and `organization_id`
> fields in `cases_db` reference entities in `auth_db` but are enforced at the application layer,
> not via database FK constraints.

### 4.1 Table Design (10 case-domain tables in cases_db)

```
Core Tables (2):
├── cases              -- Main case data + JSONB for low-cardinality items
└── sessions           -- Session management

High-Cardinality Tables (8):
├── evidence           -- Investigation evidence (many per case)
├── hypotheses         -- Hypotheses being tested (many per case)
├── solutions          -- Proposed/verified solutions (few per case)
├── uploaded_files     -- File metadata (many per case)
├── case_messages      -- Turn-by-turn messages (very high volume)
├── case_actions       -- Audit trail of actions & status transitions
│                       -- (Python alias: CaseStatusTransitionModel)
├── case_checkpoints   -- State snapshots (one per turn)
└── reports            -- Generated reports (few per case, versioned)
```

The full live table count across user + case + knowledge + conversion + config domains is 33 — see `er-diagram.md` for the authoritative enumeration.

### 4.2 cases (Main Table)

```sql
CREATE TABLE cases (
    -- ============================================================
    -- Identity
    -- ============================================================
    case_id VARCHAR(17) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,  -- No FK: users table in separate auth_db cluster
    organization_id VARCHAR(20) NOT NULL,  -- No FK: organizations table in separate auth_db cluster
    title VARCHAR(200) NOT NULL,
    description TEXT DEFAULT '',

    -- ============================================================
    -- Status & Lifecycle
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'inquiry',
    closure_reason VARCHAR(100),
    investigation_strategy VARCHAR(20) DEFAULT 'post_mortem',

    -- ============================================================
    -- Turn Tracking
    -- ============================================================
    current_turn INTEGER DEFAULT 0,
    turns_without_progress INTEGER DEFAULT 0,

    -- ============================================================
    -- Timestamps
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,

    -- ============================================================
    -- Low-Cardinality Complex Data (JSONB)
    -- ============================================================
    inquiry JSONB NOT NULL DEFAULT '{}'::jsonb,
    problem_verification JSONB,
    working_conclusion JSONB,
    root_cause_conclusion JSONB,
    path_selection JSONB,
    escalation_state JSONB,
    documentation JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT cases_status_check
        CHECK (status IN ('inquiry', 'investigating', 'resolved', 'closed')),

    CONSTRAINT cases_closure_reason_check
        CHECK (
            closure_reason IS NULL OR
            closure_reason IN ('resolved', 'abandoned', 'escalated', 'inquiry_only', 'duplicate', 'other')
        ),

    CONSTRAINT cases_strategy_check
        CHECK (investigation_strategy IN ('active_incident', 'post_mortem')),

    CONSTRAINT cases_turn_check
        CHECK (current_turn >= 0 AND turns_without_progress >= 0),

    CONSTRAINT cases_resolved_timestamp_check
        CHECK (
            (status = 'resolved' AND resolved_at IS NOT NULL) OR
            (status != 'resolved' AND resolved_at IS NULL)
        ),

    CONSTRAINT cases_closed_timestamp_check
        CHECK (
            (status = 'closed' AND closed_at IS NOT NULL) OR
            (status != 'closed' AND closed_at IS NULL)
        ),

    CONSTRAINT cases_timestamp_order_check
        CHECK (
            created_at <= updated_at AND
            created_at <= last_activity_at AND
            (resolved_at IS NULL OR created_at <= resolved_at) AND
            (closed_at IS NULL OR created_at <= closed_at) AND
            (resolved_at IS NULL OR closed_at IS NULL OR resolved_at <= closed_at)
        )
);

-- Indexes for common queries
CREATE INDEX idx_cases_user_status ON cases(user_id, status);
CREATE INDEX idx_cases_org_status ON cases(organization_id, status);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_cases_last_activity ON cases(last_activity_at DESC);
CREATE INDEX idx_cases_stuck ON cases(turns_without_progress)
    WHERE status = 'investigating' AND turns_without_progress >= 3;

-- JSONB indexes for filtered queries
CREATE INDEX idx_cases_path ON cases((path_selection->>'path'))
    WHERE path_selection IS NOT NULL;
CREATE INDEX idx_cases_urgency ON cases((problem_verification->>'urgency_level'))
    WHERE problem_verification IS NOT NULL;

-- Full-text search
CREATE INDEX idx_cases_search ON cases USING gin(
    to_tsvector('english', title || ' ' || description)
);

COMMENT ON TABLE cases IS 'Root case entity with embedded low-cardinality data in JSONB';
```

### 4.3 evidence (High-Cardinality Table)

```sql
CREATE TABLE evidence (
    evidence_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Classification
    -- ============================================================
    category VARCHAR(50) NOT NULL,              -- see EvidenceCategory enum below
    primary_purpose VARCHAR(100) NOT NULL,

    -- ============================================================
    -- Content (Three-Tier Storage)
    -- ============================================================
    summary VARCHAR(500) NOT NULL,              -- Quick preview
    preprocessed_content TEXT NOT NULL,         -- Analyzed content (in DB)
    content_ref VARCHAR(1000),                  -- S3 URI for raw content
    content_size_bytes BIGINT NOT NULL,
    preprocessing_method VARCHAR(50) NOT NULL,

    -- ============================================================
    -- Source
    -- ============================================================
    source_type VARCHAR(50) NOT NULL,           -- user_upload | system_collected | agent_generated
    form VARCHAR(20) NOT NULL,                  -- text | image | metric | structured

    -- ============================================================
    -- Metadata
    -- ============================================================
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    reliability_score REAL CHECK (reliability_score >= 0 AND reliability_score <= 1),
    tags TEXT[],                                -- PostgreSQL array for efficient queries

    -- ============================================================
    -- Flexible Additional Data (JSONB)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,         -- Source-specific metadata

    CONSTRAINT evidence_category_check
        CHECK (category IN (
            -- EvidenceCategory enum (faultmaven/modules/case/domain/models.py)
            'symptom_evidence',
            'causal_evidence',
            'mitigation_evidence',
            'solution_evidence',
            'resolution_evidence',    -- alias for solution_evidence
            'contextual_evidence',
            'rejected'
        )),

    CONSTRAINT evidence_source_check
        CHECK (source_type IN ('user_upload', 'system_collected', 'agent_generated')),

    CONSTRAINT evidence_form_check
        CHECK (form IN ('text', 'image', 'metric', 'structured'))
);

-- Note: EvidenceCategory describes the evidentiary role (why the artifact matters
-- to the investigation). Data shape (LOGS, METRICS, CONFIGURATION, CODE, TEXT,
-- IMAGE) is a separate enum — EvidenceSourceType — carried on the artifact row
-- rather than on the evidence row.

-- Indexes for evidence queries
CREATE INDEX idx_evidence_case ON evidence(case_id);
CREATE INDEX idx_evidence_category ON evidence(case_id, category);
CREATE INDEX idx_evidence_collected_at ON evidence(collected_at DESC);
CREATE INDEX idx_evidence_tags ON evidence USING gin(tags);

COMMENT ON TABLE evidence IS 'Investigation evidence - high cardinality, frequently queried/filtered';
```

### 4.4 hypotheses (High-Cardinality Table)

```sql
CREATE TABLE hypotheses (
    hypothesis_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Content
    -- ============================================================
    statement TEXT NOT NULL,                    -- The hypothesis statement
    rationale TEXT NOT NULL,                    -- Why we think this

    -- ============================================================
    -- Testing Status
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'proposed',
    confidence REAL CHECK (confidence >= 0 AND confidence <= 1),

    -- ============================================================
    -- Evidence Links
    -- ============================================================
    supporting_evidence_ids TEXT[],             -- Array of evidence IDs
    contradicting_evidence_ids TEXT[],

    -- ============================================================
    -- Testing Plan
    -- ============================================================
    test_plan TEXT,
    test_results TEXT,

    -- ============================================================
    -- Metadata
    -- ============================================================
    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    tested_at TIMESTAMP WITH TIME ZONE,
    priority INTEGER DEFAULT 0,

    CONSTRAINT hypotheses_status_check
        CHECK (status IN ('proposed', 'testing', 'confirmed', 'rejected', 'inconclusive')),

    CONSTRAINT hypotheses_tested_timestamp_check
        CHECK (
            (status IN ('confirmed', 'rejected', 'inconclusive') AND tested_at IS NOT NULL) OR
            (status NOT IN ('confirmed', 'rejected', 'inconclusive') AND tested_at IS NULL)
        )
);

-- Indexes for hypothesis queries
CREATE INDEX idx_hypotheses_case ON hypotheses(case_id);
CREATE INDEX idx_hypotheses_status ON hypotheses(case_id, status);
CREATE INDEX idx_hypotheses_priority ON hypotheses(case_id, priority DESC);
CREATE INDEX idx_hypotheses_proposed_at ON hypotheses(proposed_at DESC);

COMMENT ON TABLE hypotheses IS 'Investigation hypotheses - frequently filtered by status';
```

### 4.5 solutions (High-Cardinality Table)

```sql
CREATE TABLE solutions (
    solution_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    hypothesis_id VARCHAR(15) REFERENCES hypotheses(hypothesis_id),

    -- ============================================================
    -- Content
    -- ============================================================
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    implementation_steps TEXT NOT NULL,         -- Newline-separated or JSON array

    -- ============================================================
    -- Status
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'proposed',

    -- ============================================================
    -- Risk & Impact
    -- ============================================================
    risk_level VARCHAR(10) DEFAULT 'medium',
    estimated_effort VARCHAR(20),
    impact_scope VARCHAR(1000),

    -- ============================================================
    -- Verification
    -- ============================================================
    verification_plan TEXT,
    verification_results TEXT,

    -- ============================================================
    -- Metadata
    -- ============================================================
    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    implemented_at TIMESTAMP WITH TIME ZONE,
    verified_at TIMESTAMP WITH TIME ZONE,

    CONSTRAINT solutions_status_check
        CHECK (status IN ('proposed', 'approved', 'implementing', 'implemented', 'verified', 'rejected')),

    CONSTRAINT solutions_risk_check
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical'))
);

-- Indexes
CREATE INDEX idx_solutions_case ON solutions(case_id);
CREATE INDEX idx_solutions_status ON solutions(case_id, status);
CREATE INDEX idx_solutions_hypothesis ON solutions(hypothesis_id);

COMMENT ON TABLE solutions IS 'Proposed and verified solutions';
```

### 4.6 uploaded_files (High-Cardinality Table)

```sql
CREATE TABLE uploaded_files (
    -- Using VARCHAR for file_id to match Pydantic model (file_abc123xyz pattern)
    -- More human-readable in logs than UUID
    file_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- File Metadata (MATCHES UploadedFile Pydantic model)
    -- ============================================================
    filename VARCHAR(255) NOT NULL,
    size_bytes INTEGER NOT NULL,                -- Pydantic: size_bytes (not file_size)
    data_type VARCHAR(50) NOT NULL,             -- Pydantic: data_type (not content_type)

    -- ============================================================
    -- Upload Context
    -- ============================================================
    uploaded_at_turn INTEGER NOT NULL,          -- Which turn this file was uploaded
    uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    source_type VARCHAR(50) NOT NULL DEFAULT 'file_upload',  -- file_upload | paste | screenshot | page_injection

    -- ============================================================
    -- Storage & Processing
    -- ============================================================
    content_ref VARCHAR(1000),                  -- S3 URI or storage path (links to Evidence.content_ref)
    preprocessing_summary TEXT,                 -- AI-generated summary after analysis

    -- ============================================================
    -- Metadata (JSONB for flexibility)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,

    CONSTRAINT uploaded_files_filename_not_empty CHECK (LENGTH(TRIM(filename)) > 0),
    CONSTRAINT uploaded_files_size_positive CHECK (size_bytes > 0),
    CONSTRAINT uploaded_files_turn_nonnegative CHECK (uploaded_at_turn >= 0),
    CONSTRAINT uploaded_files_data_type_check
        CHECK (data_type IN ('log', 'metric', 'config', 'code', 'text', 'image', 'structured', 'other')),
    CONSTRAINT uploaded_files_source_type_check
        CHECK (source_type IN ('file_upload', 'paste', 'screenshot', 'page_injection', 'agent_generated'))
);

-- Indexes
CREATE INDEX idx_uploaded_files_case_id ON uploaded_files(case_id);
CREATE INDEX idx_uploaded_files_uploaded_at ON uploaded_files(uploaded_at DESC);
CREATE INDEX idx_uploaded_files_turn ON uploaded_files(case_id, uploaded_at_turn);
CREATE INDEX idx_uploaded_files_content_ref ON uploaded_files(content_ref) WHERE content_ref IS NOT NULL;

COMMENT ON TABLE uploaded_files IS 'Raw file upload metadata - aligns with UploadedFile Pydantic model';
COMMENT ON COLUMN uploaded_files.content_ref IS 'Storage path - links to Evidence.content_ref for traceability';
```

**Design Notes**:
- Uses `VARCHAR(15)` for `file_id` (not UUID) to match Pydantic model pattern `file_abc123xyz`
- Schema exactly mirrors `UploadedFile` Pydantic model fields for zero-mapping repositories
- `content_ref` links to `Evidence.content_ref` for evidence→file traceability
- No processing status tracking (moved to separate processing pipeline if needed)

### 4.7 case_messages (High-Cardinality Table)

```sql
CREATE TABLE case_messages (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,

    -- ============================================================
    -- Message Content
    -- ============================================================
    role VARCHAR(20) NOT NULL,                  -- user | assistant | system
    content TEXT NOT NULL,

    -- ============================================================
    -- Metadata
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    token_count INTEGER,

    -- ============================================================
    -- Flexible Data (JSONB)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,         -- Sources, tools used, etc.

    CONSTRAINT case_messages_role_check
        CHECK (role IN ('user', 'assistant', 'system'))
);

-- Indexes
CREATE INDEX idx_case_messages_case_turn ON case_messages(case_id, turn_number);
CREATE INDEX idx_case_messages_created_at ON case_messages(created_at DESC);

COMMENT ON TABLE case_messages IS 'Turn-by-turn conversation messages (high volume)';
```

### 4.8 case_actions (Audit Table — Python alias: CaseStatusTransitionModel)

```sql
-- The live table is case_actions. A Python-level alias (CaseStatusTransitionModel)
-- points at this table for back-compat; there is no separate case_status_transitions
-- table in the database.
CREATE TABLE case_actions (
    transition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Transition Data
    -- ============================================================
    from_status VARCHAR(20) NOT NULL,
    to_status VARCHAR(20) NOT NULL,
    reason VARCHAR(500),
    triggered_by VARCHAR(255),                  -- user_id or 'system'

    -- ============================================================
    -- Metadata
    -- ============================================================
    transitioned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT case_actions_status_check
        CHECK (
            from_status IN ('inquiry', 'investigating', 'resolved', 'closed') AND
            to_status IN ('inquiry', 'investigating', 'resolved', 'closed')
        )
);

-- Indexes
CREATE INDEX idx_case_actions_case ON case_actions(case_id);
CREATE INDEX idx_case_actions_timestamp ON case_actions(transitioned_at DESC);

COMMENT ON TABLE case_actions IS 'Audit trail of case actions and status transitions';
```

### 4.9 case_checkpoints (High-Cardinality Table)

```sql
CREATE TABLE case_checkpoints (
    checkpoint_id VARCHAR(50) PRIMARY KEY,      -- Format: {case_id}:turn:{turn_number}
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,

    -- ============================================================
    -- Snapshot Data
    -- ============================================================
    case_snapshot JSONB NOT NULL,               -- Complete case state representation
    snapshot_hash VARCHAR(64) NOT NULL,         -- SHA256 hash for drift detection
    trigger VARCHAR(50) NOT NULL,               -- reason (turn_complete, manual, etc.)

    -- ============================================================
    -- Metadata
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb,

    CONSTRAINT case_checkpoints_hash_not_empty
        CHECK (LENGTH(TRIM(snapshot_hash)) > 0)
);

-- Indexes
CREATE INDEX ix_case_turn ON case_checkpoints(case_id, turn_number);
CREATE INDEX idx_checkpoints_created_at ON case_checkpoints(created_at DESC);

COMMENT ON TABLE case_checkpoints IS 'Immutable snapshots of case state per turn/event';
```

### 4.10 reports (High-Cardinality Table)

```sql
CREATE TABLE reports (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Report Type & Versioning
    -- ============================================================
    report_type VARCHAR(30) NOT NULL,              -- resolution_summary | closure_summary | runbook
    version INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,      -- Latest version for this report_type
    linked_to_closure BOOLEAN NOT NULL DEFAULT FALSE,

    -- ============================================================
    -- Content
    -- ============================================================
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,                         -- Full markdown content
    format VARCHAR(20) NOT NULL DEFAULT 'markdown',

    -- ============================================================
    -- Generation Metadata
    -- ============================================================
    generation_status VARCHAR(20) NOT NULL,        -- generating | completed | failed
    generation_time_ms INTEGER NOT NULL CHECK (generation_time_ms >= 0 AND generation_time_ms <= 120000),
    generated_by VARCHAR(255),                     -- Optional: user_id who triggered generation (not in CaseReport model yet)

    -- ============================================================
    -- Runbook-Specific Metadata (JSONB for flexibility)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,            -- RunbookMetadata: source, domain, tags, etc.

    -- ============================================================
    -- Timestamps
    -- ============================================================
    generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT reports_type_check
        CHECK (report_type IN ('resolution_summary', 'closure_summary', 'runbook')),

    CONSTRAINT reports_status_check
        CHECK (generation_status IN ('generating', 'completed', 'failed')),

    CONSTRAINT reports_format_check
        CHECK (format IN ('markdown')),

    CONSTRAINT reports_version_check
        CHECK (version >= 1 AND version <= 5)
);

-- Ensure only one current version per report_type per case (partial unique index)
CREATE UNIQUE INDEX idx_reports_current_unique
    ON reports(case_id, report_type)
    WHERE is_current = TRUE;

-- Additional indexes for report queries (current_unique index created above)
CREATE INDEX idx_reports_case ON reports(case_id);
CREATE INDEX idx_reports_type_version ON reports(case_id, report_type, version DESC);
CREATE INDEX idx_reports_closure ON reports(case_id) WHERE linked_to_closure = TRUE;
CREATE INDEX idx_reports_generated_at ON reports(generated_at DESC);

-- Full-text search on report content
CREATE INDEX idx_reports_content_search ON reports USING gin(
    to_tsvector('english', title || ' ' || content)
);

COMMENT ON TABLE reports IS 'Generated case reports (incident reports, runbooks, post-mortems) - versioned, persistent storage';
COMMENT ON COLUMN reports.metadata IS 'Runbook-specific metadata: source (incident_driven/document_driven), domain, tags, etc.';
```

**Design Notes**:
- Reports stored persistently in PostgreSQL (TD-001 migration from Redis + ChromaDB)
- Versioning support: up to 5 versions per report_type per case
- `is_current` flag marks latest version (enforced via unique constraint)
- `linked_to_closure` tracks reports linked during case closure
- Metadata stored as JSONB for runbook-specific fields (source, domain, tags)
- Full-text search index on title and content for similarity queries
- Cascade delete when parent case is deleted

### 4.11 Supporting Tables

`users`, `organizations`, and related auth/RBAC tables are defined in [user-schema.md](./user-schema.md) — that is the authoritative source for their DDL. They are not redefined here.

`sessions` is a case-domain table and is documented in §4.1 above.

---

## 5. Normalization Decisions

### 5.1 Decision Matrix

We normalize (separate table) when:
- **High cardinality**: Many items per case (evidence, messages)
- **Frequent filtering**: Need to query/filter items independently
- **Independent lifecycle**: Items can be added/removed separately
- **Size concerns**: Items might grow large

We denormalize (JSONB) when:
- **Low cardinality**: 0-2 items per case (conclusions, path selection)
- **Rarely queried**: Not used in WHERE/JOIN clauses
- **Always fetched together**: Retrieved with parent case
- **Flexible schema**: Structure might evolve

### 5.2 Normalization Analysis by Data Type

| Data Type | Cardinality | Query Pattern | Storage | Rationale |
|-----------|-------------|---------------|---------|-----------|
| **Evidence** | Many (10-100+) | Filter by category, search | ✅ **Table** | High volume, frequently filtered |
| **Hypotheses** | Few-Many (5-20) | Filter by status, priority | ✅ **Table** | Status tracking critical |
| **Solutions** | Few (1-5) | Filter by status | ✅ **Table** | Step tracking, verification |
| **Messages** | Very Many (20-500+) | Temporal queries | ✅ **Table** | Very high volume, pagination |
| **Uploaded Files** | Many (5-50) | List, filter by phase | ✅ **Table** | Metadata queries |
| **Status Transitions** | Few (3-10) | Audit trail | ✅ **Table** | Temporal analysis |
| **Inquiry Data** | One (1) | Never filtered | ❌ **JSONB** | Always with case, flexible |
| **Problem Verification** | Zero-One (0-1) | Rarely queried | ❌ **JSONB** | Optional, flexible |
| **Conclusions** | Zero-Two (0-2) | Never filtered | ❌ **JSONB** | Terminal states only |
| **Path Selection** | Zero-One (0-1) | Rare filter | ❌ **JSONB** | Small, rarely queried |
| **Progress** | One (1) | Never filtered | ❌ **JSONB** | Complex nested, always with case |
| **Documentation** | One (1) | Never filtered | ❌ **JSONB** | Always with case |

### 5.3 Trade-off Analysis

**Why NOT fully normalize everything?**

❌ **Over-normalization issues**:
```sql
-- Too many JOINs kills performance
SELECT * FROM cases c
  LEFT JOIN inquiry co ON c.case_id = co.case_id
  LEFT JOIN problem_verification pv ON c.case_id = pv.case_id
  LEFT JOIN working_conclusion wc ON c.case_id = wc.case_id
  LEFT JOIN root_cause_conclusion rc ON c.case_id = rc.case_id
  LEFT JOIN path_selection ps ON c.case_id = ps.case_id
  LEFT JOIN progress pr ON c.case_id = pr.case_id
  LEFT JOIN documentation d ON c.case_id = d.case_id
-- Result: 8-way JOIN for every case fetch!
```

✅ **JSONB advantages**:
```sql
-- Single query, excellent performance
SELECT * FROM cases WHERE case_id = 'case_123';
-- Result: All embedded data in ONE query
```

**Performance Comparison**:

| Operation | Fully Normalized (32 tables) | Hybrid (11 tables) | Single Table (current) |
|-----------|------------------------------|-------------------|------------------------|
| Load case | 8-12 JOINs (~50ms) | 4 JOINs + JSONB (~10ms) | 1 query (~2ms) |
| Filter evidence | Efficient (indexed) | Efficient (indexed) | ❌ Slow (JSONB scan) |
| Search cases | Complex JOIN | Simple query | Simple query |
| Update evidence | UPDATE 1 row | UPDATE 1 row | ❌ Rewrite JSONB array |
| Concurrent updates | Safe (row locks) | Safe (row locks) | ❌ Full case lock |

**Our hybrid approach wins on balance!**

### 5.4 Multi-Tenancy: organization_id Normalization

**Decision**: `organization_id` is stored **ONLY on the `cases` table**, not on child tables.

**Rationale**:

1. **Query Pattern Analysis**: 0% of child table queries filter by `organization_id` without `case_id`
   - All child queries have `case_id` in WHERE clause (100% of 369 queries analyzed)
   - Organization filtering is achieved via JOIN to `cases` table

2. **Data Integrity**: Single source of truth prevents inconsistencies
   - No risk of child `organization_id` diverging from parent
   - No complex CHECK constraints needed
   - Organization transfers = single UPDATE to `cases` table

3. **Performance**: JOIN overhead is negligible
   - Normalized: ~2.5ms per query (with JOIN)
   - Denormalized: ~1.1ms per query (direct filter)
   - **Difference**: 1.4ms (< 3% of total 50-100ms query time)

**Implementation Pattern**:

```sql
-- ✅ CORRECT: Organization filter via JOIN to cases
SELECT e.* FROM evidence e
JOIN cases c ON c.case_id = e.case_id
WHERE e.case_id = :case_id
  AND c.organization_id = :organization_id;  -- Security check via JOIN

-- ❌ INCORRECT: Don't add organization_id to child tables
-- ALTER TABLE evidence ADD COLUMN organization_id VARCHAR(20);  -- NO!
```

**Tables WITH organization_id** (Top-level entities):

- ✅ `cases` - Owns the organization relationship
- ✅ `sessions` - Independent user sessions
- ✅ `knowledge_items` - Organization knowledge base
- ✅ `standalone_evidence` - Organization evidence library

**Tables WITHOUT organization_id** (Case children):

- ❌ `evidence` - Inherit org via `cases.case_id` FK
- ❌ `hypotheses` - Inherit org via `cases.case_id` FK
- ❌ `solutions` - Inherit org via `cases.case_id` FK
- ❌ `case_messages` - Inherit org via `cases.case_id` FK
- ❌ `uploaded_files` - Inherit org via `cases.case_id` FK
- ❌ `case_actions` - Inherit org via `cases.case_id` FK
- ❌ `case_checkpoints` - Inherit org via `cases.case_id` FK
- ❌ `reports` - Inherit org via `cases.case_id` FK

**Performance Optimization**:

```sql
-- Composite index on cases table for efficient org-scoped queries:
CREATE INDEX idx_cases_org_id_case_id ON cases(organization_id, case_id);
CREATE INDEX idx_cases_org_status ON cases(organization_id, status);

-- Child table indexes remain focused on case_id (no organization_id needed):
CREATE INDEX idx_evidence_case ON evidence(case_id);
CREATE INDEX idx_hypotheses_case ON hypotheses(case_id);
```

---

## 6. Performance Characteristics

⚠️ **Important**: All performance metrics below are **ESTIMATED TARGETS** based on typical PostgreSQL behavior with similar schemas. Actual performance will be validated through benchmarking after deployment.

**Assumptions**:
- PostgreSQL 15 or higher
- Proper indexes created (as per migration script)
- Connection pool configured (10-20 connections)
- ~10K cases with ~100 evidence items per case average

### 6.1 Query Performance Targets

**Common queries and their estimated performance**:

```sql
-- Load complete case (most common operation)
-- Target: ~10ms (4 LEFT JOINs with indexed lookups)
SELECT c.*,
       array_agg(e.*) as evidence,
       array_agg(h.*) as hypotheses,
       array_agg(s.*) as solutions
FROM cases c
LEFT JOIN evidence e ON c.case_id = e.case_id
LEFT JOIN hypotheses h ON c.case_id = h.case_id
LEFT JOIN solutions s ON c.case_id = s.case_id
WHERE c.case_id = $1
GROUP BY c.case_id;

-- Filter evidence by category (efficient)
-- Target: ~5ms (indexed on case_id and category)
SELECT * FROM evidence
WHERE case_id = $1 AND category = 'symptom_evidence'
ORDER BY collected_at DESC;

-- Search cases (full-text + case ID)
-- Target: ~15ms (GIN index on tsvector, ILIKE fallback for case ID)
SELECT * FROM cases
WHERE to_tsvector('english', title || ' ' || description) @@ to_tsquery('api performance')
   OR case_id ILIKE '%search_term%'
ORDER BY last_activity_at DESC
LIMIT 20;

-- Analytics: Evidence distribution across cases
-- Target: ~100ms for 10K cases
SELECT category, COUNT(*)
FROM evidence
WHERE collected_at > NOW() - INTERVAL '30 days'
GROUP BY category;
```

**Performance Validation TODO**:
- [ ] Run EXPLAIN ANALYZE on all queries
- [ ] Verify indexes are actually used (no sequential scans on large tables)
- [ ] Benchmark with realistic data volume (1K, 10K, 100K cases)
- [ ] Update this section with ACTUAL measured performance

### 6.2 Storage Efficiency

**Estimated storage per case**:

| Component | Avg Size | Storage Type |
|-----------|----------|--------------|
| Case metadata | 2 KB | Columns |
| JSONB fields | 5-10 KB | JSONB |
| Evidence (10 items) | 50 KB | Rows |
| Hypotheses (5 items) | 10 KB | Rows |
| Messages (100 items) | 50 KB | Rows |
| **Total per case** | **~120 KB** | Mixed |

**Scalability**:
- 1,000 cases = ~120 MB
- 10,000 cases = ~1.2 GB
- 100,000 cases = ~12 GB

PostgreSQL handles this easily with proper indexing.

### 6.3 Concurrent Access

**Locking granularity**:

```sql
-- Update evidence: Row-level lock
UPDATE evidence SET status = 'verified' WHERE evidence_id = 'evi_123';
-- ✅ Other evidence updates can proceed

-- Update case status: Row-level lock on cases table only
UPDATE cases SET status = 'investigating' WHERE case_id = 'case_123';
-- ✅ Evidence/hypothesis updates can proceed concurrently

-- Hybrid design allows fine-grained locking!
```

---

## 7. Concurrency Model

### 7.1 Single-Table JSONB Approach (Legacy - Not Recommended)

**Problem**: Lost update issue with concurrent writes

```python
# Thread 1: User uploads file
case = await repo.get(case_id)  # Gets {evidence: [A], files: []}
case.uploaded_files.append(new_file)
await repo.save(case)  # Writes {evidence: [A], files: [X]}

# Thread 2: Agent adds evidence (happens concurrently)
case = await repo.get(case_id)  # Gets {evidence: [A], files: []}
case.evidence.append(new_evidence)
await repo.save(case)  # ❌ OVERWRITES! Writes {evidence: [A,B], files: []}
                        # Lost the uploaded file!
```

**Root Cause**: Both threads read the same case state, modify different parts, then overwrite the entire JSONB blob.

**Mitigation** (if using single-table):
- Service layer must coordinate writes (only ONE writer per case at a time)
- Use optimistic locking with version field
- Not scalable for concurrent operations

### 7.2 Hybrid Normalized Approach (This Design - Recommended)

**Solution**: Row-level locking on separate tables

```python
# Thread 1: User uploads file
INSERT INTO uploaded_files VALUES (...)  # Inserts row in uploaded_files table

# Thread 2: Agent adds evidence (concurrent)
INSERT INTO evidence VALUES (...)  # Inserts row in evidence table

# ✅ Both succeed! Different tables, different rows, no conflict
```

**Benefits**:
- ✅ Database ACID guarantees prevent lost updates
- ✅ Can parallelize operations on same case (upload file + add evidence + update hypothesis)
- ✅ Row-level locks only block conflicting operations (updating same evidence record)
- ✅ No coordination needed at service layer

**Example Concurrent Operations** (all succeed):
```python
# All can run simultaneously on same case:
await add_evidence(case_id, evidence)           # INSERT INTO evidence
await upload_file(case_id, file)                # INSERT INTO uploaded_files
await update_hypothesis_status(hypo_id, status) # UPDATE hypotheses
await add_message(case_id, message)             # INSERT INTO case_messages
```

**Lock Conflicts** (expected behavior):
```python
# These WILL block each other (same row):
await update_evidence(evi_id, status='verified')    # UPDATE evidence WHERE evidence_id = X
await update_evidence(evi_id, status='invalidated') # Waits for first to commit
```

### 7.3 JSONB Field Concurrency (Remaining Fields in cases Table)

**Remaining JSONB fields** in `cases` table:

- `inquiry` - Initial problem description (set once, rarely updated)
- `problem_verification` - Problem validation data (set once per milestone)
- `working_conclusion` - Temporary conclusion (updated during investigation)
- `root_cause_conclusion` - Final root cause (set once at resolution)
- `path_selection` - Investigation path choice (set once)
- `progress` - Progress tracking data (updated frequently)
- `documentation` - Case documentation (updated occasionally)

**Concurrency Strategy** for JSONB updates:

**Option 1: Optimistic Locking with Version Field** (Recommended for frequent updates):

```sql
-- Add version column to cases table
ALTER TABLE cases ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

-- Update with version check
UPDATE cases
SET
    progress = :new_progress,
    version = version + 1,
    updated_at = NOW()
WHERE case_id = :case_id
  AND version = :expected_version;  -- ❗ Fails if version changed

-- Check rows affected: 0 = conflict, 1 = success
```

**Python Implementation**:
```python
async def update_progress(case_id: str, progress_update: dict) -> Case:
    """Update progress with optimistic locking."""
    max_retries = 3
    for attempt in range(max_retries):
        # Read current case with version
        case = await repo.get(case_id)
        if not case:
            raise ValueError(f"Case {case_id} not found")

        # Modify progress
        current_progress = case.progress or {}
        updated_progress = {**current_progress, **progress_update}
        case.progress = updated_progress

        # Save with version check
        result = await db.execute(
            """
            UPDATE cases
            SET progress = :progress, version = version + 1, updated_at = NOW()
            WHERE case_id = :case_id AND version = :version
            """,
            {
                "case_id": case_id,
                "progress": json.dumps(updated_progress),
                "version": case.version
            }
        )

        if result.rowcount == 1:
            case.version += 1
            return case  # Success!
        else:
            # Version conflict - retry
            logger.warning(f"Version conflict updating case {case_id}, retry {attempt + 1}")
            await asyncio.sleep(0.1 * (attempt + 1))  # Exponential backoff

    raise ConcurrencyError(f"Failed to update case {case_id} after {max_retries} retries")
```

**Option 2: PostgreSQL JSONB Merge Operators** (For independent field updates):

```sql
-- Merge new data into existing JSONB (doesn't overwrite other fields)
UPDATE cases
SET
    progress = progress || :progress_update::jsonb,  -- || is JSONB concatenation/merge
    updated_at = NOW()
WHERE case_id = :case_id;

-- Example: Update progress.turn_count without touching progress.milestones
-- Before: {"turn_count": 5, "milestones": ["A", "B"]}
-- Update: {"turn_count": 6}
-- After:  {"turn_count": 6, "milestones": ["A", "B"]}  ✅ Preserved!
```

**Option 3: Application-Level Locking** (For critical sections):

```python
from asyncio import Lock

# One lock per case_id (in-memory)
case_locks: Dict[str, Lock] = defaultdict(Lock)

async def update_conclusion_safely(case_id: str, conclusion: dict):
    """Serialize updates to same case using application lock."""
    async with case_locks[case_id]:
        case = await repo.get(case_id)
        case.working_conclusion = conclusion
        await repo.save(case)
        # Lock released after save
```

**Recommendation by Field**:

| JSONB Field | Update Frequency | Strategy | Rationale |
|-------------|------------------|----------|----------- |
| `inquiry` | Once (creation) | None needed | Set once, immutable |
| `problem_verification` | Once per milestone | Optimistic locking | Infrequent but critical |
| `working_conclusion` | Frequent (every few turns) | **JSONB merge (`\|\|`)** | Independent field updates |
| `root_cause_conclusion` | Once (resolution) | Optimistic locking | One-time critical update |
| `path_selection` | Once (path choice) | None needed | Set once, rarely changed |
| `progress` | Very frequent (every turn) | **JSONB merge (`\|\|`)** | High concurrency, independent updates |
| `documentation` | Occasional | JSONB merge | Append-only, low conflict |

**Best Practice**:

- Use **JSONB merge (`||`)** for fields with independent sub-fields (e.g., `progress.turn_count`, `progress.milestones`)
- Use **optimistic locking** for fields updated as a whole (e.g., `working_conclusion`)
- Use **application locks** only when database-level solutions aren't sufficient

### 7.4 Case Deletion Strategy

**Current Implementation**: Hard delete with CASCADE

```sql
-- Delete case (cascades to all child tables)
DELETE FROM cases WHERE case_id = :case_id;

-- Automatically deletes:
-- - All evidence records (ON DELETE CASCADE)
-- - All hypotheses (ON DELETE CASCADE)
-- - All solutions (ON DELETE CASCADE)
-- - All case_messages (ON DELETE CASCADE)
-- - All uploaded_files (ON DELETE CASCADE)
-- - All case_actions (ON DELETE CASCADE)
-- - All case_checkpoints (ON DELETE CASCADE)
-- - All reports (ON DELETE CASCADE)
```

#### Recommended: Soft Delete Implementation

Add soft delete support for case recovery and audit compliance:

```sql
-- Add deleted_at column to cases table
ALTER TABLE cases ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;

-- Create index for active cases queries
CREATE INDEX idx_cases_active ON cases(organization_id, status)
WHERE deleted_at IS NULL;

-- Soft delete (mark as deleted, keep data)
UPDATE cases
SET
    deleted_at = NOW(),
    status = 'closed',
    closure_reason = 'deleted',
    updated_at = NOW()
WHERE case_id = :case_id
  AND deleted_at IS NULL;  -- Prevent double-delete

-- Query active cases only (exclude deleted)
SELECT * FROM cases
WHERE organization_id = :organization_id
  AND deleted_at IS NULL;

-- Restore deleted case (within retention period)
UPDATE cases
SET
    deleted_at = NULL,
    status = 'investigating',  -- Or previous status
    updated_at = NOW()
WHERE case_id = :case_id
  AND deleted_at IS NOT NULL
  AND deleted_at > NOW() - INTERVAL '90 days';  -- 90-day recovery window

-- Permanent deletion (purge after retention period)
DELETE FROM cases
WHERE deleted_at IS NOT NULL
  AND deleted_at < NOW() - INTERVAL '90 days';
```

**Soft Delete Benefits**:

- ✅ **Recovery**: Users can restore accidentally deleted cases
- ✅ **Audit Trail**: Deletion events are tracked (deleted_at timestamp)
- ✅ **Compliance**: Meet regulatory requirements for data retention
- ✅ **Analytics**: Deleted cases can be analyzed before purge

**Implementation Pattern**:

```python
class CaseRepository:
    async def soft_delete(self, case_id: str) -> bool:
        """Mark case as deleted (recoverable for 90 days)."""
        result = await self.db.execute(
            """
            UPDATE cases
            SET deleted_at = NOW(),
                status = 'closed',
                closure_reason = 'deleted',
                updated_at = NOW()
            WHERE case_id = :case_id AND deleted_at IS NULL
            """,
            {"case_id": case_id}
        )
        return result.rowcount > 0

    async def restore(self, case_id: str) -> bool:
        """Restore a soft-deleted case (within 90-day window)."""
        result = await self.db.execute(
            """
            UPDATE cases
            SET deleted_at = NULL, updated_at = NOW()
            WHERE case_id = :case_id
              AND deleted_at IS NOT NULL
              AND deleted_at > NOW() - INTERVAL '90 days'
            """,
            {"case_id": case_id}
        )
        return result.rowcount > 0

    async def list(
        self,
        organization_id: str,
        include_deleted: bool = False,
        ...
    ) -> tuple[List[Case], int]:
        """List cases, excluding deleted by default."""
        query = "SELECT * FROM cases WHERE organization_id = :organization_id"

        if not include_deleted:
            query += " AND deleted_at IS NULL"

        # ... rest of query
        return await self.db.fetch_all(query, params)

    async def purge_expired(self, days: int = 90) -> int:
        """Permanently delete cases soft-deleted >90 days ago."""
        result = await self.db.execute(
            """
            DELETE FROM cases
            WHERE deleted_at IS NOT NULL
              AND deleted_at < NOW() - INTERVAL ':days days'
            """,
            {"days": days}
        )
        return result.rowcount
```

**Migration Path**:

1. Add `deleted_at` column (nullable, default NULL)
2. Update all queries to filter `WHERE deleted_at IS NULL`
3. Change `delete()` method to set `deleted_at` instead of DELETE
4. Add `restore()` method for recovery
5. Add scheduled job to purge cases after retention period

**Retention Policy**:

| Case Status | Retention After Deletion | Action |
|-------------|--------------------------|-------- |
| Any | 0-90 days | Soft deleted, recoverable |
| Any | 90+ days | Permanently purged (hard delete) |
| Compliance cases | Longer (configurable) | Per regulatory requirements |

---

## 8. Testing Requirements

Before deploying PostgreSQLHybridCaseRepository to production, validate the following:

### 8.1 Schema Validation

```bash
# Deploy PostgreSQL to K8s (if not already running)
kubectl apply -f faultmaven-k8s-infra/applications/postgresql/

# Apply migration script
psql -U faultmaven -d faultmaven_cases < migrations/001_initial_hybrid_schema.sql

# Verify all tables created
psql -U faultmaven -d faultmaven_cases -c "\dt"
# Expected case-domain tables: cases, sessions, evidence, hypotheses, solutions,
# uploaded_files, case_messages, case_actions, case_checkpoints, reports
# (see er-diagram.md for the full 33-table enumeration across all domains).

# Verify indexes created
psql -U faultmaven -d faultmaven_cases -c "\di"
# Expected: ~25-30 indexes
#
# The schema does not ship any database views — all convenience queries are
# implemented in the repository layer (see modules/case/infrastructure/).
```

### 8.2 Repository Integration Tests

**Test cases to validate**:

```python
# Test 1: Basic CRUD
async def test_case_crud():
    repo = PostgreSQLHybridCaseRepository(db_session)

    # Create
    case = Case(case_id="case_test123", title="Test case", ...)
    saved_case = await repo.save(case)
    assert saved_case.case_id == "case_test123"

    # Read
    retrieved = await repo.get("case_test123")
    assert retrieved.title == "Test case"

    # Update
    retrieved.title = "Updated title"
    await repo.save(retrieved)

    # Delete
    deleted = await repo.delete("case_test123")
    assert deleted is True

# Test 2: Evidence persistence
async def test_evidence_normalized_storage():
    case = Case(case_id="case_evi123", ...)
    case.evidence.append(Evidence(evidence_id="evi_001", ...))
    await repo.save(case)

    # Verify evidence in separate table
    result = await db.execute(text("SELECT * FROM evidence WHERE case_id = 'case_evi123'"))
    rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].evidence_id == "evi_001"

# Test 3: Concurrent operations
async def test_concurrent_writes():
    import asyncio

    case_id = "case_concurrent"
    case = Case(case_id=case_id, ...)
    await repo.save(case)

    # Concurrent writes to different tables should succeed
    async def add_evidence():
        case = await repo.get(case_id)
        case.evidence.append(Evidence(...))
        await repo.save(case)

    async def add_file():
        case = await repo.get(case_id)
        case.uploaded_files.append(UploadedFile(...))
        await repo.save(case)

    # Run concurrently
    await asyncio.gather(add_evidence(), add_file())

    # Verify both succeeded
    final_case = await repo.get(case_id)
    assert len(final_case.evidence) == 1
    assert len(final_case.uploaded_files) == 1

# Test 4: Search functionality
async def test_full_text_search():
    cases, total = await repo.search(query="database error", limit=10)
    assert total >= 0
    # Verify relevance ranking works

# Test 5: Cascade delete
async def test_cascade_delete():
    case = Case(case_id="case_cascade", ...)
    case.evidence.append(Evidence(...))
    case.hypotheses["hyp1"] = Hypothesis(...)
    await repo.save(case)

    # Delete case
    await repo.delete("case_cascade")

    # Verify evidence also deleted (FK cascade)
    result = await db.execute(text("SELECT COUNT(*) FROM evidence WHERE case_id = 'case_cascade'"))
    count = result.scalar()
    assert count == 0
```

### 8.3 Performance Benchmarking

**Run with realistic data volume**:

```bash
# Generate test data
python scripts/generate_test_cases.py --count 1000 --evidence-per-case 100

# Benchmark queries
python scripts/benchmark_queries.py

# Expected output:
# Case load (1 case): ~10ms ✅
# Evidence filter (100 evidence): ~5ms ✅
# Full-text search (1000 cases): ~15ms ✅
# Analytics aggregation: ~100ms ✅
```

**Verify indexes are used**:

```sql
EXPLAIN ANALYZE
SELECT * FROM evidence WHERE case_id = 'case_123' AND category = 'symptom_evidence';

-- Expected plan: Index Scan using idx_evidence_case_id (NOT Seq Scan)
```

### 8.4 API Integration Tests

**Test end-to-end with FaultMaven API**:

```bash
# Start API with postgres_hybrid config
CASE_STORAGE_TYPE=postgres_hybrid python -m faultmaven.main

# Create case via API
curl -X POST http://localhost:8090/api/v1/cases \
  -H "Content-Type: application/json" \
  -d '{"title": "Test API integration"}'

# Upload evidence
curl -X POST http://localhost:8090/api/v1/cases/{case_id}/data \
  -F "file=@test.log"

# Query case
curl http://localhost:8090/api/v1/cases/{case_id}

# Verify database records match API response
psql -U faultmaven -d faultmaven_cases -c "SELECT * FROM cases WHERE case_id = '{case_id}'"
psql -U faultmaven -d faultmaven_cases -c "SELECT * FROM evidence WHERE case_id = '{case_id}'"
```

---

## 9. Implementation Checklist

### ✅ Completed
- [x] Design approved (this document)
- [x] Migration script created (`docs/reference/database/001_initial_hybrid_schema.sql`)
- [x] Reports migration script created (`docs/reference/database/005_add_reports_table.sql`) - TD-001
- [x] Repository implementation (`postgresql_hybrid_case_repository.py`)
- [x] Container.py wiring (`CASE_STORAGE_TYPE=postgres_hybrid`)

### ⏳ Pending (Before Production)

- [ ] Deploy PostgreSQL to K8s cluster (if not running)
- [ ] Apply migration script (`migrations/001_initial_hybrid_schema.sql`)
- [ ] Run integration tests (Section 8.2)
- [ ] Run performance benchmarks (Section 8.3)
- [ ] Run API integration tests (Section 8.4)
- [ ] Verify all indexes are used (EXPLAIN ANALYZE)
- [ ] Update `.env` to use `CASE_STORAGE_TYPE=postgres_hybrid`
- [ ] Deploy FaultMaven API with hybrid repository
- [ ] Monitor production metrics (query performance, error rates)

---

## Summary

This design provides:

✅ **Performance**: Optimized for FaultMaven's actual access patterns
✅ **Scalability**: Handles 100K+ cases efficiently
✅ **Maintainability**: Clear normalization decisions with rationale
✅ **Flexibility**: JSONB for evolving schemas (low-cardinality data)
✅ **Concurrency**: Row-level locking eliminates lost update problems
✅ **Production-Ready**: Designed for K8s PostgreSQL deployment

**Development Philosophy**:
> Build it clean, build it right. No backward compatibility needed during development.

**What's Different from Legacy**:
- **11 tables** (not 1) → Better filtering and search performance
- **Normalized evidence/hypotheses** → Row-level locking, concurrent writes
- **JSONB for flexible data** → Inquiry, conclusions, progress tracking
- **Full-text search indexes** → Fast case and evidence search
- **No lost updates** → Database ACID guarantees

---

**Document Control**:

- **Author**: FaultMaven Team
- **Created**: 2025-11-09
- **Last Updated**: 2026-04-18
- **Version**: 3.4 (Authoritative)
- **Status**: ✅ Implemented — live schema (baseline migration `424078e5aa04`)

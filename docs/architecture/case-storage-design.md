# FaultMaven Case Storage Design - Performant Production Standard

**Version**: 3.1
**Status**: Authoritative Standard
**Supersedes**: case-data-model-design.md, db-design-specifications.md
**Last Updated**: 2025-01-09

---

## Implementation Status

**Current State** (as of 2025-01-09):

| Component | Status | Location |
|-----------|--------|----------|
| ✅ Design | Approved | This document |
| ✅ Migration Script | Complete | `/migrations/001_initial_hybrid_schema.sql` |
| ✅ Repository Code | Complete | `/faultmaven/infrastructure/persistence/postgresql_hybrid_case_repository.py` |
| ⏳ Integration Tests | Pending | Not yet run against real PostgreSQL |
| ⏳ Performance Validation | Pending | Benchmarks needed |
| ⏳ Production Deploy | Pending | PostgreSQL not yet deployed to K8s |

**Active Implementation**:
- **Development**: `InMemoryCaseRepository` (Python dict, data lost on restart)
- **Production Target**: `PostgreSQLHybridCaseRepository` (10-table hybrid schema)
- **Legacy**: `PostgreSQLCaseRepository` (single-table JSONB, deprecated)

**Reality Check**:
The 10-table hybrid schema is **designed and coded but NOT yet tested or deployed**. All performance metrics in this document are **estimated targets**, not measured results.

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

### 2.3 PostgreSQL Implementation (Production)

**File**: `faultmaven/infrastructure/persistence/case_repository.py`

```python
class PostgreSQLCaseRepository(CaseRepository):
    """Production repository using hybrid normalized + JSONB storage."""

    def __init__(self, db_session):
        self.db = db_session

    async def save(self, case: Case) -> Case:
        # Maps to normalized tables + JSONB columns
        # See Section 4 for schema details
```

**Characteristics**:
- ✅ Persistent across restarts
- ✅ ACID transactions
- ✅ Optimized queries via indexes
- ✅ Concurrent access safe
- ✅ Production-grade performance

**When to use**: Production K8s deployment, staging

---

## 3. Data Model

### 3.1 Core Case Structure

```python
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
    status: CaseStatus              # consulting | investigating | resolved | closed
    status_history: List[CaseStatusTransition]
    closure_reason: Optional[str]

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
    consulting: ConsultingData              # PostgreSQL: JSONB
    problem_verification: Optional[ProblemVerification]  # PostgreSQL: JSONB
    working_conclusion: Optional[WorkingConclusion]      # PostgreSQL: JSONB
    root_cause_conclusion: Optional[RootCauseConclusion]  # PostgreSQL: JSONB
    path_selection: Optional[PathSelection]  # PostgreSQL: JSONB
    degraded_mode: Optional[DegradedMode]    # PostgreSQL: JSONB
    escalation_state: Optional[EscalationState]  # PostgreSQL: JSONB
    documentation: DocumentationData         # PostgreSQL: JSONB

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

### 4.1 Table Design (10 Tables)

```
Core Tables (4):
├── cases              -- Main case data + JSONB for low-cardinality items
├── users              -- User accounts
├── organizations      -- Multi-tenancy
└── sessions           -- Session management

High-Cardinality Tables (6):
├── evidence           -- Investigation evidence (many per case)
├── hypotheses         -- Hypotheses being tested (many per case)
├── solutions          -- Proposed/verified solutions (few per case)
├── uploaded_files     -- File metadata (many per case)
├── case_messages      -- Turn-by-turn messages (very high volume)
└── case_status_transitions  -- Audit trail (few per case)
```

### 4.2 cases (Main Table)

```sql
CREATE TABLE cases (
    -- ============================================================
    -- Identity
    -- ============================================================
    case_id VARCHAR(17) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id),
    organization_id VARCHAR(255) NOT NULL REFERENCES organizations(organization_id),
    title VARCHAR(200) NOT NULL,
    description TEXT DEFAULT '',

    -- ============================================================
    -- Status & Lifecycle
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'consulting',
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
    consulting JSONB NOT NULL DEFAULT '{}'::jsonb,
    problem_verification JSONB,
    working_conclusion JSONB,
    root_cause_conclusion JSONB,
    path_selection JSONB,
    degraded_mode JSONB,
    escalation_state JSONB,
    documentation JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT cases_status_check
        CHECK (status IN ('consulting', 'investigating', 'resolved', 'closed')),

    CONSTRAINT cases_closure_reason_check
        CHECK (
            closure_reason IS NULL OR
            closure_reason IN ('resolved', 'abandoned', 'escalated', 'consulting_only', 'duplicate', 'other')
        ),

    CONSTRAINT cases_strategy_check
        CHECK (investigation_strategy IN ('active_incident', 'post_mortem')),

    CONSTRAINT cases_turn_check
        CHECK (current_turn >= 0 AND turns_without_progress >= 0),

    CONSTRAINT cases_resolved_timestamp_check
        CHECK (
            (status = 'resolved' AND resolved_at IS NOT NULL AND closed_at IS NOT NULL) OR
            (status != 'resolved' AND resolved_at IS NULL)
        ),

    CONSTRAINT cases_closed_timestamp_check
        CHECK (
            (status IN ('resolved', 'closed') AND closed_at IS NOT NULL) OR
            (status NOT IN ('resolved', 'closed') AND closed_at IS NULL)
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
    category VARCHAR(30) NOT NULL,              -- observation | measurement | configuration | etc.
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
            'observation', 'measurement', 'configuration', 'timeline',
            'third_party_report', 'code_artifact', 'communication'
        )),

    CONSTRAINT evidence_source_check
        CHECK (source_type IN ('user_upload', 'system_collected', 'agent_generated')),

    CONSTRAINT evidence_form_check
        CHECK (form IN ('text', 'image', 'metric', 'structured'))
);

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

### 4.8 case_status_transitions (Audit Table)

```sql
CREATE TABLE case_status_transitions (
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

    CONSTRAINT status_transitions_status_check
        CHECK (
            from_status IN ('consulting', 'investigating', 'resolved', 'closed') AND
            to_status IN ('consulting', 'investigating', 'resolved', 'closed')
        )
);

-- Indexes
CREATE INDEX idx_status_transitions_case ON case_status_transitions(case_id);
CREATE INDEX idx_status_transitions_timestamp ON case_status_transitions(transitioned_at DESC);

COMMENT ON TABLE case_status_transitions IS 'Audit trail of status changes';
```

### 4.9 Supporting Tables (users, organizations, sessions)

```sql
CREATE TABLE users (
    user_id VARCHAR(255) PRIMARY KEY,
    organization_id VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(200),
    role VARCHAR(50) NOT NULL DEFAULT 'user',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMP WITH TIME ZONE,

    CONSTRAINT users_role_check
        CHECK (role IN ('user', 'admin', 'system_admin'))
);

CREATE INDEX idx_users_org ON users(organization_id);
CREATE INDEX idx_users_email ON users(email);

CREATE TABLE organizations (
    organization_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    settings JSONB,
    plan VARCHAR(50) DEFAULT 'free',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT organizations_plan_check
        CHECK (plan IN ('free', 'pro', 'enterprise'))
);

CREATE TABLE sessions (
    session_id VARCHAR(50) PRIMARY KEY,
    user_id VARCHAR(255) REFERENCES users(user_id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_activity_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
```

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
| **Consulting Data** | One (1) | Never filtered | ❌ **JSONB** | Always with case, flexible |
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
  LEFT JOIN consulting co ON c.case_id = co.case_id
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

| Operation | Fully Normalized (32 tables) | Hybrid (10 tables) | Single Table (current) |
|-----------|------------------------------|-------------------|------------------------|
| Load case | 8-12 JOINs (~50ms) | 4 JOINs + JSONB (~10ms) | 1 query (~2ms) |
| Filter evidence | Efficient (indexed) | Efficient (indexed) | ❌ Slow (JSONB scan) |
| Search cases | Complex JOIN | Simple query | Simple query |
| Update evidence | UPDATE 1 row | UPDATE 1 row | ❌ Rewrite JSONB array |
| Concurrent updates | Safe (row locks) | Safe (row locks) | ❌ Full case lock |

**Our hybrid approach wins on balance!**

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
WHERE case_id = $1 AND category = 'observation'
ORDER BY collected_at DESC;

-- Search cases (full-text)
-- Target: ~15ms (GIN index on tsvector)
SELECT * FROM cases
WHERE to_tsvector('english', title || ' ' || description) @@ to_tsquery('api performance')
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
# Expected: 10 tables (cases, evidence, hypotheses, solutions, case_messages, uploaded_files, case_status_transitions, case_tags, agent_tool_calls, plus system tables)

# Verify indexes created
psql -U faultmaven -d faultmaven_cases -c "\di"
# Expected: ~25-30 indexes

# Verify views created
psql -U faultmaven -d faultmaven_cases -c "\dv"
# Expected: case_overview, active_hypotheses, recent_evidence
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
SELECT * FROM evidence WHERE case_id = 'case_123' AND category = 'LOGS_AND_ERRORS';

-- Expected plan: Index Scan using idx_evidence_case_id (NOT Seq Scan)
```

### 8.4 API Integration Tests

**Test end-to-end with FaultMaven API**:

```bash
# Start API with postgres_hybrid config
CASE_STORAGE_TYPE=postgres_hybrid python -m faultmaven.main

# Create case via API
curl -X POST http://localhost:8000/api/v1/cases \
  -H "Content-Type: application/json" \
  -d '{"title": "Test API integration"}'

# Upload evidence
curl -X POST http://localhost:8000/api/v1/cases/{case_id}/data \
  -F "file=@test.log"

# Query case
curl http://localhost:8000/api/v1/cases/{case_id}

# Verify database records match API response
psql -U faultmaven -d faultmaven_cases -c "SELECT * FROM cases WHERE case_id = '{case_id}'"
psql -U faultmaven -d faultmaven_cases -c "SELECT * FROM evidence WHERE case_id = '{case_id}'"
```

---

## 9. Implementation Checklist

### ✅ Completed
- [x] Design approved (this document)
- [x] Migration script created (`migrations/001_initial_hybrid_schema.sql`)
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
- **10 tables** (not 1) → Better filtering and search performance
- **Normalized evidence/hypotheses** → Row-level locking, concurrent writes
- **JSONB for flexible data** → Consulting, conclusions, progress tracking
- **Full-text search indexes** → Fast case and evidence search
- **No lost updates** → Database ACID guarantees

---

**Document Control**:
- **Author**: FaultMaven Team
- **Created**: 2025-11-09
- **Last Updated**: 2025-01-09
- **Version**: 3.1 (Authoritative)
- **Status**: Design Approved, Implementation Pending Testing

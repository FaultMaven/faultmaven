# FaultMaven Case Storage Design - Performant Production Standard

**Version**: 3.7
**Status**: Authoritative Standard
**Last Updated**: 2026-04-19

> **NOTE on `organization_id` placement**: Per the v2.1 locked design in [deployment-schema-strategy.md](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) §10, all tenanted case-domain tables carry `organization_id` for RLS policy enforcement in PostgreSQL. The v2.0-era approach of placing `organization_id` only on `cases` and filtering child tables via JOIN is superseded. The per-table DDL below reflects the correct `organization_id NOT NULL FK` placement on each tenanted table.

---

## Deployment Applicability

> **Read this before interpreting any DDL in this document.**

The DDL definitions below represent the **logical schema**: both SQLite (Local Deployment) and PostgreSQL (Cloud Deployment) implement every table and column listed. This is the Tier 1 shape — dialect-neutral, enforced in both environments via SQLAlchemy ORM models at `faultmaven/infrastructure/persistence/models.py`.

The following constructs are **Tier 2 (PostgreSQL-only)** augmentations. SQLite deployments omit them entirely by design — they are not regressions:

- `CHECK` constraints involving regex patterns, cross-column predicates, or conditional logic that SQLite handles differently.
- Partial indexes (`WHERE` clause on an index).
- Expression indexes, GIN indexes, and GIST indexes (including `to_tsvector` full-text search indexes and JSONB path expression indexes).
- Table partitioning by range.
- `JSONB` merge operators (`||`) in application code paths that are cloud-only.

Wherever a DDL element in this document is Tier 2, it is marked inline with **"Tier 2 (PostgreSQL-only)"**. All other DDL is Tier 1.

For the complete policy on dialect tiering, the per-table deployment matrix, and enum governance, see the authoritative strategy document: [deployment-schema-strategy.md](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

---

## Implementation Status

**Current State** (as of 2026-04-18):

| Component | Status | Location |
| --- | --- | --- |
| ✅ Design | Approved | This document |
| ✅ PostgreSQL Schema | Complete | `alembic/versions/20260317_1919_424078e5aa04_001_clean_baseline.py` |
| ✅ SQLite Schema | Complete | Auto-created by `SQLiteCaseRepository` |
| ✅ Reports Migration | Complete | `alembic/versions/20260329_1200_add_reports_table.py` (TD-001) |
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
   - [7.5 Row-Level Security (RLS)](#75-row-level-security-postgresql-cloud--tier-2)
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
    # NOTE: cases has no description column. Domain Case model has title only.

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
    # Turn Tracking (stored inside cases.progress JSONB blob — not first-class columns)
    # ============================================================
    turn_history: List[TurnProgress]
    # current_turn and turns_without_progress live in the cases.progress JSONB blob.

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
    # investigation_strategy is not a first-class column; stored in cases.progress JSONB blob.

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

### 4.1 Table Design (case-domain tables)

> **Post-v2.1 table count**: 4 tables deleted from the v3.5 list (`sessions`, `evidence_artifacts`, `standalone_evidence`, `agent_tool_calls` v1); `agent_tool_calls_v2` renamed to canonical `agent_tool_calls`. See [deployment-schema-strategy.md §2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) for the full 29-table applicability matrix.

```
Core Table (1):
└── cases              -- Main case data + JSONB for low-cardinality items

High-Cardinality Tables (8):
├── evidence           -- Investigation evidence, single table, case_id NOT NULL FK
├── hypotheses         -- Hypotheses being tested (many per case)
├── solutions          -- Proposed/verified solutions (few per case)
├── uploaded_files     -- File metadata (many per case)
├── case_messages      -- Turn-by-turn messages (very high volume)
├── case_actions       -- Audit trail of actions & status transitions
│                       -- (Python alias: CaseStatusTransitionModel)
├── case_checkpoints   -- State snapshots (one per turn)
├── reports            -- Generated reports (few per case, versioned)
└── case_entities      -- Phase 4 cross-artifact entity index (feature-flagged)

Agent Execution Cascade (3):
├── investigation_sessions  -- Per-investigation agent context (case-owned)
├── agent_executions        -- Per-turn agent run metadata
└── agent_tool_calls        -- Tool-call log (renamed from agent_tool_calls_v2)

DELETED tables (v2.1):
├── sessions           -- DELETED: SQL sessions violate case-and-session-concepts.md v2.1.
│                         Auth sessions live in Redis via RedisSessionStore only.
├── evidence_artifacts -- DELETED: functionally dead (written by standalone API, never read by engine)
├── standalone_evidence -- DELETED: same reason — standalone path removed entirely
└── agent_tool_calls v1 -- DELETED: zero functional readers/writers in production code paths
```

The full live table count across user + case + knowledge + conversion + config domains is **30** (post-v2.1 redesign, Phase 4 entity registry added) — see `er-diagram.md` for the authoritative enumeration.

### 4.2 cases (Main Table)

> **v2.1 column status — what changed from v3.5.**
>
> **Columns DROPPED (v2.1 locked design)**:
>
> - `session_id` — **DROPPED ENTIRELY** (not just the FK constraint). Binding a case to an auth session is Anti-Pattern 1 per [case-and-session-concepts.md](../../case-and-session/case-and-session-concepts.md) v2.1. The `sessions` table itself is also deleted. See [deployment-schema-strategy.md §8.1](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - `degraded_mode TEXT` — **DELETED**. Deprecated column with backward-compat comment. Per project directive (no backward compatibility), removed from schema. See [deployment-schema-strategy.md §4.5](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
>
> **Columns added (Tier 1, confirmed domain-backed)**:
>
> - `closure_reason VARCHAR(100)` — Tier 2 PG-only CHECK on valid values.
> - `last_activity_at TIMESTAMPTZ NOT NULL` — domain model carries this field.
> - `resolved_at TIMESTAMPTZ` — domain model carries this field.
> - `closed_at TIMESTAMPTZ` — domain model carries this field.
>
> **Columns NOT added (stale design — no domain backing)**:
>
> - `description TEXT` — Removed from spec. Domain `Case` model has `title` only. No `description` field. See [deployment-schema-strategy.md §18](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - `investigation_strategy VARCHAR(20)` — Not in domain model; remains inside `cases.progress` JSONB blob.
> - `current_turn INTEGER`, `turns_without_progress INTEGER` — Not first-class columns. Stored inside `cases.progress` JSON blob (Tier 1). The CHECK constraints below using these names are Tier 2 PG-only aspirational and remain aspirational since the fields are not promoted.
>
> **Other ORM columns** (still present):
>
> - `team_id VARCHAR(36)` (indexed) — team assignment for the case; FK to `teams.team_id`.
> - `is_archived BOOLEAN`, `archived_at TIMESTAMPTZ` — data-lifecycle archival flags. Soft delete is stripped; `is_archived` + `archived_at` is the supported preservation pattern (see §7.3).

```sql
CREATE TABLE cases (
    -- ============================================================
    -- Identity
    -- ============================================================
    case_id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(user_id),
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id),
    team_id VARCHAR(36) REFERENCES teams(team_id),
    title VARCHAR(200) NOT NULL,
    -- NOTE: cases.description does NOT exist. Domain Case model has title only.
    -- Removed from spec per deployment-schema-strategy.md §18.

    -- ============================================================
    -- Status & Lifecycle
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'inquiry',
    closure_reason VARCHAR(100),
    -- investigation_strategy: not a first-class column; stored in cases.progress JSONB blob.

    -- ============================================================
    -- Turn Tracking (JSON blob fields — not first-class columns)
    -- ============================================================
    -- current_turn and turns_without_progress are stored inside the cases.progress
    -- JSON blob, not as first-class SQL columns. The CHECK constraints below using
    -- these names are Tier 2 (PostgreSQL-only) aspirational for if the fields are
    -- ever promoted to dedicated columns; for now they remain inside the JSONB blob.

    -- ============================================================
    -- Timestamps (Tier 1 — all confirmed domain-backed)
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,

    -- ============================================================
    -- Archival (supported preservation pattern — soft delete stripped, see §7.3)
    -- ============================================================
    is_archived BOOLEAN NOT NULL DEFAULT false,
    archived_at TIMESTAMP WITH TIME ZONE,

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
    -- Note: All CHECK constraints below are Tier 2 (PostgreSQL-only).
    -- The ORM enforces enum membership at the application layer for both dialects.
    -- SQLite deployments omit these constraints by design.
    -- ============================================================

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT cases_status_check
        CHECK (status IN ('inquiry', 'investigating', 'resolved', 'closed')),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT cases_closure_reason_check
        CHECK (
            closure_reason IS NULL OR
            closure_reason IN ('resolved', 'abandoned', 'escalated', 'inquiry_only', 'duplicate', 'other')
        ),

    -- NOTE: cases_strategy_check removed — investigation_strategy is not a first-class
    -- column (lives in cases.progress JSONB blob). Removed from spec per strategy doc §18.

    -- NOTE: cases_turn_check removed — current_turn and turns_without_progress are not
    -- first-class columns (live in cases.progress JSONB blob). Removed from spec.

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT cases_resolved_timestamp_check
        CHECK (
            (status = 'resolved' AND resolved_at IS NOT NULL) OR
            (status != 'resolved' AND resolved_at IS NULL)
        ),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT cases_closed_timestamp_check
        CHECK (
            (status = 'closed' AND closed_at IS NOT NULL) OR
            (status != 'closed' AND closed_at IS NULL)
        ),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT cases_timestamp_order_check
        CHECK (
            created_at <= updated_at AND
            created_at <= last_activity_at AND
            (resolved_at IS NULL OR created_at <= resolved_at) AND
            (closed_at IS NULL OR created_at <= closed_at) AND
            (resolved_at IS NULL OR closed_at IS NULL OR resolved_at <= closed_at)
        )
);

-- Tier 1 indexes (both dialects)
CREATE INDEX idx_cases_user_status ON cases(user_id, status);
CREATE INDEX idx_cases_org_status ON cases(organization_id, status);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_cases_last_activity ON cases(last_activity_at DESC);

-- Tier 2 (PostgreSQL-only) — partial index (WHERE clause)
--
-- DEFERRED — not in current schema (audit fix, storage redesign Phase 9):
-- `turns_without_progress` is NOT a first-class column. It lives inside the
-- `cases.progress` JSONB blob (confirmed above — see §4.2 DDL note and strategy doc §18).
-- This index as written references a non-existent column and cannot be created.
-- To make it feasible post-Phase 7 (JSONB columns), rewrite as a JSONB expression index:
--   CREATE INDEX idx_cases_stuck ON cases(((progress->>'turns_without_progress')::int))
--       WHERE status = 'investigating'
--         AND (progress->>'turns_without_progress')::int >= 3;
-- The rewritten form is feasible now that `progress` is JSONB (Phase 7), but is NOT
-- implemented — no migration has created it. Document accordingly.
-- Original (unsatisfiable) form retained for reference only:
-- CREATE INDEX idx_cases_stuck ON cases(turns_without_progress)
--     WHERE status = 'investigating' AND turns_without_progress >= 3;

-- Tier 2 (PostgreSQL-only) — JSONB expression indexes
--
-- NOTE (audit fix, storage redesign Phase 9): Phase 7 converted `path_selection` and
-- `problem_verification` to JSONB columns, so JSONB expression indexes ARE now feasible.
-- However, the `->>` operator syntax below (used directly on column names) is correct for
-- JSONB in PostgreSQL — these indexes are syntactically valid post-Phase 7 and represent
-- the intended implementation. They are documented as deferred because no migration has
-- created them yet. The DDL below reflects the correct PG-on-JSONB form:
CREATE INDEX idx_cases_path ON cases((path_selection->>'path'))
    WHERE path_selection IS NOT NULL;
CREATE INDEX idx_cases_urgency ON cases((problem_verification->>'urgency_level'))
    WHERE problem_verification IS NOT NULL;
-- TODO: create these indexes in a follow-up migration once production PostgreSQL is deployed.

-- Tier 2 (PostgreSQL-only) — GIN tsvector full-text search index (title only; description removed from spec)
CREATE INDEX idx_cases_search ON cases USING gin(
    to_tsvector('english', title)
);

COMMENT ON TABLE cases IS 'Root case entity with embedded low-cardinality data in JSONB';
```

### 4.3 evidence (Single-Table Design, v2.1 Locked — Audit-Pass Corrected)

#### Role of `summary` vs `extract`

Every `evidence` row carries two semantically distinct content fields. They are **not** redundant — confusing them was the design error this section exists to prevent.

| Field | Type | Required? | What it carries |
| --- | --- | --- | --- |
| `summary` | `VARCHAR(500) NOT NULL` | always | Short label for this row. Used for UI list views, headers, and quick scanning. |
| `extract` | `TEXT NULL` | conditional | Bulk content backing the summary. Required for Paths 1 and 3 (application-enforced); optional for Path 2 (DB allows NULL). |

The two fields are filled differently per the three evidence-creation paths (see [evidence-flow-architecture.md](../../data-processing/evidence-flow-architecture.md)):

| Path | `form` | Source of `summary` | Source of `extract` |
| --- | --- | --- | --- |
| **1. File upload** (Tier 0+1, no LLM) | `DOCUMENT` | Auto-generated file summary | Structural index from preprocessor (`file_extract` + `search_map` + `file_meta`) — what the LLM reads in `<evidence_collected>` |
| **2. LLM `evidence_to_add`** (after the LLM call) | `SUBMITTED_DATA` | LLM's brief description | LLM's optional verbatim quote (the only path where `extract` may be NULL) |
| **3. Tier 2/3 tool findings** (`search_file`, `deep_analysis`) | `SUBMITTED_DATA` | Agent-written description | Tool's search excerpts or analysis answer |

**Why this matters.** The structural index on Path 1 is what the agent actually reads to investigate; without it the LLM has nothing to work with except a 500-char label. The verbatim quote on Path 2 grounds the LLM's finding in a specific snippet so a reader can verify the claim. Same column (`extract`) carries both because the role is identical: bulk content backing the summary label. The column name reflects that — extracted text from a source.

**File pointer is separate.** Neither `summary` nor `extract` carries the storage location of the original raw file. That lives on `uploaded_files.storage_ref`, reachable from `evidence.source_file_id`. Inline-only evidence (Path 2 with no quote) has both `extract IS NULL` and `source_file_id IS NULL`.

**CHECK constraints (mirrored at the Pydantic layer):**

```sql
CONSTRAINT evidence_summary_not_empty CHECK (LENGTH(TRIM(summary)) > 0)
CONSTRAINT evidence_extract_not_empty CHECK (extract IS NULL OR LENGTH(TRIM(extract)) > 0)
```

The Pydantic `Evidence` model has a matching `_extract_not_empty_when_set` validator. Same rule, two layers, neither bypassable independently.

**Naming caveat.** `EXTRACT` is a SQL function in PostgreSQL (`EXTRACT(YEAR FROM dt)`); using it as a column name works (PG treats it as a non-reserved keyword and SQLAlchemy quotes correctly), but raw-SQL readers should be aware that `SELECT EXTRACT(YEAR FROM created_at), extract FROM evidence` reads slightly ambiguously. The semantic accuracy across all three paths is structural; the keyword friction is documentary.

---

> **v2.1 design note**: The single-table evidence model with `case_id NOT NULL` FK is the **locked final design** per [deployment-schema-strategy.md §7 and §12 decision #11](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md). The v2.0-era proposal of "consolidated two-tables-plus-join" was rejected as scope creep. Every evidence record is a case-specific interpretation; there is no general "case-less evidence" concept.
>
> **Audit-pass correction (v2.2)**: bundled-in cosmetic column renames and speculative new columns were rolled back. Only the changes below remain.
>
> **Locked changes to the existing `evidence` table**:
>
> - **Rename** `source_type_new` → `source_type` (finishes the abandoned migration; ORM alias dropped) — [strategy doc §4.2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - **Type change** `file_size`: `Integer nullable` → `BIGINT NOT NULL`. **Column name unchanged**.
> - **Width normalization**: id columns to `VARCHAR(36)` per [strategy doc §4.3](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - **Enum binding fix** for existing `category` column: previously held `EvidenceCategoryEnum` (LOGS_AND_ERRORS / STRUCTURED_CONFIG / …) which was a misleadingly-named data-form enum. The ORM enum class is deleted; the column now binds to domain `EvidenceCategory` directly (`symptom_evidence | causal_evidence | …`). **Column name unchanged**.
>
> **New columns added**:
>
> - `form VARCHAR(20) NOT NULL` — bound to the renamed `EvidenceForm` enum (`text|image|metric|structured`). The data-form vocabulary that previously lived in the misnamed `EvidenceCategoryEnum` now has its own column.
> - `is_primary BOOLEAN NOT NULL DEFAULT FALSE` — preserves the "primary evidence per case" concept that was previously on the deleted `evidence_artifacts.is_primary`. Consumed by `list_evidence_tool` (the surviving agent tool path). Setter API is deferred — no current writer; the column stays at `FALSE` until a primary-evidence UI feature is wired.
> - `content_type VARCHAR(100)` — MIME type. Already available from the upload `Attachment.content_type` field; previously discarded. Forward-looking consumers: agent-tool dispatch (decide whether to UTF-8-decode vs extract-image vs parse-JSON), UI rendering (image preview vs syntax-highlighted code vs PDF viewer), S3 presigned-URL `Content-Type` header.
>
> **Tier 2 PG-only enhancements (deferred — no current producer)**:
>
> - `reliability_score REAL` with PG-only CHECK (0..1).
> - `tags TEXT` (comma-separated for SQLite parity; PG-only `TEXT[]` + GIN).
>
> **Bundled-in cosmetic changes ROLLED BACK** ([strategy doc §12.21](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md)):
>
> - ❌ Rename `file_size` → `content_size_bytes` (kept name, only changed type)
> - ❌ Rename `upload_timestamp` → `collected_at` (kept name)
> - ❌ Rename ORM `category` column → `primary_purpose` (kept name; only fixed enum binding)
> - ❌ Rename `filename` → `original_filename` (kept name)
> - ❌ Add `content_type` (MIME) — no reader
> - ❌ Add `collected_by` — duplicates linkage via `source_file_id`
> - ❌ Add `created_at` — redundant with `upload_timestamp`
>
> **Existing columns preserved** (the table extends, doesn't replace):
>
> - `evidence_id`, `case_id`, `organization_id`, `category`, `source_type`, `summary`, `preprocessed_content` (Text NOT NULL — used by agent tools), `content_ref`, `file_size` (now BIGINT NOT NULL), `filename`, `content_hash`, `collected_at_turn`, `source_file_id`, `upload_timestamp`, `metadata`.

```sql
CREATE TABLE evidence (
    -- ============================================================
    -- Existing columns (preserved — the table extends, doesn't replace)
    -- ============================================================
    evidence_id         VARCHAR(36) PRIMARY KEY,                 -- widened to VARCHAR(36)
    case_id             VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    organization_id     VARCHAR(36) NOT NULL REFERENCES organizations(organization_id),

    -- domain EvidenceCategory enum (symptom_evidence | causal_evidence | ...)
    -- Was misnamed-bound to EvidenceCategoryEnum; now bound to domain EvidenceCategory.
    category            VARCHAR(50) NOT NULL,

    -- domain EvidenceSourceType enum (logs | metrics | configuration | visual | user_description)
    -- Renamed from physical column source_type_new.
    source_type         VARCHAR(50),

    summary             VARCHAR(500) NOT NULL,
    preprocessed_content TEXT NOT NULL,                          -- preserved: used by agent tools
    content_ref         VARCHAR(1000),
    filename            VARCHAR(255),
    file_size           BIGINT NOT NULL,                         -- type change only: Integer nullable → BIGINT NOT NULL
    content_hash        VARCHAR(64),
    collected_at_turn   INTEGER,
    source_file_id      VARCHAR(36),
    upload_timestamp    TIMESTAMP WITH TIME ZONE NOT NULL,
    metadata            JSON,                                    -- column name `metadata`, ORM attribute `evidence_metadata`

    -- ============================================================
    -- New columns added by v2.1 redesign
    -- ============================================================
    -- EvidenceForm enum (data-content type): text | image | metric | structured
    -- Replaces the misnamed ORM EvidenceCategoryEnum.
    form                VARCHAR(20) NOT NULL,

    -- Preserves the "primary evidence per case" concept (was evidence_artifacts.is_primary).
    -- Consumed by list_evidence_tool; setter API deferred.
    is_primary          BOOLEAN NOT NULL DEFAULT FALSE,

    -- MIME type from upload Attachment.content_type (already available; previously discarded).
    -- Forward-looking consumers: agent-tool dispatch, UI rendering, S3 presigned-URL Content-Type.
    content_type        VARCHAR(100),

    -- ============================================================
    -- Tier 1 columns added (both dialects); Tier 2 enhancements + producers deferred
    -- ============================================================
    -- Column added Tier 1 nullable; Tier 2 PG-only adds CHECK (0..1).
    -- No current producer (agent does not yet compute scores).
    reliability_score   REAL,

    -- Column added Tier 1 as comma-separated string; Tier 2 PG-only switches to TEXT[] + GIN.
    -- No current producer (no tag-writer UI yet).
    tags                TEXT,

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT evidence_form_check
        CHECK (form IN ('text', 'image', 'metric', 'structured')),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT evidence_category_check
        CHECK (category IN (
            'symptom_evidence',
            'causal_evidence',
            'mitigation_evidence',
            'solution_evidence',
            'resolution_evidence',
            'contextual_evidence',
            'rejected'
        )),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT evidence_source_type_check
        CHECK (source_type IS NULL OR source_type IN ('logs', 'metrics', 'configuration', 'visual', 'user_description')),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT evidence_reliability_check
        CHECK (reliability_score IS NULL OR (reliability_score BETWEEN 0 AND 1))
);

-- Tier 1 indexes (both dialects)
CREATE INDEX idx_evidence_case ON evidence(case_id);
CREATE INDEX idx_evidence_category ON evidence(case_id, category);
CREATE INDEX idx_evidence_upload_timestamp ON evidence(upload_timestamp DESC);

-- Tier 2 (PostgreSQL-only) — GIN array index (when tags is TEXT[] in PG)
CREATE INDEX idx_evidence_tags ON evidence USING gin(tags);

COMMENT ON TABLE evidence IS 'Investigation evidence — single table, case_id NOT NULL FK. Standalone evidence path deleted (v2.1).';
COMMENT ON COLUMN evidence.category IS 'Domain EvidenceCategory enum (investigation role). ORM EvidenceCategoryEnum class deleted; column binds to domain enum directly.';
COMMENT ON COLUMN evidence.source_type IS 'EvidenceSourceType enum. Physical column renamed from source_type_new (alias removed).';
COMMENT ON COLUMN evidence.file_size IS 'Type changed Integer nullable → BIGINT NOT NULL. Column name unchanged.';
COMMENT ON COLUMN evidence.form IS 'New: EvidenceForm enum (data-content type). Replaces the misnamed ORM EvidenceCategoryEnum.';
COMMENT ON COLUMN evidence.is_primary IS 'New: preserves primary-evidence-per-case concept (was evidence_artifacts.is_primary). Consumed by list_evidence_tool; setter API deferred.';
COMMENT ON COLUMN evidence.content_type IS 'New: MIME type from upload Attachment. Forward-looking consumers: agent-tool dispatch, UI rendering, S3 presigned-URL Content-Type.';
```

#### `evidence.metadata` JSON contract

The `metadata JSON` column is structured — not a free-for-all bag. Top-level keys are **namespaced** and **additive**; a given key is owned by a specific consumer and is not written by any other path. Absence of a key is always valid — existing evidence rows predate each key's introduction and must continue to work.

Canonical shape (pydantic model `EvidenceMetadata`, see [faultmaven/core/preprocessing/evidence_metadata.py](../../../../faultmaven/core/preprocessing/evidence_metadata.py)):

```python
class ClassificationMetadata(BaseModel):
    """Tier 0 classifier signals for this evidence.
    Written by PreprocessingService.classify_and_extract at persistence time.
    Read by context_builder to surface a confidence marker in the <evidence> tag."""
    confidence: float               # 0.0 – 1.0
    source: str                     # "user_override" | "agent_hint" | "source_url" | "browser_context" | "rule_based" | "rule_based_best_effort"
    failed: bool                    # classification_failed short-circuit path hit
    suggested_types: list[str]      # non-empty only when failed=True

class ExtractorAttempt(BaseModel):
    """One pass through the extraction pipeline. Populated by Phase 2."""
    data_type: str                  # DataType enum value
    sanity_passed: bool
    duration_ms: int

class ExtractorMetadata(BaseModel):
    """Which extractor produced preprocessed_content, and any retries.
    Written by PreprocessingService after extraction."""
    chosen_type: str                # data type of the extractor whose output was kept
    attempts: list[ExtractorAttempt] = []

class EntitiesMetadata(BaseModel):
    """Overflow markers for Phase 4's case_entities writes.
    Rows live in the separate table; this field only records caps hit on ingest."""
    overflow_types: list[str] = []  # entity types that exceeded the 500-per-evidence cap

class CoverageMetadata(BaseModel):
    """Extractor time-range details beyond the promoted columns (Phase 3).
    The coverage_start_ts / coverage_end_ts columns are the queryable projection."""
    source: Optional[str] = None    # which timestamp pattern matched (iso8601, syslog_bsd, epoch_s, ...)

class EvidenceMetadata(BaseModel):
    classification: Optional[ClassificationMetadata] = None
    extractor:      Optional[ExtractorMetadata] = None
    entities:       Optional[EntitiesMetadata] = None
    coverage:       Optional[CoverageMetadata] = None
```

**Ownership rules.**

| Key | Writer | Reader | Introduced in |
| --- | --- | --- | --- |
| `classification` | `PreprocessingService` | `context_builder` (low-confidence marker) | Phase 1 |
| `extractor` | `PreprocessingService` | observability only (no agent-visible effect) | Phase 1 (minimal) / Phase 2 (full `attempts`) |
| `entities` | `PreprocessingService` (on cap overflow) | agent tool `find_entity` reads the row-level table; overflow marker tells the agent "not all entities are indexed" | Phase 4 |
| `coverage` | `PreprocessingService` | context builder (rerank); `list_evidence_by_time` tool | Phase 3 |

**Backward compatibility.** Every key is optional; every existing row has `metadata = NULL` or a shape without these keys. Readers must tolerate missing keys and missing fields within each key without raising.

**Why JSON and not columns.** Classifier confidence, extractor attempts, and overflow markers are **diagnostic signals** attached to an evidence row — they are not queried on their own, they are read alongside the evidence. Promoting any of these to a column would buy indexing we don't need. The `coverage_*_ts` columns are the one exception: they support indexed time-window queries and earn their column status.

### 4.3-bis evidence_artifacts — DELETED (v2.1)

`evidence_artifacts` is **deleted** as of v2.1. It was functionally a black hole: the standalone API endpoint wrote rows to this table, but the investigation engine never read from it. The case-tied investigation flow reads exclusively from the `evidence` table.

See [deployment-schema-strategy.md §7.2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) for the full deletion scope (services, endpoints, repository methods, agent-tool Path-1 fallback).

No backward compatibility. No data migration.

### 4.3-ter standalone_evidence — DELETED (v2.1)

`standalone_evidence` is **deleted** as of v2.1 for the same reason as `evidence_artifacts`. The entire standalone evidence path is removed — `POST /api/v1/evidence`, `POST /api/v1/evidence/{id}/link`, the `EvidenceService` and `APIEvidenceArtifactService`, and six `ICaseRepository` methods (`create_standalone_evidence`, `get_standalone_evidence`, `list_standalone_evidence`, `delete_standalone_evidence`, `link_standalone_evidence_to_case`, `set_primary_evidence`).

See [deployment-schema-strategy.md §7.2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) for the complete deletion list.

### 4.4 hypotheses (High-Cardinality Table)

> **v2.1 enum and column status**:
>
> **HypothesisStatus — RESOLVED** ([strategy doc §3.3](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md)): Single enum; domain vocabulary wins. ORM `HypothesisStatusEnum` is **deleted**; ORM imports domain `HypothesisStatus` directly and binds it as `SQLEnum(HypothesisStatus, name="hypothesis_status")`. Final values: `captured | active | validated | refuted | inconclusive | retired`.
>
> **Stale columns removed from spec** ([strategy doc §18](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md)):
>
> - `test_plan`, `test_results` — not in domain model.
> - `priority` — not in domain model.
>
> **Column notes** (live ORM reality):
>
> - `rationale TEXT` — nullable in live ORM.
> - Live ORM uses `likelihood` and `initial_likelihood` columns (not `confidence`). Both are kept; `confidence` is not added.
> - `evidence_links TEXT` JSON blob (Tier 1 reality) — used instead of Tier 2 TEXT[] arrays.

```sql
CREATE TABLE hypotheses (
    hypothesis_id VARCHAR(36) PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Content
    -- ============================================================
    statement TEXT NOT NULL,
    rationale TEXT,                             -- nullable

    -- ============================================================
    -- Testing Status
    -- HypothesisStatus (domain enum, authoritative):
    -- captured | active | validated | refuted | inconclusive | retired
    -- ORM HypothesisStatusEnum deleted; ORM imports domain enum directly.
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'captured',

    -- Live ORM uses likelihood + initial_likelihood (not confidence).
    likelihood REAL,
    initial_likelihood REAL,

    -- ============================================================
    -- Evidence Links
    -- ============================================================
    -- Tier 1 reality: single JSON blob.
    -- Tier 2 (PostgreSQL-only) aspirational: TEXT[] arrays (supporting_evidence_ids, contradicting_evidence_ids).
    evidence_links TEXT,                        -- JSON blob

    -- ============================================================
    -- Metadata
    -- ============================================================
    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    tested_at TIMESTAMP WITH TIME ZONE,

    -- NOTE: test_plan, test_results, priority removed from spec — no domain backing.
    -- See deployment-schema-strategy.md §18.

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT hypotheses_status_check
        CHECK (status IN ('captured', 'active', 'validated', 'refuted', 'inconclusive', 'retired')),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT hypotheses_likelihood_check
        CHECK (likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT hypotheses_tested_timestamp_check
        CHECK (
            (status IN ('validated', 'refuted', 'inconclusive') AND tested_at IS NOT NULL) OR
            (status NOT IN ('validated', 'refuted', 'inconclusive') AND tested_at IS NULL)
        )
);

-- Tier 1 indexes (both dialects)
CREATE INDEX idx_hypotheses_case ON hypotheses(case_id);
CREATE INDEX idx_hypotheses_status ON hypotheses(case_id, status);
CREATE INDEX idx_hypotheses_proposed_at ON hypotheses(proposed_at DESC);

COMMENT ON TABLE hypotheses IS 'Investigation hypotheses - frequently filtered by status';
```

### 4.5 solutions (High-Cardinality Table)

> **v2.1 column status**:
>
> - `hypothesis_id VARCHAR(36) FK → hypotheses(hypothesis_id)` — **ADDED** (Tier 1). Per [deployment-schema-strategy.md §19](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - `SolutionStatusEnum` — **DELETED**. ORM imports domain `SolutionStatus` directly. Final values (domain wins): `proposed | in_progress | implemented | verified | rejected`. See [strategy doc §3.3](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - `impact_scope VARCHAR(1000)` — **Removed from spec**. Not in domain model. See [strategy doc §18](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).
> - `verification_plan TEXT` — **Removed from spec**. Not in domain model. Code uses `verification_result` + `verification_timestamp`.

```sql
CREATE TABLE solutions (
    solution_id VARCHAR(36) PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Added in v2.1: FK to the hypothesis this solution addresses
    hypothesis_id VARCHAR(36) REFERENCES hypotheses(hypothesis_id),

    -- ============================================================
    -- Content
    -- ============================================================
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    implementation_steps TEXT,

    -- ============================================================
    -- Status
    -- SolutionStatus (domain enum, authoritative): proposed|in_progress|implemented|verified|rejected
    -- ORM SolutionStatusEnum deleted; ORM imports domain enum directly.
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'proposed',

    -- ============================================================
    -- Risk
    -- ============================================================
    risk_level VARCHAR(10) DEFAULT 'medium',
    estimated_effort VARCHAR(20),

    -- NOTE: impact_scope and verification_plan removed from spec — no domain backing.
    -- See deployment-schema-strategy.md §18.

    -- ============================================================
    -- Verification (live ORM fields)
    -- ============================================================
    verification_result TEXT,
    verification_timestamp TIMESTAMP WITH TIME ZONE,

    -- ============================================================
    -- Metadata
    -- ============================================================
    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    implemented_at TIMESTAMP WITH TIME ZONE,

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT solutions_status_check
        CHECK (status IN ('proposed', 'in_progress', 'implemented', 'verified', 'rejected')),

    -- Tier 2 (PostgreSQL-only)
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
    -- file_id normalized to VARCHAR(36) per v2.1 FK width policy
    file_id VARCHAR(36) PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

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
    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT uploaded_files_data_type_check
        CHECK (data_type IN ('log', 'metric', 'config', 'code', 'text', 'image', 'structured', 'other')),
    -- Tier 2 (PostgreSQL-only)
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
- Uses `VARCHAR(36)` for `file_id` per v2.1 entity-id width normalization policy
- Schema exactly mirrors `UploadedFile` Pydantic model fields for zero-mapping repositories
- `content_ref` links to `Evidence.content_ref` for evidence→file traceability
- No processing status tracking (moved to separate processing pipeline if needed)

### 4.7 case_messages (High-Cardinality Table)

> **Write semantics (v2.3, 2026-04-24)**: `case_messages` is an **append-only event stream** at the domain level. `save(case)` performs additive INSERT-or-UPDATE only — no code path intentionally deletes messages, and the repository no longer runs `DELETE ... NOT IN (in_memory_ids)` as part of aggregate save. See [repository-pattern.md §4.1.1](../repository-pattern.md#411-aggregate-save-semantics) for the full rule across owned sub-collections.
>
> **Column discrepancies vs. live ORM**:
>
> - `message_id UUID PRIMARY KEY DEFAULT gen_random_uuid()` — live ORM uses `String(36)` as the primary key with no auto-generation (post-Phase 4 width normalization). UUID PK with `gen_random_uuid()` is **Tier 2 (PostgreSQL-only)** (SQLite cannot express this natively). The Tier 1 reality is a VARCHAR(36) application-generated ID.
> - `CONSTRAINT case_messages_role_check` — not present in live ORM; marking Tier 2 (PostgreSQL-only).

```sql
CREATE TABLE case_messages (
    -- Live ORM: String(36) PK (application-generated). UUID + gen_random_uuid() is
    -- Tier 2 (PostgreSQL-only) — aspirational for cloud deployment.
    message_id VARCHAR(36) PRIMARY KEY,         -- Tier 1 reality (live ORM)
    -- message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- Tier 2 (PostgreSQL-only)
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
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

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT case_messages_role_check
        CHECK (role IN ('user', 'assistant', 'system'))
);

-- Indexes
CREATE INDEX idx_case_messages_case_turn ON case_messages(case_id, turn_number);
CREATE INDEX idx_case_messages_created_at ON case_messages(created_at DESC);

COMMENT ON TABLE case_messages IS 'Turn-by-turn conversation messages (high volume)';
```

### 4.8 case_actions (Audit Table — Python alias: CaseStatusTransitionModel)

> **Column discrepancies vs. live ORM**:
>
> - `transition_id UUID PRIMARY KEY DEFAULT gen_random_uuid()` — live ORM uses `Integer autoincrement` PK. UUID PK is Tier 2 (PostgreSQL-only) aspirational.
> - `from_status VARCHAR(20) NOT NULL`, `to_status VARCHAR(20) NOT NULL` — live ORM uses `String(50)` and `from_status` is nullable.
> - `triggered_by VARCHAR(255)` — not present in live ORM.
> - `CONSTRAINT case_actions_status_check` — not present in live ORM; marking Tier 2 (PostgreSQL-only).

```sql
-- The live table is case_actions. A Python-level alias (CaseStatusTransitionModel)
-- points at this table for back-compat; there is no separate case_status_transitions
-- table in the database.
CREATE TABLE case_actions (
    -- Live ORM: Integer autoincrement PK.
    -- UUID PK with gen_random_uuid() is Tier 2 (PostgreSQL-only) — aspirational.
    transition_id INTEGER PRIMARY KEY,          -- Tier 1 reality (live ORM)
    -- transition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- Tier 2 (PostgreSQL-only)
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Transition Data
    -- ============================================================
    from_status VARCHAR(50),                    -- nullable in live ORM; String(50)
    to_status VARCHAR(50) NOT NULL,             -- String(50) in live ORM
    reason VARCHAR(500),
    -- triggered_by not present in live ORM (Proposed)
    -- triggered_by VARCHAR(255),

    -- ============================================================
    -- Metadata
    -- ============================================================
    transitioned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Tier 2 (PostgreSQL-only)
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
    checkpoint_id VARCHAR(36) PRIMARY KEY,      -- Format: {case_id}:turn:{turn_number} (Phase 4 normalized 50→36)
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
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
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

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
    -- generated_by: added in v2.1 (strategy doc §19). VARCHAR(36) to match FK width policy.
    generated_by VARCHAR(36),                      -- user_id who triggered generation

    -- ============================================================
    -- Runbook-Specific Metadata (JSONB for flexibility)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,            -- RunbookMetadata: source, domain, tags, etc.

    -- ============================================================
    -- Timestamps
    -- ============================================================
    generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT reports_type_check
        CHECK (report_type IN ('resolution_summary', 'closure_summary', 'runbook')),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT reports_status_check
        CHECK (generation_status IN ('generating', 'completed', 'failed')),

    -- Tier 2 (PostgreSQL-only)
    CONSTRAINT reports_format_check
        CHECK (format IN ('markdown')),

    CONSTRAINT reports_version_check
        CHECK (version >= 1 AND version <= 5),

    CONSTRAINT reports_gen_time_check
        CHECK (generation_time_ms >= 0 AND generation_time_ms <= 120000)
);

-- Note: generated_by is confirmed in spec (v2.1, strategy doc §19). VARCHAR(36) per FK width policy.
-- Note: live ORM ReportModel has only reports_version_check and reports_gen_time_check;
-- reports_type_check, reports_status_check, reports_format_check are Tier 2 (PostgreSQL-only).

-- Tier 1 index (both dialects — exists in live ORM)
CREATE INDEX idx_reports_type_version ON reports(case_id, report_type);

-- Tier 2 (PostgreSQL-only) — partial unique index
CREATE UNIQUE INDEX idx_reports_current_unique
    ON reports(case_id, report_type)
    WHERE is_current = TRUE;

-- Tier 2 (PostgreSQL-only) — partial index
CREATE INDEX idx_reports_case ON reports(case_id);
CREATE INDEX idx_reports_closure ON reports(case_id) WHERE linked_to_closure = TRUE;
CREATE INDEX idx_reports_generated_at ON reports(generated_at DESC);

-- Tier 2 (PostgreSQL-only) — GIN tsvector full-text search index
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

### 4.11 Agent Execution Cascade Tables

The investigation engine records its runtime activity in a four-level cascade:

```text
Case → investigation_sessions → agent_executions → agent_tool_calls
```

All four levels delete by CASCADE from `cases`. Note: `investigation_sessions` is an agent-execution session (per-investigation context), **not** an auth session. Auth sessions live in Redis. See [deployment-schema-strategy.md §11.2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

#### investigation_sessions

Top of the cascade. One session groups all agent executions that occur within a single user-initiated investigation run. This table is case-owned (lives under the case module), not auth-owned. See [strategy doc §11.2](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

**When written**: Created when the user submits a turn and the milestone engine starts processing. A session spans multiple agent executions (one per LLM call iteration).

**Key columns** (see `models.py:1475`):

| Column | Type | Notes |
| --- | --- | --- |
| `session_id` | VARCHAR(36) PK | |
| `case_id` | VARCHAR(36) FK → cases CASCADE | |
| `user_id` | VARCHAR(36) | |
| `organization_id` | VARCHAR(36) | |
| `status` | VARCHAR(32) | `active\|paused\|completed\|abandoned` |
| `started_at` | TIMESTAMPTZ | |
| `ended_at` | TIMESTAMPTZ nullable | |
| `last_activity_at` | TIMESTAMPTZ | |
| `total_duration_ms` | INTEGER nullable | |
| `session_goal` | TEXT nullable | |
| `findings_summary` | TEXT nullable | |
| `total_token_usage` | INTEGER | |
| `total_agent_executions` | INTEGER | |
| `token_budget_limit` | INTEGER nullable | |
| `metadata` | TEXT (JSON) | stored as `session_metadata` Python attribute |

**Applicability**: Both deployments.

#### agent_executions

Per-turn agent run metadata. One execution record per LLM call within a session.

**When written**: Created at the start of each agent execution loop iteration by the milestone engine.

**Key columns** (see `models.py:1262`):

| Column | Type | Notes |
| --- | --- | --- |
| `execution_id` | VARCHAR(36) PK | |
| `case_id` | VARCHAR(36) FK → cases CASCADE | |
| `session_id` | VARCHAR(36) FK → investigation\_sessions SET NULL | nullable |
| `organization_id` | VARCHAR(36) | |
| `agent_type` | VARCHAR(64) | `investigator\|debugger\|researcher\|validator\|reporter\|custom` |
| `agent_model` | VARCHAR(128) | LLM model name |
| `status` | VARCHAR(32) | `queued\|running\|completed\|failed\|cancelled\|timeout` |
| `started_at` | TIMESTAMPTZ nullable | |
| `completed_at` | TIMESTAMPTZ nullable | |
| `execution_duration_ms` | INTEGER nullable | |
| `prompt` | TEXT nullable | Full prompt sent to LLM |
| `response` | TEXT nullable | Raw LLM response |
| `error_message` | TEXT nullable | |
| `token_usage` | TEXT (JSON) | |
| `metadata` | TEXT (JSON) | stored as `execution_metadata` Python attribute |

**Applicability**: Both deployments.

#### agent_tool_calls (canonical — renamed from agent_tool_calls_v2)

Tool-call log per agent execution. **Renamed from `agent_tool_calls_v2`** in v2.1. FK: `execution_id → agent_executions`. Records each tool invocation made by the agent during an execution.

**v2.1 change**: The prior `agent_tool_calls` v1 table is **deleted** (zero functional readers/writers in production code paths; only schema definition + unused `CaseModel.tool_calls` relationship). The v2 table is now canonical. See [deployment-schema-strategy.md §13](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

**Applicability**: Both deployments. Cross-reference `er-diagram.md` for full column list.

### 4.12 Supporting Tables

`users`, `organizations`, and related auth/RBAC tables are defined in [user-schema.md](./user-schema.md) — that is the authoritative source for their DDL. They are not redefined here.

`sessions` (SQL auth sessions table) — **DELETED in v2.1**. Auth sessions are Redis-only. See §4.1 table overview and [deployment-schema-strategy.md §11.1](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md).

---

### 4.13 case_entities (High-Cardinality Table — Phase 4)

Cross-artifact entity index for a case. One row per `(case, entity_type, entity_value, evidence)` tuple, populated by the preprocessing pipeline after each successful extraction. Makes *"which evidence in this case mentions IP 10.0.0.5?"* a single indexed lookup rather than an LLM scan across evidence summaries.

```sql
CREATE TABLE case_entities (
    case_id          VARCHAR(36)  NOT NULL REFERENCES cases(case_id)          ON DELETE CASCADE,
    entity_type      VARCHAR(20)  NOT NULL,
    entity_value     VARCHAR(255) NOT NULL,
    evidence_id      VARCHAR(36)  NOT NULL REFERENCES evidence(evidence_id)   ON DELETE CASCADE,
    mention_count    INTEGER      NOT NULL DEFAULT 1,
    in_error_context BOOLEAN      NOT NULL DEFAULT FALSE,
    first_seen_ts    TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (case_id, entity_type, entity_value, evidence_id)
);

-- Primary query path: "find evidence mentioning entity X in case C"
CREATE INDEX idx_case_entities_lookup
    ON case_entities(case_id, entity_type, entity_value);
-- Cleanup path: located by evidence_id on re-extraction / deletion
CREATE INDEX idx_case_entities_by_evidence
    ON case_entities(evidence_id);
```

**Design Notes**:

- Composite primary key makes the write path idempotent — re-extracting an evidence (Phase 1.5 reclassification, Phase 2 retry loop) upserts by the full tuple rather than appending duplicates.
- `entity_type` is a controlled vocabulary enforced at the Pydantic layer (not via `CHECK` — PostgreSQL-only constraints aren't portable to SQLite). Valid values: `ip`, `hostname`, `user`, `pid`, `port`, `service`, `path`, `device`, `metric_name`. Extending requires a design-doc edit so the retrieval paths stay in sync with what producers emit.
- `first_seen_ts` is nullable: populated from the evidence's `coverage_start_ts` (Phase 3a, §4.3) when the evidence is time-bound, else NULL for timeless content (configs, source code, short pastes).
- Both FKs cascade on delete. Case or evidence deletion sweeps registry rows without a separate cleanup job.
- Row growth is bounded by a preprocessor-side hard cap of **500 entities per (evidence, entity_type)** pair, tunable via `FAULTMAVEN_ENTITY_REGISTRY_CAP`. Worst case per case: `evidence_count × 500 × |entity_type vocabulary|` — 100 evidence × 500 × 9 = 450k rows, well within PostgreSQL comfort.
- Overflow is recorded on `evidence.metadata.entities.overflow_types` (list of type values) and increments the `faultmaven_case_entities_overflow_total{entity_type}` counter. Exit-criteria dashboard triggers a cap review if any type overflows on >20% of evidence.
- **Shipped dark** behind `FAULTMAVEN_ENTITY_REGISTRY` (default False). Flag controls both the producer (preprocessor writes) and the consumer (agent tools + context-builder highlights). The table stays in schema regardless of flag state.

**Write path**: `PreprocessingService._build_result` → `InvestigationService._preprocess_attachment` → `CaseRepository.upsert_case_entities(case_id, evidence_id, entities)`. Semantics: delete-then-insert scoped to `(case_id, evidence_id)`. Empty list clears without inserting.

**Read path**: `CaseRepository.find_entity(case_id, entity_value, entity_type?)` and `list_top_entities(case_id, entity_type, limit)`. Exposed to the agent via `find_entity` and `list_top_entities` tools; also pre-fetched by the milestone engine and injected as an `<entity_highlights>` block in the INVESTIGATING template.

**Applicability**: Both deployments. Full design + extractor contribution matrix: [entity-registry.md](../../data-processing/entity-registry.md).

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

**Implementation Pattern (v2.1)**:

> **v2.1 design change**: The v2.0-era pattern of placing `organization_id` only on `cases` and filtering child tables via JOIN is superseded. Per [deployment-schema-strategy.md §10](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md), all tenanted tables carry `organization_id NOT NULL` to support PostgreSQL Row-Level Security (RLS). The repository layer filters by `organization_id` directly; RLS provides defense-in-depth. Note: `sessions` and `standalone_evidence` referenced below are deleted in v2.1.

```sql
-- Repository layer: direct filter on organization_id (Tier 1, both dialects)
SELECT e.* FROM evidence e
WHERE e.case_id = :case_id
  AND e.organization_id = :organization_id;

-- PostgreSQL RLS also enforces this via SET LOCAL app.current_org_id (Tier 2, cloud only)
```

**Tables WITH organization_id NOT NULL FK** (all tenanted tables, v2.1):

- ✅ `cases` — top-level tenant anchor
- ✅ `evidence` — carries `organization_id` for RLS (v2.1 addition)
- ✅ `hypotheses` — carries `organization_id` for RLS (if present in live ORM; confirm on migration)
- ✅ `solutions` — same
- ✅ `uploaded_files` — same
- ✅ `case_messages` — same
- ✅ `case_actions` — same
- ✅ `case_checkpoints` — same
- ✅ `reports` — same
- ✅ `investigation_sessions` — carries `organization_id`
- ✅ `agent_executions` — carries `organization_id`
- ✅ `agent_tool_calls` — carries `organization_id` (confirm on migration)
- ✅ `knowledge_items` — carries `organization_id`

**Deleted tables** (no longer relevant):

- `sessions` — DELETED in v2.1
- `standalone_evidence` — DELETED in v2.1

**Performance**:

```sql
-- Composite indexes on frequently filtered columns:
CREATE INDEX idx_cases_org_id_case_id ON cases(organization_id, case_id);
CREATE INDEX idx_cases_org_status ON cases(organization_id, status);
CREATE INDEX idx_evidence_case ON evidence(case_id);
CREATE INDEX idx_evidence_org ON evidence(organization_id, case_id);
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
WHERE to_tsvector('english', title) @@ to_tsquery('api performance')
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

### 7.3 JSONB Field Concurrency

> **Decision (v2.1 locked)**: Optimistic locking (`version` column + retry/backoff) and the formal JSONB-merge operator (`||`) concurrency scheme described in the prior options below are **stripped from the design**. See [deployment-schema-strategy.md §5](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md). Realistic contention is narrow. The supported pattern is **scoped field-merge** in repository methods (`update_progress`, `update_working_conclusion`) — read-modify-write inside one transaction with row-level locking (SQLAlchemy session locking on SQLite; SELECT FOR UPDATE equivalent on PostgreSQL). Last-write-wins is the design until evidence shows otherwise.
>
> The options below are retained for historical reference only — they document what was considered and why not implemented.

**Remaining JSONB fields** in `cases` table:

- `inquiry` — set once at creation, immutable
- `problem_verification` — set once per milestone
- `working_conclusion` — updated during investigation
- `root_cause_conclusion` — set once at resolution
- `path_selection` — set once
- `progress` — updated frequently (contains investigation journal)
- `documentation` — updated occasionally

**Historical options considered (not implemented)**:

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

> **Decision (v2.1 locked)**: Soft delete (`deleted_at`, `soft_delete/restore/purge_expired`, 90-day window) is **stripped from the design**. See [deployment-schema-strategy.md §5](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md). No compliance requirement drives soft delete. The supported preservation pattern is `is_archived` + `archived_at` (already in the domain model and schema). The extensive soft-delete implementation guide below is retained for historical reference only.
>
> **Supported preservation pattern**: Set `is_archived = true` and `archived_at = NOW()`. Cases keep all child data (evidence, hypotheses, messages, reports) through RESOLVED, CLOSED, and archived states. Case vector cleanup is scheduled (6h orphan sweep) + on-delete trigger. See [case-evidence-store.md](../../case-and-session/case-evidence-store.md).

**Current live implementation**: Hard delete with CASCADE.

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
-- - All investigation_sessions (ON DELETE CASCADE)
-- - All agent_executions (ON DELETE CASCADE)
-- - All agent_tool_calls (ON DELETE CASCADE)
```

---

## 7.5 Row-Level Security (PostgreSQL, Cloud — Tier 2)

All tenanted case-domain tables get RLS policies in PostgreSQL deployments per [deployment-schema-strategy.md §10](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md). SQLite (Local Deployment) has no equivalent — tenant isolation continues to be enforced at the repository layer.

**Tenanted case-domain tables** covered by RLS:

`cases`, `case_messages`, `case_actions`, `case_tags`, `case_checkpoints`, `evidence`, `hypotheses`, `solutions`, `uploaded_files`, `investigation_sessions`, `agent_executions`, `agent_tool_calls`, `reports`, `conversion_jobs`, `conversion_drafts`

**Policy pattern (Tier 2 — PostgreSQL-only)**:

```sql
ALTER TABLE cases ENABLE ROW LEVEL SECURITY;

CREATE POLICY cases_tenant_isolation ON cases
    USING (organization_id = current_setting('app.current_org_id', true));

-- Repeat for each tenanted table above.
```

**Request middleware**: Every authenticated request sets the per-connection tenant context before any tenanted query. Planned in `faultmaven/api/middleware/tenant_isolation.py`:

```python
async def set_tenant_context(session: AsyncSession, organization_id: str):
    await session.execute(
        text("SET LOCAL app.current_org_id = :org_id"),
        {"org_id": organization_id},
    )
```

**Test requirement**: A test must demonstrate that an `AsyncSession` with `app.current_org_id` unset returns zero rows from any tenanted table. This proves RLS is enforced, not just advisory.

---

## 8. Testing Requirements

Before deploying PostgreSQLHybridCaseRepository to production, validate the following:

### 8.1 Schema Validation

```bash
# Deploy PostgreSQL to K8s (if not already running)
kubectl apply -f faultmaven-k8s-infra/applications/postgresql/

# Apply migrations via alembic (see alembic/versions/20260317_1919_424078e5aa04_001_clean_baseline.py)
alembic upgrade head

# Verify all tables created
psql -U faultmaven -d faultmaven_cases -c "\dt"
# Expected case-domain tables: cases, evidence, hypotheses, solutions,
# uploaded_files, case_messages, case_actions, case_checkpoints, reports,
# investigation_sessions, agent_executions, agent_tool_calls
# (sessions deleted; see er-diagram.md for the full 29-table enumeration across all domains).

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
# Start API with database config
CASE_STORAGE_TYPE=database python -m faultmaven.main

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
- [x] Baseline migration: `alembic/versions/20260317_1919_424078e5aa04_001_clean_baseline.py`
- [x] Reports migration: `alembic/versions/20260329_1200_add_reports_table.py` (TD-001)
- [x] Repository implementation (`postgresql_hybrid_case_repository.py`)
- [x] Container.py wiring (`CASE_STORAGE_TYPE=database`)

### ⏳ Pending (Before Production)

- [ ] Deploy PostgreSQL to K8s cluster (if not running)
- [ ] Apply alembic migrations (`alembic upgrade head`)
- [ ] Run integration tests (Section 8.2)
- [ ] Run performance benchmarks (Section 8.3)
- [ ] Run API integration tests (Section 8.4)
- [ ] Verify all indexes are used (EXPLAIN ANALYZE)
- [ ] Update `.env` to use `CASE_STORAGE_TYPE=database`
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
- **Last Updated**: 2026-04-19
- **Version**: 3.7 (Authoritative)
- **Status**: ✅ Implemented — live schema (baseline migration `424078e5aa04`)

**Changelog**:

| Version | Date | Changes |
| --- | --- | --- |
| 3.7 | 2026-04-19 | Audit fix (storage redesign Phase 9): §4.2 Tier-2 index DDL annotated. `idx_cases_stuck` — flagged as deferred/unsatisfiable as written because `turns_without_progress` is not a first-class column (it lives in the `progress` JSONB blob); provided corrected JSONB expression index form feasible post-Phase 7. `idx_cases_path` and `idx_cases_urgency` — confirmed syntactically valid for JSONB columns post-Phase 7 (the `->>` operator works on JSONB); annotated as deferred (no migration created them yet) and marked with a TODO for a follow-up migration. |
| 3.6 | 2026-04-19 | Aligned with deployment-schema-strategy.md v2.1 (locked design). Single-table evidence (reverted v2.0 consolidation) — §4.3 rewritten with column renames (`source_type_new`→`source_type`, `file_size`→`content_size_bytes`, `upload_timestamp`→`collected_at`, ORM `category`→`primary_purpose`) and new `form` column for `EvidenceForm` enum. evidence\_artifacts (§4.3-bis) and standalone\_evidence (§4.3-ter) marked DELETED — standalone path removed entirely. `cases.session_id` DROPPED ENTIRELY — Anti-Pattern 1 (case-session binding). `cases.degraded_mode` DELETED. `cases.description` removed from spec (no domain backing). `agent_tool_calls` v1 DELETED; `agent_tool_calls_v2` renamed to canonical `agent_tool_calls` (§4.11). `sessions` SQL table DELETED — auth sessions are Redis-only. `HypothesisStatus` enum unified to domain values (ORM `HypothesisStatusEnum` deleted). `SolutionStatus` enum unified. `solutions.hypothesis_id` FK added. `solutions.impact_scope` and `verification_plan` removed from spec. `hypotheses.test_plan`, `test_results`, `priority` removed from spec. `reports.generated_by VARCHAR(36)` confirmed. Aspirational §7.3 JSONB concurrency stripped; §7.4 soft delete stripped (replaced by `is_archived`+`archived_at` preservation pattern note). RLS section added (§7.5). All id column widths normalized to VARCHAR(36). §5.4 multi-tenancy section updated for v2.1 `organization_id` on all tenanted tables. Table count updated to 29. |
| 3.5 | 2026-04-18 | Aligned with deployment-schema-strategy.md v1.0. Added Deployment Applicability banner. Marked all CHECK constraints and GIN/partial/expression indexes as Tier 2 (PostgreSQL-only). Documented previously-undocumented tables: evidence\_artifacts (§4.3-bis), standalone\_evidence (§4.3-ter), investigation\_sessions, agent\_executions, agent\_tool\_calls\_v2, agent\_tool\_calls deprecated (§4.11). Called out pending HypothesisStatus enum migration (§4.4), EvidenceCategory naming collision and EvidenceFormEnum rename (§4.3), SolutionStatus enum reconciliation (§4.5), evidence.source\_type\_new pending rename (§4.3), case\_messages UUID PK aspirational (§4.7), case\_actions Integer PK reality (§4.8). Flagged ORM-only cases columns (degraded\_mode, team\_id, session\_id). Marked §7.3 and §7.4 as aspirational (not implemented). |
| 3.4 | 2026-04-18 | Previous version. |

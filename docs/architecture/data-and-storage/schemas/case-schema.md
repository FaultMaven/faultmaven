# FaultMaven Case Storage Design - Performant Production Standard

**Version**: 4.1
**Status**: Authoritative Standard
**Last Updated**: 2026-05-10

> **Scope**: This document reflects the live schema as of migration `c5d6e7f8a9b0` (033, current head). All DDL below matches the SQLAlchemy ORM models in `faultmaven/infrastructure/persistence/models.py`. When this doc disagrees with the ORM, the ORM is the source of truth.

> **NOTE on `organization_id` placement**: All tenanted case-domain tables carry `organization_id NOT NULL FK` for RLS policy enforcement in PostgreSQL and direct repository-layer filtering in both dialects. The per-table DDL below reflects this placement on every tenanted table.

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

**Current State** (as of 2026-05-11):

| Component | Status | Location |
| --- | --- | --- |
| ✅ Design | Approved | This document |
| ✅ ORM Models | Complete | `faultmaven/infrastructure/persistence/models.py` (32 tables) |
| ✅ Migration Chain | Complete | `alembic/versions/` — head revision `c5d6e7f8a9b0` (033) |
| ✅ PostgreSQL Repository | Complete | `postgresql_hybrid_case_repository.py` |
| ✅ SQLite Repository | Complete | `sqlite_case_repository.py` |
| ✅ SQLite Integration Tests | Complete | Tests passing with real SQLite database |
| ⏳ PostgreSQL Tests | Pending | Not yet run against real PostgreSQL |
| ⏳ Performance Validation | Pending | Benchmarks needed |
| ⏳ Production Deploy | Pending | PostgreSQL not yet deployed to K8s |

**Migration Chain** (linear; current head is `c5d6e7f8a9b0`):

| # | Revision | Description |
| --- | --- | --- |
| 001 | `c4689af8aa3f` | Clean baseline — creates all 32 tables and RBAC seed data |
| 002 | `00eab5e0d387` | `evidence`: restore `summary`, rename `content` → `extract` (nullable for Path 2) |
| 003 | `a3957258f451` | `users.enterprise_id` and `organizations.enterprise_id` relaxed to nullable (transitional) |
| 004 | `f7bbadb43e4c` | `uploaded_files`: drop `preprocessing_summary` column |
| 005 | `24a5adc58c77` | `cases`: relax description CHECK to `status IN ('inquiry','closed') OR LENGTH(TRIM(description)) > 0` |
| 006 | `be112b702fd4` | Enterprise tier bootstrap: seed default enterprise, backfill, tighten `enterprise_id` to NOT NULL |
| 007 | `05b6eaf5baad` | Drop `users_password_or_sso` CHECK constraint to permit passwordless dev-login |
| 008 | `317a8c329673` | `case_actions`: add `triggered_by VARCHAR(50) NOT NULL` (drop existing rows, no backfill) |
| 009 | `4b7e2f9d3a18` | Evidence/Solution coherence: `evidence` adds `primary_purpose`, `analysis`, `processing_mode`, `advances_milestones`, `collected_by`; `solutions` drops dead `created_by`/`updated_by`, renames `implemented_at`→`applied_at` and `verification_timestamp`→`verified_at`, adds `proposed_by`, `applied_by`, `verification_method`, `verification_evidence_id` (FK), `effectiveness` |
| 010 | `0b5e8c4f7d29` | Strict evidence-model redesign: collapse the dual evidence-creation paths. `uploaded_files` adds `summary`, `structural_index`, `data_type`, `coverage_start_ts`, `coverage_end_ts` (preprocessing artifacts move here from the auto-DOCUMENT Evidence rows). `evidence` drops `form` column and adds `evidence_source_invariant` CHECK: `source_file_id IS NOT NULL OR source_type = 'user_description'` — every Evidence row has a known source. All existing evidence rows are dropped (pre-production; their `extract` carried structural-index dumps incompatible with the new claim-anchored semantics). Pydantic ``EvidenceCategory`` collapses to 4 values (drops `CONTEXTUAL_EVIDENCE` and `REJECTED`); ``EvidenceSourceType`` gains `USER_DESCRIPTION` (the chat-quote case). |
| 011–014 | … | (rows omitted — see alembic/versions for evidence-needs and related migrations) |
| 015 | `f015a7b2c3d4` | Rename case-lifecycle `status`→`state`: `cases`/`hypotheses`/`solutions`/`evidence_needs`/`investigation_sessions` columns + CHECK constraints + indexes; `case_actions.from_status`/`to_status`→`from_state`/`to_state`. Projection columns (`agent_executions.status`, `reports.generation_status`, etc.) intentionally keep `status`. |
| 016 | `0a1b2c3d4e5f` | Investigation-flow redesign: drop the `cases.path_selection` column (the `InvestigationPath` / `PathSelection` fork is removed). The unified opportunistic flow stores the new assessment variables (`cause_state`, `solution_state`, `solution_feasible`) and the `stabilization` record inside the existing `progress` JSON — no new columns. |
| 017–021 | … | (rows omitted — see alembic/versions: config-override source/category (017), RLS tenant isolation (018), causal-graph chain model (019), node-evidence provenance (020), evidence-need obtainability (021)) |
| 022 | `d3e4f5a6b7c8` | PostgreSQL type-divergence fixes (forward ALTERs; PG-only, no-op on SQLite): `uploaded_files.coverage_start_ts`/`coverage_end_ts` → `TIMESTAMPTZ` (were naive `TIMESTAMP` from 010; the model is `DateTime(timezone=True)` and the app binds tz-aware datetimes, which asyncpg's naive codec rejected); `evidence.advances_milestones` → `VARCHAR(50)[]` (was `TEXT` from 009; the model's `TagsArray` binds a Python list on PG). Both were invisible on SQLite (loosely typed) and 500'd only on real PostgreSQL. |
| 023 | `f5a6b7c8d9e0` | Enrol the causal-graph tables (`causal_nodes`, `causal_edges`, `causal_node_evidence`) in RLS tenant isolation — they carry `organization_id` but were added after migration 018 and never enrolled. Applies the identical `<table>_tenant_isolation` policy. PostgreSQL-only. |
| 024 | `e6f7a8b9c0d1` | Drop `causal_node_evidence.provenance` and its value CHECK (`causal_node_evidence_provenance_check`, from migration 020). The column served the retired runbook-cause-matcher grounding arm (#658); node grounding now reads the backing datum's `CAUSAL_EVIDENCE` category only. |
| 025 | `a7b8c9d0e1f2` | `users.account_kind` + derived `cases.source` (ADR-012 two-account model). |
| 026 | `b8c9d0e1f2a3` | Rename the `enterprise` plan-tier collision to `business` (Wave 2/U5). |
| 027 | `c9d0e1f2a3b4` | Drop the orphaned `organization` KB visibility scope from `knowledge_items`/`conversion_jobs` scope CHECKs (canonical 3-tier `personal`/`team`/`global`, ADR-013). |
| 028 | `d0e1f2a3b4c5` | **Polymorphic `resource_shares` table** (`resource_type, resource_id, scope_type, scope_id`; `organization_id` denormalized + RLS `resource_shares_tenant_isolation` policy, PG-only) replacing the nullable `team_id` columns on `cases`/`knowledge_items`/`conversion_jobs` (dropped). Team visibility is now a share row + SQL-resolved visible-id allowlist (ADR-013 §D4 / ADR-011 D3). No backfill — the columns never had a live writer. v1 `scope_type=team`; `organization` reserved (D4a). |
| 029–032 | … | (rows omitted — see alembic/versions: RBAC role/permission seed (029), `team_members` RLS subquery policy (030), drop the never-written `oauth_revoked_tokens` table (031, #767), `user_audit_log` `success`/`session_id` for the SSO JIT audit trail (032, ADR-015)) |
| 033 | `c5d6e7f8a9b0` | **Global-KB platform tier (#770)**: `knowledge_items.organization_id` becomes nullable with CHECK `knowledge_items_global_org_check` — `(scope='global') ⟺ (organization_id IS NULL)`, making a tenant-org-owned global row unrepresentable; existing global rows normalized to NULL. The single FOR ALL RLS policy is replaced by four per-command policies (PG-only): `FOR SELECT` grants every tenant the `scope='global'` read exemption; INSERT/UPDATE/DELETE keep the own-org arm and allow global writes only from single-tenant sentinel sessions (`app.current_org_id` = standalone org — the bootstrap/admin path in standalone and cloud+single), never from tenant-bound sessions. Under multi, global content is seeded exclusively by the audited `kb_seed` maintenance job (BYPASSRLS role). |
| 034–036 | … | (rows omitted — see alembic/versions: conversion live-case uniqueness (034), durable append-only `operator_access_audit` (035), `operator_access_grants` break-glass over Cloud tenant case content (036)) |
| 037 | `a9b0c1d2e3f4` | **Per-turn authorship**: `case_messages.author_id VARCHAR(36) NULL` (ADR-013 §D4 as amended / ADR-011 D5). The domain model and both API schemas already carried the field and the service already stamped it; only the table and the SQL writers were missing it, so every turn was persisted unattributed. Team sharing made that a real loss — the shared-case read gate admits team members to the write endpoints, so the case owner is no longer the only possible author. Deliberately **not** a foreign key (matching `operator_access_audit.operator_user_id`, not `cases.user_id`): attribution must outlive the account it describes, and `ON DELETE SET NULL` would erase precisely the record ADR-011 D5 calls un-backfillable. No backfill — a pre-existing row's author is genuinely unknown. Both upserts `COALESCE(case_messages.author_id, EXCLUDED.author_id)`, so authorship is write-once but still fillable. |
| 038 | `b0c1d2e3f4a5` | **SSO organization mapping (#869)**: new table `sso_org_mappings` — `provider` + `provider_org_id` (PK) → `organization_id` (FK → `organizations`, CASCADE), plus `UNIQUE (provider, organization_id)` so the relation is 1:1 per provider. Resolves an IdP organization to the FaultMaven tenant a multi-tenant SSO login lands in. Deliberately **not** enrolled in the migration-018 tenant-isolation policy: the SSO callback that reads it is unauthenticated, so no tenant is bound — binding one is what this lookup decides — and under RLS every tenanted table is invisible there. A row carries only an identifier equivalence, no tenant data. Rejected alternative: mapping columns on `organizations` (unreadable pre-bind; would drag an owner-role read into the auth path). See `docs/architecture/security/sso-org-mapping.md`. |
| 039 | `d2e3f4a5b6c7` | **OAuth-PKCE tenant carry (#872)**: `oauth_authorization_codes.organization_id VARCHAR(36) NULL`. The copilot's token exchange (`POST /auth/oauth/token`) is unauthenticated by construction — it presents a code and a PKCE verifier, not a bearer token — and the `users` row it mints from carries no organization, because tenancy lives in the token chain rather than the user table. The authorization code is therefore the only carrier between the *authenticated* authorize request and the *unauthenticated* exchange; without it every copilot session under `TENANT_PROVIDER=multi` minted an empty claim and was refused at `bind_request_org_context`, the same shape #869 fixed for the SSO leg and #873 for the OAuth refresh leg. **Nullable** — a code predating the column carries none, and an absent value must stay representable so the exchange mints an unusable claim rather than a guessed one; no backfill, these rows expire in ten minutes. Under single-tenant the column holds the Standalone sentinel, which `resolve_organization_claim` would supply regardless. Deliberately **not** a foreign key and **not** enrolled in the migration-018 tenant-isolation policy, for migration 038's reason: the only reader runs before a tenant is bound, so a policy would hide the row from it. Only the Redis and in-memory repositories are wired; the column keeps the ORM implementation of `IOAuthCodeRepository` able to honour the same contract. |

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

**File**: `faultmaven/modules/case/infrastructure/case_repository.py`

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
    user_id: Optional[str]          # FK to users (SET NULL on user delete)
    organization_id: str            # FK to organizations (CASCADE)
    team_id: Optional[str]          # FK to teams (SET NULL)
    title: str                      # Max 200 chars
    description: str = ""           # Confirmed problem statement; required for INVESTIGATING/RESOLVED

    # ============================================================
    # Status & Lifecycle
    # ============================================================
    state: CaseState              # inquiry | investigating | resolved | closed
    # action_history (CaseAction) is persisted to the case_actions table.
    closure_reason: Optional[str]

    # ============================================================
    # Investigation State (first-class columns — drive milestone engine)
    # ============================================================
    investigation_strategy: Optional[str]  # Free-form strategy text
    current_turn: int = 0
    turns_without_progress: int = 0
    version: int = 1                # Optimistic concurrency control

    # ============================================================
    # Investigation Data (HIGH CARDINALITY - Separate Storage)
    # ============================================================
    evidence: List[Evidence]        # PostgreSQL: separate table
    hypotheses: Dict[str, Hypothesis]  # PostgreSQL: separate table
    solutions: List[Solution]       # PostgreSQL: separate table
    uploaded_files: List[UploadedFile]  # PostgreSQL: separate table

    # ============================================================
    # Context Data (LOW CARDINALITY - JSONB blobs on `cases`)
    # ============================================================
    inquiry: InquiryData                                  # JSONB NOT NULL
    problem_verification: Optional[ProblemVerification]   # JSONB nullable
    working_conclusion: Optional[WorkingConclusion]       # JSONB nullable
    root_cause_conclusion: Optional[RootCauseConclusion]  # JSONB nullable
    # path_selection REMOVED (migration 016) — the InvestigationPath fork is gone.
    # The unified flow's assessment vars + stabilization record live in `progress`.
    escalation_state: Optional[EscalationState]           # JSONB nullable
    documentation: DocumentationData                      # JSONB NOT NULL
    progress: InvestigationProgress                       # JSONB NOT NULL
    metadata: dict                                        # JSONB NOT NULL (column name: "metadata")

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime
    updated_at: datetime
    last_activity_at: Optional[datetime]
    resolved_at: Optional[datetime]   # Required when status == resolved
    closed_at: Optional[datetime]     # Required when status == closed
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

```text
Core Table (1):
└── cases              -- Main case data + JSONB for low-cardinality items

High-Cardinality Tables (10):
├── evidence              -- Investigation evidence, single table, case_id NOT NULL FK
├── hypotheses            -- Hypotheses being tested (many per case)
├── hypothesis_evidence   -- Junction: hypothesis ↔ evidence with relationship qualifier
├── solutions             -- Proposed/verified solutions (few per case)
├── uploaded_files        -- File metadata (many per case)
├── case_messages         -- Turn-by-turn messages (very high volume)
├── case_actions          -- Audit trail of phase transitions
├── case_tags             -- Free-form tags on a case
├── case_checkpoints      -- State snapshots (one per turn)
├── case_entities         -- Cross-artifact entity index
└── reports               -- Generated case-summary documents (resolution/closure)

Agent Execution Cascade (3):
├── investigation_sessions  -- Per-investigation agent context (case-owned)
├── agent_executions        -- Per-turn agent run metadata
└── agent_tool_calls        -- Tool-call log
```

The full live table count across user + case + knowledge + config domains is **37** — see `er-diagram.md` for the authoritative enumeration.

Tables historically present and removed by the redesign (`evidence_artifacts`, `standalone_evidence`, `agent_tool_calls` v1, `sessions`) are documented in the appendix at the end of this file.

### 4.2 cases (Main Table)

The `cases` table is the aggregate root of the case domain. It carries first-class columns for everything the milestone engine queries on (status, turn counters, timestamps) and JSONB blobs for the rich domain objects retrieved together with the case (inquiry, conclusions, progress, documentation).

**Description CHECK semantics** (migration 005, `cases_description_required_for_investigation`):

```text
state IN ('inquiry', 'closed') OR LENGTH(TRIM(description)) > 0
```

`description` is the confirmed problem statement. INVESTIGATING and RESOLVED both require it (entry gate / resolved-cases-must-have-a-known-problem invariant). INQUIRY allows empty (still being formulated). CLOSED allows empty because the inquiry → closed early-abandon path is legitimate. The Pydantic Case model mirrors this rule for INVESTIGATING and RESOLVED.

```sql
CREATE TABLE cases (
    -- ============================================================
    -- Identity
    -- ============================================================
    case_id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    -- team_id column DROPPED in migration 028 (d0e1f2a3b4c5). Team visibility is
    -- now carried by the polymorphic `resource_shares` table, not a column here.
    user_id VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL DEFAULT '',

    -- ============================================================
    -- Status & Lifecycle
    -- ============================================================
    state VARCHAR(50) NOT NULL DEFAULT 'inquiry',
    -- Case origin (ADR-012 two-account model, migration 025). Python attr: source.
    source VARCHAR(20) NOT NULL DEFAULT 'copilot',
    closure_reason VARCHAR(100),
    -- Per-disposition eligibility view, maintained at the repository save chokepoint
    -- (migration 013). Nullable JSON-in-TEXT.
    disposition_eligibility TEXT,

    -- ============================================================
    -- Investigation State (first-class — drive milestone engine logic)
    -- ============================================================
    investigation_strategy TEXT,
    current_turn INTEGER NOT NULL DEFAULT 0,
    turns_without_progress INTEGER NOT NULL DEFAULT 0,

    -- ============================================================
    -- Optimistic Concurrency
    -- ============================================================
    version INTEGER NOT NULL DEFAULT 1,

    -- ============================================================
    -- Timestamps
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMP WITH TIME ZONE,
    resolved_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,

    -- ============================================================
    -- Low-Cardinality Complex Data (JSONB on PG, TEXT on SQLite)
    -- ============================================================
    inquiry JSONB NOT NULL DEFAULT '{}'::jsonb,
    problem_verification JSONB,
    working_conclusion JSONB,
    root_cause_conclusion JSONB,
    -- path_selection JSONB column DROPPED in migration 016 (0a1b2c3d4e5f)
    escalation_state JSONB,
    documentation JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,  -- Python attr: case_metadata

    -- ============================================================
    -- Constraints (live in ORM; Tier 1, both dialects)
    -- ============================================================
    CONSTRAINT cases_title_not_empty
        CHECK (LENGTH(TRIM(title)) > 0),
    CONSTRAINT cases_state_check
        CHECK (state IN ('inquiry', 'investigating', 'resolved', 'closed')),
    -- Migration 005 (24a5adc58c77): description required for INVESTIGATING and RESOLVED.
    CONSTRAINT cases_description_required_for_investigation
        CHECK (state IN ('inquiry', 'closed') OR LENGTH(TRIM(description)) > 0),
    CONSTRAINT cases_current_turn_nonnegative
        CHECK (current_turn >= 0),
    CONSTRAINT cases_turns_without_progress_nonnegative
        CHECK (turns_without_progress >= 0),
    CONSTRAINT cases_version_positive
        CHECK (version >= 1)
);

-- Tier 1 indexes (both dialects)
CREATE INDEX ix_cases_organization_id ON cases(organization_id);
CREATE INDEX ix_cases_team_id ON cases(team_id);
CREATE INDEX ix_cases_user_id ON cases(user_id);
CREATE INDEX ix_cases_state ON cases(state);
CREATE INDEX ix_cases_last_activity_at ON cases(last_activity_at);
CREATE INDEX ix_cases_closed_at ON cases(closed_at);
CREATE INDEX ix_cases_created_at ON cases(created_at);

-- Tier 2 (PostgreSQL-only) — JSONB expression indexes (deferred; no migration yet)
-- (The former idx_cases_path on path_selection->>'path' is gone — column dropped in migration 016.)
-- CREATE INDEX idx_cases_urgency ON cases((problem_verification->>'urgency_level'))
--     WHERE problem_verification IS NOT NULL;

-- Tier 2 (PostgreSQL-only) — GIN tsvector full-text search index
-- CREATE INDEX idx_cases_search ON cases USING gin(
--     to_tsvector('english', title || ' ' || description)
-- );

COMMENT ON TABLE cases IS 'Root case entity with embedded low-cardinality data in JSONB';
```

**Notes**:

- The Pydantic validator on `Case` enforces the same description-non-empty rule for INVESTIGATING and RESOLVED, plus cross-field invariants (`resolved_at` requires RESOLVED; RESOLVED requires `resolved_at` + `closed_at` + `closure_reason`).
- `current_turn`, `turns_without_progress`, and `version` are first-class columns. The milestone engine reads/writes them directly without JSONB extraction.
- `metadata` is the SQL column name (Python attribute is `case_metadata` to avoid clashing with SQLAlchemy's `metadata`).

### 4.3 evidence (Single-Table Design)

#### Role of `summary` vs `extract`

Every `evidence` row carries two semantically distinct content fields. They are **not** redundant — confusing them was the design error this section exists to prevent.

| Field | Type | Required? | What it carries |
| --- | --- | --- | --- |
| `summary` | `VARCHAR(500) NOT NULL` | always | Short label for this row, written by the LLM in `evidence_to_add`. Used for UI list views, headers, and quick scanning. |
| `extract` | `TEXT NULL` | optional | Verbatim quote (the focused slice supporting the claim). May be NULL when the summary is self-contained. |

Post-010 single creation path: every Evidence row originates as an `EvidenceToAdd` entry the LLM declared during INVESTIGATING. The system fills lifecycle fields (id, timestamps, `advances_milestones` via `CATEGORY_MILESTONE_MAP`); everything else is the LLM's declaration.

| Field | Filled by | Notes |
| --- | --- | --- |
| `summary` | LLM | Always set. Short, scannable. |
| `extract` | LLM (optional) | Verbatim system-output quote. Omit when summary is self-contained. |
| `category` | LLM | One of four claim-anchored values (symptom / causal / mitigation / solution). |
| `source_type` | LLM | One of `EvidenceSourceType`; `USER_DESCRIPTION` for chat-extracted quotes. |
| `source_file_id` | LLM | FK to `uploaded_files`. Required unless `source_type=USER_DESCRIPTION` — guarded by `evidence_source_invariant` CHECK. |
| `advances_milestones` | System (LLM override) | Inferred from category + this-turn milestones; LLM may override. |

**File-level preprocessing artifacts live on `uploaded_files`.** Structural index, file summary, file-level `data_type`, and coverage timestamps describe the FILE, not any specific claim; they belong with the file row. The `evidence.extract` field is reserved for claim-relevant verbatim quotes.

**File pointer is separate.** Neither `summary` nor `extract` carries the storage location of the original raw file. That lives on `uploaded_files.storage_ref`, reachable from `evidence.source_file_id`. Chat-extracted evidence (`source_type=USER_DESCRIPTION`) has `source_file_id IS NULL`; its source is the user's chat message at `collected_at_turn`.

**CHECK constraints (mirrored at the Pydantic layer):**

```sql
CONSTRAINT evidence_summary_not_empty CHECK (LENGTH(TRIM(summary)) > 0)
CONSTRAINT evidence_extract_not_empty CHECK (extract IS NULL OR LENGTH(TRIM(extract)) > 0)
```

The Pydantic `Evidence` model has a matching `_extract_not_empty_when_set` validator. Same rule, two layers, neither bypassable independently.

**Naming caveat.** `EXTRACT` is a SQL function in PostgreSQL (`EXTRACT(YEAR FROM dt)`); using it as a column name works (PG treats it as a non-reserved keyword and SQLAlchemy quotes correctly), but raw-SQL readers should be aware that `SELECT EXTRACT(YEAR FROM created_at), extract FROM evidence` reads slightly ambiguously. The semantic accuracy across all three paths is structural; the keyword friction is documentary.

---

The single-table evidence model with `case_id NOT NULL` FK is the live shape. Every evidence row is a case-specific interpretation; there is no case-less evidence concept. File metadata lives on `uploaded_files` (linked via `source_file_id`).

```sql
CREATE TABLE evidence (
    evidence_id         VARCHAR(36) PRIMARY KEY,
    organization_id     VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    case_id             VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    source_file_id      VARCHAR(36) REFERENCES uploaded_files(file_id) ON DELETE SET NULL,

    -- Classification
    -- domain EvidenceCategory enum: symptom_evidence | causal_evidence
    --                              | symptom_absence_evidence | causal_absence_evidence
    category            VARCHAR(50) NOT NULL,
    -- domain EvidenceSourceType enum: logs | metrics | configuration
    --                                | code | text | image | user_description
    source_type         VARCHAR(50),   -- nullable in live ORM (models.py: nullable=True)

    -- Two-field content shape (see "Role of summary vs extract" above):
    summary             VARCHAR(500) NOT NULL,
    extract             TEXT,                                    -- nullable when summary is self-contained

    -- Audit / classification metadata (added in migration 009).
    -- ``primary_purpose`` is what this evidence validates (milestone
    -- name or hypothesis ID). Server default 'legacy' lets pre-009
    -- rows satisfy NOT NULL; new writers populate explicitly.
    primary_purpose     VARCHAR(100) NOT NULL DEFAULT 'legacy',
    -- Free-form agent analysis attached to the evidence row.
    analysis            TEXT,
    -- Processing mode used to extract: triage | directed_analysis | semantic_search.
    processing_mode     VARCHAR(50),
    -- Which milestones this evidence helped complete. TagsArray shape:
    -- VARCHAR(50)[] on PG, comma-encoded TEXT on SQLite (same TypeDecorator as
    -- ``tags``). PG type set by migration 022 (009 created it TEXT — a bug that
    -- rejected the bound list on real PostgreSQL).
    advances_milestones VARCHAR(50)[],
    -- Who collected: user UUID or sentinel ('system' for automated). Free-form
    -- VARCHAR per the case_actions.triggered_by precedent — the value space is
    -- heterogeneous, FK to users would force inventing sentinel users rows.
    collected_by        VARCHAR(50) NOT NULL DEFAULT 'system',

    -- Investigation context
    is_primary          BOOLEAN NOT NULL DEFAULT FALSE,
    reliability_score   REAL,                                    -- 0..1 when set
    tags                TEXT,                                    -- comma-separated on SQLite; TEXT[] on PG
    collected_at_turn   INTEGER,
    coverage_start_ts   TIMESTAMP WITH TIME ZONE,
    coverage_end_ts     TIMESTAMP WITH TIME ZONE,

    -- Lifecycle
    vectorized          BOOLEAN NOT NULL DEFAULT FALSE,

    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,      -- Python attr: evidence_metadata

    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ============================================================
    -- Constraints (live in ORM; Tier 1, both dialects)
    -- ============================================================
    -- Mirrored at the Pydantic layer in Evidence._summary_not_empty
    CONSTRAINT evidence_summary_not_empty
        CHECK (LENGTH(TRIM(summary)) > 0),
    -- Mirrored at the Pydantic layer in Evidence._extract_not_empty_when_set
    CONSTRAINT evidence_extract_not_empty
        CHECK (extract IS NULL OR LENGTH(TRIM(extract)) > 0),
    CONSTRAINT evidence_reliability_range
        CHECK (reliability_score IS NULL OR (reliability_score >= 0 AND reliability_score <= 1)),
    -- Every row has a known source: either a file or a chat-quoted system output.
    CONSTRAINT evidence_source_invariant
        CHECK (source_file_id IS NOT NULL OR source_type = 'user_description')
);

-- Tier 1 indexes (both dialects)
CREATE INDEX ix_evidence_case_id ON evidence(case_id);
CREATE INDEX ix_evidence_organization_id ON evidence(organization_id);
CREATE INDEX ix_evidence_source_file_id ON evidence(source_file_id);
CREATE INDEX ix_evidence_category ON evidence(category);
CREATE INDEX ix_evidence_collected_at_turn ON evidence(collected_at_turn);
CREATE INDEX ix_evidence_case_is_primary ON evidence(case_id, is_primary);
CREATE INDEX ix_evidence_coverage ON evidence(case_id, coverage_start_ts, coverage_end_ts);

-- Tier 2 (PostgreSQL-only) — GIN over TEXT[] tags
CREATE INDEX ix_evidence_tags ON evidence USING gin(tags);

COMMENT ON TABLE evidence IS 'Investigation evidence — single table, case_id NOT NULL FK';
```

**Notes**:

- `summary` is `NOT NULL` and always present; `extract` is nullable so the LLM can omit a verbatim quote when the summary is self-contained.
- `vectorized` flips to `TRUE` once the row is indexed into the case vector store.
- `coverage_start_ts` / `coverage_end_ts` are the queryable projection of the extractor's time range when an Evidence row inherits time coverage from its source file; the file's own time-range columns live on `uploaded_files`.
- `metadata` is the SQL column name (Python attribute is `evidence_metadata`).

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

### 4.4 hypotheses (High-Cardinality Table)

Hypotheses for root-cause analysis. Evidence linkage is a separate junction table (`hypothesis_evidence`, §4.4-bis); there is no JSON blob field.

```sql
CREATE TABLE hypotheses (
    hypothesis_id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Causal-graph chain model (migration 019). A hypothesis is a causal CHAIN:
    -- root_node_id anchors it to the graph; path is the ordered node-id chain.
    root_node_id VARCHAR(36) REFERENCES causal_nodes(node_id) ON DELETE SET NULL,
    path JSONB NOT NULL DEFAULT '[]',               -- ordered causal_nodes.node_id chain

    statement TEXT NOT NULL,
    -- HypothesisState enum: captured | active | validated | refuted | inconclusive | retired
    state VARCHAR(20) NOT NULL DEFAULT 'captured',
    likelihood NUMERIC(3, 2) DEFAULT 0.5,           -- 0..1
    initial_likelihood NUMERIC(3, 2) DEFAULT 0.5,
    category VARCHAR(50) NOT NULL,
    generation_mode VARCHAR(20) NOT NULL DEFAULT 'systematic',
    rationale TEXT,
    retirement_reason TEXT,
    refutation_reason VARCHAR(200),

    -- Turn tracking
    generated_at_turn INTEGER NOT NULL DEFAULT 0,
    last_updated_turn INTEGER NOT NULL DEFAULT 0,
    last_progress_at_turn INTEGER NOT NULL DEFAULT 0,
    iterations_without_progress INTEGER NOT NULL DEFAULT 0,

    tested_at TIMESTAMP WITH TIME ZONE,
    concluded_at TIMESTAMP WITH TIME ZONE,

    created_by VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,    -- Python attr: hypothesis_metadata

    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Mirrored at the Pydantic layer in Hypothesis._statement_not_empty
    CONSTRAINT hypotheses_statement_not_empty
        CHECK (LENGTH(TRIM(statement)) > 0),
    CONSTRAINT hypotheses_state_check
        CHECK (state IN ('captured', 'active', 'validated', 'refuted', 'inconclusive', 'retired')),
    CONSTRAINT hypotheses_likelihood_range
        CHECK (likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1))
);

CREATE INDEX ix_hypotheses_case_id ON hypotheses(case_id);
CREATE INDEX ix_hypotheses_organization_id ON hypotheses(organization_id);
CREATE INDEX ix_hypotheses_state ON hypotheses(state);
CREATE INDEX ix_hypotheses_category ON hypotheses(category);
CREATE INDEX ix_hypotheses_created_by ON hypotheses(created_by);

COMMENT ON TABLE hypotheses IS 'Investigation hypotheses - frequently filtered by status';
```

### 4.4-bis hypothesis_evidence (Junction Table)

Replaces the historical `hypotheses.evidence_links` JSON blob. Each row asserts that a specific piece of evidence either supports, refutes, or is related to a hypothesis, with an optional confidence score and turn-of-link audit trail.

```sql
CREATE TABLE hypothesis_evidence (
    hypothesis_id VARCHAR(36) NOT NULL REFERENCES hypotheses(hypothesis_id) ON DELETE CASCADE,
    evidence_id VARCHAR(36) NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    relationship_type VARCHAR(30) NOT NULL,         -- supports | refutes | related
    confidence NUMERIC(3, 2),                       -- 0..1 when set
    linked_at_turn INTEGER,
    linked_by VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    PRIMARY KEY (hypothesis_id, evidence_id),

    CONSTRAINT hypothesis_evidence_relationship_check
        CHECK (relationship_type IN ('supports', 'refutes', 'related')),
    CONSTRAINT hypothesis_evidence_confidence_range
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX ix_hypothesis_evidence_organization_id ON hypothesis_evidence(organization_id);
CREATE INDEX ix_hypothesis_evidence_evidence ON hypothesis_evidence(evidence_id);

COMMENT ON TABLE hypothesis_evidence IS 'Junction: hypothesis ↔ evidence with relationship qualifier (supports|refutes|related)';
```

### 4.5 solutions (High-Cardinality Table)

A solution may or may not link to a hypothesis (fast-track resolutions skip hypothesis formulation entirely).

```sql
CREATE TABLE solutions (
    solution_id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    hypothesis_id VARCHAR(36) REFERENCES hypotheses(hypothesis_id) ON DELETE SET NULL,

    -- Causal-graph linkage (migration 019). node_id anchors the solution to a
    -- causal_nodes row; quadrant classifies the fix type.
    node_id VARCHAR(36) REFERENCES causal_nodes(node_id) ON DELETE SET NULL,
    quadrant VARCHAR(20),                            -- remediation | defensive_fix | mitigation | loop_break

    title VARCHAR(500) NOT NULL,
    description TEXT NOT NULL,
    solution_type VARCHAR(30) NOT NULL DEFAULT 'other',
    -- SolutionState enum: proposed | accepted | rejected | implemented | verified.
    -- Repository derives this from lifecycle fields: verified_at set -> 'verified',
    -- applied_at set -> 'implemented', otherwise 'proposed'.
    state VARCHAR(20) NOT NULL DEFAULT 'proposed',
    risk_level VARCHAR(20),                          -- low | medium | high | critical when set
    estimated_effort VARCHAR(50),
    immediate_action TEXT,
    longterm_fix TEXT,
    implementation_steps JSONB,
    commands JSONB,
    risks JSONB,

    -- Sentinel-friendly actor columns (added in migration 009; replace the
    -- dropped ``created_by``/``updated_by`` FKs that the repo always wrote
    -- NULL for). Free-form VARCHAR per the case_actions.triggered_by
    -- precedent — Pydantic Solution.proposed_by defaults to 'agent', a value
    -- a FK to users could not represent.
    proposed_by VARCHAR(50) NOT NULL DEFAULT 'agent',
    applied_by VARCHAR(50),

    -- Verification metadata (added in migration 009 — completes the audit
    -- trail the Pydantic Solution model has carried without storage).
    verification_method VARCHAR(500),
    verification_evidence_id VARCHAR(36) REFERENCES evidence(evidence_id) ON DELETE SET NULL,
    effectiveness REAL,                              -- 0..1 when set
    verification_result TEXT,
    verified_at TIMESTAMP WITH TIME ZONE,            -- renamed from verification_timestamp in 009

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,     -- Python attr: solution_metadata

    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMP WITH TIME ZONE,             -- renamed from implemented_at in 009
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT solutions_description_not_empty
        CHECK (LENGTH(TRIM(description)) > 0),
    CONSTRAINT solutions_state_check
        CHECK (state IN ('proposed', 'accepted', 'rejected', 'implemented', 'verified')),
    CONSTRAINT solutions_risk_level_check
        CHECK (risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT solutions_effectiveness_range
        CHECK (effectiveness IS NULL OR (effectiveness >= 0 AND effectiveness <= 1)),
    CONSTRAINT solutions_quadrant_check
        CHECK (quadrant IS NULL OR quadrant IN ('remediation', 'defensive_fix', 'mitigation', 'loop_break'))
);

CREATE INDEX ix_solutions_case_id ON solutions(case_id);
CREATE INDEX ix_solutions_organization_id ON solutions(organization_id);
CREATE INDEX ix_solutions_hypothesis_id ON solutions(hypothesis_id);
CREATE INDEX ix_solutions_state ON solutions(state);

COMMENT ON TABLE solutions IS 'Proposed and verified solutions';
```

### 4.6 uploaded_files (High-Cardinality Table)

Stores file metadata **and the file-level preprocessing artifacts** that describe its content. The actual bytes live in the file-storage backend (local FS, S3, Azure blob), reachable via `storage_ref`. `case_id` is nullable because KB conversion uploads (`POST /knowledge/convert`) do not carry a case.

```sql
CREATE TABLE uploaded_files (
    file_id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    case_id VARCHAR(36) REFERENCES cases(case_id) ON DELETE CASCADE,
    uploaded_by VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,

    filename VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    content_type VARCHAR(100),                       -- MIME type
    content_hash VARCHAR(64),                        -- file-level dedup key

    -- Opaque key passed to the file-storage backend (local FS path, S3 key, Azure blob name).
    -- The backend interprets this; nothing else does.
    storage_ref VARCHAR(1000),

    -- Provenance: how this file got into the system.
    -- Distinct from evidence.source_type (which classifies the data shape).
    upload_source VARCHAR(50) NOT NULL DEFAULT 'file_upload',  -- file_upload | conversion_source | api_push | ...
    uploaded_at_turn INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,     -- Python attr: file_metadata

    uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Preprocessing artifacts. These describe the FILE; claim-anchored
    -- extracts live on Evidence rows that point back via source_file_id.
    summary TEXT,                                    -- short file summary
    structural_index TEXT,                           -- preprocessing-pipeline JSON blob
    data_type VARCHAR(50),                           -- logs | metrics | configuration | code | text | image
    coverage_start_ts TIMESTAMP WITH TIME ZONE,      -- earliest timestamp seen in the file
    coverage_end_ts   TIMESTAMP WITH TIME ZONE,      -- latest timestamp seen in the file

    CONSTRAINT uploaded_files_filename_not_empty
        CHECK (LENGTH(TRIM(filename)) > 0),
    CONSTRAINT uploaded_files_size_nonnegative
        CHECK (size_bytes >= 0),
    CONSTRAINT uploaded_files_turn_nonnegative
        CHECK (uploaded_at_turn >= 0)
);

CREATE INDEX ix_uploaded_files_organization_id ON uploaded_files(organization_id);
CREATE INDEX ix_uploaded_files_case_id ON uploaded_files(case_id);
CREATE INDEX ix_uploaded_files_uploaded_by ON uploaded_files(uploaded_by);
CREATE INDEX ix_uploaded_files_content_hash ON uploaded_files(content_hash);

COMMENT ON TABLE uploaded_files IS 'Raw file upload metadata + file-level preprocessing artifacts; storage backend opaque via storage_ref';
COMMENT ON COLUMN uploaded_files.upload_source IS 'Provenance (file_upload | conversion_source | api_push). Distinct from evidence.source_type which classifies data shape.';
COMMENT ON COLUMN uploaded_files.structural_index IS 'Preprocessing-pipeline JSON ({v, file_extract, search_map, file_meta}); the LLM sees this as <file_extract> inside <uploaded_file> during INQUIRY.';
```

**Design Notes**:

- `storage_ref` is opaque to the schema — only the storage backend interprets it. Renamed from the historical `content_ref` to make the role clear.
- `upload_source` is the file's *provenance*; `evidence.source_type` is the *data shape* classification. They are distinct concerns and live on distinct tables.
- `case_id` is nullable so conversion-job uploads (no case) and case-evidence uploads (with case) share the same table.
- **Preprocessing-artifact lifecycle**: written by the preprocessing pipeline at intake; surfaced to the LLM via `<uploaded_file file_id="…">` during INQUIRY and via `<evidence …>` blocks (whose `source_file_id` points back here) during INVESTIGATING. Repositories use `COALESCE(EXCLUDED.x, uploaded_files.x)` on UPDATE so a failed re-run (NULL incoming) cannot clobber a prior good extraction; intentional clearing must go through a dedicated path.

### 4.7 case_messages (High-Cardinality Table)

> **Write semantics (v2.3, 2026-04-24)**: `case_messages` is an **append-only event stream** at the domain level. `save(case)` performs additive INSERT-or-UPDATE only — no code path intentionally deletes messages, and the repository no longer runs `DELETE ... NOT IN (in_memory_ids)` as part of aggregate save. See [repository-pattern.md §4.1.1](../repository-pattern.md#411-aggregate-save-semantics) for the full rule across owned sub-collections.
>
> **Column discrepancies vs. live ORM**:
>
> - `message_id UUID PRIMARY KEY DEFAULT gen_random_uuid()` — live ORM uses `String(36)` as the primary key with no auto-generation (post-Phase 4 width normalization). UUID PK with `gen_random_uuid()` is **Tier 2 (PostgreSQL-only)** (SQLite cannot express this natively). The Tier 1 reality is a VARCHAR(36) application-generated ID.
> - `CONSTRAINT case_messages_role_check` — present in the live ORM as a **Tier 1** `CheckConstraint` (both dialects), `role IN ('user', 'assistant', 'system')`.

```sql
CREATE TABLE case_messages (
    -- Live ORM: String(36) PK (application-generated). UUID + gen_random_uuid() is
    -- Tier 2 (PostgreSQL-only) — aspirational for cloud deployment.
    message_id VARCHAR(36) PRIMARY KEY,         -- Tier 1 reality (live ORM)
    -- message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- Tier 2 (PostgreSQL-only)
    organization_id VARCHAR(36) NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,

    -- ============================================================
    -- Message Content
    -- ============================================================
    role VARCHAR(20) NOT NULL,                  -- user | assistant | system
    content TEXT NOT NULL,

    -- Who wrote this turn (migration 037, ADR-013 §D4 / ADR-011 D5).
    -- Deliberately NOT a foreign key, matching operator_access_audit rather
    -- than cases.user_id: attribution must outlive the account it describes.
    -- ON DELETE SET NULL would erase the record ADR-011 D5 calls
    -- un-backfillable; RESTRICT would make any user who ever wrote a turn
    -- undeletable. NULL means no human author — assistant and system turns,
    -- or a row written before 037.
    author_id VARCHAR(36),

    -- ============================================================
    -- Metadata
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    token_count INTEGER,

    -- ============================================================
    -- Flexible Data (JSONB)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,         -- Sources, tools used, etc.

    -- Tier 1 (live ORM; both dialects)
    CONSTRAINT case_messages_role_check
        CHECK (role IN ('user', 'assistant', 'system'))
);

-- Indexes
CREATE INDEX idx_case_messages_case_turn ON case_messages(case_id, turn_number);
CREATE INDEX idx_case_messages_created_at ON case_messages(created_at DESC);

COMMENT ON TABLE case_messages IS 'Turn-by-turn conversation messages (high volume)';
```

### 4.8 case_actions (Audit Table)

The audit trail of phase transitions on a case. Migration 008 added
`triggered_by NOT NULL` so every row records *who* drove the transition
(user UUID for human actions, sentinel like `"system"` / `"agent"` /
`"scheduler"` for automatic actions). Both repositories now hydrate
`Case.action_history` from this table on read; before migration 008 the
table was effectively write-only (`action_history` was hardcoded to
`[]` in `_to_domain`).

> **Write semantics (append-only, count-based tail insert).** `transition_id`
> is a DB-assigned autoincrement PK with no natural-key conflict target, so
> `ON CONFLICT DO NOTHING` can never fire. `save(case)` therefore persists
> **only the unpersisted tail** of `action_history` — it counts the rows
> already in `case_actions` for the case and inserts `action_history[count:]`.
> Re-inserting the full hydrated list every save (the prior behaviour) caused
> *geometric* row growth (R rows → 2R + new each save, since none conflicted),
> exploding a handful of real transitions into ~2ⁿ duplicate rows over n turns.
> Each save is now O(new), not O(history). Same fix in both the SQLite and
> PostgreSQL repositories.

```sql
CREATE TABLE case_actions (
    -- Integer autoincrement PK (Tier 1 reality on both dialects).
    transition_id INTEGER PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id)
        ON DELETE CASCADE,

    -- ============================================================
    -- Transition Data
    -- ============================================================
    from_state VARCHAR(50),                    -- nullable: NULL on case creation
    to_state VARCHAR(50) NOT NULL,
    reason TEXT,                                -- free-form prose

    -- Free-form actor identifier. Heterogeneous value space:
    --   - user UUID (human action)
    --   - "system" (auto-transitions, cleanup jobs)
    --   - "agent" (LLM-driven transitions)
    --   - "scheduler" (timed/idle transitions)
    -- Not a FK to users.user_id because the sentinel set is real and
    -- forcing a FK would mean inventing a sentinel users row (the same
    -- anti-pattern removed elsewhere). A CHECK constraint on the
    -- sentinel set may be added later once the values stabilize.
    triggered_by VARCHAR(50) NOT NULL,

    -- ============================================================
    -- Metadata
    -- ============================================================
    metadata JSONB NOT NULL DEFAULT '{}',       -- Tier 1: TEXT on SQLite, JSONB on PG
    transitioned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_case_actions_case ON case_actions(case_id);
CREATE INDEX idx_case_actions_timestamp ON case_actions(transitioned_at DESC);

COMMENT ON TABLE case_actions IS 'Audit trail of case actions and status transitions';
```

**Read path (post-008)**:

- `SQLiteCaseRepository._load_case_actions(case_id)` issues
  `SELECT … FROM case_actions WHERE case_id = ? ORDER BY transitioned_at ASC, transition_id ASC`
  and constructs `[CaseAction(...)]`. Wired into `_row_to_case` via the
  optional `actions_data` parameter (default `None` → empty list, so
  the bulk list path stays cheap and only the detail `get()` pays the
  extra round-trip).
- `PostgreSQLHybridCaseRepository._load_case_actions(case_id)` mirrors
  the SQLite helper. PG's `_row_to_case` is async and is invoked by
  both `get()` and `list()` (which fans out to `get()` per case_id), so
  it loads actions inline.

**Pydantic mapping**: `CaseAction` (`models.py:172`) carries
`triggered_by: str` as a required field. The `from_status` /
`to_status` enum coercion happens in `CaseState(row.value)` at the
boundary; `triggered_by` round-trips verbatim.

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

Case-summary documents generated at terminal state. Reusable knowledge derived from cases (runbooks) lives in `knowledge_items`, not here — the only `report_type` values are `resolution_summary` and `closure_summary`.

```sql
CREATE TABLE reports (
    report_id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    case_id VARCHAR(36) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    generated_by VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,

    -- ReportType enum: resolution_summary | closure_summary
    report_type VARCHAR(30) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    linked_to_closure BOOLEAN NOT NULL DEFAULT FALSE,

    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,                          -- Full markdown content
    -- ReportFormat enum: markdown | html
    format VARCHAR(20) NOT NULL DEFAULT 'markdown',
    -- ReportStatus enum: generating | completed | failed
    generation_status VARCHAR(20) NOT NULL,
    generation_time_ms INTEGER NOT NULL,
    metadata JSON,                                  -- Python attr: report_metadata

    generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT reports_type_check
        CHECK (report_type IN ('resolution_summary', 'closure_summary')),
    CONSTRAINT reports_format_check
        CHECK (format IN ('markdown', 'html')),
    CONSTRAINT reports_status_check
        CHECK (generation_status IN ('generating', 'completed', 'failed')),
    CONSTRAINT reports_version_check
        CHECK (version >= 1 AND version <= 5),
    CONSTRAINT reports_gen_time_check
        CHECK (generation_time_ms >= 0 AND generation_time_ms <= 120000)
);

CREATE INDEX idx_reports_type_version ON reports(case_id, report_type);

-- Tier 2 (PostgreSQL-only) — partial unique index
-- CREATE UNIQUE INDEX idx_reports_current_unique
--     ON reports(case_id, report_type)
--     WHERE is_current = TRUE;

COMMENT ON TABLE reports IS 'Auto-generated case-summary documents (resolution / closure). Cascades with case.';
```

**Design Notes**:

- `report_type` is restricted to `resolution_summary` and `closure_summary`. Runbooks are reusable knowledge and live in `knowledge_items`; per-case incident-report and post-mortem types are not part of the schema.
- The Python `ReportType` enum is deliberately **wider** than this CHECK: it also carries `runbook`, which is API/projection surface (`GET /cases/{id}/reports?report_type=runbook` projects case-linked `conversion_drafts` into the report shape) and is never a `reports` row. The storable subset is declared as `PERSISTED_REPORT_TYPES` in `modules/case/domain/owned_models/report.py`, and `tests/unit/modules/case/test_report_vocabulary.py` pins it against this CHECK and against the migration that owns it (fm#520).
- `format` admits `markdown` and `html`. Nothing writes `html` today, but the Pydantic `CaseReport.format` must keep admitting it: the hydration path builds the model straight from the row, so a narrower type turns a storable row into a 500 on read (fm#520).
- Versioning support: up to 5 versions per report_type per case (`reports_version_check`).
- `is_current` flags the latest version of each `(case_id, report_type)` pair.
- `linked_to_closure` marks reports attached during case closure.
- Cascade delete when the parent case is deleted — these are case-history artifacts, not standalone records.

### 4.11 Agent Execution Cascade Tables

The investigation engine records its runtime activity in a four-level cascade:

```text
Case → investigation_sessions → agent_executions → agent_tool_calls
```

All four levels delete by CASCADE from `cases`. Note: `investigation_sessions` is an agent-execution session (per-investigation context), **not** an auth session. Auth sessions live in Redis.

#### investigation_sessions

Top of the cascade. One session groups all agent executions that occur within a single user-initiated investigation run. This table is case-owned (lives under the case module), not auth-owned.

**When written**: Created when the user submits a turn and the milestone engine starts processing. A session spans multiple agent executions (one per LLM call iteration).

**Key columns** (see `InvestigationSessionModel` in `models.py`):

| Column | Type | Notes |
| --- | --- | --- |
| `session_id` | VARCHAR(36) PK | |
| `case_id` | VARCHAR(36) FK → cases CASCADE | |
| `user_id` | VARCHAR(36) | |
| `organization_id` | VARCHAR(36) | |
| `state` | VARCHAR(32) | `active\|paused\|completed\|abandoned` — renamed from `status` in migration 015 (`investigation_sessions_state_check`) |
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

#### agent_tool_calls

Tool-call log per agent execution. FK: `execution_id → agent_executions` ON DELETE CASCADE. Records each tool invocation made by the agent during an execution.

**Key columns** (see ORM `AgentToolCallModel`):

| Column | Type | Notes |
| --- | --- | --- |
| `tool_call_id` | VARCHAR(36) PK | |
| `organization_id` | VARCHAR(36) FK → organizations CASCADE | |
| `execution_id` | VARCHAR(36) FK → agent_executions CASCADE | |
| `tool_name` | VARCHAR(128) | |
| `tool_input` | JSONB nullable | |
| `tool_output` | JSONB nullable | |
| `status` | VARCHAR(32) | `pending\|running\|success\|failed` |
| `error_message` | TEXT nullable | |
| `started_at` / `completed_at` | TIMESTAMPTZ nullable | |
| `duration_ms` | INTEGER nullable | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

**Applicability**: Both deployments.

### 4.12 Supporting Tables

`users`, `organizations`, `enterprises`, `teams`, and related auth/RBAC tables are defined in [user-schema.md](./user-schema.md) — that is the authoritative source for their DDL. They are not redefined here.

#### Enterprise Tier (Enterprise → Organization → Team → User)

The user domain has a strict three-tier tenancy hierarchy:

```text
enterprises  (corporate umbrella; SSO/billing)
  └── organizations  (workspaces; hard data-isolation boundary)
        └── teams    (routing buckets within an org)
              └── users  (members; one user can belong to multiple orgs in their enterprise)
```

**enterprise_id NOT NULL invariant** (migration 006, `be112b702fd4`):

- `users.enterprise_id` and `organizations.enterprise_id` are both `VARCHAR(36) NOT NULL` with `FOREIGN KEY ... ON DELETE CASCADE` to `enterprises.enterprise_id`.
- Migration 003 transitionally relaxed these columns to nullable; migration 006 backfills any remaining NULLs to the default enterprise UUID, then tightens both columns back to NOT NULL.
- Single-tenant deployments use the default enterprise UUID `00000000-0000-0000-0000-000000000002` (seeded by migration 006 as an idempotent UPSERT — same UUID that `SingleTenantProvider.DEFAULT_ENTERPRISE_ID` uses).
- Multi-tenant cloud deployments populate `enterprise_id` via the OAuth/SSO flow at user provisioning time.

The Pydantic Case model and the case-domain tables do not carry `enterprise_id` directly — case-level tenancy is anchored on `organization_id`, and the enterprise relationship is reachable via `organizations.enterprise_id`.

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
  LEFT JOIN progress pr ON c.case_id = pr.case_id
  LEFT JOIN documentation d ON c.case_id = d.case_id
-- Result: many-way JOIN for every case fetch!
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

**Decision**: `organization_id` is **denormalized onto every tenanted table** (`NOT NULL`), not stored only on `cases`. This is what the live ORM and migration 018 (RLS tenant isolation) implement — see the Implementation Pattern below.

> **Superseded**: An earlier revision of this section proposed storing `organization_id` only on `cases` and resolving org filtering via JOIN. That was rejected in favor of the denormalized layout because PostgreSQL Row-Level Security policies filter each table by its own `organization_id` column — RLS cannot reach through a JOIN to `cases`.

**Rationale**:

1. **RLS requires the column per table**: each tenanted table's `<table>_tenant_isolation` policy filters on that table's own `organization_id`; a JOIN-only design cannot back RLS.

2. **Direct repository-layer filtering**: the repository filters by `organization_id` on every query without a mandatory JOIN to `cases`.

3. **Defense in depth**: the column + RLS provide a second enforcement layer independent of application-level `case_id` scoping.

**Implementation Pattern**:

All tenanted tables carry `organization_id NOT NULL` for PostgreSQL Row-Level Security (RLS) and direct repository-layer filtering. The repository layer filters by `organization_id` on every query; RLS provides defense-in-depth on PostgreSQL.

```sql
-- Repository layer: direct filter on organization_id (Tier 1, both dialects)
SELECT e.* FROM evidence e
WHERE e.case_id = :case_id
  AND e.organization_id = :organization_id;

-- PostgreSQL RLS also enforces this via SET LOCAL app.current_org_id (Tier 2, cloud only)
```

**Tables WITH organization_id NOT NULL FK** (all tenanted tables):

- ✅ `cases` — top-level tenant anchor
- ✅ `evidence`
- ✅ `hypotheses`
- ✅ `hypothesis_evidence`
- ✅ `solutions`
- ✅ `uploaded_files`
- ✅ `case_messages`
- ✅ `case_actions`
- ✅ `case_tags`
- ✅ `case_checkpoints`
- ✅ `case_entities`
- ✅ `reports`
- ✅ `investigation_sessions`
- ✅ `agent_executions`
- ✅ `agent_tool_calls`
- ✅ `knowledge_items`
- ✅ `knowledge_suggestions`
- ✅ `conversion_jobs`
- ✅ `conversion_drafts`

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

-- Update case state: Row-level lock on cases table only
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

The supported pattern is **scoped field-merge** in repository methods (`update_progress`, `update_working_conclusion`) — read-modify-write inside one transaction with row-level locking (SQLAlchemy session locking on SQLite; `SELECT FOR UPDATE` equivalent on PostgreSQL). The `cases.version` column provides optimistic concurrency control for full-aggregate saves. Last-write-wins is the design until evidence shows otherwise.

The options below are retained for historical reference only — they document what was considered and why not implemented.

**Remaining JSONB fields** in `cases` table:

- `inquiry` — set once at creation, immutable
- `problem_verification` — set once per milestone
- `working_conclusion` — updated during investigation
- `root_cause_conclusion` — set once at resolution
- `progress` — updated frequently (contains the investigation journal plus the
  assessment variables `cause_state`/`solution_state`/`solution_feasible` and the
  `stabilization` record introduced by the flow redesign; `path_selection` was
  dropped in migration 016)
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
| `progress` | Very frequent (every turn) | **JSONB merge (`\|\|`)** | High concurrency, independent updates; now also holds the assessment vars + `stabilization` record |
| `documentation` | Occasional | JSONB merge | Append-only, low conflict |

**Best Practice**:

- Use **JSONB merge (`||`)** for fields with independent sub-fields (e.g., `progress.turn_count`, `progress.milestones`)
- Use **optimistic locking** for fields updated as a whole (e.g., `working_conclusion`)
- Use **application locks** only when database-level solutions aren't sufficient

### 7.4 Case Deletion Strategy

**Current live implementation**: Hard delete with CASCADE. Cases and all their child rows (evidence, hypotheses, messages, reports, agent-execution cascade) are removed in a single `DELETE FROM cases WHERE case_id = :id` thanks to FK CASCADE. Soft delete is intentionally not part of the schema for case-domain tables; account-style entities (`enterprises`, `organizations`, `teams`, `users`) carry `deleted_at` separately.

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

All tenanted case-domain tables get RLS policies in PostgreSQL deployments. SQLite (Local Deployment) has no equivalent — tenant isolation continues to be enforced at the repository layer.

**Tenanted case-domain tables** covered by RLS:

`cases`, `case_messages`, `case_actions`, `case_tags`, `case_checkpoints`, `case_entities`, `evidence`, `hypotheses`, `hypothesis_evidence`, `solutions`, `uploaded_files`, `investigation_sessions`, `agent_executions`, `agent_tool_calls`, `reports`, `conversion_jobs`, `conversion_drafts`, `causal_nodes`, `causal_edges`, `causal_node_evidence`

> `causal_nodes` / `causal_edges` / `causal_node_evidence` were added after the original RLS migration (018) and enrolled later by migration **023**. New tenanted case-domain tables must be added to the RLS policy set in the migration that creates them (or a follow-up), or they silently escape tenant isolation.

**Policy pattern (Tier 2 — PostgreSQL-only)**:

```sql
ALTER TABLE cases ENABLE ROW LEVEL SECURITY;

CREATE POLICY cases_tenant_isolation ON cases
    USING (organization_id = current_setting('app.current_org_id', true));

-- Repeat for each tenanted table above.
```

**Request wiring**: A global FastAPI dependency (`faultmaven/api/middleware/tenant_scope.py::bind_request_org_context`) binds the request's organization to a contextvar (`faultmaven/config/tenant_context.py`), and an engine `begin` listener (`faultmaven/infrastructure/persistence/database.py`) applies it to **every transaction** via `SELECT set_config('app.current_org_id', :org_id, true)` (`SET LOCAL` cannot take a bound parameter). No per-endpoint code is involved.

**Test requirement**: A test must demonstrate that an `AsyncSession` with `app.current_org_id` unset returns zero rows from any tenanted table. This proves RLS is enforced, not just advisory.

---

## 8. Testing Requirements

Before deploying PostgreSQLHybridCaseRepository to production, validate the following:

### 8.1 Schema Validation

```bash
# Deploy PostgreSQL to K8s (if not already running)
kubectl apply -f faultmaven-k8s-infra/applications/postgresql/

# Apply migrations via alembic (chain head: c5d6e7f8a9b0)
alembic upgrade head

# Verify all tables created
psql -U faultmaven -d faultmaven_cases -c "\dt"
# Expected case-domain tables: cases, evidence, hypotheses, hypothesis_evidence,
# solutions, uploaded_files, case_messages, case_actions, case_tags,
# case_checkpoints, case_entities, reports, investigation_sessions,
# agent_executions, agent_tool_calls.
# See er-diagram.md for the full 32-table enumeration across all domains.

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
- [x] ORM models (`faultmaven/infrastructure/persistence/models.py`)
- [x] Migration chain through head `c5d6e7f8a9b0` (033)
- [x] Repository implementation (`postgresql_hybrid_case_repository.py`, `sqlite_case_repository.py`)
- [x] Container.py wiring (`CASE_STORAGE_TYPE=database`)
- [x] Enterprise tier bootstrap (default enterprise seed; NOT NULL `enterprise_id` on users/orgs)
- [x] `case_actions.triggered_by` column + read path wired (migration 008)
- [x] Evidence/Solution audit fields wired end-to-end (migration 009): `Evidence` adds `primary_purpose`/`analysis`/`processing_mode`/`advances_milestones`/`collected_by`; `Solution` adds `proposed_by`/`applied_by`/`verification_method`/`verification_evidence_id`/`effectiveness` and renames `implemented_at`→`applied_at`, `verification_timestamp`→`verified_at`

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

- **Normalized evidence/hypotheses/solutions** → Row-level locking, concurrent writes, indexed filtering
- **Junction table `hypothesis_evidence`** → Replaces a JSON-blob evidence-link list; queryable in both directions with relationship qualifier
- **JSONB for flexible data** → Inquiry, conclusions, progress tracking carried inside the case row
- **Full-text search indexes** (Tier 2) → Fast case and evidence search on PostgreSQL
- **Tenancy-anchored** → `organization_id NOT NULL` on every tenanted table; PostgreSQL RLS for defense in depth

---

## Appendix A: Removed in Redesign

The following tables and columns existed in earlier iterations of this design but are no longer part of the schema. They are listed here so historical readers and migration archaeologists know where the names came from; they are not part of the current ORM, current migrations, or any production code path.

### Removed tables

- **`evidence_artifacts`** — Standalone-evidence holding table written by a now-removed API endpoint and never read by the investigation engine. Replaced by `evidence` carrying `case_id NOT NULL`. The "primary evidence per case" concept that lived on `evidence_artifacts.is_primary` is preserved on `evidence.is_primary`.
- **`standalone_evidence`** — Companion to `evidence_artifacts`; same rationale. The standalone evidence path (`POST /api/v1/evidence`, `POST /api/v1/evidence/{id}/link`, the `EvidenceService` and `APIEvidenceArtifactService`) is removed entirely.
- **`agent_tool_calls` v1** — Earlier shape with no functional readers/writers. The current `agent_tool_calls` table (formerly `agent_tool_calls_v2`) is the only canonical tool-call log.
- **`sessions`** — SQL auth-session table. Auth sessions are Redis-only (FakeRedis on local, Redis on cloud) — they never had any business in the case-domain schema.

### Removed columns

- **`cases.session_id`** — Coupling a case to an auth session was an anti-pattern. Cases now live independently of any auth session.
- **`cases.degraded_mode`** — Vestigial flag with no live readers/writers.
- **`uploaded_files.preprocessing_summary`** — Dropped in migration 004 (`f7bbadb43e4c`). Preprocessing artifacts (`summary`, `structural_index`, `data_type`, `coverage_*`) now live as first-class columns on `uploaded_files` (re-added by migration 010).
- **`uploaded_files.content_ref`** — Renamed to `storage_ref` to make the role (opaque key for the storage backend) clearer.
- **`uploaded_files.source_type`** — Renamed to `upload_source` to disambiguate from `evidence.source_type`. The two columns have distinct semantics: `upload_source` is provenance (file_upload | conversion_source | api_push); `evidence.source_type` is the data-shape classification (logs | metrics | configuration | code | text | image | user_description).
- **`hypotheses.evidence_links`** — JSON-blob list replaced by the `hypothesis_evidence` junction table (§4.4-bis), which carries a `relationship_type` qualifier and per-link confidence/audit metadata.
- **Report types `incident_report` and `post_mortem`** — Removed from `ReportType`. Only `resolution_summary` and `closure_summary` remain. Reusable knowledge derived from cases lives in `knowledge_items`, not `reports`.

---

**Document Control**:

- **Author**: FaultMaven Team
- **Created**: 2025-11-09
- **Last Updated**: 2026-05-10
- **Version**: 4.2 (Authoritative)
- **Status**: ✅ Implemented — live schema (migration chain head `c5d6e7f8a9b0`, 033)

**Changelog**:

**4.2 — 2026-05-10**

Migration chain extended through 009; closes the Evidence/Solution
silent-data-loss gap surfaced by the schema-redesign coherence audit.

- §4.3 `evidence`: added `primary_purpose` (NOT NULL, server default
  `'legacy'`), `analysis`, `processing_mode`, `advances_milestones`
  (TagsArray), and `collected_by` (NOT NULL, server default
  `'system'`). Documented the Pydantic mirror for
  `evidence_summary_not_empty`.
- §4.4 `hypotheses`: documented the Pydantic mirror for
  `hypotheses_statement_not_empty`.
- §4.5 `solutions`: dropped dead FK columns (`created_by`, `updated_by`
  — repo always wrote NULL); renamed `implemented_at` → `applied_at`
  and `verification_timestamp` → `verified_at` to match domain naming;
  added `proposed_by` (NOT NULL, server default `'agent'`),
  `applied_by`, `verification_method`, `verification_evidence_id`
  (FK to `evidence` with SET NULL), and `effectiveness` with
  range CHECK. Documented the repo-derived `status` lifecycle
  (`verified_at` → 'verified', `applied_at` → 'implemented',
  otherwise 'proposed').
- Migration chain table: added row 009 (`4b7e2f9d3a18`).
- Header revision + Implementation Status row + the `alembic upgrade
  head` example all bumped to 009's revision (`4b7e2f9d3a18`).
- Implementation checklist gained an Evidence/Solution audit-fields
  completion line.

**4.1 — 2026-05-10**

Migration chain extended through 008.

- §4.8 `case_actions`: rewrote the table description and DDL to reflect
  the post-008 reality. `triggered_by VARCHAR(50) NOT NULL` is now a
  real column (free-form actor identifier; not a FK because the value
  space is heterogeneous user UUIDs + sentinels). Removed the
  "Proposed but absent" caveat that lied about the column's status.
  Documented the read path: both repos hydrate `Case.action_history`
  from `case_actions` rows in `_to_domain` (was previously hardcoded
  to `[]`). Cleaned the Tier 2-only CHECK that wasn't in the ORM.
- Migration chain table: added rows 007 (`05b6eaf5baad` — drop
  `users_password_or_sso` CHECK) and 008 (`317a8c329673` —
  `case_actions.triggered_by`).
- Header revision + Implementation Status row + the `alembic upgrade
  head` example all bumped to 008's revision (`317a8c329673`).
- Implementation checklist gained a `case_actions.triggered_by`
  completion line.

**4.0 — 2026-05-08**

Aligned doc with the live ORM in `faultmaven/infrastructure/persistence/models.py` after the major redesign (migrations 001–006).

- §4.2 `cases`: `description` is a first-class column with the migration-005 CHECK (`status IN ('inquiry','closed') OR LENGTH(TRIM(description)) > 0`); `current_turn`, `turns_without_progress`, `version` promoted to first-class columns; `is_archived`/`archived_at` removed (not in the live ORM).
- §4.3 `evidence`: rewritten around `summary` (NOT NULL) and `extract` (nullable); `form` column dropped (migration 010); `evidence_source_invariant` CHECK added; file metadata lives on `uploaded_files` (linked via `source_file_id`).
- §4.4 `hypotheses`: dropped `evidence_links` JSON blob; added `hypothesis_evidence` junction table (§4.4-bis).
- §4.5 `solutions`: `SolutionState` corrected to `proposed | accepted | rejected | implemented | verified`.
- §4.6 `uploaded_files`: renamed `content_ref` → `storage_ref` and `source_type` → `upload_source`; FK ordering and ON DELETE rules aligned with ORM; preprocessing artifacts (`summary`, `structural_index`, `data_type`, `coverage_*`) added (migration 010) so the file row is the canonical home for file-level metadata.
- §4.10 `reports`: `report_type` CHECK restricted to `resolution_summary` and `closure_summary` (no `runbook`, `incident_report`, `post_mortem`).
- §4.12: added Enterprise Tier section documenting the Enterprise → Organization → Team → User hierarchy and the migration-006 NOT NULL invariant on `users.enterprise_id` and `organizations.enterprise_id`, including the default enterprise UUID `00000000-0000-0000-0000-000000000002`.
- §5.4: tenanted-tables list aligned to live schema.
- Removed v2.1 / v3.x version markers throughout where they carried no information.
- Implementation Status table: bumped to migration head `be112b702fd4`; replaced single-baseline reference with full migration chain (001–006).
- Historical-but-deleted tables (`evidence_artifacts`, `standalone_evidence`, `agent_tool_calls` v1, `sessions`) consolidated into Appendix A.

Earlier versions (3.x and prior) carried iterative consolidation notes. They are summarized here in spirit (single-table evidence, hypothesis-status enum unification, sessions-table removal, etc.) and have been folded into the current narrative; per-version detail is in the document's git history.

# Module Organization Design: Vertical vs Horizontal

**Version**: 2.2
**Date**: 2026-04-18
**Status**: Schema-Verified Active Recommendation
**Related**: [Architectural Design Principles](architectural-design-principles.md)

**Schema Verification**: This document is verified against the live schema via [../data-and-storage/schemas/case-schema.md](../data-and-storage/schemas/case-schema.md), [../data-and-storage/er-diagram.md](../data-and-storage/er-diagram.md), and `faultmaven/infrastructure/persistence/models.py`.

---

## Executive Summary

This document provides specific recommendations for which FaultMaven components should use **vertical slicing** (domain boundaries) versus **horizontal layering** (cross-cutting infrastructure). The recommendations optimize for maintainability, testability, and future microservice extraction while respecting the principle that **not all modules need domain boundaries**.

### Quick Reference: Minimum Criteria

A module is **VERTICAL** (business domain) if and only if it meets **ALL THREE** criteria:

1. ✅ **Domain Data Ownership**: Owns database tables representing business entities
2. ✅ **Business Logic Implementation**: Implements business rules and domain constraints
3. ✅ **Domain Capability**: Represents a distinct business capability

**Decision Rule**: `IF (owns_domain_data AND implements_business_logic AND represents_domain_capability) THEN VERTICAL ELSE HORIZONTAL`

**Stability**: These criteria are stable - normal code evolution (refactoring, adding features) does NOT require re-categorization.

### Schema-Verified Classification (2026-01-09)

**Based on review of the live schema (see [../data-and-storage/er-diagram.md](../data-and-storage/er-diagram.md) for the authoritative table enumeration):**

| Module | Schema Verification | Classification |
|--------|-------------------|----------------|
| **Case** | Owns the case-domain tables (cases, evidence, hypotheses, solutions, case_messages, case_actions, uploaded_files, reports, and related high-cardinality tables) | ✅ **VERTICAL** |
| **Auth** | Owns the 10 user-domain tables (`users`, `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `teams`, `team_members`, `user_audit_log`, `oauth_authorization_codes`) | ✅ **VERTICAL** |
| **Knowledge** | Owns `knowledge_items` + `knowledge_suggestions` PostgreSQL tables + the unified `faultmaven_kb` ChromaDB collection | ✅ **VERTICAL** |
| **Evidence** | Evidence table has FK to `cases` → part of Case module's schema | ❌ **DOMAIN SERVICE** |
| **Agent** | No agent_* tables; `agent_tool_calls` is case audit data | ❌ **DOMAIN SERVICE** |
| **Preprocessing** | No tables; data classification, extraction (11 extractors), and chunking that operate on Evidence data | ❌ **DOMAIN SERVICE** |
| **Report** | Reports stored in Case module's `reports` table (FK to cases) - TD-001 complete | ❌ **DOMAIN SERVICE** |

**Result**: Only **3 modules** are truly vertical (Case, Auth, Knowledge). Evidence, Agent, Preprocessing, and Report are domain services that implement business logic but operate on data owned by other modules.

---

## Decision Framework

### Minimum Criteria for Vertical Modules

A module is **vertical** (business domain) if and only if it meets **ALL THREE** of these criteria:

#### Criterion 1: Domain Data Ownership ✅
- **Requirement**: Module owns database tables/entities that represent **business entities** (not just technical state)
- **Test**: Does the module own one or more SQL tables for its core business entities (e.g., `cases`, `users`, `knowledge_items`)? Module-name prefixes are not required — semantic prefixes (`case_messages`, `oauth_authorization_codes`) are used only to disambiguate sub-entities.
- **Exclusion**: Technical state (sessions, caches, locks) does NOT count as domain data
- **Stable**: Once a module owns domain data, this criterion remains true even if the schema evolves

#### Criterion 2: Business Logic Implementation ✅
- **Requirement**: Module implements **business rules** that enforce domain constraints and workflows
- **Test**: Does the module contain logic that would be described in business requirements (e.g., "users can only access cases they own")?
- **Exclusion**: Technical integration logic (API calls, data transformation, protocol handling) does NOT count
- **Stable**: Business logic may evolve, but the presence of business logic is stable

#### Criterion 3: Domain Capability ✅
- **Requirement**: Module represents a **distinct business capability** that can be understood independently
- **Test**: Can you describe what the module does in business terms without mentioning technical details?
- **Examples**: "Manages user authentication", "Tracks troubleshooting cases", "Stores evidence artifacts"
- **Stable**: The business capability remains constant even as implementation changes

### Decision Rule

```
IF (owns_domain_data AND implements_business_logic AND represents_domain_capability)
THEN module = VERTICAL
ELSE module = HORIZONTAL
```

**All three criteria must be true** for a module to be vertical. If any criterion is false, the module is horizontal.

### When to Use Horizontal Layering

A component is **horizontal** (infrastructure) if it fails **ANY** of the three criteria above. Common patterns:

1. ❌ **No domain data** - Only technical state (sessions, caches) or no state at all
2. ❌ **No business logic** - Only technical integration (API calls, protocol handling, data transformation)
3. ❌ **No domain capability** - Provides technical capability, not business capability

**Examples of horizontal components**:
- Provider abstractions (LLM, storage, vector stores) - No domain data, no business logic
- Cross-cutting concerns (logging, observability) - No domain data, no business logic
- Utilities (serialization, validation) - No domain data, no business logic
- Connection management - No domain data, no business logic

---

## Criteria Application Examples

### Example 1: `modules/auth/` - ✅ VERTICAL

**Criterion 1: Domain Data Ownership** ✅
- Owns tables: `users`, `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `teams`, `team_members`, `user_audit_log`, `oauth_authorization_codes` (session storage is in Redis/FakeRedis, not in a SQL table)
- These represent business entities (users, organizations, RBAC), not technical state
- **Result**: PASS

**Criterion 2: Business Logic Implementation** ✅
- Implements business rules: "Users must have valid email", "Sessions expire after 24 hours", "Team members inherit organization permissions"
- Contains domain constraints and workflows
- **Result**: PASS

**Criterion 3: Domain Capability** ✅
- Business description: "Manages user authentication, authorization, and access control"
- Can be understood without technical details
- **Result**: PASS

**Decision**: ✅ **VERTICAL** (all 3 criteria met)

---

### Example 2: `infrastructure/llm/` - ❌ HORIZONTAL

**Criterion 1: Domain Data Ownership** ❌
- No database tables
- Only manages API connections and routing
- **Result**: FAIL

**Criterion 2: Business Logic Implementation** ❌
- No business rules
- Only technical integration (API calls, token counting, response parsing)
- **Result**: FAIL

**Criterion 3: Domain Capability** ❌
- Technical description: "Provides LLM provider abstraction and routing"
- Not a business capability
- **Result**: FAIL

**Decision**: ❌ **HORIZONTAL** (0 of 3 criteria met)

---

### Example 3: `infrastructure/logging/` - ❌ HORIZONTAL

**Criterion 1: Domain Data Ownership** ❌
- No database tables (logs are written to files/streams, not domain entities)
- **Result**: FAIL

**Criterion 2: Business Logic Implementation** ❌
- No business rules
- Only technical capability (log formatting, correlation IDs, deduplication)
- **Result**: FAIL

**Criterion 3: Domain Capability** ❌
- Technical description: "Provides structured logging and correlation"
- Not a business capability
- **Result**: FAIL

**Decision**: ❌ **HORIZONTAL** (0 of 3 criteria met)

---

### Example 4: `modules/knowledge/` - ✅ VERTICAL

**Criterion 1: Domain Data Ownership** ✅
- Owns tables: `knowledge_items` + `knowledge_suggestions` (PostgreSQL) + the unified `faultmaven_kb` ChromaDB collection with metadata-based scope filtering (scope, owner_id, team_id)
- Schema reference: `../data-and-storage/overview.md` Section 5.5.2 and `004_kb_sharing_infrastructure.sql`
- These represent business entities (knowledge documents, runbooks)
- **Result**: PASS

**Criterion 2: Business Logic Implementation** ✅
- Implements business rules: "Documents must be indexed before search", "Sharing visibility controls access", "Knowledge items belong to organizations"
- Contains domain workflows (ingestion, indexing, retrieval, sharing)
- **Result**: PASS

**Criterion 3: Domain Capability** ✅
- Business description: "Manages knowledge base, document indexing, and RAG operations"
- Can be understood as a business capability
- **Result**: PASS

**Decision**: ✅ **VERTICAL** (all 3 criteria met)

**Note**: Even though `knowledge/` uses `infrastructure/knowledge/` (horizontal), it remains vertical because it owns the business logic and data.

---

### Example 5: `modules/evidence/` - ❌ DOMAIN SERVICE (Not Vertical)

**Criterion 1: Domain Data Ownership** ❌
- **Does NOT own** the `evidence` table
- Evidence table has `case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE`
- Schema verification: `../data-and-storage/schemas/case-schema.md` Section 4.3 - evidence is part of Case module's schema
- Evidence is owned by Case module, not Evidence module
- **Result**: FAIL

**Criterion 2: Business Logic Implementation** ✅
- Implements business rules: "Evidence must be validated", "Preprocessing required before storage", "Evidence linked to case"
- Contains domain workflows (collection, validation, preprocessing)
- **Result**: PASS

**Criterion 3: Domain Capability** ✅
- Business description: "Collects and processes investigation evidence"
- Can be understood as a business capability
- **Result**: PASS

**Decision**: ❌ **DOMAIN SERVICE** (fails Criterion 1: no data ownership)

**Key Insight**: Evidence module provides **collection logic** but operates on data owned by Case module. This makes it a domain service, not a vertical module.

---

### Example 6: `modules/agent/` - ❌ DOMAIN SERVICE (Not Vertical)

**Criterion 1: Domain Data Ownership** ❌
- **No `agent_*` tables** in the schema
- Any `agent_tool_calls` table (if exists) stores case audit data, not agent state
- Schema verification: `../data-and-storage/schemas/case-schema.md` Section 4.1 lists only case-owned tables; none are agent-owned
- Agent's LangGraph state is ephemeral/in-memory
- **Result**: FAIL

**Criterion 2: Business Logic Implementation** ✅
- Implements business rules: "Investigation workflows", "Milestone orchestration", "Tool selection logic"
- Contains complex domain workflows (LangGraph state machines)
- **Result**: PASS

**Criterion 3: Domain Capability** ✅
- Business description: "Orchestrates AI-powered investigation workflows"
- Can be understood as a business capability
- **Result**: PASS

**Decision**: ❌ **DOMAIN SERVICE** (fails Criterion 1: no data ownership)

**Key Insight**: Agent module provides **orchestration logic** via LangGraph but all persistent state flows through Case module's repository.

---

### Example 7: `modules/report/` - ❌ DOMAIN SERVICE (Not Vertical, TD-001 Complete)

**Criterion 1: Domain Data Ownership** ❌
- **Does NOT own** the `reports` table
- Reports stored in Case module's `reports` table with FK to `cases(case_id) ON DELETE CASCADE`
- Schema verification: `../data-and-storage/schemas/case-schema.md` Section 4.9 - `reports` table is part of Case module's schema
- Reports are owned by Case module, not Report module (same pattern as Evidence module)
- **Result**: FAIL

**Criterion 2: Business Logic Implementation** ✅
- Implements business rules: "Report generation from case data", "Versioning (max 5 versions)", "Auto-indexing runbooks"
- Contains domain workflows (generation, templating, indexing)
- **Result**: PASS

**Criterion 3: Domain Capability** ✅
- Business description: "Generates case reports, runbooks, and post-mortems"
- Can be understood as a business capability
- **Result**: PASS

**Decision**: ❌ **DOMAIN SERVICE** (fails Criterion 1: no data ownership)

**Key Insight**: Report module generates **investigation outputs** (symmetric to Evidence module's investigation inputs), but data is owned by Case module. Reports persist for the lifetime of the case, same lifecycle as evidence (TD-001 migration complete).

---

## Edge Cases and Mixed Modules

### Edge Case 1: Module with Both Business Logic and Infrastructure

**Scenario**: A module that has some business logic but primarily provides infrastructure.

**Example**: `infrastructure/security/` with PII redaction
- Has some business rules (redaction patterns)
- But no domain data ownership
- Primarily provides technical capability

**Decision**: ❌ **HORIZONTAL** (fails Criterion 1: no domain data)

**Rationale**: If a module doesn't own domain data, it cannot be vertical, regardless of business logic complexity.

---

### Edge Case 2: Module with Data but No Business Logic

**Scenario**: A module that stores data but only does technical transformation.

**Example**: Hypothetical `infrastructure/cache/` that stores cached responses
- Has data (cache entries)
- But no business rules (just cache invalidation logic)
- Technical capability, not business capability

**Decision**: ❌ **HORIZONTAL** (fails Criterion 2: no business logic, fails Criterion 3: not a domain capability)

**Rationale**: Technical state (caches, sessions, locks) is not domain data. Domain data represents business entities.

---

### Edge Case 3: Module That Evolves Over Time

**Scenario**: A module starts as infrastructure but gains business logic and data.

**Example**: Hypothetical `modules/analytics/` that starts as a utility but later:
- Adds `analytics_events` table (domain data)
- Implements business rules (event aggregation, reporting)
- Becomes a domain capability

**Decision Process**:
1. **Initial State**: ❌ HORIZONTAL (no domain data, no business logic)
2. **After Evolution**: ✅ VERTICAL (all 3 criteria met)

**Migration**: When a module gains all 3 criteria, it should be migrated to vertical structure with contracts.

**Stability**: The criteria are stable - once a module meets all 3, it remains vertical even if implementation changes.

---

### Edge Case 4: Shared Domain Logic (`core/`)

**Scenario**: Code in `core/` that contains business logic but no data ownership.

**Example**: `core/investigation/` with investigation orchestration logic
- Has business logic (investigation workflows)
- But no domain data (investigations are stored in `modules/case/`)
- Shared across modules

**Decision**: ⚠️ **SHARED** (not vertical, not horizontal infrastructure)

**Rationale**:
- Fails Criterion 1: No domain data ownership (data owned by `modules/case/`)
- Has business logic but doesn't own the domain
- Should remain in `core/` as shared domain logic
- **Future**: Consider moving into `modules/agent/` or `modules/case/` if it becomes module-specific

---

## Stability Guarantees

### Why These Criteria Are Stable

1. **Domain Data Ownership** is stable:
   - Once a module owns domain tables, it continues to own them
   - Schema evolution doesn't change ownership
   - Adding new tables to an existing module doesn't change categorization

2. **Business Logic Implementation** is stable:
   - Business logic may evolve, but its presence is stable
   - Refactoring business logic doesn't remove it
   - Adding new business rules doesn't change categorization

3. **Domain Capability** is stable:
   - The business capability remains constant
   - Implementation changes don't change the capability
   - Adding features doesn't change the core capability

### When Re-Categorization Is Required

Re-categorization is only required when:

1. **Module gains all 3 criteria** (horizontal → vertical):
   - Example: Utility gains domain data and business logic
   - Action: Migrate to vertical structure with contracts

2. **Module loses a criterion** (vertical → horizontal):
   - Example: Domain data moved to another module
   - Action: Migrate to horizontal structure (rare)

3. **Module splits** (one module → multiple modules):
   - Example: Large module splits into focused modules
   - Action: Apply criteria to each new module independently

**Note**: Normal code evolution (refactoring, adding features, changing implementation) does NOT require re-categorization.

---

## Validation Checklist

Use this checklist to validate module categorization:

### For Vertical Modules

- [ ] Module owns one or more database tables (named after the business entity, not necessarily prefixed by module name — e.g., `cases`, `users`, `knowledge_items`)
- [ ] Tables represent business entities (not technical state)
- [ ] Module contains business rules (not just technical integration)
- [ ] Module can be described in business terms
- [ ] Module has `contracts.py` defining public interfaces
- [ ] Module has `api/`, `domain/`, `infrastructure/` structure

### For Horizontal Modules

- [ ] Module has no domain data (or only technical state)
- [ ] Module provides technical capability (not business capability)
- [ ] Module is provider-agnostic (swappable implementations)
- [ ] Module is used by multiple business domains
- [ ] Module has no business rules (only technical logic)

---

## Vertical Modules: Peer Status and Dependencies

### Are All Vertical Modules Peers?

**Yes, all vertical modules are structural peers**, meaning:

1. ✅ **Equal Structure**: All vertical modules have the same structure (`contracts.py`, `api/`, `domain/`, `infrastructure/`)
2. ✅ **Equal Status**: No vertical module is "more important" or "foundational" than another
3. ✅ **Independent Domains**: Each represents a distinct business capability
4. ✅ **Can Have Dependencies**: Vertical modules CAN depend on other vertical modules (via contracts)

### High Fan-In: Does It Change Categorization?

**No, high fan-in does NOT change categorization.** Here's why:

#### The Key Distinction: Business vs Technical Dependencies

**Business Dependencies** (Vertical → Vertical):
- Module A depends on Module B for **business reasons**
- Example: `modules/case/` depends on `modules/auth/` to check user permissions
- Example: `modules/report/` depends on `modules/case/` to get case data
- **Result**: Both remain vertical (criteria-based, not usage-based)

**Technical Dependencies** (Any → Horizontal):
- Module depends on infrastructure for **technical reasons**
- Example: All modules depend on `infrastructure/logging/` for logging
- Example: All modules depend on `infrastructure/observability/` for tracing
- **Result**: Infrastructure remains horizontal (no domain data, no business logic)

#### Example: `modules/auth/` with High Fan-In

**Scenario**: `modules/auth/` is imported by all other vertical modules (case, evidence, knowledge, agent, report).

**Question**: Should `auth/` become a horizontal layer?

**Answer**: ❌ **No, it remains vertical** because:

1. ✅ **Meets All 3 Criteria**: Owns domain data (`users`, `organizations`, RBAC tables), implements business logic (authentication rules), represents domain capability (user management)
2. ✅ **Business Dependency**: Other modules depend on auth for **business reasons** (access control, user identity), not technical reasons
3. ✅ **Peer Status Maintained**: Auth is still a peer - it just happens to be used by others

**What Changes**: Dependency management strategy, not categorization.

---

## Dependency Management for High Fan-In Modules

### When a Vertical Module Has High Fan-In

If a vertical module is used by many/all other vertical modules, apply these strategies:

#### 1. **Enforce Contract Boundaries** ✅

```python
# ✅ CORRECT: Other modules import from contracts only
from faultmaven.modules.auth.contracts import IAuthService, UserDTO

# ❌ WRONG: Direct import from domain
from faultmaven.modules.auth.domain.models import User
```

**Benefit**: High fan-in doesn't create tight coupling - all dependencies go through contracts.

#### 2. **Use Dependency Injection** ✅

```python
# Composition root wires dependencies
case_service = CaseService(
    auth_service=auth_service,  # Injected, not imported
    case_repo=case_repo
)
```

**Benefit**: High fan-in doesn't create import cycles - dependencies are injected.

#### 3. **Consider Interface Segregation** ✅

If a module is used by many others, consider splitting its contract:

```python
# modules/auth/contracts.py
class IAuthService(Protocol):  # Full auth operations
    ...

class IUserQuery(Protocol):  # Read-only user queries (used by many)
    async def get_user(self, user_id: str) -> UserDTO: ...

class IPermissionChecker(Protocol):  # Permission checks (used by many)
    async def can_access(self, user_id: str, resource: str) -> bool: ...
```

**Benefit**: Modules that only need read access don't depend on full auth service.

#### 4. **Document Dependency Graph** ✅

Document which modules depend on high fan-in modules:

```python
# modules/auth/README.md
## Dependencies
- Used by: case, evidence, knowledge, agent, report
- Reason: Access control and user identity
- Contract: IAuthService, IUserQuery, IPermissionChecker
```

**Benefit**: Clear visibility into dependency patterns.

---

## When High Fan-In Indicates Horizontal Layer

High fan-in **does** indicate a horizontal layer when:

1. ❌ **No Domain Data**: Module has no database tables
2. ❌ **No Business Logic**: Module only provides technical capability
3. ❌ **Technical Dependency**: Other modules depend for technical reasons (logging, caching, etc.)

**Example**: `infrastructure/logging/` is used by all modules, but it's horizontal because:
- No domain data (logs aren't business entities)
- No business logic (just log formatting)
- Technical dependency (all modules need logging)

**Result**: Correctly categorized as horizontal, regardless of fan-in.

---

## Dependency Patterns Summary

| Pattern | Example | Category | Reason |
|---------|---------|----------|--------|
| **Vertical → Vertical** | `case/` → `auth/` | Both VERTICAL | Business dependency (access control) |
| **Vertical → Horizontal** | `case/` → `logging/` | VERTICAL → HORIZONTAL | Technical dependency (logging) |
| **High Fan-In Vertical** | All → `auth/` | All VERTICAL | Business dependencies (user identity) |
| **High Fan-In Horizontal** | All → `logging/` | All → HORIZONTAL | Technical dependencies (cross-cutting) |

**Key Insight**: Categorization is based on **criteria** (domain data, business logic, domain capability), not **usage patterns** (fan-in, fan-out).

---

## Domain Services vs Vertical Modules

### The Critical Distinction

**Vertical Modules** (Business Domains):
- ✅ Own domain data (database tables)
- ✅ Implement business logic
- ✅ Represent domain capability
- **Structure**: Full vertical slicing with `contracts.py`, `api/`, `domain/`, `infrastructure/`

**Domain Services** (Business Logic Without Data Ownership):
- ❌ Do NOT own domain data
- ✅ Implement business logic
- ✅ Represent domain capability
- **Structure**: Domain logic + API, but delegate persistence to owning modules

### Why Domain Services Exist

Some modules implement **business logic** but operate on **data owned by other modules**:

1. **Evidence Module**: Provides collection/validation logic but stores data in Case module's `evidence` table
2. **Agent Module**: Provides LangGraph orchestration but all persistent state flows through Case module
3. **Report Module**: Generates reports from Case data but stores in Case module's `reports` table (FK to cases) - TD-001 complete

### When to Use Domain Services

Use domain services when:
- ✅ Module implements complex business logic
- ✅ Module represents a distinct capability
- ❌ Module doesn't own domain data (data owned by another module)
- ✅ Module operates on data through contracts (e.g., `ICaseRepository`)

### Structural Options for Domain Services

When a module is classified as a Domain Service (not vertical), there are two structural approaches:

#### Option A: Horizontal Layer Structure

Move to `services/` directory (traditional layered architecture):

```
services/
├── evidence_service.py   # Evidence business logic
├── agent_service.py      # Agent business logic
└── report_service.py     # Report business logic

api/v1/routes/
├── evidence.py           # Evidence endpoints
├── agent.py              # Agent endpoints
└── report.py             # Report endpoints
```

**Pros**: Simple, fits traditional patterns
**Cons**: Loses domain cohesion, harder to extract as microservices

#### Option B: Domain Service Structure (Recommended)

Keep in `modules/` but remove vertical characteristics:

```
modules/evidence/          # Domain Service (NOT vertical)
├── domain/                # Business logic
└── api/                   # Endpoints
# NO contracts.py          # Don't own data, nothing to expose
# NO infrastructure/       # Uses Case repository

modules/agent/             # Domain Service (NOT vertical)
├── domain/                # LangGraph orchestration
├── tools/                 # Agent-specific tools (exception - see below)
└── api/                   # Endpoints
# NO contracts.py, NO infrastructure/

modules/report/            # Domain Service (NOT vertical, TD-001 complete)
├── domain/                # Report generation
└── api/                   # Endpoints
# NO infrastructure/       # Uses Case repository for persistence (TD-001 complete)
```

**Pros**: Maintains domain cohesion, extraction-ready, matches schema reality
**Cons**: Hybrid structure requires documentation clarity

### Recommended Approach: Domain Service Structure

**Rationale**:
1. **Preserves Domain Cohesion**: Logic organized by business domain
2. **Removes False Boundaries**: No `contracts.py` since they don't own data
3. **Correct Data Access**: Uses owning module's contracts (Case module)
4. **Future Extraction**: Still organized for potential microservice extraction

### Structural Comparison

| Aspect | Vertical Module | Domain Service | Horizontal Layer |
|--------|----------------|----------------|------------------|
| **Location** | `modules/{name}/` | `modules/{name}/` | `services/` |
| **Has contracts.py** | ✅ Yes | ❌ No | N/A |
| **Has infrastructure/** | ✅ Yes | ❌ No* | N/A |
| **Domain cohesion** | ✅ High | ✅ High | ⚠️ Medium |
| **Extraction ready** | ✅ Yes | ✅ Yes | ⚠️ Requires refactor |

*Note: Report module has no `infrastructure/` - uses Case repository for persistence (TD-001 complete)

### Purpose Achievement: Domain Services vs Vertical

| Goal | Vertical Module | Domain Service |
|------|----------------|----------------|
| **Domain Cohesion** | ✅ High | ✅ High |
| **Boundary Enforcement** | ✅ Enforced (contracts) | ⚠️ Conventions + linter |
| **Independent Development** | ✅ Yes | ✅ Yes |
| **Microservice Extraction** | ✅ Ready | ✅ Ready |
| **Schema Alignment** | ✅ Owns data | ✅ Delegates to owner |

### Domain Service Implementation Patterns

#### Evidence Service Pattern

```python
# modules/evidence/domain/evidence_service.py
from faultmaven.modules.case.contracts import ICaseRepository

class EvidenceService:
    def __init__(self, case_repo: ICaseRepository):
        self.case_repo = case_repo  # Uses Case contract

    async def collect_evidence(self, case_id: str, evidence_data: Evidence):
        # Business logic: validation, preprocessing
        validated = self.validate(evidence_data)
        processed = await self.preprocess(validated)

        # Delegate persistence to Case module (Case owns the table)
        await self.case_repo.add_evidence(case_id, processed)
```

#### Agent Service Pattern

```python
# modules/agent/domain/agent_service.py
from faultmaven.modules.case.contracts import ICaseRepository
from faultmaven.modules.knowledge.contracts import IKnowledgeService

class AgentService:
    def __init__(
        self,
        case_repo: ICaseRepository,
        knowledge_service: IKnowledgeService,
        llm_provider: ILLMProvider,
    ):
        self.case_repo = case_repo
        self.knowledge_service = knowledge_service
        self.llm_provider = llm_provider

    async def investigate(self, case_id: str, query: str):
        # LangGraph orchestration (ephemeral state)
        result = await self.orchestrate_investigation(case_id, query)

        # All persistent state via Case module's contract
        await self.case_repo.add_investigation_result(case_id, result)
```

#### Report Service Pattern (TD-001 Complete)

```python
# modules/report/domain/report_generation_service.py
from faultmaven.modules.case.contracts import ICaseRepository

class ReportGenerationService:
    def __init__(
        self,
        llm_router: Any,
        case_repo: ICaseRepository,  # TD-001: persist reports via Case contract
        runbook_kb: Optional[Any] = None,
        lock_manager: Optional[Any] = None,
        pii_redactor: Optional[Any] = None,
    ):
        self.llm_router = llm_router
        self.case_repo = case_repo
        self.runbook_kb = runbook_kb
        self.lock_manager = lock_manager
        self.pii_redactor = pii_redactor

    async def generate_report(self, case_id: str, report_type: ReportType):
        case = await self.case_repo.get(case_id)
        report = await self._generate(case, report_type)

        # TD-001 complete: persistent storage in PostgreSQL `reports` table via Case contract
        await self.case_repo.add_report(report)
        return report
```

#### Agent Tools: Exception to Domain Service Pattern

Agent keeps `tools/` directory because:

- Tools are domain-specific to agent orchestration
- Tools implement agent interfaces (BaseTool)
- Tools are NOT shared infrastructure
- Tools are NOT imported by other modules

```
modules/agent/
├── domain/
├── tools/           # ✅ Keep - domain-specific to agent
│   ├── knowledge_base.py
│   └── web_search.py
└── api/
```

### Boundary Enforcement for Domain Services

Without `contracts.py`, enforce boundaries via:

#### 1. Import Linter Rules

```yaml
# pyproject.toml or .import-linter config
forbidden_imports:
  - from: modules.case
    disallow:
      - modules.evidence.domain
      - modules.agent.domain
      - modules.report.domain
  - from: modules.auth
    disallow:
      - modules.evidence.domain
      - modules.agent.domain
      - modules.report.domain
```

#### 2. Allowed Import Patterns

```python
# ✅ CORRECT: Domain Service uses Vertical Module's contract
from faultmaven.modules.case.contracts import ICaseRepository

# ✅ CORRECT: Composition root imports Domain Service
from faultmaven.modules.evidence.domain import EvidenceService

# ❌ WRONG: Vertical Module imports Domain Service internals
from faultmaven.modules.evidence.domain.validators import validate_evidence

# ❌ WRONG: Domain Service imports another Domain Service's internals
from faultmaven.modules.agent.domain.services.investigation_service import InvestigationService
```

#### 3. Convention

- Only the composition root (`main.py`) imports Domain Service classes
- Domain Services communicate via Vertical Module contracts only
- API routes delegate to injected services, never import domain internals

### Migration Considerations

**Evidence Module**: Consider merging into Case module since:
- Evidence table is part of Case's schema
- Evidence logic is purely operational on Case-owned data
- No independent data lifecycle

**Agent Module**: Keep separate because:
- Complex LangGraph orchestration logic
- Tool system (knowledge_base, web_search) is independent
- Clear separation of concerns (orchestration vs data storage)

**Report Module**: Keep separate because:
- Report generation is complex business logic
- Reports stored persistently in PostgreSQL (owned by Case module via FK) - TD-001 complete
- Reports are investigation outputs with same lifecycle as case (symmetric to evidence)

---

## Summary Table (Schema-Verified)

| Module | Domain Data? | Business Logic? | Domain Capability? | Category | Schema Verification |
|--------|-------------|-----------------|-------------------|----------|---------------------|
| `modules/auth/` | ✅ Yes (`users`, `organizations`, RBAC, teams, OAuth) | ✅ Yes | ✅ Yes | ✅ **VERTICAL** | `../data-and-storage/schemas/user-schema.md` |
| `modules/case/` | ✅ Yes (case-domain tables including `evidence` and `reports`) | ✅ Yes | ✅ Yes | ✅ **VERTICAL** | `../data-and-storage/schemas/case-schema.md` Section 4.1 |
| `modules/knowledge/` | ✅ Yes (`knowledge_items` + unified `faultmaven_kb`) | ✅ Yes | ✅ Yes | ✅ **VERTICAL** | `../data-and-storage/overview.md` Section 5.5.2 |
| `modules/evidence/` | ❌ No (data in Case tables) | ✅ Yes | ✅ Yes | ❌ **DOMAIN SERVICE** | `../data-and-storage/schemas/case-schema.md` Section 4.3 |
| `modules/agent/` | ❌ No (no agent tables) | ✅ Yes | ✅ Yes | ❌ **DOMAIN SERVICE** | No agent tables in schema |
| `modules/preprocessing/` | ❌ No (operates on Evidence data) | ✅ Yes | ✅ Yes | ❌ **DOMAIN SERVICE** | Data classification, extraction, chunking |
| `modules/report/` | ❌ No (data in Case `reports` table) | ✅ Yes | ✅ Yes | ❌ **DOMAIN SERVICE** | `../data-and-storage/schemas/case-schema.md` Section 4.9, `../data-and-storage/overview.md` Section 8 (TD-001 complete) |
| `infrastructure/llm/` | ❌ No | ❌ No | ❌ No | ❌ **HORIZONTAL** | Provider abstraction |
| `infrastructure/storage/` | ❌ No | ❌ No | ❌ No | ❌ **HORIZONTAL** | Provider abstraction |
| `infrastructure/logging/` | ❌ No | ❌ No | ❌ No | ❌ **HORIZONTAL** | Cross-cutting concern |
| `infrastructure/observability/` | ❌ No | ❌ No | ❌ No | ❌ **HORIZONTAL** | Cross-cutting concern |
| `core/investigation/` | ❌ No* | ✅ Yes | ⚠️ Shared | ⚠️ **SHARED** | Logic only, data in Case |

*Data owned by `modules/case/`, not `core/`
**Result**: Only **3 modules** are truly vertical (Auth, Case, Knowledge). Evidence, Agent, and Report are domain services.

---

## Key Takeaways

1. ✅ **All vertical modules are structural peers** - same structure, equal status
2. ✅ **Vertical modules CAN have dependencies** - via contracts, not direct imports
3. ✅ **High fan-in doesn't change categorization** - criteria matter, not usage
4. ✅ **Business dependencies keep modules vertical** - technical dependencies indicate horizontal
5. ✅ **High fan-in requires better dependency management** - contracts, DI, interface segregation

**Bottom Line**: A module used by all others remains vertical if it meets the 3 criteria. High fan-in changes dependency management strategy, not categorization.

---

## Recommended Module Organization

### ✅ Vertical Modules (Domain Boundaries)

These modules implement **business capabilities** and should have full vertical slicing with contracts:

#### 1. **`modules/auth/`** ✅ **VERTICAL** (Schema-Verified)
- **Business Logic**: User authentication, authorization, RBAC, session management
- **Owns Data**: `users` and `organizations` PostgreSQL tables (see `../data-and-storage/schemas/case-schema.md` Section 4.9)
- **Schema Reference**: Core tables include `users`, `organizations`, `sessions`
- **Cross-Module Usage**: All modules depend on auth for access control
- **Future Extraction**: Could become identity microservice
- **Structure**:
  ```
  modules/auth/
  ├── contracts.py          # IAuthService, IUserQuery, SessionDTO, etc.
  ├── api/                  # Auth endpoints
  ├── domain/               # Auth business logic
  └── infrastructure/       # Auth persistence (users, organizations tables)
  ```

#### 2. **`modules/case/`** ✅ **VERTICAL** (Schema-Verified)
- **Business Logic**: Case lifecycle, investigation sessions, case state management
- **Owns Data**: the case-domain PostgreSQL tables — `cases`, `evidence`, `hypotheses`, `solutions`, `case_messages`, `uploaded_files`, `case_actions` (audit table; Python alias `CaseStatusTransitionModel`), `case_checkpoints`, `reports`, and related high-cardinality tables
- **Schema Reference**: See `../data-and-storage/schemas/case-schema.md` Section 4.1 (hybrid schema)
- **Key Insight**: Evidence table has `FK → cases(case_id) ON DELETE CASCADE` - evidence is owned by Case module
- **Cross-Module Usage**: Evidence, Agent, Report services operate on Case data
- **Future Extraction**: Core case management microservice
- **Structure**:
  ```
  modules/case/
  ├── contracts.py          # ICaseRepository, CaseDTO, owned model re-exports
  ├── api/                  # Case endpoints
  ├── domain/               # Case business logic
  └── infrastructure/       # Case persistence (hybrid schema)
  ```

#### 3. **`modules/knowledge/`** ✅ **VERTICAL** (Schema-Verified)
- **Business Logic**: Knowledge base management, RAG operations, document indexing
- **Owns Data**: `knowledge_items` + `knowledge_suggestions` PostgreSQL tables + the unified `faultmaven_kb` ChromaDB collection (scope/owner_id/team_id metadata filtering — no per-user collections)
- **Schema Reference**: See `../data-and-storage/overview.md` Section 5.5.2 and `004_kb_sharing_infrastructure.sql`
- **Cross-Module Usage**: Agent service uses knowledge for RAG
- **Future Extraction**: Knowledge management microservice
- **Structure**:
  ```
  modules/knowledge/
  ├── contracts.py          # IKnowledgeService, IKnowledgeQuery
  ├── api/                  # Knowledge endpoints
  ├── domain/               # Knowledge business logic
  └── infrastructure/       # Knowledge persistence (knowledge_items + faultmaven_kb)
  ```

---

### ⚠️ Domain Services (Business Logic Without Data Ownership)

These modules implement **business logic** but **operate on data owned by other modules**. They are NOT vertical modules because they fail Criterion 1 (Domain Data Ownership).

#### 1. **`modules/evidence/`** ❌ **DOMAIN SERVICE** (Schema-Verified)
- **Business Logic**: Evidence collection, validation, artifact management, preprocessing
- **Data Ownership**: ❌ **NO** - Evidence table is part of Case module's schema
- **Schema Verification**: `evidence` table has `case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE`
- **Reference**: See `../data-and-storage/schemas/case-schema.md` Section 4.3
- **Rationale**: Evidence provides collection logic but stores data in Case-owned tables
- **Structure**: Keep as domain service (business logic only, no persistence ownership)
  ```
  modules/evidence/
  ├── domain/               # Evidence collection, validation, preprocessing logic
  └── api/                  # Evidence endpoints (delegate to Case repository)
  ```

**Note**: Consider merging Evidence domain logic into Case module since evidence is purely operational on Case-owned data.

#### 2. **`modules/agent/`** ❌ **DOMAIN SERVICE** (Schema-Verified)
- **Business Logic**: AI agent orchestration via LangGraph, investigation workflows, milestone-based orchestration
- **Data Ownership**: ❌ **NO** - No `agent_*` tables in schema
- **Schema Verification**: `agent_tool_calls` table (if exists) stores case audit data, not agent state
- **Reference**: See `../data-and-storage/schemas/case-schema.md` Section 4.1 (no agent-owned tables appear in the case schema)
- **Rationale**: Agent orchestrates investigations but all persistent state flows through Case module
- **Structure**: Keep as domain service (LangGraph orchestration, operates on Case data)
  ```
  modules/agent/
  ├── domain/               # LangGraph orchestration, investigation workflows
  ├── tools/                # Agent tools (knowledge_base, web_search, etc.)
  └── api/                  # Agent query endpoints
  ```

**Note**: Agent's LangGraph state is ephemeral/in-memory. All persistent state (investigations, tool calls) is stored in Case module's tables.

#### 3. **`modules/preprocessing/`** ❌ **DOMAIN SERVICE** (Schema-Verified)
- **Business Logic**: Data classification, content extraction (11 extractors), structured chunking
- **Data Ownership**: ❌ **NO** - operates on Evidence data owned by the Case module
- **Schema Verification**: No `preprocessing_*` tables; outputs are written back to `evidence` and `evidence_artifacts`
- **Rationale**: Preprocessing transforms raw uploaded files into searchable artifacts but never owns persistent state
- **Structure**: Domain service (logic only, persistence via Case repository)
  ```
  modules/preprocessing/
  ├── domain/               # Classifiers, extractors, chunker
  └── api/                  # Preprocessing trigger endpoints
  # NO infrastructure/      # Uses Case repository for persistence
  ```

#### 4. **`modules/report/`** ❌ **DOMAIN SERVICE** (Schema-Verified, TD-001 Complete)
- **Business Logic**: Report generation, runbook creation, post-mortem generation
- **Data Ownership**: ❌ **NO** - Reports stored in PostgreSQL `reports` table (owned by Case module, FK to cases)
- **Schema Verification**: See `../data-and-storage/schemas/case-schema.md` Section 4.9 and `../data-and-storage/overview.md` Section 8 - "Storage: PostgreSQL (persistent, FK to cases)" - TD-001 migration complete
- **Rationale**: Report module generates reports but data is owned by Case module (same as Evidence module pattern)
- **Retention**: Persistent for lifetime of case (symmetric to evidence, cascade delete with case)
- **Structure**: Domain service (generates reports, delegates persistence to Case repository)
  ```
  modules/report/
  ├── domain/               # Report generation logic
  └── api/                  # Report endpoints
  # NO infrastructure/      # Uses Case repository for persistence (TD-001 complete)
  ```

**Note**: Reports are investigation **outputs** and persist alongside the case, symmetric to evidence (investigation **inputs**). Both have the same lifecycle tied to the case (TD-001 migration complete).

---

### ❌ Horizontal Layers (Cross-Cutting Infrastructure)

These components provide **technical capabilities** and should remain horizontal:

#### 1. **`infrastructure/llm/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: LLM provider abstraction and routing
- **Why Horizontal**:
  - Used by Agent, Report, Knowledge modules
  - Stateless provider adapters
  - No business logic, just technical integration
- **Structure**: Keep as-is (providers, router, registry)

#### 2. **`infrastructure/storage/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: File storage abstraction (S3, filesystem, Azure Blob)
- **Why Horizontal**:
  - Used by Evidence, Knowledge, Report modules
  - Provider-agnostic abstraction
  - No business logic
- **Structure**: Keep as-is (interfaces, implementations)

#### 3. **`infrastructure/logging/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Structured logging, correlation IDs, log coordination
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (config, coordinator, unified)

#### 4. **`infrastructure/observability/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Tracing, metrics, APM, alerting, SLA, confidence/dashboard services
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: `tracing.py`, `metrics_collector.py`, `apm_metrics.py`, `apm_integration.py`, `alerting.py`, `sla_monitor.py`, `performance_monitoring.py`, `confidence_service.py`, `dashboard_service.py`, `metrics_exporters/`

#### 5. **`infrastructure/health/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Health checks, component monitoring, SLA tracking
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (component_monitor, sla_tracker)

#### 6. **`infrastructure/protection/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Client protection, rate limiting, anomaly detection
- **Why Horizontal**:
  - Used by ALL API endpoints
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (rate_limiter, anomaly_detector, protection_coordinator)

#### 7. **`infrastructure/security/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: PII redaction, security assessment, data sanitization
- **Why Horizontal**:
  - Used by multiple modules (Evidence, Report, Agent)
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (redaction, security_assessment)

#### 8. **`infrastructure/persistence/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Database connection management, session management
- **Why Horizontal**:
  - Provides database connections to all modules
  - No business logic, just connection pooling
  - **Note**: Each module's repository uses these connections but owns its tables
- **Structure**: Keep as-is (connection management, session stores)

#### 9. **`infrastructure/caching/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Intelligent caching strategies
- **Why Horizontal**:
  - Used by multiple modules
  - Pure technical capability
  - No business logic
- **Structure**: Keep as-is (intelligent_cache)

#### 10. **`infrastructure/jobs/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Background job execution framework
- **Why Horizontal**:
  - Used by multiple modules for async tasks
  - Pure technical capability
  - No business logic
- **Structure**: Keep as-is (job_service)

#### 11. **`infrastructure/tasks/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Task queue and execution
- **Why Horizontal**:
  - Used by multiple modules
  - Pure technical capability
  - No business logic
- **Structure**: Keep as-is

---

### ⚠️ Shared/Utility Components

These should remain in shared locations:

#### 1. **`core/`** ⚠️ **SHARED DOMAIN LOGIC**
- **Purpose**: Shared domain algorithms, response parsing, preprocessing
- **Why Shared**:
  - Used by multiple modules but contains domain logic
  - Not a full business domain itself
  - **Recommendation**: Keep as-is, but consider moving module-specific logic into modules
- **Structure**: Keep as-is for now, refactor incrementally

#### 2. **`config/`** ⚠️ **SHARED CONFIGURATION**
- **Purpose**: Settings, feature flags, presets
- **Why Shared**:
  - Used by ALL components
  - Pure configuration, no business logic
- **Structure**: Keep as-is

#### 3. **`container/`** ⚠️ **SHARED DI CONTAINER**
- **Purpose**: Dependency injection wiring
- **Why Shared**:
  - Used by composition root (main.py)
  - Pure infrastructure
- **Structure**: Keep as-is

#### 4. **`utils/`** ⚠️ **SHARED UTILITIES**
- **Purpose**: Cross-cutting utilities (serialization, validation, etc.)
- **Why Shared**:
  - Used by multiple modules
  - Pure utilities, no business logic
- **Structure**: Keep as-is

#### 5. **`exceptions.py`** ⚠️ **SHARED EXCEPTIONS**
- **Purpose**: Base exception classes
- **Why Shared**:
  - Used by all modules
  - Pure infrastructure
- **Structure**: Keep as-is (each module defines its own domain exceptions)

---

## Recommended Architecture Structure

```
faultmaven/
├── modules/                          # Business domains and services
│   │
│   ├── auth/                         # ✅ Vertical (3 modules own domain data)
│   │   ├── contracts.py              # IAuthService, IUserQuery, etc.
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/           # users, organizations tables
│   │
│   ├── case/                         # ✅ Vertical (owns case-domain tables including evidence and reports)
│   │   ├── contracts.py              # ICaseRepository, CaseDTO
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/           # cases, evidence, hypotheses, solutions, etc.
│   │
│   ├── knowledge/                    # ✅ Vertical (owns knowledge_items + knowledge_suggestions + faultmaven_kb)
│   │   ├── contracts.py              # IKnowledgeService, IKnowledgeQuery
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/           # knowledge_items + knowledge_suggestions tables + faultmaven_kb collection
│   │
│   ├── evidence/                     # ❌ Domain Service (no data ownership)
│   │   ├── domain/                   # Evidence collection, validation logic
│   │   └── api/                      # Delegates persistence to Case module
│   │   # NO infrastructure/          # Uses Case repository via contracts
│   │
│   ├── agent/                        # ❌ Domain Service (no data ownership)
│   │   ├── domain/                   # LangGraph orchestration, workflows
│   │   ├── tools/                    # Agent tools (knowledge_base, web_search)
│   │   └── api/                      # Orchestrates via Case contracts
│   │   # NO infrastructure/          # All state flows through Case module
│   │
│   ├── preprocessing/                # ❌ Domain Service (no data ownership)
│   │   ├── domain/                   # Data classification, extraction (11 extractors), chunking
│   │   └── api/                      # Operates on Evidence data
│   │   # NO infrastructure/          # Uses Case repository for persistence
│   │
│   └── report/                       # ❌ Domain Service (TD-001 complete)
│       ├── domain/                   # Report generation logic
│       └── api/                      # Report endpoints
│   # NO infrastructure/              # Uses Case repository for persistence
│
├── infrastructure/                   # Horizontal cross-cutting infrastructure
│   ├── llm/                          # ❌ Horizontal
│   ├── vector/                       # ❌ Horizontal
│   ├── storage/                      # ❌ Horizontal
│   ├── logging/                      # ❌ Horizontal
│   ├── observability/                # ❌ Horizontal
│   ├── monitoring/                   # ❌ Horizontal
│   ├── health/                       # ❌ Horizontal
│   ├── protection/                   # ❌ Horizontal
│   ├── security/                     # ❌ Horizontal
│   ├── persistence/                  # ❌ Horizontal
│   ├── caching/                      # ❌ Horizontal
│   ├── jobs/                         # ❌ Horizontal
│   └── tasks/                        # ❌ Horizontal
│
├── core/                             # ⚠️ Shared domain logic
│   ├── investigation/
│   ├── knowledge/
│   ├── preprocessing/
│   └── processing/
│
├── config/                           # ⚠️ Shared configuration
│   ├── settings.py
│   ├── feature_flags.py
│   └── presets.py
│
├── container/                        # ⚠️ Shared DI container
│   ├── registry.py
│   └── providers/
│
├── utils/                            # ⚠️ Shared utilities
│
├── exceptions.py                     # ⚠️ Shared base exceptions
│
└── main.py                           # Composition root
```

---

## Key Design Decisions

### 1. **Agent Tools Location**

**Decision**: Keep `modules/agent/tools/` within the agent module.

**Rationale**:
- Tools are tightly coupled to agent orchestration
- Tools implement agent-specific interfaces (BaseTool)
- Moving to horizontal would break encapsulation

**Alternative Considered**: `infrastructure/tools/` - Rejected because tools are domain-specific to agent workflows.

### 2. **Knowledge Vector Store**

**Decision**: Knowledge module uses `infrastructure/knowledge/` (horizontal) for vector store abstraction.

**Rationale**:
- Vector stores are provider-agnostic infrastructure
- Knowledge module owns the business logic (indexing, RAG)
- Infrastructure layer provides the technical capability

**Pattern**: Module owns business logic, infrastructure provides technical capability.

### 3. **Evidence Storage**

**Decision**: Evidence module uses `infrastructure/storage/` (horizontal) for file storage.

**Rationale**:
- Storage backends are provider-agnostic (S3, filesystem, etc.)
- Evidence module owns the business logic (validation, metadata)
- Infrastructure layer provides the technical capability

**Pattern**: Same as knowledge vector store.

### 4. **Report Generation**

**Decision**: Report module is vertical, uses `infrastructure/llm/` (horizontal) for LLM calls.

**Rationale**:
- Report generation has business logic (templates, versioning, recommendations)
- LLM providers are infrastructure abstraction
- Report module orchestrates LLM calls for business purpose

**Pattern**: Business domain orchestrates infrastructure capabilities.

---

## Migration Strategy

### Phase 1: Establish Contracts (Current)
- ✅ Create `contracts.py` for each vertical module
- ✅ Define public interfaces (DTOs, protocols)
- ✅ Document cross-module dependencies

### Phase 2: Enforce Boundaries
- ⏳ Add import-linter rules to prevent cross-module domain imports
- ⏳ Migrate existing cross-module calls to use contracts
- ⏳ Add integration tests for contract compliance

### Phase 2.5: Restructure Domain Services
- ✅ Remove `contracts.py` from Evidence, Agent, Report modules (they don't own data)
- ✅ Remove `infrastructure/` from Evidence, Agent modules (use Case repository)
- ✅ Remove Report `infrastructure/` - TD-001 migration complete (now uses Case repository)
- ✅ Update Evidence, Agent, Report to use Case module's `ICaseRepository` contract
- ⏳ Add import-linter rules for Domain Service boundaries (prevent vertical modules from importing domain service internals)
- ⏳ Update composition root (`main.py`) to wire Domain Services with Case contracts
- ⏳ Update API routes to use injected Domain Service instances

### Phase 3: Database Boundaries
- ⏳ Prefix all tables with module name (`case_`, `auth_`, etc.)
- ⏳ Remove cross-module JOINs
- ⏳ Add bulk query methods to contracts (prevent N+1)

### Phase 4: Refactor Legacy Services
- ⏳ Move business logic from `services/` into appropriate modules
- ⏳ Keep only cross-cutting services in `services/` (if any)
- ⏳ Update composition root to wire modules via contracts

---

## Benefits of This Organization

### 1. **Clear Separation of Concerns**
- Business domains are self-contained
- Infrastructure is reusable and provider-agnostic
- Shared utilities don't pollute domain logic

### 2. **Independent Development**
- Teams can work on modules in parallel
- Changes to one module don't affect others (via contracts)
- Infrastructure changes don't require domain changes

### 3. **Testability**
- Domain modules can be tested in isolation
- Infrastructure can be mocked via interfaces
- Contracts enable integration testing

### 4. **Future Microservice Extraction**
- Vertical modules can become microservices
- Horizontal infrastructure can be shared services
- Contracts become service APIs

### 5. **Flexibility**
- Infrastructure providers can be swapped without domain changes
- New business domains can be added as vertical modules
- Cross-cutting concerns remain centralized

---

## Anti-Patterns to Avoid

### ❌ **Don't Make Infrastructure Vertical**
```python
# ❌ WRONG: LLM as a vertical module
modules/llm/
├── contracts.py
├── api/
├── domain/
└── infrastructure/

# ✅ RIGHT: LLM as horizontal infrastructure
infrastructure/llm/
├── providers/
├── router.py
└── registry.py
```

**Why**: LLM providers are technical capabilities, not business domains.

### ❌ **Don't Make Utilities Vertical**
```python
# ❌ WRONG: Logging as a vertical module
modules/logging/
├── contracts.py
├── api/
├── domain/
└── infrastructure/

# ✅ RIGHT: Logging as horizontal infrastructure
infrastructure/logging/
├── config.py
├── coordinator.py
└── unified.py
```

**Why**: Logging is a cross-cutting concern, not a business domain.

### ❌ **Don't Mix Business Logic in Infrastructure**
```python
# ❌ WRONG: Business logic in infrastructure
infrastructure/knowledge/knowledge_indexer.py  # Business logic!

# ✅ RIGHT: Infrastructure provides capability, module uses it
infrastructure/knowledge/vector_store.py       # Technical capability
modules/knowledge/domain/services/indexing_service.py  # Business logic
```

**Why**: Infrastructure should be provider-agnostic and stateless.

---

## Summary

| Component | Organization | Reason |
|-----------|-------------|--------|
| `modules/auth/` | ✅ Vertical | Owns domain data (users, organizations tables) |
| `modules/case/` | ✅ Vertical | Owns case-domain data (cases, evidence, hypotheses, solutions, messages, reports, and related tables) |
| `modules/knowledge/` | ✅ Vertical | Owns domain data (`knowledge_items` + `knowledge_suggestions` + the unified `faultmaven_kb` ChromaDB collection) |
| `modules/evidence/` | ❌ Domain Service | Business logic only; data owned by Case module |
| `modules/agent/` | ❌ Domain Service | Orchestration logic; no persistent state ownership |
| `modules/preprocessing/` | ❌ Domain Service | Data classification, extraction, chunking; operates on Evidence data |
| `modules/report/` | ❌ Domain Service | Generation logic; data owned by Case module (TD-001 complete) |
| `infrastructure/llm/` | ❌ Horizontal | Provider abstraction, no business logic |
| `infrastructure/storage/` | ❌ Horizontal | Provider abstraction, no business logic |
| `infrastructure/logging/` | ❌ Horizontal | Cross-cutting concern |
| `infrastructure/observability/` | ❌ Horizontal | Cross-cutting concern (tracing, metrics, APM, alerting, SLA, confidence/dashboard) |
| `infrastructure/health/` | ❌ Horizontal | Cross-cutting concern |
| `infrastructure/protection/` | ❌ Horizontal | Cross-cutting concern |
| `infrastructure/security/` | ❌ Horizontal | Cross-cutting concern |
| `infrastructure/persistence/` | ❌ Horizontal | Connection management |
| `infrastructure/caching/` | ❌ Horizontal | Technical capability |
| `infrastructure/jobs/` | ❌ Horizontal | Technical capability |
| `core/` | ⚠️ Shared | Shared domain logic (refactor incrementally) |
| `config/` | ⚠️ Shared | Configuration |
| `container/` | ⚠️ Shared | DI container |
| `utils/` | ⚠️ Shared | Utilities |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-01-09 | Initial recommendations with 6 vertical modules |
| 2.0 | 2026-01-09 | **Schema-verified revision** - Only 3 vertical modules (Case, Auth, Knowledge) after reviewing `../data-and-storage/schemas/case-schema.md` and `../data-and-storage/overview.md`. Evidence, Agent, and Report reclassified as Domain Services. |
| 2.1 | 2026-01-10 | **Domain Service structural guidance** - Added detailed implementation patterns, structural options, boundary enforcement strategies, and migration steps (Phase 2.5) for Evidence, Agent, Report modules. Consolidated vertical-vs-layer structuring guidance into this document. |
| 2.2 | 2026-04-18 | **Internal consistency pass** - Aligned table-naming examples with the live schema (unprefixed primary tables; semantic prefixes only for sub-entities). Standardized cross-module Case access on `ICaseRepository`. Made `modules/preprocessing/` first-class in the schema-verified classification table. Added `knowledge_suggestions` to the Knowledge row of every summary table. Fixed enumeration gap in horizontal-layer list. |

### Key Changes in v2.0

| Module | v1.0 Classification | v2.0 Classification | Reason |
|--------|---------------------|---------------------|--------|
| Evidence | ✅ Vertical | ❌ Domain Service | Evidence table has FK to cases - part of Case module's schema |
| Agent | ✅ Vertical | ❌ Domain Service | No agent_* tables; agent_tool_calls is case audit data, not agent state |
| Report | ✅ Vertical | ❌ Domain Service | Reports table has FK to cases - part of Case module's schema (TD-001 complete) |
| Case | ✅ Vertical | ✅ Vertical | Owns case-domain tables including evidence and reports (schema verified, TD-001 complete) |
| Auth | ✅ Vertical | ✅ Vertical | Owns users and organizations tables (schema verified) |
| Knowledge | ✅ Vertical | ✅ Vertical | Owns knowledge_items + faultmaven_kb collection (schema verified) |

**Impact**: Document now accurately reflects actual schema ownership, not assumptions. This prevents architectural misalignment and clarifies that only 3 modules truly own domain data.

---

## Technical Debt

### TD-001: Migrate Report Storage from Ephemeral to Persistent

**Status**: ✅ **COMPLETE** (2026-01-10)
**Priority**: Medium
**Related Modules**: Report (domain service), Case (vertical module)

#### Completed Migration
- ✅ Reports now stored in PostgreSQL `reports` table with FK to `cases(case_id) ON DELETE CASCADE`
- ✅ Reports persist for the lifetime of the case (symmetric to evidence)
- ✅ Report module remains Domain Service, data owned by Case module
- ✅ All legacy Redis + ChromaDB code removed

#### Implementation Completed

1. **Schema Change** ✅
   - ✅ Added `reports` table to Case module's schema (`005_add_reports_table.sql`)
   - ✅ FK constraint: `case_id REFERENCES cases(case_id) ON DELETE CASCADE`
   - ✅ Updated Case module table count from 10 to 11

2. **Storage Design Update** ✅
   - ✅ Updated Report storage section in `../data-and-storage/overview.md` to PostgreSQL (persistent)
   - ✅ Removed all TTL-based expiration references
   - ✅ Updated `../data-and-storage/schemas/case-schema.md` with reports table schema

3. **Code Migration** ✅
   - ✅ Created `reports` table migration script (`005_add_reports_table.sql`)
   - ✅ Updated Report service to use Case repository for persistence
   - ✅ Added report methods (`add_report`, `get_report`, `get_reports`, `update_report`, `delete_report`) to `CaseRepository`
   - ✅ Implemented in both `InMemoryCaseRepository` and `PostgreSQLHybridCaseRepository`
   - ✅ Removed all legacy `IReportStore` interface and `RedisReportStore` implementation

4. **Documentation Update** ✅
   - ✅ Updated this document's Report classification rationale
   - ✅ Updated schema verification references
   - ✅ All design documents reflect PostgreSQL storage

#### Acceptance Criteria (All Complete)
- ✅ `reports` table exists in PostgreSQL schema with FK to cases
- ✅ Reports persist permanently (no TTL expiration)
- ✅ Reports deleted when parent case is deleted (CASCADE)
- ✅ Report service uses `CaseRepository` for all persistence
- ✅ Schema documentation updated in `../data-and-storage/schemas/case-schema.md` and `../data-and-storage/overview.md`
- ✅ All legacy code removed (`IReportStore`, `RedisReportStore`)

**Result**: Reports are now first-class persistent entities stored in PostgreSQL via Case module's repository, with the same lifecycle as evidence (investigation inputs). Report module remains a Domain Service that generates reports but delegates persistence to Case module.

---

**Document Owner**: Engineering Leadership
**Status**: Schema-Verified Active Recommendation
**Last Updated**: 2026-04-18
**Schema Verification**: Verified against the live schema via `../data-and-storage/schemas/case-schema.md`, `../data-and-storage/er-diagram.md`, and `faultmaven/infrastructure/persistence/models.py`.

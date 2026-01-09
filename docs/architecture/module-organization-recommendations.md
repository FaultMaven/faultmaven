# Module Organization Recommendations: Vertical vs Horizontal

**Version**: 1.0  
**Date**: 2026-01-09  
**Status**: Recommendation  
**Related**: [Architectural Design Principles](architectural-design-principles.md)

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

---

## Decision Framework

### Minimum Criteria for Vertical Modules

A module is **vertical** (business domain) if and only if it meets **ALL THREE** of these criteria:

#### Criterion 1: Domain Data Ownership ✅
- **Requirement**: Module owns database tables/entities that represent **business entities** (not just technical state)
- **Test**: Does the module have tables prefixed with its name (e.g., `case_cases`, `auth_users`)?
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
- Owns tables: `auth_users`, `auth_sessions`, `auth_tokens`, `auth_organizations`, `auth_teams`
- These represent business entities (users, organizations), not technical state
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
- Owns tables: `knowledge_items`, `knowledge_embeddings`, `knowledge_metadata`
- These represent business entities (documents, knowledge items)
- **Result**: PASS

**Criterion 2: Business Logic Implementation** ✅
- Implements business rules: "Documents must be indexed before search", "Embeddings must match document version", "Knowledge items belong to organizations"
- Contains domain workflows (ingestion, indexing, retrieval)
- **Result**: PASS

**Criterion 3: Domain Capability** ✅
- Business description: "Manages knowledge base, document indexing, and RAG operations"
- Can be understood as a business capability
- **Result**: PASS

**Decision**: ✅ **VERTICAL** (all 3 criteria met)

**Note**: Even though `knowledge/` uses `infrastructure/vector/` (horizontal), it remains vertical because it owns the business logic and data.

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

- [ ] Module has database tables prefixed with module name (e.g., `case_cases`)
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

1. ✅ **Meets All 3 Criteria**: Owns domain data (`auth_users`), implements business logic (authentication rules), represents domain capability (user management)
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

## Summary Table

| Module | Domain Data? | Business Logic? | Domain Capability? | Category | Fan-In |
|--------|-------------|-----------------|-------------------|----------|--------|
| `modules/auth/` | ✅ Yes | ✅ Yes | ✅ Yes | ✅ VERTICAL | High* |
| `modules/case/` | ✅ Yes | ✅ Yes | ✅ Yes | ✅ VERTICAL | Medium |
| `modules/evidence/` | ✅ Yes | ✅ Yes | ✅ Yes | ✅ VERTICAL | Low |
| `modules/knowledge/` | ✅ Yes | ✅ Yes | ✅ Yes | ✅ VERTICAL | Low |
| `modules/agent/` | ✅ Yes | ✅ Yes | ✅ Yes | ✅ VERTICAL | Medium |
| `modules/report/` | ✅ Yes | ✅ Yes | ✅ Yes | ✅ VERTICAL | Low |
| `infrastructure/llm/` | ❌ No | ❌ No | ❌ No | ❌ HORIZONTAL | High |
| `infrastructure/vector/` | ❌ No | ❌ No | ❌ No | ❌ HORIZONTAL | Low |
| `infrastructure/storage/` | ❌ No | ❌ No | ❌ No | ❌ HORIZONTAL | Medium |
| `infrastructure/logging/` | ❌ No | ❌ No | ❌ No | ❌ HORIZONTAL | Very High |
| `infrastructure/observability/` | ❌ No | ❌ No | ❌ No | ❌ HORIZONTAL | Very High |
| `core/investigation/` | ❌ No* | ✅ Yes | ⚠️ Shared | ⚠️ SHARED | Medium |

*Data owned by `modules/case/`, not `core/`  
*High fan-in doesn't change categorization - auth remains vertical

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

#### 1. **`modules/auth/`** ✅ **KEEP VERTICAL**
- **Business Logic**: User authentication, authorization, RBAC, session management
- **Owns Data**: `auth_users`, `auth_sessions`, `auth_tokens`, `auth_organizations`, `auth_teams`
- **Cross-Module Usage**: All modules depend on auth for access control
- **Future Extraction**: Could become identity microservice
- **Structure**:
  ```
  modules/auth/
  ├── contracts.py          # IAuthService, IUserQuery, SessionDTO, etc.
  ├── api/                  # Auth endpoints
  ├── domain/               # Auth business logic
  └── infrastructure/       # Auth persistence
  ```

#### 2. **`modules/case/`** ✅ **KEEP VERTICAL**
- **Business Logic**: Case lifecycle, investigation sessions, case status management
- **Owns Data**: `case_cases`, `case_investigations`, `case_sessions`
- **Cross-Module Usage**: Report, Evidence, Agent modules depend on cases
- **Future Extraction**: Core case management microservice
- **Structure**:
  ```
  modules/case/
  ├── contracts.py          # ICaseQuery, ICaseService, CaseDTO
  ├── api/                  # Case endpoints
  ├── domain/               # Case business logic
  └── infrastructure/       # Case persistence
  ```

#### 3. **`modules/evidence/`** ✅ **KEEP VERTICAL**
- **Business Logic**: Evidence collection, validation, artifact management
- **Owns Data**: `evidence_artifacts`, `evidence_metadata`
- **Cross-Module Usage**: Case and Agent modules use evidence
- **Future Extraction**: Evidence management microservice
- **Structure**:
  ```
  modules/evidence/
  ├── contracts.py          # IEvidenceService, EvidenceDTO
  ├── api/                  # Evidence endpoints
  ├── domain/               # Evidence business logic
  └── infrastructure/       # Evidence persistence
  ```

#### 4. **`modules/knowledge/`** ✅ **KEEP VERTICAL**
- **Business Logic**: Knowledge base management, RAG operations, document indexing
- **Owns Data**: `knowledge_items`, `knowledge_embeddings`, `knowledge_metadata`
- **Cross-Module Usage**: Agent module uses knowledge for RAG
- **Future Extraction**: Knowledge management microservice
- **Structure**:
  ```
  modules/knowledge/
  ├── contracts.py          # IKnowledgeService, IKnowledgeQuery
  ├── api/                  # Knowledge endpoints
  ├── domain/               # Knowledge business logic
  └── infrastructure/       # Knowledge persistence (vector store)
  ```

#### 5. **`modules/agent/`** ✅ **KEEP VERTICAL**
- **Business Logic**: AI agent orchestration, investigation workflows, OODA loops
- **Owns Data**: `agent_investigations`, `agent_state`, `agent_memory`
- **Cross-Module Usage**: Orchestrates Case, Evidence, Knowledge modules
- **Future Extraction**: AI agent microservice
- **Structure**:
  ```
  modules/agent/
  ├── contracts.py          # IAgentService, InvestigationDTO
  ├── api/                  # Agent query endpoints
  ├── domain/               # Agent orchestration logic
  ├── infrastructure/       # Agent state persistence
  └── tools/                # Agent tools (knowledge_base, web_search, etc.)
  ```

#### 6. **`modules/report/`** ✅ **KEEP VERTICAL**
- **Business Logic**: Report generation, runbook creation, post-mortem generation
- **Owns Data**: `report_reports`, `report_versions`, `report_metadata`
- **Cross-Module Usage**: Depends on Case module for case data
- **Future Extraction**: Report generation microservice
- **Structure**:
  ```
  modules/report/
  ├── contracts.py          # IReportService, ReportDTO
  ├── api/                  # Report endpoints
  ├── domain/               # Report generation logic
  └── infrastructure/       # Report persistence
  ```

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

#### 2. **`infrastructure/vector/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Vector store abstraction (ChromaDB, InMemory, etc.)
- **Why Horizontal**:
  - Used by Knowledge module (and potentially others)
  - Provider-agnostic abstraction
  - No business logic
- **Structure**: Keep as-is (interfaces, implementations)

#### 3. **`infrastructure/storage/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: File storage abstraction (S3, filesystem, Azure Blob)
- **Why Horizontal**:
  - Used by Evidence, Knowledge, Report modules
  - Provider-agnostic abstraction
  - No business logic
- **Structure**: Keep as-is (interfaces, implementations)

#### 4. **`infrastructure/logging/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Structured logging, correlation IDs, log coordination
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (config, coordinator, unified)

#### 5. **`infrastructure/observability/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Tracing, metrics, performance monitoring
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (tracing, metrics_collector, performance_monitoring)

#### 6. **`infrastructure/monitoring/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: APM integration, alerting, SLA tracking
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (metrics_collector, alerting, apm_integration)

#### 7. **`infrastructure/health/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Health checks, component monitoring, SLA tracking
- **Why Horizontal**:
  - Used by ALL modules
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (component_monitor, sla_tracker)

#### 8. **`infrastructure/protection/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Client protection, rate limiting, anomaly detection
- **Why Horizontal**:
  - Used by ALL API endpoints
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (rate_limiter, anomaly_detector, protection_coordinator)

#### 9. **`infrastructure/security/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: PII redaction, security assessment, data sanitization
- **Why Horizontal**:
  - Used by multiple modules (Evidence, Report, Agent)
  - Pure cross-cutting concern
  - No business logic
- **Structure**: Keep as-is (redaction, security_assessment)

#### 10. **`infrastructure/persistence/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Database connection management, session management
- **Why Horizontal**:
  - Provides database connections to all modules
  - No business logic, just connection pooling
  - **Note**: Each module's repository uses these connections but owns its tables
- **Structure**: Keep as-is (connection management, session stores)

#### 11. **`infrastructure/caching/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Intelligent caching strategies
- **Why Horizontal**:
  - Used by multiple modules
  - Pure technical capability
  - No business logic
- **Structure**: Keep as-is (intelligent_cache)

#### 12. **`infrastructure/jobs/`** ❌ **KEEP HORIZONTAL**
- **Purpose**: Background job execution framework
- **Why Horizontal**:
  - Used by multiple modules for async tasks
  - Pure technical capability
  - No business logic
- **Structure**: Keep as-is (job_service)

#### 13. **`infrastructure/tasks/`** ❌ **KEEP HORIZONTAL**
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
├── modules/                          # Vertical business domains
│   ├── auth/                         # ✅ Vertical
│   │   ├── contracts.py
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/
│   │
│   ├── case/                         # ✅ Vertical
│   │   ├── contracts.py
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/
│   │
│   ├── evidence/                     # ✅ Vertical
│   │   ├── contracts.py
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/
│   │
│   ├── knowledge/                    # ✅ Vertical
│   │   ├── contracts.py
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/
│   │
│   ├── agent/                        # ✅ Vertical
│   │   ├── contracts.py
│   │   ├── api/
│   │   ├── domain/
│   │   ├── infrastructure/
│   │   └── tools/                    # Agent tools
│   │
│   └── report/                       # ✅ Vertical
│       ├── contracts.py
│       ├── api/
│       ├── domain/
│       └── infrastructure/
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

**Decision**: Knowledge module uses `infrastructure/vector/` (horizontal) for vector store abstraction.

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
infrastructure/vector/knowledge_indexer.py  # Business logic!

# ✅ RIGHT: Infrastructure provides capability, module uses it
infrastructure/vector/vector_store.py       # Technical capability
modules/knowledge/domain/services/indexing_service.py  # Business logic
```

**Why**: Infrastructure should be provider-agnostic and stateless.

---

## Summary

| Component | Organization | Reason |
|-----------|-------------|--------|
| `modules/auth/` | ✅ Vertical | Business domain with own data |
| `modules/case/` | ✅ Vertical | Business domain with own data |
| `modules/evidence/` | ✅ Vertical | Business domain with own data |
| `modules/knowledge/` | ✅ Vertical | Business domain with own data |
| `modules/agent/` | ✅ Vertical | Business domain with own data |
| `modules/report/` | ✅ Vertical | Business domain with own data |
| `infrastructure/llm/` | ❌ Horizontal | Provider abstraction, no business logic |
| `infrastructure/vector/` | ❌ Horizontal | Provider abstraction, no business logic |
| `infrastructure/storage/` | ❌ Horizontal | Provider abstraction, no business logic |
| `infrastructure/logging/` | ❌ Horizontal | Cross-cutting concern |
| `infrastructure/observability/` | ❌ Horizontal | Cross-cutting concern |
| `infrastructure/monitoring/` | ❌ Horizontal | Cross-cutting concern |
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

**Document Owner**: Engineering Leadership  
**Status**: Active Recommendation  
**Last Updated**: 2026-01-09

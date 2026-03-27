# Vertical vs Layer Structuring: Purpose and Implications

**Version**: 1.0
**Date**: 2026-01-09
**Purpose**: Clarify what "vertical" and "layer" structuring mean, their purposes, and whether restructuring Evidence/Agent/Report achieves the same goals

---

## Executive Summary

**Question**: Evidence, Agent, and Report are being reclassified as non-vertical. What does this mean structurally? Do they go back to horizontal "layers"? What is the purpose of each approach? Can we still achieve the purpose?

**Answer**: This document explains both structuring approaches, their purposes, and practical implications for Evidence, Agent, and Report modules.

---

## Understanding the Two Structuring Approaches

### 1. Vertical Structuring (Domain Modules)

**What it means**:
- Code is organized **by business domain** (e.g., `modules/case/`, `modules/auth/`)
- Each vertical module is **self-contained** with its own:
  - `contracts.py` - Public interfaces (what other modules can import)
  - `api/` - HTTP endpoints for this domain
  - `domain/` - Business logic (PRIVATE - other modules can't import)
  - `infrastructure/` - Persistence layer (PRIVATE - other modules can't import)

**Structure Example**:
```
modules/case/
├── contracts.py          # Public: ICaseService, CaseDTO
├── api/
│   └── routes.py         # POST /cases, GET /cases/{id}
├── domain/
│   ├── models.py         # Case, Investigation (PRIVATE)
│   └── services/
│       └── case_service.py  # Business logic (PRIVATE)
└── infrastructure/
    └── case_repository.py   # PostgreSQL persistence (PRIVATE)
```

**Purpose**:
1. **Domain Cohesion**: All code related to a business capability is in one place
2. **Boundary Enforcement**: Other modules can ONLY import from `contracts.py`, not internal code
3. **Independent Development**: Teams can work on domains in parallel
4. **Future Extraction**: Can become microservices (each module is already self-contained)
5. **Data Ownership**: Module owns its database tables and persistence logic

**When to use**: Only for modules that **own domain data** (database tables) - see [Module Organization Design](module-organization-recommendations.md) criteria.

---

### 2. Layer Structuring (Horizontal Layers)

**What it means**:
- Code is organized **by technical function** (API, Services, Core, Infrastructure)
- All API endpoints together, all services together, all infrastructure together
- **Cross-cutting**: Same layer type used by multiple domains

**Structure Example**:
```
faultmaven/
├── api/v1/routes/
│   ├── agent.py          # Agent endpoints
│   ├── evidence.py       # Evidence endpoints
│   └── case.py           # Case endpoints
│
├── services/
│   ├── agent_service.py  # Agent business logic
│   ├── evidence_service.py  # Evidence business logic
│   └── case_service.py   # Case business logic
│
├── core/
│   ├── investigation/    # Shared investigation logic
│   └── processing/       # Shared processing logic
│
└── infrastructure/
    ├── persistence/      # All repositories together
    └── llm/              # All LLM providers together
```

**Purpose**:
1. **Technical Separation**: Clear separation by technical concern (HTTP, business logic, data access)
2. **Reusability**: Infrastructure can be shared across domains
3. **Simpler Structure**: Less duplication of technical layers
4. **Traditional Pattern**: Common in layered architectures

**When to use**:
- Cross-cutting infrastructure (logging, LLM, storage)
- Services that don't own domain data
- Shared utilities and core logic

---

## The Key Difference

| Aspect | Vertical Structuring | Layer Structuring |
|--------|---------------------|-------------------|
| **Organization** | By business domain | By technical function |
| **Boundaries** | Module boundaries (contracts.py) | Layer boundaries (API → Service → Infrastructure) |
| **Data Ownership** | Module owns its tables | Shared infrastructure |
| **Purpose** | Domain isolation, microservice-ready | Technical separation, simpler structure |
| **Example** | `modules/case/` (owns case tables) | `services/evidence_service.py` (no tables) |

---

## Current State: Evidence, Agent, Report

### Current Structure (Vertical - Incorrect)

```
modules/evidence/
├── contracts.py          # ❌ Doesn't own data, shouldn't have contracts
├── api/
├── domain/
└── infrastructure/       # ❌ Evidence table owned by Case module

modules/agent/
├── contracts.py          # ❌ No agent tables, shouldn't have contracts
├── api/
├── domain/
└── infrastructure/       # ❌ No persistent state

modules/report/
├── contracts.py          # ❌ Ephemeral storage, shouldn't have contracts
├── api/
├── domain/
└── infrastructure/       # Redis + ChromaDB (ephemeral)
```

**Problem**: These modules have vertical structure but don't meet vertical criteria (no data ownership).

---

## Option A: Move to Horizontal Service Layer

### Structure

```
faultmaven/
├── api/v1/routes/
│   ├── evidence.py       # Evidence endpoints
│   ├── agent.py          # Agent endpoints
│   └── report.py         # Report endpoints
│
├── services/
│   ├── evidence_service.py   # Evidence collection/validation logic
│   ├── agent_service.py      # Agent orchestration logic
│   └── report_service.py     # Report generation logic
│
├── core/
│   ├── investigation/        # Shared investigation logic (if any)
│   └── preprocessing/        # Evidence preprocessing (if shared)
│
└── infrastructure/
    └── persistence/
        └── case_repository.py  # Evidence/reports stored here
```

### Purpose Achieved?

✅ **Domain Logic Separation**: Business logic still organized by domain (evidence_service, agent_service, report_service)
✅ **Technical Separation**: Clear layer boundaries (API → Service → Infrastructure)
❌ **Module Boundaries**: No `contracts.py` - direct imports allowed (but discouraged via conventions)
✅ **Data Access**: Uses Case module's repository via dependency injection
✅ **Simpler Structure**: No need for contracts when data owned elsewhere

**Pros**:
- Simpler structure (no contracts needed)
- Fits traditional layered architecture
- Clear where business logic lives (services/)

**Cons**:
- No enforced module boundaries (rely on conventions)
- Less clear domain ownership (everything in services/)
- Harder to extract as microservices later (everything coupled)

---

## Option B: Keep in Modules/ but Remove Vertical Structure

### Structure

```
modules/evidence/
├── domain/
│   └── evidence_service.py   # Business logic only
└── api/
    └── routes.py             # Delegates to Case repository

modules/agent/
├── domain/
│   └── agent_service.py      # LangGraph orchestration
├── tools/                    # Agent tools
└── api/
    └── routes.py             # Operates on Case data

modules/report/
├── domain/
│   └── report_service.py     # Report generation
└── api/
    └── routes.py             # Uses Case repository
```

**Key Difference from Vertical**: No `contracts.py`, no `infrastructure/` (data access via Case module's contract)

### Purpose Achieved?

✅ **Domain Cohesion**: Evidence/Agent/Report logic still grouped by domain
✅ **Boundary Clarity**: No contracts (they don't own data, no need to expose)
✅ **Data Access**: Uses Case module's `ICaseRepository` contract
✅ **Module Separation**: Still in `modules/` directory (domain organization)
❌ **Full Vertical Benefits**: No contracts, can't enforce strict boundaries

**Pros**:
- Domain logic stays organized by domain
- No artificial contracts for non-owned data
- Clear that these are domain services (not infrastructure)

**Cons**:
- Still in `modules/` (might confuse with vertical modules)
- No enforced boundaries (rely on conventions)
- Hybrid structure (modules/ but not fully vertical)

---

## Recommended Approach: Option C - Hybrid (Domain Services in modules/)

### Structure (Recommended)

Keep Evidence, Agent, Report in `modules/` but restructure as **Domain Services**:

```
modules/evidence/              # Domain Service (not vertical)
├── domain/
│   └── evidence_service.py   # Collection, validation, preprocessing
└── api/
    └── routes.py             # Delegates to Case.repository

# NO contracts.py (don't own data, nothing to expose)
# NO infrastructure/ (data access via Case module)

modules/agent/                 # Domain Service (not vertical)
├── domain/
│   ├── agent_service.py      # LangGraph orchestration
│   └── investigation_orchestrator.py
├── tools/                     # Agent tools (knowledge_base, web_search)
└── api/
    └── routes.py             # Operates via Case.repository

# NO contracts.py (orchestration logic, not data owner)
# NO infrastructure/ (all state via Case module)

modules/report/                # Domain Service (not vertical)
├── domain/
│   └── report_service.py     # Generation logic
└── api/
    └── routes.py             # Uses Case.repository

# NO contracts.py (generates artifacts, doesn't own data)
# NO infrastructure/ (persistence via Case module, or ephemeral Redis/ChromaDB)
```

### Key Principles

1. **Domain Organization**: Keep in `modules/` because they represent distinct business capabilities
2. **No Contracts**: Don't own data, so no `contracts.py` needed
3. **No Infrastructure**: Data persistence handled by owning module (Case) or ephemeral storage
4. **Clear Naming**: Name as "Domain Services" in documentation to distinguish from vertical modules

---

## Purpose Comparison: Can We Still Achieve the Goals?

### Goal 1: Domain Cohesion

| Approach | Evidence Logic Location | Cohesion |
|----------|------------------------|----------|
| **Vertical (current)** | `modules/evidence/domain/` | ✅ High |
| **Option A (services/)** | `services/evidence_service.py` | ⚠️ Medium (single file, but grouped with other services) |
| **Option B/C (modules/)** | `modules/evidence/domain/` | ✅ High |

**Verdict**: Options B/C maintain domain cohesion. Option A reduces it but keeps logic organized.

---

### Goal 2: Clear Boundaries

| Approach | Boundary Enforcement | Clarity |
|----------|---------------------|---------|
| **Vertical (current)** | `contracts.py` enforced | ✅ Very clear (enforced) |
| **Option A (services/)** | Layer boundaries only | ⚠️ Medium (conventions) |
| **Option B/C (modules/)** | Module boundaries (no contracts) | ⚠️ Medium (conventions) |

**Verdict**: Vertical has strongest boundaries, but Evidence/Agent/Report don't need strict boundaries since they don't own data. Options B/C achieve sufficient separation via module organization.

---

### Goal 3: Independent Development

| Approach | Team Isolation | Parallel Development |
|----------|---------------|---------------------|
| **Vertical (current)** | High (contracts enforced) | ✅ Yes (clear interfaces) |
| **Option A (services/)** | Medium (shared services/) | ✅ Yes (different files) |
| **Option B/C (modules/)** | Medium (module organization) | ✅ Yes (different modules) |

**Verdict**: All approaches support parallel development. Vertical is stronger, but Evidence/Agent/Report don't need it since they're services, not data owners.

---

### Goal 4: Future Microservice Extraction

| Approach | Extraction Readiness | Notes |
|----------|---------------------|-------|
| **Vertical (current)** | ✅ Ready | Already self-contained |
| **Option A (services/)** | ⚠️ Requires refactoring | Services coupled to shared infrastructure |
| **Option B/C (modules/)** | ✅ Ready | Modules already self-contained (just no contracts) |

**Verdict**: Options B/C maintain extraction readiness. Option A would require significant refactoring.

---

### Goal 5: Data Access Patterns

| Approach | How Evidence Accesses Data | Notes |
|----------|---------------------------|-------|
| **Vertical (current)** | Via `ICaseRepository` contract | ✅ Correct pattern |
| **Option A (services/)** | Via `ICaseRepository` contract | ✅ Correct pattern |
| **Option B/C (modules/)** | Via `ICaseRepository` contract | ✅ Correct pattern |

**Verdict**: All approaches achieve correct data access. Evidence uses Case module's contract, doesn't own data.

---

## Recommendation: Option C (Domain Services in modules/)

### Rationale

1. **Maintains Domain Cohesion**: Evidence, Agent, Report logic stays organized by domain in `modules/`
2. **Clear Distinction**: They're in `modules/` but documented as "Domain Services" (not "Vertical Modules")
3. **No False Boundaries**: No `contracts.py` since they don't own data
4. **Future Extraction**: Still organized for potential microservice extraction
5. **Matches Schema Reality**: Reflects actual data ownership (Evidence data in Case tables, Agent has no tables, Reports ephemeral)

### Structure Changes Required

**From (Current Vertical - Incorrect)**:
```
modules/evidence/
├── contracts.py          # ❌ Remove - doesn't own data
├── api/
├── domain/
└── infrastructure/       # ❌ Remove - uses Case repository
```

**To (Domain Service - Correct)**:
```
modules/evidence/
├── domain/
│   └── evidence_service.py
└── api/
    └── routes.py         # Uses Case.repository via DI
```

**Key Changes**:
1. Remove `contracts.py` (nothing to expose - data owned by Case)
2. Remove `infrastructure/` (use Case module's repository)
3. Keep `domain/` and `api/` (business logic and endpoints)
4. Update imports: `from faultmaven.modules.case.contracts import ICaseRepository`

---

## Purpose Achievement Summary

| Goal | Vertical (Current) | Option A (services/) | Option C (Domain Services) |
|------|-------------------|---------------------|---------------------------|
| **Domain Cohesion** | ✅ High | ⚠️ Medium | ✅ High |
| **Clear Boundaries** | ✅ Enforced | ⚠️ Conventions | ⚠️ Conventions (sufficient) |
| **Independent Development** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Microservice Extraction** | ✅ Ready | ⚠️ Requires refactoring | ✅ Ready |
| **Correct Data Access** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Schema Alignment** | ❌ No (false ownership) | ✅ Yes | ✅ Yes |
| **Simplicity** | ⚠️ Complex (unnecessary) | ✅ Simple | ✅ Simple |

**Winner**: **Option C (Domain Services in modules/)** - Achieves all purposes while correctly reflecting schema ownership.

---

## Practical Implications

### For Evidence Module

**Current (Incorrect)**:
- Has `contracts.py` (but doesn't own evidence table)
- Has `infrastructure/evidence_repository.py` (but table owned by Case)
- Other modules could import from evidence contracts (wrong)

**After Restructuring (Correct)**:
- No `contracts.py` (nothing to expose)
- No `infrastructure/` (uses Case repository)
- Evidence service uses: `from faultmaven.modules.case.contracts import ICaseRepository`
- Other modules access evidence via Case module, not Evidence module

**Code Example**:
```python
# modules/evidence/domain/evidence_service.py
class EvidenceService:
    def __init__(self, case_repo: ICaseRepository):  # Uses Case contract
        self.case_repo = case_repo

    async def collect_evidence(self, case_id: str, evidence_data: Evidence):
        # Business logic: validation, preprocessing
        processed = await self.preprocess(evidence_data)

        # Persistence via Case repository (Case owns the table)
        case = await self.case_repo.get_case(case_id)
        case.evidence.append(processed)
        await self.case_repo.save_case(case)
```

### For Agent Module

**Current (Incorrect)**:
- Has `contracts.py` (but has no agent tables)
- Has `infrastructure/agent_execution_repository.py` (but no persistent agent state)
- LangGraph state is ephemeral/in-memory

**After Restructuring (Correct)**:
- No `contracts.py` (orchestration logic, not data owner)
- No `infrastructure/` (all persistent state via Case module)
- Agent service uses: `from faultmaven.modules.case.contracts import ICaseService`
- Tools remain in `modules/agent/tools/` (domain-specific to agent)

**Code Example**:
```python
# modules/agent/domain/agent_service.py
class AgentService:
    def __init__(
        self,
        case_service: ICaseService,  # Uses Case contract
        knowledge_service: IKnowledgeService,  # Uses Knowledge contract
        llm_provider: ILLMProvider  # Uses infrastructure contract
    ):
        self.case_service = case_service
        self.knowledge_service = knowledge_service
        self.llm_provider = llm_provider

    async def investigate(self, case_id: str, query: str):
        # LangGraph orchestration (ephemeral state)
        # All persistent state goes through Case module
        result = await self.orchestrate_investigation(case_id, query)

        # Save investigation state via Case service
        await self.case_service.add_investigation_result(case_id, result)
```

### For Report Module

**Current (Incorrect)**:
- Has `contracts.py` (but reports are ephemeral artifacts)
- Has `infrastructure/` (Redis + ChromaDB with TTL)
- Reports expire, not first-class domain entities

**After Restructuring (Current - Keep until TD-001 fixed)**:
- No `contracts.py` (generates artifacts, doesn't own domain data)
- Keep `infrastructure/` for Redis + ChromaDB (temporary until migration)
- Report service uses: `from faultmaven.modules.case.contracts import ICaseService`

**After TD-001 (Future)**:
- No `contracts.py` (still)
- Remove `infrastructure/` (reports table in Case module)
- Report service uses Case repository for persistence

**Code Example (Current)**:
```python
# modules/report/domain/report_service.py
class ReportService:
    def __init__(
        self,
        case_service: ICaseService,  # Uses Case contract
        llm_provider: ILLMProvider
    ):
        self.case_service = case_service
        self.llm_provider = llm_provider

    async def generate_report(self, case_id: str, report_type: ReportType):
        # Get case data via Case service
        case = await self.case_service.get_case(case_id)

        # Generate report (business logic)
        report = await self.generate(case, report_type)

        # Store in ephemeral storage (Redis + ChromaDB) - temporary
        await self.report_store.save(report)  # Redis + ChromaDB
        # TODO: After TD-001, use case_repo.save_report() instead
```

---

## Summary: What "Put Back to Layer" Actually Means

### Terminology Clarification

**Important**: "Put back to layer" is **misleading terminology**. Here's what we actually mean:

1. **Vertical Structuring** = Full vertical slice with `contracts.py`, `api/`, `domain/`, `infrastructure/`
2. **Layer Structuring** = Horizontal layers (all APIs in `api/v1/routes/`, all services in `services/`, all infrastructure in `infrastructure/`)
3. **Domain Service** = Hybrid - organized by domain in `modules/` but WITHOUT full vertical structure (no `contracts.py`, no `infrastructure/`)

### What We Actually Mean

**"Put back to layer"** actually means: **Remove vertical slicing characteristics** (not necessarily move to horizontal layers)

The key change is:
- ❌ Remove `contracts.py` (they don't own data, nothing to expose)
- ❌ Remove `infrastructure/` (data owned by other modules)
- ✅ Keep `domain/` and `api/` (business logic and endpoints)
- ✅ Keep in `modules/` (domain organization preserved)

**Result**: **Domain Service structure** (not vertical, not horizontal layer - a hybrid)

### Two Structural Options

#### Option A: True Layer Structure (Horizontal Layers)

Move to `services/` directory:
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

**This is TRUE "layer structuring"** - organized by technical function (services, API).

#### Option C: Domain Service Structure (Hybrid)

Keep in `modules/` but remove vertical characteristics:
```
modules/evidence/         # Domain Service (NOT vertical)
├── domain/               # Business logic
└── api/                  # Endpoints
# NO contracts.py, NO infrastructure/

modules/agent/            # Domain Service (NOT vertical)
├── domain/
├── tools/
└── api/
# NO contracts.py, NO infrastructure/
```

**This is NOT "layer structuring"** - it's still organized by domain (modules/), just without full vertical structure.

### Correct Terminology

| Structure Type | Organization | Has contracts.py? | Has infrastructure/? | Location |
|---------------|--------------|-------------------|----------------------|----------|
| **Vertical Module** | By domain | ✅ Yes | ✅ Yes | `modules/{domain}/` |
| **Domain Service** | By domain | ❌ No | ❌ No | `modules/{domain}/` |
| **Layer Structure** | By technical function | N/A | N/A | `services/`, `api/v1/routes/` |

**Evidence, Agent, Report should be**: **Domain Services** (Option C)
- Still in `modules/` (domain organization)
- But NOT vertical (no contracts, no infrastructure)
- Also NOT layer-structured (still organized by domain, not technical function)

### Why Option C is Correct

1. **Preserves Domain Cohesion**: Logic organized by business domain (Evidence, Agent, Report)
2. **Removes False Boundaries**: No `contracts.py` since they don't own data
3. **Correct Data Access**: Uses owning module's contracts (Case module)
4. **Clear Distinction**: In `modules/` but documented as "Domain Services" (not "Vertical Modules")

**Terminology to use**: **"Domain Services"** or **"Non-Vertical Modules"** - NOT "layer-structured"

---

## Next Steps

1. **Clarify Intent**: Confirm Option C (Domain Services in modules/) is acceptable
2. **Update Documentation**: Document Evidence, Agent, Report as "Domain Services" (not vertical modules)
3. **Restructure Code** (if Option C chosen):
   - Remove `contracts.py` from Evidence, Agent, Report
   - Remove `infrastructure/` from Evidence, Agent (Report keeps for ephemeral storage)
   - Update imports to use Case module's contracts
   - Update API routes to delegate to Case repository
4. **Update Import Linter**: Remove module boundary rules for Evidence, Agent, Report (no contracts to enforce)

---

**Document Owner**: Engineering Leadership
**Status**: Clarification Document
**Last Updated**: 2026-01-09

# FaultMaven Architectural Design Principles

**Version**: 2.3
**Date**: 2026-11-08
**Status**: Active
**Related Documents**:

- [ADR-001: Monolith Evolution Strategy](../decisions/adr-001-monolith-evolution-strategy.md)
- [Module Organization Design](module-organization-design.md)

---

## Executive Summary

This document defines the **core architectural design principles** that guide FaultMaven's evolution from a battle-tested monolith to a modern, modular architecture.

### Core Philosophy

> **"Enforce what matters. Escape what you must. Sunset what you escape."**

These principles optimize for a small team (<15) building an AI-powered SRE tool. They prioritize debuggability over abstraction, and measurable outcomes over theoretical purity.

### The 14 Principles

| # | Principle | One-Line Rule |
|---|-----------|---------------|
| 1 | Deployment Agnostic | Infrastructure choices are deployment-time decisions, not code-time |
| 2 | Vertical Modules | Organize by domain; modules communicate via explicit contracts |
| 3 | Database Boundaries | Modules own their tables; no cross-module JOINs |
| 4 | Interface-Based Design | Protocols for swappable boundaries; concrete classes internally |
| 5 | Composition Root | All DI wiring in `bootstrap/composition.py`; services never touch container |
| 6 | Errors as Domain Concepts | Every module defines its exception hierarchy |
| 7 | Observability by Default | Correlation IDs, structured logs, traces on external calls |
| 8 | Boundary Enforcement | Import-linter enforces rules at build time; dead code is removed, not left behind |
| 9 | Test Safety Net | 70% code coverage floor; AI evaluation benchmarks designed, not built (#2520) |
| 10 | Bounded AI Complexity | The milestone engine owns orchestration state; LLM adapters are stateless |
| 11 | Clean Moves, Not Rewrites | Move code to its correct location; don't rewrite during the move; don't leave the origin behind |
| 12 | Escape Hatches | Architectural exceptions are allowed but tracked, counted, and time-limited |
| 13 | One Owner per Fact | Every stored or stated fact has one writer, one point where it is written, and one end of life, recorded in the design |
| 14 | Bounded Execution | Every stated deadline names the mechanism that keeps it; work over user-controlled input runs where its deadline can stop it |

### Principle Hierarchy

```
CRITICAL (Violations block deployment)
├── 5. Composition Root
├── 6. Errors as Domain Concepts
└── 10. Bounded AI Complexity

IMPORTANT (Violations require documented exception)
├── 1. Deployment Agnostic
├── 2. Vertical Modules with Contracts
├── 3. Database Boundaries
├── 7. Observability by Default
├── 8. Boundary Enforcement
├── 11. Clean Moves, Not Rewrites
├── 13. One Owner per Fact
└── 14. Bounded Execution

RECOMMENDED (Apply judgment)
├── 4. Interface-Based Design
├── 9. Test Safety Net
└── 12. Escape Hatches
```

Principles 13 and 14 are IMPORTANT, not CRITICAL: parts of the system violate both today (the Status column of each one's Enforcement table), so "violations block deployment" would be false on the day it was written.

---

## Table of Contents

1. [Deployment Agnostic Architecture](#1-deployment-agnostic-architecture)
2. [Vertical Modules with Contracts](#2-vertical-modules-with-contracts)
3. [Database-Per-Module Boundaries](#3-database-per-module-boundaries)
4. [Interface-Based Design](#4-interface-based-design)
5. [Composition Root](#5-composition-root)
6. [Errors as Domain Concepts](#6-errors-as-domain-concepts)
7. [Observability by Default](#7-observability-by-default)
8. [Architectural Boundary Enforcement](#8-architectural-boundary-enforcement)
9. [Test Safety Net](#9-test-safety-net)
10. [Bounded Complexity for AI Integration](#10-bounded-complexity-for-ai-integration)
11. [Clean Moves, Not Rewrites](#11-clean-moves-not-rewrites)
12. [Escape Hatches](#12-escape-hatches)
13. [One Owner per Fact](#13-one-owner-per-fact)
14. [Bounded Execution](#14-bounded-execution)

---

## 1. Deployment Agnostic Architecture

### Principle

> **"Infrastructure choices are deployment-time decisions, not code-time constraints."**

FaultMaven Core must remain **agnostic to where it runs** (local dev, Docker, Kubernetes, serverless, bare metal). Infrastructure differences are expressed via **provider selection** and **configuration injection**, not code branching.

### Key Rules

| Rule | What it means in practice |
|------|----------------------------|
| ✅ **Single codebase & artifact** | One repository and one build artifact runs everywhere |
| ✅ **Business logic stays neutral** | No deployment-specific branching in services/domain/API handlers |
| ✅ **Provider selection is explicit** | Composition root chooses implementations from env selectors |
| ✅ **Operational neutrality** | Metrics/tracing/jobs are exposed as hooks; runtime decides exporters |
| ✅ **Fail-fast configuration** | Crash at startup if config is invalid (see below) |
| ❌ **No separate "local"/"cloud" packages** | No parallel app trees like `faultmaven/local/` or `faultmaven/cloud/` |
| ❌ **No infra coupling in business logic** | No direct vendor calls (S3/Redis/ChromaDB) from domain services |

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    Application Code                              │
│                                                                  │
│  Business logic uses interfaces only (no deployment branching)   │
│  • CaseService calls ICaseRepository                             │
│  • KnowledgeService calls IKnowledgeVectorStore                  │
│  • EvidenceService calls IStorageBackend                         │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ depends on interfaces
                              │
┌─────────────────────────────────────────────────────────────────┐
│                   Composition Root (main.py)                     │
│                                                                  │
│  Loads settings once, validates config, wires providers          │
│  • STORAGE_BACKEND=s3 → S3StorageBackend                        │
│  • VECTOR_BACKEND=chroma → ChromaVectorStore                    │
│  • LLM_PROVIDER=openai → OpenAIProvider                         │
└─────────────────────────────────────────────────────────────────┘
```

### Fail-Fast Configuration

```python
# bootstrap/lifespan.py's lifespan (called from main.py's app = FastAPI(lifespan=lifespan))
async def startup():
    settings = Settings()  # Pydantic validates types

    # Capability checks - crash with actionable message
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise StartupError(
                "OPENAI_API_KEY required when LLM_PROVIDER=openai. "
                "Set the key or use LLM_PROVIDER=local for offline mode."
            )

    # Connectivity checks with timeout
    try:
        await asyncio.wait_for(
            verify_chromadb_health(settings.chromadb_url),
            timeout=5.0
        )
    except TimeoutError:
        raise StartupError(
            f"ChromaDB at {settings.chromadb_url} not reachable. "
            "Start an external ChromaDB server, or unset CHROMADB_URL "
            "to fall back to the local PersistentClient."
        )
```

**For an SRE Tool, This Is Non-Negotiable**: FaultMaven should never return 500s because Redis is down. If it can't do its job, it shouldn't start.

### Provider Examples

**Local preset** (Development / Self-Host):

```bash
CONFIG_PRESET=local

# Implied defaults:
DATABASE_URL=sqlite+aiosqlite:///./faultmaven.db
TENANT_PROVIDER=single
STORAGE_BACKEND=filesystem
VECTOR_BACKEND=chroma
METRICS_ENABLED=false
TRACING_ENABLED=false
```

**Enterprise preset** (Production):

```bash
CONFIG_PRESET=enterprise

DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/faultmaven
REDIS_URL=redis://redis:6379/0
VECTOR_STORAGE_TYPE=chromadb
CHROMADB_URL=http://chromadb:8000
STORAGE_BACKEND=s3
TENANT_PROVIDER=multi
OPIK_ENABLED=true
METRICS_ENABLED=true
METRICS_EXPORTER=prometheus_http
```

**Key Insight**: Both configurations run the **SAME codebase** with **ZERO conditional logic** in business services.

---

## 2. Vertical Modules with Contracts

### Principle

> **"Organize code by domain capability. Modules communicate only through explicit contracts."**

Instead of organizing by technical layer (controllers, services, repositories), organize by **domain capability** (auth, case, knowledge, evidence, agent).

**Important**: Vertical slicing applies **only to business domains**, not to all modules. Cross-cutting infrastructure (logging, observability, LLM providers, storage backends) should remain horizontal layers.

**Minimum Criteria for Vertical Modules**: A module is vertical if and only if it meets ALL THREE criteria:
1. ✅ Owns domain data (database tables representing business entities)
2. ✅ Implements business logic (business rules and domain constraints)
3. ✅ Represents a domain capability (distinct business capability)

See [Module Organization Design](module-organization-design.md) for detailed criteria, examples, and edge case handling.

### Before: Horizontal Layering

```
faultmaven/
├── api/v1/routes/           # All API routes together
├── services/                # All services together
├── infrastructure/          # All infrastructure together
├── models/                  # All models together
└── utils/                   # All utilities together
```

**Problem**: Changes to "add case sharing" require touching files across 4-5 directories.

### After: Vertical Modules

```
faultmaven/
├── modules/
│   ├── auth/
│   │   ├── contracts.py      # ← Public interface (DTOs, protocols)
│   │   ├── api/              # Auth endpoints
│   │   ├── domain/           # Auth business logic (PRIVATE)
│   │   └── infrastructure/   # Auth persistence (PRIVATE)
│   │
│   ├── case/
│   │   ├── contracts.py      # ← What case module EXPOSES
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/
│   │
│   ├── knowledge/
│   │   ├── contracts.py
│   │   ├── api/
│   │   ├── domain/
│   │   └── infrastructure/
│   │
│   └── ...
│
├── _shared/                  # Cross-cutting utilities (NOT business logic)
│   ├── exceptions.py
│   └── logging.py
│
└── core/                     # Shared infrastructure
    ├── container/            # DI wiring
    └── interfaces/           # External provider protocols
```

### The Contract Rule

```python
# ✅ ALLOWED: Import from contracts
from faultmaven.modules.case.contracts import CaseDTO, ICaseRepository

# ❌ FORBIDDEN: Import from internal domain
from faultmaven.modules.case.domain.models.case import Case
```

### When to Use Vertical Slicing

Vertical slicing should be applied to modules that meet **ALL THREE** minimum criteria:

1. ✅ **Own domain data** - Have database tables representing business entities
2. ✅ **Implement business logic** - Enforce business rules and domain constraints
3. ✅ **Represent domain capability** - Distinct business capability

**Examples of Vertical Modules**: `auth/`, `case/`, `knowledge/` (3 modules verified against schema)

**Note**: `evidence/`, `agent/`, and `report/` are **Domain Services** (implement business logic but don't own data). See [Module Organization Design](module-organization-design.md) for schema-verified classification.

**Important**: All vertical modules are **structural peers** (equal structure, equal status). Vertical modules CAN depend on other vertical modules via contracts - high fan-in does NOT change categorization. See [Module Organization Design](module-organization-design.md#vertical-modules-peer-status-and-dependencies) for details.

### When to Keep Horizontal

Components should remain horizontal when they fail **ANY** of the three criteria above. Common patterns:

1. ❌ **No domain data** - Only technical state or no state
2. ❌ **No business logic** - Only technical integration
3. ❌ **No domain capability** - Provides technical capability, not business capability

**Examples of Horizontal Infrastructure**: `infrastructure/llm/`, `infrastructure/logging/`, `infrastructure/observability/`, `infrastructure/storage/`

For complete recommendations, examples, and edge case handling, see [Module Organization Design](module-organization-design.md).

### Benefits

1. **Reduced Cognitive Load**: Developers understand one domain at a time
2. **Clear Ownership**: Each module has defined boundaries
3. **Independent Development**: Teams work on modules in parallel
4. **Easier Testing**: All aspects of a feature are co-located
5. **Future Extraction**: Modules can become microservices if needed

---

## 3. Database-Per-Module Boundaries

### Principle

> **"Modules own their tables. Cross-module data flows through services, not JOINs."**

This table rule is the relational case of [Principle 13.1](#13-one-owner-per-fact); every other store has its row in the [store registry](../data-and-storage/store-registry.md).

### Table Naming Convention

Tables are named after the business entity they store. Module-name prefixes are **not** required — the live schema uses unprefixed names for the primary entity of a module (`users`, `cases`, `evidence`, `reports`, `knowledge_items`) and semantic prefixes only to disambiguate sub-entities or related collections (`case_messages`, `case_actions`, `case_entities`, `oauth_authorization_codes`).

```sql
-- Auth module (user domain)
users, organizations, organization_members, roles, permissions,
role_permissions, teams, team_members, user_audit_log,
oauth_authorization_codes
-- Note: auth sessions live in Redis (FakeRedis local, real Redis cloud)
-- via RedisSessionStore — there is no SQL `sessions` table.

-- Case module (case domain — owns evidence, reports, agent audit data)
cases, case_messages, case_actions, case_tags,
evidence, hypotheses, solutions, uploaded_files,
reports

-- Knowledge module (conversion jobs and drafts by the #2396 ruling)
knowledge_items, conversion_jobs, conversion_drafts

-- Infrastructure-layer (not a domain module)
config_overrides
```

**Source of truth:** `faultmaven/infrastructure/persistence/models.py`. The ER diagram (`docs/architecture/data-and-storage/er-diagram.md`) is regenerated from these models. See [Deployment-Aware Schema Strategy](https://github.com/FaultMaven/faultmaven-doc-internal/blob/main/architecture/deployment-schema-strategy.md) for the per-table applicability matrix and dialect policy.

### Cross-Module Data Access

```python
# ❌ WRONG: Report module queries case tables directly
async def generate_report(case_id):
    case = await db.execute("SELECT * FROM cases WHERE case_id = ?", case_id)

# ✅ RIGHT: Report module calls case repository via contract
async def generate_report(case_id):
    case = await self.case_repo.get(case_id)
```

### Preventing N+1 Problems

When modules can't JOIN, naive implementations create N+1 patterns. **Contracts must include bulk methods**:

```python
# modules/case/contracts.py
class ICaseRepository(Protocol):
    """Public contract for cross-module case access."""

    async def get(self, case_id: str) -> CaseDTO:
        """Single case lookup."""
        ...

    async def get_by_ids(self, case_ids: list[str]) -> list[CaseDTO]:
        """Bulk lookup - prevents N+1."""
        ...

    async def list_for_user(
        self,
        user_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> PaginatedResult[CaseDTO]:
        """Paginated query - prevents unbounded results."""
        ...
```

```python
# ❌ N+1 ANTI-PATTERN
async def generate_bulk_report(case_ids: list[str]):
    cases = []
    for case_id in case_ids:  # 100 cases = 100 queries
        cases.append(await case_repo.get(case_id))

# ✅ BULK PATTERN
async def generate_bulk_report(case_ids: list[str]):
    cases = await case_repo.get_by_ids(case_ids)  # 1 query
```

### Contract Design Rules

1. Every entity query contract includes a bulk variant
2. List endpoints are always paginated (no unbounded `get_all()`)
3. Contracts expose filtering to push predicates to the owning module

---

## 4. Interface-Based Design

### Principle

> **"Depend on abstractions for external boundaries. Use concrete classes internally."**

### When to Use Protocols

| Component | Multiple Implementations? | Use Protocol? |
|-----------|---------------------------|---------------|
| LLM providers | Yes (9 providers: Anthropic, OpenAI, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, Local Ollama/vLLM) | ✅ Yes |
| Vector stores | No (one class per store; PersistentClient or HttpClient is the injected client, not a second implementation) | KB only: `IKnowledgeVectorStore`, which `KnowledgeService` is typed against (#2284). No generic vector-store protocol (#1782) |
| Storage backends | Yes (S3, Azure Blob, filesystem) | ✅ Yes |
| Session stores | Yes (Redis, FakeRedis) | ✅ Yes |
| Module contracts | Yes (for cross-module calls) | ✅ Yes |
| CaseService | No (one implementation) | ❌ No |
| ReportGenerator | No (one implementation) | ❌ No |

### IDE Navigation Rule

If "Go to Definition" takes you to a Protocol instead of real code, ask: **"Will this ever have two implementations?"** If no, delete the Protocol.

### Key Interfaces

| Interface | Purpose | Implementations |
|-----------|---------|-----------------|
| `ILLMProvider` | LLM integration | Anthropic, OpenAI, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, Local (Ollama/vLLM) |
| `IKnowledgeVectorStore` | Knowledge-base search and writes (one collection, every scope) | `KnowledgeVectorStore` over ChromaDB |
| `ISessionStore` | Session management | Redis (cloud), FakeRedis (local) |
| `IStorageBackend` | File storage | S3, Azure Blob, Filesystem |
| `ICaseRepository` | Cross-module case access | SQLite, PostgreSQL hybrid, sessionless variants |

---

## 5. Composition Root

### Principle

> **"All dependency wiring happens in one place. Services never resolve their own dependencies."**

This is the **most critical principle**. Services must be pure—they receive dependencies via constructor, never via container lookup.

### ❌ Anti-Pattern: Service Locator

```python
# WRONG: Service pulls its own dependencies (hidden global state)
class CaseService:
    def __init__(self):
        self.auth = ServiceLocator.get(IAuthService)  # Hidden dependency!
        self.repo = ServiceLocator.get(ICaseRepository)
```

**Problems**:
- Dependencies are hidden (not visible in constructor)
- Hard to test (must mock global container)
- Circular dependencies surface at runtime, not startup

### ✅ Correct: Composition Root

```python
# bootstrap/composition.py - ALL wiring happens here: _wire_composition_root,
# called from compose_application, which bootstrap/lifespan.py calls
async def startup():
    # Create infrastructure
    redis_store = RedisSessionStore(settings.redis_url)
    case_repo = PostgresCaseRepository(db_session)
    auth_service = AuthService(token_store=redis_store)

    # Wire services with explicit dependencies
    case_service = CaseService(
        auth=auth_service,
        repo=case_repo,
    )

    # Attach to app state for route access
    app.state.case_service = case_service


# modules/case/domain/services/case_service.py - NO container knowledge
class CaseService:
    def __init__(self, auth: IAuthService, repo: ICaseRepository):
        self.auth = auth  # Injected, not resolved
        self.repo = repo
```

### Benefits

- Unit tests run 10x faster (no global container to reset)
- Dependency graph is visible in one file
- Circular dependencies surface at startup, not runtime
- Constructor signatures are the complete dependency manifest

### Startup: refuse, or degrade knowingly

Wiring can fail, and a half-wired application is not a degraded feature — it is
a route that 500s on first use, or a gate that never runs, behind a `/health`
that still answers `healthy`. The composition root therefore separates *doing
the wiring* from *deciding whether a failure is survivable*:

| Function (`faultmaven/bootstrap/composition.py`) | Responsibility |
|---|---|
| `_wire_composition_root(app, settings)` | Container init, bootstrap, the RLS role gate, and the whole `app.state` cascade. Raises on failure; decides nothing. |
| `compose_application(app, settings)` | The one decision: refuse the boot, or continue with a warning. |

**One predicate decides, everywhere**: `settings.must_not_degrade` —
`DEPLOYMENT_MODE=cloud`, or any deployment declaring `ENVIRONMENT=production`.
Both composition gates — the DI container's refusal and the composition root —
key on it, so "must not degrade" has one meaning and one spelling. (Gates that
express a *capability* requirement rather than a degradation policy stay on
`is_cloud`: a shared Redis is a cloud requirement, and standalone runs
FakeRedis by design at any `ENVIRONMENT`.)

- **Must not degrade** → startup raises `RuntimeError`, uvicorn exits, the pod
  CrashLoops and the Kubernetes rollout rolls back.
- **Anywhere else** → the failure is logged and the application serves what it
  has. A self-hosted instance missing an optional service is still useful to
  its single user, who can read the log.

`RuntimeError` is the terminal channel: the container's refusal, the bootstrap
failure and the RLS role gate raise it, and both entry points — the web
lifespan and `jobs/run.py` — treat it as fatal in *every* mode, because the
callee has already decided the boot cannot continue.

Genuinely optional services (the conversion service, the query classification
engine) say so at their own wiring line and fall back to `None`. There is no
blanket `except` around the cascade: "optional service absent" is a claim a
specific line makes, not something inferred from an exception reaching a
catch-all.

Under pytest the container raises instead of returning uninitialized, so a
composition failure names itself rather than re-surfacing as an unrelated
assertion in a later test; `initialize(allow_degraded=True)` opts a test back
into the lenient path without weakening the mode gate.

*Rejected alternative*: gating on `ENVIRONMENT` alone. Cloud overlays run
`ENVIRONMENT=staging`, so that gate reads "not production" and serves a partial
API exactly where the guarantee is needed.

---

## 6. Errors as Domain Concepts

### Principle

> **"Services raise typed domain exceptions. Infrastructure errors are wrapped in domain terms. The API layer translates domain exceptions to HTTP responses centrally."**

### Shared Exception Hierarchy

FaultMaven roots every exception in a single shared hierarchy in
[`faultmaven/exceptions.py`](../../../faultmaven/exceptions.py) (base
`FaultMavenException` plus the shared cross-cutting types below). Each
module then defines its own domain exceptions that inherit from that
shared base — e.g. `CaseException(FaultMavenException)` in
[`modules/case/exceptions.py`](../../../faultmaven/modules/case/exceptions.py)
and `KnowledgeException(KnowledgeBaseException)` in
[`modules/knowledge/exceptions.py`](../../../faultmaven/modules/knowledge/exceptions.py).
Modules do not create *parallel* hierarchies rooted at their own base
class: every domain exception ultimately derives from
`FaultMavenException`, so the API layer can translate them to HTTP
responses centrally.

```python
# faultmaven/exceptions.py (excerpt)
class FaultMavenException(Exception): ...
class ServiceError(FaultMavenException): ...
class NotFoundError(ServiceError): ...        # 404
class ConflictError(ServiceError): ...        # 409
class ValidationException(FaultMavenException): ...    # 422
class PermissionDeniedException(FaultMavenException): ...  # 403
class ServiceException(FaultMavenException): ...        # 500
```

### Wrapping Infrastructure Errors

```python
# modules/case/infrastructure/repository.py
async def get_case(self, case_id: str) -> Case:
    try:
        result = await self.db.fetch_one(...)
    except DatabaseError as e:
        # Wrap infrastructure error in domain terms
        raise ServiceException(f"Failed to retrieve case: {e}") from e
    if not result:
        raise NotFoundError(resource_type="case", resource_id=case_id)
    return Case.from_row(result)
```

### API Layer Translation

Translation is centralized in
[`api/exception_handlers.py`](../../../faultmaven/api/exception_handlers.py)
— each domain exception type is registered once with FastAPI and maps
to a known HTTP shape across every route. Routes do not catch and
translate domain exceptions individually. See the [exception
contract specification](../specifications/exception-contract.md) for
the full status-code mapping, response-body shapes, and the
recommended route pattern.

---

## 7. Observability by Default

### Principle

> **"Structured logs with correlation IDs. Traces on every external call. Metrics with consistent naming."**

A trace that holds a prompt or a response is customer content, with the case-data obligations wherever it is stored ([ruling on #2616](https://github.com/FaultMaven/faultmaven/issues/2616#issuecomment-6716225805)); its row is in the [store registry](../data-and-storage/store-registry.md). Producing traces stays a core duty.

### Correlation ID Middleware

```python
@app.middleware("http")
async def correlation_middleware(request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response
```

### Structured Logging

```python
# All logs include context automatically
logger.info("investigation_started",
    case_id=case.id,
    phase="initial_triage",
    llm_provider=provider.name
)
# Output: {"event": "investigation_started", "case_id": "...", "correlation_id": "..."}
```

### Metric Naming Convention

```
faultmaven_{module}_{operation}_{unit}

Examples:
faultmaven_case_investigation_started_total
faultmaven_llm_request_duration_seconds
faultmaven_knowledge_search_results_count
```

---

## 8. Architectural Boundary Enforcement

### Principle

> **"Architectural rules must be enforced at build time, not code review time."**

FaultMaven uses **import-linter** to automatically detect and prevent violations.

### Enforced Contracts

| Contract | Rule | Enforcement |
|----------|------|-------------|
| **Module Boundaries** | Modules import only from `contracts.py` | `forbidden` type |
| **Layer Separation** | Services cannot import from API layer | `forbidden` type |
| **Model Isolation** | Models cannot import from service layer | `forbidden` type |
| **Knowledge Layers** | API → Domain → Infrastructure | `layers` type |

### Import-Linter Configuration

```ini
# .importlinter
[importlinter]
root_package = faultmaven

[importlinter:contract:module_boundaries]
name = Module internals are private
type = forbidden
source_modules =
    faultmaven.modules.*.domain
    faultmaven.modules.*.infrastructure
forbidden_modules =
    faultmaven.modules

[importlinter:contract:layer_separation]
name = Services cannot import API layer
type = forbidden
source_modules = faultmaven.services
forbidden_modules = faultmaven.api

[importlinter:contract:model_isolation]
name = Models cannot import services
type = forbidden
source_modules = faultmaven.models
forbidden_modules = faultmaven.services
```

### Dead Code Intolerance

Unused files create false signals during audits, confuse AI assistants reading the codebase, and obscure the real architecture. If code has zero imports and no route registration, remove it. Git history preserves everything — the codebase should reflect what the system *is*, not what it *was*.

### CI/CD Integration

```yaml
# .github/workflows/ci.yml
- name: Check architectural boundaries
  run: |
    pip install import-linter
    lint-imports
```

---

## 9. Test Safety Net

### Principle

> **"70% code coverage floor, plus evaluation benchmarks for AI behavior."**

### Two Testing Dimensions

| Dimension | What It Tests | Target |
|-----------|---------------|--------|
| **Code Coverage** | Lines executed | ≥70% |
| **AI Evaluation** | Output accuracy | ≥85% on benchmark |

### Code Coverage by Layer

| Layer | Target | Test Type |
|-------|--------|-----------|
| Domain services | 85% | Unit (mocked infra) |
| API routes | 70% | Integration |
| Infrastructure | 60% | Contract tests |

### AI Evaluation Strategy

**Designed, not built.** `tests/evaluation/` does not exist and `pytest.ini` registers no `evaluation` marker, so no AI evaluation gate runs. The sketch below and the commands after it are the design; #2520 tracks the outcome instrument the gate needs.

```python
# tests/evaluation/test_investigation_accuracy.py
"""
Benchmark: 50 real incidents with known root causes.
"""

@pytest.mark.evaluation
@pytest.mark.parametrize("incident", load_benchmark_incidents())
async def test_root_cause_identification(incident, agent):
    result = await agent.investigate(incident.symptoms)

    # Semantic similarity to known root cause
    similarity = compute_similarity(result.conclusion, incident.known_root_cause)
    assert similarity >= 0.85

    # Must not hallucinate non-existent services
    for service in result.mentioned_services:
        assert service in incident.known_services, f"Hallucinated: {service}"
```

### Test Commands

```bash
# Fast: Code tests only
pytest -m "not evaluation"

# Full: Include AI evaluation
pytest -m "evaluation" --benchmark
```

### Quality Gates

- ✅ All tests must pass before merging
- ✅ Coverage cannot drop below 70%
- ⏳ AI evaluation accuracy ≥85%: not built (#2520)
- ✅ CI/CD pipeline enforces all rules

---

## 10. Bounded Complexity for AI Integration

### Principle

> **"LLM calls are stateless pure functions. Orchestration handles state, retries, and fallbacks."**

### Architecture Layers

```
┌─────────────────────────────────────────────────┐
│      Orchestration Layer (Stateful)              │
│                                                  │
│  Owns: Investigation state, hypotheses, retries │
│                                                  │
│  • MilestoneEngine (opportunistic investigation) │
│  • InvestigationService (turn lifecycle)        │
│  • HypothesisManager (confidence scoring)       │
│  • LLMRouter (provider fallback chain)          │
└─────────────────────────────────────────────────┘
                      │
                      │ Delegates stateless calls
                      ▼
┌─────────────────────────────────────────────────┐
│      LLM Adapter Layer (Stateless)               │
│                                                  │
│  Owns: Provider protocol, request formatting    │
│  Rule: Pure functions, no retry logic           │
│                                                  │
│  • ILLMProvider implementations                 │
│  • Token counting (BEFORE call)                 │
│  • Response parsing                             │
└─────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│      External LLM APIs                           │
│  OpenAI, Anthropic, Fireworks, Ollama            │
└─────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Concern | Orchestration (milestone engine) | LLM Adapter |
|---------|---------------------------|-------------|
| State management | ✅ Owns | ❌ None |
| Retry logic | ✅ Owns | ❌ None |
| Provider fallback | ✅ Owns | ❌ None |
| Token counting | ❌ Delegates | ✅ Owns |
| Request formatting | ❌ Delegates | ✅ Owns |

### LLM Adapter Contract (Stateless)

```python
class ILLMAdapter(Protocol):
    """Stateless LLM call interface."""

    async def complete(
        self,
        messages: list[Message],
        max_tokens: int,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Pure function: (messages, config) → response

        - No retries (orchestration handles)
        - No state (orchestration handles)
        - Validates tokens BEFORE calling
        """
        ...

    def count_tokens(self, messages: list[Message]) -> int:
        """Synchronous token counting."""
        ...
```

### Orchestration Handles Retries

```python
# orchestration/investigation_runner.py
async def run_phase(self, phase: Phase) -> PhaseResult:
    for attempt in range(3):
        try:
            # LLM adapter is stateless - just makes the call
            return await self.llm_adapter.complete(phase.prompt)
        except RateLimitError:
            await asyncio.sleep(2 ** attempt)
        except ProviderError:
            self.llm_adapter = self.fallback_chain.next()
    raise InvestigationFailed("All LLM providers exhausted")
```

---

## 11. Clean Moves, Not Rewrites

### Principle

> **"Move code to its correct location. Don't rewrite logic during a move. Don't leave the old copy behind."**

### Process

1. **Identify** the target structure
2. **Move** files with `git mv` (preserves history and blame)
3. **Update** all references and imports
4. **Test** — all tests must pass
5. **Delete** the origin — no shims, no re-exports, no "TODO: remove old version"
6. **Commit** — one bounded move per commit, each commit complete

### Pre-Launch vs Post-Launch

**Pre-launch:** Clean breaks over compatibility layers. Dead code misleads future developers and AI assistants — it's not "safe to leave for now," it's actively harmful to codebase legibility.

**Post-launch:** When external consumers depend on import paths or API contracts, introduce a deprecation window with a sunset date (see P12). Even post-launch, bias toward completing the move over maintaining two paths.

### The Rewrite Trap

Moving code is not rewriting it. If you find yourself changing logic during a structural move, stop — that's two changes conflated into one. Move first, refactor logic in a separate commit.

---

## 12. Escape Hatches

### Principle

> **"Architectural exceptions are allowed but tracked, counted, and time-limited."**

### Exception Format

```python
# ARCHITECTURE-EXCEPTION
# Violation: Direct import from case domain (violates Principle 2)
# Reason: Report PDF generation needs deep case structure access
# Ticket: FMVN-1234
# Approved: @jane on 2026-01-15
# Sunset: 2026-04-15 (90 days)
from faultmaven.modules.case.domain.models.case import Case
```

### Pre-Launch Rule

During active development, escape hatches should not defer cleanup. If the correct location for code is known, move it there. Escape hatches exist for production constraints, not development convenience.

### Automated Enforcement

**Not built.** `scripts/check_architecture_exceptions.py` does not exist, and no `ARCHITECTURE-EXCEPTION` marker appears in the code. The sketch below is the design.

```python
# scripts/check_architecture_exceptions.py
def check_exceptions():
    exceptions = find_all_exceptions()

    # Count check: Alert if too many
    if len(exceptions) > 10:
        warn(f"Exception count ({len(exceptions)}) exceeds threshold")

    # Sunset check: Fail if expired
    for exc in exceptions:
        if exc.sunset_date < date.today():
            fail(f"Expired exception in {exc.file}")

    print(f"Active exceptions: {len(exceptions)}")
```

### CI Integration

Not built: no CI step runs the check. The design:

```yaml
- name: Check architecture exceptions
  run: python scripts/check_architecture_exceptions.py
```

### Quarterly Review

1. Export all active exceptions
2. For each exception older than 90 days:
   - Fix the underlying issue, OR
   - Renew with new sunset date (requires re-approval)
3. Track trend: exception count should decrease

---

## 13. One Owner per Fact

### Principle

> Every fact FaultMaven stores or states has one writer, one point where it is written, and one end of life, and the design records all three. Every other part reads the writer's record.

Ruled on 2026-11-08 ([transcription on #2614](https://github.com/FaultMaven/faultmaven/pull/2614#issuecomment-6716219954)).

- **13.1 Stores.** Every store whose contents outlive a request or cross a process has a row in one store registry: what it holds (customer content, derived data, or operational metadata), its owner kinds (case, conversion, document, user, enterprise, process), its tenant stamp (on the row itself, or derived through the owning row, in which case the retirement path names that join), its writer, where its write sits relative to the turn's commit (inside it, gated on it, or outside it with the reason), its end of life for each owner kind (delete, tenant retirement, wipe), and its retention bound. Principle 3's table rule is the registry's relational rows. A store is written only by the module its row names as its writer.
- **13.2 Derived facts.** A fact that more than one surface states is held in one form only. Either it is a recorded verdict: final, written once by the decider that made it, with the rows and the problem statement it was read under, and later derivations take it as input and never recompute it. Or it is a recomputation: never stored, or stored only as a cache that no surface states; if the two differ, the cache is wrong. Every surface renders the record.
- **13.3 Statements.** The model proposes through structured fields. The engine takes no outcome from prose. Text that states a case outcome is composed by the engine from the record.
- **13.4 Resources, SDK policy and work in flight.** A composition owns what it opens, sets each third-party SDK's process-wide policy once before the SDK is imported, and drains what it started. Resources and work in flight are already built to this (#2503, #2505). SDK policy is not yet (#2616 part (c)).

The registry is [Store Registry](../data-and-storage/store-registry.md).

### Enforcement

One check per clause. This table's Status column is the principle's only status surface.

| Clause | Check that fails on the next violation | Precedent | Status |
|---|---|---|---|
| 13.1 Stores | A census of the store client constructors in `faultmaven/` (ChromaDB, Redis, the trace SDK, object storage, metrics, files under the data root), kept equal to the registry's writer column. An in-process cache cannot be censused this way; it gets its row by review. | `tests/unit/architecture/test_server_written_rows_census_1660.py`, `tests/unit/architecture/test_llm_call_metering_census.py` | Pending: a stage of #2616 |
| 13.2 Derived facts | #2615's pins: a narration package that may not import the derivations; a purity test over a case whose every other attribute raises; a census of derivation calls per function. | `tests/unit/architecture/test_message_rows_have_one_constructor.py` | Pending: #2615 |
| 13.3 Statements | A turn-order test driven through `process_turn`: outcome text is written only by the renderer, after the record. | `tests/unit/architecture/test_turn_commits_once_1882.py` | Pending: #2615 |
| 13.4 Resources, SDK policy and work in flight | Resources and work in flight: compose, shut down, and the process's file descriptors and threads return to their baseline. SDK policy: the trace SDK is imported in one module only, and a subprocess probe shows no crash reporter active. | `tests/integration/container/test_a_composition_closes_what_it_opened.py`; `tests/unit/infrastructure/observability/test_opik_fail_closed.py` (the subprocess probe's pattern) | Resources and work in flight: built (#2503). SDK policy: pending, #2616 part (c) |

---

## 14. Bounded Execution

### Principle

> Every deadline the design states names the mechanism that keeps it.
>
> A thread keeps neither the event loop free nor a deadline: native code that holds the interpreter lock stalls the loop, and cancelling a thread's future does not stop the thread. A thread is the place for I/O, and for native code that releases the lock and has no deadline to keep.
>
> Work over input a user controls, and any work the design gives a deadline, runs where that deadline can stop it: a process the deadline kills, under declared time, memory and output limits.

First application: document preparation; Evidence Tier 1 and the runbook validator gate next ([ruling on #2596](https://github.com/FaultMaven/faultmaven/issues/2596#issuecomment-6716225975)).

### Enforcement

| Check that fails on the next violation | Status |
|---|---|
| A responsiveness test with a stage that holds the interpreter lock in C (a large `re.sub`); it fails on a thread-only design. | Pending: #2596 |
| A census of thread offloads over user-sized input, each naming the mechanism that keeps its deadline. | Pending: #2596 |

---

## References

### Core Documents

- **[ADR-001: Monolith Evolution Strategy](../decisions/adr-001-monolith-evolution-strategy.md)**
- **[Module Organization Design](module-organization-design.md)** — Vertical vs horizontal module organization
- **[Store Registry](../data-and-storage/store-registry.md)** — One row per store (Principle 13.1)

### Supporting Documents

- **[Testing Guide](../../development/testing/guide.md)**

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-01-05 | Original 7 principles |
| 2.0 | 2026-01-09 | Consolidated to 10 principles with enforcement mechanisms |
| 2.1 | 2026-04-16 | P8: dead code intolerance. P11: "Clean Moves, Not Rewrites" with pre/post-launch distinction. P12: pre-launch cleanup rule. |
| 2.2 | 2026-04-19 | P3 table lists synchronized with the storage redesign: removed `sessions` (auth sessions are Redis-only per `case-and-session-concepts.md`), `evidence_artifacts` and `standalone_evidence` (deleted dead-path tables), `agent_tool_calls` v1 (deleted; v2 renamed to canonical `agent_tool_calls`). Added `conversion_jobs` and `conversion_drafts` to case module list. Added `llm_config_overrides` as infrastructure-layer (not a domain module). Cross-reference to `deployment-schema-strategy.md` for the per-table applicability matrix. |
| 2.3 | 2026-11-08 | P13 "One Owner per Fact" and P14 "Bounded Execution" added as ruled ([#2614](https://github.com/FaultMaven/faultmaven/pull/2614#issuecomment-6716219954), [#2596](https://github.com/FaultMaven/faultmaven/issues/2596#issuecomment-6716225975)), each with its enforcement table. P3 and P7 point at the [store registry](../data-and-storage/store-registry.md). P5 names `bootstrap/composition.py` as the composition root. P9's AI evaluation gate and P12's automated enforcement are marked not built. |

### Key Changes Across Versions

| Area | v1.0 | v2.0 | v2.1 |
|------|------|------|------|
| DI Pattern | Service Locator example | Pure Composition Root | (unchanged) |
| Module Communication | Implicit | Explicit contracts | (unchanged) |
| Database Access | Not addressed | Per-module boundaries, N+1 prevention | Table-prefix rule relaxed: unprefixed primary tables, semantic prefixes only for sub-entities |
| Error Handling | Not addressed | Domain exception hierarchies | (unchanged) |
| Observability | Not addressed | Correlation IDs, structured logs | (unchanged) |
| Testing | "Don't decrease coverage" | 70% floor + 85% AI evaluation | (unchanged) |
| AI Architecture | Implicit | Explicit orchestration/adapter boundary | (unchanged) |
| Boundary Enforcement | Not addressed | Import-linter contracts | P8 extended: dead code intolerance |
| Exceptions | Not addressed | 90-day sunsets with automation | Promoted to standalone P12 |
| Code Movement | Not addressed | Not addressed | New P11: clean moves over rewrites; pre/post-launch distinction |

---

**Document Owner**: Engineering Leadership
**Status**: Active
**Last Updated**: 2026-11-08

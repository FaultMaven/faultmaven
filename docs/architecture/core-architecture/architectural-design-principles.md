# FaultMaven Architectural Design Principles

**Version**: 2.16
**Date**: 2026-11-16
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
| 7 | Observability by Default | Correlation IDs, structured logs, traces on external calls; what a log line may hold |
| 8 | Boundary Enforcement | Import-linter enforces rules at build time; dead code is removed, not left behind |
| 9 | Test Safety Net | 70% code coverage floor, enforced by CI; AI evaluation benchmarks designed, not built (#2520) |
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
│  • FileStorageService calls IFileStorageBackend                  │
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
```

No preset sets metrics. Recording is `ENABLE_METRICS` and exposition is `METRICS_EXPORTER`, both default off, outside the presets (see §7).

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

-- Case module (case domain — owns evidence, reports)
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
| Storage backends | Yes (S3, filesystem) | ✅ Yes |
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
| `IFileStorageBackend` | File storage | S3, filesystem |
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

### Key Rules

**One switch, one path.** Every first-party collector, of every type, is created through the metrics shim. The shim returns a real collector only when `ENABLE_METRICS` is `true` and `prometheus_client` imports, and a no-op otherwise. It reads `ENABLE_METRICS` from the environment when a collector is created. The one dotenv loader, `load_settings_dotenv`, fills the environment before any module that creates a collector is imported, so `.env` sets the switch in both run modes. No other variable switches recording. `METRICS_EXPORTER` selects how recorded metrics are exposed (`prometheus_http` mounts `/metrics`; the default is `none`). It switches no collector. Both are needed to scrape.

**Every label is bounded by code or by operator configuration.** A label's values are declared with the collector, and tied by a test to the code that produces them, or bounded by configuration. A request-supplied identifier is mapped onto the bounded set first: a client id onto the configured clients plus `unknown`. A value outside the set raises under tests. In production it is recorded as `other` and logged once per collector and value. A label left "open, with a reason" (`declare_lazy`) is not allowed. A collector with no writer is removed.

**What a log line may hold.** The server log goes to the operator's log pipeline, outside the tenant boundary (the Logs row of the [store registry](../data-and-storage/store-registry.md)). Six rules govern every output in the outputs table below ([ruling on #2886](https://github.com/FaultMaven/faultmaven/issues/2886#issuecomment-6947540305)); that table and the Enforcement rows say which are kept today:

1. **What a line holds.** A log line holds operational metadata: identifiers, counts, lengths, durations, states, class names, and literals our code wrote. A message that interpolates an exception's text, a payload, or a user-supplied value is not metadata. A first-party exception contributes its class and its `error_code` ([exception contract](../specifications/exception-contract.md)), not its message, because its message may embed a third party's value (the pattern #2868 fixed).
2. **No credential value, at any level.**
3. **No case content, at any level, DEBUG included.** Case content is evidence text, a question or search query, a case or runbook title, a prompt, or a model's reply. A line names the content's identifier and length instead.
4. **An exception renders as frames and class names only.** It is rendered as `exception_frames` (`utils/exception_name.py`) renders it: each link's class, then its frames, never a message. It follows `__cause__` and unsuppressed `__context__`, descends into `ExceptionGroup` members (`api/protection.py:403` raises one) as `traceback` caps it (15 members, 10 groups deep, the rest counted; a member already shown is listed as shown above), and drops `__notes__`. For an `exc_info` tuple, the head link's frames are the tuple's traceback. A JSON line holds the render under `frames`, the field `log_error` and the logging policy write, and a caller's own `frames` wins over `exc_info`; a console line shows it as its stack block. An exception value in a log call's fields renders as its class chain where the chain reaches it, and only there ([ruling on #2888](https://github.com/FaultMaven/faultmaven/issues/2888#issuecomment-6971596188)):
   - a top-level field (`error=e`, a stdlib `extra={"e": e}`, a native structlog event that is itself an exception), and a value of the `extra` dict a line carries, one level down, where the chain nests a caller's field that collides with a formatter key;
   - under JSON, an exception the encoder reaches: an item of a `list` or `tuple`, or a `dict` value, at any depth, subclasses included whatever their `__repr__`, since the encoder serializes them item by item.

   Anything else renders through a repr, an exception inside it included: under JSON, an object the encoder cannot serialize (a `set`, a `deque`, a dataclass, any other object); under the console renderer, every container. On the stdlib path the event is the record's message, `record.getMessage()`, so `logging.getLogger(...).error(e)` writes `str(e)`: that is rule 1's message channel (S3, #2889), not a field. A record the renderer cannot render is not written: under JSON, a key `json.dumps` refuses (anything but a `str`, `int`, `float`, `bool` or `None`, such as an exception, a tuple, a UUID or a date) or a circular reference; under either renderer, a repr that raises, or a stdlib record whose `%` arguments do not format (a native structlog event's are interpolated in the logger's chain, so a call such as `get_logger(...).error("ev %s %s", "x")` raises `TypeError` at the call site instead). `logging`'s `handleError` then writes the failure's own traceback, whose exception message is in every dump (a repr that raises prints its message), and the record's message and arguments (for a native structlog event, its whole event dict), or, when a repr in them raises too, "Unable to print the message and arguments - possible formatting error.", to stderr, unredacted and outside the chain. Every handler of the chain replaces it (`ChainStreamHandler.handleError`, `infrastructure/logging/config.py`), and so does `logging.lastResort`'s: one fixed line naming the failure's class and the record's logger, nothing from the record (#2898). Keeping an exception the chain does not reach off the line is rule 1's, at the call site (S3, #2889), as is a message that interpolates the exception's text. Chains stay intact: `from None` is not how a site satisfies this rule, because `modules/agent/tools/base.py` reads the cause chain.
5. **One chain for every output.** Every output in the outputs table renders by serializing the record through one chain, never by pasting text into a template. The redactor runs last on every output. The worker child forwards a logged exception as frames, not text. Any output left out of the table is listed with its reason.
6. **The redactor is defence in depth, not the control.** The credential names and forms it blanks are the redactor table below, and extending that table is a design edit, not a per-issue ruling.

**The outputs.** Every output a log line leaves by, and whether it keeps the rules today; an output that does not names the stage that brings it in, or is excluded with its reason. Paths are `faultmaven/` relative unless they start with `alembic`.

| Output | Configured at | In the chain today | Stage |
|---|---|---|---|
| The API process, uvicorn included | `infrastructure/logging/config.py`: `build_log_formatter` (`ProcessorFormatter`: `exc_info_as_frames`, `exceptions_as_class_chains`, the redactor last) on the root handler; uvicorn through `server.py`'s `log_config=None` | The exception's render, yes: frames on the traceback channel, class chains in fields as far as rule 4 states. A message that carries `{e}` is the message channel | Render delivered: S2a, #2888; message channel: S3, #2889 |
| The startup migration's subprocess | `bootstrap/data_init.py`'s `run_alembic_migrations` spawns `fm-migrate` (`cli/migrate.py`) with stderr inherited, the child's own chain; its stdout is counted and dropped. `fm-migrate` gates on `DATABASE_URL` alone, as `alembic/env.py` does, never on the whole settings. The parent logs the exit code and the byte count, and its `RuntimeError` names the exit code alone | Yes | Delivered: S2b, #2888 |
| The maintenance jobs | `jobs/run.py`'s `setup_logging`: `install_log_handler` (`infrastructure/logging/config.py`), the API's `build_log_formatter` on stdout, JSON or console by the log format alone (`LOG_FORMAT`, else `LOG_OUTPUT_FORMAT`, as the API reads it); the API's `get_logger` in a job adds no second handler. `install_log_handler` also configures structlog (`configure_structlog_chain`, the API's call), so a structlog event is a chain line with its fields, and installs the process outputs below; `main` installs them as its first statement | The render and the serialization, yes: the API's chain. `jobs/run.py:504`'s `"Job failed: {e}"` is the message channel | Render delivered: S2a, #2888; message channel: S3, #2889 |
| A job's error `print` | `jobs/run.py:738`: `print(f"Error: {e}")` to stderr | No | S3, #2889 |
| The alembic migration Job | `alembic/env.py`: `install_log_handler` on stderr, the API's chain; `alembic.ini` sets the levels only | Yes. A migration's uncaught exception reaches the chain's `sys.excepthook` (`install_log_handler` installs it) | Records delivered: S2a, #2888; the hook: S2b, #2888 |
| Alembic's `CommandError` under `fm-migrate` | `cli/migrate.py` calls `alembic.command.upgrade` itself, so a `CommandError` is an uncaught exception: the hook renders its class and frames, exit 1 | Yes | Delivered: S2b, #2888 |
| The `alembic` CLI | `alembic.util.err` prints `FAILED: <str(e)>` to stdout and logs the same text | No | Excluded: the third-party CLI owns its print and its log line. For the migration Job, until its command runs `fm-migrate` (a follow-up on #2886, after this image ships); for developer use, permanently |
| The worker child's forwarded records | `infrastructure/worker_process.py`: `_Forward` and `serve`'s failure record send `exception_frames` as their own element; the parent re-logs each record through the API chain with it as `frames` | The render, yes. | Render delivered: S2a, #2888; its readers log no failure text: S3b, #2889 |
| The worker child's stderr | piped: `infrastructure/worker_process.py`'s `run` drains it in fixed-size chunks and keeps none of it (the child points its fd 1 at it). After the reap one record logs the byte count and the exit code: INFO on an end with its result or exit 0, WARNING on any other | Metadata only | Delivered: S2b, #2888 |
| Tracebacks printed or pasted | `container/providers/services.py`'s five repository warnings and `_container_impl.py`'s critical initialization error log `exc_info`, no longer `print_exc()` or `format_exc()` text | The render, yes. The `{e}` in the five warnings and in `_container_impl.py:166`'s `"DI Container initialization failed: {e}"` is the message channel | Render delivered: S2a, #2888; message channel: S3, #2889 |
| An image launched through the `uvicorn` CLI rather than `run_server` | the CLI cannot set `log_config=None`, so uvicorn installs its own handlers; `install_process_outputs` (`infrastructure/logging/process_outputs.py`), run by the launcher, by `faultmaven.asgi:create_app` and by the API worker's chain, removes the handlers on `uvicorn`, `uvicorn.error` and `uvicorn.access`, and `uvicorn.access` stays off (it does not propagate) | Yes. Residual: a CLI-launched supervisor (several workers, or the reloader) imports nothing of ours, so its process start and stop lines are uvicorn's. No shipped launch path uses the CLI (`Dockerfile:154`, `scripts/faultmaven-dev.sh:284`) | Delivered: S2b, #2888 |
| uvicorn's supervisor and its worker children | `run_server` installs the process outputs as its first statement, so a supervisor of several workers or the reloader has them. Each child calls `faultmaven.asgi:create_app` (`run_server`'s core app, a factory: `factory=True` for it alone), which installs them and then imports `faultmaven.main`; a failure there is one CRITICAL record with frames and `SystemExit(1)`, which `multiprocessing` exits on without printing | Yes. Residual: a child's exception outside the app's import (uvicorn's own) is printed by `multiprocessing`; a launch of another app string (the Cloud image's) has its children covered from that app's import on | Delivered: S2b, #2888 |
| Records emitted before logging is configured | `logging.lastResort`: `faultmaven/__init__.py` installs `_last_resort.py`'s `LastResortHandler`, the chain's JSON formatter on stderr at WARNING, so every process that imports `faultmaven` has it. It imports the chain before it takes its lock; a record logged while it renders, one it has no formatter for, or a render that fails, is a fixed line | Yes | Delivered: S2b, #2888 |
| A record a handler cannot render | `logging`'s `handleError`, replaced on every handler of the chain (rule 4) | A fixed line: the failure's class and the logger | Delivered: S2b, #2898 |
| The uncaught-exception hooks | `sys.excepthook` (CRITICAL), `threading.excepthook` and `sys.unraisablehook` (ERROR), each one record with `exc_info`, installed by `install_process_outputs` at process entry while the hook is the interpreter's default (a test runner's or a debugger's stands). `KeyboardInterrupt` goes to the default hook; an unraisable's object is never written. So an uncaught error in the launcher, a job or an `fm-*` command shows the operator its class and frames, with no reason text. Where our code refuses with a remedy, it logs the remedy first, at ERROR, as a fixed literal with no interpolated count, path or value: migrations 005, 007, 008, 013, 018 and 022, `alembic/env.py` for a statement row-level security or a missing privilege refuses (SQLSTATE 42501), `LocalJwtSecretError`, and a settings failure (`get_settings`) | Yes; a hook whose logging fails writes a fixed line | Delivered: S2b, #2888 |
| `warnings` | `warnings.showwarning`, installed the same way: the category's class, the file and the line, never the message. A call with a `file`, and a warning `catch_warnings(record=True)` records, go to the original | Yes | Delivered: S2b, #2888 |
| asyncio's default exception handler and Starlette's lifespan failure | a filter on the `asyncio` and `uvicorn.error` loggers keeps a message's first line: it drops the `future:` and `handle:` reprs (and a callback's repr, from "Exception in callback"), and the `traceback.format_exc()` Starlette pastes into a lifespan failure. `bootstrap/lifespan.py` logs a startup or a shutdown failure with `exc_info` first, so its frames are on the line | Yes | Delivered: S2b, #2888 |
| A third party's own handlers | `sweep_own_handlers` (`infrastructure/logging/process_outputs.py`), at process entry, after the embedding model loads (`infrastructure/model_cache.py`) and at the end of the API's startup: every logger's handler but the chain's and a `NullHandler` is removed, and that logger propagates (`uvicorn.access` excepted). `infrastructure/observability/opik_sdk.py` strips `opik`'s and `sentry_sdk.errors`' where it imports opik. `DATABASE_ECHO` sets the `sqlalchemy.engine` logger's level instead of `echo=`, which added SQLAlchemy's stdout handler | Yes. Residual: a handler a library adds after the last sweep, in a job or a command | Delivered: S2b, #2888 |
| structlog in a process the chain has not configured | `install_log_handler` and the API's chain both call `configure_structlog_chain` | Yes. Residual: an event emitted before any install goes to structlog's default `PrintLogger` on stdout | Delivered: S2b, #2888 |
| `fm-*` commands run in a pod | each command module imports `cli/_log_chain.py` before its own imports, which installs the process outputs, and its `main` calls `install_command_logging` first: the chain on stderr (stdout is the operator's). Their `print` calls in `cli/` | The records and the process outputs, yes. The `print` calls, no | Records: S2b, #2888; `print`: S3, #2889 |
| `SystemExit` with a string | the interpreter prints a non-integer exit code to stderr itself, outside every hook: `config/tls_root_set.py`'s refusals, `main.py`'s `__main__` guard, and `multiprocessing`'s exit of a child with one | No | S3, #2889: the text is ours, but several of these interpolate an environment value or a path, which is S3's census (a fixed refusal, `exit_refusing`'s shape, or a logged literal) |

**The redactor table.** What the redactor (`redact_log_line`, `infrastructure/logging/url_redaction.py`) blanks on a rendered line.

| Name or form | Status |
|---|---|
| URL query strings (`redact_urls`) | Enforced |
| The values of `set-cookie`, `cookie`, `authorization` and `proxy-authorization`, also after a `_` or `.` segment (`HTTP_AUTHORIZATION`, `HTTP_COOKIE`) | Enforced (#2865) |
| `x-api-key`, `api-key`, `x-goog-api-key`, `x-auth-token`; `Set-Cookie2` | Enforced (#2891) |
| `www-authenticate` | Excluded: a challenge, not a credential |
| Flat pairs or lists | Excluded: they cannot be told from a scopes list |
| URL-encoded headers, value-first records, unusual YAML | Excluded: rules 1–4 keep such text off the line |

### Enforcement

| Rule | Check that fails on the next violation | Status |
|---|---|---|
| One switch, one path | Importing each collector-defining module with the switch off registers nothing. Outside the shim, no module imports from `prometheus_client` beyond a listed set of exposition, registry and parser names, and none takes a `Prometheus*` class from the shim, by import or attribute (#2745, S1's pin 2). | Enforced by `tests/unit/infrastructure/shims/test_metrics_one_path_2748.py` |
| Bounded labels | S2's pins 4-6: each label's vocabulary tied to its producer, an out-of-set value raising under tests, and no collector without a writer. | Pending: #2749 |
| The redactor table's enforced rows (rule 6) | A URL query string and a credential header value are blanked on the API process's rendered lines (in a message, a field or a traceback) and in the jobs' rendered message, at a cost linear in the line. Not tested: a job's traceback. Not covered: `jobs/run.py:738`'s print. | Enforced by `tests/unit/infrastructure/logging/test_credential_header_redaction_2865.py`, `tests/unit/infrastructure/logging/test_credential_header_redaction_2891.py` and `tests/unit/infrastructure/logging/test_url_query_redaction.py` |
| Frames and class names; one chain (rules 4 and 5) | A chained exception whose cause, context, group member and note each carry a marker renders each link's class and frames with no marker: on the API chain (`exc_info` in any shape, JSON and console), the jobs' line through `setup_logging` (JSON and console), alembic's (a record logged before `fileConfig` included, each line written once), a worker's forwarded and failure records, and the `frames` of a repository warning and of the container's critical error (whose messages are S3's). An exception field renders as rule 4 states, both sides of its boundary pinned: a top-level field, a stdlib `extra=`, a native structlog event and the chain's nested `extra` as the class chain on both renderers; a list, tuple, dict value and list subclass by the JSON encoder; a set and a dataclass under JSON, and a list and a list subclass with its own `__repr__` under console, through their repr; a stdlib `error(e)` writes `str(e)` as its message, rule 1's channel. A jobs JSON line parses to the API's keys, lowercase level and ISO timestamp included, plus the record's fields, whatever its message holds; the log format alone picks JSON, `LOG_OUTPUT_FORMAT` included; a malformed `.env` line's warning and an unrecognized format's warning are each a chain line, and alembic writes its format warning once (the dotenv warning repeats with each later `.env` load, `apply_root_set`'s in alembic and several in a job, and a job's format warning repeats when `LoggingSettings()` validates it again); a job writes each record once. A line without an exception renders as on `main` at 19ab0e8b8 (whole lines, JSON and console). A group shows 15 members and counts the rest. The process's own outputs (S2b): a record before any configure renders as a JSON line, `lastResort`'s; a record the renderer refuses (a tuple key under JSON, a raising repr) is the fixed line on each chain handler; an uncaught exception in the main thread, a thread and a `__del__`, an unretrieved task's exception and a callback's, a `UserWarning` (both chains) and a never-awaited coroutine's warning render class, file, line and frames with no marker, and a hook already set stands; uvicorn's three loggers hold no handler after `LOGGING_CONFIG`; `DATABASE_ECHO`'s statements and the embedding model's libraries log through the chain; a job's structlog event is a chain line with its fields; every `fm-*` command installs the chain; a worker child's stderr (past 1 MB) reaches no output and only its size is logged; a failing migration's stdout and stderr, its `CommandError` included, reach no parent line, exception or fd 2; a child's failed app import and a startup or shutdown failure log frames, no traceback text; `LastResortHandler` imports the chain outside its lock; opik's and `sentry_sdk.errors`' handlers are gone once opik is imported; a refusal's remedy literal is on stderr (migrations 005, 008 and 018, the row-level security refusals, `LocalJwtSecretError`); `fm-migrate` migrates under settings the API would refuse and writes no secret file. | Enforced by `tests/unit/infrastructure/logging/test_exceptions_render_as_frames_2888.py`, `tests/integration/test_alembic_logs_through_the_chain_2888.py`, `tests/unit/utils/test_exception_name.py` and, for the process's own outputs, `tests/unit/infrastructure/logging/test_process_outputs_2888.py` |
| What a line holds: the message channel (rule 1) | No site the census defines is added anywhere, and none remains in the areas S3 has switched (`infrastructure/llm/`, the engine, evidence and knowledge areas): its allowlist lists the rest, pending S3's later sub-stages. A switched site names an exception by `exception_log_name` and output by exit code and length. At the sites the render test drives, a marker in the exception's text, the subprocess's output or the response body is on no rendered line: the registry's provider-failure and chain-failure lines, the router's chain failure, the local service manager's failed-script, stop and start-output lines, the cache-breakpoint refusal, the pricing override, the call meter, the strict-schema fallback, the local provider's validation, request and HTTP-error lines, the engine's LLM error handler, tool-loop re-issue and case telemetry, an agent tool's failure, the two worker-failure readers, and the knowledge base's collection, search, reindex, ingestion, refused-PDF-page and runbook-gate lines. | Partly enforced by `tests/unit/architecture/test_log_message_channel_census_2889.py`, `tests/unit/infrastructure/llm/test_log_lines_name_exceptions_2889.py`, `tests/unit/core/investigation/test_log_lines_name_exceptions_2889_s3b.py` and `tests/unit/modules/knowledge/test_log_lines_name_exceptions_2889_s3c.py`; pending: S3, #2889 |
| No case content (rule 3) | A marker in the content is absent from the rendered line at every level, at each of the sites S4 counted (53). Not covered: a site added after it. | Enforced at those sites by `tests/unit/infrastructure/logging/test_log_lines_hold_no_case_content_2890.py` |

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

> **"70% code coverage floor, enforced by CI, plus evaluation benchmarks for AI behavior."**

### Two Testing Dimensions

| Dimension | What It Tests | Target |
|-----------|---------------|--------|
| **Code Coverage** | Lines executed | The overall floor, enforced by CI (Quality Gates) |
| **AI Evaluation** | Output accuracy | ≥85% on benchmark |

### Code Coverage by Layer

These per-layer figures are targets with no gate: CI gates only the overall total.

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
- ✅ Overall coverage cannot drop below 70%: the Test Standalone and Test Cloud jobs fail under the floor `COVERAGE_FAIL_UNDER` sets in `.github/workflows/ci-cd.yml`
- ⏳ AI evaluation accuracy ≥85%: not built (#2520)

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

Ruled on 2026-11-08 ([transcription on #2614](https://github.com/FaultMaven/faultmaven/pull/2614#issuecomment-6716219954)). The full text adopted is recorded in the [ruling on #2617](https://github.com/FaultMaven/faultmaven/issues/2617#issuecomment-6718120222).

- **13.1 Stores.** Every store whose contents outlive a request or cross a process has a row in one store registry: what it holds (customer content, derived data, or operational metadata), its owner kinds (case, conversion, document, user, enterprise, process), its tenant stamp (on the row itself, or derived through the owning row, in which case the retirement path names that join), its writer, where its write sits relative to the turn's commit (inside it, gated on it, or outside it with the reason), its end of life for each owner kind (delete, tenant retirement, wipe), and its retention bound. Principle 3's table rule is the registry's relational rows. A store is written only by the module its row names as its writer.
- **13.2 Derived facts.** A fact that more than one surface states is held in one form only. Either it is a recorded verdict: final, written once by the decider that made it, with the rows and the problem statement it was read under, and later derivations take it as input and never recompute it. Or it is a recomputation: never stored, or stored only as a cache that no surface states; if the two differ, the cache is wrong. Every surface renders the record.
- **13.3 Statements.** The model proposes through structured fields. The engine takes no outcome from prose. Text that states a case outcome is composed by the engine from the record.
- **13.4 Resources, SDK policy and work in flight.** A composition owns what it opens, sets each third-party SDK's process-wide policy once before the SDK is imported, and drains what it started. The root set a TLS client verifies the peer against is such a policy:

  > Every TLS client in the process verifies against one named root set, set in the composition before any client is imported; an operator extends it in one place; a client that cannot read it is listed as an exception with its reason.

  The set is the operating system's CA bundle, and an operator adds a private CA through one documented setting that every client reads ([ruling on #2685](https://github.com/FaultMaven/faultmaven/issues/2685#issuecomment-6754909029)). Because the composition sets it, the rule covers the local process (`scripts/faultmaven-dev.sh`) as well as the images. The setting is `TLS_EXTRA_CA_FILE`, a PEM file of CA certificates (#2692). `apply_root_set` (`faultmaven/config/tls_root_set.py`) is its one writer and the first statement of `run_server`, each `fm-*` `main()`, the jobs runner, `alembic/env.py` and the demo seeder. It names the set in every variable a client reads: `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `AWS_CA_BUNDLE`, `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` and `PGSSLROOTCERT` name one file, and `SSL_CERT_DIR` an empty directory the package ships, so no capath is a second source. A certificate is a PEM block labelled `CERTIFICATE`. The OS bundle is, in order: on native Windows, the CA and ROOT certificate stores' server-auth roots, read as CPython's `load_default_certs` reads them, when the store yields any; else OpenSSL's default file or a distribution bundle file; else the certificates in OpenSSL's certificate directory. Unset, the file is the OS bundle file, or, when the bundle comes from the Windows store or OpenSSL's directory, the written file below; set, it is the bundle, a newline, then the setting's certificates re-encoded through DER (never its keys or other blocks), at `<tmp>/faultmaven-tls-<uid>/<sha256>.pem` in a directory only that user can write: one file per content, reused by every start that computes it and never deleted. An apply that finds the file missing or altered (a host that ages `/tmp` can remove it under an idle process) re-writes it. The set is read once per start, so a change takes effect at the next start. An inherited file variable is accepted only when its real path is the file the process names, or its bytes are exactly the bytes it would write; every file variable is then exported as the process's own path. No inherited file's certificates are compared, because a PEM parser and OpenSSL can disagree on which blocks a file holds (ruling 1 on #2692, narrowed 2026-11-12): a Nix or conda bundle with the same roots in other bytes refuses, and the remedy is to unset it or put its CA in `TLS_EXTRA_CA_FILE`. An inherited `SSL_CERT_DIR`, read as an `os.pathsep` list as OpenSSL reads it, is accepted only when no element holds a hashed entry. The start is refused when no OS bundle loads, the setting holds no certificate or an `X509` or `TRUSTED CERTIFICATE` block, no temporary directory is usable, the file cannot be written safely, a written set does not load, or an owned variable is anything else ([ruling on #2692](https://github.com/FaultMaven/faultmaven/issues/2692)); for a refused file in a `faultmaven-tls-<uid>` directory, the refusal says it is a set file from an earlier start that is missing, altered or not this start's set, and to restart that process or unset the variable. The Postgres drivers read roots through inputs of their own besides `PGSSLROOTCERT`, and the set owns them by refusal ([#2787](https://github.com/FaultMaven/faultmaven/issues/2787)): a CRL source, whose every certificate asyncpg and libpq load as a trusted root (`PGSSLCRL`, `PGSSLCRLDIR`, a URL's `sslcrl` or `sslcrldir`, `root.crl` under `~/.postgresql` in `Path.home()` (`USERPROFILE` on Windows) or the effective user's password-database home, and on Windows under libpq's `%APPDATA%\postgresql`, the folder resolved as libpq resolves it), and a service-file section, whose `sslrootcert` and `sslcrl` win over the environment (`PGSERVICE`, a URL's `service`). libpq, the migration process, reads all of them; asyncpg, the app's driver, reads a CRL file through `PGSSLCRL` or the home default, and the URL keys and a service file only when handed a DSN. No file is safe to export in their place: an empty one does not load in asyncpg, and any file that loads turns on CRL checking. So each one present at start refuses it: either variable or `PGSERVICE` with any value, a `DATABASE_URL` query, read as the engine's `make_url` reads it, naming `sslcrl`, `sslcrldir`, `service` or `dsn` (which carries any of them), or holding any key that is not a plain option name (`[a-z][a-z0-9_]*`: psycopg2 writes a key into the conninfo unescaped, so `?application_name%3D=%20sslcrl%3D/z` hands libpq `sslcrl`; such a key is refused by the rule, not named), and a `root.crl` file in either home. The refusal names the input and never the URL. `pg_connect_args` and alembic's `get_database_url` refuse the same URL keys, for an engine URL the start's check does not read. Postgres CRL checking is not supported under the set. `apply_root_set` also sets `PGGSSENCMODE=disable` unless it is set, so libpq negotiates no GSSAPI encryption in place of TLS unless the URL's `gssencmode` or the environment names a mode ([#2788](https://github.com/FaultMaven/faultmaven/issues/2788)). Like every other root input, these are read once per start. The census below gives each client's root source and how it reads the set.

The registry is [Store Registry](../data-and-storage/store-registry.md).

**The TLS client census (13.4).** Every TLS client the [design check on #2685](https://github.com/FaultMaven/faultmaven/issues/2685) names: the root source it reads today, and the variable or API through which it reads the set, or why it is an exception. Each row marked *measured* accepts a private CA given through `TLS_EXTRA_CA_FILE` and refuses it without the set, by loopback handshake (`scripts/check_tls_root_set.py`). Versions are the lockfile's (`requirements/cloud.txt`; the Slack agent's are its `requirements.txt`). Each root source was read in that version's source, or for curl and git in the binaries of the published core image, and is cited on the stage's PR (#2704).

| Client, and where FaultMaven uses it | Root source today | How it reads the set, or why it is an exception |
|---|---|---|
| aiohttp 3.14.5: the 15 `aiohttp.ClientSession(` sites (the LLM providers, the local model manager, alerting, APM) | OpenSSL's default paths, through one `ssl.create_default_context()` aiohttp builds when it is imported | `SSL_CERT_FILE` (the set) and `SSL_CERT_DIR` (empty), both set before aiohttp is imported; its import-time context holds the set. Measured |
| slack-sdk 3.46.0: the Slack agent's `WebClient` | OpenSSL's default paths, through urllib's default context (the agent passes no `ssl`) | Unchanged: the Slack agent's process is its own stage (#2733) |
| curl: the core image's health probe and `scripts/faultmaven-dev.sh`, plain HTTP to localhost only | The OS bundle, libcurl's compiled default file and directory | Exception: it speaks plain HTTP to localhost, and the probe runs outside the processes FaultMaven launches. In them `CURL_CA_BUNDLE` names the set, and curl still reads its compiled default capath |
| git: installed in the core image; nothing in the image build or `faultmaven/` runs it | The OS bundle, the compiled default of the GnuTLS libcurl its HTTPS helper links | Exception: nothing runs it. Its default is the OS bundle; it is extended only through `GIT_SSL_CAINFO` / `GIT_SSL_CAPATH` or `http.sslCAInfo` |
| httpx 0.28.1: `web_search` (the Tavily SDK's client and the Google CSE call); the Slack agent's core-API client and its file downloader | certifi | `SSL_CERT_FILE`'s file alone (with the default `verify` and `trust_env`), so it trusts exactly the stdlib's roots. Measured |
| The ChromaDB client 1.5.8 (`chromadb.HttpClient`, with `CHROMADB_URL` set) | certifi, through httpx; FaultMaven sets no `chroma_server_ssl_verify` | As httpx |
| `huggingface_hub` 1.11.0: the BGE-M3 download, at image build and on first load in the local process (the image runs with `HF_HUB_OFFLINE=1`) | certifi, through httpx | As httpx. Build-time exception: a `RUN` step does not apply the set (`Dockerfile:63`) |
| `hf_xet`: `huggingface_hub`'s Rust transfer client | The OS store, through `rustls-native-certs` | Exception: no Python SSL context to measure. It reads `SSL_CERT_FILE` (the set), and the image runs offline |
| requests 2.33.1: tiktoken's encoding download (at image build; on first use in the local process), the Presidio session (its analyzer calls and both services' health probes), the local Opik health probe | certifi | `REQUESTS_CA_BUNDLE`, then `CURL_CA_BUNDLE`; both name the set. Measured. Build-time exception: the tiktoken download (`Dockerfile:78`) |
| botocore 1.43.119: the S3 storage backend | certifi (its own `cacert.pem` only when certifi is not installed) | `AWS_CA_BUNDLE`, the set. Measured |
| opik 1.11.14: the trace SDK's REST client | certifi, through httpx | `SSL_CERT_FILE`'s file alone becomes its `verify`. Measured |
| redis 7.4.0 under TLS (`rediss://`) | OpenSSL's default paths | As aiohttp. Measured |
| asyncpg 0.31.0 (the app's Postgres driver) and psycopg2 2.9.11 / libpq (the migration process's) | `sslrootcert`, `PGSSLROOTCERT` or `~/.postgresql/root.crt`; without one, `sslmode=require` encrypts without verifying. Also a CRL source (`PGSSLCRL`, `PGSSLCRLDIR`, a URL's `sslcrl` / `sslcrldir`, `~/.postgresql/root.crl`), loaded as trusted roots, and a service file (`PGSERVICE`, a URL's `service`), whose `sslrootcert` / `sslcrl` win; libpq reads every one, asyncpg the CRL file, and the URL keys and service file only when handed a DSN | `PGSSLROOTCERT`, the set, so `require` (SQLAlchemy's `?ssl=require`) verifies the chain; a private-CA Postgres needs its CA in `TLS_EXTRA_CA_FILE`. Under libpq's default `sslmode=prefer` (the migration process), libpq verifies against the set and, when that fails, connects in plaintext: put the server's CA in `TLS_EXTRA_CA_FILE`, or set `sslmode=require` or `verify-full` in `DATABASE_URL` to fail closed (ruled 2026-11-12 on #2692). The API's asyncpg path under `prefer` is unaffected. A CRL or service-file input present refuses the start, a URL's `dsn` included, and `PGGSSENCMODE` defaults to `disable` (#2787). Measured: asyncpg `verify-full` and `require`, psycopg2 `verify-full`; the CRL inputs, which refuse the start applied and make an outside-CA server verify bare (asyncpg and psycopg2 through `PGSSLCRL`, asyncpg through the home `root.crl`) |
| litellm (its `HTTPHandler`), sentry-sdk (its transport pool), WorkOS (its httpx client) | certifi | `SSL_CERT_FILE` (sentry-sdk then `REQUESTS_CA_BUNDLE`); WorkOS as httpx. Measured |
| Per-client overrides: `SSL_VERIFY` (litellm), `OPIK_CHECK_TLS_CERTIFICATE`, `rediss://…?ssl_cert_reqs=none`, `sslrootcert=` in `DATABASE_URL`; `OTEL_EXPORTER_OTLP_*CERTIFICATE`; `WEBSOCKET_CLIENT_CA_BUNDLE` | The operator's explicit value | Exceptions, unchanged: the first four turn one client's verification off or name its roots; core constructs no OTLP exporter; no core client reads the last |
| gRPC (grpcio 1.80.0, installed for chromadb and the OTLP gRPC exporter): FaultMaven opens no TLS channel at this commit | Its packaged `roots.pem`, which holds both roots `scripts/check_image_trust_store.py` bans; in the core image linked to the OS bundle | `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`, the set, which replaces `roots.pem` for `ssl_channel_credentials()`. Measured. The link is the default in a process the set was not applied to, and only the image check covers it, requiring every `roots.pem` in the image to be the cafile: the handshake check's applied leg names the set |
| pip: image builds; the core image's base ships pip 24.0, the Slack agent's (`python:3.12-slim`) pip ≥ 24.2 | Core image (pip 24.0): the `PIP_CERT` the core `Dockerfile` sets to the OS bundle, which replaces the vendored bundle; that bundle, read by `pip --isolated` or a pip without `PIP_CERT`, the `Dockerfile` links to the OS bundle. Slack agent image (pip ≥ 24.2): truststore (OpenSSL's default paths), plus the vendored bundle, which its `Dockerfile` links to the OS bundle, plus `PIP_CERT` | pip < 24.2: `PIP_CERT`. pip ≥ 24.2 loads truststore (OpenSSL's default paths), its vendored bundle and `PIP_CERT` together; `PIP_USE_DEPRECATED=legacy-certs` (`--use-deprecated=legacy-certs`) turns truststore off, and `PIP_CERT` then replaces the vendored bundle. The rule: every pip's vendored bundle must be the OS bundle, by link, so a pip that is run `--isolated` or without `PIP_CERT` still verifies against the OS bundle. Each image must link the bundle and set `PIP_CERT` (the core and Slack agent images do so); `PIP_CERT` alone is bypassed by `pip --isolated` or a scrubbed environment (`--isolated` still honours `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE`), which is why the link is the mechanism. The limit: a reinstall of pip (`--force-reinstall` replaces the link with a regular file), a `--user` install that shadows it, a venv created later, a pip config file that names `cert` (the global and site files and `PIP_CONFIG_FILE`, which `--isolated` still loads), and an operator's own `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE` outside a process that applied the set (`--isolated` still honours them; they replace `PIP_CERT`, and the linked bundle too on a pip < 24.2 or under `legacy-certs`, while a pip ≥ 24.2 adds them to it) are not covered, so the guarantee is the image as shipped ([ruling on #2756](https://github.com/FaultMaven/faultmaven/issues/2756)) |
| The ensurepip pip wheel (`ensurepip/_bundled/pip-*.whl`, which the base ships) | The vendored certifi inside the wheel | A file the base ships, not a client in the process. The rule: the image ships no pip wheel that holds a banned root, so the core `Dockerfile` removes the ensurepip pip wheel and `python -m venv` in the image needs `--without-pip` (nothing in core creates a venv). The rule, and the pip bundle's link, hold for the image's filesystem: the base's lower layer still holds the original wheel and bundle bytes, which `docker save` shows and no process in the container can read |

### Enforcement

One check per clause. This table's Status column is the principle's only status surface.

| Clause | Check that fails on the next violation | Precedent | Status |
|---|---|---|---|
| 13.1 Stores | A census of the store client constructors in `faultmaven/` (ChromaDB, Redis, the trace SDK, object storage, metrics, files under the data root; for metrics, a collector created outside the shim), kept equal to the registry's writer column. An in-process cache cannot be censused this way; it gets its row by review. | `tests/unit/architecture/test_server_written_rows_census_1660.py`, `tests/unit/architecture/test_llm_call_metering_census.py` | Pending: a stage of #2616 |
| 13.2 Derived facts | #2615's pins: a narration package that may not import the derivations; a purity test over a case whose every other attribute raises; a census of derivation calls per function. | `tests/unit/architecture/test_message_rows_have_one_constructor.py` | Pending: #2615 |
| 13.3 Statements | A turn-order test driven through `process_turn`: outcome text is written only by the renderer, after the record. | `tests/unit/architecture/test_turn_commits_once_1882.py` | Pending: #2615 |
| 13.4 Resources, SDK policy and work in flight | Resources: compose, shut down, and the process's file descriptors and threads return to their baseline. Work in flight: the drain waits for work within its bound, cancels the cancellable work past it, awaits a settlement to its end, and the last release drains before the stores close. SDK policy: the trace SDK is imported in one module only, and a subprocess probe shows no crash reporter active. | Resources: `tests/integration/container/test_a_composition_closes_what_it_opened.py`. Work in flight: `tests/unit/infrastructure/test_inflight_record_2096.py`, `tests/unit/container/test_container_shutdown.py` (`test_the_last_release_drains_then_resets_then_closes_then_stops_the_process`). SDK policy: `tests/unit/architecture/test_opik_imported_in_one_module_2620.py`, and `tests/unit/infrastructure/observability/test_opik_fail_closed.py` (`TestCrashReporting`) | Resources: built (#2503). Work in flight: built (#2505). SDK policy (the trace SDK): built (#2620) |
| 13.4 TLS root set | #2692's pins: a client built with defaults (aiohttp, httpx, requests, botocore, gRPC) loads the named set, measured from its own SSL context, not the environment; the operator's one setting adds a private CA that each of those clients accepts, in the image and in the local process; and the image check requires every pip's vendored bundle the image holds to be the cafile (certificate-set equality with the cafile, which stays `PIP_CERT`) and every pip wheel the image holds to be absent or hold no banned root, whatever the pip version, reading no `PIP_USE_DEPRECATED`, so `legacy-certs` is no exemption. This supersedes #2692's pin 3, which held only on a pip ≥ 24.2 base ([ruling on #2756](https://github.com/FaultMaven/faultmaven/issues/2756)). `pip --isolated` and a pip without `PIP_CERT` must verify against the OS bundle in the built image, measured from pip's own session by `scripts/check_pip_root_set.py`. | `scripts/check_image_trust_store.py` and `scripts/check_pip_root_set.py` at the image build (`tests/unit/scripts/test_check_image_trust_store.py`, `tests/integration/scripts/test_check_pip_root_set.py`); `scripts/check_tls_root_set.py --require-all` in the built image, the `ci-cd.yml` step "TLS root set handshakes in the image"; `tests/unit/config/test_tls_root_set_2692.py`, `test_tls_root_set_launchers_2692.py`, `tests/integration/scripts/test_check_tls_root_set.py`; the Postgres CRL and service-file refusals and `PGGSSENCMODE`: the check's `asyncpg crl input`, `psycopg2 crl input` and `asyncpg home root.crl` rows (refused at start applied, an outside-CA server verified bare), `tests/unit/config/test_tls_root_set_pg_inputs_2787.py` and `tests/integration/test_alembic_refuses_pg_root_inputs_2787.py`; and `features.tls_root_set` on `GET /admin/config/status` (`tests/unit/config/test_tls_root_set_status_2692.py`) | Enforced (#2692, #2759, #2787) |

---

## 14. Bounded Execution

### Principle

> Every deadline the design states names the mechanism that keeps it.
>
> A thread keeps neither the event loop free nor a deadline: native code that holds the interpreter lock stalls the loop, and cancelling a thread's future does not stop the thread. A thread is the place for I/O, and for native code that releases the lock and has no deadline to keep.
>
> Work over input a user controls, and any work the design gives a deadline, runs where that deadline can stop it: a process the deadline kills, under declared time, memory and output limits.
>
> Work stays in the API process only under a declared input limit and a growth test over that limit; a pattern the system did not write never runs there.

The mechanism is one shared worker, `faultmaven/infrastructure/worker_process.py`: a job runs in a spawned process of its own (`python -m` its worker module, which imports what the job needs and little else), killed and reaped at its deadline, a cancellation and any other end included, before its slot frees. The worker bounds itself before it touches the input: it dies with its parent, raises its `oom_score_adj` (which picks it first only where a container's OOM is not grouped), and sets its CPU-time and address-space limits, and the parent reads its output under a limit. A phased job gives each step a bound of its own (the start, the request, the work, the result), so the work's deadline counts the work alone. Each application declares its deadlines and limits. Built for **document preparation** (#2622): Stages 0-5 of a conversion, one worker per job under two slots, killed at the 120 s deadline (`document_preprocessor.py`, `preparation_worker.py`). Built for **Evidence Tier 1** (#2637): one phased worker per extraction under `max(2, int(index_cores()))` slots, killed when `extract` runs past its 2 s deadline; the wait for a slot, the start and each transfer have declared bounds of their own (`preprocessing_service.py`, `extraction_worker.py`). Built for the **runbook validator gate** (#2661): the checks one request needs (the verdict and score, the full gate, its security half) run in one phased worker under four slots, in order, and an `ITIMER_PROF` timer armed at the start of each check stops it past 3 s of its own CPU time, serialising its answer included; the timer is disarmed however the check ends, and `RLIMIT_CPU` stays as the backstop. A CPU-time deadline counts the check alone, so outside a band of 1 s around it a document gets the same verdict under any load. Every gate input is bounded at 100,000 characters first, with one known exception: the text verify publishes, whose frontmatter `frontmatter.dumps` re-emits with its YAML aliases expanded (#2674). The gate fails closed. A refusal that depends only on the input is a 422 with its `reason`: past the CPU deadline `validation_timeout`, past the memory limit `validation_too_large`, an answer past the output limit `validation_output_too_large`. One that depends on load is a 503 with `Retry-After`: past the slot wait `RUNBOOK_VALIDATION_BUSY`; past the wall-clock backstop, a kill or a worker that could not start `RUNBOOK_VALIDATION_UNAVAILABLE`. A check that raises is a defect in it: the generic 500 `SERVICE_ERROR`, not retryable and logged. No check runs in the API process (`runbook_gate.py`, `validation_worker.py`). Built for the **knowledge-base upload path** (#2851): every BGE-M3 token count of a KB write runs in a phased worker under two slots (Rust `tokenizers`, no torch; `embedding_guard.py`, `token_count_worker.py`), and every KB indexing encode in a phased worker of its own (`index_slot.py`, `encode_worker.py`) whose phases sum to the embedding guard's bound less a 5 s reap margin, so a ticket never reads stuck while its worker is reaped; a caller that goes away kills it. Both pin their thread pools before their imports read them. A count that finds no slot within its hang bound, or gives no answer, gives no count: KB admission refuses a request with `KNOWLEDGE_EMBEDDER_UNAVAILABLE` when a request window is set (`INDEX_REQUEST_WINDOW_SECONDS`, default 0); otherwise, and for the boot repair, which is never refused, the guard counts inside the embed and, with no answer, bounds the encode by the texts' UTF-8 bytes. The evidence guard counts no tokens: it sizes its bound from the byte bound, so an evidence encode never waits behind KB counts. The KB encode answers `KNOWLEDGE_EMBEDDER_TIMEOUT` past its phases and `KNOWLEDGE_EMBEDDER_UNAVAILABLE` at any other end. Admission charges every KB ticket the worker's measured load (30 s). The evidence encode stays in the API process at batch 4 (#2905). No application claims a victim ordering: where a deployment groups its container's OOM (`memory.oom.group = 1`), a memory-cgroup OOM kills every process in it whatever `oom_score_adj` says. A container limit at or above the worst-overlap formula (*Container memory*, below) leaves no overlap of the jobs' caps that can reach it; one below it is reported, never refused.

**Admission** ([ruling on #2661](https://github.com/FaultMaven/faultmaven/issues/2661#issuecomment-6739425246), #2645). A principal that holds no slot of a worker takes one while one is free; a principal that already holds a slot takes another only while at least two are free, so a slot stays free for a newcomer. A freed slot goes to the waiting principal holding the fewest slots, earliest first among equals, if this rule admits it. A lone principal so uses N−1 of N slots. The principal is the user who asked, not the enterprise: the wait being shared out is people's, and one user's batch would otherwise hold every slot from their teammates. The residual: two newcomers arriving at once can leave one waiting while a third principal holds two slots. `FairSlots` in `worker_process.py` applies it to all four workers: document preparation, Evidence Tier 1, the runbook gate (#2661) and the KB token counts (#2851). A service account speaks for many people, so a request from an account whose `service_channel` is `slack` may name the person who asked in `X-FaultMaven-Requester` ([ruling on #2852](https://github.com/FaultMaven/faultmaven/issues/2852#issuecomment-6998822644)). The rule above still holds the account, per pool: a requester buys no slot. Among one account's waiters, a freed slot goes to the one whose requester holds the fewest of that pool; then to the requester granted least recently since the account last held no slot and had no waiter, one not granted since then first; then the earliest. A grant takes the account's earliest place in the queue, so accounts rank against each other as they do without requesters, each account's grants are the ones it gets without them, and each person's own jobs run first-in, first-out ([ruling](https://github.com/FaultMaven/faultmaven/issues/2852#issuecomment-7000630527)). `FairSlots` keys a waiter by `(account, requester)`, never by one joined string, since an account id may hold a colon, and keeps one grant stamp per requester, dropped when its account goes idle. So on a two-slot pool the people of a workspace take turns, one job each, never side by side: a second person waits for the first's running job, not for the first's queued batch. At three or more slots they run side by side. The id orders admission and nothing else: authorization, ownership, queries and stored rows see the account. From any other account, or malformed, it is ignored and the request is admitted as the account, with no 4xx. Under it a turn reaches Tier 1 (an attachment or a reclassification) and, through the runbook conversion a resolved turn starts, the runbook gate. The conversion is admitted as the case's creator, so it keeps the requester only when the Slack account created the case.

### Container memory

Memory is the one bound the workers share ([#2921](https://github.com/FaultMaven/faultmaven/issues/2921)). Each job is bounded alone by its `RLIMIT_AS`, and each pool by its slots, but every job draws on the same container. The container's worst overlap is every slot of every pool held at once, each job at its address-space limit, beside the API processes. In the serving processes (*W*, `serving_processes` in `config/worker_stores.py`: `WORKERS`, `ServerSettings.workers`, or 1 under `RELOAD`; the `enterprise` preset sets `WORKERS` to 4, `config/presets.py`), the upload limit in MB (*U*, `UploadSettings.max_upload_size_mb`, `MAX_UPLOAD_SIZE_MB`, which has no upper bound) and the cores (*C*, `index_slot.index_cores()`: the container's CPU quota, else the node's cores, capped at `INDEX_MAX_CORES`):

> worst overlap = supervisor + *W* × (API + buffers(*U*, *C*) + pools(*U*, *C*)) + encode

- **supervisor**: uvicorn's supervising process, one per container, present when *W* is above 1 or under `RELOAD` (`run_server`, `server.py`). Before it starts the serving processes it runs the configuration's refusals and, with *W* above 1, the startup migration (`_prepare_workers`). No constant bounds it; it is measured.
- **API**: one serving process's resident memory with BGE-M3 loaded (`infrastructure/model_cache.py`), its in-process evidence encode included while #2905 is open: one encode at a time per process (`index_slot.evidence_slot`), at `model_cache.ENCODE_BATCH_SIZE` texts a pass. No constant bounds it; it is measured.
- **buffers**: what one serving process holds for each job it runs: the request, which `worker_process.run` holds twice as UTF-8 while it sends it (its encoded parts and the joined request), and the output, read under the job's output limit. Per job: preparation, `PREPARATION_OUTPUT_LIMIT_BYTES` (its request names a file); Tier 1, the upload's decoded text in, *U* MB when the upload is UTF-8-clean and up to 3 *U* when the decode replaced its bytes with U+FFFD, and `TIER1_OUTPUT_BASE_MIB` + `TIER1_OUTPUT_MIB_PER_MB` × *U* MiB out; the gate, one call's texts in, up to two (verify checks the draft and the text it publishes, `aenforce_runbook_security`), each at most `DRAFT_CONTENT_MAX_CHARS` characters (`modules/knowledge/draft_content.py`; 400 KB of UTF-8; verify's expanded text aside, #2674), and `GATE_OUTPUT_LIMIT_BYTES` out; a count, the document's texts in and `TOKEN_COUNT_OUTPUT_LIMIT_BYTES`, 1 MiB, out (`infrastructure/embedding_guard.py`). Each pool's slots times these.
- **pools**: each per-loop pool's slots times its `RLIMIT_AS`. A pool's slots are made per event loop (`worker_process.loop_slots`), so every serving process has its own: the table's first four rows.
- **encode**: the KB indexing encode's `RLIMIT_AS`, once per container while the host lock (`index_slot.HOST_LOCK_PATH`) holds: one encode at a time per container, whichever serving process holds the lock; with its texts in and `index_slot.encode_output_limit_bytes(texts)` out. A serving process that cannot open or lock the file encodes without it (`EncodeSlot._lock_host`), so without the lock the term is *W* × `ENCODE_MEMORY_BYTES`.

| Pool | Slots | `RLIMIT_AS` per job | At *U* = 10, *C* = 2 |
|---|---|---|---|
| Document preparation | `PREPARATION_SLOTS` = 2 (`modules/knowledge/domain/services/document_preprocessor.py`) | `preparation_memory_limit(U)`: `PREPARATION_MEMORY_BASE_MIB` + `PREPARATION_MEMORY_MIB_PER_UPLOAD_MB` × *U* = 128 + 64 *U* MiB | 2 × 768 = 1,536 MiB |
| Evidence Tier 1 | `TIER1_SLOTS` = max(2, int(*C*)), 2 to 4 (`modules/preprocessing/preprocessing_service.py`) | `tier1_limits(U)`: `TIER1_MEMORY_BASE_MIB` + `TIER1_MEMORY_MIB_PER_MB` × *U* = 96 + 240 *U* MiB | 2 × 2,496 = 4,992 MiB |
| Runbook gate | `GATE_SLOTS` = 4 (`modules/knowledge/domain/services/runbook_gate.py`) | `GATE_MEMORY_BYTES` = 256 MiB | 4 × 256 = 1,024 MiB |
| KB token counts | `TOKEN_COUNT_SLOTS` = 2 (`infrastructure/embedding_guard.py`) | `TOKEN_COUNT_MEMORY_BYTES` = 1 GiB | 2 × 1,024 = 2,048 MiB |
| KB indexing encode | 1 per container while the host lock holds (`index_slot.HOST_LOCK_PATH`, `infrastructure/index_slot.py`) | `ENCODE_MEMORY_BYTES` = 12 GiB | 12,288 MiB |

At the default 10 MB, two cores and one serving process, the pools' caps sum to 1,536 + 4,992 + 1,024 + 2,048 + 12,288 = 21,888 MiB, about 21.4 GiB, before the API term. The buffers add about 0.25 GiB for UTF-8-clean uploads (2 × 5 + 2 × (2 × 10 + 61) + 4 × (2 × 0.76 + 10) + 2 × (2 × 10 + 1) MiB, and the encode's), about 0.33 GiB when Tier 1's input decoded to replacement characters. Each further MB of upload limit adds *W* × (2 × 64 + `TIER1_SLOTS` × 240) MiB of caps and *W* × (`TIER1_SLOTS` × (2 + 6) + 2 × 2) MiB of buffers for UTF-8-clean uploads, *W* × (`TIER1_SLOTS` × (6 + 6) + 2 × 2) MiB at Tier 1's worst input: on two cores and one serving process, 608 MiB of caps and 20 or 28 MiB of buffers, 636 MiB a MB in all at the worst. At 50 MB one Tier 1 slot is 12,096 MiB. Each further serving process adds the first four rows, the API term and the buffers again, and the first past one adds the supervisor term; each core past two, up to four, adds a Tier 1 slot.

The formula bounds the jobs' caps, not resident use. `RLIMIT_AS` counts every mapping a job makes, so a job's resident peak is below its cap, and a job that reaches its cap ends in its own process without taking more of the container. At the cap, preparation refuses the document as out of memory (`DOCUMENT_TOO_COMPLEX`) and the gate as `validation_too_large`; Tier 1 gives the preview with `error_fallback`; a count gives no answer, and KB admission refuses a request with `KNOWLEDGE_EMBEDDER_UNAVAILABLE` when a request window is set (`INDEX_REQUEST_WINDOW_SECONDS`, default 0; otherwise, and for the boot repair, which is never refused, the guard counts inside the embed and, with no answer, bounds the encode by the texts' UTF-8 bytes); and a KB encode gives no answer, which KB indexing answers `KNOWLEDGE_EMBEDDER_UNAVAILABLE`. A native allocator that fails under the cap can abort its worker with a signal instead of raising: Tier 1, a count and a KB encode then answer as above, the gate answers `RUNBOOK_VALIDATION_UNAVAILABLE`, and preparation refuses the document as unreadable while parsing (stage 1) but fails the conversion with a 500 after it. A limit at or above the formula leaves no overlap of the jobs' caps that can reach it; that holds for the jobs only. The supervisor and API terms are measured, not bounded, and the input can carry them past what was measured: one 10 MB upload's evidence encode grew a process by about 0.3 GiB for source code and 1.6 GiB for a log (measured on #2934). A limit below the formula is sized from resident use, which grows with the input. The resident peaks measured for this formula are recorded in its pull request (#2934), not here, because they move with every parser, model and limit.

Whether the container's OOM is grouped is the deployment's choice. On cgroup v2, recent Kubernetes releases group a container's OOM (`memory.oom.group = 1`), and the kubelet can opt out. Where it is grouped, one OOM kills the container whole, the API process with its workers, and every request and turn in flight fails. Where it is not, each worker's `oom_score_adj` makes it the kernel's first choice. Standalone compose sets no container memory limit, so no container OOM is reached there; a host-wide OOM still can be, the kernel does not group it, and each worker's `oom_score_adj` makes the workers its first victims.

A formula over the container's limit is reported, never refused at startup ([ruling (c)](https://github.com/FaultMaven/faultmaven/issues/2853#issuecomment-6996091690)). A refusal would stop a running deployment, a standalone `docker run --memory` included, the moment a setting moved the formula past its limit. The report is #2921's configuration status (Enforcement, below): `container_memory` on `GET /admin/config/status` gives the bounded terms, Tier 1 at its worst input and the encode once, as under the host lock, as `job_caps_bytes` (the lock-less term is `encode_without_host_lock_bytes`), with the `headroom_bytes` the container's limit leaves, and lists the supervisor, API and the encode's buffers as outside it (`unbounded_terms`). Startup logs a warning when the caps exceed the limit, and a failure of that check is logged by its class, never refused.

### Enforcement

| Check that fails on the next violation | Status |
|---|---|
| A responsiveness test with a stage that holds the interpreter lock in C (for example a large `re.sub`); it fails on a thread-only design. | Built for document preparation (#2622): `test_a_stage_holding_the_lock_in_c_leaves_the_event_loop_free` in `tests/unit/modules/knowledge/test_document_extraction_off_the_loop_2153.py`. Its stage holds the lock in a C loop without a regex, so the test adds no ReDoS pattern. Built for Evidence Tier 1 (#2637): `test_an_extraction_past_its_deadline_stops_at_it` in `tests/unit/modules/preprocessing/test_tier1_runs_in_a_killable_worker_2637.py`, with an extractor that holds the lock in C (a cubic `re.sub` over a constant) and one that loops in Python: while it runs the loop turns three times with its worker alive, and once the preview is given no thread of the API process is inside the extractor and its worker was killed. Built for the runbook gate (#2661): `test_a_check_past_its_deadline_is_killed_and_refused` in `tests/unit/modules/knowledge/test_runbook_gate_runs_in_a_killable_worker_2661.py`, the same two checks, driven through `POST /knowledge/documents` and `POST /knowledge/runbooks/create`. |
| A census of thread offloads over user-sized input, each naming the mechanism that keeps its deadline. | Built (#2622): the table below, kept equal to the files by `tests/unit/architecture/test_thread_offload_census_2622.py`. The test checks the set of files, not each one's mechanism: a file that starts or stops offloading fails it until its row is added or removed. |
| Each in-process path over user input names its input limit and its growth test, or it moves to a worker. | Pending: #2596's in-process stage builds the growth tests (pin 3). The table below lists today's paths; nothing yet fails when a new one appears. A path it marks *moving* moves in #2596's in-process stage (#2686); Phase 4 is its own stage (#2644). A growth test is a `tests.wallclock.assert_linear_growth` check (`tests/wallclock/growth.py`). |
| The configuration status reports the computed worst overlap (*Container memory*) beside the container's limit. | Built (#2921). In `tests/unit/api/test_container_memory_status_2921.py`, `test_the_cgroup_parse_matrix` reads each cgroup state with its source (an unreadable `memory.oom.group` keeps the limit); `test_the_worked_figures_at_ten_mb_one_process_two_cores`, `test_each_further_upload_mb_adds_636_mib` and `test_a_second_serving_process_adds_its_pools_buffers_and_the_supervisor` pin this section's figures as literals. In `tests/integration/api/test_container_memory_status_2921.py`, `test_the_app_starts_serves_and_reports_under_every_cgroup_state` boots the app over the limit (reported, warned once) and under absent, invalid and unreadable cgroups, and `test_the_app_starts_and_serves_when_the_check_raises` boots it with the check failing. |

**In-process work over user input.** Work that runs inline in the API process, on the event loop, never enters the census below, which lists thread offloads only. Under the sentence above each such path names the pattern's author, its input limit and its growth test, or it moves to a worker. Paths are `faultmaven/` relative.

| Path | Input, and who writes the pattern | Input limit | Growth test | Disposition |
|---|---|---|---|---|
| `search_file`, the model's regex (`modules/agent/tools/search_file_tool.py:568`, `:771`, matched at `:575`, `:781`) | The model's pattern over an evidence file | The file, by `MAX_UPLOAD_SIZE_MB`; the pattern, by nothing | none | **Moving** to a Principle 14 worker per search: the in-process stage (#2686), since the system did not write the pattern |
| `search_file`'s vocabulary scan (`modules/agent/tools/search_file_tool.py:896-904`; its patterns at `:803-811`, run at `:910-919`) | The system's patterns over the first 102,400 characters | 102,400 characters | `tests/unit/modules/agent/tools/test_vocabulary_scan_2723.py` | Stays: its patterns are linear (the host:port pattern is `HOST_PORT_RE`, `utils/host_port.py`, #2723) |
| The Tier 0 classifier (`modules/preprocessing/classifier.py:413`, `:617`) | The system's patterns over the first 5,000 characters | 5,000 characters | none yet | Stays |
| The coverage scan, `extract_time_range_ts` (`modules/preprocessing/extractors/utils.py:442`; called at `modules/preprocessing/preprocessing_service.py:973` in the parent, and at `core/investigation/milestone_engine/milestone_inference.py:131`, there over an evidence extract the model wrote, bounded by nothing declared) | The system's timestamp patterns over the first 10 lines and at most the last 100 (`_HEAD_SCAN_LINES`, `_TAIL_SCAN_LINES`, `utils.py:46-47`) | Lines bounded at 110; line length unbounded, the file to `MAX_UPLOAD_SIZE_MB` on the upload; the extract on the `milestone_inference.py` call, none declared | none yet (over line length) | Stays once the in-process stage (#2596, pin 3) declares the line-length and extract limits and builds its growth test |
| Phase 4 entity extraction (`modules/preprocessing/preprocessing_service.py:1029`, `extract_entities_for_data_type(detailed_data_type, content)`) | The system's patterns over the whole upload | The file, by `MAX_UPLOAD_SIZE_MB` | none yet | Its own stage, #2644; the mechanism is decided in its plan (a worker, or a declared limit and growth test) |
| Frontmatter (`utils/frontmatter.py:90` `yaml.safe_load`; the alias expansion at `:125` and `:131`, after the load; on `tags`, which is not in `_LIST_FIELDS` (`:20`), at `modules/knowledge/domain/services/conversion_service/service.py:2230` (after `yaml.safe_load` at `:2225`) and `modules/knowledge/domain/services/conversion_service/runbook_scan.py:360`, `:362` (after `:253`)) | YAML whose aliases the author writes, over the frontmatter block | The runbook upload, by `MAX_UPLOAD_SIZE_MB` (`modules/knowledge/api/routes.py:631`); the conversion upload, by the same setting (`modules/knowledge/api/conversion_routes.py:260`); the draft edit, by `DRAFT_CONTENT_MAX_CHARS` = 100,000 (`modules/knowledge/draft_content.py:24`; `DraftUpdateRequest.content`, `modules/knowledge/domain/models/conversion.py:267-272`), parsed on verify (`conversion_service/service.py:2220`), where the text is not bounded by it: `frontmatter.dumps` re-emits the aliases expanded, so a 62 KB draft published 30 MB (#2674); the scan (`runbook_scan.py:241` check, parsed at `:253`, `:353`); the manual create form (`conversion_routes.py:740`) declares no `max_length` beyond `title`, but `RequestBodySizeLimitMiddleware` (`api/middleware/body_size.py`, mounted at `bootstrap/middleware.py:321`) caps the whole JSON body at `MAX_UPLOAD_SIZE_MB` | none yet | The in-process stage (#2674): refused, or expanded under a declared limit off the loop |
| The query classifier (`modules/agent/domain/services/query_classifier.py`, `classify_query`) and the continuation gate (`modules/agent/domain/services/out_of_band.py`, `reads_as_continuation`) | The system's patterns over the user's message | `classification_window`: the whole message up to 4,000 characters, else its first and last 2,000, cut at whitespace (at most 4,003; #2798) | `tests/unit/modules/agent/domain/services/test_query_classifier.py` (`TestClassificationReadsABoundedWindow`, `TestTheWindowsSeam`), `test_out_of_band.py` (`TestTheContinuationGateIsBounded`) | Stays |
| The KB query patterns (`infrastructure/knowledge/knowledge_vector_store.py:96-103`, used at `:1095`, `:1167`; the tokenizer split `_TOKEN_SEPARATORS.split(query)` at `:1071`) | The system's patterns over the query | none declared on the `hybrid_search` callers (`modules/knowledge/domain/services/knowledge_service.py:535`, `modules/agent/tools/document_qa_tool.py:509`) | none yet | Stays once the in-process stage (#2596, pin 3) declares its limit and builds its growth test; its patterns at `:97`, `:99` and `:102` are quadratic over a long separator-free token (#2721) |
| `redact_log_line` (`infrastructure/logging/url_redaction.py:372`: `redact_urls` at `:121`, then `redact_credential_headers` at `:360`, #2865; run by the logging renderer, `redacting_renderer` at `:407` (calling it at `:419`), in every formatter `build_log_formatter` builds: the API process, the maintenance jobs and alembic) | The system's URL and credential-header patterns over each log record, inline on every record; [§7](#7-observability-by-default) bars case content from a log line, and S4 (#2890) removes the sites that still carry it | none declared | `test_a_long_near_miss_line_does_not_blow_up` and `test_a_line_of_many_scheme_separators_does_not_blow_up` in `tests/unit/infrastructure/logging/test_url_query_redaction.py`; `TestTheCostIsLinear` in `tests/unit/infrastructure/logging/test_credential_header_redaction_2865.py` | Stays once the in-process stage declares its limit |

`core/processing/log_analyzer.py` is not yet traced.

**The thread offload census.** Every module in `faultmaven/` that calls `asyncio.to_thread`, `run_in_executor` or an executor's `submit`, with what it hands to the thread and what keeps the rule: I/O, native code that releases the interpreter lock and has no deadline to keep, redaction (ruled to keep its thread on #2596, measured inside its bound: its patterns are the system's; its input is bounded by `MAX_UPLOAD_SIZE_MB` for uploads and turns, but `DataSanitizer` also runs over the KB query (`modules/knowledge/domain/services/knowledge_service.py:526`), which has no declared limit: none yet, the in-process stage. `model_boundary_redacted` (`infrastructure/security/model_content.py:326`, `:329`) redacts knowledge-authoring content on its way to the model (module docstring `:22-29`); its production callers pass message lists from the conversion pipeline (`conversion_service/pipeline.py:60`, `conversion_service/service.py:1022`, `document_preprocessor.py:399`) over the conversion upload, bounded by `MAX_UPLOAD_SIZE_MB` (`pipeline.py:60`, the uploaded text; `document_preprocessor.py:399`, the triage sample of the upload), except `service.py:1022`, which case-to-runbook conversion also reaches (`_convert_from_case_impl` to `_prepare_conversion`, `service.py:695`) over text built from `CaseConversionRequest` fields (`modules/knowledge/domain/models/conversion.py:317-332`, no `max_length`): no declared limit there, none yet, the in-process stage; redaction's growth tests, which cover the shared patterns and the shared span finder every redaction caller uses, are `test_a_hostile_shape_costs_linear_time` and `test_a_chain_of_rounds_is_cut_and_fails_closed` in `tests/unit/infrastructure/security/test_secret_patterns_1980.py`; Presidio's calls have no growth test here: none yet), or a #2596 stage still to come.

| Module | What runs in the thread | Kind |
|---|---|---|
| `faultmaven/api/routes/admin_config.py` | The trace SDK's status for `GET /admin/config/status`: a read of its SQLite message store (#2629) | I/O |
| `faultmaven/cli/personal_tenant.py` | The identity provider's organization calls | I/O |
| `faultmaven/core/preprocessing/vector_storage.py` | Chunking a stored structural index (at most `MAX_STORED_EXTRACT_CHARS`) for `vectorize_file`: tiktoken's encodes and the split around them (#2936) | Native code that releases the lock for the encodes; the split around them Pending: #2596 |
| `faultmaven/infrastructure/base_client.py` | A blocking client's calls (the ChromaDB HTTP client) | I/O |
| `faultmaven/infrastructure/health/component_monitor.py` | Health probes | I/O |
| `faultmaven/infrastructure/index_slot.py` | A KB indexing encode's wait: the host lock, then its `encode_worker` (#2851); an evidence encode, in this process | I/O for the KB encode; the evidence encode Pending: #2596 (#2905) |
| `faultmaven/infrastructure/model_cache.py` | BGE-M3 encoding a query | Native code that releases the lock |
| `faultmaven/infrastructure/observability/trace_erasure.py` | The trace backend's deletes | I/O |
| `faultmaven/infrastructure/security/case_redaction.py` | Case redaction | Redaction |
| `faultmaven/infrastructure/security/redaction.py` | `DataSanitizer.sanitize`: Presidio's calls and the regex layer | Redaction |
| `faultmaven/infrastructure/storage/filesystem.py` | File reads, writes and walks | I/O |
| `faultmaven/infrastructure/storage/s3.py` | S3 calls | I/O |
| `faultmaven/modules/auth/domain/services/sso_login_service.py` | The identity provider's calls | I/O |
| `faultmaven/modules/case/api/title_generation.py` | Redacting a title candidate | Redaction |
| `faultmaven/modules/knowledge/api/conversion_routes.py` | Copying and hashing the upload | I/O |
| `faultmaven/modules/knowledge/api/routes.py` | Hashing the upload for its idempotency fingerprint | I/O |

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
| 2.3 | 2026-11-08 | P13 "One Owner per Fact" and P14 "Bounded Execution" added as ruled ([#2614](https://github.com/FaultMaven/faultmaven/pull/2614#issuecomment-6716219954); P13's adopted text: [#2617](https://github.com/FaultMaven/faultmaven/issues/2617#issuecomment-6718120222); P14: [#2596](https://github.com/FaultMaven/faultmaven/issues/2596#issuecomment-6716225975)), each with its enforcement table. P3 and P7 point at the [store registry](../data-and-storage/store-registry.md). P5 names `bootstrap/composition.py` as the composition root. P9's AI evaluation gate and P12's automated enforcement are marked not built. |
| 2.4 | 2026-11-08 | P9: the 70% coverage floor is enforced by CI, which gates on `COVERAGE_FAIL_UNDER` in `ci-cd.yml` ([#2619](https://github.com/FaultMaven/faultmaven/issues/2619)). The Two Testing Dimensions row points to Quality Gates, the per-layer figures are marked targets with no gate, and Quality Gates no longer claims the pipeline enforces all rules. |
| 2.5 | 2026-11-08 | P14 applied to document preparation ([#2622](https://github.com/FaultMaven/faultmaven/issues/2622)): its responsiveness test and the thread offload census built. |
| 2.6 | 2026-11-09 | P14 applied to Evidence Tier 1 ([#2637](https://github.com/FaultMaven/faultmaven/issues/2637)): the shared worker named as the mechanism of both applications, phased bounds for Tier 1, its responsiveness test built, and its census row removed. |
| 2.7 | 2026-11-10 | P13.4 names the TLS root set as an SDK policy ([ruling on #2685](https://github.com/FaultMaven/faultmaven/issues/2685#issuecomment-6754909029)): one set, the OS bundle, for every TLS client and run mode, extended in one place. Its enforcement row and the TLS client census added ([#2691](https://github.com/FaultMaven/faultmaven/issues/2691)). 13.4's build status is stated in its Status column only. |
| 2.8 | 2026-11-11 | P14 states when work may stay in the API process ([ruling on #2596](https://github.com/FaultMaven/faultmaven/issues/2596#issuecomment-6754909397)), with its enforcement row and the in-process table ([#2693](https://github.com/FaultMaven/faultmaven/issues/2693)). |
| 2.9 | 2026-11-12 | P13.4: the TLS root set's mechanism, `TLS_EXTRA_CA_FILE` and `apply_root_set`, its equality rule (narrowed to the set's path or bytes) and file-lifetime rules, the Windows certificate store as the OS bundle there, and the census's reading of it, with its exceptions ([#2692](https://github.com/FaultMaven/faultmaven/issues/2692) S1.1). |
| 2.10 | 2026-11-12 | P13.4: the TLS root set enforced in the core image ([#2692](https://github.com/FaultMaven/faultmaven/issues/2692) S1.2): grpcio's `roots.pem` linked to the OS bundle, the image check reading every `roots.pem` and the bundle of every pip and pip wheel ([#2665](https://github.com/FaultMaven/faultmaven/issues/2665)), the handshake check run in the built image, and `features.tls_root_set` on the config status. |
| 2.11 | 2026-11-12 | P13.4 states the ruled pip mechanism: every pip's vendored bundle linked to the OS bundle, no pip wheel holding a banned root, an image check that judges every pip bundle whatever its version or `legacy-certs`; with the reinstall, `--user`, later-venv and operator `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` limit ([ruling on #2756](https://github.com/FaultMaven/faultmaven/issues/2756); S1.3a, #2758). To be built by S1.3b (#2759). |
| 2.12 | 2026-11-12 | P13.4: the pip mechanism built in the core image (S1.3b, [#2759](https://github.com/FaultMaven/faultmaven/issues/2759)): every pip's vendored bundle linked to the OS bundle, the ensurepip pip wheel removed, the image check judging every pip bundle whatever its version, and `pip --isolated` and a pip without `PIP_CERT` measured from pip's own session at the image build. |
| 2.13 | 2026-11-13 | P7 states the metrics switch and the label rules ([#2747](https://github.com/FaultMaven/faultmaven/issues/2747), stage D of [#2745](https://github.com/FaultMaven/faultmaven/issues/2745)): one switch, `ENABLE_METRICS`, read by the shim; every label bounded by code or by operator configuration. §1's presets no longer list a metrics variable. |
| 2.14 | 2026-11-13 | P14 states the admission rule for its workers' slots ([ruling on #2661](https://github.com/FaultMaven/faultmaven/issues/2661#issuecomment-6739425246)), applied to document preparation and Evidence Tier 1 ([#2645](https://github.com/FaultMaven/faultmaven/issues/2645)). |
| 2.15 | 2026-11-16 | P13.4: the Postgres drivers' CRL sources and service file are root inputs the set owns by refusal, a URL's `dsn` included, and `PGGSSENCMODE` defaults to `disable` ([#2787](https://github.com/FaultMaven/faultmaven/issues/2787), stage S1d of [#2685](https://github.com/FaultMaven/faultmaven/issues/2685); #2786, #2834, #2788). The census row and the enforcement row name them and the check's CRL rows. |
| 2.16 | 2026-11-16 | P14 applied to the runbook validator gate ([#2661](https://github.com/FaultMaven/faultmaven/issues/2661), 3b1): one worker call per request's checks, each under a CPU-time deadline, and the admission rule; its 422, 503 and 500 answers; its responsiveness test; its census row removed; the verify exception to the 100,000-character bound (#2674). |

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
**Last Updated**: 2026-11-16

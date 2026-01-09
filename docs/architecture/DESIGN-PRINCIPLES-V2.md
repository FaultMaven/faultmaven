# FaultMaven Design Principles v2

*Final version incorporating architectural review feedback*

---

## Core Philosophy

> **"Enforce what matters. Escape what you must. Sunset what you escape."**

These principles optimize for a small team (<15) building an AI-powered SRE tool. They prioritize debuggability over abstraction, and measurable outcomes over theoretical purity.

---

## The 10 Principles

### 1. Composition Root, Not Service Locator

**Principle**: All dependency wiring happens in one place. Services are pure—they receive dependencies, never resolve them.

```python
# ❌ Service Locator (hidden global state)
class CaseService:
    def __init__(self):
        self.auth = ServiceContainer.get(IAuthService)

# ✅ Composition Root (explicit wiring)
# main.py
auth_service = AuthService(token_store=redis_store)
case_service = CaseService(auth=auth_service)
app.state.case_service = case_service

# services/case_service.py
class CaseService:
    def __init__(self, auth: IAuthService):
        self.auth = auth  # No container knowledge
```

**Why This Is Non-Negotiable**:
- Unit tests run 10x faster (no global container to reset)
- Dependency graph is visible in one file
- Circular dependencies surface at startup, not runtime

---

### 2. Vertical Modules with Enforced Contracts

**Principle**: Organize by domain. Modules communicate only through explicit contracts. Cross-module imports are tracked and limited.

```
modules/
├── case/
│   ├── contracts.py    # Public interface (DTOs, protocols)
│   ├── domain/         # Internal (never imported externally)
│   └── infrastructure/
├── knowledge/
│   └── contracts.py
└── _shared/            # Pure utilities only (logging, exceptions)
```

**The Contract Rule**:
```python
# ✅ ALLOWED: Import from contracts
from faultmaven.modules.case.contracts import CaseDTO, ICaseQuery

# ❌ FORBIDDEN: Import from internal domain
from faultmaven.modules.case.domain.models import Case
```

**Enforcement**:
```ini
# .importlinter - Contract 5
[importlinter:contract:module_boundaries]
name = Module internals are private
type = forbidden
source_modules = faultmaven.modules.*.domain
                 faultmaven.modules.*.infrastructure
forbidden_modules = faultmaven.modules  # Can't import sibling internals
ignore_imports =
    # Explicit exceptions tracked here (see Principle 10)
```

---

### 3. Database-Per-Module Boundaries

**Principle**: Modules own their tables. Cross-module data flows through services, not JOINs.

**Table Naming**:
```sql
-- Each module prefixes its tables
case_cases, case_investigations, case_evidence
knowledge_items, knowledge_embeddings
auth_users, auth_sessions, auth_tokens
```

**Cross-Module Access**:
```python
# ❌ WRONG: Report queries case tables directly
async def generate_report(case_id):
    case = await db.execute("SELECT * FROM case_cases WHERE id = ?", case_id)

# ✅ RIGHT: Report calls case service
async def generate_report(case_id):
    case = await self.case_service.get_case(case_id)
```

**Preventing N+1 Problems**:

When modules can't JOIN, naive implementations create N+1 query patterns. Contracts must include bulk methods:

```python
# modules/case/contracts.py

class ICaseQuery(Protocol):
    """Public contract for case queries."""

    async def get_case(self, case_id: str) -> CaseDTO:
        """Single case lookup."""
        ...

    async def get_cases_by_ids(self, case_ids: list[str]) -> list[CaseDTO]:
        """Bulk lookup - prevents N+1 when other modules need multiple cases."""
        ...

    async def get_cases_for_user(
        self,
        user_id: str,
        limit: int = 100,
        cursor: str | None = None
    ) -> PaginatedResult[CaseDTO]:
        """Paginated query - prevents unbounded result sets."""
        ...
```

```python
# ❌ N+1 ANTI-PATTERN
async def generate_bulk_report(case_ids: list[str]):
    cases = []
    for case_id in case_ids:  # 100 cases = 100 queries
        cases.append(await case_service.get_case(case_id))

# ✅ BULK PATTERN
async def generate_bulk_report(case_ids: list[str]):
    cases = await case_service.get_cases_by_ids(case_ids)  # 1 query
```

**Contract Design Rules**:
1. Every entity query contract includes a bulk variant
2. List endpoints are always paginated (no unbounded `get_all()`)
3. Contracts expose filtering to push predicates to the owning module

**Why This Enables Future Scaling**:
- `knowledge/` can become a separate service with its own vector DB
- Schema migrations are scoped to one module
- No hidden coupling through database constraints

---

### 4. Protocols for Swappable Boundaries Only

**Principle**: Use `Protocol` only when you have (or plan) multiple implementations. Prefer concrete classes for internal services.

**When to Use Protocol**:

| Component | Multiple Implementations? | Use Protocol? |
|-----------|---------------------------|---------------|
| LLM providers | Yes (7 providers) | ✅ Yes |
| Vector stores | Yes (ChromaDB, InMemory) | ✅ Yes |
| Storage backends | Yes (S3, filesystem) | ✅ Yes |
| CaseService | No (one implementation) | ❌ No |
| ReportGenerator | No (one implementation) | ❌ No |
| KnowledgeIngester | Maybe future | ⚠️ Defer until needed |

**IDE Navigation Rule**: If "Go to Definition" on a type takes you to a Protocol instead of real code, ask: "Will this ever have two implementations?" If no, delete the Protocol.

**Practical Guidance**:
```python
# ❌ Over-abstraction (one implementation, no value)
class IReportGenerator(Protocol):
    async def generate(self, case: Case) -> Report: ...

class ReportGenerator:  # The only implementation
    async def generate(self, case: Case) -> Report: ...

# ✅ Just use the class directly
class ReportGenerator:
    async def generate(self, case: Case) -> Report: ...
```

---

### 5. Fail-Fast Configuration

**Principle**: Validate everything at startup. Crash loudly on misconfiguration.

```python
# main.py lifespan
async def startup():
    settings = Settings()  # Pydantic validates types

    # Capability checks
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise StartupError(
                "OPENAI_API_KEY required when LLM_PROVIDER=openai. "
                "Set the key or use LLM_PROVIDER=local for offline mode."
            )

    # Connectivity checks (with timeout)
    try:
        await asyncio.wait_for(
            verify_chromadb_health(settings.chromadb_url),
            timeout=5.0
        )
    except TimeoutError:
        raise StartupError(
            f"ChromaDB at {settings.chromadb_url} not reachable. "
            "Start ChromaDB or set VECTOR_BACKEND=inmemory."
        )
```

**For an SRE Tool, This Is Non-Negotiable**:
- FaultMaven should never return 500s because Redis is down
- If it can't do its job, it shouldn't start
- Error messages must be actionable (tell users what to fix)

---

### 6. Errors as Domain Concepts

**Principle**: Every module defines its exception hierarchy. Infrastructure errors are wrapped in domain terms.

```python
# modules/case/domain/exceptions.py
class CaseError(Exception):
    """Base for all case errors."""

class CaseNotFoundError(CaseError):
    def __init__(self, case_id: str):
        self.case_id = case_id
        super().__init__(f"Case {case_id} not found")

class InvestigationQuotaExceeded(CaseError):
    """User has hit their investigation limit."""

# modules/case/infrastructure/repository.py
async def get_case(self, case_id: str) -> Case:
    try:
        result = await self.db.fetch_one(...)
    except DatabaseError as e:
        # Wrap infrastructure error in domain terms
        raise CaseError(f"Failed to retrieve case: {e}") from e
    if not result:
        raise CaseNotFoundError(case_id)
```

**API Layer Translation**:
```python
# api/exception_handlers.py
@app.exception_handler(CaseNotFoundError)
async def handle_not_found(request, exc):
    return JSONResponse(
        status_code=404,
        content={"error": "case_not_found", "case_id": exc.case_id}
    )
```

---

### 7. Observability by Default

**Principle**: Structured logs with correlation IDs. Traces on every external call. Metrics with consistent naming.

```python
# Middleware injects correlation ID
@app.middleware("http")
async def correlation_middleware(request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response

# All logs include context automatically
logger.info("investigation_started",
    case_id=case.id,
    phase="initial_triage",
    llm_provider=provider.name
)
# → {"event": "investigation_started", "case_id": "...", "correlation_id": "..."}
```

**Metric Naming Convention**:
```
faultmaven_{module}_{operation}_{unit}
faultmaven_case_investigation_started_total
faultmaven_llm_request_duration_seconds
faultmaven_knowledge_search_results_count
```

---

### 8. Testing: Coverage Floor + AI Evaluation

**Principle**: 70% code coverage floor, plus evaluation benchmarks for AI behavior.

**Two Testing Dimensions**:

| Dimension | What It Tests | Target |
|-----------|---------------|--------|
| **Code Coverage** | Lines executed | ≥70% |
| **AI Evaluation** | Output quality | ≥85% accuracy on benchmark |

**Code Coverage Strategy**:
```ini
# pytest.ini
[pytest]
addopts = --cov=faultmaven --cov-fail-under=70 --cov-report=term-missing
```

| Layer | Coverage Target | Test Type |
|-------|-----------------|-----------|
| Domain services | 85% | Unit (mocked infra) |
| API routes | 70% | Integration |
| Infrastructure | 60% | Contract tests |

**AI Evaluation Strategy**:
```python
# tests/evaluation/test_investigation_accuracy.py
"""
Benchmark dataset: 50 real incidents with known root causes.
Tests whether the agent identifies the correct root cause.
"""

@pytest.mark.evaluation
@pytest.mark.parametrize("incident", load_benchmark_incidents())
async def test_root_cause_identification(incident, agent):
    result = await agent.investigate(incident.symptoms)

    # Semantic similarity to known root cause
    similarity = compute_similarity(result.conclusion, incident.known_root_cause)
    assert similarity >= 0.85, f"Expected root cause match ≥85%, got {similarity}"

    # Must not hallucinate non-existent services
    for service in result.mentioned_services:
        assert service in incident.known_services, f"Hallucinated service: {service}"
```

**Run Evaluation Separately**:
```bash
# Fast: Code tests only
pytest -m "not evaluation"

# Full: Include AI evaluation (slower, needs API keys)
pytest -m "evaluation" --benchmark
```

---

### 9. Bounded Complexity for AI Integration

**Principle**: LLM calls are stateless pure functions. Orchestration handles state, retries, and fallbacks.

**Architecture Layers**:

```
┌─────────────────────────────────────────────────┐
│      Orchestration Layer (Stateful)              │
│                                                  │
│  Owns: Investigation state, memory, retries     │
│  Tech: LangGraph state machines                 │
│                                                  │
│  • InvestigationOrchestrator                    │
│  • MemoryManager (64% token reduction)          │
│  • OODAEngine (adaptive investigation)          │
│  • FallbackChain (provider switching)           │
└─────────────────────────────────────────────────┘
                      │
                      │ Delegates stateless calls
                      ▼
┌─────────────────────────────────────────────────┐
│      LLM Adapter Layer (Stateless)               │
│                                                  │
│  Owns: Provider protocol, request normalization │
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

**Layer Responsibilities**:

| Concern | Orchestration (LangGraph) | LLM Adapter |
|---------|---------------------------|-------------|
| State management | ✅ Owns | ❌ None |
| Retry logic | ✅ Owns | ❌ None |
| Provider fallback | ✅ Owns | ❌ None |
| Token counting | ❌ Delegates | ✅ Owns |
| Request formatting | ❌ Delegates | ✅ Owns |
| Memory/context | ✅ Owns | ❌ None |

**LangGraph Boundary Clarification**:

LangGraph is an orchestration framework that manages stateful workflows. It lives in the **Orchestration Layer**, not the LLM Adapter Layer:

```python
# orchestration/investigation_graph.py
from langgraph.graph import StateGraph

class InvestigationState(TypedDict):
    case: Case
    phase: Phase
    memory: list[Message]
    conclusions: list[Conclusion]

# LangGraph manages the state machine
graph = StateGraph(InvestigationState)
graph.add_node("triage", triage_node)
graph.add_node("investigate", investigate_node)
graph.add_node("conclude", conclude_node)

# Nodes delegate to stateless LLM adapter
async def investigate_node(state: InvestigationState) -> InvestigationState:
    # Orchestration handles retries
    for attempt in range(3):
        try:
            # LLM adapter is stateless - just makes the call
            response = await llm_adapter.complete(
                messages=state["memory"],
                max_tokens=2000
            )
            break
        except RateLimitError:
            await asyncio.sleep(2 ** attempt)

    # Orchestration updates state
    state["conclusions"].append(parse_conclusion(response))
    return state
```

**LLM Adapter Contract** (stateless):
```python
# infrastructure/llm/adapters/base.py
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
        - No fallback (orchestration handles)
        - Validates tokens BEFORE calling
        """
        ...

    def count_tokens(self, messages: list[Message]) -> int:
        """Synchronous token counting."""
        ...
```

---

### 10. Escape Hatches with Mandatory Sunset

**Principle**: Architectural exceptions are allowed but tracked, counted, and time-limited.

**Exception Format** (in code):
```python
# ARCHITECTURE-EXCEPTION
# Violation: Direct import from case domain (violates Principle 2)
# Reason: Report PDF generation needs deep case structure access
# Ticket: FMVN-1234
# Approved: @jane on 2026-01-15
# Sunset: 2026-04-15 (90 days)
from faultmaven.modules.case.domain.models import Case, Investigation
```

**Automated Enforcement**:
```python
# scripts/check_architecture_exceptions.py
"""
Run in CI to enforce exception hygiene.
"""
import re
from datetime import date

EXCEPTION_PATTERN = r"# ARCHITECTURE-EXCEPTION.*?# Sunset: (\d{4}-\d{2}-\d{2})"

def check_exceptions():
    exceptions = find_all_exceptions()

    # Count check: Alert if too many
    if len(exceptions) > 10:
        warn(f"Architecture exception count ({len(exceptions)}) exceeds threshold (10)")

    # Sunset check: Fail if expired
    today = date.today()
    for exc in exceptions:
        if exc.sunset_date < today:
            fail(f"Expired exception in {exc.file}: sunset was {exc.sunset_date}")

    # Report for visibility
    print(f"Active exceptions: {len(exceptions)}")
    for exc in exceptions:
        print(f"  - {exc.file}: {exc.reason} (expires {exc.sunset_date})")
```

**CI Integration**:
```yaml
# .github/workflows/ci.yml
- name: Check architecture exceptions
  run: python scripts/check_architecture_exceptions.py

- name: Track exception count
  run: |
    COUNT=$(grep -r "ARCHITECTURE-EXCEPTION" faultmaven/ | wc -l)
    echo "exception_count=$COUNT" >> $GITHUB_OUTPUT
```

**Quarterly Review Process**:
1. Export all active exceptions
2. For each exception older than 90 days:
   - Either fix the underlying issue
   - Or explicitly renew with new sunset date (requires re-approval)
3. Track trend: exception count should decrease over time

---

## Principle Hierarchy

```
CRITICAL (Violations block deployment)
├── 1. Composition Root, Not Service Locator
├── 5. Fail-Fast Configuration
├── 6. Errors as Domain Concepts
└── 9. Bounded Complexity for AI Integration

IMPORTANT (Violations require exception)
├── 2. Vertical Modules with Enforced Contracts
├── 3. Database-Per-Module Boundaries
├── 7. Observability by Default
└── 8. Testing: Coverage + AI Evaluation

RECOMMENDED (Apply judgment)
├── 4. Protocols for Swappable Boundaries Only
└── 10. Escape Hatches with Mandatory Sunset
```

---

## Quick Reference Card

| # | Principle | One-Line Rule |
|---|-----------|---------------|
| 1 | Composition Root | Wiring in `main.py` only; services never touch container |
| 2 | Module Contracts | Import from `contracts.py` only; internals are private |
| 3 | DB Boundaries | No cross-module JOINs; use bulk methods to prevent N+1 |
| 4 | Protocols Sparingly | Only for components with 2+ implementations |
| 5 | Fail-Fast | Crash at startup if config is invalid |
| 6 | Domain Errors | Every module has its exception hierarchy |
| 7 | Observability | Correlation IDs, structured logs, traces on external calls |
| 8 | Coverage + Eval | 70% code coverage AND 85% AI accuracy benchmarks |
| 9 | AI Boundaries | LangGraph owns state; LLM adapters are stateless |
| 10 | Sunset Escapes | Exceptions expire in 90 days; count tracked in CI |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v1.0 | 2026-01-05 | Original 7 principles |
| v2.0 | 2026-01-09 | Expanded to 10 principles; added enforcement mechanisms |
| v2.1 | 2026-01-09 | Added N+1 prevention (Principle 3), LangGraph boundary clarification (Principle 9) |

---

## Supersedes

This document supersedes `architectural-design-principles.md` (v1.0). The original document remains as historical reference but should not be used for new development decisions.

---

**Document Owner**: Engineering Leadership
**Status**: Active
**Last Updated**: 2026-01-09

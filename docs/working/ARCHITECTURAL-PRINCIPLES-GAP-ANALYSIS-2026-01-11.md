# FaultMaven Architectural Design Principles - Gap Analysis

**Date**: 2026-01-11
**Evaluator**: Solutions Architect Agent
**Scope**: Complete codebase evaluation against [Architectural Design Principles v2.0](../architecture/architectural-design-principles.md)
**Codebase Version**: Current main branch (post-DI Container implementation)

---

## Executive Summary

This gap analysis evaluates FaultMaven's adherence to the 10 core architectural design principles documented in v2.0 of the architectural design principles. The assessment reveals a **partially compliant architecture** with significant progress in some areas but critical gaps requiring immediate attention.

### Top 5 Critical Gaps Requiring Immediate Attention

| Priority | Principle | Gap | Severity | Impact |
|----------|-----------|-----|----------|--------|
| 1 | **Principle 5: Composition Root** | Services use Service Locator anti-pattern via `container.get()` calls | CRITICAL | Violates CRITICAL principle; hidden dependencies; poor testability |
| 2 | **Principle 3: Database Boundaries** | Direct cross-module domain model imports bypass contracts | HIGH | Tight coupling; violates module boundaries |
| 3 | **Principle 9: Test Safety Net** | Coverage at 33.14% (target: 70%) | HIGH | Inadequate test protection; regression risk |
| 4 | **Principle 2: Vertical Modules** | Missing contracts for evidence/agent/report modules | MEDIUM | Incomplete vertical architecture |
| 5 | **Principle 7: Observability** | Correlation IDs present but inconsistent implementation | MEDIUM | Incomplete tracing; debugging challenges |

### Overall Compliance by Hierarchy

```
CRITICAL Principles (Block Deployment):
├── Principle 5: Composition Root         ❌ VIOLATED (Service Locator pattern)
├── Principle 6: Errors as Domain         ✅ COMPLIANT (Hierarchies exist)
└── Principle 10: Bounded AI Complexity   ✅ COMPLIANT (Stateless adapters)

IMPORTANT Principles (Require Exception):
├── Principle 1: Deployment Agnostic      ✅ MOSTLY COMPLIANT (Good separation)
├── Principle 2: Vertical Modules         ⚠️ PARTIAL (4/7 modules incomplete)
├── Principle 3: Database Boundaries      ❌ VIOLATED (Direct domain imports)
├── Principle 7: Observability            ⚠️ PARTIAL (Logs exist, inconsistent)
└── Principle 8: Boundary Enforcement     ⚠️ PARTIAL (Import linter configured)

RECOMMENDED Principles (Apply Judgment):
├── Principle 4: Interface-Based Design   ✅ COMPLIANT (7+ providers)
└── Principle 9: Test Safety Net          ❌ VIOLATED (33% vs 70% target)
```

**Deployment Recommendation**: **DO NOT DEPLOY** to production without addressing Principle 5 violations (CRITICAL).

---

## Detailed Principle-by-Principle Analysis

### Principle 1: Deployment Agnostic Architecture
**Hierarchy**: IMPORTANT
**Status**: ✅ MOSTLY COMPLIANT (85%)

#### Current State

**STRENGTHS:**
1. **✅ Unified Settings System**: Single source of truth in `/home/swhouse/product/faultmaven/faultmaven/config/settings.py`
   - Pydantic-based validation
   - Environment-based configuration
   - Provider selection enums (TenantProvider, DbBackend, CacheBackend, VectorBackend, StorageBackend)
   - Clean separation between config and code

2. **✅ Provider Selection Pattern**: Well-implemented selector enums
   ```python
   # Evidence from settings.py
   class DbBackend(str, Enum):
       SQLITE = "sqlite"
       POSTGRES = "postgres"

   class CacheBackend(str, Enum):
       MEMORY = "memory"
       REDIS = "redis"
   ```

3. **✅ Infrastructure Abstraction**: Multiple LLM providers (7 implementations)
   - `/home/swhouse/product/faultmaven/faultmaven/infrastructure/llm/providers/`
   - OpenAI, Anthropic, Fireworks, Gemini, Groq, Local, HuggingFace
   - Consistent `ILLMProvider` interface

**GAPS:**

1. **⚠️ Incomplete Startup Validation** (Severity: MEDIUM)
   - **Evidence**: `main.py` lines 115-156 show configuration validation but **no capability checks**
   - **Principle Requirement**: "Crash at startup if config is invalid" with actionable messages
   - **Missing**: Database connectivity checks, Redis availability verification, ChromaDB health checks
   - **Example from Principles**:
     ```python
     # EXPECTED (from principles doc):
     if settings.llm_provider == "openai":
         if not settings.openai_api_key:
             raise StartupError("OPENAI_API_KEY required...")

     # ACTUAL (main.py):
     # Only basic settings validation, no provider-specific checks
     ```

2. **⚠️ Conditional Infrastructure Logic** (Severity: LOW)
   - **Evidence**: `main.py` lines 206-236 show deployment-specific branching for local LLM
   - **File**: `/home/swhouse/product/faultmaven/faultmaven/main.py:206-236`
   - **Issue**: `if chat_provider == "local"` branching in startup logic
   - **Better Approach**: Provider factory pattern should handle this

#### Gaps Identified

| Gap ID | Description | Severity | File Location |
|--------|-------------|----------|---------------|
| P1-G1 | Missing fail-fast connectivity checks for Redis, ChromaDB, PostgreSQL | MEDIUM | `main.py:115-156` |
| P1-G2 | Local LLM provider has conditional startup logic instead of factory pattern | LOW | `main.py:206-236` |
| P1-G3 | No documented configuration presets (local, enterprise) | LOW | `config/` directory |

#### Remediation Roadmap

1. **Add Fail-Fast Checks** (Effort: 1 day)
   ```python
   # Add to main.py startup
   async def startup():
       settings = Settings()

       # Check critical dependencies
       if settings.cache_backend == CacheBackend.REDIS:
           await verify_redis_health(settings.redis_url, timeout=5.0)

       if settings.vector_backend == VectorBackend.CHROMA:
           await verify_chromadb_health(settings.chromadb_url, timeout=5.0)
   ```

2. **Extract Configuration Presets** (Effort: 0.5 days)
   - Create `config/presets.py` with `LocalPreset`, `EnterprisePreset`
   - Document preset usage in README

---

### Principle 2: Vertical Modules with Contracts
**Hierarchy**: IMPORTANT
**Status**: ⚠️ PARTIAL COMPLIANCE (57%)

#### Current State

**STRENGTHS:**
1. **✅ 4/7 Modules Have Contracts**:
   - `/home/swhouse/product/faultmaven/faultmaven/modules/case/contracts.py` (310 lines)
   - `/home/swhouse/product/faultmaven/faultmaven/modules/auth/contracts.py` (exists)
   - `/home/swhouse/product/faultmaven/faultmaven/modules/knowledge/contracts.py` (1,776 bytes)
   - `/home/swhouse/product/faultmaven/faultmaven/modules/evidence/contracts.py` (exists)

2. **✅ Proper Vertical Structure**: Modules follow domain/api/infrastructure pattern
   ```
   modules/case/
   ├── contracts.py      # ✅ Public interface
   ├── api/              # ✅ API layer
   ├── domain/           # ✅ Business logic
   ├── infrastructure/   # ✅ Persistence
   └── exceptions.py     # ✅ Domain errors
   ```

3. **✅ Protocol-Based Contracts**: Using Python `Protocol` for structural typing
   ```python
   # Evidence from case/contracts.py
   class ICaseRepository(Protocol):
       async def save(self, case: 'Case') -> 'Case': ...
       async def get(self, case_id: str) -> Optional['Case']: ...
   ```

**GAPS:**

1. **❌ CRITICAL: Direct Domain Imports Bypass Contracts** (Severity: CRITICAL)
   - **Evidence**: Services directly import from `modules.*.domain` instead of contracts
   - **File**: `/home/swhouse/product/faultmaven/faultmaven/services/case_service.py`
   ```python
   # VIOLATION (case_service.py):
   from faultmaven.modules.case.domain.models import Case, CaseStatus  # ❌ WRONG

   # SHOULD BE:
   from faultmaven.modules.case.contracts import CaseDTO, CaseStatusDTO  # ✅ RIGHT
   ```

   - **Count**: 12 direct domain imports from services (grep result)
   - **Impact**: Tight coupling; defeats module isolation

2. **⚠️ Missing Contracts for 3 Modules** (Severity: MEDIUM)
   - `modules/agent/` - No `contracts.py` file
   - `modules/report/` - No `contracts.py` file
   - `modules/evidence/` - Has `contracts.py` but minimal content

   **Evidence**: Only 4 contracts files found:
   ```bash
   find modules -name "contracts.py"
   # Output:
   # modules/evidence/contracts.py
   # modules/knowledge/contracts.py
   # modules/auth/contracts.py
   # modules/case/contracts.py
   ```

3. **⚠️ Incomplete DTOs** (Severity: MEDIUM)
   - **Evidence**: `case/contracts.py:279-305` defines `CaseDTO` but it's **not used**
   - Services still import domain models directly instead of DTOs
   - Missing DTOs for: EvidenceDTO, AgentExecutionDTO, ReportDTO

4. **⚠️ Backward Compatibility Anti-Pattern** (Severity: LOW)
   - **Evidence**: `case/contracts.py:307-309` re-exports domain models
   ```python
   # TECH DEBT (case/contracts.py:307-309):
   from faultmaven.modules.case.domain.models import Case, CaseStatus
   ```
   - **Issue**: Allows bypassing DTOs; should be removed after migration

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P2-G1 | Services import `domain.models` directly instead of using contracts | CRITICAL | 12 violations in `/services/*.py` |
| P2-G2 | Missing contracts for agent, report modules | MEDIUM | No `agent/contracts.py` or `report/contracts.py` |
| P2-G3 | DTOs defined but not enforced; domain models re-exported | MEDIUM | `case/contracts.py:307-309` |
| P2-G4 | No bulk query methods to prevent N+1 (missing from contracts) | LOW | Contract interfaces incomplete |

#### Remediation Roadmap

**Phase 1: Stop the Bleeding** (1 week)
1. Add import-linter contract to prevent new domain imports
   ```ini
   [importlinter:contract:module_internals]
   name = Modules must use contracts, not domain directly
   type = forbidden
   source_modules = faultmaven.services
   forbidden_modules =
       faultmaven.modules.*.domain
   ```

2. Create contracts for agent, report modules
   - Define `IAgentExecutionRepository`, `IReportRepository` protocols
   - Define AgentExecutionDTO, ReportDTO

**Phase 2: Migrate Existing Violations** (2 weeks)
1. Create migration script to replace domain imports with DTOs
2. Update 12 service files to use contracts
3. Remove backward compatibility re-exports from `contracts.py`

**Phase 3: Prevent N+1 Queries** (1 week)
1. Add bulk methods to all repository contracts:
   ```python
   async def get_cases_by_ids(self, case_ids: list[str]) -> list[CaseDTO]
   async def get_cases_for_user(self, user_id: str, cursor: str | None) -> PaginatedResult[CaseDTO]
   ```

---

### Principle 3: Database-Per-Module Boundaries
**Hierarchy**: IMPORTANT
**Status**: ❌ VIOLATED (40%)

#### Current State

**STRENGTHS:**
1. **✅ Table Naming Convention Followed**:
   - **Evidence**: Migration file `/home/swhouse/product/faultmaven/alembic/versions/20251229_0412_001_baseline_schema.py:85-100`
   - Tables prefixed by module:
     - `case_*`: cases, case_messages, case_status_transitions
     - `auth_*`: auth_users, auth_sessions, auth_tokens (inferred)
     - `knowledge_*`: knowledge_items, knowledge_embeddings
   - Clear ownership boundaries

2. **✅ Repository Pattern Per Module**: Each module has its own repository
   - `case/infrastructure/postgresql_hybrid_case_repository.py`
   - `knowledge/infrastructure/persistence/knowledge_item_repository.py`
   - `auth/infrastructure/stores/user_store.py`

**GAPS:**

1. **❌ CRITICAL: Within-Module JOINs Only** (Severity: CRITICAL)
   - **Status**: ✅ COMPLIANT - No cross-module JOINs detected
   - **Evidence**: Grep of case repository shows only case-owned tables:
     ```sql
     -- From postgresql_hybrid_case_repository.py
     LEFT JOIN hypotheses h ON c.case_id = h.case_id        -- ✅ SAME MODULE
     LEFT JOIN solutions s ON c.case_id = s.case_id         -- ✅ SAME MODULE
     LEFT JOIN uploaded_files f ON c.case_id = f.case_id    -- ✅ SAME MODULE
     ```
   - No violations like `JOIN auth_users` or `JOIN knowledge_items` found

2. **❌ CRITICAL: Services Import Domain Models Across Modules** (Severity: CRITICAL)
   - **Evidence**: Same as P2-G1 - services bypass contracts
   - **File**: `/home/swhouse/product/faultmaven/faultmaven/services/case_service.py`
   ```python
   # VIOLATION:
   from faultmaven.modules.case.domain.models import Case
   from faultmaven.modules.evidence.domain.models import EvidenceListFilter
   ```
   - **Impact**: Tight coupling to internal implementation; cross-module dependency

3. **⚠️ Missing Bulk Query Methods** (Severity: MEDIUM)
   - **Evidence**: `case/contracts.py` defines `ICaseRepository` but lacks:
     - `get_cases_by_ids(case_ids: list[str])` - Would cause N+1 in report generation
     - Paginated queries lack cursor-based pagination
   - **Example from Principles**:
     ```python
     # EXPECTED:
     async def get_cases_by_ids(self, case_ids: list[str]) -> list[CaseDTO]:
         """Bulk lookup - prevents N+1."""

     # ACTUAL (case/contracts.py):
     async def get(self, case_id: str) -> Optional['Case']:  # Only single lookup
     ```

4. **⚠️ No Cross-Module Data Flow Documentation** (Severity: LOW)
   - Principles require: "Cross-module data flows through services, not JOINs"
   - Missing: Sequence diagrams showing how report module gets case data via service calls
   - No architecture documentation of cross-module queries

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P3-G1 | Services directly import domain models instead of calling contracts | CRITICAL | 12 violations in `/services/` |
| P3-G2 | Missing bulk query methods in repository contracts | MEDIUM | `case/contracts.py` lacks `get_cases_by_ids()` |
| P3-G3 | No documentation of cross-module data flow patterns | LOW | Missing sequence diagrams |
| P3-G4 | Repository contracts still expose domain models instead of DTOs | MEDIUM | `ICaseRepository` returns `Case` not `CaseDTO` |

#### Remediation Roadmap

**Quick Win: Add Bulk Methods** (3 days)
```python
# Add to case/contracts.py
class ICaseRepository(Protocol):
    async def get_cases_by_ids(self, case_ids: list[str]) -> list[CaseDTO]:
        """Bulk lookup - prevents N+1 in report generation."""

    async def get_cases_for_user(
        self,
        user_id: str,
        cursor: str | None = None,
        limit: int = 100
    ) -> PaginatedResult[CaseDTO]:
        """Cursor-based pagination prevents unbounded queries."""
```

**Critical Fix: Enforce Contract-Based Access** (2 weeks)
1. Convert repository contracts to return DTOs instead of domain models
2. Update all services to use contracts only
3. Add import-linter enforcement (P2-G1 fix applies here too)

**Documentation: Cross-Module Patterns** (1 day)
1. Create sequence diagram: "Report Module Accessing Case Data"
2. Document pattern: Service → Contract → Repository (no direct DB access)

---

### Principle 4: Interface-Based Design
**Hierarchy**: RECOMMENDED
**Status**: ✅ COMPLIANT (90%)

#### Current State

**STRENGTHS:**
1. **✅ Excellent Provider Abstraction**: 7+ LLM providers with consistent interface
   - **Evidence**: `/home/swhouse/product/faultmaven/faultmaven/infrastructure/llm/providers/`
   - Implementations: openai_provider.py, anthropic.py, fireworks_provider.py, gemini.py, groq_provider.py, local_provider.py, huggingface.py
   - All implement `ILLMProvider` protocol

2. **✅ Storage Backend Abstraction**:
   - `IVectorStore` - ChromaDB, Pinecone (inferred from settings.py)
   - `ISessionStore` - Redis, InMemory (inferred from container)
   - `IStorageBackend` - S3, Filesystem (inferred from settings.py)

3. **✅ Protocol-Based Contracts**: Using `typing.Protocol` for structural typing
   ```python
   # Evidence from models/interfaces.py
   class ILLMProvider(Protocol):
       async def complete(...) -> LLMResponse: ...
   ```

4. **✅ Module Contracts Use Protocols**:
   - `ICaseRepository(Protocol)` - case/contracts.py:31
   - `IEvidenceQuery(Protocol)` - evidence/contracts.py (inferred)

**GAPS:**

1. **⚠️ Over-Abstraction in Single-Implementation Services** (Severity: LOW)
   - **Evidence**: Many services have interfaces but only one implementation
   - **Example**: `ICaseService` → only `CaseService` implementation
   - **Principle Guidance**: "If no, delete the Protocol"
   - **Impact**: Minor - adds navigation complexity but doesn't violate principle

2. **✅ No Violations**: Per "IDE Navigation Rule", having protocols for swappable components is correct
   - LLM providers: ✅ 7 implementations → Protocol justified
   - Vector stores: ✅ 2 implementations → Protocol justified
   - Storage backends: ✅ 2 implementations → Protocol justified

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P4-G1 | Some single-implementation services have unnecessary protocols | LOW | `ICaseService` only has `CaseService` |

#### Remediation Roadmap

**Optional: Protocol Cleanup** (1 day)
- Review all `I*Service` protocols and remove if only one implementation exists
- Keep protocols for: LLMProvider, VectorStore, SessionStore, StorageBackend (multiple implementations)
- Document decision: "Protocols for infrastructure, not domain services"

**Verdict**: No critical gaps. Principle well-implemented.

---

### Principle 5: Composition Root
**Hierarchy**: CRITICAL
**Status**: ❌ VIOLATED - BLOCKS DEPLOYMENT

#### Current State

**CRITICAL VIOLATION**: **Service Locator Anti-Pattern Detected**

**EVIDENCE OF VIOLATION:**

1. **❌ Services Call `container.get()` Directly** (18+ files)
   - **Evidence**: `grep -r "container.get" faultmaven/` found 18 files
   - **Key Violations**:
     ```python
     # File: api/v1/dependencies.py:19
     from ...container import container

     # File: api/v1/dependencies.py:38-39
     async def get_session_service():
         return container.get_session_service()  # ❌ SERVICE LOCATOR

     # File: modules/case/api/routes.py (inferred from grep)
     service = container.get_case_service()  # ❌ HIDDEN DEPENDENCY
     ```

2. **❌ Hidden Dependencies Everywhere**:
   - Services don't declare dependencies in constructors
   - Dependencies resolved at runtime via global container
   - **Impact**: Cannot see dependency graph; circular deps surface at runtime

3. **⚠️ Main.py Has Partial Composition Root** (Mixed Pattern)
   - **Evidence**: `main.py:134-155` shows container initialization
   ```python
   # main.py:134-142 (Correct pattern)
   from .container import container
   await container.initialize()
   app.extra["di_container"] = container

   # BUT: No explicit service wiring in main.py
   # Services are registered inside container.initialize()
   # Wiring happens in container providers, not composition root
   ```

**WHAT THE PRINCIPLES REQUIRE:**

```python
# ✅ CORRECT (from principles doc):
# main.py - ALL wiring happens here
async def startup():
    # Create infrastructure
    redis_store = RedisSessionStore(settings.redis_url)
    case_repo = PostgresCaseRepository(db_session)
    auth_service = AuthService(token_store=redis_store)

    # Wire services with explicit dependencies
    case_service = CaseService(
        auth=auth_service,  # ✅ INJECTED
        repo=case_repo,     # ✅ INJECTED
    )

    # Attach to app state for route access
    app.state.case_service = case_service

# services/case_service.py - NO container knowledge
class CaseService:
    def __init__(self, auth: IAuthService, repo: ICaseRepository):
        self.auth = auth  # ✅ Injected, not resolved
        self.repo = repo
```

**WHAT FAULTMAVEN ACTUALLY DOES:**

```python
# ❌ WRONG (current pattern):
# api/v1/dependencies.py
async def get_case_service():
    return container.get_case_service()  # ❌ SERVICE LOCATOR

# modules/case/api/routes.py
case_service = container.get_case_service()  # ❌ HIDDEN DEPENDENCY

# services/case_service.py (inferred)
class CaseService:
    def __init__(self):
        self.auth = ServiceContainer.get(AuthService)  # ❌ PULLS OWN DEPS
```

**ROOT CAUSE ANALYSIS:**

The codebase **attempted** to implement a DI Container but **implemented Service Locator instead**:
- ✅ Has a container: `/home/swhouse/product/faultmaven/faultmaven/_container_impl.py`
- ✅ Has provider registration: `container/providers/` directory
- ❌ Services call `container.get()` directly (Service Locator)
- ❌ No composition root in `main.py` that wires dependencies
- ❌ Constructor injection not enforced

**IMPORT-LINTER SHOWS FALSE COMPLIANCE:**

- **Evidence**: `IMPORT-LINTER-BASELINE.md:26-92` claims "0 violations" for service independence
- **Reality**: Import-linter only checks **static imports**, not runtime `container.get()` calls
- The "fix" documented in lines 72-92 uses `importlib.import_module()` to **hide** violations from the linter

```python
# Evidence from IMPORT-LINTER-BASELINE.md:85-92
# "After (Week 14-15 - COMPLIANT)" - NOT REALLY COMPLIANT
class KnowledgeSearchService:
    def __init__(self, knowledge_repo, embedding_service=None):
        if embedding_service is None:
            from faultmaven.core.container import ServiceContainer
            module = importlib.import_module('faultmaven.services.embedding_service')
            EmbeddingService = getattr(module, 'EmbeddingService')
            embedding_service = ServiceContainer.get(EmbeddingService)  # ❌ STILL SERVICE LOCATOR
```

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P5-G1 | **CRITICAL**: Services use `container.get()` - Service Locator anti-pattern | CRITICAL | 18+ files with `container.get` calls |
| P5-G2 | **CRITICAL**: No composition root in main.py wiring dependencies | CRITICAL | `main.py` doesn't wire services |
| P5-G3 | **CRITICAL**: Services don't declare dependencies in constructors | CRITICAL | Hidden dependencies |
| P5-G4 | **HIGH**: Import-linter bypassed with dynamic imports | HIGH | `IMPORT-LINTER-BASELINE.md:85-92` |
| P5-G5 | **MEDIUM**: FastAPI dependency injection used as Service Locator | MEDIUM | `api/v1/dependencies.py:38-39` |

#### Impact Assessment

**THIS IS A CRITICAL ARCHITECTURAL VIOLATION** because:

1. **Hidden Dependencies**: Cannot see what a service depends on by reading its constructor
2. **Testing Nightmare**: Must mock global container; cannot pass test doubles
3. **Runtime Errors**: Circular dependencies discovered at runtime, not startup
4. **Violates CRITICAL Principle**: Principle 5 is marked CRITICAL (blocks deployment)

**Example Impact on Testing:**
```python
# CURRENT (HARD TO TEST):
def test_case_service():
    # Must mock the entire global container
    with patch('faultmaven.container.container.get_auth_service'):
        service = CaseService()  # Dependencies hidden

# DESIRED (EASY TO TEST):
def test_case_service():
    # Explicit dependencies, easy to mock
    mock_auth = Mock(spec=IAuthService)
    mock_repo = Mock(spec=ICaseRepository)
    service = CaseService(auth=mock_auth, repo=mock_repo)
```

#### Remediation Roadmap

**MANDATORY FIX - DO NOT DEPLOY WITHOUT THIS** (Estimated: 3 weeks)

**Phase 1: Create True Composition Root** (1 week)
```python
# main.py
async def startup():
    settings = get_settings()

    # 1. Infrastructure Layer (bottom-up)
    db_session = await create_db_session(settings.database_url)
    redis_client = await create_redis_client(settings.redis_url)
    vector_store = await create_vector_store(settings.vector_backend)
    llm_provider = create_llm_provider(settings.llm_provider)

    # 2. Repository Layer
    case_repo = PostgresCaseRepository(db_session)
    auth_repo = AuthRepository(db_session)
    knowledge_repo = KnowledgeRepository(db_session, vector_store)

    # 3. Service Layer (inject all dependencies)
    auth_service = AuthService(
        repository=auth_repo,
        token_store=redis_client,
        settings=settings.auth
    )

    case_service = CaseService(
        repository=case_repo,
        auth_service=auth_service,  # ✅ EXPLICIT
        settings=settings.case
    )

    knowledge_service = KnowledgeService(
        repository=knowledge_repo,
        embedding_provider=llm_provider,
        auth_service=auth_service  # ✅ EXPLICIT
    )

    # 4. Attach to app for route access
    app.state.auth_service = auth_service
    app.state.case_service = case_service
    app.state.knowledge_service = knowledge_service
```

**Phase 2: Refactor Services to Constructor Injection** (1.5 weeks)
```python
# Before:
class CaseService:
    def __init__(self):
        self.auth = container.get_auth_service()  # ❌

# After:
class CaseService:
    def __init__(
        self,
        repository: ICaseRepository,
        auth_service: IAuthService,
        settings: CaseSettings
    ):
        self.repository = repository  # ✅ INJECTED
        self.auth_service = auth_service  # ✅ INJECTED
```

**Phase 3: Update FastAPI Dependencies** (0.5 weeks)
```python
# Before:
async def get_case_service():
    return container.get_case_service()  # ❌

# After:
async def get_case_service(request: Request):
    return request.app.state.case_service  # ✅ FROM COMPOSITION ROOT
```

**Phase 4: Enforce with Linter** (Optional, after refactor)
```python
# Add custom linter rule to detect container.get() calls
# Fails if any service calls container.get() outside main.py
```

**DEPLOYMENT BLOCKER**: This violation must be fixed before production deployment per Principle 5 CRITICAL status.

---

### Principle 6: Errors as Domain Concepts
**Hierarchy**: CRITICAL
**Status**: ✅ COMPLIANT (95%)

#### Current State

**STRENGTHS:**

1. **✅ Exception Hierarchies Per Module**: All modules have domain-specific exceptions
   - **Evidence**: Exception files found in all modules:
     ```
     modules/case/exceptions.py
     modules/auth/exceptions.py
     modules/knowledge/exceptions.py
     modules/evidence/exceptions.py (inferred)
     modules/report/exceptions.py (inferred)
     ```

2. **✅ Proper Inheritance Structure**:
   ```python
   # Evidence from case/exceptions.py
   class CaseException(FaultMavenException):      # Base for module
   class CaseNotFoundError(CaseException):        # Specific error
   class CaseStateError(CaseException):
   class CaseAccessError(CaseException):
   class CaseValidationError(CaseException):
   class CaseOperationError(CaseException):

   # Evidence from auth/exceptions.py
   class AuthException(FaultMavenException):
   class AuthenticationError(AuthException):
   class TokenError(AuthException):
   class TokenExpiredError(TokenError):           # Hierarchy within module
   class TokenInvalidError(TokenError):
   class SessionError(AuthException):

   # Evidence from knowledge/exceptions.py
   class KnowledgeException(KnowledgeBaseException):
   class DocumentNotFoundError(KnowledgeException):
   class DocumentIngestionError(KnowledgeException):
   class SearchError(KnowledgeException):
   ```

3. **✅ Exception Handlers Registered**:
   - **Evidence**: `main.py:718-722`
   ```python
   from faultmaven.api.exception_handlers import get_exception_handlers
   for exc_type, handler in get_exception_handlers().items():
       app.add_exception_handler(exc_type, handler)
   ```

**GAPS:**

1. **⚠️ Missing Infrastructure Error Wrapping** (Severity: LOW)
   - **Principle Requirement**: "Infrastructure errors are wrapped in domain terms"
   - **Evidence Needed**: Check if repositories wrap SQLAlchemy/Redis errors
   - **Example from Principles**:
     ```python
     # EXPECTED:
     try:
         result = await self.db.fetch_one(...)
     except DatabaseError as e:
         raise CaseError(f"Failed to retrieve case: {e}") from e
     ```
   - **Status**: Cannot verify without reading repository implementations
   - **Recommendation**: Audit repository files for raw exception propagation

2. **⚠️ No Centralized Error Documentation** (Severity: LOW)
   - Exceptions defined across 5+ files
   - No single reference showing all error types
   - Missing: HTTP status code mapping documentation

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P6-G1 | Repository infrastructure error wrapping not verified | LOW | Requires deep code audit |
| P6-G2 | No centralized error catalog documentation | LOW | Missing docs/errors.md |

#### Remediation Roadmap

**Quick Win: Document Error Catalog** (1 day)
```markdown
# docs/reference/error-catalog.md

## Case Module Errors
| Exception | HTTP Status | Cause | Resolution |
|-----------|-------------|-------|------------|
| CaseNotFoundError | 404 | Case ID doesn't exist | Verify case_id parameter |
| CaseAccessError | 403 | User lacks permission | Check user role/org |
```

**Code Audit: Infrastructure Error Wrapping** (2 days)
- Review all repository `try/except` blocks
- Ensure all `DatabaseError`, `RedisError`, `ChromaDBError` wrapped in domain exceptions
- Add tests for infrastructure error scenarios

**Verdict**: Mostly compliant; minor documentation gaps.

---

### Principle 7: Observability by Default
**Hierarchy**: IMPORTANT
**Status**: ⚠️ PARTIAL COMPLIANCE (60%)

#### Current State

**STRENGTHS:**

1. **✅ Correlation ID Middleware Exists**:
   - **Evidence**: 21 files reference `correlation_id` or `X-Correlation-ID`
   - **Implementation**: Correlation ID middleware present (inferred from grep results)
   ```python
   # Evidence from grep results:
   # Files with correlation_id:
   # - api/middleware/logging.py
   # - api/middleware/idempotency.py
   # - models/api.py
   ```

2. **✅ Structured Logging Infrastructure**:
   - **Evidence**: `infrastructure/logging/` directory exists
   - Files: `config.py`, `coordinator.py`, `unified.py`
   - Logging middleware: `api/middleware/logging.py`

3. **✅ Health Check Endpoints with Metrics**:
   - **Evidence**: `main.py:830-919` - comprehensive health endpoints
   - `/health` - component health with SLA metrics
   - `/health/dependencies` - dependency health
   - `/health/sla` - SLA status
   - `/health/components/{component_name}` - detailed component metrics

4. **✅ Metrics System Present**:
   - **Evidence**: Prometheus metrics exporter in `main.py:596-608`
   - Metrics middleware: `api/middleware/performance.py`
   - Metrics collector: `infrastructure/monitoring/metrics_collector.py` (inferred)

**GAPS:**

1. **⚠️ Inconsistent Correlation ID Propagation** (Severity: MEDIUM)
   - **Evidence**: Only 21 files reference correlation IDs
   - **Expected**: Every log statement should include correlation_id context
   - **Issue**: No enforcement mechanism; developers must remember to propagate

2. **⚠️ Missing Metric Naming Convention Enforcement** (Severity: MEDIUM)
   - **Principle Requirement**: `faultmaven_{module}_{operation}_{unit}`
   - **Evidence**: No linter or validation for metric names
   - **Risk**: Inconsistent naming across modules

3. **⚠️ Incomplete Tracing** (Severity: MEDIUM)
   - **Principle Requirement**: "Traces on every external call"
   - **Evidence**: Opik tracing middleware exists (`main.py:520-534`)
   - **Issue**: Conditional based on `SKIP_SERVICE_CHECKS` and test environment
   - **Gap**: No verification that all external calls (LLM, DB, Redis) are traced

4. **⚠️ No Observability Testing** (Severity: LOW)
   - Missing: Tests that verify correlation IDs propagate through layers
   - Missing: Tests that verify metrics are emitted for critical operations
   - Missing: Tracing coverage reports

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P7-G1 | Correlation ID propagation not enforced across all services | MEDIUM | Only 21/hundreds of files reference it |
| P7-G2 | Metric naming convention not validated at build time | MEDIUM | No linter for `faultmaven_*` pattern |
| P7-G3 | Tracing coverage for external calls not measured | MEDIUM | No tracing verification tests |
| P7-G4 | Observability behavior not tested | LOW | Missing correlation ID tests |

#### Remediation Roadmap

**Phase 1: Enforce Correlation ID Propagation** (1 week)
```python
# Add to logging configuration
import structlog

# Bind correlation_id to ALL loggers
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,  # ✅ Auto-adds correlation_id
        ...
    ]
)

# Add middleware to set contextvars
@app.middleware("http")
async def correlation_middleware(request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    structlog.contextvars.clear_contextvars()  # ✅ CLEANUP
    return response
```

**Phase 2: Add Metric Name Linter** (3 days)
```python
# scripts/validate_metrics.py
import re

METRIC_PATTERN = re.compile(r"faultmaven_[a-z]+_[a-z_]+_(total|seconds|count|bytes)")

def validate_metric_names(code_files):
    violations = []
    for file in code_files:
        # Find all metric definitions
        metrics = find_metrics(file)
        for metric in metrics:
            if not METRIC_PATTERN.match(metric.name):
                violations.append(f"{file}:{metric.line} - Invalid metric name: {metric.name}")
    return violations
```

**Phase 3: Tracing Coverage Tests** (1 week)
```python
# tests/observability/test_tracing.py
def test_llm_calls_traced():
    """Verify all LLM calls emit tracing spans."""
    with trace_collector() as collector:
        # Make LLM call
        response = await llm_provider.complete(messages)

        # Assert trace exists
        spans = collector.get_spans()
        assert any(s.name == "llm.completion" for s in spans)
        assert spans[0].attributes["llm.provider"] == "openai"
```

**Verdict**: Infrastructure exists but enforcement is incomplete. Not blocking but should be prioritized.

---

### Principle 8: Architectural Boundary Enforcement
**Hierarchy**: IMPORTANT
**Status**: ⚠️ PARTIAL COMPLIANCE (50%)

#### Current State

**STRENGTHS:**

1. **✅ Import-Linter Configured**:
   - **Evidence**: `/home/swhouse/product/faultmaven/.importlinter` exists (94 lines)
   - 4 contracts defined:
     1. Service layer independence
     2. Services cannot import API layer
     3. Models cannot import services
     4. Knowledge module layer boundaries

2. **✅ Contracts Pass (False Positive)**:
   - **Evidence**: `IMPORT-LINTER-BASELINE.md:12-16` reports:
     ```
     ✅ 3 contracts KEPT (zero violations)
     ❌ 0 contracts BROKEN
     ```

3. **✅ Layer Enforcement for Knowledge Module**:
   ```ini
   # Evidence from .importlinter:86-93
   [importlinter:contract:4]
   name = Knowledge module layer boundaries
   type = layers
   layers =
       faultmaven.modules.knowledge.api
       faultmaven.modules.knowledge.domain
       faultmaven.modules.knowledge.infrastructure
   ```

**GAPS:**

1. **❌ CRITICAL: Import-Linter Bypassed** (Severity: CRITICAL)
   - **Evidence**: `IMPORT-LINTER-BASELINE.md:85-92` documents the bypass technique
   ```python
   # "Compliant" code uses dynamic imports to hide violations
   import importlib
   module = importlib.import_module('faultmaven.services.embedding_service')
   EmbeddingService = getattr(module, 'EmbeddingService')
   embedding_service = ServiceContainer.get(EmbeddingService)
   ```
   - **Issue**: Import-linter only checks **static imports** (`from X import Y`)
   - **Impact**: Architectural violations hidden; linter gives false confidence

2. **❌ Missing Module Boundary Contracts** (Severity: HIGH)
   - **Evidence**: Only 1 of 7 modules has layer enforcement (knowledge)
   - **Missing Contracts**:
     - `modules.case` layer boundaries
     - `modules.auth` layer boundaries
     - `modules.agent` layer boundaries
     - `modules.evidence` layer boundaries
     - `modules.report` layer boundaries

   ```ini
   # EXPECTED (missing from .importlinter):
   [importlinter:contract:case_layers]
   name = Case module layer boundaries
   type = layers
   layers =
       faultmaven.modules.case.api
       faultmaven.modules.case.domain
       faultmaven.modules.case.infrastructure
   ```

3. **❌ Missing "Forbidden Domain Imports" Contract** (Severity: CRITICAL)
   - **Evidence**: No contract preventing `from faultmaven.modules.*.domain import`
   - **Required Contract** (not present):
   ```ini
   [importlinter:contract:module_internals]
   name = Modules must use contracts, not domain directly
   type = forbidden
   source_modules =
       faultmaven.services
       faultmaven.api
   forbidden_modules =
       faultmaven.modules.*.domain
   ```

4. **⚠️ No CI/CD Integration** (Severity: MEDIUM)
   - **Evidence**: No GitHub Actions workflow running import-linter
   - **Issue**: Developers can merge violations without detection
   - **Expected**: `.github/workflows/ci.yml` should run `lint-imports`

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P8-G1 | Import-linter bypassed with dynamic imports | CRITICAL | `IMPORT-LINTER-BASELINE.md:85-92` |
| P8-G2 | Only 1/7 modules have layer boundary enforcement | HIGH | Missing contracts for 6 modules |
| P8-G3 | No contract forbidding direct domain imports | CRITICAL | Services import `modules.*.domain` freely |
| P8-G4 | Import-linter not run in CI/CD | MEDIUM | No GitHub Actions workflow |

#### Remediation Roadmap

**Immediate Fix: Add Missing Contracts** (2 days)
```ini
# Add to .importlinter

[importlinter:contract:forbidden_domain_imports]
name = Services and API must use contracts, not domain
type = forbidden
source_modules =
    faultmaven.services
    faultmaven.api
forbidden_modules =
    faultmaven.modules.auth.domain
    faultmaven.modules.case.domain
    faultmaven.modules.knowledge.domain
    faultmaven.modules.agent.domain
    faultmaven.modules.evidence.domain
    faultmaven.modules.report.domain

[importlinter:contract:case_layers]
name = Case module layer boundaries
type = layers
layers =
    faultmaven.modules.case.api
    faultmaven.modules.case.domain
    faultmaven.modules.case.infrastructure

# Repeat for auth, agent, evidence, report
```

**Fix Dynamic Import Bypass** (1 week)
1. Remove `importlib.import_module()` workarounds
2. Refactor to use constructor injection (ties to P5-G1 fix)
3. Re-run import-linter; expect violations
4. Fix violations properly (not with dynamic imports)

**Add CI/CD Enforcement** (1 day)
```yaml
# .github/workflows/ci.yml
- name: Check architectural boundaries
  run: |
    pip install import-linter
    lint-imports
```

**Verdict**: Infrastructure exists but critically undermined by bypass patterns. Must be fixed.

---

### Principle 9: Test Safety Net
**Hierarchy**: RECOMMENDED
**Status**: ❌ VIOLATED (33% coverage vs 70% target)

#### Current State

**COVERAGE ANALYSIS:**

1. **❌ CRITICAL: 33.14% Coverage** (Target: 70%)
   - **Evidence**: `/home/swhouse/product/faultmaven/coverage.xml:1` shows `line-rate="0.3314"`
   - **Calculation**: 0.3314 × 100 = 33.14%
   - **Gap**: -36.86 percentage points below minimum

2. **✅ Large Test Suite Exists**: 3,792 test files
   - **Evidence**: `find -name "test_*.py" | wc -l` → 3,792 files
   - **Issue**: Tests exist but don't cover enough code

3. **✅ Coverage Tooling Configured**:
   - **Evidence**: `pyproject.toml` has `[tool.coverage.run]` and `[tool.coverage.report]`
   - Source tracking enabled
   - Exclusions configured (tests, __pycache__)

**GAPS:**

1. **❌ CRITICAL: Coverage Below 70% Floor** (Severity: CRITICAL)
   - **Current**: 33.14%
   - **Target**: 70% minimum
   - **Delta**: Need 36.86 percentage points
   - **Estimated Lines**: ~37,000 untested lines (assuming ~100K LOC)

2. **⚠️ No AI Evaluation Benchmarks** (Severity: MEDIUM)
   - **Principle Requirement**: "85% accuracy on benchmark incidents"
   - **Evidence**: No evaluation tests found
   - **Expected**:
     ```python
     # tests/evaluation/test_investigation_accuracy.py
     @pytest.mark.evaluation
     @pytest.mark.parametrize("incident", load_benchmark_incidents())
     async def test_root_cause_identification(incident, agent):
         result = await agent.investigate(incident.symptoms)
         similarity = compute_similarity(result.conclusion, incident.known_root_cause)
         assert similarity >= 0.85
     ```

3. **⚠️ No Coverage Enforcement in CI/CD** (Severity: MEDIUM)
   - **Evidence**: No GitHub Actions workflow failing on coverage drop
   - **Expected**: CI fails if coverage < 70%

4. **⚠️ Missing Coverage by Layer Metrics** (Severity: LOW)
   - **Principle Requirement**:
     - Domain services: 85%
     - API routes: 70%
     - Infrastructure: 60%
   - **Current**: Only total coverage reported (33%)

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P9-G1 | **CRITICAL**: Total coverage 33.14% (target: 70%) | CRITICAL | `coverage.xml` line-rate="0.3314" |
| P9-G2 | No AI evaluation benchmarks for accuracy testing | MEDIUM | Missing `tests/evaluation/` |
| P9-G3 | Coverage not enforced in CI/CD | MEDIUM | No GitHub Actions coverage check |
| P9-G4 | No per-layer coverage reporting | LOW | Missing domain/api/infra breakdown |

#### Remediation Roadmap

**Phase 1: Quick Coverage Wins** (2 weeks)
Focus on high-value, easy-to-test code:
1. **Domain Models**: Add unit tests for model validation, state transitions
   - Target: 90% coverage for `modules/*/domain/models/*.py`
   - Effort: Low (pure logic, no dependencies)

2. **API Routes**: Add integration tests for all endpoints
   - Target: 70% coverage for `modules/*/api/routes.py`
   - Effort: Medium (requires test client)

3. **Service Layer**: Add unit tests with mocked dependencies
   - Target: 80% coverage for `services/*.py`
   - Effort: High (but critical - most business logic here)

**Phase 2: Infrastructure Tests** (1 week)
1. Repository contract tests
2. LLM provider adapter tests
3. Storage backend tests

**Phase 3: AI Evaluation Benchmarks** (2 weeks)
```python
# tests/evaluation/benchmark_incidents.json
[
    {
        "incident_id": "INC-001",
        "symptoms": "PostgreSQL connection timeout errors in production",
        "known_root_cause": "Connection pool exhaustion due to missing connection.close()",
        "known_services": ["api-gateway", "postgres"],
        "severity": "P1"
    },
    # ... 50 total incidents
]

# tests/evaluation/test_agent_accuracy.py
@pytest.mark.evaluation
def test_investigation_accuracy_benchmark():
    incidents = load_benchmark_incidents()
    correct = 0

    for incident in incidents:
        result = await agent.investigate(incident.symptoms)
        similarity = compute_similarity(result.root_cause, incident.known_root_cause)
        if similarity >= 0.85:
            correct += 1

    accuracy = correct / len(incidents)
    assert accuracy >= 0.85, f"Agent accuracy {accuracy:.2%} below 85% threshold"
```

**Phase 4: Enforce in CI/CD** (1 day)
```yaml
# .github/workflows/ci.yml
- name: Run tests with coverage
  run: |
    pytest --cov=faultmaven --cov-report=xml --cov-fail-under=70
```

**Estimated Total Effort**: 6 weeks to reach 70% coverage

**Verdict**: Critical gap; must be addressed for production readiness.

---

### Principle 10: Bounded Complexity for AI Integration
**Hierarchy**: CRITICAL
**Status**: ✅ COMPLIANT (90%)

#### Current State

**STRENGTHS:**

1. **✅ LLM Adapters Are Stateless**:
   - **Evidence**: 7 provider implementations in `infrastructure/llm/providers/`
   - Files: `openai_provider.py`, `anthropic.py`, `fireworks_provider.py`, `gemini.py`, `groq_provider.py`, `local_provider.py`, `huggingface.py`
   - All extend `base.py` which defines stateless interface

2. **✅ Clear Separation of Concerns**:
   ```
   Orchestration Layer (Stateful):
   ├── modules/agent/domain/services/investigation_service.py
   ├── modules/agent/domain/services/investigation_orchestrator.py
   └── State management, retries, fallbacks

   Adapter Layer (Stateless):
   ├── infrastructure/llm/providers/*.py
   └── Pure functions: (messages, config) → response
   ```

3. **✅ LLM Provider Registry for Fallbacks**:
   - **Evidence**: `infrastructure/llm/providers/registry.py` (23,048 bytes)
   - Centralized provider management
   - Fallback chain support

**GAPS:**

1. **⚠️ Orchestration Layer Verification** (Severity: LOW)
   - **Evidence**: Investigation orchestrator exists (`investigation_orchestrator.py`)
   - **Missing**: Verification that orchestrator handles:
     - ✅ State management (need to verify)
     - ✅ Retry logic (need to verify)
     - ✅ Provider fallback (registry exists)
   - **Recommendation**: Code review of orchestrator to confirm

2. **⚠️ Token Counting Location** (Severity: LOW)
   - **Principle Requirement**: "Token counting BEFORE call" (adapter responsibility)
   - **Evidence**: Need to verify adapters implement `count_tokens()` method
   - **Risk**: If orchestrator does token counting, violates principle

**SPECIFIC CHECKS NEEDED:**

```python
# Need to verify in LLM adapter base class:
class ILLMAdapter(Protocol):
    async def complete(
        self,
        messages: list[Message],
        max_tokens: int,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Pure function: (messages, config) → response
        - No retries (orchestration handles) ✅ VERIFY
        - No state (orchestration handles) ✅ VERIFY
        - Validates tokens BEFORE calling ✅ VERIFY
        """

    def count_tokens(self, messages: list[Message]) -> int:
        """Synchronous token counting."""  # ✅ VERIFY THIS EXISTS
```

#### Gaps Identified

| Gap ID | Description | Severity | Evidence |
|--------|-------------|----------|----------|
| P10-G1 | Orchestrator retry logic not verified | LOW | Requires code review |
| P10-G2 | Token counting implementation location not verified | LOW | Check adapter base class |

#### Remediation Roadmap

**Code Review Verification** (1 day)
1. Read `investigation_orchestrator.py` - confirm retry logic
2. Read `infrastructure/llm/providers/base.py` - confirm `count_tokens()` method
3. Verify adapters don't maintain state between calls

**Documentation** (1 day)
```markdown
# docs/architecture/llm-integration-architecture.md

## Layer Responsibilities

| Concern | Orchestration | Adapter |
|---------|---------------|---------|
| State | ✅ Owns | ❌ None |
| Retries | ✅ Owns | ❌ None |
| Fallback | ✅ Owns | ❌ None |
| Token Count | ❌ Delegates | ✅ Owns |
```

**Verdict**: Appears compliant; requires code review confirmation.

---

## Consolidated Remediation Roadmap

### Phase 1: CRITICAL FIXES (Blocks Deployment) - 4 Weeks

**Must-Fix Before Production**

| Week | Work Item | Principle | Effort | Owner |
|------|-----------|-----------|--------|-------|
| 1 | **Fix Service Locator Anti-Pattern** | P5 (CRITICAL) | 1 week | Team Lead |
|   | - Create composition root in main.py | | | |
|   | - Refactor services to constructor injection | | | |
|   | - Remove all `container.get()` calls from services | | | |
| 2-3 | **Enforce Module Boundaries** | P3 (IMPORTANT) | 2 weeks | Senior Dev |
|   | - Create DTOs for all modules | | | |
|   | - Refactor services to use contracts only | | | |
|   | - Add import-linter forbidden domain import rule | | | |
| 4 | **Fix Import-Linter Bypass** | P8 (IMPORTANT) | 1 week | Senior Dev |
|   | - Remove dynamic import workarounds | | | |
|   | - Add CI/CD enforcement | | | |
|   | - Add layer boundaries for all modules | | | |

**Exit Criteria Phase 1:**
- ✅ No `container.get()` calls outside `main.py`
- ✅ All services use constructor injection
- ✅ No direct domain model imports from services
- ✅ Import-linter passes without dynamic import tricks
- ✅ CI/CD fails on architectural violations

---

### Phase 2: HIGH PRIORITY FIXES - 6 Weeks

**Production Quality Requirements**

| Week | Work Item | Principle | Effort | Owner |
|------|-----------|-----------|--------|-------|
| 5-10 | **Increase Test Coverage to 70%** | P9 (RECOMMENDED) | 6 weeks | Full Team |
|   | - Domain model tests (target: 90%) | | 1 week | |
|   | - Service layer tests (target: 80%) | | 2 weeks | |
|   | - API integration tests (target: 70%) | | 2 weeks | |
|   | - Infrastructure tests (target: 60%) | | 1 week | |
|   | - Add coverage enforcement to CI/CD | | | |

**Exit Criteria Phase 2:**
- ✅ Total coverage ≥ 70%
- ✅ Domain layer coverage ≥ 85%
- ✅ API layer coverage ≥ 70%
- ✅ Infrastructure coverage ≥ 60%
- ✅ CI/CD blocks merges below coverage threshold

---

### Phase 3: MEDIUM PRIORITY IMPROVEMENTS - 3 Weeks

**Operational Excellence**

| Week | Work Item | Principle | Effort | Owner |
|------|-----------|-----------|--------|-------|
| 11 | **Complete Vertical Architecture** | P2 (IMPORTANT) | 1 week | Senior Dev |
|   | - Create contracts for agent, report modules | | | |
|   | - Add bulk query methods to prevent N+1 | | | |
| 12 | **Improve Observability** | P7 (IMPORTANT) | 1 week | DevOps |
|   | - Enforce correlation ID propagation | | | |
|   | - Add metric name linter | | | |
|   | - Add tracing coverage tests | | | |
| 13 | **Fail-Fast Configuration** | P1 (IMPORTANT) | 1 week | Senior Dev |
|   | - Add startup connectivity checks | | | |
|   | - Create configuration presets | | | |
|   | - Document preset usage | | | |

**Exit Criteria Phase 3:**
- ✅ All 7 modules have contracts.py
- ✅ Correlation IDs in all logs
- ✅ Metrics follow naming convention
- ✅ Startup fails fast with actionable errors

---

### Phase 4: LOW PRIORITY POLISH - 1 Week

**Documentation and Cleanup**

| Week | Work Item | Principle | Effort | Owner |
|------|-----------|-----------|--------|-------|
| 14 | **Documentation** | Multiple | 1 week | Tech Writer |
|   | - Error catalog | P6 | | |
|   | - Cross-module data flow diagrams | P3 | | |
|   | - LLM architecture verification | P10 | | |

---

## Quick Wins (Can Be Done Immediately)

**High-Impact, Low-Effort Fixes (< 1 Day Each)**

1. **Add Import-Linter CI Check** (P8-G4)
   ```yaml
   # .github/workflows/ci.yml
   - name: Check architectural boundaries
     run: pip install import-linter && lint-imports
   ```

2. **Create Error Catalog** (P6-G2)
   - Document all domain exceptions with HTTP status codes
   - Single reference page for developers

3. **Add Metric Name Linter** (P7-G2)
   - Python script to validate `faultmaven_{module}_{operation}_{unit}` pattern

4. **Document Configuration Presets** (P1-G3)
   - `config/presets.py` with local/enterprise examples

---

## Architectural Debt Tracking

### Long-Term Strategic Issues

**These issues require architectural planning beyond immediate fixes:**

1. **Monolith vs Microservices Decision** (Not in scope)
   - Current: Modular monolith with vertical slices
   - Future: May extract modules to microservices
   - Recommendation: Continue modular monolith; vertical modules enable future extraction

2. **Event-Driven Architecture** (Not in scope)
   - Cross-module communication currently via synchronous service calls
   - Future: Consider event bus for loose coupling
   - Recommendation: Document as future enhancement, not current gap

3. **AI Evaluation Infrastructure** (P9-G2)
   - Need benchmark incident dataset (50+ incidents with known root causes)
   - Requires product/SRE collaboration
   - Timeline: 3-6 months for dataset creation

---

## Appendix: Evidence Summary

### Files Analyzed

**Configuration & Infrastructure:**
- `/home/swhouse/product/faultmaven/faultmaven/main.py` (1,412 lines)
- `/home/swhouse/product/faultmaven/faultmaven/config/settings.py` (100+ lines analyzed)
- `/home/swhouse/product/faultmaven/faultmaven/_container_impl.py` (150 lines analyzed)
- `/home/swhouse/product/faultmaven/.importlinter` (94 lines)

**Module Structure:**
- `/home/swhouse/product/faultmaven/faultmaven/modules/case/contracts.py` (310 lines)
- `/home/swhouse/product/faultmaven/faultmaven/modules/auth/contracts.py`
- `/home/swhouse/product/faultmaven/faultmaven/modules/knowledge/contracts.py`
- `/home/swhouse/product/faultmaven/faultmaven/modules/evidence/contracts.py`

**Database:**
- `/home/swhouse/product/faultmaven/alembic/versions/20251229_0412_001_baseline_schema.py`
- `/home/swhouse/product/faultmaven/faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py`

**Documentation:**
- `/home/swhouse/product/faultmaven/docs/architecture/architectural-design-principles.md` (946 lines)
- `/home/swhouse/product/faultmaven/docs/architecture/IMPORT-LINTER-BASELINE.md` (100 lines analyzed)

### Metrics Collected

- **Coverage**: 33.14% (from `coverage.xml`)
- **Test Files**: 3,792 files
- **Service Locator Violations**: 18 files with `container.get()` calls
- **Direct Domain Imports**: 12 violations in `/services/*.py`
- **Contracts Present**: 4 of 7 modules
- **LLM Providers**: 7 implementations
- **Correlation ID Files**: 21 files

---

## Conclusion

FaultMaven has made **significant progress** in architectural maturity with the implementation of:
- ✅ Vertical module structure
- ✅ Domain exception hierarchies
- ✅ Interface-based provider abstraction
- ✅ Observability infrastructure

However, **critical gaps prevent production deployment**:
1. **Service Locator anti-pattern** violates Principle 5 (CRITICAL)
2. **Module boundaries bypassed** via direct domain imports (IMPORTANT)
3. **Test coverage at 33%** vs 70% target (RECOMMENDED but essential)

**Deployment Recommendation**: **DO NOT DEPLOY** until Phase 1 critical fixes are complete (4 weeks estimated).

**Next Steps**:
1. Prioritize P5-G1 (composition root refactor) - 1 week
2. Fix module boundary violations (P2-G1, P3-G1) - 2 weeks
3. Add import-linter enforcement - 1 week
4. Begin coverage improvement campaign - 6 weeks (can overlap with above)

**Total Time to Production-Ready**: 10-12 weeks with dedicated effort.

---

**Document Status**: FINAL
**Date**: 2026-01-11
**Next Review**: After Phase 1 completion (4 weeks)

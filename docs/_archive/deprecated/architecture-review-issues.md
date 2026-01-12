# FaultMaven Architecture Review Issues

**Review Date**: 2026-01-09
**Based On**: `docs/architecture/architectural-design-principles.md` v2.0
**Total Issues**: 25
**Status**: Open for Assignment

---

## Issue Priority Legend

| Priority | Criteria |
|----------|----------|
| **P0 - CRITICAL** | Violates critical principles (5, 6, 10); blocks deployment |
| **P1 - HIGH** | Violates important principles (1, 2, 3, 7, 8); requires documented exception |
| **P2 - MEDIUM** | Violates recommended principles (4, 9); apply judgment |
| **P3 - LOW** | Code quality improvements; nice to have |

---

## Issue Index

| # | Title | Priority | Principle | Dependencies |
|---|-------|----------|-----------|--------------|
| 1 | [Replace raw Exception raises in LLM providers](#issue-1-replace-raw-exception-raises-in-llm-providers) | P0 | 6 | None |
| 2 | [Move HuggingFace retry logic to orchestration layer](#issue-2-move-huggingface-retry-logic-to-orchestration-layer) | P0 | 10 | None |
| 3 | [Create module-level exception hierarchies](#issue-3-create-module-level-exception-hierarchies) | P0 | 6 | None |
| 4 | [Fix silent failure patterns in user_store.py](#issue-4-fix-silent-failure-patterns-in-user_storepy) | P0 | 6 | #3 |
| 5 | [Remove ServiceContainer.get() from agent_orchestration_service.py](#issue-5-remove-servicecontainerget-from-agent_orchestration_servicepy) | P0 | 5 | None |
| 6 | [Remove ServiceContainer.get() from search_service.py](#issue-6-remove-servicecontainerget-from-search_servicepy) | P0 | 5 | None |
| 7 | [Remove ServiceContainer.get() from user_service.py](#issue-7-remove-servicecontainerget-from-user_servicepy) | P0 | 5 | None |
| 8 | [Remove ServiceContainer.get() from evidence_artifact_service.py](#issue-8-remove-servicecontainerget-from-evidence_artifact_servicepy) | P0 | 5 | None |
| 9 | [Create contracts.py for auth module](#issue-9-create-contractspy-for-auth-module) | P1 | 2 | None |
| 10 | [Create contracts.py for case module](#issue-10-create-contractspy-for-case-module) | P1 | 2 | None |
| 11 | [Create contracts.py for knowledge module](#issue-11-create-contractspy-for-knowledge-module) | P1 | 2 | None |
| 12 | [Create contracts.py for evidence module](#issue-12-create-contractspy-for-evidence-module) | P1 | 2 | None |
| 13 | [Create contracts.py for agent module](#issue-13-create-contractspy-for-agent-module) | P1 | 2 | None |
| 14 | [Create contracts.py for report module](#issue-14-create-contractspy-for-report-module) | P1 | 2 | None |
| 15 | [Fix cross-module imports in case/api/routes.py](#issue-15-fix-cross-module-imports-in-caseapiroutespy) | P1 | 2 | #9, #14 |
| 16 | [Fix cross-module imports in report services](#issue-16-fix-cross-module-imports-in-report-services) | P1 | 2 | #10 |
| 17 | [Fix cross-module imports in agent tools](#issue-17-fix-cross-module-imports-in-agent-tools) | P1 | 2 | #12 |
| 18 | [Remove cross-module JOINs in postgresql_hybrid_case_repository.py](#issue-18-remove-cross-module-joins-in-postgresql_hybrid_case_repositorypy) | P1 | 3 | #12 |
| 19 | [Decouple ChromaDB from core/knowledge/ingestion.py](#issue-19-decouple-chromadb-from-coreknowledgeingestionpy) | P1 | 1 | None |
| 20 | [Decouple ChromaDB from vector_store_service.py](#issue-20-decouple-chromadb-from-vector_store_servicepy) | P1 | 1 | None |
| 21 | [Decouple Redis from deduplication middleware](#issue-21-decouple-redis-from-deduplication-middleware) | P1 | 1 | None |
| 22 | [Add bulk query methods to repositories](#issue-22-add-bulk-query-methods-to-repositories) | P1 | 3 | None |
| 23 | [Unify correlation ID headers](#issue-23-unify-correlation-id-headers) | P1 | 7 | None |
| 24 | [Fix metric naming convention](#issue-24-fix-metric-naming-convention) | P1 | 7 | None |
| 25 | [Increase test coverage floor to 70%](#issue-25-increase-test-coverage-floor-to-70) | P2 | 9 | None |

---

## Detailed Issue Descriptions

---

### Issue 1: Replace raw Exception raises in LLM providers

**Priority**: P0 - CRITICAL
**Principle Violated**: 6 (Errors as Domain Concepts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### Problem Description

LLM provider implementations raise generic `Exception()` instead of domain-specific `LLMException`. This violates the principle that infrastructure errors must be wrapped in domain terms.

#### Files to Modify

| File | Lines | Current Code |
|------|-------|--------------|
| `faultmaven/infrastructure/llm/providers/openai_provider.py` | 97 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/anthropic.py` | 104 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/groq_provider.py` | 105 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/huggingface.py` | 112, 170 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/fireworks_provider.py` | 83, 91 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/local_provider.py` | 70, 108, 117, 179, 188, 222, 263, 272 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/gemini.py` | 137 | `raise Exception(...)` |
| `faultmaven/infrastructure/llm/providers/registry.py` | 455 | `raise Exception(...)` |

#### Required Changes

1. Import `LLMException` from `faultmaven/exceptions.py` in each provider file
2. Replace all `raise Exception(...)` with `raise LLMException(...)`
3. Preserve the original exception chain using `from e`

#### Example Fix

```python
# BEFORE
except aiohttp.ClientError as e:
    raise Exception(f"OpenAI API request failed: {e}")

# AFTER
from faultmaven.exceptions import LLMException

except aiohttp.ClientError as e:
    raise LLMException(f"OpenAI API request failed: {e}") from e
```

#### Acceptance Criteria

- [ ] All `raise Exception(...)` replaced with `raise LLMException(...)`
- [ ] Exception chaining preserved with `from e`
- [ ] All existing tests pass
- [ ] New unit tests verify `LLMException` is raised on provider errors

#### Testing Instructions

```bash
pytest tests/infrastructure/llm/ -v
pytest tests/ -k "llm" -v
```

---

### Issue 2: Move HuggingFace retry logic to orchestration layer

**Priority**: P0 - CRITICAL
**Principle Violated**: 10 (Bounded AI Complexity)
**Dependencies**: None
**Estimated Effort**: 3-4 hours

#### Problem Description

The HuggingFace provider implements its own retry logic for 503 (model loading) responses. Per Principle 10, LLM adapters must be stateless pure functions. Retry logic belongs in the orchestration layer (`BaseExternalClient`).

#### File to Modify

`faultmaven/infrastructure/llm/providers/huggingface.py`

#### Current Violation (Lines 105-197)

```python
# Lines 105-108 - VIOLATION: Adapter making retry decisions
if response.status == 503:
    # Model is loading, wait and retry once
    await self._handle_model_loading(session, url, headers, request_body)
    return await self._retry_request(session, url, headers, request_body, start_time, selected_model)

# Lines 159-197 - VIOLATION: Retry implementation in adapter
async def _retry_request(self, session: aiohttp.ClientSession, ...):
    """Retry request after model loading"""
    await asyncio.sleep(20)  # Wait for model to load
    # ... retry logic
```

#### Required Changes

1. **Remove `_retry_request` method** from HuggingFace provider
2. **Remove `_handle_model_loading` method** from HuggingFace provider
3. **Create new exception** `ModelLoadingException(LLMException)`
4. **Raise exception on 503** instead of retrying internally
5. **Configure retry in orchestration** via `BaseExternalClient.call_external()`

#### Target Architecture

```python
# huggingface.py - Adapter (stateless)
class HuggingFaceProvider:
    async def generate(self, ...):
        async with session.post(url, ...) as response:
            if response.status == 503:
                # Raise exception - let orchestration handle retry
                raise ModelLoadingException(
                    "HuggingFace model is loading. Retry recommended.",
                    retry_after=20
                )
            # ... normal response handling

# base_client.py or LLMRouter - Orchestration (stateful)
async def route_with_retry(self, ...):
    for attempt in range(retries):
        try:
            return await self.provider.generate(...)
        except ModelLoadingException as e:
            if attempt < retries - 1:
                await asyncio.sleep(e.retry_after)
            else:
                raise
```

#### Acceptance Criteria

- [ ] `_retry_request` method removed from HuggingFace provider
- [ ] `_handle_model_loading` method removed
- [ ] `ModelLoadingException` created in `faultmaven/exceptions.py`
- [ ] 503 responses raise `ModelLoadingException`
- [ ] Retry handled by `LLMRouter` or `BaseExternalClient`
- [ ] All HuggingFace tests pass
- [ ] New test verifies retry behavior at orchestration layer

#### Testing Instructions

```bash
pytest tests/infrastructure/llm/providers/test_huggingface.py -v
pytest tests/infrastructure/llm/test_router.py -v
```

---

### Issue 3: Create module-level exception hierarchies

**Priority**: P0 - CRITICAL
**Principle Violated**: 6 (Errors as Domain Concepts)
**Dependencies**: None
**Estimated Effort**: 4-5 hours

#### Problem Description

No module defines its own exception hierarchy in `domain/exceptions.py`. All modules rely on the central `faultmaven/exceptions.py`. Each module should define domain-specific exceptions that inherit from the base `FaultMavenException`.

#### Files to Create

| Module | File to Create |
|--------|----------------|
| auth | `faultmaven/modules/auth/domain/exceptions.py` |
| case | `faultmaven/modules/case/domain/exceptions.py` |
| knowledge | `faultmaven/modules/knowledge/domain/exceptions.py` |
| evidence | `faultmaven/modules/evidence/domain/exceptions.py` |
| agent | `faultmaven/modules/agent/domain/exceptions.py` |
| report | `faultmaven/modules/report/domain/exceptions.py` |

#### Template for Each Module

```python
# faultmaven/modules/{module}/domain/exceptions.py
"""
{Module} module domain exceptions.

All exceptions in this module inherit from {Module}Error,
which inherits from FaultMavenException.
"""

from faultmaven.exceptions import FaultMavenException


class {Module}Error(FaultMavenException):
    """Base exception for all {module} domain errors."""
    pass


class {Entity}NotFoundError({Module}Error):
    """Raised when a {entity} cannot be found."""

    def __init__(self, {entity}_id: str):
        self.{entity}_id = {entity}_id
        super().__init__(f"{Entity} {{{entity}_id}} not found")


class {Entity}AccessDeniedError({Module}Error):
    """Raised when user lacks permission to access {entity}."""

    def __init__(self, {entity}_id: str, user_id: str):
        self.{entity}_id = {entity}_id
        self.user_id = user_id
        super().__init__(f"User {user_id} cannot access {entity} {{{entity}_id}}")


# Add module-specific exceptions below...
```

#### Module-Specific Exceptions to Define

**Auth Module:**
- `AuthError` (base)
- `UserNotFoundError`
- `InvalidCredentialsError`
- `TokenExpiredError`
- `TokenInvalidError`
- `SessionNotFoundError`
- `PermissionDeniedError`

**Case Module:**
- `CaseError` (base)
- `CaseNotFoundError`
- `CaseAccessDeniedError`
- `InvestigationQuotaExceededError`
- `CaseAlreadyClosedError`

**Knowledge Module:**
- `KnowledgeError` (base)
- `KnowledgeItemNotFoundError`
- `EmbeddingGenerationError`
- `VectorSearchError`
- `DocumentIngestionError`

**Evidence Module:**
- `EvidenceError` (base)
- `EvidenceNotFoundError`
- `EvidenceUploadError`
- `EvidenceAccessDeniedError`
- `InvalidEvidenceTypeError`

**Agent Module:**
- `AgentError` (base)
- `AgentExecutionNotFoundError`
- `AgentExecutionFailedError`
- `ToolExecutionError`
- `AgentTimeoutError`

**Report Module:**
- `ReportError` (base)
- `ReportNotFoundError`
- `ReportGenerationError`
- `InvalidReportFormatError`

#### Acceptance Criteria

- [ ] All 6 exception files created
- [ ] Each module has base error class inheriting from `FaultMavenException`
- [ ] At least 3-5 specific exceptions per module
- [ ] Exception classes include relevant context (IDs, etc.)
- [ ] All existing tests pass
- [ ] New unit tests for exception classes

#### Testing Instructions

```bash
pytest tests/modules/ -v
python -c "from faultmaven.modules.auth.domain.exceptions import AuthError; print('OK')"
```

---

### Issue 4: Fix silent failure patterns in user_store.py

**Priority**: P0 - CRITICAL
**Principle Violated**: 6 (Errors as Domain Concepts)
**Dependencies**: Issue #3 (auth exceptions)
**Estimated Effort**: 2-3 hours

#### Problem Description

`DevUserStore` contains multiple `except Exception: return None` patterns that silently swallow errors. This hides failures and makes debugging extremely difficult.

#### File to Modify

`faultmaven/modules/auth/infrastructure/stores/user_store.py`

#### Current Violations

| Lines | Pattern | Problem |
|-------|---------|---------|
| 89-91 | `except Exception: return None` | Silent failure in `get_user()` |
| 114-116 | `except Exception: return None` | Silent failure in `get_user_by_username()` |
| 139-141 | `except Exception: return None` | Silent failure in `get_user_by_email()` |
| 218-222 | `raise Exception("User creation failed...")` | Generic exception |
| 269-273 | `raise Exception("User update failed...")` | Generic exception |
| 304-306 | `except Exception: return None` | Silent failure |
| 332-334 | `except Exception: return None` | Silent failure |
| 344-346 | `except Exception: return None` | Silent failure |

#### Required Changes

1. Import auth module exceptions (after Issue #3 is complete)
2. Replace `except Exception: return None` with proper exception handling
3. Replace `raise Exception(...)` with `raise UserCreationError(...)`, etc.
4. Add logging before re-raising exceptions

#### Example Fix

```python
# BEFORE
async def get_user(self, user_id: str) -> Optional[User]:
    try:
        # ... lookup logic
    except Exception:
        return None  # SILENT FAILURE

# AFTER
from faultmaven.modules.auth.domain.exceptions import (
    UserNotFoundError,
    AuthError
)
import structlog

logger = structlog.get_logger(__name__)

async def get_user(self, user_id: str) -> User:
    try:
        result = await self.db.fetch_one(...)
        if not result:
            raise UserNotFoundError(user_id)
        return User.from_row(result)
    except UserNotFoundError:
        raise  # Re-raise domain exception
    except Exception as e:
        logger.error("user_lookup_failed", user_id=user_id, error=str(e))
        raise AuthError(f"Failed to retrieve user {user_id}") from e
```

#### Acceptance Criteria

- [ ] All `except Exception: return None` patterns removed
- [ ] All `raise Exception(...)` replaced with domain exceptions
- [ ] Logging added for error conditions
- [ ] Method signatures updated (return `User` not `Optional[User]` where applicable)
- [ ] All auth tests pass
- [ ] New tests verify exceptions are raised on failures

#### Testing Instructions

```bash
pytest tests/modules/auth/ -v
pytest tests/ -k "user_store" -v
```

---

### Issue 5: Remove ServiceContainer.get() from agent_orchestration_service.py

**Priority**: P0 - CRITICAL
**Principle Violated**: 5 (Composition Root)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### Problem Description

`AgentOrchestrationService` uses dynamic imports and `ServiceContainer.get()` to resolve dependencies at runtime. This is a service locator anti-pattern that hides dependencies and makes testing difficult.

#### File to Modify

`faultmaven/modules/agent/domain/services/agent_orchestration_service.py`

#### Current Violation (Lines 228-236)

```python
def __init__(
    self,
    session_service: Optional[APIInvestigationSessionService] = None,
    evidence_service: Optional[APIEvidenceArtifactService] = None,
    ...
):
    if session_service is None or evidence_service is None:
        import importlib
        ServiceContainer = importlib.import_module('faultmaven.core.container').ServiceContainer
        APIInvestigationSessionService = importlib.import_module(
            'faultmaven.services.investigation_session_service'
        ).APIInvestigationSessionService
        APIEvidenceArtifactService = importlib.import_module(
            'faultmaven.services.evidence_artifact_service'
        ).APIEvidenceArtifactService

    self.session_service = session_service or ServiceContainer.get(APIInvestigationSessionService)
    self.evidence_service = evidence_service or ServiceContainer.get(APIEvidenceArtifactService)
```

#### Required Changes

1. **Remove all `ServiceContainer.get()` calls**
2. **Remove dynamic imports** (`importlib.import_module`)
3. **Make dependencies required** in constructor (not Optional)
4. **Update main.py** to wire dependencies at startup
5. **Update route handlers** to pass dependencies from app.state

#### Target Implementation

```python
# agent_orchestration_service.py
class AgentOrchestrationService:
    def __init__(
        self,
        session_service: IInvestigationSessionService,  # Required, use interface
        evidence_service: IEvidenceArtifactService,      # Required, use interface
        llm_router: ILLMRouter,
        ...
    ):
        self.session_service = session_service
        self.evidence_service = evidence_service
        self.llm_router = llm_router
        # No ServiceContainer usage!

# main.py (composition root)
async def lifespan(app: FastAPI):
    # Wire dependencies here
    session_service = InvestigationSessionService(...)
    evidence_service = EvidenceArtifactService(...)

    app.state.agent_orchestration_service = AgentOrchestrationService(
        session_service=session_service,
        evidence_service=evidence_service,
        ...
    )
```

#### Files to Update

| File | Change |
|------|--------|
| `modules/agent/domain/services/agent_orchestration_service.py` | Remove ServiceContainer usage |
| `faultmaven/main.py` | Add dependency wiring |
| `modules/agent/api/routes.py` | Get service from `request.app.state` |

#### Acceptance Criteria

- [ ] No `ServiceContainer.get()` in agent_orchestration_service.py
- [ ] No `importlib.import_module` for dependency resolution
- [ ] Constructor parameters are required (not Optional with defaults)
- [ ] Dependencies wired in main.py lifespan
- [ ] All agent tests pass
- [ ] Service can be instantiated with mock dependencies in tests

#### Testing Instructions

```bash
pytest tests/modules/agent/ -v
pytest tests/ -k "orchestration" -v
```

---

### Issue 6: Remove ServiceContainer.get() from search_service.py

**Priority**: P0 - CRITICAL
**Principle Violated**: 5 (Composition Root)
**Dependencies**: None
**Estimated Effort**: 1-2 hours

#### Problem Description

`SearchService` in the knowledge module resolves `EmbeddingService` and `VectorStoreService` via `ServiceContainer.get()` at runtime.

#### File to Modify

`faultmaven/modules/knowledge/domain/services/search_service.py`

#### Current Violation (Lines 83-84)

```python
def __init__(
    self,
    embedding_service: Optional[EmbeddingService] = None,
    vector_store: Optional[VectorStoreService] = None,
):
    self.embedding_service = embedding_service or ServiceContainer.get(EmbeddingService)
    self.vector_store = vector_store or ServiceContainer.get(VectorStoreService)
```

#### Required Changes

1. Remove `ServiceContainer.get()` fallbacks
2. Make `embedding_service` and `vector_store` required parameters
3. Use interface types instead of concrete classes
4. Wire dependencies in main.py

#### Target Implementation

```python
# search_service.py
from faultmaven.models.interfaces import IEmbeddingService, IVectorStore

class SearchService:
    def __init__(
        self,
        embedding_service: IEmbeddingService,
        vector_store: IVectorStore,
    ):
        self.embedding_service = embedding_service
        self.vector_store = vector_store
```

#### Acceptance Criteria

- [ ] No `ServiceContainer.get()` calls
- [ ] Dependencies are required constructor parameters
- [ ] Interface types used for type hints
- [ ] Wired in main.py
- [ ] All knowledge tests pass

#### Testing Instructions

```bash
pytest tests/modules/knowledge/ -v
```

---

### Issue 7: Remove ServiceContainer.get() from user_service.py

**Priority**: P0 - CRITICAL
**Principle Violated**: 5 (Composition Root)
**Dependencies**: None
**Estimated Effort**: 1-2 hours

#### Problem Description

`UserService` resolves `AuthService` via `ServiceContainer.get()`.

#### Files to Modify

- `faultmaven/services/user_service.py` (line 105)
- `faultmaven/modules/auth/domain/services/user_service.py` (line 105)

#### Current Violation

```python
self.auth_service = ServiceContainer.get(AuthService)
```

#### Required Changes

1. Remove `ServiceContainer.get()` call
2. Add `auth_service` as required constructor parameter
3. Wire in main.py

#### Acceptance Criteria

- [ ] No `ServiceContainer.get()` calls in either file
- [ ] `auth_service` is required parameter
- [ ] Wired in main.py
- [ ] All auth tests pass

---

### Issue 8: Remove ServiceContainer.get() from evidence_artifact_service.py

**Priority**: P0 - CRITICAL
**Principle Violated**: 5 (Composition Root)
**Dependencies**: None
**Estimated Effort**: 1-2 hours

#### Problem Description

`EvidenceArtifactService` resolves `FileStorageService` via `ServiceContainer.get()`.

#### File to Modify

`faultmaven/services/evidence_artifact_service.py` (line 78)

#### Current Violation

```python
self.file_storage = ServiceContainer.get(FileStorageService)
```

#### Required Changes

1. Remove `ServiceContainer.get()` call
2. Add `file_storage: IFileStorageBackend` as required constructor parameter
3. Wire in main.py using storage factory

#### Acceptance Criteria

- [ ] No `ServiceContainer.get()` calls
- [ ] Uses `IFileStorageBackend` interface type
- [ ] Wired in main.py
- [ ] All evidence tests pass

---

### Issue 9: Create contracts.py for auth module

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### Problem Description

The auth module lacks a `contracts.py` file that defines its public interface. Other modules should only import from `contracts.py`, not from internal domain/infrastructure.

#### File to Create

`faultmaven/modules/auth/contracts.py`

#### Required Contents

```python
"""
Auth Module Public Contracts

This file defines the public interface of the auth module.
Other modules MUST only import from this file, never from
domain/ or infrastructure/ directories.
"""

from typing import Protocol, Optional
from datetime import datetime
from uuid import UUID

# ============================================
# Data Transfer Objects (DTOs)
# ============================================

from dataclasses import dataclass

@dataclass(frozen=True)
class UserDTO:
    """Public representation of a user."""
    user_id: UUID
    username: str
    email: str
    display_name: Optional[str]
    is_active: bool
    created_at: datetime


@dataclass(frozen=True)
class SessionDTO:
    """Public representation of a session."""
    session_id: UUID
    user_id: UUID
    device_id: Optional[str]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticationResult:
    """Result of authentication attempt."""
    success: bool
    user: Optional[UserDTO]
    session: Optional[SessionDTO]
    error_message: Optional[str] = None


# ============================================
# Service Protocols (Interfaces)
# ============================================

class IAuthQuery(Protocol):
    """Read-only auth operations for cross-module use."""

    async def get_user(self, user_id: UUID) -> Optional[UserDTO]:
        """Get user by ID."""
        ...

    async def get_users_by_ids(self, user_ids: list[UUID]) -> list[UserDTO]:
        """Bulk user lookup - prevents N+1 queries."""
        ...

    async def get_session(self, session_id: UUID) -> Optional[SessionDTO]:
        """Get session by ID."""
        ...

    async def validate_session(self, session_id: UUID) -> bool:
        """Check if session is valid and not expired."""
        ...


class IAuthCommand(Protocol):
    """Write operations for auth (typically internal use)."""

    async def create_session(self, user_id: UUID, device_id: Optional[str] = None) -> SessionDTO:
        """Create new session for user."""
        ...

    async def invalidate_session(self, session_id: UUID) -> None:
        """Invalidate/logout a session."""
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from domain (allows: from auth.contracts import UserNotFoundError)
from faultmaven.modules.auth.domain.exceptions import (
    AuthError,
    UserNotFoundError,
    SessionNotFoundError,
    InvalidCredentialsError,
    TokenExpiredError,
)

__all__ = [
    # DTOs
    "UserDTO",
    "SessionDTO",
    "AuthenticationResult",
    # Protocols
    "IAuthQuery",
    "IAuthCommand",
    # Exceptions
    "AuthError",
    "UserNotFoundError",
    "SessionNotFoundError",
    "InvalidCredentialsError",
    "TokenExpiredError",
]
```

#### Acceptance Criteria

- [ ] `contracts.py` created with DTOs, protocols, and exception re-exports
- [ ] `IAuthQuery` includes bulk method (`get_users_by_ids`)
- [ ] No internal imports exposed (only contracts)
- [ ] Documented with module docstring
- [ ] Tests can import from contracts

---

### Issue 10: Create contracts.py for case module

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### File to Create

`faultmaven/modules/case/contracts.py`

#### Required Contents

Define public DTOs and protocols:

- `CaseDTO` - Public case representation
- `InvestigationDTO` - Public investigation representation
- `ICaseQuery` - Read operations (get_case, get_cases_by_ids, get_cases_for_user)
- `ICaseCommand` - Write operations (create_case, update_case, close_case)

Include bulk methods to prevent N+1:
- `get_cases_by_ids(case_ids: list[str]) -> list[CaseDTO]`

#### Acceptance Criteria

- [ ] `contracts.py` created
- [ ] DTOs defined for Case, Investigation
- [ ] Protocols include bulk methods
- [ ] Exceptions re-exported

---

### Issue 11: Create contracts.py for knowledge module

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### File to Create

`faultmaven/modules/knowledge/contracts.py`

#### Required Contents

- `KnowledgeItemDTO`
- `SearchResultDTO`
- `IKnowledgeQuery` (search, get_item, get_items_by_ids)
- `IKnowledgeCommand` (ingest, update, delete)

---

### Issue 12: Create contracts.py for evidence module

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### File to Create

`faultmaven/modules/evidence/contracts.py`

#### Required Contents

- `EvidenceDTO`
- `EvidenceMetadataDTO`
- `IEvidenceQuery` (get_evidence, get_evidence_by_ids, list_evidence_for_case)
- `IEvidenceCommand` (upload, delete)

---

### Issue 13: Create contracts.py for agent module

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### File to Create

`faultmaven/modules/agent/contracts.py`

#### Required Contents

- `AgentExecutionDTO`
- `ToolCallDTO`
- `IAgentQuery` (get_execution, list_executions_by_case)
- `IAgentCommand` (execute, cancel)

---

### Issue 14: Create contracts.py for report module

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### File to Create

`faultmaven/modules/report/contracts.py`

#### Required Contents

- `ReportDTO`
- `ReportRequestDTO`
- `IReportQuery` (get_report, list_reports_for_case)
- `IReportCommand` (generate_report)

---

### Issue 15: Fix cross-module imports in case/api/routes.py

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: Issues #9, #14 (auth and report contracts)
**Estimated Effort**: 3-4 hours

#### Problem Description

Case API routes directly import from auth and report module internals instead of using contracts.

#### File to Modify

`faultmaven/modules/case/api/routes.py`

#### Current Violations

| Line | Violation |
|------|-----------|
| 77 | `from faultmaven.modules.auth.domain.models.auth import DevUser` |
| 78 | `from faultmaven.modules.auth.domain.services.auth_session_service import AuthSessionService` |
| 1897-1898 | `from faultmaven.modules.report.domain.models import ReportRecommendation` |
| 1974-1975 | `from faultmaven.modules.report.domain.services.report_generation_service import ReportGenerationService` |
| 2177 | `from faultmaven.modules.report.domain.models import CaseClosureResponse, ArchivedReport` |

#### Required Changes

1. Replace auth internal imports with `from faultmaven.modules.auth.contracts import ...`
2. Replace report internal imports with `from faultmaven.modules.report.contracts import ...`
3. Use DTOs from contracts instead of domain models
4. Inject services via dependency injection, not direct imports

#### Example Fix

```python
# BEFORE
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import AuthSessionService

# AFTER
from faultmaven.modules.auth.contracts import UserDTO, IAuthQuery
```

#### Acceptance Criteria

- [ ] No imports from `modules.auth.domain` or `modules.auth.infrastructure`
- [ ] No imports from `modules.report.domain` or `modules.report.infrastructure`
- [ ] Only imports from `contracts.py` files
- [ ] Services accessed via DI, not direct instantiation
- [ ] All case API tests pass

---

### Issue 16: Fix cross-module imports in report services

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: Issue #10 (case contracts)
**Estimated Effort**: 2-3 hours

#### Problem Description

Report services directly import Case domain models.

#### Files to Modify

| File | Line | Violation |
|------|------|-----------|
| `modules/report/domain/services/report_generation_service.py` | 27 | `from faultmaven.modules.case.domain.models import Case, CaseStatus` |
| `modules/report/domain/services/report_recommendation_service.py` | 22 | `from faultmaven.modules.case.domain.models import Case` |

#### Required Changes

1. Import `CaseDTO` from `faultmaven.modules.case.contracts`
2. Update service methods to accept `CaseDTO` instead of `Case`
3. Inject `ICaseQuery` to fetch case data

#### Acceptance Criteria

- [ ] No imports from `modules.case.domain`
- [ ] Uses `CaseDTO` from contracts
- [ ] All report tests pass

---

### Issue 17: Fix cross-module imports in agent tools

**Priority**: P1 - HIGH
**Principle Violated**: 2 (Vertical Modules with Contracts)
**Dependencies**: Issue #12 (evidence contracts)
**Estimated Effort**: 1-2 hours

#### File to Modify

`faultmaven/modules/agent/tools/list_evidence_tool.py`

#### Current Violation (Line 100)

```python
from faultmaven.modules.evidence.domain.models import EvidenceArtifactType
```

#### Required Changes

1. Export `EvidenceArtifactType` in evidence contracts
2. Import from contracts instead of domain

#### Acceptance Criteria

- [ ] Import from `modules.evidence.contracts`
- [ ] All agent tool tests pass

---

### Issue 18: Remove cross-module JOINs in postgresql_hybrid_case_repository.py

**Priority**: P1 - HIGH
**Principle Violated**: 3 (Database Boundaries)
**Dependencies**: Issue #12 (evidence contracts - need IEvidenceQuery)
**Estimated Effort**: 4-6 hours

#### Problem Description

Case repository directly JOINs evidence module tables, violating database boundaries.

#### File to Modify

`faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py`

#### Current Violations

| Lines | SQL |
|-------|-----|
| 217-220 | `LEFT JOIN evidence e ON c.case_id = e.case_id` |
| 502 | `LEFT JOIN evidence e ON c.case_id = e.case_id` |
| 657-661 | Multiple JOINs including evidence table |

#### Required Changes

1. Remove all `LEFT JOIN evidence` clauses
2. Inject `IEvidenceQuery` into repository
3. Load evidence separately using evidence module's API
4. Compose results in application layer

#### Target Architecture

```python
class PostgresHybridCaseRepository:
    def __init__(
        self,
        db: AsyncSession,
        evidence_query: IEvidenceQuery,  # Injected dependency
    ):
        self.db = db
        self.evidence_query = evidence_query

    async def get_case_with_evidence(self, case_id: str) -> CaseWithEvidence:
        # Step 1: Get case (own module's table)
        case = await self._get_case(case_id)

        # Step 2: Get evidence via evidence module's API
        evidence = await self.evidence_query.list_evidence_for_case(case_id)

        # Step 3: Compose result
        return CaseWithEvidence(case=case, evidence=evidence)
```

#### Acceptance Criteria

- [ ] No JOINs on `evidence` table
- [ ] Evidence loaded via `IEvidenceQuery`
- [ ] Query performance acceptable (add tests)
- [ ] All case tests pass
- [ ] No N+1 queries (use bulk methods)

---

### Issue 19: Decouple ChromaDB from core/knowledge/ingestion.py

**Priority**: P1 - HIGH
**Principle Violated**: 1 (Deployment Agnostic)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### Problem Description

Business logic directly imports and instantiates ChromaDB client.

#### File to Modify

`faultmaven/core/knowledge/ingestion.py`

#### Current Violations (Lines 34, 37)

```python
import chromadb
from chromadb.config import Settings

# Later in code:
client = chromadb.HttpClient(...)
# or
client = chromadb.PersistentClient(...)
```

#### Required Changes

1. Remove direct `chromadb` imports
2. Accept `IVectorBackend` via constructor/parameter
3. Use existing factory: `infrastructure/vector/factory.py`

#### Target Implementation

```python
# BEFORE
import chromadb
class KnowledgeIngestionService:
    def __init__(self):
        self.client = chromadb.PersistentClient(...)

# AFTER
from faultmaven.infrastructure.vector.base import IVectorBackend

class KnowledgeIngestionService:
    def __init__(self, vector_backend: IVectorBackend):
        self.vector_backend = vector_backend
```

#### Acceptance Criteria

- [ ] No `import chromadb` in file
- [ ] Uses `IVectorBackend` interface
- [ ] Vector backend injected via DI
- [ ] All ingestion tests pass

---

### Issue 20: Decouple ChromaDB from vector_store_service.py

**Priority**: P1 - HIGH
**Principle Violated**: 1 (Deployment Agnostic)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### File to Modify

`faultmaven/modules/knowledge/domain/services/vector_store_service.py`

#### Current Violations (Lines 24-25)

```python
import chromadb
from chromadb.config import Settings
```

#### Required Changes

Same as Issue #19 - use `IVectorBackend` interface.

#### Acceptance Criteria

- [ ] No `import chromadb`
- [ ] Uses `IVectorBackend`
- [ ] All knowledge tests pass

---

### Issue 21: Decouple Redis from deduplication middleware

**Priority**: P1 - HIGH
**Principle Violated**: 1 (Deployment Agnostic)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### Problem Description

Deduplication middleware directly imports Redis, preventing local development without Redis.

#### File to Modify

`faultmaven/api/middleware/deduplication.py`

#### Current Violations (Lines 18-19)

```python
import redis.asyncio as aioredis
from redis.exceptions import RedisError
```

#### Required Changes

1. Create `IDeduplicationStore` interface
2. Implement `RedisDeduplicationStore` and `InMemoryDeduplicationStore`
3. Inject store via middleware configuration
4. Add factory function for store selection

#### Target Implementation

```python
# interfaces.py
class IDeduplicationStore(Protocol):
    async def check_and_set(self, key: str, ttl: int) -> bool:
        """Returns True if key was set (not duplicate), False if exists."""
        ...

# middleware/deduplication.py
class DeduplicationMiddleware:
    def __init__(self, store: IDeduplicationStore):
        self.store = store
```

#### Acceptance Criteria

- [ ] No direct Redis imports in middleware
- [ ] `IDeduplicationStore` interface created
- [ ] Redis and InMemory implementations exist
- [ ] Store selected via configuration
- [ ] Works without Redis in local dev

---

### Issue 22: Add bulk query methods to repositories

**Priority**: P1 - HIGH
**Principle Violated**: 3 (Database Boundaries - N+1 Prevention)
**Dependencies**: None
**Estimated Effort**: 3-4 hours

#### Problem Description

Several repositories lack bulk query methods, leading to N+1 query patterns when modules need to fetch multiple entities.

#### Files to Modify

| Repository | Method to Add |
|------------|---------------|
| `modules/auth/infrastructure/repositories/user_repository.py` | `get_users_by_ids(user_ids: list[UUID])` |
| `modules/case/infrastructure/case_repository.py` | `get_cases_by_ids(case_ids: list[str])` |
| `modules/evidence/infrastructure/evidence_repository.py` | `get_evidence_by_ids(evidence_ids: list[UUID])` |

#### Implementation Template

```python
async def get_users_by_ids(self, user_ids: list[UUID]) -> list[User]:
    """Bulk user lookup - single query instead of N queries."""
    if not user_ids:
        return []

    stmt = select(UserModel).where(UserModel.id.in_(user_ids))
    result = await self.db.execute(stmt)
    rows = result.scalars().all()

    # Preserve order matching input
    user_map = {u.id: User.from_orm(u) for u in rows}
    return [user_map.get(uid) for uid in user_ids if uid in user_map]
```

#### Acceptance Criteria

- [ ] `get_*_by_ids` method added to each repository
- [ ] Returns results in input order
- [ ] Handles empty input list
- [ ] Single query execution (verify with SQL logging)
- [ ] Unit tests for bulk methods

---

### Issue 23: Unify correlation ID headers

**Priority**: P1 - HIGH
**Principle Violated**: 7 (Observability by Default)
**Dependencies**: None
**Estimated Effort**: 1-2 hours

#### Problem Description

Two different headers used for request correlation:
- `X-Request-ID` in `api/middleware/request_id.py`
- `X-Correlation-ID` in `api/middleware/logging.py`

#### Files to Modify

| File | Change |
|------|--------|
| `faultmaven/api/middleware/request_id.py` | Rename to use `X-Correlation-ID` |
| `faultmaven/api/middleware/logging.py` | Already uses correct header |

#### Required Changes

1. Standardize on `X-Correlation-ID` (industry standard for distributed tracing)
2. Update `request_id.py` middleware to use `X-Correlation-ID`
3. Ensure both middlewares use the same context variable
4. Update any documentation

#### Acceptance Criteria

- [ ] Only `X-Correlation-ID` header used
- [ ] Single source of truth for correlation ID
- [ ] All middleware tests pass

---

### Issue 24: Fix metric naming convention

**Priority**: P1 - HIGH
**Principle Violated**: 7 (Observability by Default)
**Dependencies**: None
**Estimated Effort**: 2-3 hours

#### Problem Description

Metrics don't follow the documented naming convention: `faultmaven_{module}_{operation}_{unit}`

#### File to Modify

`faultmaven/infrastructure/observability/tracing.py`

#### Current vs Expected Names

| Current Name | Expected Name |
|--------------|---------------|
| `faultmaven_request_duration_seconds` | `faultmaven_api_request_duration_seconds` |
| `faultmaven_llm_requests_total` | `faultmaven_llm_route_requests_total` |
| `faultmaven_llm_request_duration_seconds` | `faultmaven_llm_route_duration_seconds` |
| `faultmaven_function_duration_seconds` | Module-specific names |

#### Required Changes

1. Rename all metrics to follow `faultmaven_{module}_{operation}_{unit}` pattern
2. Update any dashboards/alerts that reference old names
3. Document metric naming convention

#### Acceptance Criteria

- [ ] All metrics follow naming convention
- [ ] Module component clearly identified in name
- [ ] Unit suffix accurate (seconds, total, bytes, etc.)

---

### Issue 25: Increase test coverage floor to 70%

**Priority**: P2 - MEDIUM
**Principle Violated**: 9 (Test Safety Net)
**Dependencies**: None
**Estimated Effort**: 1 hour (config) + ongoing (test writing)

#### Problem Description

Current coverage floor is 50%, but principle specifies 70% minimum.

#### File to Modify

`pytest.ini`

#### Current Setting (Line 19)

```ini
--cov-fail-under=50
```

#### Required Changes

1. Increase to `--cov-fail-under=70`
2. Run coverage report to identify gaps
3. Plan test additions for uncovered code

#### Phased Approach (if needed)

If 70% cannot be achieved immediately:
1. Week 1: Increase to 55%
2. Week 2: Increase to 60%
3. Week 3: Increase to 65%
4. Week 4: Increase to 70%

#### Acceptance Criteria

- [ ] `pytest.ini` updated to `--cov-fail-under=70`
- [ ] CI passes with 70% coverage
- [ ] Coverage report shows actual coverage ≥70%

#### Commands

```bash
# Check current coverage
pytest --cov=faultmaven --cov-report=term-missing

# Generate HTML report for analysis
pytest --cov=faultmaven --cov-report=html
open htmlcov/index.html
```

---

## Appendix: Validation Commands

### Run All Architecture Checks

```bash
# Import linter
lint-imports --config .importlinter

# Architecture violation script
python scripts/check_import_violations.py

# Full test suite
pytest --cov=faultmaven --cov-fail-under=70

# Type checking
mypy faultmaven/
```

### Verify Specific Issues

```bash
# Issue 1-2: LLM providers
pytest tests/infrastructure/llm/ -v

# Issues 5-8: Composition root
grep -r "ServiceContainer.get" faultmaven/ --include="*.py"

# Issues 9-17: Module contracts
find faultmaven/modules -name "contracts.py"

# Issue 18: Database boundaries
grep -r "LEFT JOIN evidence" faultmaven/ --include="*.py"

# Issues 19-21: Deployment coupling
grep -r "import chromadb\|import redis" faultmaven/ --include="*.py" | grep -v infrastructure
```

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-01-09 | Architecture Review | Initial issue documentation |

---

**Next Steps**: Assign issues to developers/agents in dependency order. Start with P0 issues that have no dependencies (Issues 1, 2, 3, 5, 6, 7, 8).

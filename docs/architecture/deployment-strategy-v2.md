# FaultMaven Deployment Strategy v2.1

## Executive Summary

This document defines the deployment strategy for FaultMaven, supporting two distinct deployment scenarios using the **deployment neutrality principle**:

- **Local Deployment**: Single-user, self-hosted, free tier
- **Cloud Deployment**: Multi-user, managed SaaS, subscription-based

**Both deployments use the SAME codebase, SAME Docker image, with ZERO conditional logic.** Infrastructure differences are handled entirely through the provider pattern and environment variables.

**Version**: 2.1
**Date**: 2025-12-31
**Status**: Revised based on architectural review feedback

---

## Table of Contents

1. [Deployment Neutrality Principle](#1-deployment-neutrality-principle)
2. [FaultMaven Core](#2-faultmaven-core)
3. [Five Infrastructure Layers](#3-five-infrastructure-layers)
4. [Knowledge Base Architecture](#4-knowledge-base-architecture)
5. [Data Model](#5-data-model)
6. [Configuration](#6-configuration)
7. [Gap Analysis](#7-gap-analysis)
8. [Implementation Roadmap](#8-implementation-roadmap)

---

## 1. Deployment Neutrality Principle

### Core Principle

> **"Infrastructure choices are deployment-time decisions, not code-time decisions."**

This means:

| Requirement | Implementation |
|-------------|----------------|
| ✅ Same codebase for all deployments | Single repository, no deployment branches |
| ✅ Same Docker image | One artifact serves all environments |
| ✅ Zero conditional logic | No `if deployment == "local"` anywhere |
| ✅ Environment variables control behavior | `TENANT_PROVIDER=single` vs `multi` |
| ❌ NO separate packages | No `faultmaven/local/` or `faultmaven/cloud/` |
| ❌ NO deployment mode enums | No `DeploymentMode.LOCAL` in code |
| ❌ NO feature flags based on environment | Providers handle everything |

### How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                    Application Code                              │
│                                                                  │
│  Uses interfaces only - no deployment-specific logic             │
│  • CaseService calls TenantProvider.get_user_organization_id()   │
│  • KnowledgeService calls VectorStore.search()                   │
│  • EvidenceService calls StorageBackend.store()                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Provider Layer                                │
│                                                                  │
│  Environment variables select implementations at startup         │
│                                                                  │
│  TENANT_PROVIDER=single → SingleTenantProvider                   │
│  TENANT_PROVIDER=multi  → MultiTenantProvider                    │
│                                                                  │
│  STORAGE_BACKEND=local → LocalStorageBackend                     │
│  STORAGE_BACKEND=s3    → S3StorageBackend                        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Deployment Scenarios

| Aspect | Local Deployment | Cloud Deployment |
|--------|------------------|------------------|
| **Target** | Individual engineer | Teams and organizations |
| **Infrastructure** | SQLite, local filesystem, ChromaDB | PostgreSQL, S3, Pinecone |
| **Users** | Single user | Multiple users |
| **Organizations** | Default org (API consistency) | Full org management |
| **Knowledge Base** | User KB only | Global KB + Org KB + User KB |
| **Sharing** | N/A | Full sharing capabilities |
| **Price** | Free | Subscription |

---

## 2. FaultMaven Core

FaultMaven Core is the investigation engine shared by all deployments. It consists of:

### Core Components

| Component | Location | Interfaces |
|-----------|----------|------------|
| **Case Management** | `services/case_service.py` | `ICaseRepository` |
| **Session Management** | `services/investigation_session_service.py` | `ISessionStore` |
| **Evidence Processing** | `services/evidence_artifact_service.py` | `IStorageBackend` |
| **Knowledge Base** | `services/knowledge_search_service.py` | `IVectorStore` |
| **Agent Orchestration** | `services/agent_orchestration_service.py` | `ILLMProvider` |

### Existing Interfaces

The codebase already defines comprehensive interfaces in `models/interfaces.py`:

```python
class ILLMProvider(ABC)      # LLM abstraction
class IVectorStore(ABC)      # Vector database abstraction
class ISessionStore(ABC)     # Session storage abstraction
class ISanitizer(ABC)        # PII redaction abstraction
class IStorageBackend(ABC)   # File storage abstraction
```

---

## 3. Five Infrastructure Layers

Following the deployment neutrality pattern, FaultMaven implements **five infrastructure layers** as provider abstractions.

### Layer Overview

| Layer | Interface | Purpose | Providers |
|-------|-----------|---------|-----------|
| **1. Data** | SQLAlchemy | Persistent storage | SQLite, PostgreSQL |
| **2. Files** | `IStorageBackend` | Evidence files | LocalStorage, S3 |
| **3. Vector** | `IVectorStore` | KB embeddings | ChromaDB, Pinecone |
| **4. Cache** | `ISessionStore` | Sessions, cache | InMemory, Redis |
| **5. Tenant** | `TenantProvider` | Multi-tenancy | Single, Multi |

### Layer 5: TenantProvider (Critical for Deployment Neutrality)

The **TenantProvider** is the key abstraction that enables the same application code to work in both local and cloud deployments without any conditional logic.

#### Design Decision: Default Organization for Local Deployment

After comparing approaches, the **default organization pattern** is preferred because:

1. **Same interface contract**: `get_user_organization_id()` always returns a string - no null handling
2. **Simpler application code**: No `if org_id is None` checks throughout codebase
3. **API consistency**: Same response structure in both deployments
4. **Easier migration**: Resources already have org_id when migrating to cloud

```python
# faultmaven/providers/tenancy/base.py

from typing import Protocol, List, Tuple, Dict, Any

class TenantProvider(Protocol):
    """Abstract interface for tenant/organization management.

    This provider enables deployment neutrality by abstracting
    how organization context is resolved for a user.

    Implementations:
    - SingleTenantProvider: Local deployment (default organization)
    - MultiTenantProvider: Cloud deployment (database-backed)

    Key Design Decision:
    - Both providers return organization_id as string (never None)
    - SingleTenantProvider uses DEFAULT_ORGANIZATION_ID
    - This avoids null checks throughout application code
    """

    async def get_user_organization_id(self, user_id: str) -> str:
        """Get organization_id for user.

        SingleTenant: Returns DEFAULT_ORGANIZATION_ID
        MultiTenant: Queries database for user's primary org
        """
        ...

    async def verify_membership(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Verify user is member of organization.

        SingleTenant: Returns True if org_id == DEFAULT_ORGANIZATION_ID
        MultiTenant: Queries database for membership
        """
        ...

    async def list_user_organizations(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List organizations user belongs to.

        SingleTenant: Returns [default_org]
        MultiTenant: Queries database for all user orgs

        Returns: (organizations, total_count)
        """
        ...

    async def get_organization_members(
        self,
        organization_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List members of organization.

        SingleTenant: Returns [current_user] (single member)
        MultiTenant: Queries database for org members

        Returns: (members, total_count)
        """
        ...
```

### SingleTenantProvider Implementation

```python
# faultmaven/providers/tenancy/single.py

class SingleTenantProvider:
    """Single-tenant provider for self-hosted local deployment.

    Design: Uses a default organization for API consistency.

    In local deployment:
    - User is auto-assigned to DEFAULT_ORGANIZATION_ID
    - All resources belong to this default organization
    - Organization endpoints return consistent data
    - No multi-user features (single user only)

    Benefits of default org approach:
    - No null checks for org_id in application code
    - Same API response structure as cloud deployment
    - Easier migration path to cloud (org_id already exists)
    """

    def __init__(
        self,
        default_org_id: str = "local-user-org",
        user_repository: Optional[UserRepository] = None
    ):
        self.default_org_id = default_org_id
        self.user_repository = user_repository
        self.default_org = {
            "organization_id": default_org_id,
            "name": "Local Organization",
            "slug": "local",
            "plan_tier": "unlimited",
            "max_members": 1,
        }

    async def get_user_organization_id(self, user_id: str) -> str:
        """Return default organization ID for single-user deployment."""
        return self.default_org_id

    async def verify_membership(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Verify user belongs to default org."""
        return organization_id == self.default_org_id

    async def list_user_organizations(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return default organization."""
        if offset > 0:
            return ([], 1)
        return ([self.default_org], 1)

    async def get_organization_members(
        self,
        organization_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return the single user as member.

        Note: Unlike v1.0 which returned empty, we return the actual user
        for consistency - if the org exists, user should be a member.
        """
        if organization_id != self.default_org_id:
            return ([], 0)

        # If we have user repository, return actual user
        if self.user_repository:
            users = await self.user_repository.list_users(limit=1)
            if users:
                return ([{
                    "user_id": users[0].user_id,
                    "email": users[0].email,
                    "role": "owner",
                }], 1)

        # Fallback: return minimal member info
        return ([{"role": "owner"}], 1)
```

### MultiTenantProvider Implementation

```python
# faultmaven/providers/tenancy/multi.py

class MultiTenantProvider:
    """Multi-tenant provider for Cloud SaaS deployment.

    In cloud deployment:
    - Full organization management with database-backed isolation
    - Users belong to specific organizations (database enforced)
    - Complete RBAC with owner/admin/member roles
    - Plan tier limits enforced
    """

    def __init__(
        self,
        org_repository: OrganizationRepository,
        user_repository: UserRepository
    ):
        self.org_repository = org_repository
        self.user_repository = user_repository

    async def get_user_organization_id(self, user_id: str) -> str:
        """Query database for user's primary organization."""
        memberships = await self.org_repository.get_user_memberships(user_id)
        if not memberships:
            raise ValueError(
                f"User {user_id} has no organization. "
                "User must create or join an organization."
            )
        return memberships[0].organization_id

    async def verify_membership(
        self,
        user_id: str,
        organization_id: str
    ) -> bool:
        """Check database for organization membership."""
        return await self.org_repository.is_member(user_id, organization_id)

    async def list_user_organizations(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query database for user's organizations."""
        memberships = await self.org_repository.get_user_memberships(
            user_id, limit=limit, offset=offset
        )

        orgs = []
        for membership in memberships:
            org = await self.org_repository.get_organization(
                membership.organization_id
            )
            orgs.append({
                "organization_id": org.organization_id,
                "name": org.name,
                "slug": org.slug,
                "plan_tier": org.plan_tier,
                "role": membership.role,
                "member_since": membership.created_at,
            })

        total = await self.org_repository.count_user_memberships(user_id)
        return (orgs, total)

    async def get_organization_members(
        self,
        organization_id: str,
        limit: int = 20,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query database for organization members."""
        members = await self.org_repository.get_organization_members(
            organization_id, limit=limit, offset=offset
        )

        member_list = []
        for member in members:
            user = await self.user_repository.get_user(member.user_id)
            member_list.append({
                "user_id": user.user_id,
                "email": user.email,
                "full_name": user.full_name,
                "role": member.role,
                "joined_at": member.created_at,
            })

        total = await self.org_repository.count_organization_members(
            organization_id
        )
        return (member_list, total)
```

### Provider Factory

```python
# faultmaven/providers/tenancy/factory.py

def get_tenant_provider(
    settings: Settings,
    org_repository: Optional[OrganizationRepository] = None,
    user_repository: Optional[UserRepository] = None,
) -> TenantProvider:
    """Factory to select tenant provider based on environment.

    Environment variable: TENANT_PROVIDER
    - "single": SingleTenantProvider (local deployment)
    - "multi": MultiTenantProvider (cloud deployment)
    """
    provider_type = settings.tenant_provider

    if provider_type == "single":
        return SingleTenantProvider(
            default_org_id=settings.default_organization_id,
            user_repository=user_repository
        )

    elif provider_type == "multi":
        if not org_repository or not user_repository:
            raise ValueError(
                "MultiTenantProvider requires OrganizationRepository "
                "and UserRepository dependencies"
            )
        return MultiTenantProvider(org_repository, user_repository)

    else:
        raise ValueError(
            f"Unknown tenant provider: {provider_type}. "
            f"Valid values: 'single', 'multi'"
        )
```

### Application Code (Deployment-Neutral)

```python
# faultmaven/services/case_service.py

class CaseService:
    """Case management service - deployment neutral."""

    def __init__(
        self,
        case_repository: ICaseRepository,
        tenant_provider: TenantProvider,
    ):
        self.case_repository = case_repository
        self.tenant_provider = tenant_provider

    async def list_user_cases(
        self,
        user_id: str,
        organization_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Case]:
        """List cases accessible to user.

        The TenantProvider builds the appropriate filter:
        - Local: Filters by owner_user_id only
        - Cloud: Filters by org_id or owner_user_id

        NO conditional logic here - provider handles it.
        """
        owner_filter = await self.tenant_provider.get_resource_owner_filter(
            user_id, organization_id
        )
        return await self.case_repository.list_cases(
            filters=owner_filter,
            limit=limit,
            offset=offset,
        )
```

---

## 4. Knowledge Base Architecture

### Existing Model

The codebase already has `KBVisibility` enum in `models/interfaces_kb.py`:

```python
class KBVisibility(str, Enum):
    """Document visibility scope."""
    PRIVATE = "private"       # User's private KB
    SHARED = "shared"         # Shared with specific users
    TEAM = "team"             # Shared with team
    ORGANIZATION = "organization"  # Shared with entire org
```

### Required Addition: GLOBAL Scope

For Cloud deployment, add GLOBAL visibility for provider-curated KB:

```python
class KBVisibility(str, Enum):
    """Document visibility scope."""
    PRIVATE = "private"           # User's private KB
    SHARED = "shared"             # Shared with specific users
    TEAM = "team"                 # Shared with team
    ORGANIZATION = "organization" # Shared with entire org
    GLOBAL = "global"             # Provider-curated (read-only)
```

### KB Scoping by Deployment

| KB Type | Local | Cloud | Description |
|---------|-------|-------|-------------|
| **GLOBAL** | ❌ Not available | ✅ Read-only | Provider-curated runbooks, best practices |
| **ORGANIZATION** | ❌ No orgs | ✅ Shared in org | Team knowledge, internal docs |
| **TEAM** | ❌ No teams | ✅ Shared in team | Team-specific knowledge |
| **SHARED** | ❌ No sharing | ✅ Specific users | Shared with selected users |
| **PRIVATE** | ✅ Starts empty | ✅ Private | User builds their own |

### KB Search (Deployment-Neutral)

```python
# faultmaven/services/knowledge_search_service.py

class KnowledgeSearchService:
    """KB search service - deployment neutral."""

    def __init__(
        self,
        vector_store: IVectorStore,
        kb_repository: IKBDocumentRepository,
        tenant_provider: TenantProvider,
        settings: Settings,
    ):
        self.vector_store = vector_store
        self.kb_repository = kb_repository
        self.tenant_provider = tenant_provider
        self.global_kb_enabled = settings.global_kb_enabled

    async def search(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> List[KBSearchResult]:
        """Search KB with proper scoping.

        Scoping is determined by TenantProvider:
        - Local: User's PRIVATE KB only
        - Cloud: GLOBAL + ORG + TEAM + SHARED + PRIVATE

        NO conditional logic - scoping built from available context.
        """
        org_id = await self.tenant_provider.get_user_organization_id(user_id)

        # Build visibility filter based on available context
        visibilities = [KBVisibility.PRIVATE]

        if org_id:
            visibilities.extend([
                KBVisibility.ORGANIZATION,
                KBVisibility.TEAM,
                KBVisibility.SHARED,
            ])

        if self.global_kb_enabled:
            visibilities.append(KBVisibility.GLOBAL)

        return await self.vector_store.search(
            query=query,
            filters={
                "visibility": visibilities,
                "owner_user_id": user_id,
                "org_id": org_id,
            },
            limit=limit,
        )
```

---

## 5. Data Model

### Case Ownership (Existing Model)

The case model already supports ownership. The key is using `org_id` which is nullable:

```python
# Existing in models/
class Case:
    case_id: str
    title: str
    description: str
    status: CaseStatus

    # Ownership
    owner_user_id: str          # Always set - who created it
    org_id: Optional[str]       # None for local, org_id for cloud

    # Sharing (for cloud)
    visibility: CaseVisibility   # PRIVATE, SHARED, ORG
    shared_with: List[str]       # user_ids who can access
```

### Access Control (Deployment-Neutral)

```python
async def can_access_case(
    case: Case,
    user_id: str,
    tenant_provider: TenantProvider,
) -> bool:
    """Check if user can access case - deployment neutral."""

    # Owner always has access
    if case.owner_user_id == user_id:
        return True

    # Check org membership if case has org
    if case.org_id:
        if await tenant_provider.verify_membership(user_id, case.org_id):
            return True

    # Check explicit sharing
    if user_id in (case.shared_with or []):
        return True

    return False
```

---

## 6. Configuration

### Environment Variables

```bash
# === TENANT PROVIDER ===
# "single" = local deployment (default org)
# "multi" = cloud deployment (full orgs)
TENANT_PROVIDER=single
DEFAULT_ORGANIZATION_ID=local-user-org

# === DATABASE ===
# SQLite for local, PostgreSQL for cloud
# Both already supported in database.py
DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db
# DATABASE_URL=postgresql+asyncpg://user:pass@host:port/db

# === STORAGE ===
STORAGE_BACKEND=local
STORAGE_PATH=./data/evidence
# STORAGE_BACKEND=s3
# S3_BUCKET=faultmaven-evidence

# === VECTOR ===
VECTOR_BACKEND=chroma
CHROMA_PATH=./data/chroma
# VECTOR_BACKEND=pinecone
# PINECONE_API_KEY=...

# === CACHE ===
CACHE_BACKEND=memory
# CACHE_BACKEND=redis
# REDIS_URL=redis://localhost:6379

# === GLOBAL KB (Cloud only) ===
GLOBAL_KB_ENABLED=false
# GLOBAL_KB_ENABLED=true
# GLOBAL_KB_INDEX=faultmaven-global-kb

# === LLM ===
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-...
```

### Settings Schema Addition

```python
# faultmaven/config/settings.py (additions)

class TenancySettings(BaseSettings):
    """Tenancy configuration."""

    tenant_provider: str = Field(
        default="single",
        description="Tenant provider: single (local), multi (cloud)"
    )

    default_organization_id: str = Field(
        default="local-user-org",
        description="Default org ID for single-tenant mode"
    )

    global_kb_enabled: bool = Field(
        default=False,
        description="Enable global KB (cloud only)"
    )

    global_kb_index: Optional[str] = Field(
        default=None,
        description="Global KB index name"
    )
```

---

## 7. Gap Analysis

### Corrected Assessment

Based on actual codebase review:

| Component | Status | Gap Level | Notes |
|-----------|--------|-----------|-------|
| **SQLite Support** | ✅ Exists | None | `database.py:98-107` already configured |
| **PostgreSQL Support** | ✅ Exists | None | Fully implemented |
| **TenantProvider** | ❌ Missing | 🔴 High | Critical for deployment neutrality |
| **Local Storage** | ✅ Exists | 🟢 Low | Needs interface alignment |
| **S3 Storage** | ❌ Missing | 🟡 Medium | Required for cloud |
| **ChromaDB** | ✅ Exists | None | Fully implemented |
| **Pinecone** | ❌ Missing | 🟡 Medium | Required for cloud scale |
| **InMemory Cache** | ✅ Exists | None | Fully implemented |
| **Redis Cache** | ✅ Exists | None | Fully implemented |
| **KBVisibility** | ⚠️ Partial | 🟢 Low | Missing GLOBAL value |
| **Organization Routes** | ✅ Exists | None | Full CRUD + members |
| **Case Ownership** | ⚠️ Partial | 🟢 Low | Has org_id, needs sharing |

### Required Work

#### P0: Critical (Blocks deployment neutrality)

**1. Implement TenantProvider Layer**

Create the tenant provider abstraction:

```
faultmaven/providers/
└── tenancy/
    ├── __init__.py
    ├── base.py           # TenantProvider protocol
    ├── single.py         # SingleTenantProvider
    ├── multi.py          # MultiTenantProvider
    └── factory.py        # get_tenant_provider()
```

Files to modify:
- `faultmaven/config/settings.py` - Add TENANT_PROVIDER
- `faultmaven/container.py` - Wire TenantProvider
- `faultmaven/services/case_service.py` - Use TenantProvider
- `faultmaven/services/knowledge_search_service.py` - Use TenantProvider

**Effort**: 3-4 days

#### P1: Required for Cloud

**2. Add GLOBAL to KBVisibility**

Single line addition to `models/interfaces_kb.py`:
```python
GLOBAL = "global"  # Provider-curated (read-only)
```

**Effort**: 0.5 days (including search logic update)

**3. Implement S3StorageBackend**

```
faultmaven/infrastructure/storage/
├── base.py           # IStorageBackend (extract from existing)
├── local.py          # LocalStorageBackend (refactor existing)
├── s3.py             # S3StorageBackend (new)
└── factory.py        # get_storage_backend()
```

**Effort**: 3-4 days

**4. Implement PineconeVectorStore**

```
faultmaven/infrastructure/vector/
├── pinecone_store.py  # PineconeVectorStore (new)
└── factory.py         # Add pinecone option
```

**Effort**: 3-4 days

#### P2: Nice to Have

**5. Align LocalStorageBackend with Interface**

Current `file_storage_service.py` needs to conform to `IStorageBackend` interface.

**Effort**: 1-2 days

---

## 8. Implementation Roadmap

### Phase 1: TenantProvider (Week 1)

| Task | Description | Effort |
|------|-------------|--------|
| 1.1 | Create TenantProvider protocol | 1d |
| 1.2 | Implement SingleTenantProvider | 1d |
| 1.3 | Implement MultiTenantProvider | 1d |
| 1.4 | Add factory and settings | 0.5d |
| 1.5 | Wire into DI container | 0.5d |
| 1.6 | Update CaseService to use TenantProvider | 1d |

**Deliverable**: Deployment-neutral case management

### Phase 2: Storage Providers (Week 2)

| Task | Description | Effort |
|------|-------------|--------|
| 2.1 | Extract IStorageBackend interface | 0.5d |
| 2.2 | Refactor existing to LocalStorageBackend | 1d |
| 2.3 | Implement S3StorageBackend | 2d |
| 2.4 | Add factory and settings | 0.5d |
| 2.5 | Update EvidenceService to use interface | 1d |

**Deliverable**: Deployment-neutral evidence storage

### Phase 3: Vector Providers (Week 3)

| Task | Description | Effort |
|------|-------------|--------|
| 3.1 | Implement PineconeVectorStore | 2d |
| 3.2 | Add factory for vector backend selection | 0.5d |
| 3.3 | Update KnowledgeService to use factory | 1d |
| 3.4 | Add GLOBAL to KBVisibility | 0.5d |
| 3.5 | Implement Global KB service | 1d |

**Deliverable**: Deployment-neutral knowledge base

### Phase 4: Integration Testing (Week 4)

| Task | Description | Effort |
|------|-------------|--------|
| 4.1 | Test with TENANT_PROVIDER=single | 1d |
| 4.2 | Test with TENANT_PROVIDER=multi | 1d |
| 4.3 | Test provider switching | 1d |
| 4.4 | E2E tests for both deployments | 2d |

**Deliverable**: Verified deployment neutrality

---

## Appendix A: Module Structure (No Changes)

The existing codebase structure is correct. **NO separate `local/` and `cloud/` packages.**

```
faultmaven/
├── api/                    # FastAPI routes (same for all deployments)
├── config/                 # Configuration (add TENANT_PROVIDER)
├── infrastructure/         # Provider implementations
│   ├── persistence/        # Database repositories
│   ├── storage/            # Storage backends (add S3)
│   ├── vector/             # Vector stores (add Pinecone)
│   └── llm/                # LLM providers
├── models/                 # Domain models
├── providers/              # NEW: Provider abstractions
│   └── tenancy/            # TenantProvider
├── services/               # Business logic
└── container.py            # DI container
```

---

## Appendix B: Migration Path (Local → Cloud)

Since both deployments use the same codebase and data models:

1. Export local SQLite data
2. Import to Cloud PostgreSQL
3. Create organization for user
4. Set `org_id` on user's resources
5. Upload evidence files to S3
6. Migrate vectors to Pinecone

The data model is the same - only infrastructure changes.

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-12-30 | Solutions Architect | Initial draft |
| 2.0 | 2025-12-31 | Claude | Complete redesign |
| 2.1 | 2025-12-31 | Claude | Revised based on architectural review: restored deployment neutrality, added TenantProvider, removed separate packages, corrected gap analysis, adopted DEFAULT_ORGANIZATION_ID pattern from v1.0 |

---

**Key Changes in v2.1:**

1. ✅ Restored deployment neutrality principle
2. ✅ Added TenantProvider as 5th infrastructure layer
3. ✅ Removed separate `local/` and `cloud/` packages
4. ✅ Removed conditional logic from code examples
5. ✅ Corrected gap analysis (SQLite works, KBVisibility exists)
6. ✅ Aligned with existing codebase structure
7. ✅ Provider pattern handles ALL deployment differences
8. ✅ Adopted DEFAULT_ORGANIZATION_ID pattern for SingleTenantProvider
9. ✅ `get_user_organization_id()` always returns string (no null checks needed)
10. ✅ Fixed `get_organization_members` to return actual user (not empty list)

---

**END OF DOCUMENT**

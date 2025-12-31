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
9. [Request Flow Diagrams](#9-request-flow-diagrams)

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

### Module Boundaries (Internal Communication)

To prevent tight coupling and enable future module extraction, each module exposes a **public API** that other modules must use. Direct imports of internal services are prohibited.

```
faultmaven/
├── identity/                    # Identity & Auth module
│   ├── api.py                   # PUBLIC: Only import from here
│   ├── services/                # INTERNAL: Do not import directly
│   └── repositories/            # INTERNAL: Do not import directly
├── cases/                       # Case Management module
│   ├── api.py                   # PUBLIC: Only import from here
│   ├── services/
│   └── repositories/
├── knowledge/                   # Knowledge Base module
│   ├── api.py                   # PUBLIC: Only import from here
│   ├── services/
│   └── repositories/
└── investigation/               # Investigation module
    ├── api.py                   # PUBLIC: Only import from here
    ├── services/
    └── repositories/
```

**Module API Pattern:**

```python
# faultmaven/identity/api.py
"""
PUBLIC API for the Identity module.
Other modules MUST only import from this file.
"""

from faultmaven.identity.services.user_service import UserService
from faultmaven.identity.services.auth_service import AuthService

# Type-only exports for dependency injection
from faultmaven.identity.protocols import IUserLookup, IAuthValidator

__all__ = [
    "UserService",
    "AuthService",
    "IUserLookup",
    "IAuthValidator",
]
```

**Correct Usage (Other Modules):**

```python
# faultmaven/cases/services/case_service.py

# ✅ CORRECT: Import from public API
from faultmaven.identity.api import IUserLookup

class CaseService:
    def __init__(self, user_lookup: IUserLookup):
        self.user_lookup = user_lookup
```

**Incorrect Usage (Avoid):**

```python
# ❌ WRONG: Direct import from internal service
from faultmaven.identity.services.user_service import UserService
```

**Why This Matters:**

| Without Module Boundaries | With Module Boundaries |
|--------------------------|------------------------|
| `CaseService` imports 10 internal identity classes | `CaseService` imports 1 interface |
| Changing `UserService` breaks `CaseService` | Changes are encapsulated |
| Cannot extract module to microservice | Clean extraction possible |

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

### Layer 2: Files (Extended for Cloud Scale)

The `IStorageBackend` interface must support **presigned URLs** for cloud deployments. Without this, all file uploads/downloads route through the API server, creating a bottleneck.

```python
# faultmaven/infrastructure/storage/base.py

from typing import Protocol, Optional
from datetime import timedelta

class IStorageBackend(Protocol):
    """Storage abstraction supporting both local and cloud backends."""

    async def store(
        self,
        file_path: str,
        content: bytes,
        content_type: str = "application/octet-stream"
    ) -> str:
        """Store file and return storage URI."""
        ...

    async def retrieve(self, storage_uri: str) -> bytes:
        """Retrieve file contents by URI."""
        ...

    async def delete(self, storage_uri: str) -> bool:
        """Delete file by URI."""
        ...

    async def generate_upload_url(
        self,
        file_path: str,
        content_type: str,
        expires_in: timedelta = timedelta(minutes=15)
    ) -> str:
        """Generate URL for direct upload (bypasses API server).

        Local: Returns API endpoint path (e.g., /api/v1/upload/{path})
        Cloud: Returns S3 presigned PUT URL
        """
        ...

    async def generate_download_url(
        self,
        storage_uri: str,
        expires_in: timedelta = timedelta(hours=1)
    ) -> str:
        """Generate URL for direct download.

        Local: Returns static file path (e.g., /static/evidence/{id})
        Cloud: Returns S3 presigned GET URL
        """
        ...
```

**LocalStorageBackend Implementation:**

```python
# faultmaven/infrastructure/storage/local.py

class LocalStorageBackend:
    """Local filesystem storage for self-hosted deployment."""

    def __init__(self, base_path: Path, base_url: str = "/static/evidence"):
        self.base_path = base_path
        self.base_url = base_url

    async def generate_upload_url(
        self,
        file_path: str,
        content_type: str,
        expires_in: timedelta = timedelta(minutes=15)
    ) -> str:
        """Return API upload endpoint - no presigning needed locally."""
        return f"/api/v1/evidence/upload/{file_path}"

    async def generate_download_url(
        self,
        storage_uri: str,
        expires_in: timedelta = timedelta(hours=1)
    ) -> str:
        """Return static file path - served by FastAPI/nginx."""
        return f"{self.base_url}/{storage_uri}"
```

**S3StorageBackend Implementation:**

```python
# faultmaven/infrastructure/storage/s3.py

class S3StorageBackend:
    """S3 storage for cloud SaaS deployment."""

    def __init__(self, bucket: str, region: str):
        self.bucket = bucket
        self.s3_client = boto3.client("s3", region_name=region)

    async def generate_upload_url(
        self,
        file_path: str,
        content_type: str,
        expires_in: timedelta = timedelta(minutes=15)
    ) -> str:
        """Generate S3 presigned PUT URL for direct browser upload."""
        return self.s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": file_path,
                "ContentType": content_type,
            },
            ExpiresIn=int(expires_in.total_seconds()),
        )

    async def generate_download_url(
        self,
        storage_uri: str,
        expires_in: timedelta = timedelta(hours=1)
    ) -> str:
        """Generate S3 presigned GET URL for secure download."""
        return self.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_uri},
            ExpiresIn=int(expires_in.total_seconds()),
        )
```

**Why Presigned URLs Matter:**

| Without Presigned URLs | With Presigned URLs |
|----------------------|---------------------|
| 100MB file → API server → S3 | 100MB file → S3 directly |
| API server becomes bottleneck | API server only generates URL |
| High bandwidth costs | Minimal API traffic |
| Timeout risk for large files | Reliable large file handling |

### Layer 3: Vector (Metadata Sanitization)

ChromaDB and Pinecone handle metadata differently. To ensure portability, all metadata must be sanitized to the **lowest common denominator**.

```python
# faultmaven/infrastructure/vector/base.py

from typing import Protocol, Dict, Any, List

class IMetadataSanitizer(Protocol):
    """Ensures vector metadata is portable across backends."""

    def sanitize(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize metadata for vector store compatibility.

        Rules (Pinecone constraints):
        - No None values (use empty string or 0)
        - Only str, int, float, bool types
        - No nested objects (flatten to dot notation)
        - String values max 512 chars
        """
        ...

class VectorMetadataSanitizer:
    """Enforces strict metadata format for cross-backend compatibility."""

    MAX_STRING_LENGTH = 512

    def sanitize(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for key, value in metadata.items():
            sanitized = self._sanitize_value(key, value)
            if sanitized is not None:
                result.update(sanitized)
        return result

    def _sanitize_value(
        self, key: str, value: Any
    ) -> Optional[Dict[str, Any]]:
        # Handle None - convert to empty string
        if value is None:
            return {key: ""}

        # Handle basic types
        if isinstance(value, bool):
            return {key: value}
        if isinstance(value, (int, float)):
            return {key: value}
        if isinstance(value, str):
            return {key: value[:self.MAX_STRING_LENGTH]}

        # Handle nested dict - flatten with dot notation
        if isinstance(value, dict):
            result = {}
            for nested_key, nested_value in value.items():
                flat_key = f"{key}.{nested_key}"
                nested_result = self._sanitize_value(flat_key, nested_value)
                if nested_result:
                    result.update(nested_result)
            return result

        # Handle list - convert to comma-separated string
        if isinstance(value, list):
            str_value = ",".join(str(v) for v in value)
            return {key: str_value[:self.MAX_STRING_LENGTH]}

        # Unknown type - convert to string
        return {key: str(value)[:self.MAX_STRING_LENGTH]}
```

**Usage in VectorStore:**

```python
class ChromaVectorStore:
    def __init__(self, sanitizer: IMetadataSanitizer):
        self.sanitizer = sanitizer

    async def upsert(
        self, doc_id: str, embedding: List[float], metadata: Dict[str, Any]
    ):
        # ALWAYS sanitize before storing
        clean_metadata = self.sanitizer.sanitize(metadata)
        self.collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            metadatas=[clean_metadata]
        )
```

**Why Metadata Sanitization Matters:**

| Without Sanitizer | With Sanitizer |
|------------------|----------------|
| `{"tags": ["a", "b"]}` → Chroma OK, Pinecone fails | `{"tags": "a,b"}` → Both OK |
| `{"user": None}` → Pinecone crashes | `{"user": ""}` → Both OK |
| `{"meta": {"nested": 1}}` → Inconsistent | `{"meta.nested": 1}` → Both OK |

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

    async def get_resource_owner_filter(
        self,
        user_id: str,
        organization_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build filter dict for querying user-accessible resources.

        SingleTenant: Returns {"owner_user_id": user_id}
        MultiTenant: Returns {"org_id": org_id} or complex filter

        This abstracts the ownership model differences between deployments.
        """
        ...

    def get_kb_visibility_scopes(
        self,
        user_id: str,
        org_id: str,
        global_kb_enabled: bool = False
    ) -> List[str]:
        """Get list of KB visibility scopes user can access.

        SingleTenant: Returns [PRIVATE] only
        MultiTenant: Returns [PRIVATE, SHARED, TEAM, ORGANIZATION, GLOBAL*]

        *GLOBAL only if global_kb_enabled=True
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

    async def get_resource_owner_filter(
        self,
        user_id: str,
        organization_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Filter by owner only - no org-level sharing in local."""
        return {"owner_user_id": user_id}

    def get_kb_visibility_scopes(
        self,
        user_id: str,
        org_id: str,
        global_kb_enabled: bool = False
    ) -> List[str]:
        """Local deployment: only private KB access."""
        return [KBVisibility.PRIVATE]
```

### Startup Bootstrapper (Critical for Referential Integrity)

**Problem:** The `DEFAULT_ORGANIZATION_ID` is returned by `SingleTenantProvider`, but if this organization doesn't actually exist in the database, foreign key constraints will fail when inserting cases, KB documents, or other org-scoped resources.

**Solution:** On application startup in single-tenant mode, ensure the default organization row exists.

```python
# faultmaven/bootstrap/single_tenant.py

from sqlalchemy.ext.asyncio import AsyncSession
from faultmaven.models.organization import Organization
from faultmaven.config import settings

async def ensure_default_organization(session: AsyncSession) -> None:
    """Bootstrap: Ensure DEFAULT_ORGANIZATION_ID exists in database.

    This MUST run on startup when TENANT_PROVIDER=single.
    Guarantees referential integrity for org_id foreign keys.
    """
    if settings.tenant_provider != "single":
        return

    # Check if default org exists
    result = await session.execute(
        select(Organization).where(
            Organization.organization_id == settings.default_organization_id
        )
    )
    existing = result.scalar_one_or_none()

    if existing is None:
        # Create the default organization
        default_org = Organization(
            organization_id=settings.default_organization_id,
            name="Local Organization",
            slug="local",
            plan_tier="unlimited",
            max_members=1,
            is_system=True,  # Flag to identify bootstrap-created orgs
        )
        session.add(default_org)
        await session.commit()
        logger.info(f"Created default organization: {settings.default_organization_id}")
```

**Integration with Application Startup:**

```python
# faultmaven/main.py

from faultmaven.bootstrap.single_tenant import ensure_default_organization

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - runs on startup/shutdown."""
    async with get_db_session() as session:
        # CRITICAL: Ensure default org exists before any requests
        await ensure_default_organization(session)

    yield  # Application runs here

    # Shutdown logic here

app = FastAPI(lifespan=lifespan)
```

**Why This Matters:**

| Without Bootstrapper | With Bootstrapper |
|---------------------|-------------------|
| `INSERT INTO cases (org_id=...) → FK violation` | `INSERT INTO cases (org_id=...) → Success` |
| Local deployment crashes on first case creation | Local deployment works identically to cloud |
| Migration to cloud requires data fixup | Migration is seamless - org_id already valid |

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

    async def get_resource_owner_filter(
        self,
        user_id: str,
        organization_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build filter for org-scoped or user-scoped resources."""
        if organization_id:
            # Org-level access: user must be member
            if await self.verify_membership(user_id, organization_id):
                return {"org_id": organization_id}
        # Fallback to user-owned resources
        return {"owner_user_id": user_id}

    def get_kb_visibility_scopes(
        self,
        user_id: str,
        org_id: str,
        global_kb_enabled: bool = False
    ) -> List[str]:
        """Cloud deployment: full KB scope access."""
        scopes = [
            KBVisibility.PRIVATE,
            KBVisibility.SHARED,
            KBVisibility.TEAM,
            KBVisibility.ORGANIZATION,
        ]
        if global_kb_enabled:
            scopes.append(KBVisibility.GLOBAL)
        return scopes
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

        NO conditional logic - TenantProvider determines scoping.
        """
        org_id = await self.tenant_provider.get_user_organization_id(user_id)

        # Use TenantProvider helper - handles deployment differences
        visibilities = self.tenant_provider.get_kb_visibility_scopes(
            user_id=user_id,
            org_id=org_id,
            global_kb_enabled=self.global_kb_enabled
        )

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
| **Startup Bootstrapper** | ❌ Missing | 🔴 High | Required for FK integrity in local |
| **Local Storage** | ✅ Exists | 🟡 Medium | Needs presigned URL methods |
| **S3 Storage** | ❌ Missing | 🟡 Medium | Required for cloud |
| **ChromaDB** | ✅ Exists | 🟢 Low | Needs MetadataSanitizer |
| **Pinecone** | ❌ Missing | 🟡 Medium | Required for cloud scale |
| **MetadataSanitizer** | ❌ Missing | 🟡 Medium | Required for vector portability |
| **InMemory Cache** | ✅ Exists | None | Fully implemented |
| **Redis Cache** | ✅ Exists | None | Fully implemented |
| **KBVisibility** | ⚠️ Partial | 🟢 Low | Missing GLOBAL value |
| **Organization Routes** | ✅ Exists | None | Full CRUD + members |
| **Module Boundaries** | ⚠️ Partial | 🟢 Low | Needs public API pattern |
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

**2. Implement Startup Bootstrapper**

Ensure default organization exists in database on startup:

```
faultmaven/bootstrap/
├── __init__.py
└── single_tenant.py    # ensure_default_organization()
```

Files to modify:
- `faultmaven/main.py` - Add lifespan handler calling bootstrapper

**Effort**: 0.5 days

#### P1: Required for Cloud

**3. Add GLOBAL to KBVisibility**

Single line addition to `models/interfaces_kb.py`:
```python
GLOBAL = "global"  # Provider-curated (read-only)
```

**Effort**: 0.5 days (including search logic update)

**4. Implement S3StorageBackend with Presigned URLs**

```
faultmaven/infrastructure/storage/
├── base.py           # IStorageBackend (with presigned URL methods)
├── local.py          # LocalStorageBackend (add generate_upload/download_url)
├── s3.py             # S3StorageBackend (new, with presigned URLs)
└── factory.py        # get_storage_backend()
```

**Effort**: 3-4 days

**5. Implement PineconeVectorStore**

```
faultmaven/infrastructure/vector/
├── pinecone_store.py  # PineconeVectorStore (new)
└── factory.py         # Add pinecone option
```

**Effort**: 3-4 days

**6. Implement MetadataSanitizer**

```
faultmaven/infrastructure/vector/
├── base.py            # Add IMetadataSanitizer protocol
├── sanitizer.py       # VectorMetadataSanitizer implementation
├── chroma_store.py    # Update to use sanitizer
└── pinecone_store.py  # Use sanitizer
```

**Effort**: 1-2 days

#### P2: Nice to Have

**7. Align LocalStorageBackend with Extended Interface**

Current `file_storage_service.py` needs to conform to extended `IStorageBackend` interface with presigned URL methods.

**Effort**: 1-2 days

**8. Establish Module Boundaries**

Create public `api.py` files for each module and enforce import rules.

**Effort**: 2-3 days

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

## 9. Request Flow Diagrams

### Case Creation Flow

```
┌──────────┐     ┌──────────┐     ┌────────────────┐     ┌────────────┐
│  Client  │     │   API    │     │  CaseService   │     │ TenantProv │
└────┬─────┘     └────┬─────┘     └───────┬────────┘     └─────┬──────┘
     │                │                    │                    │
     │ POST /cases    │                    │                    │
     │───────────────>│                    │                    │
     │                │                    │                    │
     │                │ create_case(       │                    │
     │                │   user_id, data)   │                    │
     │                │───────────────────>│                    │
     │                │                    │                    │
     │                │                    │ get_user_org_id()  │
     │                │                    │───────────────────>│
     │                │                    │                    │
     │                │                    │    org_id          │
     │                │                    │<───────────────────│
     │                │                    │                    │
     │                │                    │ ┌────────────────┐ │
     │                │                    │ │ Same code path │ │
     │                │                    │ │ Local: default │ │
     │                │                    │ │ Cloud: real id │ │
     │                │                    │ └────────────────┘ │
     │                │                    │                    │
     │                │    Case created    │                    │
     │                │<───────────────────│                    │
     │                │                    │                    │
     │  201 Created   │                    │                    │
     │<───────────────│                    │                    │
```

### KB Search Flow

```
┌──────────┐     ┌──────────┐     ┌────────────────┐     ┌────────────┐     ┌─────────────┐
│  Client  │     │   API    │     │  KBService     │     │ TenantProv │     │ VectorStore │
└────┬─────┘     └────┬─────┘     └───────┬────────┘     └─────┬──────┘     └──────┬──────┘
     │                │                    │                    │                   │
     │ GET /kb/search │                    │                    │                   │
     │───────────────>│                    │                    │                   │
     │                │                    │                    │                   │
     │                │ search(user, q)    │                    │                   │
     │                │───────────────────>│                    │                   │
     │                │                    │                    │                   │
     │                │                    │ get_user_org_id()  │                   │
     │                │                    │───────────────────>│                   │
     │                │                    │    org_id          │                   │
     │                │                    │<───────────────────│                   │
     │                │                    │                    │                   │
     │                │                    │ get_kb_scopes()    │                   │
     │                │                    │───────────────────>│                   │
     │                │                    │   [PRIVATE] or     │                   │
     │                │                    │   [PRIV,ORG,...]   │                   │
     │                │                    │<───────────────────│                   │
     │                │                    │                    │                   │
     │                │                    │ search(q, scopes)                      │
     │                │                    │───────────────────────────────────────>│
     │                │                    │                    │                   │
     │                │                    │    results                             │
     │                │                    │<───────────────────────────────────────│
     │                │                    │                    │                   │
     │                │    results         │                    │                   │
     │                │<───────────────────│                    │                   │
     │  200 OK        │                    │                    │                   │
     │<───────────────│                    │                    │                   │
```

### Provider Selection at Startup

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Application Startup                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │   Read Environment Vars  │
                        │   TENANT_PROVIDER=?      │
                        └─────────────────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    │                                   │
                    ▼                                   ▼
        ┌───────────────────┐               ┌───────────────────┐
        │ TENANT_PROVIDER   │               │ TENANT_PROVIDER   │
        │    = "single"     │               │    = "multi"      │
        └─────────┬─────────┘               └─────────┬─────────┘
                  │                                   │
                  ▼                                   ▼
        ┌───────────────────┐               ┌───────────────────┐
        │ SingleTenant      │               │ MultiTenant       │
        │ Provider          │               │ Provider          │
        │                   │               │                   │
        │ • DEFAULT_ORG_ID  │               │ • OrgRepository   │
        │ • Returns default │               │ • UserRepository  │
        │   org for all     │               │ • Queries DB      │
        └─────────┬─────────┘               └─────────┬─────────┘
                  │                                   │
                  └─────────────────┬─────────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │   Inject into Services   │
                        │   (CaseService, etc.)    │
                        └─────────────────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │  Same Application Code   │
                        │  Different Behavior      │
                        └─────────────────────────┘
```

### Evidence Upload Flow (Local vs Cloud)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Evidence Upload Request                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │   EvidenceService       │
                        │   store(file, user_id)  │
                        └─────────────────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │   StorageBackend.store()│
                        │   (Interface call)      │
                        └─────────────────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    │                                   │
                    ▼                                   ▼
    ┌───────────────────────────┐       ┌───────────────────────────┐
    │   STORAGE_BACKEND=local   │       │   STORAGE_BACKEND=s3      │
    └───────────────────────────┘       └───────────────────────────┘
                    │                                   │
                    ▼                                   ▼
    ┌───────────────────────────┐       ┌───────────────────────────┐
    │   LocalStorageBackend     │       │   S3StorageBackend        │
    │                           │       │                           │
    │   ./data/evidence/{id}    │       │   s3://bucket/evidence/   │
    │   shutil.copy(file, path) │       │   s3.upload_file(...)     │
    └───────────────────────────┘       └───────────────────────────┘
                    │                                   │
                    └─────────────────┬─────────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │   Return storage_uri     │
                        │   (Same interface)       │
                        └─────────────────────────┘
```

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

Since both deployments use the same codebase and data models, migration is straightforward.

### Step 1: Export Local SQLite Data

```bash
#!/bin/bash
# scripts/migration/export_local.sh

LOCAL_DB="./data/faultmaven.db"
EXPORT_DIR="./migration_export"

mkdir -p $EXPORT_DIR

# Export each table to JSON
sqlite3 $LOCAL_DB <<EOF
.mode json
.output $EXPORT_DIR/users.json
SELECT * FROM users;
.output $EXPORT_DIR/cases.json
SELECT * FROM cases;
.output $EXPORT_DIR/sessions.json
SELECT * FROM investigation_sessions;
.output $EXPORT_DIR/evidence.json
SELECT * FROM evidence_artifacts;
.output $EXPORT_DIR/kb_documents.json
SELECT * FROM kb_documents;
EOF

echo "Exported to $EXPORT_DIR"
```

### Step 2: Create Organization and Update References

```python
# scripts/migration/prepare_for_cloud.py

import json
import uuid
from pathlib import Path

EXPORT_DIR = Path("./migration_export")
NEW_ORG_ID = str(uuid.uuid4())
OLD_ORG_ID = "local-user-org"  # DEFAULT_ORGANIZATION_ID

def migrate_references():
    """Update org_id from default to new cloud organization."""

    # Update cases
    cases = json.loads((EXPORT_DIR / "cases.json").read_text())
    for case in cases:
        if case.get("org_id") == OLD_ORG_ID:
            case["org_id"] = NEW_ORG_ID
    (EXPORT_DIR / "cases_migrated.json").write_text(json.dumps(cases))

    # Update KB documents
    kb_docs = json.loads((EXPORT_DIR / "kb_documents.json").read_text())
    for doc in kb_docs:
        if doc.get("org_id") == OLD_ORG_ID:
            doc["org_id"] = NEW_ORG_ID
        # Upgrade PRIVATE to ORGANIZATION for shared access
        if doc.get("visibility") == "private":
            doc["visibility"] = "organization"
    (EXPORT_DIR / "kb_documents_migrated.json").write_text(json.dumps(kb_docs))

    print(f"Migrated to new org_id: {NEW_ORG_ID}")
    return NEW_ORG_ID

if __name__ == "__main__":
    migrate_references()
```

### Step 3: Import to PostgreSQL

```python
# scripts/migration/import_to_cloud.py

import asyncio
import json
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

EXPORT_DIR = Path("./migration_export")
CLOUD_DB_URL = "postgresql+asyncpg://user:pass@host:5432/faultmaven"

async def import_data():
    engine = create_async_engine(CLOUD_DB_URL)

    async with AsyncSession(engine) as session:
        # Import users
        users = json.loads((EXPORT_DIR / "users.json").read_text())
        for user in users:
            await session.execute(
                "INSERT INTO users (user_id, email, ...) VALUES (:user_id, :email, ...)",
                user
            )

        # Import migrated cases
        cases = json.loads((EXPORT_DIR / "cases_migrated.json").read_text())
        for case in cases:
            await session.execute(
                "INSERT INTO cases (case_id, org_id, ...) VALUES (:case_id, :org_id, ...)",
                case
            )

        await session.commit()

    print("Import complete")

if __name__ == "__main__":
    asyncio.run(import_data())
```

### Step 4: Migrate Evidence Files to S3

```python
# scripts/migration/migrate_evidence_to_s3.py

import boto3
from pathlib import Path

LOCAL_EVIDENCE = Path("./data/evidence")
S3_BUCKET = "faultmaven-evidence"

def migrate_to_s3():
    s3 = boto3.client("s3")

    for file_path in LOCAL_EVIDENCE.rglob("*"):
        if file_path.is_file():
            s3_key = str(file_path.relative_to(LOCAL_EVIDENCE))
            s3.upload_file(str(file_path), S3_BUCKET, s3_key)
            print(f"Uploaded: {s3_key}")

    print("Evidence migration complete")

if __name__ == "__main__":
    migrate_to_s3()
```

### Step 5: Migrate Vectors to Pinecone

```python
# scripts/migration/migrate_vectors_to_pinecone.py

import chromadb
import pinecone
from tqdm import tqdm

CHROMA_PATH = "./data/chroma"
PINECONE_INDEX = "faultmaven-kb"

def migrate_vectors():
    # Connect to local ChromaDB
    chroma = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = chroma.get_collection("kb_documents")

    # Connect to Pinecone
    pinecone.init(api_key="...", environment="...")
    index = pinecone.Index(PINECONE_INDEX)

    # Get all vectors from ChromaDB
    results = collection.get(include=["embeddings", "metadatas", "documents"])

    # Batch upload to Pinecone
    batch_size = 100
    vectors = []

    for i, (id_, embedding, metadata) in enumerate(
        zip(results["ids"], results["embeddings"], results["metadatas"])
    ):
        vectors.append({
            "id": id_,
            "values": embedding,
            "metadata": metadata
        })

        if len(vectors) >= batch_size:
            index.upsert(vectors=vectors)
            vectors = []

    # Upload remaining
    if vectors:
        index.upsert(vectors=vectors)

    print(f"Migrated {len(results['ids'])} vectors")

if __name__ == "__main__":
    migrate_vectors()
```

### Migration Checklist

| Step | Script | Verification |
|------|--------|--------------|
| 1. Export SQLite | `export_local.sh` | Check JSON files exist |
| 2. Update org_id | `prepare_for_cloud.py` | Verify new org_id in files |
| 3. Import PostgreSQL | `import_to_cloud.py` | Query tables for data |
| 4. Upload to S3 | `migrate_evidence_to_s3.py` | List S3 bucket contents |
| 5. Migrate vectors | `migrate_vectors_to_pinecone.py` | Query Pinecone index |
| 6. Create org membership | Cloud admin panel | User can access org |

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

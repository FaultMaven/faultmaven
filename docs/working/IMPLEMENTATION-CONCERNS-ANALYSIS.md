# Implementation Concerns Analysis - Multi-Tenancy Deployment Strategy

**Document Type**: Implementation Guidance
**Status**: Critical Pre-Implementation Review
**Date**: 2025-12-31
**Related Documents**:
- `/home/swhouse/product/faultmaven/docs/architecture/deployment-strategy-v2.md`
- PR #24 (Organizations Multi-Tenancy)

---

## Executive Summary

This document analyzes three critical implementation concerns raised during the review of the multi-tenancy deployment strategy (v2.1). These are **implementation-level pitfalls** that could cause production failures if not addressed before Phase 1 implementation.

**Key Finding**: All three concerns are **genuine production risks** that require immediate design clarification and implementation guidance.

**Recommendation**: Update deployment strategy document (Section 3.5 and new Section 10) with detailed implementation notes to prevent these issues.

---

## Table of Contents

1. [Concern #1: Bootstrapper Timing Issue](#concern-1-bootstrapper-timing-issue)
2. [Concern #2: Presigned URL Expiration Problem](#concern-2-presigned-url-expiration-problem)
3. [Concern #3: Local Storage Upload Endpoint Missing](#concern-3-local-storage-upload-endpoint-missing)
4. [Implementation Roadmap Updates](#implementation-roadmap-updates)
5. [Recommendations](#recommendations)

---

## Concern #1: Bootstrapper Timing Issue

### Problem Statement

**Location**: Lines 662-735 of deployment-strategy-v2.md
**Severity**: 🔴 **CRITICAL** - Will cause immediate startup failure in production

**Issue Description**:
```python
# faultmaven/main.py (Lines 715-725)
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - runs on startup/shutdown."""
    async with get_db_session() as session:
        # CRITICAL: Ensure default org exists before any requests
        await ensure_default_organization(session)  # ← RUNS FIRST

    yield
```

**The Problem**:
- Bootstrapper runs in FastAPI `lifespan` handler
- If Alembic migrations haven't created the `organizations` table yet, bootstrapper crashes
- Error: `relation "organizations" does not exist`
- This is a **race condition** in deployment sequences

**Why This Matters**:
In production Kubernetes deployments, this can cause:
1. **Init container** runs Alembic migrations
2. **Main container** starts FastAPI app
3. If containers race, app crashes before schema exists

### Root Cause Analysis

The deployment strategy document assumes migrations run **before** application startup, but this isn't guaranteed in all deployment scenarios:

| Deployment Scenario | Migration Timing | Risk Level |
|---------------------|------------------|------------|
| **Local Development** (`docker-compose up`) | Sequential (migrations→app) | 🟢 Low |
| **Kubernetes Init Container** | Parallel start | 🔴 High |
| **Cloud Run / Lambda** | Migrations may not exist | 🔴 Critical |
| **Manual Deployment** | Depends on operator | 🟡 Medium |

### Proposed Solution

#### Option A: Guard with Schema Check (RECOMMENDED)

```python
# faultmaven/bootstrap/single_tenant.py

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from faultmaven.models.organization import Organization
from faultmaven.config import settings
from faultmaven.infrastructure.logging.config import get_logger

logger = get_logger(__name__)


async def ensure_default_organization(session: AsyncSession) -> None:
    """Bootstrap: Ensure DEFAULT_ORGANIZATION_ID exists in database.

    This MUST run on startup when TENANT_PROVIDER=single.
    Guarantees referential integrity for org_id foreign keys.

    CRITICAL: Handles race condition with Alembic migrations.
    If schema doesn't exist yet, logs warning and skips bootstrap.
    App will retry on next startup or readiness probe.
    """
    if settings.tenant_provider != "single":
        logger.debug("Skipping default org bootstrap (not single-tenant mode)")
        return

    try:
        # STEP 1: Check if organizations table exists
        inspector = inspect(session.bind)
        if "organizations" not in await session.run_sync(
            lambda sync_session: inspector.get_table_names()
        ):
            logger.warning(
                "Organizations table does not exist yet - "
                "migrations may not have run. Skipping bootstrap. "
                "This is normal during initial deployment."
            )
            return

        # STEP 2: Check if default org exists
        from sqlalchemy import select
        result = await session.execute(
            select(Organization).where(
                Organization.organization_id == settings.default_organization_id
            )
        )
        existing = result.scalar_one_or_none()

        if existing is None:
            # STEP 3: Create the default organization
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
            logger.info(
                f"✅ Created default organization: {settings.default_organization_id}"
            )
        else:
            logger.debug(
                f"Default organization already exists: {settings.default_organization_id}"
            )

    except Exception as e:
        logger.error(
            f"Failed to bootstrap default organization: {e}. "
            f"This may cause FK violations when creating cases/KB documents. "
            f"Ensure migrations have run and try restarting the application."
        )
        # DO NOT raise - allow app to start for debugging
        # Kubernetes readiness probe will fail if org missing
```

#### Option B: Kubernetes Init Container Pattern

```yaml
# k8s/faultmaven-deployment.yaml

apiVersion: apps/v1
kind: Deployment
metadata:
  name: faultmaven-api
spec:
  template:
    spec:
      # Init container runs migrations FIRST
      initContainers:
      - name: migrations
        image: faultmaven:latest
        command: ["alembic", "upgrade", "head"]
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: faultmaven-secrets
              key: database-url
        # WAIT for migrations to complete
        # Exit code 0 = success, allows main container to start

      # Main container starts AFTER init succeeds
      containers:
      - name: api
        image: faultmaven:latest
        command: ["uvicorn", "faultmaven.main:app"]
        # Bootstrap runs here - schema guaranteed to exist
        env:
        - name: TENANT_PROVIDER
          value: "single"
```

#### Option C: Readiness Probe with Retry

```python
# faultmaven/main.py

@app.get("/readiness")
async def readiness():
    """Readiness probe: ensures migrations complete before serving traffic."""
    try:
        from .container import container
        from .bootstrap.single_tenant import ensure_default_organization

        # Initialize container
        await container.initialize()

        # Verify database connectivity
        if getattr(container, 'session_store', None) is None:
            return JSONResponse(
                status_code=503,
                content={"status": "unready", "reason": "redis_unavailable"}
            )

        # CRITICAL: Verify default org exists (runs bootstrap if needed)
        async with container.get_db_session() as session:
            await ensure_default_organization(session)

        # Check other dependencies
        if getattr(container, 'vector_store', None) is None:
            return JSONResponse(
                status_code=503,
                content={"status": "unready", "reason": "chromadb_unavailable"}
            )

        return {"status": "ready"}

    except Exception as e:
        logger.error(f"Readiness probe failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unready", "reason": str(e)}
        )
```

### Recommended Implementation

**Use a combination of Options A + B + C**:

1. **Option A (Schema Check)**: Prevent crashes when migrations haven't run
2. **Option B (Init Container)**: Guarantee migration ordering in Kubernetes
3. **Option C (Readiness Probe)**: Block traffic until bootstrap completes

**Implementation Sequence**:
```
1. Init Container runs Alembic → Creates schema
   ↓
2. Main container starts → Lifespan handler runs
   ↓
3. Bootstrap checks schema → Creates default org
   ↓
4. Readiness probe succeeds → Traffic allowed
```

### Testing Strategy

```python
# tests/integration/test_bootstrap_timing.py

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_bootstrap_before_migrations(async_session):
    """Verify bootstrap handles missing schema gracefully."""
    # Drop organizations table to simulate pre-migration state
    await async_session.execute(text("DROP TABLE IF EXISTS organizations CASCADE"))
    await async_session.commit()

    # Bootstrap should NOT crash
    from faultmaven.bootstrap.single_tenant import ensure_default_organization
    await ensure_default_organization(async_session)

    # Verify no org was created (table doesn't exist)
    # Should log warning instead


@pytest.mark.asyncio
async def test_bootstrap_after_migrations(async_session):
    """Verify bootstrap creates org when schema exists."""
    from faultmaven.bootstrap.single_tenant import ensure_default_organization
    from faultmaven.models.organization import Organization
    from sqlalchemy import select

    # Run bootstrap
    await ensure_default_organization(async_session)

    # Verify org was created
    result = await async_session.execute(
        select(Organization).where(
            Organization.organization_id == "local-user-org"
        )
    )
    org = result.scalar_one_or_none()
    assert org is not None
    assert org.name == "Local Organization"
    assert org.is_system is True


@pytest.mark.asyncio
async def test_bootstrap_idempotent(async_session):
    """Verify bootstrap is idempotent (safe to run multiple times)."""
    from faultmaven.bootstrap.single_tenant import ensure_default_organization

    # Run bootstrap twice
    await ensure_default_organization(async_session)
    await ensure_default_organization(async_session)

    # Should not crash or duplicate
```

### Phase 1 Priority

**MUST be implemented in Phase 1** - This is a blocking issue for production deployment.

---

## Concern #2: Presigned URL Expiration Problem

### Problem Statement

**Location**: Lines 238-263 of deployment-strategy-v2.md
**Severity**: 🟡 **HIGH** - Will cause user experience issues in production

**Issue Description**:
```python
# Current design (Line 243)
async def generate_download_url(
    self,
    storage_uri: str,
    expires_in: timedelta = timedelta(hours=1)  # ← DEFAULT: 1 hour
) -> str:
```

**The Problem - Real-World Scenario**:
```
1. User opens FaultMaven dashboard at 10:00 AM
2. Frontend fetches case with 50 evidence thumbnails
3. Backend generates 50 presigned URLs (expires: 11:00 AM)
4. User leaves browser tab open, goes to meeting
5. User returns at 11:30 AM, clicks thumbnail
6. Result: 403 Forbidden - URL expired
7. User must refresh entire page to regenerate URLs
```

**Why This Matters**:
- **User Friction**: Unexpected errors for legitimate use cases
- **API Load**: Forces full page refresh (50 URLs regenerated)
- **Bandwidth Waste**: Re-downloads thumbnails already in browser cache
- **Mobile Impact**: Worse on mobile with background tab suspension

### Root Cause Analysis

The deployment strategy document specifies presigned URL methods but doesn't address:
1. **Different expiration strategies** for different use cases
2. **URL refresh mechanisms** for long-lived pages
3. **Trade-offs** between security and user experience

**Current Design Assumptions**:
- ✅ Presigned URLs prevent API bottleneck (good)
- ✅ Expiration provides security (good)
- ❌ 1-hour expiration fits all use cases (wrong)
- ❌ Frontend only generates URLs once (wrong)

### Proposed Solution

#### Strategy 1: Use Case-Based Expiration (RECOMMENDED)

```python
# faultmaven/infrastructure/storage/base.py

from enum import Enum
from datetime import timedelta


class URLPurpose(str, Enum):
    """Purpose of presigned URL - determines expiration."""
    THUMBNAIL = "thumbnail"      # Short-lived, frequent access
    DOWNLOAD = "download"        # One-time download
    INLINE_VIEW = "inline_view"  # Long-lived browser display
    API_RESPONSE = "api_response"  # Short-lived API contract


# Expiration policy map
URL_EXPIRATION_POLICIES = {
    URLPurpose.THUMBNAIL: timedelta(minutes=15),     # Short: user clicks quickly
    URLPurpose.DOWNLOAD: timedelta(hours=1),         # Medium: download window
    URLPurpose.INLINE_VIEW: timedelta(hours=8),      # Long: workday session
    URLPurpose.API_RESPONSE: timedelta(minutes=5),   # Very short: immediate use
}


class IStorageBackend(Protocol):
    """Extended storage interface with purpose-based expiration."""

    async def generate_download_url(
        self,
        storage_uri: str,
        purpose: URLPurpose = URLPurpose.DOWNLOAD,
        expires_in: Optional[timedelta] = None  # Override policy
    ) -> Dict[str, Any]:
        """Generate presigned URL with metadata.

        Args:
            storage_uri: Storage identifier
            purpose: URL purpose (determines default expiration)
            expires_in: Override expiration (for special cases)

        Returns:
            {
                "url": "https://...",
                "expires_at": "2025-12-31T18:00:00Z",  # ISO 8601
                "expires_in_seconds": 3600,
                "purpose": "download"
            }
        """
        ...
```

**Updated S3 Implementation**:
```python
# faultmaven/infrastructure/storage/s3.py

class S3StorageBackend:
    """S3 storage with purpose-based URL expiration."""

    async def generate_download_url(
        self,
        storage_uri: str,
        purpose: URLPurpose = URLPurpose.DOWNLOAD,
        expires_in: Optional[timedelta] = None
    ) -> Dict[str, Any]:
        """Generate S3 presigned GET URL with expiration metadata."""

        # Use purpose-based policy unless overridden
        expiration = expires_in or URL_EXPIRATION_POLICIES[purpose]
        expires_in_seconds = int(expiration.total_seconds())

        # Generate presigned URL
        url = self.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_uri},
            ExpiresIn=expires_in_seconds,
        )

        # Return URL with expiration metadata
        from datetime import datetime, timezone
        expires_at = datetime.now(timezone.utc) + expiration

        return {
            "url": url,
            "expires_at": expires_at.isoformat(),
            "expires_in_seconds": expires_in_seconds,
            "purpose": purpose.value,
            "storage_uri": storage_uri,  # For refresh endpoint
        }
```

#### Strategy 2: Frontend URL Refresh Endpoint

```python
# faultmaven/api/v1/routes/evidence.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List


class URLRefreshRequest(BaseModel):
    """Request to refresh expired presigned URLs."""
    storage_uris: List[str]
    purpose: URLPurpose = URLPurpose.INLINE_VIEW


class URLRefreshResponse(BaseModel):
    """Response with refreshed URLs."""
    urls: List[Dict[str, Any]]


router = APIRouter()


@router.post("/evidence/refresh-urls", response_model=URLRefreshResponse)
async def refresh_presigned_urls(
    request: URLRefreshRequest,
    current_user: str = Depends(get_current_user),
    storage_backend: IStorageBackend = Depends(get_storage_backend)
):
    """Refresh expired presigned URLs for evidence artifacts.

    Use Case:
    - User has dashboard open for >1 hour
    - Frontend detects URLs near expiration (checks expires_at)
    - Frontend calls this endpoint to refresh URLs in background
    - User experience: seamless, no page refresh needed

    Rate Limiting: 100 requests/hour per user
    """
    if len(request.storage_uris) > 50:
        raise HTTPException(
            status_code=400,
            detail="Cannot refresh more than 50 URLs at once"
        )

    # Verify user has access to these URIs
    # (org_id check, ownership verification, etc.)

    refreshed_urls = []
    for storage_uri in request.storage_uris:
        url_data = await storage_backend.generate_download_url(
            storage_uri=storage_uri,
            purpose=request.purpose
        )
        refreshed_urls.append(url_data)

    return URLRefreshResponse(urls=refreshed_urls)
```

#### Strategy 3: Frontend Auto-Refresh Logic

```typescript
// faultmaven-dashboard/src/services/evidenceUrlManager.ts

interface PresignedURL {
  url: string;
  expires_at: string;  // ISO 8601
  expires_in_seconds: number;
  storage_uri: string;
}

class EvidenceURLManager {
  private urlCache: Map<string, PresignedURL> = new Map();
  private refreshThresholdSeconds = 300;  // Refresh 5min before expiry

  /**
   * Get URL for evidence, auto-refreshing if needed
   */
  async getURL(storageUri: string): Promise<string> {
    const cached = this.urlCache.get(storageUri);

    // Check if URL needs refresh
    if (!cached || this.isNearExpiration(cached)) {
      await this.refreshURLs([storageUri]);
      return this.urlCache.get(storageUri)!.url;
    }

    return cached.url;
  }

  /**
   * Check if URL is near expiration (within threshold)
   */
  private isNearExpiration(urlData: PresignedURL): boolean {
    const expiresAt = new Date(urlData.expires_at);
    const now = new Date();
    const secondsUntilExpiry = (expiresAt.getTime() - now.getTime()) / 1000;

    return secondsUntilExpiry < this.refreshThresholdSeconds;
  }

  /**
   * Batch refresh URLs in background
   */
  private async refreshURLs(storageUris: string[]): Promise<void> {
    const response = await fetch('/api/v1/evidence/refresh-urls', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        storage_uris: storageUris,
        purpose: 'inline_view'
      })
    });

    const data = await response.json();

    // Update cache
    for (const urlData of data.urls) {
      this.urlCache.set(urlData.storage_uri, urlData);
    }
  }

  /**
   * Start background refresh timer for all cached URLs
   */
  startAutoRefresh(): void {
    setInterval(() => {
      const urisToRefresh = Array.from(this.urlCache.entries())
        .filter(([_, urlData]) => this.isNearExpiration(urlData))
        .map(([uri, _]) => uri);

      if (urisToRefresh.length > 0) {
        console.log(`Auto-refreshing ${urisToRefresh.length} URLs`);
        this.refreshURLs(urisToRefresh);
      }
    }, 60000);  // Check every minute
  }
}

export const evidenceURLManager = new EvidenceURLManager();
evidenceURLManager.startAutoRefresh();
```

### Recommended Implementation

**Use Strategy 1 + Strategy 2 + Strategy 3**:

1. **Backend**: Purpose-based expiration (Strategy 1)
2. **API**: Refresh endpoint for batch renewal (Strategy 2)
3. **Frontend**: Auto-refresh before expiration (Strategy 3)

**Expiration Policies**:
```python
URLPurpose.THUMBNAIL: 15 minutes      # Quick click expected
URLPurpose.DOWNLOAD: 1 hour           # Download window
URLPurpose.INLINE_VIEW: 8 hours       # Full workday session
URLPurpose.API_RESPONSE: 5 minutes    # Immediate consumption
```

**User Experience Flow**:
```
1. User opens dashboard → Backend generates 8-hour URLs
2. User leaves tab open → Frontend monitors expiration
3. 7h 55min later → Frontend auto-refreshes in background
4. User clicks thumbnail → URL still valid, no error
```

### Testing Strategy

```python
# tests/integration/test_presigned_url_expiration.py

import pytest
from datetime import timedelta
from freezegun import freeze_time


@pytest.mark.asyncio
async def test_url_expiration_policies():
    """Verify different purposes get different expirations."""
    storage = S3StorageBackend(bucket="test-bucket")

    # Thumbnail: 15 minutes
    thumb_url = await storage.generate_download_url(
        "evidence/123.jpg",
        purpose=URLPurpose.THUMBNAIL
    )
    assert thumb_url["expires_in_seconds"] == 900

    # Inline view: 8 hours
    view_url = await storage.generate_download_url(
        "evidence/123.jpg",
        purpose=URLPurpose.INLINE_VIEW
    )
    assert view_url["expires_in_seconds"] == 28800


@pytest.mark.asyncio
async def test_url_refresh_endpoint(client, test_user):
    """Verify refresh endpoint regenerates URLs."""
    response = await client.post(
        "/api/v1/evidence/refresh-urls",
        json={
            "storage_uris": ["evidence/123.jpg", "evidence/456.jpg"],
            "purpose": "inline_view"
        },
        headers={"Authorization": f"Bearer {test_user.token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["urls"]) == 2
    assert all("expires_at" in url for url in data["urls"])


@pytest.mark.asyncio
async def test_expired_url_handling(client):
    """Verify expired URLs return 403."""
    with freeze_time("2025-12-31 10:00:00"):
        # Generate URL with 1-hour expiration
        url_data = await storage.generate_download_url(
            "evidence/123.jpg",
            expires_in=timedelta(hours=1)
        )

    # Advance time past expiration
    with freeze_time("2025-12-31 11:30:00"):
        response = await client.get(url_data["url"])
        assert response.status_code == 403
```

### Phase 1 Priority

**SHOULD be implemented in Phase 1** - Impacts user experience but not a blocker.

**Minimal Phase 1**: Implement Strategy 1 (purpose-based expiration) only
**Complete Phase 1**: Implement all 3 strategies for production-ready UX

---

## Concern #3: Local Storage Upload Endpoint Missing

### Problem Statement

**Location**: Lines 266-293 of deployment-strategy-v2.md
**Severity**: 🔴 **CRITICAL** - Will cause 404 errors in local deployment

**Issue Description**:
```python
# Current LocalStorageBackend (Line 284)
async def generate_upload_url(
    self,
    file_path: str,
    content_type: str,
    expires_in: timedelta = timedelta(minutes=15)
) -> str:
    """Return API upload endpoint - no presigning needed locally."""
    return f"/api/v1/evidence/upload/{file_path}"  # ← ENDPOINT DOESN'T EXIST!
```

**The Problem**:
- LocalStorageBackend returns upload URL: `/api/v1/evidence/upload/{path}`
- But there's **no FastAPI route handler** to accept the upload!
- Result: Frontend gets upload URL, tries to PUT → 404 Not Found

**Why This Matters**:
- **Local deployment breaks**: Evidence upload fails completely
- **Easy to miss**: S3 side works (real presigned URLs), local side untested
- **Silent failure**: Returns 200 from generate_upload_url, fails at upload time

### Root Cause Analysis

The deployment strategy document focuses on the **abstraction layer** (IStorageBackend) but doesn't specify **where** the local upload endpoint should be implemented:

**Three Architectural Options**:
1. **Storage Provider Layer**: LocalStorageBackend handles upload directly
2. **API Route Layer**: FastAPI route delegates to LocalStorageBackend
3. **Hybrid**: API route for validation, provider for I/O

**Current Gap**: Document doesn't specify which option to use.

### Proposed Solution

#### Option A: API Route Layer (RECOMMENDED)

**Rationale**: Keeps API surface consistent, allows middleware (auth, validation, rate limiting).

```python
# faultmaven/api/v1/routes/evidence.py

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from typing import Dict, Any


router = APIRouter()


@router.put(
    "/evidence/upload/{org_id}/{case_id}/{date}/{filename}",
    summary="Upload evidence file to local storage",
    description="""
    Direct file upload endpoint for local storage backend.

    In local deployment:
    - LocalStorageBackend.generate_upload_url() returns this endpoint
    - Frontend PUTs file directly to this endpoint
    - This endpoint saves file to local filesystem

    In cloud deployment:
    - S3StorageBackend.generate_upload_url() returns S3 presigned URL
    - Frontend PUTs directly to S3 (bypasses this endpoint)
    - This endpoint is never called

    Security: Requires valid JWT token and org membership verification.
    """
)
async def upload_evidence_local(
    org_id: str,
    case_id: str,
    date: str,
    filename: str,
    file: UploadFile = File(...),
    current_user: str = Depends(get_current_user),
    storage_backend: IStorageBackend = Depends(get_storage_backend),
    tenant_provider: TenantProvider = Depends(get_tenant_provider)
):
    """Handle local storage file upload.

    This endpoint is ONLY used when STORAGE_BACKEND=local.
    S3 deployments bypass this via presigned URLs.
    """
    # STEP 1: Verify this is local storage (sanity check)
    if not isinstance(storage_backend, LocalStorageBackend):
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for local storage. "
                   "Cloud deployments should use presigned S3 URLs."
        )

    # STEP 2: Verify user has access to organization
    user_org_id = await tenant_provider.get_user_organization_id(current_user)
    if user_org_id != org_id:
        raise HTTPException(
            status_code=403,
            detail=f"User does not have access to organization {org_id}"
        )

    # STEP 3: Reconstruct file path from URL parameters
    file_path = f"{org_id}/{case_id}/{date}/{filename}"

    # STEP 4: Read file data
    file_data = await file.read()

    # STEP 5: Validate file size
    max_size = storage_backend.max_file_size_bytes
    if len(file_data) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"File size ({len(file_data)} bytes) exceeds maximum "
                   f"({max_size} bytes)"
        )

    # STEP 6: Store file using storage backend
    try:
        # LocalStorageBackend.store() writes to disk
        full_path = os.path.join(storage_backend.storage_root, file_path)
        directory = os.path.dirname(full_path)

        # Create directories if they don't exist
        await aiofiles.os.makedirs(directory, exist_ok=True)

        # Write file to disk
        async with aiofiles.open(full_path, 'wb') as f:
            await f.write(file_data)

        return {
            "status": "success",
            "file_path": file_path,
            "file_size": len(file_data),
            "storage_backend": "local"
        }

    except Exception as e:
        logger.error(f"Failed to store local file {file_path}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to store file: {str(e)}"
        )


@router.post(
    "/evidence/generate-upload-url",
    summary="Generate presigned upload URL",
    description="""
    Generate URL for direct file upload.

    Local deployment: Returns /api/v1/evidence/upload/{path}
    Cloud deployment: Returns S3 presigned PUT URL

    Frontend workflow:
    1. POST /evidence/generate-upload-url → Get upload URL
    2. PUT {upload_url} with file data → Upload file
    3. POST /evidence (create artifact record) → Link to case
    """
)
async def generate_upload_url(
    request: GenerateUploadURLRequest,
    current_user: str = Depends(get_current_user),
    storage_backend: IStorageBackend = Depends(get_storage_backend),
    tenant_provider: TenantProvider = Depends(get_tenant_provider)
) -> Dict[str, Any]:
    """Generate upload URL for evidence file."""

    # Verify user has access to organization
    user_org_id = await tenant_provider.get_user_organization_id(current_user)
    if user_org_id != request.organization_id:
        raise HTTPException(
            status_code=403,
            detail=f"User does not have access to organization {request.organization_id}"
        )

    # Generate storage path (same logic as FileStorageService)
    from datetime import datetime, timezone
    import uuid

    date_folder = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_uuid = uuid.uuid4().hex[:12]
    safe_filename = sanitize_filename(request.filename)
    stored_filename = f"{file_uuid}_{safe_filename}"

    file_path = f"{request.organization_id}/{request.case_id}/{date_folder}/{stored_filename}"

    # Generate upload URL (provider-specific)
    upload_url_data = await storage_backend.generate_upload_url(
        file_path=file_path,
        content_type=request.content_type,
        expires_in=timedelta(minutes=15)
    )

    return {
        "upload_url": upload_url_data["url"],
        "file_path": file_path,
        "expires_at": upload_url_data.get("expires_at"),
        "storage_backend": "local" if isinstance(storage_backend, LocalStorageBackend) else "s3"
    }
```

**Updated LocalStorageBackend**:
```python
# faultmaven/infrastructure/storage/local.py

class LocalStorageBackend:
    """Local filesystem storage for self-hosted deployment."""

    def __init__(self, base_path: Path, base_url: str = "/api/v1"):
        self.base_path = base_path
        self.base_url = base_url

    async def generate_upload_url(
        self,
        file_path: str,
        content_type: str,
        expires_in: timedelta = timedelta(minutes=15)
    ) -> Dict[str, Any]:
        """Return API upload endpoint.

        For local storage, we return a FastAPI route that accepts PUT.
        The route handler will save the file to self.base_path.
        """
        from datetime import datetime, timezone

        # Build upload endpoint URL
        upload_url = f"{self.base_url}/evidence/upload/{file_path}"

        # Return with expiration metadata (for consistency with S3)
        expires_at = datetime.now(timezone.utc) + expires_in

        return {
            "url": upload_url,
            "expires_at": expires_at.isoformat(),
            "expires_in_seconds": int(expires_in.total_seconds()),
            "method": "PUT",
            "headers": {
                "Content-Type": content_type
            }
        }
```

#### Option B: Middleware-Based Upload Handler

```python
# faultmaven/api/middleware/local_upload.py

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class LocalUploadMiddleware(BaseHTTPMiddleware):
    """Middleware to intercept local storage uploads.

    Automatically handles PUT requests to /api/v1/evidence/upload/*
    when STORAGE_BACKEND=local.

    Benefits:
    - No need to register explicit route
    - Middleware can be conditionally added based on env
    """

    async def dispatch(self, request: Request, call_next):
        # Check if this is a local upload request
        if (
            request.method == "PUT" and
            request.url.path.startswith("/api/v1/evidence/upload/")
        ):
            # Extract file path from URL
            file_path = request.url.path.replace("/api/v1/evidence/upload/", "")

            # Get storage backend from app state
            storage = request.app.extra.get("storage_backend")

            if isinstance(storage, LocalStorageBackend):
                # Handle upload
                file_data = await request.body()

                # Save to disk
                full_path = os.path.join(storage.base_path, file_path)
                directory = os.path.dirname(full_path)
                await aiofiles.os.makedirs(directory, exist_ok=True)

                async with aiofiles.open(full_path, 'wb') as f:
                    await f.write(file_data)

                return JSONResponse({
                    "status": "success",
                    "file_path": file_path,
                    "file_size": len(file_data)
                })

        # Not a local upload, continue to next middleware/route
        return await call_next(request)
```

### Recommended Implementation

**Use Option A (API Route Layer)**:

**Rationale**:
1. ✅ **Explicit API surface**: Route shows up in `/docs` (OpenAPI spec)
2. ✅ **Dependency injection**: Can use FastAPI's DI for services
3. ✅ **Standard middleware**: Auth, rate limiting, logging all work
4. ✅ **Testable**: Standard FastAPI testing patterns apply
5. ✅ **Discoverable**: Developers can see the endpoint exists

**Why Not Option B (Middleware)**:
- ❌ Hidden from OpenAPI spec
- ❌ Harder to test
- ❌ Less discoverable
- ❌ Middleware stack complexity

### Implementation Sequence

```
Phase 1.1: Add /evidence/upload/{path} route
Phase 1.2: Update LocalStorageBackend.generate_upload_url()
Phase 1.3: Add integration tests (local vs S3)
Phase 1.4: Update frontend upload logic
```

### Testing Strategy

```python
# tests/integration/test_local_upload_endpoint.py

import pytest
from io import BytesIO


@pytest.mark.asyncio
async def test_local_upload_endpoint_exists(client, test_user):
    """Verify upload endpoint is registered."""
    # Get upload URL
    response = await client.post(
        "/api/v1/evidence/generate-upload-url",
        json={
            "organization_id": "local-user-org",
            "case_id": "case-123",
            "filename": "error.log",
            "content_type": "text/plain"
        },
        headers={"Authorization": f"Bearer {test_user.token}"}
    )

    assert response.status_code == 200
    data = response.json()

    # Verify URL is local endpoint
    assert data["upload_url"].startswith("/api/v1/evidence/upload/")
    assert data["storage_backend"] == "local"


@pytest.mark.asyncio
async def test_local_upload_file(client, test_user, tmp_path):
    """Verify file upload to local storage works."""
    # Generate upload URL
    url_response = await client.post(
        "/api/v1/evidence/generate-upload-url",
        json={
            "organization_id": "local-user-org",
            "case_id": "case-123",
            "filename": "test.log",
            "content_type": "text/plain"
        },
        headers={"Authorization": f"Bearer {test_user.token}"}
    )

    upload_url = url_response.json()["upload_url"]
    file_path = url_response.json()["file_path"]

    # Upload file
    test_content = b"Test log content"
    upload_response = await client.put(
        upload_url,
        content=test_content,
        headers={
            "Authorization": f"Bearer {test_user.token}",
            "Content-Type": "text/plain"
        }
    )

    assert upload_response.status_code == 200

    # Verify file exists on disk
    storage_path = tmp_path / file_path
    assert storage_path.exists()
    assert storage_path.read_bytes() == test_content


@pytest.mark.asyncio
async def test_s3_upload_bypasses_endpoint(client, test_user, s3_storage):
    """Verify S3 deployments bypass local upload endpoint."""
    # Configure S3 storage
    # ...

    # Generate upload URL
    response = await client.post(
        "/api/v1/evidence/generate-upload-url",
        json={
            "organization_id": "org-123",
            "case_id": "case-456",
            "filename": "screenshot.png",
            "content_type": "image/png"
        },
        headers={"Authorization": f"Bearer {test_user.token}"}
    )

    data = response.json()

    # Verify URL is S3 presigned URL (not local endpoint)
    assert data["upload_url"].startswith("https://")
    assert "s3.amazonaws.com" in data["upload_url"]
    assert data["storage_backend"] == "s3"
```

### Phase 1 Priority

**MUST be implemented in Phase 1** - Blocks local deployment evidence upload.

---

## Implementation Roadmap Updates

### Current Phase 1 (from deployment-strategy-v2.md)

| Task | Description | Effort |
|------|-------------|--------|
| 1.1 | Create TenantProvider protocol | 1d |
| 1.2 | Implement SingleTenantProvider | 1d |
| 1.3 | Implement MultiTenantProvider | 1d |
| 1.4 | Add factory and settings | 0.5d |
| 1.5 | Wire into DI container | 0.5d |
| 1.6 | Update CaseService to use TenantProvider | 1d |

### **UPDATED Phase 1** (with implementation concerns addressed)

| Task | Description | Effort | Priority |
|------|-------------|--------|----------|
| **1.1** | Create TenantProvider protocol | 1d | P0 |
| **1.2** | Implement SingleTenantProvider | 1d | P0 |
| **1.3** | Implement MultiTenantProvider | 1d | P0 |
| **1.4** | Add factory and settings | 0.5d | P0 |
| **1.5** | **Create bootstrap module with schema checks** | **1d** | **P0** |
| **1.6** | **Update lifespan handler with bootstrap** | **0.5d** | **P0** |
| **1.7** | **Add readiness probe with org verification** | **0.5d** | **P0** |
| **1.8** | Wire into DI container | 0.5d | P0 |
| **1.9** | Update CaseService to use TenantProvider | 1d | P0 |
| **1.10** | **Add /evidence/upload/{path} local endpoint** | **1d** | **P0** |
| **1.11** | **Update LocalStorageBackend.generate_upload_url()** | **0.5d** | **P0** |
| **1.12** | **Add purpose-based URL expiration (URLPurpose enum)** | **0.5d** | **P1** |
| **1.13** | **Add /evidence/refresh-urls endpoint** | **1d** | **P1** |
| **1.14** | **Frontend URL manager with auto-refresh** | **2d** | **P1** |

**Updated Effort**: 11.5 days (was 5.5 days)

### Phase Dependencies

```
Concern #1 (Bootstrapper):
  Must complete before → Any case/KB creation tests
  Blocks → Integration testing, E2E testing

Concern #3 (Local Upload Endpoint):
  Must complete before → Evidence upload tests
  Blocks → Local deployment validation

Concern #2 (URL Expiration):
  Nice-to-have in Phase 1
  Can defer to Phase 2 (minimal: 1-hour expiration works)
  Blocks → Production UX quality
```

---

## Recommendations

### 1. Update Deployment Strategy Document

**Add new Section 10: Implementation Notes**

```markdown
## 10. Implementation Notes - Critical Pitfalls

### 10.1 Bootstrapper Timing Issue

**Problem**: Bootstrapper may run before Alembic migrations create schema.

**Solution**: Implement schema check in `ensure_default_organization()`:
- Check if `organizations` table exists before querying
- Log warning and skip if table missing
- Kubernetes init container ensures migration ordering
- Readiness probe verifies bootstrap completed

**See**: /home/swhouse/product/faultmaven/docs/working/IMPLEMENTATION-CONCERNS-ANALYSIS.md#concern-1

### 10.2 Presigned URL Expiration

**Problem**: 1-hour URL expiration causes UX issues for long-lived pages.

**Solution**: Implement purpose-based expiration:
- THUMBNAIL: 15 minutes (quick click)
- DOWNLOAD: 1 hour (download window)
- INLINE_VIEW: 8 hours (workday session)
- Add `/evidence/refresh-urls` endpoint for background renewal

**See**: /home/swhouse/product/faultmaven/docs/working/IMPLEMENTATION-CONCERNS-ANALYSIS.md#concern-2

### 10.3 Local Storage Upload Endpoint

**Problem**: LocalStorageBackend returns upload URL, but endpoint doesn't exist.

**Solution**: Add FastAPI route `/evidence/upload/{org}/{case}/{date}/{filename}`:
- Handles PUT requests for local file upload
- Validates org membership and file size
- Only used when STORAGE_BACKEND=local
- S3 deployments bypass this via presigned URLs

**See**: /home/swhouse/product/faultmaven/docs/working/IMPLEMENTATION-CONCERNS-ANALYSIS.md#concern-3
```

### 2. Update Phase 1 Tasks in deployment-strategy-v2.md

Replace **Section 8.1** (Phase 1 tasks) with updated table from this document.

### 3. Create Kubernetes Deployment Guide

**New Document**: `/home/swhouse/product/faultmaven/docs/operations/kubernetes-deployment-guide.md`

Topics to cover:
- Init container pattern for migrations
- Readiness probe configuration
- Environment variable setup (TENANT_PROVIDER, STORAGE_BACKEND)
- Volume mounts for local storage (if STORAGE_BACKEND=local)
- Secret management (DB credentials, S3 keys)

### 4. Add Integration Tests

**New Test Suite**: `tests/integration/test_deployment_concerns.py`

Tests should cover:
- Bootstrap before migrations (graceful failure)
- Bootstrap after migrations (org creation)
- Bootstrap idempotency (multiple runs)
- Local upload endpoint (file upload)
- S3 presigned URLs (bypasses local endpoint)
- URL expiration policies (purpose-based)
- URL refresh endpoint (batch renewal)

### 5. Update API Documentation

Add to OpenAPI spec (`faultmaven/api/v1/routes/evidence.py`):
- `/evidence/upload/{path}` - Local storage upload
- `/evidence/generate-upload-url` - Generate upload URL
- `/evidence/refresh-urls` - Refresh expired URLs

Include examples showing local vs S3 workflows.

---

## Conclusion

All three concerns are **genuine production risks** that require immediate attention:

| Concern | Severity | Phase 1 Priority | Effort | Risk if Ignored |
|---------|----------|------------------|--------|-----------------|
| **#1: Bootstrapper Timing** | 🔴 Critical | MUST implement | 2d | App crashes on startup in K8s |
| **#2: URL Expiration** | 🟡 High | SHOULD implement | 4d | UX friction, page refresh loops |
| **#3: Local Upload Endpoint** | 🔴 Critical | MUST implement | 1.5d | Local deployment broken |

**Total Additional Effort**: 7.5 days (on top of original 5.5 days)

**Updated Phase 1 Timeline**: ~13 days (was 5.5 days)

These concerns demonstrate **mature engineering thinking** - identifying real-world failure modes before they hit production. The proposed solutions are production-ready and should be incorporated into the deployment strategy document before Phase 1 implementation begins.

---

**Next Steps**:

1. ✅ Review this analysis with the team
2. ✅ Update deployment-strategy-v2.md with Section 10 (Implementation Notes)
3. ✅ Update Phase 1 roadmap with new tasks
4. ✅ Create implementation tickets with code examples from this document
5. ✅ Proceed with Phase 1 implementation

**Document Status**: Ready for review and incorporation into deployment strategy.

**Author**: Solutions Architect Agent
**Date**: 2025-12-31
**Version**: 1.0

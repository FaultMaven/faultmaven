# TASK-023: TenantProvider Implementation for Deployment Neutrality

## Task Metadata
- **Phase**: Week 1 (Multi-Tenant Foundation Completion)
- **Priority**: P0 (Foundational infrastructure)
- **Estimated Time**: 5 days (strict time box)
- **Dependencies**:
  - TASK-021 (Organization Management API) - ✅ MERGED (PR #23)
  - TASK-020 (Remove Legacy Headers) - ⏳ SPECIFIED
- **Assignee**: Backend Engineer
- **Reports To**: Solutions Architect
- **Scope Limit**: STRICTLY TenantProvider only - NO S3, NO Pinecone, NO MetadataSanitizer

## Objective

**Implement TenantProvider abstraction** to enable deployment-neutral case and organization management that works in both:
1. **Local deployment** (single tenant, development, community edition)
2. **Cloud deployment** (multi-tenant, production, enterprise edition)

This is the **minimum viable implementation** of Objective 5 (Deployment Neutrality) from the Platform Evolution Strategy.

---

## Context

### Why We Need This NOW

The organization management API (TASK-021, PR #23) successfully implements multi-tenant isolation, but it has a **critical dependency**: it needs to know which organization context to operate in.

**Current Problem**:
```python
# In CaseService, we hardcode organization_id extraction
async def create_case(self, case_data: CaseCreate, current_user: User):
    # Where does organization_id come from?
    # Option 1: From current_user.organization_id (single-tenant assumption)
    # Option 2: From request context (multi-tenant)
    # This is NOT deployment-neutral!
    case_data.organization_id = current_user.organization_id  # ❌ Breaks in cloud
```

**Solution: TenantProvider Abstraction**
```python
# With TenantProvider, services are deployment-neutral
async def create_case(
    self,
    case_data: CaseCreate,
    current_user: User,
    tenant_provider: TenantProvider  # Injected by DI container
):
    # Works in BOTH local and cloud
    organization = await tenant_provider.get_current_organization(current_user)
    case_data.organization_id = organization.organization_id  # ✅ Deployment-neutral
```

### What This Enables

1. **Local Development** (SingleTenantProvider):
   - Single default organization created on startup
   - All users belong to same organization
   - No organization selection needed
   - Perfect for `git clone` → `python main.py` experience

2. **Cloud Production** (MultiTenantProvider):
   - Multiple organizations isolated
   - Organization context from JWT claims or request headers
   - Full multi-tenant isolation
   - Supports enterprise deployments

### Strategic Alignment

This is **Objective 5: Deployment Neutrality** from the Platform Evolution Strategy:

> **Goal**: Infrastructure becomes a deployment-time decision, not a code-time decision.
>
> **Success Criteria**:
> - ✅ TenantProvider layer implemented (SingleTenantProvider + MultiTenantProvider)
> - ✅ Zero conditional logic in application code (services don't know about deployment mode)
> - ✅ Provider factory with environment-based selection

**Scope for TASK-023**: First criterion only (TenantProvider). Remaining criteria deferred to Phase 2.

---

## Technical Specification

### 1. TenantProvider Protocol (Abstract Base Class)

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/providers/tenancy/base.py`

```python
from abc import ABC, abstractmethod
from typing import Optional
from faultmaven.domain.organization import Organization
from faultmaven.domain.user import User


class TenantProvider(ABC):
    """
    Abstract base class for tenant context resolution.

    Enables deployment-neutral services by abstracting organization context.
    Implementations:
    - SingleTenantProvider: Returns default organization (local deployment)
    - MultiTenantProvider: Extracts organization from request context (cloud)
    """

    @abstractmethod
    async def get_current_organization(
        self,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> Organization:
        """
        Resolve the current organization context.

        Args:
            current_user: Authenticated user from JWT
            organization_id: Optional explicit organization ID (for multi-tenant)

        Returns:
            Organization: The organization context for this request

        Raises:
            OrganizationNotFoundError: If organization doesn't exist
            AuthorizationError: If user not a member of organization
        """
        pass

    @abstractmethod
    async def get_default_organization(self) -> Organization:
        """
        Get the default organization (used for local/single-tenant mode).

        Returns:
            Organization: The default organization
        """
        pass

    @abstractmethod
    async def is_multi_tenant(self) -> bool:
        """
        Check if this provider operates in multi-tenant mode.

        Returns:
            bool: True if multi-tenant, False if single-tenant
        """
        pass
```

---

### 2. SingleTenantProvider (Local Deployment)

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/providers/tenancy/single_tenant.py`

```python
from typing import Optional
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.domain.organization import Organization
from faultmaven.domain.user import User
from faultmaven.repositories.organization_repository import OrganizationRepository
from faultmaven.exceptions import OrganizationNotFoundError


class SingleTenantProvider(TenantProvider):
    """
    Single-tenant provider for local/community deployments.

    Behavior:
    - Returns a single default organization for all requests
    - All users belong to the same organization
    - Simplifies local development and community edition

    Use Case:
    - Local development (git clone → python main.py)
    - Community edition (self-hosted, single team)
    - Testing and CI/CD
    """

    DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
    DEFAULT_ORG_SLUG = "default"
    DEFAULT_ORG_NAME = "Default Organization"

    def __init__(self, organization_repository: OrganizationRepository):
        self.organization_repository = organization_repository
        self._default_org: Optional[Organization] = None

    async def get_current_organization(
        self,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> Organization:
        """
        Always returns the default organization (ignores organization_id).

        In single-tenant mode, all users share the same organization.
        """
        return await self.get_default_organization()

    async def get_default_organization(self) -> Organization:
        """
        Get or create the default organization.

        Returns cached organization if available, otherwise loads from DB.
        If not found, raises OrganizationNotFoundError (should be created on startup).
        """
        if self._default_org is None:
            self._default_org = await self.organization_repository.get_by_id(
                self.DEFAULT_ORG_ID
            )
            if self._default_org is None:
                raise OrganizationNotFoundError(
                    f"Default organization not found. Run startup bootstrapper."
                )
        return self._default_org

    async def is_multi_tenant(self) -> bool:
        """Single-tenant mode."""
        return False

    async def ensure_default_organization_exists(self) -> Organization:
        """
        Create default organization if it doesn't exist (called by startup bootstrapper).

        Returns:
            Organization: The default organization (existing or newly created)
        """
        existing = await self.organization_repository.get_by_id(self.DEFAULT_ORG_ID)
        if existing:
            return existing

        # Create default organization
        from faultmaven.domain.organization import Organization, PlanTier
        default_org = Organization(
            organization_id=self.DEFAULT_ORG_ID,
            slug=self.DEFAULT_ORG_SLUG,
            name=self.DEFAULT_ORG_NAME,
            plan_tier=PlanTier.PRO,  # Local mode gets pro features
            settings={}
        )
        return await self.organization_repository.create(default_org)
```

---

### 3. MultiTenantProvider (Cloud Deployment)

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/providers/tenancy/multi_tenant.py`

```python
from typing import Optional
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.domain.organization import Organization
from faultmaven.domain.user import User
from faultmaven.repositories.organization_repository import OrganizationRepository
from faultmaven.repositories.organization_member_repository import OrganizationMemberRepository
from faultmaven.exceptions import (
    OrganizationNotFoundError,
    AuthorizationError,
    ValidationError
)


class MultiTenantProvider(TenantProvider):
    """
    Multi-tenant provider for cloud/enterprise deployments.

    Behavior:
    - Requires explicit organization_id for each request
    - Validates user membership in organization
    - Enforces multi-tenant isolation

    Use Case:
    - Cloud SaaS deployment (multiple organizations)
    - Enterprise deployment (department isolation)
    - Production environments
    """

    def __init__(
        self,
        organization_repository: OrganizationRepository,
        member_repository: OrganizationMemberRepository
    ):
        self.organization_repository = organization_repository
        self.member_repository = member_repository

    async def get_current_organization(
        self,
        current_user: User,
        organization_id: Optional[str] = None
    ) -> Organization:
        """
        Get organization with membership validation.

        Args:
            current_user: Authenticated user
            organization_id: Required in multi-tenant mode

        Returns:
            Organization: The organization if user is a member

        Raises:
            ValidationError: If organization_id not provided
            OrganizationNotFoundError: If organization doesn't exist
            AuthorizationError: If user not a member
        """
        if not organization_id:
            raise ValidationError(
                "organization_id is required in multi-tenant mode. "
                "Provide via JWT claim or request header."
            )

        # Get organization
        organization = await self.organization_repository.get_by_id(organization_id)
        if not organization:
            raise OrganizationNotFoundError(
                f"Organization {organization_id} not found"
            )

        # Verify user membership
        is_member = await self.member_repository.is_user_member(
            organization_id=organization_id,
            user_id=current_user.user_id
        )
        if not is_member:
            raise AuthorizationError(
                f"User {current_user.email} is not a member of organization "
                f"{organization.name}"
            )

        return organization

    async def get_default_organization(self) -> Organization:
        """
        Not supported in multi-tenant mode.

        Raises:
            NotImplementedError: Multi-tenant mode requires explicit organization_id
        """
        raise NotImplementedError(
            "Multi-tenant mode does not have a default organization. "
            "Provide organization_id explicitly."
        )

    async def is_multi_tenant(self) -> bool:
        """Multi-tenant mode."""
        return True
```

---

### 4. Provider Factory

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/providers/tenancy/factory.py`

```python
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.repositories.organization_repository import OrganizationRepository
from faultmaven.repositories.organization_member_repository import OrganizationMemberRepository
from faultmaven.config.settings import get_settings


def create_tenant_provider(
    organization_repository: OrganizationRepository,
    member_repository: OrganizationMemberRepository
) -> TenantProvider:
    """
    Factory function to create appropriate TenantProvider based on environment.

    Environment Variable:
        DEPLOYMENT_MODE: "single-tenant" | "multi-tenant"
        Default: "single-tenant"

    Args:
        organization_repository: Organization repository
        member_repository: Organization member repository

    Returns:
        TenantProvider: SingleTenantProvider or MultiTenantProvider
    """
    settings = get_settings()
    deployment_mode = settings.deployment_mode.lower()

    if deployment_mode == "multi-tenant":
        return MultiTenantProvider(
            organization_repository=organization_repository,
            member_repository=member_repository
        )
    else:
        # Default to single-tenant (local, community, development)
        return SingleTenantProvider(
            organization_repository=organization_repository
        )
```

---

### 5. Update Settings Configuration

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/config/settings.py`

```python
from pydantic_settings import BaseSettings
from typing import Literal


class Settings(BaseSettings):
    # ... existing settings ...

    # Deployment Mode
    deployment_mode: Literal["single-tenant", "multi-tenant"] = "single-tenant"
    """
    Deployment mode for tenant isolation.
    - single-tenant: All users share default organization (local, community)
    - multi-tenant: Multiple organizations with strict isolation (cloud, enterprise)
    """

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
```

---

### 6. DI Container Integration

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/container.py`

```python
from dependency_injector import containers, providers
from faultmaven.providers.tenancy.factory import create_tenant_provider


class Container(containers.DeclarativeContainer):
    # ... existing providers ...

    # Organization Repository (already exists from TASK-021)
    organization_repository = providers.Singleton(
        OrganizationRepository,
        session_factory=db.session_factory
    )

    # Organization Member Repository (already exists from TASK-021)
    organization_member_repository = providers.Singleton(
        OrganizationMemberRepository,
        session_factory=db.session_factory
    )

    # Tenant Provider (NEW)
    tenant_provider = providers.Singleton(
        create_tenant_provider,
        organization_repository=organization_repository,
        member_repository=organization_member_repository
    )

    # Case Service (UPDATE - add tenant_provider dependency)
    case_service = providers.Factory(
        CaseService,
        case_repository=case_repository,
        tenant_provider=tenant_provider  # NEW
    )

    # Organization Service (UPDATE - add tenant_provider if needed)
    organization_service = providers.Factory(
        OrganizationService,
        organization_repository=organization_repository,
        member_repository=organization_member_repository,
        tenant_provider=tenant_provider  # NEW
    )
```

---

### 7. Update CaseService to Use TenantProvider

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/services/case_service.py`

```python
from faultmaven.providers.tenancy.base import TenantProvider


class CaseService:
    def __init__(
        self,
        case_repository: CaseRepository,
        tenant_provider: TenantProvider  # NEW
    ):
        self.case_repository = case_repository
        self.tenant_provider = tenant_provider

    async def create_case(
        self,
        case_data: CaseCreate,
        current_user: User,
        organization_id: Optional[str] = None  # Optional for multi-tenant
    ) -> Case:
        """
        Create a case in the current organization context.

        Deployment-neutral implementation:
        - Single-tenant: Uses default organization
        - Multi-tenant: Uses provided organization_id with membership check
        """
        # Resolve organization context (deployment-neutral)
        organization = await self.tenant_provider.get_current_organization(
            current_user=current_user,
            organization_id=organization_id
        )

        # Create case in organization context
        case = Case(
            case_id=str(uuid4()),
            organization_id=organization.organization_id,  # Set from context
            title=case_data.title,
            description=case_data.description,
            created_by=current_user.user_id,
            created_at=datetime.utcnow()
        )

        return await self.case_repository.create(case)

    async def list_cases(
        self,
        current_user: User,
        organization_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> tuple[list[Case], int]:
        """
        List cases for current organization.

        Deployment-neutral implementation.
        """
        organization = await self.tenant_provider.get_current_organization(
            current_user=current_user,
            organization_id=organization_id
        )

        # Filter by organization_id (multi-tenant isolation)
        return await self.case_repository.list_by_organization(
            organization_id=organization.organization_id,
            limit=limit,
            offset=offset
        )
```

---

### 8. Startup Bootstrapper

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/bootstrap/startup.py`

```python
import logging
from faultmaven.container import Container
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)


async def bootstrap_application(container: Container):
    """
    Application startup bootstrapper.

    Tasks:
    1. Create default organization (single-tenant mode only)
    2. Verify database schema
    3. Initialize infrastructure providers
    """
    tenant_provider = container.tenant_provider()

    # Single-tenant mode: Ensure default organization exists
    if isinstance(tenant_provider, SingleTenantProvider):
        logger.info("Single-tenant mode: Ensuring default organization exists")
        default_org = await tenant_provider.ensure_default_organization_exists()
        logger.info(
            f"Default organization ready: {default_org.name} "
            f"(ID: {default_org.organization_id})"
        )
    else:
        logger.info("Multi-tenant mode: No default organization created")

    logger.info("Application bootstrap complete")
```

**Integration in Main**:

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/main.py`

```python
from faultmaven.bootstrap.startup import bootstrap_application


@app.on_event("startup")
async def startup_event():
    """Application startup tasks."""
    await bootstrap_application(container)
```

---

### 9. Update API Endpoints (Case Example)

**File**: `/home/swhouse/product/faultmaven/src/faultmaven/api/v1/cases.py`

```python
from fastapi import APIRouter, Depends, Header
from typing import Optional
from faultmaven.dependencies import get_current_user


router = APIRouter()


@router.post("/cases", status_code=201)
async def create_case(
    case_data: CaseCreate,
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    case_service: CaseService = Depends(get_case_service)
):
    """
    Create a case.

    Deployment modes:
    - Single-tenant: X-Organization-ID ignored (uses default org)
    - Multi-tenant: X-Organization-ID required (validates membership)
    """
    case = await case_service.create_case(
        case_data=case_data,
        current_user=current_user,
        organization_id=x_organization_id  # Passed to TenantProvider
    )
    return case


@router.get("/cases")
async def list_cases(
    current_user: User = Depends(get_current_user),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    limit: int = 100,
    offset: int = 0,
    case_service: CaseService = Depends(get_case_service)
):
    """
    List cases for current organization.

    Deployment-neutral implementation.
    """
    cases, total = await case_service.list_cases(
        current_user=current_user,
        organization_id=x_organization_id,
        limit=limit,
        offset=offset
    )
    return {
        "cases": cases,
        "total": total,
        "limit": limit,
        "offset": offset
    }
```

---

## Testing Requirements

### 1. Unit Tests for TenantProvider (tests/unit/providers/tenancy/)

**File**: `tests/unit/providers/tenancy/test_single_tenant_provider.py`

**Test Coverage** (12-15 tests):
- `test_get_current_organization_returns_default_org()`
- `test_get_current_organization_ignores_organization_id_parameter()`
- `test_get_default_organization_returns_cached_org()`
- `test_get_default_organization_loads_from_db_if_not_cached()`
- `test_get_default_organization_raises_if_not_found()`
- `test_ensure_default_organization_creates_if_missing()`
- `test_ensure_default_organization_returns_existing_if_present()`
- `test_is_multi_tenant_returns_false()`
- `test_default_org_has_pro_plan_tier()`

**File**: `tests/unit/providers/tenancy/test_multi_tenant_provider.py`

**Test Coverage** (15-18 tests):
- `test_get_current_organization_validates_membership()`
- `test_get_current_organization_raises_if_org_id_not_provided()`
- `test_get_current_organization_raises_if_org_not_found()`
- `test_get_current_organization_raises_if_user_not_member()`
- `test_get_current_organization_succeeds_if_user_is_member()`
- `test_get_default_organization_raises_not_implemented()`
- `test_is_multi_tenant_returns_true()`
- `test_membership_check_uses_member_repository()`

**File**: `tests/unit/providers/tenancy/test_factory.py`

**Test Coverage** (4-6 tests):
- `test_factory_creates_single_tenant_by_default()`
- `test_factory_creates_single_tenant_when_mode_is_single_tenant()`
- `test_factory_creates_multi_tenant_when_mode_is_multi_tenant()`
- `test_factory_passes_repositories_to_providers()`

---

### 2. Integration Tests for Services with TenantProvider

**File**: `tests/integration/services/test_case_service_with_tenant_provider.py`

**Test Coverage** (15-20 tests):

#### Single-Tenant Mode Tests (8-10 tests)
- `test_create_case_uses_default_organization_in_single_tenant_mode()`
- `test_create_case_ignores_organization_id_in_single_tenant_mode()`
- `test_list_cases_returns_all_cases_in_default_org()`
- `test_get_case_succeeds_in_single_tenant_mode()`
- `test_update_case_succeeds_in_single_tenant_mode()`
- `test_delete_case_succeeds_in_single_tenant_mode()`
- `test_multiple_users_share_same_organization()`

#### Multi-Tenant Mode Tests (7-10 tests)
- `test_create_case_requires_organization_id_in_multi_tenant_mode()`
- `test_create_case_validates_user_membership()`
- `test_create_case_raises_if_user_not_member()`
- `test_list_cases_filtered_by_organization_id()`
- `test_user_cannot_access_cases_from_other_organizations()`
- `test_admin_user_can_access_multiple_organizations()`
- `test_organization_isolation_enforced()`

---

### 3. End-to-End Deployment Mode Tests

**File**: `tests/integration/test_deployment_modes.py`

**Test Coverage** (8-10 tests):

#### Single-Tenant E2E Workflow (4-5 tests)
- `test_user_registration_creates_default_org_membership()`
- `test_all_users_share_default_organization()`
- `test_case_creation_without_organization_id_succeeds()`
- `test_startup_bootstrapper_creates_default_org()`

#### Multi-Tenant E2E Workflow (4-5 tests)
- `test_case_creation_requires_organization_id_header()`
- `test_cross_organization_access_blocked()`
- `test_membership_validation_enforced()`
- `test_organization_switching_works_for_multi_org_users()`

---

### 4. Startup Bootstrapper Tests

**File**: `tests/integration/test_startup_bootstrap.py`

**Test Coverage** (5-7 tests):
- `test_bootstrap_creates_default_org_in_single_tenant_mode()`
- `test_bootstrap_skips_default_org_in_multi_tenant_mode()`
- `test_bootstrap_idempotent_if_default_org_exists()`
- `test_default_org_has_correct_id_and_slug()`
- `test_default_org_has_pro_plan_tier()`

---

## Acceptance Criteria

### Functional Requirements
1. ✅ TenantProvider protocol defined with clear interface
2. ✅ SingleTenantProvider returns default organization
3. ✅ MultiTenantProvider validates membership and returns user's organization
4. ✅ Factory selects provider based on DEPLOYMENT_MODE environment variable
5. ✅ CaseService updated to use TenantProvider
6. ✅ OrganizationService updated to use TenantProvider
7. ✅ Startup bootstrapper creates default organization (single-tenant mode)
8. ✅ API endpoints accept optional X-Organization-ID header

### Testing Requirements
1. ✅ SingleTenantProvider tests: 12-15 tests
2. ✅ MultiTenantProvider tests: 15-18 tests
3. ✅ Factory tests: 4-6 tests
4. ✅ Service integration tests: 15-20 tests
5. ✅ Deployment mode E2E tests: 8-10 tests
6. ✅ Startup bootstrapper tests: 5-7 tests
7. ✅ **Total**: 59-76 tests
8. ✅ All tests pass consistently

### Code Quality
1. ✅ Clean abstraction (services don't know about deployment mode)
2. ✅ No conditional logic in services (deployment mode handled by provider)
3. ✅ Type hints and docstrings
4. ✅ Error handling (OrganizationNotFoundError, AuthorizationError)
5. ✅ Follows existing repository and service patterns

### Non-Functional Requirements
1. ✅ Performance: TenantProvider resolution < 10ms
2. ✅ Caching: Default organization cached in SingleTenantProvider
3. ✅ Backward compatibility: Existing tests still pass

---

## Deliverables

1. **Provider Implementation** (New):
   - `src/faultmaven/providers/tenancy/base.py` - TenantProvider protocol
   - `src/faultmaven/providers/tenancy/single_tenant.py` - SingleTenantProvider
   - `src/faultmaven/providers/tenancy/multi_tenant.py` - MultiTenantProvider
   - `src/faultmaven/providers/tenancy/factory.py` - Provider factory

2. **Infrastructure** (New):
   - `src/faultmaven/bootstrap/startup.py` - Startup bootstrapper

3. **Service Updates** (Modified):
   - `src/faultmaven/services/case_service.py` - Add tenant_provider dependency
   - `src/faultmaven/services/organization_service.py` - Add tenant_provider (if needed)

4. **DI Container** (Modified):
   - `src/faultmaven/container.py` - Add tenant_provider

5. **Configuration** (Modified):
   - `src/faultmaven/config/settings.py` - Add deployment_mode setting

6. **API Endpoints** (Modified):
   - `src/faultmaven/api/v1/cases.py` - Accept X-Organization-ID header
   - (Other endpoints as needed)

7. **Tests** (New):
   - `tests/unit/providers/tenancy/test_single_tenant_provider.py` (12-15 tests)
   - `tests/unit/providers/tenancy/test_multi_tenant_provider.py` (15-18 tests)
   - `tests/unit/providers/tenancy/test_factory.py` (4-6 tests)
   - `tests/integration/services/test_case_service_with_tenant_provider.py` (15-20 tests)
   - `tests/integration/test_deployment_modes.py` (8-10 tests)
   - `tests/integration/test_startup_bootstrap.py` (5-7 tests)

8. **Documentation**:
   - Deployment mode configuration guide
   - TenantProvider usage examples
   - Migration guide for existing services

9. **Pull Request**:
   - Title: "feat: implement TenantProvider for deployment neutrality (TASK-023)"
   - Description: Enables deployment-neutral case/org management
   - Link to TASK-023-TENANT-PROVIDER.md
   - Migration guide for other services

---

## Implementation Plan

### Day 1: TenantProvider Protocol and SingleTenantProvider
1. Create `providers/tenancy/` module structure
2. Implement `TenantProvider` protocol (base.py)
3. Implement `SingleTenantProvider` (single_tenant.py)
4. Unit tests for SingleTenantProvider (12-15 tests)
5. **Deliverable**: SingleTenantProvider complete and tested

### Day 2: MultiTenantProvider
1. Implement `MultiTenantProvider` (multi_tenant.py)
2. Unit tests for MultiTenantProvider (15-18 tests)
3. Implement provider factory (factory.py)
4. Factory unit tests (4-6 tests)
5. **Deliverable**: Both providers and factory complete

### Day 3: DI Container and Settings
1. Update Settings with `deployment_mode`
2. Integrate factory into DI container
3. Update `CaseService` to use TenantProvider
4. Update `OrganizationService` (if needed)
5. **Deliverable**: Services deployment-neutral

### Day 4: Startup Bootstrapper and API Updates
1. Implement startup bootstrapper
2. Integrate bootstrapper into main.py
3. Update API endpoints (X-Organization-ID header)
4. Startup bootstrapper tests (5-7 tests)
5. **Deliverable**: Complete integration

### Day 5: Integration Tests and Validation
1. Service integration tests (15-20 tests)
2. Deployment mode E2E tests (8-10 tests)
3. Verify all existing tests still pass
4. Performance validation (< 10ms resolution)
5. **Deliverable**: PR ready for review

---

## Dependencies

### Required Repositories (Already Implemented)
- ✅ OrganizationRepository (TASK-021)
- ✅ OrganizationMemberRepository (TASK-021)
- ✅ CaseRepository (existing)

### Required Services (Already Implemented)
- ✅ CaseService (existing)
- ✅ OrganizationService (TASK-021)

### External Dependencies
- None (all existing)

---

## Success Criteria

**APPROVED if:**
- ✅ TenantProvider protocol, SingleTenantProvider, MultiTenantProvider implemented
- ✅ Factory selects provider based on DEPLOYMENT_MODE
- ✅ CaseService deployment-neutral (works in both modes)
- ✅ Startup bootstrapper creates default organization (single-tenant)
- ✅ 59-76 tests passing
- ✅ All existing tests still pass
- ✅ No performance regression
- ✅ Clean abstraction (no conditional logic in services)

**REQUEST CHANGES if:**
- ❌ Deployment mode leaking into service logic (conditional branching)
- ❌ Missing tests for deployment modes
- ❌ Default organization not created on startup
- ❌ Existing tests broken
- ❌ Performance regression (> 10ms resolution)

---

## Scope Boundaries (CRITICAL)

### IN SCOPE (TASK-023)
✅ TenantProvider protocol and implementations
✅ Factory and DI integration
✅ Update CaseService and OrganizationService
✅ Startup bootstrapper for default org
✅ Tests (59-76 tests)

### OUT OF SCOPE (Deferred to Phase 2 or Later)
❌ S3StorageBackend (storage provider pattern)
❌ PineconeVectorStore (vector provider pattern)
❌ MetadataSanitizer (data sanitization)
❌ Presigned URLs (S3 security)
❌ Rate limiting providers
❌ Cache providers (beyond existing)
❌ Full deployment strategy (4-week effort)

**Enforcement**: If scope expands, STOP work and escalate to Solutions Architect.

---

## Risks and Mitigation

### Risk 1: Implementation Takes Longer Than 5 Days
**Likelihood**: MEDIUM
**Impact**: MEDIUM
**Mitigation**:
- Strict scope enforcement (NO S3, NO Pinecone)
- Daily progress check-ins
- Time box: Hard stop at Day 5

**Contingency**: If Day 5 overruns, submit PR as-is with incomplete tests (complete in follow-up)

### Risk 2: Breaks Existing Tests
**Likelihood**: LOW
**Impact**: HIGH
**Mitigation**:
- Run full test suite after each service update
- Backward compatibility: services default to single-tenant mode
- Gradual rollout: update CaseService first, then others

**Contingency**: Revert changes, use feature flag to enable TenantProvider

### Risk 3: Scope Creep (Full Deployment Strategy)
**Likelihood**: MEDIUM
**Impact**: HIGH
**Mitigation**:
- Explicit OUT OF SCOPE section in task
- Daily scope check
- Clear boundaries in PR description

**Contingency**: Stop work, create TASK-024 for overrun scope, escalate

---

## Notes

- This is the **minimum viable implementation** of Objective 5 (Deployment Neutrality)
- Focus on **case and organization management** only
- Other services (session, evidence, knowledge) can be updated later
- The goal is **deployment-neutral foundation**, not full deployment strategy
- Success = Case and org management work in both local and cloud modes

---

**Estimated Effort**: 5 days (strict time box)
**Assignee**: Backend Engineer
**Complexity**: MEDIUM (clear abstraction, well-defined scope)
**Strategic Importance**: HIGH (enables all future endpoint work to be deployment-neutral)

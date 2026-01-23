# OAuth Service Wiring Plan

## Overview

This document outlines the wiring needed to integrate the OAuth 2.0 service into the FaultMaven dependency injection container.

## Components Created

### 1. Domain Layer
- ✅ `IOAuthService` interface (contracts.py)
- ✅ `OAuthServiceImpl` implementation (oauth_service.py)
- ✅ `IJWTTokenGenerator` interface
- ✅ `RS256JWTTokenGenerator` implementation

### 2. Infrastructure Layer
- ✅ `IOAuthCodeRepository` interface
- ✅ `InMemoryOAuthCodeRepository` (for local)
- ✅ `RedisOAuthCodeRepository` (for cloud)
- ✅ `PostgresOAuthCodeRepository` (for enterprise)
- ✅ `ITokenRevocationStore` interface
- ✅ `InMemoryTokenRevocationStore` (for local)
- ✅ `RedisTokenRevocationStore` (for cloud)
- ✅ `PostgresTokenRevocationStore` (for enterprise)

### 3. API Layer
- ✅ `oauth.py` router with endpoints:
  - `GET /auth/oauth/authorize`
  - `POST /auth/oauth/token`
  - `POST /auth/oauth/revoke`

### 4. Configuration
- ✅ `AuthMode` enum (DEV_LOGIN, OAUTH)
- ✅ `AuthSettings` class with OAuth configuration

## Corrected Architecture Understanding

**Two Deployments:**
1. **Local (Single-User)**: SQLite database + in-memory caching
2. **Cloud (SaaS Enterprise)**: PostgreSQL database + Redis caching

**Storage Layers:**
- **Cache Layer** (ephemeral, short-lived data): In-memory (local) or Redis (cloud)
- **Database Layer** (persistent): SQLite (local) or PostgreSQL (cloud)

**OAuth Code Storage Strategy:**
- Authorization codes are **ephemeral** (10 min expiry)
- Should use **cache layer only** for retrieval (in-memory or Redis)
- **Optional** database persistence for compliance/audit trail (write-only)

## Wiring Tasks

### Step 1: Add OAuth Service Factory Function

**File**: `/home/swhouse/product/faultmaven/faultmaven/container/providers/services.py`

**Location**: After `create_session_service` function

```python
def create_oauth_service(
    settings: Any,
    user_repository: Any,
    code_repository: Any,
    token_generator: Any,
) -> Any:
    """Create OAuth service based on configuration.

    Args:
        settings: FaultMavenSettings instance
        user_repository: User repository for user lookups
        code_repository: OAuth code storage (cache layer: in-memory or Redis)
        token_generator: JWT token generator (RS256)

    Returns:
        OAuthServiceImpl instance or None if OAuth disabled
    """
    from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl

    # Check if OAuth enabled
    if not settings.auth.oauth_enabled:
        logger.info("OAuth service disabled (using dev-login)")
        return None

    return OAuthServiceImpl(
        code_repository=code_repository,
        user_repository=user_repository,
        token_generator=token_generator,
        settings=settings.auth,
    )
```

### Step 2: Add JWT Token Generator Factory

**File**: Same as above

```python
def create_jwt_token_generator(
    settings: Any,
    revocation_store: Any,
) -> Any:
    """Create JWT token generator with RS256 signing.

    Args:
        settings: FaultMavenSettings instance
        revocation_store: Token revocation tracking store

    Returns:
        RS256JWTTokenGenerator instance
    """
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        RS256JWTTokenGenerator,
    )

    # Load RSA key pair from settings
    # TODO: For now, use placeholder keys - need to implement key management
    private_key = settings.auth.jwt_private_key
    public_key = settings.auth.jwt_public_key

    return RS256JWTTokenGenerator(
        private_key=private_key,
        public_key=public_key,
        revocation_store=revocation_store,
        settings=settings.auth,
    )
```

### Step 3: Add OAuth Code Repository Factory

**File**: Same as above

```python
def create_oauth_code_repository(
    settings: Any,
    cache_client: Any = None,  # Redis for cloud, None for local
) -> Any:
    """Create OAuth code repository based on deployment.

    Authorization codes are ephemeral (10 min) and should use cache layer only.
    Database persistence is optional for compliance/audit (write-only).

    Args:
        settings: FaultMavenSettings instance
        cache_client: Redis client for cloud, None for local (uses in-memory)

    Returns:
        OAuth code repository instance (cache layer only)
    """
    # Determine if we're in cloud or local deployment
    is_cloud = cache_client is not None

    if is_cloud:
        # Cloud deployment: Use Redis cache
        from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
            RedisOAuthCodeRepository,
        )
        return RedisOAuthCodeRepository(cache_client)
    else:
        # Local deployment: Use in-memory cache
        from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
            InMemoryOAuthCodeRepository,
        )
        return InMemoryOAuthCodeRepository()
```

### Step 4: Add Token Revocation Store Factory

**File**: Same as above

```python
def create_token_revocation_store(
    settings: Any,
    cache_client: Any = None,  # Redis for cloud, None for local
) -> Any:
    """Create token revocation store based on deployment.

    Revoked tokens are tracked with TTL (matching token expiration).
    Uses cache layer only for ephemeral tracking.

    Args:
        settings: FaultMavenSettings instance
        cache_client: Redis client for cloud, None for local (uses in-memory)

    Returns:
        Token revocation store instance (cache layer only)
    """
    # Determine if we're in cloud or local deployment
    is_cloud = cache_client is not None

    if is_cloud:
        # Cloud deployment: Use Redis cache
        from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
            RedisTokenRevocationStore,
        )
        return RedisTokenRevocationStore(cache_client)
    else:
        # Local deployment: Use in-memory cache
        from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
            InMemoryTokenRevocationStore,
        )
        return InMemoryTokenRevocationStore()
```

### Step 5: Register Services in Container

**File**: `/home/swhouse/product/faultmaven/faultmaven/container/providers/services.py`

**Location**: In `register_services` function, after session service registration

```python
# OAuth Service (if enabled)
if settings.auth.oauth_enabled:
    logger.info("Registering OAuth service...")

    # Get cache client (Redis for cloud, None for local)
    cache_client = container.get_service("redis_client", required=False)

    # Create OAuth code repository (cache layer only)
    oauth_code_repository = create_oauth_code_repository(
        settings,
        cache_client=cache_client,
    )
    container._register_service("oauth_code_repository", oauth_code_repository)

    # Create token revocation store (cache layer only)
    token_revocation_store = create_token_revocation_store(
        settings,
        cache_client=cache_client,
    )
    container._register_service("token_revocation_store", token_revocation_store)

    # Create JWT token generator
    jwt_token_generator = create_jwt_token_generator(
        settings,
        revocation_store=token_revocation_store,
    )
    container._register_service("jwt_token_generator", jwt_token_generator)

    # Create OAuth service
    oauth_service = create_oauth_service(
        settings,
        user_repository=container.get_service("user_repository"),
        code_repository=oauth_code_repository,
        token_generator=jwt_token_generator,
    )
    container._register_service("oauth_service", oauth_service)

    logger.info("✅ OAuth service registered (cache: %s)",
                "Redis" if cache_client else "in-memory")
else:
    logger.info("OAuth service disabled (using dev-login mode)")
```

### Step 6: Add Getter Method to Container

**File**: `/home/swhouse/product/faultmaven/faultmaven/_container_impl.py`

**Location**: After `get_session_service()` method (around line 373)

```python
def get_oauth_service(self):
    """Get the OAuth service (if enabled)."""
    if not self._initialized and not getattr(self, "_initializing", False):
        self._ensure_initialized_for_getter()
    return getattr(self, "oauth_service", None)
```

### Step 7: Register OAuth Router in FastAPI

**File**: `/home/swhouse/product/faultmaven/faultmaven/main.py`

**Location**: Where other routers are registered

```python
# Import OAuth router
from faultmaven.modules.auth.api.oauth import router as oauth_router

# Register OAuth router (if enabled)
if settings.auth.oauth_enabled:
    app.include_router(oauth_router)
    logger.info("✅ OAuth endpoints registered")
```

### Step 8: Add RSA Key Pair to Settings

**File**: `/home/swhouse/product/faultmaven/faultmaven/config/settings.py`

**Location**: In `AuthSettings` class

```python
# JWT RS256 Key Pair
jwt_private_key: Optional[SecretStr] = Field(
    default=None,
    env="JWT_PRIVATE_KEY",
    description="RSA private key for JWT signing (PEM format)"
)

jwt_public_key: Optional[str] = Field(
    default=None,
    env="JWT_PUBLIC_KEY",
    description="RSA public key for JWT validation (PEM format)"
)
```

## Deployment Configuration

### Local Development (.env)

```bash
# Authentication: Dev-login mode (username-only)
AUTH_MODE=dev-login
OAUTH_ENABLED=false

# Database: SQLite
DATABASE_URL=sqlite:///./faultmaven.db

# Cache: In-memory (no Redis needed)
# No REDIS_URL required for local
```

### Cloud Deployment (SaaS Enterprise) (.env)

```bash
# Authentication: OAuth 2.0 + PKCE
AUTH_MODE=oauth
OAUTH_ENABLED=true

# Database: PostgreSQL (persistent layer)
DATABASE_URL=postgresql://user:pass@host:5432/faultmaven

# Cache: Redis (ephemeral layer)
REDIS_URL=redis://host:6379/0

# JWT Key Pair (RS256)
JWT_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"

# OAuth Configuration
OAUTH_ALLOWED_CLIENTS=["faultmaven-copilot"]
DASHBOARD_URL=https://dashboard.faultmaven.ai
```

## Testing Strategy

### Unit Tests
- Test OAuth service PKCE verification
- Test JWT token generation and validation
- Test code repository implementations
- Test revocation store implementations

### Integration Tests
- Test complete OAuth flow end-to-end
- Test token refresh flow
- Test token revocation
- Test error cases (expired codes, invalid verifiers, etc.)

## Architecture Summary

**Correct Layered Approach:**

1. **Cache Layer** (ephemeral, TTL-based):
   - Local: In-memory dictionaries
   - Cloud: Redis
   - Used for: OAuth codes (10 min), revoked tokens (until token expiry)

2. **Database Layer** (persistent):
   - Local: SQLite
   - Cloud: PostgreSQL
   - Used for: Users, sessions, cases, evidence, etc.
   - NOT used for: OAuth code retrieval (cache only)

3. **Optional Audit Trail**:
   - OAuth codes can be persisted to DB for compliance
   - Write-only (never read for authorization flow)
   - Enabled via `OAUTH_PERSIST_CODES_TO_DB=true`

## Security Considerations

1. **RSA Key Management**: Need to implement secure key generation and storage
2. **Key Rotation**: Plan for periodic key rotation without downtime
3. **Revocation Cleanup**: Automatic via TTL (Redis) or in-memory expiry
4. **Code Cleanup**: Automatic via TTL (Redis) or in-memory expiry

## Next Steps

1. ✅ Create factory functions
2. ✅ Register services in container
3. ✅ Add getter methods
4. ✅ Register OAuth router
5. ⬜ Generate RSA key pair for development
6. ⬜ Write unit tests
7. ⬜ Write integration tests
8. ⬜ Add observability (structured logging, metrics)
9. ⬜ Create database migrations (OAuth tables)
10. ⬜ Update documentation

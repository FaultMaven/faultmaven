# OAuth Architecture - Corrected

## Deployment Strategy (Corrected Understanding)

FaultMaven has **two deployment models**, not three:

1. **Local (Single-User)**
   - Database: SQLite
   - Cache: In-memory Python dictionaries
   - Auth: Dev-login (username-only)
   - Target: Individual developers

2. **Cloud (SaaS Enterprise)**
   - Database: PostgreSQL (persistent layer)
   - Cache: Redis (ephemeral layer)
   - Auth: OAuth 2.0 + PKCE
   - Target: Enterprise multi-user deployments

## Storage Layers

### Cache Layer (Ephemeral, TTL-based)

**Purpose**: Short-lived, high-frequency data with automatic expiration

**Local Implementation**: In-memory Python dictionaries with manual TTL tracking

**Cloud Implementation**: Redis with automatic TTL expiration

**Used For:**

- OAuth authorization codes (10 minute TTL)
- Revoked token JTIs (TTL matches token expiration)
- Session data (optional caching)

**Key Characteristic**: Data disappears after TTL expires (by design)

### Database Layer (Persistent)

**Purpose**: Long-term storage, audit trails, business data

**Local Implementation**: SQLite file-based database

**Cloud Implementation**: PostgreSQL server

**Used For:**

- Users, teams, organizations
- Cases, evidence, investigations
- Knowledge base documents
- Audit logs (optional for OAuth codes)

**Key Characteristic**: Data persists until explicitly deleted

## OAuth Code Storage Strategy

### Primary Storage: Cache Layer ONLY

Authorization codes are **ephemeral by design** (10 minute lifespan):

1. **Generation**: Dashboard creates code with PKCE challenge
2. **Storage**: Save to cache layer (in-memory or Redis)
3. **Retrieval**: Extension exchanges code for tokens (cache lookup)
4. **Expiration**: Automatic after 10 minutes (TTL)

**Why cache-only?**

- Codes are single-use (marked as used after exchange)
- Short lifespan (10 minutes)
- High frequency (new code per auth attempt)
- No need for persistence after expiry

### Optional: Database Persistence

For compliance/audit requirements ONLY:

- Enable via `OAUTH_PERSIST_CODES_TO_DB=true`
- Writes codes to database (PostgreSQL only)
- **Never used for code retrieval** (cache remains source of truth)
- Provides audit trail for security investigations

## Token Revocation Storage Strategy

### Primary Storage: Cache Layer ONLY

Revoked tokens tracked by JTI (JWT ID) with TTL:

1. **Revocation**: User logs out or token compromised
2. **Storage**: Add JTI to revocation store (cache layer)
3. **Validation**: Check if JTI revoked before accepting token
4. **Expiration**: Automatic after token's original expiry time

**Why cache-only?**

- Revocation entry only needed until token expires
- After expiry, token is invalid anyway (no need to track)
- High-frequency lookups on every API request
- TTL matches token expiration (self-cleaning)

## Implementation Validation

### Files Reviewed

1. ✅ **OAuth Service** ([oauth_service.py](../../faultmaven/modules/auth/domain/services/oauth_service.py))
   - Uses injected `code_repository` (abstraction)
   - No assumption about storage backend

2. ✅ **Code Repositories** ([oauth_code_repository.py](../../faultmaven/modules/auth/infrastructure/repositories/oauth_code_repository.py))
   - Three implementations provided
   - **Need to update**: PostgreSQL should be optional write-only

3. ✅ **Token Revocation Stores** ([token_revocation_store.py](../../faultmaven/modules/auth/infrastructure/stores/token_revocation_store.py))
   - Three implementations provided
   - **Need to update**: PostgreSQL should be optional write-only

4. ✅ **Configuration** ([settings.py](../../faultmaven/config/settings.py))
   - **CORRECTED**: Removed `oauth_code_storage` selector
   - **ADDED**: `oauth_use_cache` (always true)
   - **ADDED**: `oauth_persist_codes_to_db` (optional audit)

5. ✅ **Wiring Plan** ([OAUTH_WIRING_PLAN.md](./OAUTH_WIRING_PLAN.md))
   - **UPDATED**: Factory functions use cache_client parameter
   - **UPDATED**: Deployment configs show layered approach

## Required Code Changes

### 1. Configuration (COMPLETED)

**File**: `faultmaven/config/settings.py`

**Change**: Replace `oauth_code_storage` selector with layered approach

**Status**: ✅ DONE

### 2. Wiring Plan (COMPLETED)

**File**: `docs/working/OAUTH_WIRING_PLAN.md`

**Change**: Update factory functions to use cache_client parameter

**Status**: ✅ DONE

### 3. PostgreSQL Repository (OPTIONAL)

**File**: `faultmaven/modules/auth/infrastructure/repositories/oauth_code_repository.py`

**Change**: Make PostgresOAuthCodeRepository write-only (audit trail)

**Status**: ⬜ NOT REQUIRED (implementation works, but not optimal)

**Recommendation**: If audit trail needed, implement composite repository:

```python
class AuditedOAuthCodeRepository(IOAuthCodeRepository):
    """Composite repository: cache for retrieval + DB for audit."""

    def __init__(self, cache_repo, db_repo=None):
        self.cache = cache_repo  # Primary (in-memory or Redis)
        self.db = db_repo  # Optional (PostgreSQL write-only)

    async def save_code(self, code_data):
        await self.cache.save_code(code_data)
        if self.db:
            await self.db.save_code(code_data)  # Audit trail

    async def get_code(self, code):
        return await self.cache.get_code(code)  # Cache only

    async def mark_code_used(self, code):
        await self.cache.mark_code_used(code)
        if self.db:
            await self.db.mark_code_used(code)  # Update audit
```

## Summary

The OAuth implementation is **architecturally correct** with the clarified understanding:

- ✅ Uses cache layer (in-memory or Redis) for ephemeral data
- ✅ Deployment-agnostic via dependency injection
- ✅ TTL-based automatic expiration
- ✅ Configuration updated to reflect layered approach
- ✅ Wiring plan updated with correct factory patterns

**No breaking changes required** - the existing implementation works correctly with the updated configuration and wiring plan.

The key correction was in **understanding and documentation**, not in code logic.

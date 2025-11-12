# Redis Usage Design - FaultMaven

**Version**: 1.0
**Status**: Authoritative Standard
**Last Updated**: 2025-01-09

---

## Executive Summary

This document defines **what data is stored in Redis** and why, providing a complete picture of Redis usage in FaultMaven.

**Key Principle**: Redis is used for **high-performance temporary/cached data** that benefits from:
- ✅ Fast O(1) lookups (sessions, auth tokens)
- ✅ Built-in TTL support (automatic expiration)
- ✅ Atomic operations (token validation, session locking)
- ✅ Pub/sub capabilities (future: real-time updates)

**What is NOT in Redis**:
- ❌ Case data (moved to PostgreSQL - see case-storage-design.md)
- ❌ Knowledge base content (ChromaDB vector store)
- ❌ Long-term persistent data (PostgreSQL)

---

## Table of Contents

1. [Current Redis Usage](#1-current-redis-usage)
2. [Session Storage](#2-session-storage)
3. [Authentication Data](#3-authentication-data)
4. [Report Metadata](#4-report-metadata)
5. [Deprecated: Case Storage](#5-deprecated-case-storage)
6. [Redis Key Schema Reference](#6-redis-key-schema-reference)
7. [Configuration](#7-configuration)
8. [Failover Strategy](#8-failover-strategy)

---

## 1. Current Redis Usage

### Active Redis Data Types (2025-01-09)

| Data Type | Module | Purpose | TTL | Fallback |
|-----------|--------|---------|-----|----------|
| **Sessions** | `RedisSessionStore` | Conversation state | 30 min | InMemory |
| **Users** | `DevUserStore` | User accounts (dev) | None | InMemory |
| **Auth Tokens** | `DevTokenManager` | API authentication | 24 hrs | InMemory |
| **Report Metadata** | `RedisReportStore` | Case report indexes | 90 days | None |

### Storage Size Estimates

| Data Type | Avg Size/Record | Expected Records | Total Storage |
|-----------|-----------------|------------------|---------------|
| Sessions | ~5-10 KB | 100-1000 active | ~0.5-10 MB |
| Users | ~500 bytes | 10-100 users | ~5-50 KB |
| Auth Tokens | ~200 bytes | 10-100 tokens | ~2-20 KB |
| Report Metadata | ~1 KB | 100-1000 reports | ~100 KB-1 MB |
| **Total** | | | **~1-12 MB** |

**Conclusion**: Redis storage is minimal, easily fits in memory.

---

## 2. Session Storage

### Purpose

Store conversation session state for real-time user interactions with FaultMaven troubleshooting AI.

### Storage Schema

**Implementation**: `RedisSessionStore` ([code](../../faultmaven/infrastructure/persistence/redis_session_store.py))

```
Key Pattern: session:{session_id}
Value: JSON hash
TTL: 1800 seconds (30 minutes)

Example:
session:sess_abc123xyz → {
  "session_id": "sess_abc123xyz",
  "user_id": "user_def456",
  "client_id": "browser_abc",
  "case_id": "case_xyz789",  // Linked case if exists
  "created_at": "2025-01-09T10:00:00Z",
  "last_activity": "2025-01-09T10:25:00Z",
  "messages": [...],  // Recent messages
  "context": {...}    // Session context
}
```

### Additional Indexes

```
# Client lookup (find session by user + client)
client_session:{user_id}:{client_id} → session_id
TTL: Same as session (30 min)
```

### Why Redis?

- ✅ **TTL Support**: Sessions auto-expire after inactivity (30 min)
- ✅ **Fast Lookup**: O(1) session retrieval by session_id
- ✅ **Atomic Updates**: SETNX for session locking
- ✅ **High Write Rate**: Real-time conversation updates

### Fallback Strategy

```python
# Development: InMemory fallback
if not redis_available:
    session_store = InMemorySessionStore()
    logger.warning("Using InMemory session store (data lost on restart)")
```

**Production Requirement**: Redis is REQUIRED in production for session persistence across API restarts.

---

## 3. Authentication Data

### 3.1 User Accounts

**Implementation**: `DevUserStore` ([code](../../faultmaven/infrastructure/auth/user_store.py))

**Purpose**: Store user account data for development authentication system.

```
# User record
auth:user:{user_id} → {
  "user_id": "user_abc123",
  "username": "john@example.com",
  "email": "john@example.com",
  "display_name": "John Doe",
  "hashed_password": "bcrypt_hash...",
  "created_at": "2025-01-09T10:00:00Z",
  "last_login": "2025-01-09T11:00:00Z",
  "roles": ["user", "admin"]
}

# Username → User ID index
auth:username:{username} → user_id
Example: auth:username:john@example.com → user_abc123

# Email → User ID index
auth:email:{email} → user_id
Example: auth:email:john@example.com → user_abc123

# User list
auth:user_list → [user_id1, user_id2, ...]
```

**Why Redis?**
- ✅ Fast username uniqueness check (O(1) lookup)
- ✅ Atomic user creation (prevents duplicate usernames)
- ✅ Development simplicity (no PostgreSQL user DB needed for dev)

**Production Note**: In production, user data should move to PostgreSQL `users` table with proper RBAC schema. Redis is suitable for development only.

### 3.2 Authentication Tokens

**Implementation**: `DevTokenManager` ([code](../../faultmaven/infrastructure/auth/token_manager.py))

**Purpose**: Manage API authentication tokens with secure hashing and automatic expiration.

```
# Token hash → User ID (for validation)
auth:token:{sha256_hash} → user_id
TTL: 86400 seconds (24 hours)
Example: auth:token:a1b2c3... → user_abc123

# User's active tokens
auth:user_tokens:{user_id} → [token_id1, token_id2, ...]
TTL: 86400 seconds (24 hours)
Example: auth:user_tokens:user_abc123 → ["tok_xyz", "tok_def"]

# Token metadata
auth:token_meta:{token_id} → {
  "token_id": "tok_xyz789",
  "user_id": "user_abc123",
  "created_at": "2025-01-09T10:00:00Z",
  "last_used": "2025-01-09T11:00:00Z",
  "expires_at": "2025-01-10T10:00:00Z",
  "usage_count": 42
}
TTL: 86400 seconds (24 hours)
```

**Security Features**:
- ✅ Tokens stored as SHA-256 hashes (never plaintext)
- ✅ Automatic expiration (24 hour TTL)
- ✅ Usage tracking
- ✅ Token revocation support

**Why Redis?**
- ✅ Built-in TTL (automatic token expiration)
- ✅ Fast validation (O(1) hash lookup)
- ✅ Atomic operations (prevent race conditions)

---

## 4. Report Metadata

### Purpose

Store case report metadata for fast querying, while actual report content lives in ChromaDB.

### Hybrid Architecture

**Metadata**: Redis (fast lookups, indexes)
**Content**: ChromaDB (efficient text storage, similarity search)

### Storage Schema

**Implementation**: `RedisReportStore` ([code](../../faultmaven/infrastructure/persistence/redis_report_store.py))

```
# All reports for a case (sorted by timestamp)
case:{case_id}:reports → Sorted Set
  Score: timestamp (Unix)
  Members: report_id
Example: case:case_abc:reports → [
  (1704801600, "rep_xyz"),
  (1704805200, "rep_def")
]

# Report metadata
report:{report_id}:metadata → Hash
Example: report:rep_xyz:metadata → {
  "report_id": "rep_xyz789",
  "case_id": "case_abc123",
  "type": "INCIDENT_REPORT",
  "version": 1,
  "status": "final",
  "created_at": "2025-01-09T10:00:00Z",
  "created_by": "user_abc",
  "is_current": true,
  "content_location": "chromadb://faultmaven_case_reports/rep_xyz789"
}

# Reports by type (sorted by version desc)
case:{case_id}:reports:{type} → Sorted Set
  Score: -version (negative for desc order)
  Members: report_id
Example: case:case_abc:reports:INCIDENT_REPORT → [
  (-3, "rep_xyz"),  // v3 (latest)
  (-2, "rep_def"),  // v2
  (-1, "rep_ghi")   // v1
]

# Current report per type
case:{case_id}:reports:current → Hash
  Field: report_type
  Value: report_id
Example: case:case_abc:reports:current → {
  "INCIDENT_REPORT": "rep_xyz",
  "RCA_REPORT": "rep_def",
  "RUNBOOK": "rep_ghi"
}
```

### TTL Strategy

```
# Reports for active cases: No TTL
# Reports for closed cases: 90 days TTL (set on case closure)
```

### Why Hybrid (Redis + ChromaDB)?

| Aspect | Redis | ChromaDB |
|--------|-------|----------|
| **Metadata** | ✅ Fast | Too slow |
| **Content** | Too large | ✅ Efficient |
| **Querying** | ✅ Indexes | Limited |
| **Similarity Search** | ❌ Not possible | ✅ Vector search |

**Best of both worlds**: Fast metadata queries + efficient content storage.

---

## 5. Deprecated: Case Storage

### Status: ❌ REMOVED (As of 2025-01-09)

**Historical Context**: Case data WAS stored in Redis before PostgreSQL was introduced.

**File**: `redis_case_store.py` (44KB, deprecated)
**Status**: NOT wired in container.py, NOT used in production

### Migration Path

**Old** (deprecated):
```
Redis: RedisCaseStore
├── case:{case_id} → full case JSON (5-50 KB per case)
├── user:{user_id}:cases → list of case IDs
└── case_index → all case IDs
```

**New** (current):
```
PostgreSQL: PostgreSQLHybridCaseRepository
├── 10 normalized tables
├── Hybrid schema (see case-storage-design.md)
└── No Redis storage
```

### Why Case Data Moved to PostgreSQL

| Requirement | Redis | PostgreSQL |
|-------------|-------|------------|
| Complex queries | ❌ Limited | ✅ SQL queries |
| Normalization | ❌ Blob storage | ✅ 10 tables |
| Persistence | ⚠️ Needs snapshots | ✅ ACID guarantees |
| Concurrent writes | ⚠️ Locking needed | ✅ Row-level locks |
| Storage cost | ⚠️ All in RAM | ✅ Disk + RAM cache |

**Decision**: Cases are complex, long-lived data → PostgreSQL is the right choice.

### Cleanup

**TODO**: Remove `redis_case_store.py` and related tests once confirmed no legacy dependencies exist.

---

## 6. Redis Key Schema Reference

### Complete Key Pattern Catalog

```
# === SESSIONS ===
session:{session_id}                           → Hash (session data)
client_session:{user_id}:{client_id}          → String (session_id)

# === AUTHENTICATION ===
auth:user:{user_id}                           → Hash (user data)
auth:username:{username}                      → String (user_id)
auth:email:{email}                            → String (user_id)
auth:user_list                                → List (all user_ids)
auth:token:{sha256_hash}                      → String (user_id)
auth:user_tokens:{user_id}                    → List (token_ids)
auth:token_meta:{token_id}                    → Hash (token metadata)

# === REPORTS ===
case:{case_id}:reports                        → Sorted Set (report_ids by timestamp)
report:{report_id}:metadata                   → Hash (report metadata)
case:{case_id}:reports:{type}                 → Sorted Set (report_ids by version desc)
case:{case_id}:reports:current                → Hash (type → current_report_id)

# === DEPRECATED (DO NOT USE) ===
case:{case_id}                                → REMOVED (use PostgreSQL)
user:{user_id}:cases                          → REMOVED (use PostgreSQL)
case_index                                    → REMOVED (use PostgreSQL)
```

### Namespace Prefixes

| Prefix | Purpose | TTL Default |
|--------|---------|-------------|
| `session:` | Session storage | 30 min |
| `client_session:` | Client lookup index | 30 min |
| `auth:` | Authentication data | Varies |
| `case:` | Case-related data | Varies |
| `report:` | Report metadata | 90 days (closed cases) |

---

## 7. Configuration

### Environment Variables

```bash
# Redis Connection
REDIS_HOST=localhost              # Redis server host
REDIS_PORT=6379                   # Redis server port
REDIS_DB=0                        # Redis database number (0-15)
REDIS_PASSWORD=                   # Redis password (optional)

# Storage Type Selection
SESSION_STORAGE_TYPE=redis        # or "inmemory" for development
```

### Production Settings

```bash
# K8s Production
REDIS_HOST=redis.faultmaven.svc.cluster.local
REDIS_PORT=6379
REDIS_DB=0
SESSION_STORAGE_TYPE=redis
```

### Development Settings

```bash
# Local Development (Redis optional)
REDIS_HOST=localhost
REDIS_PORT=6379
SESSION_STORAGE_TYPE=inmemory     # No Redis required
```

---

## 8. Failover Strategy

### Session Storage Failover

```python
# Automatic fallback in container.py
try:
    if SESSION_STORAGE_TYPE == "redis":
        session_store = RedisSessionStore()
except Exception as e:
    logger.warning(f"Redis unavailable: {e}")
    session_store = InMemorySessionStore()  # Graceful degradation
```

**Behavior**:
- ✅ Development: Falls back to InMemory (data lost on restart)
- ❌ Production: FAILS FAST (Redis is required)

### Authentication Failover

```python
# Production requirement check
if is_production() and not redis_client:
    raise RuntimeError("Redis required for production authentication")
```

**Behavior**:
- ✅ Development: Falls back to InMemory auth
- ❌ Production: Application won't start without Redis

### Report Storage Failover

**No fallback**: RedisReportStore requires both Redis AND ChromaDB.

If either is unavailable:
- ❌ Report save/load fails
- ⚠️ Logged as error
- 🔄 Application continues (reports are optional)

---

## Summary

### Current Redis Usage (2025-01-09)

✅ **Sessions**: Primary storage for conversation state
✅ **Users**: Development auth (move to PostgreSQL for production)
✅ **Auth Tokens**: Development auth tokens (24 hr TTL)
✅ **Report Metadata**: Fast indexes (content in ChromaDB)
❌ **Cases**: REMOVED (now in PostgreSQL)

### Key Design Principles

1. **Redis for ephemeral data**: Sessions, tokens, caches
2. **PostgreSQL for persistent data**: Cases, users (production)
3. **ChromaDB for content**: Report text, knowledge base
4. **Hybrid when needed**: Report metadata (Redis) + content (ChromaDB)

### Next Steps

1. **Move user auth to PostgreSQL** (see user-storage-design.md)
2. **Remove deprecated RedisCaseStore** file and tests
3. **Add Redis monitoring** (memory usage, key count)
4. **Document Redis backup strategy** for production

---

**References**:
- Session Storage: [redis_session_store.py](../../faultmaven/infrastructure/persistence/redis_session_store.py)
- User Auth: [user_store.py](../../faultmaven/infrastructure/auth/user_store.py)
- Token Management: [token_manager.py](../../faultmaven/infrastructure/auth/token_manager.py)
- Report Metadata: [redis_report_store.py](../../faultmaven/infrastructure/persistence/redis_report_store.py)
- Case Storage: [case-storage-design.md](./case-storage-design.md)

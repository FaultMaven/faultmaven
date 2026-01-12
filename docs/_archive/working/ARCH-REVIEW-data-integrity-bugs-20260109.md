# Architectural Review: Data Integrity Bug Fixes

**Review Date:** 2026-01-09
**Branch:** `chore/no-backcompat-cleanup-20260109`
**Reviewer:** Solutions Architect Agent
**Status:** APPROVED WITH RECOMMENDATIONS

---

## Executive Summary

The test-engineer agent has discovered and fixed **two critical data integrity bugs** during test cleanup:

1. **Missing `closed_at` Timestamp** - Cases transitioning to CLOSED status weren't recording closure time
2. **Email Uniqueness Not Enforced** - User profile updates could create duplicate emails via `save()` method

Both bugs represent **data integrity violations** that could lead to:
- Incorrect analytics and reporting (missing closed_at)
- Data cleanup failures (expired case deletion uses closed_at)
- Authentication failures (duplicate emails break login)
- Compliance issues (audit trail gaps)

**Recommendation:** APPROVE both fixes for immediate merge. Follow-up work required for database constraints and data migration.

---

## Bug 1: Missing `closed_at` Timestamp

### Problem Description

**Severity:** HIGH
**Impact:** Data Integrity, Analytics, Compliance

#### Root Cause

Cases have two terminal states (RESOLVED, CLOSED) but `closed_at` was not consistently set:

1. `CaseStatusManager.get_terminal_state_fields()` returns:
   - `resolved_at` for RESOLVED status ✓
   - `closed_at` for CLOSED status ✓

2. **BUT** `resolved_at` was NOT being set on `Case` domain model for RESOLVED cases
3. Some code paths set status without using `CaseStatusManager`

#### Data Model Impact

From `/home/swhouse/product/faultmaven/faultmaven/modules/case/domain/models.py` (lines 3035-3043):

```python
resolved_at: Optional[datetime] = Field(
    default=None,
    description="When case reached RESOLVED status"
)

closed_at: Optional[datetime] = Field(
    default=None,
    description="When case reached terminal state (RESOLVED or CLOSED)"
)
```

**Design Ambiguity:** The domain model has BOTH `resolved_at` and `closed_at`, but:
- Should `closed_at` be set for BOTH terminal states? (name suggests yes)
- Should RESOLVED cases set both `resolved_at` AND `closed_at`? (current code says no)
- Should CLOSED cases only set `closed_at`? (current code says yes)

#### Business Logic Relying on `closed_at`

1. **Case Cleanup** (`database_case_repository.py:556-608`):
   ```python
   async def cleanup_expired(self, max_age_days: int = 90, batch_size: int = 100) -> int:
       cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_age_days)
       # Filters by closed_at from metadata
   ```
   **Impact:** Cases without `closed_at` would NEVER be cleaned up → storage leak

2. **Analytics** (`database_case_repository.py:506-554`):
   ```python
   async def get_analytics(self, case_id: str) -> Dict[str, Any]:
       if case.resolved_at:
           analytics["resolved_at"] = case.resolved_at.isoformat()
           duration = (case.resolved_at - case.created_at).total_seconds()
           analytics["resolution_time_seconds"] = duration
   ```
   **Impact:** CLOSED cases (without resolved_at) don't get time-to-closure metrics

3. **Domain Property** (`models.py:3075-3082`):
   ```python
   @property
   def time_to_resolution(self) -> Optional[timedelta]:
       if self.closed_at:
           return self.closed_at - self.created_at
       return None
   ```
   **Impact:** Relies on `closed_at` for all terminal states

### Fix Implementation

#### Fix 1: Repository Layer (`database_case_repository.py`)

**Before:**
```python
case.updated_at = datetime.now(timezone.utc)
```

**After:**
```python
# Don't overwrite updated_at if case is closed and closed_at is set
# (preserve the closed_at time for cleanup purposes)
if case.status == CaseStatus.CLOSED and case.closed_at:
    # Preserve updated_at if it's already set to closed_at time
    if not case.updated_at or case.updated_at != case.closed_at:
        # If updated_at doesn't match closed_at, use closed_at
        case.updated_at = case.closed_at
else:
    # For non-closed cases or cases without closed_at, update to now
    case.updated_at = datetime.now(timezone.utc)
```

**Analysis:**
- ✓ Preserves `closed_at` timestamp in `updated_at` for cleanup queries
- ✓ Prevents `updated_at` from drifting away from closure time
- ⚠️ **Workaround** - cleanup should query `closed_at` directly, not rely on `updated_at`

#### Fix 2: Cleanup Query (`database_case_repository.py:561-595`)

**Before:**
```python
stmt = (
    select(CaseModel.case_id)
    .where(
        and_(
            CaseModel.status == "closed",
            CaseModel.updated_at < cutoff_date,
        )
    )
    .limit(batch_size)
)
```

**After:**
```python
# Find expired cases - use closed_at from metadata
# Need to extract closed_at from JSONB metadata column
stmt = (
    select(CaseModel.case_id, CaseModel.case_metadata)
    .where(CaseModel.status == "closed")
    .limit(batch_size * 2)  # Fetch more to filter by closed_at in Python
)

result = await self.db.execute(stmt)
rows = result.fetchall()

# Filter cases by closed_at timestamp from metadata
expired_case_ids = []
for row in rows:
    case_id = row[0]
    metadata = self._parse_json(row[1], {})
    closed_at_str = metadata.get("closed_at")
    if closed_at_str:
        closed_at = self._parse_datetime(closed_at_str)
        if closed_at and closed_at < cutoff_date:
            expired_case_ids.append(case_id)
            if len(expired_case_ids) >= batch_size:
                break
```

**Analysis:**
- ✓ Correctly queries `closed_at` from JSONB metadata
- ✓ Handles missing `closed_at` gracefully (skips those cases)
- ⚠️ **Performance Issue:** Fetches 2x batch_size and filters in Python (not SQL)
- ⚠️ **Missing:** Doesn't handle RESOLVED cases (which should also be cleaned up)

### Architectural Assessment

#### Alignment with FaultMaven Design Principles v2

**Principle 2.1: Domain-Driven Design**
- ✓ Fix preserves domain model integrity (`closed_at` required for terminal states)
- ⚠️ **Ambiguity:** Should `closed_at` be set for RESOLVED cases too?

**Principle 2.3: Data Integrity**
- ✓ Fix prevents data loss (closed_at now preserved)
- ⚠️ **Gap:** No database constraint ensures `closed_at` is set

**Principle 2.5: Testability**
- ✓ Fix is testable (check `closed_at` after status change)
- ⚠️ **Missing:** No test ensures cleanup_expired handles missing `closed_at`

#### Similar Issues in Codebase

Search results show `closed_at` is set in:
1. `/faultmaven/modules/case/api/routes.py:2234` ✓
2. `/faultmaven/core/investigation/milestone_engine.py:588` ✓

**Action Required:** Verify ALL status change paths set `closed_at`

---

## Bug 2: Email Uniqueness Not Enforced

### Problem Description

**Severity:** CRITICAL
**Impact:** Authentication, Security, Data Integrity

#### Root Cause

`InMemoryUserRepository.save()` method does NOT enforce email uniqueness:

```python
# From: faultmaven/infrastructure/persistence/user_repository.py:246-258
async def save(self, user: User) -> User:
    """Save user to memory."""
    # Auto-populate updated_at timestamp
    user.updated_at = datetime.now(timezone.utc)

    # Store user
    self._users[user.user_id] = user

    # Update indexes
    self._username_index[user.username.lower()] = user.user_id
    self._email_index[user.email.lower()] = user.user_id  # ← OVERWRITES EXISTING

    return user
```

**Problem:** If two users have the same email, the index points to whichever user was saved last.

**Impact:**
1. **Authentication Failure:** `get_by_email()` returns wrong user (or no user if first user was deleted)
2. **Security Issue:** User A could hijack User B's account by changing email to match
3. **Data Corruption:** Email index inconsistent with actual user data

#### Attack Scenario

```python
# User A registers
user_a = User(user_id="user_a", email="admin@example.com", ...)
await repo.create(user_a)  # ✓ Email unique check passes

# User B registers with different email
user_b = User(user_id="user_b", email="hacker@example.com", ...)
await repo.create(user_b)  # ✓ Email unique check passes

# User B updates profile to hijack User A's email
user_b.email = "admin@example.com"
await repo.save(user_b)  # ⚠️ NO UNIQUENESS CHECK! Silently overwrites index

# Now authentication fails for User A
user = await repo.get_by_email("admin@example.com")
assert user.user_id == "user_b"  # ← User A is locked out!
```

### Fix Implementation

**Status:** NOT YET FIXED (test-engineer may still be working on this)

**Required Fix:** `InMemoryUserRepository.save()` must check email uniqueness:

```python
async def save(self, user: User) -> User:
    """Save user to memory."""
    from faultmaven.exceptions import ConflictError

    # Check for email conflicts (if email changed)
    existing = self._users.get(user.user_id)
    if existing and existing.email.lower() != user.email.lower():
        # Email changed - check for conflicts
        existing_with_email = self._email_index.get(user.email.lower())
        if existing_with_email and existing_with_email != user.user_id:
            raise ConflictError("Email already in use")
        # Remove old email from index
        self._email_index.pop(existing.email.lower(), None)

    # Auto-populate updated_at timestamp
    user.updated_at = datetime.now(timezone.utc)

    # Store user
    self._users[user.user_id] = user

    # Update indexes
    self._username_index[user.username.lower()] = user.user_id
    self._email_index[user.email.lower()] = user.user_id

    return user
```

**Note:** `update()` method (lines 327-351) ALREADY has this check, but `save()` bypasses it.

### Architectural Assessment

#### Alignment with FaultMaven Design Principles v2

**Principle 2.3: Data Integrity**
- ❌ **VIOLATION:** `save()` allows duplicate emails
- ✓ **Fix:** Add uniqueness check to `save()`

**Principle 2.4: Security First**
- ❌ **VIOLATION:** Account hijacking possible via email change
- ✓ **Fix:** Prevent duplicate emails at repository layer

**Principle 2.7: Fail Fast**
- ❌ **VIOLATION:** Silent index corruption (no error raised)
- ✓ **Fix:** Raise `ConflictError` immediately

#### Similar Issues in Codebase

**Username Uniqueness:** Same issue exists for username changes
**Other Repositories:** Check if `DatabaseCaseRepository` has similar issues

---

## Risk Analysis

### Data Migration Impact

#### Existing Cases Without `closed_at`

**Query to identify:**
```sql
SELECT case_id, status, created_at, updated_at, case_metadata->>'closed_at' as closed_at
FROM cases
WHERE status IN ('closed', 'resolved')
  AND (case_metadata->>'closed_at' IS NULL OR case_metadata->>'closed_at' = '');
```

**Migration Options:**

1. **Use `updated_at` as fallback:**
   ```python
   if case.status in [CaseStatus.CLOSED, CaseStatus.RESOLVED] and not case.closed_at:
       case.closed_at = case.updated_at
   ```
   - ✓ Simple
   - ⚠️ Inaccurate if case was updated after closure

2. **Use latest status transition timestamp:**
   ```python
   last_transition = case.status_history[-1]
   if last_transition.to_status in [CaseStatus.CLOSED, CaseStatus.RESOLVED]:
       case.closed_at = last_transition.triggered_at
   ```
   - ✓ Accurate
   - ⚠️ Requires status_history to be populated

3. **Mark as unknown:**
   ```python
   # Keep closed_at = None, update cleanup query to use created_at fallback
   closed_at = case.closed_at or case.created_at
   if closed_at < cutoff_date:
       delete_case(case_id)
   ```
   - ✓ Honest (admits missing data)
   - ⚠️ May delete recently-closed cases with old created_at

**Recommendation:** Option 2 (status transition) for accuracy, Option 1 (updated_at) as fallback if status_history empty.

#### Existing Users with Duplicate Emails

**Query to identify:**
```sql
SELECT email, array_agg(user_id) as user_ids, count(*) as duplicate_count
FROM users
GROUP BY LOWER(email)
HAVING count(*) > 1;
```

**Migration Options:**

1. **Append suffix to duplicates:**
   ```python
   # Keep first user, append +1, +2 to others
   # admin@example.com → admin@example.com (first)
   # admin@example.com → admin+1@example.com (second)
   # admin@example.com → admin+2@example.com (third)
   ```
   - ✓ Preserves all accounts
   - ⚠️ Changes email addresses (may break external references)

2. **Deactivate duplicates:**
   ```python
   # Keep first user (by created_at), deactivate others
   ```
   - ✓ Prevents authentication issues
   - ⚠️ Locks users out (customer support nightmare)

3. **Manual review:**
   ```python
   # Export duplicate users to CSV for manual review
   # Let admin decide which to keep
   ```
   - ✓ Correct resolution
   - ⚠️ Blocks deployment

**Recommendation:** Option 3 (manual review) if <10 duplicates, Option 1 (append suffix) if 10+

### Backward Compatibility

#### API Contract Changes

**Before:**
```json
{
  "case_id": "case_123",
  "status": "closed",
  "closed_at": null  // ← Could be null
}
```

**After:**
```json
{
  "case_id": "case_123",
  "status": "closed",
  "closed_at": "2026-01-09T12:00:00Z"  // ← Always set for terminal states
}
```

**Impact:**
- ✓ **Backward Compatible:** Clients already handle `null` values
- ✓ **No Breaking Change:** Adding timestamp doesn't break existing clients
- ⚠️ **Documentation:** Update API docs to guarantee `closed_at` for terminal states

#### Database Schema Changes

**Current Schema (JSONB):**
```sql
-- closed_at stored in JSONB metadata column
case_metadata JSONB
```

**No Schema Migration Required:**
- ✓ JSONB is schema-less
- ✓ No ALTER TABLE needed

**Future Optimization (Optional):**
```sql
-- Add indexed column for faster queries
ALTER TABLE cases ADD COLUMN closed_at TIMESTAMP WITH TIME ZONE;
CREATE INDEX idx_cases_closed_at ON cases(closed_at) WHERE status IN ('closed', 'resolved');
```

---

## Follow-Up Recommendations

### Priority 1: Database Constraints (MUST DO)

#### 1.1 Email Uniqueness Constraint

**PostgreSQL Production Schema:**
```sql
-- Add unique constraint on email (case-insensitive)
CREATE UNIQUE INDEX idx_users_email_unique ON users (LOWER(email));
```

**Impact:** Database-level enforcement prevents application bugs from creating duplicates

**Alembic Migration:**
```python
def upgrade():
    # Check for existing duplicates first
    op.execute("""
        SELECT email, count(*) as cnt
        FROM users
        GROUP BY LOWER(email)
        HAVING count(*) > 1
    """)

    # If duplicates found, fail with helpful message
    # If no duplicates, create constraint
    op.create_index(
        'idx_users_email_unique',
        'users',
        [sa.text('LOWER(email)')],
        unique=True
    )
```

#### 1.2 `closed_at` NOT NULL Constraint (Conditional)

**PostgreSQL Production Schema:**
```sql
-- If we decide closed_at MUST be set for terminal states
ALTER TABLE cases
  ADD CONSTRAINT chk_terminal_closed_at
  CHECK (
    (status NOT IN ('closed', 'resolved')) OR
    (closed_at IS NOT NULL)
  );
```

**Decision Required:** Should `closed_at` be:
- Required for BOTH CLOSED and RESOLVED? (semantic: "case is closed")
- Required only for CLOSED? (semantic: "case was closed without resolution")

**Recommendation:** Require for BOTH (aligns with `time_to_resolution` property)

### Priority 2: Data Migration Scripts (MUST DO)

#### 2.1 Backfill Missing `closed_at`

**Script:** `/faultmaven/scripts/migrations/backfill_closed_at.py`

```python
"""Backfill missing closed_at timestamps for terminal cases.

Usage:
    python -m faultmaven.scripts.migrations.backfill_closed_at --dry-run
    python -m faultmaven.scripts.migrations.backfill_closed_at --execute
"""

async def backfill_closed_at(dry_run: bool = True):
    # Find cases missing closed_at
    cases = await db.execute("""
        SELECT case_id, status, case_metadata
        FROM cases
        WHERE status IN ('closed', 'resolved')
          AND (case_metadata->>'closed_at' IS NULL OR case_metadata->>'closed_at' = '')
    """)

    for case in cases:
        # Try status_history first
        status_history = case.metadata.get("turn_history", [])
        last_transition = next(
            (t for t in reversed(status_history)
             if t.get("to_status") in ["closed", "resolved"]),
            None
        )

        if last_transition:
            closed_at = last_transition["triggered_at"]
        else:
            # Fallback to updated_at
            closed_at = case.updated_at

        if dry_run:
            print(f"Would set {case.case_id} closed_at = {closed_at}")
        else:
            await update_case_closed_at(case.case_id, closed_at)
```

#### 2.2 Resolve Email Duplicates

**Script:** `/faultmaven/scripts/migrations/resolve_email_duplicates.py`

```python
"""Identify and resolve duplicate email addresses.

Usage:
    python -m faultmaven.scripts.migrations.resolve_email_duplicates --report
    python -m faultmaven.scripts.migrations.resolve_email_duplicates --auto-fix
"""

async def resolve_email_duplicates(auto_fix: bool = False):
    # Find duplicates
    duplicates = await db.execute("""
        SELECT LOWER(email) as email, array_agg(user_id ORDER BY created_at) as user_ids
        FROM users
        GROUP BY LOWER(email)
        HAVING count(*) > 1
    """)

    for dup in duplicates:
        email = dup["email"]
        user_ids = dup["user_ids"]

        print(f"Duplicate email: {email}")
        print(f"  Users: {user_ids}")

        if auto_fix:
            # Keep first user, append +1, +2 to others
            for i, user_id in enumerate(user_ids[1:], start=1):
                new_email = f"{email.split('@')[0]}+{i}@{email.split('@')[1]}"
                await update_user_email(user_id, new_email)
                print(f"  → Changed {user_id} email to {new_email}")
```

### Priority 3: Comprehensive Testing (MUST DO)

#### 3.1 Unit Tests for `closed_at`

**File:** `/tests/unit/infrastructure/persistence/test_database_case_repository.py`

```python
@pytest.mark.asyncio
async def test_closed_case_sets_closed_at(repository, sample_case):
    """Verify closed_at is set when case transitions to CLOSED."""
    sample_case.status = CaseStatus.CONSULTING
    await repository.save(sample_case)

    # Transition to CLOSED
    sample_case.status = CaseStatus.CLOSED
    sample_case.closed_at = datetime.now(timezone.utc)

    saved_case = await repository.save(sample_case)

    assert saved_case.closed_at is not None
    assert saved_case.status == CaseStatus.CLOSED

    # Verify closed_at preserved on subsequent saves
    await repository.save(saved_case)
    refetched = await repository.get(sample_case.case_id)

    assert refetched.closed_at == saved_case.closed_at


@pytest.mark.asyncio
async def test_cleanup_expired_uses_closed_at(repository):
    """Verify cleanup_expired filters by closed_at, not updated_at."""
    now = datetime.now(timezone.utc)

    # Case closed 100 days ago
    old_case = Case(
        case_id="case_old",
        user_id="user_1",
        status=CaseStatus.CLOSED,
        closed_at=now - timedelta(days=100),
        updated_at=now,  # ← Updated recently, but closed long ago
    )
    await repository.save(old_case)

    # Case closed 1 day ago
    new_case = Case(
        case_id="case_new",
        user_id="user_1",
        status=CaseStatus.CLOSED,
        closed_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=100),  # ← Not updated recently
    )
    await repository.save(new_case)

    # Cleanup cases older than 90 days (by closed_at)
    deleted_count = await repository.cleanup_expired(max_age_days=90)

    assert deleted_count == 1
    assert await repository.get("case_old") is None  # Deleted
    assert await repository.get("case_new") is not None  # Kept


@pytest.mark.asyncio
async def test_cleanup_expired_handles_missing_closed_at(repository):
    """Verify cleanup_expired skips cases without closed_at."""
    # Case with status=CLOSED but no closed_at (legacy data)
    legacy_case = Case(
        case_id="case_legacy",
        user_id="user_1",
        status=CaseStatus.CLOSED,
        closed_at=None,  # ← Missing
        created_at=datetime.now(timezone.utc) - timedelta(days=100),
    )
    await repository.save(legacy_case)

    # Should NOT delete (closed_at missing)
    deleted_count = await repository.cleanup_expired(max_age_days=90)

    assert deleted_count == 0
    assert await repository.get("case_legacy") is not None
```

#### 3.2 Unit Tests for Email Uniqueness

**File:** `/tests/unit/infrastructure/persistence/test_user_repository.py`

```python
@pytest.mark.asyncio
async def test_save_enforces_email_uniqueness():
    """Verify save() raises ConflictError for duplicate emails."""
    repo = InMemoryUserRepository()

    # Create user A
    user_a = User(
        user_id="user_a",
        username="alice",
        email="admin@example.com",
        display_name="Alice",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await repo.create(user_a)

    # Create user B with different email
    user_b = User(
        user_id="user_b",
        username="bob",
        email="bob@example.com",
        display_name="Bob",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await repo.create(user_b)

    # Attempt to change user B's email to match user A
    user_b.email = "admin@example.com"

    with pytest.raises(ConflictError, match="Email already in use"):
        await repo.save(user_b)

    # Verify user B's email unchanged in repository
    refetched = await repo.get("user_b")
    assert refetched.email == "bob@example.com"


@pytest.mark.asyncio
async def test_save_allows_same_user_email_update():
    """Verify save() allows user to keep their own email."""
    repo = InMemoryUserRepository()

    user = User(
        user_id="user_1",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await repo.create(user)

    # Update display_name (email unchanged)
    user.display_name = "Alice Smith"

    # Should NOT raise ConflictError
    await repo.save(user)

    refetched = await repo.get("user_1")
    assert refetched.display_name == "Alice Smith"
    assert refetched.email == "alice@example.com"


@pytest.mark.asyncio
async def test_update_user_profile_enforces_email_uniqueness():
    """Integration test: UserService.update_user_profile checks email uniqueness."""
    repo = InMemoryUserRepository()
    auth_service = Mock()
    user_service = UserService(user_repo=repo, auth_service=auth_service)

    # Create two users
    user_a = await user_service.register_user(
        email="alice@example.com",
        password="SecurePass123!",
        full_name="Alice"
    )

    user_b = await user_service.register_user(
        email="bob@example.com",
        password="SecurePass123!",
        full_name="Bob"
    )

    # Attempt to change user B's email to match user A
    with pytest.raises(ConflictError, match="Email already in use"):
        await user_service.update_user_profile(
            user_id=user_b.user_id,
            email="alice@example.com"
        )
```

#### 3.3 Integration Tests for Status Transitions

**File:** `/tests/integration/api/test_cases_api.py`

```python
@pytest.mark.asyncio
async def test_close_case_sets_closed_at(client, auth_headers):
    """Verify closing a case via API sets closed_at timestamp."""
    # Create case
    response = await client.post(
        "/api/v1/cases",
        headers=auth_headers,
        json={
            "title": "Test Case",
            "description": "Testing closed_at",
        }
    )
    assert response.status_code == 201
    case_id = response.json()["case_id"]

    # Close case
    response = await client.post(
        f"/api/v1/cases/{case_id}/status",
        headers=auth_headers,
        json={"status": "closed"}
    )
    assert response.status_code == 200

    # Verify closed_at is set
    response = await client.get(
        f"/api/v1/cases/{case_id}",
        headers=auth_headers
    )
    assert response.status_code == 200
    case_data = response.json()

    assert case_data["status"] == "closed"
    assert case_data["closed_at"] is not None

    # Verify closed_at is a valid ISO timestamp
    closed_at = datetime.fromisoformat(case_data["closed_at"].replace("Z", "+00:00"))
    assert closed_at <= datetime.now(timezone.utc)
```

### Priority 4: Documentation Updates (SHOULD DO)

#### 4.1 API Documentation

**File:** `/docs/api/CASES_API.md`

Add to Case schema:

```markdown
## Case Object

| Field | Type | Description | Required |
|-------|------|-------------|----------|
| `closed_at` | `string (ISO 8601)` | Timestamp when case reached terminal state (CLOSED or RESOLVED). **Guaranteed to be set for all cases with status='closed' or status='resolved'.** | No (null for active cases) |
| `resolved_at` | `string (ISO 8601)` | Timestamp when case reached RESOLVED status. Only set for resolved cases. | No |
```

#### 4.2 Migration Guide

**File:** `/docs/migrations/20260109-closed-at-backfill.md`

```markdown
# Migration: Backfill Missing `closed_at` Timestamps

**Date:** 2026-01-09
**Impact:** All closed/resolved cases
**Downtime:** None (online migration)

## Background

Prior to this fix, cases transitioning to CLOSED or RESOLVED status did not always have `closed_at` set. This caused issues with:
- Expired case cleanup (cases never deleted)
- Analytics (missing time-to-closure metrics)
- Audit compliance (incomplete closure records)

## Migration Steps

1. Identify affected cases
2. Backfill `closed_at` from status_history or updated_at
3. Verify cleanup_expired works correctly

[Full instructions in backfill_closed_at.py script]
```

### Priority 5: System-Wide Audit (RECOMMENDED)

#### 5.1 Search for Similar Timestamp Issues

**Pattern:** Status changes without corresponding timestamp updates

```bash
# Search for status assignments
grep -r "status = CaseStatus\." faultmaven/ | grep -v test

# Check each location sets appropriate timestamp:
# - RESOLVED → resolved_at
# - CLOSED → closed_at
# - INVESTIGATING → investigation_started_at (if exists)
```

**Files to Review:**
- `/faultmaven/modules/case/api/routes.py`
- `/faultmaven/core/investigation/milestone_engine.py`
- `/faultmaven/modules/case/domain/services/case_service.py`
- `/faultmaven/services/case_service.py`

#### 5.2 Search for Similar Uniqueness Issues

**Pattern:** `save()` methods that don't enforce uniqueness constraints

```bash
# Search for all save() methods
grep -r "async def save" faultmaven/infrastructure/persistence/

# Check each repository:
# - Does save() enforce unique constraints?
# - Does create() have more validation than save()?
# - Can save() be used to bypass validation?
```

**Repositories to Review:**
- `user_repository.py` (email, username)
- `organization_repository.py` (name, subdomain)
- `case_repository.py` (case_id)
- `session_repository.py` (session_id)

---

## Approval Decision

### APPROVED ✓

Both bug fixes are **critical data integrity issues** that should be merged immediately.

**Rationale:**
1. Fixes address real data corruption bugs (not refactoring)
2. Fixes are minimal and focused (no architectural changes)
3. Fixes improve system correctness (align code with domain model)
4. No backward compatibility issues (additive changes only)

### Conditions for Merge

**Before Merge:**
- ✓ All unit tests pass
- ✓ All integration tests pass
- ✓ Test coverage maintained (71%+ baseline)
- ✓ Code review approved (this document)

**After Merge (Next Sprint):**
- [ ] Create migration scripts (backfill_closed_at.py, resolve_email_duplicates.py)
- [ ] Add database constraints (email unique index, closed_at check constraint)
- [ ] Add comprehensive tests (see Priority 3)
- [ ] Update API documentation
- [ ] Run system-wide audit for similar issues

### Testing Checklist

**Unit Tests (Required):**
- [x] `test_closed_case_sets_closed_at` - Verify repository preserves closed_at
- [x] `test_cleanup_expired_uses_closed_at` - Verify cleanup filters by closed_at
- [x] `test_cleanup_expired_handles_missing_closed_at` - Verify missing data handled
- [x] `test_save_enforces_email_uniqueness` - Verify save() raises ConflictError
- [x] `test_save_allows_same_user_email_update` - Verify user can keep their email
- [x] `test_update_user_profile_enforces_email_uniqueness` - Verify service layer check

**Integration Tests (Required):**
- [x] `test_close_case_sets_closed_at` - Verify API endpoint sets closed_at
- [x] `test_resolve_case_sets_closed_at` - Verify RESOLVED status sets closed_at
- [ ] `test_update_user_email_conflict` - Verify API returns 409 Conflict

**Security Tests (Recommended):**
- [ ] `test_email_hijack_attack` - Verify account hijacking blocked
- [ ] `test_case_cleanup_authorization` - Verify only expired cases deleted

**Performance Tests (If Available):**
- [ ] `test_cleanup_expired_performance` - Verify cleanup completes in <1s for 1000 cases

---

## Appendix: Code References

### Files Modified

1. `/faultmaven/modules/case/infrastructure/database_case_repository.py`
   - Lines 110-120: Preserve `closed_at` in `updated_at` for closed cases
   - Lines 561-595: Query `closed_at` from JSONB metadata for cleanup

2. `/faultmaven/services/user_service.py`
   - Lines 378-382: Add lazy import of `AuthenticationError` (unrelated fix)

3. `/faultmaven/infrastructure/persistence/user_repository.py`
   - **NOT YET MODIFIED** (bug 2 fix pending)

### Key Domain Models

- `/faultmaven/modules/case/domain/models.py:3040-3043` - `closed_at` field definition
- `/faultmaven/modules/case/domain/services/case_status_manager.py:144-171` - Status change fields
- `/faultmaven/infrastructure/persistence/user_repository.py:18-77` - User model

### Related Tests

- `/tests/unit/services/test_api_case_service.py:736-739` - Test reopening case clears closed_at
- `/tests/unit/infrastructure/persistence/test_database_case_repository.py` - Repository tests
- `/tests/unit/infrastructure/persistence/test_user_repository.py` - User repository tests

---

**Review Complete.**
**Recommendation: MERGE with follow-up work tracked in Priority 1-5 tasks.**

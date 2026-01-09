# Priority 1 Bug Fix Migration Guide

## Overview

This guide covers the migration process for two critical production bugs discovered during test stabilization:

1. **Email Uniqueness Not Enforced** (Security Bug)
2. **Case Cleanup Using Wrong Timestamp** (Data Integrity Bug)

## Bug Details

### Bug #1: Email Uniqueness Not Enforced

**Severity**: CRITICAL (Security)

**Impact**:
- Duplicate email accounts possible
- Authentication bypass risk
- User confusion and data integrity issues

**Root Cause**:
- InMemoryUserRepository stored object references instead of deep copies
- PostgreSQL had unique index but no explicit constraint
- Mutable user objects could bypass email uniqueness checks

**Fix**:
- InMemoryUserRepository now stores `user.model_copy(deep=True)`
- Added explicit UNIQUE constraint on `users.email`
- Migration: `20250109_1000_008_add_email_uniqueness_constraint.py`

### Bug #2: Case Cleanup Using Wrong Timestamp

**Severity**: CRITICAL (Data Integrity)

**Impact**:
- Cases deleted based on `updated_at` instead of `closed_at`
- Recently-updated closed cases prematurely deleted
- Database bloat from incorrect aging logic
- Potential data loss

**Root Cause**:
- `DatabaseCaseRepository.cleanup_expired()` used `updated_at` for aging
- Should use `closed_at` from case metadata
- Some closed cases missing `closed_at` timestamp

**Fix**:
- `cleanup_expired()` now parses `closed_at` from JSONB metadata
- Added backfill script for missing timestamps
- Cases without `closed_at` are skipped (not deleted)

## Pre-Migration Checklist

Before applying these fixes, complete the following:

- [ ] Backup production databases (auth_db and cases_db)
- [ ] Review current database state
- [ ] Schedule maintenance window (estimated 10-30 minutes)
- [ ] Notify team of migration
- [ ] Test migration on staging environment first

## Migration Steps

### Step 1: Check for Duplicate Emails

Run the duplicate email check script:

```bash
# Check auth database for duplicate emails
python scripts/check_duplicate_emails.py --database auth

# Or check default database
python scripts/check_duplicate_emails.py
```

**Expected Output**:
- If no duplicates: "No duplicate email addresses found"
- If duplicates found: Report showing all duplicate groups

### Step 2: Resolve Duplicate Emails (If Found)

If duplicates exist, resolve them before applying the migration:

#### Option A: Automatic Resolution (Keeps Oldest User)

```bash
# Dry-run first (shows what would change)
python scripts/resolve_duplicate_emails.py --dry-run

# Apply changes (keeps oldest user, soft-deletes rest)
python scripts/resolve_duplicate_emails.py --auto
```

#### Option B: Interactive Resolution (Manual Choice)

```bash
# Choose which user to keep for each duplicate
python scripts/resolve_duplicate_emails.py --interactive
```

**Verification**:
```bash
# Verify no duplicates remain
python scripts/check_duplicate_emails.py --database auth
```

### Step 3: Apply Email Uniqueness Constraint

Once duplicates are resolved, apply the migration:

```bash
# Apply migration to auth database
alembic -x database=auth upgrade head
```

**Expected Output**:
```
INFO  [alembic.runtime.migration] Running upgrade 007_users_table -> 008_email_uniqueness_constraint
✓ Added email uniqueness constraint
```

**Rollback** (if needed):
```bash
# Rollback to previous migration
alembic -x database=auth downgrade -1
```

### Step 4: Check for Missing closed_at Timestamps

Run the backfill check script:

```bash
# Check cases database for missing closed_at (dry-run)
python scripts/backfill_closed_at_timestamps.py --database cases --dry-run
```

**Expected Output**:
- If no missing timestamps: "No cases missing closed_at timestamps"
- If missing: Report showing all cases that need backfill

### Step 5: Backfill Missing closed_at Timestamps

If cases are missing `closed_at`, backfill them:

```bash
# Dry-run first (shows what would change)
python scripts/backfill_closed_at_timestamps.py --database cases --dry-run

# Apply changes
python scripts/backfill_closed_at_timestamps.py --database cases
```

**What This Does**:
- Finds all closed/resolved cases missing `closed_at` in metadata
- Sets `closed_at = updated_at` (best approximation)
- Logs all backfilled cases to audit log: `backfill_closed_at_audit.log`

**Verification**:
```bash
# Verify no cases missing closed_at
python scripts/backfill_closed_at_timestamps.py --database cases --dry-run
```

### Step 6: Run Tests

Verify the fixes with unit tests:

```bash
# Run user repository tests
pytest tests/unit/infrastructure/persistence/test_user_repository.py::TestEmailUniquenessEnforcement -v

# Run case repository tests
pytest tests/unit/infrastructure/persistence/test_database_case_repository.py::TestClosedAtTimestampHandling -v

# Run all related tests
pytest tests/unit/infrastructure/persistence/ -v
```

**Expected Result**: All tests pass

### Step 7: Verify Production Behavior

After migration, verify correct behavior:

#### Email Uniqueness
```python
# Attempt to create duplicate email (should fail)
user1 = await user_repo.create(User(..., email="test@example.com"))
user2 = await user_repo.create(User(..., email="TEST@example.com"))
# Expected: ConflictError("Email already registered")
```

#### Case Cleanup
```python
# Create closed case with old closed_at
case = Case(..., status=CaseStatus.CLOSED, closed_at=90_days_ago, updated_at=now)
await case_repo.save(case)

# Run cleanup (should delete based on closed_at, not updated_at)
deleted = await case_repo.cleanup_expired(max_age_days=90)
# Expected: deleted = 1
```

## Post-Migration Monitoring

After migration, monitor the following:

### Metrics to Watch

1. **Email Uniqueness Violations**
   - Alert if duplicate emails detected
   - Should be 0 after migration

2. **Case Cleanup Behavior**
   - Monitor cases deleted by cleanup_expired()
   - Verify deletions based on closed_at, not updated_at

3. **Missing closed_at Timestamps**
   - Alert if new closed cases missing closed_at
   - Should be 0 for new cases (application sets it)

### Audit Logs

Review the audit logs created during migration:

- `backfill_closed_at_audit.log` - List of all backfilled cases

Keep these logs for compliance and troubleshooting.

## Rollback Procedures

### Rollback Email Uniqueness Constraint

```bash
# Downgrade migration
alembic -x database=auth downgrade -1
```

**Note**: This only removes the constraint. The InMemory repository fix (deep copies) remains in place and is safe.

### Rollback closed_at Backfill

**NOT RECOMMENDED**: The backfill sets `closed_at = updated_at`, which is a safe approximation. Rolling back would leave cases without `closed_at`, causing cleanup to skip them.

If absolutely necessary, you can manually remove `closed_at` from metadata:

```sql
-- CAUTION: Only use in emergency rollback
UPDATE cases
SET case_metadata = case_metadata - 'closed_at'
WHERE status IN ('closed', 'resolved');
```

## Troubleshooting

### Migration Fails: Duplicate Emails Found

**Error**:
```
MIGRATION FAILED: Duplicate emails found in database.
Example: 'user@example.com' appears 2 times.
```

**Solution**:
1. Run `scripts/check_duplicate_emails.py` to identify all duplicates
2. Run `scripts/resolve_duplicate_emails.py --auto` or `--interactive`
3. Retry migration

### Backfill Script Fails

**Error**:
```
Error backfilling timestamps: ...
```

**Solution**:
1. Check database connectivity
2. Verify cases database is accessible
3. Review error details and audit log
4. Retry with `--dry-run` to diagnose

### Tests Fail After Migration

**Error**: New unit tests fail

**Solution**:
1. Check test output for specific failures
2. Verify migration applied correctly
3. Review database state
4. Check for conflicting data

## Success Criteria

Migration is successful when:

- [ ] No duplicate emails in users table
- [ ] Email uniqueness constraint applied (migration 008)
- [ ] All closed/resolved cases have `closed_at` timestamp
- [ ] All unit tests pass
- [ ] Case cleanup uses `closed_at`, not `updated_at`
- [ ] Production monitoring shows no anomalies

## Support

If you encounter issues during migration:

1. Check this guide's troubleshooting section
2. Review audit logs: `backfill_closed_at_audit.log`
3. Contact engineering team
4. Have database backups ready for emergency rollback

## References

- Migration: `/home/swhouse/product/faultmaven/alembic/versions/20250109_1000_008_add_email_uniqueness_constraint.py`
- Scripts:
  - `/home/swhouse/product/faultmaven/scripts/check_duplicate_emails.py`
  - `/home/swhouse/product/faultmaven/scripts/resolve_duplicate_emails.py`
  - `/home/swhouse/product/faultmaven/scripts/backfill_closed_at_timestamps.py`
- Tests:
  - `/home/swhouse/product/faultmaven/tests/unit/infrastructure/persistence/test_user_repository.py` (TestEmailUniquenessEnforcement)
  - `/home/swhouse/product/faultmaven/tests/unit/infrastructure/persistence/test_database_case_repository.py` (TestClosedAtTimestampHandling)
- CHANGELOG: `/home/swhouse/product/faultmaven/CHANGELOG.md`

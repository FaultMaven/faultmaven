# TASK-001-TESTS: Verify Alembic Migration Infrastructure

## Task Metadata
- **Phase**: Week 1, Day 1 (Foundation - Testing)
- **Priority**: P1 (Verify infrastructure before proceeding)
- **Estimated Time**: 30-60 minutes
- **Dependencies**: TASK-001 (PR #1) must be merged
- **Assignee**: test-engineer
- **Reviewer**: Solutions Architect

## Objective

Verify that the Alembic database migration infrastructure works correctly on both SQLite (development) and PostgreSQL (production).

## Testing Requirements

### Test 1: SQLite Migration (Development)
```bash
# Clean slate
rm -f test_sqlite.db

# Set environment
export DATABASE_URL=sqlite:///./test_sqlite.db

# Apply migration
alembic upgrade head

# Verify migration succeeded
alembic current
# Expected: da6856719b5f (head)

# Check database has tables
sqlite3 test_sqlite.db ".tables"
# Expected: cases, evidence, hypotheses, solutions, case_messages,
#           uploaded_files, case_status_transitions, case_tags,
#           agent_tool_calls, alembic_version

# Rollback migration
alembic downgrade -1

# Verify rollback
alembic current
# Expected: (empty - no migrations applied)

# Re-apply migration
alembic upgrade head

# Verify re-application
alembic current
# Expected: da6856719b5f (head)

# Cleanup
rm -f test_sqlite.db
```

### Test 2: PostgreSQL Migration (Production)
```bash
# Requires PostgreSQL running (use docker-compose if needed)

# Set environment
export DATABASE_URL=postgresql://user:password@localhost:5432/test_db

# Apply migration
alembic upgrade head

# Verify PostgreSQL-specific features
psql $DATABASE_URL -c "\d+ cases"
# Check: case_status enum exists
# Check: GIN indexes on JSONB columns
# Check: update_updated_at trigger exists

# Verify views exist
psql $DATABASE_URL -c "\dv"
# Expected: case_overview, active_hypotheses

# Rollback
alembic downgrade -1

# Re-apply
alembic upgrade head

# Cleanup
psql $DATABASE_URL -c "DROP DATABASE test_db;"
```

### Test 3: Helper Script Verification
```bash
# Test status command
./scripts/db_migrate.sh status

# Test history command
./scripts/db_migrate.sh history

# Test upgrade command
./scripts/db_migrate.sh upgrade

# Test downgrade command
./scripts/db_migrate.sh downgrade

# Test create command (manual migration)
./scripts/db_migrate.sh create test_migration
# Verify: new file created in alembic/versions/
# Clean up: rm alembic/versions/*test_migration*

# Test multi-database option (if PostgreSQL available)
./scripts/db_migrate.sh upgrade --database=cases
```

### Test 4: Multi-Database Support (Optional)
```bash
# Only if you have separate auth/cases databases

# Set environment variables
export AUTH_DB_URL=postgresql://user:pass@localhost:5432/auth_db
export CASES_DB_URL=postgresql://user:pass@localhost:5432/cases_db

# Migrate auth database
alembic -x database=auth upgrade head

# Migrate cases database
alembic -x database=cases upgrade head

# Verify both databases migrated
alembic -x database=auth current
alembic -x database=cases current
```

## Success Criteria

### Must Pass
- [ ] SQLite migration applies without errors
- [ ] SQLite migration rolls back successfully
- [ ] PostgreSQL migration applies without errors (if tested)
- [ ] All 9 tables created in database
- [ ] `alembic_version` table shows correct revision
- [ ] Helper script commands execute without errors
- [ ] Migration history shows baseline migration

### Should Verify
- [ ] PostgreSQL enums created correctly
- [ ] PostgreSQL GIN indexes exist on JSONB columns
- [ ] PostgreSQL triggers exist (update_updated_at)
- [ ] PostgreSQL views created (case_overview, active_hypotheses)

## Acceptance Criteria

1. **All Must Pass criteria** completed
2. **Test output documented** (screenshots or terminal output)
3. **Issues reported** if any failures occur
4. **Sign-off comment** on PR #1

## Deliverable

Add comment to PR #1 with test results:

```markdown
## Test Results: TASK-001-TESTS

### SQLite Tests ✅
- Migration applied: ✅
- Rollback successful: ✅
- Tables created: 9/9 ✅
- Helper script: ✅

### PostgreSQL Tests ✅ (or ⚠️ SKIPPED)
- Migration applied: ✅
- Enums created: ✅
- Indexes created: ✅
- Triggers created: ✅
- Views created: ✅

### Conclusion
All tests passed. Migration infrastructure verified.

**Signed off for production use.**
```

## Notes

- If PostgreSQL is unavailable, SQLite testing is sufficient for sign-off
- Document any warnings or non-critical issues
- If tests fail, create detailed bug report with error messages

## Questions?

Contact solutions-architect if:
- Migration commands are unclear
- Database connection issues
- Unexpected errors occur

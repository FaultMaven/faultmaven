# TASK-001: Database Migration Infrastructure Setup

## Task Metadata
- **Phase**: Week 1, Day 1 (Foundation)
- **Priority**: P0 (Blocker for all future work)
- **Estimated Time**: 1-2 hours
- **Dependencies**: None
- **Assignee**: Developer
- **Reviewer**: Solutions Architect

## Objective

Set up Alembic database migration infrastructure to enable safe, versioned database schema changes throughout the 12-week evolution plan. This is the foundation for all data model changes in subsequent tasks.

## Why This Matters

FaultMaven currently lacks a formal database migration system. As we evolve the platform, we'll need to:
- Add new tables for advanced features (knowledge graph, multi-tenant, audit logs)
- Modify existing schemas (session enhancements, case metadata, analytics)
- Migrate data safely between schema versions
- Support rollback capabilities for failed deployments

Without migration infrastructure, schema changes are error-prone and manual.

## Acceptance Criteria

### Functional Requirements
- [ ] Alembic installed and configured for FaultMaven
- [ ] Initial migration captures current database schema (baseline)
- [ ] Migration can be applied to fresh database (SQLite for dev, PostgreSQL for prod)
- [ ] Migration can be rolled back successfully
- [ ] Migration versioning follows semantic pattern (e.g., `001_baseline_schema`)
- [ ] Documentation explains how to create and apply migrations

### Technical Requirements
- [ ] Alembic configuration in `/home/swhouse/product/faultmaven/alembic/`
- [ ] Environment variable support for database URLs
- [ ] Separate migration paths for SQLite (dev) and PostgreSQL (prod)
- [ ] Migrations tracked in git with clear commit messages
- [ ] Helper scripts for common migration operations

### Quality Requirements
- [ ] Zero errors when running migration on clean database
- [ ] Zero errors when rolling back migration
- [ ] Clear error messages if migration fails
- [ ] Documentation includes troubleshooting section

## Implementation Steps

### Step 1: Install Alembic
```bash
cd /home/swhouse/product/faultmaven
source .venv/bin/activate
pip install alembic
pip freeze > requirements.txt  # Update requirements
```

### Step 2: Initialize Alembic
```bash
alembic init alembic
```

This creates:
- `alembic/` directory with migration scripts
- `alembic.ini` configuration file
- `alembic/env.py` environment setup

### Step 3: Configure Alembic for FaultMaven

**Edit `alembic/env.py`**:
- Import FaultMaven SQLAlchemy models
- Configure target metadata from models
- Add support for both SQLite and PostgreSQL
- Handle environment-based database URLs

**Edit `alembic.ini`**:
- Set `sqlalchemy.url` from environment variable
- Configure file naming template: `%%(year)d%%(month).2d%%(day).2d_%%(hour).2d%%(minute).2d_%%(slug)s`

### Step 4: Locate Current Database Schema

Search for SQLAlchemy models in the codebase:
```bash
# Find existing database models
find /home/swhouse/product/faultmaven -name "*.py" -type f -exec grep -l "Base = declarative_base\|DeclarativeBase\|SQLAlchemyBase" {} \;

# Identify tables
grep -r "class.*Base\):" /home/swhouse/product/faultmaven --include="*.py"
```

Common locations:
- `/home/swhouse/product/faultmaven/models/`
- `/home/swhouse/product/faultmaven/infrastructure/persistence/`

### Step 5: Create Baseline Migration

```bash
alembic revision --autogenerate -m "001_baseline_schema"
```

This generates a migration file in `alembic/versions/` capturing the current schema.

**Review the generated migration**:
- Verify all tables are included
- Check foreign key constraints
- Validate index creation
- Ensure no sensitive data in migration

### Step 6: Test Migration

```bash
# Apply migration (fresh database)
alembic upgrade head

# Verify database schema
# (Use SQLite browser or psql to inspect tables)

# Rollback migration
alembic downgrade -1

# Re-apply migration
alembic upgrade head
```

### Step 7: Create Helper Scripts

Create `scripts/db_migrate.sh`:
```bash
#!/bin/bash
# Helper script for database migrations

set -e

ACTION=${1:-upgrade}

case $ACTION in
  upgrade)
    echo "Applying migrations..."
    alembic upgrade head
    ;;
  downgrade)
    echo "Rolling back one migration..."
    alembic downgrade -1
    ;;
  status)
    echo "Current migration status:"
    alembic current
    ;;
  history)
    echo "Migration history:"
    alembic history --verbose
    ;;
  create)
    MESSAGE=${2:-"new_migration"}
    echo "Creating new migration: $MESSAGE"
    alembic revision --autogenerate -m "$MESSAGE"
    ;;
  *)
    echo "Usage: $0 {upgrade|downgrade|status|history|create <message>}"
    exit 1
    ;;
esac
```

Make executable:
```bash
chmod +x scripts/db_migrate.sh
```

### Step 8: Document Migration Workflow

Create `docs/development/DATABASE_MIGRATIONS.md`:

Include:
- How to create new migrations
- How to apply migrations
- How to rollback migrations
- Environment variable configuration
- Troubleshooting common issues
- Best practices for writing migrations

## Files to Create/Modify

### Create
- `/home/swhouse/product/faultmaven/alembic/` (directory structure)
- `/home/swhouse/product/faultmaven/alembic.ini`
- `/home/swhouse/product/faultmaven/alembic/env.py` (configured)
- `/home/swhouse/product/faultmaven/alembic/versions/YYYYMMDD_HHMM_001_baseline_schema.py`
- `/home/swhouse/product/faultmaven/scripts/db_migrate.sh`
- `/home/swhouse/product/faultmaven/docs/development/DATABASE_MIGRATIONS.md`

### Modify
- `/home/swhouse/product/faultmaven/requirements.txt` (add alembic)
- `/home/swhouse/product/faultmaven/.env.example` (add DATABASE_URL if missing)
- `/home/swhouse/product/faultmaven/README.md` (reference migration docs)

## Testing Checklist

### Unit Testing
- [ ] No unit tests required (infrastructure setup)

### Integration Testing
- [ ] Apply migration to clean SQLite database
- [ ] Apply migration to clean PostgreSQL database
- [ ] Rollback migration successfully
- [ ] Re-apply migration after rollback
- [ ] Verify all tables exist after migration
- [ ] Verify foreign keys are correct

### Manual Testing
```bash
# Test 1: Fresh SQLite migration
rm -f faultmaven.db
alembic upgrade head
sqlite3 faultmaven.db ".tables"  # Verify tables exist

# Test 2: Rollback and re-apply
alembic downgrade -1
alembic current  # Should show previous version
alembic upgrade head
alembic current  # Should show current version

# Test 3: Migration script helpers
./scripts/db_migrate.sh status
./scripts/db_migrate.sh history
```

## Environment Variables

Add to `.env.example`:
```bash
# Database Configuration
DATABASE_URL=sqlite:///./faultmaven.db  # SQLite for development
# DATABASE_URL=postgresql://user:pass@localhost:5432/faultmaven  # PostgreSQL for production
```

## Documentation Requirements

### `docs/development/DATABASE_MIGRATIONS.md` Must Include:

1. **Overview**
   - What are database migrations?
   - Why we use Alembic
   - Migration workflow diagram

2. **Creating Migrations**
   ```bash
   # Auto-generate from model changes
   alembic revision --autogenerate -m "add_user_roles_table"

   # Manual migration (for complex changes)
   alembic revision -m "migrate_legacy_data"
   ```

3. **Applying Migrations**
   ```bash
   # Upgrade to latest
   alembic upgrade head

   # Upgrade to specific version
   alembic upgrade <revision_id>
   ```

4. **Rollback Migrations**
   ```bash
   # Rollback one step
   alembic downgrade -1

   # Rollback to specific version
   alembic downgrade <revision_id>
   ```

5. **Best Practices**
   - Always review auto-generated migrations
   - Test rollback before deploying
   - Never edit applied migrations
   - Use descriptive migration messages
   - Include data migrations when schema changes affect data

6. **Troubleshooting**
   - "Table already exists" errors
   - "Multiple heads" error
   - PostgreSQL vs SQLite differences
   - Migration conflicts

## Success Metrics

### Definition of Done
- Alembic installed and configured
- Baseline migration created and tested
- Migration can be applied to fresh database (both SQLite and PostgreSQL)
- Migration can be rolled back without errors
- Helper script works for all operations
- Documentation complete and accurate
- All files committed to git with clear commit message

### Performance
- Migration completes in < 5 seconds for baseline schema
- No database locks or blocking issues

### Quality
- Zero errors when running migration forward
- Zero errors when running migration backward
- Clear error messages if environment not configured

## PR Template

When submitting your PR, use this template:

---

**Title**: `[TASK-001] Setup Alembic Database Migration Infrastructure`

**Description**:
This PR implements database migration infrastructure using Alembic as defined in the Week 1 evolution strategy.

**Changes**:
- Installed and configured Alembic for FaultMaven
- Created baseline migration capturing current schema
- Added helper scripts for common migration operations
- Documented migration workflow and best practices

**Testing**:
- [x] Applied migration to fresh SQLite database
- [x] Applied migration to fresh PostgreSQL database
- [x] Rolled back migration successfully
- [x] Verified all tables created correctly
- [x] Tested helper script operations

**Checklist**:
- [ ] All files created/modified as specified in task
- [ ] Baseline migration tested on both SQLite and PostgreSQL
- [ ] Rollback tested and working
- [ ] Documentation complete and accurate
- [ ] Helper script executable and functional
- [ ] `.env.example` updated with DATABASE_URL
- [ ] `requirements.txt` updated with alembic

**Screenshots**:
```bash
# Include output from:
alembic current
alembic history
./scripts/db_migrate.sh status
```

**Related Issues**:
- Implements Week 1, Day 1 of Evolution Strategy
- Foundation for TASK-002 (Session Enhancement)
- Blocks all future schema changes

**Deployment Notes**:
- Requires `alembic upgrade head` on first deploy
- DATABASE_URL environment variable must be set
- PostgreSQL users need to run migration separately

---

## Risks & Mitigation

### Risk 1: Existing Database Has Manual Schema Changes
**Mitigation**:
- Baseline migration should match current production schema exactly
- If discrepancies exist, create manual migration to reconcile
- Document any manual schema changes before migration setup

### Risk 2: SQLite vs PostgreSQL Differences
**Mitigation**:
- Test migration on both database types
- Use Alembic's database-specific operations when needed
- Document known differences in migration guide

### Risk 3: Migration Fails Mid-Application
**Mitigation**:
- Always backup database before migration
- Test rollback procedure before production deploy
- Use database transactions where possible

## Next Steps After Completion

Once this task is complete and PR is merged:
1. **TASK-002**: Session Enhancement Schema Migration (Week 1, Day 2)
   - Uses this migration infrastructure to add session metadata tables
2. **TASK-003**: Case Analytics Schema Addition (Week 1, Day 3)
   - Adds analytics tables using established migration patterns

## Questions?

Before starting implementation, ensure you understand:
- Where current database models are located
- Difference between SQLite and PostgreSQL migration requirements
- How to test migrations without affecting production data
- What happens if a migration fails mid-execution

Ask the Solutions Architect if anything is unclear.

---

**Ready to start?** Review this task definition, ask any clarifying questions, then begin implementation. Submit your PR when all acceptance criteria are met.

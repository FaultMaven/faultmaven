# FaultMaven Database Migrations

This guide covers the database migration system for FaultMaven, powered by [Alembic](https://alembic.sqlalchemy.org/).

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Creating Migrations](#creating-migrations)
- [Applying Migrations](#applying-migrations)
- [Rolling Back Migrations](#rolling-back-migrations)
- [Multi-Database Support](#multi-database-support)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

### What Are Database Migrations?

Database migrations are version-controlled schema changes that allow you to:
- Evolve your database schema safely over time
- Track changes alongside application code
- Roll back problematic changes
- Maintain consistency across development, staging, and production environments

### Why Alembic?

FaultMaven uses Alembic for database migrations because:
- It integrates seamlessly with SQLAlchemy (our ORM layer)
- It supports both PostgreSQL (production) and SQLite (development)
- It provides robust upgrade and downgrade capabilities
- It supports branching and merging for complex migration scenarios

### Architecture

```
FaultMaven Database Architecture

┌─────────────────────────────────────────────────────────────────┐
│                        Alembic Migrations                        │
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │   001_base   │ ──► │   002_xxx    │ ──► │   003_yyy    │     │
│  │   schema     │     │              │     │              │     │
│  └──────────────┘     └──────────────┘     └──────────────┘     │
│                                                                  │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
        ┌───────────────────┴───────────────────┐
        │                                       │
        ▼                                       ▼
┌───────────────────┐               ┌───────────────────┐
│     auth_db       │               │     cases_db      │
│   (PostgreSQL)    │               │   (PostgreSQL)    │
├───────────────────┤               ├───────────────────┤
│ • users           │               │ • cases           │
│ • organizations   │               │ • evidence        │
│ • teams           │               │ • hypotheses      │
│ • roles           │               │ • solutions       │
│ • permissions     │               │ • case_messages   │
│ • audit_log       │               │ • uploaded_files  │
└───────────────────┘               └───────────────────┘
```

## Quick Start

### 1. Set Up Your Environment

```bash
# Ensure DATABASE_URL is configured
export DATABASE_URL=sqlite:///./faultmaven.db  # Development
# OR for production PostgreSQL:
# export DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

### 2. Apply All Migrations

```bash
# Using helper script (recommended)
./scripts/db_migrate.sh upgrade

# Or using Alembic directly
alembic upgrade head
```

### 3. Check Migration Status

```bash
./scripts/db_migrate.sh status
```

## Configuration

### Environment Variables

FaultMaven supports multiple database configuration options:

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | Primary database URL (SQLite or PostgreSQL) | `sqlite:///./faultmaven.db` |
| `AUTH_DB_URL` | Auth database URL (for multi-DB setup) | `postgresql://user:pass@host:5432/auth_db` |
| `CASES_DB_URL` | Cases database URL (for multi-DB setup) | `postgresql://user:pass@host:5432/cases_db` |

For multi-database setups, you can also use individual components:

```bash
# Auth Database
AUTH_DB_HOST=postgres.faultmaven.local
AUTH_DB_PORT=30432
AUTH_DB_NAME=auth_db
AUTH_DB_USER=auth_service
AUTH_DB_PASSWORD=your_password

# Cases Database
CASES_DB_HOST=postgres.faultmaven.local
CASES_DB_PORT=30432
CASES_DB_NAME=cases_db
CASES_DB_USER=case_service
CASES_DB_PASSWORD=your_password
```

### Database Support

| Database | Support Level | Notes |
|----------|--------------|-------|
| PostgreSQL | Full | Recommended for production. Includes enums, GIN indexes, triggers |
| SQLite | Development | Simplified schema for local development |

## Creating Migrations

### Auto-generate

All tables have SQLAlchemy ORM models in `faultmaven/infrastructure/persistence/models.py`. Auto-generation works for schema changes:

```bash
alembic revision --autogenerate -m "description"
```

Review the generated migration before applying — auto-generation may miss seed data or complex constraints.

### Manual Migration

```bash
# Create a new migration
./scripts/db_migrate.sh create add_user_preferences

# Or using Alembic directly
alembic revision -m "add_user_preferences"
```

This creates a new file in `alembic/versions/` with the format:
```
YYYYMMDD_HHMM_add_user_preferences.py
```

### Migration Template

```python
"""add_user_preferences

Revision ID: abc123def456
Revises: previous_revision
Create Date: 2025-01-15 10:30:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = "abc123def456"
down_revision: Union[str, Sequence[str], None] = "previous_revision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """Apply schema changes."""
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """Revert schema changes."""
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


def _upgrade_postgresql() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        ALTER TABLE users ADD COLUMN preferences JSONB DEFAULT '{}'::jsonb
    """))


def _downgrade_postgresql() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        ALTER TABLE users DROP COLUMN preferences
    """))


def _upgrade_sqlite() -> None:
    op.add_column("users", sa.Column("preferences", sa.Text, server_default="{}"))


def _downgrade_sqlite() -> None:
    op.drop_column("users", "preferences")
```

## Applying Migrations

### Apply All Pending Migrations

```bash
# Using helper script
./scripts/db_migrate.sh upgrade

# Using Alembic directly
alembic upgrade head
```

### Apply to Specific Revision

```bash
# Upgrade to specific revision
alembic upgrade abc123def456

# Upgrade by relative number (e.g., +2 revisions)
alembic upgrade +2
```

### Generate SQL Without Executing

```bash
# View SQL that would be executed
./scripts/db_migrate.sh upgrade --sql

# Or
alembic upgrade head --sql
```

## Rolling Back Migrations

### Rollback One Migration

```bash
./scripts/db_migrate.sh downgrade

# Or
alembic downgrade -1
```

### Rollback to Specific Revision

```bash
alembic downgrade abc123def456
```

### Rollback All Migrations

```bash
alembic downgrade base
```

> ⚠️ **Warning**: Rolling back in production can cause data loss. Always backup your database first.

## Multi-Database Support

FaultMaven uses two PostgreSQL databases in production:

- **auth_db**: User accounts, organizations, teams, RBAC
- **cases_db**: Troubleshooting cases, evidence, hypotheses

### Migrating Specific Databases

```bash
# Migrate auth database only
./scripts/db_migrate.sh upgrade --database=auth

# Migrate cases database only
./scripts/db_migrate.sh upgrade --database=cases

# Or using Alembic directly
alembic -x database=auth upgrade head
alembic -x database=cases upgrade head
```

### Multi-Database Strategy

For production deployments:

1. **Plan the migration sequence**: Some changes may need to be applied in order
2. **Test on staging first**: Apply to both databases in staging
3. **Deploy during low-traffic periods**: Minimize impact
4. **Have rollback ready**: Know exactly how to revert

## Best Practices

### 1. Always Review Auto-Generated Migrations

Even though FaultMaven doesn't use auto-generation, if you ever switch:
- Review every migration before applying
- Check for unintended changes
- Verify data integrity won't be compromised

### 2. Test Rollback Before Deploying

```bash
# Apply migration
alembic upgrade head

# Test rollback
alembic downgrade -1

# Re-apply
alembic upgrade head
```

### 3. Never Edit Applied Migrations

Once a migration has been applied to any environment:
- Create a new migration for fixes
- Don't modify existing revision files
- This prevents version mismatch issues

### 4. Use Descriptive Migration Messages

Good:
```bash
./scripts/db_migrate.sh create add_user_preferences_jsonb_column
./scripts/db_migrate.sh create create_audit_log_table
./scripts/db_migrate.sh create add_index_on_cases_status
```

Bad:
```bash
./scripts/db_migrate.sh create fix
./scripts/db_migrate.sh create update
```

### 5. Include Data Migrations When Needed

When schema changes affect existing data:

```python
def upgrade() -> None:
    # 1. Add new column with nullable
    op.add_column("users", sa.Column("status", sa.String(20)))

    # 2. Migrate existing data
    conn = op.get_bind()
    conn.execute(text("UPDATE users SET status = 'active' WHERE deleted_at IS NULL"))
    conn.execute(text("UPDATE users SET status = 'deleted' WHERE deleted_at IS NOT NULL"))

    # 3. Make column non-nullable
    op.alter_column("users", "status", nullable=False)
```

### 6. Backup Before Production Migrations

```bash
# PostgreSQL backup
pg_dump -h host -U user -d dbname > backup_$(date +%Y%m%d_%H%M%S).sql

# Apply migration
alembic upgrade head
```

## Troubleshooting

### "Table Already Exists" Error

**Cause**: Migration was partially applied or table was created manually.

**Solution**:
```bash
# Option 1: Stamp the database to skip the migration
alembic stamp <revision>

# Option 2: Drop and recreate (CAUTION: data loss)
# Only in development:
alembic downgrade base
alembic upgrade head
```

### "Multiple Heads" Error

**Cause**: Branch in migration history (usually from concurrent development).

**Solution**:
```bash
# View current heads
alembic heads

# Create merge migration
alembic merge heads -m "merge_branches"
```

### "Can't Locate Revision" Error

**Cause**: Missing migration file or incorrect revision ID.

**Solution**:
```bash
# View full history
alembic history --verbose

# Check for gaps in revision chain
```

### PostgreSQL vs SQLite Differences

**Symptom**: Migration works locally (SQLite) but fails in production (PostgreSQL).

**Common Differences**:

| Feature | PostgreSQL | SQLite |
|---------|-----------|--------|
| Enums | Native `CREATE TYPE` | VARCHAR |
| Arrays | Native `TEXT[]` | JSON/TEXT |
| JSONB | Native `JSONB` | TEXT |
| GIN Indexes | Supported | Not supported |
| Triggers | Full support | Limited |

**Solution**: Always test against PostgreSQL before deploying:

```bash
# Use Docker for local PostgreSQL testing
docker run -d --name faultmaven-pg \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=faultmaven \
  -p 5432:5432 \
  postgres:15

export DATABASE_URL=postgresql://postgres:test@localhost:5432/faultmaven
alembic upgrade head
```

### Migration Fails Mid-Execution

**Cause**: Syntax error or constraint violation during migration.

**Solution**:
1. Check the error message for the specific issue
2. Fix the migration file
3. If partially applied, you may need to manually clean up:

```sql
-- Check current state
SELECT * FROM alembic_version;

-- Manually fix state if needed (CAUTION)
DELETE FROM alembic_version;
INSERT INTO alembic_version (version_num) VALUES ('last_successful_revision');
```

### Connection Timeout

**Cause**: Database connection issues during migration.

**Solution**:
```bash
# Check database connectivity
psql -h host -U user -d dbname -c "SELECT 1"

# Increase timeout if needed
export SQLALCHEMY_POOL_TIMEOUT=30
alembic upgrade head
```

## Migration History

| Version                        | Revision       | Date       | Description                                                                     |
|--------------------------------|----------------|------------|---------------------------------------------------------------------------------|
| `001_clean_baseline`           | `424078e5aa04` | 2026-03-17 | Clean baseline: 30 tables (auth, case, knowledge, config) + RBAC seed data      |
| `add_scope_isolation_...`      | `0a6eafc2e4cf` | 2026-03-24 | Add `scope_isolation` fields to `knowledge_items`                               |
| `add_source_type_to_conversions` | —            | 2026-03-26 | Add `conversion_jobs` + `conversion_drafts` tables for document→runbook pipeline |
| `add_reports_table`            | —              | 2026-03-29 | Add `reports` table (TD-001 migration)                                          |
| `add_kb_metadata_to_drafts`    | —              | 2026-04-04 | Add KB metadata fields to `conversion_drafts`                                   |

Current total: **33 tables**.

## Related Documentation

- [System Architecture](../architecture/architecture-overview.md)
- [Case Schema](../architecture/data-and-storage/schemas/case-schema.md)
- [User Schema](../architecture/data-and-storage/schemas/user-schema.md)
- [ER Diagram](../architecture/data-and-storage/er-diagram.md)

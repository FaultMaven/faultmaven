# FaultMaven Database Migrations

This guide covers the database migration system for FaultMaven, powered by [Alembic](https://alembic.sqlalchemy.org/).

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Creating Migrations](#creating-migrations)
- [SQLite and PostgreSQL](#sqlite-and-postgresql)
- [Applying Migrations](#applying-migrations)
- [Rolling Back Migrations](#rolling-back-migrations)
- [Re-provisioning a Database](#re-provisioning-a-database)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

### One database, one chain

FaultMaven keeps every table in **one** database: the one `DATABASE_URL` names.
Standalone defaults to SQLite (`sqlite+aiosqlite:///./data/faultmaven.db`); a
Cloud deployment points it at PostgreSQL. There is one Alembic chain in
`alembic/versions/`, and every table has its SQLAlchemy model in
`faultmaven/infrastructure/persistence/models.py`, which is the metadata
`alembic/env.py` hands to autogenerate.

While the chain is a single baseline (`001_enterprise_baseline`, ADR-017), that
one migration creates the whole schema and `downgrade()` drops the whole schema —
see [Migration History](#migration-history).

### Who runs the migrations

| Deployment | Runs `alembic upgrade head` |
|------------|-----------------------------|
| Standalone (Docker or a local process) | The app, at startup, in a subprocess, against the database the app itself opens. Skipped when `DATABASE_URL` configures no persistent database (empty, or in-memory SQLite). |
| Kubernetes | A migration Job. The app connects as a non-owner role that cannot run DDL, so `RUN_STARTUP_MIGRATIONS=false` turns the startup run off. |

`RUN_STARTUP_MIGRATIONS` defaults to `true`. The startup run is given 60 seconds;
past that it logs `Alembic migration timed out after 60 seconds` and raises
`RuntimeError`, and the app does not start. A failed migration stops the boot the
same way.

## Quick Start

Run Alembic from the repository root, where `alembic.ini` lives.

### 1. Point at a database

Use the same URL the app uses; `alembic/env.py` swaps the async driver for a
sync one itself, so one `DATABASE_URL` serves both the app and Alembic.

```bash
# Standalone default (the same file the app opens when run from the repo root)
mkdir -p data    # SQLite creates the file, not its directory
export DATABASE_URL=sqlite+aiosqlite:///./data/faultmaven.db
# OR PostgreSQL:
# export DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/faultmaven
```

### 2. Apply all migrations

```bash
# Using the helper script
./scripts/db_migrate.sh upgrade

# Or using Alembic directly
alembic upgrade head
```

### 3. Check migration status

```bash
./scripts/db_migrate.sh status   # runs `alembic current`
alembic heads                    # the head revision on disk
```

## Configuration

### Which database Alembic migrates

`alembic/env.py` loads the repository's `.env` (without overriding variables
already set in the environment) and then resolves the URL:

1. `DATABASE_URL`, if set. An async driver is swapped for its sync
   counterpart, because Alembic runs synchronously: `postgresql+asyncpg://`
   becomes `postgresql://` (psycopg2, installed from `requirements/cloud.txt`)
   and `sqlite+aiosqlite://` becomes `sqlite://`. The app's own URL can
   therefore be used unchanged.
2. Otherwise `<repository root>/data/faultmaven.db` (SQLite).

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | The one database the app opens and Alembic migrates | `sqlite+aiosqlite:///./data/faultmaven.db` |
| `RUN_STARTUP_MIGRATIONS` | Run `alembic upgrade head` at app startup (default `true`) | `false` when a migration Job owns the schema |

### Database support

| Database | Used by | What the schema carries there |
|----------|---------|-------------------------------|
| PostgreSQL | Cloud / Kubernetes | Everything: tables, CHECKs, partial indexes, row-level security policies keyed on the session's enterprise, the append-only and immutability triggers on the operator-access tables, the last-admin constraint trigger, `JSONB`, `VARCHAR(50)[]` tags |
| SQLite | Standalone (default) | The tables, CHECKs, partial indexes and SQLite spellings of the append-only/immutability triggers. No RLS (standalone is single-tenant, and SQLite has none) and no last-admin trigger |

## Creating Migrations

### Auto-generate

Edit the models in `faultmaven/infrastructure/persistence/models.py` first, then:

```bash
alembic revision --autogenerate -m "description"
```

Autogenerate diffs the models against the database `DATABASE_URL` points at, so
that database must already be at head — otherwise Alembic stops with
`Target database is not up to date.` Review the generated file before applying
it: autogenerate does not write seed data or dialect-specific DDL (RLS policies,
triggers), and it can mistake a model-state mismatch for a table to drop.

`alembic check` compares the models with a migrated database and fails when they
disagree, which is a quick way to confirm a new migration captured the whole
model change.

### Manual migration

```bash
# Create an empty migration
./scripts/db_migrate.sh create add_user_preferences

# Or using Alembic directly
alembic revision -m "add_user_preferences"
```

The file lands in `alembic/versions/` named by the `file_template` in
`alembic.ini`:

```
YYYYMMDD_HHMM_<revision>_add_user_preferences.py
```

### After creating a migration

- **Chain onto the real head.** `down_revision` must be what `alembic heads`
  prints. Do not copy a revision id from a document: prose goes stale, and a
  migration parented onto a revision read from it parents onto one that may no
  longer be the head (#1246).
- **Move the test pins.** `tests/integration/test_alembic_migrations.py` pins
  `HEAD_REVISION` and `EXPECTED_TABLES`. It fails when the chain has more than
  one head, when a migration lands without `HEAD_REVISION` moving, and when the
  migrated table set differs from `EXPECTED_TABLES`.
- **Regenerate the ER diagram** when tables or columns change:
  `python scripts/generate_er_diagram.py --update`.

### Migration template

Generated files follow `alembic/script.py.mako`. A migration with a
PostgreSQL-specific column type branches on the dialect (the `preferences`
column is an illustration, not an existing one):

```python
"""add_user_preferences

Revision ID: abc123def456
Revises: <output of `alembic heads`>
Create Date: 2026-01-15 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "abc123def456"
down_revision: Union[str, Sequence[str], None] = "<output of `alembic heads`>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_context().dialect.name == "postgresql":
        column_type = postgresql.JSONB()
    else:
        column_type = sa.Text()
    op.add_column("users", sa.Column("preferences", column_type, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "preferences")
```

## SQLite and PostgreSQL

Every migration runs on **both** dialects. CI checks both: the test suites
migrate a SQLite file (`tests/integration/test_alembic_migrations.py`),
and the *Test PostgreSQL Integration* job applies `alembic upgrade head` to a
PostgreSQL 16 service before running the tests marked `postgres`.

- **Guard PostgreSQL-only DDL on the dialect.** The baseline reads
  `op.get_context().dialect.name` and branches: RLS
  (`ALTER TABLE … ENABLE ROW LEVEL SECURITY`, `CREATE POLICY`), PL/pgSQL
  trigger functions and `NOW()` run on PostgreSQL only; SQLite gets its own
  trigger spellings and `CURRENT_TIMESTAMP`.
- **Keep column types cross-dialect.** The models use `JsonBlob`
  (`Text` with a `JSONB` variant on PostgreSQL) for JSON-shaped data and
  `TagsArray` (`VARCHAR(50)[]` on PostgreSQL, comma-separated `TEXT` on SQLite)
  for tags. A migration declares the same type the model does — the baseline's
  `_TAGS_ARRAY` is `sa.Text().with_variant(postgresql.ARRAY(sa.String(length=50)), "postgresql")`.
- **State partial indexes for both dialects**: pass `sqlite_where=` *and*
  `postgresql_where=`, as the baseline does.
- **Alter columns in batch mode.** SQLite has no `ALTER COLUMN`, so
  `op.alter_column` fails there with a syntax error. `op.batch_alter_table(...)`
  rebuilds the table on SQLite (create `_alembic_tmp_<table>`, copy, drop,
  rename) and emits a plain `ALTER TABLE` on PostgreSQL. `op.add_column`, and
  `op.drop_column` of a column no index, constraint or trigger uses, need no
  rebuild.
- **Drop an indexed column inside a batch, index first.** A plain
  `op.drop_column` of an indexed column fails on SQLite, and so does a batch
  that drops only the column: it stops with `no such column` and leaves
  `_alembic_tmp_<table>` behind. Drop the index in the same batch, before the
  column:

  ```python
  with op.batch_alter_table("cases") as batch_op:
      batch_op.drop_index("ix_cases_example")
      batch_op.drop_column("example")
  ```
- **A SQLite rebuild does not carry triggers.** The baseline gives SQLite
  triggers to `operator_access_audit`, `operator_access_grants` and
  `team_members`. Rebuilding one of those tables drops its triggers *silently* —
  the append-only and immutability guards vanish and the migration still
  succeeds. Rebuilding `users` or `teams` fails outright at the rename, because
  the `team_members` triggers read them. A batch migration on any of these
  tables drops the affected SQLite triggers first and re-creates them
  afterwards.
- **A new tenant-scoped table follows the baseline's pattern**: an
  `enterprise_id` column, `NOT NULL`, with a foreign key to `enterprises`, and
  on PostgreSQL RLS enabled with a policy keyed on
  `current_setting('app.current_enterprise_id', true)`.

## Applying Migrations

### Apply all pending migrations

```bash
# Using the helper script
./scripts/db_migrate.sh upgrade

# Using Alembic directly
alembic upgrade head
```

### Apply to a specific revision

```bash
alembic upgrade abc123def456   # to a revision
alembic upgrade +2             # by relative count
```

### Generate SQL without executing

```bash
./scripts/db_migrate.sh upgrade --sql

# Or
alembic upgrade head --sql
```

Offline mode cannot render a batch operation on SQLite: batch mode reflects the
live table, so `--sql` stops with `This operation cannot proceed in --sql mode`
unless the migration passes a complete `Table` as `copy_from=`.

## Rolling Back Migrations

```bash
./scripts/db_migrate.sh downgrade   # runs `alembic downgrade -1`
alembic downgrade abc123def456      # to a revision
alembic downgrade base              # everything
```

> ⚠️ While the chain is a single baseline, `alembic downgrade -1` **is**
> `alembic downgrade base`: it drops every table and leaves only
> `alembic_version`. Back up first.

## Re-provisioning a Database

While the chain is a single baseline there is nothing to migrate *from*: an
existing deployment is re-provisioned, not migrated. The baseline has also been
amended in place (it is the only migration), and a database stamped with the
baseline's revision before an amendment never receives it — Alembic considers
it up to date, so `alembic upgrade head` does nothing. Re-provision instead:

- **PostgreSQL**: drop and re-create the database, then re-run the migration
  Job. `fm-wipe-deployment --wipe` clears the surfaces a `DROP DATABASE` does
  not reach (vectors, object storage, Redis). Follow
  [deployment-wipe.md](../operations/deployment-wipe.md) exactly — it also
  covers the role grants that `DROP DATABASE` destroys.
- **SQLite (standalone)**: with the API stopped, delete `data/faultmaven.db`
  and run `alembic upgrade head` (or start the app, which runs it).

## Best Practices

### 1. Always review auto-generated migrations

- Review every migration before applying it
- Check for unintended changes, above all proposed table drops
- Add what autogenerate cannot write: seed rows, RLS policies, triggers

### 2. Test rollback before deploying

```bash
# Apply migration
alembic upgrade head

# Test rollback
alembic downgrade -1

# Re-apply
alembic upgrade head
```

### 3. Never edit applied migrations

Once a migration has been applied to any environment, fix it with a new
migration rather than by editing the file: a database already stamped at that
revision never sees the edit. The ADR-017 baseline is the exception that proves
the rule — it has been amended in place, and every database stamped before an
amendment has to be re-provisioned
([Re-provisioning a Database](#re-provisioning-a-database)).

### 4. Use descriptive migration messages

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

### 5. Include data migrations when needed

When a schema change affects existing rows, migrate the data in the same
migration. Batch mode keeps the `NOT NULL` step working on SQLite (the
`review_state` column is an illustration, not an existing one):

```python
def upgrade() -> None:
    # 1. Add the new column as nullable
    op.add_column("cases", sa.Column("review_state", sa.String(20), nullable=True))

    # 2. Migrate existing data
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE cases SET review_state = 'closed' WHERE closed_at IS NOT NULL"))
    conn.execute(sa.text("UPDATE cases SET review_state = 'open' WHERE closed_at IS NULL"))

    # 3. Make the column non-nullable
    with op.batch_alter_table("cases") as batch_op:
        batch_op.alter_column("review_state", existing_type=sa.String(20), nullable=False)
```

### 6. Back up before production migrations

```bash
# PostgreSQL backup
pg_dump -h host -U user -d dbname > backup_$(date +%Y%m%d_%H%M%S).sql

# Apply migration
alembic upgrade head
```

## Troubleshooting

### "Table Already Exists" Error

**Cause**: Migration was partially applied or a table was created manually.

**Solution**: rebuild the database from empty. Do not `alembic stamp` past the
failure: while the chain is a single baseline, stamping marks the whole schema
applied, and whatever the migration had not yet created (tables, triggers, RLS
policies, seed rows) is simply missing.

- **SQLite**: with the API stopped, delete `data/faultmaven.db` and run
  `alembic upgrade head`.
- **PostgreSQL**: drop and re-create the database, then re-run the migration Job
  ([Re-provisioning a Database](#re-provisioning-a-database)).

### "Multiple Heads" Error

**Cause**: Two migrations were parented onto the same revision (usually
concurrent development). `tests/integration/test_alembic_migrations.py` fails on
this too.

**Solution**:
```bash
# View current heads
alembic heads

# Create merge migration
alembic merge heads -m "merge_branches"
```

### "Can't Locate Revision" Error

**Cause**: The database's `alembic_version` names a revision that is not in
`alembic/versions/` — a missing file, or a database provisioned on the chain the
ADR-017 baseline replaced. Alembic has no path from a revision it does not know,
so `alembic upgrade head` fails with `Can't locate revision identified by '<id>'`.

**Solution**:
```bash
# View full history
alembic history --verbose

# Which revision is the database stamped with?
psql -d faultmaven -c "SELECT version_num FROM alembic_version;"
```

A database stamped on the retired chain is re-provisioned, not upgraded — see
[Re-provisioning a Database](#re-provisioning-a-database).

### PostgreSQL vs SQLite Differences

**Symptom**: Migration works locally (SQLite) but fails on PostgreSQL, or the
other way round.

**Common Differences**:

| Feature | PostgreSQL | SQLite |
|---------|-----------|--------|
| JSON | `JSONB` | `TEXT` |
| Arrays | `VARCHAR(50)[]` | comma-separated `TEXT` |
| GIN indexes (`postgresql_using="gin"`) | Supported | Created as a plain index |
| Row-level security | Supported | Not supported |
| Triggers | PL/pgSQL functions | SQLite trigger syntax; dropped by a batch rebuild |
| `ALTER COLUMN` | Supported | Needs batch mode |

**Solution**: Test against PostgreSQL before pushing (CI's PostgreSQL job uses
`postgres:16`):

```bash
# Use Docker for local PostgreSQL testing
docker run -d --name faultmaven-pg \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=faultmaven \
  -p 5432:5432 \
  postgres:16

export DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5432/faultmaven
alembic upgrade head
```

### Migration Fails Mid-Execution

**Cause**: Syntax error or constraint violation during migration.

**Solution**:
1. Check the error message for the specific issue
2. Fix the migration file
3. On PostgreSQL the upgrade runs in one transaction, so a failure rolls it back
   and the database stays at its previous revision. On SQLite (Alembic logs
   `Will assume non-transactional DDL.`) statements that ran before the failure
   stay applied, including the `_alembic_tmp_<table>` copy of a failed batch
   operation — until it is dropped, the next batch on that table fails with
   `table _alembic_tmp_<table> already exists`. For a development database, the
   simplest recovery is to delete the file and run `alembic upgrade head`
   again. To inspect or repair the recorded revision by hand:

```sql
-- Check current state
SELECT * FROM alembic_version;

-- Manually fix state if needed (CAUTION)
DELETE FROM alembic_version;
INSERT INTO alembic_version (version_num) VALUES ('last_successful_revision');
```

### Connection Errors

**Cause**: The database is unreachable, or `DATABASE_URL` names the wrong one.

**Solution**:
```bash
# Check database connectivity
psql -h host -U user -d dbname -c "SELECT 1"

# Confirm which database Alembic is migrating
alembic current --verbose
```

## Migration History

While the chain is a single baseline, `001_enterprise_baseline`
(`alembic/versions/20260906_1200_a1e0c17bd001_001_enterprise_baseline.py`)
creates every table, the RLS policies, the append-only operator triggers, the
last-admin constraint trigger and the seed rows. There is no chain because the
isolation key moved a tier (ADR-017): every tenant-scoped table, every policy
and both SSO lookup tables changed at once, and the system was pre-user — no
backward compatibility, no data preservation. An existing deployment is
re-provisioned on this baseline, not migrated: the database is dropped and
re-created and the migration Job re-run, and `fm-wipe-deployment --wipe` clears
the surfaces a `DROP DATABASE` does not reach (vectors, object storage, Redis) —
follow [deployment-wipe.md](../operations/deployment-wipe.md) exactly. So there
is nothing to migrate *from*; `downgrade()` drops everything. The migration's
own docstring carries the reasoning per table group.

Run `alembic heads` for the current head. Do not copy a revision id from prose:
a lane that parents a new migration onto a revision read from a document
parents onto one that may no longer be the head (#1246).
`tests/integration/test_alembic_migrations.py` pins `HEAD_REVISION` and the
expected table set, and fails when a migration lands without them moving.

## Related Documentation

- [System Architecture](../architecture/architecture-overview.md)
- [Case Schema](../architecture/data-and-storage/schemas/case-schema.md)
- [User Schema](../architecture/data-and-storage/schemas/user-schema.md)
- [ER Diagram](../architecture/data-and-storage/er-diagram.md)
- [Deployment Wipe](../operations/deployment-wipe.md)

---
description: Create an alembic migration using FaultMaven's conventions. Does not auto-apply.
---

# /migrate

Create an alembic migration using FaultMaven's conventions. Outputs the migration file for user review. Does **not** auto-apply.

## Argument

`$ARGUMENTS` — a short migration description (used as the alembic message). Required.

If missing, reject with a usage example: `/migrate add archived_at to cases`.

## Procedure

### 1. Read migration conventions

Read `docs/guides/database-migrations.md` fully. It covers FaultMaven's migration conventions (one database, chaining onto the real head, the test pins, SQLite/PostgreSQL compatibility including the SQLite trigger hazards of batch mode, seed data). Do not skip this step — most of the conventions are not mechanically enforced.

The guide's "Creating Migrations" and "SQLite and PostgreSQL" sections are the procedure this command follows.

### 2. Confirm the model state is the source of truth

Before generating:
- Verify SQLAlchemy model changes are already in `faultmaven/infrastructure/persistence/models.py`. Autogenerate diffs models against the database `DATABASE_URL` points at — if models don't reflect the desired schema yet, tell the user and stop.
- From the repository root (where `alembic.ini` lives), confirm the head on disk and that the database is at it — autogenerate refuses with `Target database is not up to date.` otherwise:
  ```bash
  alembic heads     # the head revision the new migration must chain onto
  alembic current   # the revision the database is stamped with
  ```

### 3. Generate the migration

From the repository root:
```bash
alembic revision --autogenerate -m "$ARGUMENTS"
```

### 4. Verify the generated file

Read the generated file in `alembic/versions/`. Check:

- **Both `upgrade()` and `downgrade()` are populated.** Autogenerate sometimes leaves `downgrade()` empty — fill it in or stop and ask the user. An empty `downgrade()` blocks rollback.
- **`down_revision` matches the output of `alembic heads`.** The migration must chain correctly; never take a revision id from a document.
- **SQLite vs PostgreSQL compatibility.** Every migration runs on both. Flag any of these for user review:
  - `op.alter_column` (SQLite has no `ALTER COLUMN`; needs `batch_alter_table`)
  - `op.drop_column` of an indexed column (SQLite refuses). It needs `batch_alter_table` with the index dropped first in the same batch — a batch that drops only the column fails with `no such column` and leaves `_alembic_tmp_<table>` behind
  - A batch rebuild of a table that carries SQLite triggers (`operator_access_audit`, `operator_access_grants`, `team_members`) — the rebuild drops them silently — or that those triggers read (`users`, `teams`) — the rebuild fails at the rename. The migration must drop and re-create the affected SQLite triggers around the batch operation.
  - PostgreSQL-only DDL (RLS policies, PL/pgSQL triggers, `NOW()`) not guarded on `op.get_context().dialect.name`
  - PostgreSQL-specific types (`JSONB`, `ARRAY`, `UUID`) without the SQLite variant the model uses
  - `server_default` with expressions that differ between the two
  - Partial indexes without both `sqlite_where=` and `postgresql_where=`
  For each flagged item, note the incompatibility and propose the `batch_alter_table` or dialect-branching pattern.
- **No unintended table drops.** If autogenerate proposes dropping a table, stop and confirm with the user — this is usually a model-state mismatch.
- **Move the test pins.** Set `HEAD_REVISION` in `tests/integration/test_alembic_migrations.py` to the new revision, and update `EXPECTED_TABLES` there for any table added or removed — the suite fails until both match.

### 5. Report

Show the user:
- Path to the generated migration file
- Summary of the `upgrade()` / `downgrade()` operations
- Any SQLite/PostgreSQL compatibility flags raised in step 4
- Explicit next step: *"Review the file, then run `alembic upgrade head` from the repository root when ready."*

## Completion Criteria

Done when: (a) the migration file exists, (b) it has both directions populated, (c) it chains correctly, (d) the test pins are moved, and (e) compatibility flags (if any) have been reported to the user.

## Out of Scope

- Applying the migration — user does this after review
- Modifying SQLAlchemy models — must be done before invoking this command
- Data backfills — write these as separate migrations or scripts

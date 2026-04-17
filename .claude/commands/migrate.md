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

Read `docs/guides/database-migrations.md` fully. It covers FaultMaven's migration conventions (naming, chaining, SQLite/PostgreSQL compatibility, seed data). Do not skip this step — the conventions are not mechanically enforced.

Also skim `CLAUDE.md` §Modifying Database Schema for the top-level procedure.

### 2. Confirm the model state is the source of truth

Before generating:
- Verify SQLAlchemy model changes are already in `faultmaven/infrastructure/persistence/models.py` (or `models/`). Autogenerate diffs models against the live DB — if models don't reflect the desired schema yet, tell the user and stop.
- Confirm the current head revision:
  ```bash
  cd faultmaven && alembic current
  ```

### 3. Generate the migration

From the `faultmaven/` directory:
```bash
alembic revision --autogenerate -m "$ARGUMENTS"
```

### 4. Verify the generated file

Read the generated file in `alembic/versions/`. Check:

- **Both `upgrade()` and `downgrade()` are populated.** Autogenerate sometimes leaves `downgrade()` empty — fill it in or stop and ask the user. An empty `downgrade()` blocks rollback.
- **`down_revision` matches the previous head.** The migration must chain correctly.
- **SQLite vs PostgreSQL compatibility.** FaultMaven supports both. Flag any of these for user review:
  - `ALTER TABLE ... DROP COLUMN` (SQLite has limited support)
  - `ALTER TABLE ... ALTER COLUMN` type changes (SQLite needs batch mode)
  - PostgreSQL-specific types (`JSONB`, `ARRAY`, `UUID` without fallback)
  - `server_default` with expressions that differ between the two
  - Index types / partial indexes
  For each flagged item, note the incompatibility and propose the `batch_alter_table` or branching pattern.
- **No unintended table drops.** If autogenerate proposes dropping a table, stop and confirm with the user — this is usually a model-state mismatch.

### 5. Report

Show the user:
- Path to the generated migration file
- Summary of the `upgrade()` / `downgrade()` operations
- Any SQLite/PostgreSQL compatibility flags raised in step 4
- Explicit next step: *"Review the file, then run `alembic upgrade head` from `faultmaven/` when ready."*

## Completion Criteria

Done when: (a) the migration file exists, (b) it has both directions populated, (c) it chains correctly, and (d) compatibility flags (if any) have been reported to the user.

## Out of Scope

- Applying the migration — user does this after review
- Modifying SQLAlchemy models — must be done before invoking this command
- Data backfills — write these as separate migrations or scripts

# Database Reference

## Schema

FaultMaven uses **Alembic** for database migrations. The current schema (33 tables across 3 domains) is created by a clean baseline migration plus subsequent migrations for `reports`, `conversion_jobs`, and `conversion_drafts`.

- **Authoritative source**: `alembic/versions/` in the repo root
- **ER diagram**: [docs/architecture/data-and-storage/er-diagram.md](../../architecture/data-and-storage/er-diagram.md) (regenerate via `scripts/generate_er_diagram.py`)
- **Schema specifications**: [docs/architecture/data-and-storage/schemas/](../../architecture/data-and-storage/schemas/)

### Migration commands

```bash
alembic upgrade head          # Apply all migrations
alembic revision --autogenerate -m "description"  # Create new migration
alembic downgrade -1          # Revert last migration
```

Historical pre-Alembic SQL scripts are preserved at `docs/archive/legacy-schema/` for reference only. Do not apply them.

## Engine configuration

The async SQLAlchemy engine setup lives in [`faultmaven/infrastructure/persistence/database.py`](../../../faultmaven/infrastructure/persistence/database.py) and branches on the URL scheme.

- **SQLite (Standalone)**: `NullPool` + a per-connection `connect` event listener that sets six PRAGMAs (3 correctness: `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`; 3 performance: `synchronous=NORMAL`, `temp_store=MEMORY`, `cache_size=-64000`). Full table, rationale, and deployment assumptions in [docs/operations/data-storage-management.md → SQLite Database Management → Engine configuration](../../operations/data-storage-management.md#engine-configuration).
- **PostgreSQL (Cloud)**: connection pool with `pool_pre_ping`, env-driven pool sizing (`pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`). Server-side knobs (`shared_buffers`, `work_mem`, `synchronous_commit`, etc.) live in the PG server config, not the application engine layer.

The SQLite PRAGMAs do **not** run against PostgreSQL — the branch is gated on `is_sqlite(url)`.

### Regression coverage

Engine setup is pinned by tests in [`tests/unit/infrastructure/persistence/test_sqlite_pragmas.py`](../../../tests/unit/infrastructure/persistence/test_sqlite_pragmas.py) — verifies all 6 PRAGMAs apply on every NullPool checkout, since PRAGMAs are per-connection state in SQLite.

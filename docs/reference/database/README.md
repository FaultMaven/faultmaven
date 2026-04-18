# Database Schema

FaultMaven uses **Alembic** for database migrations. The current schema (33 tables across 3 domains) is created by a clean baseline migration plus subsequent migrations for `reports`, `conversion_jobs`, and `conversion_drafts`.

- **Authoritative source**: `alembic/versions/` in the repo root
- **ER diagram**: [docs/architecture/data-and-storage/er-diagram.md](../../architecture/data-and-storage/er-diagram.md) (regenerate via `scripts/generate_er_diagram.py`)
- **Schema specifications**: [docs/architecture/data-and-storage/schemas/](../../architecture/data-and-storage/schemas/)

## Commands

```bash
alembic upgrade head          # Apply all migrations
alembic revision --autogenerate -m "description"  # Create new migration
alembic downgrade -1          # Revert last migration
```

Historical pre-Alembic SQL scripts are preserved at `docs/archive/legacy-schema/` for reference only. Do not apply them.

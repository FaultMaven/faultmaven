"""022_pg_type_divergence_fixes

Correct two PostgreSQL column-type divergences found by a live-DB audit, via
forward ALTERs so already-migrated databases (the cluster) are actually fixed.

Both columns were created by earlier migrations with a type that is loose on
SQLite but wrong on PostgreSQL, so the defects were invisible in local/CI
(SQLite) and only 500'd on the real-PostgreSQL (cloud) path:

- ``uploaded_files.coverage_start_ts`` / ``coverage_end_ts`` (migration 010):
  created as naive ``TIMESTAMP``; the model is ``DateTime(timezone=True)`` and the
  app binds tz-aware datetimes, which asyncpg's naive codec rejects. -> ``TIMESTAMPTZ``.
- ``evidence.advances_milestones`` (migration 009): created as ``TEXT``; the model
  is ``TagsArray`` which binds a Python list on PG (``ARRAY(String(50))``), which
  a ``TEXT`` column rejects. -> ``VARCHAR(50)[]`` (the ``String(50)`` width mirrors
  the model's ``_TagsArrayType``; keep the two in sync if that width ever changes).

PostgreSQL-only DDL — a no-op on SQLite, where these columns are already TEXT and
the type flags have no storage effect. Dialect is read via ``op.get_context()``
(not ``op.get_bind()``) so the migration is also safe under ``alembic upgrade --sql``
(offline mode has no bind).

Revision ID: d3e4f5a6b7c8
Revises: c1d2e3f4a5b6
Create Date: 2026-07-07 07:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return  # SQLite stores these as TEXT; nothing to alter.
    # coverage_*_ts: naive TIMESTAMP -> TIMESTAMPTZ, interpreting existing naive
    # values as UTC (the app's canonical zone).
    for col in ("coverage_start_ts", "coverage_end_ts"):
        op.execute(
            f"ALTER TABLE uploaded_files ALTER COLUMN {col} "
            f"TYPE TIMESTAMP WITH TIME ZONE USING {col} AT TIME ZONE 'UTC'"
        )
    # advances_milestones: TEXT -> VARCHAR(50)[], interpreting any legacy value as
    # the SQLite comma-joined form (empty in practice — populated writes 500'd on PG).
    op.execute(
        "ALTER TABLE evidence ALTER COLUMN advances_milestones "
        "TYPE VARCHAR(50)[] USING "
        "CASE WHEN advances_milestones IS NULL OR advances_milestones = '' "
        "THEN NULL ELSE string_to_array(advances_milestones, ',') END"
    )


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE evidence ALTER COLUMN advances_milestones "
        "TYPE TEXT USING array_to_string(advances_milestones, ',')"
    )
    for col in ("coverage_start_ts", "coverage_end_ts"):
        op.execute(
            f"ALTER TABLE uploaded_files ALTER COLUMN {col} "
            f"TYPE TIMESTAMP WITHOUT TIME ZONE USING {col} AT TIME ZONE 'UTC'"
        )

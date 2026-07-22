"""034_conversion_live_case_uniqueness

At most one live case-conversion per case, enforced at the database.

``convert_from_case`` guards against duplicate case->runbook drafts with two
in-process layers — the ``_inflight_runbook`` task registry and a read-then-check
idempotence guard — and both fail across Kubernetes replicas (each pod has its
own registry; the read-then-write is unserialized). Two replicas converting the
same case could each pass the read, each run the LLM, and each persist a
``conversion_jobs`` row holding a live draft, yielding duplicate runbooks.

This migration adds ``conversion_jobs.live_case_id`` (nullable) with a unique
index. The column equals ``case_id`` iff the job is case-source and currently
holds >=1 non-discarded draft; it is NULL otherwise. NULLs are distinct under a
unique index on both SQLite and PostgreSQL, so document jobs, failed no-draft
jobs, and fully-discarded jobs never collide — only concurrent live
case-conversions do, and the loser's commit fails with an IntegrityError the
service turns into the winner's response.

Initialization runs BEFORE the unique index is created: for each case that
already has live case-source jobs (possible if the race fired pre-migration),
the NEWEST such job (max ``created_at``, tie-break by ``id``) takes the key;
older duplicate live jobs stay NULL. They remain readable but no longer hold the
case — a deterministic newest-wins resolution so the unique index can be built.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-07-22 14:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Portable newest-wins initialization: for each case-source job with a live
# draft, set live_case_id = case_id only if it is the newest live case-source
# job for its case. The correlated subquery (ORDER BY created_at DESC, id DESC
# LIMIT 1) is valid on both SQLite and PostgreSQL.
_INIT_LIVE_CASE_ID = """
UPDATE conversion_jobs
SET live_case_id = case_id
WHERE source_type = 'case'
  AND case_id IS NOT NULL
  AND id = (
      SELECT j.id
      FROM conversion_jobs j
      WHERE j.case_id = conversion_jobs.case_id
        AND j.source_type = 'case'
        AND EXISTS (
            SELECT 1 FROM conversion_drafts d
            WHERE d.conversion_id = j.id
              AND d.status <> 'discarded'
        )
      ORDER BY j.created_at DESC, j.id DESC
      LIMIT 1
  )
"""


def upgrade() -> None:
    """Add live_case_id, seed newest-wins, then enforce uniqueness."""
    op.add_column(
        "conversion_jobs",
        sa.Column("live_case_id", sa.String(length=36), nullable=True),
    )
    # Seed BEFORE the unique index so a pre-existing race can't block the build.
    op.execute(_INIT_LIVE_CASE_ID)
    op.create_index(
        "uq_conversion_jobs_live_case_id",
        "conversion_jobs",
        ["live_case_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_conversion_jobs_live_case_id", table_name="conversion_jobs")
    op.drop_column("conversion_jobs", "live_case_id")

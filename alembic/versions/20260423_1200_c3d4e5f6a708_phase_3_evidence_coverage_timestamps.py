"""phase_3_evidence_coverage_timestamps

Add ``coverage_start_ts`` and ``coverage_end_ts`` columns to the
``evidence`` table, plus a composite index on
``(case_id, coverage_start_ts, coverage_end_ts)``.

Purpose (see ``docs/working/WIP-data-processing-improvement-plan.md``
Phase 3): the extractor already computes the time span the evidence's
*content* covers via ``extract_time_range`` (utils.py). Today that
signal is stringified into the structural_index and lost. Promoting it
to indexed columns enables case-level time-window queries — "what
evidence covers 14:30?" becomes a single indexed lookup instead of a
scan across every evidence's structural_index.

Semantics, per case-schema.md §4.3:

- ``coverage_start_ts`` — earliest timestamp parsed from the evidence's
  content.
- ``coverage_end_ts`` — latest timestamp parsed.
- Both columns are nullable. Evidence without parseable timestamps
  (configs, source code, short pastes, screenshots) have both NULL.
  This is a valid state — not all evidence is time-bound.

Distinct from the existing time axes on the table:

- ``upload_timestamp``: when FaultMaven received the evidence.
- ``collected_at_turn``: which agent turn registered it.
- ``coverage_*_ts``: the time span the content itself describes.

Revision ID: c3d4e5f6a708
Revises: b1c2d3e4f506
Create Date: 2026-04-23 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a708"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f506"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the two nullable TIMESTAMPTZ columns and the composite index.

    ``batch_alter_table`` is used for SQLite compatibility. The column
    type is ``DateTime(timezone=True)`` which maps to ``TIMESTAMPTZ``
    under PostgreSQL and ``DATETIME`` under SQLite. Both columns are
    nullable by design (see docstring); no backfill — existing rows
    remain NULL and the Phase 3b query tolerates that.
    """
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.add_column(
            sa.Column(
                "coverage_start_ts",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "coverage_end_ts",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    # Composite index supports the planned time-window query:
    # ``WHERE case_id = ? AND coverage_start_ts <= end AND coverage_end_ts >= start``.
    # Ordered (case_id, start, end) so the case_id prefix narrows cheaply
    # before the overlap check runs.
    op.create_index(
        "idx_evidence_coverage",
        "evidence",
        ["case_id", "coverage_start_ts", "coverage_end_ts"],
    )


def downgrade() -> None:
    op.drop_index("idx_evidence_coverage", table_name="evidence")
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_column("coverage_end_ts")
        batch_op.drop_column("coverage_start_ts")

"""049_coverage_source

Promote WHICH PATTERN produced a coverage span to a column, beside the span.

``coverage_start_ts`` / ``coverage_end_ts`` are consumed as fact — by the prompt
(``observed_through`` / ``age``), by symptom currency, by the evidence-by-time
tool, and by the repository's time-window query. Nothing could tell where a span
came from, so all of these were equally true of:

- a caller-declared ``observed_at`` — validated, rejected if in the future,
  supplied by a client that watched the content arrive; and
- an ``epoch_s`` regex hit — ``\\b([12]\\d{9})\\b``, run on every upload ungated
  by data type, which reads ``maxBytes: 2147483647`` as 2038-01-19.

``extract_time_range_ts`` has always returned the pattern name as its third
value. ``preprocessing_service`` packed it into ``CoverageMetadata`` and nothing
ever persisted it, so it was computed once per upload and dropped.

Promoted rather than left in a metadata blob for the reason ``CoverageMetadata``
itself gives: the columns are "the queryable projection", the blob carries
"auxiliary signal". Provenance that decides whether a span may be ASSERTED to
the model is not auxiliary. It is also the only representation that can express
``caller_declared``, which is applied at intake — after the extractor has already
serialized its own metadata.

Nullable, no default, no backfill: a row written before this column existed has
genuinely unrecorded provenance, and stamping one on would assert exactly the
confidence this column exists to qualify. Consumers read NULL as unknown.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-30 23:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("uploaded_files", "evidence")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("coverage_source", sa.String(length=50), nullable=True),
        )


def downgrade() -> None:
    """Drop the column from both tables.

    ``batch_alter_table`` because SQLite has no native DROP COLUMN before 3.35
    and alembic's batch mode rebuilds the table; on PostgreSQL it is a
    passthrough. The recorded provenance is lost and the spans stay — which
    returns the schema to exactly the state this migration was written to fix.
    """
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("coverage_source")

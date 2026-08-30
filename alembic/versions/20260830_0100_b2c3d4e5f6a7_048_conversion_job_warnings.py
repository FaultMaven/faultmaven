"""048_conversion_job_warnings

Persist a conversion job's ``warnings``, so the REASON a job is partial
survives the response that created it.

Migration 047 made ``status='partial'`` storable. It did not make the partiality
explicable: the per-failure-mode refusals reach the user only through
``ConversionResponse.warnings``, which is assembled in memory by
``convert_document`` and never written anywhere. ``get_conversion`` rebuilds the
response from ``conversion_jobs`` + ``conversion_drafts`` and does not set the
field at all, so it defaults to ``[]``.

The user-visible consequence: a document that yielded 2 runbooks out of 3 shows
the message naming both colliding failure modes exactly once, and one page
refresh replaces it with an unexplained PARTIAL. The field is part of the
published response contract (``ConversionResponse.warnings``), so a client has
no way to tell "no warnings" from "warnings discarded".

Nullable with no default and no backfill: a job written before this column
exists genuinely has no recorded warnings, and inventing ``[]`` for it would
assert the stronger claim that it had none. ``get_conversion`` maps NULL to
``[]`` at the edge because the response field is non-optional, and that is the
one place the distinction is deliberately collapsed.

Revision ID: b2c3d4e5f6a7
Revises: f0a1b2c3d4e5
Create Date: 2026-08-30 01:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversion_jobs",
        sa.Column("warnings", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Drop the column.

    ``batch_alter_table`` because SQLite has no native DROP COLUMN before 3.35
    and alembic's batch mode rebuilds the table; on PostgreSQL it is a
    passthrough. Recorded warnings are lost, which is the honest outcome — there
    is nowhere else to put them.
    """
    with op.batch_alter_table("conversion_jobs") as batch:
        batch.drop_column("warnings")

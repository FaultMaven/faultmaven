"""016_drop_path_selection

Drops `cases.path_selection`. The investigation-flow redesign removes the
prospective path fork (InvestigationPath / PathSelection); a stabilization
is now an optional inserted sub-activity recorded inside
`progress.stabilization` (StabilizationRecord), which round-trips with the
existing `progress` JSON column. There is no longer any code that reads or
writes the `path_selection` column.

Pre-production, no data to preserve (per feedback_no_backcompat_pre_data):
the column is dropped outright rather than migrated.

Revision ID: 0a1b2c3d4e5f
Revises: f015a7b2c3d4
Create Date: 2026-06-05 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "f015a7b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches the ORM JsonBlob type (Text on SQLite, JSONB on PostgreSQL).
_JSON_BLOB = sa.Text().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    """Drop path_selection from cases."""
    # batch_alter_table for SQLite (no native DROP COLUMN until recent
    # versions); plain ALTER on PostgreSQL.
    with op.batch_alter_table("cases") as batch:
        batch.drop_column("path_selection")


def downgrade() -> None:
    """Restore the nullable path_selection JSON column on cases."""
    with op.batch_alter_table("cases") as batch:
        batch.add_column(sa.Column("path_selection", _JSON_BLOB, nullable=True))

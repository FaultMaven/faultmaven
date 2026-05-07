"""002_evidence_summary_restore_and_rename_to_extract

Restores `evidence.summary` (always-present short label) and renames the
former `evidence.content` field to `evidence.extract` (bulk content
backing the summary, now nullable for Path 2). Updates CHECK constraints
to mirror the new shape; the new ones are mirrored in the Pydantic
Evidence model for cross-layer defense in depth.

See `case-schema.md` §4.3 "Role of summary vs extract" for the design.

Revision ID: 00eab5e0d387
Revises: c4689af8aa3f
Create Date: 2026-05-07 05:49:03.288930
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "00eab5e0d387"
down_revision: Union[str, Sequence[str], None] = "c4689af8aa3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add summary, rename content -> extract, swap CHECK constraints."""
    # batch_alter_table handles SQLite's column-drop / CHECK-rebuild
    # requirement (recreate-and-copy under the hood) and is a no-op
    # passthrough on PostgreSQL where ALTER TABLE handles it directly.
    with op.batch_alter_table("evidence") as batch:
        # New columns first.
        batch.add_column(
            sa.Column("summary", sa.String(length=500), nullable=False)
        )
        batch.add_column(sa.Column("extract", sa.Text(), nullable=True))

        # Drop the old content column and its CHECK.
        batch.drop_constraint("evidence_content_not_empty", type_="check")
        batch.drop_column("content")

        # Install the new CHECKs.
        batch.create_check_constraint(
            "evidence_summary_not_empty", "LENGTH(TRIM(summary)) > 0"
        )
        batch.create_check_constraint(
            "evidence_extract_not_empty",
            "extract IS NULL OR LENGTH(TRIM(extract)) > 0",
        )


def downgrade() -> None:
    """Reverse: drop summary/extract and restore content + its CHECK."""
    with op.batch_alter_table("evidence") as batch:
        batch.drop_constraint("evidence_extract_not_empty", type_="check")
        batch.drop_constraint("evidence_summary_not_empty", type_="check")
        batch.drop_column("extract")
        batch.drop_column("summary")
        batch.add_column(sa.Column("content", sa.Text(), nullable=False))
        batch.create_check_constraint(
            "evidence_content_not_empty", "LENGTH(TRIM(content)) > 0"
        )

"""004_drop_uploaded_files_preprocessing_summary

Drops `uploaded_files.preprocessing_summary`. The auto-generated file
summary now lives canonically on `evidence.summary` (per case-schema.md
§4.3 "Role of summary vs extract", Path 1 row). Storing it on both the
file and the evidence was a stale denormalization left over from before
the schema redesign restored `evidence.summary` as the case-scoped label.

Single source of truth: file-intrinsic metadata stays on uploaded_files
(filename, size_bytes, content_type, content_hash, storage_ref,
upload_source). Case-scoped interpretation (summary, extract, category)
lives on evidence, reachable via Evidence.source_file_id.

Revision ID: f7bbadb43e4c
Revises: a3957258f451
Create Date: 2026-05-07 10:47:05.698205
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7bbadb43e4c"
down_revision: Union[str, Sequence[str], None] = "a3957258f451"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop preprocessing_summary from uploaded_files."""
    # batch_alter_table for SQLite (no native DROP COLUMN until recent
    # versions); no-op passthrough on PostgreSQL.
    with op.batch_alter_table("uploaded_files") as batch:
        batch.drop_column("preprocessing_summary")


def downgrade() -> None:
    """Restore preprocessing_summary on uploaded_files."""
    with op.batch_alter_table("uploaded_files") as batch:
        batch.add_column(sa.Column("preprocessing_summary", sa.Text(), nullable=True))

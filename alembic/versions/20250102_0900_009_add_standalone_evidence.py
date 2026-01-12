"""Add standalone_evidence table

Revision ID: 011_add_standalone_evidence
Revises: 010_hypothesis_solution_multitenancy
Create Date: 2025-01-02 09:00:00

PR #46b: Infrastructure layer for Evidence Service module.
Creates the standalone_evidence table for evidence files that can be
linked to multiple cases (unlike evidence_artifacts which is case-scoped).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '011_add_standalone_evidence'
down_revision: Union[str, None] = '010_hypothesis_solution_multitenancy'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create standalone_evidence table."""
    op.create_table(
        'standalone_evidence',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('filename', sa.String(512), nullable=False),
        sa.Column('content_type', sa.String(256), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('storage_path', sa.String(2048), nullable=False),
        sa.Column('uploaded_by', sa.String(36), nullable=False, index=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('tags', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('linked_cases', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('metadata', sa.Text(), server_default='{}'),
        sa.CheckConstraint("LENGTH(TRIM(filename)) > 0", name='standalone_evidence_filename_not_empty'),
        sa.CheckConstraint("LENGTH(TRIM(storage_path)) > 0", name='standalone_evidence_storage_path_not_empty'),
        sa.CheckConstraint("size_bytes >= 0", name='standalone_evidence_size_non_negative'),
    )

    # Create index on uploaded_at for listing/filtering
    op.create_index(
        'ix_standalone_evidence_uploaded_at',
        'standalone_evidence',
        ['uploaded_at'],
        unique=False,
    )


def downgrade() -> None:
    """Drop standalone_evidence table."""
    op.drop_index('ix_standalone_evidence_uploaded_at', table_name='standalone_evidence')
    op.drop_table('standalone_evidence')

"""gap20_unified_data_processing

Revision ID: c7f8e3a1d9b4
Revises: a32b2452ebb2
Create Date: 2026-02-16 14:00:00.000000

Gap #20: Unified Data Processing
- Drop uq_evidence_case_turn unique constraint (allows multiple evidence per turn)
- Add source_file_id column for UploadedFile linkage
- Non-unique idx_evidence_case_turn already exists for query performance
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7f8e3a1d9b4'
down_revision: Union[str, Sequence[str], None] = 'a32b2452ebb2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    1. Drop uq_evidence_case_turn unique index — the LLM can return
       multiple evidence_to_add items per turn, so the one-per-turn
       constraint was incorrectly blocking inserts.
       The non-unique idx_evidence_case_turn index (created in prior
       migration) remains for query performance.
    2. Add source_file_id column — links Evidence to its source
       UploadedFile for traceability.
    """

    # Drop the unique constraint (implemented as unique index)
    op.drop_index('uq_evidence_case_turn', table_name='evidence')

    # Add source_file_id column for file linkage
    op.add_column(
        'evidence',
        sa.Column('source_file_id', sa.String(20), nullable=True)
    )


def downgrade() -> None:
    """Revert Gap #20 changes."""

    # Remove source_file_id column
    op.drop_column('evidence', 'source_file_id')

    # Re-create the unique constraint
    op.create_index(
        'uq_evidence_case_turn',
        'evidence',
        ['case_id', 'collected_at_turn'],
        unique=True,
        postgresql_where=sa.text('collected_at_turn IS NOT NULL')
    )

"""rename_case_status_transitions_to_case_actions

Revision ID: f1a2b3c4d5e6
Revises: e9b4c2d1f7a6
Create Date: 2026-03-02 01:00:00.000000

Terminology standardization: rename the case_status_transitions table to
case_actions to align with the Investigation Terminology Guide.
Case actions encompass both phase transitions (e.g., INQUIRY → INVESTIGATING)
and dispositions (e.g., INVESTIGATING → RESOLVED).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e9b4c2d1f7a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table('case_status_transitions', 'case_actions')


def downgrade() -> None:
    op.rename_table('case_actions', 'case_status_transitions')

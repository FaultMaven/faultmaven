"""add_hypothesis_generated_at_turn

Revision ID: d8a3f1b2c4e5
Revises: c7f8e3a1d9b4
Create Date: 2026-02-20 10:45:00.000000

The Hypothesis domain model requires generated_at_turn (mandatory, no default),
but the hypotheses table was missing this column. Any case that generated
a hypothesis during investigation would fail Pydantic validation on subsequent
reads, causing 500 errors on all endpoints that load the case.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8a3f1b2c4e5'
down_revision: Union[str, Sequence[str], None] = 'c7f8e3a1d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text("PRAGMA table_info(hypotheses)"))
    columns = {row[1] for row in result}
    if "generated_at_turn" not in columns:
        op.add_column('hypotheses', sa.Column('generated_at_turn', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('hypotheses', 'generated_at_turn')

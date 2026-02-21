"""add_solution_type_and_title

Revision ID: e9b4c2d1f7a6
Revises: d8a3f1b2c4e5
Create Date: 2026-02-20 14:30:00.000000

The Solution domain model requires solution_type (mandatory) and title (mandatory),
but the solutions table was missing these columns. Any case that generated
a solution during investigation would fail Pydantic validation on subsequent
reads, causing 404/500 errors on all endpoints that load the case.

Also adds commands and risks columns (JSON lists) that are part of the
current Solution model but missing from the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9b4c2d1f7a6'
down_revision: Union[str, Sequence[str], None] = 'd8a3f1b2c4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text("PRAGMA table_info(solutions)"))
    columns = {row[1] for row in result}

    if "solution_type" not in columns:
        op.add_column('solutions', sa.Column('solution_type', sa.String(30), nullable=False, server_default='other'))

    if "title" not in columns:
        op.add_column('solutions', sa.Column('title', sa.String(200), nullable=False, server_default=''))

    if "immediate_action" not in columns:
        op.add_column('solutions', sa.Column('immediate_action', sa.Text(), nullable=True))

    if "longterm_fix" not in columns:
        op.add_column('solutions', sa.Column('longterm_fix', sa.Text(), nullable=True))

    if "commands" not in columns:
        op.add_column('solutions', sa.Column('commands', sa.Text(), nullable=True))

    if "risks" not in columns:
        op.add_column('solutions', sa.Column('risks', sa.Text(), nullable=True))

    # Backfill: copy description → title for existing rows with empty title
    conn.execute(sa.text("""
        UPDATE solutions SET title = description WHERE title = '' AND description IS NOT NULL AND description != ''
    """))

    # Backfill: copy description → immediate_action for existing rows
    conn.execute(sa.text("""
        UPDATE solutions SET immediate_action = description WHERE immediate_action IS NULL AND description IS NOT NULL
    """))


def downgrade() -> None:
    op.drop_column('solutions', 'risks')
    op.drop_column('solutions', 'commands')
    op.drop_column('solutions', 'longterm_fix')
    op.drop_column('solutions', 'immediate_action')
    op.drop_column('solutions', 'title')
    op.drop_column('solutions', 'solution_type')

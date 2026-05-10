"""008_case_actions_triggered_by

Adds a NOT NULL ``triggered_by VARCHAR(50)`` column to ``case_actions``.

Background: the Pydantic ``CaseAction`` domain model has carried a
``triggered_by: str`` field for some time, but the ORM ``CaseActionModel``
had no matching column — meaning every persist silently dropped the
value. Worse, the entire ``case_actions`` table has been write-only
(both repos hardcoded ``action_history=[]`` in ``_to_domain``), so the
audit-trail capability has been broken across the board.

This migration restores the value space (free-form string: user UUID or
sentinel like ``"system"`` / ``"agent"`` / ``"scheduler"``) and clears
existing rows. Per project principle (no backward-compatibility / no
data preservation in pre-production), we drop existing ``case_actions``
rows rather than backfilling — they are unread dead audit data.

Not a FK — value space is heterogeneous. A CHECK constraint can be
added later when the sentinel set stabilizes.

Revision ID: 317a8c329673
Revises: 05b6eaf5baad
Create Date: 2026-05-09 23:20:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "317a8c329673"
down_revision: Union[str, Sequence[str], None] = "05b6eaf5baad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop existing rows, then add the NOT NULL column.

    Both steps are required: NOT NULL would fail without an existing-row
    purge or a backfill, and we have explicit license to skip backfill
    (per project principle).
    """
    op.execute("DELETE FROM case_actions")
    with op.batch_alter_table("case_actions") as batch:
        batch.add_column(
            sa.Column("triggered_by", sa.String(length=50), nullable=False)
        )


def downgrade() -> None:
    """Drop the column. Existing rows would have been dropped on upgrade
    so there's nothing to preserve here either."""
    with op.batch_alter_table("case_actions") as batch:
        batch.drop_column("triggered_by")

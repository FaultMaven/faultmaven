"""005_relax_description_check_to_investigation_states

Relaxes the description CHECK constraint on `cases` to enforce non-empty
description only for the two states that require it as a product
invariant: INVESTIGATING and RESOLVED. INQUIRY (still being formulated)
and CLOSED (legitimate inquiry → closed early-abandon path) accept
empty.

Old CHECK (cases_description_required_post_inquiry):
    status = 'inquiry' OR LENGTH(TRIM(description)) > 0

New CHECK (cases_description_required_for_investigation):
    status IN ('inquiry', 'closed') OR LENGTH(TRIM(description)) > 0

The Pydantic Case validator is extended to RESOLVED in lockstep so the
two layers enforce the same rule.

Revision ID: 24a5adc58c77
Revises: f7bbadb43e4c
Create Date: 2026-05-07 20:29:52.730500
"""

from typing import Sequence, Union

from alembic import op

revision: str = "24a5adc58c77"
down_revision: Union[str, Sequence[str], None] = "f7bbadb43e4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the old CHECK and install the relaxed one (with renamed name).

    SQLite requires batch_alter_table to drop/recreate a CHECK; PostgreSQL
    can do it directly but batch is a no-op passthrough on PG so this works
    on both dialects.
    """
    with op.batch_alter_table("cases") as batch:
        batch.drop_constraint(
            "cases_description_required_post_inquiry", type_="check"
        )
        batch.create_check_constraint(
            "cases_description_required_for_investigation",
            "status IN ('inquiry', 'closed') OR LENGTH(TRIM(description)) > 0",
        )


def downgrade() -> None:
    """Restore the original (stricter) CHECK."""
    with op.batch_alter_table("cases") as batch:
        batch.drop_constraint(
            "cases_description_required_for_investigation", type_="check"
        )
        batch.create_check_constraint(
            "cases_description_required_post_inquiry",
            "status = 'inquiry' OR LENGTH(TRIM(description)) > 0",
        )

"""Drop invalid unique constraint on case_messages

Revision ID: 014_drop_message_constraint
Revises: 013_verification_suggestions
Create Date: 2026-01-30 01:00:00.000000

This migration removes an incorrect unique constraint on (case_id, turn_number)
that was defined in SQLAlchemy models but never created in the database schema.

The constraint is incorrect because multiple messages (user + assistant) can
legitimately share the same turn_number within a conversation.

Only message_id needs to be unique (enforced by PRIMARY KEY).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014_drop_message_constraint"
down_revision: Union[str, None] = "013_verification_suggestions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop unique constraint if it exists (defensive migration)."""
    # Try to drop the constraint - will fail silently if it doesn't exist
    # PostgreSQL
    try:
        op.drop_constraint(
            "uq_case_messages_case_turn", "case_messages", type_="unique"
        )
    except Exception:
        # Constraint doesn't exist (expected for most deployments)
        pass

    # SQLite doesn't support dropping constraints directly
    # But the constraint was never created in SQLite migrations anyway


def downgrade() -> None:
    """No downgrade - the constraint should never have existed."""
    # We don't re-add the constraint because it was incorrect
    pass

"""015 rename lifecycle `status` columns to `state`

Aligns the database with the State/Status DDD split: the lifecycle entities
governed by a state machine (case, hypothesis, solution, evidence_need,
investigation_session) expose `state`; progress/projection columns
(agent_executions.status, agent_tool_calls.status, reports.generation_status,
knowledge_suggestions.status, conversion_*.status) keep `status`.

Both SQLite (>=3.25) and PostgreSQL propagate a column rename into CHECK
constraint expressions and dependent indexes automatically, so a plain
RENAME COLUMN preserves data and validation. Constraint/index *names* keep
their historical `status` token (cosmetic only); the one explicitly-named
composite index is renamed to match the model.

Revision ID: f015a7b2c3d4
Revises: e1d2c3b4a5f6
Create Date: 2026-06-03 12:00:00.000000
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "f015a7b2c3d4"
down_revision = "e1d2c3b4a5f6"
branch_labels = None
depends_on = None


# (table, old_column, new_column)
_RENAMES = [
    ("cases", "status", "state"),
    ("hypotheses", "status", "state"),
    ("solutions", "status", "state"),
    ("evidence_needs", "status", "state"),
    ("investigation_sessions", "status", "state"),
    ("case_actions", "from_status", "from_state"),
    ("case_actions", "to_status", "to_state"),
]


def upgrade() -> None:
    for table, old, new in _RENAMES:
        op.alter_column(table, old, new_column_name=new)

    # Explicitly-named composite index follows the column rename in the model.
    op.drop_index("ix_evidence_needs_case_status", table_name="evidence_needs")
    op.create_index(
        "ix_evidence_needs_case_state", "evidence_needs", ["case_id", "state"]
    )


def downgrade() -> None:
    # Drop the renamed index first (it references the `state` column), then
    # rename the columns back, then recreate the index against `status` —
    # the index column must exist under its old name before we reference it.
    op.drop_index("ix_evidence_needs_case_state", table_name="evidence_needs")

    for table, old, new in reversed(_RENAMES):
        op.alter_column(table, new, new_column_name=old)

    op.create_index(
        "ix_evidence_needs_case_status", "evidence_needs", ["case_id", "status"]
    )

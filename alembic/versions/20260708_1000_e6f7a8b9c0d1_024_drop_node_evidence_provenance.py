"""024_drop_node_evidence_provenance

Drops ``causal_node_evidence.provenance`` (and its value CHECK). The column
recorded who authored the predicate behind a node-evidence link
(``runbook`` / ``llm_fallback``) for the runbook-cause-matcher's deterministic
grounding arm. That arm is retired (NO-GO, #658): the matcher never shipped
enabled, so no row ever carried a non-NULL value, and with the producer and
every consumer removed the column is dead. Forward drop, never a baseline edit
(applied-migration convention).

Revision ID: e6f7a8b9c0d1
Revises: f5a6b7c8d9e0
Create Date: 2026-07-08 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the CHECK, then the column (batch mode so it applies on SQLite,
    which cannot ALTER a constraint/column in place)."""
    with op.batch_alter_table("causal_node_evidence") as batch:
        batch.drop_constraint("causal_node_evidence_provenance_check", type_="check")
        batch.drop_column("provenance")


def downgrade() -> None:
    import sqlalchemy as sa

    with op.batch_alter_table("causal_node_evidence") as batch:
        batch.add_column(sa.Column("provenance", sa.String(length=20), nullable=True))
        batch.create_check_constraint(
            "causal_node_evidence_provenance_check",
            "provenance IS NULL OR provenance IN ('runbook', 'llm_fallback')",
        )

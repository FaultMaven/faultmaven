"""020_node_evidence_provenance

Adds ``provenance`` to the ``causal_node_evidence`` junction so a node-evidence
link records who authored the predicate it encodes:

- ``runbook``       — an expert-authored predicate (authority-grounded, sound)
- ``llm_fallback``  — a predicate the model authored for itself (lower-assurance)
- ``NULL``          — a legacy/unlabeled link (predates tagging), read as sound

Without this column the in-memory ``NodeEvidenceLink.provenance`` was silently
dropped on save and reloaded as NULL, making the fallback-only validation signal
dead across a save/reload cycle.

Revision ID: b2d4f6a8c0e1
Revises: 888d280e336e
Create Date: 2026-06-29 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b2d4f6a8c0e1"
down_revision: Union[str, Sequence[str], None] = "888d280e336e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable ``provenance`` column + a value CHECK (batch mode so it
    applies on SQLite, which cannot ALTER in a CHECK constraint in place)."""
    with op.batch_alter_table("causal_node_evidence") as batch:
        batch.add_column(sa.Column("provenance", sa.String(length=20), nullable=True))
        batch.create_check_constraint(
            "causal_node_evidence_provenance_check",
            "provenance IS NULL OR provenance IN ('runbook', 'llm_fallback')",
        )


def downgrade() -> None:
    with op.batch_alter_table("causal_node_evidence") as batch:
        batch.drop_constraint("causal_node_evidence_provenance_check", type_="check")
        batch.drop_column("provenance")

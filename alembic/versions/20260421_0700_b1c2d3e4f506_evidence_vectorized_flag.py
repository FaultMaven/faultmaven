"""evidence_vectorized_flag

Add a persistent `vectorized` lifecycle flag to the `evidence` table.

Prior to this migration, the investigation engine checked
``getattr(ev, "vectorized", False)`` against an attribute that did not
exist on the Evidence model, so every turn's proactive-vectorization
gate evaluated ``False`` and re-fired embedding tasks for the same
evidence. On real loads this stacked concurrent BGE-M3 encodes,
contended CPU cores, and drove each task past the 60 s ``wait_for``
bound in ``_vectorize_evidence``.

Completing the Evidence lifecycle model with a persistent ``vectorized``
boolean makes the existing gate work correctly across turns. See
``modules/case/domain/models.py::Evidence.vectorized`` for the domain
field and ``docs/architecture/core-architecture/architectural-design-principles.md``
P2 / P3 / P10 for why this state belongs on the domain entity rather
than being queried from ChromaDB side-effects.

Revision ID: b1c2d3e4f506
Revises: 042c3b49b88a
Create Date: 2026-04-21 07:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f506"
down_revision: Union[str, Sequence[str], None] = "042c3b49b88a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``vectorized`` to the ``evidence`` table (default False).

    ``server_default="0"`` produces ``FALSE`` under both SQLite and
    PostgreSQL so existing rows backfill without a separate data step.
    ``nullable=False`` reflects that every evidence row has a definite
    vectorization state — unknown is modeled as ``False`` (not yet
    vectorized), which is safe: the gate will then attempt to vectorize
    once and flip it True on success.
    """
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.add_column(
            sa.Column(
                "vectorized",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_column("vectorized")

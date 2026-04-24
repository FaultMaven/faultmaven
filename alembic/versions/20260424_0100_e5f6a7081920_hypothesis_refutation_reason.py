"""hypothesis_refutation_reason

Add ``refutation_reason`` column to the ``hypotheses`` table.

Phase 3 (F10) introduces a pair-integrity invariant at the domain layer:
a hypothesis with ``status=REFUTED`` must carry a ``refutation_reason``
(max 200 chars), and the reason is only valid when the status is REFUTED.
This migration adds the persistent column that backs the new field.

Nullable because the invariant is status-conditional: non-REFUTED
hypotheses have no reason. Enforcement lives in the Pydantic model
(``Hypothesis._validate_refutation_reason_pairs_with_status``); the
SQL schema is permissive because the invariant can't be expressed as
a column-level constraint without referencing another column.

Revision ID: e5f6a7081920
Revises: d4e5f6a70819
Create Date: 2026-04-24 01:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7081920"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a70819"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``refutation_reason`` (nullable VARCHAR(200)) to ``hypotheses``.

    Nullable by design — the pair invariant between ``status`` and
    ``refutation_reason`` is enforced by the Pydantic domain model, not
    the SQL schema. The 200-char cap matches the domain field's
    ``max_length`` and keeps refuted-hypothesis context dense when
    rendered into the LLM prompt.
    """
    with op.batch_alter_table("hypotheses") as batch_op:
        batch_op.add_column(
            sa.Column(
                "refutation_reason",
                sa.String(length=200),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("hypotheses") as batch_op:
        batch_op.drop_column("refutation_reason")

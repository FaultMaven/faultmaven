"""case_version_for_occ

Add ``version`` column to the ``cases`` table for optimistic concurrency
control on the Case aggregate.

Every successful aggregate ``save(case)`` bumps this counter. The write
path in the repository issues an UPDATE with a ``WHERE ... AND version =
:expected_version`` predicate; on mismatch the save raises
``StaleCaseException`` so the caller can re-fetch and re-apply their
mutation (or surface a 409 Conflict to the user). Scoped single-row
UPDATEs (e.g. ``update_evidence_vectorized``) do NOT bump this — they
operate on child tables and don't touch the Case aggregate version.

``NOT NULL DEFAULT 1`` so any row migrated from a schema that predates
this column lands at version 1, matching freshly created cases.

Revision ID: a1b2c3d4e5f6
Revises: e5f6a7081920
Create Date: 2026-04-24 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e5f6a7081920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``version`` (NOT NULL INTEGER, default 1) to ``cases``."""
    with op.batch_alter_table("cases") as batch_op:
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch_op:
        batch_op.drop_column("version")

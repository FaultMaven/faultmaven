"""021_knowledge_item_ingest_complete

``knowledge_items.ingest_complete`` (#2394): whether a runbook's ingest wrote
its vectors.

An authored ingest (an upload, or ``verify_draft``) inserts its row with
``false`` and sets it ``true`` in one guarded UPDATE once its vectors are
written. A row left ``false`` by an ingest that failed, or whose INSERT
committed after its caller gave up on it, is never listed and never
re-embedded by the startup repair, and the KB bootstrap removes it with its
vectors once it is older than ``KB_INCOMPLETE_INGEST_MAX_AGE_HOURS``. A pack
ingest inserts ``true`` and never writes the column: under
``TENANT_PROVIDER=multi`` the ``kb_seed`` maintenance role holds no UPDATE on
the table.

Column
------
``BOOLEAN NOT NULL DEFAULT '1'``, the baseline's spelling of
``is_published``'s default, so every existing row reads complete: it was
written before the column existed, by an ingest that either completed or was
already removed by its own cleanup. No index: the readers that filter on it
also filter on columns that are indexed, and the sweep reads a small table
once per boot.

Dialects
--------
Both add the column in place. SQLite admits ``ADD COLUMN ... NOT NULL`` with a
non-NULL default; PostgreSQL 11+ adds a column with a constant default without
rewriting the table. ``knowledge_items`` is already enrolled in row-level
security, and its policies are per row, so there is no RLS change.

``downgrade()`` drops the column and nothing else. No index, constraint or
trigger names it, so SQLite (3.35+) drops it in place.

Revision ID: d0eb318ce3e0
Revises: 596f06c52909
Create Date: 2026-10-29 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d0eb318ce3e0"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "596f06c52909"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "knowledge_items"
COLUMN = "ingest_complete"


def upgrade() -> None:
    """Add ``knowledge_items.ingest_complete``; existing rows read true."""
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Boolean(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column(TABLE, COLUMN)

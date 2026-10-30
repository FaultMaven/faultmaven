"""022_knowledge_item_vector_generation

``knowledge_items.vector_generation`` (#2388): the generation every chunk the
row serves carries, in its id (``{item_id}_chunk_{i}_{generation}``) and in its
``vector_generation`` metadata key. KB admission serves a chunk only while its
row carries the chunk's generation, so two pack versions ingesting one id, or
an update and its predecessor, can no longer serve each other's chunks, and no
writer deletes a chunk to replace it.

Column
------
``VARCHAR(32) NOT NULL``, no default: every INSERT and every content change
mints a value (``mint_vector_generation``, 32 hex digits).

Existing rows (the owner's ruling (c), 2026-10-30)
---------------------------------------------------
The column is added nullable, each existing row is given a random generation in
ONE UPDATE (``lower(hex(randomblob(16)))`` on SQLite,
``md5(random()::text || item_id)`` on PostgreSQL), and the column is then made
NOT NULL. That fills a column; it migrates no chunk. No chunk carries those
values, so nothing written before this revision is served from the first query
after it. The next pack-loading bootstrap re-ingests every built-in row from
the pack (a pack row with no chunk of its generation, pre-embedded, no model);
an authored row goes to the bootstrap's capped repair, or is re-provisioned
(``fm-reset-kb`` on standalone, the deployment wipe under multi). The rows
left serving nothing are counted as ``unserved_rows`` on the bootstrap and
``kb_seed`` results, and the generation sweep collects the old chunks.

Dialects
--------
PostgreSQL adds the column, updates under ``SET LOCAL row_security = off`` (the
migrating role owns the table and is exempt; a role a policy would filter
raises instead of updating one enterprise's rows), restores the default, and
sets NOT NULL in place. ``knowledge_items`` is already enrolled in row-level
security, and its policies are per row, so there is no RLS change.

SQLite cannot add NOT NULL to a column, so the table is rebuilt
(``batch_alter_table``, reflecting the live table). ``conversion_drafts``
references it ``ON DELETE SET NULL``, which SQLite runs when the old table is
dropped if foreign keys are enforced; ``env.py`` does not enforce them, and the
revision refuses to run otherwise rather than unlink every draft.

``downgrade()`` drops the column and nothing else: no index, constraint or
trigger names it, so SQLite (3.35+) drops it in place.

Revision ID: 03f93180f630
Revises: d0eb318ce3e0
Create Date: 2026-10-30 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "03f93180f630"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "d0eb318ce3e0"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "knowledge_items"
COLUMN = "vector_generation"

#: Each existing row's random generation, one UPDATE per dialect.
FILL = {
    "sqlite": f"UPDATE {TABLE} SET {COLUMN} = lower(hex(randomblob(16)))",
    "postgresql": f"UPDATE {TABLE} SET {COLUMN} = md5(random()::text || item_id)",
}
ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"


def _refuse_cascading_rebuild() -> None:
    """SQLite runs ON DELETE actions when a referenced table is dropped if
    foreign keys are enforced; the rebuild would then unlink every
    conversion draft from its item. env.py does not enforce them — refuse if
    something has turned them on."""
    if op.get_context().as_sql:
        return
    if op.get_bind().execute(text("PRAGMA foreign_keys")).scalar():
        raise RuntimeError(
            "022 rebuilds the knowledge_items table on SQLite and must run "
            "with PRAGMA foreign_keys=OFF: with it on, dropping the old table "
            "runs conversion_drafts' ON DELETE SET NULL."
        )


def upgrade() -> None:
    """Add ``vector_generation``, give every existing row a random one, then
    make it NOT NULL."""
    dialect = op.get_context().dialect.name
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=32), nullable=True))
    if dialect == "postgresql":
        op.execute(ROW_SECURITY_OFF)
        op.execute(FILL[dialect])
        op.execute(ROW_SECURITY_RESTORED)
        op.alter_column(
            TABLE, COLUMN, existing_type=sa.String(length=32), nullable=False
        )
        return
    op.execute(FILL[dialect])
    _refuse_cascading_rebuild()
    with op.batch_alter_table(TABLE, recreate="always") as batch:
        batch.alter_column(COLUMN, existing_type=sa.String(length=32), nullable=False)


def downgrade() -> None:
    """Drop the column."""
    op.drop_column(TABLE, COLUMN)

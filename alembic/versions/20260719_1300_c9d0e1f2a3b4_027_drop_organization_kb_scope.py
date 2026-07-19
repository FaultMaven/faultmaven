"""027_drop_organization_kb_scope

Removes the orphaned ``organization`` KB visibility scope. The canonical
3-tier knowledge model (ADR-013) is user-specific (``personal``), team
(``team``), and platform-wide (``global``); ``organization`` was a phantom
fourth tier that nothing wrote (routes/validator already reject it) and that
retrieval treated identically to ``global``.

Changes:
- ``knowledge_items``: drop ``organization`` from ``knowledge_items_scope_check``
  and move the ``scope`` server default off ``organization`` to ``global``
  (matching the KnowledgeItem domain default).
- ``conversion_jobs``: drop ``organization`` from ``conversion_jobs_scope_check``.
- Any pre-existing ``organization``-scoped rows are converted to ``global``,
  which is behavior-preserving: the inventory-visibility predicate already
  treated ``organization`` exactly like ``global`` (visible to everyone in the
  org).

New valid set (both tables): ``personal | team | global``.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-19 13:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the ``organization`` scope from the KB scope CHECKs + default.

    batch_alter_table is required for SQLite (no native ALTER for CHECK
    constraints or column defaults); on PostgreSQL it is a passthrough that
    issues the ALTER statements directly.
    """
    bind = op.get_bind()

    # Convert orphaned rows first so the copy into the rebuilt table (SQLite
    # batch mode) satisfies the new CHECK. The old CHECK still permits
    # 'global', so these UPDATEs are valid pre-rebuild.
    bind.execute(
        sa.text(
            "UPDATE knowledge_items SET scope = 'global' WHERE scope = 'organization'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE conversion_jobs SET scope = 'global' WHERE scope = 'organization'"
        )
    )

    with op.batch_alter_table("knowledge_items") as batch:
        batch.drop_constraint("knowledge_items_scope_check", type_="check")
        batch.create_check_constraint(
            "knowledge_items_scope_check",
            "scope IN ('personal', 'team', 'global')",
        )
        batch.alter_column(
            "scope",
            existing_type=sa.String(length=20),
            existing_nullable=False,
            server_default="global",
        )

    with op.batch_alter_table("conversion_jobs") as batch:
        batch.drop_constraint("conversion_jobs_scope_check", type_="check")
        batch.create_check_constraint(
            "conversion_jobs_scope_check",
            "scope IN ('personal', 'team', 'global')",
        )


def downgrade() -> None:
    """Restore ``organization`` in the KB scope CHECKs + default.

    Rows converted 'organization' -> 'global' on upgrade are NOT restored:
    the two were indistinguishable to retrieval and 'organization' was
    orphaned (no writer), so there is nothing to reconstruct.
    """
    with op.batch_alter_table("knowledge_items") as batch:
        batch.drop_constraint("knowledge_items_scope_check", type_="check")
        batch.create_check_constraint(
            "knowledge_items_scope_check",
            "scope IN ('personal', 'team', 'organization', 'global')",
        )
        batch.alter_column(
            "scope",
            existing_type=sa.String(length=20),
            existing_nullable=False,
            server_default="organization",
        )

    with op.batch_alter_table("conversion_jobs") as batch:
        batch.drop_constraint("conversion_jobs_scope_check", type_="check")
        batch.create_check_constraint(
            "conversion_jobs_scope_check",
            "scope IN ('personal', 'team', 'organization', 'global')",
        )

"""028_polymorphic_resource_shares

Replace the three nullable ``team_id`` columns (``cases``, ``knowledge_items``,
``conversion_jobs``) with a single polymorphic share-association table,
``resource_shares`` (ADR-013 §D4/D4a, adopting ADR-011 D2/D3).

A share row ``(resource_type, resource_id, scope_type, scope_id)`` means
"``resource_type``:``resource_id`` is visible to ``scope_type``:``scope_id``".
v1 shares only to teams; the CHECK sets already admit ``organization`` so the
D4a org-level extension is a new *row*, not a schema migration. Retrieval
(ADR-011 D3) resolves the visible-id allowlist from this table in SQL — it is
the single source of truth for team visibility; ChromaDB metadata keeps only
immutable facts (owner, personal/global).

No backfill: the ``team_id`` columns never had a live writer — ``team_service``
was wired only in Wave 4/U7 and stays ``None`` in standalone, and the multi-
tenant activation gate has never opened — so no ``team_id`` row exists in any
deployment to migrate. The columns are dropped outright.

``organization_id`` is denormalized onto every share row (RLS tenant key +
cross-org-share guard backstop) and enrolled in the standard tenant-isolation
policy. Polymorphic ``resource_id``/``scope_id`` carry no FK (no single target);
referential integrity is application-enforced (fail-safe: a dangling id matches
nothing in the allowlist join). PostgreSQL RLS only; SQLite (standalone) is
single-tenant.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-19 14:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables losing their nullable team_id share column.
_TEAM_ID_TABLES = ("cases", "knowledge_items", "conversion_jobs")


def upgrade() -> None:
    """Create ``resource_shares`` (+ RLS) and drop the three ``team_id`` columns."""
    op.create_table(
        "resource_shares",
        sa.Column("share_id", sa.String(length=36), nullable=False),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resource_type IN ('case', 'knowledge_item', 'conversion_job')",
            name="resource_shares_resource_type_check",
        ),
        sa.CheckConstraint(
            "scope_type IN ('team', 'organization')",
            name="resource_shares_scope_type_check",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("share_id"),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            "scope_type",
            "scope_id",
            name="uq_resource_shares_target",
        ),
    )
    op.create_index(
        "ix_resource_shares_organization_id", "resource_shares", ["organization_id"]
    )
    # Allowlist direction: scope(s) -> resource_ids (covering on PostgreSQL).
    op.create_index(
        "ix_resource_shares_scope",
        "resource_shares",
        ["scope_type", "scope_id", "resource_type", "resource_id"],
    )

    # RLS tenant isolation (PostgreSQL only) — same read-isolation policy as 018.
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "resource_shares" ENABLE ROW LEVEL SECURITY')
        op.execute(
            'CREATE POLICY "resource_shares_tenant_isolation" ON "resource_shares" '
            "USING (organization_id = current_setting('app.current_org_id', true))"
        )

    # Drop the retired team_id columns. Drop their indexes first: SQLite batch
    # mode rebuilds the table and would otherwise try to recreate an index over
    # the now-absent column. (conversion_jobs.team_id had no index.) On
    # PostgreSQL DROP COLUMN cascades the FK + index automatically, but dropping
    # the index explicitly first is harmless and keeps the two dialects aligned.
    op.drop_index("ix_cases_team_id", table_name="cases")
    op.drop_index("ix_knowledge_items_team_id", table_name="knowledge_items")
    for table in _TEAM_ID_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("team_id")


def downgrade() -> None:
    """Re-add the three ``team_id`` columns and drop ``resource_shares``."""
    for table in _TEAM_ID_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("team_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_team_id",
                "teams",
                ["team_id"],
                ["team_id"],
                ondelete="SET NULL",
            )
    # cases + knowledge_items had an index on team_id (conversion_jobs did not).
    op.create_index("ix_cases_team_id", "cases", ["team_id"])
    op.create_index("ix_knowledge_items_team_id", "knowledge_items", ["team_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            'DROP POLICY IF EXISTS "resource_shares_tenant_isolation" '
            'ON "resource_shares"'
        )
        op.execute('ALTER TABLE "resource_shares" DISABLE ROW LEVEL SECURITY')

    op.drop_table("resource_shares")

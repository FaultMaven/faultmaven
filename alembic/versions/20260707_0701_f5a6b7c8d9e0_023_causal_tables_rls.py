"""023_causal_tables_rls

Enrol the causal-graph tables in Row-Level Security tenant isolation.

Migration 018 (``018_rls_tenant_isolation``) enabled RLS + a
``<table>_tenant_isolation`` policy on every table carrying ``organization_id``.
The causal-graph tables were added *later* — migration 019 creates all three
(``causal_nodes``, ``causal_edges``, ``causal_node_evidence``), 020 augments
``causal_node_evidence`` — and each carries ``organization_id``, but they were
never enrolled — so in cloud (multi-tenant) mode the limited application role can
read causal-graph rows across organizations. Every other tenant table is covered;
this closes the gap by applying the identical policy 018 uses.

Read-isolation (USING) only, matching 018. Fail-closed: with no
``app.current_org_id`` set, ``current_setting(...)`` is NULL and no row matches.
PostgreSQL only — SQLite (Standalone) is single-tenant and has no RLS.

See 018's INFRA REQUIREMENT: RLS only bites for a non-superuser, non-owner role
(the app's ``faultmaven_app`` role); migrations/admin run as the owner and are
intentionally exempt.

Revision ID: f5a6b7c8d9e0
Revises: d3e4f5a6b7c8
Create Date: 2026-07-07 07:02:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Causal-graph tables carrying organization_id, added after migration 018.
_CAUSAL_TABLES = (
    "causal_nodes",
    "causal_edges",
    "causal_node_evidence",
)


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return  # SQLite (Standalone) is single-tenant; no RLS.
    for table in _CAUSAL_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            "USING (organization_id = current_setting('app.current_org_id', true))"
        )


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    for table in _CAUSAL_TABLES:
        op.execute(f'DROP POLICY IF EXISTS "{table}_tenant_isolation" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

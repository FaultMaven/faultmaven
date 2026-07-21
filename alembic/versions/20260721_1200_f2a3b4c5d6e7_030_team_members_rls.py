"""030_team_members_rls

PostgreSQL Row-Level Security for the ``team_members`` join table (ADR-010
forward-consolidation P2d). Follows the 018/023 tenant-isolation pattern.

``team_members`` carries NO ``organization_id`` column — it is a pure
``(user_id, team_id)`` join — so it cannot use the direct
``organization_id = current_setting(...)`` policy the other tenanted tables use
(018). Its organization is reached through its team: ``team_members.team_id ->
teams.team_id -> teams.organization_id``. The policy therefore scopes via a
subquery over ``teams``. A membership row is visible only when its team belongs
to the connection's current organization; cross-organization membership rows
(and, fail-closed, all rows when ``app.current_org_id`` is unset) are invisible.

The ``teams`` subquery is itself RLS-scoped (``teams`` is in 018's tenanted
set), by the same ``current_setting('app.current_org_id', true)`` predicate, so
the two policies agree by construction.

Read-isolation (USING) only, matching 018/023 — write-isolation (WITH CHECK) is
deferred to the hosted-IAM composed module that owns membership writes.

PostgreSQL only. SQLite (Standalone) is single-tenant and has no RLS, so this is
a no-op there. The same INFRA REQUIREMENT as 018 applies: RLS only takes effect
for a non-superuser, non-owner application role (enforced at startup by
``infrastructure/persistence/rls_role_guard.py`` when multi-tenant is enabled).

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-21 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable RLS + a subquery tenant-isolation policy on team_members (PG only)."""
    if op.get_context().dialect.name != "postgresql":
        return  # SQLite (Standalone) is single-tenant; no RLS.
    op.execute('ALTER TABLE "team_members" ENABLE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "team_members_tenant_isolation" ON "team_members" '
        "USING (team_id IN ("
        "SELECT team_id FROM teams "
        "WHERE organization_id = current_setting('app.current_org_id', true)"
        "))"
    )


def downgrade() -> None:
    """Drop the policy and disable RLS on team_members (PG only)."""
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(
        'DROP POLICY IF EXISTS "team_members_tenant_isolation" ON "team_members"'
    )
    op.execute('ALTER TABLE "team_members" DISABLE ROW LEVEL SECURITY')

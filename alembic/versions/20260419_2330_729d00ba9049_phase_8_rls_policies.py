"""phase_8_rls_policies

Storage redesign 2026-04 — Phase 8 (FINAL): Row-Level Security policies
for PostgreSQL.

This migration adds defense-in-depth tenant isolation at the PostgreSQL
level via Row-Level Security (RLS). Even a code path that forgets to
filter by ``organization_id`` cannot leak cross-tenant data — the
database itself enforces row visibility based on the per-connection
setting ``app.current_org_id``.

All Phase 8 changes are PostgreSQL-only — dialect-guarded; SQLite (local
deployment) is a documented no-op. SQLite has no RLS equivalent, and
Local Deployment has exactly one organization, so the practical risk is
zero. Per ``deployment-schema-strategy.md`` v2.2 §10 (Row-Level Security
for PostgreSQL).

Per-table effect on PostgreSQL:

1. ``ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;``
2. ``CREATE POLICY <table>_tenant_isolation ON <table>
   USING (organization_id = current_setting('app.current_org_id', true));``

The ``true`` second argument to ``current_setting`` returns ``NULL`` when
the GUC is unset rather than raising. The equality check then evaluates
to ``NULL`` (treated as no-match by RLS), so an unset-context session
sees zero rows — a safe failure mode.

RLS policies are applied to tenanted tables that own an
``organization_id`` column. Sub-tables without their own
``organization_id`` (currently: ``reports``, ``conversion_drafts``)
inherit isolation through their FK CASCADE to the parent table — when
RLS hides the parent row, orphaned sub-rows are unreachable via
legitimate queries.

The middleware wire-in for setting ``app.current_org_id`` per request is
in ``faultmaven/api/middleware/tenant_isolation.py``. RLS bypass roles
(for migrations, background workers, etc.) are an operational concern
deferred to deployment configuration — this migration intentionally
does not create them.

Tables receiving RLS (16):

- ``cases``
- ``case_messages``
- ``case_actions``
- ``case_tags``
- ``case_checkpoints``
- ``evidence``
- ``hypotheses``
- ``solutions``
- ``uploaded_files``
- ``investigation_sessions``
- ``agent_executions``
- ``agent_tool_calls``
- ``knowledge_items``
- ``knowledge_suggestions``
- ``conversion_jobs``
- ``user_audit_log``

Tables intentionally excluded:

- ``reports``, ``conversion_drafts`` — no own ``organization_id``;
  isolation inherited via FK CASCADE to parent.
- ``users``, ``roles``, ``permissions``, ``role_permissions``,
  ``organization_members``, ``team_members`` — RBAC tables, not
  tenant-scoped data; isolation via JWT scope, not RLS.
- ``organizations``, ``teams`` — tenancy entities themselves; RLS would
  create chicken-and-egg.
- ``oauth_revoked_tokens``, ``oauth_authorization_codes`` — auth
  artifacts, not tenanted business data.
- ``llm_config_overrides`` — global config, cloud-mode only, not
  tenant-scoped.

Revision ID: 729d00ba9049
Revises: 5a127abe7e28
Create Date: 2026-04-19 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "729d00ba9049"
down_revision: Union[str, Sequence[str], None] = "5a127abe7e28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tenanted tables that receive RLS policies. Each table owns an
# ``organization_id`` column (verified against
# ``faultmaven/infrastructure/persistence/models.py`` at Phase 8
# authoring time).
TENANTED_TABLES: tuple[str, ...] = (
    "cases",
    "case_messages",
    "case_actions",
    "case_tags",
    "case_checkpoints",
    "evidence",
    "hypotheses",
    "solutions",
    "uploaded_files",
    "investigation_sessions",
    "agent_executions",
    "agent_tool_calls",
    "knowledge_items",
    "knowledge_suggestions",
    "conversion_jobs",
    "user_audit_log",
)


def upgrade() -> None:
    """Enable RLS + create tenant_isolation policy on each tenanted table.

    PostgreSQL-only; SQLite is a documented no-op.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect != "postgresql":
        # RLS is PG-only. SQLite no-op (per strategy doc §1.1).
        return

    for table in TENANTED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (organization_id = current_setting('app.current_org_id', true))
            """
        )


def downgrade() -> None:
    """Drop tenant_isolation policies and disable RLS on each tenanted table.

    Order is reversed for symmetry with ``upgrade``; the actual order does
    not matter functionally because each policy/table is independent.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect != "postgresql":
        return

    for table in reversed(TENANTED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

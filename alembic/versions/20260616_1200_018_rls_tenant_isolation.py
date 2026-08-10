"""018_rls_tenant_isolation

PostgreSQL Row-Level Security for tenant isolation (ADR-010 forward-consolidation
P2). Enables RLS and a per-table ``<table>_tenant_isolation`` policy on every table
that carries ``organization_id`` (VARCHAR(36)), so a connection scoped via
``app.current_org_id`` can only read rows for its own organization. The scope is
applied by the engine's ``begin`` listener in
``faultmaven/infrastructure/persistence/database.py``, which sets the GUC once
per transaction from the tenant contextvar.

The policy is created with USING only and no ``FOR`` clause, which in PostgreSQL
means ``FOR ALL`` with the USING expression ALSO applied as the WITH CHECK on
writes — so INSERT/UPDATE of a row whose ``organization_id`` differs from the
session's ``app.current_org_id`` is rejected, not just hidden. Writers must stamp
rows with the current tenant-context org (see ``audit_repository.py`` for the
canonical pattern). (An earlier version of this docstring claimed write-isolation
was deferred — that was wrong about PostgreSQL's defaulting.)

Fail-closed: with no ``app.current_org_id`` set, ``current_setting(...)`` is NULL and
every row is filtered out, so the context MUST be set on every session (the
chokepoint sets it, defaulting to the Standalone org).

PostgreSQL only. SQLite (Standalone) is single-tenant and has no RLS, so this is a
no-op there.

⚠️ INFRA REQUIREMENT: RLS only takes effect for a connection that is NEITHER a
superuser NOR the table owner — PostgreSQL exempts both. The application's
production database role MUST be a non-superuser, non-owner role (provisioned by
faultmaven-enterprise-infra) or these policies are silently bypassed. Migrations
and admin run as the owner/superuser and are (intentionally) exempt.

Revision ID: c1f3a5b7d9e2
Revises: a7c9e1b3d5f7
Create Date: 2026-06-16 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c1f3a5b7d9e2"
down_revision: Union[str, Sequence[str], None] = "a7c9e1b3d5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every table carrying organization_id (VARCHAR(36)). `enterprises` is excluded
# (it has no organization_id — it is the parent tier, keyed by enterprise_id).
#
# Note: `user_audit_log.organization_id` is nullable; a NULL-org row matches no
# `app.current_org_id` and is therefore invisible to all tenants — fail-closed by
# design (org-less audit rows are not leaked cross-tenant). Audit writes should
# stamp an organization to keep those rows readable within their org.
_TENANTED_TABLES = (
    "organizations",
    "organization_members",
    "teams",
    "user_audit_log",
    "cases",
    "uploaded_files",
    "evidence",
    "evidence_needs",
    "evidence_need_fulfillment",
    "case_entities",
    "hypotheses",
    "hypothesis_evidence",
    "solutions",
    "case_messages",
    "case_actions",
    "case_tags",
    "case_checkpoints",
    "investigation_sessions",
    "agent_executions",
    "agent_tool_calls",
    "reports",
    "knowledge_items",
    "knowledge_suggestions",
    "conversion_jobs",
    "conversion_drafts",
)


def upgrade() -> None:
    """Enable RLS + a tenant-isolation policy per tenanted table (PostgreSQL only)."""
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite (Standalone) is single-tenant; no RLS.
    for table in _TENANTED_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            "USING (organization_id = current_setting('app.current_org_id', true))"
        )


def downgrade() -> None:
    """Drop the policies and disable RLS (PostgreSQL only)."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TENANTED_TABLES:
        op.execute(f'DROP POLICY IF EXISTS "{table}_tenant_isolation" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

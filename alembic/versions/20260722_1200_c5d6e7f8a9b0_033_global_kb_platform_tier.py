"""033_global_kb_platform_tier

Global-tier KB rows become the org-free platform corpus (issue #770).

The ``global`` scope is the platform-seeded knowledge tier, readable by every
tenant (doc-internal multi-tenancy model). Until now global rows were stamped
with the single-tenant default organization, which made them invisible under
``TENANT_PROVIDER=multi`` on both the RLS policy and the per-query SQL
predicates, while the org-free vector path still served their chunks to every
tenant — a split-brain. This migration makes the ownership model structural:

- ``knowledge_items.organization_id`` becomes NULLABLE, and a new CHECK
  constraint ``knowledge_items_global_org_check`` enforces
  ``(scope = 'global') ⟺ (organization_id IS NULL)`` — the platform corpus is
  owned by NO tenant org, and a tenant-org-stamped global row is
  unrepresentable. Existing global rows are normalized to NULL.
  (A dedicated "platform org" row was rejected: it would have to be seeded in
  every deployment mode, and reusing the standalone default org under multi
  would silently un-break the SSO JIT default-org FK failure that currently
  fails loudly — see issue #770.)

- The single FOR ALL policy ``knowledge_items_tenant_isolation`` is replaced by
  four per-command policies. Putting the global read exemption into a FOR ALL
  policy's USING would let any tenant UPDATE/DELETE platform rows, so reads and
  writes must split:

  * ``knowledge_items_tenant_read`` (SELECT): own-org rows OR ``scope='global'``
    — the platform-tier read exemption.
  * ``knowledge_items_tenant_insert`` / ``_update`` / ``_delete``: own-org rows,
    plus a global-write arm that is valid ONLY when the session's
    ``app.current_org_id`` is the single-tenant sentinel org. Single-tenant
    deployments (including cloud+single, today's production shape) bind every
    session to the sentinel, so the KB pack bootstrap and admin editing of
    global docs keep working through the limited app role. Under multi, every
    tenant session is bound to its real org (fail-closed request binder,
    ADR-010 P2), so the sentinel arm is unreachable for tenants; platform
    seeding runs on the audited BYPASSRLS maintenance path instead
    (``jobs/run.py kb_seed --cross-tenant-maintenance``).

Write-policy consequence under multi (intentional, fail-closed): tenant
sessions cannot modify global rows at all — including the helpful/not-helpful
feedback counters. Per-tenant feedback storage for the platform tier is a
follow-up.

PostgreSQL only for the policy changes; the nullability + CHECK changes apply
on both dialects (SQLite via batch mode, same pattern as 027).

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-22 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of faultmaven.config.constants.STANDALONE_ORG_ID (migrations are
# history — they must not import live app constants).
_STANDALONE_ORG_ID = "00000000-0000-0000-0000-000000000001"

_GLOBAL_ORG_CHECK = (
    "(scope = 'global' AND organization_id IS NULL) "
    "OR (scope <> 'global' AND organization_id IS NOT NULL)"
)

_ORG_MATCHES_SESSION = "organization_id = current_setting('app.current_org_id', true)"

# Existing-row arm for UPDATE/DELETE USING: the row's org IS NULL by the CHECK
# constraint, so matching on scope alone is sufficient; the session must be the
# single-tenant sentinel (standalone / cloud+single), never a bound tenant org.
_GLOBAL_ROW_SENTINEL_SESSION = (
    "(scope = 'global' "
    f"AND current_setting('app.current_org_id', true) = '{_STANDALONE_ORG_ID}')"
)

# New-row arm for INSERT/UPDATE WITH CHECK: additionally pins the incoming row
# shape (org-free) rather than relying on the CHECK constraint alone.
_GLOBAL_NEW_ROW_SENTINEL_SESSION = (
    "(scope = 'global' AND organization_id IS NULL "
    f"AND current_setting('app.current_org_id', true) = '{_STANDALONE_ORG_ID}')"
)


def upgrade() -> None:
    """Org-free global rows + per-command RLS policies with read exemption."""
    # 1) Nullability first, so the normalization UPDATE below is valid.
    with op.batch_alter_table("knowledge_items") as batch:
        batch.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )

    # 2) Normalize: the platform corpus is owned by no tenant org.
    op.execute(
        "UPDATE knowledge_items SET organization_id = NULL WHERE scope = 'global'"
    )

    # 3) Make the ownership model unrepresentable-wrong.
    with op.batch_alter_table("knowledge_items") as batch:
        batch.create_check_constraint(
            "knowledge_items_global_org_check",
            _GLOBAL_ORG_CHECK,
        )

    # 4) Per-command RLS policies (PostgreSQL only).
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        'DROP POLICY IF EXISTS "knowledge_items_tenant_isolation" ON "knowledge_items"'
    )
    op.execute(
        'CREATE POLICY "knowledge_items_tenant_read" ON "knowledge_items" '
        "FOR SELECT USING "
        f"({_ORG_MATCHES_SESSION} OR scope = 'global')"
    )
    op.execute(
        'CREATE POLICY "knowledge_items_tenant_insert" ON "knowledge_items" '
        "FOR INSERT WITH CHECK "
        f"({_ORG_MATCHES_SESSION} OR {_GLOBAL_NEW_ROW_SENTINEL_SESSION})"
    )
    op.execute(
        'CREATE POLICY "knowledge_items_tenant_update" ON "knowledge_items" '
        "FOR UPDATE USING "
        f"({_ORG_MATCHES_SESSION} OR {_GLOBAL_ROW_SENTINEL_SESSION}) "
        "WITH CHECK "
        f"({_ORG_MATCHES_SESSION} OR {_GLOBAL_NEW_ROW_SENTINEL_SESSION})"
    )
    op.execute(
        'CREATE POLICY "knowledge_items_tenant_delete" ON "knowledge_items" '
        "FOR DELETE USING "
        f"({_ORG_MATCHES_SESSION} OR {_GLOBAL_ROW_SENTINEL_SESSION})"
    )


def downgrade() -> None:
    """Restore the single FOR ALL policy and the NOT NULL org column.

    Global rows are restamped to the single-tenant default org — this requires
    that org row to exist (true in any standalone/cloud-single DB, where the
    bootstrap seeds it; a multi DB has no business downgrading below the
    platform-tier model).
    """
    if op.get_bind().dialect.name == "postgresql":
        for policy in (
            "knowledge_items_tenant_read",
            "knowledge_items_tenant_insert",
            "knowledge_items_tenant_update",
            "knowledge_items_tenant_delete",
        ):
            op.execute(f'DROP POLICY IF EXISTS "{policy}" ON "knowledge_items"')
        op.execute(
            'CREATE POLICY "knowledge_items_tenant_isolation" ON "knowledge_items" '
            f"USING ({_ORG_MATCHES_SESSION})"
        )

    op.execute(
        "UPDATE knowledge_items "
        f"SET organization_id = '{_STANDALONE_ORG_ID}' "
        "WHERE scope = 'global'"
    )
    with op.batch_alter_table("knowledge_items") as batch:
        batch.drop_constraint("knowledge_items_global_org_check", type_="check")
        batch.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )

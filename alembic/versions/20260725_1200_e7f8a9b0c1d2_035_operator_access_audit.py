"""035_operator_access_audit

Durable, append-only audit table for platform-operator access to tenant data
(ADR-012 D8/D9, faultmaven#813). Replaces the structured log line as the
system of record: a log line is not queryable as SOC 2 / ISO 27001 evidence and
does not survive log rotation.

**Why not reuse ``user_audit_log``.** That table is RLS-tenanted (migration 018
keys its policy on ``organization_id``), so it structurally cannot hold a
cross-tenant event — an operator list spanning every tenant has no single
organization to stamp, and a NULL is rejected by the policy's WITH CHECK. This
table is therefore separate and deliberately *not* tenant-scoped, with a
nullable ``target_organization_id`` where NULL means "spanned all tenants".

**Append-only is enforced by the database, not by convention.** UPDATE and
DELETE are rejected by triggers on both engines. The threat this addresses is
the audited operator themselves: an operator who can amend or erase their own
access record leaves the table with no evidentiary value. Table-level GRANTs
would be the other lever, but those are managed outside migrations (the
``faultmaven_app`` role is provisioned by infrastructure), so a trigger is the
control that actually ships with the schema in every deployment.

Retention/erasure, when it is needed, is an owner-role operation: the owner can
drop the triggers deliberately. That is the intended escape hatch — an explicit,
privileged, auditable act rather than an ambient capability of the app role.

No RLS policy is added. Tenant-scoping a cross-tenant audit trail would hide
exactly the rows it exists to record, and the table holds no tenant content —
only identifiers, an action, and counts.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-07-25 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_APPEND_ONLY_MESSAGE = "operator_access_audit is append-only"


def upgrade() -> None:
    """Create the table, its indexes, and the append-only triggers."""
    op.create_table(
        "operator_access_audit",
        sa.Column("audit_id", sa.Integer(), autoincrement=True, nullable=False),
        # Deliberately NOT a foreign key to users.user_id. Evidence must outlive
        # the account it describes, and every ondelete action is executed as a
        # write against this table, which the append-only triggers below reject:
        # ON DELETE SET NULL would make deleting an audited operator fail, and
        # ON DELETE CASCADE would try to erase the evidence. The denormalised
        # operator_username keeps the trail readable after the account is gone.
        sa.Column("operator_user_id", sa.String(length=36), nullable=True),
        sa.Column("operator_username", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_organization_id", sa.String(length=36), nullable=True),
        sa.Column("target_case_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("grant_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deployment_mode", sa.String(length=32), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_id"),
        # Constrain the governed distinction (metadata vs content) at the schema
        # layer, so a typo cannot silently create a third, unaudited category.
        sa.CheckConstraint(
            "action IN ('list', 'content_open')",
            name="operator_access_audit_action_valid",
        ),
    )
    # Indexed for the queries that exist: the default newest-first listing and
    # the three filters the review path offers. `action` is deliberately not
    # indexed (the CHECK pins it to two values, so it is ~50% selective and
    # loses to created_at under the ORDER BY), nor is `grant_id` (100% NULL
    # until break-glass); on an append-only table an index no query uses is
    # pure write amplification. #815 adds the grant_id index with the reader.
    op.create_index(
        "ix_operator_access_audit_created_at", "operator_access_audit", ["created_at"]
    )
    op.create_index(
        "ix_operator_access_audit_operator",
        "operator_access_audit",
        ["operator_user_id", "created_at"],
    )
    op.create_index(
        "ix_operator_access_audit_target_org",
        "operator_access_audit",
        ["target_organization_id", "created_at"],
    )
    op.create_index(
        "ix_operator_access_audit_case",
        "operator_access_audit",
        ["target_case_id", "created_at"],
    )

    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_audit_append_only()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_APPEND_ONLY_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER operator_access_audit_no_{event.lower()} "
                f"BEFORE {event} ON operator_access_audit "
                "FOR EACH ROW EXECUTE FUNCTION operator_access_audit_append_only()"
            )
    elif dialect == "sqlite":
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER operator_access_audit_no_{event.lower()} "
                f"BEFORE {event} ON operator_access_audit "
                f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MESSAGE}'); END"
            )


def downgrade() -> None:
    """Drop the table, and the PostgreSQL function it left behind.

    Both engines drop a table's own indexes and triggers with it, so only the
    standalone plpgsql function needs explicit teardown.
    """
    op.drop_table("operator_access_audit")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS operator_access_audit_append_only()")

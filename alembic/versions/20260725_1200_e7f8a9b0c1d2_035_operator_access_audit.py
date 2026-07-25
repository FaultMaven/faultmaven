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
        # SET NULL, not CASCADE: removing an operator account must never remove
        # the evidence of what that account did.
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
        sa.ForeignKeyConstraint(
            ["operator_user_id"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("audit_id"),
        # Constrain the governed distinction (metadata vs content) at the schema
        # layer, so a typo cannot silently create a third, unaudited category.
        sa.CheckConstraint(
            "action IN ('list', 'content_open')",
            name="operator_access_audit_action_valid",
        ),
    )
    op.create_index(
        "ix_operator_access_audit_action", "operator_access_audit", ["action"]
    )
    op.create_index(
        "ix_operator_access_audit_grant_id", "operator_access_audit", ["grant_id"]
    )
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
        op.execute(
            "CREATE TRIGGER operator_access_audit_no_update "
            "BEFORE UPDATE ON operator_access_audit "
            "FOR EACH ROW EXECUTE FUNCTION operator_access_audit_append_only()"
        )
        op.execute(
            "CREATE TRIGGER operator_access_audit_no_delete "
            "BEFORE DELETE ON operator_access_audit "
            "FOR EACH ROW EXECUTE FUNCTION operator_access_audit_append_only()"
        )
    elif dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER operator_access_audit_no_update "
            "BEFORE UPDATE ON operator_access_audit "
            f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MESSAGE}'); END"
        )
        op.execute(
            "CREATE TRIGGER operator_access_audit_no_delete "
            "BEFORE DELETE ON operator_access_audit "
            f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MESSAGE}'); END"
        )


def downgrade() -> None:
    """Drop the triggers, then the table.

    The triggers must go first: on PostgreSQL a DELETE-blocking trigger does not
    prevent DROP TABLE, but dropping them explicitly keeps the teardown
    symmetric with upgrade() and leaves no orphaned function behind.
    """
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        # PostgreSQL scopes DROP TRIGGER to a table; SQLite does not.
        for trigger in ("no_update", "no_delete"):
            op.execute(
                f"DROP TRIGGER IF EXISTS operator_access_audit_{trigger} "
                "ON operator_access_audit"
            )
        op.execute("DROP FUNCTION IF EXISTS operator_access_audit_append_only()")
    elif dialect == "sqlite":
        for trigger in ("no_update", "no_delete"):
            op.execute(f"DROP TRIGGER IF EXISTS operator_access_audit_{trigger}")

    op.drop_index("ix_operator_access_audit_case", table_name="operator_access_audit")
    op.drop_index(
        "ix_operator_access_audit_target_org", table_name="operator_access_audit"
    )
    op.drop_index(
        "ix_operator_access_audit_operator", table_name="operator_access_audit"
    )
    op.drop_index(
        "ix_operator_access_audit_created_at", table_name="operator_access_audit"
    )
    op.drop_index(
        "ix_operator_access_audit_grant_id", table_name="operator_access_audit"
    )
    op.drop_index("ix_operator_access_audit_action", table_name="operator_access_audit")
    op.drop_table("operator_access_audit")

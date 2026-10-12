"""017_drop_investigation_sessions

Retires the investigation-sessions subsystem: ``investigation_sessions`` is
dropped (#1935, owner ruling 2026-10-12).

The table was scaffolded as the top of a four-level hierarchy, Case →
InvestigationSession → AgentExecution → AgentToolCall, for an agent orchestrator
that no longer exists. The three lower levels went in #982 and #1001; the
session row kept counters nothing wrote (``total_token_usage``,
``total_agent_executions``, #1696) and a budget nothing enforced. What it was
meant to hold is kept elsewhere, as data that is read: grouping per period in
``turn_history`` / ``case_messages`` / ``turn_receipts``, token spend in
``llm_turn_spend`` / ``llm_usage_daily``, pause and resume in the case state,
and "one active session" in the case's single driver (ADR-020). No first-party
client, engine path, report, job or operator surface ever read or wrote a row;
the only rows were written by tests and probes. The routes that served it
(``/api/v1/cases/{case_id}/sessions…``) are removed in the same change.

The ``sessions:*`` permissions
------------------------------

The baseline also seeded four permissions for the subsystem — ``sessions:read``,
``sessions:create``, ``sessions:execute`` (named for the long-gone
``/sessions/{sid}/execute``) and ``sessions:manage`` (pause, resume, complete) —
and granted them to the system roles: all four to ``admin`` and ``member``,
``sessions:read`` to ``viewer``. Nothing ever checked one; the retired routes
gated on the case instead. This revision deletes their grants from
``role_permissions`` (every grant, a non-system role's included), then the four
``permissions`` rows, matched on ``resource = 'sessions'``. The grants go first
and explicitly: SQLite runs the ``ON DELETE CASCADE`` on ``role_permissions``
only when ``PRAGMA foreign_keys`` is on. Neither table is enrolled in row-level
security (``_TENANT_TABLES`` in the baseline does not list them, and no later
revision enrols them), so no ``row_security`` setting is needed on PostgreSQL.

Dialects
--------

``DROP TABLE`` on both. Nothing references ``investigation_sessions`` by foreign
key (it only references ``cases``, ``enterprises``, ``organizations`` and
``users``), so the drop runs no ON DELETE action on SQLite whatever ``PRAGMA
foreign_keys`` says, and it needs no guard. On PostgreSQL the table's RLS policy
(``investigation_sessions_tenant_isolation``) and its indexes go with it; no
other revision enrols the table anywhere, so there is nothing to disable first.

``downgrade()``
---------------

Recreates the table exactly as the baseline (``001_enterprise_baseline``) left
it at revision ``b5334d07ae6c``: columns, CHECKs, foreign keys, primary key, the
six indexes, and on PostgreSQL row-level security with the plain tenant policy.
Copied from the baseline, not imported, so a later edit there cannot move it. It
comes back EMPTY: the rows this revision dropped are not recoverable, which is
the ruling (nothing read them). It then re-seeds exactly the baseline's four
``sessions:*`` permission rows and its nine grants of them to the system roles,
with the baseline's ids, copied rather than imported; a grant to a non-system
role that the upgrade deleted is not restored.

Revision ID: f95a5b3a58eb
Revises: b5334d07ae6c
Create Date: 2026-10-12 13:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f95a5b3a58eb"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "b5334d07ae6c"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "investigation_sessions"
#: The baseline's tenant policy expression, verbatim.
_ENTERPRISE_MATCHES_SESSION = (
    "enterprise_id = current_setting('app.current_enterprise_id', true)"
)


#: The baseline's ``sessions:*`` permission rows: value -> permission_id.
_SESSION_PERMISSIONS = {
    "sessions:read": "f7c7be05-bf80-5504-832d-210827a0e450",
    "sessions:create": "cd3ba768-fd85-5c10-83da-830194251e29",
    "sessions:execute": "7123672b-6cb6-529d-87b1-6c74101c44a3",
    "sessions:manage": "7a9a6bde-3a6a-5086-b5c1-7703d524ce59",
}
#: The baseline's grants of them: system role_id -> permission values.
_SESSION_GRANTS = {
    # admin
    "50551907-a02c-5bf7-9aa4-4a98f3c4eb64": list(_SESSION_PERMISSIONS),
    # member
    "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72": list(_SESSION_PERMISSIONS),
    # viewer
    "834b74a5-33a7-5248-9fd0-b040c12aef7b": ["sessions:read"],
}


def upgrade() -> None:
    """Drop the table (its indexes and, on PostgreSQL, its policy go with it),
    then retire the ``sessions:*`` grants and permissions."""
    op.drop_table(TABLE)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT permission_id FROM permissions WHERE resource = 'sessions')"
    )
    op.execute("DELETE FROM permissions WHERE resource = 'sessions'")


def downgrade() -> None:
    """Recreate the table, empty, exactly as the baseline defined it."""
    op.create_table(
        TABLE,
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "state", sa.String(length=32), server_default="active", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column("session_goal", sa.Text(), nullable=True),
        sa.Column("findings_summary", sa.Text(), nullable=True),
        sa.Column(
            "total_token_usage", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "total_agent_executions", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("token_budget_limit", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'completed', 'abandoned')",
            name="investigation_sessions_state_check",
        ),
        sa.CheckConstraint(
            "token_budget_limit IS NULL OR token_budget_limit >= 0",
            name="investigation_sessions_budget_check",
        ),
        sa.CheckConstraint(
            "total_agent_executions >= 0",
            name="investigation_sessions_executions_check",
        ),
        sa.CheckConstraint(
            "total_duration_ms IS NULL OR total_duration_ms >= 0",
            name="investigation_sessions_duration_check",
        ),
        sa.CheckConstraint(
            "total_token_usage >= 0", name="investigation_sessions_token_usage_check"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(op.f("ix_investigation_sessions_case_id"), TABLE, ["case_id"])
    op.create_index(op.f("ix_investigation_sessions_created_at"), TABLE, ["created_at"])
    op.create_index(
        op.f("ix_investigation_sessions_enterprise_id"), TABLE, ["enterprise_id"]
    )
    op.create_index(
        op.f("ix_investigation_sessions_organization_id"), TABLE, ["organization_id"]
    )
    op.create_index(op.f("ix_investigation_sessions_state"), TABLE, ["state"])
    op.create_index(op.f("ix_investigation_sessions_user_id"), TABLE, ["user_id"])

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{TABLE}_tenant_isolation" ON "{TABLE}" '
            f"USING ({_ENTERPRISE_MATCHES_SESSION})"
        )

    permissions = sa.table(
        "permissions",
        sa.column("permission_id", sa.String),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.Text),
    )
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.String),
        sa.column("permission_id", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {
                "permission_id": permission_id,
                "resource": value.split(":", 1)[0],
                "action": value.split(":", 1)[1],
                "description": None,
            }
            for value, permission_id in _SESSION_PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": role_id, "permission_id": _SESSION_PERMISSIONS[value]}
            for role_id, values in _SESSION_GRANTS.items()
            for value in values
        ],
    )

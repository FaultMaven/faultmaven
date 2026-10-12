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
the ruling (nothing read them).

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


def upgrade() -> None:
    """Drop the table; its indexes and (on PostgreSQL) its policy go with it."""
    op.drop_table(TABLE)


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

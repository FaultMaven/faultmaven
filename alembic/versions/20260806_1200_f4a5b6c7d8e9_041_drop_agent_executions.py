"""041: drop agent_executions and agent_tool_calls

The tables backed ``AgentOrchestrationService``, which was deleted in #982 as an
unused second implementation of the investigation path. It was the only writer.
The three service methods that read executions — ``get_case_with_details``'s
executions branch, ``InvestigationSessionService.get_session_with_executions`` and
``.add_execution_to_session`` — had no callers of their own, so no request path
reached either table: nothing created a row and nothing rendered one.

The live investigation path (``milestone_engine`` behind the Case module's
``/turns`` endpoints) has no execution-audit trail. That is a deliberate open
question, not something this schema was answering: these tables model the deleted
orchestrator's execution → tool-call chain, and a turn audit would key on turn and
milestone semantics instead. Keeping an unwritten schema so a future feature might
adopt it costs more than it saves — the health checker validates perpetually empty
tables, RLS policies guard nothing, and every reader takes the models for live.

RLS: the per-table policies created in 018 are dropped implicitly with the tables.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-08-06 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the tool-call table first — it carries the FK to executions."""
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_executions")


def downgrade() -> None:
    """Recreate both tables, empty, matching the 001 baseline definition.

    Restoring the schema restores nothing else: the code that wrote these rows is
    gone, so a downgraded deployment has the tables and still no writer. This
    exists so the migration chain stays reversible, not because rolling back
    recovers a capability.
    """
    op.create_table(
        "agent_executions",
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("agent_model", sa.String(length=128), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="queued", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_duration_ms", sa.Integer(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "token_usage",
            sa.Text().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            sa.Text().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
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
            "agent_type IN ('investigator', 'debugger', 'researcher', "
            "'validator', 'reporter', 'custom')",
            name="agent_executions_agent_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', "
            "'cancelled', 'timeout')",
            name="agent_executions_status_check",
        ),
        sa.CheckConstraint(
            "execution_duration_ms IS NULL OR execution_duration_ms >= 0",
            name="agent_executions_duration_check",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.case_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["investigation_sessions.session_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("execution_id"),
    )
    op.create_table(
        "agent_tool_calls",
        sa.Column("tool_call_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column(
            "tool_input",
            sa.Text().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "tool_output",
            sa.Text().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "status", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
            "status IN ('pending', 'running', 'success', 'failed')",
            name="agent_tool_calls_status_check",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="agent_tool_calls_duration_check",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["agent_executions.execution_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tool_call_id"),
    )

    if op.get_bind().dialect.name == "postgresql":
        # Mirror 018 for the recreated tables; dropping them took their policies.
        for table in ("agent_executions", "agent_tool_calls"):
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(
                f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
                "USING (organization_id = current_setting('app.current_org_id', true))"
            )

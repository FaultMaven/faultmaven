"""phase_1_drop_dead_tables_columns

Storage redesign 2026-04 — Phase 1: pure deletions of dead/dormant code.

Drops:
1. `agent_tool_calls` table (v1) — zero functional usage. Only schema definition
   plus an unused `CaseModel.tool_calls` relationship. The v2 table
   (`agent_tool_calls_v2`) is the actively-used tool-call log; v2 will be
   renamed to canonical `agent_tool_calls` in Phase 4.
2. `cases.degraded_mode` column — marked deprecated in code with comment
   "Deprecated: DegradedMode removed from domain model. Column retained for
   backward compatibility." Per the project no-backward-compat directive,
   the column is removed.

Note on `kb_documents` and related sharing tables: the `PostgreSQLKBDocumentRepository`
file was orphan code targeting tables that never existed in any migration. The
repository file is deleted in this phase (no schema change needed for the
non-existent tables).

See deployment-schema-strategy.md §12 decisions #5, #13 for the locked design.

Revision ID: eb98f7b39fbc
Revises: d9e5f3a2b4c8
Create Date: 2026-04-19 11:10:54.878074

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "eb98f7b39fbc"
down_revision: Union[str, Sequence[str], None] = "d9e5f3a2b4c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop agent_tool_calls v1 table and cases.degraded_mode column."""
    # Drop v1 indexes first (PG enforces this strictly; SQLite is permissive)
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.drop_index("ix_agent_tool_calls_tool_name", table_name="agent_tool_calls")
        op.drop_index("ix_agent_tool_calls_status", table_name="agent_tool_calls")
        op.drop_index(
            "ix_agent_tool_calls_organization_id", table_name="agent_tool_calls"
        )
        op.drop_index("ix_agent_tool_calls_case_id", table_name="agent_tool_calls")

    op.drop_table("agent_tool_calls")

    # Drop cases.degraded_mode column.
    # Note: SQLite supports DROP COLUMN since 3.35. SQLAlchemy + alembic emit
    # a batch-mode rebuild on older SQLite; modern SQLite handles it natively.
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.drop_column("degraded_mode")


def downgrade() -> None:
    """Recreate the dropped table and column.

    Recreated to match the v1 baseline schema. Note that recreating the v1
    table here does NOT restore the agent's `CaseModel.tool_calls`
    relationship in the ORM — that is also gone. This downgrade is provided
    for completeness; in practice, post-redesign rollback should be a full
    schema reset rather than a stepwise downgrade.
    """
    import sqlalchemy as sa

    # Recreate cases.degraded_mode column
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("degraded_mode", sa.Text(), nullable=True))

    # Recreate agent_tool_calls v1 table
    op.create_table(
        "agent_tool_calls",
        sa.Column("call_id", sa.String(length=20), nullable=False),
        sa.Column("case_id", sa.String(length=17), nullable=False),
        sa.Column(
            "organization_id",
            sa.String(length=64),
            nullable=False,
            server_default="00000000-0000-0000-0000-000000000001",
        ),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("tool_input", sa.Text(), nullable=False),
        sa.Column("tool_output", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True, server_default="{}"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id"),
        sa.CheckConstraint(
            "LENGTH(TRIM(tool_name)) > 0",
            name="agent_tool_calls_tool_name_not_empty",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'error')",
            name="agent_tool_calls_status_valid",
        ),
    )
    op.create_index(
        "ix_agent_tool_calls_case_id",
        "agent_tool_calls",
        ["case_id"],
    )
    op.create_index(
        "ix_agent_tool_calls_organization_id",
        "agent_tool_calls",
        ["organization_id"],
    )
    op.create_index(
        "ix_agent_tool_calls_status",
        "agent_tool_calls",
        ["status"],
    )
    op.create_index(
        "ix_agent_tool_calls_tool_name",
        "agent_tool_calls",
        ["tool_name"],
    )

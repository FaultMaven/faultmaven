"""004_add_agent_executions

Add agent execution tracking tables for AI agent transparency and debugging.

This migration:
- Creates the 'agent_executions' table for tracking agent execution lifecycle
- Creates the 'agent_tool_calls_v2' table for tracking individual tool invocations
- Adds foreign keys with CASCADE delete (executions → cases, tool_calls → executions)
- Creates indexes for efficient queries by case, status, type, and date
- Supports both PostgreSQL (with native types) and SQLite (for development)

Note: agent_tool_calls_v2 is used to avoid conflict with the existing agent_tool_calls table
from the baseline migration. The new table has a different structure optimized for
execution-level tracking rather than case-level tracking.

Revision ID: 004_agent_executions
Revises: 003_evidence_artifacts
Create Date: 2025-12-29 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "004_agent_executions"
down_revision: Union[str, Sequence[str], None] = "003_evidence_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """
    Apply agent execution tracking migration.

    Creates agent_executions and agent_tool_calls_v2 tables with foreign keys.
    """
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """
    Remove agent execution tracking schema objects.

    Drops the agent_tool_calls_v2 and agent_executions tables and all associated constraints.
    """
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema for agent executions."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Table: agent_executions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_executions (
            execution_id VARCHAR(64) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL,
            agent_type VARCHAR(64) NOT NULL,
            agent_model VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            execution_duration_ms INTEGER,
            prompt TEXT,
            response TEXT,
            error_message TEXT,
            token_usage JSONB,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_executions_status_valid CHECK (
                status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timeout')
            ),
            CONSTRAINT agent_executions_agent_type_valid CHECK (
                agent_type IN ('investigator', 'debugger', 'researcher', 'validator', 'reporter', 'custom')
            ),
            CONSTRAINT agent_executions_duration_non_negative CHECK (
                execution_duration_ms IS NULL OR execution_duration_ms >= 0
            ),
            CONSTRAINT agent_executions_case_id_not_empty CHECK (LENGTH(TRIM(case_id)) > 0),
            CONSTRAINT agent_executions_agent_model_not_empty CHECK (LENGTH(TRIM(agent_model)) > 0)
        )
    """))

    # Indexes for agent_executions
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_case_id "
        "ON agent_executions(case_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_status "
        "ON agent_executions(status)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_created_at "
        "ON agent_executions(created_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_agent_type "
        "ON agent_executions(agent_type)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_agent_model "
        "ON agent_executions(agent_model)"
    ))

    # Foreign key to cases with CASCADE delete
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_agent_executions_case_id'
            ) THEN
                ALTER TABLE agent_executions
                ADD CONSTRAINT fk_agent_executions_case_id
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Table: agent_tool_calls_v2 (execution-level tool calls)
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_tool_calls_v2 (
            tool_call_id VARCHAR(64) PRIMARY KEY,
            execution_id VARCHAR(64) NOT NULL,
            tool_name VARCHAR(128) NOT NULL,
            tool_input JSONB,
            tool_output JSONB,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            error_message TEXT,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            duration_ms INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_tool_calls_v2_status_valid CHECK (
                status IN ('pending', 'running', 'success', 'failed')
            ),
            CONSTRAINT agent_tool_calls_v2_duration_non_negative CHECK (
                duration_ms IS NULL OR duration_ms >= 0
            ),
            CONSTRAINT agent_tool_calls_v2_tool_name_not_empty CHECK (LENGTH(TRIM(tool_name)) > 0)
        )
    """))

    # Indexes for agent_tool_calls_v2
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_v2_execution_id "
        "ON agent_tool_calls_v2(execution_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_v2_tool_name "
        "ON agent_tool_calls_v2(tool_name)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_v2_status "
        "ON agent_tool_calls_v2(status)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_v2_created_at "
        "ON agent_tool_calls_v2(created_at DESC)"
    ))

    # Foreign key to agent_executions with CASCADE delete
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_agent_tool_calls_v2_execution_id'
            ) THEN
                ALTER TABLE agent_tool_calls_v2
                ADD CONSTRAINT fk_agent_tool_calls_v2_execution_id
                FOREIGN KEY (execution_id) REFERENCES agent_executions(execution_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Triggers: auto-update updated_at timestamps
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_agent_executions_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS agent_executions_update_timestamp ON agent_executions;
        CREATE TRIGGER agent_executions_update_timestamp
            BEFORE UPDATE ON agent_executions
            FOR EACH ROW
            EXECUTE FUNCTION update_agent_executions_updated_at()
    """))

    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_agent_tool_calls_v2_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS agent_tool_calls_v2_update_timestamp ON agent_tool_calls_v2;
        CREATE TRIGGER agent_tool_calls_v2_update_timestamp
            BEFORE UPDATE ON agent_tool_calls_v2
            FOR EACH ROW
            EXECUTE FUNCTION update_agent_tool_calls_v2_updated_at()
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL agent execution objects."""
    conn = op.get_bind()

    # Drop triggers
    conn.execute(text(
        "DROP TRIGGER IF EXISTS agent_tool_calls_v2_update_timestamp "
        "ON agent_tool_calls_v2 CASCADE"
    ))
    conn.execute(text(
        "DROP TRIGGER IF EXISTS agent_executions_update_timestamp "
        "ON agent_executions CASCADE"
    ))

    # Drop functions
    conn.execute(text("DROP FUNCTION IF EXISTS update_agent_tool_calls_v2_updated_at() CASCADE"))
    conn.execute(text("DROP FUNCTION IF EXISTS update_agent_executions_updated_at() CASCADE"))

    # Drop foreign key constraints
    conn.execute(text("""
        ALTER TABLE agent_tool_calls_v2
        DROP CONSTRAINT IF EXISTS fk_agent_tool_calls_v2_execution_id
    """))
    conn.execute(text("""
        ALTER TABLE agent_executions
        DROP CONSTRAINT IF EXISTS fk_agent_executions_case_id
    """))

    # Drop indexes for agent_tool_calls_v2
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_tool_calls_v2_created_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_tool_calls_v2_status"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_tool_calls_v2_tool_name"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_tool_calls_v2_execution_id"))

    # Drop indexes for agent_executions
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_executions_agent_model"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_executions_agent_type"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_executions_created_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_executions_status"))
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_executions_case_id"))

    # Drop tables (order matters due to FK)
    conn.execute(text("DROP TABLE IF EXISTS agent_tool_calls_v2 CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS agent_executions CASCADE"))


# =============================================================================
# SQLite Implementation (Development)
# =============================================================================


def _upgrade_sqlite() -> None:
    """Create SQLite schema for agent executions."""

    # Table: agent_executions
    op.create_table(
        "agent_executions",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("case_id", sa.String(17), nullable=False),
        sa.Column("agent_type", sa.String(64), nullable=False),
        sa.Column("agent_model", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("execution_duration_ms", sa.Integer, nullable=True),
        sa.Column("prompt", sa.Text, nullable=True),
        sa.Column("response", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("token_usage", sa.Text, nullable=True),  # JSON as TEXT
        sa.Column("metadata", sa.Text, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.case_id"],
            name="fk_agent_executions_case_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for agent_executions
    op.create_index(
        "idx_agent_executions_case_id",
        "agent_executions",
        ["case_id"],
    )
    op.create_index(
        "idx_agent_executions_status",
        "agent_executions",
        ["status"],
    )
    op.create_index(
        "idx_agent_executions_created_at",
        "agent_executions",
        ["created_at"],
    )
    op.create_index(
        "idx_agent_executions_agent_type",
        "agent_executions",
        ["agent_type"],
    )
    op.create_index(
        "idx_agent_executions_agent_model",
        "agent_executions",
        ["agent_model"],
    )

    # Table: agent_tool_calls_v2
    op.create_table(
        "agent_tool_calls_v2",
        sa.Column("tool_call_id", sa.String(64), primary_key=True),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_input", sa.Text, nullable=True),  # JSON as TEXT
        sa.Column("tool_output", sa.Text, nullable=True),  # JSON as TEXT
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["agent_executions.execution_id"],
            name="fk_agent_tool_calls_v2_execution_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for agent_tool_calls_v2
    op.create_index(
        "idx_agent_tool_calls_v2_execution_id",
        "agent_tool_calls_v2",
        ["execution_id"],
    )
    op.create_index(
        "idx_agent_tool_calls_v2_tool_name",
        "agent_tool_calls_v2",
        ["tool_name"],
    )
    op.create_index(
        "idx_agent_tool_calls_v2_status",
        "agent_tool_calls_v2",
        ["status"],
    )
    op.create_index(
        "idx_agent_tool_calls_v2_created_at",
        "agent_tool_calls_v2",
        ["created_at"],
    )


def _downgrade_sqlite() -> None:
    """Remove SQLite agent execution objects."""

    # Drop indexes for agent_tool_calls_v2
    op.drop_index("idx_agent_tool_calls_v2_created_at", table_name="agent_tool_calls_v2")
    op.drop_index("idx_agent_tool_calls_v2_status", table_name="agent_tool_calls_v2")
    op.drop_index("idx_agent_tool_calls_v2_tool_name", table_name="agent_tool_calls_v2")
    op.drop_index("idx_agent_tool_calls_v2_execution_id", table_name="agent_tool_calls_v2")

    # Drop indexes for agent_executions
    op.drop_index("idx_agent_executions_agent_model", table_name="agent_executions")
    op.drop_index("idx_agent_executions_agent_type", table_name="agent_executions")
    op.drop_index("idx_agent_executions_created_at", table_name="agent_executions")
    op.drop_index("idx_agent_executions_status", table_name="agent_executions")
    op.drop_index("idx_agent_executions_case_id", table_name="agent_executions")

    # Drop tables (order matters due to FK)
    op.drop_table("agent_tool_calls_v2")
    op.drop_table("agent_executions")

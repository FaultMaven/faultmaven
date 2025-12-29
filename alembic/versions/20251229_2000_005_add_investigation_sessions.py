"""005_add_investigation_sessions

Add investigation session tracking tables for temporal investigation structure.

This migration:
- Creates the 'investigation_sessions' table for tracking investigation session lifecycle
- Adds 'session_id' column to 'agent_executions' table to link executions to sessions
- Adds foreign keys with CASCADE delete (sessions → cases) and SET NULL (executions → sessions)
- Creates indexes for efficient queries by case, user, organization, status, and timestamps
- Supports both PostgreSQL (with native types) and SQLite (for development)

Note: This creates a four-level CASCADE delete chain:
    Case → InvestigationSession → AgentExecution → AgentToolCall

Revision ID: 005_investigation_sessions
Revises: 004_agent_executions
Create Date: 2025-12-29 20:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "005_investigation_sessions"
down_revision: Union[str, Sequence[str], None] = "004_agent_executions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """
    Apply investigation session tracking migration.

    Creates investigation_sessions table and adds session_id to agent_executions.
    """
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """
    Remove investigation session tracking schema objects.

    Drops the session_id column from agent_executions and the investigation_sessions table.
    """
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema for investigation sessions."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Table: investigation_sessions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS investigation_sessions (
            session_id VARCHAR(64) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            organization_id VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            last_activity_at TIMESTAMPTZ NOT NULL,
            total_duration_ms INTEGER,
            session_goal TEXT,
            findings_summary TEXT,
            total_token_usage INTEGER NOT NULL DEFAULT 0,
            total_agent_executions INTEGER NOT NULL DEFAULT 0,
            token_budget_limit INTEGER,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT investigation_sessions_status_valid CHECK (
                status IN ('active', 'paused', 'completed', 'abandoned')
            ),
            CONSTRAINT investigation_sessions_duration_non_negative CHECK (
                total_duration_ms IS NULL OR total_duration_ms >= 0
            ),
            CONSTRAINT investigation_sessions_token_usage_non_negative CHECK (
                total_token_usage >= 0
            ),
            CONSTRAINT investigation_sessions_executions_non_negative CHECK (
                total_agent_executions >= 0
            ),
            CONSTRAINT investigation_sessions_budget_non_negative CHECK (
                token_budget_limit IS NULL OR token_budget_limit >= 0
            ),
            CONSTRAINT investigation_sessions_session_id_not_empty CHECK (
                LENGTH(TRIM(session_id)) > 0
            ),
            CONSTRAINT investigation_sessions_case_id_not_empty CHECK (
                LENGTH(TRIM(case_id)) > 0
            ),
            CONSTRAINT investigation_sessions_user_id_not_empty CHECK (
                LENGTH(TRIM(user_id)) > 0
            ),
            CONSTRAINT investigation_sessions_organization_id_not_empty CHECK (
                LENGTH(TRIM(organization_id)) > 0
            )
        )
    """))

    # Indexes for investigation_sessions
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_investigation_sessions_case_id "
        "ON investigation_sessions(case_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_investigation_sessions_user_id "
        "ON investigation_sessions(user_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_investigation_sessions_organization_id "
        "ON investigation_sessions(organization_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_investigation_sessions_status "
        "ON investigation_sessions(status)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_investigation_sessions_started_at "
        "ON investigation_sessions(started_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_investigation_sessions_last_activity_at "
        "ON investigation_sessions(last_activity_at DESC)"
    ))

    # Foreign key to cases with CASCADE delete
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_investigation_sessions_case_id'
            ) THEN
                ALTER TABLE investigation_sessions
                ADD CONSTRAINT fk_investigation_sessions_case_id
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Add session_id column to agent_executions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agent_executions' AND column_name = 'session_id'
            ) THEN
                ALTER TABLE agent_executions
                ADD COLUMN session_id VARCHAR(64);
            END IF;
        END $$;
    """))

    # Create index on session_id
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_session_id "
        "ON agent_executions(session_id)"
    ))

    # Foreign key from agent_executions to investigation_sessions with SET NULL
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_agent_executions_session_id'
            ) THEN
                ALTER TABLE agent_executions
                ADD CONSTRAINT fk_agent_executions_session_id
                FOREIGN KEY (session_id) REFERENCES investigation_sessions(session_id)
                ON DELETE SET NULL;
            END IF;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Trigger: auto-update updated_at timestamp
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_investigation_sessions_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS investigation_sessions_update_timestamp ON investigation_sessions;
        CREATE TRIGGER investigation_sessions_update_timestamp
            BEFORE UPDATE ON investigation_sessions
            FOR EACH ROW
            EXECUTE FUNCTION update_investigation_sessions_updated_at()
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL investigation session objects."""
    conn = op.get_bind()

    # Drop trigger
    conn.execute(text(
        "DROP TRIGGER IF EXISTS investigation_sessions_update_timestamp "
        "ON investigation_sessions CASCADE"
    ))

    # Drop function
    conn.execute(text("DROP FUNCTION IF EXISTS update_investigation_sessions_updated_at() CASCADE"))

    # Drop foreign key from agent_executions
    conn.execute(text("""
        ALTER TABLE agent_executions
        DROP CONSTRAINT IF EXISTS fk_agent_executions_session_id
    """))

    # Drop index on agent_executions.session_id
    conn.execute(text("DROP INDEX IF EXISTS idx_agent_executions_session_id"))

    # Drop session_id column from agent_executions
    conn.execute(text("""
        ALTER TABLE agent_executions
        DROP COLUMN IF EXISTS session_id
    """))

    # Drop foreign key constraint from investigation_sessions
    conn.execute(text("""
        ALTER TABLE investigation_sessions
        DROP CONSTRAINT IF EXISTS fk_investigation_sessions_case_id
    """))

    # Drop indexes for investigation_sessions
    conn.execute(text("DROP INDEX IF EXISTS idx_investigation_sessions_last_activity_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_investigation_sessions_started_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_investigation_sessions_status"))
    conn.execute(text("DROP INDEX IF EXISTS idx_investigation_sessions_organization_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_investigation_sessions_user_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_investigation_sessions_case_id"))

    # Drop table
    conn.execute(text("DROP TABLE IF EXISTS investigation_sessions CASCADE"))


# =============================================================================
# SQLite Implementation (Development)
# =============================================================================


def _upgrade_sqlite() -> None:
    """Create SQLite schema for investigation sessions."""

    # Table: investigation_sessions
    op.create_table(
        "investigation_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("case_id", sa.String(17), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("ended_at", sa.DateTime, nullable=True),
        sa.Column("last_activity_at", sa.DateTime, nullable=False),
        sa.Column("total_duration_ms", sa.Integer, nullable=True),
        sa.Column("session_goal", sa.Text, nullable=True),
        sa.Column("findings_summary", sa.Text, nullable=True),
        sa.Column("total_token_usage", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_agent_executions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("token_budget_limit", sa.Integer, nullable=True),
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
            name="fk_investigation_sessions_case_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for investigation_sessions
    op.create_index(
        "idx_investigation_sessions_case_id",
        "investigation_sessions",
        ["case_id"],
    )
    op.create_index(
        "idx_investigation_sessions_user_id",
        "investigation_sessions",
        ["user_id"],
    )
    op.create_index(
        "idx_investigation_sessions_organization_id",
        "investigation_sessions",
        ["organization_id"],
    )
    op.create_index(
        "idx_investigation_sessions_status",
        "investigation_sessions",
        ["status"],
    )
    op.create_index(
        "idx_investigation_sessions_started_at",
        "investigation_sessions",
        ["started_at"],
    )
    op.create_index(
        "idx_investigation_sessions_last_activity_at",
        "investigation_sessions",
        ["last_activity_at"],
    )

    # Add session_id column to agent_executions
    # SQLite doesn't support adding foreign keys to existing tables easily,
    # so we just add the column without the constraint for development
    op.add_column(
        "agent_executions",
        sa.Column("session_id", sa.String(64), nullable=True),
    )

    # Create index on session_id
    op.create_index(
        "idx_agent_executions_session_id",
        "agent_executions",
        ["session_id"],
    )


def _downgrade_sqlite() -> None:
    """Remove SQLite investigation session objects."""

    # Drop index on agent_executions.session_id
    op.drop_index("idx_agent_executions_session_id", table_name="agent_executions")

    # Drop session_id column from agent_executions
    op.drop_column("agent_executions", "session_id")

    # Drop indexes for investigation_sessions
    op.drop_index("idx_investigation_sessions_last_activity_at", table_name="investigation_sessions")
    op.drop_index("idx_investigation_sessions_started_at", table_name="investigation_sessions")
    op.drop_index("idx_investigation_sessions_status", table_name="investigation_sessions")
    op.drop_index("idx_investigation_sessions_organization_id", table_name="investigation_sessions")
    op.drop_index("idx_investigation_sessions_user_id", table_name="investigation_sessions")
    op.drop_index("idx_investigation_sessions_case_id", table_name="investigation_sessions")

    # Drop table
    op.drop_table("investigation_sessions")

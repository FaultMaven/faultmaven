"""002_add_session_management

Add sessions table and link cases to sessions for session-based case tracking.

This migration:
- Creates the 'sessions' table for user session management
- Adds 'session_id' column to 'cases' table with foreign key
- Creates indexes for efficient session-based queries
- Uses ON DELETE SET NULL to preserve cases when sessions are deleted

Revision ID: 8f2b4c9d1e3a
Revises: da6856719b5f
Create Date: 2025-12-29 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "8f2b4c9d1e3a"
down_revision: Union[str, Sequence[str], None] = "da6856719b5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """
    Apply session management migration.

    Creates sessions table and links cases to sessions.
    """
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """
    Remove session management schema objects.

    Drops session_id from cases and the sessions table.
    """
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema for session management."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Table: sessions
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT sessions_user_id_not_empty CHECK (LENGTH(TRIM(user_id)) > 0)
        )
    """))

    # Sessions indexes
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_last_accessed ON sessions(last_accessed DESC)"))

    # -------------------------------------------------------------------------
    # Add session_id to cases table
    # -------------------------------------------------------------------------
    conn.execute(text("""
        ALTER TABLE cases
        ADD COLUMN IF NOT EXISTS session_id VARCHAR(36)
    """))

    # Add foreign key constraint with ON DELETE SET NULL
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_cases_session_id'
            ) THEN
                ALTER TABLE cases
                ADD CONSTRAINT fk_cases_session_id
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                ON DELETE SET NULL;
            END IF;
        END $$;
    """))

    # Add index for session-based case queries
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cases_session_id ON cases(session_id)"))

    # -------------------------------------------------------------------------
    # Function: update_session_last_accessed
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_session_last_accessed()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.last_accessed = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    # Trigger for last_accessed auto-update
    conn.execute(text("""
        DROP TRIGGER IF EXISTS sessions_update_last_accessed ON sessions;
        CREATE TRIGGER sessions_update_last_accessed
            BEFORE UPDATE ON sessions
            FOR EACH ROW
            EXECUTE FUNCTION update_session_last_accessed()
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL session management objects."""
    conn = op.get_bind()

    # Drop trigger
    conn.execute(text("DROP TRIGGER IF EXISTS sessions_update_last_accessed ON sessions CASCADE"))

    # Drop function
    conn.execute(text("DROP FUNCTION IF EXISTS update_session_last_accessed() CASCADE"))

    # Drop foreign key and column from cases
    conn.execute(text("""
        ALTER TABLE cases
        DROP CONSTRAINT IF EXISTS fk_cases_session_id
    """))

    conn.execute(text("DROP INDEX IF EXISTS idx_cases_session_id"))

    conn.execute(text("""
        ALTER TABLE cases
        DROP COLUMN IF EXISTS session_id
    """))

    # Drop sessions table
    conn.execute(text("DROP INDEX IF EXISTS idx_sessions_user_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_sessions_created_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_sessions_expires_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_sessions_last_accessed"))
    conn.execute(text("DROP TABLE IF EXISTS sessions CASCADE"))


# =============================================================================
# SQLite Implementation (Development)
# =============================================================================


def _upgrade_sqlite() -> None:
    """Create SQLite schema for session management."""

    # Table: sessions
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("last_accessed", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("metadata", sa.Text, server_default='{}'),
    )

    op.create_index("idx_sessions_user_id", "sessions", ["user_id"])
    op.create_index("idx_sessions_created_at", "sessions", ["created_at"])
    op.create_index("idx_sessions_expires_at", "sessions", ["expires_at"])

    # Add session_id to cases table
    # Note: SQLite doesn't support adding foreign keys to existing tables easily,
    # so we just add the column without the constraint for development purposes
    op.add_column(
        "cases",
        sa.Column("session_id", sa.String(36), nullable=True)
    )

    op.create_index("idx_cases_session_id", "cases", ["session_id"])


def _downgrade_sqlite() -> None:
    """Remove SQLite session management objects."""

    # Drop index and column from cases
    op.drop_index("idx_cases_session_id", table_name="cases")
    op.drop_column("cases", "session_id")

    # Drop sessions table
    op.drop_index("idx_sessions_expires_at", table_name="sessions")
    op.drop_index("idx_sessions_created_at", table_name="sessions")
    op.drop_index("idx_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")

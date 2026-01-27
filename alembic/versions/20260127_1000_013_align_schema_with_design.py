"""Align uploaded_files and case_messages schema with design document

Revision ID: 013_align_schema_with_design
Revises: 012_fix_schema_inconsistencies
Create Date: 2026-01-27 10:00:00.000000

This migration aligns the database schema with the authoritative design document:
docs/architecture/data-and-storage/schemas/case-schema.md

Changes to uploaded_files (§4.6):
1. Rename file_size → size_bytes (matches UploadedFile Pydantic model)
2. Rename content_type → data_type (matches design spec)
3. Rename storage_path → content_ref (matches design spec)
4. Add uploaded_at_turn INTEGER NOT NULL DEFAULT 0
5. Add source_type VARCHAR(50) DEFAULT 'file_upload'
6. Add preprocessing_summary TEXT

Changes to case_messages (§4.7):
1. Rename timestamp → created_at (matches design spec)
2. Add turn_number INTEGER NOT NULL DEFAULT 0
3. Add token_count INTEGER
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "013_align_schema_with_design"
down_revision: Union[str, None] = "012_fix_schema_inconsistencies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Align schema with design document."""
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        _upgrade_postgresql(conn)
    elif dialect == "sqlite":
        _upgrade_sqlite(conn)


def _upgrade_postgresql(conn) -> None:
    """PostgreSQL-specific upgrade using ALTER TABLE."""

    # =========================================================================
    # uploaded_files table changes
    # =========================================================================

    # Rename columns to match design spec
    conn.execute(
        text("ALTER TABLE uploaded_files RENAME COLUMN file_size TO size_bytes")
    )
    conn.execute(
        text("ALTER TABLE uploaded_files RENAME COLUMN content_type TO data_type")
    )
    conn.execute(
        text("ALTER TABLE uploaded_files RENAME COLUMN storage_path TO content_ref")
    )

    # Add missing columns from design spec
    conn.execute(
        text(
            """
        ALTER TABLE uploaded_files
        ADD COLUMN IF NOT EXISTS uploaded_at_turn INTEGER NOT NULL DEFAULT 0
    """
        )
    )

    conn.execute(
        text(
            """
        ALTER TABLE uploaded_files
        ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) NOT NULL DEFAULT 'file_upload'
    """
        )
    )

    conn.execute(
        text(
            """
        ALTER TABLE uploaded_files
        ADD COLUMN IF NOT EXISTS preprocessing_summary TEXT
    """
        )
    )

    # =========================================================================
    # case_messages table changes
    # =========================================================================

    # Rename timestamp to created_at (design spec)
    conn.execute(
        text("ALTER TABLE case_messages RENAME COLUMN timestamp TO created_at")
    )

    # Add missing columns from design spec
    conn.execute(
        text(
            """
        ALTER TABLE case_messages
        ADD COLUMN IF NOT EXISTS turn_number INTEGER NOT NULL DEFAULT 0
    """
        )
    )

    conn.execute(
        text(
            """
        ALTER TABLE case_messages
        ADD COLUMN IF NOT EXISTS token_count INTEGER
    """
        )
    )


def _upgrade_sqlite(conn) -> None:
    """SQLite-specific upgrade - must recreate tables."""

    # =========================================================================
    # uploaded_files table - recreate with correct schema
    # =========================================================================

    # Check if table exists
    result = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='uploaded_files'"
        )
    )
    if result.fetchone():
        # Create new table with design-compliant schema
        conn.execute(
            text(
                """
            CREATE TABLE uploaded_files_new (
                file_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                data_type TEXT NOT NULL DEFAULT 'unknown',
                uploaded_at_turn INTEGER NOT NULL DEFAULT 0,
                uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source_type TEXT NOT NULL DEFAULT 'file_upload',
                content_ref TEXT,
                preprocessing_summary TEXT,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
            )
        """
            )
        )

        # Copy data from old table, mapping old column names to new
        conn.execute(
            text(
                """
            INSERT INTO uploaded_files_new (
                file_id, case_id, filename, size_bytes, data_type,
                uploaded_at_turn, uploaded_at, source_type, content_ref,
                preprocessing_summary, metadata
            )
            SELECT
                file_id,
                case_id,
                filename,
                COALESCE(file_size, 0),
                COALESCE(content_type, 'unknown'),
                0,
                uploaded_at,
                'file_upload',
                storage_path,
                processing_error,
                COALESCE(metadata, '{}')
            FROM uploaded_files
        """
            )
        )

        # Drop old table and rename new one
        conn.execute(text("DROP TABLE uploaded_files"))
        conn.execute(text("ALTER TABLE uploaded_files_new RENAME TO uploaded_files"))

        # Recreate indexes
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_uploaded_files_case_id ON uploaded_files(case_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_uploaded_files_uploaded_at ON uploaded_files(uploaded_at DESC)"
            )
        )

    # =========================================================================
    # case_messages table - recreate with correct schema
    # =========================================================================

    result = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='case_messages'"
        )
    )
    if result.fetchone():
        # Create new table with design-compliant schema
        conn.execute(
            text(
                """
            CREATE TABLE case_messages_new (
                message_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                token_count INTEGER,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
            )
        """
            )
        )

        # Copy data from old table
        conn.execute(
            text(
                """
            INSERT INTO case_messages_new (
                message_id, case_id, turn_number, role, content,
                created_at, token_count, metadata
            )
            SELECT
                message_id,
                case_id,
                0,
                role,
                content,
                COALESCE(timestamp, CURRENT_TIMESTAMP),
                NULL,
                COALESCE(metadata, '{}')
            FROM case_messages
        """
            )
        )

        # Drop old table and rename new one
        conn.execute(text("DROP TABLE case_messages"))
        conn.execute(text("ALTER TABLE case_messages_new RENAME TO case_messages"))

        # Recreate indexes
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_case_messages_case_id ON case_messages(case_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_case_messages_created_at ON case_messages(created_at DESC)"
            )
        )


def downgrade() -> None:
    """Revert schema changes."""
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        _downgrade_postgresql(conn)
    elif dialect == "sqlite":
        _downgrade_sqlite(conn)


def _downgrade_postgresql(conn) -> None:
    """PostgreSQL-specific downgrade."""

    # Revert uploaded_files column names
    conn.execute(
        text("ALTER TABLE uploaded_files RENAME COLUMN size_bytes TO file_size")
    )
    conn.execute(
        text("ALTER TABLE uploaded_files RENAME COLUMN data_type TO content_type")
    )
    conn.execute(
        text("ALTER TABLE uploaded_files RENAME COLUMN content_ref TO storage_path")
    )

    # Drop added columns
    conn.execute(
        text("ALTER TABLE uploaded_files DROP COLUMN IF EXISTS uploaded_at_turn")
    )
    conn.execute(text("ALTER TABLE uploaded_files DROP COLUMN IF EXISTS source_type"))
    conn.execute(
        text("ALTER TABLE uploaded_files DROP COLUMN IF EXISTS preprocessing_summary")
    )

    # Revert case_messages column name
    conn.execute(
        text("ALTER TABLE case_messages RENAME COLUMN created_at TO timestamp")
    )

    # Drop added columns
    conn.execute(text("ALTER TABLE case_messages DROP COLUMN IF EXISTS turn_number"))
    conn.execute(text("ALTER TABLE case_messages DROP COLUMN IF EXISTS token_count"))


def _downgrade_sqlite(conn) -> None:
    """SQLite-specific downgrade - must recreate tables."""

    # Recreate uploaded_files with old schema
    result = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='uploaded_files'"
        )
    )
    if result.fetchone():
        conn.execute(
            text(
                """
            CREATE TABLE uploaded_files_old (
                file_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                content_type TEXT,
                storage_path TEXT,
                processing_status TEXT NOT NULL DEFAULT 'pending',
                processing_error TEXT,
                uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
            )
        """
            )
        )

        conn.execute(
            text(
                """
            INSERT INTO uploaded_files_old
            SELECT file_id, case_id, filename, size_bytes, data_type,
                   content_ref, 'pending', preprocessing_summary, uploaded_at,
                   NULL, metadata
            FROM uploaded_files
        """
            )
        )

        conn.execute(text("DROP TABLE uploaded_files"))
        conn.execute(text("ALTER TABLE uploaded_files_old RENAME TO uploaded_files"))

    # Recreate case_messages with old schema
    result = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='case_messages'"
        )
    )
    if result.fetchone():
        conn.execute(
            text(
                """
            CREATE TABLE case_messages_old (
                message_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT,
                FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
            )
        """
            )
        )

        conn.execute(
            text(
                """
            INSERT INTO case_messages_old
            SELECT message_id, case_id, role, content, created_at, metadata
            FROM case_messages
        """
            )
        )

        conn.execute(text("DROP TABLE case_messages"))
        conn.execute(text("ALTER TABLE case_messages_old RENAME TO case_messages"))

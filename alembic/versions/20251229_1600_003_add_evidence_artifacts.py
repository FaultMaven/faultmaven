"""003_add_evidence_artifacts

Add evidence_artifacts table for managing file artifacts associated with cases.

This migration:
- Creates the 'evidence_artifacts' table for evidence file storage metadata
- Adds foreign key to cases table with CASCADE delete
- Creates indexes for efficient queries by case, user, organization, type, and date
- Supports both PostgreSQL (with native types) and SQLite (for development)

Revision ID: 003_evidence_artifacts
Revises: 8f2b4c9d1e3a
Create Date: 2025-12-29 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "003_evidence_artifacts"
down_revision: Union[str, Sequence[str], None] = "8f2b4c9d1e3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """
    Apply evidence artifacts migration.

    Creates evidence_artifacts table with foreign key to cases.
    """
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """
    Remove evidence artifacts schema objects.

    Drops the evidence_artifacts table and all associated constraints.
    """
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema for evidence artifacts."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Table: evidence_artifacts
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence_artifacts (
            evidence_id VARCHAR(64) PRIMARY KEY,
            case_id VARCHAR(17) NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            organization_id VARCHAR(64) NOT NULL,
            original_filename VARCHAR(512) NOT NULL,
            stored_filename VARCHAR(512) NOT NULL,
            file_path VARCHAR(2048) NOT NULL,
            evidence_type VARCHAR(64) NOT NULL,
            mime_type VARCHAR(256) NOT NULL,
            file_size BIGINT NOT NULL,
            storage_backend VARCHAR(64) NOT NULL DEFAULT 'local_filesystem',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb,
            description TEXT,
            is_primary BOOLEAN NOT NULL DEFAULT FALSE,
            CONSTRAINT evidence_artifacts_case_id_not_empty CHECK (LENGTH(TRIM(case_id)) > 0),
            CONSTRAINT evidence_artifacts_user_id_not_empty CHECK (LENGTH(TRIM(user_id)) > 0),
            CONSTRAINT evidence_artifacts_org_id_not_empty CHECK (LENGTH(TRIM(organization_id)) > 0),
            CONSTRAINT evidence_artifacts_original_filename_not_empty CHECK (LENGTH(TRIM(original_filename)) > 0),
            CONSTRAINT evidence_artifacts_file_path_not_empty CHECK (LENGTH(TRIM(file_path)) > 0),
            CONSTRAINT evidence_artifacts_file_size_non_negative CHECK (file_size >= 0),
            CONSTRAINT evidence_artifacts_evidence_type_valid CHECK (
                evidence_type IN (
                    'screenshot', 'log_file', 'network_trace', 'code_snippet',
                    'configuration', 'video_recording', 'har_file', 'crash_dump',
                    'heap_dump', 'thread_dump', 'metrics_export', 'other'
                )
            ),
            CONSTRAINT evidence_artifacts_storage_backend_valid CHECK (
                storage_backend IN ('local_filesystem', 's3', 'azure_blob', 'gcs')
            )
        )
    """))

    # Indexes for efficient queries
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_case_id "
        "ON evidence_artifacts(case_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_user_id "
        "ON evidence_artifacts(user_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_organization_id "
        "ON evidence_artifacts(organization_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_evidence_type "
        "ON evidence_artifacts(evidence_type)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_created_at "
        "ON evidence_artifacts(created_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_is_primary "
        "ON evidence_artifacts(case_id, is_primary) WHERE is_primary = TRUE"
    ))

    # Add foreign key constraint with ON DELETE CASCADE
    conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_evidence_artifacts_case_id'
            ) THEN
                ALTER TABLE evidence_artifacts
                ADD CONSTRAINT fk_evidence_artifacts_case_id
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
    """))

    # -------------------------------------------------------------------------
    # Trigger: auto-update updated_at timestamp
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_evidence_artifacts_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS evidence_artifacts_update_timestamp ON evidence_artifacts;
        CREATE TRIGGER evidence_artifacts_update_timestamp
            BEFORE UPDATE ON evidence_artifacts
            FOR EACH ROW
            EXECUTE FUNCTION update_evidence_artifacts_updated_at()
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL evidence artifacts objects."""
    conn = op.get_bind()

    # Drop trigger
    conn.execute(text(
        "DROP TRIGGER IF EXISTS evidence_artifacts_update_timestamp "
        "ON evidence_artifacts CASCADE"
    ))

    # Drop function
    conn.execute(text(
        "DROP FUNCTION IF EXISTS update_evidence_artifacts_updated_at() CASCADE"
    ))

    # Drop foreign key constraint
    conn.execute(text("""
        ALTER TABLE evidence_artifacts
        DROP CONSTRAINT IF EXISTS fk_evidence_artifacts_case_id
    """))

    # Drop indexes
    conn.execute(text("DROP INDEX IF EXISTS idx_evidence_artifacts_case_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_evidence_artifacts_user_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_evidence_artifacts_organization_id"))
    conn.execute(text("DROP INDEX IF EXISTS idx_evidence_artifacts_evidence_type"))
    conn.execute(text("DROP INDEX IF EXISTS idx_evidence_artifacts_created_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_evidence_artifacts_is_primary"))

    # Drop table
    conn.execute(text("DROP TABLE IF EXISTS evidence_artifacts CASCADE"))


# =============================================================================
# SQLite Implementation (Development)
# =============================================================================


def _upgrade_sqlite() -> None:
    """Create SQLite schema for evidence artifacts."""

    # Table: evidence_artifacts
    op.create_table(
        "evidence_artifacts",
        sa.Column("evidence_id", sa.String(64), primary_key=True),
        sa.Column("case_id", sa.String(17), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("stored_filename", sa.String(512), nullable=False),
        sa.Column("file_path", sa.String(2048), nullable=False),
        sa.Column("evidence_type", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(256), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "storage_backend",
            sa.String(64),
            nullable=False,
            server_default="local_filesystem",
        ),
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
        sa.Column("metadata", sa.Text, server_default="{}"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default="0"),
        # Note: SQLite doesn't support adding foreign keys easily,
        # but the relationship is defined in the ORM model
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.case_id"],
            name="fk_evidence_artifacts_case_id",
            ondelete="CASCADE",
        ),
    )

    # Create indexes
    op.create_index(
        "idx_evidence_artifacts_case_id",
        "evidence_artifacts",
        ["case_id"],
    )
    op.create_index(
        "idx_evidence_artifacts_user_id",
        "evidence_artifacts",
        ["user_id"],
    )
    op.create_index(
        "idx_evidence_artifacts_organization_id",
        "evidence_artifacts",
        ["organization_id"],
    )
    op.create_index(
        "idx_evidence_artifacts_evidence_type",
        "evidence_artifacts",
        ["evidence_type"],
    )
    op.create_index(
        "idx_evidence_artifacts_created_at",
        "evidence_artifacts",
        ["created_at"],
    )


def _downgrade_sqlite() -> None:
    """Remove SQLite evidence artifacts objects."""

    # Drop indexes
    op.drop_index("idx_evidence_artifacts_created_at", table_name="evidence_artifacts")
    op.drop_index("idx_evidence_artifacts_evidence_type", table_name="evidence_artifacts")
    op.drop_index("idx_evidence_artifacts_organization_id", table_name="evidence_artifacts")
    op.drop_index("idx_evidence_artifacts_user_id", table_name="evidence_artifacts")
    op.drop_index("idx_evidence_artifacts_case_id", table_name="evidence_artifacts")

    # Drop table
    op.drop_table("evidence_artifacts")

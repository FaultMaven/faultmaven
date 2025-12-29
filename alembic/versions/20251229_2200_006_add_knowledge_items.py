"""006_add_knowledge_items

Add knowledge items table for RAG (Retrieval-Augmented Generation) system.

This migration:
- Creates the 'knowledge_items' table for storing knowledge base content
- Creates indexes for efficient queries by organization, type, category, and timestamps
- Supports both PostgreSQL (with JSONB, GIN indexes, optional pgvector) and SQLite (for development)
- Does NOT have foreign key to cases - knowledge items persist independently for compliance/audit

Note: For PostgreSQL with pgvector extension, the embedding_vector column can be upgraded
to use native VECTOR(1536) type with HNSW index for fast similarity search.
This is handled in TASK-010 (Vector Search Integration).

Revision ID: 006_knowledge_items
Revises: 005_investigation_sessions
Create Date: 2025-12-29 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "006_knowledge_items"
down_revision: Union[str, Sequence[str], None] = "005_investigation_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def is_postgresql() -> bool:
    """Check if we're running against PostgreSQL."""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    """
    Apply knowledge items migration.

    Creates knowledge_items table with indexes and constraints.
    """
    if is_postgresql():
        _upgrade_postgresql()
    else:
        _upgrade_sqlite()


def downgrade() -> None:
    """
    Remove knowledge items schema objects.

    Drops the knowledge_items table and associated indexes.
    """
    if is_postgresql():
        _downgrade_postgresql()
    else:
        _downgrade_sqlite()


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


def _upgrade_postgresql() -> None:
    """Create PostgreSQL schema for knowledge items."""
    conn = op.get_bind()

    # -------------------------------------------------------------------------
    # Table: knowledge_items
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS knowledge_items (
            item_id VARCHAR(64) PRIMARY KEY,
            organization_id VARCHAR(64) NOT NULL,
            title VARCHAR(512) NOT NULL,
            content TEXT NOT NULL,
            item_type VARCHAR(64) NOT NULL,
            category VARCHAR(128),
            tags JSONB DEFAULT '[]'::jsonb,
            embedding_model VARCHAR(128) NOT NULL DEFAULT 'text-embedding-3-small',
            embedding_vector TEXT,
            embedding_version INTEGER NOT NULL DEFAULT 1,
            source_url VARCHAR(2048),
            author VARCHAR(255),
            language VARCHAR(8) NOT NULL DEFAULT 'en',
            view_count INTEGER NOT NULL DEFAULT 0,
            helpful_count INTEGER NOT NULL DEFAULT 0,
            not_helpful_count INTEGER NOT NULL DEFAULT 0,
            last_retrieved_at TIMESTAMPTZ,
            is_published BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'::jsonb,
            CONSTRAINT knowledge_items_item_type_valid CHECK (
                item_type IN (
                    'troubleshooting_guide', 'error_pattern', 'solution_template',
                    'api_documentation', 'configuration_guide', 'best_practice',
                    'faq', 'runbook'
                )
            ),
            CONSTRAINT knowledge_items_view_count_non_negative CHECK (view_count >= 0),
            CONSTRAINT knowledge_items_helpful_count_non_negative CHECK (helpful_count >= 0),
            CONSTRAINT knowledge_items_not_helpful_count_non_negative CHECK (not_helpful_count >= 0),
            CONSTRAINT knowledge_items_embedding_version_positive CHECK (embedding_version >= 1),
            CONSTRAINT knowledge_items_item_id_not_empty CHECK (
                LENGTH(TRIM(item_id)) > 0
            ),
            CONSTRAINT knowledge_items_organization_id_not_empty CHECK (
                LENGTH(TRIM(organization_id)) > 0
            ),
            CONSTRAINT knowledge_items_title_not_empty CHECK (
                LENGTH(TRIM(title)) > 0
            ),
            CONSTRAINT knowledge_items_content_not_empty CHECK (
                LENGTH(TRIM(content)) > 0
            )
        )
    """))

    # -------------------------------------------------------------------------
    # Indexes for knowledge_items
    # -------------------------------------------------------------------------
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_organization_id "
        "ON knowledge_items(organization_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_item_type "
        "ON knowledge_items(item_type)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_category "
        "ON knowledge_items(category)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_is_published "
        "ON knowledge_items(is_published)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_created_at "
        "ON knowledge_items(created_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_last_retrieved_at "
        "ON knowledge_items(last_retrieved_at DESC)"
    ))

    # GIN index on tags for efficient JSONB array containment queries
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_tags "
        "ON knowledge_items USING GIN (tags)"
    ))

    # Full-text search index on title and content
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_items_fulltext
        ON knowledge_items USING GIN (
            to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(content, ''))
        )
    """))

    # -------------------------------------------------------------------------
    # Trigger: auto-update updated_at timestamp
    # -------------------------------------------------------------------------
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION update_knowledge_items_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS knowledge_items_update_timestamp ON knowledge_items;
        CREATE TRIGGER knowledge_items_update_timestamp
            BEFORE UPDATE ON knowledge_items
            FOR EACH ROW
            EXECUTE FUNCTION update_knowledge_items_updated_at()
    """))


def _downgrade_postgresql() -> None:
    """Remove PostgreSQL knowledge items objects."""
    conn = op.get_bind()

    # Drop trigger
    conn.execute(text(
        "DROP TRIGGER IF EXISTS knowledge_items_update_timestamp "
        "ON knowledge_items CASCADE"
    ))

    # Drop function
    conn.execute(text("DROP FUNCTION IF EXISTS update_knowledge_items_updated_at() CASCADE"))

    # Drop indexes
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_fulltext"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_tags"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_last_retrieved_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_created_at"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_is_published"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_category"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_item_type"))
    conn.execute(text("DROP INDEX IF EXISTS idx_knowledge_items_organization_id"))

    # Drop table
    conn.execute(text("DROP TABLE IF EXISTS knowledge_items CASCADE"))


# =============================================================================
# SQLite Implementation (Development)
# =============================================================================


def _upgrade_sqlite() -> None:
    """Create SQLite schema for knowledge items."""

    # Table: knowledge_items
    op.create_table(
        "knowledge_items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("item_type", sa.String(64), nullable=False),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("tags", sa.Text, nullable=False, server_default="[]"),
        sa.Column(
            "embedding_model",
            sa.String(128),
            nullable=False,
            server_default="text-embedding-3-small",
        ),
        sa.Column("embedding_vector", sa.Text, nullable=True),
        sa.Column("embedding_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("language", sa.String(8), nullable=False, server_default="en"),
        sa.Column("view_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("helpful_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("not_helpful_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_retrieved_at", sa.DateTime, nullable=True),
        sa.Column("is_published", sa.Boolean, nullable=False, server_default="1"),
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
    )

    # Indexes for knowledge_items
    op.create_index(
        "idx_knowledge_items_organization_id",
        "knowledge_items",
        ["organization_id"],
    )
    op.create_index(
        "idx_knowledge_items_item_type",
        "knowledge_items",
        ["item_type"],
    )
    op.create_index(
        "idx_knowledge_items_category",
        "knowledge_items",
        ["category"],
    )
    op.create_index(
        "idx_knowledge_items_is_published",
        "knowledge_items",
        ["is_published"],
    )
    op.create_index(
        "idx_knowledge_items_created_at",
        "knowledge_items",
        ["created_at"],
    )
    op.create_index(
        "idx_knowledge_items_last_retrieved_at",
        "knowledge_items",
        ["last_retrieved_at"],
    )


def _downgrade_sqlite() -> None:
    """Remove SQLite knowledge items objects."""

    # Drop indexes
    op.drop_index("idx_knowledge_items_last_retrieved_at", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_created_at", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_is_published", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_category", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_item_type", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_organization_id", table_name="knowledge_items")

    # Drop table
    op.drop_table("knowledge_items")

"""Add verification status and knowledge suggestions

Revision ID: 013_verification_suggestions
Revises: 012_fix_schema_inconsistencies
Create Date: 2026-01-29 18:00:00.000000

This migration adds:
1. Verification columns to knowledge_items table (verification_level, verification_reason, etc.)
2. source_suggestion_id for bidirectional lineage
3. New knowledge_suggestions table for the Review Inbox workflow
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013_verification_suggestions"
down_revision: Union[str, None] = "012_fix_schema_inconsistencies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add verification columns to knowledge_items and create knowledge_suggestions table."""

    # Add verification columns to knowledge_items table
    with op.batch_alter_table("knowledge_items", schema=None) as batch_op:
        # Verification status (0=experimental, 1=community, 2=admin_verified)
        batch_op.add_column(
            sa.Column(
                "verification_level",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("verification_reason", sa.String(512), nullable=True)
        )
        batch_op.add_column(
            sa.Column("verified_by", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
        )
        # Bidirectional lineage to suggestions
        batch_op.add_column(
            sa.Column("source_suggestion_id", sa.String(64), nullable=True)
        )

        # Add indexes for efficient filtering
        batch_op.create_index(
            "ix_knowledge_items_verification_level", ["verification_level"]
        )
        batch_op.create_index(
            "ix_knowledge_items_source_suggestion_id", ["source_suggestion_id"]
        )

    # Add check constraint for verification_level
    # Note: SQLite doesn't enforce CHECK constraints on ALTER, but PostgreSQL does
    try:
        op.create_check_constraint(
            "knowledge_items_verification_level_check",
            "knowledge_items",
            "verification_level >= 0 AND verification_level <= 2",
        )
    except Exception:
        # SQLite may not support adding check constraint via ALTER
        pass

    # Create knowledge_suggestions table
    op.create_table(
        "knowledge_suggestions",
        # Primary Key
        sa.Column("suggestion_id", sa.String(64), primary_key=True),
        # Organization and Case scope
        sa.Column("organization_id", sa.String(64), nullable=False, index=True),
        sa.Column("case_id", sa.String(64), nullable=False, index=True),
        # Status
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending_review",
            index=True,
        ),
        # Suggested content
        sa.Column("suggested_title", sa.String(512), nullable=False),
        sa.Column("suggested_content", sa.Text(), nullable=False),
        sa.Column(
            "suggested_type",
            sa.String(64),
            nullable=False,
            server_default="troubleshooting_guide",
        ),
        # Extraction metadata
        sa.Column("extracted_by", sa.String(64), nullable=False, index=True),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("include_messages", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("include_evidence", sa.Boolean(), nullable=False, server_default="1"),
        # PII scanning (HITL requirement)
        sa.Column(
            "pii_scan_status",
            sa.String(32),
            nullable=False,
            server_default="not_scanned",
            index=True,
        ),
        sa.Column("pii_scan_result", sa.Text(), nullable=True),
        sa.Column("pii_remediated_by", sa.String(64), nullable=True),
        sa.Column("pii_remediated_at", sa.DateTime(timezone=True), nullable=True),
        # Lineage (for Review Inbox footer)
        sa.Column("source_case_title", sa.String(512), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        # Review metadata
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        # Bidirectional link to KnowledgeItem (when approved)
        sa.Column("knowledge_item_id", sa.String(64), nullable=True, index=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        # Metadata (JSON)
        sa.Column("metadata", sa.Text(), server_default="{}"),
        # Check constraints
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'draft')",
            name="knowledge_suggestions_status_check",
        ),
        sa.CheckConstraint(
            "pii_scan_status IN ('not_scanned', 'scanning', 'clean', "
            "'pii_detected', 'remediated', 'scan_failed')",
            name="knowledge_suggestions_pii_scan_status_check",
        ),
        sa.CheckConstraint(
            "message_count >= 0", name="knowledge_suggestions_message_count_check"
        ),
        sa.CheckConstraint(
            "evidence_count >= 0", name="knowledge_suggestions_evidence_count_check"
        ),
    )


def downgrade() -> None:
    """Remove verification columns and knowledge_suggestions table."""

    # Drop knowledge_suggestions table
    op.drop_table("knowledge_suggestions")

    # Remove columns from knowledge_items
    with op.batch_alter_table("knowledge_items", schema=None) as batch_op:
        # Drop indexes first
        batch_op.drop_index("ix_knowledge_items_verification_level")
        batch_op.drop_index("ix_knowledge_items_source_suggestion_id")

        # Drop columns
        batch_op.drop_column("source_suggestion_id")
        batch_op.drop_column("verified_at")
        batch_op.drop_column("verified_by")
        batch_op.drop_column("verification_reason")
        batch_op.drop_column("verification_level")

    # Try to drop check constraint (may not exist in SQLite)
    try:
        op.drop_constraint(
            "knowledge_items_verification_level_check", "knowledge_items"
        )
    except Exception:
        pass

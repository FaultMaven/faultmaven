"""Add conversion_jobs and conversion_drafts tables with source_type tracking

Revision ID: b7f3c1d2e5a8
Revises: 0a6eafc2e4cf
Create Date: 2026-03-26 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f3c1d2e5a8"
down_revision: str | Sequence[str] | None = "0a6eafc2e4cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create conversion_jobs and conversion_drafts tables.

    These tables were defined in the ORM but never had a migration.
    Includes source_type and case_id columns for case-to-runbook support.
    """
    # Check if tables already exist (for existing deployments that created them via ORM)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = inspector.get_table_names()

    if "conversion_jobs" not in existing_tables:
        op.create_table(
            "conversion_jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=True),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("team_id", sa.String(length=36), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="processing",
            ),
            sa.Column("source_filename", sa.String(length=255), nullable=False),
            sa.Column("source_content_type", sa.String(length=100), nullable=False),
            sa.Column("source_size_bytes", sa.Integer(), nullable=False),
            sa.Column("source_path", sa.String(length=500), nullable=False),
            sa.Column(
                "source_type",
                sa.String(length=20),
                nullable=False,
                server_default="document",
            ),
            sa.Column("case_id", sa.String(length=36), nullable=True),
            sa.Column(
                "failure_modes_detected",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("analysis_result", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_conversion_jobs_user_id"),
            "conversion_jobs",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_conversion_jobs_case_id"),
            "conversion_jobs",
            ["case_id"],
            unique=False,
        )
    else:
        # Tables exist — add new columns if missing
        existing_cols = {c["name"] for c in inspector.get_columns("conversion_jobs")}
        if "source_type" not in existing_cols:
            op.add_column(
                "conversion_jobs",
                sa.Column(
                    "source_type",
                    sa.String(length=20),
                    nullable=False,
                    server_default="document",
                ),
            )
        if "case_id" not in existing_cols:
            op.add_column(
                "conversion_jobs",
                sa.Column("case_id", sa.String(length=36), nullable=True),
            )
            op.create_index(
                op.f("ix_conversion_jobs_case_id"),
                "conversion_jobs",
                ["case_id"],
                unique=False,
            )

    if "conversion_drafts" not in existing_tables:
        op.create_table(
            "conversion_drafts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("conversion_id", sa.String(length=36), nullable=False),
            sa.Column("runbook_id", sa.String(length=100), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column(
                "status", sa.String(length=20), nullable=False, server_default="draft"
            ),
            sa.Column(
                "source_type",
                sa.String(length=20),
                nullable=False,
                server_default="document",
            ),
            sa.Column(
                "validation_passed", sa.Boolean(), nullable=False, server_default="1"
            ),
            sa.Column("validation_errors", sa.JSON(), nullable=True),
            sa.Column("validation_warnings", sa.JSON(), nullable=True),
            sa.Column("quality_score", sa.Numeric(precision=5, scale=1), nullable=True),
            sa.Column("quality_details", sa.JSON(), nullable=True),
            sa.Column("knowledge_item_id", sa.String(length=36), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verified_by", sa.String(length=36), nullable=True),
            sa.ForeignKeyConstraint(
                ["conversion_id"], ["conversion_jobs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_conversion_drafts_conversion_id"),
            "conversion_drafts",
            ["conversion_id"],
            unique=False,
        )
    else:
        # Tables exist — add new columns if missing
        existing_cols = {c["name"] for c in inspector.get_columns("conversion_drafts")}
        if "source_type" not in existing_cols:
            op.add_column(
                "conversion_drafts",
                sa.Column(
                    "source_type",
                    sa.String(length=20),
                    nullable=False,
                    server_default="document",
                ),
            )


def downgrade() -> None:
    """Drop conversion tables."""
    op.drop_table("conversion_drafts")
    op.drop_table("conversion_jobs")

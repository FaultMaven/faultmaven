"""phase_2_drop_standalone_evidence_tables

Storage redesign 2026-04 — Phase 2: drop the two tables that backed the
deleted standalone evidence path.

Drops:
1. `evidence_artifacts` table — backed the case-scoped EvidenceArtifact /
   APIEvidenceArtifactService surface that has been deleted in this phase
   (along with the entire `faultmaven/modules/evidence/api/` directory and
   the `EvidenceArtifactRepository`). Evidence is now case-tied only and
   lives on the existing `evidence` table, accessed via
   `Case._load_evidence_for_case`.
2. `standalone_evidence` table — backed the never-fully-implemented
   "standalone" evidence-upload path (POST /api/v1/evidence). Removed
   together with the routes and service.

PG indexes are dropped explicitly first (PostgreSQL enforces this; SQLite
is permissive). The `cases.evidence_artifacts` ORM relationship and the
`EvidenceArtifactModel` / `StandaloneEvidenceModel` classes are deleted in
the same commit.

See deployment-schema-strategy.md §7.2 and §12 decision #12.

Revision ID: 7a2da68429da
Revises: eb98f7b39fbc
Create Date: 2026-04-19 19:57:07.674816

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7a2da68429da"
down_revision: Union[str, Sequence[str], None] = "eb98f7b39fbc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop evidence_artifacts and standalone_evidence tables."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Drop PG indexes first (PG enforces, SQLite is permissive).
    if dialect == "postgresql":
        op.drop_index(
            "ix_evidence_artifacts_user_id", table_name="evidence_artifacts"
        )
        op.drop_index(
            "ix_evidence_artifacts_organization_id", table_name="evidence_artifacts"
        )
        op.drop_index(
            "ix_evidence_artifacts_evidence_type", table_name="evidence_artifacts"
        )
        op.drop_index(
            "ix_evidence_artifacts_created_at", table_name="evidence_artifacts"
        )
        op.drop_index(
            "ix_evidence_artifacts_case_id", table_name="evidence_artifacts"
        )

    op.drop_table("evidence_artifacts")

    if dialect == "postgresql":
        op.drop_index(
            "ix_standalone_evidence_uploaded_by", table_name="standalone_evidence"
        )
        op.drop_index(
            "ix_standalone_evidence_uploaded_at", table_name="standalone_evidence"
        )
        op.drop_index(
            "ix_standalone_evidence_organization_id",
            table_name="standalone_evidence",
        )

    op.drop_table("standalone_evidence")


def downgrade() -> None:
    """Recreate the dropped tables.

    DDL copied from the 001 baseline migration so a stepwise rollback
    restores the prior schema. Note: rollback only restores the empty
    tables — it does NOT restore the deleted application code (services,
    API routes, ORM models). In practice, post-redesign rollback should be
    a full schema reset rather than a stepwise downgrade.
    """
    op.create_table(
        "evidence_artifacts",
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=17), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("stored_filename", sa.String(length=512), nullable=False),
        sa.Column("file_path", sa.String(length=2048), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=256), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False),
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
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "LENGTH(TRIM(file_path)) > 0",
            name="evidence_artifacts_file_path_not_empty",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(original_filename)) > 0",
            name="evidence_artifacts_filename_not_empty",
        ),
        sa.CheckConstraint(
            "file_size >= 0", name="evidence_artifacts_file_size_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.case_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    op.create_index(
        "ix_evidence_artifacts_case_id",
        "evidence_artifacts",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifacts_created_at",
        "evidence_artifacts",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifacts_evidence_type",
        "evidence_artifacts",
        ["evidence_type"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifacts_organization_id",
        "evidence_artifacts",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_artifacts_user_id",
        "evidence_artifacts",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "standalone_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=256), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(length=2048), nullable=False),
        sa.Column("uploaded_by", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("linked_cases", sa.Text(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "LENGTH(TRIM(filename)) > 0",
            name="standalone_evidence_filename_not_empty",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(storage_path)) > 0",
            name="standalone_evidence_storage_path_not_empty",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name="standalone_evidence_size_non_negative"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_standalone_evidence_organization_id",
        "standalone_evidence",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_standalone_evidence_uploaded_at",
        "standalone_evidence",
        ["uploaded_at"],
        unique=False,
    )
    op.create_index(
        "ix_standalone_evidence_uploaded_by",
        "standalone_evidence",
        ["uploaded_by"],
        unique=False,
    )

"""Add KB metadata columns to conversion_drafts

Revision ID: d9e5f3a2b4c8
Revises: c8d4e2f1a3b7
Create Date: 2026-04-04 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9e5f3a2b4c8'
down_revision: Union[str, Sequence[str], None] = 'c8d4e2f1a3b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add domain, service, severity, tags, document_type to conversion_drafts.

    These columns store frontmatter metadata for dashboard filtering and
    listing without querying ChromaDB. Populated during scan and verify.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = {
        col["name"] for col in inspector.get_columns("conversion_drafts")
    }

    if "domain" not in existing_columns:
        op.add_column(
            "conversion_drafts",
            sa.Column("domain", sa.String(50), nullable=True),
        )
    if "service" not in existing_columns:
        op.add_column(
            "conversion_drafts",
            sa.Column("service", sa.String(100), nullable=True),
        )
    if "severity" not in existing_columns:
        op.add_column(
            "conversion_drafts",
            sa.Column("severity", sa.String(20), nullable=True),
        )
    if "tags" not in existing_columns:
        op.add_column(
            "conversion_drafts",
            sa.Column("tags", sa.Text, nullable=True),
        )
    if "document_type" not in existing_columns:
        op.add_column(
            "conversion_drafts",
            sa.Column(
                "document_type",
                sa.String(50),
                nullable=True,
                server_default="runbook",
            ),
        )


def downgrade() -> None:
    """Remove KB metadata columns."""
    op.drop_column("conversion_drafts", "document_type")
    op.drop_column("conversion_drafts", "tags")
    op.drop_column("conversion_drafts", "severity")
    op.drop_column("conversion_drafts", "service")
    op.drop_column("conversion_drafts", "domain")

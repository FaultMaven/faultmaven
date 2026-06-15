"""017_config_overrides_source_category

Generalizes the dashboard-managed config-override store beyond LLM settings
(llm-configuration-design.md, Phase 2):

1. Renames ``llm_config_overrides`` -> ``config_overrides`` (the scope is
   broader than LLM).
2. Adds ``category`` (e.g. 'llm', 'feature', 'operational', 'observability')
   so the dashboard can group settings.
3. Adds ``source`` ('env-seed' | 'admin') tracking provenance, so the admin
   dashboard can show whether a value is an explicit admin override or just the
   env/seed default — closing the "two silent sources of truth" gap.

Existing rows are admin-applied LLM overrides, so they backfill to
category='llm', source='admin'.

Revision ID: a7c9e1b3d5f7
Revises: 0a1b2c3d4e5f
Create Date: 2026-06-15 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a7c9e1b3d5f7"
down_revision: Union[str, Sequence[str], None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename the table and add category/source provenance columns."""
    op.rename_table("llm_config_overrides", "config_overrides")
    with op.batch_alter_table("config_overrides") as batch:
        batch.add_column(
            sa.Column(
                "category",
                sa.String(50),
                nullable=False,
                server_default="llm",
            )
        )
        batch.add_column(
            sa.Column(
                "source",
                sa.String(20),
                nullable=False,
                server_default="admin",
            )
        )


def downgrade() -> None:
    """Drop the provenance columns and restore the original table name."""
    with op.batch_alter_table("config_overrides") as batch:
        batch.drop_column("source")
        batch.drop_column("category")
    op.rename_table("config_overrides", "llm_config_overrides")

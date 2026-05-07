"""003_relax_enterprise_id_nullable

Temporarily relaxes users.enterprise_id and organizations.enterprise_id
to nullable. Will be tightened back to NOT NULL once the Enterprise tier
bootstrap is wired (default-enterprise creation in SingleTenantProvider
+ enterprise_id propagation through user/org creation paths).

Revision ID: a3957258f451
Revises: 00eab5e0d387
Create Date: 2026-05-07 06:28:55.089726
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3957258f451"
down_revision: Union[str, Sequence[str], None] = "00eab5e0d387"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make enterprise_id nullable on users and organizations."""
    # batch_alter_table is required for SQLite (no native ALTER COLUMN
    # nullability change); on PostgreSQL it's a no-op passthrough.
    with op.batch_alter_table("organizations") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=True,
        )
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=True,
        )


def downgrade() -> None:
    """Revert enterprise_id to NOT NULL on users and organizations."""
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=False,
        )
    with op.batch_alter_table("organizations") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=False,
        )

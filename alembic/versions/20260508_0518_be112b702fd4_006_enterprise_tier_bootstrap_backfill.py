"""006_enterprise_tier_bootstrap_backfill_and_tighten_not_null

Wires the Enterprise tier:

1. Inserts the default enterprise row (idempotent UPSERT) so the FK
   target exists for the backfill below. This is the same UUID that
   ``SingleTenantProvider.DEFAULT_ENTERPRISE_ID`` uses; bootstrap will
   no-op if the row already exists.
2. Backfills any rows in ``organizations`` and ``users`` that still
   carry NULL enterprise_id (legacy rows from migration 003) so the
   subsequent NOT NULL constraint succeeds.
3. Tightens ``organizations.enterprise_id`` and ``users.enterprise_id``
   to NOT NULL (reverses migration 003).

Revision ID: be112b702fd4
Revises: 24a5adc58c77
Create Date: 2026-05-08 05:18:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "be112b702fd4"
down_revision: Union[str, Sequence[str], None] = "24a5adc58c77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_ENTERPRISE_SLUG = "default"
DEFAULT_ENTERPRISE_NAME = "Default Enterprise"


def upgrade() -> None:
    """Seed the default enterprise, backfill, then tighten NOT NULL."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1) Insert the default enterprise row (idempotent).
    if dialect == "postgresql":
        op.execute(
            sa.text(
                """
                INSERT INTO enterprises (
                    enterprise_id, name, slug, plan_tier, max_members,
                    settings, created_at, updated_at
                ) VALUES (
                    :id, :name, :slug, 'pro', 100,
                    '{}', NOW(), NOW()
                )
                ON CONFLICT (enterprise_id) DO NOTHING
                """
            ).bindparams(
                id=DEFAULT_ENTERPRISE_ID,
                name=DEFAULT_ENTERPRISE_NAME,
                slug=DEFAULT_ENTERPRISE_SLUG,
            )
        )
    else:
        # SQLite — INSERT OR IGNORE is the idempotent equivalent.
        op.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO enterprises (
                    enterprise_id, name, slug, plan_tier, max_members,
                    settings, created_at, updated_at
                ) VALUES (
                    :id, :name, :slug, 'pro', 100,
                    '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ).bindparams(
                id=DEFAULT_ENTERPRISE_ID,
                name=DEFAULT_ENTERPRISE_NAME,
                slug=DEFAULT_ENTERPRISE_SLUG,
            )
        )

    # 2) Backfill any pre-existing NULL enterprise_id rows.
    op.execute(
        sa.text(
            "UPDATE organizations SET enterprise_id = :id "
            "WHERE enterprise_id IS NULL"
        ).bindparams(id=DEFAULT_ENTERPRISE_ID)
    )
    op.execute(
        sa.text(
            "UPDATE users SET enterprise_id = :id WHERE enterprise_id IS NULL"
        ).bindparams(id=DEFAULT_ENTERPRISE_ID)
    )

    # 3) Tighten to NOT NULL. batch_alter_table is required for SQLite;
    #    PostgreSQL accepts it as a passthrough.
    with op.batch_alter_table("organizations") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=False,
        )
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=False,
        )


def downgrade() -> None:
    """Revert enterprise_id to nullable on users and organizations.

    Does NOT delete the default enterprise row — leaving it in place is
    safe and preserves any FK references that would otherwise dangle.
    """
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=True,
        )
    with op.batch_alter_table("organizations") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=True,
        )

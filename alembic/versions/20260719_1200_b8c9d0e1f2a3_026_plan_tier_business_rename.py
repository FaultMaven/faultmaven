"""026_plan_tier_business_rename

Renames the top plan tier value ``enterprise`` → ``business`` on
``enterprises.plan_tier``. The literal ``enterprise`` overloaded three
distinct concepts — the Enterprise tenant entity, ``RoleScope.ENTERPRISE``,
and a billing level — so the billing level is renamed to ``business`` to
remove the collision. Only the plan-tier value changes; the entity and the
RBAC scope keep the ``enterprise`` name.

Old CHECK (enterprises_plan_tier_check):
    plan_tier IN ('free', 'starter', 'pro', 'enterprise')

New CHECK (same name):
    plan_tier IN ('free', 'starter', 'pro', 'business')

No data migration: the seeded plan_tier is ``pro`` and nothing writes
``enterprise``, so no row can violate the new constraint.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-19 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Swap the plan-tier CHECK from the ``enterprise`` label to ``business``.

    SQLite requires batch_alter_table to drop/recreate a CHECK; PostgreSQL
    can do it directly but batch is a no-op passthrough on PG so this works
    on both dialects.
    """
    with op.batch_alter_table("enterprises") as batch:
        batch.drop_constraint("enterprises_plan_tier_check", type_="check")
        batch.create_check_constraint(
            "enterprises_plan_tier_check",
            "plan_tier IN ('free', 'starter', 'pro', 'business')",
        )


def downgrade() -> None:
    """Restore the original CHECK with the ``enterprise`` label."""
    with op.batch_alter_table("enterprises") as batch:
        batch.drop_constraint("enterprises_plan_tier_check", type_="check")
        batch.create_check_constraint(
            "enterprises_plan_tier_check",
            "plan_tier IN ('free', 'starter', 'pro', 'enterprise')",
        )

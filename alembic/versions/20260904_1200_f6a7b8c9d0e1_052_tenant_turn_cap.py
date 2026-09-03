"""052_tenant_turn_cap

The per-tenant investigation-turn cap (ADR-016 D5.3, owner decision 2026-09-03).

Two schema changes, one mechanism:

``organizations.daily_turn_cap`` — the **operator override**, per organization.
It is deliberately three-valued rather than a plain integer, because the policy
it overrides is itself conditional:

===============  ==========================================================
value            meaning
===============  ==========================================================
``NULL``         no override. A **personal** tenant is capped at the
                 deployment default (``TENANT_DAILY_TURN_CAP``); a
                 **company** tenant is uncapped. This is the shipped state
                 for every existing row, which is why the column is added
                 nullable with no backfill: company tenants must not
                 acquire a cap by this migration running.
``0``            explicitly uncapped, whatever the tenant's kind. The
                 operator's "clear this tenant's cap" action.
``N > 0``        capped at N turns per UTC day, whatever the tenant's kind.
                 Raises a personal tenant's ceiling, or lowers it, or caps
                 a company tenant that has earned one.
===============  ==========================================================

A CHECK keeps the column non-negative so "unlimited" has exactly one spelling
and a negative value cannot become a second, silent one.

``organization_turn_usage`` — the **ledger**: one row per (organization, UTC
day) carrying the turns accepted that day. It is a table rather than a Redis
counter for one reason: ADR-016 D5.3 requires the cap to fail **closed** at the
limit, and a counter whose store can be unavailable fails open — a Redis blip
would silently un-cap every tenant until it healed. The turn already cannot be
served without this database, so a ledger here can never be the reason a turn is
refused that would otherwise have run.

The primary key is ``(organization_id, usage_date)``, which is what makes the
reservation a single atomic statement: ``INSERT … ON CONFLICT … DO UPDATE SET
turn_count = turn_count + 1 WHERE turn_count < :cap RETURNING turn_count``. An
empty RETURNING *is* the refusal, so two concurrent turns at the boundary cannot
both be admitted, and a refused turn increments nothing.

RLS: ``organization_turn_usage`` carries ``organization_id`` and is enrolled
exactly like every other tenanted table (migration 018) — policy with no ``FOR``
clause, so ``USING`` doubles as ``WITH CHECK`` and a tenant can neither read nor
write another tenant's ledger row. ``organizations`` is already enrolled; the new
column inherits that policy and needs nothing here.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-04 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the per-organization override and the per-UTC-day turn ledger."""
    # batch_alter_table so SQLite (Standalone) gets the CHECK constraint too —
    # a plain add_column cannot attach one there, and a constraint that exists
    # on only one backend is the shape that lets a bad value in locally and
    # fails in production.
    with op.batch_alter_table("organizations") as batch:
        batch.add_column(sa.Column("daily_turn_cap", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "organizations_daily_turn_cap_non_negative",
            "daily_turn_cap IS NULL OR daily_turn_cap >= 0",
        )

    op.create_table(
        "organization_turn_usage",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column(
            "turn_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", "usage_date"),
        sa.CheckConstraint(
            "turn_count >= 0", name="organization_turn_usage_non_negative"
        ),
    )

    # PostgreSQL only — SQLite (Standalone) is single-tenant and has no RLS.
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "organization_turn_usage" ENABLE ROW LEVEL SECURITY')
        op.execute(
            'CREATE POLICY "organization_turn_usage_tenant_isolation" '
            'ON "organization_turn_usage" '
            "USING (organization_id = current_setting('app.current_org_id', true))"
        )


def downgrade() -> None:
    """Drop the ledger and the override column.

    Dropping the ledger discards the day's counts, so a downgrade-and-upgrade
    inside one UTC day hands every tenant a fresh allowance. That is stated
    rather than worked around: preserving a usage ledger across a schema
    rollback would mean keeping the table, which is not what a downgrade means.
    """
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            'DROP POLICY IF EXISTS "organization_turn_usage_tenant_isolation" '
            'ON "organization_turn_usage"'
        )
        op.execute('ALTER TABLE "organization_turn_usage" DISABLE ROW LEVEL SECURITY')

    op.drop_table("organization_turn_usage")

    with op.batch_alter_table("organizations") as batch:
        batch.drop_constraint(
            "organizations_daily_turn_cap_non_negative", type_="check"
        )
        batch.drop_column("daily_turn_cap")

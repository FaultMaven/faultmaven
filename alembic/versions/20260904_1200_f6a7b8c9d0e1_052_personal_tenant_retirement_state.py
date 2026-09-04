"""052_personal_tenant_retirement_state

Typed state for personal-tenant retirement (#1045 D8, ADR-016 D5/D8 amended).

Three changes, one decision behind them: a retirement must be expressible in
**typed columns**, not encoded in a JSON blob or in a renamed slug.

1. ``users.enterprise_id`` becomes **nullable** — the reverse of migration 006's
   tightening. An account that is anchored to nothing is a real state: it is
   what a ``--next-login fresh-tenant`` retirement leaves, and it is what lets
   the login's verdict be a column read rather than a marker parse. There is no
   pre-data back-compat to preserve (house rule), and the backfill 006 performed
   is left in place: this widens the column, it does not re-null any row.

2. ``enterprises.personal_tenant_retirement`` records the operator's
   ``--next-login`` choice, constrained to the two values the code implements.
   It is what distinguishes a soft-deleted *personal* enterprise from a
   soft-deleted company one, so the login can tell "this subject's tenant was
   retired" from "this company is gone" without reading any JSON.

3. The two slug uniqueness rules become **partial on ``deleted_at IS NULL``**.
   A retired tenant keeps its slug, and the next tenant for the same subject
   derives exactly the same one — so uniqueness has to apply among *live* rows
   only. Doing it here is what removes the slug rename the previous design
   needed: nothing is renamed, and retired tenants are addressed by id.

``organizations_slug_unique_per_enterprise`` is a UNIQUE *constraint* and a
constraint cannot be partial, so it is dropped and replaced by a partial unique
*index*. On SQLite that is a table rebuild, hence ``batch_alter_table``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The two policies the CLI writes and the code implements. Spelled here as
#: well as in ``contracts`` on purpose: a migration must not import application
#: code, and a CHECK constraint is the only thing that keeps a hand-written
#: UPDATE from inventing a third value the login has no branch for.
_POLICIES = ("refuse", "fresh_tenant")


def upgrade() -> None:
    # 1. Widen users.enterprise_id. batch_alter_table for SQLite; PostgreSQL
    #    takes it as a passthrough.
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=True,
        )

    # 2. The typed policy column.
    with op.batch_alter_table("enterprises") as batch:
        batch.add_column(
            sa.Column("personal_tenant_retirement", sa.String(length=16), nullable=True)
        )
        batch.create_check_constraint(
            "enterprises_personal_tenant_retirement_check",
            "personal_tenant_retirement IS NULL OR personal_tenant_retirement IN "
            "('refuse', 'fresh_tenant')",
        )

    # 3a. enterprises.slug: unique among LIVE rows only.
    #
    # Migration 001 declared the column ``unique=True``, which PostgreSQL
    # implements as a named UNIQUE *constraint* (``enterprises_slug_key``) in
    # ADDITION to the index below. Both have to go, or the constraint keeps
    # enforcing deployment-wide uniqueness and the partial index is decorative.
    #
    # SQLite implements the same declaration as an unnamed auto-index, which
    # cannot be dropped by name and would need a full table rebuild. It is left
    # in place there, deliberately and with the consequence stated: a SQLite
    # deployment that migrated from 001 keeps deployment-wide slug uniqueness on
    # ``enterprises``. Personal tenants exist only under multi-tenant
    # PostgreSQL, so nothing that needs the partial rule runs there — and a
    # SQLite database built from the ORM metadata (which no longer declares the
    # column unique) does not have it either.
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("enterprises_slug_key", "enterprises", type_="unique")
    op.drop_index("ix_enterprises_slug", table_name="enterprises")
    op.create_index(
        "ix_enterprises_slug_live",
        "enterprises",
        ["slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # A non-unique index still serves the lookups that are not liveness-scoped.
    op.create_index("ix_enterprises_slug", "enterprises", ["slug"], unique=False)

    # 3b. organizations (enterprise_id, slug): same, and the constraint has to
    #     go because a UNIQUE constraint cannot carry a WHERE clause.
    with op.batch_alter_table("organizations") as batch:
        batch.drop_constraint(
            "organizations_slug_unique_per_enterprise", type_="unique"
        )
    op.create_index(
        "ix_organizations_slug_live",
        "organizations",
        ["enterprise_id", "slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_slug_live", table_name="organizations")
    with op.batch_alter_table("organizations") as batch:
        batch.create_unique_constraint(
            "organizations_slug_unique_per_enterprise", ["enterprise_id", "slug"]
        )

    op.drop_index("ix_enterprises_slug", table_name="enterprises")
    op.drop_index("ix_enterprises_slug_live", table_name="enterprises")
    op.create_index("ix_enterprises_slug", "enterprises", ["slug"], unique=True)
    if op.get_bind().dialect.name == "postgresql":
        op.create_unique_constraint("enterprises_slug_key", "enterprises", ["slug"])

    with op.batch_alter_table("enterprises") as batch:
        batch.drop_constraint(
            "enterprises_personal_tenant_retirement_check", type_="check"
        )
        batch.drop_column("personal_tenant_retirement")

    # Re-tightening users.enterprise_id needs every row to carry one, exactly as
    # migration 006 did before it tightened. A retired account legitimately has
    # NULL, so this re-anchors it to the default enterprise rather than failing
    # the downgrade — the same choice 006 made, and the reason it is safe is
    # that the column is about to stop being able to express "unanchored".
    from faultmaven.providers.tenancy.single_tenant import DEFAULT_ENTERPRISE_ID

    op.execute(
        sa.text(
            "UPDATE users SET enterprise_id = :id WHERE enterprise_id IS NULL"
        ).bindparams(id=DEFAULT_ENTERPRISE_ID)
    )
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "enterprise_id",
            existing_type=sa.VARCHAR(length=36),
            nullable=False,
        )

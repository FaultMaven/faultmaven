"""038_sso_org_mappings

Create ``sso_org_mappings`` — the IdP organization → FaultMaven organization
lookup that makes multi-tenant SSO login land a user in their own tenant
(#869, refs #629, ADR-010 P2, ADR-015).

A row means "``provider``'s organization ``provider_org_id`` **is** FaultMaven
organization ``organization_id``". ``(provider, provider_org_id)`` is the
primary key (an IdP org resolves to at most one tenant) and
``(provider, organization_id)`` is unique (a tenant is claimed by at most one
IdP org per provider), so the relation is 1:1 per provider in v1.

**This table is deliberately NOT enrolled in RLS.** Every other organization-
keyed table is (migration 018: ``organizations`` and ``organization_members``
included), but the SSO callback that reads this row is *unauthenticated* — no
tenant is bound at that moment, because binding the tenant is precisely what
this lookup decides. Under the migration-018 policy the tenanted tables are
unreadable there, so the mapping cannot live on ``organizations``. A mapping
row contains only an identifier equivalence and no tenant data, so keeping it
outside RLS discloses nothing that the operator who provisioned it did not
already choose to relate.

Rejected alternative: mapping columns on ``organizations`` — unreadable
pre-bind under RLS, and reading them would drag an RLS-bypassing owner-role
query into the unauthenticated auth path.

No backfill: multi-tenant SSO has never completed a login, so there is no
existing affiliation to migrate. Operators create mappings with
``scripts/auth/provision_sso_org.py``.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-07-28 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the non-RLS ``sso_org_mappings`` lookup table."""
    op.create_table(
        "sso_org_mappings",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_org_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
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
        sa.PrimaryKeyConstraint("provider", "provider_org_id"),
        sa.UniqueConstraint(
            "provider", "organization_id", name="uq_sso_org_mappings_organization"
        ),
    )
    # No ENABLE ROW LEVEL SECURITY here, on purpose — see the module docstring.


def downgrade() -> None:
    """Drop the mapping table (multi-tenant SSO login reverts to fail-closed)."""
    op.drop_table("sso_org_mappings")

"""051_sso_personal_orgs

Create ``sso_personal_orgs`` — the IdP **subject** → personal FaultMaven
organization lookup that lets a returning individual land in the same tenant on
every login (#1045, ADR-016 D5 amending ADR-015).

Migration 038 gave the callback a way to resolve a tenant from the IdP's
*organization*. That cannot serve an individual. AuthKit reports an
``organization_id`` only when the sign-in was organization-scoped, so a
returning individual's callback may carry none at all — and the natural
alternative, reading ``organization_members``, is unavailable: that table is
RLS-tenanted (migration 018) and no tenant is bound at callback time, because
binding the tenant is precisely what the lookup decides. The one identifier
every login carries is the subject, so that is the key.

**This table is deliberately NOT enrolled in RLS**, for the same reason as
``sso_org_mappings`` and no other: it is read on the *unauthenticated* callback.
A row holds two identifiers and no tenant data. The subject is the IdP's own
opaque handle (``user_01H…``), never an email.

``(provider, provider_user_id)`` is the primary key — a subject owns at most one
personal organization. That is what makes first-login provisioning idempotent,
and it is also the race arbiter: two concurrent first logins for the same
subject both try to insert here, the loser violates the key, and its whole
provisioning transaction (enterprise, organization, team, mapping) rolls back.
Exactly one organization survives and the loser adopts it.

``(provider, organization_id)`` is unique in the other direction — a personal
organization belongs to exactly one subject, so a second subject can never be
bound onto someone's personal tenant.

``provider_org_id`` records the IdP organization minted to hold that one member,
so a tenant can be reconciled against the IdP without re-deriving the external
id from the subject.

No backfill: no personal tenant has ever been provisioned. The path that writes
these rows is off by default (``SSO_JIT_PERSONAL_TENANT_ENABLED``), so applying
this migration changes no behaviour on its own.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-03 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the non-RLS ``sso_personal_orgs`` lookup table."""
    op.create_table(
        "sso_personal_orgs",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("provider_org_id", sa.String(length=255), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column(
            "membership_confirmed",
            sa.Boolean(),
            server_default=sa.text("false"),
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
        sa.PrimaryKeyConstraint("provider", "provider_user_id"),
        sa.UniqueConstraint(
            "provider", "organization_id", name="uq_sso_personal_orgs_organization"
        ),
    )
    # No ENABLE ROW LEVEL SECURITY here, on purpose — see the module docstring.


def downgrade() -> None:
    """Drop the lookup (an org-less identity reverts to fail-closed)."""
    op.drop_table("sso_personal_orgs")

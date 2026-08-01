"""039_oauth_code_organization_id

Add ``organization_id`` to ``oauth_authorization_codes`` — carry the tenant
across the OAuth-PKCE hop (#872).

The copilot's token exchange (``POST /auth/oauth/token``) is unauthenticated by
construction: the extension presents a code and a PKCE verifier, not a bearer
token. The ``users`` row it loads to mint from carries no organization (there is
no such column, by design — tenancy lives in the token chain). So the only place
the tenant can survive the hop from the *authenticated* authorize request to the
*unauthenticated* exchange is the authorization code itself.

Without it, under ``TENANT_PROVIDER=multi`` every copilot session mints an empty
organization claim and is then refused at ``bind_request_org_context`` on its
first API call — the same shape #869 fixed for the SSO login leg and #873 for the
OAuth refresh leg. This closes the last leg.

**Nullable**, for two reasons that each stand alone: a single-tenant deployment
has no tenant to carry (``resolve_organization_claim`` supplies the Standalone
sentinel at mint time), and a code issued before this migration genuinely has
none. No backfill — these rows expire in ten minutes.

**Deliberately not a foreign key and not RLS-tenanted.** The exchange reads this
row by primary key with no tenant bound; a tenant-isolation policy would hide the
row from the only reader it has, and an FK buys nothing on a credential that
outlives nothing. Migration 038 declines RLS on ``sso_org_mappings`` for exactly
this reason — an unauthenticated reader that must run before a tenant exists.

Note that only the Redis and in-memory repositories are wired
(``create_oauth_code_repository`` returns one of those two; codes are ephemeral
and live in the cache layer). This column keeps the ORM-backed implementation of
``IOAuthCodeRepository`` capable of honoring the same contract, so wiring it
later cannot silently reintroduce #872.

Revision ID: d2e3f4a5b6c7
Revises: b0c1d2e3f4a5
Create Date: 2026-08-01 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the organization the authorization code was issued under."""
    op.add_column(
        "oauth_authorization_codes",
        sa.Column("organization_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    """Drop the organization column."""
    op.drop_column("oauth_authorization_codes", "organization_id")

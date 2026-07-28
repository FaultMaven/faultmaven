#!/usr/bin/env python3
"""Provision a Cloud tenant and map an IdP organization onto it (#869).

Under ``TENANT_PROVIDER=multi`` an SSO login lands in the FaultMaven
organization that the IdP's organization is mapped to. There is deliberately no
self-service path: an unmapped IdP organization fails the login closed
(``sso_org_unmapped``) rather than provisioning a tenant just-in-time. This
script is that out-of-band provisioning step.

What it creates (all idempotent — re-running with the same arguments is a no-op
that prints the current state):

1. an **enterprise** (the top tier), unless ``--enterprise-id`` names one;
2. an **organization** inside it, keyed by ``--slug`` within that enterprise;
3. the organization's **default team** (ADR-013: every organization has one);
4. the ``sso_org_mappings`` row binding ``(workos, --workos-org-id)`` to it.

Remapping is NOT a script default. If the IdP organization is already mapped to
a *different* FaultMaven organization the script prints both and exits non-zero:
moving a tenant's IdP binding is a deliberate operator act with token- and
membership-level consequences (see
``docs/operations/sso-org-provisioning.md``).

**Run it with the owner DSN.** ``organizations`` and ``teams`` are RLS-tenanted
(migration 018) and this script writes rows for a tenant that does not exist
yet, so it needs the RLS-owning role (``faultmaven``), not the limited
application role (``faultmaven_app``). It also binds the new organization as the
current tenant while it writes, so the writes stay inside policy even where RLS
is forced.

Admin binding is manual and post-hoc (ADR-015 D5): no login path grants
elevated roles, so the first user signs in via SSO and an operator promotes
them with the existing role scripts.

Usage:
    DATABASE_URL=postgresql+asyncpg://faultmaven:...@host/faultmaven \\
    python scripts/auth/provision_sso_org.py \\
        --name "Acme Corp" --slug acme --workos-org-id org_01H...

    # Reuse an existing enterprise instead of creating one
    python scripts/auth/provision_sso_org.py \\
        --name "Acme EU" --slug acme-eu --workos-org-id org_01J... \\
        --enterprise-id 8f1c...

In a Kubernetes deployment, run it in the API pod:
    kubectl exec -it deploy/faultmaven-api -- \\
        python scripts/auth/provision_sso_org.py --name ... --slug ... \\
        --workos-org-id org_...
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Make `faultmaven` importable when run as a script.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import select  # noqa: E402

from faultmaven.config.tenant_context import set_current_org_id  # noqa: E402
from faultmaven.infrastructure.persistence.database import (  # noqa: E402
    get_db_session,
)
from faultmaven.infrastructure.persistence.models import (  # noqa: E402
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    TeamModel,
)

#: The only SSO provider FaultMaven ships an adapter for (ADR-015).
PROVIDER = "workos"

#: Every organization gets one team at creation (ADR-013). Named to match the
#: standalone bootstrap's default team (``config/constants.py``), so the two
#: deployment shapes read the same.
DEFAULT_TEAM_NAME = "Default Team"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_or_create_enterprise(
    session, *, enterprise_id: str | None, name: str, slug: str
) -> tuple[EnterpriseModel, bool]:
    """Return (enterprise, created). Looks up by id, then by slug."""
    if enterprise_id:
        found = await session.get(EnterpriseModel, enterprise_id)
        if found is None:
            raise LookupError(f"No enterprise with enterprise_id={enterprise_id}")
        return found, False

    existing = (
        await session.execute(
            select(EnterpriseModel).where(EnterpriseModel.slug == slug)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    enterprise = EnterpriseModel(
        enterprise_id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(enterprise)
    await session.flush()
    return enterprise, True


async def _get_or_create_organization(
    session, *, enterprise_id: str, name: str, slug: str
) -> tuple[OrganizationModel, bool]:
    """Return (organization, created). Identity is (enterprise_id, slug)."""
    existing = (
        await session.execute(
            select(OrganizationModel).where(
                OrganizationModel.enterprise_id == enterprise_id,
                OrganizationModel.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    organization = OrganizationModel(
        organization_id=str(uuid.uuid4()),
        enterprise_id=enterprise_id,
        name=name,
        slug=slug,
        is_active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(organization)
    await session.flush()
    return organization, True


async def _get_or_create_default_team(
    session, *, organization_id: str
) -> tuple[TeamModel, bool]:
    """Return (team, created). One default team per organization."""
    existing = (
        await session.execute(
            select(TeamModel).where(
                TeamModel.organization_id == organization_id,
                TeamModel.name == DEFAULT_TEAM_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    team = TeamModel(
        team_id=str(uuid.uuid4()),
        organization_id=organization_id,
        name=DEFAULT_TEAM_NAME,
        description="Default team for this organization",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(team)
    await session.flush()
    return team, True


class RemapRefused(Exception):
    """The IdP org is already mapped to a different FaultMaven organization."""

    def __init__(self, provider_org_id: str, mapped_to: str, requested: str) -> None:
        super().__init__(provider_org_id)
        self.provider_org_id = provider_org_id
        self.mapped_to = mapped_to
        self.requested = requested


async def _ensure_mapping(
    session, *, provider_org_id: str, organization_id: str
) -> bool:
    """Create the mapping row if absent. Returns True when created.

    Raises ``RemapRefused`` when the IdP org already points elsewhere.
    """
    existing = await session.get(SSOOrgMappingModel, (PROVIDER, provider_org_id))
    if existing is not None:
        if existing.organization_id != organization_id:
            raise RemapRefused(
                provider_org_id, existing.organization_id, organization_id
            )
        return False

    session.add(
        SSOOrgMappingModel(
            provider=PROVIDER,
            provider_org_id=provider_org_id,
            organization_id=organization_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.flush()
    return True


async def provision(
    *, name: str, slug: str, workos_org_id: str, enterprise_id: str | None
) -> bool:
    """Provision (or report) the tenant + mapping. Returns True on success."""
    print("=" * 80)
    print("Provision SSO Organization Mapping")
    print("=" * 80)

    async with get_db_session() as session:
        try:
            enterprise, enterprise_created = await _get_or_create_enterprise(
                session, enterprise_id=enterprise_id, name=name, slug=slug
            )
        except LookupError as exc:
            print(f"❌ {exc}")
            return False

        organization, org_created = await _get_or_create_organization(
            session,
            enterprise_id=enterprise.enterprise_id,
            name=name,
            slug=slug,
        )

        # Bind the tenant for the remaining writes, so they stay inside the
        # migration-018 policy even under FORCE ROW LEVEL SECURITY.
        set_current_org_id(organization.organization_id)

        team, team_created = await _get_or_create_default_team(
            session, organization_id=organization.organization_id
        )

        try:
            mapping_created = await _ensure_mapping(
                session,
                provider_org_id=workos_org_id,
                organization_id=organization.organization_id,
            )
        except RemapRefused as exc:
            print(
                f"\n❌ {PROVIDER} organization '{exc.provider_org_id}' is already "
                "mapped to a different FaultMaven organization."
            )
            print(f"   currently mapped to: {exc.mapped_to}")
            print(f"   requested:           {exc.requested}")
            print(
                "\n   Remapping is a deliberate operator action — it changes which "
                "tenant\n   existing users land in on their next login. See "
                "docs/operations/sso-org-provisioning.md."
            )
            return False

    def mark(created: bool) -> str:
        return "created" if created else "already present"

    print("\n✅ Tenant ready\n")
    print(f"  Enterprise:   {enterprise.enterprise_id}  ({mark(enterprise_created)})")
    print(f"    name/slug:  {enterprise.name} / {enterprise.slug}")
    print(f"  Organization: {organization.organization_id}  ({mark(org_created)})")
    print(f"    name/slug:  {organization.name} / {organization.slug}")
    print(f"  Default team: {team.team_id}  ({mark(team_created)})")
    print(f"  Mapping:      {PROVIDER}:{workos_org_id}  ({mark(mapping_created)})")
    print("")
    print("Next steps:")
    print("  1. In WorkOS, ensure the users you expect are members of")
    print(f"     organization {workos_org_id}.")
    print("  2. Have the first user sign in through the dashboard's SSO button.")
    print("     They are provisioned just-in-time as an organization member.")
    print("  3. Promote them if they need admin rights:")
    print("       python scripts/auth/promote_to_platform_admin.py <username>")
    print("")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision a Cloud tenant and map a WorkOS organization to it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--name", required=True, help="Organization display name (e.g. 'Acme Corp')"
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="URL-friendly organization slug, unique within the enterprise",
    )
    parser.add_argument(
        "--workos-org-id",
        required=True,
        help="WorkOS organization id to map (looks like org_01H...)",
    )
    parser.add_argument(
        "--enterprise-id",
        default=None,
        help=(
            "Existing enterprise to create the organization under. "
            "Default: reuse (or create) an enterprise with the same slug."
        ),
    )
    args = parser.parse_args()

    success = asyncio.run(
        provision(
            name=args.name,
            slug=args.slug,
            workos_org_id=args.workos_org_id,
            enterprise_id=args.enterprise_id,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

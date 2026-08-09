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
application role (``faultmaven_app``). A preflight verifies the connected role
really is RLS-exempt and refuses before any write if it is not — the pod's own
``DATABASE_URL`` is the application role by design, so an unqualified
``kubectl exec`` would otherwise run under exactly the role this script forbids.

That exemption is the *whole* mechanism. Nothing here scopes the writes to the
new tenant, because nothing can: the tenant policies key on ``organization_id``,
while this script resolves an organization by ``(enterprise_id, slug)`` — the id
is what the lookup exists to learn. A role RLS applies to cannot read the row it
must be idempotent about under any binding, so its INSERT then trips the policy's
WITH CHECK arm (migration 018 omits ``FOR``, so USING doubles as WITH CHECK).
Provisioning is therefore incompatible with FORCE ROW LEVEL SECURITY, which
subjects a table's *owner* to its policies — superusers and ``BYPASSRLS`` roles
are never forced. FaultMaven does not enable FORCE RLS anywhere; enabling it
would break this path rather than harden it.

Admin binding is manual and post-hoc (ADR-015 D5): no login path grants
elevated roles, so the first user signs in via SSO and an operator promotes
them with the existing role scripts.

Usage (``fm-provision-sso-org``, installed with the package):
    DATABASE_URL=postgresql+asyncpg://faultmaven:...@host/faultmaven \\
    fm-provision-sso-org \\
        --name "Acme Corp" --slug acme --workos-org-id org_01H...

    # Reuse an existing enterprise instead of creating one
    fm-provision-sso-org \\
        --name "Acme EU" --slug acme-eu --workos-org-id org_01J... \\
        --enterprise-id 8f1c...

In a Kubernetes deployment, run it in the API pod — with the owner DSN passed
explicitly, because the pod's environment holds the limited application role:
    kubectl exec -it deploy/faultmaven-api -- \\
        env DATABASE_URL="$OWNER_DSN" \\
        fm-provision-sso-org --name ... --slug ... \\
        --workos-org-id org_...
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    TeamModel,
)
from faultmaven.infrastructure.persistence.rls_role_guard import (
    assert_provisioning_db_role_bypasses_rls,
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
    """Return (organization, created). Identity is (enterprise_id, slug).

    Resolving by slug is what makes re-running a no-op — and it is also why the
    script needs an RLS-exempt role. ``organizations`` is RLS-tenanted
    (migration 018) and its policy keys on ``organization_id``, so a role that
    RLS applies to could not read this row without already knowing the id this
    lookup exists to find. See the module docstring. ``enterprises`` is not
    tenanted (no ``organization_id`` column — migration 018), so the enterprise
    write above is unaffected either way.
    """
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

    organization_id = str(uuid.uuid4())

    organization = OrganizationModel(
        organization_id=organization_id,
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


class OrgAlreadyClaimed(Exception):
    """The FaultMaven organization is already claimed by another IdP org."""

    def __init__(
        self, organization_id: str, claimed_by: str, requested_by: str
    ) -> None:
        super().__init__(organization_id)
        self.organization_id = organization_id
        self.claimed_by = claimed_by
        self.requested_by = requested_by


async def _find_mapping(session, *, provider_org_id: str):
    """Return the mapping row for this IdP org, or None."""
    return await session.get(SSOOrgMappingModel, (PROVIDER, provider_org_id))


async def _ensure_mapping(
    session, *, provider_org_id: str, organization_id: str
) -> bool:
    """Create the mapping row if absent. Returns True when created.

    Both directions of the 1:1 relation are checked before writing, because
    both are ways to bind the wrong customers together and neither should
    surface as a raw ``IntegrityError``:

    * ``RemapRefused`` — this IdP org already points at a *different*
      organization. Repointing changes which tenant existing users land in.
    * ``OrgAlreadyClaimed`` — this organization is already claimed by a
      *different* IdP org. This is what the ``UNIQUE (provider,
      organization_id)`` constraint would otherwise raise, and it is the alarm
      that fires when a slug collision has silently resolved a new customer
      onto someone else's tenant.

    Both are deliberate operator acts, so the script refuses and exits non-zero
    rather than guessing.
    """
    existing = await _find_mapping(session, provider_org_id=provider_org_id)
    if existing is not None:
        if existing.organization_id != organization_id:
            raise RemapRefused(
                provider_org_id, existing.organization_id, organization_id
            )
        return False

    claimed = (
        await session.execute(
            select(SSOOrgMappingModel).where(
                SSOOrgMappingModel.provider == PROVIDER,
                SSOOrgMappingModel.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if claimed is not None:
        raise OrgAlreadyClaimed(
            organization_id, claimed.provider_org_id, provider_org_id
        )

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

    # Preflight, before any write: the connected role must be RLS-exempt. The
    # docstring's "run it with the owner DSN" was previously advice only, and
    # the documented `kubectl exec` recipe inherits the pod's DATABASE_URL —
    # which main.py's assert_app_db_role_enforces_rls *guarantees* is the
    # RLS-scoped application role. Advice that the happy path contradicts is a
    # gate that never fires; this one does.
    try:
        db_role = await assert_provisioning_db_role_bypasses_rls()
    except DeploymentCoherenceError as exc:
        print(f"\n❌ {exc}")
        return False
    if db_role:
        print(f"\nDatabase role: {db_role} (RLS-exempt — provisioning allowed)")

    # Every refusal raises out of the session block on purpose: get_db_session
    # commits on normal exit, so returning from inside it would COMMIT the
    # enterprise/organization/team this run just created and leave a tenant with
    # no mapping behind — the very state that makes the next run's slug lookup
    # dangerous. Raising rolls the whole thing back.
    try:
        async with get_db_session() as session:
            enterprise, enterprise_created = await _get_or_create_enterprise(
                session, enterprise_id=enterprise_id, name=name, slug=slug
            )

            organization, org_created = await _get_or_create_organization(
                session,
                enterprise_id=enterprise.enterprise_id,
                name=name,
                slug=slug,
            )

            team, team_created = await _get_or_create_default_team(
                session, organization_id=organization.organization_id
            )

            # Is this run about to bind the IdP org to a tenant it is not
            # already bound to? Read before the write, so the reuse warning
            # fires before anything is written — and so a plain idempotent
            # re-run (mapping already points here) stays quiet.
            prior = await _find_mapping(session, provider_org_id=workos_org_id)
            binding_is_new = (
                prior is None or prior.organization_id != organization.organization_id
            )

            if binding_is_new and not (enterprise_created and org_created):
                # A brand-new IdP binding onto a tenant this run did not create.
                # Legitimate (a second IdP org for an existing customer), but it
                # is also exactly what a slug collision looks like — say so
                # loudly, and say it before the mapping is written.
                print("")
                print("⚠️  REUSING AN EXISTING TENANT — confirm this is the right one.")
                if not enterprise_created:
                    print(
                        f"    enterprise   {enterprise.enterprise_id} "
                        f"({enterprise.name} / {enterprise.slug}) already existed"
                    )
                if not org_created:
                    print(
                        f"    organization {organization.organization_id} "
                        f"({organization.name} / {organization.slug}) already existed"
                    )
                print(
                    f"    {PROVIDER}:{workos_org_id} is being bound to it, so its "
                    "users will land in\n    that tenant and see its cases. If this "
                    "is a different customer, stop and\n    re-provision under a "
                    "distinct --slug."
                )

            mapping_created = await _ensure_mapping(
                session,
                provider_org_id=workos_org_id,
                organization_id=organization.organization_id,
            )
    except LookupError as exc:
        print(f"❌ {exc}")
        return False
    except RemapRefused as exc:
        print(
            f"\n❌ {PROVIDER} organization '{exc.provider_org_id}' is already "
            "mapped to a different FaultMaven organization."
        )
        print(f"   currently mapped to: {exc.mapped_to}")
        print(f"   requested:           {exc.requested}")
        print(
            "\n   Remapping is a deliberate operator action — it changes which "
            "tenant\n   existing users land in on their next login. Nothing was "
            "written. See\n   docs/operations/sso-org-provisioning.md."
        )
        return False
    except OrgAlreadyClaimed as exc:
        print(
            f"\n❌ FaultMaven organization {exc.organization_id} is already "
            f"claimed by a different {PROVIDER} organization."
        )
        print(f"   claimed by: {exc.claimed_by}")
        print(f"   requested:  {exc.requested_by}")
        print(
            "\n   This usually means --slug resolved onto an EXISTING tenant that "
            "belongs\n   to another customer. Nothing was written. Re-provision the "
            "new customer\n   under a distinct slug (or --enterprise-id). See\n   "
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
    print("       fm-promote-platform-admin <username>")
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

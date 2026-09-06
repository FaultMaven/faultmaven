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

That exemption is the mechanism. Nothing here scopes the writes to the new
tenant: the tenant policies key on ``organization_id``, while this script
resolves an organization by ``(enterprise_id, slug)`` — the id is what the
lookup exists to learn. So under FORCE ROW LEVEL SECURITY a scoped role could
not read that row whatever it bound, and the INSERT that followed would trip the
policy's WITH CHECK arm (migration 018 omits ``FOR``, so USING doubles as WITH
CHECK). FORCE RLS subjects a table's *owner* to its policies — superusers and
``BYPASSRLS`` roles are never forced — and FaultMaven enables it nowhere.

Slug-keyed resolution is what forces that, and it *could* be avoided:
``sso_org_mappings`` is deliberately untenanted (migration 038), so a re-run
could recover the organization id from the mapping and bind it before opening
the session. That is not done, and not from inertia — it would only help
bindings that already exist, and a first run would then meet a slug collision as
a raw unique-constraint error rather than as ``OrgAlreadyClaimed`` and the
REUSING alarm below. Those refusals are the point of this script; trading them
for resilience against a setting nothing sets would be a bad exchange.

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

from faultmaven.config.constants import STANDALONE_TEAM_NAME
from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.rls_role_guard import (
    assert_provisioning_db_role_bypasses_rls,
)

# The tenant rows, their order and their conflict refusals live in ONE writer
# (`infrastructure/persistence/tenant_bootstrap`), shared with the login path
# that provisions personal tenants (#1045). This module keeps the operator
# policy — refuse conflicts, narrate loudly — and delegates the writes, so the
# ordering constraints cannot drift between the two callers. The private
# aliases below are the names this module has always exposed.
from faultmaven.infrastructure.persistence.tenant_bootstrap import (  # noqa: F401
    PROVIDER,
    OrgAlreadyClaimed,
    RemapRefused,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    ensure_mapping as _shared_ensure_mapping,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    find_mapping as _find_mapping,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    get_or_create_default_team as _get_or_create_default_team,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    get_or_create_enterprise as _get_or_create_enterprise,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    get_or_create_organization as _shared_get_or_create_organization,
)

#: Every organization gets one team at creation (ADR-013). Aliased from the
#: standalone bootstrap's constant rather than re-spelled, so the deployment
#: shapes cannot drift into "default" teams that differ by a word.
DEFAULT_TEAM_NAME = STANDALONE_TEAM_NAME


async def _get_or_create_organization(
    session, *, enterprise_id: str, name: str, slug: str
):
    """Operator shape: the id is generated by the writer."""
    return await _shared_get_or_create_organization(
        session,
        enterprise_id=enterprise_id,
        name=name,
        slug=slug,
        organization_id=None,
    )


async def _ensure_mapping(session, *, provider_org_id: str, enterprise_id: str) -> bool:
    """Operator shape: a conflict is a refusal a human resolves, never adopted."""
    return await _shared_ensure_mapping(
        session,
        provider_org_id=provider_org_id,
        enterprise_id=enterprise_id,
    )


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
                session, enterprise_id=enterprise.enterprise_id
            )

            # Is this run about to bind the IdP org to a tenant it is not
            # already bound to? Read before the write, so the reuse warning
            # fires before anything is written — and so a plain idempotent
            # re-run (mapping already points here) stays quiet.
            prior = await _find_mapping(session, provider_org_id=workos_org_id)
            binding_is_new = (
                prior is None or prior.enterprise_id != enterprise.enterprise_id
            )

            if binding_is_new and not org_created:
                # A brand-new IdP binding onto an organization this run did not
                # create. Legitimate (a second IdP org for an existing customer),
                # but it is also exactly what a slug collision looks like — say
                # so loudly, and say it before the mapping is written.
                #
                # The organization is the isolation boundary — RLS keys on
                # organization_id — so reusing the *enterprise* alone is not the
                # hazard: a new organization under an existing enterprise is the
                # documented --enterprise-id recipe, and warning about it would
                # tell the operator that a tenant they just created is somebody
                # else's. Gating on org_created alone also makes the enterprise
                # line below unconditional: an organization this run did not
                # create implies the enterprise holding it already existed.
                print("")
                print("⚠️  REUSING AN EXISTING TENANT — confirm this is the right one.")
                print(
                    f"    enterprise   {enterprise.enterprise_id} "
                    f"({enterprise.name} / {enterprise.slug}) already existed"
                )
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
            elif org_created and not enterprise_id and not enterprise_created:
                # The organization is new, so there is no tenant to confuse — but
                # its PARENT was matched by --slug rather than named with
                # --enterprise-id, which is how a new customer silently ends up
                # owned by an existing one. Not an isolation breach (cases belong
                # to the organization), and so deliberately quieter than the
                # alarm above; it is expensive because it is hard to undo — an
                # account under the wrong enterprise fails login closed with
                # reason=enterprise_mismatch and needs a manual migration.
                #
                # Truthiness rather than `is None`, to match the test
                # _get_or_create_enterprise itself applies when it picks the slug
                # path: an empty --enterprise-id (an unset shell variable in the
                # documented kubectl recipe) IS the matched-by-slug case, and
                # must not be read as the operator naming a parent.
                print("")
                print("⚠️  NEW ORGANIZATION UNDER AN EXISTING ENTERPRISE.")
                print(
                    f"    enterprise   {enterprise.enterprise_id} "
                    f"({enterprise.name} / {enterprise.slug}) already existed and "
                    "was matched\n                 by --slug, not named with "
                    "--enterprise-id."
                )
                print(
                    "    If this customer does not belong to that enterprise, stop "
                    "and re-run\n    with a distinct --slug (or an explicit "
                    "--enterprise-id). Moving an account\n    between enterprises "
                    "later is a manual migration, not a login fix — see\n    "
                    "docs/operations/sso-org-provisioning.md."
                )

            mapping_created = await _ensure_mapping(
                session,
                provider_org_id=workos_org_id,
                enterprise_id=enterprise.enterprise_id,
            )
    except LookupError as exc:
        print(f"❌ {exc}")
        return False
    except RemapRefused as exc:
        print(
            f"\n❌ {PROVIDER} organization '{exc.provider_org_id}' is already "
            "mapped to a different FaultMaven enterprise."
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
            f"\n❌ FaultMaven enterprise {exc.enterprise_id} is already "
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

    # An --enterprise-id that was passed but is empty is ambiguous, and the two
    # readings are materially different: "put this under the enterprise I named"
    # versus "resolve the enterprise from --slug". Falling through to the slug
    # path silently joins — or creates — an enterprise the operator did not name,
    # and an account under the wrong enterprise fails login closed
    # (reason=enterprise_mismatch) and needs a manual migration to move. A bogus
    # NON-empty id already refuses with LookupError; refusing the empty one keeps
    # the boundary consistent instead of guessing. The documented kubectl recipe
    # interpolates a shell variable here, which is exactly how it arrives empty.
    if args.enterprise_id is not None and not args.enterprise_id.strip():
        parser.error(
            "--enterprise-id was given but is empty. Pass a real enterprise id, "
            "or omit the flag entirely to resolve the enterprise from --slug."
        )

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

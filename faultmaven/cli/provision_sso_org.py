"""Provision a Cloud tenant and map an IdP organization onto it (#869).

Under ``TENANT_PROVIDER=multi`` an SSO login lands in the FaultMaven
enterprise that the IdP's organization is mapped to. There is deliberately no
self-service path: an unmapped IdP organization fails the login closed
(``sso_org_unmapped``) rather than provisioning a tenant just-in-time. This
script is that out-of-band provisioning step.

What it creates (all idempotent — re-running with the same arguments is a no-op
that prints the current state):

1. an **enterprise** carrying the customer's email domain (``--domain``),
   unless ``--enterprise-id`` names one;
2. an **organization** inside it, keyed by ``--slug`` within that enterprise;
3. the ``sso_org_mappings`` row binding ``(workos, --workos-org-id)`` to the
   enterprise (ADR-017 D9).

**The domain is not decoration.** ``enterprises.domain`` is the column two live
rules key on. The team invitation rule reads a NULL domain as a *personal*
enterprise — an island that invites nobody — so a customer provisioned without
one cannot build a single team. And the SSO sign-up path looks a domain
enterprise up by that column, so a colleague signing in on the same domain
would not find this tenant and would create a **second** enterprise beside it.
Both failures are silent at provisioning time and surface days later, which is
why ``--domain`` is required rather than optional.

It creates NO team: under ADR-017 D4 a team is parented by the enterprise and
forms by consent (any account creates one; invitees accept), so nothing here
has standing to create one on a member's behalf. A team minted here would have
no members — invisible to every membership-gated read, impossible to administer
or retire, and holding its name against the enterprise's partial unique index.

Remapping is NOT a script default. If the IdP organization is already mapped to
a *different* FaultMaven enterprise the script prints both and exits non-zero:
moving a tenant's IdP binding is a deliberate operator act with token- and
membership-level consequences (see
``docs/operations/sso-org-provisioning.md``).

**Run it with the owner DSN.** ``organizations`` and ``teams`` are RLS-tenanted
(the baseline keys every policy on ``app.current_enterprise_id``) and this script writes rows for a tenant that does not exist
yet, so it needs the RLS-owning role (``faultmaven``), not the limited
application role (``faultmaven_app``). A preflight verifies the connected role
really is RLS-exempt and refuses before any write if it is not — the pod's own
``DATABASE_URL`` is the application role by design, so an unqualified
``kubectl exec`` would otherwise run under exactly the role this script forbids.

That exemption is the mechanism. Nothing here scopes the writes to the new
tenant: the tenant policies key on ``enterprise_id``, and on a first run the
enterprise this script is about to create has no id to bind yet — the id is what
the run exists to mint. So under FORCE ROW LEVEL SECURITY a scoped role could
not read that row whatever it bound, and the INSERT that followed would trip the
policy's WITH CHECK arm (the policies omit ``FOR``, so USING doubles as WITH
CHECK). FORCE RLS subjects a table's *owner* to its policies — superusers and
``BYPASSRLS`` roles are never forced — and FaultMaven enables it nowhere.

Id-blind resolution is what forces that, and it *could* be avoided:
``sso_org_mappings`` is deliberately untenanted (it is read on the
unauthenticated callback), so a re-run could recover the enterprise id from the
mapping and bind it before opening the session. That is not done, and not from
inertia — it would only help bindings that already exist, and a first run would
then meet a collision as a raw unique-constraint error rather than as
``OrgAlreadyClaimed`` and the REUSING alarm below. Those refusals are the point
of this script; trading them for resilience against a setting nothing sets would
be a bad exchange.

Admin binding is manual and post-hoc (ADR-015 D5): no login path grants
elevated roles, so the first user signs in via SSO and an operator promotes
them with the existing role scripts.

Usage (``fm-provision-sso-org``, installed with the package):
    DATABASE_URL=postgresql+asyncpg://faultmaven:...@host/faultmaven \\
    fm-provision-sso-org \\
        --name "Acme Corp" --slug acme --domain acme.com \\
        --workos-org-id org_01H...

    # Reuse an existing enterprise instead of creating one. It already carries
    # its domain, so --domain is optional here — and if given it must match.
    fm-provision-sso-org \\
        --name "Acme EU" --slug acme-eu --workos-org-id org_01J... \\
        --enterprise-id 8f1c...

In a Kubernetes deployment, run it in the API pod — with the owner DSN passed
explicitly, because the pod's environment holds the limited application role:
    kubectl exec -it deploy/faultmaven-api -- \\
        env DATABASE_URL="$OWNER_DSN" \\
        fm-provision-sso-org --name ... --slug ... --domain ... \\
        --workos-org-id org_...
"""

from __future__ import annotations

import argparse
import asyncio
import sys

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
    find_live_enterprise_by_slug as _find_live_enterprise_by_slug,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    find_mapping as _find_mapping,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    get_or_create_enterprise as _get_or_create_enterprise,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    get_or_create_enterprise_for_domain as _get_or_create_enterprise_for_domain,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    get_or_create_organization as _shared_get_or_create_organization,
)

# The fold and the consumer-domain test the SSO sign-up path applies, reused
# rather than re-spelled: both write and read `enterprises.domain`, and a second
# spelling of either rule is a tenant the other half cannot find.
from faultmaven.modules.auth.domain.personal_tenant import (
    is_personal_domain,
    normalize_domain,
)


class DomainMismatch(Exception):
    """``--enterprise-id`` names a tenant whose domain is not ``--domain``."""

    def __init__(self, enterprise_id: str, current: str | None, requested: str) -> None:
        super().__init__(enterprise_id)
        self.enterprise_id = enterprise_id
        self.current = current
        self.requested = requested


class SlugHeldByAnotherEnterprise(Exception):
    """``--slug`` is a live enterprise's, and that enterprise is not this domain's."""

    def __init__(self, slug: str, enterprise_id: str, domain: str | None) -> None:
        super().__init__(slug)
        self.slug = slug
        self.enterprise_id = enterprise_id
        self.domain = domain


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


def _personal_email_domains() -> list[str]:
    """The consumer-mail domains a company may not be provisioned onto.

    Read at the point of use, like the sign-up path reads it, so the list a
    deployment configured is the one the refusal uses.
    """
    from faultmaven.config.settings import get_settings

    return list(get_settings().auth.personal_email_domains)


async def provision(
    *,
    name: str,
    slug: str,
    workos_org_id: str,
    domain: str | None,
    enterprise_id: str | None,
) -> bool:
    """Provision (or report) the tenant + mapping. Returns True on success."""
    print("=" * 80)
    print("Provision SSO Organization Mapping")
    print("=" * 80)

    folded = normalize_domain(domain)

    # A company cannot be provisioned onto a consumer mail domain. Under
    # ADR-017 D3 such a domain yields a PRIVATE enterprise per account — an
    # island by construction — so an enterprise stamped `gmail.com` would claim
    # every consumer-mail account that ever signs up and put strangers inside
    # one isolation boundary. Refused before the role preflight: it needs no
    # database at all, and the earliest refusal is the cheapest one.
    if folded is not None and is_personal_domain(folded, _personal_email_domains()):
        print(f"\n❌ '{folded}' is a consumer mail domain (PERSONAL_EMAIL_DOMAINS).")
        print(
            "   A company tenant cannot be provisioned onto one: ADR-017 D3 gives "
            "every\n   account on such a domain a private enterprise of its own, so "
            "this row would\n   claim strangers. Provision the customer's own domain "
            "instead."
        )
        return False

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
    # enterprise and organization this run just created and leave a tenant with
    # no mapping behind — the very state that makes the next run's lookup
    # dangerous. Raising rolls the whole thing back.
    try:
        async with get_db_session() as session:
            if enterprise_id:
                enterprise, enterprise_created = await _get_or_create_enterprise(
                    session, enterprise_id=enterprise_id, name=name, slug=slug
                )
                # Naming an enterprise AND a domain asserts they agree. They may
                # not, and re-domaining an enterprise is not something a
                # provisioning run gets to do quietly: the domain decides which
                # addresses its teams may invite and which sign-ups join it, so
                # moving it moves both at once, for accounts already inside.
                current = normalize_domain(getattr(enterprise, "domain", None))
                if folded is not None and current != folded:
                    raise DomainMismatch(enterprise.enterprise_id, current, folded)
            else:
                # Resolution is keyed on the DOMAIN, not on --slug, because the
                # domain is the fact the sign-up path will re-derive on every
                # login and look this tenant up by. The slug is an operator
                # convenience that happens to be unique; keying on it would work
                # today and put a colleague in a second enterprise the day the
                # two disagreed.
                #
                # The slug is still unique among live enterprises, so a --slug
                # that belongs to a DIFFERENT tenant has to be refused here: the
                # domain lookup would miss, the INSERT would trip
                # `ix_enterprises_slug_live`, and the operator would get a raw
                # IntegrityError where this script promises a named refusal.
                slug_holder = await _find_live_enterprise_by_slug(session, slug)
                if (
                    slug_holder is not None
                    and normalize_domain(getattr(slug_holder, "domain", None)) != folded
                ):
                    raise SlugHeldByAnotherEnterprise(
                        slug,
                        slug_holder.enterprise_id,
                        normalize_domain(getattr(slug_holder, "domain", None)),
                    )
                enterprise, enterprise_created = (
                    await _get_or_create_enterprise_for_domain(
                        session, domain=folded, name=name, slug=slug
                    )
                )

            organization, org_created = await _get_or_create_organization(
                session,
                enterprise_id=enterprise.enterprise_id,
                name=name,
                slug=slug,
            )

            # Is this run about to bind the IdP org to a tenant it is not
            # already bound to? Read before the write, so the reuse warning
            # fires before anything is written — and so a plain idempotent
            # re-run (mapping already points here) stays quiet.
            prior = await _find_mapping(session, provider_org_id=workos_org_id)
            binding_is_new = (
                prior is None or prior.enterprise_id != enterprise.enterprise_id
            )

            if binding_is_new and not enterprise_created and not enterprise_id:
                # A brand-new IdP binding onto an ENTERPRISE this run did not
                # create and the operator did not name — it was matched by
                # ``--domain``. Legitimate (a colleague signed up first and the
                # sign-up path created the domain's enterprise, which is exactly
                # the row this customer should be onboarded onto), and also what
                # a mistyped domain looks like. Under ADR-017 D1 the enterprise
                # is the isolation boundary, so the consequence of the second
                # reading is the sharp one: the new customer's users land inside
                # somebody else's wall and become eligible for its teams. Say so
                # loudly, and before the mapping is written.
                #
                # Naming the enterprise with ``--enterprise-id`` is the operator
                # stating that intent, which is the documented second-organization
                # recipe and stays quiet.
                #
                # Truthiness rather than ``is None`` on ``enterprise_id``, to
                # match the test ``_get_or_create_enterprise`` itself applies
                # when it picks the id path: an empty ``--enterprise-id`` (an
                # unset shell variable in the documented kubectl recipe) IS the
                # matched-by-domain case, and must not be read as the operator
                # naming a parent.
                print("")
                print("⚠️  REUSING AN EXISTING TENANT — confirm this is the right one.")
                print(
                    f"    enterprise   {enterprise.enterprise_id} "
                    f"({enterprise.name} / {enterprise.slug}) already existed and "
                    "was matched\n                 by --domain, not named with "
                    "--enterprise-id."
                )
                print(
                    f"    organization {organization.organization_id} "
                    f"({organization.name} / {organization.slug})"
                )
                print(
                    f"    {PROVIDER}:{workos_org_id} is being bound to that "
                    "enterprise, so its users\n    will land inside it and can be "
                    "invited to its teams. If this is a different\n    customer, "
                    "stop and re-provision under its own --domain."
                )

            if not getattr(enterprise, "domain", None):
                # Only reachable through ``--enterprise-id`` — the domain arm
                # always stamps one. An enterprise with no domain is the state
                # the 2026-09-10 cutover left behind: its teams can invite
                # nobody, and the next sign-up from the customer's domain builds
                # a second enterprise beside it.
                print("")
                print("⚠️  THIS ENTERPRISE CARRIES NO DOMAIN.")
                print(
                    "    Team invitations inside it are refused (a NULL domain "
                    "reads as a\n    personal enterprise), and a colleague signing "
                    "in on the customer's domain\n    will create a SECOND "
                    "enterprise. Re-run with --domain to stamp it."
                )

            mapping_created = await _ensure_mapping(
                session,
                provider_org_id=workos_org_id,
                enterprise_id=enterprise.enterprise_id,
            )
    except LookupError as exc:
        print(f"❌ {exc}")
        return False
    except DomainMismatch as exc:
        print(
            f"\n❌ enterprise {exc.enterprise_id} carries domain "
            f"{exc.current or '(none)'}, not '{exc.requested}'."
        )
        print(
            "\n   Re-domaining an enterprise changes which addresses its teams may "
            "invite\n   and which sign-ups join it, for the accounts already inside "
            "it. Nothing\n   was written. Drop --domain to use the enterprise as it "
            "stands, or provision\n   this domain under its own enterprise. See\n   "
            "docs/operations/sso-org-provisioning.md."
        )
        return False
    except SlugHeldByAnotherEnterprise as exc:
        print(
            f"\n❌ slug '{exc.slug}' already belongs to enterprise "
            f"{exc.enterprise_id} (domain {exc.domain or '(none)'})."
        )
        print(
            "\n   The enterprise for this --domain does not exist yet, and creating "
            "it under\n   a slug another live tenant holds is refused. Nothing was "
            "written. Pick a\n   distinct --slug, or name the intended tenant with "
            "--enterprise-id. See\n   docs/operations/sso-org-provisioning.md."
        )
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
            "\n   This usually means --domain resolved onto an EXISTING tenant that "
            "belongs\n   to another customer. Nothing was written. Re-provision the "
            "new customer\n   under its own domain (or --enterprise-id). See\n   "
            "docs/operations/sso-org-provisioning.md."
        )
        return False

    def mark(created: bool) -> str:
        return "created" if created else "already present"

    print("\n✅ Tenant ready\n")
    print(f"  Enterprise:   {enterprise.enterprise_id}  ({mark(enterprise_created)})")
    print(f"    name/slug:  {enterprise.name} / {enterprise.slug}")
    print(f"    domain:     {enterprise.domain or '(none)'}")
    print(f"  Organization: {organization.organization_id}  ({mark(org_created)})")
    print(f"    name/slug:  {organization.name} / {organization.slug}")
    print(f"  Mapping:      {PROVIDER}:{workos_org_id}  ({mark(mapping_created)})")
    print("")
    print("Next steps:")
    print("  1. In WorkOS, ensure the users you expect are members of")
    print(f"     organization {workos_org_id}.")
    print("  2. Have the first user sign in through the dashboard's SSO button.")
    print("     They are provisioned just-in-time and land in this ENTERPRISE —")
    print("     the isolation boundary, and the only membership a sign-in")
    print("     establishes (ADR-017 D9). They join NO organization: an")
    print("     organization is a billing target created by payment (D5), so a")
    print("     sign-in cannot know of one and must not invent one. Their")
    print("     allowance until then is the per-account default.")
    print("  3. Promote them if they need admin rights:")
    print("       fm-promote-platform-admin <username>")
    print("  4. Teams are formed by the customer, not by this script: any account")
    print("     creates one and invitees accept (ADR-017 D4). Invitations are")
    print("     decided by the enterprise's domain above, so a tenant without one")
    print("     can invite nobody.")
    print("  5. Billing membership, if and when somebody pays, is a separate")
    print("     deliberate act against the organization above:")
    print(f"       organization {organization.organization_id}")
    print("     Removing one is fm-remove-org-member; the account keeps its")
    print("     enterprise anchor either way, because leaving an organization")
    print("     changes what is metered, not what is visible.")
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
        "--domain",
        default=None,
        help=(
            "The customer's email domain (e.g. acme.com). This is what the "
            "enterprise is found by when a colleague signs in, and what decides "
            "which addresses its teams may invite. Required unless "
            "--enterprise-id names an enterprise that already carries one."
        ),
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
            "Default: reuse (or create) the enterprise for --domain."
        ),
    )
    args = parser.parse_args()

    # An --enterprise-id that was passed but is empty is ambiguous, and the two
    # readings are materially different: "put this under the enterprise I named"
    # versus "resolve the enterprise from --domain". Falling through to the
    # domain path silently joins — or creates — an enterprise the operator did
    # not name, and an account under the wrong enterprise fails login closed
    # (reason=enterprise_mismatch) and needs a manual migration to move. A bogus
    # NON-empty id already refuses with LookupError; refusing the empty one keeps
    # the boundary consistent instead of guessing. The documented kubectl recipe
    # interpolates a shell variable here, which is exactly how it arrives empty.
    if args.enterprise_id is not None and not args.enterprise_id.strip():
        parser.error(
            "--enterprise-id was given but is empty. Pass a real enterprise id, "
            "or omit the flag entirely to resolve the enterprise from --domain."
        )

    # Same reasoning for --domain, and one step further: an empty value must not
    # fall through to "no domain", because a domainless enterprise is the defect
    # this argument exists to close.
    if args.domain is not None and not args.domain.strip():
        parser.error(
            "--domain was given but is empty. Pass the customer's email domain "
            "(e.g. acme.com)."
        )
    if args.domain is None and not args.enterprise_id:
        parser.error(
            "--domain is required. It is the column a colleague's sign-in finds "
            "this enterprise by and the one team invitations are decided on; an "
            "enterprise without it can invite nobody and is duplicated by the "
            "next sign-up. Pass --enterprise-id instead only to reuse an "
            "enterprise that already carries its domain."
        )

    domain = normalize_domain(args.domain)
    if domain is not None and ("@" in domain or any(c.isspace() for c in domain)):
        # Almost always a pasted email address. It would provision cleanly and
        # then match nothing, which is exactly the silent shape --domain exists
        # to prevent — so it is refused rather than folded into something.
        parser.error(
            "--domain takes a bare domain (acme.com), not an email address or a "
            f"phrase: {args.domain!r}"
        )

    success = asyncio.run(
        provision(
            name=args.name,
            slug=args.slug,
            workos_org_id=args.workos_org_id,
            domain=domain,
            enterprise_id=args.enterprise_id,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

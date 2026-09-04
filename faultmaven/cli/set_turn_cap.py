"""Set, raise or clear one organization's daily investigation-turn cap.

The operator half of the per-tenant turn cap (ADR-016 D5.3). The cap's
*default* is a setting and moves only with a redeploy; a single tenant's cap is
a row, and this command writes it — so raising a customer off the default, or
taking a cap off entirely, takes effect on that tenant's **next turn** with no
restart and no redeploy.

Why the operator CLI and not the platform-admin API
----------------------------------------------------
Per-organization operator actions in this repo are console entrypoints, not
routes: ``fm-provision-sso-org`` creates a tenant, ``fm-remove-org-member``
removes a membership, ``fm-reassign-cases`` moves cases between owners. The
platform-admin API's own per-organization surface is empty — ``/admin/*``
addresses users, LLM config, config status, cross-tenant case metadata and
break-glass grants, and nothing there mutates an organization. Adding the first
such route to change a spend control is a bigger decision than this change; the
precedent the repo already has is a command, and this follows it.

Usage (``fm-set-turn-cap``, installed with the package)
--------------------------------------------------------
    fm-set-turn-cap --organization-id <org-id> --show
    fm-set-turn-cap --organization-id <org-id> --cap 200 --yes
    fm-set-turn-cap --organization-id <org-id> --unlimited --yes
    fm-set-turn-cap --organization-id <org-id> --clear --yes

In a Kubernetes deployment, run it in the API pod::

    kubectl exec -it deploy/faultmaven-api -- \\
        fm-set-turn-cap --organization-id <org-id> --cap 200 --yes

The three write modes are the three states the column can hold, and they are
separate flags rather than a magic number an operator has to remember:

``--cap N``      cap this tenant at N turns per UTC day, whatever kind it is.
``--unlimited``  no cap for this tenant, whatever kind it is (stores ``0``).
``--clear``      remove the override. A personal tenant falls back to
                 ``TENANT_DAILY_TURN_CAP``; a company tenant becomes uncapped.

``--clear`` and ``--unlimited`` are **not** the same action and the difference
bites on a personal tenant: clearing returns it to the deployment default,
un-limiting takes the cap off. On a company tenant they happen to have the same
effect today, which is exactly why they must not share a spelling — a later
change to the company default would silently reinterpret every ``--clear`` an
operator meant as "uncapped".

The organization is addressed by **id**, not slug, for the same reason
``fm-remove-org-member`` is: the id lets the command bind the tenant context and
run under the pod's own RLS-scoped application role (migration 018), where a
slug lookup would need to read ``organizations`` across tenants.

Exit codes
----------
| 0 | success, a dry run, or ``--show`` |
| 1 | refused: no such organization, or the write matched no row |
| 2 | argparse usage error (a bad flag), reserved by argparse — nothing written |
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from faultmaven.cli._confirmation import require_confirmation

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = (
    "Set, raise or clear one organization's daily investigation-turn cap. "
    "Takes effect on that tenant's next turn; no restart."
)


#: How each ``CapPolicy.source`` the resolver can answer with reads to a human.
#: Keyed on the source rather than re-deriving the rule from the override, which
#: is the whole point: what an operator is shown is the verdict the next turn
#: will actually meet, resolved by the same object, not a second description of
#: the policy that can drift from it.
_SOURCE_WORDS = {
    "single_tenant": "uncapped — single-tenant deployments are never capped",
    "override_unlimited": "override 0 → uncapped (explicitly)",
    "override": "override {limit} → {limit} turns/day",
    "default_personal": (
        "no override → {limit} turns/day "
        "(the deployment default, because this is a personal tenant)"
    ),
    "company_uncapped": "no override → uncapped (a company organization)",
    "indeterminate": (
        "could not be determined → {limit} turns/day "
        "(the default, applied fail-closed)"
    ),
    "cleared": (
        "no override → the deployment policy "
        "(the default cap for a personal tenant, uncapped for a company)"
    ),
}


def _describe(policy) -> str:
    """One line for a resolved :class:`CapPolicy`."""
    words = _SOURCE_WORDS.get(policy.source, "{limit}")
    return words.format(limit=policy.limit)


async def set_turn_cap(
    *,
    organization_id: str,
    new_value: object,
    show_only: bool,
    dry_run: bool,
    resolver=None,
    organizations=None,
    ledger=None,
) -> int:
    """Read, and optionally write, one organization's cap. Returns the exit code.

    ``new_value`` is the value to store: an ``int`` (0 for unlimited) or ``None``
    to clear the override. It is ignored when ``show_only``.

    Everything is reached through the same ports the enforcement uses — the
    ``CapPolicyResolver``, the organization repository and the ledger — rather
    than through SQL of its own. Three things follow, and each was a defect
    before: what ``--show`` prints is the verdict the next turn will meet rather
    than a second rendering of the policy; a soft-deleted organization stops
    resolving, because the repository filters ``deleted_at`` and this no longer
    goes around it; and the write goes through ``update_organization``, so the
    domain object is what carries the value.
    """
    from faultmaven.config.tenant_context import set_current_org_id
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )
    from faultmaven.infrastructure.protection.tenant_turn_cap import (
        CapPolicyResolver,
        SqlTurnLedger,
        utc_day,
    )
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_org_repository import (  # noqa: E501
        SessionlessSSOPersonalOrgRepository,
    )

    print("=" * 80)
    print("Tenant Daily Turn Cap")
    print("=" * 80)

    # RLS (migration 018) scopes `organizations` and the usage ledger by
    # `app.current_org_id`. Bind it before any read so everything below runs
    # under the pod's own application role, exactly as the request path does.
    set_current_org_id(organization_id)

    organizations = organizations or SessionlessOrganizationRepository()
    resolver = resolver or CapPolicyResolver(
        SessionlessSSOPersonalOrgRepository(),
        organizations,
        # An operator asking about a tenant is asking about the multi-tenant
        # policy even on a box where the API happens to run single-tenant, so
        # the deployment short-circuit is deliberately not applied here — it
        # would print "uncapped" for every organization and answer nothing.
        multi_tenant=lambda: True,
    )
    ledger = ledger or SqlTurnLedger()
    today = utc_day()

    organization = await organizations.get_organization(organization_id)
    if organization is None:
        print(
            f"\n❌ No organization '{organization_id}' is visible.\n"
            "   Check the id (it is an id, not a slug); a deleted "
            "organization does not resolve."
        )
        return 1

    policy = await resolver.resolve(organization_id)
    used_today = await ledger.usage(organization_id, today)

    print(f"\nOrganization: {organization.name} ({organization_id})")
    print(f"Current cap:  {_describe(policy)}")
    print(f"Used today:   {used_today} turns (UTC day {today.isoformat()})")

    if show_only:
        return 0

    print(f"New cap:      {_describe(_policy_for(new_value, policy))}")

    if dry_run:
        print("\nDry run — nothing was written. Re-run with --yes to apply.")
        return 0

    organization.daily_turn_cap = new_value
    if not await organizations.update_organization(organization):
        # The organization was readable moments ago on the same bound tenant,
        # so this is a concurrent change rather than a scoping mistake.
        # Reporting success would tell an operator a spend control moved when
        # it did not.
        print(
            "\n❌ The organization was readable but the update matched no "
            "row. Nothing was written — re-run and check the id."
        )
        return 1

    print("\n✅ Cap updated. It applies to this tenant's next turn; no restart.")
    return 0


def _policy_for(new_value, current: "object") -> "object":
    """What the cap will read as once ``new_value`` is stored.

    Derived by asking the same question the resolver asks, in the same order:
    an override decides on its own, and only its absence falls back to the kind
    the current policy already established.
    """
    from faultmaven.infrastructure.protection.tenant_turn_cap import (
        UNLIMITED_OVERRIDE,
        CapPolicy,
    )

    if new_value is None:
        # Back to the deployment policy. Which one that is depends on the
        # tenant's kind, which the CURRENT policy already tells us whenever it
        # was not itself an override.
        if current.source in ("default_personal", "company_uncapped", "indeterminate"):
            return current
        # The current policy is an override, so it says nothing about the kind.
        # Say so rather than guess.
        return CapPolicy(limit=None, source="cleared")
    if new_value == UNLIMITED_OVERRIDE:
        return CapPolicy(limit=None, source="override_unlimited")
    return CapPolicy(limit=int(new_value), source="override")


def main() -> None:
    """Console entrypoint (``fm-set-turn-cap``)."""
    parser = argparse.ArgumentParser(
        prog="fm-set-turn-cap",
        description=_SUMMARY,
        epilog=(
            "--clear returns the tenant to the deployment policy (a personal "
            "tenant to TENANT_DAILY_TURN_CAP, a company organization to "
            "uncapped); --unlimited takes the cap off outright. They are not "
            "the same action."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--organization-id",
        required=True,
        help="Organization id to read or change (an id, not a slug)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--show",
        action="store_true",
        help="Report the current cap and today's usage; write nothing",
    )
    mode.add_argument(
        "--cap",
        type=int,
        metavar="N",
        help="Cap this tenant at N investigation turns per UTC day (N >= 1)",
    )
    mode.add_argument(
        "--unlimited",
        action="store_true",
        help="Remove this tenant's cap entirely (stored as 0)",
    )
    mode.add_argument(
        "--clear",
        action="store_true",
        help=(
            "Remove the override so the deployment policy applies again "
            "(personal → the default cap, company → uncapped)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and exit without writing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the write (required for --cap / --unlimited / --clear)",
    )
    args = parser.parse_args()

    if args.cap is not None and args.cap < 1:
        # Zero has a spelling of its own (--unlimited) and negatives are refused
        # by the column's CHECK. Catching it here means the operator is told
        # which flag they wanted rather than reading an integrity error.
        parser.error("--cap must be at least 1; use --unlimited to remove the cap.")

    # ``--show`` and ``--clear`` both mean "store nothing / store NULL", and the
    # ``show_only`` flag below is what distinguishes them — so there is one
    # expression here rather than an ``if args.show`` arm that computed the same
    # value the ``--clear`` arm already does.
    new_value: object = 0 if args.unlimited else args.cap

    if not args.show:
        require_confirmation(
            parser, args, "This changes what a tenant is allowed to spend."
        )

    sys.exit(
        asyncio.run(
            set_turn_cap(
                organization_id=args.organization_id,
                new_value=new_value,
                show_only=args.show,
                dry_run=args.dry_run,
            )
        )
    )


if __name__ == "__main__":
    main()

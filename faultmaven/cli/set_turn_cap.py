"""Read, raise or clear one billing subject's daily investigation-turn cap.

The operator half of the per-tenant turn cap (ADR-016 D5.3, ADR-017 D5). The
cap's *default* is a setting and moves only with a redeploy; a single tenant's
cap is a row, and this command writes it — so raising a customer off the
default, or taking a cap off entirely, takes effect on that tenant's **next
turn** with no restart and no redeploy.

What this command addresses
---------------------------
A **billing subject** (ADR-017 D5), which is one of exactly two things: the
**organization** that pays for an account, or the **account itself** when
nobody does. Both are addressable here because both are metered, and an
operator asked "why was this person refused?" needs to be able to ask about the
person. Only one of them is writable: the override lives on
``organizations.daily_turn_cap``, and an account in no organization has no row
to carry one. Asking to write an account's cap is therefore refused, in the one
place that can say what to do instead, rather than accepted and silently
dropped.

Before ADR-017 "personal" was an organization of its own, so ``--organization-id``
addressed every subject there was. It no longer does: a personal account has no
organization, and pointing this command at one would report on a tenant that
does not exist.

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
    fm-set-turn-cap --enterprise-id <ent-id> --organization-id <org-id> --show
    fm-set-turn-cap --enterprise-id <ent-id> --account-id <user-id> --show
    fm-set-turn-cap --enterprise-id <ent-id> --organization-id <org-id> --cap 200 --yes
    fm-set-turn-cap --enterprise-id <ent-id> --organization-id <org-id> --unlimited --yes
    fm-set-turn-cap --enterprise-id <ent-id> --organization-id <org-id> --clear --yes

In a Kubernetes deployment, run it in the API pod::

    kubectl exec -it deploy/faultmaven-api -- \\
        fm-set-turn-cap --enterprise-id <ent-id> --organization-id <org-id> \\
        --cap 200 --yes

The three write modes are the three states the column can hold, and they are
separate flags rather than a magic number an operator has to remember:

``--cap N``      cap this tenant at N turns per UTC day, whatever kind it is.
``--unlimited``  no cap for this tenant, whatever kind it is (stores ``0``).
``--clear``      remove the override. An account in no organization falls back
                 to ``TENANT_DAILY_TURN_CAP``; a company organization becomes
                 uncapped.

``--clear`` and ``--unlimited`` are **not** the same action and the difference
bites on an organization that was put on an explicit cap: clearing returns it to
the deployment policy, un-limiting takes the cap off. On a company organization
they happen to have the same effect today, which is exactly why they must not
share a spelling — a later change to the company default would silently
reinterpret every ``--clear`` an operator meant as "uncapped".

The subject is addressed by **id**, not slug, for the same reason
``fm-remove-org-member`` is: the id lets the command bind the tenant context and
run under the pod's own RLS-scoped application role, where a slug lookup would
need to read ``organizations`` across tenants.

Exit codes
----------
| 0 | success, a dry run, or ``--show`` |
| 1 | refused: no such organization, an account write, or a write that matched
      no row |
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
    "Read, raise or clear one billing subject's daily investigation-turn cap. "
    "Takes effect on that subject's next turn; no restart."
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
        "(the deployment default, because nobody pays for this account)"
    ),
    "company_uncapped": "no override → uncapped (a company organization)",
    "indeterminate": (
        "could not be determined → {limit} turns/day "
        "(the default, applied fail-closed)"
    ),
    "cleared": (
        "no override → the deployment policy "
        "(the default cap for an unpaid account, uncapped for a company)"
    ),
}


def _describe(policy) -> str:
    """One line for a resolved :class:`CapPolicy`."""
    words = _SOURCE_WORDS.get(policy.source, "{limit}")
    return words.format(limit=policy.limit)


async def set_turn_cap(
    *,
    organization_id: str | None = None,
    account_id: str | None = None,
    new_value: object,
    show_only: bool,
    dry_run: bool,
    enterprise_id: str,
    resolver=None,
    organizations=None,
    ledger=None,
) -> int:
    """Read, and optionally write, one billing subject's cap. Returns the exit code.

    Exactly one of ``organization_id`` / ``account_id`` names the subject
    (ADR-017 D5). ``new_value`` is the value to store: an ``int`` (0 for
    unlimited) or ``None`` to clear the override. It is ignored when
    ``show_only``, and it is **refused** for an account subject, which has no
    row to carry an override.

    Everything is reached through the same ports the enforcement uses — the
    ``CapPolicyResolver``, the organization repository and the ledger — rather
    than through SQL of its own. Three things follow, and each was a defect
    before: what ``--show`` prints is the verdict the next turn will meet rather
    than a second rendering of the policy; a soft-deleted organization stops
    resolving, because the repository filters ``deleted_at`` and this no longer
    goes around it; and the write goes through ``update_organization``, so the
    domain object is what carries the value.
    """
    from faultmaven.config.tenant_context import set_current_enterprise_id
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )
    from faultmaven.infrastructure.protection.tenant_turn_cap import (
        SUBJECT_ACCOUNT,
        SUBJECT_ORGANIZATION,
        BillingSubject,
        CapPolicyResolver,
        SqlTurnLedger,
        utc_day,
    )

    if bool(organization_id) == bool(account_id):
        raise ValueError(
            "name exactly one billing subject: an organization or an account"
        )

    print("=" * 80)
    print("Tenant Daily Turn Cap")
    print("=" * 80)

    # RLS scopes `organizations` and the usage ledger by
    # ``app.current_enterprise_id`` (ADR-017 D1). Bind the ENTERPRISE before any
    # read so everything below runs under the pod's own application role,
    # exactly as the request path does — a subject id alone resolves nothing,
    # which is why the enterprise is a required argument rather than something
    # this could derive.
    set_current_enterprise_id(enterprise_id)

    subject = (
        BillingSubject(SUBJECT_ORGANIZATION, organization_id)
        if organization_id
        else BillingSubject(SUBJECT_ACCOUNT, account_id)
    )

    organizations = organizations or SessionlessOrganizationRepository()
    resolver = resolver or CapPolicyResolver(
        organizations,
        # An operator asking about a tenant is asking about the multi-tenant
        # policy even on a box where the API happens to run single-tenant, so
        # the deployment short-circuit is deliberately not applied here — it
        # would print "uncapped" for every organization and answer nothing.
        multi_tenant=lambda: True,
    )
    ledger = ledger or SqlTurnLedger()
    today = utc_day()

    organization = None
    if not subject.is_account:
        organization = await organizations.get_organization(organization_id)
        if organization is None:
            print(
                f"\n❌ No organization '{organization_id}' is visible.\n"
                "   Check the id (it is an id, not a slug); a deleted "
                "organization does not resolve."
            )
            return 1

    policy = await resolver.resolve(subject)
    used_today = await ledger.usage(subject, today)

    if organization is not None:
        print(f"\nOrganization: {organization.name} ({organization_id})")
    else:
        # No row is read for an account, and none exists to read: "nobody pays
        # for this account" is the whole of what makes it its own subject
        # (ADR-017 D5), so the id is all there is to name.
        print(f"\nAccount:      {account_id} (in no organization)")
    print(f"Current cap:  {_describe(policy)}")
    print(f"Used today:   {used_today} turns (UTC day {today.isoformat()})")

    if show_only:
        return 0

    if subject.is_account:
        # Refused here rather than at argparse, so the operator sees the
        # subject's CURRENT cap and usage above — which is usually the question
        # behind the attempted write — together with the only action that can
        # change it.
        print(
            "\n❌ An account has no cap of its own to write: the override lives "
            "on `organizations.daily_turn_cap`, and this account is in no "
            "organization.\n"
            "   Add the account to an organization and set the cap there, or "
            "move the deployment default (TENANT_DAILY_TURN_CAP)."
        )
        return 1

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
            "The subject is an organization or an account (ADR-017 D5); only an "
            "organization is writable. --clear returns it to the deployment "
            "policy (an unpaid account to TENANT_DAILY_TURN_CAP, a company "
            "organization to uncapped); --unlimited takes the cap off outright. "
            "They are not the same action."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--enterprise-id",
        required=True,
        help=(
            "Enterprise the subject belongs to — the tenant every read and "
            "write below is RLS-scoped by (an id, not a slug)"
        ),
    )
    # Exactly one billing subject (ADR-017 D5). Mutually exclusive AND required,
    # so "which subject?" is never answered by a default — an operator who omits
    # it is told, rather than silently shown the wrong one.
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument(
        "--organization-id",
        help="Organization id to read or change (an id, not a slug)",
    )
    who.add_argument(
        "--account-id",
        help=(
            "Account (user) id to read — the billing subject for an account in "
            "no organization. Read-only: an account carries no override"
        ),
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
            "(an unpaid account → the default cap, an organization → uncapped)"
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
                enterprise_id=args.enterprise_id,
                organization_id=args.organization_id,
                account_id=args.account_id,
                new_value=new_value,
                show_only=args.show,
                dry_run=args.dry_run,
            )
        )
    )


if __name__ == "__main__":
    main()

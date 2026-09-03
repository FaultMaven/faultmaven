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

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = (
    "Set, raise or clear one organization's daily investigation-turn cap. "
    "Takes effect on that tenant's next turn; no restart."
)


def _describe(override, is_personal: bool, default_cap: int) -> str:
    """The effective cap, in the same words the enforcement decides it in."""
    if override is None:
        if is_personal:
            return (
                f"no override → {default_cap} turns/day "
                "(the deployment default, because this is a personal tenant)"
            )
        return "no override → uncapped (a company organization)"
    if override == 0:
        return "override 0 → uncapped (explicitly)"
    return f"override {override} → {override} turns/day"


async def set_turn_cap(
    *,
    organization_id: str,
    new_value: object,
    show_only: bool,
    dry_run: bool,
) -> int:
    """Read, and optionally write, one organization's cap. Returns the exit code.

    ``new_value`` is the value to store: an ``int`` (0 for unlimited) or ``None``
    to clear the override. It is ignored when ``show_only``.
    """
    from sqlalchemy import select, update

    from faultmaven.config.settings import get_settings
    from faultmaven.config.tenant_context import set_current_org_id
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import (
        OrganizationModel,
        OrganizationTurnUsageModel,
        SSOPersonalOrgModel,
    )
    from faultmaven.infrastructure.protection.tenant_turn_cap import utc_day

    print("=" * 80)
    print("Tenant Daily Turn Cap")
    print("=" * 80)

    # RLS (migration 018) scopes `organizations` and the usage ledger by
    # `app.current_org_id`. Bind it before opening a transaction so both the
    # read and the UPDATE run under the pod's own application role, exactly as
    # the request path does.
    set_current_org_id(organization_id)

    default_cap = get_settings().agent.tenant_daily_turn_cap
    today = utc_day()

    async with get_db_session() as session:
        row = (
            await session.execute(
                select(OrganizationModel.name, OrganizationModel.daily_turn_cap).where(
                    OrganizationModel.organization_id == organization_id
                )
            )
        ).first()
        if row is None:
            print(
                f"\n❌ No organization '{organization_id}' is visible.\n"
                "   Check the id (it is an id, not a slug); a deleted "
                "organization does not resolve."
            )
            return 1

        is_personal = (
            await session.execute(
                select(SSOPersonalOrgModel.organization_id)
                .where(SSOPersonalOrgModel.organization_id == organization_id)
                .limit(1)
            )
        ).scalar_one_or_none() is not None

        used_today = (
            await session.execute(
                select(OrganizationTurnUsageModel.turn_count).where(
                    OrganizationTurnUsageModel.organization_id == organization_id,
                    OrganizationTurnUsageModel.usage_date == today,
                )
            )
        ).scalar_one_or_none() or 0

        print(f"\nOrganization: {row.name} ({organization_id})")
        print(f"Kind:         {'personal tenant' if is_personal else 'company'}")
        print(
            f"Current cap:  {_describe(row.daily_turn_cap, is_personal, default_cap)}"
        )
        print(f"Used today:   {used_today} turns (UTC day {today.isoformat()})")

        if show_only:
            return 0

        print(f"New cap:      {_describe(new_value, is_personal, default_cap)}")

        if dry_run:
            print("\nDry run — nothing was written. Re-run with --yes to apply.")
            return 0

        result = await session.execute(
            update(OrganizationModel)
            .where(OrganizationModel.organization_id == organization_id)
            .values(daily_turn_cap=new_value)
        )
        if result.rowcount == 0:
            # The row was visible moments ago on the same bound tenant, so this
            # is a concurrent change rather than a scoping mistake. Reporting
            # success would tell an operator a spend control moved when it did
            # not.
            print(
                "\n❌ The organization was readable but the update matched no "
                "row. Nothing was written — re-run and check the id."
            )
            return 1

    print("\n✅ Cap updated. It applies to this tenant's next turn; no restart.")
    return 0


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

    if args.show:
        new_value: object = None
    elif args.unlimited:
        new_value = 0
    elif args.clear:
        new_value = None
    else:
        new_value = args.cap

    if not args.show:
        # --dry-run with --yes is a usage error, not a preference: the two
        # invocations differ by one flag, and silently taking the dry-run branch
        # would exit 0 and read as "the cap moved" when nothing was written.
        if args.dry_run and args.yes:
            parser.error(
                "--dry-run and --yes are mutually exclusive: pass --dry-run to "
                "preview, --yes to write."
            )
        if not args.dry_run and not args.yes:
            print(
                "❌ Refusing to run without --yes. This changes what a tenant is "
                "allowed to spend.\n"
                "   Use --show to read the current cap, or --dry-run to preview."
            )
            sys.exit(1)

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

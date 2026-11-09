"""Give every stored case its first case outcome record (#2615 stage S1).

``progress.outcome`` is written at the end of each decision cycle and at both
terminal transitions (``core/investigation/outcome.py``), so a case stored
before that release has none until it next takes a turn, and a terminal case
never takes one. This command computes the record once for each stored case
without one, **from the case as stored**: a fold over no prior (ruled
2026-11-09 on #2636). Verdicts overwritten in place before the record existed
cannot be recovered by any computation, so none is attempted.

Usage (``fm-backfill-case-outcome``, installed with the package)::

    fm-backfill-case-outcome --dry-run
    fm-backfill-case-outcome --yes
    # Under TENANT_PROVIDER=multi, once per tenant:
    fm-backfill-case-outcome --enterprise-id <enterprise-id> --yes

In a Kubernetes deployment, run it in the API pod::

    kubectl exec -i deploy/faultmaven-api -- fm-backfill-case-outcome --yes

The precondition it serves
--------------------------
There is no legacy read path: no reader computes a record when it meets
``None``. So this runs after the release that writes the record and before any
release that switches a reader onto it (#2636's stage R, or #2615's S2). After
that, a reader meeting ``None`` is a defect.

What it writes
--------------
One save per case through the case repository, which bumps the case's
``version``: a turn that loaded the case before the save and saves after it
misses its optimistic-concurrency check (409) rather than writing the record
back out, and the user re-sends. A case whose save loses that race is listed
as failed; re-run the command for it. A case that already has a record is left
alone, so the command is idempotent: a second run writes nothing.

Under ``TENANT_PROVIDER=multi`` RLS scopes every case read to the enterprise
bound in the tenant context, so ``--enterprise-id`` is required and the command
runs once per tenant, as ``fm-reassign-cases`` does.

A case whose outcome cannot be computed (a derivation raises), or whose save
fails, is counted and listed, and the run goes on to the next case.

Exit codes
----------
| 0 | every case without a record now has one, or a dry run that found no failure |
| 1 | refused (nothing written), or one or more cases failed (the rest written) |
| 2 | argparse usage error (a bad flag), reserved by argparse |
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from faultmaven.cli._confirmation import require_confirmation
from faultmaven.cli._container import run_with_container
from faultmaven.cli._database_gate import require_persistent_database_or_exit

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings.
_SUMMARY = (
    "Compute the case outcome record once for every stored case that has "
    "none, from the case as stored (#2615)."
)


async def backfill_case_outcome(
    *,
    enterprise_id: Optional[str],
    dry_run: bool,
    repository: Any = None,
) -> int:
    """Run the backfill. Returns the process exit code.

    ``repository`` is an ``ICaseRepository`` to read and save through; by
    default the container's, composed here (a test passes its own).
    """
    from faultmaven.cli._tenant import EnterpriseRefused, bind_and_load_enterprise
    from faultmaven.core.investigation.outcome import compute_case_outcome
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    print("=" * 80)
    print("Backfill Case Outcome")
    print("=" * 80)

    if requested_tenant_provider() == BUILTIN_MULTI and not enterprise_id:
        print(
            "\n❌ TENANT_PROVIDER=multi scopes every case read to one enterprise, "
            "so --enterprise-id is required: run this once per tenant. Nothing "
            "was written."
        )
        return 1

    if repository is None:
        from faultmaven.container import container

        print("\nInitializing...")
        await container.initialize()
        repository = container.get_service("case_repository")
    if enterprise_id:
        try:
            enterprise = await bind_and_load_enterprise(enterprise_id)
        except EnterpriseRefused as exc:
            print(f"\n❌ {exc}")
            return 1
        print(f"\nEnterprise:   {enterprise.name} ({enterprise_id})")

    case_ids = await repository.list_all_case_ids()
    already = 0
    computed: list[str] = []
    failed: list[tuple[str, str]] = []
    for case_id in case_ids:
        try:
            case = await repository.get(case_id)
            if case is None:
                continue
            if case.progress.outcome is not None:
                already += 1
                continue
            record = compute_case_outcome(None, case)
            if not dry_run:
                case.progress.outcome = record
                await repository.save(case)
            computed.append(case_id)
        except Exception as exc:  # noqa: BLE001 — one case never stops the run
            failed.append((case_id, f"{type(exc).__name__}: {exc}"))

    verb = "would be written" if dry_run else "written"
    print(f"\nCases:        {len(case_ids)}")
    print(f"Have one:     {already} (left alone)")
    print(f"Records {verb}: {len(computed)}")
    if failed:
        print(f"Failed:       {len(failed)} (not written; re-run once fixed)")
        for case_id, reason in failed:
            print(f"  - {case_id}: {reason}")
    if dry_run:
        print("\nDry run — nothing was written. Re-run with --yes to write.")
    return 1 if failed else 0


def main() -> None:
    """Console entrypoint (``fm-backfill-case-outcome``)."""
    parser = argparse.ArgumentParser(
        prog="fm-backfill-case-outcome",
        description=_SUMMARY,
        epilog=(
            "Runs after the release that writes the record and before any "
            "release that switches a reader onto it. Idempotent: a case that "
            "has a record is left alone."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--enterprise-id",
        default=None,
        help=(
            "Enterprise to backfill (an id, not a slug). Required under "
            "TENANT_PROVIDER=multi, where the run is per tenant"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report what would be written, and write nothing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the write (required)",
    )
    args = parser.parse_args()

    require_confirmation(
        parser,
        args,
        "This writes a case outcome record onto every stored case without one.",
    )

    require_persistent_database_or_exit()

    sys.exit(
        run_with_container(
            backfill_case_outcome(
                enterprise_id=args.enterprise_id,
                dry_run=args.dry_run,
            )
        )
    )

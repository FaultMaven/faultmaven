"""Give every stored case its first case outcome record (#2615 stage S1).

``progress.outcome`` is written at the end of each decision cycle and at both
terminal transitions (``core/investigation/outcome.py``), so a case stored
before that release has none until it next takes a turn, and a terminal case
never takes one. This command computes the record once for each stored case
without one, **from the case as stored**: a fold over no prior (ruled
2026-11-09 on #2636). Verdicts overwritten in place before the record existed
cannot be recovered by any computation, so none is attempted.

It also repairs one record: a terminal case's record that no terminal write
folded (``CaseOutcome.folded_under`` is an open state). That is what a
terminal transition leaves when its record write raised (the write is in
shadow, so the transition went on): the record still holds the open case's
closure, which is not final. It is recomputed the same way, with no prior and
from the case as stored.

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
Only the ``progress`` blob, compare-and-swap on ``version``
(``ICaseRepository.save_progress``): the version is bumped, so a turn that
loaded the case before the write fails its save with a version conflict rather
than writing the old progress back, and ``updated_at`` is kept, so no user's
case list re-sorts. No save-time derivation runs (no offer lapses). A write
that loses the version race is retried from a fresh load
(``update_case_with_retry``), up to three times.

A case with work in flight (a turn, or a task one spawned, holds its lease on
some replica: ``CaseDeleteGate.work_in_flight``, the lease a delete reads) is
skipped and listed, and so is a case whose lease cannot be read; a re-run
picks them up. Under standalone the lease store lives in the API's own process,
which this command cannot see: run it with the API idle there.

A case that has a record is left alone, so the command is idempotent: a second
run writes nothing.

Under ``TENANT_PROVIDER=multi`` RLS scopes every case read to the enterprise
bound in the tenant context, so ``--enterprise-id`` is required and the command
runs once per tenant, as ``fm-reassign-cases`` does.

A case whose outcome cannot be computed (a derivation raises), or whose write
fails three times, is counted and listed, and the run goes on to the next case.

Exit codes
----------
| 0 | every case that needs a record has one, or a dry run that found nothing else |
| 1 | refused (nothing written), or one or more cases failed (the rest written) |
| 2 | argparse usage error (a bad flag), reserved by argparse |
| 3 | one or more cases were skipped for work in flight (the rest written): re-run |
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

#: The exit status of a run that skipped a case for work in flight.
EXIT_SKIPPED = 3


class _Leave(Exception):
    """The case is not written: ``why`` says why, ``skipped`` whether a
    re-run should pick it up, ``would_write`` whether a dry run left it."""

    def __init__(
        self, why: str, *, skipped: bool = False, would_write: bool = False
    ) -> None:
        super().__init__(why)
        self.why, self.skipped, self.would_write = why, skipped, would_write


def needs_record(case: Any) -> bool:
    """Whether the case needs its record computed: it has none, or it is
    terminal and no terminal write folded the record it has."""
    record = case.progress.outcome
    return record is None or (case.is_terminal and not record.folded_under.is_terminal)


async def backfill_case_outcome(
    *,
    enterprise_id: Optional[str],
    dry_run: bool,
    repository: Any = None,
    gate: Any = None,
) -> int:
    """Run the backfill. Returns the process exit code.

    ``repository`` is an ``ICaseRepository`` to read and write through, and
    ``gate`` the ``CaseDeleteGate`` whose lease read skips a case with work in
    flight; by default the container's, composed here (a test passes its own).
    """
    from faultmaven.cli._tenant import EnterpriseRefused, bind_and_load_enterprise
    from faultmaven.core.investigation.outcome import compute_case_outcome
    from faultmaven.modules.case.exceptions import CaseNotFoundError
    from faultmaven.modules.case.utils.retry import update_case_with_retry
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
        gate = getattr(container.get_case_service(), "delete_gate", None)
    if enterprise_id:
        try:
            enterprise = await bind_and_load_enterprise(enterprise_id)
        except EnterpriseRefused as exc:
            print(f"\n❌ {exc}")
            return 1
        print(f"\nEnterprise:   {enterprise.name} ({enterprise_id})")

    async def _apply(case: Any) -> None:
        if gate is not None:
            try:
                busy = await gate.work_in_flight((case.enterprise_id, case.case_id))
            except Exception as exc:  # noqa: BLE001 — unread is refused
                raise _Leave(
                    f"its lease could not be read ({exc})", skipped=True
                ) from exc
            if busy:
                raise _Leave("a turn or its task is in flight", skipped=True)
        if not needs_record(case):
            raise _Leave("it has a record")
        repairing[case.case_id] = case.progress.outcome is not None
        record = compute_case_outcome(None, case)
        if dry_run:
            raise _Leave("dry run", would_write=True)
        case.progress.outcome = record

    case_ids = await repository.list_all_case_ids()
    already = 0
    written: list[str] = []
    repairing: dict[str, bool] = {}
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for case_id in case_ids:
        try:
            await update_case_with_retry(
                repository, case_id, _apply, save=repository.save_progress
            )
            written.append(case_id)
        except _Leave as left:
            if left.skipped:
                skipped.append((case_id, left.why))
            elif left.would_write:
                written.append(case_id)
            else:
                already += 1
        except CaseNotFoundError:
            continue  # deleted since the list was read
        except Exception as exc:  # noqa: BLE001 — one case never stops the run
            failed.append((case_id, f"{type(exc).__name__}: {exc}"))

    repaired = [i for i in written if repairing.get(i)]
    verb = "would be written" if dry_run else "written"
    print(f"\nCases:        {len(case_ids)}")
    print(f"Have one:     {already} (left alone)")
    print(f"Records {verb}: {len(written)} ({len(repaired)} repaired)")
    for case_id in repaired:
        print(f"  - {case_id}: a terminal record no terminal write folded")
    if skipped:
        print(f"Skipped:      {len(skipped)} (work in flight; re-run to write them)")
        for case_id, why in skipped:
            print(f"  - {case_id}: {why}")
    if failed:
        print(f"Failed:       {len(failed)} (not written; re-run once fixed)")
        for case_id, reason in failed:
            print(f"  - {case_id}: {reason}")
    if dry_run:
        print("\nDry run — nothing was written. Re-run with --yes to write.")
    if failed:
        return 1
    return EXIT_SKIPPED if skipped else 0


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

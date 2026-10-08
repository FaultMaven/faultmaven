"""What a turn commits besides the case, and the one call that commits it (#1882).

A turn's writes are one commit: the case (its messages, files, clock and state)
plus the report rows the turn produced and, for a keyed turn, its receipt
(#1888), written by ``ICaseRepository.save(case, reports=..., receipt=...)`` in
one transaction.
Work that may only start once that commit has landed (the runbook conversion)
waits on a gate future in ``on_commit``: released after the commit, cancelled
when the commit fails, so it never runs for a turn that did not commit.

The engine performs no case-scoped write of its own: every site that used to
commit mid-turn (the engine's Step-7 save, the deterministic branches' saves,
the report rows) now adds to the turn's plan, and the
engine returns the plan in its result as ``commit_plan``.
``InvestigationService`` commits it once, with ``commit_turn_plan``, inside the
shielded settlement coroutine that owns it
(``investigation_service.turn_settlement.settle_turn``). An
engine-only test commits the same way, through the same function.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from faultmaven.modules.case.contracts import (
    Case,
    CaseReport,
    ICaseRepository,
    ReportType,
    TurnReceipt,
)

logger = logging.getLogger(__name__)


@dataclass
class TurnCommitPlan:
    """The rows a turn commits with its case, and what waits on that commit."""

    reports: List[CaseReport] = field(default_factory=list)
    on_commit: List["asyncio.Future[Any]"] = field(default_factory=list)
    receipt: Optional[TurnReceipt] = None
    """The keyed turn's receipt (#1888), set by
    ``InvestigationService.commit_turn`` once the response it records exists;
    ``None`` for an unkeyed turn and for every engine-only commit."""

    def add_reports(self, reports: Iterable[CaseReport]) -> None:
        """Carry rendered ``reports`` to the commit, in render order."""
        self.reports.extend(reports)

    def pending_reports(self) -> Dict[ReportType, int]:
        """Per type, the report rows this plan holds uncommitted.

        What ``ReportGenerationService.render_reports(pending=...)`` and every
        ack site's regeneration count (``_remaining_regens_for``) add to the
        committed rows: a summary rendered earlier in this turn is a version
        the next render and the "regenerations left" label must already see.
        """
        counts: Dict[ReportType, int] = {}
        for report in self.reports:
            counts[report.report_type] = counts.get(report.report_type, 0) + 1
        return counts

    def gate(self) -> "asyncio.Future[Any]":
        """A future that resolves once the turn has committed, and is cancelled
        if it does not. Must be called inside a running event loop."""
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.on_commit.append(fut)
        return fut

    def release_gates(self) -> None:
        """Resolve every gate still pending. Safe to call more than once."""
        for fut in self.on_commit:
            if not fut.done():
                fut.set_result(None)

    def cancel_gates(self) -> None:
        """Cancel every gate still pending. Safe to call more than once, and
        after ``release_gates`` (a released gate stays released)."""
        for fut in self.on_commit:
            if not fut.done():
                fut.cancel()


async def commit_turn_plan(
    repository: ICaseRepository, case: Case, plan: TurnCommitPlan
) -> Case:
    """Commit ``case`` with the plan's rows in one transaction, then settle the
    gates: released on success, cancelled on any failure (cancellation
    included), which is then re-raised.

    ‼ Call it only from inside the shielded settlement coroutine that owns the
    plan (#1882 design v2, R1/R2), never directly under a deadline or a
    cancellable request task. It cancels the gates on ANY ``BaseException``,
    and a cancellation can land inside ``db.commit()``, where the commit may
    already have reached the database: run unshielded, a cancel there would
    cancel the gates (and so the work waiting on them) of a turn that did
    commit. Shielded, no outside cancel reaches it, and every exception it
    sees is one the commit really raised.
    """
    try:
        saved = await repository.save(
            case, reports=tuple(plan.reports), receipt=plan.receipt
        )
    except BaseException:
        plan.cancel_gates()
        raise
    plan.release_gates()
    return saved

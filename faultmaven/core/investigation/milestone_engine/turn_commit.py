"""What a turn commits besides the case, and the one call that commits it (#1882).

A turn's writes are one commit: the case (its messages, files, clock and state)
plus the report and checkpoint rows the turn produced, written by
``ICaseRepository.save(case, reports=..., checkpoints=...)`` in one transaction.
Work that may only start once that commit has landed (the runbook conversion)
waits on a gate future in ``on_commit``: released after the commit, cancelled
when the commit fails, so it never runs for a turn that did not commit.

``commit_turn_plan`` is that commit. The engine and the service do not call it
yet (#1882 part 2 moves the turn's saves onto it); the tests do.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List

from faultmaven.modules.case.contracts import (
    Case,
    CaseCheckpoint,
    CaseReport,
    ICaseRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class TurnCommitPlan:
    """The rows a turn commits with its case, and what waits on that commit."""

    reports: List[CaseReport] = field(default_factory=list)
    checkpoints: List[CaseCheckpoint] = field(default_factory=list)
    on_commit: List["asyncio.Future[Any]"] = field(default_factory=list)

    def add_checkpoint(self, checkpoint: CaseCheckpoint) -> bool:
        """Carry ``checkpoint`` to the commit, once.

        A checkpoint id is (case, turn, trigger, target), so a second one under
        the same id is the same snapshot point taken twice in one turn. The
        first, taken before anything moved, is the one kept; the repeat is
        logged and dropped HERE, explicitly, because the repository refuses a
        duplicate id loudly and would fail the turn's whole commit over it.

        Returns:
            Whether the checkpoint was added.
        """
        if any(c.checkpoint_id == checkpoint.checkpoint_id for c in self.checkpoints):
            logger.warning(
                "Case %s: checkpoint %s (turn %s, %s) taken twice in one turn; "
                "the first is kept",
                checkpoint.case_id,
                checkpoint.checkpoint_id,
                checkpoint.turn_number,
                checkpoint.trigger,
            )
            return False
        self.checkpoints.append(checkpoint)
        return True

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
            case, reports=tuple(plan.reports), checkpoints=tuple(plan.checkpoints)
        )
    except BaseException:
        plan.cancel_gates()
        raise
    plan.release_gates()
    return saved

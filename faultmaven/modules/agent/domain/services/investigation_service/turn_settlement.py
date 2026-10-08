"""A prepared turn, and the one coroutine that commits and settles it (#1882).

A turn is split in two at its commit:

- ``InvestigationService.prepare_turn`` does everything that decides the turn
  and commits NOTHING: the case is loaded and mutated in memory, the engine
  runs, the agent's reply row is appended, and the ``TurnResponse`` is built.
  It returns a ``PreparedTurn``. The route bounds it with its ``wait_for``.
- ``InvestigationService.commit_turn`` checks the commit reserve and then runs
  ``settle_turn`` under ``asyncio.shield``. The route does not bound it.

``settle_turn`` is the turn's ONE commit (``commit_turn_plan``: the case, its
messages, files, clock and state, with the plan's report rows, in one
transaction) followed by the steps that must follow a commit and may not
undo it. It OWNS the plan's settlement (design v2, R1): it releases the gates on
success, and on a failed commit it cancels them and emits the #1142 error row.
Nobody else settles a plan once it has started.

So a 2xx means everything the turn wrote is committed, and a non-2xx means none
of it is: the only writes a failed turn leaves behind are the ones whose truth
does not depend on the turn committing (the turn-cap unit, the LLM spend
ledger, an unlinked upload blob the orphan sweep reclaims, redaction mappings,
evidence vectors, and the log line).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from faultmaven.core.investigation.case_telemetry import TurnPath, emit_case_turn
from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
    commit_turn_plan,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.models.api_models import TurnResponse
from faultmaven.modules.agent.domain.services.investigation_service.attachments import (
    _mark_turn_uploads_linked,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_messages import (
    _emit_committed_turn,
)
from faultmaven.modules.case.contracts import Case, ICaseRepository

logger = logging.getLogger(__name__)


@dataclass
class PreparedTurn:
    """A turn whose preparation finished: everything decided, nothing committed."""

    case: Case
    """The case as the turn leaves it, in memory: messages, files, clock and
    state. Committed by ``settle_turn``."""

    plan: TurnCommitPlan
    """The rows that commit with the case, and the gates waiting on it."""

    response: TurnResponse
    """What the route returns once the commit has landed. Built before the
    commit (R4), from the in-memory case, so the commit is the last fallible
    step of the turn."""

    payload: TurnPayload

    preprocess_results: List[Any] = field(default_factory=list)
    """The turn's ``_PreprocessedAttachment`` results: whose blobs are marked
    linked once the turn's rows are committed (#1878)."""

    committed_turn_row: Dict[str, Any] = field(default_factory=dict)
    """Keyword arguments for ``_emit_committed_turn``: the #1748 counters and
    the #1142 row of a committed turn."""

    def emit_error_row(self) -> None:
        """The #1142 row for this turn, as one that did not commit.

        Carries the volume facts off ``payload``: they are known whatever
        failed, and omitting them reports the user as having gone silent on a
        turn they pasted 4 KB into.
        """
        emit_case_turn(
            self.case,
            path=TurnPath.ERROR,
            user_message_chars=len(self.payload.query or ""),
            attachment_count=len(self.payload.attachments or []),
        )


async def settle_turn(
    repository: ICaseRepository, file_storage_service, prepared: PreparedTurn
) -> None:
    """Commit the prepared turn once, then run the steps that follow a commit.

    Run under ``asyncio.shield`` by ``InvestigationService.commit_turn``, so it
    runs to completion even if its caller is cancelled: the commit is never
    abandoned half-way, and the gates of a turn that committed are released.
    Under this stack a client disconnect does
    NOT cancel the request handler (uvicorn + Starlette with
    ``BaseHTTPMiddleware``, probed on #1882), and no ``wait_for`` covers the
    commit, so the shield is belt and braces rather than the mechanism.

    A failure that is NOT the save's own (#1888 A3): a raise from closing the
    session after the repository committed, or, for a keyed turn, a raise
    inside the commit whose receipt then reads back, is resolved inside
    ``SessionlessCaseRepository.save``, which returns normally, so the gates
    are released and the committed telemetry emitted, not the error row. One
    window stays open: a shutdown that cancels the loop inside ``db.commit()``
    (window 1). Nothing can run in a dying loop; a gate whose turn did commit
    is cancelled (e.g. a runbook conversion whose "started" reply committed)
    and is not re-fired on replay. A retry after the restart replays the
    committed reply from its receipt instead of running the turn again.

    Beside it, for a keyed turn: when the HANDLER is cancelled (a client
    disconnect, a shutdown) while this settlement is shielded and still
    committing, the route's ``finally`` releases the turn's in-flight claim
    before the commit has landed. A duplicate arriving in that gap misses the
    receipt and runs the turn; OCC or the receipt's unique key still lets only
    one commit, and the route replays the committed one
    (``turn_idempotency``'s degraded mode). Named, not restructured.

    Post-commit steps, in order, none of which can turn the committed turn into
    an error, and none of which the response waits on beyond its own CPU:

    1. the plan's gates are released (inside ``commit_turn_plan``);
    2. the turn's upload blobs are marked linked — in a BACKGROUND task
       (``_spawn_post_commit``), each call bounded by its own timeout
       (``_mark_turn_uploads_linked``). It is best effort by design (the orphan
       sweep keeps every blob a row references, #1232), so the response does
       not wait for a storage backend: what follows the commit on the response
       path stays bounded by CPU, not I/O;
    3. the #1748 counters and the #1142 turn row.
    """
    try:
        await commit_turn_plan(repository, prepared.case, prepared.plan)
    except BaseException:
        # ``commit_turn_plan`` has cancelled the gates. The turn did not
        # commit, so its row is the error row (#1142).
        prepared.emit_error_row()
        raise

    # (2) The turn is committed, and with it every upload row it carried
    # (#1878): only now may their blobs be marked linked. Best-effort and
    # never fatal (``_mark_turn_uploads_linked`` counts every failure), and run
    # off the response path: a sequential per-blob timeout here would add
    # seconds to a 2xx for a step whose failure the sweep already tolerates.
    if any(result.newly_stored_ref for result in prepared.preprocess_results):
        _spawn_post_commit(
            _mark_turn_uploads_linked(
                file_storage_service, prepared.preprocess_results
            ),
            what=f"mark_linked for case {prepared.case.case_id}",
        )

    # (3) Never raises by contract (both halves catch); guarded anyway, because
    # an exception here would surface as an error for a turn that committed.
    try:
        _emit_committed_turn(**prepared.committed_turn_row)
    except Exception:  # noqa: BLE001 - telemetry must not fail a committed turn
        logger.warning(
            "Turn telemetry failed for committed turn on case %s",
            prepared.case.case_id,
            exc_info=True,
        )


#: Post-commit background work in flight (#1882): the upload links. Held so the
#: event loop's weak reference cannot drop a task before it ran; each removes
#: itself when done.
_POST_COMMIT_TASKS: set["asyncio.Task[None]"] = set()


def _spawn_post_commit(coro, *, what: str) -> None:
    """Run ``coro`` after the turn's commit, off the response path.

    Only ever called once the commit has returned, so it can never act for a
    turn that did not commit. A failure is logged here and never reaches the
    request: the work spawned here is best effort by contract.
    """
    task = asyncio.ensure_future(coro)
    _POST_COMMIT_TASKS.add(task)

    def _done(finished: "asyncio.Task[None]") -> None:
        _POST_COMMIT_TASKS.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.warning(
                "Post-commit step failed (%s): %r", what, finished.exception()
            )

    task.add_done_callback(_done)


#: Settlements in flight. The event loop holds only a weak reference to a task;
#: this keeps a shielded settlement alive until it finishes, including one
#: whose caller was cancelled and no longer awaits it.
_SETTLEMENTS: set["asyncio.Task[None]"] = set()


def _settlement_finished(task: "asyncio.Task[None]") -> None:
    """Drop the finished settlement and retrieve its outcome, so one whose
    caller had gone and that then failed is logged here rather than as
    "Task exception was never retrieved"."""
    _SETTLEMENTS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.info("Turn settlement ended in error: %r", error)


async def run_settlement_shielded(
    repository: ICaseRepository, file_storage_service, prepared: PreparedTurn
) -> None:
    """Run ``settle_turn`` so that cancelling the caller cannot stop it.

    If the caller is cancelled, ``CancelledError`` reaches it unchanged and the
    settlement carries on to its end: the commit lands (or fails) and the plan
    is settled by the settlement itself, never by the caller (R1).
    """
    task = asyncio.ensure_future(
        settle_turn(repository, file_storage_service, prepared)
    )
    _SETTLEMENTS.add(task)
    task.add_done_callback(_settlement_finished)
    await asyncio.shield(task)

"""Bound an await by its deadline, whatever the work does with its cancellation.

``asyncio.wait_for`` cancels the work at its timeout and then waits for the
cancellation to finish, so work whose cleanup awaits I/O outlasts the bound by
as long as that cleanup takes. An asyncpg query cancelled against a server that
has stopped answering is that case: the driver sends a CancelRequest on a new
connection, with no timeout, and every later operation on the connection
(SQLAlchemy's rollback and close among them) awaits it. While the server stays
silent the work never ends, and neither does the ``wait_for`` (#2266).
"""

import asyncio
import logging
from typing import Any, Awaitable, Set, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The work given up on, held until it ends: the event loop keeps only weak
#: references to tasks, and a task nothing references can be collected while
#: it is still pending.
_abandoned: Set["asyncio.Task[Any]"] = set()


async def wait_within(work: Awaitable[T], timeout_s: float, *, what: str) -> T:
    """Await ``work`` for at most ``timeout_s`` seconds; raise ``TimeoutError`` after.

    At the deadline the work is cancelled and left to finish its cancellation on
    its own; the wait does not extend past the bound. Its outcome is retrieved
    when it ends, so nothing is reported as never retrieved. A cancellation of
    the caller cancels the work the same way and propagates at once.
    """
    task = asyncio.ensure_future(work)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except BaseException:
        _abandon(task, what)
        raise
    if done:
        return task.result()
    _abandon(task, what)
    raise TimeoutError(f"{what} did not finish within {timeout_s}s")


def _abandon(task: "asyncio.Task[Any]", what: str) -> None:
    """Cancel ``task`` and stop waiting for it: at the deadline, or because the
    caller was itself cancelled."""
    task.cancel()
    _abandoned.add(task)

    def _settled(ended: "asyncio.Task[Any]") -> None:
        _abandoned.discard(ended)
        if not ended.cancelled() and ended.exception() is not None:
            logger.debug(
                "%s, given up on, ended with %s",
                what,
                type(ended.exception()).__name__,
            )

    task.add_done_callback(_settled)

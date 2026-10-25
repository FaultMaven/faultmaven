"""A run's teardown: steps that cannot stop each other.

Every composer of a run tears it down through :func:`run_teardown`: the API
lifespan's shutdown and composition's own teardown (``faultmaven/bootstrap/``),
the jobs runner (``faultmaven/jobs/run.py``) and the ``fm-*`` commands
(``faultmaven/cli/_container.py``). Each step runs whatever the ones before it
did. One that raises an ``Exception`` is logged. One that is interrupted (a
cancellation, a ``KeyboardInterrupt``, a ``SystemExit``) lets the rest run, and
the first interruption is then re-raised: deferred, never swallowed.
"""

import asyncio
import inspect
import logging
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


async def cancel_and_wait(task: "asyncio.Task[Any]") -> Optional[BaseException]:
    """Cancel ``task`` and wait for it to end; return the exception it ended with.

    Waited for through ``asyncio.wait``, which never raises the task's own
    outcome: neither its cancellation nor its exception is raised here, so a
    ``CancelledError`` that is raised is a cancellation of the caller, and
    propagates. ``await task`` under ``except CancelledError`` cannot tell the
    two apart, and swallows both.

    When the wait completes, the exception is retrieved and returned (``None``
    when the task was cancelled or returned). When the caller is cancelled
    first, the task is no longer awaited, and an exception it ends with later
    may be reported as never retrieved.
    """
    task.cancel()
    await asyncio.wait({task})
    return None if task.cancelled() else task.exception()


async def _teardown_step(what: str, step: Callable[[], Any]) -> Optional[BaseException]:
    """Run one step of a teardown, so that it cannot stop the steps after it.

    ``step`` may return an awaitable, which is awaited. An ``Exception`` is
    logged. Any other ``BaseException`` is returned for the caller to re-raise
    once the whole teardown has run. A ``GeneratorExit`` propagates at once: a
    coroutine being closed may not await.
    """
    try:
        outcome = step()
        if inspect.isawaitable(outcome):
            await outcome
    except GeneratorExit:
        raise
    except Exception as e:
        logger.warning(f"{what} failed (non-critical): {e}")
    except BaseException as interrupted:
        return interrupted
    return None


async def run_teardown(steps: Iterable[tuple[str, Callable[[], Any]]]) -> None:
    """Run every ``(what, step)`` of a teardown, whatever the ones before it did.

    Each step runs through :func:`_teardown_step`. The first interruption
    deferred is re-raised once the last step has run.
    """
    deferred: Optional[BaseException] = None
    for what, step in steps:
        interrupted = await _teardown_step(what, step)
        if deferred is None:
            deferred = interrupted
    if deferred is not None:
        raise deferred

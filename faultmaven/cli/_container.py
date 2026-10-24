"""Run an operator command on the DI container it composes, then shut it down.

Whoever initializes the container shuts it down, on every exit path. A command
that awaits ``container.initialize()`` has opened what the container owns: with
service checks on, a real ``redis.asyncio`` client whose connection pool is
bound to the event loop it was opened on. Nothing else closes it, and a bare
``asyncio.run`` ends that loop over the open transports (#2237).

``run_with_container`` takes the place of ``asyncio.run`` for those commands.
It awaits the command and then, in a ``finally`` on the same loop, awaits
``container.shutdown()``: when the command returns, when it raises, and when
``initialize()`` itself raised after building the client. A failed shutdown is
logged and does not change the command's exit status, as in the jobs runner
(``faultmaven/jobs/run.py``). Shutting down a container that was never
initialized does nothing, so an operation that does not compose may run here
too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_with_container(main: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run(main)``, shutting the container down before the loop ends."""
    return asyncio.run(_run_then_shut_down(main))


async def _run_then_shut_down(main: Coroutine[Any, Any, T]) -> T:
    try:
        return await main
    finally:
        # Resolved now, not at import, like every command's own reference.
        from faultmaven.container import container

        shutdown = container.shutdown
        try:
            await shutdown()
        except Exception as e:
            logger.warning(f"Container shutdown failed (non-critical): {e}")

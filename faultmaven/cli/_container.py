"""Run an operator command on the DI container it composes, then shut it down.

Whoever composes the container for a run shuts it down at the run's end, on
every path. A command that awaits ``container.initialize()`` has opened what the
container owns: with service checks on, a real ``redis.asyncio`` client whose
connection pool is bound to the event loop it was opened on. Nothing else
closes it, and a bare ``asyncio.run`` ends that loop over the open transports
(#2237).

``run_with_container`` takes the place of ``asyncio.run`` for those commands and
for the dev scripts that compose. It awaits the command and then, in a
``finally`` on the same loop, awaits the container's ``shutdown()`` and then
``close_database()``, which disposes the database engine the container's stores
opened on this loop (#2254): when the command returns, when it raises, and when
``initialize()`` itself raised after building the client. Both run as steps of
one teardown (``run_teardown``), so a failed shutdown still lets the engine be
disposed. A failed step is logged and does not change the command's exit
status, as in the jobs runner (``faultmaven/jobs/run.py``). Shutting down a
container that was never initialized, or closing an engine never opened, does
nothing, so an operation that does not compose may run here too.

The container shut down is the one the command composed. A command that binds
``container`` at import passes that reference; otherwise the process singleton
is resolved when the shutdown runs, as the command resolved it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_with_container(main: Coroutine[Any, Any, T], container: Any = None) -> T:
    """``asyncio.run(main)``, shutting ``container`` down and disposing the
    database engine before the loop ends."""
    return asyncio.run(_run_then_shut_down(main, container))


async def _run_then_shut_down(main: Coroutine[Any, Any, T], container: Any) -> T:
    try:
        return await main
    finally:
        if container is None:
            from faultmaven.container import container
        shutdown = container.shutdown
        from faultmaven.bootstrap.composition import run_teardown
        from faultmaven.infrastructure.persistence.database import close_database

        await run_teardown(
            (("Container shutdown", shutdown), ("Database close", close_database))
        )

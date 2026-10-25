"""Run an operator command, then tear down what it opened, on the loop it ran on.

Whoever composes for a run tears it down at the run's end, on every path, while
the loop what it opened is bound to still runs. A bare ``asyncio.run`` ends that
loop over open transports:

- the database engine the command's stores opened (#2254);
- for a command that awaits ``container.initialize()``, what the container
  owns: with service checks on, a real ``redis.asyncio`` client whose
  connection pool is bound to that loop (#2237).

``run_command`` takes the place of ``asyncio.run`` for a command that composes
no container (``fm-provision-sso-org``, ``fm-set-turn-cap``,
``fm-wipe-deployment``): it disposes the engine. ``run_with_container`` takes
its place for the commands and dev scripts that compose: it shuts the container
down, then disposes the engine. Each awaits the command and then, in a
``finally`` on the same loop, runs those steps through ``run_teardown``: when
the command returns, when it raises, and when ``initialize()`` itself raised
after building the client. A failed step is logged, the steps after it still
run, and the command's exit status is unchanged, as in the jobs runner
(``faultmaven/jobs/run.py``). Disposing an engine never opened, or shutting
down a container never initialized, does nothing.

The container shut down is the one the command composed. A command that binds
``container`` at import passes that reference; otherwise the process singleton
is resolved when the shutdown runs, as the command resolved it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, TypeVar

from faultmaven.infrastructure.concurrency.teardown import run_teardown

T = TypeVar("T")


def run_command(main: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run(main)``, disposing the database engine before the loop ends."""
    return asyncio.run(_run_then_tear_down(main, composed=False, container=None))


def run_with_container(main: Coroutine[Any, Any, T], container: Any = None) -> T:
    """:func:`run_command`, shutting ``container`` down before the engine goes."""
    return asyncio.run(_run_then_tear_down(main, composed=True, container=container))


async def _run_then_tear_down(
    main: Coroutine[Any, Any, T], *, composed: bool, container: Any
) -> T:
    try:
        return await main
    finally:
        # Each step imports what it needs, so a failed import is that step's
        # failure alone (#2267).
        steps: list[tuple[str, Callable[[], Any]]] = []
        if composed:
            if container is None:
                steps.append(("Container shutdown", _shut_down_the_container))
            else:
                # Resolved outside the steps: a container without a shutdown is
                # a defect that surfaces, not a failed step that is logged.
                steps.append(("Container shutdown", container.shutdown))
        steps.append(("Database close", _close_the_database))
        await run_teardown(steps)


async def _shut_down_the_container() -> None:
    from faultmaven.container import container

    await container.shutdown()


async def _close_the_database() -> None:
    from faultmaven.infrastructure.persistence.database import close_database

    await close_database()

"""``run_teardown`` runs every step of a teardown, whatever the ones before it did (#2257).

The one contained-step runner: composition's teardown, the lifespan's shutdown,
and the ends of the jobs runner and the ``fm-*`` commands run their steps
through it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from faultmaven.bootstrap.composition import run_teardown

pytestmark = pytest.mark.unit


async def test_every_step_runs_and_the_first_cancellation_propagates_after_the_last(
    caplog,
):
    ran = []
    first, second = asyncio.CancelledError(), asyncio.CancelledError()

    def _fails():
        ran.append("fails")
        raise ValueError("the step blew up")

    async def _cancelled(name, cancellation):
        ran.append(name)
        raise cancellation

    async def _last():
        ran.append("last")

    with caplog.at_level(logging.WARNING, logger="faultmaven.bootstrap.composition"):
        with pytest.raises(asyncio.CancelledError) as raised:
            await run_teardown(
                (
                    ("A failing step", _fails),
                    ("A cancelled step", lambda: _cancelled("cancelled", first)),
                    ("Another", lambda: _cancelled("cancelled again", second)),
                    ("The last step", _last),
                )
            )

    assert ran == ["fails", "cancelled", "cancelled again", "last"]
    assert raised.value is first, "not the first cancellation"
    assert "A failing step failed (non-critical): the step blew up" in caplog.text

"""Virtual time for asyncio tests whose subject IS a deadline (#1579).

Some tests are not about how fast anything is. They ask an ORDERING
question — did the retry ladder give up before the turn-wide ``wait_for``
fired? did an open circuit breaker answer without waiting? — and a wall
clock answers that only on an idle machine. Under ``pytest-xdist`` the
event loop shares its CPU with three other workers, a starved loop runs its
timers late, and ``tests/integration/core/test_llm_ladder_turn_budget.py``
measured ``2.98 < 2.0`` on a required gate while its classification — the
thing the test is about — was correct. No threshold is right for that: the
number is the scheduler's, not the code's.

``VirtualTimeLoop`` is an ordinary selector event loop whose clock stands
still while there is work to do and jumps to the next timer when there is
none. Every ``asyncio.sleep``, ``wait_for`` and ``call_later`` then costs
exactly its nominal duration, CPU costs nothing, and every elapsed figure is
the same on every machine under any load — so a test can compare it against
a deadline and mean it.

Using it
--------

Override ``event_loop_policy`` (the pytest-asyncio fixture) in the module,
and point every production module that READS the monotonic clock on the
path under test at ``virtual_time_module()``:

.. code-block:: python

    @pytest.fixture
    def event_loop_policy():
        return VirtualTimePolicy()

    @pytest.fixture(autouse=True)
    def _virtual_clock(monkeypatch):
        monkeypatch.setattr(turn_budget, "time", virtual_time_module())

Read elapsed time with ``virtual_now()``. It refuses to run on any other
loop, which is what makes it safe for the guard in
``tests/unit/ci/test_benchmark_calibration.py`` to treat it as NOT a wall
clock: a test cannot reach real time through it by accident.

What it does not model
----------------------

Work on another thread. A ``to_thread`` or executor job does not hold the
virtual clock back, so a timer pending beside one fires as soon as the loop
is idle. The tests that use this drive coroutines, sleeps and timeouts only.
"""

from __future__ import annotations

import asyncio
import time
import types


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """A selector loop whose ``time()`` advances only when it has nothing to do."""

    def __init__(self, selector=None) -> None:
        super().__init__(selector)
        self._virtual_now = 0.0

    def time(self) -> float:
        return self._virtual_now

    def _run_once(self) -> None:
        # Idle with timers pending: jump to the earliest LIVE one. Cancelled
        # handles stay in the heap until the base loop pops them, and jumping
        # to one — a ``wait_for`` timeout whose inner call already finished —
        # would bill the test for a deadline that never fired.
        if not self._ready and not self._stopping:
            pending = [h.when() for h in self._scheduled if not h.cancelled()]
            if pending:
                self._virtual_now = max(self._virtual_now, min(pending))
        super()._run_once()


class VirtualTimePolicy(asyncio.DefaultEventLoopPolicy):
    """Hands pytest-asyncio a ``VirtualTimeLoop`` for every test."""

    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        return VirtualTimeLoop()


def virtual_now() -> float:
    """The running loop's virtual time, in seconds since the loop started.

    ‼ Raises outside a ``VirtualTimeLoop``. Read on an ordinary loop, the
    same number would be the wall clock under a name that says otherwise.
    """
    loop = asyncio.get_running_loop()
    if not isinstance(loop, VirtualTimeLoop):
        raise RuntimeError(
            "virtual_now() needs a VirtualTimeLoop; this test is running on "
            f"{type(loop).__name__}. Override the module's event_loop_policy "
            "fixture with VirtualTimePolicy()."
        )
    return loop.time()


def virtual_time_module() -> types.SimpleNamespace:
    """A stand-in for the ``time`` module whose ``monotonic()`` is virtual.

    Patched over a production module's ``time`` attribute, it moves that
    module's deadline arithmetic onto the same clock ``asyncio`` schedules
    by. Everything else is the real ``time`` module, so a module that also
    logs a wall-clock duration keeps doing so.
    """
    shim = types.SimpleNamespace(
        **{name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    )
    shim.monotonic = virtual_now
    return shim

"""Timed windows for ``tests/performance/``: no statistic is one window (#1916).

Why
---

Both required gates (Test Standalone, Test Cloud) run ``tests/performance/``
under ``pytest-xdist`` beside the whole suite, so a runner can be preempted
for a second in the middle of any timed region. One timed window cannot tell
that stall from a regression. #1916 is the case: one ``asyncio.gather`` of 20
logged requests took 1.2 s instead of ~16 ms, and the per-request figure
judged against ``CONCURRENT_REQUEST`` was that one window divided by 20.

So every statistic a pull request judges here is taken over several
INDEPENDENT windows, and a stall lands in one of them:

* ``median_of_windows`` / ``median_of_windows_async`` — the median of N
  windows. One stalled window cannot move it; a regression moves every window
  and so moves the median.
* ``fastest_of_windows`` — the fastest of N, for the rows whose statistic was
  already a minimum (every source of noise is additive, so two windows
  already survive one stall).

The one other statistic in the directory, ``TURN_COMMIT``'s p50 of 180
commits, is an order statistic over many samples and is robust for the same
reason (``tests/turn_commit_latency.py``).

The clock is handed in
----------------------

``measure_once`` receives a ``Window`` and reads the clock only through it.
That is what makes the rule checkable: a test module under
``tests/performance/`` never calls a clock itself, so a timed region outside
a window is visible to the call-graph guard in
``tests/unit/ci/test_benchmark_calibration.py``
(``TestEveryStatisticIsWindowed``), and a ``Window`` exists only inside one of
the aggregators below, which refuse fewer than ``MIN_WINDOWS`` windows.

A window builds its own state
-----------------------------

Each call of ``measure_once`` is one window, and it must build whatever state
it measures. A window that inherits the previous window's state can measure a
different path: ``LoggingCoordinator.log_once``'s first call per key logs and
every later one is a dedup hit, so a shared coordinator would make windows
2..N measure only the duplicate path.
"""

from __future__ import annotations

import statistics
import time
from typing import Awaitable, Callable, List

#: The fewest windows a median may be taken over: the smallest count whose
#: median survives one stalled window.
MIN_WINDOWS = 3

#: The fewest windows a minimum may be taken over. A stall only ever adds
#: time, so the fastest of two survives one stalled window.
MIN_FASTEST_WINDOWS = 2


class Window:
    """The clock of one timed window, handed to ``measure_once``.

    The only clock a module under ``tests/performance/`` reads. Its two
    methods time one call of ``work`` and return the seconds it took; a
    window may time several regions (a logged loop and the same loop
    un-logged, for an overhead) and combine them into its one figure.
    """

    __slots__ = ()

    def seconds(self, work: Callable[[], object]) -> float:
        """Seconds one synchronous call of ``work`` took."""
        started = time.perf_counter()
        work()
        return time.perf_counter() - started

    async def seconds_async(self, work: Callable[[], Awaitable[object]]) -> float:
        """Seconds one awaited call of ``work`` took."""
        started = time.perf_counter()
        await work()
        return time.perf_counter() - started


def _check_windows(windows: int, fewest: int, statistic: str) -> None:
    if windows < fewest:
        raise ValueError(
            f"{windows} window(s): a {statistic} over fewer than {fewest} windows "
            "cannot tell one stalled window from a regression (#1916)"
        )


def _run(
    measure_once: Callable[[Window], float], windows: int, fewest: int, statistic: str
) -> List[float]:
    _check_windows(windows, fewest, statistic)
    window = Window()
    return [float(measure_once(window)) for _ in range(windows)]


def median_of_windows(
    measure_once: Callable[[Window], float], windows: int = MIN_WINDOWS
) -> float:
    """The median of ``windows`` independent calls of ``measure_once``."""
    return statistics.median(_run(measure_once, windows, MIN_WINDOWS, "median"))


async def median_of_windows_async(
    measure_once: Callable[[Window], Awaitable[float]], windows: int = MIN_WINDOWS
) -> float:
    """``median_of_windows`` for a window that awaits (a gather, a sleep)."""
    _check_windows(windows, MIN_WINDOWS, "median")
    window = Window()
    return statistics.median(
        [float(await measure_once(window)) for _ in range(windows)]
    )


def fastest_of_windows(
    measure_once: Callable[[Window], float], windows: int = MIN_WINDOWS
) -> float:
    """The smallest of ``windows`` independent calls of ``measure_once``."""
    return min(_run(measure_once, windows, MIN_FASTEST_WINDOWS, "minimum"))

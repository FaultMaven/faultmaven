"""Event-loop lag: how late the API's loop wakes from a tick (#2056, #2066).

Nothing else measures it. A call that holds the loop (a blocking HTTP round
trip, a CPU-bound parse) stalls every request on the pod at once, and no
request-level metric shows the stall as such. :class:`LoopLagMonitor` sleeps a
fixed tick and records how much later than that it actually woke, as one
histogram on ``/metrics``.

Reading it: a value is the lateness of one wake-up. A loop held for H seconds
is recorded as at least H minus one tick interval (the part of the tick that
had already elapsed when the hold began), so a hold is under-read by at most
``LOOP_LAG_TICK_SECONDS``. ``le="10"`` is the bucket #2066 reads: ticks above
that bucket woke more than 10 s late. The series is per pod; do not ``sum`` the
buckets across pods to read one pod's hold.

The tick makes no I/O, and nothing on the request path or the scrape path reads
or writes the histogram. No health decision reads it either.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from faultmaven.infrastructure.shims.metrics import Histogram

logger = logging.getLogger(__name__)

#: Seconds between ticks.
LOOP_LAG_TICK_SECONDS = 0.25

#: How late the loop woke from each tick.
event_loop_lag_seconds = Histogram(
    "faultmaven_event_loop_lag_seconds",
    "How late the event loop woke from a tick, in seconds. A hold of H seconds "
    'is under-read by at most one tick interval (0.25 s). le="10" is the '
    "bucket the liveness decision (#2066) reads. Per pod: do not sum the "
    "buckets across pods to read a single pod's hold.",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120],
)


class LoopLagMonitor:
    """Records the event loop's wake-up lateness on a fixed tick."""

    async def run_periodic(
        self,
        interval: float = LOOP_LAG_TICK_SECONDS,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Tick forever: sleep ``interval``, observe how late the wake was.

        ``clock`` defaults to the running loop's ``time``. A failing ``observe``
        is logged once, by class name, and the loop continues.
        """
        if clock is None:
            clock = asyncio.get_running_loop().time
        failure_logged = False
        while True:
            expected = clock() + interval
            await sleep(interval)
            try:
                event_loop_lag_seconds.observe(max(0.0, clock() - expected))
            except Exception as exc:
                if not failure_logged:
                    failure_logged = True
                    logger.warning(
                        "Event-loop lag observation failed (non-fatal): %s",
                        type(exc).__name__,
                    )


monitor = LoopLagMonitor()

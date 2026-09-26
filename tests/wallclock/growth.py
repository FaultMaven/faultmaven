"""Is a cost linear in its input, or worse? — the growth-shape check (#1579).

A ReDoS or accidentally-quadratic guard asks a SHAPE question: does
multiplying the input multiply the cost by the same factor, or by its
square? An absolute budget answers a different one — "is this fast on this
machine right now" — and under ``pytest-xdist`` that answer depends on what
three other workers are doing.

Why not a ratio of two sizes
----------------------------

The first version of this module compared ``t(16n) / t(n)`` against 64x,
and its own quadratic control went green on the CI runners while reading
121-124x here. A two-size ratio carries the function's FIXED per-call cost
``C`` on both sides::

    t(16n) / t(n)  =  (C + 256q) / (C + q)

which lands anywhere between 1 and 256 depending on ``C/q`` — and ``C/q``
is a property of the machine. Nothing checked that ``C`` was small, so the
bound meant something different on hardware nobody here can see.

What this does instead
----------------------

**Three sizes, and the ratio of the DIFFERENCES.** ``n``, ``8n``, ``64n``::

    (t(64n) - t(8n)) / (t(8n) - t(n))

``C`` cancels exactly. A linear cost reads 8, a quadratic one 64, and the
bound is their geometric midpoint, ``8 ** 1.5`` ~ 22.6. Step 8 rather than
4 on purpose — at step 4 a pure quadratic reads 16 against a bound of 8, so
a runner that read it at half would sit on the bound; at step 8 half is 32.

**Noise is discounted, not ignored.** The difference cancels ``C`` but not
noise, and when the work at ``8n`` is small beside ``C``, ``t(8n) - t(n)``
is a small difference of two large numbers. Measured on fixed code: the
sshd reader's ``slot-lookalikes`` shape read 23.6 — over the bound — with
``t(8n)`` only 12% above ``t(n)``. So the verdict is taken on the reading
that survives every per-size minimum being off by ``NOISE_ALLOWANCE`` of
itself in the direction that favours linear::

    (D2 - e*(t2 + t3)) / (D1 + e*(t1 + t2))

That shape read 6.6. A restored quadratic loses a fifth of its reading
(~60 to ~50) and still clears the bound twice over.

**The window finds the work.** A check whose payload is too small for the
work to show beside ``C`` would pass anything — the hollow guard this
replaces became on CI. So unless the largest size costs at least
``MIN_TOTAL_GROWTH`` times the smallest, the whole window moves up by
``step`` and is measured again, at most ``MAX_ESCALATIONS`` times, and a
window that still shows nothing is refused as miscalibrated rather than
passed. A runner whose fixed cost is larger relative to the work simply
escalates once more; a quadratic shows its growth in the first window and
fails there, at the smallest sizes, fast.

The rest is the measurement, each choice against a measured failure:

* **Thread CPU time, not wall clock.** ``time.thread_time`` does not
  advance while this thread is descheduled, which is most of what xdist
  adds.
* **Interleaved rounds, minimum per size.** Every source of noise here is
  additive, so the fastest sample is the least contaminated, and taking
  the three sizes in each round keeps a burst of contention from landing
  on one of them only.
* **A call count per size.** Each size repeats its call until one sample
  costs ``MIN_SAMPLE_SECONDS``, the per-call figure is what is compared,
  and the sample that settled the count is the first observation — so the
  large side of a restored quadratic runs once per round, not twice.
* **Stop at the unmistakable.** A round whose discounted reading is
  already twice the bound ends the measurement.

What it does NOT do, stated so nobody reads it in: it says nothing about
absolute cost. A linear path that got 50x slower passes. That is a latency
budget's question, and latency budgets live in ``tests/performance/`` and
``tests/benchmarks/`` behind the calibrated helpers in ``assertions.py``.

Scope: synchronous, CPU-bound work done on the calling thread — a regex, a
parser, a scorer. Thread CPU time does not see work handed to another
thread, so a function that does that would read as free.
"""

from __future__ import annotations

import gc
import time
from dataclasses import dataclass
from typing import Callable, List, Tuple, TypeVar

P = TypeVar("P")

#: The ratio between consecutive sizes: ``n``, ``step * n``, ``step**2 * n``.
DEFAULT_STEP = 8

#: Interleaved rounds per window. Five kept the minima stable on a box
#: running four other pytest workers.
DEFAULT_REPETITIONS = 5

#: Each size repeats its call until one sample costs at least this much CPU,
#: so no size is measuring the timer.
MIN_SAMPLE_SECONDS = 0.002

#: Upper limit on that repetition, so a function that does nothing at all
#: cannot spin the check forever.
MAX_CALLS_PER_SAMPLE = 1 << 16

#: How far each per-size minimum is assumed to be off, as a fraction of
#: itself, when the verdict is taken. Measured spread of the minima on a
#: loaded box is a few percent; ten is the margin.
NOISE_ALLOWANCE = 0.10

#: The largest size must cost at least this many times the smallest for the
#: window to count as showing the work; otherwise it moves up by ``step``.
MIN_TOTAL_GROWTH = 4.0

#: How many times the window may move up before the call is refused.
MAX_ESCALATIONS = 3


def growth_bound(step: int) -> float:
    """The largest difference ratio a linear cost may show at ``step``.

    The geometric midpoint of linear (``step``) and quadratic
    (``step ** 2``), so the margin is the same multiple on both sides.
    """
    return float(step) ** 1.5


@dataclass(frozen=True)
class Growth:
    """Per-call CPU seconds at three sizes, and what they say."""

    sizes: Tuple[int, int, int]
    seconds: Tuple[float, float, float]
    step: int

    @property
    def total_growth(self) -> float:
        """``t3 / t1``: how much the work shows beside the fixed cost."""
        return self.seconds[2] / max(self.seconds[0], 1e-12)

    @property
    def ratio(self) -> float:
        """The raw difference ratio ``(t3 - t2) / (t2 - t1)``."""
        t1, t2, t3 = self.seconds
        lower = t2 - t1
        return float("inf") if lower <= 0 else (t3 - t2) / lower

    @property
    def evidence(self) -> float:
        """The difference ratio after ``NOISE_ALLOWANCE`` is spent against it.

        Every minimum is moved by ``NOISE_ALLOWANCE`` of itself in the
        direction that makes the cost look MORE linear, so noise alone
        cannot produce a super-linear verdict.
        """
        t1, t2, t3 = self.seconds
        upper = (t3 - t2) - NOISE_ALLOWANCE * (t2 + t3)
        lower = (t2 - t1) + NOISE_ALLOWANCE * (t1 + t2)
        return upper / lower

    def describe(self) -> str:
        return ", ".join(
            f"{size}: {seconds * 1e3:.4f} ms"
            for size, seconds in zip(self.sizes, self.seconds)
        )


def _cpu_seconds(fn: Callable[[P], object], payload: P, calls: int) -> float:
    """Thread CPU seconds for ``calls`` calls, with the collector paused.

    A collection that happens to land inside one sample is CPU time spent on
    something other than ``fn`` — the minimum would absorb it, but pausing
    costs nothing and removes the question.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        started = time.thread_time()
        for _ in range(calls):
            fn(payload)
        return time.thread_time() - started
    finally:
        if was_enabled:
            gc.enable()


def _calibrate(fn: Callable[[P], object], payload: P) -> Tuple[int, float]:
    """The call count for ``payload``, and the per-call time that settled it.

    The sample that reached ``MIN_SAMPLE_SECONDS`` is a real measurement and
    is returned as the first observation, so the large side of a restored
    quadratic — one call already costing seconds — is not run twice.
    """
    calls = 1
    while True:
        sample = _cpu_seconds(fn, payload, calls)
        if sample >= MIN_SAMPLE_SECONDS or calls >= MAX_CALLS_PER_SAMPLE:
            return calls, sample / calls
        calls *= 2


def _measure_window(
    fn: Callable[[P], object],
    payload_at: Callable[[int], P],
    small: int,
    step: int,
    repetitions: int,
) -> Growth:
    sizes = (small, small * step, small * step * step)
    payloads = [payload_at(size) for size in sizes]

    fn(payloads[0])  # warm-up: first-call costs belong to no size
    calls: List[int] = []
    best: List[float] = []
    for payload in payloads:
        count, first = _calibrate(fn, payload)
        calls.append(count)
        best.append(first)

    growth = Growth(sizes=sizes, seconds=tuple(best), step=step)
    for round_number in range(repetitions):
        if round_number:
            for index, payload in enumerate(payloads):
                per_call = _cpu_seconds(fn, payload, calls[index]) / calls[index]
                best[index] = min(best[index], per_call)
            growth = Growth(sizes=sizes, seconds=tuple(best), step=step)
        if (
            growth.total_growth >= MIN_TOTAL_GROWTH
            and growth.evidence >= 2 * growth_bound(step)
        ):
            break
    return growth


def measure_growth(
    fn: Callable[[P], object],
    payload_at: Callable[[int], P],
    *,
    small: int,
    step: int = DEFAULT_STEP,
    repetitions: int = DEFAULT_REPETITIONS,
    max_escalations: int = MAX_ESCALATIONS,
) -> Growth:
    """The first window from ``small`` upwards in which the work shows.

    Each window is ``small``, ``step*small``, ``step**2*small`` times
    ``step ** k`` for the smallest ``k <= max_escalations`` whose largest
    size costs ``MIN_TOTAL_GROWTH`` times its smallest. If none does, the
    last window is returned and the caller refuses it.
    """
    if step < 2:
        raise ValueError(f"step must be at least 2, got {step}")
    if small < 1 or repetitions < 1 or max_escalations < 0:
        raise ValueError("small and repetitions must be positive")
    growth = None
    for escalation in range(max_escalations + 1):
        growth = _measure_window(
            fn, payload_at, small * step**escalation, step, repetitions
        )
        if growth.total_growth >= MIN_TOTAL_GROWTH:
            break
    return growth


def assert_linear_growth(
    fn: Callable[[P], object],
    payload_at: Callable[[int], P],
    *,
    small: int,
    label: str,
    step: int = DEFAULT_STEP,
    repetitions: int = DEFAULT_REPETITIONS,
    max_escalations: int = MAX_ESCALATIONS,
) -> Growth:
    """Fail if ``fn``'s cost grows faster than linearly in its input.

    ‼ The single site where a growth reading meets its bound, in the same
    sense that ``assertions.py`` is for latencies:
    ``tests/unit/ci/test_benchmark_calibration.py`` fails the build on a
    test that measures a duration and judges it anywhere else.

    Fails in one of two ways, and says which: no window up to
    ``step ** max_escalations`` times ``small`` showed the work at all (the
    call is miscalibrated — raise ``small``), or the growth is
    super-linear.

    Args:
        fn: The synchronous, CPU-bound function under test.
        payload_at: Builds the input at a given size, in whatever unit the
            caller counts (repetitions of a hostile unit, kilobytes, lines).
        small: The smallest size tried; the window is it, ``step`` and
            ``step**2`` times it, and may move up (see ``measure_growth``).
        label: What was measured, for the failure message.

    Returns:
        The measurement, so a caller can print it.
    """
    growth = measure_growth(
        fn,
        payload_at,
        small=small,
        step=step,
        repetitions=repetitions,
        max_escalations=max_escalations,
    )
    assert growth.total_growth >= MIN_TOTAL_GROWTH, (
        f"{label}: payload too small to show its growth — even at "
        f"{growth.sizes[2]} the cost is only {growth.total_growth:.2f}x the "
        f"cost at {growth.sizes[0]}, below {MIN_TOTAL_GROWTH}, so the fixed "
        f"per-call cost hides whatever grows ({growth.describe()}). Raise "
        "`small`."
    )
    bound = growth_bound(step)
    assert growth.evidence < bound, (
        f"{label}: the cost added from {growth.sizes[1]} to {growth.sizes[2]} "
        f"is {growth.evidence:.1f}x the cost added from {growth.sizes[0]} to "
        f"{growth.sizes[1]} (raw {growth.ratio:.1f}x, after allowing "
        f"{NOISE_ALLOWANCE:.0%} noise per size). Linear reads ~{step}x and "
        f"quadratic ~{step ** 2}x; the bound is the midpoint, {bound:.1f}x — "
        "a backtracking pattern or a rescan per element is the usual cause "
        f"({growth.describe()})"
    )
    return growth

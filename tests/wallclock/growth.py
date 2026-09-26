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
is a property of the machine.

The reading: three sizes and the ratio of the DIFFERENCES
---------------------------------------------------------

``n``, ``8n``, ``64n``, and ``R = (t(64n) - t(8n)) / (t(8n) - t(n))``. ``C``
cancels exactly, so the TRUE ``R`` of a cost does not depend on it: a linear
cost has ``R = 8`` and a quadratic one ``R = 64`` whatever its fixed cost. The
bound is their geometric midpoint, ``8 ** 1.5`` ~ 22.6. Step 8 rather than 4
so a quadratic read at half still clears it.

The verdict: an interval, and a third answer
--------------------------------------------

``R`` cancels ``C`` but noise does not, and when ``C`` dominates,
``t(8n) - t(n)`` is a small difference of two large numbers. The second
version of this module spent a noise allowance against one side only and
passed whatever that could not call super-linear — so a pure quadratic with a
moderate fixed cost passed (``fixed + n**2`` loop, ``C/q`` from ~430 to
~1360: 3/3 green). The rule is now two-sided. With every per-size minimum
allowed to be off by ``NOISE_ALLOWANCE`` of itself, ``[ratio_floor,
ratio_ceiling]`` is every ``R`` consistent with the measurement, and:

* **super-linear** only if ``ratio_floor >= bound`` — even the most linear
  reading of the numbers is over it;
* **linear** only if ``ratio_ceiling < bound`` — even the most quadratic
  reading is under it;
* otherwise **undecided**. So is any window whose largest size costs under
  ``MIN_TOTAL_GROWTH`` times its smallest: the work does not show there, and
  deciding either way on it is deciding on noise.

An undecided window moves up by ``step`` — the work grows, ``C`` does not —
at most ``MAX_ESCALATIONS`` times, and a call still undecided is REFUSED as
miscalibrated. It is never passed.

The invariant
-------------

If every per-size minimum ``m`` is within ``NOISE_ALLOWANCE * m`` of the true
per-call cost, the true ``R`` lies in ``[ratio_floor, ratio_ceiling]``. So a
cost whose true ``R`` is under the bound can never be failed, and one at or
over it can never be passed — **for every fixed cost C**, because ``C`` is
not in ``R``. ``C`` only decides whether a window can decide. In particular:

* ``C + a*n`` (``R = 8``) never fails. It passes once ``C`` is under ~16x
  the work at the smallest size; three escalations reach that from ~8000x,
  and above that it is refused;
* ``C + q*n**2`` (``R = 64``) never passes. It fails once ``C`` is under
  ~430x the work at the smallest size — one escalation divides that by 64;
* ``C + a*n + q*n**2`` has ``R = (56a + 4032q) / (7a + 63q)``, increasing
  in ``q/a``: it crosses the bound where the quadratic term at the window's
  LARGEST size is ~2.5x the linear term. With no fixed cost the window says
  linear while that share is at most ~1.5x and super-linear from ~4x; in
  between it escalates, which multiplies ``q/a`` by 8, so a window only ever
  moves a mixture toward failing. The check answers for the sizes it
  measured; a call site picks sizes that reach the payloads it guards.

``tests/unit/ci/test_benchmark_calibration.py`` pins all of this over fixed
costs from 0 to 1e7 times the work, and outside the model it searches for an
adversarial per-size error: a wrong verdict needs one over 13% (a quadratic
passed) or over 18% (a linear failed). The measured spread of the minima
here is a few percent.

A cost exactly at the bound (``n ** 1.5``) is undecidable by construction and
is refused, which is the honest answer.

The measurement, each choice against a measured failure
-------------------------------------------------------

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
* **Stop at the unmistakable.** A round whose ``ratio_floor`` is already
  twice the bound ends the measurement.

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
import math
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

#: How far each per-size minimum may be off, as a fraction of itself. The
#: measured spread of the minima on a loaded box is a few percent.
NOISE_ALLOWANCE = 0.10

#: A window whose largest size costs less than this many times its smallest
#: decides nothing. A linear window can only pass above ~4.75 anyway, so this
#: never delays a correct pass; what it removes is a verdict taken where the
#: fixed cost swamps the work, which is where error beyond the allowance
#: could flip one (the sweep in ``test_benchmark_calibration.py``).
MIN_TOTAL_GROWTH = 4.0

#: How many times an undecided window may move up before the call is refused.
MAX_ESCALATIONS = 3

SUPER_LINEAR = "super-linear"
LINEAR = "linear"
UNDECIDED = "undecided"


def growth_bound(step: int) -> float:
    """The difference ratio that separates linear from super-linear at ``step``.

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
        """The difference ratio as measured, ``(t3 - t2) / (t2 - t1)``."""
        t1, t2, t3 = self.seconds
        lower = t2 - t1
        return math.inf if lower <= 0 else (t3 - t2) / lower

    @property
    def ratio_floor(self) -> float:
        """The least true ratio consistent with the minima.

        Numerator as small and denominator as large as ``NOISE_ALLOWANCE``
        permits. ``-inf`` when the minima are consistent with ``t(8n)`` not
        exceeding ``t(n)``, where no ratio is defined.
        """
        t1, t2, t3 = self.seconds
        upper = (t3 - t2) - NOISE_ALLOWANCE * (t2 + t3)
        lower = (t2 - t1) + NOISE_ALLOWANCE * (t1 + t2)
        return upper / lower if lower > 0 else -math.inf

    @property
    def ratio_ceiling(self) -> float:
        """The greatest true ratio consistent with the minima.

        ``inf`` when the noise could make ``t(8n) - t(n)`` zero: nothing then
        bounds the ratio from above.
        """
        t1, t2, t3 = self.seconds
        upper = (t3 - t2) + NOISE_ALLOWANCE * (t2 + t3)
        lower = (t2 - t1) - NOISE_ALLOWANCE * (t1 + t2)
        return upper / lower if lower > 0 else math.inf

    @property
    def verdict(self) -> str:
        """``SUPER_LINEAR``, ``LINEAR``, or ``UNDECIDED`` — see the module."""
        if self.total_growth < MIN_TOTAL_GROWTH:
            return UNDECIDED
        bound = growth_bound(self.step)
        if self.ratio_floor >= bound:
            return SUPER_LINEAR
        if self.ratio_ceiling < bound:
            return LINEAR
        return UNDECIDED

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
        if growth.verdict == SUPER_LINEAR and growth.ratio_floor >= 2 * growth_bound(
            step
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
    """The first window from ``small`` upwards that decides.

    Each window is ``small``, ``step*small``, ``step**2*small`` times
    ``step ** k`` for the smallest ``k <= max_escalations`` whose
    ``verdict`` is not ``UNDECIDED``. If none decides, the last window is
    returned and ``assert_linear_growth`` refuses it.
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
        if growth.verdict != UNDECIDED:
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
    """Fail unless ``fn``'s cost is shown to grow linearly in its input.

    ‼ The single site where a growth reading meets its bound, in the same
    sense that ``assertions.py`` is for latencies:
    ``tests/unit/ci/test_benchmark_calibration.py`` fails the build on a
    test that measures a duration and judges it anywhere else.

    Fails in one of two ways, and says which: no window up to
    ``step ** max_escalations`` times ``small`` could decide (the call is
    miscalibrated — raise ``small``), or the growth is super-linear.

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
    bound = growth_bound(step)
    reading = (
        f"the cost added from {growth.sizes[1]} to {growth.sizes[2]} is "
        f"{growth.ratio_floor:.1f}x to {growth.ratio_ceiling:.1f}x the cost "
        f"added from {growth.sizes[0]} to {growth.sizes[1]} (as measured "
        f"{growth.ratio:.1f}x; the range allows {NOISE_ALLOWANCE:.0%} error "
        f"per size). Linear reads ~{step}x and quadratic ~{step ** 2}x; the "
        f"bound is the midpoint, {bound:.1f}x"
    )
    assert growth.verdict != UNDECIDED, (
        f"{label}: could not decide — even at {growth.sizes[2]} the cost is "
        f"{growth.total_growth:.2f}x the cost at {growth.sizes[0]}, and "
        f"{reading}. The fixed per-call cost hides whatever grows "
        f"({growth.describe()}). Raise `small`."
    )
    assert growth.verdict == LINEAR, (
        f"{label}: {reading} — a backtracking pattern or a rescan per element "
        f"is the usual cause ({growth.describe()})"
    )
    return growth

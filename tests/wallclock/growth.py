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
so a quadratic read at half still clears it; at step 4 not even a noise-free
linear cost could pass (``min_total_growth``).

The verdict: an interval, and a third answer
--------------------------------------------

``R`` cancels ``C`` but noise does not, and when ``C`` dominates,
``t(8n) - t(n)`` is a small difference of two large numbers. The second
version of this module spent a noise allowance against one side only and
passed whatever that could not call super-linear — so a pure quadratic with a
moderate fixed cost passed (``fixed + n**2`` loop, ``C/q`` from ~430 to
~1360: 3/3 green). The rule is now two-sided. With every per-size figure
allowed to be off by an allowance of itself, ``[ratio_floor,
ratio_ceiling]`` is every ``R`` consistent with the measurement, and:

* **super-linear** only if ``ratio_floor >= bound`` — even the most linear
  reading of the numbers is over it;
* **linear** only if ``ratio_ceiling < bound`` — even the most quadratic
  reading is under it;
* otherwise **undecided**. So is any window whose largest size costs under
  ``min_total_growth(step)`` times its smallest, ~11.5 at step 8: the total
  growth at which a noise-free linear cost's ceiling meets the bound. Below
  it a window could only ever FAIL, where the fixed cost swamps the first
  difference and noise decides. Every early-window false fail CI recorded
  before #1781 read a total growth of 4.1-10.8, just over the old gate of 4.

The two sides do not allow the same error. The floor allows
``NOISE_ALLOWANCE`` and the ceiling ``PASS_NOISE_ALLOWANCE``, twice as much,
because the two wrong verdicts do not cost the same: a false fail breaks the
build and is looked at, a false pass lets a super-linear cost through
unseen. With one allowance for both, a CI runner's per-size error once passed
the planted quadratic from the window where ``C`` swamps ``t(8n) - t(n)``
(#1741).

An undecided window moves up by ``step`` — the work grows, ``C`` does not —
at most ``MAX_ESCALATIONS`` times, and a call still undecided is REFUSED as
miscalibrated. It is never passed.

The invariant
-------------

If every per-size figure ``m`` is within ``NOISE_ALLOWANCE * m`` of the true
per-call cost, the true ``R`` is at least ``ratio_floor``, so a cost whose
true ``R`` is under the bound can never be failed. If every figure is within
``PASS_NOISE_ALLOWANCE * m``, the true ``R`` is at most ``ratio_ceiling``, so
a cost at or over the bound can never be passed. A call still undecided at
the last window is refused, never passed. All of it holds **for every fixed
cost C**, because ``C`` is not in ``R``. ``C`` only decides whether a window
can decide. In particular:

* ``C + a*n`` (``R = 8``) never fails. It passes once ``C`` is under ~5x
  the work at the smallest size, which is where the gate sits, so the gate
  never delays a noise-free pass; three escalations reach that from ~2600x,
  and above that it is refused. A reading within the allowance can decide
  linear from a total growth of ~3.6, and the gate moves those up one window
  (the sshd reader's ``rhost-repeats`` on the CI probe: first-window passes
  50 of 128 before, 1 of 128 after, +0.054 s a call);
* ``C + q*n**2`` (``R = 64``) never passes. It fails once ``C`` is under
  ~390x the work at the smallest size — one escalation divides that by 64;
* ``C + a*n + q*n**2`` has ``R = (56a + 4032q) / (7a + 63q)``, increasing
  in ``q/a``: it crosses the bound where the quadratic term at the window's
  LARGEST size is ~2.5x the linear term. With no fixed cost the window says
  linear while that share is at most ~0.8x and super-linear from ~4x; in
  between it escalates, which multiplies ``q/a`` by 8, so a window only ever
  moves a mixture toward failing. The check answers for the sizes it
  measured; a call site picks sizes that reach the payloads it guards.

The figure is chosen so that one sample cannot set it. A size's figure is
the second-smallest of its ``n`` samples, and it is a sample from the speed
regime the other sizes were read in provided at most ONE of that size's
samples reads below that regime and at most ``n - 2`` read above it: one
sample in either direction cannot set it; two reading below it can (the
residual, measured below).

``tests/unit/ci/test_benchmark_calibration.py`` pins all of this over fixed
costs from 0 to 1e7 times the work, and outside the model it searches for an
adversarial per-size error: a wrong verdict needs one over 30.9% (a quadratic
passed) or over 29.5% (a linear failed), against 24.0% and 18.3% under the
old gate. It also replays recorded samples through the measurement itself.

A cost exactly at the bound (``n ** 1.5``) is undecidable by construction and
is refused, which is the honest answer.

What the samples look like (#1781)
----------------------------------

A probe on the CI runners recorded every sample of 6525 windows of the
linear controls (``C + n`` at known ``C``) under two conditions: four
saturated measuring workers, and the unit suite under xdist with coverage
beside one measurer. Typical error is small, p50 2.5% for the minimum of
five. The tail is ONE size read in a different speed regime from the other
two:

* Under saturation it is a FAST sample, one 20-30% below ten consistent
  ones (``51.1, 51.1, 51.2, 35.6, 51.0`` ns per unit), at most 47%. Under
  the suite it is small SLOW ones.
* Whether a fast sample is the thread clock under-counting or contention
  easing for a moment cannot be told from the probe. The same work ran at
  17.9 ns per unit in the suite untraced, 48.4 traced and 53.2 saturated;
  about half the fast outliers are round-wide (every size of that round
  reads fast); a x0.645 shift can hold for 2-4 rounds. The design does not
  depend on which: every size's figure must be read in the same regime.
* The minimum stops guaranteeing that once the fast state is rare: it
  selects exactly the one fast sample, and more samples make that worse.
  One fast sample explains the three kinds of CI failure. A fast ``t(8n)``
  reads ``t(8n) <= t(n)`` and the floor's denominator vanishes: a false
  super-linear. A fast middle reads ``R`` 12-16 and the ceiling straddles
  the bound: a refusal. A fast ``t(n)`` on a quadratic lowers its ``R``: a
  false pass.

Per-size error by figure: the worse of ``t(n)`` and ``t(64n)`` per unit of
work, relative to ``t(8n)``'s, over the 6525 windows; then its maximum
saturated, in the suite untraced, and in the suite traced::

    figure              p99    p99.9  max    >15%  >20%  >30%   sat / suite / traced
    minimum of 5        .163   .305   .492   81    34    7      .492 / .163 / .141
    minimum of 10       .194   .388   .505   120   62    16     .505 / .113 / .141
    2nd-smallest of 5   .094   .157   .205   10    1     0      .205 / .128 / .091
    median of 5         .062   .116   .264   1     1     0      .264 / .148 / .096
    3rd-smallest of 10  .066   .123   .270   3     2     0      .270 / .148 / .123
    median of 10        .055   .070   .099   0     0     0      .075 / .099 / .079

The second-smallest of the same five samples costs nothing and keeps the
worst window under the ~30% a wrong verdict needs. The median of five reads
better at p99 and worse in the worst window, where a regime shift mid-window
held three of the five; the median of ten is best and takes twice the time.
The residual is two fast samples at one size: 56 of 14652 saturated windows
had them, and the second-smallest of five read over 20% in 1 of 6525.

The measurement, each choice against a measured failure
-------------------------------------------------------

* **Thread CPU time, not wall clock.** ``time.thread_time`` does not
  advance while this thread is descheduled, which is most of what xdist
  adds.
* **Interleaved rounds, the second-smallest per size.** A size's figure is
  the second-smallest of its per-call samples (the one that settled its
  call count, then one per round), for the reasons above. Taking the three
  sizes in each round keeps a burst of contention from landing on one of
  them only.
* **A call count per size.** Each size repeats its call until one sample
  costs ``MIN_SAMPLE_SECONDS``, the per-call figure is what is compared,
  and the sample that settled the count is the first observation — so the
  large side of a restored quadratic runs once per round, not twice.
* **Stop at the unmistakable.** Once every size has ``MIN_REPETITIONS``
  samples, a round whose ``ratio_floor`` is already twice the bound ends the
  measurement. Not before: on one sample per size, the figure IS the sample
  the tail lives in.

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
from typing import Callable, List, Sequence, Tuple, TypeVar

P = TypeVar("P")

#: The ratio between consecutive sizes: ``n``, ``step * n``, ``step**2 * n``.
DEFAULT_STEP = 8

#: Samples per size per window: the one that settles the call count, then one
#: per further round. A size's figure is the second-smallest of them.
DEFAULT_REPETITIONS = 5

#: The fewest samples per size a window takes, and how many every size has
#: before a window may end early: the second-smallest of three is their
#: median, so no one sample sets it.
MIN_REPETITIONS = 3

#: Each size repeats its call until one sample costs at least this much CPU,
#: so no size is measuring the timer.
MIN_SAMPLE_SECONDS = 0.002

#: Upper limit on that repetition, so a function that does nothing at all
#: cannot spin the check forever.
MAX_CALLS_PER_SAMPLE = 1 << 16

#: How far each per-size figure may be off, as a fraction of itself, before a
#: reading can FAIL a call. On the CI probe the figure's error was 9.4% at
#: p99 and 20.5% at most; a false fail needs over 29.5% (#1781).
NOISE_ALLOWANCE = 0.10

#: The same allowance before a reading can PASS a call, twice as wide. A false
#: fail breaks the build and gets looked at; a false pass lets a super-linear
#: cost through silently, so a pass has to survive twice the error. A CI runner
#: produced a per-size error past the old symmetric margin and passed the
#: planted quadratic (#1741).
PASS_NOISE_ALLOWANCE = 2 * NOISE_ALLOWANCE

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


def min_total_growth(step: int) -> float:
    """The least total growth ``t3 / t1`` at which a window may decide.

    The total growth of a noise-free linear cost ``C + n`` whose ceiling,
    under ``PASS_NOISE_ALLOWANCE``, meets the bound. Its fixed cost there,
    in units of the work at the window's smallest size, solves
    ``ratio_ceiling == growth_bound(step)`` in closed form::

        C* = ((k - 1)(B - k) - p(k + 1)(B + k)) / (2p(B + 1))

    for step ``k``, bound ``B`` and allowance ``p``, and the growth is
    ``(C* + k**2) / (C* + 1)``: 11.4986 at step 8 and 20% (``C*`` 5.0008).
    A linear cost with a smaller ``C`` reads a larger growth, so the gate
    never delays a noise-free pass; below it a window could only fail.

    At step 4 ``C*`` is 0: the bound is 8 and even ``C = 0`` reads a ceiling
    of exactly 8, so no noise-free linear cost can pass at all. ``C*`` is
    held at 0 there and below, which makes the gate ``step ** 2``, the
    growth of a cost with no fixed part.
    """
    k = float(step)
    bound = growth_bound(step)
    allowance = PASS_NOISE_ALLOWANCE
    fixed = ((k - 1) * (bound - k) - allowance * (k + 1) * (bound + k)) / (
        2 * allowance * (bound + 1)
    )
    fixed = max(0.0, fixed)
    return (fixed + k * k) / (fixed + 1)


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
        """The least true ratio consistent with the per-size figures.

        Numerator as small and denominator as large as ``NOISE_ALLOWANCE``
        permits. ``-inf`` when the figures are consistent with ``t(8n)`` not
        exceeding ``t(n)``, where no ratio is defined.
        """
        t1, t2, t3 = self.seconds
        upper = (t3 - t2) - NOISE_ALLOWANCE * (t2 + t3)
        lower = (t2 - t1) + NOISE_ALLOWANCE * (t1 + t2)
        return upper / lower if lower > 0 else -math.inf

    @property
    def ratio_ceiling(self) -> float:
        """The greatest true ratio consistent with the per-size figures, each
        off by up to ``PASS_NOISE_ALLOWANCE`` (the wider allowance: this is
        the side a pass is judged on).

        ``inf`` when the noise could make ``t(8n) - t(n)`` zero: nothing then
        bounds the ratio from above.
        """
        t1, t2, t3 = self.seconds
        upper = (t3 - t2) + PASS_NOISE_ALLOWANCE * (t2 + t3)
        lower = (t2 - t1) - PASS_NOISE_ALLOWANCE * (t1 + t2)
        return upper / lower if lower > 0 else math.inf

    @property
    def verdict(self) -> str:
        """``SUPER_LINEAR``, ``LINEAR``, or ``UNDECIDED`` — see the module."""
        if self.total_growth < min_total_growth(self.step):
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
    something other than ``fn``. The figure would shed one such sample, but
    pausing costs nothing and removes the question.
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


def _figure(samples: Sequence[float]) -> float:
    """A size's per-call figure: the second-smallest of its samples."""
    return sorted(samples)[1]


def _reading(
    sizes: Tuple[int, int, int], samples: Sequence[Sequence[float]], step: int
) -> Growth:
    t1, t2, t3 = (_figure(taken) for taken in samples)
    return Growth(sizes=sizes, seconds=(t1, t2, t3), step=step)


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
    samples: List[List[float]] = []
    for payload in payloads:
        count, first = _calibrate(fn, payload)
        calls.append(count)
        samples.append([first])

    bound = growth_bound(step)
    for taken in range(2, repetitions + 1):
        for index, payload in enumerate(payloads):
            per_call = _cpu_seconds(fn, payload, calls[index]) / calls[index]
            samples[index].append(per_call)
        if taken >= MIN_REPETITIONS:
            growth = _reading(sizes, samples, step)
            if growth.verdict == SUPER_LINEAR and growth.ratio_floor >= 2 * bound:
                return growth
    return _reading(sizes, samples, step)


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
    if repetitions < MIN_REPETITIONS:
        raise ValueError(
            f"repetitions must be at least {MIN_REPETITIONS}, got {repetitions}: "
            f"a size's figure is the second-smallest of its samples, and a "
            f"window may end early only once every size has {MIN_REPETITIONS}"
        )
    if small < 1 or max_escalations < 0:
        raise ValueError("small must be positive and max_escalations not negative")
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
        repetitions: Samples per size per window, at least
            ``MIN_REPETITIONS``.

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
        f"per size at the floor and {PASS_NOISE_ALLOWANCE:.0%} at the ceiling). "
        f"Linear reads "
        f"~{step}x and quadratic ~{step ** 2}x; the "
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

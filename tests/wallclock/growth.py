"""Is a cost linear in its input, or worse? — the growth-shape check (#1579).

A ReDoS or accidentally-quadratic guard asks a SHAPE question: does doubling
the input double the cost, or quadruple it? An absolute budget answers a
different one — "is this fast on this machine right now" — and under
``pytest-xdist`` that answer depends on what three other workers are doing.
#1579 recorded the failure in both spellings a growth test had here:

* an absolute ``elapsed < 2.0`` measured ``2.98`` on a required gate, on a
  commit that touched nothing it covered;
* a ratio ``doubling the body multiplied scoring work by 3.7x`` against a
  bound of ``3.0`` — on a code path whose honest ratio is ~2.

The second is the instructive one, because a ratio is already machine-
independent: a runner k times slower is k times slower at BOTH sizes, and
the k cancels. What it is not is noise-independent. At a factor of 2 the
linear answer (2x) and the quadratic one (4x) are only 2x apart, so the
bound has to sit inside that window, and one contended sample on one side
bridges it.

So this check makes four choices, each against a measured failure:

* **Sizes far apart.** At ``factor`` 16 linear reads ~16x and quadratic
  ~256x. The bound is their geometric midpoint, ``factor ** 1.5`` = 64x:
  a 4x margin on either side, where factor 2 had 1.4x.
* **Thread CPU time, not wall clock.** ``time.thread_time`` does not
  advance while this thread is descheduled, which is the dominant noise
  under xdist. What remains (cache and SMT contention) is a fraction of the
  4x margin.
* **Interleaved pairs, minimum of each side.** Every source of noise here
  is additive, so the fastest sample is the least contaminated, and pairing
  the sizes in time keeps a burst of contention from landing on one side
  only.
* **Enough calls per sample.** The call is repeated until the SMALL side
  costs at least ``MIN_SAMPLE_SECONDS``, and the large side runs the same
  number of calls — a microsecond-scale sample is dominated by the timer.

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
from typing import Callable, TypeVar

P = TypeVar("P")

#: The ratio between the two input sizes. See the module docstring for why
#: 16 rather than the 2 that flaked.
DEFAULT_FACTOR = 16

#: Interleaved (small, large) pairs per check. Five was enough for the
#: minimum to be stable on a box running four other pytest workers.
DEFAULT_REPETITIONS = 5

#: The small side is repeated until one sample costs at least this much CPU,
#: so neither side is measuring the timer.
MIN_SAMPLE_SECONDS = 0.002

#: Upper limit on that repetition, so a function that does nothing at all
#: cannot spin the check forever.
MAX_CALLS_PER_SAMPLE = 1 << 16


def growth_bound(factor: int) -> float:
    """The largest ratio a linear cost may show at ``factor``.

    The geometric midpoint of linear (``factor``) and quadratic
    (``factor ** 2``), so the margin is the same multiple on both sides.
    """
    return float(factor) ** 1.5


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


def growth_ratio(
    fn: Callable[[P], object],
    payload_at: Callable[[int], P],
    *,
    small: int,
    factor: int = DEFAULT_FACTOR,
    repetitions: int = DEFAULT_REPETITIONS,
) -> float:
    """CPU cost of ``fn(payload_at(small * factor))`` over ``fn(payload_at(small))``.

    Stops early once the ratio reaches ``factor ** 2`` — the quadratic
    signature itself. Thread CPU time cannot be inflated 16x by
    descheduling, so a ratio that high is the code, and a reintroduced
    backtracking pattern should fail in one pair rather than five.
    """
    if factor < 2:
        raise ValueError(f"factor must be at least 2, got {factor}")
    if small < 1 or repetitions < 1:
        raise ValueError("small and repetitions must be positive")
    small_payload = payload_at(small)
    large_payload = payload_at(small * factor)

    fn(small_payload)  # warm-up: first-call costs belong to neither side
    calls = 1
    while (
        _cpu_seconds(fn, small_payload, calls) < MIN_SAMPLE_SECONDS
        and calls < MAX_CALLS_PER_SAMPLE
    ):
        calls *= 2

    small_best = large_best = float("inf")
    for _ in range(repetitions):
        small_best = min(small_best, _cpu_seconds(fn, small_payload, calls))
        large_best = min(large_best, _cpu_seconds(fn, large_payload, calls))
        if large_best >= factor**2 * max(small_best, 1e-9):
            break
    return large_best / max(small_best, 1e-9)


def assert_linear_growth(
    fn: Callable[[P], object],
    payload_at: Callable[[int], P],
    *,
    small: int,
    label: str,
    factor: int = DEFAULT_FACTOR,
    repetitions: int = DEFAULT_REPETITIONS,
) -> None:
    """Fail if ``fn``'s cost grows faster than linearly in its input.

    ‼ The single site where a growth ratio meets its bound, in the same
    sense that ``assertions.py`` is for latencies:
    ``tests/unit/ci/test_benchmark_calibration.py`` fails the build on a
    test that measures a duration and judges it anywhere else.

    Args:
        fn: The synchronous, CPU-bound function under test.
        payload_at: Builds the input at a given size, in whatever unit the
            caller counts (repetitions of a hostile unit, kilobytes, lines).
        small: The smaller size; the larger is ``small * factor``.
        label: What was measured, for the failure message.
    """
    ratio = growth_ratio(
        fn, payload_at, small=small, factor=factor, repetitions=repetitions
    )
    bound = growth_bound(factor)
    assert ratio < bound, (
        f"{label}: {factor}x the input cost {ratio:.1f}x the CPU time. Linear "
        f"reads ~{factor}x and quadratic ~{factor ** 2}x; the bound is the "
        f"midpoint, {bound:.0f}x — a backtracking pattern or a rescan per "
        "element is the usual cause"
    )

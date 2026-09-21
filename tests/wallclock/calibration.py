"""Machine-throughput calibration for the latency benchmarks.

Why this module exists
----------------------

Every latency assertion in this suite used to compare a wall-clock
measurement against an absolute millisecond target. On a GitHub-hosted
runner that is not a measurement of the code — it is a measurement of which
runner GitHub handed us.

#908 quantified it from the runs' own ``benchmark_results.json`` artifacts.
Comparing a **failing** run to a **passing** run of the *same commit*, the
median per-test ratio across all 30 tests was **1.28, uniform across every
one of them**. Against a run of *different* code it was 0.98 — statistically
identical. No code path was slower; the whole pytest process scaled with
machine throughput. The thinnest-margin test is whichever one happens to sit
closest to its target that week, so naming it and raising its number just
elects the next canary.

The fix is to change the instrument rather than the thresholds, which encode
product targets. We take a fixed, cheap, CPU-bound measurement **in the same
pytest process** as the benchmarks and express each budget as
``target * calibration_scale()``. A uniform 1.28x slowdown then cancels,
while a 30% regression in one code path still fails that one test.

What the calibration measures
-----------------------------

``_calibration_work`` is pure Python with no I/O, no syscalls and no
allocation of anything the GC will have to walk: attribute loads, bound
method calls, integer arithmetic, dict stores and one f-string per
iteration. That shape is deliberate — the operations being benchmarked are
SQLAlchemy and aiosqlite against an **in-memory** database, so they too are
CPU-bound Python. A workload of a different shape (say pure integer
arithmetic in a tight loop) would track the runner's ALU and not the
bytecode dispatch, attribute lookup and dict traffic that actually dominate
here.

What it is correcting, measured on the real runners
---------------------------------------------------

This matters before the statistic does, because it decides what the
calibration has to be sensitive to. Two mechanisms can make a runner slower
and they call for different instruments:

* **Throughput** — a different CPU model, a lower clock, a noisier
  neighbour on the memory bus. Every instruction costs more, so everything
  scales by one factor regardless of how long an operation is.
* **Descheduling** — the VM is oversubscribed and the process is taken off
  CPU. Here a long operation suffers and a short one can finish inside a
  single scheduler slice and not notice, so the effect grows with operation
  length.

Measured on two green ``main`` runs of this suite (35499510079 and
35496672222), 50 tests each: the per-test ratio has a median of **0.829**
with an interquartile range of 0.794-0.900, and the correlation between
log(operation duration) and log(ratio) is **r = 0.018**. By duration
bucket: 0.838 under 10 ms, 0.830 from 10-100 ms, 0.822 over 100 ms —
indistinguishable across two and a half orders of magnitude.

GitHub's runner-to-runner variance is therefore squarely the **throughput**
kind, which is also what #908 concluded from its own three runs (a uniform
1.28 across all 30 tests). A CPU-bound loop measures exactly that factor,
and the calibration's repetition length is irrelevant to it — which frees
that length to be chosen for stability instead.

The statistic, and why it is sized the way it is
------------------------------------------------

The estimate is the **median of nine block minima**, each block the fastest
of 120 repetitions of a ~0.5 ms unit of work (on a runner; 1.8-2.4 ms on
the development box). Measured rather than assumed, on a deliberately contended
48-core box (load average ~20, concurrent VMs and test suites):

*Minimum within a block*, same argument as ``measure_min_latency``: every
error source on a shared runner is one-sided, so the fastest observed
repetition is the best estimate of what the work costs when the machine
lets it run.

*Median across blocks*, because a single minimum is an extreme-order
statistic whose stability depends on how often a perfectly clean window
occurs — which is a property of the ambient load, not of the machine's
speed. Measured head to head in one window, across six processes: a plain
minimum over 600 repetitions varied by 1.12-1.15x, the median of nine block
minima over comparable total work by **1.07x**. Both figures are a
comparison BETWEEN the two estimators in the same conditions, not an
absolute stability claim for either — see "How noisy is it, honestly"
below.

*Enough total work*, because neither statistic converges on a short sample:
a minimum over 200 repetitions varied by a factor of **1.98** within a
single process, which is noisier than the thing it corrects. Roughly a
second is what it takes. It is paid once per pytest process against a suite
that runs for minutes.

How noisy is it, honestly. Two figures, and the gap between them is the
point:

* three full pytest runs taken back to back — 1778.7, 1787.1 and
  1788.7 us/rep, a **0.6%** spread;
* twelve fresh processes over a longer window on the same box —
  1837.1 to 2434.9 us/rep, a **1.33x** spread.

The first is the statistic on a quiet machine; the second is the statistic
plus that machine's ambient load genuinely moving, and it is the honest
number to plan against. So the instrument is NOT an order of magnitude
quieter than the 1.2-1.5x runner-to-runner variance it corrects — on a
contended shared box it is comparable to it. What keeps that acceptable is
not precision but direction: the scale is floored at 1.0 (below), so noise
can only ever produce unearned relief, never a new red, and the amount of
relief is bounded by the noise. A dedicated runner executing nothing but
pytest should be quieter than this box; the printed calibration line makes
that measurable from the first few green runs rather than assumed.

A short repetition is also the FAIL-SAFE choice
-----------------------------------------------

Descheduling is not what GitHub's variance is made of, but it can still
happen, and the two candidate repetition lengths behave oppositely under
it. Both were measured, pinning the suite to four cores and adding twelve
competing processes:

* a **short** (~1.5-2 ms there) repetition reports ~**1.0x** — it keeps
  finding
  clean slices and sees nothing;
* a **long** (20 ms) repetition reports **2.2-2.6x** while the suite's own
  asserted statistic barely moved (median 1.00x and 1.06x over two
  interleaved rounds), because ``measure_min_latency``'s minimum over a
  short operation absorbs descheduling for the same reason the short
  calibration does.

So under descheduling the short repetition under-corrects (the scale floors
at 1.0 and nothing changes) while the long one would hand out 2.5x of
relief the tests never needed — a silent weakening of the gate. Given the
floor below, under-correcting costs nothing and over-correcting costs the
gate, so the short repetition wins on both counts.

Does the correction actually land? The end-to-end check available is a
cross-machine one, and it is the reason to believe the scheme rather than
just its parts. The development box is 3.44x slower than the reference by
this calibration. Running the suite there, the worst budget sits at
**88.2%** of its raw target — close enough to flake — and at **25.6%** of
its calibrated budget. The same test on the reference runner sits at
**27.7%** of the raw target. Two machines a factor of 3.4 apart report the
same utilisation once corrected, to within 2 points, which is exactly what
"the threshold stops measuring which machine you got" means.

One honest gap remains: no *within-machine* A/B validated it, because the
development box could not produce a throughput change to track. It is a
2015 Xeon running at 41% of nominal clock, and its async SQLite benchmarks
are dominated by CPU idle-exit latency — every contention lever available
(competing processes on the same cores, spinners on the hyperthread
siblings) made the benchmarks **faster**, 0.56-0.59x with the siblings
loaded, by keeping the cores out of deep C-states. That is a property of
that box rather than of a cloud runner, but it is why the argument leans on
the cross-machine check above and on the CI measurement, not on a local
before/after.

What it corrects well, and what only partly
-------------------------------------------

Two thirds of the suite compares ``measure_min_latency``'s MINIMUM, which
is a clean-window estimate like the calibration's own, so the two measure
the same quantity. The other third
(``test_case_service_operations``, ``test_investigation_session_service_operations``)
compares a **p95** over 50-100 iterations, which absorbs contention a
minimum rejects; there the scale moves the budget the right way but cannot
be expected to cancel exactly.

Measured rather than assumed: dividing every test's reported statistic in a
green ``main`` CI run (35499510079) by the same test's value on the
development box gives a median of **0.322** across the 34
minimum-statistic tests and **0.300** across the 16 p95 ones — the two
families track machine throughput to within about 7% of each other, so one
scale for both is reasonable.

The scale is deliberately NOT capped from above. A cap would hand a
genuinely degraded machine a budget it cannot meet, which is the false red
this exists to remove; and the median-of-block-minima statistic is what
keeps a transient spike from inflating the scale in the first place. The
cost is that the gate goes soft on a machine far slower than CI — a
Raspberry Pi, an emulated runner — where it was never meaningful anyway.

The scale never tightens a budget
---------------------------------

``calibration_scale()`` is floored at ``1.0``. A machine at or above the
reference speed gets exactly the thresholds that are written in the tests;
only a machine measurably slower than the reference gets proportional
relief. Two consequences worth being explicit about:

* **The scale can never tighten a budget**, on CI or on a developer's
  laptop. Whatever the calibration does, the worst case is that it does
  nothing. (Stated about the scale rather than about the pull request as a
  whole, because one unrelated part of #908 is a hair stricter: the nine
  budgets in ``test_investigation_session_service_operations`` moved from
  ``p95 <= target`` to the shared helper's ``observed < budget``. See its
  ``report_benchmark`` docstring.)
* It makes the reference constant fail safe in one direction, so being
  approximately right is enough. That asymmetry is spelled out where the
  constant is defined.

Absolute mode
-------------

``FM_BENCHMARK_ABSOLUTE=1`` pins the scale at 1.0, so the suite asserts the
raw product targets. That is what the nightly job runs: "does this operation
meet its wall-clock target" is still the honest question to ask somewhere,
just not on a pull request whose author cannot influence the answer.
"""

from __future__ import annotations

import os
import statistics
import time
from typing import Optional, Tuple

#: Iterations inside one repetition of the calibration workload. Sized so a
#: repetition costs roughly 0.5 ms on a GitHub-hosted runner (~1.8 ms on
#: the development box): long enough that ``perf_counter`` overhead is
#: irrelevant, short enough that a repetition usually fits inside one
#: scheduler slice.
#:
#: That second property is a deliberate choice, not an accident of sizing —
#: see "A short repetition is also the FAIL-SAFE choice" in the module
#: docstring. It makes the measurement blind to descheduling, which is the
#: direction that costs nothing, and GitHub's runner variance is not
#: descheduling anyway (r = 0.018 against operation duration, measured).
CALIBRATION_WORK_ITERATIONS = 2_000

#: Repetitions inside one block. The block's estimate is its MINIMUM, for
#: the reason ``measure_min_latency`` takes one: every error source here is
#: one-sided, so the fastest repetition is the closest estimate of what the
#: work costs when the machine lets it run.
CALIBRATION_REPETITIONS_PER_BLOCK = 120

#: Blocks taken; the estimate is the MEDIAN of their minima. A single
#: minimum over the same total work is an extreme-order statistic whose
#: stability depends on how often a perfectly clean window occurs, which is
#: a property of the ambient load rather than of the machine's speed.
#: Measured across six processes on the contended development box
#: (2026-09-20), head to head in one window: a plain minimum over 600
#: repetitions varied by 1.12-1.15x between processes, the median of nine
#: block minima by 1.07x. That is the two estimators compared under the
#: same conditions; over a longer window the same statistic spans 1.33x on
#: that box, which is the figure to plan against. Total cost is about 0.8 s
#: on a runner, against a job that runs for four minutes.
CALIBRATION_BLOCKS = 9

#: Seconds one repetition of ``_calibration_work`` costs on the reference
#: machine — a healthy GitHub-hosted ``ubuntu-latest`` runner, which is the
#: machine class the thresholds in this suite were tuned against.
#:
#: How it was obtained (2026-09-20): the calibration was measured in the
#: same pytest process as a full benchmark run on the development box, and
#: every test's reported statistic was divided by the same test's value in
#: the ``benchmark-results`` artifact of a green ``main`` run (35499510079) —
#: the same artifacts #908 itself reasoned from. The median of that ratio
#: over 49 joined tests says how much faster the runner is than the box, and
#: the reference is the box's calibration scaled by it. Two independent full
#: runs agreed closely: 1778.7 us x 0.2860 = 508.8 us and
#: 1787.1 us x 0.2833 = 506.3 us. The value below is rounded UP from those,
#: for the reason in the last paragraph here.
#:
#: Re-anchor it the same way, or more simply from the runner itself: every
#: run prints its own calibration (see ``describe_calibration``) and that
#: line lands in ``benchmark_output.txt`` and the job summary, so the value
#: this constant should have is observable from any green run rather than
#: needing a special one. Take the middle of a few and set it here.
#:
#: The error is one-sided, which is why an approximate value is safe. Too
#: HIGH and the scale floors at 1.0 more often, i.e. degrades to the
#: uncalibrated behaviour. Too LOW and every runner gets permanent relief
#: and the gate quietly weakens. Err high.
CALIBRATION_REFERENCE_SECONDS = 5.2e-4

#: Set to 1/true to assert the raw targets with no scaling (nightly job).
ABSOLUTE_MODE_ENV = "FM_BENCHMARK_ABSOLUTE"


class _Cell:
    """Attribute + bound-method traffic, the shape ORM code is made of."""

    __slots__ = ("a", "b")

    def __init__(self, a: int, b: int) -> None:
        self.a = a
        self.b = b

    def step(self, i: int) -> int:
        self.a = (self.a + i * i) ^ (self.b >> 3)
        self.b = self.a & 0xFFFF
        return self.a


def _calibration_work(iterations: int = CALIBRATION_WORK_ITERATIONS) -> int:
    """A fixed unit of CPU-bound work. No I/O, no syscalls, no randomness."""
    cell = _Cell(1, 2)
    scratch: dict = {}
    acc = 0
    for i in range(iterations):
        acc = cell.step(i)
        key = i & 255
        scratch[key] = (acc, f"{key:03d}")
        acc ^= len(scratch[key][1])
    return acc


def measure_calibration(
    blocks: int = CALIBRATION_BLOCKS,
    repetitions_per_block: int = CALIBRATION_REPETITIONS_PER_BLOCK,
) -> float:
    """Seconds one repetition of the workload costs on this machine.

    Median of ``blocks`` block-minima, each over ``repetitions_per_block``
    runs. See the two constants for why it is that shape rather than one
    minimum over the same total work.

    One untimed warm-up first, for the same reason ``measure_min_latency``
    takes one: the first call pays interpreter-level costs that belong to
    nobody's budget.
    """
    if blocks < 1:
        raise ValueError(f"blocks must be >= 1, got {blocks}")
    if repetitions_per_block < 1:
        raise ValueError(
            f"repetitions_per_block must be >= 1, got {repetitions_per_block}"
        )

    _calibration_work()
    block_minima = []
    for _ in range(blocks):
        best = float("inf")
        for _ in range(repetitions_per_block):
            start = time.perf_counter()
            _calibration_work()
            elapsed = time.perf_counter() - start
            if elapsed < best:
                best = elapsed
        block_minima.append(best)
    return statistics.median(block_minima)


def absolute_mode() -> bool:
    """True when the suite must assert raw targets with no scaling."""
    return os.environ.get(ABSOLUTE_MODE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_measured: Optional[float] = None
_scale_used: bool = False


def measured_calibration() -> float:
    """The calibration for this pytest process, measured once and cached."""
    global _measured
    if _measured is None:
        _measured = measure_calibration()
    return _measured


def reset_calibration_cache() -> None:
    """Drop the cached measurement. For this module's own tests only.

    ‼ This clears ``_scale_used`` as well, which is SESSION state, not
    per-test state: the terminal-summary hook keys on it to decide whether
    to print the scale a red was measured against. A test that resets the
    cache and does not put the flag back therefore silences that line for
    everything that ran BEFORE it. That is not hypothetical — pytest
    collects ``tests/performance/`` before ``tests/unit/``, so in both
    required CI gates the calibrated budgets there run first and this
    module's own tests then erased the evidence (#1557 review). Use
    ``calibration_state`` / ``restore_calibration_state`` around a reset
    that is part of a test fixture.
    """
    global _measured, _scale_used
    _measured = None
    _scale_used = False


def calibration_state() -> Tuple[Optional[float], bool]:
    """A snapshot of the module's cached state, for a fixture to restore."""
    return _measured, _scale_used


def restore_calibration_state(state: Tuple[Optional[float], bool]) -> None:
    """Put back what ``calibration_state`` returned.

    Restoring rather than clearing is what keeps a test of this module
    from deciding, for the whole session, that no budget was ever
    asserted.
    """
    global _measured, _scale_used
    _measured, _scale_used = state


def scale_was_used() -> bool:
    """True once a budget has actually been scaled in this process.

    The terminal summary keys on this so that the ordinary CI invocation —
    ``pytest tests/ -m "not benchmark"``, which collects this package and
    deselects every test in it — never pays for a calibration nothing asked
    for.
    """
    return _scale_used


def calibration_scale() -> float:
    """Factor every latency budget in this suite is multiplied by.

    ``1.0`` on a machine at or above the reference speed, and
    ``observed / reference`` on one measurably slower. Never below 1.0 — see
    the module docstring.
    """
    global _scale_used
    _scale_used = True
    if absolute_mode():
        return 1.0
    # ‼ Argument order is load-bearing, not style. `max(1.0, nan)` is 1.0
    # because every comparison against nan is False and the first argument
    # survives; `max(nan, 1.0)` is nan, which would make every budget nan
    # and every assertion a hard failure across the whole suite. No path
    # produces nan today — the timer cannot and the reference is a positive
    # constant — so this is a guard against a future tidy-up, and
    # `test_a_nan_measurement_still_floors_at_one` is what enforces it.
    return max(1.0, measured_calibration() / CALIBRATION_REFERENCE_SECONDS)


def describe_calibration() -> str:
    """One line naming the calibration, the reference and the scale.

    Printed by the suite so a red run can be read as "slow runner" or "real
    regression" from the job log alone, and so the value
    ``CALIBRATION_REFERENCE_SECONDS`` *should* have is observable from any
    run rather than needing a special one.

    ‼ Reports what was already measured; it never triggers a measurement.
    The ordinary CI invocation registers this package's conftest (it
    collects ``tests/benchmarks`` and deselects it by marker), so a
    describe-that-measures would put a second of CPU on the end of every
    run in the repository for a line nobody asked for.
    """
    if absolute_mode():
        head = (
            "benchmark calibration: ABSOLUTE mode "
            f"({ABSOLUTE_MODE_ENV} set) - budgets are the raw targets"
        )
        if _measured is None:
            return head
        # ‼ The nightly job is the ONE run whose reds genuinely need "slow
        # runner or real regression" disambiguating, because it is the only
        # one asserting wall-clock. Reporting only "ABSOLUTE mode" there
        # would leave the reader with no number to disambiguate WITH, while
        # the documentation told them to read one. So absolute mode still
        # measures — for the report; the scale stays pinned at 1.0.
        return (
            f"{head}; machine measured {_measured * 1e6:.1f}us/rep "
            f"vs reference {CALIBRATION_REFERENCE_SECONDS * 1e6:.1f}us/rep "
            f"(raw ratio {_measured / CALIBRATION_REFERENCE_SECONDS:.2f}x, "
            "NOT applied)"
        )
    if _measured is None:
        return "benchmark calibration: not measured (no budget was asserted)"
    scale = max(1.0, _measured / CALIBRATION_REFERENCE_SECONDS)
    return (
        f"benchmark calibration: {_measured * 1e6:.1f}us/rep "
        f"(reference {CALIBRATION_REFERENCE_SECONDS * 1e6:.1f}us/rep, "
        f"raw ratio {_measured / CALIBRATION_REFERENCE_SECONDS:.2f}x) "
        f"-> budget scale {scale:.2f}x"
    )

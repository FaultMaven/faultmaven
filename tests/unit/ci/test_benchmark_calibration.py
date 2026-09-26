"""The benchmark gate's instrument (#908).

`tests/benchmarks/` is excluded from both CI test suites (`-m "not
benchmark"`) and runs in a workflow of its own, so nothing in the ordinary
suite exercises it. This file does — it is deliberately unmarked so it runs
in `Test Standalone`/`Test Cloud`, where a broken calibration is cheap to
find rather than being discovered by a benchmark job a week later.

What it holds down, and why each one:

* **The floor.** `calibration_scale()` never drops below 1.0, so the scale
  cannot tighten a budget. That is the property that makes the reference
  constant safe to be approximately right, and the one that makes the
  instrument's own noise affordable.
* **The correction.** A machine measurably slower than the reference gets a
  proportionally larger budget, which is the one thing #908 asked for.
* **The direction on throughput.** Throughput is 1/latency, so its floor
  must be DIVIDED by the scale. Multiplying would tighten it on exactly the
  runners the correction exists to relieve, and the error is invisible at a
  scale of 1.0 — which is every developer machine.
* **Discrimination.** A uniform slowdown across every test cancels; a
  regression in one path does not. Asserted as the two columns, because a
  scheme that only passes the first has disabled the benchmarks. #1556
  re-anchored the budgets to 2-3x measured cost, so the second column is
  now a GROSS regression and the 30%-detection column asserts that it does
  NOT fire — the trade, pinned rather than left to be rediscovered.
* **The budget table (#1556).** Every anchor in `tests/benchmarks/budgets.py`
  sits 2-3x above the p95 that module records for it, every anchor is the
  stricter of that budget's two numbers, and every entry is used by a test
  that exists. The band is enforced at import time by the dataclasses too;
  here it is visible as a test.
* **Which number is asserted.** A pull request compares the regression
  anchor, scaled; `FM_BENCHMARK_ABSOLUTE` compares the raw product target,
  unscaled. Driven through the helper, because the split being described
  correctly in three docstrings is not the same as it working.
* **Single comparison site.** An AST scan of `tests/benchmarks/` fails if a
  latency or throughput threshold is compared anywhere but the two helpers.
  Before #908 the same rule was written three ways in three modules, which
  is the only reason calibrating it was a multi-file change. The scan
  carries a positive control, because a scan whose vocabulary has drifted
  reports a clean tree exactly like a clean tree does.
* **The rest of `tests/` (#1579).** Both required gates run every other
  directory too, where 50 raw wall-clock thresholds were found, two of them
  red on unrelated commits once the gates went under `pytest-xdist`. A
  third scan finds the code that MEASURES a duration anywhere outside the
  two timing suites, and holds every test that reaches it to the same two
  rules — no threshold, and a helper as the judge.
* **Laziness.** The ordinary CI invocation collects this package and
  deselects every test in it. Nothing may pay the calibration's ~1s for
  that.
"""

from __future__ import annotations

import ast
import math
import os
import warnings
from pathlib import Path
from typing import List, Tuple

import pytest
import yaml

import tests.conftest as root_conftest
from tests.benchmarks import budgets as budget_table
from tests.performance import budgets as performance_table
from tests.wallclock import calibration
from tests.wallclock.assertions import (
    assert_latency_within,
    assert_throughput_at_least,
)
from tests.wallclock.budgets import (
    MAX_REGRESSION_MULTIPLE,
    MIN_REGRESSION_MULTIPLE,
    LatencyBudget,
    ThroughputBudget,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_DIR = REPO_ROOT / "tests" / "benchmarks"

#: ‼ The second directory is the whole of #1557. ``tests/benchmarks/`` is
#: excluded from both required CI gates by ``-m "not benchmark"``;
#: ``tests/performance/`` is not, so a hand-rolled threshold there reds a
#: REQUIRED check on a diff that changed nothing. #1555 scanned only the
#: first, and when #1557 pointed the FIRST version of the scan at the
#: second it reported **zero** violations against **27** live ones —
#: because the vocabulary it matched on (``latency``, ``elapsed``, ``p95``,
#: ``_ms``…) was built from the benchmark suite's variable names and none
#: of ``tests/performance/``'s (``per_record_time``, ``time_per_call``,
#: ``overhead_percentage``…) contain one. The rule below matches on the
#: SHAPE of the comparison instead, so it cannot be escaped by naming.
PERFORMANCE_DIR = REPO_ROOT / "tests" / "performance"
GUARDED_DIRS = (BENCHMARK_DIR, PERFORMANCE_DIR)

#: Where the shared machinery lives. Scanned for the call graph only (it
#: holds the helpers themselves), never for violations.
WALLCLOCK_DIR = REPO_ROOT / "tests" / "wallclock"

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "benchmarks.yml"


#: Both suites' tables in one mapping, for the properties that hold of
#: every anchor regardless of which suite it belongs to. Names are unique
#: across the two tables, and this asserts it rather than assuming it.
ALL_ANCHORS = {**budget_table.ALL_BUDGETS, **performance_table.ALL_BUDGETS}
assert len(ALL_ANCHORS) == len(budget_table.ALL_BUDGETS) + len(
    performance_table.ALL_BUDGETS
), "a budget name is used by both tables; the guard would check only one"


def _table_for(directory: Path):
    return (
        budget_table.ALL_BUDGETS
        if directory == BENCHMARK_DIR
        else performance_table.ALL_BUDGETS
    )


def _suite_sources(directory: Path) -> str:
    return "\n".join(path.read_text() for path in sorted(directory.glob("test_*.py")))


@pytest.fixture(autouse=True)
def _clean_calibration():
    """Every test here starts from an unmeasured, non-absolute process,
    and the process it found is put back afterwards.

    ‼ ``reset_calibration_cache()`` clears ``_scale_used``, which is
    SESSION state: the terminal-summary hook keys on it to decide whether
    to print the scale a red was measured against. pytest collects
    ``tests/performance/`` before ``tests/unit/``, so in BOTH required
    gates the calibrated budgets are asserted before this module runs —
    and a bare reset here erased the fact for the whole session, so a red
    performance test shipped with no scale line at all. Measured on the
    first version of this branch: ``pytest tests/performance
    tests/unit/ci/test_benchmark_calibration.py`` printed the line 0
    times, the same two paths in the other order 2.

    ‼ It also does NOT take ``monkeypatch``. Fixtures finalize in reverse
    order of setup, so one that requests ``monkeypatch`` is torn down
    BEFORE it — and ``_pin_calibration`` sets ``_measured`` through
    ``monkeypatch.setattr``, whose undo would then run after this restore
    and put ``None`` back. Measured: with the dependency the line printed
    "not measured (no budget was asserted)" instead of the scale. The
    environment variable is saved and restored by hand for that reason.
    """
    state = calibration.calibration_state()
    previous_env = os.environ.pop(calibration.ABSOLUTE_MODE_ENV, None)
    calibration.reset_calibration_cache()
    try:
        yield
    finally:
        calibration.reset_calibration_cache()
        calibration.restore_calibration_state(state)
        if previous_env is None:
            os.environ.pop(calibration.ABSOLUTE_MODE_ENV, None)
        else:
            os.environ[calibration.ABSOLUTE_MODE_ENV] = previous_env


def _pin_calibration(monkeypatch, seconds: float) -> None:
    """Pretend this machine measured ``seconds`` per calibration repetition."""
    calibration.reset_calibration_cache()
    monkeypatch.setattr(calibration, "_measured", seconds)


def _latency(regression: float, product_target: float = None) -> LatencyBudget:
    """A throwaway latency budget for exercising the comparison itself.

    ``reference`` is derived so the budget sits in the middle of the band
    ``LatencyBudget`` enforces; these probes are about the comparison, not
    about any shipped anchor.
    """
    if product_target is None:
        product_target = regression * 10
    return LatencyBudget(
        "probe",
        regression=regression,
        product_target=product_target,
        reference=regression / 2.5,
    )


def _throughput(regression: float, product_target: float = None) -> ThroughputBudget:
    """Throughput counterpart of ``_latency``."""
    if product_target is None:
        product_target = regression / 10
    return ThroughputBudget(
        "probe",
        regression=regression,
        product_target=product_target,
        reference=regression * 2.5,
    )


# ---------------------------------------------------------------- the scale


class TestCalibrationScale:
    def test_reference_is_a_positive_duration(self):
        assert calibration.CALIBRATION_REFERENCE_SECONDS > 0

    def test_a_machine_at_reference_speed_gets_the_written_thresholds(
        self, monkeypatch
    ):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        assert calibration.calibration_scale() == pytest.approx(1.0)

    def test_a_faster_machine_is_never_held_to_a_tighter_budget(self, monkeypatch):
        # Floored at 1.0. Without this, every developer box — which is
        # typically faster than a shared runner — would start failing
        # benchmarks that pass in CI, and the mechanism meant to remove
        # false reds would be the thing producing them.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS / 4)
        assert calibration.calibration_scale() == 1.0

    def test_a_slower_machine_gets_a_proportionally_larger_budget(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.28)
        assert calibration.calibration_scale() == pytest.approx(1.28)

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_absolute_mode_pins_the_scale_and_measures_nothing(
        self, monkeypatch, value
    ):
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, value)

        def _explode(*_a, **_k):
            raise AssertionError("absolute mode must not measure anything")

        monkeypatch.setattr(calibration, "measure_calibration", _explode)
        assert calibration.calibration_scale() == 1.0

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_absolute_mode_is_off_for_anything_else(self, monkeypatch, value):
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, value)
        assert calibration.absolute_mode() is False

    def test_the_measurement_is_taken_once_per_process(self, monkeypatch):
        calls = []

        def _count(*_a, **_k):
            calls.append(1)
            return calibration.CALIBRATION_REFERENCE_SECONDS

        monkeypatch.setattr(calibration, "measure_calibration", _count)
        calibration.calibration_scale()
        calibration.calibration_scale()
        calibration.calibration_scale()
        assert len(calls) == 1

    def test_nothing_is_measured_until_a_budget_asks(self, monkeypatch):
        # `pytest tests/ -m "not benchmark"` collects tests/benchmarks and
        # deselects all of it. The terminal-summary hook keys on this, so a
        # regression here puts a second of CPU on every CI run.
        def _explode(*_a, **_k):
            raise AssertionError("measured without a budget asking")

        monkeypatch.setattr(calibration, "measure_calibration", _explode)
        assert calibration.scale_was_used() is False
        # Describing must not be a back door into measuring, because the
        # terminal-summary hook is registered on every `pytest tests/` run.
        assert "not measured" in calibration.describe_calibration()

    def test_the_description_names_the_scale_that_was_applied(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 2)
        line = calibration.describe_calibration()
        assert "2.00x" in line
        # Describing is not using: only an asserted budget flips the flag
        # the terminal summary keys on.
        assert calibration.scale_was_used() is False
        assert_latency_within(0.001, _latency(1.0), "probe")
        assert calibration.scale_was_used() is True

    def test_the_description_says_so_in_absolute_mode(self, monkeypatch):
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        assert "ABSOLUTE" in calibration.describe_calibration()

    def test_absolute_mode_reports_the_number_when_it_has_one(self, monkeypatch):
        # The nightly job is the only run asserting raw wall-clock, so it is
        # the one whose reds need "slow runner or real regression"
        # disambiguating — and the docs tell the reader to read the scale
        # there. Reporting only "ABSOLUTE mode" would leave them nothing.
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        monkeypatch.setattr(
            calibration, "_measured", calibration.CALIBRATION_REFERENCE_SECONDS * 1.22
        )
        line = calibration.describe_calibration()
        assert "ABSOLUTE" in line
        assert "1.22x" in line
        assert "NOT applied" in line

    def test_a_nan_measurement_still_floors_at_one(self, monkeypatch):
        # `max(1.0, nan)` is 1.0 but `max(nan, 1.0)` is nan, and a nan scale
        # would make every budget nan and every assertion a hard failure.
        # Unreachable today; this pins the argument order against a tidy-up.
        _pin_calibration(monkeypatch, float("nan"))
        assert calibration.calibration_scale() == 1.0


# ---------------------------------------------------- the real measurement


class TestTheWorkloadItself:
    def test_the_workload_is_deterministic(self):
        assert calibration._calibration_work() == calibration._calibration_work()

    def test_a_measurement_is_a_positive_finite_duration(self):
        # A handful of repetitions, not the shipped counts — this test is
        # about the shape of the answer, not its stability.
        #
        # ‼ No upper bound (#1579). This read ``0 < observed < 1.0``: a raw
        # wall-clock ceiling on a real measurement in both required gates,
        # which the #1579 census missed because the clock is read inside
        # ``tests/wallclock/``. What the ceiling was for — the right UNIT —
        # is pinned exactly, on a stub clock, by the median-of-block-minima
        # test below, which fails if the estimator stops returning
        # ``perf_counter`` seconds.
        observed = calibration.measure_calibration(blocks=3, repetitions_per_block=3)
        assert observed > 0
        assert math.isfinite(observed)

    @pytest.mark.parametrize("kwargs", [{"blocks": 0}, {"repetitions_per_block": 0}])
    def test_the_sample_sizes_must_be_positive(self, kwargs):
        with pytest.raises(ValueError):
            calibration.measure_calibration(**kwargs)

    def test_the_estimate_is_the_median_of_block_minima(self, monkeypatch):
        # Driven off a stub clock so the answer is arithmetic, and chosen
        # so the two candidate statistics disagree: the block minima are
        # 10, 4 and 8, whose median is 8, while a plain minimum over every
        # repetition — the estimator this shape exists to replace — would
        # answer 4.
        #
        # perf_counter is read twice per timed repetition (start, end) and
        # not at all for the untimed warm-up, so this is six repetitions
        # lasting 10, 12, 4, 20, 30, 8.
        clock = iter([0, 10, 10, 22, 22, 26, 26, 46, 46, 76, 76, 84])
        monkeypatch.setattr(
            calibration.time, "perf_counter", lambda: float(next(clock))
        )
        result = calibration.measure_calibration(blocks=3, repetitions_per_block=2)
        assert result == 8.0


# ------------------------------------------------------------- the helpers


class TestLatencyBudget:
    def test_passes_under_the_target_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        assert_latency_within(0.150, _latency(0.200), "probe")

    def test_fails_over_the_target_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError) as excinfo:
            assert_latency_within(0.250, _latency(0.200), "probe")
        assert "200ms regression budget" in str(excinfo.value)
        assert "1.00 calibration" in str(excinfo.value)

    def test_the_budget_moves_with_the_scale(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.5)
        assert_latency_within(0.250, _latency(0.200), "probe")  # 250ms < 200 * 1.5
        with pytest.raises(AssertionError):
            assert_latency_within(0.310, _latency(0.200), "probe")

    def test_the_failure_message_carries_the_detail(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError) as excinfo:
            assert_latency_within(0.9, _latency(0.1), "probe", "min 900.0ms (n=5)")
        assert "min 900.0ms (n=5)" in str(excinfo.value)

    def test_a_throughput_budget_is_refused(self, monkeypatch):
        # Both budgets are a pair of floats, and handing one to the wrong
        # helper inverts the direction in silence: a throughput floor
        # multiplied by the scale would TIGHTEN on a slow runner, and the
        # numbers are plausible enough that nothing else would notice.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(TypeError):
            assert_latency_within(0.150, _throughput(50.0), "probe")


class TestThroughputFloor:
    def test_passes_above_the_floor_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        assert_throughput_at_least(60.0, _throughput(50.0), "probe")

    def test_fails_below_the_floor_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError):
            assert_throughput_at_least(40.0, _throughput(50.0), "probe")

    def test_a_slower_machine_gets_a_LOWER_floor_not_a_higher_one(self, monkeypatch):
        # The direction guard. Throughput is 1/latency; multiplying by the
        # scale here would demand MORE work per second from a machine that
        # was just measured to be slower.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 2)
        assert_throughput_at_least(26.0, _throughput(50.0), "probe")  # floor 25/s
        with pytest.raises(AssertionError):
            assert_throughput_at_least(24.0, _throughput(50.0), "probe")

    def test_a_latency_budget_is_refused(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(TypeError):
            assert_throughput_at_least(60.0, _latency(0.200), "probe")


# -------------------------------------------- the two numbers, and the split


class TestTheBudgetTable:
    """The shipped anchors, checked against the ruling that set them (#1556).

    The table is data, so these are the assertions that keep it honest. The
    band is also enforced by the dataclasses at import time — which is the
    check that cannot be skipped — and re-asserted here so the property is
    visible as a test rather than only as a constructor side effect.
    """

    def test_every_budget_in_the_suite_is_in_the_table(self):
        # 50 latency/throughput budgets, the number #1556 measured. The
        # memory assertions are deliberately NOT among them: megabytes do
        # not scale with machine throughput and #1556 re-anchored nothing
        # there.
        assert len(budget_table.ALL_BUDGETS) == 50

    def test_every_budget_in_the_performance_suite_is_in_its_table(self):
        # 21 latency budgets. 27 hand-rolled comparisons went in; four
        # came out as deletions rather than budgets (they measured
        # `asyncio.sleep` granularity), one percentage was folded into the
        # duration beside it, and two more were dropped in review as
        # arithmetically redundant — a per-operation figure that is the
        # per-task one divided by a constant, so its anchor could never
        # fire first. The memory and object-count assertions are not among
        # them, for the same reason as above.
        #
        # +7 in #1579: timings moved here from unit and integration tests the
        # required gates ran as raw wall clock — two sanitizer throughputs,
        # two disabled-shim call costs, vocabulary and timestamp extraction,
        # and extraction beside a hostile line against the Tier-1 timeout.
        assert len(performance_table.ALL_BUDGETS) == 28

    @pytest.mark.parametrize("name", sorted(ALL_ANCHORS))
    def test_the_anchor_is_2_to_3x_its_measured_reference(self, name):
        budget = ALL_ANCHORS[name]
        if isinstance(budget, ThroughputBudget):
            multiple = budget.reference / budget.regression
        else:
            multiple = budget.regression / budget.reference
        assert (
            MIN_REGRESSION_MULTIPLE <= multiple <= MAX_REGRESSION_MULTIPLE
        ), f"{name} is {multiple:.2f}x its reference"

    @pytest.mark.parametrize("name", sorted(ALL_ANCHORS))
    def test_the_per_pull_request_anchor_is_the_stricter_of_the_two(self, name):
        # The structural fact behind "the nightly is where a product target
        # is asserted": on every budget the per-PR anchor is TIGHTER than
        # the product target, so a green pull request implies the product
        # target held too, and the nightly's job is the wall clock rather
        # than the regression.
        budget = ALL_ANCHORS[name]
        if isinstance(budget, ThroughputBudget):
            assert budget.regression >= budget.product_target
        else:
            assert budget.regression <= budget.product_target

    @pytest.mark.parametrize(
        "kwargs",
        [
            # anchor far above the band: a re-anchor that moved `regression`
            # and forgot `reference`
            dict(regression=0.100, product_target=1.0, reference=0.001),
            # anchor inside the band but looser than the product target
            dict(regression=2.0, product_target=1.0, reference=0.8),
            dict(regression=0.0, product_target=1.0, reference=0.001),
        ],
    )
    def test_a_budget_outside_the_ruling_cannot_be_constructed(self, kwargs):
        with pytest.raises(ValueError):
            LatencyBudget("probe", **kwargs)

    def test_a_throughput_budget_checks_the_band_the_other_way_round(self):
        # reference / regression, because a throughput floor DIVIDES. Using
        # the latency formula here would accept a floor 2.5x ABOVE the
        # measured rate, which fails every run, and reject the correct one.
        ThroughputBudget("probe", regression=80, product_target=50, reference=200)
        with pytest.raises(ValueError):
            ThroughputBudget("probe", regression=200, product_target=50, reference=80)

    @pytest.mark.parametrize("directory", GUARDED_DIRS, ids=lambda d: d.name)
    def test_the_table_names_tests_that_exist(self, directory):
        """A budget pointing at a renamed test is a budget nobody applies."""
        sources = _suite_sources(directory)
        missing = [
            budget.test
            for budget in _table_for(directory).values()
            if f"def {budget.test}(" not in sources
        ]
        assert not missing, missing

    @pytest.mark.parametrize("name", sorted(ALL_ANCHORS))
    def test_every_entry_is_actually_used_by_the_suite(self, name):
        """An unused entry is a number that looks enforced and is not."""
        directory = (
            BENCHMARK_DIR if name in budget_table.ALL_BUDGETS else PERFORMANCE_DIR
        )
        assert name in _suite_sources(
            directory
        ), f"{name} is in the {directory.name} table but no test there uses it"

    def test_no_test_carries_two_budgets_without_saying_why(self):
        """‼ Two budgets on one test are usually one budget twice.

        Review found two: `avg_operation_time` is exactly
        `avg_task_time / operations_per_task`, so a budget on each is the
        same constraint in different units, and the looser of the pair can
        never fire before the tighter one. Both shipped that way —
        `1.8e-5 x 20 = 3.6e-4` against `3.5e-4`, and `3.5e-4 x 20 = 0.007`
        against `0.007` — and no table check could see it, because nothing
        in the table says what statistic a row judges.

        This one cannot see it either. What it does is refuse the
        SITUATION silently: a test with two budgets has to name, here, the
        two independent timed windows they come from. A rescaling of one
        measurement has no honest entry to write.
        """
        by_test: dict = {}
        for name, budget in sorted(ALL_ANCHORS.items()):
            by_test.setdefault(budget.test, []).append(name)
        doubled = {test: names for test, names in by_test.items() if len(names) > 1}
        undeclared = sorted(set(doubled) - set(INDEPENDENT_MEASUREMENTS))
        assert not undeclared, (
            "these tests carry more than one budget and do not say which "
            "independent measurements they come from: " + ", ".join(undeclared)
        )
        stale = sorted(set(INDEPENDENT_MEASUREMENTS) - set(doubled))
        assert not stale, f"no longer carries two budgets: {stale}"

    @pytest.mark.parametrize("directory", GUARDED_DIRS, ids=lambda d: d.name)
    def test_every_timed_test_in_the_suite_owns_a_budget(self, directory):
        """‼ The direction the two tests above do NOT cover.

        They check the table against the suite. This checks the suite
        against the table: a test that takes a clock reading and is not
        named by any budget is a measurement nobody judges. It is the same
        question ``test_every_measured_test_reaches_a_helper`` asks of the
        call graph, asked of the data instead, and it is here because a
        budget can be deleted without deleting the test that used it.
        """
        named = {budget.test for budget in _table_for(directory).values()}
        unjudged = sorted(
            name
            for _path, name, module in _timed_tests(directory)
            if name not in named and (module, name) not in UNJUDGED_TIMED_TESTS
        )
        assert not unjudged, unjudged


class TestWhichNumberIsAsserted:
    """The #1556 split, driven through the helper rather than described."""

    _BUDGET = LatencyBudget(
        "probe", regression=0.050, product_target=0.500, reference=0.020
    )

    def test_a_pull_request_asserts_the_regression_anchor(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        assert_latency_within(0.049, self._BUDGET, "probe")
        with pytest.raises(AssertionError) as excinfo:
            # Comfortably inside the 500ms product target, and still red:
            # that is the whole point of re-anchoring.
            assert_latency_within(0.100, self._BUDGET, "probe")
        assert "50ms regression budget" in str(excinfo.value)

    def test_the_nightly_asserts_the_raw_product_target(self, monkeypatch):
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        # 100ms is over the regression anchor and under the product target.
        assert_latency_within(0.100, self._BUDGET, "probe")
        with pytest.raises(AssertionError) as excinfo:
            assert_latency_within(0.600, self._BUDGET, "probe")
        assert "500ms product target" in str(excinfo.value)

    def test_the_nightly_target_is_never_scaled(self, monkeypatch):
        # The scale is pinned at 1.0 in absolute mode, so a slow nightly
        # runner gets no relief — which is what makes it a wall-clock
        # question. Pin a 3x machine and check the product target holds.
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        monkeypatch.setattr(
            calibration, "_measured", calibration.CALIBRATION_REFERENCE_SECONDS * 3
        )
        with pytest.raises(AssertionError):
            assert_latency_within(0.600, self._BUDGET, "probe")

    def test_the_throughput_floor_splits_the_same_way(self, monkeypatch):
        budget = ThroughputBudget(
            "probe", regression=80, product_target=50, reference=200
        )
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError):
            assert_throughput_at_least(60.0, budget, "probe")  # under 80/s
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        assert_throughput_at_least(60.0, budget, "probe")  # over 50/s


class TestDiscrimination:
    """What each comparison can and cannot see, run rather than argued.

    #908's version of this class pinned a **30% regression** against a
    synthetic budget at 95% utilisation, because 95% was the suite's
    thinnest margin then. #1556 re-anchored every budget to 2-3x its
    measured cost, so no budget sits at 95% any more and 30% sensitivity is
    not a property the per-PR gate has. Below are the properties it does
    have — plus the one it gave up, asserted rather than left to be
    rediscovered.

    ``_ANCHOR`` is the shipped shape: a budget 2.5x its measured reference.
    """

    _ANCHOR = 2.5
    _REFERENCE = 0.020
    _BUDGET = LatencyBudget(
        "probe",
        regression=_REFERENCE * _ANCHOR,
        product_target=0.500,
        reference=_REFERENCE,
    )

    def test_a_uniform_1_3x_slowdown_fails_nothing(self, monkeypatch):
        # Unchanged from #908 and still the point: the whole process being
        # 1.3x slower cancels, because the budget moves with it.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.3)
        assert_latency_within(self._REFERENCE * 1.3, self._BUDGET, "uniform")

    def test_a_gross_regression_fails_its_own_path(self, monkeypatch):
        # 3x: an N+1, a lost index, a sync call on an async path. Measured
        # on the same 1.3x machine, so the two columns differ only in WHERE
        # the extra cost is.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.3)
        with pytest.raises(AssertionError):
            assert_latency_within(self._REFERENCE * 1.3 * 3.0, self._BUDGET, "gross")

    def test_a_30_percent_regression_does_NOT_fire_per_pull_request(self):
        # ‼ The trade #1556 made, asserted so it cannot be forgotten or
        # quietly claimed back. At 2-3x a path must get 100-200% slower to
        # trip. The alternative was a 1.3-1.5x threshold, and the calibrated
        # noise floor measured 1.07x typical / 1.33x worst — so that
        # threshold would flake, which is how #908 started.
        assert_latency_within(self._REFERENCE * 1.3, self._BUDGET, "drift")

    def test_the_absolute_comparison_keeps_its_30_percent_sensitivity(
        self, monkeypatch
    ):
        # Where 30% detection went: the nightly, against a raw target, on a
        # quiet runner. This pins the COMPARISON — a budget at 95% of its
        # product target does catch a 30% regression there, with no scale to
        # absorb it.
        #
        # ‼ It does not claim the shipped suite has that sensitivity. The
        # product targets sit 3.6x to 172x above measured cost, median 35x
        # (``product_target / reference`` per row of the table), so getting
        # 30% out of the nightly means tightening a product target — an
        # owner decision #908's ruling reserved and #1556 did not reopen.
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        tight = LatencyBudget(
            "probe", regression=0.100, product_target=0.200, reference=0.040
        )
        healthy = 0.190  # 95% of the product target
        assert_latency_within(healthy, tight, "at the wall")
        with pytest.raises(AssertionError):
            assert_latency_within(healthy * 1.3, tight, "at the wall")


# ------------------------------------------------- the terminal-summary hook


class _FakeReporter:
    def __init__(self):
        self.lines = []

    def write_sep(self, _char, title):
        self.lines.append(title)

    def write_line(self, line):
        self.lines.append(line)


class TestTerminalSummary:
    """Exercised through the hook, not by calling ``describe_calibration``.

    A hook that is registered on every `pytest tests/` run is worth driving
    the way pytest drives it — the laziness property and the nightly's
    number both live in the hook body, not in the function it calls.
    """

    def test_it_says_nothing_when_no_budget_was_asserted(self, monkeypatch):
        def _explode(*_a, **_k):
            raise AssertionError("measured for a summary nobody asked for")

        monkeypatch.setattr(calibration, "measure_calibration", _explode)
        reporter = _FakeReporter()
        root_conftest.pytest_terminal_summary(reporter, 0, None)
        assert reporter.lines == []

    def test_it_reports_the_scale_once_a_budget_was_asserted(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 2)
        assert_latency_within(0.001, _latency(1.0), "probe")
        reporter = _FakeReporter()
        root_conftest.pytest_terminal_summary(reporter, 0, None)
        assert any("2.00x" in line for line in reporter.lines), reporter.lines

    def test_this_modules_fixture_does_not_erase_an_earlier_scale(self):
        """‼ ``tests/performance/`` runs BEFORE this file in both gates.

        Its budgets are what set the flag the summary keys on, and
        this module's own fixture used to clear it on the way past — so
        the one line a reader needs to tell a slow runner from a
        regression never reached the job log of either required gate.
        Exercised through the fixture body, because the bug was in the
        fixture and not in anything it calls.
        """
        calibration.reset_calibration_cache()
        calibration.restore_calibration_state(
            (calibration.CALIBRATION_REFERENCE_SECONDS * 2, False)
        )
        assert_latency_within(0.001, _latency(1.0), "probe")
        before = calibration.calibration_state()
        assert before[1] is True, "precondition: a budget was asserted"

        body = getattr(_clean_calibration, "__wrapped__", _clean_calibration)
        generator = body()
        next(generator)
        assert (
            calibration.scale_was_used() is False
        ), "a test in this file must still start from a clean slate"
        with pytest.raises(StopIteration):
            next(generator)

        assert (
            calibration.calibration_state() == before
        ), "the fixture must put back the measurement AND the flag it found"

    def test_the_summary_still_reports_after_this_module_has_run(self):
        """The property above, read out where it is consumed."""
        calibration.reset_calibration_cache()
        calibration.restore_calibration_state(
            (calibration.CALIBRATION_REFERENCE_SECONDS * 2, False)
        )
        assert_latency_within(0.001, _latency(1.0), "probe")

        body = getattr(_clean_calibration, "__wrapped__", _clean_calibration)
        generator = body()
        next(generator)
        with pytest.raises(StopIteration):
            next(generator)

        reporter = _FakeReporter()
        root_conftest.pytest_terminal_summary(reporter, 0, None)
        assert any("2.00x" in line for line in reporter.lines), reporter.lines

    def test_absolute_mode_measures_for_the_report(self, monkeypatch):
        # The nightly path. `calibration_scale()` short-circuits before
        # measuring in absolute mode, so if the hook did not take the
        # measurement itself the one job that needs the number would ship
        # without it.
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        calls = []

        def _count(*_a, **_k):
            calls.append(1)
            return calibration.CALIBRATION_REFERENCE_SECONDS * 1.5

        monkeypatch.setattr(calibration, "measure_calibration", _count)
        assert_latency_within(0.001, _latency(1.0), "probe")
        assert calls == [], "the scale must not measure in absolute mode"

        reporter = _FakeReporter()
        root_conftest.pytest_terminal_summary(reporter, 0, None)
        assert calls == [1], "the summary must measure in absolute mode"
        body = " ".join(reporter.lines)
        assert "ABSOLUTE" in body and "1.50x" in body, body


# -------------------------------------------- one comparison site, scanned
#
# #1555 scanned for `assert <timing-word> <op> <numeric literal>` in
# `tests/benchmarks/`. #1557 pointed that scan at `tests/performance/` and
# it returned **zero** against **27** live hand-rolled comparisons, because
# its vocabulary (`latency`, `elapsed`, `p95`, `_ms`, …) was read off the
# benchmark suite's variable names and `tests/performance/` used none of
# them — `per_record_time`, `time_per_call`, `overhead_percentage`. A guard
# keyed on what a variable is CALLED is defeated by calling it something
# else, which is not a hypothetical: it had already happened, in the one
# directory that runs in both required gates.
#
# So the rule below keys on the SHAPE of the comparison and nothing else,
# and it is an over-approximation on purpose. Measured cost over the
# widened scope: **9 findings** — eight memory, object-count or GC
# assertions, plus one argument validation — listed with their reasons in
# `THRESHOLD_ALLOWLIST`, and the count asserted below. #1555's
# version expressed the same carve-out by simply never naming those
# variables, which is a silent allowlist; this one is a written list that
# fails when an entry stops matching anything.
#
# The shape rule is still a syntax rule, so `test_every_measured_test_
# reaches_a_helper` below asks the other question — does every test that
# takes a clock reading end up at a helper, by any route — which no
# spelling escapes.


#: Ordering operators. Equality is excluded on principle rather than to
#: quieten the scan: a budget is never "exactly", so `== 0.3` is always a
#: correctness assertion. (There are 7 in the guarded directories.)
ORDERING_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)

#: Spellings of the same comparison that are not an operator. None are
#: live anywhere in this repository (measured: 0 ``assertLess`` family, 0
#: ``operator.lt`` family across ``tests/`` and ``faultmaven/``), which is
#: exactly why they are covered — a shape with no live sites costs nothing
#: to guard and is the easy one to reach for once the obvious ones are
#: closed.
COMPARISON_CALLS = (
    "assertLess",
    "assertLessEqual",
    "assertGreater",
    "assertGreaterEqual",
)
#: ``operator.lt(elapsed, 0.2)``. Matched on the ``operator.`` prefix so a
#: local ``lt`` helper is not mistaken for one.
OPERATOR_COMPARISONS = ("lt", "le", "gt", "ge")

#: Comparisons the scan must NOT flag, each with the reason it is not a
#: duration. Keyed by (file, the comparison as `ast.unparse` writes it) so
#: the entry survives the line moving, and checked below for being live —
#: an allowlist entry that matches nothing is a suppression nobody reads.
#:
#: ‼ No entry here is a DURATION, and none may become one. Eight are
#: memory, an object count or a GC outcome — megabytes and object counts
#: do not scale with machine throughput, so the calibration must NOT be
#: applied to them — and the ninth is argument validation on a helper.
#: If a duration ever needs an entry, the right answer is a budget.
#: ‼ Keyed on the REPO-RELATIVE path, not the basename. Both guarded
#: directories contain a ``budgets.py``, and ``tests/performance/`` could
#: grow a ``conftest.py`` tomorrow — a basename key would then exempt the
#: same comparison in a file nobody reviewed.
_BENCH = "tests/benchmarks"
_PERF = "tests/performance"
THRESHOLD_ALLOWLIST = {
    (
        f"{_BENCH}/test_memory_usage.py",
        "rss_mb < 1500",
    ): "resident memory, megabytes",
    (
        f"{_BENCH}/test_memory_usage.py",
        "final_memory < 2000",
    ): "resident memory, megabytes",
    (
        f"{_BENCH}/test_memory_usage.py",
        "memory_delta < 100",
    ): "resident memory, megabytes",
    (
        f"{_BENCH}/conftest.py",
        "samples < 1",
    ): "argument validation on measure_min_latency, not a measurement",
    (
        f"{_PERF}/test_context_overhead.py",
        "cleanup_percentage > 80",
    ): "share of contexts the GC reclaimed; a lifetime property",
    (
        f"{_PERF}/test_context_overhead.py",
        "memory_per_worker < 100",
    ): "objects allocated per async worker; an object count",
    (
        f"{_PERF}/test_context_overhead.py",
        "memory_ratio <= count_ratio * 2",
    ): "memory growth against data growth; a ratio of object counts",
    (
        f"{_PERF}/test_logging_overhead.py",
        "object_growth < 1000",
    ): "objects surviving a create/destroy cycle; an object count",
    (
        f"{_PERF}/test_logging_overhead.py",
        "timing_count <= expected_combinations",
    ): "distinct (layer, operation) keys recorded; a count against 4 x 50",
}

#: Tests that take a clock reading and deliberately judge nothing, with the
#: reason. #1557 deleted these three comparisons rather than re-anchoring
#: them: each subtracted a NOMINAL sleep total from a measured one and
#: called the difference overhead, and a bare loop of the same sleeps with
#: no instrumentation at all accounts for most of the result. See
#: `tests/performance/budgets.py` for the measurement.
UNJUDGED_TIMED_TESTS = {
    (
        f"{_PERF}/test_context_overhead.py",
        "test_async_context_propagation_overhead",
    ): "expected work computed serially for concurrent tasks; the figure "
    "came out at -1250% and the comparison could not fail",
    (
        f"{_PERF}/test_logging_overhead.py",
        "test_operation_context_manager_overhead",
    ): "74% of the reported overhead is asyncio.sleep granularity "
    "(19.4ms of 26.3ms, measured)",
    (
        f"{_PERF}/test_logging_overhead.py",
        "test_high_frequency_operations",
    ): "92% of the reported overhead is asyncio.sleep granularity "
    "(97.3ms of 106ms, measured)",
}

#: Tests that carry more than one budget, and the independent timed
#: windows each pair comes from. ‼ "Independent" means separately timed,
#: not merely differently named: a per-operation figure computed by
#: dividing a per-task one is the SAME measurement, and a budget on each
#: is the same constraint twice with the looser half unreachable. Two of
#: those shipped on this branch and were caught in review.
INDEPENDENT_MEASUREMENTS = {
    "test_context_variable_access_speed": "two timed loops, get and set",
    "test_context_copying_performance": (
        "two timed loops, copy_context() and Context.run()"
    ),
    "test_context_isolation_performance": (
        "the wall clock over the gather, and the spread between the "
        "per-task means each task measured for itself"
    ),
}

#: One planted violation per SHAPE a threshold can be written in, each
#: written so exactly one shape matches it. Two of these are live idioms in
#: this repository rather than hypotheses — measured across `tests/`:
#: `named_bound` has **333** live sites and `chained` **63**, which is why
#: #1556's ruling called them out by name. The other four have 0-1 and are
#: covered because they are free.
PLANTED_PER_SHAPE = {
    "right_literal": "assert elapsed < 0.2",
    "left_literal": "assert 0.2 > elapsed",
    "named_bound": "assert elapsed < BUDGET_SECONDS",
    "chained": "assert 0.0 < elapsed < 0.2",
    "negated": "assert not elapsed > 0.2",
    "unittest": "self.assertLess(elapsed, 0.2)",
    "operator_call": "assert operator.lt(elapsed, 0.2)",
    "if_fail": "if elapsed > 0.2:\n        pytest.fail('slow')",
    "if_raise": "if elapsed > 0.2:\n        raise AssertionError('slow')",
    # #1579: four shapes the top-level check could not see. The first is
    # live outside these directories (``assert all(t < 2.0 for t in ...)``
    # in tests/infrastructure); none is live inside them, so covering all
    # four cost nothing.
    "inside_all": "assert all(t < 0.2 for t in times)",
    "inside_a_boolean": "assert ok and elapsed < 0.2",
    "assert_true": "self.assertTrue(elapsed < 0.2)",
    "compared_to_true": "assert (elapsed < 0.2) is True",
    "if_boolean_raise": "if ok and elapsed > 0.2:\n        raise AssertionError('slow')",
}

#: The same corpus #1555 planted, kept as a regression control: whatever
#: the rule becomes, it must still catch every comparison the vocabulary
#: version caught. (`p95` earned its place there by being dead weight; the
#: shape rule does not care about names, so these are now checked as a set
#: rather than one per token.)
PLANTED_VOCABULARY = (
    "assert measured.best < 0.2",
    "assert measured.median < 0.2",
    "assert measured.worst < 0.2",
    "assert p50 < 200",
    "assert p95 < 200",
    "assert p99 < 200",
    "assert throughput > 50",
    "assert latency < 0.2",
    "assert elapsed < 0.2",
    "assert duration < 0.2",
    "assert stats['total_ms'] < 200",
    "assert budget_seconds < 0.2",
    "assert items_per_second > 50",
)

#: Assertions the scan must NOT flag, chosen to pin the two carve-outs the
#: rule makes: equality is never a threshold, and a comparison whose only
#: numeric literal is 0 is an existence check.
PLANTED_NEGATIVES = (
    "assert len(results) == 100",
    "assert threshold == 0.3",
    "assert operations_logged > 0",
    "assert len(metric_calls) > 0",
    "assert elapsed >= 0",
)


def _numeric_literals(node: ast.AST) -> List[float]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, (int, float))
        and not isinstance(child.value, bool)
    ]


def _is_threshold_comparison(node: ast.Compare) -> bool:
    """Is this comparison judging a measurement against a bound?

    Two things are NOT, and both are excluded by a property rather than by
    a name:

    * an equality — a budget is never "exactly";
    * a comparison whose only numeric literal is ``0`` — an existence
      check. Note the wording: ``elapsed - 0.2 > 0`` carries a 0.2 as well
      and is caught, which is the form that would otherwise smuggle a
      budget past the exclusion.

    A comparison with NO literal at all IS caught, because that is the
    named-bound idiom (``assert elapsed < BUDGET_SECONDS``) — 333 live
    sites across ``tests/`` and the most obvious way to move a threshold
    out of the scan's reach.
    """
    if not any(isinstance(op, ORDERING_OPS) for op in node.ops):
        return False
    literals = _numeric_literals(node)
    return not (literals and all(value == 0 for value in literals))


def _threshold_comparisons(source: str, filename: str) -> List[Tuple[str, str, int]]:
    """Every hand-rolled threshold comparison in ``source``.

    Returns (filename, enclosing function, source text) triples. The third
    element is the comparison as ``ast.unparse`` writes it rather than a
    line number, because that is what ``THRESHOLD_ALLOWLIST`` is keyed on
    and a line number moves when anything above it does.
    """
    tree = ast.parse(source, filename=filename)
    enclosing: List[str] = []
    found: List[Tuple[str, str, int]] = []

    def _record(node: ast.AST, text: str) -> None:
        found.append((filename, enclosing[-1] if enclosing else "", text))

    class _Walk(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802 - ast API
            enclosing.append(node.name)
            self.generic_visit(node)
            enclosing.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815 - ast API

        def visit_Assert(self, node):  # noqa: N802 - ast API
            # EVERY comparison inside the assertion, not only the one at its
            # top (#1579): ``assert all(t < 0.2 for t in ts)``, ``assert ok
            # and t < 0.2`` and ``assert (t < 0.2) is True`` were each
            # invisible to the top-level check. Measured cost of looking
            # inside, over both directories: zero new findings.
            for child in ast.walk(node.test):
                if isinstance(child, ast.Compare) and _is_threshold_comparison(child):
                    _record(node, ast.unparse(child))
            self.generic_visit(node)

        def visit_Call(self, node):  # noqa: N802 - ast API
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr in COMPARISON_CALLS:
                    _record(node, ast.unparse(node))
                elif func.attr.startswith("assert"):
                    # ``self.assertTrue(t < 0.2)``: an assert with the
                    # operator moved into an argument.
                    for arg in node.args:
                        for child in ast.walk(arg):
                            if isinstance(
                                child, ast.Compare
                            ) and _is_threshold_comparison(child):
                                _record(node, ast.unparse(child))
                elif (
                    func.attr in OPERATOR_COMPARISONS
                    and ast.unparse(func).startswith("operator.")
                    and _numeric_literals(node)
                ):
                    _record(node, ast.unparse(node))
            self.generic_visit(node)

        def visit_If(self, node):  # noqa: N802 - ast API
            # `if elapsed > 0.2: pytest.fail(...)` and `... : raise
            # AssertionError(...)` are assertions with the word `assert`
            # taken out of them. The second is a live idiom — 10 sites
            # across `tests/`, one of them in `tests/benchmarks/conftest.py`
            # — so covering it costs exactly one allowlist entry, which is
            # cheaper than leaving the shape open.
            compares = [
                child
                for child in ast.walk(node.test)
                if isinstance(child, ast.Compare) and _is_threshold_comparison(child)
            ]
            if compares and any(
                isinstance(child, ast.Raise)
                or (isinstance(child, ast.Call) and "fail" in ast.unparse(child.func))
                for child in ast.walk(node)
            ):
                for compare in compares:
                    _record(node, ast.unparse(compare))
            self.generic_visit(node)

    _Walk().visit(tree)
    return found


def _planted_source(statements=None) -> str:
    """A synthetic module carrying one violation per shape, plus negatives."""
    chosen = (
        list(PLANTED_PER_SHAPE.values()) if statements is None else list(statements)
    )
    body = chosen + list(PLANTED_NEGATIVES)
    return "def test_x(self):\n" + "".join(f"    {line}\n" for line in body)


def _key(path: Path) -> str:
    """This file's identity in the allowlists: its repo-relative path.

    A path outside the repository — the scratch tree the recursion probe
    below builds — keys on itself. It can never match an allowlist entry,
    which is what that probe wants.
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _modules_in(directory: Path) -> List[Path]:
    """‼ RECURSIVE on purpose.

    A non-recursive ``glob`` was the first draft here, and a new
    ``tests/performance/sub/test_x.py`` walked past both rules with nothing
    to show for it. The detector's input is where these go wrong.
    """
    return sorted(directory.rglob("*.py"))


def _scan_directory(directory: Path) -> List[Tuple[str, str, str]]:
    """Every threshold comparison in ``directory``, allowlist NOT applied."""
    hits: List[Tuple[str, str, str]] = []
    for path in _modules_in(directory):
        hits.extend(_threshold_comparisons(path.read_text(), _key(path)))
    return hits


class TestOneComparisonSite:
    def test_the_scan_finds_every_planted_shape(self):
        """Positive control: a drifted rule reads like a clean tree."""
        hits = _threshold_comparisons(_planted_source(), "planted.py")
        assert len(hits) == len(PLANTED_PER_SHAPE)
        assert [h[2] for h in hits] != []

    @pytest.mark.parametrize("shape", sorted(PLANTED_PER_SHAPE))
    def test_every_shape_is_caught_on_its_own(self, shape):
        """Each shape, alone, with the negatives around it.

        Parametrized rather than counted in one pass because a shape that
        stopped being caught would otherwise be masked by the next one —
        which is how `p95` sat dead in the vocabulary version.
        """
        source = _planted_source([PLANTED_PER_SHAPE[shape]])
        hits = _threshold_comparisons(source, "planted.py")
        assert len(hits) == 1, f"{shape!r} is not caught: {PLANTED_PER_SHAPE[shape]!r}"

    def test_it_still_catches_everything_the_vocabulary_version_did(self):
        """#1555's corpus, as a regression control on the rule change."""
        source = _planted_source(PLANTED_VOCABULARY)
        hits = _threshold_comparisons(source, "planted.py")
        assert len(hits) == len(PLANTED_VOCABULARY)

    def test_correctness_assertions_are_never_flagged(self):
        source = "def test_x():\n" + "".join(
            f"    {line}\n" for line in PLANTED_NEGATIVES
        )
        assert _threshold_comparisons(source, "planted.py") == []

    def test_a_zero_bound_hiding_a_budget_is_still_caught(self):
        """The `> 0` carve-out must not be a door.

        `elapsed - 0.2 > 0` is `elapsed < 0.2` with the bound moved to the
        left, and the exclusion is written as "every literal is 0" rather
        than "the right-hand literal is 0" precisely so it does not let
        this through.
        """
        source = "def test_x():\n    assert elapsed - 0.2 > 0\n"
        assert len(_threshold_comparisons(source, "planted.py")) == 1

    @pytest.mark.parametrize("directory", GUARDED_DIRS, ids=lambda d: d.name)
    def test_the_scan_looks_at_every_module_in_scope(self, directory):
        """A guard that watched the wrong directory would be green forever."""
        modules = sorted(path.name for path in directory.glob("*.py"))
        assert "budgets.py" in modules, directory
        assert len([m for m in modules if m.startswith("test_")]) >= 2, directory

    def test_the_performance_suite_is_in_scope_at_all(self):
        """‼ The #1557 defect itself, as a test.

        `tests/performance/` is collected by both required CI gates and
        `tests/benchmarks/` is not, so this is the directory where a
        hand-rolled threshold does damage. It was outside the scan for the
        whole of #1555.
        """
        assert PERFORMANCE_DIR in GUARDED_DIRS
        assert PERFORMANCE_DIR.is_dir()

    @pytest.mark.parametrize("directory", GUARDED_DIRS, ids=lambda d: d.name)
    def test_no_threshold_is_compared_outside_the_helpers(self, directory):
        violations = [
            f"{filename}: {text}  (in {func or '<module>'})"
            for filename, func, text in _scan_directory(directory)
            if (filename, text) not in THRESHOLD_ALLOWLIST
        ]
        assert not violations, (
            "latency/throughput thresholds must go through "
            "assert_latency_within / assert_throughput_at_least so the #908 "
            "calibration applies and the #1556 split holds; found: "
            + ", ".join(violations)
        )

    def test_every_allowlist_entry_is_live(self):
        """An entry that matches nothing is a suppression nobody reads.

        It is also how an allowlist outlives the code it was written for:
        the comparison is deleted, the entry stays, and the next reader
        takes it for a statement about the tree.
        """
        seen = {
            (filename, text)
            for directory in GUARDED_DIRS
            for filename, _func, text in _scan_directory(directory)
        }
        dead = sorted(key for key in THRESHOLD_ALLOWLIST if key not in seen)
        assert not dead, dead

    def test_the_allowlist_cost_is_what_was_measured(self):
        """#1557 counted the over-approximation rather than tuning it away.

        Nine findings over the widened scope: eight memory, object-count
        or GC assertions, plus one argument validation. The number is
        asserted so that widening the allowlist is a visible act rather
        than a quiet one.
        """
        assert len(THRESHOLD_ALLOWLIST) == 9

    def test_the_scan_reads_subdirectories_too(self, tmp_path):
        """A nested module is where the next one of these will land.

        The first draft of ``_modules_in`` used a non-recursive ``glob``,
        and a threshold in ``tests/performance/sub/test_x.py`` walked past
        both checks with nothing to show for it.

        ‼ Built in ``tmp_path``, NOT in the directory under test. The
        first version of this test planted the violating module inside
        ``tests/performance/`` while the suite was running: under
        ``-n auto`` — which ``scripts/tests.py``'s ``ci`` and ``ci-full``
        modes pass, and whose default ``--dist load`` splits within a file
        — another worker scanning the same directory would see it and fail
        for real. A kill between the write and the cleanup left it behind
        permanently. Reproduced in review. The scan takes a directory, so
        there is no reason to aim it at a live one.
        """
        nested = tmp_path / "sub" / "deeper"
        nested.mkdir(parents=True)
        (nested / "test_probe.py").write_text("def test_x():\n    assert e < 0.2\n")
        (tmp_path / "test_top.py").write_text("def test_y():\n    assert f < 0.3\n")

        found = {text for _f, _fn, text in _scan_directory(tmp_path)}
        assert "f < 0.3" in found, "the scan lost the top-level module"
        assert "e < 0.2" in found, "the scan did not recurse"


# ------------------------------------------- one comparison site, reachable
#
# The scan above is a syntax rule, and a syntax rule is a list of spellings
# somebody has thought of. This one is not: it asks whether a test that
# takes a clock reading ends up at a helper, following calls, so a new
# spelling, a private wrapper or a comparison written with no `assert` at
# all still has to answer for itself.
#
# It is also the check that would have caught #1555's own blind spot from
# the other side: `tests/performance/`'s 27 comparisons were invisible to
# the vocabulary, but all 26 of its timed tests would have shown up here on
# day one.

CLOCK_FUNCTIONS = frozenset(
    {"perf_counter", "perf_counter_ns", "monotonic", "monotonic_ns", "process_time"}
)
HELPER_FUNCTIONS = frozenset({"assert_latency_within", "assert_throughput_at_least"})


def _call_graph(directories) -> Tuple[dict, list]:
    """Functions in ``directories``, what each calls, and the test ones.

    Returns ``({(module, name): {called names}}, [(path, name, module)])``.
    ``time.time()`` is counted as a clock alongside `perf_counter`, because
    a threshold written against it is no less a threshold for being badly
    measured.
    """
    definitions: dict = {}
    tests: list = []
    for directory in directories:
        for path in _modules_in(directory):
            tree = ast.parse(path.read_text(), filename=_key(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = set()
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    func = child.func
                    if isinstance(func, ast.Attribute):
                        calls.add(func.attr)
                        if (
                            isinstance(func.value, ast.Name)
                            and func.value.id == "time"
                            and func.attr == "time"
                        ):
                            calls.add("perf_counter")
                    elif isinstance(func, ast.Name):
                        calls.add(func.id)
                definitions[(_key(path), node.name)] = calls
                if node.name.startswith("test_"):
                    tests.append((path, node.name, _key(path)))
    return definitions, tests


def _reaches(definitions, start, targets, on_ambiguous: bool) -> bool:
    """Does ``start`` reach any of ``targets`` through the call graph?

    ``on_ambiguous`` is the answer when a called name is defined in more
    than one scanned module and cannot be resolved. Both callers pass the
    value that produces MORE findings, so an unresolvable name never
    silences the guard.
    """
    by_name: dict = {}
    for module, name in definitions:
        by_name.setdefault(name, []).append(module)
    seen = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for called in definitions.get(current, ()):
            if called in targets:
                return True
            if (current[0], called) in definitions:
                stack.append((current[0], called))
            elif len(by_name.get(called, [])) == 1:
                stack.append((by_name[called][0], called))
            elif called in by_name:
                return on_ambiguous
    return False


def _timed_tests(directory: Path):
    """(path, test name, module) for every test there that reads a clock."""
    definitions, tests = _call_graph((directory, WALLCLOCK_DIR))
    return [
        (path, name, module)
        for path, name, module in tests
        if _reaches(definitions, (module, name), CLOCK_FUNCTIONS, True)
    ]


class TestEveryMeasurementIsJudged:
    @pytest.mark.parametrize("directory", GUARDED_DIRS, ids=lambda d: d.name)
    def test_every_measured_test_reaches_a_helper(self, directory):
        definitions, _tests = _call_graph((directory, WALLCLOCK_DIR))
        unjudged = [
            f"{module}::{name}"
            for _path, name, module in _timed_tests(directory)
            if not _reaches(definitions, (module, name), HELPER_FUNCTIONS, False)
            and (module, name) not in UNJUDGED_TIMED_TESTS
        ]
        assert not unjudged, (
            "these tests take a clock reading and judge it by some route "
            "other than the helpers, or by no route at all: " + ", ".join(unjudged)
        )

    def test_the_search_follows_a_wrapper(self):
        """‼ The property the first draft of this check got wrong.

        `tests/benchmarks/test_case_service_operations.py` calls
        `report_p95`, which calls `assert_latency_within`. A one-level
        search reported it as unjudged — a false positive that, had it been
        allowlisted instead of read, would have left a real benchmark
        exempt from the guard forever.
        """
        definitions = {
            ("m.py", "test_x"): {"report_p95"},
            ("m.py", "report_p95"): {"assert_latency_within"},
        }
        assert _reaches(definitions, ("m.py", "test_x"), HELPER_FUNCTIONS, False)

    def test_the_search_does_not_invent_a_route(self):
        definitions = {
            ("m.py", "test_x"): {"report_p95"},
            ("m.py", "report_p95"): {"print"},
        }
        assert not _reaches(definitions, ("m.py", "test_x"), HELPER_FUNCTIONS, False)

    def test_it_finds_the_timed_tests_at_all(self):
        """A reachability check that resolved nothing would pass silently."""
        for directory in GUARDED_DIRS:
            assert len(_timed_tests(directory)) >= 10, directory

    def test_every_unjudged_entry_is_live(self):
        live = {
            (module, name)
            for directory in GUARDED_DIRS
            for _path, name, module in _timed_tests(directory)
        }
        dead = sorted(key for key in UNJUDGED_TIMED_TESTS if key not in live)
        assert not dead, dead


# ------------------------------------ the rest of tests/, in the gates (#1579)
#
# Everything above watches ``tests/benchmarks/`` and ``tests/performance/``.
# Both required gates run ``pytest tests/`` and deselect only the
# ``benchmark`` marker, so every OTHER directory under ``tests/`` runs there
# too — and #1579 counted 50 hand-rolled wall-clock thresholds in 18 files of
# it. After #1651 put both gates under ``pytest-xdist`` two of them went red
# on unrelated commits within hours (``2.98 < 2.0``; ``3.7x`` against 3.0).
#
# The rule used above cannot simply be pointed at the rest of ``tests/``.
# It flags every ordering comparison in a directory, which is right where
# every test is a timing test and would flag thousands of ``len(x) > 0``
# elsewhere. So this rule scopes itself to the code that MEASURES:
#
# * a function MEASURES when it subtracts two clock readings — or compares
#   two, the ``deadline = monotonic() + 0.5 ... monotonic() < deadline``
#   idiom — or calls ``timeit``. A clock reading is a ``time`` clock
#   (aliased or not, imported by name or not), an event loop's ``.time()``,
#   or a call to a function that returns one. A single reading used as a
#   TIMESTAMP (``time.time() + 60``, ``now - 55.0``) is not a measurement,
#   which is what keeps the 100-odd timestamp-only tests out of scope.
# * a test is TIMED when it reaches a measuring function through the call
#   graph, which resolves same-module names, ``from x import f``, module
#   aliases and package re-exports across the whole of ``tests/``.
#
# and asks the two questions the rules above ask, of that scope:
#
# * **no threshold** — every ordering comparison inside a timed test or a
#   measuring function, ANYWHERE in its body (not only at the top of an
#   ``assert``: ``assert all(t < 0.2 ...)``, ``ok = e < 0.2``,
#   ``assert a and e < 0.2`` are all live shapes), is a finding unless
#   ``TREE_THRESHOLD_ALLOWLIST`` says why it is not a duration budget;
# * **judged by a helper** — every timed test must reach
#   ``assert_latency_within``, ``assert_throughput_at_least`` or
#   ``assert_linear_growth``, or be named in ``TREE_UNJUDGED_TIMED_TESTS``.
#
# Measured, counted rather than tuned. This scanner, run over origin/main's
# ``tests/`` as #1579 found it: 96 timed tests, 72 comparisons in 17 files
# inside the timed scope, 64 timed tests reaching no judge. After the
# conversions: 56 timed tests, and the only findings left are the nine
# ``TREE_THRESHOLD_ALLOWLIST`` entries and fourteen
# ``TREE_UNJUDGED_TIMED_TESTS`` below, each read and given its reason.

TESTS_ROOT = REPO_ROOT / "tests"

#: Directories the tree-wide rule does not scan for violations, each with the
#: reason. Everything else under ``tests/`` is in scope, and
#: ``test_the_three_scopes_partition_tests`` fails if a file falls through.
TREE_EXCLUDED_DIRS = {
    BENCHMARK_DIR: "stricter rule above: every comparison in the directory",
    PERFORMANCE_DIR: "stricter rule above: every comparison in the directory",
    WALLCLOCK_DIR: "the helpers themselves; scanned for the call graph only",
}

#: Attributes of the ``time`` module that read a clock.
#:
#: ‼ ``datetime.now()`` / ``utcnow()`` are deliberately NOT here, and that is
#: a measured decision rather than an oversight. Counting them costs 38 new
#: findings and 33 unjudged tests on the current tree, every one read: brackets
#: (``before <= stamped <= after``), orderings of stored timestamps, and JWT
#: expiry windows — none a budget on how long code took. A duration measured
#: with ``datetime`` has 0 live sites. If one appears, it is the known miss.
TIME_MODULE_CLOCKS = frozenset(
    {
        "perf_counter",
        "perf_counter_ns",
        "monotonic",
        "monotonic_ns",
        "time",
        "time_ns",
        "process_time",
        "process_time_ns",
        "thread_time",
        "thread_time_ns",
    }
)
#: The same, minus the two whose NAME is too common to trust on any receiver
#: (``histogram.time()``, ``loop.time()`` — the second handled below).
ANY_RECEIVER_CLOCKS = TIME_MODULE_CLOCKS - {"time", "time_ns"}
#: Calls whose result is an event loop, whose ``.time()`` is a clock.
LOOP_FACTORIES = frozenset({"get_event_loop", "get_running_loop", "new_event_loop"})
#: ‼ Clocks that are NOT wall clocks: ``virtual_now`` reads a
#: ``VirtualTimeLoop`` and raises on any other loop
#: (``test_virtual_now_refuses_a_real_loop`` holds it to that), so an elapsed
#: figure built from it is the same on every machine and is not a duration
#: this rule is about. The call graph does not descend into it.
VIRTUAL_CLOCKS = frozenset({"virtual_now"})
#: The judges a timed test may reach.
TREE_JUDGES = HELPER_FUNCTIONS | {"assert_linear_growth"}


def _tree_scope_modules(root: Path = TESTS_ROOT) -> List[Path]:
    """Every module the tree-wide rule scans: ``tests/`` minus the exclusions."""
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
        and not any(path.is_relative_to(d) for d in TREE_EXCLUDED_DIRS)
    ]


def _dotted(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class _Facts:
    """One function body: its calls and its loop-bound names."""

    __slots__ = ("node", "calls", "loop_names")

    def __init__(self, node: ast.AST) -> None:
        self.node = node
        self.calls: List[ast.Call] = []
        self.loop_names: set = set()


def _one_pass(tree: ast.AST):
    """Every import and every function's facts, in ONE walk of the module.

    A call inside a nested function belongs to every function around it —
    exactly what walking each function separately would say — so the walk
    keeps the stack of enclosing functions and credits all of them. Walking
    each function on its own re-walked every nested body once per level and
    was two thirds of this scan's cost.
    """
    imports: List[ast.AST] = []
    functions: List[_Facts] = []
    enclosing: List[_Facts] = []
    pending: list = [(tree, False)]
    while pending:
        node, leaving = pending.pop()
        if leaving:
            enclosing.pop()
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            facts = _Facts(node)
            functions.append(facts)
            enclosing.append(facts)
            pending.append((node, True))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, ast.Call):
            for facts in enclosing:
                facts.calls.append(node)
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _called_name(node.value) in LOOP_FACTORIES
        ):
            bound = {t.id for t in node.targets if isinstance(t, ast.Name)}
            for facts in enclosing:
                facts.loop_names.update(bound)
        pending.extend((child, False) for child in ast.iter_child_nodes(node))
    return imports, functions


class _TreeIndex:
    """Functions, calls and clock reads across a set of modules.

    ``root`` is what dotted module names are computed against: the
    repository for the real tree, a scratch directory for planted ones.
    Every function body is walked once, in ``_Facts``; the taint pass runs
    only on the few hundred functions that can reach a clock at all.
    """

    def __init__(self, paths: List[Path], root: Path) -> None:
        self.root = root
        self.by_dotted = {_dotted(p, root): _key(p) for p in paths}
        self.functions: dict = {}
        self.module_names: dict = {}
        for path in paths:
            key = _key(path)
            with warnings.catch_warnings():
                # A module with an invalid escape in a docstring warns on
                # parse; that is its business, not this scan's.
                warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
                tree = ast.parse(path.read_text(), filename=key)
            imports, functions = _one_pass(tree)
            self.module_names[key] = self._names(path, imports)
            for facts in functions:
                self.functions.setdefault((key, facts.node.name), []).append(facts)
        self.conftests = {
            key: self._conftest_ancestry(
                Path(key) if Path(key).is_absolute() else root / key
            )
            for key in self.module_names
        }
        self.reads_clock = set()
        self.calls: dict = {}
        for fn, bodies in self.functions.items():
            targets = set()
            for facts in bodies:
                # A parameter is how pytest hands a test its fixtures, so a
                # parameter NAMED like a function is an edge to it: the test
                # that takes ``stopwatch`` runs ``stopwatch`` before its body.
                args = facts.node.args
                for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                    targets.add((fn[0], arg.arg))
                for call in facts.calls:
                    if self._is_clock_call(call, fn[0], facts.loop_names):
                        self.reads_clock.add(fn)
                        continue
                    target = self._target(call, fn[0])
                    if target is not None:
                        targets.add(target)
            self.calls[fn] = targets
        near_a_clock = {
            fn
            for fn, targets in self.calls.items()
            if fn in self.reads_clock
            or any(self.resolve(t) in self.reads_clock for t in targets)
        }
        self.measuring = {fn for fn in near_a_clock if self._measures(fn)}

    # -- names ---------------------------------------------------------------

    def _names(self, path: Path, imports: List[ast.AST]) -> dict:
        """time/timeit aliases, clock names, imported functions, module aliases."""
        names = {
            "time": set(),
            "clock": set(),
            "timeit": set(),
            "imported": {},
            "modules": {},
        }
        package = _dotted(path, self.root).split(".")
        if path.name != "__init__.py":
            package = package[:-1]
        for node in imports:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "time":
                        names["time"].add(local)
                    elif alias.name == "timeit":
                        names["timeit"].add(local)
                    elif alias.name in self.by_dotted and alias.asname:
                        names["modules"][local] = self.by_dotted[alias.name]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = package[: len(package) - (node.level - 1)]
                    base = ".".join(anchor + ([base] if base else []))
                for alias in node.names:
                    local = alias.asname or alias.name
                    if base == "time" and alias.name in TIME_MODULE_CLOCKS:
                        names["clock"].add(local)
                    elif base == "timeit":
                        names["timeit"].add(local)
                    elif f"{base}.{alias.name}" in self.by_dotted:
                        names["modules"][local] = self.by_dotted[f"{base}.{alias.name}"]
                    elif base in self.by_dotted:
                        names["imported"][local] = (self.by_dotted[base], alias.name)
        return names

    def _is_clock_call(self, call: ast.Call, module: str, loop_names: set) -> bool:
        names = self.module_names[module]
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in names["clock"] or func.id in names["timeit"]
        if not isinstance(func, ast.Attribute):
            return False
        receiver = func.value
        if isinstance(receiver, ast.Name):
            if receiver.id in names["time"] and func.attr in TIME_MODULE_CLOCKS:
                return True
            if receiver.id in names["timeit"]:
                return True
        if func.attr in ANY_RECEIVER_CLOCKS:
            return True
        if func.attr == "time":
            if isinstance(receiver, ast.Call) and _called_name(receiver) in (
                LOOP_FACTORIES
            ):
                return True
            if isinstance(receiver, ast.Name) and receiver.id in loop_names:
                return True
        return False

    def _target(self, call: ast.Call, module: str):
        """The (module, name) a call reaches, as far as syntax can say."""
        names = self.module_names[module]
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in VIRTUAL_CLOCKS:
                return None
            return names["imported"].get(func.id, (module, func.id))
        if isinstance(func, ast.Attribute):
            if func.attr in VIRTUAL_CLOCKS:
                return None
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in names["modules"]:
                return (names["modules"][receiver.id], func.attr)
            return (module, func.attr)
        return None

    def _conftest_ancestry(self, path: Path) -> List[str]:
        """The ``conftest.py`` modules pytest would look in for ``path``'s fixtures."""
        found = []
        directory = path.parent
        while directory.is_relative_to(self.root):
            conftest = directory / "conftest.py"
            if _key(conftest) in self.module_names:
                found.append(_key(conftest))
            if directory == self.root:
                break
            directory = directory.parent
        return found

    def resolve(self, target, depth: int = 0):
        """Follow a target to a defined function.

        Through re-exports, and — the way pytest resolves a fixture — through
        the ``conftest.py`` files above the calling module, nearest first.
        """
        if target in self.functions:
            return target
        module, name = target
        imported = self.module_names.get(module, {}).get("imported", {})
        if depth < 5 and name in imported:
            return self.resolve(imported[name], depth + 1)
        for conftest in self.conftests.get(module, ()):
            if (conftest, name) in self.functions:
                return (conftest, name)
        return None

    # -- measurement ---------------------------------------------------------

    def _tainted(self, expr: ast.AST, tainted: set, module: str, facts) -> bool:
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and node.id in tainted:
                return True
            if isinstance(node, ast.Call):
                if self._is_clock_call(node, module, facts.loop_names):
                    return True
                target = self._target(node, module)
                if target is not None and self.resolve(target) in self.reads_clock:
                    return True
        return False

    def _measures(self, fn) -> bool:
        module = fn[0]
        for facts in self.functions[fn]:
            scope = facts.node
            tainted: set = set()
            bindings = list(_bindings(scope))
            changed = True
            while changed:
                changed = False
                for targets, value in bindings:
                    if value is None or not self._tainted(
                        value, tainted, module, facts
                    ):
                        continue
                    for target in targets:
                        for name in ast.walk(target):
                            if isinstance(name, ast.Name) and name.id not in tainted:
                                tainted.add(name.id)
                                changed = True

            def is_tainted(expr):
                return self._tainted(expr, tainted, module, facts)

            for node in ast.walk(scope):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                    if is_tainted(node.left) and is_tainted(node.right):
                        return True
                elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Sub):
                    if is_tainted(node.target) and is_tainted(node.value):
                        return True
                elif isinstance(node, ast.Compare) and any(
                    isinstance(op, ORDERING_OPS) for op in node.ops
                ):
                    sides = [node.left, *node.comparators]
                    if sum(1 for side in sides if is_tainted(side)) >= 2:
                        return True
            if any(
                isinstance(getattr(c.func, "value", None), ast.Name)
                and c.func.value.id in self.module_names[module]["timeit"]
                for c in facts.calls
            ):
                return True
        return False

    def reaches(self, start, found) -> bool:
        """Does ``start`` reach a function ``found`` accepts, following calls?"""
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if found(current):
                return True
            for target in self.calls.get(current, ()):
                if found(target):
                    return True
                resolved = self.resolve(target)
                if resolved is not None:
                    stack.append(resolved)
        return False


def _called_name(call: ast.Call) -> str:
    func = call.func
    return getattr(func, "attr", None) or getattr(func, "id", "")


def _bindings(scope: ast.AST):
    """(targets, value) for every way ``scope`` binds a name."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            yield [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            yield [node.target], node.iter
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            yield [node.optional_vars], node.context_expr


def _ordering_findings(node: ast.AST) -> List[str]:
    """Every threshold-shaped comparison anywhere in ``node``'s body.

    The same two carve-outs as the directory rule (equality is never a
    budget; a comparison whose only literal is 0 is an existence check),
    applied to EVERY ordering comparison rather than only the one at the top
    of an ``assert``, plus the ``assertLess`` and ``operator.lt`` spellings.
    """
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Compare) and _is_threshold_comparison(child):
            found.append(ast.unparse(child))
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in COMPARISON_CALLS or (
                child.func.attr in OPERATOR_COMPARISONS
                and ast.unparse(child.func).startswith("operator.")
                and _numeric_literals(child)
            ):
                found.append(ast.unparse(child))
    return found


def _tree_scan(scope: List[Path], context: List[Path], root: Path = REPO_ROOT):
    """(timed tests, findings, unjudged) for the modules in ``scope``.

    ``context`` is indexed for the call graph as well — the helpers in
    ``tests/wallclock/`` and anything the scope imports from the excluded
    directories — but nothing in it is reported.
    """
    index = _TreeIndex(sorted(set(scope) | set(context)), root)
    in_scope = {_key(p) for p in scope}
    timed = sorted(
        fn
        for fn in index.functions
        if fn[0] in in_scope
        and fn[1].startswith("test_")
        and index.reaches(fn, lambda f: f in index.measuring)
    )
    scanned = set(timed) | {fn for fn in index.measuring if fn[0] in in_scope}
    findings = sorted(
        {
            (fn[0], fn[1], text)
            for fn in scanned
            for facts in index.functions[fn]
            for text in _ordering_findings(facts.node)
        }
    )
    unjudged = sorted(
        fn for fn in timed if not index.reaches(fn, lambda f: f[1] in TREE_JUDGES)
    )
    return timed, findings, unjudged


_TREE_RESULT = None


def _tree_result():
    """The real tree's scan, computed once per process: it parses ~900 files."""
    global _TREE_RESULT
    if _TREE_RESULT is None:
        scope = _tree_scope_modules()
        context = [
            path for directory in TREE_EXCLUDED_DIRS for path in _modules_in(directory)
        ]
        _TREE_RESULT = _tree_scan(scope, context)
    return _TREE_RESULT


#: Comparisons inside the timed scope that are NOT a duration budget, each
#: with the reason. Keyed by (file, the comparison as ``ast.unparse`` writes
#: it), checked for being live, and counted below — widening this is a
#: visible act. ‼ No entry is a budget on a measured duration. If one ever
#: needs to be, the answer is a row in a ``budgets.py`` or a growth check.
_INFRA = "tests/infrastructure"
TREE_THRESHOLD_ALLOWLIST = {
    (
        f"{_INFRA}/test_observability_integration.py",
        "completed_span.end_time > completed_span.start_time",
    ): "ORDERS the span's two timestamps; no bound is involved",
    (
        f"{_INFRA}/test_observability_integration.py",
        "execution_time >= 0.01",
    ): "a FLOOR under an awaited asyncio.sleep(0.01): a slow runner can only "
    "raise the figure, so this cannot go red for being slow",
    (
        f"{_INFRA}/test_observability_integration.py",
        "len(logs) >= 6",
    ): "a count of log records in a test that times its simulated steps",
    (
        f"{_INFRA}/test_observability_integration.py",
        "len(session_logs) >= 6",
    ): "a count of log records in a test that times its simulated steps",
    (
        f"{_INFRA}/test_observability_integration.py",
        "start_log.created < completion_log.created",
    ): "ORDERS two log records' creation stamps; no bound is involved",
    (
        f"{_INFRA}/test_infrastructure_utils.py",
        "time.time() > self.expirations[key]",
    ): "a test double's TTL expiry check (get/exists); the clock is compared "
    "with a deadline it set itself, not a measured duration with a budget",
    (
        "tests/unit/core/investigation/test_turn_budget.py",
        "60 - (after - before) <= remaining <= 60",
    ): "BRACKETED by two readings of the same monotonic clock taken around "
    "the computation: a slower runner widens the lower bound by exactly the "
    "time it took, so no machine speed can put the value outside it",
    (
        "tests/unit/modules/knowledge/test_gate_stays_off_the_event_loop.py",
        "hopped_ticks >= 3",
    ): "a COUNT of event-loop turns, structural rather than temporal: zero "
    "when the gate runs inline however fast the machine, never zero when it "
    "hops to a thread; load raises it",
    (
        "tests/unit/modules/knowledge/test_gate_stays_off_the_event_loop.py",
        "started < t < finished",
    ): "window membership: which heartbeat ticks fell between two readings",
}

#: Timed tests that reach no judge, each with the reason. Checked for being
#: live and counted below.
_CALIBRATION_SELF_TEST = (
    "a test OF the calibration instrument: the call graph reaches "
    "measure_calibration, but the measurement is pinned (_pin_calibration), "
    "stubbed, refused before it starts, or judged only for being a positive "
    "finite number"
)
_SELF = "tests/unit/ci/test_benchmark_calibration.py"
TREE_UNJUDGED_TIMED_TESTS = {
    (
        f"{_INFRA}/test_observability_integration.py",
        "test_real_span_lifecycle",
    ): "judges span ORDER and a floor under an awaited sleep (allowlisted)",
    (
        f"{_INFRA}/test_observability_integration.py",
        "test_real_integrated_observability_workflow",
    ): "records simulated step durations into a mock histogram and never "
    "compares them",
    (
        "tests/unit/core/investigation/test_turn_budget.py",
        "test_the_deadline_is_monotonic_not_wall_clock",
    ): "a bracket, not a budget (allowlisted)",
    (
        "tests/unit/modules/knowledge/test_gate_stays_off_the_event_loop.py",
        "test_the_gate_does_not_stall_the_event_loop",
    ): "judges a count of event-loop turns (allowlisted)",
    (_SELF, "test_a_faster_machine_is_never_held_to_a_tighter_budget"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_a_machine_at_reference_speed_gets_the_written_thresholds"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_a_measurement_is_a_positive_finite_duration"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_a_nan_measurement_still_floors_at_one"): _CALIBRATION_SELF_TEST,
    (_SELF, "test_a_slower_machine_gets_a_proportionally_larger_budget"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_absolute_mode_pins_the_scale_and_measures_nothing"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_it_says_nothing_when_no_budget_was_asserted"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_the_estimate_is_the_median_of_block_minima"): (
        _CALIBRATION_SELF_TEST
    ),
    (_SELF, "test_the_measurement_is_taken_once_per_process"): (_CALIBRATION_SELF_TEST),
    (_SELF, "test_the_sample_sizes_must_be_positive"): _CALIBRATION_SELF_TEST,
}

#: One planted module per shape a wall-clock threshold is written in outside
#: the timing suites. Each is a whole module, because the shapes that matter
#: here span functions: a helper returning a duration, a judge the test
#: calls, a clock imported under another name. Live-site counts across
#: ``tests/`` on the tree #1579 started from are in the comments.
TREE_PLANTED = {
    # 23 sites: the dominant spelling.
    "inline_perf_counter": (
        "import time\n"
        "def test_x():\n"
        "    start = time.perf_counter()\n"
        "    work()\n"
        "    assert time.perf_counter() - start < 0.25\n"
    ),
    # 6: the ladder's shape — the clock is read in a helper and the
    # comparison is on an attribute of what it returns, against a subscript.
    "helper_returns_an_outcome": (
        "import time\n"
        "class Outcome:\n"
        "    def __init__(self, elapsed):\n"
        "        self.elapsed = elapsed\n"
        "def _run():\n"
        "    started = time.monotonic()\n"
        "    work()\n"
        "    return Outcome(time.monotonic() - started)\n"
        "PARAMS = {'turn_seconds': 2.0}\n"
        "def test_x():\n"
        "    outcome = _run()\n"
        "    assert outcome.elapsed < PARAMS['turn_seconds']\n"
    ),
    # 6: the ReDoS file's shape — a helper returns seconds, compared to a name.
    "helper_returns_seconds": (
        "import time\n"
        "BUDGET = 1.0\n"
        "def _once(fn):\n"
        "    start = time.perf_counter()\n"
        "    fn()\n"
        "    return time.perf_counter() - start\n"
        "def test_x():\n"
        "    assert _once(work) < BUDGET\n"
    ),
    # 1 (``all(t < 2.0 ...)``, missed by #1579's own census).
    "inside_all": (
        "import time\n"
        "def test_x():\n"
        "    times = []\n"
        "    for _ in range(3):\n"
        "        s = time.time()\n"
        "        work()\n"
        "        times.append(time.time() - s)\n"
        "    assert all(t < 2.0 for t in times)\n"
    ),
    "inside_a_boolean": (
        "import time\n"
        "def test_x():\n"
        "    s = time.monotonic()\n"
        "    ok = work()\n"
        "    assert ok and time.monotonic() - s < 0.5\n"
    ),
    "bound_to_a_name_first": (
        "import time\n"
        "def test_x():\n"
        "    s = time.monotonic()\n"
        "    work()\n"
        "    fast = time.monotonic() - s < 0.5\n"
        "    assert fast\n"
    ),
    "deadline_idiom": (
        "import time\n"
        "def test_x():\n"
        "    deadline = time.monotonic() + 0.5\n"
        "    work()\n"
        "    assert time.monotonic() < deadline\n"
    ),
    "aliased_module": (
        "import time as std_time\n"
        "def test_x():\n"
        "    s = std_time.time()\n"
        "    work()\n"
        "    assert std_time.time() - s < 1.0\n"
    ),
    "imported_by_name": (
        "from time import perf_counter as clock\n"
        "def test_x():\n"
        "    s = clock()\n"
        "    work()\n"
        "    assert clock() - s < 1.0\n"
    ),
    # 1 (``asyncio.get_event_loop().time()`` in the investigation lifecycle).
    "event_loop_clock": (
        "import asyncio\n"
        "async def test_x():\n"
        "    loop = asyncio.get_running_loop()\n"
        "    s = loop.time()\n"
        "    await work()\n"
        "    assert loop.time() - s < 0.1\n"
    ),
    "event_loop_clock_inline": (
        "import asyncio\n"
        "async def test_x():\n"
        "    a = asyncio.get_event_loop().time()\n"
        "    b = asyncio.get_event_loop().time()\n"
        "    assert abs(a - b) < 0.1\n"
    ),
    "timeit_call": (
        "import timeit\n"
        "def test_x():\n"
        "    assert timeit.timeit(work, number=5) < 0.1\n"
    ),
    "unittest_spelling": (
        "import time\n"
        "class T:\n"
        "    def test_x(self):\n"
        "        s = time.perf_counter()\n"
        "        work()\n"
        "        self.assertLess(time.perf_counter() - s, 0.2)\n"
    ),
    "if_then_fail": (
        "import time, pytest\n"
        "def test_x():\n"
        "    s = time.perf_counter()\n"
        "    work()\n"
        "    if time.perf_counter() - s > 0.2:\n"
        "        pytest.fail('slow')\n"
    ),
    # The growth test's shape before #1579: a RATIO of two durations.
    "ratio_of_durations": (
        "import time\n"
        "def _t(n):\n"
        "    s = time.perf_counter()\n"
        "    work(n)\n"
        "    return time.perf_counter() - s\n"
        "def test_x():\n"
        "    ratio = _t(2) / _t(1)\n"
        "    assert ratio < 3.0\n"
    ),
    # performance/'s shape: a context manager stores the duration on self.
    "stored_on_self": (
        "import time, contextlib\n"
        "class T:\n"
        "    @contextlib.contextmanager\n"
        "    def measure(self):\n"
        "        start = time.perf_counter()\n"
        "        yield\n"
        "        self.measured = time.perf_counter() - start\n"
        "    def test_x(self):\n"
        "        with self.measure():\n"
        "            work()\n"
        "        assert self.measured < 0.1\n"
    ),
}

#: Shapes the NO-THRESHOLD rule cannot see, and which the JUDGE rule must
#: catch instead: the comparison lives where the scan does not look, but the
#: test still measures and never reaches a helper.
TREE_PLANTED_JUDGE_ONLY = {
    "judged_by_a_local_helper": (
        "import time\n"
        "def _check_fast(seconds):\n"
        "    assert seconds < 0.2\n"
        "def test_x():\n"
        "    s = time.perf_counter()\n"
        "    work()\n"
        "    _check_fast(time.perf_counter() - s)\n"
    ),
    "judged_by_isclose": (
        "import math, time\n"
        "def test_x():\n"
        "    s = time.perf_counter()\n"
        "    work()\n"
        "    assert math.isclose(time.perf_counter() - s, 0.0, abs_tol=0.2)\n"
    ),
}

#: Modules the rule must leave alone: time used as a TIMESTAMP, virtual
#: time, and a measurement judged by a helper.
TREE_PLANTED_NEGATIVES = {
    "expiry_timestamp": (
        "import time\n"
        "def test_x():\n"
        "    token = make(exp=time.time() + 60)\n"
        "    assert token.ttl > 30\n"
    ),
    "aged_window": (
        "import time\n"
        "def test_x():\n"
        "    now = time.time()\n"
        "    stamps = [now - 55.0] * 5\n"
        "    assert 1 <= retry_after(stamps) <= 6\n"
    ),
    "virtual_clock": (
        "import asyncio\n"
        "from tests.wallclock import virtual_now\n"
        "async def test_x():\n"
        "    s = virtual_now()\n"
        "    await asyncio.sleep(1)\n"
        "    assert virtual_now() - s < 2\n"
    ),
    "judged_by_the_helper": (
        "import time\n"
        "from tests.wallclock import assert_latency_within\n"
        "def test_x():\n"
        "    s = time.perf_counter()\n"
        "    work()\n"
        "    assert_latency_within(time.perf_counter() - s, BUDGET, 'x')\n"
    ),
}


def _plant(tmp_path: Path, source: str) -> List[Path]:
    module = tmp_path / "test_planted.py"
    module.write_text(source)
    return [module]


def _scan_planted(tmp_path: Path, source: str):
    return _tree_scan(_plant(tmp_path, source), [], root=tmp_path)


class TestTheRestOfTheTree:
    @pytest.mark.parametrize("shape", sorted(TREE_PLANTED))
    def test_every_planted_shape_is_a_finding(self, tmp_path, shape):
        """Each shape, alone, in its own module: a miss cannot hide behind another."""
        timed, findings, unjudged = _scan_planted(tmp_path, TREE_PLANTED[shape])
        assert timed, f"{shape!r}: the test was not recognised as timed"
        assert findings, f"{shape!r}: the threshold was not found"
        assert unjudged, f"{shape!r}: the judge rule did not report it either"

    @pytest.mark.parametrize("shape", sorted(TREE_PLANTED_JUDGE_ONLY))
    def test_a_threshold_the_scan_cannot_see_is_still_unjudged(self, tmp_path, shape):
        timed, _findings, unjudged = _scan_planted(
            tmp_path, TREE_PLANTED_JUDGE_ONLY[shape]
        )
        assert timed and unjudged, f"{shape!r} escaped both rules"

    @pytest.mark.parametrize("shape", sorted(TREE_PLANTED_NEGATIVES))
    def test_what_is_not_a_measurement_is_left_alone(self, tmp_path, shape):
        timed, findings, unjudged = _scan_planted(
            tmp_path, TREE_PLANTED_NEGATIVES[shape]
        )
        assert not findings and not unjudged, (shape, findings, unjudged)

    def test_a_measuring_helper_in_another_module_is_followed(self, tmp_path):
        """``from x import f`` across modules — the ReDoS helpers' shape."""
        (tmp_path / "timing.py").write_text(
            "import time\n"
            "def once(fn):\n"
            "    s = time.perf_counter()\n"
            "    fn()\n"
            "    return time.perf_counter() - s\n"
        )
        (tmp_path / "test_uses_it.py").write_text(
            "from timing import once\n"
            "def test_x():\n"
            "    assert once(work) < 1.0\n"
        )
        scope = [tmp_path / "timing.py", tmp_path / "test_uses_it.py"]
        timed, findings, unjudged = _tree_scan(scope, [], root=tmp_path)
        assert ("test_uses_it.py", "test_x") in [(Path(m).name, n) for m, n in timed]
        assert any("once(work) < 1.0" in f[2] for f in findings)

    def test_a_timing_fixture_in_a_parent_conftest_is_followed(self, tmp_path):
        """A fixture is reached through a PARAMETER, from a conftest above.

        That is pytest's own resolution, and the house idiom for shared
        setup: ``tests/integration/conftest.py`` defines three measuring
        fixtures. Without modelling it, a test that took ``stopwatch`` and
        compared what it returned was neither timed nor scanned.
        """
        (tmp_path / "conftest.py").write_text(
            "import time, pytest\n"
            "@pytest.fixture\n"
            "def stopwatch():\n"
            "    def run(fn):\n"
            "        s = time.perf_counter()\n"
            "        fn()\n"
            "        return time.perf_counter() - s\n"
            "    return run\n"
        )
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "test_uses_it.py").write_text(
            "def test_x(stopwatch):\n    assert stopwatch(work) < 0.2\n"
        )
        scope = [tmp_path / "conftest.py", tmp_path / "sub" / "test_uses_it.py"]
        timed, findings, unjudged = _tree_scan(scope, [], root=tmp_path)
        assert [name for _module, name in timed] == ["test_x"]
        assert [text for _m, _f, text in findings] == ["stopwatch(work) < 0.2"]
        assert unjudged

    def test_no_wall_clock_threshold_outside_the_timing_suites(self):
        """‼ The #1579 defect itself, on the real tree."""
        _timed, findings, _unjudged = _tree_result()
        violations = [
            f"{module}::{func}: {text}"
            for module, func, text in findings
            if (module, text) not in TREE_THRESHOLD_ALLOWLIST
        ]
        assert not violations, (
            "wall-clock thresholds in modules both required gates run. Route "
            "a latency through assert_latency_within against a budgets.py "
            "row (tests/performance/), a growth question through "
            "assert_linear_growth, a deadline question onto VirtualTimeLoop "
            "— or measure something that is not a clock: " + "; ".join(violations)
        )

    def test_every_timed_test_outside_the_timing_suites_reaches_a_judge(self):
        _timed, _findings, unjudged = _tree_result()
        stray = [
            f"{module}::{name}"
            for module, name in unjudged
            if (module, name) not in TREE_UNJUDGED_TIMED_TESTS
        ]
        assert not stray, (
            "these tests measure a duration and judge it by some route other "
            "than the helpers, or not at all: " + ", ".join(stray)
        )

    def test_every_tree_allowlist_entry_is_live(self):
        _timed, findings, unjudged = _tree_result()
        seen = {(module, text) for module, _func, text in findings}
        dead = sorted(k for k in TREE_THRESHOLD_ALLOWLIST if k not in seen)
        assert not dead, dead
        dead = sorted(k for k in TREE_UNJUDGED_TIMED_TESTS if k not in set(unjudged))
        assert not dead, dead

    def test_the_tree_allowlist_cost_is_what_was_measured(self):
        """Counted, not tuned: nine comparisons, fourteen tests.

        The nine are orderings of timestamps, two counts of log records, a
        floor under a sleep, a bracket, a tick count, a window membership and
        a test double's TTL check. Ten of the fourteen tests are the
        calibration instrument's own tests, which reach the real measurement
        through the call graph and pin or stub it at run time.
        """
        assert len(TREE_THRESHOLD_ALLOWLIST) == 9
        assert len(TREE_UNJUDGED_TIMED_TESTS) == 14

    def test_the_three_scopes_partition_tests(self):
        """Every module under tests/ is watched by exactly one rule."""
        everything = {
            p for p in TESTS_ROOT.rglob("*.py") if "__pycache__" not in p.parts
        }
        tree = set(_tree_scope_modules())
        suites = {p for directory in TREE_EXCLUDED_DIRS for p in _modules_in(directory)}
        assert tree | suites == everything
        assert not tree & suites

    def test_the_helpers_directory_holds_no_tests(self):
        """``tests/wallclock/`` is scanned for the call graph ONLY.

        Both required gates collect ``test_*`` there like anywhere else, so a
        test placed in it would be watched by neither rule. It holds none,
        and this is what keeps it that way.
        """
        assert not list(WALLCLOCK_DIR.rglob("test_*.py"))
        tests_in_helpers = [
            f"{_key(path)}::{node.name}"
            for path in _modules_in(WALLCLOCK_DIR)
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        assert not tests_in_helpers, tests_in_helpers

    def test_the_rule_looks_where_the_defect_was(self):
        """A rule scoped away from its violations is green forever.

        #1579's census found the thresholds in tests/infrastructure,
        tests/integration and tests/unit. Each must be in scope, and the scan
        must still be resolving timed tests on the real tree — a detector
        whose index silently came back empty would pass every check above
        it. Reach over each SHAPE is what the planted modules prove; this is
        reach over the directories.
        """
        scope = _tree_scope_modules()
        for directory in ("tests/infrastructure", "tests/integration", "tests/unit"):
            inside = [p for p in scope if p.is_relative_to(REPO_ROOT / directory)]
            assert len(inside) >= 20, (directory, len(inside))
        timed, _findings, _unjudged = _tree_result()
        assert len(timed) >= 20, timed

    def test_virtual_now_refuses_a_real_loop(self):
        """The one exemption the rule makes is sound only if this holds."""
        import asyncio

        from tests.wallclock import virtual_now

        async def read():
            return virtual_now()

        with pytest.raises(RuntimeError, match="VirtualTimeLoop"):
            asyncio.run(read())

    def test_the_virtual_loop_charges_nominal_time(self):
        """A 5 s sleep costs 5 virtual seconds and no wall time to speak of."""
        import asyncio

        from tests.wallclock import VirtualTimeLoop, virtual_now

        async def five_seconds():
            started = virtual_now()
            await asyncio.sleep(5)
            return virtual_now() - started

        loop = VirtualTimeLoop()
        try:
            assert loop.run_until_complete(five_seconds()) == 5
        finally:
            loop.close()

    def test_a_cancelled_timer_does_not_move_virtual_time(self):
        """``wait_for`` leaves a cancelled timeout handle in the heap."""
        import asyncio

        from tests.wallclock import VirtualTimeLoop, virtual_now

        async def quick_inside_a_long_timeout():
            await asyncio.wait_for(asyncio.sleep(1), timeout=60)
            await asyncio.sleep(0)
            return virtual_now()

        loop = VirtualTimeLoop()
        try:
            assert loop.run_until_complete(quick_inside_a_long_timeout()) == 1
        finally:
            loop.close()


# -------------------------------------------------------- the growth helper


def _linear(text: str) -> int:
    return sum(1 for _ in text)


def _quadratic(text: str) -> int:
    return sum(text.count(text[i]) for i in range(0, len(text), 8))


class TestTheGrowthHelper:
    """``assert_linear_growth`` is a judge; these hold it to both columns."""

    def test_a_linear_cost_passes(self):
        from tests.wallclock import assert_linear_growth

        assert_linear_growth(
            _linear, lambda n: "ab" * n, small=512, label="linear control"
        )

    def test_a_quadratic_cost_fails(self):
        from tests.wallclock import assert_linear_growth

        with pytest.raises(AssertionError, match="quadratic ~256x"):
            assert_linear_growth(
                _quadratic, lambda n: "ab" * n, small=64, label="quadratic control"
            )

    def test_the_bound_is_the_midpoint_of_linear_and_quadratic(self):
        from tests.wallclock import growth_bound

        assert growth_bound(16) == 64.0
        assert growth_bound(4) == 8.0


# ------------------------------------------------------------ the workflow
#
# ‼ These conditions must be EVALUATED, never matched as substrings. The
# first version of this guard asserted `"schedule" in condition`, and
# `"schedule"` is a substring of `!= 'schedule'` exactly as much as of
# `== 'schedule'` — so inverting the nightly job's condition, which would
# run the absolute targets on every pull request and reinstate the flake
# #908 exists to remove, left the suite green. The sibling job's condition
# at the top of the same file is literally `github.event_name !=
# 'schedule' && ...`, which is how easy it is to have the string and the
# wrong side of the comparison at once.


class _ExpressionError(AssertionError):
    """The workflow uses a construct this evaluator was not taught."""


def _tokenize(expression: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    while i < len(expression):
        ch = expression[i]
        if ch.isspace():
            i += 1
        elif expression.startswith(("==", "!=", "&&", "||"), i):
            tokens.append(expression[i : i + 2])
            i += 2
        elif ch in "!()":
            tokens.append(ch)
            i += 1
        elif ch == "'":
            end = expression.find("'", i + 1)
            if end < 0:
                raise _ExpressionError(f"unterminated string in {expression!r}")
            tokens.append(expression[i : end + 1])
            i = end + 1
        elif ch.isalnum() or ch in "_.":
            j = i
            while j < len(expression) and (
                expression[j].isalnum() or expression[j] in "_."
            ):
                j += 1
            tokens.append(expression[i:j])
            i = j
        else:
            raise _ExpressionError(f"unexpected {ch!r} in {expression!r}")
    return tokens


def _truthy(value) -> bool:
    """GitHub's coercion: null, false, 0 and the empty string are false."""
    return bool(value)


def evaluate_condition(expression, *, event_name: str, inputs: dict) -> bool:
    """Evaluate the subset of GitHub's expression syntax these jobs use.

    Deliberately narrow: anything it was not taught — a function call such
    as ``always()``, an unknown context — raises rather than guessing, so a
    condition that outgrows this evaluator fails loudly instead of being
    quietly waved through. A missing ``if:`` means the job always runs.
    """
    if expression is None:
        return True
    tokens = _tokenize(str(expression))
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        return token

    def primary():
        token = take()
        if token == "(":
            value = or_expr()
            if peek() != ")":
                raise _ExpressionError(f"missing ')' in {expression!r}")
            take()
            return value
        if token.startswith("'"):
            return token[1:-1]
        if token == "github.event_name":
            return event_name
        if token.startswith("inputs."):
            return inputs.get(token[len("inputs.") :])
        if token in ("true", "false"):
            return token == "true"
        raise _ExpressionError(f"unsupported term {token!r} in {expression!r}")

    def unary():
        if peek() == "!":
            take()
            return not _truthy(unary())
        return primary()

    def comparison():
        left = unary()
        if peek() in ("==", "!="):
            operator = take()
            right = unary()
            return (left == right) if operator == "==" else (left != right)
        return left

    def and_expr():
        value = comparison()
        while peek() == "&&":
            take()
            right = comparison()
            value = right if _truthy(value) else value
        return value

    def or_expr():
        value = and_expr()
        while peek() == "||":
            take()
            right = and_expr()
            value = value if _truthy(value) else right
        return value

    result = or_expr()
    if pos != len(tokens):
        raise _ExpressionError(f"trailing tokens in {expression!r}")
    return _truthy(result)


#: Every event shape this workflow can be reached by, and which of the two
#: jobs must run on it. Naming the job rather than counting is deliberate:
#: "exactly one runs" is also satisfied by swapping them.
EVENT_SHAPES = [
    ("pull_request", {}, "benchmarks"),
    ("push", {}, "benchmarks"),
    ("schedule", {}, "nightly-absolute"),
    (
        "workflow_dispatch",
        {"absolute_targets": False, "run_full_suite": False},
        "benchmarks",
    ),
    (
        "workflow_dispatch",
        {"absolute_targets": True, "run_full_suite": False},
        "nightly-absolute",
    ),
]

PAIR = ("benchmarks", "nightly-absolute")


class TestTheConditionEvaluator:
    """The detector's own correctness, before it is trusted with the pair.

    A guard built on an evaluator is only as good as the evaluator, and
    this one exists precisely because the obvious cheap check was wrong.
    """

    @pytest.mark.parametrize(
        "expression,event,inputs,expected",
        [
            # The pair the substring check could not tell apart.
            ("github.event_name == 'schedule'", "schedule", {}, True),
            ("github.event_name != 'schedule'", "schedule", {}, False),
            ("github.event_name == 'schedule'", "pull_request", {}, False),
            ("github.event_name != 'schedule'", "pull_request", {}, True),
            # Absent inputs are falsy, which is what a push looks like.
            ("inputs.absolute_targets", "push", {}, False),
            ("!inputs.absolute_targets", "push", {}, True),
            (
                "inputs.absolute_targets",
                "workflow_dispatch",
                {"absolute_targets": True},
                True,
            ),
            (
                "!inputs.absolute_targets",
                "workflow_dispatch",
                {"absolute_targets": True},
                False,
            ),
            # Precedence and short-circuiting.
            (
                "github.event_name != 'schedule' && !inputs.absolute_targets",
                "schedule",
                {},
                False,
            ),
            (
                "github.event_name != 'schedule' && !inputs.absolute_targets",
                "pull_request",
                {},
                True,
            ),
            (
                "github.event_name == 'schedule' || inputs.absolute_targets",
                "pull_request",
                {},
                False,
            ),
            (
                "github.event_name == 'schedule' || inputs.absolute_targets",
                "schedule",
                {},
                True,
            ),
            # No condition at all means the job runs.
            (None, "pull_request", {}, True),
        ],
    )
    def test_known_expressions(self, expression, event, inputs, expected):
        assert (
            evaluate_condition(expression, event_name=event, inputs=inputs) is expected
        )

    @pytest.mark.parametrize(
        "expression",
        ["always()", "success() && true", "github.ref == 'refs/heads/main'", "1 +"],
    )
    def test_it_refuses_what_it_was_not_taught(self, expression):
        # Fail loudly, never fail open: replacing a condition with
        # `always()` must break this guard rather than slip past it.
        with pytest.raises(AssertionError):
            evaluate_condition(expression, event_name="push", inputs={})


class TestWorkflowWiring:
    """The calibrated and absolute runs must both exist, on opposite events.

    The scheme has two halves and each is useless alone: a calibrated run
    that never checks wall-clock stops measuring the product target, and an
    absolute run on a pull request is the flake #908 is about. So the
    property is two-sided — on every event shape exactly one of the pair
    runs, and it is the right one. Pinning only the nightly job's condition
    is not a property at all: replacing the other one with `always()` would
    satisfy it.
    """

    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load(WORKFLOW.read_text())

    def _condition(self, job: str):
        jobs = self._workflow()["jobs"]
        assert job in jobs, f"{job} is gone from the workflow"
        return jobs[job].get("if")

    def test_the_pull_request_job_does_not_set_absolute_mode(self):
        job = self._workflow()["jobs"]["benchmarks"]
        for step in job["steps"]:
            assert calibration.ABSOLUTE_MODE_ENV not in (step.get("env") or {})

    def test_a_nightly_job_runs_the_absolute_targets(self):
        job = self._workflow()["jobs"]["nightly-absolute"]
        envs = [step.get("env") or {} for step in job["steps"]]
        assert any(
            env.get(calibration.ABSOLUTE_MODE_ENV) in ("1", 1, "true", True)
            for env in envs
        ), ("the nightly job must set " + calibration.ABSOLUTE_MODE_ENV)

    @pytest.mark.parametrize("directory", GUARDED_DIRS, ids=lambda d: d.name)
    def test_every_product_target_is_asserted_somewhere(self, directory):
        """‼ A product target nothing runs is a number, not a check.

        `asserted_target` picks `product_target` only under
        `FM_BENCHMARK_ABSOLUTE`, and the only job that sets it ran
        `pytest tests/benchmarks/ -m benchmark`. `tests/performance/` is
        not marked `benchmark`, so none of its rows was ever asserted —
        every `product_target` there was inert, and
        `test_typical_api_request_overhead`'s real `logging_overhead <
        0.05` had been deleted rather than relocated (#1557 review).

        So the property is per DIRECTORY, not per job: some step of the
        absolute job has to select each guarded tree.
        """
        job = self._workflow()["jobs"]["nightly-absolute"]
        selecting = [
            step
            for step in job["steps"]
            if calibration.ABSOLUTE_MODE_ENV in (step.get("env") or {})
            and directory.name in (step.get("run") or "")
        ]
        assert selecting, (
            f"no step of nightly-absolute runs tests/{directory.name}/ under "
            f"{calibration.ABSOLUTE_MODE_ENV}, so every product_target in "
            f"tests/{directory.name}/budgets.py is asserted by nothing"
        )

    def test_the_absolute_step_for_performance_does_not_filter_it_away(self):
        """`-m benchmark` would select nothing in `tests/performance/`."""
        job = self._workflow()["jobs"]["nightly-absolute"]
        for step in job["steps"]:
            run = step.get("run") or ""
            if "tests/performance/" in run:
                assert "-m benchmark" not in run, (
                    "tests/performance/ carries no benchmark marker; "
                    "`-m benchmark` would deselect all of it and the step "
                    "would pass having run nothing"
                )

    @pytest.mark.parametrize(
        "event,inputs,expected", EVENT_SHAPES, ids=lambda v: str(v)[:40]
    )
    def test_exactly_one_of_the_pair_runs_and_it_is_the_right_one(
        self, event, inputs, expected
    ):
        running = [
            job
            for job in PAIR
            if evaluate_condition(self._condition(job), event_name=event, inputs=inputs)
        ]
        assert running == [expected], (
            f"on {event} with inputs {inputs}, expected only {expected!r} "
            f"to run, got {running}"
        )

    def test_the_absolute_targets_never_gate_a_pull_request(self):
        # The single most important consequence, asserted on its own so a
        # failure names it: an absolute run on a pull request is #908.
        assert not evaluate_condition(
            self._condition("nightly-absolute"),
            event_name="pull_request",
            inputs={},
        )

    def test_the_schedule_is_nightly(self):
        # `on` parses as the boolean True under YAML 1.1.
        workflow = self._workflow()
        triggers = workflow.get("on") or workflow[True]
        crons = [entry["cron"] for entry in triggers["schedule"]]
        assert crons == ["0 2 * * *"], crons


def test_absolute_env_var_is_not_set_in_this_process():
    """Guard for the file itself: a stray export would make it vacuous."""
    assert calibration.ABSOLUTE_MODE_ENV not in os.environ

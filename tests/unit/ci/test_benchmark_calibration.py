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
* **Laziness.** The ordinary CI invocation collects this package and
  deselects every test in it. Nothing may pay the calibration's ~1s for
  that.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List, Tuple

import pytest
import yaml

from tests.benchmarks import budgets as budget_table
from tests.benchmarks import calibration
from tests.benchmarks import conftest as bench_conftest
from tests.benchmarks.budgets import LatencyBudget, ThroughputBudget
from tests.benchmarks.conftest import (
    assert_latency_within,
    assert_throughput_at_least,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_DIR = REPO_ROOT / "tests" / "benchmarks"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "benchmarks.yml"


@pytest.fixture(autouse=True)
def _clean_calibration(monkeypatch):
    """Every test here starts from an unmeasured, non-absolute process."""
    monkeypatch.delenv(calibration.ABSOLUTE_MODE_ENV, raising=False)
    calibration.reset_calibration_cache()
    yield
    calibration.reset_calibration_cache()


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

    def test_a_measurement_is_a_plausible_positive_duration(self):
        # A handful of repetitions, not the shipped counts — this test is
        # about the shape of the answer, not its stability.
        observed = calibration.measure_calibration(blocks=3, repetitions_per_block=3)
        assert 0 < observed < 1.0

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

    @pytest.mark.parametrize("name", sorted(budget_table.ALL_BUDGETS))
    def test_the_anchor_is_2_to_3x_its_measured_reference(self, name):
        budget = budget_table.ALL_BUDGETS[name]
        if isinstance(budget, ThroughputBudget):
            multiple = budget.reference / budget.regression
        else:
            multiple = budget.regression / budget.reference
        assert (
            budget_table.MIN_REGRESSION_MULTIPLE
            <= multiple
            <= budget_table.MAX_REGRESSION_MULTIPLE
        ), f"{name} is {multiple:.2f}x its reference"

    @pytest.mark.parametrize("name", sorted(budget_table.ALL_BUDGETS))
    def test_the_per_pull_request_anchor_is_the_stricter_of_the_two(self, name):
        # The structural fact behind "the nightly is where a product target
        # is asserted": on every budget the per-PR anchor is TIGHTER than
        # the product target, so a green pull request implies the product
        # target held too, and the nightly's job is the wall clock rather
        # than the regression.
        budget = budget_table.ALL_BUDGETS[name]
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

    def test_the_table_names_tests_that_exist(self):
        """A budget pointing at a renamed test is a budget nobody applies."""
        sources = "\n".join(
            path.read_text() for path in sorted(BENCHMARK_DIR.glob("test_*.py"))
        )
        missing = [
            budget.test
            for budget in budget_table.ALL_BUDGETS.values()
            if f"async def {budget.test}(" not in sources
        ]
        assert not missing, missing

    @pytest.mark.parametrize("name", sorted(budget_table.ALL_BUDGETS))
    def test_every_entry_is_actually_used_by_the_suite(self, name):
        """An unused entry is a number that looks enforced and is not."""
        sources = "\n".join(
            path.read_text() for path in sorted(BENCHMARK_DIR.glob("test_*.py"))
        )
        assert name in sources, f"{name} is in the table but no benchmark uses it"


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
        bench_conftest.pytest_terminal_summary(reporter, 0, None)
        assert reporter.lines == []

    def test_it_reports_the_scale_once_a_budget_was_asserted(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 2)
        assert_latency_within(0.001, _latency(1.0), "probe")
        reporter = _FakeReporter()
        bench_conftest.pytest_terminal_summary(reporter, 0, None)
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
        bench_conftest.pytest_terminal_summary(reporter, 0, None)
        assert calls == [1], "the summary must measure in absolute mode"
        body = " ".join(reporter.lines)
        assert "ABSOLUTE" in body and "1.50x" in body, body


# -------------------------------------------- one comparison site, scanned

#: Identifiers that mean "this number is a duration or a rate". A comparison
#: between one of these and a numeric literal is a latency/throughput
#: threshold and must go through the helpers. Memory assertions
#: (``rss_mb``, ``memory_delta``, ``final_memory``) are deliberately absent:
#: megabytes do not scale with machine throughput and must NOT be corrected.
TIMING_TOKENS = (
    "measured.best",
    "measured.median",
    "measured.worst",
    "p50",
    "p95",
    "p99",
    "throughput",
    "latency",
    "elapsed",
    "duration",
    "_ms",
    "_seconds",
    "_per_second",
)

#: One planted violation per token, each written so that EXACTLY ONE token
#: matches it. That is what makes every entry in ``TIMING_TOKENS``
#: load-bearing, checked below by deleting each token in turn — without it
#: a token can rot unnoticed because a sibling happens to cover the same
#: planted line (``stats['p95_ms']`` matches both ``p95`` and ``_ms``, which
#: is how ``p95`` was dead weight in the first version of this control).
PLANTED_PER_TOKEN = {
    "measured.best": "assert measured.best < 0.2",
    "measured.median": "assert measured.median < 0.2",
    "measured.worst": "assert measured.worst < 0.2",
    "p50": "assert p50 < 200",
    "p95": "assert p95 < 200",
    "p99": "assert p99 < 200",
    "throughput": "assert throughput > 50",
    "latency": "assert latency < 0.2",
    "elapsed": "assert elapsed < 0.2",
    "duration": "assert duration < 0.2",
    "_ms": "assert stats['total_ms'] < 200",
    "_seconds": "assert budget_seconds < 0.2",
    "_per_second": "assert items_per_second > 50",
}

#: Assertions the scan must NOT flag. Memory is not scaled by the
#: calibration, so a megabyte threshold is a legitimate literal comparison.
PLANTED_NEGATIVES = (
    "assert rss_mb < 1500",
    "assert memory_delta < 100",
    "assert final_memory < 2000",
)


def _threshold_comparisons(
    source: str, filename: str, tokens=None
) -> List[Tuple[str, str, int]]:
    """Every ``assert <timing> <op> <number>`` in ``source``.

    Returns (filename, enclosing function, line) triples. ``tokens``
    overrides the vocabulary, which is how the load-bearing check below
    drops one entry at a time without mutating module state.
    """
    vocabulary = TIMING_TOKENS if tokens is None else tokens
    tree = ast.parse(source, filename=filename)
    enclosing: List[str] = []
    found: List[Tuple[str, str, int]] = []

    class _Walk(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802 - ast API
            enclosing.append(node.name)
            self.generic_visit(node)
            enclosing.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815 - ast API

        def visit_Assert(self, node):  # noqa: N802 - ast API
            test = node.test
            if isinstance(test, ast.Compare) and len(test.comparators) == 1:
                right = test.comparators[0]
                if isinstance(right, ast.Constant) and isinstance(
                    right.value, (int, float)
                ):
                    left = ast.unparse(test.left)
                    if any(tok in left for tok in vocabulary):
                        found.append(
                            (filename, enclosing[-1] if enclosing else "", node.lineno)
                        )
            self.generic_visit(node)

    _Walk().visit(tree)
    return found


def _planted_source(tokens=None) -> str:
    """A synthetic module carrying one violation per token, plus negatives."""
    chosen = PLANTED_PER_TOKEN if tokens is None else tokens
    body = list(chosen.values()) + list(PLANTED_NEGATIVES)
    return "def test_x():\n" + "".join(f"    {line}\n" for line in body)


class TestOneComparisonSite:
    def test_the_scan_finds_every_planted_violation(self):
        """Positive control: a drifted vocabulary reads like a clean tree."""
        hits = _threshold_comparisons(_planted_source(), "planted.py")
        # One hit per token, and none for the three memory negatives that
        # follow them.
        assert len(hits) == len(PLANTED_PER_TOKEN)
        assert [h[2] for h in hits] == list(range(2, 2 + len(PLANTED_PER_TOKEN)))

    @pytest.mark.parametrize("token", PLANTED_PER_TOKEN)
    def test_every_token_in_the_vocabulary_is_load_bearing(self, token):
        """Removing any one token must lose exactly one planted violation.

        The check that a vocabulary entry is doing work. `p95` was not: its
        only planted line was `stats['p95_ms']`, which `_ms` already
        matched, so dropping `p95` from the vocabulary entirely left the
        suite green.
        """
        source = _planted_source()
        full = len(_threshold_comparisons(source, "planted.py"))
        reduced = tuple(t for t in TIMING_TOKENS if t != token)
        assert len(reduced) == len(TIMING_TOKENS) - 1, f"{token!r} is not in the list"
        remaining = len(_threshold_comparisons(source, "planted.py", tokens=reduced))
        assert remaining == full - 1, (
            f"{token!r} is dead weight: removing it from TIMING_TOKENS took "
            f"the hit count from {full} to {remaining}, so another token "
            "already covers its planted line"
        )

    def test_memory_thresholds_are_never_flagged(self):
        source = "def test_x():\n" + "".join(
            f"    {line}\n" for line in PLANTED_NEGATIVES
        )
        assert _threshold_comparisons(source, "planted.py") == []

    def test_the_scan_looks_at_every_benchmark_module(self):
        """A guard that watched the wrong directory would be green forever."""
        modules = sorted(p.name for p in BENCHMARK_DIR.glob("*.py"))
        assert "conftest.py" in modules
        # budgets.py carries 50 numeric thresholds (#1556). It is in scope
        # for the same reason conftest.py is: a hand-rolled comparison
        # would be just as invisible there.
        assert "budgets.py" in modules
        assert len([m for m in modules if m.startswith("test_")]) >= 7

    def test_no_benchmark_compares_a_threshold_outside_the_helpers(self):
        # No allowlist. The two helpers compare against a computed `budget`
        # / `floor` rather than a literal, so the scan's own rule — a timing
        # name against a NUMERIC LITERAL — already excludes them. An
        # allowlist here would suppress nothing and would read as "the scan
        # confirms these two exist", which it does not.
        violations = []
        for path in sorted(BENCHMARK_DIR.glob("*.py")):
            for filename, func, lineno in _threshold_comparisons(
                path.read_text(), path.name
            ):
                violations.append(f"{filename}:{lineno} in {func or '<module>'}")
        assert not violations, (
            "latency/throughput thresholds must go through "
            "assert_latency_within / assert_throughput_at_least so the #908 "
            "calibration applies; found: " + ", ".join(violations)
        )


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

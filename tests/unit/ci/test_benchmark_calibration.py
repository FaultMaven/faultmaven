"""The benchmark gate's instrument (#908).

`tests/benchmarks/` is excluded from both CI test suites (`-m "not
benchmark"`) and runs in a workflow of its own, so nothing in the ordinary
suite exercises it. This file does — it is deliberately unmarked so it runs
in `Test Standalone`/`Test Cloud`, where a broken calibration is cheap to
find rather than being discovered by a benchmark job a week later.

What it holds down, and why each one:

* **The floor.** `calibration_scale()` never drops below 1.0, so this whole
  mechanism cannot turn a passing benchmark red. That is the property that
  makes the reference constant safe to be approximately right.
* **The correction.** A machine measurably slower than the reference gets a
  proportionally larger budget, which is the one thing #908 asked for.
* **The direction on throughput.** Throughput is 1/latency, so its floor
  must be DIVIDED by the scale. Multiplying would tighten it on exactly the
  runners the correction exists to relieve, and the error is invisible at a
  scale of 1.0 — which is every developer machine.
* **Discrimination.** A uniform slowdown across every test cancels; a
  regression in one path does not. Asserted as the two columns, because a
  scheme that only passes the first has disabled the benchmarks.
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

from tests.benchmarks import calibration
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
        assert_latency_within(0.001, 1.0, "probe")
        assert calibration.scale_was_used() is True

    def test_the_description_says_so_in_absolute_mode(self, monkeypatch):
        monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
        assert "ABSOLUTE" in calibration.describe_calibration()


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
        assert_latency_within(0.150, 0.200, "probe")

    def test_fails_over_the_target_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError) as excinfo:
            assert_latency_within(0.250, 0.200, "probe")
        assert "200ms target" in str(excinfo.value)
        assert "1.00 calibration" in str(excinfo.value)

    def test_the_budget_moves_with_the_scale(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.5)
        assert_latency_within(0.250, 0.200, "probe")  # 250ms < 200 * 1.5
        with pytest.raises(AssertionError):
            assert_latency_within(0.310, 0.200, "probe")

    def test_the_failure_message_carries_the_detail(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError) as excinfo:
            assert_latency_within(0.9, 0.1, "probe", "min 900.0ms (n=5)")
        assert "min 900.0ms (n=5)" in str(excinfo.value)


class TestThroughputFloor:
    def test_passes_above_the_floor_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        assert_throughput_at_least(60.0, 50.0, "probe")

    def test_fails_below_the_floor_at_scale_one(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        with pytest.raises(AssertionError):
            assert_throughput_at_least(40.0, 50.0, "probe")

    def test_a_slower_machine_gets_a_LOWER_floor_not_a_higher_one(self, monkeypatch):
        # The direction guard. Throughput is 1/latency; multiplying by the
        # scale here would demand MORE work per second from a machine that
        # was just measured to be slower.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 2)
        assert_throughput_at_least(26.0, 50.0, "probe")  # floor is now 25/s
        with pytest.raises(AssertionError):
            assert_throughput_at_least(24.0, 50.0, "probe")


class TestDiscrimination:
    """The two columns #908 must be judged on, run rather than argued.

    ``_TARGETS`` is the real budget table of the suite, and ``_HEALTHY`` the
    worst (relative) utilisation each one was measured at. A uniform
    slowdown must clear all of them; a single-path regression must fail its
    own and nothing else.
    """

    # name -> (target seconds, healthy observed seconds)
    _CASES = {
        "thin_margin": (0.200, 0.190),  # 95% of budget: the canary
        "typical": (0.150, 0.060),
        "roomy": (1.000, 0.020),
    }

    def test_a_uniform_1_3x_slowdown_fails_nothing(self, monkeypatch):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.3)
        for name, (target, healthy) in self._CASES.items():
            assert_latency_within(healthy * 1.3, target, name)

    def test_a_30_percent_regression_in_one_path_fails_that_path(self, monkeypatch):
        # Same 1.3x machine, so the two columns differ only in WHERE the
        # 1.3 is applied. The regressed path is over budget; its siblings,
        # measured on the same slow machine, are not.
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS * 1.3)
        target, healthy = self._CASES["thin_margin"]
        with pytest.raises(AssertionError):
            assert_latency_within(healthy * 1.3 * 1.3, target, "thin_margin")
        for name in ("typical", "roomy"):
            t, h = self._CASES[name]
            assert_latency_within(h * 1.3, t, name)

    def test_a_30_percent_regression_on_a_reference_machine_also_fails(
        self, monkeypatch
    ):
        _pin_calibration(monkeypatch, calibration.CALIBRATION_REFERENCE_SECONDS)
        target, healthy = self._CASES["thin_margin"]
        with pytest.raises(AssertionError):
            assert_latency_within(healthy * 1.3, target, "thin_margin")


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

#: Where the comparison is allowed to be written out.
ALLOWED = {
    ("conftest.py", "assert_latency_within"),
    ("conftest.py", "assert_throughput_at_least"),
}


def _threshold_comparisons(source: str, filename: str) -> List[Tuple[str, str, int]]:
    """Every ``assert <timing> <op> <number>`` in ``source``.

    Returns (filename, enclosing function, line) triples.
    """
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
                    if any(tok in left for tok in TIMING_TOKENS):
                        found.append(
                            (filename, enclosing[-1] if enclosing else "", node.lineno)
                        )
            self.generic_visit(node)

    _Walk().visit(tree)
    return found


class TestOneComparisonSite:
    def test_the_scan_finds_a_planted_violation(self):
        """Positive control: a drifted vocabulary reads exactly like a clean tree."""
        planted = (
            "def test_x():\n"
            "    assert measured.best < 0.200\n"
            "    assert stats['p95_ms'] < 200\n"
            "    assert throughput > 50\n"
            "    assert rss_mb < 1500\n"  # memory: must NOT be flagged
        )
        hits = _threshold_comparisons(planted, "planted.py")
        assert [h[2] for h in hits] == [2, 3, 4]

    def test_the_scan_looks_at_every_benchmark_module(self):
        """A guard that watched the wrong directory would be green forever."""
        modules = sorted(p.name for p in BENCHMARK_DIR.glob("*.py"))
        assert "conftest.py" in modules
        assert len([m for m in modules if m.startswith("test_")]) >= 7

    def test_no_benchmark_compares_a_threshold_outside_the_helpers(self):
        violations = []
        for path in sorted(BENCHMARK_DIR.glob("*.py")):
            for filename, func, lineno in _threshold_comparisons(
                path.read_text(), path.name
            ):
                if (filename, func) in ALLOWED:
                    continue
                violations.append(f"{filename}:{lineno} in {func or '<module>'}")
        assert not violations, (
            "latency/throughput thresholds must go through "
            "assert_latency_within / assert_throughput_at_least so the #908 "
            "calibration applies; found: " + ", ".join(violations)
        )


# ------------------------------------------------------------ the workflow


class TestWorkflowWiring:
    """The calibrated and absolute runs must both actually exist.

    The scheme has two halves and each is useless alone: a calibrated run
    that never checks wall-clock stops measuring the product target, and an
    absolute run on a pull request is the flake #908 is about.
    """

    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load(WORKFLOW.read_text())

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

    def test_the_nightly_job_never_gates_a_pull_request(self):
        condition = self._workflow()["jobs"]["nightly-absolute"]["if"]
        assert "schedule" in condition

    def test_the_schedule_is_nightly(self):
        # `on` parses as the boolean True under YAML 1.1.
        workflow = self._workflow()
        triggers = workflow.get("on") or workflow[True]
        crons = [entry["cron"] for entry in triggers["schedule"]]
        assert crons == ["0 2 * * *"], crons


def test_absolute_env_var_is_not_set_in_this_process():
    """Guard for the file itself: a stray export would make it vacuous."""
    assert calibration.ABSOLUTE_MODE_ENV not in os.environ

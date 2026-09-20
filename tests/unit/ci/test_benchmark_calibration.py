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
from tests.benchmarks import conftest as bench_conftest
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
        assert_latency_within(0.001, 1.0, "probe")
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
        assert_latency_within(0.001, 1.0, "probe")
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

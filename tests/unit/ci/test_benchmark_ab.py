"""The A/B gate: its recorder, its decision rule, and its wiring (#1567).

`tests/benchmarks/` runs in a workflow of its own and is deselected from
both required CI gates, so nothing in the ordinary suite exercises it.
This file is deliberately unmarked, like its sibling
`test_benchmark_calibration.py`, so a broken A/B is found by
`Test Standalone`/`Test Cloud` rather than by a benchmark job a week
later.

What it holds down, and why each one:

* **The recorder is reached from both helpers, and reached on failure.**
  The A/B's whole input is the number each assertion compares. If a
  helper stopped recording, the comparator would see a benchmark
  disappear — and "disappeared" is not a failure, so the gate would
  narrow itself in silence. Recording BEFORE the assert is asserted too:
  a side that is already over its absolute budget is exactly when knowing
  whether the other side was too is most useful.
* **The ratio's orientation.** Throughput is 1/latency, so its ratio is
  inverted. Getting that backwards reports every throughput regression as
  an improvement — silent by construction, and the same shape of error
  #908's docstring warns about for the calibration divisor.
* **An empty comparison is not a pass.** The single most dangerous
  outcome for a relative gate is measuring nothing and saying nothing.
* **One rule gates, and it is the median; a second one only reports.** A
  per-test residual rule and a count rule were both measured on #1567's
  null experiment and rejected by their own numbers. #1989 re-adjudicated
  them on the runner null (#1963): the residual stays rejected, and the
  count rule is computed on every gated comparison and printed, in SHADOW,
  until the evidence says it can fail a pull request. What that costs is
  asserted: a regression confined to a minority of the suite still does NOT
  fail here, and a test says so, because a gap nobody wrote down is a gap
  somebody will later mistake for coverage. And the shadow rule's own
  invariant is asserted from both sides: the report states, on every gated
  comparison, how many benchmarks are at least the count ratio slower and
  whether the rule would fail, and the verdict is the same with the rule
  configured as without it.
* **The comparator imports no application code.** Two editable installs
  of `faultmaven` are made and unmade around it in the A/B job, so a
  comparison of two JSON files must not depend on which is current — and
  an import error anywhere in the application graph would take the
  verdict with it.
* **The two sides really are two trees.** Which tree `import faultmaven`
  resolves to is decided by the step's working directory, `PYTHONPATH`
  and the editable install's finder together, and their precedence
  belongs to setuptools rather than to this workflow. If either side
  resolved to the other's tree the A/B would compare head against head,
  report 1.00x forever and never fail. The workflow asserts each side's
  `faultmaven.__file__` against that side's own root; here we assert
  those two roots differ, because two assertions against one root are
  one assertion.
"""

from __future__ import annotations

import ast
import builtins
import json
import math
import os
import re
from pathlib import Path

import pytest
import yaml

from tests.benchmarks import budgets as benchmark_budgets
from tests.wallclock import ab, calibration, record
from tests.wallclock.assertions import (
    assert_latency_within,
    assert_throughput_at_least,
)
from tests.wallclock.budgets import LatencyBudget, ThroughputBudget

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "benchmarks.yml"
AB_MODULE = REPO_ROOT / "tests" / "wallclock" / "ab.py"
BENCHMARK_DIR = REPO_ROOT / "tests" / "benchmarks"
AB_JOB = "ab-regression"

#: The worst suite median measured with IDENTICAL code on both sides, on the
#: gate's own kind of runner: 144 same-runner ordered pairs of the
#: 19-benchmark suite on 12 GitHub runners, run 38538085168 of a throwaway
#: branch (#1963, 2026-10-14). The one executable copy of that number: the
#: notes in `ab.py` and `benchmarks.yml` name this constant, and
#: `docs/development/testing/performance.md` carries the distribution it is
#: the maximum of. Update them together.
RUNNER_NULL_MAX_MEDIAN = 1.053

#: The shadow count rule's null (#1989), from the same 144 pairs of run
#: 38538085168: for each candidate ratio R, the most benchmarks of the 19
#: that were at least R times slower on any clean pair. The one executable
#: copy; `FM_AB_COUNT_RATIO` has to be a ratio listed here, so a number that
#: was never measured cannot be declared, and `FM_AB_COUNT_MIN` has to clear
#: the maximum by two. (At 1.25 the count is 0 on 133 pairs and 1 on 11.)
RUNNER_NULL_MAX_COUNT = {1.15: 3, 1.20: 2, 1.25: 1, 1.30: 1}

#: The same null, the other way round: the largest value the K-th largest
#: ratio reached on any clean pair, for K = 1..4. "At least K rows at least
#: R slower" is the statement "the K-th largest ratio is at least R", so
#: `FM_AB_COUNT_RATIO` is a margin over `RUNNER_NULL_MAX_KTH_LARGEST[K]`.
RUNNER_NULL_MAX_KTH_LARGEST = {1: 1.749, 2: 1.231, 3: 1.190, 4: 1.149}

#: The most rows at least R slower in any of the 21 real reports of this job
#: (2026-10-12 to 2026-10-14; geometric mean of the median 0.984). At 1.25 it
#: was 1 in 2 of the 21 (0 in 19), and the third-largest ratio's maximum was
#: 1.133. A threshold a real clean pull request has already reached is a
#: gate that is red on a green one.
REAL_GATE_MAX_COUNT = {1.15: 2, 1.20: 2, 1.25: 1, 1.30: 1}

#: The margin the shadow rule's ratio keeps over the null's K-th largest
#: ratio: 1.25 / 1.190 = 1.050. Much thinner than the median's (1.30 / 1.053
#: = 1.23) and deliberately so: at the median gate's own 15% a random
#: quarter of the suite 30% slower is seen in 5% of cases, at 5% in 91%
#: (#1989). What that margin costs is why the rule only reports, and this
#: floor is why it cannot be thinned further without new evidence.
COUNT_RATIO_MARGIN = 1.05

#: The shadow count rule exactly as measured and decided (#1989): the ratio
#: and K. ‼ Pinned EXACTLY, because every other check on it is a floor and a
#: floor is one-sided. 1.30 with K=3 and 1.25 with K=4 both clear the null,
#: and both are strictly worse detectors: 1.30 cuts a random quarter of the
#: suite 30% slower from 91% to 50%, and the fourth-largest ratio at 1.25
#: sees the same quarter in 71% against the third-largest's 92% (the margin
#: sweep in `performance.md`). A looser pair passes every floor and narrows
#: what the rule can see without anyone deciding it. Changing this pair
#: requires re-running the #1989 measurement; move it here, in
#: `benchmarks.yml` and in `performance.md` together.
DECLARED_COUNT_RULE = (1.25, 3)

#: How many benchmark rows the count rule's null was measured at. R = 1.25 and
#: K = 3 are numbers about THIS many rows: K=3 of 19 is a different statement
#: from K=3 of 30.
RUNNER_NULL_SUITE_ROWS = 19

#: The step-down floor for `FM_AB_SUITE`: the threshold the owner decided on
#: (#1963). The docs say a step below it is taken on evidence from the gate's
#: own medians, not on an argument, so a lower value fails here until that
#: evidence exists and this number moves with it.
THRESHOLD_FLOOR = 1.25

#: A producer piped into a short-circuiting reader. ``grep -q`` exits on
#: its first match and closes the pipe; the producer then dies with
#: SIGPIPE, and under ``set -o pipefail`` the whole pipeline reports 141.
#: Measured: ``yes | grep -q y`` under pipefail exits 141.
PIPED_INTO_QUIET_GREP = re.compile(r"\|\s*(?:\\\s*\n\s*)?grep\b[^\n|]*\s-[A-Za-z]*q")


def _shell_code(run: str) -> str:
    """The step's script with whole-line comments removed.

    ‼ The scan's own input. Without this the rule below flags the
    COMMENT that explains the rule, and an author's obvious fix is to
    delete the explanation — which is the one thing that must survive.
    Only whole-line comments are dropped, so a trailing `#` inside a
    quoted string is left alone rather than guessed at.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.fixture
def pinned_calibration():
    """Pin the machine correction at exactly 1.0 for this test.

    ‼ Without it, every probe here that calls the real
    `assert_latency_within` is comparing against `budget * scale`, and
    the scale is how fast the box is. The margins below are chosen for a
    readable failure message, not to survive a slow machine: measured on
    the development box the scale sat at 3.40x-3.79x against an
    effective limit of 9, so the probes pass here and would fail on a
    box a little over twice as slow. `test_benchmark_calibration.py`
    pins `_measured` for the same reason.

    ‼ It does NOT take `monkeypatch`, and the reason is in that file's
    `_clean_calibration` docstring: fixtures finalize in reverse order
    of setup, so a monkeypatch-based undo would run after this restore
    and put the pin back. It also restores `_scale_used`, which is
    SESSION state the terminal-summary hook reads — resetting it
    without restoring erases, for the whole run, the fact that some
    earlier suite asserted a scaled budget.
    """
    state = calibration.calibration_state()
    previous = os.environ.pop(calibration.ABSOLUTE_MODE_ENV, None)
    calibration.reset_calibration_cache()
    calibration._measured = calibration.CALIBRATION_REFERENCE_SECONDS
    try:
        yield
    finally:
        calibration.reset_calibration_cache()
        calibration.restore_calibration_state(state)
        if previous is None:
            os.environ.pop(calibration.ABSOLUTE_MODE_ENV, None)
        else:
            os.environ[calibration.ABSOLUTE_MODE_ENV] = previous


@pytest.fixture
def recorder(tmp_path, monkeypatch, pinned_calibration):
    """Point the recorder at a fresh file and hand back a reader."""
    path = tmp_path / "records.jsonl"
    monkeypatch.setenv(record.RECORD_ENV, str(path))
    record.reset_for_testing()

    def rows():
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    yield rows
    record.reset_for_testing()


def _latency(regression: float = 1.0) -> LatencyBudget:
    """A throwaway budget: these probes are about the recorder, not an anchor."""
    return LatencyBudget(
        "probe",
        regression=regression,
        product_target=regression * 10,
        reference=regression / 2.5,
    )


def _throughput(regression: float = 100.0) -> ThroughputBudget:
    return ThroughputBudget(
        "probe",
        regression=regression,
        product_target=regression / 10,
        reference=regression * 2.5,
    )


class TestTheRecorder:
    def test_it_is_inert_when_the_variable_is_unset(
        self, tmp_path, monkeypatch, pinned_calibration
    ):
        """‼ Asserted by spying on `open`, not by checking a path.

        The first version of this test asserted that a tmp_path file the
        recorder was never told about did not exist — true whatever the
        recorder did, so it passed vacuously. What has to hold is that
        NOTHING is written: an ordinary `pytest tests/benchmarks/`, a
        developer's run and both required CI gates all run with the
        variable unset, and a recorder that wrote anyway would be a new
        file appearing in everyone's working tree.
        """
        monkeypatch.delenv(record.RECORD_ENV, raising=False)
        monkeypatch.chdir(tmp_path)
        opened = []
        real_open = builtins.open

        def spy(file, mode="r", *args, **kwargs):
            if "w" in mode or "a" in mode or "+" in mode:
                opened.append((str(file), mode))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", spy)
        assert_latency_within(0.1, _latency(), "quiet")
        assert_throughput_at_least(500.0, _throughput(), "quiet rate")
        monkeypatch.setattr(builtins, "open", real_open)
        assert opened == [], opened
        assert list(tmp_path.iterdir()) == []

    def test_the_spy_would_have_seen_a_write(self, tmp_path, monkeypatch):
        """The positive control for the test above.

        A spy that never fires is indistinguishable from a recorder that
        never writes, so the same spy is pointed at a recorder that IS
        switched on and must see the append.
        """
        path = tmp_path / "records.jsonl"
        monkeypatch.setenv(record.RECORD_ENV, str(path))
        record.reset_for_testing()
        opened = []
        real_open = builtins.open

        def spy(file, mode="r", *args, **kwargs):
            if "a" in mode:
                opened.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", spy)
        try:
            record.record_comparison(
                metric=ab.LATENCY_METRIC,
                label="control",
                observed=0.1,
                budget=1.0,
                kind="regression budget",
                scale=1.0,
                raw_ratio=1.0,
            )
        finally:
            monkeypatch.setattr(builtins, "open", real_open)
            record.reset_for_testing()
        assert opened == [str(path)], opened

    def test_the_calibration_is_actually_pinned(self, recorder):
        """‼ The fix for an unpinned calibration needs its own check.

        `pinned_calibration` is what stops the probes here comparing
        against `budget * how-fast-this-box-is`. If it ever stopped
        pinning, every margin below would quietly go back to being
        machine-dependent and nothing would say so — which is the defect
        it was added to remove. The recorder writes the scale it used,
        so the pin is observable.
        """
        assert_latency_within(0.25, _latency(1.0), "pinned")
        (row,) = recorder()
        assert row["scale"] == pytest.approx(1.0), (
            "pinned_calibration did not pin; every margin in this file is "
            "then a function of the machine"
        )

    def test_a_latency_comparison_is_recorded(self, recorder):
        assert_latency_within(0.25, _latency(1.0), "Case creation latency")
        (row,) = recorder()
        assert row["scale"] == pytest.approx(1.0)
        assert row["metric"] == ab.LATENCY_METRIC
        assert row["observed"] == pytest.approx(0.25)
        assert row["budget"] == pytest.approx(1.0)
        assert row["label"] == "Case creation latency"

    def test_a_throughput_comparison_is_recorded(self, recorder):
        assert_throughput_at_least(500.0, _throughput(100.0), "Batch throughput")
        (row,) = recorder()
        assert row["metric"] == ab.THROUGHPUT_METRIC
        assert row["observed"] == pytest.approx(500.0)

    def test_a_failing_comparison_is_still_recorded(self, recorder):
        """‼ The number is written BEFORE the assert.

        A benchmark already over its absolute budget is precisely when the
        question "was the base over it too" is worth answering. Recording
        after the assert would drop that row, and a dropped row reads as a
        benchmark that disappeared — which this gate does not fail on.
        """
        with pytest.raises(AssertionError):
            assert_latency_within(9.0, _latency(1.0), "way over")
        with pytest.raises(AssertionError):
            assert_throughput_at_least(1.0, _throughput(100.0), "way under")
        assert [row["label"] for row in recorder()] == ["way over", "way under"]

    def test_the_nodeid_identifies_the_test(self, recorder):
        assert_latency_within(0.1, _latency(), "labelled")
        (row,) = recorder()
        assert row["nodeid"].endswith("test_the_nodeid_identifies_the_test")

    def test_repeated_labels_get_distinct_keys(self, recorder):
        for _ in range(3):
            assert_latency_within(0.1, _latency(), "same label")
        assert [row["occurrence"] for row in recorder()] == [0, 1, 2]

    def test_different_labels_count_independently(self, recorder):
        """Counting per (nodeid, label) keeps the join stable under reordering.

        A per-nodeid counter would number these 0 and 1 in whatever order
        the test happened to make them, so swapping two comparisons inside
        one test would silently pair each against the other's baseline.
        """
        assert_latency_within(0.1, _latency(), "first")
        assert_latency_within(0.1, _latency(), "second")
        assert [(r["label"], r["occurrence"]) for r in recorder()] == [
            ("first", 0),
            ("second", 0),
        ]

    def test_a_nodeid_with_a_space_keeps_its_parameter(self):
        """`PYTEST_CURRENT_TEST` is "<nodeid> (<phase>)" and a nodeid can
        itself contain spaces, so only the trailing phase is removed."""
        os.environ["PYTEST_CURRENT_TEST"] = "t.py::test[a b] (call)"
        try:
            assert record._current_nodeid() == "t.py::test[a b]"
        finally:
            del os.environ["PYTEST_CURRENT_TEST"]

    def test_an_unwritable_destination_raises(self, tmp_path, monkeypatch):
        """Loud, not quiet: a recorder that fails silently hands the
        comparator an empty file, which looks exactly like "nothing
        regressed"."""
        monkeypatch.setenv(record.RECORD_ENV, str(tmp_path / "no" / "such" / "d.jsonl"))
        record.reset_for_testing()
        with pytest.raises(OSError):
            assert_latency_within(0.1, _latency(), "nowhere")


def _rows(*specs):
    """Build one side's loaded rows. Each spec is (name, metric, observed)."""
    return {
        (name, name, 0): ab.Row(
            key=(name, name, 0),
            nodeid=f"tests/benchmarks/test_x.py::{name}",
            label=name,
            metric=metric,
            observed=observed,
            budget=1.0,
        )
        for name, metric, observed in specs
    }


def _suite(scale_by_name):
    """A 50-benchmark side, each at 10ms times its named scale factor.

    ‼ Synthetic, and fifty on purpose. These tests exercise `ab.py`'s
    LOGIC (the median rule, the floor, the classification) at a size chosen
    so that "one benchmark" and "the largest module" are round fractions
    of it; they make no claim about how many benchmarks the real suite has,
    and must not be edited when it changes. The real suite's shape is read
    from the real budget table, where a test needs it
    (`_the_real_suite`, below).
    """
    return _rows(
        *[(f"b{i:02d}", ab.LATENCY_METRIC, 0.010 * scale_by_name(i)) for i in range(50)]
    )


def _the_real_suite():
    """The real benchmark suite's rows, grouped by module, from its budget table.

    A budget records its test's function name and nothing else, and the
    recorder writes one row per comparison, one per test here, so the real
    suite's shape is: for every `ALL_BUDGETS` entry, the module under
    `tests/benchmarks/` that defines `.test`. Read from the table and the
    source, never copied, so it follows the suite when the suite changes.
    Returns `(rows, {module file: [test names]})`.
    """
    defined = {}
    for path in sorted(BENCHMARK_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.setdefault(node.name, []).append(path.name)
    by_module = {}
    for budget in benchmark_budgets.ALL_BUDGETS.values():
        modules = defined.get(budget.test, [])
        assert len(modules) == 1, (budget.test, modules)
        by_module.setdefault(modules[0], []).append(budget.test)
    assert sum(len(tests) for tests in by_module.values()) == len(
        benchmark_budgets.ALL_BUDGETS
    )
    rows = {}
    for module, tests in by_module.items():
        for name in tests:
            nodeid = f"tests/benchmarks/{module}::{name}"
            key = (nodeid, name, 0)
            rows[key] = ab.Row(
                key=key,
                nodeid=nodeid,
                label=name,
                metric=ab.LATENCY_METRIC,
                observed=0.010,
                budget=1.0,
            )
    return rows, by_module


def _without_module(rows, module):
    """`rows` with every row of one benchmark module removed."""
    prefix = f"tests/benchmarks/{module}::"
    return {key: row for key, row in rows.items() if not row.nodeid.startswith(prefix)}


class TestTheDecisionRule:
    def test_identical_sides_compare_at_one(self):
        base = _suite(lambda i: 1.0)
        verdict = ab.compare(base, dict(base), suite_threshold=1.30)
        assert verdict.ok
        assert verdict.median == pytest.approx(1.0)
        assert verdict.worst_residual == pytest.approx(1.0)

    def test_a_single_path_regression_is_reported_but_does_not_fail(self):
        """‼ A declared gap, asserted so nobody mistakes it for coverage.

        One benchmark 40% slower moves no median, so this gate stays
        green. That is deliberate: the per-test residual was rejected on
        #1567's null (1.18x-3.38x at 50 benchmarks on a loaded box) and
        again on the runner null (#1989: 1.03x-1.74x at 19, a tail of
        single-row bursts, so a 15%-margin threshold of 2.0 sees a
        minority regression in under 3% of cases and a one-row +100% in
        46%), and every absolute budget in `tests/benchmarks/budgets.py`
        already fails between 2.46x and 3.57x. The count rule cannot see
        one or two rows either, by construction (asserted below). The
        single-path class belongs to those budgets. What this gate owes it
        is visibility, so the benchmark must still come out top of the
        table.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.4 if i == 7 else 1.0)
        verdict = ab.compare(
            base, head, suite_threshold=1.30, count_ratio=1.25, count_min=3
        )
        assert verdict.ok
        assert verdict.median == pytest.approx(1.0)
        assert verdict.worst.row.label == "b07"
        assert verdict.worst_residual == pytest.approx(1.4)
        # ‼ Counted, and still not enough: one row over is below K = 3.
        assert [entry.row.label for entry in verdict.over] == ["b07"]
        assert verdict.count_would_fail is False
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )
        assert "b07" in rendered
        # The residual's null range is posted on every pull request, so it
        # names where each figure comes from and at how many benchmarks,
        # rather than saying "measured on identical code" in the present
        # tense about a suite that has since changed size.
        assert "1.18x-3.38x at 50 benchmarks" in rendered and "(#1567)" in rendered
        assert "1.03x-1.74x" in rendered and "19 benchmarks" in rendered
        assert "(#1963)" in rendered
        assert "single-row bursts" in rendered

    def test_a_minority_regression_does_not_fail_either(self):
        """The same gap, at the scale it is most likely to be met.

        A quarter of the suite 30% slower leaves the median at 1.0, and the
        verdict stays green. #1567 measured a count rule for exactly this
        case and rejected it on a loaded 50-benchmark box (the null count
        at 1.30x reached 19 of 50, half the suite 30% slower gave 15); the
        runner null (#1989) separates them, so the rule now REPORTS this
        case (`TestTheShadowCountRule`) and still does not fail it.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.3 if i < 12 else 1.0)
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert verdict.ok
        assert verdict.median == pytest.approx(1.0)

    def test_a_uniform_regression_fails(self):
        """‼ The class this gate exists for.

        Every benchmark 30% slower moves the median to 1.30. Absolute
        budgets need 2.46x for the same call, so before this gate a
        uniform +30% — an ORM event listener, a logging or tracing hook,
        a validator on every write — was invisible on every runner.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.3)
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert not verdict.ok
        assert verdict.worst_residual == pytest.approx(1.0)
        assert "whole suite" in verdict.failures[0]

    def test_a_shared_slowdown_of_the_machine_is_what_cancels(self):
        """The A/B's reason to exist, asserted rather than assumed.

        #908 measured the whole pytest process scaling 1.28x between two
        runs of identical code. Here BOTH sides are measured on that
        slower box — which is what "both sides in one job" buys — so every
        ratio is 1.0 and neither rule fires.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.0)
        slow_base = {k: r._replace(observed=r.observed * 1.28) for k, r in base.items()}
        slow_head = {k: r._replace(observed=r.observed * 1.28) for k, r in head.items()}
        verdict = ab.compare(slow_base, slow_head, suite_threshold=1.2)
        assert verdict.ok
        assert verdict.median == pytest.approx(1.0)

    def test_a_throughput_drop_is_a_regression_not_an_improvement(self):
        """‼ Inverting this is silent: every throughput regression would
        read as a 0.7x improvement and no rule would ever fire on one."""
        base = _rows(("rate", ab.THROUGHPUT_METRIC, 100.0))
        head = _rows(("rate", ab.THROUGHPUT_METRIC, 70.0))
        verdict = ab.compare(base, head, suite_threshold=1.2)
        assert verdict.compared[0].ratio == pytest.approx(100.0 / 70.0)
        # With one row the residual is 1.0 by construction (the median IS
        # the ratio), so the SUITE rule is what has to catch this.
        assert not verdict.ok
        assert "whole suite" in verdict.failures[0]

    def test_a_throughput_rise_is_an_improvement(self):
        base = _rows(("rate", ab.THROUGHPUT_METRIC, 100.0))
        head = _rows(("rate", ab.THROUGHPUT_METRIC, 140.0))
        verdict = ab.compare(base, head, suite_threshold=1.2)
        assert verdict.compared[0].ratio == pytest.approx(100.0 / 140.0)
        assert verdict.ok

    def test_a_head_that_lost_the_suite_is_a_failure_not_a_pass(self):
        """‼ The gate narrowing in silence — the outcome it must never
        call green.

        Both benchmark steps are `continue-on-error`, so a head-side
        crash does not red its own step: it arrives here as a handful of
        matched rows whose "suite median" is their own noise. Probed on
        the real comparator before this rule existed, base 50 / head 1
        returned `ok=True` with `median=1.0`.
        """
        base = _suite(lambda i: 1.0)
        head = dict(list(base.items())[:1])
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert not verdict.ok
        assert not verdict.gated
        assert "1 benchmarks against the base's 50" in verdict.failures[0]

    def test_a_reorganised_suite_is_reported_and_not_gated(self):
        """‼ The same collapse from a legitimate cause, told apart.

        A renamed or moved benchmark file changes every `nodeid` at
        once, so nothing matches — but the head still MEASURED the
        suite. Failing that would leave the pull request no exit, which
        is the reason a single deleted benchmark is already reported
        rather than failed. The discriminator is the head's own row
        count, not the matched count.
        """
        base = _suite(lambda i: 1.0)
        head = {
            (f"moved/{k[0]}", k[1], k[2]): r._replace(key=(f"moved/{k[0]}", k[1], k[2]))
            for k, r in base.items()
        }
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert verdict.ok
        # ‼ Green is not the claim. The claim is that it says so.
        assert not verdict.gated
        assert "NOT GATED" in verdict.notes[0]
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )
        assert "not gated" in rendered and "NOT GATED" in rendered

    def test_deleting_the_smaller_module_still_gates(self):
        """The floor is not sized to catch an ordinary deletion.

        The real suite's modules and their row counts come from the real
        budget table, so this cannot describe a suite that no longer
        exists. Deleting the smaller module leaves most of the base, well
        above the floor, and the pull request is still gated.
        """
        base, by_module = _the_real_suite()
        smaller = min(by_module, key=lambda module: len(by_module[module]))
        head = _without_module(base, smaller)
        kept = len(head) / len(base)
        assert kept >= ab.COVERAGE_FLOOR, (
            f"deleting {smaller} would leave {kept:.2f} of the base, under "
            f"the {ab.COVERAGE_FLOOR:.0%} floor: no module of the real suite "
            f"can be deleted and still gated, so the COVERAGE_FLOOR note in "
            f"ab.py and this test describe a suite that no longer exists"
        )
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert verdict.gated and verdict.ok
        assert verdict.coverage == pytest.approx(kept)

    def test_deleting_the_larger_module_fails_the_floor(self):
        """‼ The intended reading, not a gap.

        The larger module holds most of the real suite, so deleting it
        leaves a fragment: its median is not the suite the threshold was
        measured on, and the head recorded fewer than half of the base's
        rows. That is a FAILURE ("the head measured a fragment"), not a
        gate on the remainder and not a pass. A pull request that really
        does retire the module re-measures the null first, which is what
        the message says.
        """
        base, by_module = _the_real_suite()
        larger = max(by_module, key=lambda module: len(by_module[module]))
        head = _without_module(base, larger)
        left = len(head) / len(base)
        assert left < ab.COVERAGE_FLOOR, (
            f"deleting {larger} would leave {left:.2f} of the base, at or "
            f"above the {ab.COVERAGE_FLOOR:.0%} floor: the real suite no "
            f"longer has a module holding most of its rows, so the "
            f"COVERAGE_FLOOR note in ab.py (13 and 6 of 19) is stale"
        )
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert not verdict.ok
        assert not verdict.gated
        assert (
            f"the head recorded {len(head)} benchmarks against the base's "
            f"{len(base)}" in verdict.failures[0]
        )
        assert "fragment" in verdict.failures[0]

    def test_the_floor_note_states_the_real_suite(self):
        """The `COVERAGE_FLOOR` note in ab.py, and the null experiment behind
        the threshold, are about this many benchmarks in this many modules.

        ‼ A tripwire, deliberately. The null measured how noisy a median of
        THIS suite is (#1963: 13 and 6 rows, 19 in all), and one module
        holding 13 of the 19 rows is the reason a contention burst can move
        it. When this fails, the suite changed shape: re-run the null
        experiment if the change is material (the method is in
        `docs/development/testing/performance.md`), then update together the
        note on `COVERAGE_FLOOR`, the `FM_AB_SUITE` and shadow-rule comment
        in benchmarks.yml, `RUNNER_NULL_MAX_MEDIAN` and the count-rule
        constants above (`RUNNER_NULL_MAX_COUNT`,
        `RUNNER_NULL_MAX_KTH_LARGEST`, `REAL_GATE_MAX_COUNT`: the count rule
        was measured on the same 19 rows), the re-measurement in
        `docs/development/testing/performance.md`, and these numbers.
        """
        _, by_module = _the_real_suite()
        assert sorted(len(tests) for tests in by_module.values()) == [6, 13]

    def test_an_empty_base_is_a_failure_not_a_pass(self):
        verdict = ab.compare(
            {},
            _suite(lambda i: 1.0),
            suite_threshold=1.2,
        )
        assert not verdict.ok

    def test_new_and_vanished_benchmarks_are_classified(self):
        base = _rows(
            ("shared", ab.LATENCY_METRIC, 0.01), ("vanished", ab.LATENCY_METRIC, 0.01)
        )
        head = _rows(
            ("shared", ab.LATENCY_METRIC, 0.01), ("added", ab.LATENCY_METRIC, 0.01)
        )
        verdict = ab.compare(base, head, suite_threshold=1.2)
        assert [row.label for row in verdict.head_only] == ["added"]
        assert [row.label for row in verdict.base_only] == ["vanished"]
        assert verdict.ok
        # Both are reported, and the vanished one is flagged — a head that
        # stops emitting a benchmark shrinks what the gate can see.
        rendered = ab.render(
            verdict,
            base_label="b",
            head_label="h",
            suite_threshold=1.2,
        )
        assert "added" in rendered and "vanished" in rendered

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
    def test_an_unusable_observation_is_reported_not_ratioed(self, bad):
        """One broken row is dropped from the ratio and named, and the
        other forty-nine still decide."""
        base = _suite(lambda i: 1.0)
        victim = list(base)[0]
        base = dict(base)
        base[victim] = base[victim]._replace(observed=bad)
        verdict = ab.compare(base, _suite(lambda i: 1.0), suite_threshold=1.30)
        assert [row.label for row, _ in verdict.unusable] == ["b00"]
        assert len(verdict.compared) == 49
        assert verdict.gated and verdict.ok

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
    def test_a_suite_of_unusable_observations_is_not_a_pass(self, bad):
        """‼ And when they are ALL broken, the gate does not report a
        clean run: nothing is comparable, so nothing was gated."""
        base = {k: r._replace(observed=bad) for k, r in _suite(lambda i: 1.0).items()}
        verdict = ab.compare(base, _suite(lambda i: 1.0), suite_threshold=1.30)
        assert len(verdict.unusable) == 50
        assert not verdict.gated

    def test_a_metric_that_changed_kind_is_not_compared(self):
        verdict = ab.compare(
            _rows(("x", ab.LATENCY_METRIC, 0.01)),
            _rows(("x", ab.THROUGHPUT_METRIC, 100.0)),
            suite_threshold=1.2,
        )
        assert "metric changed" in verdict.unusable[0][1]


def _write_side(path, scale_by_index, *, rows=19):
    """One side as a recorder file: `rows` latencies at 10 ms times a scale.

    Nineteen, the real suite's size, because `main` is what the job runs
    and its output is what a scorer reads.
    """
    path.write_text(
        "".join(
            json.dumps(
                {
                    "v": record.RECORD_FORMAT_VERSION,
                    "nodeid": f"tests/benchmarks/test_x.py::test_{i:02d}",
                    "label": f"row {i:02d}",
                    "occurrence": 0,
                    "metric": ab.LATENCY_METRIC,
                    "observed": 0.010 * scale_by_index(i),
                    "budget": 1.0,
                    "kind": "regression budget",
                    "scale": 1.0,
                },
                sort_keys=True,
            )
            + "\n"
            for i in range(rows)
        )
    )


class TestTheShadowCountRule:
    """The count rule (#1989): reported on every gated comparison, never gating.

    The invariant has two halves and both are asserted: the report states
    how many benchmarks are at least the count ratio slower and whether the
    rule would fail, and the job's verdict is the one it would have had
    without the rule.
    """

    RULE = {"count_ratio": 1.25, "count_min": 3}

    def test_a_minority_regression_is_counted_and_does_not_fail(self):
        """‼ The class the median cannot see, seen, and the job still green.

        A quarter of the suite (12 of 50) 30% slower: the median is 1.0 and
        the verdict ok, while the shadow rule counts 12 rows over 1.25 and
        says it would fail at 3.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.3 if i < 12 else 1.0)
        verdict = ab.compare(base, head, suite_threshold=1.30, **self.RULE)
        assert verdict.gated and verdict.ok and verdict.failures == []
        assert verdict.median == pytest.approx(1.0)
        assert len(verdict.over) == 12
        assert verdict.count_would_fail is True
        assert (verdict.count_ratio, verdict.count_min) == (1.25, 3)
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )
        assert (
            "Shadow rule (not gating yet, #1989): 12 of 50 benchmarks "
            "≥ 1.25x slower; would fail at 3" in rendered
        )
        assert "would fail this pull request if it gated" in rendered
        assert (
            "The job's verdict is the median rule's alone; this line does not "
            "change it" in rendered
        )
        # It names the rows, and it is not dressed as a failure.
        assert all(f"b{i:02d}" in rendered for i in range(12))
        assert "#### Regression" not in rendered

    def test_the_rule_reports_when_it_would_not_fail(self):
        """The same line says so when it is clean: a silent clean report
        and a missing rule would look the same."""
        base = _suite(lambda i: 1.0)
        verdict = ab.compare(base, dict(base), suite_threshold=1.30, **self.RULE)
        assert verdict.count_would_fail is False and verdict.over == []
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )
        assert (
            "Shadow rule (not gating yet, #1989): 0 of 50 benchmarks "
            "≥ 1.25x slower; would fail at 3, so it would not fail this "
            "pull request" in rendered
        )

    def test_the_rows_come_worst_first(self):
        base = _suite(lambda i: 1.0)
        slowdown = {3: 1.6, 9: 1.3, 20: 1.45, 31: 1.26, 40: 1.24}
        head = _suite(lambda i: slowdown.get(i, 1.0))
        verdict = ab.compare(base, head, suite_threshold=1.30, **self.RULE)
        assert [entry.row.label for entry in verdict.over] == [
            "b03",
            "b20",
            "b09",
            "b31",
        ], "b40 at 1.24x is under the ratio and must not be counted"

    @pytest.mark.parametrize(("over", "more"), [(15, None), (16, 1), (20, 5), (40, 25)])
    def test_a_long_list_of_rows_is_cut_at_the_table_size_and_says_so(self, over, more):
        """The `…N more` branch: the list stops at `TABLE_ROWS` and counts
        the rest, so a 40-row regression cannot print 40 lines into a
        pull-request comment and cannot hide that it was cut.

        ‼ N is `over - TABLE_ROWS` (20 over rows are `…5 more`, 40 are
        `…25 more`); at exactly `TABLE_ROWS` there is no suffix at all.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.3 if i < over else 1.0)
        verdict = ab.compare(base, head, suite_threshold=1.40, **self.RULE)
        assert len(verdict.over) == over
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.40
        )
        listed = [line for line in rendered.splitlines() if line.startswith("- b")]
        assert (
            len(listed)
            == min(over, ab.TABLE_ROWS)
            == len([line for line in listed if "1.30x" in line])
        )
        suffix = [line for line in rendered.splitlines() if line.startswith("- …")]
        if more is None:
            assert suffix == []
        else:
            assert suffix == [f"- …{more} more"]
        assert f"{over} of 50 benchmarks ≥ 1.25x slower" in rendered

    @pytest.mark.parametrize(
        ("scale", "label"),
        [
            (lambda i: 1.3 if i < 12 else 1.0, "a quarter 30% slower"),
            (lambda i: 1.3, "the whole suite 30% slower"),
            (lambda i: 1.0, "identical sides"),
            (lambda i: 1.4 if i == 7 else 1.0, "one row 40% slower"),
            (lambda i: 0.7, "the whole suite faster"),
        ],
    )
    def test_the_verdict_is_the_one_it_would_have_had_without_the_rule(
        self, scale, label
    ):
        """‼ The invariant's second half, probed against the unconfigured run.

        Everything the job decides on, and everything the reader is shown
        as a failure or a coverage note, is identical with and without the
        rule; only the shadow fields differ.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(scale)
        plain = ab.compare(base, head, suite_threshold=1.30)
        shadow = ab.compare(base, head, suite_threshold=1.30, **self.RULE)
        assert shadow.ok is plain.ok, label
        assert shadow.failures == plain.failures, label
        assert shadow.notes == plain.notes, label
        assert shadow.gated is plain.gated, label
        assert shadow.median == plain.median, label
        assert shadow.compared == plain.compared, label

    def test_a_uniform_regression_fails_by_the_median_alone(self):
        """The rule is not a second reason to fail: the one failure is the
        median's, even though every row is over the count ratio."""
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.3)
        verdict = ab.compare(base, head, suite_threshold=1.30, **self.RULE)
        assert not verdict.ok
        assert len(verdict.failures) == 1 and "whole suite" in verdict.failures[0]
        assert len(verdict.over) == 50 and verdict.count_would_fail is True
        # ‼ The line sits above a `#### Regression` section when the median
        # fails, so it must not claim the verdict is "above" or unchanged in
        # a way that reads as clean: it says whose rule decides the job.
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )
        assert "#### Regression" in rendered
        assert "The job's verdict is the median rule's alone" in rendered
        assert "verdict above" not in rendered

    def test_it_is_inert_when_it_is_not_configured(self):
        """No arguments, no shadow line, no shadow state: the comparator is
        what it was before #1989 for any caller that does not ask."""
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.3 if i < 12 else 1.0)
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert verdict.count_ratio is None and verdict.count_min is None
        assert verdict.count_would_fail is None and verdict.over == []
        rendered = ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )
        assert "Shadow rule" not in rendered

    def test_it_is_inert_when_the_comparison_is_not_gated(self):
        """‼ Not evaluated is not clean.

        A reorganised suite and a fragment are not what the count was
        measured on. The rule does not run, the report does not claim it
        did, and `count_would_fail` is None rather than False so a scorer
        cannot count the run as clean.
        """
        base = _suite(lambda i: 1.0)
        moved = {
            (f"moved/{k[0]}", k[1], k[2]): r._replace(
                key=(f"moved/{k[0]}", k[1], k[2]), observed=r.observed * 1.5
            )
            for k, r in base.items()
        }
        fragment = dict(list(base.items())[:1])
        for head in (moved, fragment):
            verdict = ab.compare(base, head, suite_threshold=1.30, **self.RULE)
            assert not verdict.gated
            assert verdict.count_would_fail is None and verdict.over == []
            rendered = ab.render(
                verdict, base_label="b", head_label="h", suite_threshold=1.30
            )
            assert "Shadow rule" not in rendered

    def test_a_ratio_exactly_at_the_threshold_is_counted(self):
        """`at least`, not `more than`: both boundaries (the ratio and K).

        The values are binary fractions so the ratio is exactly 1.25 rather
        than 1.2500000000000002 or 1.2499999999999998.
        """

        def side(heads):
            base = {}
            head = {}
            for index, observed in enumerate(heads):
                key = (f"t.py::t{index}", f"t{index}", 0)
                for target, value in ((base, 0.5), (head, observed)):
                    target[key] = ab.Row(
                        key=key,
                        nodeid=key[0],
                        label=key[1],
                        metric=ab.LATENCY_METRIC,
                        observed=value,
                        budget=1.0,
                    )
            return base, head

        on = 0.625  # exactly 1.25x
        just_under = 0.62499
        three_on, six = side([on, on, on, 0.5, 0.5, 0.5])
        verdict = ab.compare(three_on, six, suite_threshold=1.30, **self.RULE)
        assert len(verdict.over) == 3 and verdict.count_would_fail is True
        two_on, six = side([on, on, just_under, 0.5, 0.5, 0.5])
        verdict = ab.compare(two_on, six, suite_threshold=1.30, **self.RULE)
        assert len(verdict.over) == 2 and verdict.count_would_fail is False

    def test_a_throughput_drop_is_counted_and_a_rise_is_not(self):
        """The count reads the ratio, which is oriented so above 1.0 is
        worse, so a rate that fell is a slower row and a rate that rose is
        not. Getting this backwards would count every improvement."""
        base = _rows(*[(f"rate{i}", ab.THROUGHPUT_METRIC, 100.0) for i in range(6)])
        head = _rows(
            *[
                (f"rate{i}", ab.THROUGHPUT_METRIC, 70.0 if i < 3 else 140.0)
                for i in range(6)
            ]
        )
        verdict = ab.compare(base, head, suite_threshold=1.60, **self.RULE)
        assert [entry.row.label for entry in verdict.over] == [
            "rate0",
            "rate1",
            "rate2",
        ]
        assert verdict.count_would_fail is True

    def test_a_row_that_could_not_be_compared_is_not_counted(self):
        base = _suite(lambda i: 1.0)
        base = dict(base)
        victim = list(base)[0]
        base[victim] = base[victim]._replace(observed=float("nan"))
        head = _suite(lambda i: 1.3)
        verdict = ab.compare(base, head, suite_threshold=1.40, **self.RULE)
        assert len(verdict.over) == 49 and len(verdict.compared) == 49

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"count_ratio": 1.25},
            {"count_min": 3},
            {"count_ratio": 1.0, "count_min": 3},
            {"count_ratio": 0.8, "count_min": 3},
            {"count_ratio": float("nan"), "count_min": 3},
            {"count_ratio": 1.25, "count_min": 0},
        ],
    )
    def test_a_half_configured_or_meaningless_rule_is_refused(self, kwargs):
        """A rule that silently never ran, or ran on a number nobody chose,
        would look exactly like a clean report."""
        base = _suite(lambda i: 1.0)
        with pytest.raises(ValueError):
            ab.compare(base, dict(base), suite_threshold=1.30, **kwargs)

    def test_the_command_line_takes_both_or_neither(self, tmp_path, capsys):
        base, head = tmp_path / "base.jsonl", tmp_path / "head.jsonl"
        _write_side(base, lambda i: 1.0)
        _write_side(head, lambda i: 1.0)
        common = [
            "--base",
            str(base),
            "--head",
            str(head),
            "--suite-threshold",
            "1.30",
        ]
        for extra in (["--count-ratio", "1.25"], ["--count-min", "3"]):
            with pytest.raises(SystemExit) as exit_:
                ab.main(common + extra)
            assert exit_.value.code == 2
            assert "together or not at all" in capsys.readouterr().err
        with pytest.raises(SystemExit) as exit_:
            ab.main(common + ["--count-ratio", "0.9", "--count-min", "3"])
        assert exit_.value.code == 2

    def test_the_job_exit_status_and_report_through_main(self, tmp_path, capsys):
        """The whole path the workflow takes: a quarter of 19 rows 30% slower.

        Exit status 0 (the Verdict step turns anything else into a red job),
        and the markdown and the JSON both carry the rule.
        """
        base, head = tmp_path / "base.jsonl", tmp_path / "head.jsonl"
        _write_side(base, lambda i: 1.0)
        _write_side(head, lambda i: 1.3 if i < 5 else 1.0)
        markdown, report = tmp_path / "ab_report.md", tmp_path / "ab_report.json"
        status = ab.main(
            [
                "--base",
                str(base),
                "--head",
                str(head),
                "--suite-threshold",
                "1.30",
                "--count-ratio",
                "1.25",
                "--count-min",
                "3",
                "--markdown-out",
                str(markdown),
                "--json-out",
                str(report),
            ]
        )
        assert status == 0
        assert capsys.readouterr().out == markdown.read_text(encoding="utf-8")
        assert (
            "Shadow rule (not gating yet, #1989): 5 of 19 benchmarks "
            "≥ 1.25x slower; would fail at 3" in markdown.read_text(encoding="utf-8")
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["ok"] is True and data["failures"] == []
        assert data["gated"] is True
        assert data["count_ratio"] == 1.25 and data["count_min"] == 3
        assert data["count_would_fail"] is True
        assert data["over_count"] == 5 and len(data["over"]) == 5
        assert {entry["benchmark"] for entry in data["over"]} == {
            f"tests/benchmarks/test_x.py::test_{i:02d}|row {i:02d}|0" for i in range(5)
        }
        assert all(entry["ratio"] == pytest.approx(1.3) for entry in data["over"])
        # The `over` keys are the `ratios` keys, so a scorer can join them.
        assert {entry["benchmark"] for entry in data["over"]} <= set(data["ratios"])

    def test_the_json_says_not_evaluated_rather_than_clean(self, tmp_path):
        """‼ What a scorer reads. `null` is "the rule did not run", and only
        `false` is a run it passed: scoring null as false would count a run
        that measured nothing toward promotion."""
        base, head = tmp_path / "base.jsonl", tmp_path / "head.jsonl"
        _write_side(base, lambda i: 1.0)
        _write_side(head, lambda i: 1.3 if i < 5 else 1.0)
        unconfigured = tmp_path / "unconfigured.json"
        ab.main(
            [
                "--base",
                str(base),
                "--head",
                str(head),
                "--suite-threshold",
                "1.30",
                "--json-out",
                str(unconfigured),
            ]
        )
        data = json.loads(unconfigured.read_text(encoding="utf-8"))
        assert data["count_ratio"] is None and data["count_min"] is None
        assert data["count_would_fail"] is None and data["over_count"] is None
        assert data["over"] == []

        # Configured but not gated: the head measured one row of 19.
        _write_side(head, lambda i: 1.3, rows=1)
        fragment = tmp_path / "fragment.json"
        status = ab.main(
            [
                "--base",
                str(base),
                "--head",
                str(head),
                "--suite-threshold",
                "1.30",
                "--count-ratio",
                "1.25",
                "--count-min",
                "3",
                "--json-out",
                str(fragment),
            ]
        )
        data = json.loads(fragment.read_text(encoding="utf-8"))
        assert status == 1 and data["gated"] is False
        assert data["count_would_fail"] is None and data["over_count"] is None
        assert data["count_ratio"] == 1.25 and data["count_min"] == 3

    def test_a_clean_gated_run_is_false_and_zero(self, tmp_path):
        base, head = tmp_path / "base.jsonl", tmp_path / "head.jsonl"
        _write_side(base, lambda i: 1.0)
        _write_side(head, lambda i: 1.0 if i != 3 else 1.3)
        report = tmp_path / "ab_report.json"
        ab.main(
            [
                "--base",
                str(base),
                "--head",
                str(head),
                "--suite-threshold",
                "1.30",
                "--count-ratio",
                "1.25",
                "--count-min",
                "3",
                "--json-out",
                str(report),
            ]
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["count_would_fail"] is False and data["over_count"] == 1


class TestLoading:
    def _write(self, path, rows):
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))

    def _row(self, **overrides):
        row = {
            "v": record.RECORD_FORMAT_VERSION,
            "nodeid": "t.py::test_a",
            "label": "l",
            "occurrence": 0,
            "metric": ab.LATENCY_METRIC,
            "observed": 0.010,
            "budget": 1.0,
            "kind": "regression budget",
            "scale": 1.0,
        }
        row.update(overrides)
        return row

    def test_several_files_reduce_to_the_best_latency(self, tmp_path):
        """Repeats on one side are reduced by the MINIMUM, for the same
        one-sided-error reason `measure_min_latency` takes a minimum."""
        a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        self._write(a, [self._row(observed=0.020)])
        self._write(b, [self._row(observed=0.011)])
        (row,) = ab.load([a, b]).values()
        assert row.observed == pytest.approx(0.011)

    def test_several_files_reduce_to_the_best_throughput(self, tmp_path):
        """And by the MAXIMUM for a rate — the same "best", inverted."""
        a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        self._write(a, [self._row(metric=ab.THROUGHPUT_METRIC, observed=120.0)])
        self._write(b, [self._row(metric=ab.THROUGHPUT_METRIC, observed=90.0)])
        (row,) = ab.load([a, b]).values()
        assert row.observed == pytest.approx(120.0)

    def test_the_reduction_does_not_depend_on_argv_order(self, tmp_path):
        """‼ A detector whose answer depends on its command line.

        `min`/`max` are not order-invariant across a NaN — `min(nan, x)`
        is `nan` and `min(x, nan)` is `x` — so before this was fixed the
        same two record files reduced to a usable number or an unusable
        one depending only on which was passed first.
        """
        a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        self._write(a, [self._row(observed=float("nan"))])
        self._write(b, [self._row(observed=0.011)])
        forward = list(ab.load([a, b]).values())[0].observed
        backward = list(ab.load([b, a]).values())[0].observed
        assert forward == backward == pytest.approx(0.011)

    def test_an_all_non_finite_key_survives_as_unusable(self):
        """Dropping the non-finite values must not drop the ROW: a key
        that vanishes reads as a missing benchmark, and a missing
        benchmark is not reported the same way a broken one is."""
        assert math.isnan(ab._better(ab.LATENCY_METRIC, float("nan"), float("nan")))

    def test_an_unsupported_format_version_is_refused(self, tmp_path):
        """‼ The base side is an arbitrary commit on `main`.

        A base that carries the recorder but writes an older row shape
        must be SKIPPED by the workflow, not parsed. This is the
        backstop for when it is not: refuse loudly rather than
        mis-reading a renamed field as a missing one.
        """
        path = tmp_path / "a.jsonl"
        self._write(path, [self._row(v=99)])
        with pytest.raises(ValueError, match="record format version"):
            ab.load([path])

    def test_a_record_with_no_version_is_refused(self, tmp_path):
        path = tmp_path / "a.jsonl"
        row = self._row()
        del row["v"]
        self._write(path, [row])
        with pytest.raises(ValueError, match="record format version"):
            ab.load([path])

    def test_a_malformed_line_raises_rather_than_being_skipped(self, tmp_path):
        """Skipping it would report a broken recorder as a missing
        benchmark, and a missing benchmark is not a failure."""
        path = tmp_path / "a.jsonl"
        path.write_text('{"nodeid": "t", "label"\n')
        with pytest.raises(ValueError):
            ab.load([path])

    def test_a_record_missing_a_field_raises(self, tmp_path):
        path = tmp_path / "a.jsonl"
        row = self._row()
        del row["observed"]
        self._write(path, [row])
        with pytest.raises(ValueError):
            ab.load([path])

    def test_an_unknown_metric_is_refused(self, tmp_path):
        """‼ Refuse what this module was not taught.

        A third metric added to `record.py` alone would be reduced as a
        throughput by `load` and dropped as unusable by `_ratio` — so the
        benchmark would silently go missing from the comparison instead
        of failing it.
        """
        path = tmp_path / "a.jsonl"
        self._write(path, [self._row(metric="bytes_allocated")])
        with pytest.raises(ValueError, match="unknown metric"):
            ab.load([path])

    def test_blank_lines_are_tolerated(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(json.dumps(self._row()) + "\n\n")
        assert len(ab.load([path])) == 1

    def test_a_real_recorder_file_round_trips(self, recorder, tmp_path, monkeypatch):
        """End to end: what `record.py` writes is what `ab.load` reads.

        Two modules, two formats, one join key — asserted together so a
        field renamed on one side cannot pass its own tests.
        """
        path = Path(os.environ[record.RECORD_ENV])
        assert_latency_within(0.012, _latency(1.0), "round trip")
        assert_throughput_at_least(500.0, _throughput(100.0), "round trip rate")
        loaded = ab.load([path])
        assert len(loaded) == 2
        assert {row.metric for row in loaded.values()} == {
            ab.LATENCY_METRIC,
            ab.THROUGHPUT_METRIC,
        }


def test_the_comparator_reads_what_the_recorder_writes():
    """‼ The two halves of the version handshake, pinned to each other.

    `record.py` stamps `RECORD_FORMAT_VERSION` on every row and
    `ab.py` lists what it can read. Bumping one without the other makes
    the workflow skip every comparison (green, silent, gate off) or the
    comparator refuse every row (red, every pull request). Neither is
    discoverable from either file alone.
    """
    assert record.RECORD_FORMAT_VERSION in ab.SUPPORTED_RECORD_VERSIONS


def test_the_comparator_reads_a_literal_v1_row(tmp_path):
    """A base tree older than #1910 still writes v1; the head must read it."""
    path = tmp_path / "base.jsonl"
    path.write_text(
        '{"budget": 1.0, "kind": "regression budget", "label": "l", '
        '"metric": "latency_seconds", "nodeid": "t.py::test_a", '
        '"observed": 0.01, "occurrence": 0, "scale": 1.0, "v": 1}\n'
    )
    (row,) = ab.load([path]).values()
    assert row.observed == 0.01


def test_the_metric_names_are_the_same_on_both_sides():
    """`ab.py` re-declares them rather than importing `record.py`, so that
    it stays stdlib-only. That duplication is pinned here."""
    assert ab.LATENCY_METRIC == record.LATENCY_METRIC
    assert ab.THROUGHPUT_METRIC == record.THROUGHPUT_METRIC


def test_the_comparator_imports_no_application_code():
    """‼ Two editable installs are made and unmade around this step, so
    the verdict must not depend on which is current — and an import error
    anywhere in the application graph would take the verdict with it."""
    tree = ast.parse(AB_MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"faultmaven", "pytest", "tests"}), sorted(imported)


class TestWorkflowWiring:
    @staticmethod
    def _workflow() -> dict:
        return yaml.safe_load(WORKFLOW.read_text())

    def _job(self) -> dict:
        jobs = self._workflow()["jobs"]
        assert AB_JOB in jobs, f"{AB_JOB} is gone from the workflow"
        return jobs[AB_JOB]

    def _run_steps(self) -> list:
        return [
            step
            for step in self._job()["steps"]
            if record.RECORD_ENV in (step.get("env") or {})
        ]

    def test_both_sides_run_in_one_job(self):
        """‼ The ruling's first constraint, and the whole mechanism.

        Two jobs are two runners, which reintroduces exactly the
        machine-to-machine variance the A/B exists to cancel — #908
        measured it at 1.28x on identical code. So the two recorded runs
        must be two steps of ONE job.
        """
        steps = self._run_steps()
        # Exactly two on purpose. Measuring a side more than once is a
        # defensible change, but it changes the statistic the thresholds
        # were sized against, so it should have to edit this line.
        assert len(steps) == 2, (
            "expected exactly two recorded benchmark runs in this job, got "
            f"{[step.get('name') for step in steps]}"
        )
        destinations = {step["env"][record.RECORD_ENV] for step in steps}
        assert len(destinations) == 2, destinations

    def test_the_two_sides_are_two_trees(self):
        """‼ The failure that is silent by construction.

        If both steps import the same `faultmaven`, the A/B compares head
        against head, reports 1.00x forever and can never fail. Each step
        asserts its own root; these roots must differ, or the two
        assertions are one assertion.
        """
        roots = [step["env"]["AB_EXPECTED_ROOT"] for step in self._run_steps()]
        assert len(set(roots)) == 2, roots
        for step in self._run_steps():
            assert "faultmaven.__file__" in step["run"], step.get("name")
            assert "AB_EXPECTED_ROOT" in step["run"], step.get("name")

    def test_it_runs_only_on_pull_requests(self):
        assert self._job()["if"] == "github.event_name == 'pull_request'"

    def test_it_does_not_set_absolute_mode(self):
        """The product targets are the nightly's question. Setting them
        here would fail most benchmarks on both sides and measure nothing
        new.

        ‼ Job level AND step level. GitHub merges the two, so a guard
        that reads only `steps[].env` is blind to the place someone would
        most naturally put it — `FM_AB_SUITE` already lives in this job's
        `env:` block, so that is the block a reader reaches for.
        """
        job = self._job()
        assert "FM_BENCHMARK_ABSOLUTE" not in (job.get("env") or {})
        for step in job["steps"]:
            assert "FM_BENCHMARK_ABSOLUTE" not in (step.get("env") or {})

    def test_the_threshold_is_declared_once_and_is_clear_of_the_noise(self):
        """‼ The gate's whole credibility is this number.

        Two floors, and they answer different questions. `THRESHOLD_FLOOR`
        (1.25) is the decided threshold: lowering `FM_AB_SUITE` below it is
        a re-anchoring that waits for evidence from the gate's own medians,
        not a tweak. `RUNNER_NULL_MAX_MEDIAN` is the noise: the null
        experiment on the gate's own kind of runner (#1963, run
        38538085168: identical code on both sides, 144 same-runner ordered
        pairs of the 19-benchmark suite on 12 GitHub runners) put the suite
        median anywhere up to it, and #1567's, at 50 benchmarks on a loaded
        box, up to 1.216. A threshold near the worst observation is a gate
        that goes red on a clean pull request, and a flaky performance gate
        gets muted, which is how #908 began.
        """
        env = self._job()["env"]
        assert "FM_AB_SUITE" in env, env
        threshold = float(env["FM_AB_SUITE"])
        assert threshold >= THRESHOLD_FLOOR, (
            f"FM_AB_SUITE is {threshold}, below the decided {THRESHOLD_FLOOR}: "
            f"a step down is a re-anchoring taken on evidence from the gate's "
            f"own medians (the note in benchmarks.yml), not a tweak"
        )
        margin = 1.15
        assert threshold >= RUNNER_NULL_MAX_MEDIAN * margin, (
            f"the suite median reached {RUNNER_NULL_MAX_MEDIAN}x on identical "
            f"code on the gate's own kind of runner (#1963); a threshold "
            f"within {margin - 1:.0%} of it will flake"
        )

    def test_the_comparison_step_passes_the_threshold(self):
        (step,) = [
            step
            for step in self._job()["steps"]
            if "python -m tests.wallclock.ab" in (step.get("run") or "")
        ]
        assert "--suite-threshold" in step["run"]
        assert "FM_AB_SUITE" in step["run"]

    def test_the_count_rule_is_exactly_the_pair_that_was_measured(self):
        """‼ One-sided floors let a worse pair through; this one does not.

        `DECLARED_COUNT_RULE` says why 1.30/3 and 1.25/4 are not "also fine".
        """
        env = self._job()["env"]
        declared = (float(env["FM_AB_COUNT_RATIO"]), int(env["FM_AB_COUNT_MIN"]))
        assert declared == DECLARED_COUNT_RULE, (
            f"the shadow count rule is {declared}, not the measured "
            f"{DECLARED_COUNT_RULE}: a different pair needs the #1989 "
            f"measurement re-run, and this constant, benchmarks.yml and "
            f"performance.md moved together"
        )

    def test_the_count_rule_was_measured_at_this_suite_size(self):
        """‼ R=1.25 and K=3 are statements about 19 rows.

        The null count (at most 1 of 19 at 1.25) and the third-largest
        ratio's null maximum (1.190) are both functions of how many rows
        there are, so a suite that gained or lost rows has not been
        measured. When this fails, re-run the null on the new suite
        (`docs/development/testing/performance.md`) before moving
        `RUNNER_NULL_SUITE_ROWS` and the numbers it stands behind.
        """
        _, by_module = _the_real_suite()
        rows = sum(len(tests) for tests in by_module.values())
        assert rows == RUNNER_NULL_SUITE_ROWS, (
            f"the suite has {rows} benchmarks; the count rule's ratio and K "
            f"were measured at {RUNNER_NULL_SUITE_ROWS}"
        )

    def test_the_count_rule_is_declared_once_and_is_clear_of_the_null(self):
        """‼ The shadow rule's two numbers, held to the null they came from.

        The rule reports instead of gating, so a number that flaps costs a
        misleading line rather than a red job; that is not a reason to
        declare one the null never measured, because promotion (#1989) will
        copy these two numbers into a gate. Held to: a ratio the runner
        null measured (`RUNNER_NULL_MAX_COUNT`, run 38538085168), a K at
        least two clear of the most rows that ever reached that ratio on a
        clean pair, a K clear of what a real green pull request reached
        (`REAL_GATE_MAX_COUNT`, the 21 real reports), and a ratio at least
        `COUNT_RATIO_MARGIN` over the K-th largest ratio the null reached
        (1.190 for K = 3). Declared in the job's `env:` block and nowhere
        else, so there is one place to change it and this test reads it.
        """
        job = self._job()
        env = job["env"]
        assert {"FM_AB_COUNT_RATIO", "FM_AB_COUNT_MIN"} <= set(env), env
        ratio = float(env["FM_AB_COUNT_RATIO"])
        minimum = int(env["FM_AB_COUNT_MIN"])
        for step in job["steps"]:
            assert not {"FM_AB_COUNT_RATIO", "FM_AB_COUNT_MIN"} & set(
                step.get("env") or {}
            ), f"{step.get('name')} redeclares the count rule's numbers"
        assert ratio in RUNNER_NULL_MAX_COUNT, (
            f"FM_AB_COUNT_RATIO is {ratio}, a ratio the runner null never "
            f"measured ({sorted(RUNNER_NULL_MAX_COUNT)}): measure it first"
        )
        assert minimum >= RUNNER_NULL_MAX_COUNT[ratio] + 2, (
            f"at {ratio}x up to {RUNNER_NULL_MAX_COUNT[ratio]} of 19 rows were "
            f"over on identical code (#1989), so K={minimum} is within one "
            f"row of the noise"
        )
        assert minimum >= REAL_GATE_MAX_COUNT[ratio] + 2, (
            f"a real green pull request had {REAL_GATE_MAX_COUNT[ratio]} rows "
            f"at {ratio}x or more; K={minimum} is within one row of that"
        )
        assert minimum in RUNNER_NULL_MAX_KTH_LARGEST, (
            f"no null maximum is recorded for the K={minimum}-th largest "
            f"ratio ({sorted(RUNNER_NULL_MAX_KTH_LARGEST)})"
        )
        floor = RUNNER_NULL_MAX_KTH_LARGEST[minimum] * COUNT_RATIO_MARGIN
        assert ratio >= floor, (
            f"the {minimum}-th largest ratio reached "
            f"{RUNNER_NULL_MAX_KTH_LARGEST[minimum]} on identical code; "
            f"{ratio} is under a {COUNT_RATIO_MARGIN - 1:.0%} margin over it"
        )

    def test_the_comparison_step_passes_the_count_rule(self):
        """‼ A rule that is declared and never passed reports nothing and
        looks like a rule that found nothing. Both numbers reach the
        comparator from the env, so the line in the report is the same
        numbers this file pins."""
        (step,) = [
            step
            for step in self._job()["steps"]
            if "python -m tests.wallclock.ab" in (step.get("run") or "")
        ]
        run = _shell_code(step["run"])
        assert '--count-ratio "${FM_AB_COUNT_RATIO}"' in run
        assert '--count-min "${FM_AB_COUNT_MIN}"' in run

    def test_the_count_rule_does_not_gate_the_job(self):
        """‼ The invariant's second half, at the workflow.

        The job's verdict is the comparator's exit status, carried by one
        output to the Verdict step, and the comparator's status is `ok`,
        which is the median's failures. So the count rule has no path to a
        red job: nothing in the workflow reads its fields, and the Verdict
        step reads the status alone.
        """
        steps = self._job()["steps"]
        verdict = next(s for s in steps if s.get("name") == "Verdict")
        assert set(verdict["env"]) == {"COMPARE_STATUS"}, verdict["env"]
        for step in steps:
            code = _shell_code(step.get("run") or "")
            assert "count_would_fail" not in code, step.get("name")
            assert "over_count" not in code, step.get("name")

    def test_the_artifact_that_scores_the_rule_is_uploaded(self):
        """The promotion criterion is scored from `benchmark-ab`'s
        `ab_report.json` (`docs/development/testing/performance.md`), so the
        artifact has to keep carrying it."""
        (upload,) = [
            step
            for step in self._job()["steps"]
            if step.get("name") == "Upload A/B results"
        ]
        assert upload["with"]["name"] == "benchmark-ab"
        assert "ab_report.json" in upload["with"]["path"]

    def test_no_step_pipes_into_a_quiet_grep(self):
        """‼ `set -o pipefail` plus `| grep -q` can turn a gate off and
        report green.

        `grep -q` exits on its first match and closes the pipe; the
        producer dies with SIGPIPE and pipefail propagates 141, so an
        `if` around it silently takes the ELSE branch. Measured:
        `yes | grep -q y` under pipefail exits 141.

        The base-comparability probe was written that way. Measured on
        this repository with the real producer, `git show <blob> |
        grep -q .`:

            tests/wallclock/record.py      6.0 KB   rc=0    (12/12)
            .../case_repository.py        65.2 KB   rc=0
            .../modules/auth/api/auth.py  74.4 KB   rc=141  (12/12)
            .../investigation/schemas.py  80.8 KB   rc=0    (12/12)
            docs/reference/api/openapi.json  501 KB rc=141

        So it does not fire at today's 6 KB, it does fire well within
        the size an ordinary source file reaches, and it is **not
        predictable from size** — 74 KB fires every time and 81 KB never
        does. Banned outright rather than reasoned about per site for
        exactly that reason: the failure mode is the worst on offer —
        every pull request reports "the base predates the recorder", the
        job is green, and the gate is off with nobody told — and no
        author can tell locally whether their own site is one of the
        ones that fires.
        """
        scanned, offenders = [], []
        for job_name, job in self._workflow()["jobs"].items():
            for step in job["steps"]:
                code = _shell_code(step.get("run") or "")
                # Scoped to the hazard's actual condition. Without
                # `pipefail` a SIGPIPE producer does not decide the
                # pipeline's status, so `| grep -q` there is safe — and
                # `benchmarks / Parse benchmark results` uses exactly
                # that form. Banning it everywhere would have cost one
                # correct site and taught the next reader to widen the
                # allowlist instead of the rule.
                if "pipefail" not in code:
                    continue
                scanned.append((job_name, step.get("name")))
                if PIPED_INTO_QUIET_GREP.search(code):
                    offenders.append((job_name, step.get("name")))
        # A scan that looked nowhere reports a clean workflow exactly
        # like a clean workflow does.
        assert scanned, "no step in this workflow sets pipefail — scan looked nowhere"
        assert not offenders, (
            "these steps pipe into `grep -q`, which exits 141 under pipefail "
            f"once the producer outruns the pipe buffer: {offenders}"
        )

    @pytest.mark.parametrize(
        "script",
        [
            "git show x | grep -q 'y'",
            "cat f \\\n  | grep -q needle",
            "printf x | grep -qE 'a|b'",
            "cat f | grep -i -q needle",
        ],
    )
    def test_the_quiet_grep_scan_finds_what_it_is_for(self, script):
        """‼ The positive control.

        A scan whose vocabulary has drifted reports a clean workflow
        exactly like a clean workflow does — and this one has to survive
        line continuations and clustered flags, which is how the
        original was written.
        """
        assert PIPED_INTO_QUIET_GREP.search(script), script

    @pytest.mark.parametrize(
        "script",
        [
            "grep -q needle file",
            "git show x > f\ngrep -q needle f",
            "cat f | grep -c needle > /dev/null",
            "cat f | grep needle | head -1",
        ],
    )
    def test_the_quiet_grep_scan_leaves_the_safe_forms_alone(self, script):
        """The over-approximation's cost, counted rather than assumed:
        an unpiped `grep -q`, a file read, and a counting grep are all
        safe and must not be flagged."""
        assert not PIPED_INTO_QUIET_GREP.search(script), script

    def test_the_verdict_survives_the_reporting_steps(self):
        """‼ The comparison step must not abort the job before the summary
        and the comment are written — that is how a gate's detail goes
        missing exactly when it matters. So the exit status is carried to
        a later step instead."""
        steps = self._job()["steps"]
        names = [step.get("name") for step in steps]
        compare = next(
            s for s in steps if "python -m tests.wallclock.ab" in (s.get("run") or "")
        )
        verdict = next(s for s in steps if s.get("name") == "Verdict")
        assert "status=" in compare["run"] and "GITHUB_OUTPUT" in compare["run"]
        assert "exit 1" in verdict["run"]
        assert names.index("Verdict") == len(names) - 1, names

    def test_the_head_checkout_has_full_history(self):
        """`git worktree add <base>` needs the base commit locally, and a
        shallow clone does not contain it."""
        (checkout,) = [
            step
            for step in self._job()["steps"]
            if (step.get("uses") or "").startswith("actions/checkout@")
        ]
        assert checkout["with"]["fetch-depth"] == 0

    def test_the_report_tells_a_skip_apart_from_a_broken_job(self):
        """‼ Three states, not two.

        "The base cannot take part" and "the base step never finished"
        produce the same empty `comparable` output, and printing the
        first for the second would explain away a broken job as a clean
        skip — the exact shape of "correct and unread" this campaign
        keeps finding. So the Report step branches on `true`, on `false`,
        and on neither.
        """
        report = next(
            step for step in self._job()["steps"] if step.get("name") == "Report"
        )
        run = report["run"]
        assert "= 'true'" in run and "= 'false'" in run, run
        assert "else" in run
        assert "did not complete" in run
        # And it runs whatever happened above it, or the skip is silent.
        assert report["if"] == "always()"

    def test_a_base_that_cannot_take_part_is_detected_not_assumed(self):
        """A base that cannot produce readable records yields an empty
        file, which is indistinguishable from "nothing regressed". The
        workflow decides by INSPECTING the base tree, before spending
        twenty minutes of runner time on a comparison it cannot make.

        ‼ It tests the record FORMAT, not a function name. A base can
        carry `record_comparison` and still write a shape this head
        cannot parse — the comparator then hard-fails, and every pull
        request is red until `main` catches up. That is the same
        mis-report this probe exists to prevent, pointing the other way.
        """
        (step,) = [step for step in self._job()["steps"] if step.get("id") == "base"]
        assert "RECORD_FORMAT_VERSION" in step["run"]
        assert "SUPPORTED_RECORD_VERSIONS" in step["run"]
        assert "comparable=false" in step["run"]
        # Both skip causes carry their own reason, because printing one
        # of them for the other is how a reader stops trusting the line.
        assert step["run"].count("reason=") == 2
        # And every expensive step is gated on that answer.
        for name in ("Install dependencies (base)", "Run benchmarks at base"):
            gated = next(s for s in self._job()["steps"] if s.get("name") == name)
            assert "steps.base.outputs.comparable == 'true'" in gated["if"]

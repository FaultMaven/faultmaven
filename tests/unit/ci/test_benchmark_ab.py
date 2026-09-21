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
* **There is one rule, and it is the median.** A per-test residual rule
  and a count rule were both measured on #1567's null experiment and
  rejected by their own numbers (`ab.py`'s docstring carries them), so
  the gate fires on the suite median alone. What that costs is asserted
  too: a regression confined to a minority of the suite does NOT fail
  here, and a test says so, because a gap nobody wrote down is a gap
  somebody will later mistake for coverage.
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
import json
import os
from pathlib import Path

import pytest
import yaml

from tests.wallclock import ab, record
from tests.wallclock.assertions import (
    assert_latency_within,
    assert_throughput_at_least,
)
from tests.wallclock.budgets import LatencyBudget, ThroughputBudget

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "benchmarks.yml"
AB_MODULE = REPO_ROOT / "tests" / "wallclock" / "ab.py"
AB_JOB = "ab-regression"


@pytest.fixture
def recorder(tmp_path, monkeypatch):
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
    def test_it_is_inert_when_the_variable_is_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv(record.RECORD_ENV, raising=False)
        path = tmp_path / "nothing.jsonl"
        assert_latency_within(0.1, _latency(), "quiet")
        assert not path.exists()

    def test_a_latency_comparison_is_recorded(self, recorder):
        assert_latency_within(0.25, _latency(1.0), "Case creation latency")
        (row,) = recorder()
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
    """A 50-benchmark side, each at 10ms times its named scale factor."""
    return _rows(
        *[(f"b{i:02d}", ab.LATENCY_METRIC, 0.010 * scale_by_name(i)) for i in range(50)]
    )


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
        green. That is deliberate: #1567 measured the per-test residual
        on identical code at 1.18x-3.38x, so a rule on it would need a
        ~3.5x threshold, and every absolute budget in
        `tests/benchmarks/budgets.py` already fails between 2.46x and
        3.57x. The single-path class belongs to those budgets. What this
        gate owes it is visibility, so the benchmark must still come out
        top of the table.
        """
        base = _suite(lambda i: 1.0)
        head = _suite(lambda i: 1.4 if i == 7 else 1.0)
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert verdict.ok
        assert verdict.median == pytest.approx(1.0)
        assert verdict.worst.row.label == "b07"
        assert verdict.worst_residual == pytest.approx(1.4)
        assert "b07" in ab.render(
            verdict, base_label="b", head_label="h", suite_threshold=1.30
        )

    def test_a_minority_regression_does_not_fail_either(self):
        """The same gap, at the scale it is most likely to be met.

        A quarter of the suite 30% slower leaves the median at 1.0. #1567
        measured a count rule for exactly this case and rejected it: on
        the null the count at 1.30x reached 19 of 50, while slowing HALF
        the suite by 30% produced 15 — the signal sits below the noise.
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

    def test_nothing_in_common_is_a_failure_not_a_pass(self):
        """‼ The one outcome a relative gate must never call green."""
        verdict = ab.compare(
            _rows(("gone", ab.LATENCY_METRIC, 0.01)),
            _rows(("new", ab.LATENCY_METRIC, 0.01)),
            suite_threshold=1.2,
        )
        assert not verdict.ok
        assert "nothing was compared" in verdict.failures[0]

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
        verdict = ab.compare(
            _rows(("x", ab.LATENCY_METRIC, bad)),
            _rows(("x", ab.LATENCY_METRIC, 0.01)),
            suite_threshold=1.2,
        )
        assert [row.label for row, _ in verdict.unusable] == ["x"]
        # Nothing comparable is left, so the verdict is the empty-comparison
        # failure rather than a green.
        assert not verdict.ok

    def test_a_metric_that_changed_kind_is_not_compared(self):
        verdict = ab.compare(
            _rows(("x", ab.LATENCY_METRIC, 0.01)),
            _rows(("x", ab.THROUGHPUT_METRIC, 100.0)),
            suite_threshold=1.2,
        )
        assert "metric changed" in verdict.unusable[0][1]


class TestLoading:
    def _write(self, path, rows):
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))

    def _row(self, **overrides):
        row = {
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
        new."""
        for step in self._job()["steps"]:
            assert "FM_BENCHMARK_ABSOLUTE" not in (step.get("env") or {})

    def test_the_threshold_is_declared_once_and_is_clear_of_the_noise(self):
        """‼ The gate's whole credibility is this number.

        #1567's null experiment — identical code on both sides, 132
        ordered pairs — put the suite median between 0.822x and 1.216x.
        Anything at or below that worst observation is a gate that goes
        red on a clean pull request, and a flaky performance gate gets
        muted, which is how #908 began.
        """
        env = self._job()["env"]
        assert "FM_AB_SUITE" in env, env
        assert float(env["FM_AB_SUITE"]) >= 1.25, (
            "the suite median reached 1.216x on identical code (#1567); a "
            "threshold this close to the measured noise floor will flake"
        )

    def test_the_comparison_step_passes_the_threshold(self):
        (step,) = [
            step
            for step in self._job()["steps"]
            if "tests.wallclock.ab" in (step.get("run") or "")
        ]
        assert "--suite-threshold" in step["run"]
        assert "FM_AB_SUITE" in step["run"]

    def test_the_verdict_survives_the_reporting_steps(self):
        """‼ The comparison step must not abort the job before the summary
        and the comment are written — that is how a gate's detail goes
        missing exactly when it matters. So the exit status is carried to
        a later step instead."""
        steps = self._job()["steps"]
        names = [step.get("name") for step in steps]
        compare = next(s for s in steps if "tests.wallclock.ab" in (s.get("run") or ""))
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

    def test_a_base_without_the_recorder_is_detected_not_assumed(self):
        """A base that predates the recorder produces an empty file, which
        is indistinguishable from "nothing regressed". The workflow decides
        by INSPECTING the base tree, before spending twenty minutes of
        runner time on a comparison it cannot make."""
        (step,) = [step for step in self._job()["steps"] if step.get("id") == "base"]
        assert "record_comparison" in step["run"]
        assert "comparable=false" in step["run"]
        # And every expensive step is gated on that answer.
        for name in ("Install dependencies (base)", "Run benchmarks at base"):
            gated = next(s for s in self._job()["steps"] if s.get("name") == name)
            assert "steps.base.outputs.comparable == 'true'" in gated["if"]

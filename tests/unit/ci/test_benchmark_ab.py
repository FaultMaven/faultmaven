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
import builtins
import json
import math
import os
import re
from pathlib import Path

import pytest
import yaml

from tests.wallclock import ab, calibration, record
from tests.wallclock.assertions import (
    assert_latency_within,
    assert_throughput_at_least,
)
from tests.wallclock.budgets import LatencyBudget, ThroughputBudget

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "benchmarks.yml"
AB_MODULE = REPO_ROOT / "tests" / "wallclock" / "ab.py"
AB_JOB = "ab-regression"

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
            )
        finally:
            monkeypatch.setattr(builtins, "open", real_open)
            record.reset_for_testing()
        assert opened == [str(path)], opened

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

    def test_deleting_one_module_still_gates(self):
        """The floor is not sized to catch an ordinary deletion.

        The five benchmark modules hold 15, 13, 9, 7 and 6 of the 50
        rows, so deleting the largest leaves 0.70 of the base — well
        above the floor, and still gated.
        """
        base = _suite(lambda i: 1.0)
        head = dict(list(base.items())[:35])
        verdict = ab.compare(base, head, suite_threshold=1.30)
        assert verdict.gated and verdict.ok
        assert verdict.coverage == pytest.approx(0.70)

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
            if "python -m tests.wallclock.ab" in (step.get("run") or "")
        ]
        assert "--suite-threshold" in step["run"]
        assert "FM_AB_SUITE" in step["run"]

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

"""The #1910 recorder additions and the collector that reads them."""

import json

import pytest

from tests.wallclock import collect, record


def _row(**overrides):
    row = {
        "v": 2,
        "nodeid": "t.py::test_a",
        "label": "l",
        "occurrence": 0,
        "metric": collect.LATENCY,
        "observed": 0.40,
        "budget": 0.1,
        "kind": "regression budget",
        "scale": 4.0,
        "raw_ratio": 4.0,
        "profile": "standalone",
        "sha": "abc",
    }
    row.update(overrides)
    return row


def test_latency_is_divided_and_throughput_multiplied_by_the_raw_ratio():
    assert collect.normalised(_row()) == pytest.approx(0.10)
    assert collect.normalised(
        _row(metric=collect.THROUGHPUT, observed=100.0, scale=2.0, raw_ratio=2.0)
    ) == pytest.approx(200.0)


def test_nightly_and_per_pr_rows_of_the_same_true_speed_agree():
    # Same code, same 4x-slow machine: the per-PR row applied scale 4.0, the
    # nightly (absolute mode) pinned scale at 1.0 -- raw seconds, 4x larger
    # than the reference-machine figure.
    per_pr = _row(observed=0.40, scale=4.0, raw_ratio=4.0, profile="standalone")
    nightly = _row(observed=0.40, scale=1.0, raw_ratio=4.0, profile="nightly")
    assert collect.normalised(nightly) == pytest.approx(0.10)
    assert collect.normalised(nightly) == pytest.approx(collect.normalised(per_pr))
    # A per-PR row on a runner FASTER than the reference: scale clamped at 1.0.
    fast = _row(observed=0.05, scale=1.0, raw_ratio=0.5)
    assert collect.normalised(fast) == pytest.approx(0.10)


def test_a_v1_row_falls_back_to_the_applied_scale():
    old = _row(scale=2.0)
    del old["raw_ratio"]
    assert collect.normalised(old) == pytest.approx(0.20)
    assert collect.normalised(_row(scale=2.0, raw_ratio=float("nan"))) == (
        pytest.approx(0.20)
    )


@pytest.mark.parametrize("n,rank", [(10, 9), (30, 27), (50, 45)])
def test_p90_is_the_nearest_rank(n, rank):
    assert collect.percentile(list(range(1, n + 1)), 0.90) == rank


def test_distinct_runs_are_counted_per_group():
    rows = [
        _row(run_id="1", run_attempt="1"),
        _row(run_id="1", run_attempt="1", occurrence=1),
        _row(run_id="2", run_attempt="1"),
        _row(run_id="2", run_attempt="2"),
    ]
    (stats,) = collect.summarise(rows).values()
    assert (stats["n"], stats["runs"]) == (4, 3)


def test_an_existing_directory_without_a_trailing_slash_is_a_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(record.RECORD_ENV, str(tmp_path))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
    record.reset_for_testing()
    try:
        record.record_comparison(
            metric=record.LATENCY_METRIC,
            label="x",
            observed=0.1,
            budget=1.0,
            kind="regression budget",
            scale=1.0,
            raw_ratio=1.0,
        )
    finally:
        record.reset_for_testing()
    (row,) = [
        json.loads(x) for x in (tmp_path / "rows-gw1.jsonl").read_text().splitlines()
    ]
    assert (row["run_id"], row["run_attempt"]) == ("77", None)


def test_summary_is_per_profile_and_never_pools_profiles():
    rows = [_row(observed=0.4 * i, profile="standalone") for i in (1, 2, 3, 4, 5)]
    rows.append(_row(observed=9.0, scale=1.0, profile="nightly"))
    summary = collect.summarise(rows)
    standalone = summary[("t.py::test_a", "l", "standalone")]
    assert standalone["n"] == 5
    assert standalone["min"] == pytest.approx(0.1)
    assert standalone["median"] == pytest.approx(0.3)
    assert standalone["max"] == pytest.approx(0.5)
    assert standalone["p90"] == pytest.approx(0.5)
    assert summary[("t.py::test_a", "l", "nightly")]["n"] == 1


def test_a_v1_row_without_profile_lands_under_unknown():
    old = _row()
    for field in ("profile", "raw_ratio", "sha"):
        del old[field]
    summary = collect.summarise([old])
    assert ("t.py::test_a", "l", "unknown") in summary


def test_main_reads_a_directory_tree_and_refuses_empty_input(tmp_path, capsys):
    nested = tmp_path / "run1" / "wallclock-rows-cloud"
    nested.mkdir(parents=True)
    (nested / "rows-gw0.jsonl").write_text(json.dumps(_row()) + "\n")
    assert collect.main([str(tmp_path)]) == 0
    assert "t.py::test_a :: l" in capsys.readouterr().out
    empty = tmp_path / "empty"
    empty.mkdir()
    assert collect.main([str(empty)]) == 1


def test_a_malformed_line_raises(tmp_path):
    (tmp_path / "x.jsonl").write_text("{not json\n")
    with pytest.raises(ValueError):
        list(collect.iter_rows([tmp_path]))


def test_directory_mode_writes_one_file_per_xdist_worker(tmp_path, monkeypatch):
    target = tmp_path / "rows"
    monkeypatch.setenv(record.RECORD_ENV, str(target) + "/")
    monkeypatch.setenv(record.PROFILE_ENV, "cloud")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    kwargs = dict(
        metric=record.LATENCY_METRIC,
        label="x",
        observed=0.1,
        budget=1.0,
        kind="regression budget",
        scale=1.0,
        raw_ratio=0.8,
    )
    record.reset_for_testing()
    try:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
        record.record_comparison(**kwargs)
        monkeypatch.delenv("PYTEST_XDIST_WORKER")
        record.record_comparison(**kwargs)
    finally:
        record.reset_for_testing()
    assert sorted(p.name for p in target.iterdir()) == [
        "rows-gw3.jsonl",
        "rows-main.jsonl",
    ]
    row = json.loads((target / "rows-gw3.jsonl").read_text())
    assert (row["profile"], row["sha"], row["raw_ratio"], row["v"]) == (
        "cloud",
        "deadbeef",
        0.8,
        2,
    )


def test_absolute_mode_row_carries_the_ratio_it_did_not_apply(tmp_path, monkeypatch):
    from tests.wallclock import calibration
    from tests.wallclock.assertions import assert_latency_within
    from tests.wallclock.budgets import LatencyBudget

    state = calibration.calibration_state()
    monkeypatch.setenv(calibration.ABSOLUTE_MODE_ENV, "1")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv(record.RECORD_ENV, str(tmp_path) + "/")
    calibration.reset_calibration_cache()
    monkeypatch.setattr(
        calibration,
        "measure_calibration",
        lambda: calibration.CALIBRATION_REFERENCE_SECONDS * 2.5,
    )
    record.reset_for_testing()
    try:
        assert_latency_within(
            0.001,
            LatencyBudget("probe", regression=0.5, product_target=1.0, reference=0.2),
            "probe",
        )
    finally:
        record.reset_for_testing()
        calibration.reset_calibration_cache()
        calibration.restore_calibration_state(state)
    (row,) = [
        json.loads(x) for x in (tmp_path / "rows-main.jsonl").read_text().splitlines()
    ]
    assert row["scale"] == 1.0
    assert row["raw_ratio"] == pytest.approx(2.5)


# ------------------------------------------------------------ --propose (#1910)


def _propose(values, **overrides):
    kwargs = dict(
        throughput=False,
        old_reference=0.0353,  # 0.010 once divided by the 3.53 box factor
        old_regression=0.08,
        product_target=1.0,
        box_factor=3.53,
    )
    kwargs.update(overrides)
    return collect.propose_row("ROW", values, **kwargs)


@pytest.mark.parametrize(
    "value,direction,expected",
    [
        (0.0123, "nearest", 0.012),
        (0.0126, "nearest", 0.013),
        (0.0121, "up", 0.013),
        (0.0129, "down", 0.012),
        (0.012, "up", 0.012),  # already two figures: not pushed up
        (7.2352e-7, "up", 7.3e-7),
        (19661 / 2.5, "nearest", 7900.0),
    ],
)
def test_two_significant_figures(value, direction, expected):
    assert collect._sig2(value, direction) == pytest.approx(expected)


def test_a_latency_reference_is_the_larger_of_the_box_and_the_ci_p95():
    # 20 jobs at 4 ms: the box term (10 ms) wins and the anchor is 2.5x it.
    box_wins = _propose([0.004] * 20)
    assert (box_wins.source, box_wins.reference) == ("box", pytest.approx(0.010))
    assert box_wins.regression == pytest.approx(0.025)
    # 20 jobs at 20 ms: the CI p95 wins.
    ci_wins = _propose([0.020] * 20)
    assert (ci_wins.source, ci_wins.reference) == ("ci", pytest.approx(0.020))
    assert ci_wins.regression == pytest.approx(0.050)


def test_the_anchor_is_raised_until_it_clears_the_worst_job():
    # p95 of 20 is the 19th value: 10 ms. 2.5x is 25 ms, but one job read
    # 18 ms, and 1.5x that is 27 ms.
    proposal = _propose([0.005] * 18 + [0.010, 0.018], old_reference=0.001)
    assert proposal.reference == pytest.approx(0.010)
    assert proposal.regression == pytest.approx(0.027)
    assert proposal.clearance == pytest.approx(1.5)
    assert proposal.stop == ""


def test_a_row_that_cannot_clear_its_worst_job_inside_the_band_stops():
    proposal = _propose([0.005] * 18 + [0.010, 0.021], old_reference=0.001)
    assert proposal.regression is None
    assert "past the band" in proposal.stop


def test_a_reference_a_hair_short_of_the_band_is_rounded_up(monkeypatch):
    """#1940's ruling on CONTEXT_COPY: the p95 2.4104e-7 and the worst job
    4.8234e-7 need a 7.3e-7 anchor, 3.03x the p95. Rounding the reference up
    1.2% to 2.44e-7 puts that anchor at 2.99x, inside the band."""
    values = [1e-7] * 24 + [2.4104e-7, 4.8234e-7]  # p95 of 26: the 25th
    proposal = _propose(values, old_reference=3.9065e-7)
    assert proposal.stop == ""
    assert proposal.reference == pytest.approx(2.44e-7)
    assert proposal.regression == pytest.approx(7.3e-7)
    assert proposal.rounded_by == pytest.approx(2.44e-7 / 2.4104e-7)
    assert proposal.multiple <= 3.0
    assert proposal.clearance >= 1.5
    # Past MAX_REFERENCE_ROUNDING it is a STOP, not a bigger rounding: the
    # test above needs the reference raised 7%.
    assert collect.MAX_REFERENCE_ROUNDING == 1.05


def test_a_throughput_reference_is_rounded_down_the_same_way():
    # p5 of 26 is the 2nd smallest, 3010/s. The floor must be <= 1500 / 1.5
    # = 1000/s, which would sit 3.01x below it. Rounded down to 3000/s, it sits at 3.00x.
    proposal = _propose(
        [3010.0] + [5000.0] * 24 + [1500.0],
        throughput=True,
        old_reference=10000.0,
        product_target=10.0,
    )
    assert proposal.stop == ""
    assert proposal.reference == pytest.approx(3000.0)
    assert proposal.regression == pytest.approx(1000.0)
    assert proposal.rounded_by < 1.0


def test_a_row_that_would_outgrow_its_product_target_stops():
    proposal = _propose([0.020] * 20, product_target=0.040)
    assert proposal.regression is None
    assert "product target" in proposal.stop


def test_a_throughput_reference_is_the_smaller_and_its_floor_divides():
    # Box term 1000/s x 3.53 = 3530/s; CI p5 = 5000/s: the box wins.
    proposal = _propose(
        [5000.0] * 20, throughput=True, old_reference=1000.0, product_target=10.0
    )
    assert (proposal.source, proposal.reference) == ("box", pytest.approx(3530.0))
    assert proposal.regression == pytest.approx(1400.0)  # 3530 / 2.5, two figures
    assert proposal.multiple == pytest.approx(3530.0 / 1400.0)


def test_a_throughput_floor_is_lowered_until_it_clears_the_worst_job():
    # SANITIZE_DOCUMENT_BATCH's shape: 2.5x would be 7900/s, the worst job
    # 11675/s needs the floor at or below 7784/s.
    proposal = _propose(
        [30000.0] * 25 + [11675.0],
        throughput=True,
        old_reference=19661.0 / 3.53,
        product_target=10.0,
    )
    assert proposal.regression == pytest.approx(7700.0)
    assert proposal.clearance >= 1.5


def test_a_worst_job_row_anchors_on_the_worst_at_the_3x_end():
    proposal = _propose([1e-7] * 25 + [6.7509e-7], anchor_on_worst=True)
    assert (proposal.source, proposal.reference) == ("worst", pytest.approx(6.7509e-7))
    assert proposal.regression == pytest.approx(2.0e-6)
    assert 2.9 <= proposal.multiple <= 3.0


def test_the_job_statistic_is_the_worst_comparison_of_each_job():
    rows = [
        _row(run_id="1", run_attempt="1", observed=0.4, label="a"),
        _row(run_id="1", run_attempt="1", observed=0.8, label="b"),
        _row(run_id="2", run_attempt="1", observed=0.2),
        _row(run_id="2", run_attempt="1", observed=9.9, profile="nightly"),
    ]
    stats = collect.job_statistics(rows, lambda row: "ROW", throughput_rows=())
    values, ratios = stats["ROW"]
    assert sorted(values) == [pytest.approx(0.05), pytest.approx(0.2)]
    assert len(ratios) == 3  # the nightly row is not a per-PR job


def test_every_recorded_comparison_in_the_performance_suite_maps_to_a_row():
    """The mapping is read from the call sites. A test owning two rows must
    label each comparison with a literal, or a re-anchor cannot tell them
    apart; this fails if one stops doing so."""
    import ast

    from tests.performance import budgets

    row_of = collect.row_mapper(budgets.ALL_BUDGETS)
    resolved = set()
    for path in sorted(collect.PERFORMANCE_DIR.rglob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in collect._HELPERS
                ):
                    label = call.args[2]
                    text = label.value if isinstance(label, ast.Constant) else "f"
                    # A judge outside the test (judge_turn_commit) records
                    # under the test that calls it, the row's own test.
                    test = node.name
                    if not test.startswith("test_"):
                        test = budgets.ALL_BUDGETS[call.args[1].id].test
                    record = {"nodeid": f"x.py::{test}[p]", "label": text}
                    name = row_of(record)
                    assert name == call.args[1].id, (node.name, text)
                    resolved.add(name)
    assert resolved == set(budgets.ALL_BUDGETS)


def test_the_worst_job_rows_are_rows_of_the_table():
    from tests.performance import budgets

    assert budgets.ANCHOR_ON_WORST_JOB <= set(budgets.ALL_BUDGETS)


def test_propose_needs_the_box_revision(tmp_path):
    (tmp_path / "x.jsonl").write_text(json.dumps(_row()) + "\n")
    with pytest.raises(SystemExit):
        collect.main(["--propose", str(tmp_path)])

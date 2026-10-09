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


def test_latency_is_divided_and_throughput_multiplied_by_the_applied_scale():
    assert collect.normalised(_row()) == pytest.approx(0.10)
    assert collect.normalised(
        _row(metric=collect.THROUGHPUT, observed=100.0, scale=2.0)
    ) == pytest.approx(200.0)


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

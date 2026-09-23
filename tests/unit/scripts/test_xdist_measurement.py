"""The xdist measurement reads CI's own logs, and each test pins one way that
reading could be wrong (#1594).

The number that matters is the pytest phase, so it comes from the session's
summary line and never from job timestamps. The list that matters is the
failure set, so it comes only from the short test summary section -- a
``FAILED`` progress line under ``-v`` is not a list entry, and a parametrize id
containing `` - `` must not be cut at its own dash. A log with no summary line
is incomplete, never "zero failures". And the diff must separate a test that
fails in every xdist run (deterministic isolation work) from one that fails in
some (the instability #1594 was filed about).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "xdist_measurement.py"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def xm():
    spec = importlib.util.spec_from_file_location("xdist_measurement", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["xdist_measurement"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("xdist_measurement", None)


def _gh_log(*lines: str) -> str:
    """Lines as `gh api .../jobs/<id>/logs` returns them: BOM, then stamps."""
    stamped = [
        f"2026-09-23T21:{i // 60:02d}:{i % 60:02d}.1234567Z {line}"
        for i, line in enumerate(lines)
    ]
    return "\ufeff" + "\n".join(stamped) + "\n"


SERIAL_LOG = _gh_log(
    "##[group]Run actions/checkout@v6",
    "HEAD is now at 0123abcd Merge 1111 into 2222",
    "XDIST_MEASURE nproc=4",
    "============================= test session starts ==============================",
    "tests/unit/test_a.py::test_one PASSED                                    [  1%]",
    "tests/unit/test_a.py::test_two FAILED                                    [  2%]",
    # A nested session's footer inside captured output: not the real one.
    "==================== 1 passed in 0.01s ====================",
    "=================================== FAILURES ===================================",
    # Captured output of a nested session: its list is not this one's.
    "FAILED tests/decoy.py::not_a_list_entry - nested session",
    "=========================== short test summary info ============================",
    "FAILED tests/unit/test_a.py::test_two - AssertionError: assert 1 == 2",
    "= 1 failed, 15661 passed, 12 skipped, 3 warnings in 1640.80s (0:27:20) =",
)

XDIST_LOG = _gh_log(
    "XDIST_MEASURE nproc=4",
    "XDIST_MEASURE commit=0123abcdef0123abcdef0123abcdef0123abcdef",
    "XDIST_MEASURE arm=xdist-a",
    "created: 4/4 workers",
    "4 workers [15674 items]",
    "[gw1] [  2%] FAILED tests/unit/test_a.py::test_two ",
    "=========================== short test summary info ============================",
    "FAILED tests/unit/test_a.py::test_two - AssertionError: assert 1 == 2",
    "FAILED tests/unit/test_b.py::test_param[a - b] - AssertionError: assert 'a - b' == 'c'",
    "ERROR tests/integration/test_c.py::test_boot - RuntimeError: bound to a different event loop",
    "====== 2 failed, 15659 passed, 12 skipped, 1 error in 512.34s (0:08:32) ======",
)

CANCELLED_LOG = _gh_log(
    "XDIST_MEASURE nproc=4",
    "tests/unit/test_a.py::test_one PASSED                                    [  1%]",
    "##[error]The operation was canceled.",
)


def test_serial_log_reads_the_final_summary_and_only_the_summary_list(xm):
    r = xm.parse_log(SERIAL_LOG)
    assert r.complete is True
    assert r.seconds == 1640.80
    assert r.counts == {"failed": 1, "passed": 15661, "skipped": 12, "warnings": 3}
    assert r.failures == ["tests/unit/test_a.py::test_two"]
    assert r.nproc == 4
    assert r.workers is None
    assert r.commit == "0123abcd"


def test_xdist_log_keeps_a_dash_inside_a_parametrize_id(xm):
    r = xm.parse_log(XDIST_LOG)
    assert r.workers == 4
    assert r.arm == "xdist-a"
    assert r.seconds == 512.34
    assert r.failures == [
        "tests/integration/test_c.py::test_boot",
        "tests/unit/test_a.py::test_two",
        "tests/unit/test_b.py::test_param[a - b]",
    ]
    assert r.counts["error"] == 1


def test_a_log_with_no_summary_is_incomplete_not_clean(xm):
    r = xm.parse_log(CANCELLED_LOG)
    assert r.complete is False
    assert r.seconds is None
    assert r.failures == []


def test_run_view_prefix_is_stripped_too(xm):
    line = "Test Cloud\tRun Tests (Cloud)\t2026-09-23T21:00:00.0000000Z === 5 passed in 1.50s ==="
    assert xm.strip_line(line) == "=== 5 passed in 1.50s ==="


@pytest.mark.parametrize(
    ("rest", "nodeid"),
    [
        ("t.py::a - msg", "t.py::a"),
        ("t.py::a[x - y] - msg - more", "t.py::a[x - y]"),
        ("t.py::a", "t.py::a"),
        ("t.py::a[unbalanced - msg", "t.py::a[unbalanced"),
    ],
)
def test_split_nodeid(xm, rest, nodeid):
    assert xm.split_nodeid(rest) == nodeid


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Test Standalone", ("standalone", "serial-required")),
        ("Test Cloud", ("cloud", "serial-required")),
        ("xdist Measurement Cloud (serial)", ("cloud", "serial")),
        ("xdist Measurement Standalone (xdist-b)", ("standalone", "xdist-b")),
        ("Test PostgreSQL Integration", None),
        ("Test Packaging Configuration", None),
    ],
)
def test_classify(xm, name, expected):
    assert xm.classify(name) == expected


def _entry(xm, arm, failures, secs=100.0, complete=True, id_=1):
    return {
        "id": id_,
        "suite": "cloud",
        "arm": arm,
        "label": f"{arm} #{id_}",
        "result": {
            "complete": complete,
            "seconds": secs if complete else None,
            "counts": {"passed": 10, "failed": len(failures)} if complete else {},
            "failures": sorted(failures),
            "workers": 4 if xm.is_xdist(arm) else None,
            "nproc": 4,
            "commit": "abc",
            "arm": arm,
        },
    }


def test_diff_separates_every_run_from_some_runs(xm):
    runs = [
        _entry(xm, "serial-required", {"s"}, id_=1),
        _entry(xm, "serial", {"s"}, id_=2),
        _entry(xm, "xdist-a", {"s", "iso", "flaky"}, id_=3),
        _entry(xm, "xdist-b", {"s", "iso"}, id_=4),
        _entry(xm, "xdist-c", set(), complete=False, id_=5),
    ]
    d = xm.diff_suite("cloud", runs)
    assert d.serial_failures == ["s"]
    assert d.xdist_only_every_run == ["iso"]
    assert d.xdist_only_some_runs == ["flaky"]
    assert d.serial_only == []
    assert d.xdist_runs_agree is False
    assert (d.serial_runs, d.xdist_runs) == (2, 2)
    # The cancelled run's empty list is not evidence that nothing failed.
    assert d.incomplete == ["xdist-c #5"]


def test_identical_xdist_lists_agree(xm):
    runs = [
        _entry(xm, "serial", set(), id_=1),
        _entry(xm, "xdist-a", {"iso"}, id_=2),
        _entry(xm, "xdist-b", {"iso"}, id_=3),
    ]
    d = xm.diff_suite("cloud", runs)
    assert d.xdist_runs_agree is True
    assert d.xdist_only_every_run == ["iso"]


def test_render_states_speedup_against_the_serial_median(xm):
    runs = [
        _entry(xm, "serial-required", set(), secs=1600.0, id_=1),
        _entry(xm, "serial", set(), secs=1400.0, id_=2),
        _entry(xm, "xdist-a", set(), secs=500.0, id_=3),
        _entry(xm, "xdist-b", set(), secs=500.0, id_=4),
    ]
    text = xm.render(runs)
    assert "**3.00x**" in text  # median(1600, 1400) / median(500, 500)
    assert "67% less wall clock" in text
    assert "identical across 2 complete run(s): **yes**" in text


def test_fetch_asks_by_commit_for_every_attempt_and_skips_the_rest(
    xm, monkeypatch, tmp_path
):
    runs = [
        {
            "id": 11,
            "name": "Test Cloud",
            "head_sha": "h",
            "status": "completed",
            "conclusion": "success",
            "started_at": "t",
        },
        {
            "id": 12,
            "name": "xdist Measurement Cloud (xdist-a)",
            "head_sha": "h",
            "status": "completed",
            "conclusion": "success",
            "started_at": "t",
        },
        {
            "id": 13,
            "name": "xdist Measurement Cloud (xdist-b)",
            "head_sha": "h",
            "status": "in_progress",
            "conclusion": None,
            "started_at": "t",
        },
        {
            "id": 14,
            "name": "Code Quality Checks",
            "head_sha": "h",
            "status": "completed",
            "conclusion": "success",
            "started_at": "t",
        },
    ]
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if "--jq" in args:
            return "\n".join(json.dumps(r) for r in runs)
        return XDIST_LOG

    monkeypatch.setattr(xm, "_gh", fake_gh)
    entries = xm.fetch("deadbeef", tmp_path)

    assert (
        "repos/FaultMaven/faultmaven/commits/deadbeef/check-runs?filter=all&per_page=100"
        in calls[0]
    )
    assert [e["id"] for e in entries] == [11, 12]
    assert json.loads((tmp_path / "manifest.json").read_text())[1]["arm"] == "xdist-a"
    loaded = xm.load_manifest(tmp_path / "manifest.json")
    assert loaded[1]["result"]["seconds"] == 512.34


# The first measurement run's actual shape (run on 9ddd169fd): every worker
# crashed in pytest-cov's session start, and pytest exited 3 with no footer.
INTERNALERROR_LOG = _gh_log(
    "XDIST_MEASURE nproc=4",
    "XDIST_MEASURE arm=xdist-a",
    "created: 2/2 workers",
    "INTERNALERROR> E     TypeError: expected str, bytes or os.PathLike object, not Mock",
    "INTERNALERROR> E   assert False",
    "XDIST_MEASURE pytest_exit=3",
)


def test_an_internal_error_is_named_not_read_as_a_slow_or_clean_run(xm):
    r = xm.parse_log(INTERNALERROR_LOG)
    assert r.complete is False
    assert r.internal_error is True
    assert r.exit_code == 3
    assert r.workers == 2
    entry = {
        "id": 7,
        "suite": "standalone",
        "arm": "xdist-a",
        "label": "x #7",
        "result": xm.asdict(r),
    }
    assert "INTERNALERROR, no tests ran" in xm.render([entry])

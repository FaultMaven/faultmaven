"""``scripts/ci_shard_durations.py``: per-file seconds from a slice job's log.

The parser is run on log text shaped like the Actions logs API returns it
(timestamped lines, a BOM at each step's start, the xdist ``-v`` report
lines); nothing is fetched.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ci_shard_durations.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("ci_shard_durations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOG = (
    "﻿2026-11-03T01:28:24.0000000Z ##[group]Run pytest tests/\n"
    "2026-11-03T01:29:59.0000000Z created: 2/2 workers\n"
    "2026-11-03T01:30:00.0000000Z 2 workers [5 items]\n"
    "2026-11-03T01:30:01.5000000Z [gw0] [ 20%] PASSED tests/a.py::test_one \n"
    "2026-11-03T01:30:04.0000000Z [gw1] [ 40%] PASSED tests/b.py::test_x[case with spaces] \n"
    "2026-11-03T01:30:02.0000000Z [gw0] [ 60%] SKIPPED tests/a.py::test_two \n"
    "2026-11-03T01:30:12.0000000Z [gw0] [ 80%] FAILED tests/c.py::T::test_slow \n"
    "2026-11-03T01:30:05.0000000Z [gw1] [100%] XFAIL tests/b.py::test_y \n"
    "2026-11-03T01:30:05.1000000Z tests/b.py::test_not_a_worker_line PASSED\n"
    "﻿2026-11-03T01:31:00.0000000Z ============ 4 passed in 65.00s ============\n"
)


def test_each_gap_on_a_worker_is_charged_to_the_later_lines_file(gen):
    costs = gen.parse_log(LOG)

    assert set(costs) == {"tests/a.py", "tests/b.py", "tests/c.py"}
    assert costs["tests/a.py"] == (
        pytest.approx(2.0),
        2,
    )  # 1.5 + 0.5, from the session start
    assert costs["tests/b.py"] == (pytest.approx(5.0), 2)  # 4.0 + 1.0, on gw1
    assert costs["tests/c.py"] == (pytest.approx(10.0), 1)  # after a.py on gw0


def test_time_before_collection_finishes_is_charged_to_nothing(gen):
    """Collection, which every slice pays for the whole suite, is not any
    one file's cost: the clock starts at `N workers [M items]`."""
    costs = gen.parse_log(LOG)
    assert sum(s for s, _ in costs.values()) == pytest.approx(17.0)


def test_a_second_session_restarts_the_clock(gen):
    second = (
        "2026-11-03T02:00:00.0000000Z 1 worker [1 item]\n"
        "2026-11-03T02:00:03.0000000Z [gw0] [100%] PASSED tests/d.py::test \n"
    )
    assert gen.parse_log(LOG + second)["tests/d.py"] == (pytest.approx(3.0), 1)


def test_rates_pool_every_log_and_the_default_is_the_overall_mean(gen):
    default, rates = gen.combine(
        [{"tests/a.py": (2.0, 2), "tests/b.py": (6.0, 3)}, {"tests/a.py": (4.0, 2)}]
    )
    assert rates == pytest.approx({"tests/a.py": 1.5, "tests/b.py": 2.0})
    assert default == pytest.approx(12.0 / 7)


def test_no_test_lines_at_all_is_an_error_not_an_empty_file(gen):
    with pytest.raises(SystemExit, match="no test lines"):
        gen.combine([{}])


def test_the_rendered_file_is_what_the_plugin_reads(gen, tmp_path):
    from tests.ci_shard import load_rates

    out = tmp_path / "durations.json"
    out.write_text(
        gen.render(0.123456, {"tests/b.py": 2.0, "tests/a.py": 0.0012345}, [2, 1])
    )

    assert load_rates(out) == (0.1235, {"tests/a.py": 0.001234, "tests/b.py": 2.0})
    assert list(json.loads(out.read_text())["files"]) == ["tests/a.py", "tests/b.py"]

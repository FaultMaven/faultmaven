"""The backlog metrics separate the residue from same-lane churn (#1453).

The script exists because the open-issue count could not tell the owner
whether the backlog was converging. Each test here pins one distinction the
count hides: an issue closed inside its lane is not residue, a residue closure
is counted in the week it closed rather than the week it opened, survival is
measured only over issues that had the full exposure, and a fix's latency is
the median age of the lines it removed rather than the oldest import in the
file.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backlog_metrics.py"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def metrics():
    spec = importlib.util.spec_from_file_location("backlog_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # A dataclass under ``from __future__ import annotations`` resolves its
    # field types through ``sys.modules``, so the module must be registered
    # before it executes.
    sys.modules["backlog_metrics"] = module
    spec.loader.exec_module(module)
    return module


_UTC = dt.UTC


def _ts(day: int, hour: int = 12) -> str:
    """An ISO timestamp on the given day of September 2026."""
    return (
        dt.datetime(2026, 9, day, hour, tzinfo=_UTC).isoformat().replace("+00:00", "Z")
    )


def _issue(number, created_day, closed_day=None, labels=(), body=""):
    return {
        "number": number,
        "title": f"issue {number}",
        "state": "CLOSED" if closed_day is not None else "OPEN",
        "createdAt": _ts(created_day),
        "closedAt": _ts(closed_day) if closed_day is not None else None,
        "labels": [{"name": label} for label in labels],
        "body": body,
    }


def test_a_same_lane_closure_is_not_residue(metrics):
    # Opened Tuesday 2026-09-01 (ISO week 36), closed the same day: churn.
    fast = metrics.load_issues([_issue(1, 1, 1)])[0]
    # Opened the same day, closed nine days later (week 37): residue, drained.
    slow = metrics.load_issues([_issue(2, 1, 10)])[0]
    still_open = metrics.load_issues([_issue(3, 1)])[0]

    assert not fast.is_residue()
    assert slow.is_residue()
    assert still_open.is_residue()


def test_weekly_flow_books_residue_drain_in_the_week_it_closed(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    rows = {row["week"]: row for row in metrics.weekly_flow(issues)}

    opened_week = rows["2026-W36"]
    assert opened_week["opened"] == 3
    assert opened_week["closed"] == 1
    # Two of the three joined the residue; the same-day closure did not.
    assert opened_week["residue_in"] == 2
    assert opened_week["residue_out"] == 0

    drained_week = rows["2026-W37"]
    assert drained_week["opened"] == 0
    assert drained_week["closed"] == 1
    assert drained_week["residue_out"] == 1
    assert drained_week["residue_net"] == -1


def test_open_count_is_a_point_in_time_read(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    at_day_5 = dt.datetime(2026, 9, 5, tzinfo=_UTC)
    at_day_20 = dt.datetime(2026, 9, 20, tzinfo=_UTC)

    assert metrics.open_count_at(issues, at_day_5) == 2
    assert metrics.open_count_at(issues, at_day_20) == 1


def test_survival_excludes_issues_without_full_exposure(metrics):
    now = dt.datetime(2026, 9, 30, tzinfo=_UTC)
    issues = metrics.load_issues(
        [
            _issue(1, 1, 1),  # 29 days old: not yet exposed for 30 days
            _issue(2, 1, 10),
        ]
    )
    result = metrics.survival(issues, now, horizons=(1, 7, 30), min_exposure_days=30)
    assert result["cohort"] == 0

    later = dt.datetime(2026, 10, 2, tzinfo=_UTC)
    result = metrics.survival(issues, later, horizons=(1, 7, 30), min_exposure_days=30)
    assert result["cohort"] == 2
    assert result["closed_within"][1] == pytest.approx(0.5)
    assert result["closed_within"][30] == pytest.approx(1.0)
    assert result["survivors"] == 1
    assert result["survivors_closed"] == 1
    assert result["survivors_open"] == 0


def test_residue_snapshot_reads_priority_from_labels(metrics):
    now = dt.datetime(2026, 9, 30, tzinfo=_UTC)
    issues = metrics.load_issues(
        [
            _issue(1, 1, labels=("P2", "tech-debt")),
            _issue(2, 20, labels=("bug",)),
            _issue(3, 1, 2),
        ]
    )
    snap = metrics.residue_snapshot(issues, now)
    assert snap["open"] == 2
    assert snap["older_than_threshold"] == 2
    assert snap["older_than_30d"] == 0
    assert snap["by_priority"] == {"P2": 1, "unranked": 1}


def test_follow_ups_read_the_lane_marker_not_any_reference(metrics):
    issues = metrics.load_issues(
        [
            _issue(10, 1, 1),
            _issue(11, 2, body="Found while working on #10: the writer disagrees."),
            _issue(12, 2, body="Deferred from #10 — which policy wins is a decision."),
            _issue(
                13, 2, body="Compare with the shape #10 describes; unrelated origin."
            ),
        ]
    )
    result = metrics.follow_ups(issues)
    assert result["attributed"] == 2
    assert result["parents"] == 1
    assert result["top"] == [(10, [11, 12])]


def test_removed_lines_skip_tests_and_mark_pure_insertions(metrics):
    diff = (
        "--- a/faultmaven/modules/case/repo.py\n"
        "+++ b/faultmaven/modules/case/repo.py\n"
        "@@ -10,2 +10,3 @@\n"
        "-old one\n"
        "-old two\n"
        "+new\n"
        "@@ -40,0 +41,1 @@\n"
        "+inserted\n"
        "--- a/tests/unit/test_repo.py\n"
        "+++ b/tests/unit/test_repo.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-assert old\n"
        "+assert new\n"
    )
    lines = metrics.removed_lines(diff)
    assert ("faultmaven/modules/case/repo.py", 10) in lines
    assert ("faultmaven/modules/case/repo.py", 11) in lines
    # A pure insertion is recorded as a negative anchor at the insertion point.
    assert ("faultmaven/modules/case/repo.py", -40) in lines
    assert not any(path.startswith("tests/") for path, _ in lines)


def test_latency_is_the_median_age_of_the_removed_lines(metrics):
    found = dt.datetime(2026, 9, 15, tzinfo=_UTC)
    day = 86400
    intro = [
        int((found - dt.timedelta(days=200)).timestamp()),  # an old import
        int((found - dt.timedelta(days=30)).timestamp()),
        int((found - dt.timedelta(days=31)).timestamp()),
    ]
    stats = metrics.latency_days(found, intro)
    assert stats["n_lines"] == 3
    assert stats["median"] == pytest.approx(31, abs=0.01)
    assert stats["oldest"] == pytest.approx(200, abs=0.01)
    assert stats["newest"] == pytest.approx(30, abs=0.01)
    assert day  # the unit the fields are expressed in


def test_latency_distribution_reports_the_two_tails(metrics):
    rows = [{"median": v} for v in (1.0, 3.0, 50.0, 120.0, 400.0)]
    dist = metrics.latency_distribution(rows)
    assert dist["n"] == 5
    assert dist["median"] == 50.0
    assert dist["under_7d"] == pytest.approx(0.4)
    assert dist["over_90d"] == pytest.approx(0.4)
    assert metrics.latency_distribution([]) == {"n": 0}


def test_report_renders_without_latency(metrics):
    now = dt.datetime(2026, 10, 2, tzinfo=_UTC)
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    text = metrics.report(issues, now, None, weeks=4)
    assert "## Weekly flow" in text
    assert "2026-W36" in text
    assert "## Survival" in text
    assert "## Open set" in text
    assert "Fix latency" not in text

    with_latency = metrics.report(issues, now, [{"median": 12.0}], weeks=4)
    assert "1 dated fixes" in with_latency

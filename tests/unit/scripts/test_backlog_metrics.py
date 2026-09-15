"""The backlog metrics separate the residue from same-lane churn (#1453).

The script exists because the open-issue count could not tell the owner
whether the backlog was converging. Each test here pins one distinction the
count hides: an issue closed inside its lane is not residue, an open issue
too young to have left the fast population is pending rather than residue,
a residue closure is counted in the week it closed rather than the week it
opened, survival is measured only over issues that had the full exposure
and its parts partition the survivors, a parent marker naming a PR resolves
to the issue that PR closed, and a fix's latency is the median age of the
pre-issue lines it removed rather than the oldest import in the file.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
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

#: A "now" well after every fixture issue has had 30 days of exposure.
LATER = dt.datetime(2026, 10, 2, tzinfo=_UTC)


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


def test_residue_is_decided_by_age_not_by_being_open(metrics):
    # Opened Tuesday 2026-09-01 (ISO week 36), closed the same day: churn.
    fast, slow, still_open = metrics.load_issues(
        [_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)]
    )
    two_days_in = dt.datetime(2026, 9, 3, tzinfo=_UTC)

    assert not fast.is_residue(two_days_in)
    # Closed nine days later: residue, drained (whatever "now" is).
    assert slow.is_residue(two_days_in)
    # An open issue two days old has not left the fast population yet.
    assert not still_open.is_residue(two_days_in)
    assert still_open.is_pending(two_days_in)
    # The same open issue, a month on, is residue.
    assert still_open.is_residue(LATER)
    assert not still_open.is_pending(LATER)


def test_weekly_flow_reports_young_open_issues_as_pending(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    two_days_in = dt.datetime(2026, 9, 3, tzinfo=_UTC)
    rows = {row["week"]: row for row in metrics.weekly_flow(issues, two_days_in)}

    opened_week = rows["2026-W36"]
    assert opened_week["opened"] == 3
    # Only the nine-day closure is known residue; the open one is pending.
    assert opened_week["residue_in"] == 1
    assert opened_week["pending"] == 1

    rows = {row["week"]: row for row in metrics.weekly_flow(issues, LATER)}
    assert rows["2026-W36"]["residue_in"] == 2
    assert rows["2026-W36"]["pending"] == 0


def test_weekly_flow_books_residue_drain_in_the_week_it_closed(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    rows = {row["week"]: row for row in metrics.weekly_flow(issues, LATER)}

    opened_week = rows["2026-W36"]
    assert opened_week["closed"] == 1
    assert opened_week["residue_out"] == 0
    assert opened_week["open_at_week_end"] == 2

    drained_week = rows["2026-W37"]
    assert drained_week["opened"] == 0
    assert drained_week["closed"] == 1
    assert drained_week["residue_out"] == 1
    assert drained_week["residue_net"] == -1
    assert drained_week["open_at_week_end"] == 1


def test_survival_partitions_the_survivors(metrics):
    now = dt.datetime(2026, 9, 30, tzinfo=_UTC)
    issues = metrics.load_issues(
        [
            _issue(1, 1, 1),  # 29 days old: not yet exposed for 30 days
            _issue(2, 1, 10),
        ]
    )
    result = metrics.survival(issues, now, horizons=(1, 7, 30), min_exposure_days=30)
    assert result["cohort"] == 0
    # The empty answer carries every key the report reads.
    assert result["survivors_open"] == 0
    assert result["survivors_closed_late"] == 0

    much_later = dt.datetime(2026, 12, 1, tzinfo=_UTC)
    issues = metrics.load_issues(
        [
            _issue(1, 1, 1),  # fast
            _issue(2, 1, 10),  # survivor, closed by day 30
            _issue(3, 1),  # survivor, still open
            _issue(4, 1, 20),  # survivor, closed by day 30
        ]
        + [
            {**_issue(5, 1), "closedAt": "2026-10-20T12:00:00Z", "state": "CLOSED"}
        ]  # survivor, closed on day 49: the sweep drain
    )
    result = metrics.survival(issues, much_later, horizons=(1, 7, 30))
    assert result["cohort"] == 5
    assert result["closed_within"][1] == pytest.approx(0.2)
    assert result["survivors"] == 4
    assert result["survivors_closed"] == 2
    assert result["survivors_open"] == 1
    assert result["survivors_closed_late"] == 1


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


def test_follow_ups_resolve_a_pr_parent_to_the_issue_it_closed(metrics):
    issues = metrics.load_issues(
        [
            _issue(10, 1, 1),
            _issue(11, 2, body="Found while working on #10: the writer disagrees."),
            _issue(12, 2, body="Deferred from #10 — which policy wins is a decision."),
            _issue(
                13, 2, body="Compare with the shape #10 describes; unrelated origin."
            ),
            # The PR the lane was working: resolved through the linkage.
            _issue(14, 2, body="Surfaced by the review of PR #999."),
            # A PR that closed no issue here: unresolved.
            _issue(15, 2, body="Deferred from #998."),
            # A cross-repository reference is never a parent here.
            _issue(16, 2, body="Deferred from faultmaven-dashboard#10."),
            _issue(17, 2, body="Follow-up to FaultMaven/faultmaven-copilot#10."),
        ]
    )
    assert metrics.marker_numbers(issues) == [998, 999]

    without = metrics.follow_ups(issues)
    assert without["attributed"] == 2
    assert without["via_pr"] == 0
    assert without["unresolved"] == 2

    result = metrics.follow_ups(issues, {999: [10], 998: []})
    assert result["attributed"] == 3
    assert result["parents"] == 1
    assert result["via_pr"] == 1
    assert result["unresolved"] == 1
    assert result["top"] == [(10, [11, 12, 14])]


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
        "--- a/faultmaven/modules/case/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-deleted module line\n"
        "--- /dev/null\n"
        "+++ b/faultmaven/modules/case/new.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+brand new\n"
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
    # A deleted file's lines are pre-fix lines and are dated.
    assert ("faultmaven/modules/case/gone.py", 1) in lines
    assert not any(path.startswith("tests/") for path, _ in lines)
    assert not any(path.endswith("new.py") for path, _ in lines)


def test_blame_times_reads_porcelain_by_final_line_number(metrics, monkeypatch):
    sha_a = "a" * 40
    sha_b = "b" * 40
    porcelain = (
        f"{sha_a} 1 1 2\n"
        "author X\n"
        "author-time 1700000000\n"
        "\tline one\n"
        f"{sha_a} 2 2\n"
        "\tline two\n"
        f"{sha_b} 7 3 1\n"
        "author Y\n"
        "author-time 1800000000\n"
        "\tline three\n"
    )
    monkeypatch.setattr(metrics, "_git", lambda *args: porcelain)
    assert metrics._blame_times("deadbeef", "faultmaven/x.py") == {
        1: 1700000000,
        2: 1700000000,
        3: 1800000000,
    }
    monkeypatch.setattr(metrics, "_git", lambda *args: None)
    assert metrics._blame_times("deadbeef", "faultmaven/x.py") == {}


def test_latency_drops_lines_authored_after_the_issue(metrics):
    found = dt.datetime(2026, 9, 15, tzinfo=_UTC)
    day = dt.timedelta(days=1)
    intro = [
        int((found - 200 * day).timestamp()),  # an old import
        int((found - 30 * day).timestamp()),
        int((found - 31 * day).timestamp()),
        int((found + 2 * day).timestamp()),  # a peer PR reflowed the file
        int((found + 3 * day).timestamp()),
    ]
    stats = metrics.latency_days(found, intro)
    assert stats["n_lines"] == 3
    assert stats["n_dropped"] == 2
    assert stats["median"] == pytest.approx(31, abs=0.01)
    assert stats["oldest"] == pytest.approx(200, abs=0.01)
    assert stats["newest"] == pytest.approx(30, abs=0.01)
    # Nothing pre-issue: undatable, not a negative latency.
    assert metrics.latency_days(found, intro[3:]) is None


def test_latency_distribution_uses_nearest_rank_percentiles(metrics):
    rows = [{"median": v} for v in (1.0, 3.0, 50.0, 120.0, 400.0)]
    dist = metrics.latency_distribution(rows)
    assert dist["n"] == 5
    assert dist["median"] == 50.0
    assert dist["p25"] == 3.0
    assert dist["p75"] == 120.0
    assert dist["under_7d"] == pytest.approx(0.4)
    assert dist["over_90d"] == pytest.approx(0.4)
    # Two values: p75 is the upper one, not an index past the end.
    assert metrics.latency_distribution(rows[:2])["p75"] == 3.0
    assert metrics.latency_distribution([]) == {"n": 0}


def test_fix_latency_is_one_row_per_pr_and_counts_what_it_skips(metrics, monkeypatch):
    issues = metrics.load_issues(
        [_issue(1, 1, 5), _issue(2, 2, 5), _issue(3, 3, 6), _issue(4, 3, 6)]
    )
    # PR 10 sweeps #1 and #2; PR 20's line postdates #3; PR 30 is not fetched.
    linkage = {
        1: [{"number": 10, "mergeCommit": {"oid": "a" * 40}}],
        2: [{"number": 10, "mergeCommit": {"oid": "a" * 40}}],
        3: [{"number": 20, "mergeCommit": {"oid": "b" * 40}}],
        4: [{"number": 30, "mergeCommit": {"oid": "c" * 40}}],
    }
    monkeypatch.setattr(metrics, "closing_prs", lambda numbers, repo: linkage)
    diff = "--- a/faultmaven/x.py\n+++ b/faultmaven/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"

    def git(*args):
        if args[0] == "cat-file":
            return None if args[-1].startswith("c") else ""
        return diff

    monkeypatch.setattr(metrics, "_git", git)
    old = int(dt.datetime(2026, 8, 1, tzinfo=_UTC).timestamp())
    after = int(dt.datetime(2026, 9, 4, tzinfo=_UTC).timestamp())
    monkeypatch.setattr(
        metrics,
        "_blame_times",
        lambda commit, path: {1: old} if commit.startswith("a") else {1: after},
    )

    result = metrics.fix_latency(issues, "o/r")
    assert [row["pr"] for row in result["rows"]] == [10]
    assert result["rows"][0]["issues"] == [1, 2]
    # Dated against the EARLIEST issue the PR closed.
    assert result["rows"][0]["median"] == pytest.approx(31.5, abs=0.01)
    assert result["rows"][0]["method"] == "removed"
    assert result["skipped"] == {
        "every removed line postdates the issue": 1,
        "merge commit not in local checkout (fetch?)": 1,
    }


def _run_returning(stdout: str, returncode: int = 1):
    class Result:
        pass

    Result.returncode = returncode
    Result.stdout = stdout
    Result.stderr = "gh: something"
    return lambda *a, **k: Result()


def test_graphql_keeps_the_resolvable_aliases_on_a_partial_error(metrics, monkeypatch):
    payload = {
        "data": {
            "repository": {
                "i1": {"closedByPullRequestsReferences": {"nodes": []}},
                "i2": None,
            }
        },
        "errors": [{"message": "Could not resolve to an Issue"}],
    }
    monkeypatch.setattr(metrics.subprocess, "run", _run_returning(json.dumps(payload)))
    assert metrics.closing_prs([1, 2], "o/r") == {1: []}


def test_graphql_reports_a_whole_batch_failure(metrics, monkeypatch, capsys):
    rate_limited = {"data": None, "errors": [{"message": "API rate limit exceeded"}]}
    monkeypatch.setattr(
        metrics.subprocess, "run", _run_returning(json.dumps(rate_limited))
    )
    assert metrics.pr_closing_issues([1, 2], "o/r") == {}
    assert "API rate limit exceeded" in capsys.readouterr().err

    bad_credentials = {"message": "Bad credentials"}
    monkeypatch.setattr(
        metrics.subprocess, "run", _run_returning(json.dumps(bad_credentials))
    )
    assert metrics.closing_prs([1], "o/r") == {}
    assert "Bad credentials" in capsys.readouterr().err


def test_report_renders_on_an_empty_cohort_and_without_latency(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    two_days_in = dt.datetime(2026, 9, 3, tzinfo=_UTC)
    results = metrics.compute(issues, two_days_in, "o/r")
    text = metrics.report(results, weeks=4)
    assert "## Weekly flow" in text
    assert "2026-W36" in text
    assert "No issue has had 30 days of exposure yet." in text
    assert "## Open set" in text
    assert "Fix latency" not in text

    results = metrics.compute(issues, LATER, "o/r")
    results["latency"] = {"rows": [{"median": 12.0}], "skipped": {"x": 2}}
    with_latency = metrics.report(results, weeks=4)
    assert "Cohort with ≥30 days exposure: 3." in with_latency
    assert "1 dated fix PRs, 2 skipped (2 x)" in with_latency


def test_main_replays_a_dump_as_of_the_time_it_was_taken(metrics, tmp_path, capsys):
    dump = tmp_path / "issues.json"
    dump.write_text(json.dumps([_issue(1, 1), _issue(2, 1, 1)]))
    # Two days after the dump: the open issue is pending, not residue.
    assert (
        metrics.main(
            ["--issues", str(dump), "--as-of", _ts(3), "--offline", "--weeks", "2"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "| 2026-W36 | 2 | 1 | +1 | 0 | 0 | +0 | 1 | 1 |" in out

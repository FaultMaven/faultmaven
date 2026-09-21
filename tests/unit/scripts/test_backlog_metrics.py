"""The backlog metrics separate the residue from same-lane churn (#1453).

The script exists because the open-issue count could not tell the owner
whether the backlog was converging. Each test here pins one distinction the
count hides: an issue closed inside its lane is not residue, an open issue
too young to have left the fast population is pending rather than residue,
a quiet week is a row of zeros rather than a missing row, survival is
measured only over issues that had the full exposure and its parts
partition the survivors, a parent marker names an issue here and nothing
else (not a PR, not another repository, not the issue itself), and a fix's
latency is the median age of the pre-issue lines it removed at their old
path.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backlog_metrics.py"

pytestmark = pytest.mark.unit

REPO = "FaultMaven/faultmaven"


@pytest.fixture(scope="module")
def metrics():
    spec = importlib.util.spec_from_file_location("backlog_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # A dataclass under ``from __future__ import annotations`` resolves its
    # field types through ``sys.modules``, so the module must be registered
    # before it executes.
    sys.modules["backlog_metrics"] = module
    before = list(sys.path)
    spec.loader.exec_module(module)
    yield module
    # The script puts the repo root on sys.path at import time. Leaving it
    # there for the rest of the session is how an editable install gets
    # shadowed by a worktree in a later test.
    sys.modules.pop("backlog_metrics", None)
    sys.path[:] = before


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
    # The newest week is incomplete: its count is the count NOW, not at a
    # Sunday that has not happened.
    assert opened_week["open_at_week_end"] == 2

    rows = {row["week"]: row for row in metrics.weekly_flow(issues, LATER)}
    assert rows["2026-W36"]["residue_in"] == 2
    assert rows["2026-W36"]["pending"] == 0


def test_weekly_flow_keeps_quiet_weeks_and_books_drain_where_it_closed(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    rows = metrics.weekly_flow(issues, LATER)
    # W36 (the filings) through W40 (LATER): five calendar weeks, no gaps.
    assert [row["week"] for row in rows] == [
        "2026-W36",
        "2026-W37",
        "2026-W38",
        "2026-W39",
        "2026-W40",
    ]
    by_week = {row["week"]: row for row in rows}

    opened_week = by_week["2026-W36"]
    assert opened_week["closed"] == 1
    assert opened_week["residue_out"] == 0
    assert opened_week["open_at_week_end"] == 2

    drained_week = by_week["2026-W37"]
    assert drained_week["opened"] == 0
    assert drained_week["closed"] == 1
    assert drained_week["residue_out"] == 1
    assert drained_week["residue_net"] == -1
    assert drained_week["open_at_week_end"] == 1

    quiet = by_week["2026-W38"]
    assert (quiet["opened"], quiet["closed"], quiet["residue_net"]) == (0, 0, 0)
    assert quiet["open_at_week_end"] == 1
    assert metrics.weekly_flow([], LATER) == []


def test_snapshot_at_makes_a_replay_a_point_in_time_read(metrics):
    issues = metrics.load_issues([_issue(1, 1, 10), _issue(2, 5), _issue(3, 20)])
    at = dt.datetime(2026, 9, 6, tzinfo=_UTC)
    snapshot = metrics.snapshot_at(issues, at)
    # #3 was not filed yet; #1's closure has not happened yet.
    assert [issue.number for issue in snapshot] == [1, 2]
    assert snapshot[0].is_open


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
            {**_issue(5, 1), "closedAt": "2026-10-20T12:00:00Z"}
        ]  # survivor, closed on day 49: the sweep drain
    )
    result = metrics.survival(issues, much_later, horizons=(1, 7, 30))
    assert result["cohort"] == 5
    assert result["closed_within"][1] == pytest.approx(0.2)
    assert result["survivors"] == 4
    assert result["survivors_closed"] == 2
    assert result["survivors_open"] == 1
    assert result["survivors_closed_late"] == 1


def test_residue_snapshot_uses_the_one_residue_predicate(metrics):
    now = dt.datetime(2026, 9, 30, tzinfo=_UTC)
    issues = metrics.load_issues(
        [
            _issue(1, 1, labels=("P2", "tech-debt")),
            _issue(2, 20, labels=("bug",)),
            _issue(3, 27),  # three days old: open, but not residue
            _issue(4, 1, 2),
        ]
    )
    snap = metrics.residue_snapshot(issues, now)
    assert snap["open"] == 3
    assert snap["older_than_threshold"] == 2
    assert snap["older_than_30d"] == 0
    assert snap["by_priority"] == {"P2": 1, "unranked": 2}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Found while working on #10: the writer disagrees.", 10),
        ("Deferred from #10 — which policy wins is a decision.", 10),
        ("Found while fixing the case-messages writer #10.", 10),
        ("Surfaced by the review of PR #999.", 999),
        ("during the review of #77 this came up", 77),
        ("Found while working on FaultMaven/faultmaven#1440", 1440),
        ("Deferred from faultmaven#10.", 10),
        # Not a lane marker: ordinary prose citing an issue.
        ("This is a review of the design; compare with #10.", None),
        ("Compare with the shape #10 describes; unrelated origin.", None),
        # A marker phrase cannot reach across a sentence boundary. Every
        # real marker gap in the corpus ends in a letter, "(" or a comma.
        ("Found while reviewing the ladder; background is in #1200.", None),
        ("This was found while the suite was red. Compare with #1200.", None),
        ("Split out of the discussion. The spec lives in #1200.", None),
        ("Follow-up to the ruling. Background: #1200 and #1201.", None),
        # Another repository. A qualifier binds only when ATTACHED: every
        # attached token in the corpus is a repository and every spaced one
        # is an ordinary word, so a bare attached name is a repository too.
        ("Deferred from faultmaven-dashboard#10.", None),
        ("Follow-up to FaultMaven/faultmaven-copilot#10.", None),
        ("Deferred from copilot#10.", None),
        ("Surfaced by the review of infra#131.", None),
    ],
)
def test_parent_of_reads_the_lane_marker_and_nothing_else(metrics, body, expected):
    issue = metrics.load_issues([_issue(1, 2, body=body)])[0]
    assert metrics.parent_of(issue, REPO) == expected


def test_parent_of_never_names_the_issue_itself(metrics):
    issue = metrics.load_issues(
        [_issue(1447, 2, body="> Corrected (review of #1447): the count was wrong.")]
    )[0]
    assert metrics.parent_of(issue, REPO) is None


def test_parent_of_reads_every_marker_not_only_the_first(metrics):
    """A first marker naming elsewhere must not suppress a real one after it.

    The answer used to depend on the order the two sentences were written
    in, which is not a property of the issue.
    """
    elsewhere_first = metrics.load_issues(
        [
            _issue(
                1,
                2,
                body="Deferred from faultmaven-dashboard#10.\n"
                "Also: found while working on #55.",
            )
        ]
    )[0]
    assert metrics.parent_of(elsewhere_first, REPO) == 55

    self_first = metrics.load_issues(
        [_issue(7, 2, body="review of #7\nfound while working on #55")]
    )[0]
    assert metrics.parent_of(self_first, REPO) == 55


def test_follow_ups_resolve_a_pr_parent_to_the_issue_it_closed(metrics):
    issues = metrics.load_issues(
        [
            _issue(10, 1, 1),
            _issue(11, 2, body="Found while working on #10: the writer disagrees."),
            _issue(12, 2, body="Deferred from #10 — which policy wins is a decision."),
            # The PR the lane was working: resolved through the linkage.
            _issue(14, 2, body="Surfaced by the review of PR #999."),
            # A PR that closed no issue here: unresolved.
            _issue(15, 2, body="Deferred from #998."),
            # A correction note citing the review of its own fix PR, which
            # closed this very issue: never its own child.
            _issue(16, 2, body="> Corrected (review of #997)."),
        ]
    )
    assert metrics.marker_numbers(issues, REPO) == [997, 998, 999]

    without = metrics.follow_ups(issues, REPO)
    assert without["attributed"] == 2
    assert without["via_pr"] == 0
    assert without["unresolved"] == 3

    result = metrics.follow_ups(issues, REPO, {999: [10], 998: [], 997: [16]})
    assert result["attributed"] == 3
    assert result["parents"] == 1
    # ONE marker resolved through a PR (#14 -> PR 999 -> #10). #16's PR
    # closed #16 itself, which is not a resolution and is not counted as one.
    assert result["via_pr"] == 1
    assert result["unresolved"] == 2
    assert result["top"] == [(10, [11, 12, 14])]


def test_follow_ups_attribute_a_sweep_pr_to_its_lowest_issue(metrics):
    """A PR that closed several issues has no marker saying which one."""
    issues = metrics.load_issues(
        [
            _issue(1100, 1, 2),
            _issue(1180, 1, 2),
            _issue(20, 3, body="Surfaced by the review of PR #500."),
        ]
    )
    result = metrics.follow_ups(issues, REPO, {500: [1180, 1100]})
    assert result["top"] == [(1100, [20])]
    assert result["via_pr"] == 1


def test_removed_lines_date_the_old_path_and_skip_tests_and_new_files(metrics):
    diff = (
        "diff --git a/faultmaven/modules/case/repo.py b/faultmaven/modules/case/repo.py\n"
        "--- a/faultmaven/modules/case/repo.py\n"
        "+++ b/faultmaven/modules/case/repo.py\n"
        "@@ -10,2 +10,3 @@\n"
        "-old one\n"
        "-- a/looks like a header but is a removed content line\n"
        "+new\n"
        "@@ -40,0 +41,1 @@\n"
        "+inserted\n"
        "diff --git a/faultmaven/old/moved.py b/faultmaven/new/moved.py\n"
        "similarity index 90%\n"
        "rename from faultmaven/old/moved.py\n"
        "rename to faultmaven/new/moved.py\n"
        "--- a/faultmaven/old/moved.py\n"
        "+++ b/faultmaven/new/moved.py\n"
        "@@ -5,1 +5,1 @@\n"
        "-the one line the fix changed\n"
        "+fixed\n"
        "diff --git a/faultmaven/modules/case/gone.py b/faultmaven/modules/case/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/faultmaven/modules/case/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-deleted module line\n"
        "diff --git a/faultmaven/modules/case/new.py b/faultmaven/modules/case/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/faultmaven/modules/case/new.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+brand new\n"
        "diff --git a/tests/unit/test_repo.py b/tests/unit/test_repo.py\n"
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
    # A moved file is dated on the line the fix changed, at its OLD path.
    assert ("faultmaven/old/moved.py", 5) in lines
    assert not any(path.startswith("faultmaven/new/") for path, _ in lines)
    # A deleted file's lines are pre-fix lines and are dated.
    assert ("faultmaven/modules/case/gone.py", 1) in lines
    assert not any(path.startswith("tests/") for path, _ in lines)
    assert not any(path.endswith("new.py") for path, _ in lines)
    # The bogus "-- a/" content line did not re-point the later hunk.
    assert not any("looks like" in path for path, _ in lines)


def test_removed_lines_survive_a_path_containing_the_b_prefix(metrics):
    """A greedy split of the ``diff --git`` line mangles such a path."""
    diff = (
        "diff --git a/faultmaven/x b/y.py b/faultmaven/x b/y.py\n"
        "--- a/faultmaven/x b/y.py\n"
        "+++ b/faultmaven/x b/y.py\n"
        "@@ -3,1 +3,1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert metrics.removed_lines(diff) == [("faultmaven/x b/y.py", 3)]


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


def test_fix_latency_dates_every_merged_pr_and_counts_what_it_skips(
    metrics, monkeypatch
):
    issues = metrics.load_issues(
        [
            _issue(1, 1, 5),
            _issue(2, 2, 5),
            _issue(3, 3, 6),
            _issue(4, 3, 6),
            _issue(5, 3, 6),  # closed by hand: no merged PR
        ]
    )
    # PR 10 sweeps #1 and #2 and ALSO carries "Closes #3" beside PR 20 (the
    # real fix, whose line postdates #3); PR 30 is not fetched locally.
    linkage = {
        1: [{"number": 10, "mergeCommit": {"oid": "a" * 40}}],
        2: [{"number": 10, "mergeCommit": {"oid": "a" * 40}}],
        3: [
            {"number": 20, "mergeCommit": {"oid": "b" * 40}},
            {"number": 10, "mergeCommit": {"oid": "a" * 40}},
        ],
        4: [{"number": 30, "mergeCommit": {"oid": "c" * 40}}],
        5: [{"number": 40, "mergeCommit": None}],
    }
    monkeypatch.setattr(metrics, "closing_prs", lambda numbers, repo: linkage)
    diff = (
        "diff --git a/faultmaven/x.py b/faultmaven/x.py\n"
        "--- a/faultmaven/x.py\n+++ b/faultmaven/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    git_calls = []

    def git(*args):
        git_calls.append(args)
        if args[0] == "cat-file":
            return None if args[-1].startswith("c") else ""
        return diff

    monkeypatch.setattr(metrics, "_git", git)
    old = int(dt.datetime(2026, 8, 1, tzinfo=_UTC).timestamp())
    after = int(dt.datetime(2026, 9, 4, tzinfo=_UTC).timestamp())
    blamed = []

    def blame(commit, path):
        blamed.append(commit)
        return {1: old} if commit.startswith("a") else {1: after}

    monkeypatch.setattr(metrics, "_blame_times", blame)

    result = metrics.fix_latency(issues, "o/r")
    assert [row["pr"] for row in result["rows"]] == [10]
    # Every issue the PR closed, including the one it shares with PR 20.
    assert result["rows"][0]["issues"] == [1, 2, 3]
    # Dated against the EARLIEST issue the PR closed.
    assert result["rows"][0]["median"] == pytest.approx(31.5, abs=0.01)
    assert result["rows"][0]["method"] == "removed"
    assert result["skipped"] == {
        "every removed line postdates the issue": 1,
        "merge commit not in local checkout (fetch?)": 1,
    }
    assert result["unlinked_issues"] == 1
    # The invariants the docstrings attach to the git calls: blame at the
    # PARENT of the merge commit, prefixes forced, renames detected, the
    # source pathspec.
    assert all(commit.endswith("^") for commit in blamed)
    diff_calls = [c for c in git_calls if "diff" in c]
    assert diff_calls
    for call in diff_calls:
        assert "--src-prefix=a/" in call and "--dst-prefix=b/" in call
        assert "-M" in call and "--diff-filter=MDR" in call
        assert call[-1] == "faultmaven/"
        assert call[-4].endswith("^") and call[-3] == call[-4][:-1]


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

    monkeypatch.setattr(metrics.subprocess, "run", _run_returning("not json"))
    assert metrics.closing_prs([1], "o/r") == {}
    assert "no body" in capsys.readouterr().err


def test_gh_failures_are_one_line_exits(metrics, monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(metrics.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="not installed"):
        metrics.fetch_issues("o/r")

    monkeypatch.setattr(metrics.subprocess, "run", _run_returning("", returncode=1))
    with pytest.raises(SystemExit, match="gh issue list failed: gh: something"):
        metrics.fetch_issues("o/r")


def test_report_renders_on_an_empty_cohort_and_without_latency(metrics):
    issues = metrics.load_issues([_issue(1, 1, 1), _issue(2, 1, 10), _issue(3, 1)])
    two_days_in = dt.datetime(2026, 9, 3, tzinfo=_UTC)
    results = metrics.compute(issues, two_days_in, "o/r")
    # compute() snapshots: #2's closure is eight days after this "now", so
    # the report must not show it as closed.
    assert sum(row["closed"] for row in results["weekly"]) == 1
    assert results["open"]["open"] == 2
    text = metrics.report(results, weeks=4)
    assert "## Weekly flow" in text
    assert "2026-W36" in text
    assert "No issue has had 30 days of exposure yet." in text
    assert "## Open set" in text
    assert "Fix latency" not in text

    results = metrics.compute(issues, LATER, "o/r")
    results["latency"] = {
        "rows": [{"median": 12.0}],
        "skipped": {"x": 2},
        "unlinked_issues": 1,
    }
    with_latency = metrics.report(results, weeks=4)
    assert "Cohort with ≥30 days exposure: 3." in with_latency
    assert (
        "1 dated fix PRs, 2 skipped (2 x); 1 closed issues link to no" in with_latency
    )

    results["latency"] = {"rows": [], "skipped": {}, "unlinked_issues": 3}
    assert "No fix could be dated (none; 3 closed" in metrics.report(results, 4)


def test_report_omits_the_prolific_list_when_nothing_was_attributed(metrics):
    """The state the first cycle runs in, pasted verbatim into the Queue."""
    issues = metrics.load_issues([_issue(1, 1), _issue(2, 1, 2)])
    text = metrics.report(metrics.compute(issues, LATER, "o/r"), weeks=4)
    assert "0 issues name a parent" in text
    assert "Most prolific" not in text


def test_main_replays_a_dump_as_of_the_time_it_was_taken(metrics, tmp_path, capsys):
    dump = tmp_path / "issues.json"
    dump.write_text(json.dumps([_issue(1, 1), _issue(2, 1, 1), _issue(3, 9)]))
    out_json = tmp_path / "out.json"
    # Two days after the dump: #1 is pending, #2 closed, #3 not filed yet.
    argv = ["--issues", str(dump), "--as-of", _ts(3), "--offline", "--weeks", "2"]
    assert metrics.main(argv + ["--json", str(out_json)]) == 0
    captured = capsys.readouterr()
    assert "| 2026-W36 | 2 | 1 | +1 | 0 | 0 | +0 | 1 | 1 |" in captured.out
    assert "1 issues filed later are dropped" in captured.err
    assert json.loads(out_json.read_text())["as_of"] == "2026-09-03T12:00:00+00:00"

    # Replaying without --as-of is allowed but says so.
    assert metrics.main(["--issues", str(dump), "--offline"]) == 0
    assert "without --as-of" in capsys.readouterr().err


def test_main_refuses_contradictory_flags(metrics, tmp_path):
    dump = tmp_path / "issues.json"
    dump.write_text("[]")
    with pytest.raises(SystemExit):
        metrics.main(["--issues", str(dump), "--weeks", "0"])
    with pytest.raises(SystemExit):
        metrics.main(["--issues", str(dump), "--offline", "--latency"])
    # --offline says "never call gh"; without a dump there is nothing to read.
    with pytest.raises(SystemExit):
        metrics.main(["--offline", "--weeks", "2"])


# --------------------------------------------------------------------------
# The rule-4 tier (#1511)
# --------------------------------------------------------------------------

#: Every spelling of the blocked-on statement this repository writes, and
#: what each must resolve to. Six of these read wrong in the first cut and
#: five of the six turned a real dependency into "waiting on an owner
#: ruling" — which is #1513's six-round miscount recreated inside the
#: measurement built to end it.
_BLOCKED_ON_CASES = [
    ("**Blocked on:** #1294 — the arithmetic moves.", (True, [1294], "")),
    ("**Blocked on**: #1294", (True, [1294], "")),
    ("- **Blocked on:** #1294", (True, [1294], "")),
    ("**Blocked on:** issue #1294 landing", (True, [1294], "")),
    ("**Blocked on:** fm#1294 landing", (True, [1294], "")),
    ("**Blocked on:** FaultMaven/faultmaven#1294", (True, [1294], "")),
    (
        "**Blocked on:** [#1294](https://github.com/FaultMaven/faultmaven/issues/1294)",
        (True, [1294], ""),
    ),
    # A pull request number: reference-shaped and leading, so it is named
    # here; the CORPUS is what decides it is not an issue (see below).
    ("**Blocked on:** PR #1543 merging", (True, [1543], "")),
    ("**Blocked on:**\n#1294 landing", (True, [1294], "")),
    ("> **Blocked on:** #1294", (True, [1294], "")),
    ("**Blocked on:** #1294 and #1300", (True, [1294, 1300], "")),
    # A ruling: stated, no edge, and nothing reported as unreadable.
    (
        "**Blocked on:** an owner ruling on which axis owns it. "
        "Not blocked on another issue.",
        (True, [], ""),
    ),
    ("**Blocked on:** an owner ruling; see #1294 for background", (True, [], "")),
    # The live shape that must never produce an edge, wherever it wraps to.
    ("Not blocked on another issue.", (False, [], "")),
    ("Refs #1294, #1287.", (False, [], "")),
    # Prose ABOUT being blocked is not a statement of it. The label has to
    # open its line, or narration mid-paragraph is filed as owner latency —
    # and "nobody said" and "waiting on the owner" are the two answers this
    # pile exists to keep apart.
    ("The reviewer asked what this is blocked on: nothing yet.", (False, [], "")),
    # Edited in place, so a leftover above the current statement loses.
    ("**Blocked on:** #10\n\n**Blocked on:** an owner ruling", (True, [], "")),
    # A body that quotes the procedure's own examples parses the statement,
    # not the quote.
    (
        "**Blocked on:** #1294\n\n```\n**Blocked on:** an owner ruling\n```",
        (True, [1294], ""),
    ),
]


@pytest.mark.parametrize("body, expected", _BLOCKED_ON_CASES)
def test_blocked_on_reads_every_spelling_this_repository_writes(
    metrics, body, expected
):
    assert metrics.blocked_on(body, REPO) == expected


@pytest.mark.parametrize(
    "body, unreadable",
    [
        # A reference that is not LEADING is a modifier of a ruling, or a
        # spelling this cannot read. Either way it is reported rather than
        # filed as owner latency: a guard that misses can be fixed, a guard
        # that answers the wrong bucket lies.
        ("**Blocked on:** an owner ruling on #1294's shape", "#1294"),
        ("**Blocked on:** faultmaven-dashboard#12 shipping", "faultmaven-dashboard#12"),
    ],
)
def test_an_unreadable_statement_is_reported_not_called_a_ruling(
    metrics, body, unreadable
):
    stated, named, text = metrics.blocked_on(body, REPO)

    assert (stated, named) == (True, [])
    assert unreadable in text


def test_is_own_repo_accepts_this_repository_own_shorthand(metrics):
    # Two live parent markers read "Found while settling fm#918"; without the
    # alias both resolve as another repository and the follow-up is dropped.
    assert metrics.is_own_repo("fm", REPO) is True
    assert metrics.is_own_repo("", REPO) is True
    assert metrics.is_own_repo(None, REPO) is True
    assert metrics.is_own_repo("FaultMaven/faultmaven", REPO) is True
    assert metrics.is_own_repo("faultmaven-dashboard", REPO) is False


def _ready(number, day, body=""):
    return _issue(number, day, labels=("pile:ready",), body=body)


def _blocked(number, day, body=""):
    return _issue(number, day, labels=("pile:blocked",), body=body)


def test_rule_1_is_read_from_the_blocked_pile_not_from_follow_up_markers(metrics):
    """A follow-up marker is provenance, not dependency.

    Measured on this repository's own corpus: of the six children the three
    highest-in-degree ready items have, five had ALREADY CLOSED while the
    supposed blocker was still open — so an in-edge count would put items
    that unblock nobody at rule 1, the top rank. The tier is an upper bound,
    and an item wrongly taken out of it is the one error it cannot afford.
    """
    issues = metrics.load_issues(
        [
            _ready(100, 1),
            # Two follow-ups naming #100 — and both closed while it is open.
            _issue(101, 2, 3, body="Found while working on #100."),
            _issue(102, 2, 3, body="Found while working on #100."),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["excluded"] == []
    assert tier["tier"] == [100]

    # The same item, with two blocked issues that SAY they wait on it.
    issues = metrics.load_issues(
        [
            _ready(100, 1),
            _blocked(101, 2, body="**Blocked on:** #100"),
            _blocked(102, 2, body="**Blocked on:** #100"),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["excluded"] == [100]
    assert tier["tier"] == []


def test_a_blocker_inherits_its_dependents_claim(metrics):
    """Leak B: rule 1 needs two, so a blocker of one held no rank at all."""
    issues = metrics.load_issues(
        [
            _ready(200, 23),  # blocks #201 only, so holds no rule of its own
            _blocked(201, 24, body="**Blocked on:** #200"),
            # #201 in turn blocks two, so IT holds rule 1 — and lends it.
            _blocked(202, 25, body="**Blocked on:** #201"),
            _blocked(203, 25, body="**Blocked on:** #201"),
            _ready(204, 23, body="Unrelated."),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["excluded"] == [200]
    assert tier["lent_claim"] == [(200, 201)]
    assert tier["tier"] == [204]


def test_a_blocker_inherits_its_dependents_age(metrics):
    """The other half of the lend: the pair is as old as its older end.

    #1513 waited six rounds on #1294 with no rule broken at any step,
    because the reserved slot is oldest-first and the blocker's own filing
    date is all the tier had to order it by.
    """
    issues = metrics.load_issues(
        [
            _ready(300, 20),  # young, and the only exit #301 has
            _blocked(301, 3, body="**Blocked on:** #300"),
            _ready(302, 10),  # older than #300, younger than #301
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["tier"] == [300, 302]
    assert tier["lent_age"] == [(300, 301)]
    assert tier["lent_claim"] == []  # age only: nothing to attribute to rule 1

    # Without the lend the order is the blockers' own dates.
    plain = metrics.rule4_tier(
        metrics.load_issues([_ready(300, 20), _ready(302, 10)]), LATER, REPO
    )
    assert plain["tier"] == [302, 300]


def test_the_lend_does_not_restate_rule_1s_threshold(metrics, monkeypatch):
    """The lend is unconditional, as the procedure states it.

    Gating it on "exactly one dependent" writes rule 1's threshold a second
    time, and the two then disagree the moment the constant moves: at a
    threshold of 3 a blocker of two holds neither rule 1 nor the lend, and
    leak B is back on a one-line config change.
    """
    monkeypatch.setattr(metrics, "RULE_1_DEPENDENTS", 3)
    issues = metrics.load_issues(
        [
            _ready(400, 20),
            _blocked(401, 3, body="**Blocked on:** #400"),
            _blocked(402, 4, body="**Blocked on:** #400"),
            _ready(403, 10),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["excluded"] == []  # two dependents is below the threshold now
    assert tier["lent_age"] == [(400, 401)]
    assert tier["tier"] == [400, 403]  # still ranked at its dependent's age


def test_a_mid_move_item_is_not_in_the_ready_pile(metrics):
    """More than one `pile:` label reads as blocked, in both procedure files.

    Read as ready, the same item is the reserved slot's first buy AND is
    reported as waiting on the owner — and the round would name it and then
    be unable to dispatch it, so the slot drains zero.
    """
    issues = metrics.load_issues(
        [
            _issue(
                500,
                1,
                labels=("pile:ready", "pile:blocked"),
                body="**Blocked on:** an owner ruling",
            ),
            _ready(501, 5),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["ready"] == 1
    assert tier["tier"] == [501]
    assert tier["multi_labelled"] == [500]
    assert tier["blocking"]["on_ruling"] == [500]
    assert "#500" in metrics._rule4_text(tier)


def test_a_reference_that_is_not_an_issue_here_is_reported_not_counted(metrics):
    """GitHub numbers issues and pull requests in one sequence.

    Counted as an edge it sits in the reported total, in no bucket anyone
    reads, can never reach `condition_met`, and no ready item can inherit
    from it — a blocked state with no detector.
    """
    issues = metrics.load_issues(
        [_blocked(600, 2, body="**Blocked on:** PR #1543 merging")]
    )
    graph = metrics.blocking_graph(issues, REPO)

    assert graph.waiting_on == {}
    assert graph.on_ruling == []
    assert graph.unresolved == [(600, "#1543")]
    assert "#1543" in metrics._rule4_text(metrics.rule4_tier(issues, LATER, REPO))


def test_a_closed_blocked_item_waits_on_nothing(metrics):
    """`pile:blocked` survives closing, and a closed item blocks nobody.

    Live: `gh issue list --state closed --label pile:blocked` returns #1477.
    Counted, two closed dependents give their blocker rule 1 and take it out
    of the tier, so the reserved slot never reaches an item whose blockers
    are already resolved — the direction this measurement cannot afford.
    """
    issues = metrics.load_issues(
        [
            _ready(100, 1),
            _issue(101, 2, 4, labels=("pile:blocked",), body="**Blocked on:** #100"),
            _issue(102, 2, 4, labels=("pile:blocked",), body="**Blocked on:** #100"),
        ]
    )
    graph = metrics.blocking_graph(issues, REPO)
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert graph.waiting_on == {}
    assert (graph.on_ruling, graph.unstated) == ([], [])
    assert tier["excluded"] == []
    assert tier["tier"] == [100]


def test_a_blocked_item_whose_issue_has_closed_is_reported(metrics):
    issues = metrics.load_issues(
        [_issue(700, 1, 4), _blocked(701, 2, body="**Blocked on:** #700")]
    )
    graph = metrics.blocking_graph(issues, REPO)

    assert graph.waiting_on == {700: [701]}
    assert graph.condition_met == [(701, 700)]


# Every path below is SHAPED like this repository's but names nothing in it,
# and that is load-bearing rather than tidiness. The docs-only CI classifier
# harvests string literals from `tests/` and treats each as a pin — "a test
# reads this document, so a change to it must run the suite" — walking
# `ast.Constant`, which includes docstrings. Naming the real procedure
# document here killed the docs-only fast path for the one file this campaign
# edits every round. These tests do not READ any document; the path is sample
# text for `cited_paths`, so a real one is a pin nobody meant to write. The
# top-level directory must still be one `_CITED_PATH` admits, or the sample
# stops exercising the pattern.


def test_a_cited_path_does_not_take_an_item_out_of_the_tier(metrics):
    """Rule 3 is reported, never applied — a citation is not a production.

    #1462 (chromadb credentials) and #1463 (filter-shaped routes) were
    unranked on the procedure document, which they cite only because this
    campaign's issues quote its gates.
    """
    gates = "docs/development/sample-procedure.md"
    issues = metrics.load_issues(
        [
            _issue(800, 20, body=f"Per the *Building* gates in `{gates}`."),
            _issue(801, 21, body=f"The *Building* gate in `{gates}` is explicit."),
            _ready(802, 22, body=f"Unrelated defect. Per `{gates}`, it must declare."),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert list(tier["seams"]) == [gates]  # still reported …
    assert tier["tier"] == [802]  # … and still ranked
    assert "APPLY BY READING" in metrics._rule4_text(tier)


def test_a_seam_is_a_file_outside_a_code_block(metrics):
    """#1351 was unranked on a bare migrations directory, pasted inside an
    `op.drop_table` sample among fifteen other paths."""
    fenced = "```\nop.drop_table('x')  # alembic/sample-versions/041_drop.py\n```"
    assert metrics.cited_paths(fenced) == set()
    assert metrics.cited_paths("see alembic/sample-versions for the base") == set()
    assert metrics.cited_paths("`scripts/sample_metrics.py:894` is the site") == {
        "scripts/sample_metrics.py"
    }


def test_a_seam_needs_three_issues_inside_the_window(metrics):
    path = "faultmaven/api/middleware/sample_limiter.py"
    # LATER is 2026-10-02, so the 30-day window opens on 2026-09-02.
    outside = [_issue(n, 1, body=f"`{path}`") for n in (1, 2)]
    inside = [_issue(3, 10, body=f"`{path}`")]
    ready = _ready(900, 11, body=f"Also `{path}`.")

    two_inside = metrics.rule4_tier(
        metrics.load_issues(outside + inside + [ready]), LATER, REPO
    )
    assert two_inside["seams"] == {}

    inside.append(_issue(4, 12, body=f"`{path}`"))
    three_inside = metrics.rule4_tier(
        metrics.load_issues(outside + inside + [ready]), LATER, REPO
    )
    assert list(three_inside["seams"]) == [path]


def test_an_unlabelled_corpus_and_an_empty_pile_are_not_a_drained_tier(metrics):
    """A zero here would read as a drained tier; it means "not measured"."""
    unlabelled = metrics.rule4_tier(metrics.load_issues([_issue(1, 1)]), LATER, REPO)
    assert unlabelled["labelled"] is False
    text = metrics._rule4_text(unlabelled)
    assert "not computable here" in text
    assert "UPPER BOUND" not in text

    # Labelled, but no ready issue at all: also not a drained tier.
    no_ready = metrics.rule4_tier(
        metrics.load_issues([_blocked(1, 1, body="**Blocked on:** a ruling")]),
        LATER,
        REPO,
    )
    assert (no_ready["labelled"], no_ready["ready"]) == (True, 0)
    text = metrics._rule4_text(no_ready)
    assert "no tier to measure" in text
    assert "The tier is empty" not in text

    # Ready issues, all of which hold rule 1: THAT is a drained tier.
    drained = metrics.rule4_tier(
        metrics.load_issues(
            [
                _ready(1, 1),
                _blocked(2, 2, body="**Blocked on:** #1"),
                _blocked(3, 2, body="**Blocked on:** #1"),
            ]
        ),
        LATER,
        REPO,
    )
    assert "The tier is empty" in metrics._rule4_text(drained)


def test_the_reported_arithmetic_closes(metrics):
    """The round is told to paste this verbatim, so `ready - tier` has to be
    the excluded count and nothing else may be attributed to it."""
    issues = metrics.load_issues(
        [
            _ready(1, 1),
            _ready(2, 5),
            _blocked(3, 6, body="**Blocked on:** #1"),
            _blocked(4, 6, body="**Blocked on:** #1"),
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)
    text = metrics._rule4_text(tier)

    assert tier["ready"] - len(tier["tier"]) == len(tier["excluded"]) == 1
    assert "**1 of 2 ready items — an UPPER BOUND.**" in text
    assert "it excludes 1." in text
    # Age-only inheritance is not an exclusion and must not be claimed as one.
    age_only = metrics.rule4_tier(
        metrics.load_issues([_ready(1, 20), _blocked(2, 3, body="**Blocked on:** #1")]),
        LATER,
        REPO,
    )
    assert age_only["lent_age"] and not age_only["lent_claim"]
    assert "it excludes 0." in metrics._rule4_text(age_only)
    assert "inheriting the claim" not in metrics._rule4_text(age_only)


def test_the_report_names_the_slots_candidates(metrics):
    issues = metrics.load_issues(
        [_ready(1, 1), _ready(2, 5), _blocked(3, 6, body="**Blocked on:** a ruling")]
    )
    text = metrics.report(metrics.compute(issues, LATER, REPO), weeks=4)

    assert "## Rule-4 tier" in text
    assert "#1 (30d), #2 (26d)" in text
    assert "1 item(s) waiting on a ruling, 0 stating nothing" in text


def test_the_blocking_graph_survives_a_json_round_trip(metrics, tmp_path):
    """`--json` is the machine-readable copy of what the report prints, and
    `json.dumps` coerces an int key to a string with nothing coercing it
    back, so int keys made the two disagree silently."""
    issues = metrics.load_issues(
        [_ready(1, 1), _blocked(2, 2, body="**Blocked on:** #1")]
    )
    results = metrics.compute(issues, LATER, REPO)
    in_process = results["rule4"]["blocking"]["waiting_on"]

    assert in_process == {"1": [2]}
    assert (
        json.loads(json.dumps(results, default=str))["rule4"]["blocking"]["waiting_on"]
        == in_process
    )


# --------------------------------------------------------------------------
# The reference grammar and the rule-4 report (#1576, #1577, #1580, #1581)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape, body",
    [
        ("closed LF fence", "```\ndocs/sample-note.md\n```"),
        # The commonest shape for a pasted log or traceback, and GitHub
        # renders it as code to the end of the body.
        ("unclosed fence", "```\ndocs/sample-note.md\n"),
        # A fence around content that itself contains a fence.
        ("four backticks", "````\n```\ndocs/sample-note.md\n```\n````"),
        ("tilde fence", "~~~\ndocs/sample-note.md\n~~~"),
        ("indented block", "A paragraph.\n\n    docs/sample-note.md\n"),
        # GitHub returns CRLF for any body authored or edited in the web UI.
        ("CRLF", "```\r\ndocs/sample-note.md\r\n```\r\n"),
        ("CRLF unclosed", "```\r\ndocs/sample-note.md\r\n"),
    ],
)
def test_a_code_block_hides_a_path_in_every_shape_github_renders(metrics, shape, body):
    """The guard's purpose is to keep an illustration from reading as a
    statement, and for four of these it did not (#1580)."""
    assert metrics.cited_paths(body) == set(), shape


def test_an_indented_run_under_a_list_is_content_not_a_code_block(metrics):
    """The one direction this guard must not fail in.

    Blanking a wrapped bullet's continuation would delete a real
    `**Blocked on:**` statement, which is worse than leaving an
    illustration in: the item would be reported as stating nothing.
    """
    listed = "- a bullet\n\n    **Blocked on:** #99 landing\n"
    assert metrics.blocked_on(listed, REPO) == (True, [99], "")
    assert metrics.cited_paths("1. step\n\n    docs/sample-note.md\n") == {
        "docs/sample-note.md"
    }


def test_crlf_does_not_erase_a_blocked_on_edge(metrics):
    """The same root as the code-block guard, in the direction that lies.

    The closing anchor could not cross the `\\r` and the paragraph split
    never fired, so the quoted example inside the fence won "last
    statement" and the real edge disappeared — the #1513 miscount by a
    different door.
    """
    body = "**Blocked on:** #99\n\n```\n**Blocked on:** an owner ruling\n```\n"

    assert metrics.blocked_on(body, REPO) == (True, [99], "")
    assert metrics.blocked_on(body.replace("\n", "\r\n"), REPO) == (True, [99], "")


@pytest.mark.parametrize(
    "body",
    [
        # The paragraph break is what bounds the statement, and in CRLF it
        # is `\r\n\r\n` — so without normalising, the payload runs on into
        # the paragraph below and a reference there is read as part of the
        # statement. The first of these changes the BUCKET (a ruling becomes
        # "stated but unreadable"), which is the failure the buckets exist
        # to prevent; the second keeps the edge and invents a second finding.
        "**Blocked on:** an owner ruling\n\n#1234 tracks the follow-up\n",
        "**Blocked on:** #99\n\n#1234 tracks the follow-up\n",
        "**Blocked on:** #99\n\n```\n**Blocked on:** an owner ruling\n```\n",
        "**Blocked on:** #99 landing\n\n    docs/sample-note.md\n",
    ],
)
def test_a_body_reads_the_same_in_crlf_as_in_lf(metrics, body):
    """The corpus is 100% LF only because every issue so far was filed by an
    agent through `gh`; GitHub returns CRLF for any body edited in the web
    UI, so the first human edit to a blocked issue arms this."""
    assert metrics.blocked_on(body.replace("\n", "\r\n"), REPO) == metrics.blocked_on(
        body, REPO
    )
    assert metrics.cited_paths(body.replace("\n", "\r\n")) == metrics.cited_paths(body)


def test_the_statement_ends_at_its_paragraph(metrics):
    """A blank line bounds the statement, so a reference in the paragraph
    below is not part of it.

    Blanking a code block has to preserve the line count for that to hold:
    collapse the blanked lines away and the paragraph break goes with them,
    the payload runs on, and a ruling is reported as *stated but
    unreadable* — the wrong bucket, which is the failure the buckets exist
    to keep apart.
    """
    assert metrics.blocked_on(
        "**Blocked on:** an owner ruling\n\n#1234 tracks the follow-up\n", REPO
    ) == (True, [], "")
    assert metrics.blocked_on(
        "**Blocked on:** #99\n\n#1234 tracks the follow-up\n", REPO
    ) == (True, [99], "")
    # The property that makes it hold, asserted where it lives.
    fenced = "one\n\n```\ntwo\nthree\n```\n\nfour\n"
    assert metrics.without_code_blocks(fenced).count("\n") == fenced.count("\n")
    assert metrics.without_code_blocks("a\n\n    indented\n\nb").count("\n") == 4


def test_a_long_unreadable_statement_says_it_was_cut(metrics):
    body = "**Blocked on:** the owner's call, " + "which is long " * 20 + "see #1294"
    stated, named, text = metrics.blocked_on(body, REPO)

    assert (stated, named) == (True, [])
    assert len(text) == 121 and text.endswith("…")


def test_a_marker_inside_a_code_block_is_an_illustration(metrics):
    """The two reference grammars in the file now read the same text.

    `parent_of` refuses a self-citation but read the raw body, so an issue
    quoting another's body inside a fence was attributed as its follow-up
    and inflated the parent count the campaign is judged by.
    """
    quoted, real = metrics.load_issues(
        [
            _issue(500, 1, body="```\nFound while working on #400\n```\n"),
            _issue(501, 1, body="Found while working on #400."),
        ]
    )

    assert metrics.parent_of(quoted, REPO) is None
    assert metrics.parent_of(real, REPO) == 400


@pytest.mark.parametrize(
    "payload",
    # Emphasis and backticks are stripped before the comparison: the label
    # is written `**Blocked on:**`, so whatever follows it is being written
    # in a line that is already marked up.
    ["", " ", " TBD", " tbd.", " n/a", " ?", " —", " `TBD`", " **TBD**", " _tbd_"],
)
def test_a_label_with_no_question_after_it_states_nothing(metrics, payload):
    """Not owner latency: nobody was asked anything.

    All of these used to fall through to `on_ruling` and be listed under
    *Needs your call* with no question to answer, while `unstated` — the
    bucket step 2's blocked-pile check reads to ask for one — stayed empty,
    so the gap was never repaired.
    """
    assert metrics.blocked_on(f"**Blocked on:**{payload}", REPO) == (False, [], "")
    issues = metrics.load_issues([_blocked(1, 1, body=f"**Blocked on:**{payload}")])
    graph = metrics.blocking_graph(issues, REPO)

    assert (graph.unstated, graph.on_ruling, graph.unresolved) == ([1], [], [])


def test_a_statement_naming_both_an_edge_and_an_unreadable_reference(metrics):
    """`elif here:` computed the rest and threw it away.

    #9999 was neither an edge nor reported — the blocked-state-with-no-
    detector the unreadable bucket was added for.
    """
    issues = metrics.load_issues(
        [_ready(100, 1), _blocked(101, 2, body="**Blocked on:** #100 and #9999")]
    )
    graph = metrics.blocking_graph(issues, REPO)

    assert graph.waiting_on == {100: [101]}
    assert graph.unresolved == [(101, "#9999")]
    assert graph.on_ruling == []


def test_a_reference_past_the_leading_run_is_reported(metrics):
    """ "#1294 (see also #1300)" resolved #1294 and dropped #1300 entirely."""
    assert metrics.blocked_on("**Blocked on:** #1294 (see also #1300)", REPO) == (
        True,
        [1294],
        "#1300",
    )


def test_an_item_does_not_wait_on_itself(metrics):
    """`parent_of` refuses a self-citation; this grammar did not.

    A self-edge inflates the printed edge count, and a closed self would
    print "#N waits on #N, which has closed".
    """
    issues = metrics.load_issues([_blocked(1513, 1, body="**Blocked on:** #1513")])
    graph = metrics.blocking_graph(issues, REPO)

    assert graph.waiting_on == {}
    assert graph.on_ruling == []
    assert graph.unresolved == [(1513, "#1513 (itself)")]


def test_the_lend_records_one_source_per_item_and_only_in_the_tier(metrics):
    """The append sat inside the improvement test, so it fired per
    improvement — and for items rule 1 had excluded, claiming a rank in a
    tier they have no place in."""
    three_dependents = metrics.load_issues(
        [
            _ready(200, 20),
            _blocked(201, 15, body="**Blocked on:** #200"),
            _blocked(202, 10, body="**Blocked on:** #200"),
            _blocked(203, 5, body="**Blocked on:** #200"),
        ]
    )
    excluded = metrics.rule4_tier(three_dependents, LATER, REPO)

    assert excluded["excluded"] == [200]  # rule 1: three dependents
    assert excluded["lent_age"] == []  # so no rank to claim
    assert "Ranked earlier" not in metrics._rule4_text(excluded)

    one_dependent = metrics.load_issues(
        [_ready(300, 20), _blocked(301, 5, body="**Blocked on:** #300")]
    )
    in_tier = metrics.rule4_tier(one_dependent, LATER, REPO)

    assert in_tier["tier"] == [300]
    assert in_tier["lent_age"] == [(300, 301)]
    assert "#300 (from #301)" in metrics._rule4_text(in_tier)


def test_a_fourth_pile_label_is_a_half_finished_move_too(metrics):
    """`pile_of` read the set label by label and `multi_labelled` read its
    size, so one item was named as the reserved slot's first buy AND as
    undispatchable — and step 4's predicate then drained the slot of
    nothing, which is the failure `pile_of` exists to prevent."""
    issues = metrics.load_issues(
        [_issue(900, 1, labels=("pile:ready", "pile:needs-triage")), _ready(901, 5)]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)
    text = metrics._rule4_text(tier)

    assert tier["tier"] == [901]
    assert tier["multi_labelled"] == [900]
    assert "#900" not in text.split("Oldest in the tier")[1].split("\n")[0]
    # "blocked, or yours if blocked is not among them" — the rule's own words.
    mixed, both, single = metrics.load_issues(
        [
            _issue(902, 1, labels=("pile:ready", "pile:yours")),
            _issue(903, 1, labels=("pile:ready", "pile:blocked")),
            _issue(904, 1, labels=("pile:ready",)),
        ]
    )
    assert metrics.pile_of(mixed) == metrics.YOURS_LABEL
    assert metrics.pile_of(both) == metrics.BLOCKED_LABEL
    assert metrics.pile_of(single) == metrics.READY_LABEL


def test_every_multi_labelled_item_is_out_of_the_tier(metrics):
    """The invariant behind the sentence above: the two readings of a pile
    label set cannot disagree about one item, whatever the extra label is."""
    extras = ("pile:blocked", "pile:yours", "pile:needs-triage", "pile:zzz")
    issues = metrics.load_issues(
        [
            _issue(n, 1, labels=("pile:ready", extra))
            for n, extra in enumerate(extras, start=1)
        ]
    )
    tier = metrics.rule4_tier(issues, LATER, REPO)

    assert tier["multi_labelled"] == [1, 2, 3, 4]
    assert tier["tier"] == []
    assert tier["ready"] == 0


def test_an_unreadable_statement_never_emits_broken_markdown(metrics):
    """The round pastes this section into the `Queue` verbatim, so a
    backtick in the quoted statement would break the board's markdown."""
    body = "**Blocked on:** the owner's call on whether `kb_qa` should see #1294"
    issues = metrics.load_issues([_blocked(777, 1, body=body)])
    text = metrics._rule4_text(metrics.rule4_tier(issues, LATER, REPO))
    span = text.split("#777 (")[1].split(").")[0]

    assert span.startswith("``") and span.endswith("``")
    assert "`kb_qa`" in span
    # Every inline span in the section closes.
    for line in text.split("\n"):
        assert len(re.findall(r"`+", line)) % 2 == 0, line


def test_inline_code_fences_any_content(metrics):
    for text in ["plain", "a `span`", "``double``", "`leading", "trailing`"]:
        rendered = metrics._inline_code(text)
        fence = re.match(r"`+", rendered).group(0)
        assert rendered.endswith(fence)
        assert len(re.findall(r"`+", text) or [""]) and fence not in text


def test_the_cited_path_pattern_covers_every_tracked_top_level_directory(metrics):
    """Hand-written, it went four directories stale in silence (#1576).

    The fallback is what a replay outside a checkout uses, so it is the
    half that can still drift; the derived set cannot.
    """
    tracked = metrics.tracked_top_level()

    assert "faultmaven" in tracked and "tests" in tracked  # a real checkout
    missing = [
        name
        for name in tracked
        if not metrics._cited_path_pattern(metrics._FALLBACK_TOP_LEVEL).search(
            f"{name}/sample.py"
        )
    ]
    assert missing == [], f"fallback list is stale: {missing}"


def test_the_cited_path_pattern_falls_back_outside_a_checkout(metrics, tmp_path):
    assert metrics.tracked_top_level(tmp_path) == metrics._FALLBACK_TOP_LEVEL


def test_every_body_is_parsed_once_per_run(metrics, monkeypatch):
    """Four readers used to strip each body for themselves, which is the
    place two readings of one body could drift apart (#1577)."""
    seen = []
    original = metrics.without_code_blocks
    monkeypatch.setattr(
        metrics,
        "without_code_blocks",
        lambda body: (seen.append(body), original(body))[1],
    )
    issues = metrics.load_issues(
        [
            _ready(1, 1, body="Found while working on #9. `scripts/sample_a.py`"),
            _blocked(2, 2, body="**Blocked on:** #1 — `scripts/sample_b.py`"),
            _issue(3, 3, body="unlabelled, `docs/sample-note.md`"),
        ]
    )
    metrics.compute(issues, LATER, REPO, resolve_parents=False)

    assert len(seen) == len(issues)

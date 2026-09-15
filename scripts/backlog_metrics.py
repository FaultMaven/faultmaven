#!/usr/bin/env python3
"""Measure whether the backlog is converging (#1453).

The open-issue count is a poor instrument: it has sat between 50 and 63 for
ten weeks while ~25 issues a week were opened and ~22 closed. The count hides
that the backlog is TWO populations with different dynamics:

* **Fast closures** — filed and fixed inside the same lane, usually within a
  day. These are discovery, not debt; counting them as churn is what makes
  the backlog look like it is not moving.
* **The residue** — issues that survive their first week. Almost nothing
  closes them except a deliberate sweep, so the residue grows at its inflow
  rate and drains in bursts. The residue is what "converging" has to mean.

This script reports the residue on its own, plus the two rates the campaign
in ``docs/development/issue-processing.md`` is judged by: how long a fixed
defect had been latent before it was found (draining an old pool versus
generating new debt), and how many follow-up issues a fixed issue produced.

Usage::

    python scripts/backlog_metrics.py                  # fetch via gh, print
    python scripts/backlog_metrics.py --issues i.json --as-of 2026-09-15T20:00:00Z
    python scripts/backlog_metrics.py --latency        # also blame fix PRs

``--latency`` runs ``git blame`` over every fix PR's diff and is slow (about
half a minute per hundred fix PRs); the rest completes in seconds. The
flow, survival and open-set numbers come from GitHub metadata and never from
reading issue text; the follow-up count is the one section that reads a
body, and only for the lane marker ("found while working on #N"). An open
issue younger than the residue threshold is reported as *pending*, not as
residue, so the newest week's row is comparable to the same row on a later
run. A saved dump is replayed with ``--as-of <the time it was taken>``; ages
are measured from that instant, not from when the file is re-read.
``--offline`` makes a replay a no-network run (parent markers naming a PR
then stay unresolved).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

#: The checkout this script lives in. Every git call is anchored here so the
#: script measures the same tree from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

from faultmaven.utils.datetime import parse_utc_timestamp  # noqa: E402

_DAY = 86400.0

#: An issue that is still open after this many days has left the fast
#: population: from here on only a deliberate pick closes it.
RESIDUE_THRESHOLD_DAYS = 7

#: Independent subprocesses per PR (git) and per batch (gh); each releases
#: the GIL while it waits, so the outer loops run concurrently.
_WORKERS = 8

#: The markers a lane writes when it files an issue it found while doing
#: other work. Matching the marker (rather than any ``#N``) keeps a citation
#: of a related issue from being read as a parent. ``review of`` is bound
#: tightly to its ``#`` because the bare phrase occurs in ordinary prose.
_PARENT_MARKER = re.compile(
    r"(?:found while|surfaced (?:while|by|during)|while (?:working on|fixing|"
    r"reviewing)|spun out of|deferred from|follow-?up (?:to|from|of)|"
    r"out of scope (?:for|of)|split (?:out|from)|during (?:the )?work on)"
    r"[^\n#]{0,80}?(?P<ref>(?:[\w.-]*[-/][\w.-]*\s*)?#(?P<n>\d+))"
    r"|review of (?:PR |pull request )?(?P<ref2>(?:[\w.-]*[-/][\w.-]*\s*)?#(?P<n2>\d+))",
    re.IGNORECASE,
)

_ISSUE_FIELDS = "number,createdAt,closedAt,labels,body"


@dataclass(frozen=True)
class Issue:
    number: int
    created: dt.datetime
    closed: dt.datetime | None
    labels: tuple[str, ...]
    body: str

    @property
    def is_open(self) -> bool:
        return self.closed is None

    @property
    def age_at_close_days(self) -> float | None:
        if self.closed is None:
            return None
        return (self.closed - self.created).total_seconds() / _DAY

    def age_days(self, now: dt.datetime) -> float:
        return (now - self.created).total_seconds() / _DAY

    def is_residue(self, now: dt.datetime) -> bool:
        """Open past the threshold as of ``now``, whether or not it closed later.

        An open issue younger than the threshold is NOT residue yet: it is
        still inside the window in which most issues close. Counting it would
        inflate the newest week by exactly the population this measure exists
        to exclude, and the same row would shrink on the next run.
        """
        age = self.age_at_close_days
        if age is not None:
            return age > RESIDUE_THRESHOLD_DAYS
        return self.age_days(now) > RESIDUE_THRESHOLD_DAYS

    def is_pending(self, now: dt.datetime) -> bool:
        """Open, and too young to be called residue."""
        return self.is_open and not self.is_residue(now)


def load_issues(raw: Iterable[dict]) -> list[Issue]:
    """Build ``Issue`` rows from ``gh issue list --json`` output."""
    issues = []
    for item in raw:
        closed_at = item.get("closedAt")
        issues.append(
            Issue(
                number=int(item["number"]),
                created=parse_utc_timestamp(item["createdAt"]),
                closed=parse_utc_timestamp(closed_at) if closed_at else None,
                labels=tuple(label["name"] for label in item.get("labels", ())),
                body=item.get("body") or "",
            )
        )
    return sorted(issues, key=lambda issue: issue.number)


def _gh(*args: str) -> subprocess.CompletedProcess:
    """Run ``gh``; a missing binary is a one-line exit, not a traceback."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("gh is not installed or not on PATH; pass --offline to skip it")


def fetch_issues(repo: str) -> list[Issue]:
    result = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        "5000",
        "--json",
        _ISSUE_FIELDS,
    )
    if result.returncode != 0:
        sys.exit(f"gh issue list failed: {result.stderr.strip() or 'no diagnostic'}")
    return load_issues(json.loads(result.stdout))


def _week(when: dt.datetime) -> tuple[int, int]:
    iso = when.isocalendar()
    return (iso[0], iso[1])


def _week_end(year: int, week: int) -> dt.datetime:
    return dt.datetime.fromisocalendar(year, week, 7).replace(
        hour=23, minute=59, second=59, tzinfo=dt.UTC
    )


def _weeks_between(first: tuple[int, int], last: tuple[int, int]):
    """Every ISO week from ``first`` to ``last`` inclusive, quiet ones too."""
    cursor = dt.date.fromisocalendar(first[0], first[1], 1)
    end = dt.date.fromisocalendar(last[0], last[1], 1)
    while cursor <= end:
        iso = cursor.isocalendar()
        yield (iso[0], iso[1])
        cursor += dt.timedelta(days=7)


def snapshot_at(issues: Sequence[Issue], now: dt.datetime) -> list[Issue]:
    """The corpus as it stood at ``now``: later filings absent, later
    closures not yet closed. Makes ``--as-of`` a point-in-time read rather
    than a mix of ages measured at one instant and events from another.
    """
    snapshot = []
    for issue in issues:
        if issue.created > now:
            continue
        if issue.closed is not None and issue.closed > now:
            issue = Issue(issue.number, issue.created, None, issue.labels, issue.body)
        snapshot.append(issue)
    return snapshot


def open_count_at(issues: Sequence[Issue], when: dt.datetime) -> int:
    return sum(
        1
        for issue in issues
        if issue.created <= when and (issue.closed is None or issue.closed > when)
    )


def weekly_flow(issues: Sequence[Issue], now: dt.datetime) -> list[dict]:
    """Per ISO week, quiet weeks included: opened, closed, and the residue's
    own inflow and drain.

    ``residue_in`` counts issues opened that week that were NOT closed within
    the threshold. ``residue_out`` counts closures that week of issues older
    than the threshold. ``pending`` is what the newest rows carry instead:
    open issues too young to be residue yet, so the row is provisional by
    exactly that number. The difference ``residue_net`` is what the campaign
    has to drive negative; ``opened - closed`` is dominated by same-lane
    discovery and says little. A week with no event is a row of zeros, so
    "the last N weeks" means calendar weeks and a zero week is visible.
    """
    if not issues:
        return []
    opened: Counter = Counter()
    closed: Counter = Counter()
    residue_in: Counter = Counter()
    residue_out: Counter = Counter()
    pending: Counter = Counter()
    for issue in issues:
        week = _week(issue.created)
        opened[week] += 1
        if issue.is_pending(now):
            pending[week] += 1
        elif issue.is_residue(now):
            residue_in[week] += 1
        if issue.closed is not None:
            closed[_week(issue.closed)] += 1
            if issue.is_residue(now):
                residue_out[_week(issue.closed)] += 1
    first = min(_week(issue.created) for issue in issues)
    rows = []
    for week in _weeks_between(first, _week(now)):
        rows.append(
            {
                "week": f"{week[0]}-W{week[1]:02d}",
                "opened": opened[week],
                "closed": closed[week],
                "net": opened[week] - closed[week],
                "residue_in": residue_in[week],
                "residue_out": residue_out[week],
                "residue_net": residue_in[week] - residue_out[week],
                "pending": pending[week],
                # The newest week is incomplete: its count is "open now".
                "open_at_week_end": open_count_at(issues, min(_week_end(*week), now)),
            }
        )
    return rows


def survival(
    issues: Sequence[Issue],
    now: dt.datetime,
    horizons: Sequence[int] = (1, 7, 30),
    min_exposure_days: int = 30,
) -> dict:
    """What fraction of issues close within each horizon.

    Only issues old enough to have had the full exposure are counted, so a
    burst of recent filings cannot depress the rates. The survivors are then
    partitioned three ways — closed by the last horizon, closed later (the
    sweep drain the residue measure is about), still open — so the parts
    sum to the survivors.
    """
    cohort = [issue for issue in issues if issue.age_days(now) >= min_exposure_days]
    result = {
        "cohort": len(cohort),
        "closed_within": {},
        "survivors": 0,
        "survivors_closed": 0,
        "survivors_closed_late": 0,
        "survivors_open": 0,
    }
    if not cohort:
        return result

    def closed_within(issue: Issue, days: int) -> bool:
        age = issue.age_at_close_days
        return age is not None and age <= days

    survivors = [issue for issue in cohort if issue.is_residue(now)]
    last = max(horizons)
    result["closed_within"] = {
        days: sum(closed_within(issue, days) for issue in cohort) / len(cohort)
        for days in horizons
    }
    result["survivors"] = len(survivors)
    result["survivors_closed"] = sum(closed_within(issue, last) for issue in survivors)
    result["survivors_open"] = sum(1 for issue in survivors if issue.is_open)
    result["survivors_closed_late"] = (
        len(survivors) - result["survivors_closed"] - result["survivors_open"]
    )
    return result


def residue_snapshot(issues: Sequence[Issue], now: dt.datetime) -> dict:
    """The open set by age and by priority label."""
    open_issues = [issue for issue in issues if issue.is_open]
    ages = sorted(issue.age_days(now) for issue in open_issues)
    by_priority: Counter = Counter()
    for issue in open_issues:
        priority = next(
            (label for label in issue.labels if re.fullmatch(r"P[0-3]", label)),
            "unranked",
        )
        by_priority[priority] += 1
    return {
        "open": len(ages),
        "median_age_days": statistics.median(ages) if ages else 0.0,
        "older_than_threshold": sum(issue.is_residue(now) for issue in open_issues),
        "older_than_30d": sum(1 for age in ages if age > 30),
        "by_priority": dict(sorted(by_priority.items())),
    }


def parent_of(issue: Issue, repo: str) -> int | None:
    """The number a lane marker in ``issue`` names, or ``None``.

    A reference qualified with this repository's own name
    (``FaultMaven/faultmaven#N``) is that number; one qualified with any
    other repository-shaped token — a slash, or a hyphenated name — before
    the ``#`` (``faultmaven-dashboard#N``, ``faultmaven-dashboard #N``) is a
    reference elsewhere and is ``None``. An issue never parents itself (a
    correction note citing the review of its own fix PR would otherwise
    resolve back to it). This is the ONE place the marker grammar lives;
    every reader of it calls here.
    """
    match = _PARENT_MARKER.search(issue.body)
    if not match:
        return None
    ref = match.group("ref") or match.group("ref2")
    number = int(match.group("n") or match.group("n2"))
    qualifier = ref[: ref.index("#")].strip()
    if qualifier and qualifier.lower() != repo.lower():
        return None
    return None if number == issue.number else number


def marker_numbers(issues: Sequence[Issue], repo: str) -> list[int]:
    """Every number a parent marker names that is not an issue here."""
    known = {issue.number for issue in issues}
    found = {parent_of(issue, repo) for issue in issues} - known - {None}
    return sorted(found)


def follow_ups(
    issues: Sequence[Issue], repo: str, pr_links: dict[int, list[int]] | None = None
) -> dict:
    """Issues that name a parent they were found while working on.

    A parent is counted only when it is an issue in this corpus. A number
    that is not is usually the PR the lane was working; ``pr_links`` (PR
    number → the issues it closed, from :func:`pr_closing_issues`) resolves
    those to the issue the PR was for. What still resolves to nothing — a PR
    that closed no issue — is reported as ``unresolved`` rather than as a
    parent, because the per-lane rate is a rate per ISSUE.

    The regex reads the lane's own marker, so an issue whose parent is named
    without one is missed; treat the count as a floor. Whether a follow-up
    was CAUSED by the parent's fix (a regression) or merely found during it
    cannot be read from metadata and is classified by hand in the campaign
    report.
    """
    known = {issue.number for issue in issues}
    children: dict[int, list[int]] = defaultdict(list)
    unresolved = 0
    via_pr = 0
    for issue in issues:
        parent = parent_of(issue, repo)
        if parent is None:
            continue
        if parent not in known and pr_links:
            closed_here = [n for n in pr_links.get(parent, ()) if n in known]
            if closed_here:
                parent = min(closed_here)
                via_pr += 1
        if parent == issue.number:
            unresolved += 1  # the PR named closed this very issue
        elif parent in known:
            children[parent].append(issue.number)
        else:
            unresolved += 1
    parents = sorted(children.items(), key=lambda item: (-len(item[1]), item[0]))
    return {
        "attributed": sum(len(kids) for kids in children.values()),
        "parents": len(children),
        "via_pr": via_pr,
        "unresolved": unresolved,
        "top": [(parent, kids) for parent, kids in parents[:10]],
    }


# --------------------------------------------------------------------------
# Fix latency: how long the defective code sat in the tree before it was found
# --------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT
    )
    return result.stdout if result.returncode == 0 else None


def _graphql_batch(chunk: Sequence[int], repo: str, field: str, label: str) -> dict:
    owner, name = repo.split("/")
    fields = " ".join(f"i{n}: " + field.format(n=n) for n in chunk)
    query = f'{{ repository(owner:"{owner}",name:"{name}"){{ {fields} }} }}'
    result = _gh("api", "graphql", "-f", f"query={query}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"graphql returned no body: {result.stderr[:200]}", file=sys.stderr)
        return {}
    repository = (payload.get("data") or {}).get("repository") or {}
    if not repository:
        reason = payload.get("message") or "; ".join(
            e.get("message", "") for e in payload.get("errors", [])
        )
        print(
            f"graphql batch of {len(chunk)} {label} failed: "
            f"{reason or result.stderr[:200] or 'no diagnostic'}",
            file=sys.stderr,
        )
        return {}
    out = {}
    for alias, value in repository.items():
        if value is None:
            print(f"unresolvable {label} {alias[1:]}", file=sys.stderr)
            continue
        out[int(alias[1:])] = value
    return out


def _graphql_batches(
    numbers: Sequence[int], repo: str, field: str, label: str
) -> dict[int, dict]:
    """Run ``field`` (a GraphQL field template with ``{n}``) for every number
    in batches of 40, concurrently, and return ``number → node``.

    GitHub answers a batch with partial data plus an ``errors`` array when
    one number cannot be resolved (deleted, transferred, or the wrong kind),
    and ``gh`` exits non-zero on that. The body is still parsed and the
    resolvable aliases kept. A whole-batch failure — ``data: null`` with
    ``errors``, or a REST-style ``{"message": ...}`` such as a bad credential
    or a rate limit — is reported to stderr with GitHub's own words, so a run
    that dated nothing says why.
    """
    chunks = [numbers[start : start + 40] for start in range(0, len(numbers), 40)]
    out: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for part in pool.map(lambda c: _graphql_batch(c, repo, field, label), chunks):
            out.update(part)
    return out


def closing_prs(numbers: Sequence[int], repo: str) -> dict[int, list[dict]]:
    """Which merged PRs closed each issue, from GitHub's own linkage."""
    nodes = _graphql_batches(
        numbers,
        repo,
        "issue(number:{n}){{ closedByPullRequestsReferences(first:5)"
        "{{ nodes {{ number mergedAt mergeCommit {{ oid }} }} }} }}",
        "issue",
    )
    return {n: v["closedByPullRequestsReferences"]["nodes"] for n, v in nodes.items()}


def pr_closing_issues(numbers: Sequence[int], repo: str) -> dict[int, list[int]]:
    """Which issues each PR closed — the inverse linkage, for parent markers."""
    nodes = _graphql_batches(
        numbers,
        repo,
        "pullRequest(number:{n}){{ closingIssuesReferences(first:10)"
        "{{ nodes {{ number }} }} }}",
        "pull request",
    )
    return {
        n: [node["number"] for node in v["closingIssuesReferences"]["nodes"]]
        for n, v in nodes.items()
    }


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")


def removed_lines(diff: str) -> list[tuple[str, int]]:
    """``(old path, old line number)`` for every line a unified diff removed.

    The file is taken from the ``diff --git`` header (a removed CONTENT line
    can begin ``-- a/`` and would fool a ``---`` scan), and the diff must
    carry the ``a/``/``b/`` prefixes — the caller forces them, because
    ``diff.noprefix`` would otherwise blank every path. A renamed file is
    dated on its OLD path, so a move-and-edit contributes the lines the fix
    changed and not the whole moved file. A new file has no pre-fix lines
    and a test file is not the defect. A pure insertion is recorded as a
    NEGATIVE anchor at the insertion point; the caller uses the anchors only
    when nothing was removed.
    """
    lines: list[tuple[str, int]] = []
    path: str | None = None
    for line in diff.split("\n"):
        header = _DIFF_HEADER.match(line)
        if header:
            path = header.group(1)
        elif line.startswith("--- /dev/null"):
            path = None
        elif line.startswith("@@") and path and not _is_test_path(path):
            hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if hunk is None:
                continue
            start = int(hunk.group(1))
            count = int(hunk.group(2)) if hunk.group(2) is not None else 1
            if count == 0:
                lines.append((path, -max(start, 1)))
            else:
                lines.extend((path, n) for n in range(start, start + count))
    return lines


def _blame_times(commit: str, path: str) -> dict[int, int]:
    out = _git("blame", "--porcelain", "-w", commit, "--", path)
    times: dict[int, int] = {}
    if not out:
        return times
    line_to_commit: dict[int, str] = {}
    commit_time: dict[str, int] = {}
    current = None
    for line in out.split("\n"):
        head = re.match(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$", line)
        if head:
            current = head.group(1)
            line_to_commit[int(head.group(3))] = current
        elif line.startswith("author-time ") and current:
            commit_time[current] = int(line.split()[1])
    return {n: commit_time[c] for n, c in line_to_commit.items() if c in commit_time}


def _intro_times(
    pairs: Sequence[tuple[str, int]], commit: str, cache: dict[str, dict[int, int]]
) -> list[int]:
    """Author times of the given ``(path, line)`` pairs at ``commit``."""
    found = []
    for path, line in pairs:
        if path not in cache:
            cache[path] = _blame_times(commit, path)
        if line in cache[path]:
            found.append(cache[path][line])
    return found


def latency_days(found: dt.datetime, intro_times: Sequence[int]) -> dict | None:
    """Latency of one defect from the introduction times of its removed lines.

    The median is the headline: a fix usually touches lines of several ages
    and the median resists both a reformatting commit and one ancient import.
    A line authored AFTER the issue was filed cannot have caused it (a peer
    PR reflowed the file in between), so such lines are dropped before the
    median is taken; a fix with no pre-issue line at all is undatable and
    answers ``None``.
    """
    found_ts = found.timestamp()
    dated = [t for t in intro_times if t <= found_ts]
    if not dated:
        return None
    return {
        "n_lines": len(dated),
        "n_dropped": len(intro_times) - len(dated),
        "median": (found_ts - statistics.median(dated)) / _DAY,
        "oldest": (found_ts - min(dated)) / _DAY,
        "newest": (found_ts - max(dated)) / _DAY,
    }


#: The diff a fix PR is dated from. Prefixes forced (``diff.noprefix`` would
#: blank them), non-ASCII paths unquoted, renames detected so a move-and-edit
#: is dated on the lines it changed at the OLD path rather than on the whole
#: moved file, and deleted files kept (their lines are pre-fix lines).
_DIFF_ARGS = (
    "-c",
    "core.quotePath=false",
    "diff",
    "-U0",
    "-M",
    "--diff-filter=MDR",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def _date_pr(pr_number: int, entry: dict, by_number: dict[int, Issue]) -> tuple:
    """``("row", row)`` or ``("skipped", reason)`` for one fix PR."""
    oid = entry["oid"]
    if _git("cat-file", "-e", oid) is None:
        return "skipped", "merge commit not in local checkout (fetch?)"
    diff = _git(*_DIFF_ARGS, f"{oid}^", oid, "--", "faultmaven/")
    if diff is None:
        return "skipped", "git diff failed (shallow clone?)"
    if not diff:
        return "skipped", "no source change under faultmaven/"
    targets = removed_lines(diff)
    removed = [(p, n) for p, n in targets if n > 0]
    anchors = [(p, -n) for p, n in targets if n < 0]
    cache: dict[str, dict[int, int]] = {}
    intro = _intro_times(removed, f"{oid}^", cache)
    method = "removed"
    if not intro:
        intro = _intro_times(anchors, f"{oid}^", cache)
        method = "context"
    if not intro:
        return "skipped", "no blameable line"
    found = min(by_number[n].created for n in entry["issues"])
    stats = latency_days(found, intro)
    if stats is None:
        return "skipped", "every removed line postdates the issue"
    return "row", {
        "pr": pr_number,
        "issues": sorted(entry["issues"]),
        "method": method,
        **stats,
    }


def fix_latency(issues: Sequence[Issue], repo: str) -> dict:
    """One row per FIX PR, dated against the earliest issue it closed.

    Per PR rather than per issue: a sweep PR closing six issues would
    otherwise contribute six identical medians and weight the distribution by
    PR size; an issue closed by two merged PRs contributes to both rows.
    Returns ``rows``, ``skipped`` (PRs, by reason) and ``unlinked_issues``
    (closed issues GitHub links to no merged PR — by hand, by duplicate, by
    commit message) so the report has its denominator on both axes.

    ``mergeCommit`` is the squash commit for a squash merge, which is how
    this repository merges; a rebase merge would name only the PR's last
    commit and date a partial diff.
    """
    closed = [issue for issue in issues if issue.closed is not None]
    by_number = {issue.number: issue for issue in closed}
    linkage = closing_prs([issue.number for issue in closed], repo)
    per_pr: dict[int, dict] = {}
    unlinked = 0
    for issue in closed:
        merged = [pr for pr in linkage.get(issue.number, ()) if pr.get("mergeCommit")]
        if not merged:
            unlinked += 1
            continue
        for pr in merged:
            entry = per_pr.setdefault(
                pr["number"], {"oid": pr["mergeCommit"]["oid"], "issues": []}
            )
            entry["issues"].append(issue.number)

    rows = []
    skipped: Counter = Counter()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = pool.map(
            lambda item: _date_pr(item[0], item[1], by_number), sorted(per_pr.items())
        )
        for kind, value in results:
            if kind == "row":
                rows.append(value)
            else:
                skipped[value] += 1
    return {"rows": rows, "skipped": dict(skipped), "unlinked_issues": unlinked}


def _percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile over an ascending sequence (rank = ⌈p·n⌉)."""
    rank = max(1, math.ceil(pct / 100 * len(values)))
    return values[rank - 1]


def latency_distribution(rows: Sequence[dict]) -> dict:
    values = sorted(row["median"] for row in rows)
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p25": _percentile(values, 25),
        "median": statistics.median(values),
        "p75": _percentile(values, 75),
        "under_7d": sum(1 for v in values if v < 7) / len(values),
        "over_90d": sum(1 for v in values if v > 90) / len(values),
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Iterable[Sequence]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _skipped_text(skipped: dict) -> str:
    return ", ".join(f"{v} {k}" for k, v in sorted(skipped.items())) or "none"


def compute(
    issues: Sequence[Issue],
    now: dt.datetime,
    repo: str,
    latency: bool = False,
    resolve_parents: bool = False,
) -> dict:
    pr_links = (
        pr_closing_issues(marker_numbers(issues, repo), repo)
        if resolve_parents
        else None
    )
    return {
        "as_of": now.isoformat(),
        "weekly": weekly_flow(issues, now),
        "survival": survival(issues, now),
        "open": residue_snapshot(issues, now),
        "follow_ups": follow_ups(issues, repo, pr_links),
        "latency": fix_latency(issues, repo) if latency else None,
    }


def report(results: dict, weeks: int) -> str:
    out = []
    flow = results["weekly"][-weeks:]
    out.append(f"## Weekly flow (residue = open past {RESIDUE_THRESHOLD_DAYS} days)\n")
    out.append(
        _table(
            [
                "week",
                "opened",
                "closed",
                "net",
                "residue in",
                "residue out",
                "residue net",
                "pending",
                "open at week end",
            ],
            (
                (
                    row["week"],
                    row["opened"],
                    row["closed"],
                    f"{row['net']:+d}",
                    row["residue_in"],
                    row["residue_out"],
                    f"{row['residue_net']:+d}",
                    row["pending"],
                    row["open_at_week_end"],
                )
                for row in flow
            ),
        )
    )
    total_in = sum(row["residue_in"] for row in flow)
    total_out = sum(row["residue_out"] for row in flow)
    pending = sum(row["pending"] for row in results["weekly"])
    out.append(
        f"\nResidue over the window: in {total_in}, out {total_out}, "
        f"net {total_in - total_out:+d}; {pending} open issues are still too "
        f"young to count.\n"
    )

    surv = results["survival"]
    out.append("## Survival\n")
    if surv["cohort"]:
        last = max(surv["closed_within"])
        out.append(
            f"Cohort with ≥30 days exposure: {surv['cohort']}. Closed within "
            + ", ".join(f"{d}d: {p:.0%}" for d, p in surv["closed_within"].items())
            + f". Survived {RESIDUE_THRESHOLD_DAYS}d: {surv['survivors']} = "
            f"closed by {last}d: {surv['survivors_closed']} + closed later: "
            f"{surv['survivors_closed_late']} + still open: "
            f"{surv['survivors_open']}.\n"
        )
    else:
        out.append("No issue has had 30 days of exposure yet.\n")

    snap = results["open"]
    out.append("## Open set\n")
    out.append(
        f"Open: {snap['open']}; median age {snap['median_age_days']:.0f}d; "
        f"{snap['older_than_threshold']} older than {RESIDUE_THRESHOLD_DAYS}d, "
        f"{snap['older_than_30d']} older than 30d. By priority label: "
        f"{snap['by_priority']}.\n"
    )

    fu = results["follow_ups"]
    out.append("## Follow-ups (regex floor)\n")
    out.append(
        f"{fu['attributed']} issues name a parent they were found while working "
        f"on, across {fu['parents']} parent issues ({fu['via_pr']} resolved "
        f"through the PR the marker named); {fu['unresolved']} name a number "
        "that resolves to no issue here (a PR that closed none). "
        "Most prolific: "
        + ", ".join(f"#{p} ({len(k)})" for p, k in fu["top"][:6])
        + ".\n"
    )

    if results["latency"] is not None:
        dist = latency_distribution(results["latency"]["rows"])
        skipped = results["latency"]["skipped"]
        unlinked = results["latency"]["unlinked_issues"]
        out.append("## Fix latency (blame on the removed lines of each fix PR)\n")
        if dist["n"]:
            out.append(
                f"{dist['n']} dated fix PRs, {sum(skipped.values())} skipped "
                f"({_skipped_text(skipped)}); {unlinked} closed issues link to no "
                f"merged PR and are outside this measure. "
                f"Median latency {dist['median']:.0f}d "
                f"(p25 {dist['p25']:.0f}d, p75 {dist['p75']:.0f}d); "
                f"{dist['under_7d']:.0%} under a week (introduced by recent work), "
                f"{dist['over_90d']:.0%} over 90 days (old pool).\n"
            )
        else:
            out.append(
                f"No fix could be dated ({_skipped_text(skipped)}; {unlinked} "
                "closed issues link to no merged PR).\n"
            )
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0] or "backlog metrics"
    )
    parser.add_argument(
        "--issues", type=Path, help="saved `gh issue list --json` output"
    )
    parser.add_argument("--repo", default="FaultMaven/faultmaven")
    parser.add_argument(
        "--weeks", type=int, default=12, help="most recent calendar weeks to print"
    )
    parser.add_argument(
        "--latency",
        action="store_true",
        help="also date every fix via git blame (slow)",
    )
    parser.add_argument(
        "--as-of",
        type=parse_utc_timestamp,
        help="measure ages from this instant (the time a --issues dump was taken); "
        "default: now",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never call gh: parent markers naming a PR stay unresolved",
    )
    parser.add_argument("--json", type=Path, help="write every computed row here")
    args = parser.parse_args(argv)
    if args.weeks < 1:
        parser.error("--weeks must be at least 1")
    if args.offline and args.latency:
        parser.error("--latency needs gh; drop --offline")

    if args.issues:
        issues = load_issues(json.loads(args.issues.read_text()))
        if args.as_of is None:
            print(
                "note: replaying a dump without --as-of measures ages from now; "
                "pass the time the dump was taken for a reproducible run",
                file=sys.stderr,
            )
    else:
        issues = fetch_issues(args.repo)
    now = args.as_of or dt.datetime.now(dt.UTC)
    before = len(issues)
    later_closures = sum(1 for i in issues if i.closed and i.closed > now)
    issues = snapshot_at(issues, now)
    if before - len(issues) or later_closures:
        print(
            f"note: --as-of precedes the dump's own events: {before - len(issues)} "
            f"issues filed later are dropped and {later_closures} closures later "
            "are treated as still open, so the run is a snapshot at that instant",
            file=sys.stderr,
        )
    results = compute(
        issues,
        now,
        args.repo,
        latency=args.latency,
        resolve_parents=not args.offline,
    )
    print(report(results, args.weeks))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

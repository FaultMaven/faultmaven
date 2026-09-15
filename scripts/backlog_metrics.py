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
    python scripts/backlog_metrics.py --issues i.json  # from a saved dump
    python scripts/backlog_metrics.py --latency        # also blame fix PRs

``--latency`` runs ``git blame`` over every fix PR's diff and is slow (about a
minute per hundred closed issues); the rest completes in under a second.
Everything the script prints is derived from GitHub metadata and the git
history, never from reading issue text, so a re-run is comparable to the
last one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_DAY = 86400.0

#: An issue that is still open after this many days has left the fast
#: population: from here on only a deliberate pick closes it.
RESIDUE_THRESHOLD_DAYS = 7

#: The markers a lane writes when it files an issue it found while doing other
#: work. Matching the marker (rather than any ``#N``) keeps a citation of a
#: related issue from being read as a parent.
_PARENT_MARKER = re.compile(
    r"(?:found while|surfaced (?:while|by|during)|while (?:working on|fixing|"
    r"reviewing)|spun out of|deferred from|follow-?up (?:to|from|of)|"
    r"out of scope (?:for|of)|split (?:out|from)|during (?:the )?(?:work on|"
    r"review of)|review of)[^\n#]{0,80}#(\d+)",
    re.IGNORECASE,
)

_ISSUE_FIELDS = "number,title,state,createdAt,closedAt,labels,body,author,url"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    created: dt.datetime
    closed: dt.datetime | None
    labels: tuple[str, ...]
    body: str

    @property
    def age_at_close_days(self) -> float | None:
        if self.closed is None:
            return None
        return (self.closed - self.created).total_seconds() / _DAY

    def is_residue(self) -> bool:
        """Still open past the threshold, whether or not it closed later."""
        age = self.age_at_close_days
        return age is None or age > RESIDUE_THRESHOLD_DAYS


def _parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_issues(raw: Iterable[dict]) -> list[Issue]:
    """Build ``Issue`` rows from ``gh issue list --json`` output."""
    issues = []
    for item in raw:
        issues.append(
            Issue(
                number=int(item["number"]),
                title=item.get("title", ""),
                state=item["state"],
                created=_parse_ts(item["createdAt"]),
                closed=_parse_ts(item["closedAt"]) if item.get("closedAt") else None,
                labels=tuple(label["name"] for label in item.get("labels", ())),
                body=item.get("body") or "",
            )
        )
    return sorted(issues, key=lambda issue: issue.number)


def fetch_issues(repo: str | None) -> list[Issue]:
    cmd = [
        "gh",
        "issue",
        "list",
        "--state",
        "all",
        "--limit",
        "1000",
        "--json",
        _ISSUE_FIELDS,
    ]
    if repo:
        cmd += ["--repo", repo]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return load_issues(json.loads(out))


def _week(when: dt.datetime) -> tuple[int, int]:
    iso = when.isocalendar()
    return (iso[0], iso[1])


def weekly_flow(issues: Sequence[Issue]) -> list[dict]:
    """Per ISO week: opened, closed, and the residue's own inflow and drain.

    ``residue_in`` counts issues opened that week that were NOT closed within
    the threshold (they joined the residue, or are on their way to it).
    ``residue_out`` counts closures that week of issues older than the
    threshold. The difference is the number the campaign has to drive
    negative; ``opened - closed`` is dominated by same-lane discovery and says
    little.
    """
    opened: Counter = Counter()
    closed: Counter = Counter()
    residue_in: Counter = Counter()
    residue_out: Counter = Counter()
    for issue in issues:
        opened[_week(issue.created)] += 1
        if issue.is_residue():
            residue_in[_week(issue.created)] += 1
        if issue.closed is not None:
            closed[_week(issue.closed)] += 1
            if issue.is_residue():
                residue_out[_week(issue.closed)] += 1
    rows = []
    for week in sorted(set(opened) | set(closed)):
        rows.append(
            {
                "week": f"{week[0]}-W{week[1]:02d}",
                "opened": opened[week],
                "closed": closed[week],
                "net": opened[week] - closed[week],
                "residue_in": residue_in[week],
                "residue_out": residue_out[week],
                "residue_net": residue_in[week] - residue_out[week],
            }
        )
    return rows


def open_count_at(issues: Sequence[Issue], when: dt.datetime) -> int:
    return sum(
        1
        for issue in issues
        if issue.created <= when and (issue.closed is None or issue.closed > when)
    )


def survival(
    issues: Sequence[Issue],
    now: dt.datetime,
    horizons: Sequence[int] = (1, 7, 30),
    min_exposure_days: int = 30,
) -> dict:
    """What fraction of issues close within each horizon.

    Only issues old enough to have had the full exposure are counted, so a
    burst of recent filings cannot depress the rates. The second number is the
    conditional one that matters for the residue: of the issues that survived
    the threshold, how many closed by the last horizon.
    """
    cohort = [
        issue
        for issue in issues
        if (now - issue.created).total_seconds() / _DAY >= min_exposure_days
    ]
    if not cohort:
        return {"cohort": 0, "closed_within": {}, "survivors": 0, "survivors_closed": 0}

    def closed_within(issue: Issue, days: int) -> bool:
        age = issue.age_at_close_days
        return age is not None and age <= days

    survivors = [issue for issue in cohort if issue.is_residue()]
    last = max(horizons)
    return {
        "cohort": len(cohort),
        "closed_within": {
            days: sum(closed_within(issue, days) for issue in cohort) / len(cohort)
            for days in horizons
        },
        "survivors": len(survivors),
        "survivors_closed": sum(closed_within(issue, last) for issue in survivors),
        "survivors_open": sum(1 for issue in survivors if issue.state == "OPEN"),
    }


def residue_snapshot(issues: Sequence[Issue], now: dt.datetime) -> dict:
    """The open set by age and by priority label."""
    ages = sorted(
        (now - issue.created).total_seconds() / _DAY
        for issue in issues
        if issue.state == "OPEN"
    )
    by_priority: Counter = Counter()
    for issue in issues:
        if issue.state != "OPEN":
            continue
        priority = next(
            (label for label in issue.labels if re.fullmatch(r"P[0-3]", label)),
            "unranked",
        )
        by_priority[priority] += 1
    return {
        "open": len(ages),
        "median_age_days": statistics.median(ages) if ages else 0.0,
        "older_than_threshold": sum(1 for age in ages if age > RESIDUE_THRESHOLD_DAYS),
        "older_than_30d": sum(1 for age in ages if age > 30),
        "by_priority": dict(sorted(by_priority.items())),
    }


def follow_ups(issues: Sequence[Issue]) -> dict:
    """Issues that name a parent they were found while working on.

    The regex reads the lane's own marker, so an issue whose parent is named
    without one is missed; treat the count as a floor. Whether a follow-up was
    CAUSED by the parent's fix (a regression) or merely found during it cannot
    be read from metadata and is classified by hand in the campaign report.
    """
    children: dict[int, list[int]] = defaultdict(list)
    for issue in issues:
        match = _PARENT_MARKER.search(issue.body)
        if match:
            children[int(match.group(1))].append(issue.number)
    parents = sorted(children.items(), key=lambda item: (-len(item[1]), item[0]))
    return {
        "attributed": sum(len(kids) for kids in children.values()),
        "parents": len(children),
        "top": [(parent, kids) for parent, kids in parents[:10]],
    }


# --------------------------------------------------------------------------
# Fix latency: how long the defective code sat in the tree before it was found
# --------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str | None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    return result.stdout if result.returncode == 0 else None


def closing_prs(numbers: Sequence[int], repo: str) -> dict[int, list[dict]]:
    """Which merged PR closed each issue, from GitHub's own linkage."""
    owner, name = repo.split("/")
    out: dict[int, list[dict]] = {}
    for start in range(0, len(numbers), 40):
        chunk = numbers[start : start + 40]
        fields = " ".join(
            f"i{n}: issue(number:{n}){{ number closedByPullRequestsReferences(first:5)"
            f"{{ nodes {{ number mergedAt mergeCommit {{ oid }} }} }} }}"
            for n in chunk
        )
        query = f'{{ repository(owner:"{owner}",name:"{name}"){{ {fields} }} }}'
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"graphql failed: {result.stderr[:200]}", file=sys.stderr)
            continue
        data = json.loads(result.stdout)["data"]["repository"]
        for value in data.values():
            out[value["number"]] = value["closedByPullRequestsReferences"]["nodes"]
    return out


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


def removed_lines(diff: str) -> list[tuple[str, int]]:
    """``(path, old line number)`` for every line a unified diff removed.

    Only modified files count (a new file has no pre-fix lines to date) and a
    test file is not the defect. Pure insertions contribute nothing here; the
    caller falls back to the insertion point's neighbours for those.
    """
    lines: list[tuple[str, int]] = []
    path: str | None = None
    for line in diff.split("\n"):
        if line.startswith("--- a/"):
            path = line[6:]
        elif line.startswith("--- /dev/null"):
            path = None
        elif line.startswith("@@") and path and not _is_test_path(path):
            header = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if header is None:
                continue
            start = int(header.group(1))
            count = int(header.group(2)) if header.group(2) is not None else 1
            if count == 0:
                lines.append((path, -max(start, 1)))  # insertion anchor, negative
            else:
                lines.extend((path, n) for n in range(start, start + count))
    return lines


def _blame_times(commit: str, path: str, cwd: Path | None) -> dict[int, int]:
    out = _git("blame", "--porcelain", "-w", commit, "--", path, cwd=cwd)
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
    pairs: Sequence[tuple[str, int]],
    commit: str,
    cache: dict[str, dict[int, int]],
    cwd: Path | None,
) -> list[int]:
    """Author times of the given ``(path, line)`` pairs at ``commit``."""
    found = []
    for path, line in pairs:
        if path not in cache:
            cache[path] = _blame_times(commit, path, cwd)
        if line in cache[path]:
            found.append(cache[path][line])
    return found


def latency_days(found: dt.datetime, intro_times: Sequence[int]) -> dict:
    """Latency of one defect from the introduction times of its removed lines.

    The median is the headline: a fix usually touches lines of several ages
    and the median resists both a reformatting commit and one ancient import.
    """
    found_ts = found.timestamp()
    return {
        "n_lines": len(intro_times),
        "median": (found_ts - statistics.median(intro_times)) / _DAY,
        "oldest": (found_ts - min(intro_times)) / _DAY,
        "newest": (found_ts - max(intro_times)) / _DAY,
    }


def fix_latency(
    issues: Sequence[Issue], repo: str, cwd: Path | None = None
) -> list[dict]:
    closed = [issue for issue in issues if issue.closed is not None]
    by_number = {issue.number: issue for issue in closed}
    linkage = closing_prs([issue.number for issue in closed], repo)
    rows = []
    for number, prs in linkage.items():
        merged = [pr for pr in prs if pr.get("mergeCommit")]
        if not merged:
            continue
        oid = merged[0]["mergeCommit"]["oid"]
        if _git("cat-file", "-e", oid, cwd=cwd) is None:
            continue
        diff = _git(
            "diff",
            "-U0",
            "--diff-filter=M",
            f"{oid}^",
            oid,
            "--",
            "faultmaven/",
            cwd=cwd,
        )
        if not diff:
            continue
        targets = removed_lines(diff)
        removed = [(p, n) for p, n in targets if n > 0]
        anchors = [(p, -n) for p, n in targets if n < 0]
        cache: dict[str, dict[int, int]] = {}
        intro = _intro_times(removed, f"{oid}^", cache, cwd)
        method = "removed"
        if len(intro) < 2:
            intro = _intro_times(anchors, f"{oid}^", cache, cwd)
            method = "context"
        if not intro:
            continue
        stats = latency_days(by_number[number].created, intro)
        rows.append(
            {"number": number, "pr": merged[0]["number"], "method": method, **stats}
        )
    return rows


def latency_distribution(rows: Sequence[dict]) -> dict:
    values = sorted(row["median"] for row in rows)
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p25": values[len(values) // 4],
        "median": statistics.median(values),
        "p75": values[(3 * len(values)) // 4],
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


def report(
    issues: Sequence[Issue],
    now: dt.datetime,
    latency_rows: Sequence[dict] | None,
    weeks: int,
) -> str:
    out = []
    flow = weekly_flow(issues)[-weeks:]
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
                    open_count_at(
                        issues,
                        dt.datetime.fromisocalendar(
                            int(row["week"][:4]), int(row["week"][-2:]), 7
                        ).replace(hour=23, minute=59, tzinfo=dt.UTC),
                    ),
                )
                for row in flow
            ),
        )
    )
    total_in = sum(row["residue_in"] for row in flow)
    total_out = sum(row["residue_out"] for row in flow)
    out.append(
        f"\nResidue over the window: in {total_in}, out {total_out}, net {total_in - total_out:+d}.\n"
    )

    surv = survival(issues, now)
    out.append("## Survival\n")
    out.append(
        f"Cohort with ≥30 days exposure: {surv['cohort']}. Closed within "
        + ", ".join(f"{d}d: {p:.0%}" for d, p in surv["closed_within"].items())
        + f". Survived {RESIDUE_THRESHOLD_DAYS}d: {surv['survivors']}, of which closed by 30d: "
        f"{surv['survivors_closed']}, still open: {surv['survivors_open']}.\n"
    )

    snap = residue_snapshot(issues, now)
    out.append("## Open set\n")
    out.append(
        f"Open: {snap['open']}; median age {snap['median_age_days']:.0f}d; "
        f"{snap['older_than_threshold']} older than {RESIDUE_THRESHOLD_DAYS}d, "
        f"{snap['older_than_30d']} older than 30d. By priority label: {snap['by_priority']}.\n"
    )

    fu = follow_ups(issues)
    out.append("## Follow-ups (regex floor)\n")
    out.append(
        f"{fu['attributed']} issues name a parent they were found while working on, "
        f"across {fu['parents']} parents. Most prolific: "
        + ", ".join(f"#{p} ({len(k)})" for p, k in fu["top"][:6])
        + ".\n"
    )

    if latency_rows is not None:
        dist = latency_distribution(latency_rows)
        out.append("## Fix latency (blame on the removed lines of each fix PR)\n")
        if dist["n"]:
            out.append(
                f"{dist['n']} dated fixes. Median latency {dist['median']:.0f}d "
                f"(p25 {dist['p25']:.0f}d, p75 {dist['p75']:.0f}d); {dist['under_7d']:.0%} under a week "
                f"(introduced by recent work), {dist['over_90d']:.0%} over 90 days (old pool).\n"
            )
        else:
            out.append("No fix could be dated.\n")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--issues", type=Path, help="saved `gh issue list --json` output"
    )
    parser.add_argument("--repo", default="FaultMaven/faultmaven")
    parser.add_argument("--weeks", type=int, default=12, help="weeks of flow to print")
    parser.add_argument(
        "--latency",
        action="store_true",
        help="also date every fix via git blame (slow)",
    )
    parser.add_argument(
        "--json", type=Path, help="write every computed row here as well"
    )
    args = parser.parse_args(argv)

    issues = (
        load_issues(json.loads(args.issues.read_text()))
        if args.issues
        else fetch_issues(args.repo)
    )
    now = dt.datetime.now(dt.UTC)
    latency_rows = fix_latency(issues, args.repo) if args.latency else None
    print(report(issues, now, latency_rows, args.weeks))
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "generated": now.isoformat(),
                    "weekly": weekly_flow(issues),
                    "survival": survival(issues, now),
                    "open": residue_snapshot(issues, now),
                    "follow_ups": follow_ups(issues),
                    "latency": latency_rows,
                },
                indent=1,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

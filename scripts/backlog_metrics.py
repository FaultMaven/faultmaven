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
history, never from reading issue text. An open issue younger than the
residue threshold is reported as *pending*, not as residue, so the newest
week's row is comparable to the same row on a later run.
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

#: The checkout this script lives in. Every git call is anchored here so the
#: script measures the same tree from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

from faultmaven.utils.datetime import parse_utc_timestamp  # noqa: E402

_DAY = 86400.0

#: An issue that is still open after this many days has left the fast
#: population: from here on only a deliberate pick closes it.
RESIDUE_THRESHOLD_DAYS = 7

#: The markers a lane writes when it files an issue it found while doing other
#: work. Matching the marker (rather than any ``#N``) keeps a citation of a
#: related issue from being read as a parent, and the negative look-behind
#: refuses ``owner/repo#N`` and ``dashboard#N`` — a cross-repository reference
#: is not a parent in this repository.
_PARENT_MARKER = re.compile(
    r"(?:found while|surfaced (?:while|by|during)|while (?:working on|fixing|"
    r"reviewing)|spun out of|deferred from|follow-?up (?:to|from|of)|"
    r"out of scope (?:for|of)|split (?:out|from)|during (?:the )?(?:work on|"
    r"review of)|review of)[^\n#]{0,80}(?<![\w/])#(\d+)",
    re.IGNORECASE,
)

_ISSUE_FIELDS = "number,title,state,createdAt,closedAt,labels,body"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
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
                title=item.get("title", ""),
                created=parse_utc_timestamp(item["createdAt"]),
                closed=parse_utc_timestamp(closed_at) if closed_at else None,
                labels=tuple(label["name"] for label in item.get("labels", ())),
                body=item.get("body") or "",
            )
        )
    return sorted(issues, key=lambda issue: issue.number)


def fetch_issues(repo: str) -> list[Issue]:
    cmd = [
        "gh",
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
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return load_issues(json.loads(out))


def _week(when: dt.datetime) -> tuple[int, int]:
    iso = when.isocalendar()
    return (iso[0], iso[1])


def _week_end(year: int, week: int) -> dt.datetime:
    return dt.datetime.fromisocalendar(year, week, 7).replace(
        hour=23, minute=59, second=59, tzinfo=dt.UTC
    )


def open_count_at(issues: Sequence[Issue], when: dt.datetime) -> int:
    return sum(
        1
        for issue in issues
        if issue.created <= when and (issue.closed is None or issue.closed > when)
    )


def weekly_flow(issues: Sequence[Issue], now: dt.datetime) -> list[dict]:
    """Per ISO week: opened, closed, and the residue's own inflow and drain.

    ``residue_in`` counts issues opened that week that were NOT closed within
    the threshold. ``residue_out`` counts closures that week of issues older
    than the threshold. ``pending`` is what the newest rows carry instead:
    open issues too young to be residue yet, so the row is provisional by
    exactly that number. The difference ``residue_net`` is what the campaign
    has to drive negative; ``opened - closed`` is dominated by same-lane
    discovery and says little.
    """
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
                "pending": pending[week],
                "open_at_week_end": open_count_at(issues, _week_end(*week)),
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
    burst of recent filings cannot depress the rates. The second number is the
    conditional one that matters for the residue: of the issues that survived
    the threshold, how many closed by the last horizon.
    """
    cohort = [issue for issue in issues if issue.age_days(now) >= min_exposure_days]
    result = {
        "cohort": len(cohort),
        "closed_within": {},
        "survivors": 0,
        "survivors_closed": 0,
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
        "older_than_threshold": sum(1 for a in ages if a > RESIDUE_THRESHOLD_DAYS),
        "older_than_30d": sum(1 for age in ages if age > 30),
        "by_priority": dict(sorted(by_priority.items())),
    }


def follow_ups(issues: Sequence[Issue]) -> dict:
    """Issues that name a parent they were found while working on.

    A parent is counted only when it is an issue in this corpus. A number
    that is not — usually the PR the lane was working, or a cross-repository
    reference — is reported separately as ``unresolved`` rather than as a
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
    for issue in issues:
        match = _PARENT_MARKER.search(issue.body)
        if not match:
            continue
        parent = int(match.group(1))
        if parent in known:
            children[parent].append(issue.number)
        else:
            unresolved += 1
    parents = sorted(children.items(), key=lambda item: (-len(item[1]), item[0]))
    return {
        "attributed": sum(len(kids) for kids in children.values()),
        "parents": len(children),
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


def closing_prs(numbers: Sequence[int], repo: str) -> dict[int, list[dict]]:
    """Which merged PR closed each issue, from GitHub's own linkage.

    GitHub answers a batch with partial data plus an ``errors`` array when
    one number cannot be resolved (deleted, transferred, or a PR number), and
    ``gh`` exits non-zero on that. The body is still parsed, the resolvable
    aliases are kept, and the failed numbers are reported to stderr.
    """
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
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"graphql returned no body: {result.stderr[:200]}", file=sys.stderr)
            continue
        repository = (payload.get("data") or {}).get("repository") or {}
        for alias, value in repository.items():
            if value is None:
                print(f"unresolvable issue {alias[1:]}", file=sys.stderr)
                continue
            out[value["number"]] = value["closedByPullRequestsReferences"]["nodes"]
    return out


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


def removed_lines(diff: str) -> list[tuple[str, int]]:
    """``(path, old line number)`` for every line a unified diff removed.

    The diff must carry the ``a/``/``b/`` prefixes (the caller forces them,
    because ``diff.noprefix`` would otherwise drop every hunk silently). A
    new file has no pre-fix lines to date and a test file is not the defect.
    A pure insertion is recorded as a NEGATIVE anchor at the insertion point;
    the caller uses the anchors only when nothing was removed.
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


def fix_latency(issues: Sequence[Issue], repo: str) -> list[dict]:
    """One row per FIX PR, dated against the earliest issue it closed.

    Per PR rather than per issue: a sweep PR closing six issues would
    otherwise contribute six identical medians and weight the distribution by
    PR size. A PR whose removed lines were all authored AFTER the issue was
    filed (a peer PR landed in between) is dropped as undatable rather than
    reported as a fast regression.
    """
    closed = [issue for issue in issues if issue.closed is not None]
    by_number = {issue.number: issue for issue in closed}
    linkage = closing_prs([issue.number for issue in closed], repo)
    per_pr: dict[int, dict] = {}
    for number, prs in linkage.items():
        merged = [pr for pr in prs if pr.get("mergeCommit")]
        if not merged:
            continue
        entry = per_pr.setdefault(
            merged[0]["number"],
            {"oid": merged[0]["mergeCommit"]["oid"], "issues": []},
        )
        entry["issues"].append(number)

    rows = []
    for pr_number, entry in sorted(per_pr.items()):
        oid = entry["oid"]
        if _git("cat-file", "-e", oid) is None:
            continue
        diff = _git(
            "diff",
            "-U0",
            "--diff-filter=MD",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            f"{oid}^",
            oid,
            "--",
            "faultmaven/",
        )
        if not diff:
            continue
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
            continue
        found = min(by_number[n].created for n in entry["issues"])
        stats = latency_days(found, intro)
        if stats["oldest"] < 0:
            continue
        rows.append(
            {
                "pr": pr_number,
                "issues": sorted(entry["issues"]),
                "method": method,
                **stats,
            }
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


def compute(issues: Sequence[Issue], now: dt.datetime, latency: bool, repo: str):
    return {
        "generated": now.isoformat(),
        "weekly": weekly_flow(issues, now),
        "survival": survival(issues, now),
        "open": residue_snapshot(issues, now),
        "follow_ups": follow_ups(issues),
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
    pending = sum(row["pending"] for row in flow)
    out.append(
        f"\nResidue over the window: in {total_in}, out {total_out}, "
        f"net {total_in - total_out:+d}; {pending} open issues are still too "
        f"young to count.\n"
    )

    surv = results["survival"]
    out.append("## Survival\n")
    if surv["cohort"]:
        out.append(
            f"Cohort with ≥30 days exposure: {surv['cohort']}. Closed within "
            + ", ".join(f"{d}d: {p:.0%}" for d, p in surv["closed_within"].items())
            + f". Survived {RESIDUE_THRESHOLD_DAYS}d: {surv['survivors']}, of which "
            f"closed by 30d: {surv['survivors_closed']}, still open: "
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
        f"{fu['attributed']} issues name a parent issue they were found while "
        f"working on, across {fu['parents']} parents; {fu['unresolved']} more name "
        "a number that is not an issue here (a PR, or another repository). "
        "Most prolific: "
        + ", ".join(f"#{p} ({len(k)})" for p, k in fu["top"][:6])
        + ".\n"
    )

    if results["latency"] is not None:
        dist = latency_distribution(results["latency"])
        out.append("## Fix latency (blame on the removed lines of each fix PR)\n")
        if dist["n"]:
            out.append(
                f"{dist['n']} dated fix PRs. Median latency {dist['median']:.0f}d "
                f"(p25 {dist['p25']:.0f}d, p75 {dist['p75']:.0f}d); "
                f"{dist['under_7d']:.0%} under a week (introduced by recent work), "
                f"{dist['over_90d']:.0%} over 90 days (old pool).\n"
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
    parser.add_argument(
        "--weeks", type=int, default=12, help="most recent weeks of flow to print"
    )
    parser.add_argument(
        "--latency",
        action="store_true",
        help="also date every fix via git blame (slow)",
    )
    parser.add_argument("--json", type=Path, help="write every computed row here")
    args = parser.parse_args(argv)

    if args.issues:
        issues = load_issues(json.loads(args.issues.read_text()))
    else:
        issues = fetch_issues(args.repo)
    now = dt.datetime.now(dt.UTC)
    results = compute(issues, now, args.latency, args.repo)
    print(report(results, args.weeks))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

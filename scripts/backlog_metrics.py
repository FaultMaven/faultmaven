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

It also reports the **rule-4 tier** — the ready items holding none of that
document's picking rules 1-3, which the reserved slot in every round draws
from. That figure is the third of the document's three "this is wrong rather
than the work" signals and, until #1511, nothing computed it: nine rounds
reported a label proxy the issue itself calls plainly wrong. Rule 1 is
computed here; rules 2 and 3 are not (see :func:`rule4_tier` for why rule 3
is reported rather than applied), so the tier is an UPPER BOUND and says so
every time.

Usage::

    python scripts/backlog_metrics.py                  # fetch via gh, print
    python scripts/backlog_metrics.py --issues i.json --as-of 2026-09-15T20:00:00Z
    python scripts/backlog_metrics.py --latency        # also blame fix PRs

``--latency`` runs ``git blame`` over every fix PR's diff and is slow (about
half a minute per hundred fix PRs); the rest completes in seconds. The
flow, survival and open-set numbers come from GitHub metadata and never from
reading issue text. Three sections do read bodies, each through one stated
grammar: the follow-up count reads the lane marker ("found while working on
#N"), the rule-4 tier reads the blocked pile's ``**Blocked on:**`` statement
(an issue, a ruling or a condition) and the repository files an issue cites
outside a code block, and the question list reads the ready pile for
decision, gating and trigger language — a list to read, never a label move
(#1639). Each body is parsed once per run, into a :class:`BodyFacts` every
consumer shares; the open issues' comments are read for one thing only, a
ruling heading. An issue labelled ``tracking`` is in no pile.
An open issue younger than the residue threshold is reported as *pending*,
not as residue, so the newest week's row is comparable to the same row on a
later run. A saved dump is replayed with ``--as-of <the time it was taken>``; ages
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
from dataclasses import asdict, dataclass, replace
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

#: The phrases a lane writes when it files an issue it found while doing
#: other work. Matching a marker (rather than any ``#N``) keeps a citation of
#: a related issue from being read as a parent.
_MARKER_PHRASE = (
    r"(?:found while|surfaced (?:while|by|during)|while (?:working on|fixing|"
    r"reviewing)|spun out of|deferred from|follow-?up (?:to|from|of)|"
    r"out of scope (?:for|of)|split (?:out|from)|during (?:the )?work on|"
    r"review of)"
)

#: A marker and the reference it governs. Two properties are measured
#: against this repository's own corpus rather than guessed:
#:
#: * The gap may not cross a sentence boundary (no ``.;:!?``). "Found while
#:   reviewing the ladder; unrelated background is in #1200" cites, it does
#:   not parent. No real marker in the corpus spans such a break — the gaps
#:   that occur end in a letter, ``(`` or a comma.
#: * A qualifier binds only when ATTACHED (``repo#N``). Every attached token
#:   in the corpus is a repository (``faultmaven``, ``faultmaven-dashboard``,
#:   ``infra``, ``FaultMaven/faultmaven-dashboard``) and every SPACED one is
#:   an ordinary word (``PR``, ``of``, ``fixing``, ``the``), so requiring
#:   attachment is what separates "another repository" from prose. This is
#:   also GitHub's own cross-repository syntax.
_PARENT_MARKER = re.compile(
    _MARKER_PHRASE + r"[^\n#.;:!?]{0,80}?(?P<qual>[\w./-]*)#(?P<n>\d+)",
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
    #: The comment bodies, oldest first, or ``None`` when they were not read
    #: (a dump taken without them). Only the ruling-heading check reads them,
    #: and ``None`` is told apart from "no comments" so the report can say
    #: that check ran on the body alone.
    comments: tuple[str, ...] | None = None

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
        comments = item.get("comments")
        issues.append(
            Issue(
                number=int(item["number"]),
                created=parse_utc_timestamp(item["createdAt"]),
                closed=parse_utc_timestamp(closed_at) if closed_at else None,
                labels=tuple(label["name"] for label in item.get("labels", ())),
                body=item.get("body") or "",
                comments=(
                    None
                    if comments is None
                    else tuple(
                        (c.get("body") or "") if isinstance(c, dict) else str(c)
                        for c in comments
                    )
                ),
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
    raw = json.loads(result.stdout)
    # Comments for the OPEN issues only, in a second call: the ruling-heading
    # check is the one reader of them, and it reads the ready pile. Fetching
    # them for every closed issue as well would multiply the payload for
    # nothing. A failure here leaves them unread (``None``), which the report
    # names, rather than failing the run.
    comments = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "number,comments",
    )
    if comments.returncode == 0:
        by_number = {
            item["number"]: item.get("comments") or []
            for item in json.loads(comments.stdout)
        }
        for item in raw:
            if item["number"] in by_number:
                item["comments"] = by_number[item["number"]]
    else:
        print(
            "note: open issues' comments could not be read; a ruling recorded "
            "only in a comment is not seen this run",
            file=sys.stderr,
        )
    return load_issues(raw)


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
            issue = replace(issue, closed=None)
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


#: Shorthands this repository is written about in its own issues, beside its
#: real names. ``fm#918`` is the campaign's own spelling and appears in live
#: parent markers; without it those resolve as "another repository" and the
#: follow-up they record is dropped.
_OWN_REPO_ALIASES = frozenset({"fm"})


def is_own_repo(qualifier: str | None, repo: str) -> bool:
    """Whether a reference qualifier (``fm#N``, ``owner/name#N``) names here.

    An EMPTY qualifier is here by definition — that is what a bare ``#N``
    means on GitHub. One place, because two grammars read references in this
    file and a disagreement between them is invisible.
    """
    token = (qualifier or "").strip("/.").lower()
    if not token:
        return True
    return token in {repo.lower(), repo.split("/")[-1].lower()} | _OWN_REPO_ALIASES


def parent_of(issue: Issue, repo: str) -> int | None:
    """The number a lane marker in ``issue`` names, or ``None``.

    A reference attached to this repository's own name (``faultmaven#N`` or
    ``FaultMaven/faultmaven#N``) is that number; one attached to any other
    (``faultmaven-dashboard#N``, ``infra#N``) is a reference elsewhere. An
    issue never parents itself — a correction note citing the review of its
    own fix PR would otherwise resolve back to it.

    EVERY marker in the body is considered, and the first that resolves to
    an issue here wins: a body whose first marker names another repository
    used to suppress a real marker later in the same body, which made the
    answer depend on the order the two were written in.

    A marker inside a code block is an illustration, exactly as a cited path
    is: an issue quoting another's body would otherwise be attributed as its
    follow-up and inflate the parent count the campaign is judged by. The
    two reference grammars in this file read the same stripped text (#1580).

    This is the ONE place the marker grammar lives; every reader calls here.
    """
    return _parent_in_text(without_code_blocks(issue.body), issue.number, repo)


def _parent_in_text(text: str, number: int, repo: str) -> int | None:
    """:func:`parent_of` over a body whose code blocks are already blanked."""
    for match in _PARENT_MARKER.finditer(text):
        if not is_own_repo(match.group("qual"), repo):
            continue  # another repository
        found = int(match.group("n"))
        if found != number:
            return found
    return None


def marker_numbers(
    issues: Sequence[Issue], repo: str, facts: dict[int, BodyFacts] | None = None
) -> list[int]:
    """Every number a parent marker names that is not an issue here."""
    facts = facts if facts is not None else read_bodies(issues, repo)
    known = {issue.number for issue in issues}
    return sorted({facts[issue.number].parent for issue in issues} - known - {None})


def follow_ups(
    issues: Sequence[Issue],
    repo: str,
    pr_links: dict[int, list[int]] | None = None,
    facts: dict[int, BodyFacts] | None = None,
) -> dict:
    """Issues that name a parent they were found while working on.

    A parent is counted only when it is an issue in this corpus. A number
    that is not is usually the PR the lane was working; ``pr_links`` (PR
    number → the issues it closed, from :func:`pr_closing_issues`) resolves
    those to the issue the PR was for. A PR that closed SEVERAL issues
    attributes to the lowest-numbered one: the marker does not say which of
    them the lane was on, and attributing to all would count one follow-up
    several times. What still resolves to nothing — a PR that closed no
    issue here, or one that closed this very issue — is reported as
    ``unresolved`` rather than as a parent, because the per-lane rate is a
    rate per ISSUE.

    The regex reads the lane's own marker, so an issue whose parent is named
    without one is missed; treat the count as a floor. Whether a follow-up
    was CAUSED by the parent's fix (a regression) or merely found during it
    cannot be read from metadata and is classified by hand in the campaign
    report.
    """
    facts = facts if facts is not None else read_bodies(issues, repo)
    known = {issue.number for issue in issues}
    children: dict[int, list[int]] = defaultdict(list)
    unresolved = 0
    via_pr = 0
    for issue in issues:
        parent = facts[issue.number].parent
        if parent is None:
            continue
        resolved_via_pr = False
        if parent not in known and pr_links:
            closed_here = [n for n in pr_links.get(parent, ()) if n in known]
            if closed_here:
                parent = min(closed_here)
                resolved_via_pr = True
        if parent in known and parent != issue.number:
            children[parent].append(issue.number)
            via_pr += resolved_via_pr
        else:
            # A PR that closed nothing here, or that closed this very issue.
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
# The rule-4 tier: ready items holding none of picking rules 1-3 (#1511)
# --------------------------------------------------------------------------

#: The pile labels ``docs/development/issue-processing.md`` sorts by. A pile
#: is a label on the issue, so each is a query and never a list.
_PILE_PREFIX = "pile:"
READY_LABEL = "pile:ready"
BLOCKED_LABEL = "pile:blocked"
YOURS_LABEL = "pile:yours"

#: An issue carrying this label is in NO pile, whatever ``pile:`` labels it
#: also carries: the ``Queue`` board, a campaign tracker, a document refined
#: each round. None of them is work a lane can be dispatched against, so in
#: ready one would be counted in the rule-4 tier and named as the reserved
#: slot's buy; and a tracker carrying no pile label must not read as an
#: unsorted arrival either, or step 2 sorts it straight back into ready
#: (#1639, leak C — #819 carried ``tracking`` and ``pile:ready`` both).
TRACKING_LABEL = "tracking"

#: Picking rule 1: "it unblocks two or more other items".
RULE_1_DEPENDENTS = 2

#: Picking rule 3: "a seam that produced three or more issues in the last
#: month". REPORTED, never applied — see :func:`rule4_tier`.
SEAM_WINDOW_DAYS = 30
SEAM_ISSUES = 3

#: A code fence, opening or closing: three or more backticks or tildes.
#: Indent is permissive rather than CommonMark's three spaces, because an
#: indented fence inside a list is still a fence and stripping it is the
#: safe direction. The CLOSING fence must repeat the opening character at
#: least as many times with nothing after it, which is GitHub's rule and
#: what makes ```` ```` ```` inside a ``` block content rather than an end.
_FENCE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

#: A line of an INDENTED code block: four spaces or a tab. GitHub renders
#: such a run as code, so a path in one is an illustration exactly as a
#: fenced one is.
_INDENTED_CODE = re.compile(r"^(?: {4}|\t)")

#: A list marker. An indented run under one is the list item's own content,
#: not a code block — CommonMark's rule, and the reason this is checked at
#: all: blanking a wrapped bullet would delete a real ``**Blocked on:**``
#: statement, which is the one direction this guard must not fail in.
_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")

#: The ``**Blocked on:**`` statement every ``pile:blocked`` item carries in
#: its body. This matches only the LABEL; what follows it is resolved by
#: :func:`blocked_on`, because the three answers it has to separate —
#: an issue, a ruling, and a spelling this cannot read — are not separable
#: by one pattern. Anchored at a line start past any blockquote marker,
#: bullet and emphasis, and requiring the colon, so the "Not blocked on
#: another issue." both live statements end with cannot match however the
#: body wraps.
_BLOCKED_ON_LABEL = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:[-*+][ \t]+)?\*{0,2}blocked on\*{0,2}[ \t]*:[ \t]*\*{0,2}",
    re.IGNORECASE | re.MULTILINE,
)

#: ``[#1294](https://…/issues/1294)`` → ``#1294``. Applied before the payload
#: is cut at a sentence boundary, or the URL's own ``:`` and ``.`` would cut
#: the reference off the statement that carries it.
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)")

#: An issue reference, in every spelling this repository writes: ``#1294``,
#: ``issue #1294`` (the word is separated, so the qualifier is empty),
#: ``fm#1294``, ``FaultMaven/faultmaven#1294``.
_REFERENCE = r"(?<![\w/-])(?P<qual%s>[\w][\w.-]*(?:/[\w.-]+)?)?#(?P<n%s>\d+)\b"
_ANY_REFERENCE = re.compile(_REFERENCE % ("", ""))

#: A noun a reference is introduced by, which carries no meaning here: what
#: #1543 IS decides whether it resolves, not what the sentence calls it.
_REFERENCE_NOUN = r"(?:(?:issues?|prs?|pull requests?)[ \t]+)?"

#: The reference, or list of them, that OPENS the statement. Leading is what
#: separates "blocked on #1294" from "blocked on an owner ruling on #1294's
#: shape": in the second the issue is a modifier, and reading it as a
#: dependency would attribute owner latency to an issue instead. A reference
#: anywhere else in the statement makes it UNREADABLE rather than either —
#: a guard that misses can be fixed, a guard that answers the wrong bucket
#: lies, and this pile's whole point is telling those two apart.
_LEADING_REFERENCES = re.compile(
    r"^"
    + _REFERENCE_NOUN
    + (_REFERENCE % ("0", "0"))
    + r"(?:[ \t]*(?:,|and|,[ \t]*and)[ \t]*"
    + _REFERENCE_NOUN
    + (_REFERENCE % ("", ""))
    + r")*",
    re.IGNORECASE,
)

#: How far past the label a reference still belongs to the statement. The
#: first sentence boundary, as in :data:`_PARENT_MARKER`: "an owner ruling;
#: see #1294 for background" is owner latency and the #1294 is context.
_SENTENCE_END = re.compile(r"[.;:!?]")

#: The top-level directories a cited path may start with, when the tree
#: cannot be read (a saved dump replayed outside a checkout). A FALLBACK,
#: pinned by a test against the tracked tree rather than maintained by hand:
#: it went four directories stale — ``demo``, ``requirements``, ``resources``
#: and ``.githooks`` — and nothing failed, because an unlisted directory
#: simply contributes no citation (#1576).
_FALLBACK_TOP_LEVEL = (
    ".claude",
    ".githooks",
    ".github",
    "alembic",
    "demo",
    "docs",
    "faultmaven",
    "requirements",
    "resources",
    "scripts",
    "tests",
)


def tracked_top_level(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """The repository's own top-level directories, from the tracked tree.

    ``git ls-tree`` rather than ``iterdir`` on purpose: the working copy
    also holds ``.venv``, ``data``, ``htmlcov`` and the caches, and admitting
    those would make a pasted traceback out of ``site-packages`` read as a
    citation of this repository. Anything that is not a checkout — a saved
    dump replayed elsewhere, a tarball, no ``git`` on PATH — falls back to
    :data:`_FALLBACK_TOP_LEVEL`.
    """
    try:
        result = subprocess.run(
            ["git", "ls-tree", "--name-only", "-d", "HEAD"],
            capture_output=True,
            text=True,
            cwd=root,
        )
    except (OSError, ValueError):
        return _FALLBACK_TOP_LEVEL
    names = (
        tuple(n for n in result.stdout.split("\n") if n)
        if not result.returncode
        else ()
    )
    return names or _FALLBACK_TOP_LEVEL


def _cited_path_pattern(top_level: Sequence[str]) -> re.Pattern:
    """A repository path cited in an issue body, as a stand-in for a seam.

    The top-level names are this repository's own, which is what makes the
    match a path rather than any token with a slash in it, and the required
    extension is what makes it a FILE: ``alembic/versions`` is a region, and
    a region is not a seam.
    """
    names = "|".join(
        re.escape(name) for name in sorted(top_level, key=len, reverse=True)
    )
    return re.compile(r"(?<![\w/.-])((?:" + names + r")/[\w./-]*\.[A-Za-z]\w*)")


_CITED_PATH = _cited_path_pattern(tracked_top_level())


def without_code_blocks(body: str) -> str:
    """``body`` with every code block blanked, line for line.

    Four shapes, because the guard's purpose is to keep an illustration from
    reading as a statement and only one of them used to be covered (#1580):
    a fence may be **unclosed** (the commonest shape for a pasted log, and
    GitHub renders it as code to the end of the body), it may use **four or
    more** backticks, a block may be **indented** rather than fenced, and any
    of them may arrive with **CRLF** line endings — which the corpus does not
    carry today only because every issue so far was filed through ``gh``, and
    which GitHub returns for any body edited in the web UI.

    Line count is preserved, so nothing that reads the result has to know a
    block was there.
    """
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    fence: tuple[str, int] | None = None
    indented = False
    blank_before = True  # the start of the body opens a block like a blank line
    in_list = False
    for line in lines:
        if fence is not None:
            out.append("")
            match = _FENCE.match(line)
            if (
                match
                and match.group("fence")[0] == fence[0]
                and len(match.group("fence")) >= fence[1]
                and not match.group("info").strip()
            ):
                fence = None
            continue
        # A continuation of an indented block is checked BEFORE the fence, or
        # a fence pasted inside one would open a state that runs to the end.
        if indented and _INDENTED_CODE.match(line):
            out.append("")
            continue
        match = _FENCE.match(line)
        if match:
            fence = (match.group("fence")[0], len(match.group("fence")))
            out.append("")
            indented, blank_before = False, False
            continue
        if not line.strip():
            out.append(line)
            indented, blank_before = False, True
            continue
        if blank_before and not in_list and _INDENTED_CODE.match(line):
            out.append("")
            indented, blank_before = True, False
            continue
        # A content line. Whether a list is open decides what an indented
        # run after the next blank line means, and a list stays open across
        # its own wrapped lines and further paragraphs: only a fresh block
        # at column 0 that is not itself a list item closes it. Resetting
        # per line instead read a bullet's SECOND line as the end of the
        # list, so a wrapped bullet's indented statement was blanked.
        out.append(line)
        if _LIST_MARKER.match(line):
            in_list = True
        elif blank_before and not line[:1].isspace():
            in_list = False
        indented, blank_before = False, False
    return "\n".join(out)


def pile_of(issue: Issue) -> str | None:
    """The one pile an issue is in, by the procedure's own reading rule.

    "More than one ``pile:`` label means **blocked**, or **yours** if blocked
    is not among them." A half-finished move carries both labels, and reading
    it as ready would put it in the tier — where it would be named as the
    reserved slot's next buy and then refused by the dispatch predicate, so
    the slot drains nothing that round.

    The precondition is the COUNT, not which labels are present: any second
    pile label makes the move half-finished, whether or not this file knows
    the label's name. ``multi_labelled`` in the report keys on the same
    count, so the two cannot name the same item as the slot's buy and as
    undispatchable (#1581).

    An issue labelled ``tracking`` is in no pile at all (:data:`TRACKING_LABEL`)
    and answers ``None`` whatever else it carries.

    Which pile it lands in is the rule's own second clause, read in that
    order — blocked when blocked is among them, **yours otherwise**.
    Defaulting to blocked instead put an item carrying no blocked label
    into the blocked pile, where a body with no ``**Blocked on:**`` line
    reported it as *stating nothing* as well as as a half-finished move:
    two repairs for one item, one of them for a statement it was never
    asked to write.
    """
    if TRACKING_LABEL in issue.labels:
        # In no pile, and not an unsorted arrival: a tracker (#1639). Any
        # pile label it also carries is reported by the tier as a leftover.
        return None
    piles = {label for label in issue.labels if label.startswith(_PILE_PREFIX)}
    if not piles:
        return None
    if len(piles) == 1:
        return piles.pop()
    # MORE THAN ONE, which is the rule's own precondition. Reading the set
    # label by label instead put `pile:ready` plus any FOURTH pile label back
    # in the tier, where the reserved slot names it and step 4's dispatch
    # predicate — "any it returns that carries a second `pile:` label" —
    # then refuses it, so the slot drains nothing (#1581).
    return BLOCKED_LABEL if BLOCKED_LABEL in piles else YOURS_LABEL


@dataclass(frozen=True)
class BlockingGraph:
    """What the open ``pile:blocked`` items say they are waiting on."""

    #: blocker → the open blocked issues waiting on it, ascending.
    waiting_on: dict[int, list[int]]
    #: blocked items whose statement names a ruling rather than any issue.
    on_ruling: list[int]
    #: ``(blocked item, the text)`` where the statement carries something
    #: reference-shaped this could not resolve to an issue here — another
    #: repository, a pull request number, a number that is not an issue.
    #: Reported rather than filed as owner latency: a guard that misses is
    #: recoverable, a guard that answers the wrong bucket lies.
    unresolved: list[tuple[int, str]]
    #: blocked items that state nothing: no ``**Blocked on:**`` label at
    #: all, or one with only a placeholder after it. Both are the same
    #: repair — write the question — and step 2's blocked-pile check reads
    #: this bucket to ask for it.
    unstated: list[int]
    #: ``(blocked item, the issue it waits on)`` where that issue has closed —
    #: the condition has been met and nothing has moved the item.
    condition_met: list[tuple[int, int]]
    #: ``(blocked item, the condition)`` for a deferral on something to be
    #: OBSERVED rather than on an owner or an issue (#1639, leak B). Not
    #: owner latency: *Needs your call* lists these as answered-and-waiting,
    #: and step 2 checks each condition as it sorts.
    on_condition: list[tuple[int, str]]
    #: The subset of ``on_condition`` whose statement says it cannot be
    #: observed today. Nothing checks those, so they are surfaced by name.
    unobservable: list[int]


#: A payload that says nothing. An agent that writes the required label and
#: forgets the question used to fall through to *owner latency* and appear
#: under "Needs your call" with no question to answer, while ``unstated`` —
#: the bucket that exists for stating nothing, and the one step 2's
#: blocked-pile check keys on — stayed empty, so the gap was never repaired
#: (#1580). Emphasis and backticks are stripped before the comparison.
_UNSTATED_PAYLOADS = frozenset(
    {
        "",
        "-",
        "--",
        "–",
        "—",
        "?",
        "??",
        "tbd",
        "tba",
        "todo",
        "n/a",
        "na",
        "unknown",
        "xxx",
    }
)

#: How much of an unreadable statement the report prints. Long enough to
#: recognise the spelling, short enough not to paste a paragraph onto the
#: board.
_UNREADABLE_CLIP = 120


#: The third form a statement can take (#1639, leak B): a deferral on a
#: CONDITION — ``**Blocked on:** condition — <what would be observed>``. It
#: must OPEN the statement — nothing before it, not an article, not a
#: qualifier — so "an owner ruling on the condition for …" stays a ruling.
#: The first cut admitted one word before it, and that read issue
#: dependencies as conditions: "the condition in #1116 must hold first",
#: "no condition — #1116", "Precondition: #12 lands". In the condition
#: bucket such an item has no edge, so when #1116 closes nothing moves it —
#: a state with no exit, made by the fix for one.
#: ``precondition`` is the same word for this purpose and is admitted, so
#: "Precondition: #12 lands" is caught as a condition naming an issue (and
#: reported) rather than cut at its colon and filed as a ruling.
_CONDITION_LEAD = re.compile(r"^(?:pre)?condition\b", re.IGNORECASE)

#: What may sit between the word and the condition itself: the separator,
#: and the "not a ruling" the first live statements carried.
_CONDITION_SEPARATOR = re.compile(
    r"^[\s,]*(?:not[ \t]+a[ \t]+ruling)?[\s,]*[—–:\-]*[\s]*", re.IGNORECASE
)

#: A condition stated as not observable today. The procedure asks for this
#: spelling rather than silence, because a deferral nothing can check has
#: no exit but the owner, and saying so is what puts it in front of them.
_UNOBSERVABLE = re.compile(r"^unobservable\b", re.IGNORECASE)


def _statement_payload(text: str) -> str | None:
    """The last ``**Blocked on:**`` statement's payload, or ``None``.

    To the end of its paragraph, so a reference on the line BELOW the label
    still belongs to it, with markdown links reduced to their text.
    """
    matches = list(_BLOCKED_ON_LABEL.finditer(text))
    if not matches:
        return None
    payload = text[matches[-1].end() :].split("\n\n")[0]
    return _MD_LINK.sub(r"\1", payload).strip()


def _condition_form(payload: str) -> str | None:
    """What follows ``condition`` and its separator, whitespace collapsed,
    or ``None`` when the payload does not open with the word."""
    lead = _CONDITION_LEAD.match(payload)
    if lead is None:
        return None
    rest = payload[lead.end() :]
    rest = rest[_CONDITION_SEPARATOR.match(rest).end() :]
    return " ".join(rest.split())


def _condition_of(payload: str) -> str | None:
    """The condition a payload defers on, ``""`` if it names none, or
    ``None`` when it is not a readable condition.

    A condition naming an issue (``#N`` anywhere in it) is NOT one: waiting
    on an issue is the ``#N`` form, which gives the issue an edge and moves
    the item when it closes. :func:`_blocked_on_text` reports it as
    unreadable instead, for rewording.
    """
    rest = _condition_form(payload)
    if rest is None or _ANY_REFERENCE.search(rest):
        return None
    return "" if _unstated(rest) else rest


def blocked_on_condition(body: str) -> str | None:
    """The condition a ``**Blocked on:**`` statement defers on, if it is one."""
    payload = _statement_payload(without_code_blocks(body))
    return None if payload is None else _condition_of(payload)


def blocked_on(body: str, repo: str) -> tuple[bool, list[int], str]:
    """``(stated, the issues it names, the part that could not be read)``.

    The LAST statement wins: a body is edited in place rather than appended
    to, so a second one is a leftover. A reference resolves here only if it
    opens the statement and its qualifier names this repository
    (:func:`is_own_repo`); whether the NUMBER is an issue is the caller's
    question, because only the caller has the corpus. Anything else
    reference-shaped comes back verbatim, so the report can print the
    spelling it did not read instead of filing it as owner latency.

    ``stated`` is "a statement was made", not "the label is present": a
    label with nothing but a placeholder after it answers ``False`` and
    lands in ``unstated``, which is the bucket that gets repaired.

    A statement in the CONDITION form (:func:`blocked_on_condition`) is
    stated, names no issue and has nothing unreadable. One that writes the
    word and no condition states nothing, and one whose condition names an
    issue is reported as unreadable: waiting on an issue is the ``#N`` form.
    """
    return _blocked_on_text(without_code_blocks(body), repo)


def _unstated(payload: str) -> bool:
    """Whether a statement's payload says nothing at all.

    The WHOLE payload, never the part before the first sentence boundary:
    ``:`` and ``;`` are boundaries, so ``TBD: the owner's call on #1294``
    clipped to ``TBD`` and a written question — reference and all — was
    filed as *stating nothing*, whose repair is to write the question that
    is already there. Sentence punctuation is stripped from the ENDS so
    ``TBD.`` still answers yes.
    """
    return payload.strip(" \t*_`~.;:!?").lower() in _UNSTATED_PAYLOADS


def _blocked_on_text(text: str, repo: str) -> tuple[bool, list[int], str]:
    """:func:`blocked_on` over a body whose code blocks are already blanked."""
    payload = _statement_payload(text)
    if payload is None or _unstated(payload):
        return False, [], ""
    form = _condition_form(payload)
    if form is not None:
        if _ANY_REFERENCE.search(form):
            # A condition naming an issue: neither a condition nor an edge.
            return True, [], _clip(form)
        condition = _condition_of(payload)
        return bool(condition), [], ""
    # Then to the first sentence boundary.
    cut = _SENTENCE_END.search(payload)
    head = payload[: cut.start()] if cut else payload
    leading = _LEADING_REFERENCES.match(head)
    if leading is None:
        # No reference at the front. A reference further in is a modifier of
        # a ruling, or a spelling this cannot read; either way, not an edge.
        return True, [], _clip(head) if _ANY_REFERENCE.search(head) else ""
    named, unreadable = [], []
    for reference in _ANY_REFERENCE.finditer(leading.group(0)):
        if is_own_repo(reference.group("qual"), repo):
            named.append(int(reference.group("n")))
        else:
            unreadable.append(reference.group(0))
    # Past the leading run the reference is a modifier rather than an edge,
    # and saying so is the point: "#1294 (see also #1300)" used to report
    # #1300 in no bucket at all, which is the blocked-state-with-no-detector
    # the unreadable bucket exists for (#1580).
    unreadable += [m.group(0) for m in _ANY_REFERENCE.finditer(head[leading.end() :])]
    return True, named, " ".join(unreadable)


def _clip(text: str) -> str:
    """``text`` bounded for the report, saying so when it was cut."""
    return text if len(text) <= _UNREADABLE_CLIP else text[:_UNREADABLE_CLIP] + "…"


@dataclass(frozen=True)
class BodyFacts:
    """Everything the report reads out of ONE issue body, parsed once.

    Four consumers used to scan each body for themselves — the follow-up
    graph twice, the hot seams, and the blocking graph — so every body was
    stripped of its code blocks and re-matched four times a run, and two
    readers of the same body could drift apart without anything saying so
    (#1577). Now :func:`read_bodies` produces this record once and every
    consumer takes it.
    """

    #: The issue a lane marker names, resolved to this repository, or ``None``.
    parent: int | None
    #: Every repository FILE the body names outside a code block.
    cited: frozenset[str]
    #: Whether a ``**Blocked on:**`` statement was made at all.
    stated: bool
    #: The issue numbers that statement names, in order.
    named: tuple[int, ...]
    #: The reference-shaped part of it this could not resolve, verbatim.
    unreadable: str
    #: The condition a statement in the condition form defers on, else
    #: ``None`` (an empty condition is ``stated=False``, not a condition).
    condition: str | None = None
    #: The decision, gating and trigger phrases the body carries, in order
    #: of first appearance — what :func:`ready_questions` lists.
    question: tuple[str, ...] = ()
    #: Whether a ruling is recorded where the body can show it: a
    #: ``**Ruled …**`` line or a ruling heading.
    ruled: bool = False


def read_bodies(issues: Sequence[Issue], repo: str) -> dict[int, BodyFacts]:
    """One :class:`BodyFacts` per issue, code blocks stripped once each."""
    facts = {}
    for issue in issues:
        text = without_code_blocks(issue.body)
        stated, named, unreadable = _blocked_on_text(text, repo)
        payload = _statement_payload(text) if stated else None
        facts[issue.number] = BodyFacts(
            parent=_parent_in_text(text, issue.number, repo),
            cited=frozenset(_CITED_PATH.findall(text)),
            stated=stated,
            named=tuple(named),
            unreadable=unreadable,
            condition=_condition_of(payload) if payload is not None else None,
            question=_question_phrases(text),
            ruled=_ruling_recorded(text),
        )
    return facts


# --------------------------------------------------------------------------
# Ready items that read like a question (#1639, leak A)
# --------------------------------------------------------------------------

#: Decision, gating and trigger language: what an item that needs a ruling,
#: waits on another issue or defers on a condition says about itself. A
#: HEURISTIC, and the output is a list for step 2 to READ — never a label
#: move. Measured on the live ready pile when this was written: round 15's
#: cruder first cut flagged 14 of 81 and 5 were real, and this one lists 13
#: of 79 after that round had already moved the real ones out. Bare
#: "decide" / "deferred" are left out on purpose: they are prose about the
#: code ("the router decides", "the write is deferred") far more often than
#: about the issue, and doubled the list.
_QUESTION_LANGUAGE = re.compile(
    r"""(?:
      \bdecide\s+(?:which|whether|between|how)\b
    | \bdecision\s+(?:needed|required|pending)\b
    | \bneeds?\s+(?:a\s+|an\s+owner\s+|the\s+owner'?s?\s+)?(?:ruling|decision)\b
    | \bowner(?:'s)?\s+(?:ruling|call|decision)\b
    | \bdesign\s+call\b
    | \bopen\s+question\b
    | \bjudge?ment\s+call\b
    | \bwhich\s+of\s+(?:the|these)\s+(?:two|three|four|options)\b
    | \bgated\s+on\b
    | (?<!not\s)\bblocked\s+(?:on|by)\b
    | \bdepends\s+on\s+\#\d+
    | \bwait(?:s|ing)\s+(?:on|for)\s+\#\d+
    | \buntil\s+\#\d+\s+(?:lands|merges|closes|is\s+(?:fixed|merged|closed))\b
    | \bonce\s+\#\d+\s+(?:lands|merges|closes|is)\b
    | \btrigger\s+to\s+revisit\b
    | \brevisit\s+(?:when|if|once)\b
    | \bdeferred\s+(?:until|pending|on)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: How many distinct phrases the report prints per item: enough to show
#: why it was listed, not a transcript.
_QUESTION_PHRASES_SHOWN = 3

#: A ruling recorded in a BODY line: ``**Ruled 2026-09-24.** …``, the line
#: step 3 writes when it moves an item. ``Ruled`` only — a bold ``**Ruling
#: needed**`` opener is a question, and reading it as an answer would take
#: a live question off the list.
_RULING_LINE = re.compile(
    r"^[ \t]*(?:>[ \t]*)*\*\*Ruled\b", re.IGNORECASE | re.MULTILINE
)

#: A ruling recorded as a HEADING, in a body or a comment. The spellings are
#: the ones this repository's issues actually carry, counted across ~400 of
#: them: ``## Ruling`` (15), ``## Ruling recorded`` (3), ``## Ruled`` (3),
#: ``## Owner ruling, <date>`` (3), ``## Ruling on item 15``, ``## Owner
#: rulings``, ``## Owner ruling``, ``## Decision record``. Matching one
#: spelling was the round-15 detector's defect: #1451 and #1502 were ruled
#: under ``## Ruling recorded`` and were moved to blocked as open questions.
#: A false negative here asks the owner a question they have answered, which
#: is the expensive direction, so any heading line counts, not only a first.
#:
#: A heading that ASKS for a ruling is not one: ``## Ruling needed`` or
#: ``## Ruling requested`` counted as made would take a live question off the
#: list — leak A again. Same rule as the bold line: the word followed by a
#: request, or a heading that ends in a question mark, is a question.
_RULING_HEADING = re.compile(
    r"^[ \t]*#{1,6}[ \t]*(?:Ruling|Ruled|Owner[ \t]+rulings?|Decision[ \t]+record)\b"
    r"(?![ \t:—–-]*(?:needed|requested|required|pending|wanted|asked|sought"
    r"|to[ \t]+come)\b)"
    r"(?![^\n]*\?[ \t]*$)",
    re.IGNORECASE | re.MULTILINE,
)


def _question_phrases(text: str) -> tuple[str, ...]:
    """The distinct question-language phrases in ``text``, first seen first."""
    seen: dict[str, None] = {}
    for match in _QUESTION_LANGUAGE.finditer(text):
        seen.setdefault(" ".join(match.group(0).lower().split()), None)
    return tuple(seen)


def _ruling_recorded(text: str) -> bool:
    """Whether ``text`` (code blocks already blanked) records a ruling."""
    return bool(_RULING_LINE.search(text) or _RULING_HEADING.search(text))


def ruling_recorded(issue: Issue, facts: BodyFacts | None = None) -> bool:
    """A ruling in the body, or a comment carrying a ruling heading."""
    in_body = (
        facts.ruled
        if facts is not None
        else _ruling_recorded(without_code_blocks(issue.body))
    )
    return in_body or any(
        _RULING_HEADING.search(without_code_blocks(comment))
        for comment in issue.comments or ()
    )


def ready_questions(
    issues: Sequence[Issue], repo: str, facts: dict[int, BodyFacts] | None = None
) -> dict:
    """Ready items whose body reads like a question and records no ruling.

    Leak A (#1639): step 2 sorts only arrivals, so an item that entered
    ready carrying a question — or was left there by a sort that missed it —
    is never read again, and its only exits are the rule-4 slot and being
    ranked into a round. This is the list that gives it a reader. It is
    NEVER a label move: the language is a symptom, and most hits are prose
    about the code rather than about the issue.

    ``comments_read`` is False when any listed item's comments were not
    fetched, in which case a ruling recorded only in a comment is not seen
    and the list over-counts — the safe direction for a list to read.
    """
    facts = facts if facts is not None else read_bodies(issues, repo)
    ready = [i for i in issues if i.is_open and pile_of(i) == READY_LABEL]
    listed = []
    unread = False
    for issue in sorted(ready, key=lambda i: i.number):
        body = facts[issue.number]
        if not body.question or ruling_recorded(issue, body):
            continue
        unread = unread or issue.comments is None
        listed.append((issue.number, list(body.question)))
    return {"ready": len(ready), "listed": listed, "comments_read": not unread}


def blocking_graph(
    issues: Sequence[Issue], repo: str, facts: dict[int, BodyFacts] | None = None
) -> BlockingGraph:
    """The blocked pile's edges, read from the bodies and nowhere else.

    This is NOT the follow-up graph. A marker says an issue was *found while
    working on* another, which is provenance, not dependency: of the six
    children the three highest-in-degree ready items have in this corpus,
    five closed while their supposed blocker was still open. An in-edge count
    would therefore rank items that unblock nobody, and rule 1 is the top
    rank — so the edge is read from the statement the procedure requires
    instead of inferred from prose that means something else.

    An edge and an unreadable reference are not exclusive. ``**Blocked on:**
    #100 and #9999`` used to take the edge and DISCARD the rest, so #9999 —
    a blocked state with no detector, which is the whole reason the
    unreadable bucket exists — appeared nowhere (#1580).
    """
    facts = facts if facts is not None else read_bodies(issues, repo)
    by_number = {issue.number: issue for issue in issues}
    waiting_on: dict[int, list[int]] = defaultdict(list)
    on_ruling: list[int] = []
    unresolved: list[tuple[int, str]] = []
    unstated: list[int] = []
    condition_met: list[tuple[int, int]] = []
    on_condition: list[tuple[int, str]] = []
    unobservable: list[int] = []
    # OPEN, as :class:`BlockingGraph` says: a closed blocked item is not
    # waiting on anything. Counting one would exclude its blocker from the
    # tier under rule 1, so the reserved slot would never reach an item
    # whose blockers are already resolved — the direction this measurement
    # cannot afford. `ready` and `multi_labelled` filter it too.
    blocked = [
        issue for issue in issues if issue.is_open and pile_of(issue) == BLOCKED_LABEL
    ]
    for issue in sorted(blocked, key=lambda i: i.number):
        body = facts[issue.number]
        if not body.stated:
            unstated.append(issue.number)
            continue
        if body.condition is not None:
            # A deferral on something to be observed: neither an edge nor a
            # question. Counted as a ruling it was listed under *Needs your
            # call* as a question nobody had (#673, #1639 leak B).
            on_condition.append((issue.number, body.condition))
            if _UNOBSERVABLE.match(body.condition):
                unobservable.append(issue.number)
            continue
        here, leftover, seen = [], [], set()
        for number in body.named:
            # ``#100 and #100`` is one dependency stated twice. Counted
            # twice it gives a SINGLE blocked item enough in-edges to trip
            # rule 1, which excludes its blocker from the tier — "the leak
            # the slot was written to close" — and prints the
            # condition-met line twice.
            if number in seen:
                continue
            seen.add(number)
            if number == issue.number:
                # :func:`parent_of` refuses a self-citation for the same
                # reason; without it here a self-edge inflates the printed
                # edge count and a closed self prints "#N waits on #N,
                # which has closed" (#1580).
                leftover.append(f"#{number} (itself)")
            elif number in by_number:
                here.append(number)
            else:
                leftover.append(f"#{number}")
        for number in here:
            waiting_on[number].append(issue.number)
            if not by_number[number].is_open:
                condition_met.append((issue.number, number))
        text = " ".join([*leftover, body.unreadable]).strip()
        if text:
            # Reference-shaped and unreadable: a pull request number, another
            # repository, an issue this corpus does not carry. NOT a ruling.
            unresolved.append((issue.number, text))
        elif not here:
            on_ruling.append(issue.number)
    return BlockingGraph(
        # Already ascending: the loop above visits the blocked items in
        # number order, so each blocker's list is built in that order.
        waiting_on=dict(sorted(waiting_on.items())),
        on_ruling=on_ruling,
        unresolved=unresolved,
        unstated=unstated,
        condition_met=condition_met,
        on_condition=on_condition,
        unobservable=unobservable,
    )


def cited_paths(body: str) -> set[str]:
    """Every repository FILE an issue body names outside a code block."""
    return set(_CITED_PATH.findall(without_code_blocks(body)))


def hot_seams(
    issues: Sequence[Issue], now: dt.datetime, facts: dict[int, BodyFacts] | None = None
) -> dict[str, list[int]]:
    """Paths that ``SEAM_ISSUES`` or more issues named inside the window.

    Information for whoever ranks, and nothing else — :func:`rule4_tier`
    does not exclude on this. See there for why.
    """
    cutoff = now - dt.timedelta(days=SEAM_WINDOW_DAYS)
    produced: dict[str, list[int]] = defaultdict(list)
    for issue in issues:
        if issue.created < cutoff:
            continue
        cited = (
            facts[issue.number].cited if facts is not None else cited_paths(issue.body)
        )
        for path in cited:
            produced[path].append(issue.number)
    return {
        path: sorted(numbers)
        for path, numbers in sorted(produced.items())
        if len(numbers) >= SEAM_ISSUES
    }


def rule4_tier(
    issues: Sequence[Issue],
    now: dt.datetime,
    repo: str,
    facts: dict[int, BodyFacts] | None = None,
) -> dict:
    """The ready items holding none of picking rules 1-3, oldest first.

    An UPPER BOUND, and the report says so every time. Over-approximating is
    the safe direction and the only one: the figure exists so that a tier
    GROWING across four rounds is visible, and the reserved slot is aimed at
    the tier's oldest member. An item wrongly left in is one the slot may
    reach early; an item wrongly taken out is one nothing reaches at all,
    which is the leak the slot was written to close.

    **Only rule 1 is computed.** Rule 2 is a property of the defect rather
    than of a label and has no mechanical form at all. Rule 3 was computed
    from cited paths in the first cut of this and is not any more, on two
    measurements over the live corpus:

    * **A citation is not a production.** Of the ten items it excluded,
      three were wrong and all three by the same mechanism — a path named
      as a REFERENCE. #1462 (chromadb credentials) and #1463 (filter-shaped
      routes) were unranked on ``docs/development/issue-processing.md``,
      which they cite only because this campaign's issues quote its gates,
      and which leads the hot list at 7 for exactly that reason. The
      campaign's own procedure file had become a seam that silently unranked
      unrelated work.
    * **It would not be comparable round over round**, which is the third
      thing #1511 asks of this figure. What the procedure file scores is a
      function of how many issues happened to quote a gate that month, so
      the tier would move by three for reasons with nothing to do with the
      backlog.

    So the hot seams are REPORTED, for whoever ranks to apply rule 3 by
    reading — which is what "it sits on a seam" always required, being a
    judgement about the issue's subject rather than about its text. The cost
    is stated rather than hidden: false positives 0 by construction, false
    negatives every genuine rule-3 item, which on this corpus is at least
    the seven of those ten that were right plus the whole health-signal seam
    (#1515, #1516, #1547, #1565, #1568 — five issues in a month, and not one
    of them names a path).

    Blocker inheritance (*Picking*, leak B): a blocked item lends the issue
    it waits on its own claim and its own filing date, so a blocker is
    ranked for what it releases. Unconditional, as the procedure states it —
    a blocker of two or more already holds rule 1, so the lend changes
    nothing there and no threshold has to be restated here.
    """
    facts = facts if facts is not None else read_bodies(issues, repo)
    by_number = {issue.number: issue for issue in issues}
    open_issues = [issue for issue in issues if issue.is_open]
    ready = [issue for issue in open_issues if pile_of(issue) == READY_LABEL]
    graph = blocking_graph(issues, repo, facts)
    seams = hot_seams(issues, now, facts)

    def rule_1(issue: Issue) -> bool:
        return len(graph.waiting_on.get(issue.number, ())) >= RULE_1_DEPENDENTS

    excluded: list[int] = []
    lent_claim: list[tuple[int, int]] = []
    lent_age: list[tuple[int, int]] = []
    tier: list[tuple[dt.datetime, int]] = []
    for issue in ready:
        held = rule_1(issue)
        since = issue.created
        # The SOURCE of the age it ends up ranked at, recorded after the
        # loop and only if the item is in the tier: the append used to sit
        # inside the improvement test, so three successively older
        # dependents printed three lines for one item — and printed them
        # for an item rule 1 had excluded, claiming a rank in a tier it has
        # no place in (#1581).
        lent_from: int | None = None
        for number in graph.waiting_on.get(issue.number, ()):
            dependent = by_number[number]
            if rule_1(dependent) and not held:
                held = True
                lent_claim.append((issue.number, number))
            if dependent.created < since:
                since = dependent.created
                lent_from = number
        if held:
            excluded.append(issue.number)
        else:
            tier.append((since, issue.number))
            if lent_from is not None:
                lent_age.append((issue.number, lent_from))
    tier.sort()
    lent_age.sort()
    return {
        # Told apart from an empty tier on purpose: a corpus with no pile
        # labels at all would otherwise report 0 and read as a drained tier.
        "labelled": any(
            label.startswith(_PILE_PREFIX) for issue in issues for label in issue.labels
        ),
        "ready": len(ready),
        "excluded": sorted(excluded),
        "lent_claim": lent_claim,
        "lent_age": lent_age,
        "tier": [n for _, n in tier],
        "oldest": [
            (n, round((now - since).total_seconds() / _DAY)) for since, n in tier[:5]
        ],
        "multi_labelled": sorted(
            issue.number
            for issue in open_issues
            if TRACKING_LABEL not in issue.labels
            and len({la for la in issue.labels if la.startswith(_PILE_PREFIX)}) > 1
        ),
        # In no pile (#1639, leak C). Listed so a tracker is visible rather
        # than merely exempt — the label is also a way to take real work out
        # of every pile, and a list read each round is what would show it.
        "tracking": sorted(
            issue.number for issue in open_issues if TRACKING_LABEL in issue.labels
        ),
        "tracking_in_pile": sorted(
            issue.number
            for issue in open_issues
            if TRACKING_LABEL in issue.labels
            and any(la.startswith(_PILE_PREFIX) for la in issue.labels)
        ),
        "seams": seams,
        # String keys, so the in-process value and a --json round trip agree:
        # json.dumps coerces an int key to a string and nothing coerces it
        # back, which made the two disagree silently.
        "blocking": {
            **asdict(graph),
            "waiting_on": {str(n): v for n, v in graph.waiting_on.items()},
        },
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


_DIFF_START = "diff --git "


def removed_lines(diff: str) -> list[tuple[str, int]]:
    """``(old path, old line number)`` for every line a unified diff removed.

    The old path is read from the ``--- a/`` line of each file's HEADER, and
    a ``diff --git`` line is what opens a header: a removed CONTENT line can
    begin ``-- a/`` (and a path can contain the substring `` b/``), so
    neither a bare ``---`` scan nor a split of the ``diff --git`` line is
    safe on its own. The diff must carry the ``a/``/``b/`` prefixes — the
    caller forces them, because ``diff.noprefix`` would otherwise blank
    every path. A renamed file is dated on its OLD path, so a move-and-edit
    contributes the lines the fix changed and not the whole moved file. A
    new file has no pre-fix lines (the caller's filter excludes added files
    anyway, so the ``/dev/null`` arm guards other callers) and a test file
    is not the defect. A pure insertion is recorded as a NEGATIVE anchor at
    the insertion point; the caller uses the anchors only when nothing was
    removed.
    """
    lines: list[tuple[str, int]] = []
    path: str | None = None
    in_header = False
    for line in diff.split("\n"):
        if line.startswith(_DIFF_START):
            path, in_header = None, True
        elif in_header and line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("@@"):
            in_header = False
            if not path or _is_test_path(path):
                continue
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
#: blank them), colour off (``color.diff = always`` wraps every header in
#: escapes and would silently date nothing), non-ASCII paths unquoted,
#: renames detected so a move-and-edit is dated on the lines it changed at
#: the OLD path rather than on the whole moved file, and deleted files kept
#: (their lines are pre-fix lines).
_DIFF_ARGS = (
    "-c",
    "core.quotePath=false",
    "-c",
    "color.ui=never",
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
    """Every section, over the corpus as it stood at ``now``.

    The snapshot is taken HERE rather than at the argv boundary, so a caller
    that asks for a past instant cannot get ages measured at one time mixed
    with events from another.
    """
    issues = snapshot_at(issues, now)
    # ONE parse of every body, shared by all four readers of them (#1577).
    facts = read_bodies(issues, repo)
    pr_links = (
        pr_closing_issues(marker_numbers(issues, repo, facts), repo)
        if resolve_parents
        else None
    )
    return {
        "as_of": now.isoformat(),
        "weekly": weekly_flow(issues, now),
        "survival": survival(issues, now),
        "open": residue_snapshot(issues, now),
        "follow_ups": follow_ups(issues, repo, pr_links, facts),
        "rule4": rule4_tier(issues, now, repo, facts),
        "questions": ready_questions(issues, repo, facts),
        "latency": fix_latency(issues, repo) if latency else None,
    }


def _inline_code(text: str) -> str:
    """``text`` as a markdown code span, whatever backticks it contains.

    The statement is a raw slice of an issue body, so it may hold backticks
    of its own — "the owner's call on whether `kb_qa` should see #1294" — and
    a fixed single-backtick span around one emits unbalanced markdown onto
    the `Queue`, which the round pastes this section into verbatim (#1581).
    The fence is one backtick longer than the longest run inside, and a span
    whose content starts or ends with a backtick is padded, both per
    CommonMark. Whitespace is collapsed first, because a code span cannot
    span a line.
    """
    # One line, always: a statement is bounded by a blank line, so it can
    # carry newlines, and a span broken across lines leaves each of them
    # with an odd number of backtick runs — the very breakage this exists
    # to prevent, on the common shape of a wrapped `**Blocked on:**` line.
    text = " ".join(text.split())
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if not text or text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _rule4_text(tier: dict) -> str:
    """The rule-4 tier, as an upper bound that says it is one.

    The number answers one question — is the tier growing across four
    rounds — and names the item the reserved slot buys first. The arithmetic
    has to close, because the round is told to quote this verbatim into the
    Queue: ``ready - tier`` is the excluded count and nothing else is
    attributed to it.
    """
    out = ["## Rule-4 tier (ready items holding none of picking rules 1-3)\n"]
    if not tier["labelled"]:
        out.append(
            "No `pile:` label in this corpus, so the piles cannot be read and "
            "the tier is not computable here — which is not the same as empty.\n"
        )
        return "\n".join(out)
    if not tier["ready"]:
        out.append(
            "No `pile:ready` issue is open, so there is no tier to measure — "
            "which is not the same as a drained one.\n"
        )
    else:
        out.append(
            f"**{len(tier['tier'])} of {tier['ready']} ready items — an UPPER "
            f"BOUND.** Only rule 1 is computed, and it excludes "
            f"{len(tier['excluded'])}"
            + (
                f" (of which {len(tier['lent_claim'])} by inheriting the claim "
                "of a blocked item waiting on them)"
                if tier["lent_claim"]
                else ""
            )
            + '. Rule 2 ("a security or correctness defect reachable in a '
            'shipped configuration") is a property of the defect, not of a '
            "label; rule 3 is reported below rather than applied, because a "
            "path an issue CITES is not the seam that produced it. Every item "
            "holding either is counted here.\n"
        )
    if tier["lent_age"]:
        out.append(
            "Ranked earlier than their own filing date by the lend: "
            + ", ".join(f"#{a} (from #{b})" for a, b in tier["lent_age"])
            + ".\n"
        )
    if tier["oldest"]:
        out.append(
            "Oldest in the tier, which is what the round's reserved slot buys "
            "first (candidates to read, not a verdict — the oldest may hold "
            "rule 2 or rule 3): "
            + ", ".join(f"#{n} ({age}d)" for n, age in tier["oldest"])
            + ".\n"
        )
    elif tier["ready"]:
        out.append("The tier is empty: every ready item holds rule 1.\n")
    graph = tier["blocking"]
    edges = sum(len(v) for v in graph["waiting_on"].values())
    out.append(
        f"Blocking graph: {edges} edge(s) from the blocked pile — "
        f"{len(graph['on_ruling'])} item(s) waiting on a ruling, "
        f"{len(graph['on_condition'])} waiting on a condition, "
        f"{len(graph['unstated'])} stating nothing in the body "
        "(`**Blocked on:**` is what is read; a statement left in a comment is "
        "not).\n"
    )
    if graph["on_condition"]:
        unobservable = set(graph["unobservable"])
        out.append(
            "**Waiting on a condition** — answered and waiting, so *Needs your "
            "call* lists each with its condition, never as a question; step 2 "
            "checks the condition as it sorts and moves the item to ready the "
            "round it holds: "
            + ", ".join(
                f"#{n}{' (UNOBSERVABLE today)' if n in unobservable else ''} "
                f"({_inline_code(_clip(text))})"
                for n, text in graph["on_condition"]
            )
            + ".\n"
        )
    if graph["unobservable"]:
        out.append(
            "**Nothing can check** "
            + ", ".join(f"#{n}" for n in graph["unobservable"])
            + ": the condition is stated as unobservable, so its only exit is "
            "the owner — build the measurement, re-rule, or close.\n"
        )
    if graph["unresolved"]:
        out.append(
            "**Stated but unreadable** — reference-shaped and not resolved to "
            "an issue here, so not an edge (the statement may carry edges too): "
            + ", ".join(
                f"#{n} ({_inline_code(text)})" for n, text in graph["unresolved"]
            )
            + ".\n"
        )
    if graph["condition_met"]:
        out.append(
            "**Condition met, still blocked:** "
            + ", ".join(
                f"#{a} waits on #{b}, which has closed"
                for a, b in graph["condition_met"]
            )
            + " — these move to ready at the next sort.\n"
        )
    if tier["multi_labelled"]:
        out.append(
            "**Carrying more than one `pile:` label** — a half-finished move, "
            "read as blocked (or as yours where blocked is not among them), so "
            "out of the tier, and undispatchable either way because step 4 "
            "skips any ready item carrying a second `pile:` label: "
            + ", ".join(f"#{n}" for n in tier["multi_labelled"])
            + ".\n"
        )
    if tier["tracking_in_pile"]:
        out.append(
            "**Tracking, but carrying a `pile:` label** — a tracker is in no "
            "pile and is read as such; remove the leftover label: "
            + ", ".join(f"#{n}" for n in tier["tracking_in_pile"])
            + ".\n"
        )
    if tier["tracking"]:
        out.append(
            "Tracking, in no pile (never ranked, never dispatched, never "
            "sorted): " + ", ".join(f"#{n}" for n in tier["tracking"]) + ".\n"
        )
    if tier["seams"]:
        ranked = sorted(tier["seams"].items(), key=lambda kv: (-len(kv[1]), kv[0]))
        out.append(
            f"Hot seams for rule 3, to APPLY BY READING (>={SEAM_ISSUES} issues "
            f"citing one file in {SEAM_WINDOW_DAYS}d; a floor, because an issue "
            "naming symbols rather than files contributes none): "
            + ", ".join(f"`{path}` ({len(nums)})" for path, nums in ranked[:6])
            + (f", and {len(ranked) - 6} more" if len(ranked) > 6 else "")
            + ".\n"
        )
    return "\n".join(out)


def _questions_text(questions: dict) -> str:
    """The ready items step 2 reads for a question the pile does not show."""
    out = [
        "## Ready items that read like a question (step 2 READS each; "
        "never a label move on this list alone)\n"
    ]
    if not questions["listed"]:
        out.append(
            f"None of {questions['ready']} ready items carries decision, "
            "gating or trigger language without a recorded ruling.\n"
        )
        return "\n".join(out)
    out.append(
        f"**{len(questions['listed'])} of {questions['ready']} ready items** "
        "carry decision, gating or trigger language and no recorded ruling "
        "(a `**Ruled` body line, or a heading `Ruling`, `Ruled`, "
        "`Owner ruling(s)` or `Decision record` in the body or a comment). A "
        "heuristic: most hits are prose about the code. Each one read either "
        "stays in ready or is placed by *What escalates* — blocked on a "
        "ruling, an issue or a condition, split, or `tracking`: "
        + ", ".join(
            f"#{n} ("
            + ", ".join(_inline_code(p) for p in phrases[:_QUESTION_PHRASES_SHOWN])
            + ")"
            for n, phrases in questions["listed"]
        )
        + ".\n"
    )
    if not questions["comments_read"]:
        out.append(
            "Comments were not read for some of these, so a ruling recorded "
            "only in a comment is not seen: an over-count.\n"
        )
    return "\n".join(out)


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
        f"net {total_in - total_out:+d}; {pending} open issues in these weeks "
        f"are still too young to count.\n"
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

    out.append(_rule4_text(results["rule4"]))
    out.append(_questions_text(results["questions"]))

    fu = results["follow_ups"]
    out.append("## Follow-ups (regex floor)\n")
    out.append(
        f"{fu['attributed']} issues name a parent they were found while working "
        f"on, across {fu['parents']} parent issues ({fu['via_pr']} resolved "
        f"through the PR the marker named); {fu['unresolved']} name a number "
        "that resolves to no issue here (a PR that closed none)."
        + (
            " Most prolific: "
            + ", ".join(f"#{p} ({len(k)})" for p, k in fu["top"][:6])
            + "."
            if fu["top"]
            else ""
        )
        + "\n"
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
    if args.offline and not args.issues:
        parser.error("--offline needs --issues: there is nothing to read offline")

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
    later_filings = sum(1 for i in issues if i.created > now)
    later_closures = sum(1 for i in issues if i.closed and i.closed > now)
    if later_filings or later_closures:
        print(
            f"note: --as-of precedes the dump's own events: {later_filings} "
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

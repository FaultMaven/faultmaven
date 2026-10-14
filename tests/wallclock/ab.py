"""Compare two recorded benchmark runs — the A/B gate's decision rule (#1567).

What this exists to do that a budget cannot
-------------------------------------------

``tests/benchmarks/budgets.py`` holds **absolute** per-test thresholds.
#1556 pulled them from ~38x measured cost down to 2.6x, which was real
work, and #1567 measured what that buys: of the 50 budgets it then held
(19 remain since #1890 and #1935; the figures here are that
measurement's, not re-measured), **none** fails on a 30% regression, and the smallest
regression any of them catches is about
115% — that figure is against each budget's anchoring p95, and against
the MEDIAN of eight green ``main`` runs the same budgets need 2.46x-3.57x
(+146% to +257%). Two statistics of one fact; neither is near 30%.
Tightening further is not available — run-to-run variance on a
shared runner is itself tens of percent (#908 measured the whole pytest
process scaling 1.28x between two runs of identical code), so a budget
tight enough to fail at 30% would flake, and a flaky performance gate gets
muted. That is how #908 began.

A *relative* measurement escapes that: the same benchmark, on the same
box, minutes apart, on two checkouts. Machine speed, co-tenant load and
thermal state are shared by both sides and cancel. What is left is the
code.

The rule, the one rejected, and the one in shadow
-------------------------------------------------

Every matched row yields a ratio in which **above 1.0 is worse** — head
over base for a latency, base over head for a throughput, so one number
means one thing. The gate fires on **one** statistic: the **median** of
those ratios. That is the whole-suite factor — "did the data layer get
slower overall" — and it is #908's uniform scaling measured directly
instead of corrected for.

Two more sensitive-looking rules were measured on #1567's 12-run null
experiment (identical code on both sides, 132 ordered pairs, 50
benchmarks each, on a loaded development box) and both were rejected by
their own numbers: a per-test rule on ``ratio / median`` (null range
1.18x-3.38x) and a count rule (at R = 1.30 the null count reached 19 of 50
while slowing half the suite by 30% gave 15). #1963 then measured the
suite's null at 19 benchmarks on the gate's own kind of runner, and #1989
re-adjudicated both rules on it (run 38538085168: 144 same-runner ordered
pairs, 24 of them the job's own A1 to B1 and A2 to B2 direction, plus the 21
real reports of this job). They come out differently:

* **The per-test rule stays rejected, for a new reason.** Its null tail on
  the runner is single-row bursts: the worst ``ratio / median`` over the 144
  pairs is 1.742 (p90 1.23), and one of the 21 real reports shows the same
  shape (+57% in one row). Clear of that by the 15% the median gate keeps
  the threshold is 2.0, and there it sees a quarter of the suite 30%
  slower in 0.5% of cases and the 6-row module in 1.4% — it fires, 46% of
  the time, only on ONE row twice as slow: the single-path class, not the
  one it was re-measured for. The residual is still computed and printed,
  because it is how you read a red median.
* **The count rule is adopted in SHADOW.** "Fail if at least K benchmarks
  are at least R times slower" is the same statement as "the K-th largest
  ratio is at least R". On the runner null the count at R = 1.25 is at most
  1 of 19 (0 on 133 of the 144 pairs), and the third-largest ratio's
  maximum is 1.190, so R = 1.25 clears it by 5% and K = 3 is the null's
  maximum count plus two. Through ``compare`` it detects a random quarter of
  the suite 30% slower in 91% of cases, a random half in 99.9%, the 6-row
  case module in 94% and a uniform +30% in 100% — where the median gate sees
  0%, 0.8%, 0% and 50%. One or two slowed rows are invisible to it by
  construction. It is red on 0 of the 144 pairs, 0 of the 24 and 0 of the
  21 real reports, but that is 33 independent clean run-sets (12 runners,
  21 real ones): too few to bound its false-red rate below about 9%, and a
  loaded box (the stress case, not the gate's environment) reddens it on 328
  of 552 clean pairs. So it is **computed and reported on every gated
  comparison and does not fail the job** until the evidence says it should,
  the same evidence-first policy ``THRESHOLD_FLOOR`` (in
  ``tests/unit/ci/test_benchmark_ab.py``) pins for the median. The promotion
  criterion, and the numbers behind all of this, are in
  ``docs/development/testing/performance.md``.

What the median cannot see is stated rather than hidden: a regression
confined to a minority of the suite. Slowing a quarter of these
benchmarks by 30% moves the median to about 1.05, well under any usable
threshold. The shadow rule is the instrument pointed at that class; until it
is promoted the class stays with the absolute budgets, and the report says
how many rows the shadow rule saw.

The threshold is supplied by the caller and is chosen from a null
experiment; the number and its margin are in the workflow's ``env:``
block, in #1567 (at 50 benchmarks) and in #1963 (re-measured at 19, the
note on ``COVERAGE_FLOOR``).

Reading a red
-------------

‼ These benchmarks are SQLite CRUD against an in-memory database at
single-digit milliseconds. FaultMaven's user-perceived latency is
dominated by LLM calls measured in seconds. A 30% regression here is
roughly 1.7 ms inside a multi-second turn, so this gate is a **code-health
detector, not a product-latency one**: it answers "did this change make
the data layer do more work", and it does not answer "did this change make
the product slower for anyone".
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

#: Copied from ``record.py`` rather than imported, because this module
#: imports nothing but the standard library. Two editable installs of
#: ``faultmaven`` are made and unmade around it in the A/B job, and a
#: comparison of two JSON files should not depend on which of them is
#: current, nor drag the application's import graph into a step that only
#: reads numbers. The duplicated values are pinned to ``record.py``'s by
#: ``tests/unit/ci/test_benchmark_ab.py``.
LATENCY_METRIC = "latency_seconds"
THROUGHPUT_METRIC = "throughput_per_second"

#: Record-schema versions this comparator can read.
#:
#: The base side of an A/B is an arbitrary commit on ``main``, so its
#: ``record.py`` is a different file from this one's neighbour. A base
#: that carries the recorder but writes an older shape must be SKIPPED,
#: not parsed — the workflow reads the base tree's
#: ``RECORD_FORMAT_VERSION`` and checks it against this set before it
#: spends a benchmark run, and ``load`` refuses an unreadable row as a
#: backstop. Add the old version here when a bump is backward-readable;
#: leave it out when it is not.
SUPPORTED_RECORD_VERSIONS = frozenset({1, 2})

#: The share of the BASE's benchmarks the comparison has to cover before
#: its median means anything.
#:
#: ‼ Without a floor the gate narrows in silence. Both benchmark steps
#: are ``continue-on-error``, so a head-side crash that kills 18 of 19
#: benchmarks leaves ONE matched row, a "suite median" that is that row's
#: noise, and a green job. The empty case was already refused for exactly
#: this reason; 1-of-19 is the same argument one step in.
#:
#: 0.5 rather than something tighter because a pull request may legitimately
#: delete a whole benchmark module. Measured on the current tree (#1963),
#: the two benchmark modules hold 13 (``test_knowledge_item_operations``)
#: and 6 (``test_case_operations``) of the 19 rows, so deleting the smaller
#: leaves 0.68 of the base and is still gated, while deleting the larger
#: leaves 0.32 and FAILS here as "the head measured a fragment". That is
#: the intended reading, not a gap: a 6-row median is not the suite the
#: threshold was measured on. The floor separates "the head measured the
#: suite" from "the head measured a fragment"; it is deliberately not sized
#: to catch a small deletion, which the budget table's own guards already
#: refuse to let pass silently. (The suite got here in two steps from the
#: 50 rows #1567 measured: #1890 removed a 7-row module, 50 to 43, and #1935
#: removed the 15- and 9-row investigation-session modules, 43 to 19. With
#: the four modules of 15, 13, 9 and 6 rows in between, deleting the largest
#: left 28 of 43 = 0.65 and was still gated.)
#:
#: ‼ The 1.30 threshold is backed by a null experiment on the suite as it
#: is. Measured 2026-10-14 at 19 benchmarks a side (#1963; the original,
#: #1567 / #1592, was at 50, on a loaded development box):
#:
#: * **The gate's own environment** — run 38538085168 of a throwaway
#:   branch: 12 GitHub runners, each running the 19-row suite four times
#:   (A1, B1, A2, B2), base == head. It is a probe of the job's
#:   procedure, not the job: one checkout rather than a base worktree
#:   beside the head, no ``PYTHONPATH``/``AB_EXPECTED_ROOT``,
#:   ``-p no:cacheprovider``, and one install between A1 and B1 only
#:   (A2 and B2 follow with none). The suite median over the 144
#:   same-runner ordered pairs ranged **0.950 up to
#:   ``RUNNER_NULL_MAX_MEDIAN``** (the worst, pinned in
#:   ``tests/unit/ci/test_benchmark_ab.py``; p99 1.043), A1 as base and
#:   B1 as head (the pairs with the install between) 0.973-1.040: 0 red at
#:   every threshold from 1.15 up. Across runners the in-process
#:   calibration spanned 737-1132 us/rep (1.53x) — the spread a same-job
#:   comparison cancels; the at most 1.09x drift within one runner is what
#:   remains, and it does not move the median (r = -0.07).
#: * **The real gate** — the 21 A/B runs on pull requests since #1960
#:   merged (2026-10-12 17:17Z to 2026-10-14 10:51Z) all sit inside that
#:   range, 0.956-1.034, but not centred on 1.0: geometric mean 0.984, 18 of
#:   21 below 1.0, an offset toward head-faster that the probe did not
#:   reproduce. The job's own procedure differs from the probe's in the
#:   points above, and these are real pull requests, so some of the offset
#:   may be their own changes; the gate prints its median on every run, so
#:   it keeps accumulating.
#: * **The stress case** — 24 runs alternating two byte-identical trees on
#:   one 48-core development box at load average 22-50 (the original ran
#:   at 8-19): 552 ordered pairs, suite median **0.700-1.428** (p99 1.345),
#:   red at 1.30 on **12 of 552**, at 1.40 on 1. That is the suite under
#:   contention the runner does not see, not the gate's environment. Most
#:   of the red pairs show one cause: one module holds 13 of the 19 rows,
#:   so a burst that lands inside that module's window alone moves the
#:   median (9 of the 12 red pairs have that module's own median at 1.3-1.8
#:   and the other at 1.0-1.14; in the other 3 the 6-row module is at
#:   1.33-1.35 too). The load, which was shared with other work on the same
#:   box, is a confound: it may be what lifted either module. At 50 rows in
#:   five modules none held more than 30%, so one module's burst could not
#:   do that.
#:
#: If the suite changes materially again (rows added or removed, or the
#: modules rebalanced), re-run the null experiment before trusting the
#: number; the method and the data sets are in
#: ``docs/development/testing/performance.md``.
COVERAGE_FLOOR = 0.5

#: How many rows the worst-first table prints before it is truncated.
TABLE_ROWS = 15

Key = Tuple[str, str, int]


class Row(NamedTuple):
    """One recorded comparison, reduced across that side's repeats."""

    key: Key
    nodeid: str
    label: str
    metric: str
    observed: float
    budget: float


class Comparison(NamedTuple):
    """One benchmark present on both sides."""

    row: Row
    base: float
    head: float
    ratio: float

    @property
    def label(self) -> str:
        return f"{self.row.nodeid.rsplit('::', 1)[-1]} ({self.row.label})"


class Verdict(NamedTuple):
    """Everything the workflow needs to report and to decide."""

    compared: List[Comparison]
    head_only: List[Row]
    base_only: List[Row]
    unusable: List[Tuple[Row, str]]
    median: Optional[float]
    worst: Optional[Comparison]
    worst_residual: Optional[float]
    failures: List[str]
    #: Things the reader has to know that are not failures.
    notes: List[str]
    #: Whether the suite rule was actually applied. ‼ False means this
    #: pull request was NOT gated, which is a different thing from
    #: passing, and the report has to say which happened.
    gated: bool
    #: Matched benchmarks as a share of the base's.
    coverage: float
    #: The SHADOW count rule (#1989), as configured: rows at least
    #: ``count_ratio`` slower, and how many of them fail it. ``None`` for
    #: both means the caller did not ask for the rule.
    count_ratio: Optional[float]
    count_min: Optional[int]
    #: The matched rows whose ratio is at least ``count_ratio``, worst
    #: first. Empty when the rule was not evaluated, which is also what a
    #: clean comparison looks like: ``count_would_fail`` tells them apart.
    over: List[Comparison]
    #: ‼ Three states, not two. ``True``: the rule would fail this
    #: comparison. ``False``: it ran and would not. ``None``: it did NOT
    #: run (not configured, or the comparison was not gated), which is not
    #: the same thing as clean and must not be scored as clean.
    count_would_fail: Optional[bool]

    @property
    def ok(self) -> bool:
        # ‼ The shadow rule is deliberately absent. `failures` holds what
        # gates; the count rule only reports until it is promoted (#1989).
        return not self.failures


def load(paths: Sequence[Path]) -> Dict[Key, Row]:
    """Read one side's JSONL files, reducing repeats to the best observation.

    Several paths mean the side was measured more than once in the job.
    They are reduced per key by taking the **best** observation — the
    minimum latency, the maximum throughput — for the same one-sided-error
    reason ``measure_min_latency`` takes a minimum: on a contended box
    scheduling delay, page-cache misses and co-tenant bursts can only add
    time, so the fastest observation is the closest estimate of what the
    operation costs. Taking the best on BOTH sides keeps the ratio
    unbiased.

    A malformed line raises rather than being skipped: a recorder writing
    junk is a broken detector, and dropping the line would report it as a
    missing benchmark.
    """
    rows: Dict[Key, Row] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number}: not JSON ({error})") from error
            version = raw.get("v")
            if version not in SUPPORTED_RECORD_VERSIONS:
                raise ValueError(
                    f"{path}:{number}: record format version {version!r} is not "
                    f"one this comparator reads "
                    f"({sorted(SUPPORTED_RECORD_VERSIONS)}); the workflow is "
                    f"meant to skip the comparison before reaching here"
                )
            try:
                key: Key = (raw["nodeid"], raw["label"], int(raw["occurrence"]))
                row = Row(
                    key=key,
                    nodeid=raw["nodeid"],
                    label=raw["label"],
                    metric=raw["metric"],
                    observed=float(raw["observed"]),
                    budget=float(raw["budget"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{path}:{number}: malformed record ({error})"
                ) from error
            if row.metric not in (LATENCY_METRIC, THROUGHPUT_METRIC):
                # Refuse a metric this module was not taught rather than
                # guessing its direction: `load` would reduce it as a
                # throughput and `_ratio` would drop it as unusable, so a
                # new metric added to `record.py` alone would go missing
                # instead of failing.
                raise ValueError(
                    f"{path}:{number}: unknown metric {row.metric!r} "
                    f"(teach tests/wallclock/ab.py about it)"
                )
            previous = rows.get(key)
            if previous is None:
                rows[key] = row
                continue
            if previous.metric != row.metric:
                raise ValueError(
                    f"{path}:{number}: {key} recorded as {row.metric} here and "
                    f"{previous.metric} in an earlier file"
                )
            rows[key] = previous._replace(
                observed=_better(row.metric, previous.observed, row.observed)
            )
    return rows


def _better(metric: str, first: float, second: float) -> float:
    """The better of two observations of one benchmark on one side.

    ‼ Non-finite values are dropped rather than compared, because
    ``min``/``max`` are not order-invariant across a NaN:
    ``min(nan, 1.0)`` is ``nan`` and ``min(1.0, nan)`` is ``1.0``. The
    same two record files would then classify differently depending on
    the order they were passed on the command line, which is a detector
    whose answer depends on its argv. A key whose every observation is
    non-finite keeps one, so ``_ratio`` still reports it as unusable
    instead of it vanishing.
    """
    finite = [value for value in (first, second) if math.isfinite(value)]
    if not finite:
        return first
    if len(finite) == 1:
        return finite[0]
    return min(finite) if metric == LATENCY_METRIC else max(finite)


def _ratio(metric: str, base: float, head: float) -> Optional[float]:
    """Head against base, oriented so that ABOVE 1.0 is always worse.

    Returns ``None`` when the ratio is not defined. A non-positive or
    non-finite observation on either side is not a fast benchmark, it is a
    broken measurement, and inventing a ratio for it would either invent a
    regression or hide one.
    """
    for value in (base, head):
        if not math.isfinite(value) or value <= 0:
            return None
    if metric == LATENCY_METRIC:
        return head / base
    if metric == THROUGHPUT_METRIC:
        # Throughput is 1/latency: the head being SLOWER means a smaller
        # rate, so the ratio is inverted to keep "above 1.0 is worse".
        # Getting this backwards would report every throughput regression
        # as an improvement, which is silent by construction.
        return base / head
    return None


def check_count_rule(count_ratio: Optional[float], count_min: Optional[int]) -> None:
    """Refuse a half-configured or meaningless shadow count rule (#1989).

    Both or neither: a half-configured rule would either never run or run on
    a number nobody chose, and the job would still look green. ``main``
    turns the ``ValueError`` into a usage error.
    """
    if (count_ratio is None) != (count_min is None):
        raise ValueError(
            "the count rule's ratio and minimum are given together or not at all"
        )
    if count_ratio is not None and not count_ratio > 1.0:
        raise ValueError(
            f"the count ratio must exceed 1.0 (above 1.0 is slower), got "
            f"{count_ratio!r}"
        )
    if count_min is not None and count_min < 1:
        raise ValueError(f"the count minimum must be at least 1, got {count_min!r}")


def compare(
    base: Dict[Key, Row],
    head: Dict[Key, Row],
    *,
    suite_threshold: float,
    count_ratio: Optional[float] = None,
    count_min: Optional[int] = None,
) -> Verdict:
    """Join the two sides and apply the rule.

    ``count_ratio`` and ``count_min`` configure the SHADOW count rule
    (#1989): on a gated comparison, the matched rows at least
    ``count_ratio`` slower are collected and ``count_would_fail`` says
    whether there are ``count_min`` of them. They come together or not at
    all, and they never reach ``failures``: the verdict is the median's
    until the rule is promoted.
    """
    check_count_rule(count_ratio, count_min)

    compared: List[Comparison] = []
    unusable: List[Tuple[Row, str]] = []

    for key, head_row in head.items():
        base_row = base.get(key)
        if base_row is None:
            continue
        if base_row.metric != head_row.metric:
            unusable.append(
                (head_row, f"metric changed: {base_row.metric} -> {head_row.metric}")
            )
            continue
        ratio = _ratio(head_row.metric, base_row.observed, head_row.observed)
        if ratio is None:
            unusable.append(
                (
                    head_row,
                    f"unusable observations (base {base_row.observed!r}, "
                    f"head {head_row.observed!r})",
                )
            )
            continue
        compared.append(
            Comparison(
                row=head_row,
                base=base_row.observed,
                head=head_row.observed,
                ratio=ratio,
            )
        )

    head_only = [row for key, row in head.items() if key not in base]
    base_only = [row for key, row in base.items() if key not in head]

    compared.sort(key=lambda entry: entry.ratio, reverse=True)

    median = statistics.median(entry.ratio for entry in compared) if compared else None
    worst = compared[0] if compared else None
    # The residual is against the suite median. A median of 0 is
    # impossible here — every ratio in `compared` is finite and positive
    # by construction — but the guard costs nothing and a
    # ZeroDivisionError inside a CI step reads as a broken job rather
    # than a broken measurement.
    worst_residual = (
        worst.ratio / median if worst is not None and median and median > 0 else None
    )

    base_total = len(base)
    head_total = len(head)
    matched = len(compared)
    coverage = matched / base_total if base_total else 0.0
    needed = COVERAGE_FLOOR * base_total

    failures: List[str] = []
    notes: List[str] = []
    gated = False
    over: List[Comparison] = []
    count_would_fail: Optional[bool] = None

    if base_total == 0:
        # The base is a commit already green on `main`; recording nothing
        # is a broken measurement, never a clean one.
        failures.append(
            "the base side recorded no benchmarks at all, so there was nothing "
            "to compare against — an empty A/B is not a pass"
        )
    elif head_total < needed:
        # ‼ The head measured a FRACTION of the suite. Both benchmark
        # steps are `continue-on-error`, so this is what a head-side
        # crash looks like from here, and without this branch it arrives
        # as a green job whose "suite median" is a handful of rows'
        # noise.
        # ‼ Name both causes. The comparator cannot see the diff, so
        # asserting "your run crashed" would be a guess, and a red whose
        # stated cause is wrong is how a gate loses its reader.
        failures.append(
            f"the head recorded {head_total} benchmarks against the base's "
            f"{base_total} ({head_total / base_total:.0%}), below the "
            f"{COVERAGE_FLOOR:.0%} floor — either the head benchmark run did "
            f"not complete (both run steps are continue-on-error, so a crash "
            f"arrives here rather than as a red step) or this pull request "
            f"removed most of the suite. Either way the median would be a "
            f"fragment's noise: if the removal is deliberate, the 1.30 "
            f"threshold was measured at 19 benchmarks a side and has to be "
            f"re-measured for the smaller suite"
        )
    elif matched < needed:
        # Both sides measured the suite, but the KEYS moved: renamed,
        # moved or re-parametrised benchmarks. Nothing is broken and the
        # diff shows it, so this is reported rather than failed — the
        # same treatment a single deleted benchmark already gets. It is
        # an acknowledged escape: a pull request that renames the whole
        # suite is not gated by this job.
        notes.append(
            f"‼ NOT GATED. Only {matched} of the base's {base_total} benchmarks "
            f"({coverage:.0%}) matched, below the {COVERAGE_FLOOR:.0%} floor, "
            f"while the head measured {head_total} of them — so the suite was "
            f"reorganised (renamed, moved or re-parametrised) rather than lost. "
            f"The comparison cannot speak for this pull request; read the diff."
        )
    else:
        gated = True
        if median is not None and median >= suite_threshold:
            failures.append(
                f"the whole suite is {median:.2f}x slower than base (median over "
                f"{matched} benchmarks); the suite rule fails at "
                f"{suite_threshold:.2f}x"
            )
        # ‼ The shadow rule reads the same gated rows the median does and
        # writes nowhere near `failures`. `compared` is sorted worst-first,
        # so `over` is too. Only a gated comparison evaluates it: a
        # fragment or a renamed suite is not what the count was measured
        # on, and "not evaluated" must not read as "clean" (#1989).
        if count_ratio is not None and count_min is not None:
            over = [entry for entry in compared if entry.ratio >= count_ratio]
            count_would_fail = len(over) >= count_min

    return Verdict(
        compared=compared,
        head_only=head_only,
        base_only=base_only,
        unusable=unusable,
        median=median,
        worst=worst,
        worst_residual=worst_residual,
        failures=failures,
        notes=notes,
        gated=gated,
        coverage=coverage,
        count_ratio=count_ratio,
        count_min=count_min,
        over=over,
        count_would_fail=count_would_fail,
    )


def _units(metric: str, value: float) -> str:
    if metric == LATENCY_METRIC:
        return f"{value * 1000:.2f}ms"
    return f"{value:.1f}/s"


def _shadow_lines(verdict: Verdict) -> List[str]:
    """The shadow count rule's paragraph (#1989), for an evaluated verdict.

    ‼ Says in so many words that it does not gate. The line sits beside the
    one that does, and a reader who cannot tell the two apart will either
    ignore a rule that is collecting its evidence or blame the pull request
    for a red that is not there.
    """
    would_fail = bool(verdict.count_would_fail)
    outcome = (
        "so it would fail this pull request if it gated"
        if would_fail
        else "so it would not fail this pull request"
    )
    lines = [
        f"Shadow rule (not gating yet, #1989): {len(verdict.over)} of "
        f"{len(verdict.compared)} benchmarks ≥ {verdict.count_ratio:.2f}x "
        f"slower; would fail at {verdict.count_min}, {outcome}. The verdict "
        f"above is the median's alone and is unchanged."
    ]
    if verdict.over:
        lines.append("")
        for entry in verdict.over[:TABLE_ROWS]:
            lines.append(f"- {entry.label}: {entry.ratio:.2f}x")
        if len(verdict.over) > TABLE_ROWS:
            lines.append(f"- …{len(verdict.over) - TABLE_ROWS} more")
    return lines


def render(
    verdict: Verdict,
    *,
    base_label: str,
    head_label: str,
    suite_threshold: float,
) -> str:
    """The markdown both the step summary and the pull-request comment use."""
    lines: List[str] = ["### Benchmark A/B against the merge base", ""]
    lines.append(f"`{head_label}` (head) vs `{base_label}` (base), same runner.")
    lines.append("")

    if verdict.median is None:
        # Reached both when the measurement broke and when the suite was
        # reorganised. Either way the one thing the reader must not have
        # to infer is whether the rule ran.
        lines.append(
            "**No comparison was possible** — nothing was measured on both "
            "sides, so this pull request was **not gated**."
        )
    else:
        residual = (
            f"{verdict.worst_residual:.2f}x"
            if verdict.worst_residual is not None
            else "not computable"
        )
        # ‼ Say whether the rule RAN, not just what it would have said.
        # "green" and "not gated" are different outcomes and a reader who
        # cannot tell them apart has been told nothing.
        verdict_line = (
            f"Suite median **{verdict.median:.3f}x** — this is the gate, "
            f"and it fails at {suite_threshold:.2f}x."
            if verdict.gated
            else f"Suite median {verdict.median:.3f}x, **not applied** "
            f"(see below) — this pull request was not gated."
        )
        lines.append(
            f"**{len(verdict.compared)} benchmarks compared** "
            f"({verdict.coverage:.0%} of the base's). {verdict_line}"
        )
        lines.append("")
        lines.append(
            f"Worst single benchmark, relative to that median: {residual}. "
            "That number is **not** a gate: on identical code it ranged "
            "1.18x-3.38x at 50 benchmarks on a loaded box (#1567) and "
            "1.03x-1.74x (p50 1.16, p90 1.23) at 19 benchmarks on GitHub "
            "runners (#1963), a tail of single-row bursts, so one "
            "benchmark's residual is triage information, not evidence."
        )
        if verdict.count_would_fail is not None:
            lines.append("")
            lines.extend(_shadow_lines(verdict))
    lines.append("")

    if verdict.failures:
        lines.append("#### Regression")
        for failure in verdict.failures:
            lines.append(f"- {failure}")
        lines.append("")

    if verdict.notes:
        lines.append("#### Coverage")
        for note in verdict.notes:
            lines.append(f"- {note}")
        lines.append("")

    if verdict.compared:
        lines.append("| benchmark | base | head | ratio | vs suite |")
        lines.append("|---|---|---|---|---|")
        for entry in verdict.compared[:TABLE_ROWS]:
            residual = (
                f"{entry.ratio / verdict.median:.2f}x" if verdict.median else "n/a"
            )
            lines.append(
                f"| {entry.label} | {_units(entry.row.metric, entry.base)} "
                f"| {_units(entry.row.metric, entry.head)} "
                f"| {entry.ratio:.2f}x | {residual} |"
            )
        if len(verdict.compared) > TABLE_ROWS:
            lines.append(
                f"| …{len(verdict.compared) - TABLE_ROWS} lower-ratio "
                f"benchmarks omitted | | | | |"
            )
        lines.append("")

    # Reported even when empty is not worth the noise, but reported
    # PROMINENTLY when not: a head that stops emitting a benchmark shrinks
    # what the A/B can see, and silence about that is how a gate narrows
    # itself.
    if verdict.head_only:
        lines.append(
            f"{len(verdict.head_only)} benchmark(s) on head have no counterpart "
            "at base (new, renamed or moved) and were not compared: "
            + ", ".join(
                sorted(row.nodeid.rsplit("::", 1)[-1] for row in verdict.head_only)[:10]
            )
        )
        lines.append("")
    if verdict.base_only:
        lines.append(
            f"‼ {len(verdict.base_only)} benchmark(s) measured at base produced "
            "no measurement on head (deleted, renamed, skipped or errored): "
            + ", ".join(
                sorted(row.nodeid.rsplit("::", 1)[-1] for row in verdict.base_only)[:10]
            )
        )
        lines.append("")
    if verdict.unusable:
        lines.append(f"{len(verdict.unusable)} benchmark(s) could not be compared:")
        for row, reason in verdict.unusable[:10]:
            lines.append(f"- {row.nodeid.rsplit('::', 1)[-1]}: {reason}")
        lines.append("")

    lines.append(
        "These benchmarks are SQLite CRUD at single-digit milliseconds; "
        "FaultMaven's user-perceived latency is LLM calls in seconds. Read a "
        "red here as *the data layer started doing more work*, not as *the "
        "product got slower*."
    )
    return "\n".join(lines) + "\n"


def _existing(values: Iterable[str]) -> List[Path]:
    paths = [Path(value) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit(f"benchmark_ab: no such record file: {', '.join(missing)}")
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.wallclock.ab",
        description="Compare two recorded benchmark runs (#1567).",
    )
    parser.add_argument("--base", action="append", required=True, metavar="JSONL")
    parser.add_argument("--head", action="append", required=True, metavar="JSONL")
    parser.add_argument("--base-label", default="base")
    parser.add_argument("--head-label", default="head")
    parser.add_argument("--suite-threshold", type=float, required=True)
    parser.add_argument(
        "--count-ratio",
        type=float,
        help="shadow count rule (#1989): a benchmark this many times slower "
        "or more is counted; requires --count-min",
    )
    parser.add_argument(
        "--count-min",
        type=int,
        help="shadow count rule (#1989): the rule would fail at this many "
        "counted benchmarks; requires --count-ratio",
    )
    parser.add_argument("--markdown-out", metavar="PATH")
    parser.add_argument("--json-out", metavar="PATH")
    args = parser.parse_args(argv)
    try:
        check_count_rule(args.count_ratio, args.count_min)
    except ValueError as error:
        parser.error(f"--count-ratio / --count-min: {error}")

    verdict = compare(
        load(_existing(args.base)),
        load(_existing(args.head)),
        suite_threshold=args.suite_threshold,
        count_ratio=args.count_ratio,
        count_min=args.count_min,
    )
    markdown = render(
        verdict,
        base_label=args.base_label,
        head_label=args.head_label,
        suite_threshold=args.suite_threshold,
    )
    sys.stdout.write(markdown)
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown, encoding="utf-8")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "ok": verdict.ok,
                    "gated": verdict.gated,
                    "coverage": verdict.coverage,
                    "notes": verdict.notes,
                    "median": verdict.median,
                    "worst_residual": verdict.worst_residual,
                    # The shadow count rule (#1989). ‼ `count_would_fail`
                    # and `over_count` are null, not false and 0, when the
                    # rule did not run (not configured, or not gated): a
                    # scorer that reads null as clean would count a run
                    # that measured nothing toward promotion.
                    "count_ratio": verdict.count_ratio,
                    "count_min": verdict.count_min,
                    "count_would_fail": verdict.count_would_fail,
                    "over_count": (
                        len(verdict.over)
                        if verdict.count_would_fail is not None
                        else None
                    ),
                    "over": [
                        {
                            "benchmark": f"{entry.row.nodeid}|{entry.row.label}|"
                            f"{entry.row.key[2]}",
                            "ratio": entry.ratio,
                        }
                        for entry in verdict.over
                    ],
                    "compared": len(verdict.compared),
                    "head_only": [row.nodeid for row in verdict.head_only],
                    "base_only": [row.nodeid for row in verdict.base_only],
                    "failures": verdict.failures,
                    "ratios": {
                        f"{entry.row.nodeid}|{entry.row.label}|"
                        f"{entry.row.key[2]}": entry.ratio
                        for entry in verdict.compared
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return 0 if verdict.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

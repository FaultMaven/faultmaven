"""Compare two recorded benchmark runs — the A/B gate's decision rule (#1567).

What this exists to do that a budget cannot
-------------------------------------------

``tests/benchmarks/budgets.py`` holds **absolute** per-test thresholds.
#1556 pulled them from ~38x measured cost down to 2.6x, which was real
work, and #1567 measured what that buys: of 50 budgets, **none** fails on
a 30% regression, and the smallest regression any of them catches is about
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

The rule, and the two that were measured and rejected
-----------------------------------------------------

Every matched row yields a ratio in which **above 1.0 is worse** — head
over base for a latency, base over head for a throughput, so one number
means one thing. The gate fires on **one** statistic: the **median** of
those ratios. That is the whole-suite factor — "did the data layer get
slower overall" — and it is #908's uniform scaling measured directly
instead of corrected for.

Two more sensitive-looking rules were measured on a 12-run null
experiment (identical code on both sides, 132 ordered pairs, 50
benchmarks each) and both were rejected by their own numbers:

* **A per-test rule** on ``ratio / median`` — "did THIS path get slower
  relative to the rest of the suite". Its statistic is the maximum over
  50 residuals, an extreme-order statistic over 50 draws, and on the null
  it ranged **1.18x to 3.38x**. A threshold with no false positives is
  therefore about 3.5x, which is above the 2.46x-3.57x band the absolute
  budgets already fire in. A per-test A/B rule is dominated by the
  budgets it would sit beside, so it is not a gate. The residual is still computed and printed, because it is how
  you read a red median.
* **A count rule** — "fail if K of the 50 are at least R times slower".
  At R = 1.30 the null count reached **19 of 50**, while a regression
  slowing **half** the suite by 30% produced **15**. The signal is below
  the noise; there is no K that separates them.

What the median cannot see is stated rather than hidden: a regression
confined to a minority of the suite. Slowing a quarter of these
benchmarks by 30% moves the median to about 1.05, well under any usable
threshold. That class stays with the absolute budgets.

The threshold is supplied by the caller and is chosen from the same null
experiment; the number and its margin are in the workflow's ``env:``
block and in #1567.

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
SUPPORTED_RECORD_VERSIONS = frozenset({1})

#: The share of the BASE's benchmarks the comparison has to cover before
#: its median means anything.
#:
#: ‼ Without a floor the gate narrows in silence. Both benchmark steps
#: are ``continue-on-error``, so a head-side crash that kills 49 of 50
#: benchmarks leaves ONE matched row, a "suite median" that is that row's
#: noise, and a green job. The empty case was already refused for exactly
#: this reason; 1-of-50 is the same argument one step in.
#:
#: 0.5 rather than something tighter because a pull request may legitimately
#: delete a whole benchmark module. Measured on the current tree, the five
#: modules hold 15, 13, 9, 7 and 6 of the 50 rows, so deleting the largest
#: leaves 0.70 of the base and deleting the two largest leaves 0.44. The
#: floor separates "the head measured the suite" from "the head measured a
#: fragment"; it is deliberately not sized to catch a small deletion, which
#: the budget table's own guards already refuse to let pass silently.
#:
#: ‼ The 1.30 threshold's noise was measured with 50 benchmarks a side. If
#: the suite ever shrinks materially, re-run the null experiment before
#: trusting the number.
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

    @property
    def ok(self) -> bool:
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


def compare(
    base: Dict[Key, Row],
    head: Dict[Key, Row],
    *,
    suite_threshold: float,
) -> Verdict:
    """Join the two sides and apply the rule."""
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
            f"threshold was measured at 50 benchmarks a side and has to be "
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
    )


def _units(metric: str, value: float) -> str:
    if metric == LATENCY_METRIC:
        return f"{value * 1000:.2f}ms"
    return f"{value:.1f}/s"


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
            "That number is **not** a gate: measured on identical code it "
            "ranges 1.18x-3.38x, so one benchmark's residual is triage "
            "information, not evidence (#1567)."
        )
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
    parser.add_argument("--markdown-out", metavar="PATH")
    parser.add_argument("--json-out", metavar="PATH")
    args = parser.parse_args(argv)

    verdict = compare(
        load(_existing(args.base)),
        load(_existing(args.head)),
        suite_threshold=args.suite_threshold,
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

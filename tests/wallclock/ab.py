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
            better = (
                min(previous.observed, row.observed)
                if row.metric == LATENCY_METRIC
                else max(previous.observed, row.observed)
            )
            rows[key] = previous._replace(observed=better)
    return rows


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

    failures: List[str] = []
    if not compared:
        failures.append(
            "no benchmark was measured on both sides, so nothing was compared "
            "— an empty A/B is not a pass"
        )
        return Verdict(
            compared=compared,
            head_only=head_only,
            base_only=base_only,
            unusable=unusable,
            median=None,
            worst=None,
            worst_residual=None,
            failures=failures,
        )

    median = statistics.median(entry.ratio for entry in compared)
    worst = compared[0]
    # The residual is against the suite median, so the per-test rule asks
    # "slower than the rest of the suite" and the suite rule below asks
    # "slower than base overall". A median of 0 is impossible here —
    # every ratio in `compared` is finite and positive by construction —
    # but the guard costs nothing and a ZeroDivisionError inside a CI
    # step reads as a broken job rather than a broken measurement.
    worst_residual = worst.ratio / median if median > 0 else None

    if median >= suite_threshold:
        failures.append(
            f"the whole suite is {median:.2f}x slower than base (median over "
            f"{len(compared)} benchmarks); the suite rule fails at "
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
        lines.append("**No comparison was possible.**")
    else:
        residual = (
            f"{verdict.worst_residual:.2f}x"
            if verdict.worst_residual is not None
            else "not computable"
        )
        lines.append(
            f"**{len(verdict.compared)} benchmarks compared.** "
            f"Suite median **{verdict.median:.3f}x** — this is the gate, "
            f"and it fails at {suite_threshold:.2f}x."
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

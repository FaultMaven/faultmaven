"""Summarise wall-clock rows collected from many CI runs (#1910).

Why this exists
---------------

``tests/performance/budgets.py`` was anchored on a development box, and a
re-anchor from CI needs the statistic every timed comparison saw, on enough
green runs, normalised by that run's calibration. The per-PR run of the
timed tests (``benchmarks.yml``'s "Run wall-clock tests" step, #2090) passes
no ``-s``, so a passing test's stdout never reaches its log; the rows are
therefore recorded where they are computed (``record.py``) and uploaded as
the ``wallclock-rows-<profile>`` artifacts. This script is the join.

Operator steps (no network access happens in this script)::

    for run in $(gh run list --workflow benchmarks.yml --branch main \\
            --status success --limit 30 --json databaseId -q '.[].databaseId'); do
      gh run download "$run" --pattern 'wallclock-rows-*' --dir "rows/$run"
    done
    python -I tests/wallclock/collect.py rows/

A push run carries ``wallclock-rows-performance`` (the per-PR profile), a
scheduled run ``wallclock-rows-nightly`` (isolated runner, absolute mode).

Every ``*.jsonl`` under the given paths is read, recursively.

What it prints
--------------

Per ``(nodeid, label)`` and per job profile: the sample count, the number of
distinct CI runs, and the min / median / p90 / max of the NORMALISED
statistic.

Every row is normalised by its ``raw_ratio`` -- the machine's measured
calibration over the reference, unfloored -- when that is present and
positive, so per-PR rows (scale applied, floored at 1.0) and nightly rows
(scale pinned at 1.0 in absolute mode) land on one axis, "the time on the
reference machine":

* latency rows: ``observed / raw_ratio``.
* throughput rows: ``observed * raw_ratio``.

A v1 row has no ``raw_ratio``; it falls back to the applied ``scale``.

Joining runs: ``sha`` on a pull_request run is GitHub's throwaway test-merge
commit, not a commit on any branch. Join runs by ``run_id`` (with
``run_attempt`` for re-runs), never by ``sha``.

``--propose``: a re-anchor of ``tests/performance/budgets.py``
-------------------------------------------------------------

::

    python tests/wallclock/collect.py --propose --box-rev <rev> rows/

prints, per row of ``tests/performance/budgets.py``, the anchor the rule in
that module's docstring gives for the per-PR (``performance``) rows under
``rows/``. The row's statistic per CI job is its WORST comparison
in that job (a test that asserts one row several times, such as eleven
extraction shapes or four deduplication sizes, must pass all of them), and:

* latency: ``reference = max(box, p95 of the jobs)``, where the box term is the
  reference at ``--box-rev`` divided by ``--box-factor`` (that table was
  measured raw on a development box running that many times slower than the
  calibration reference);
* throughput: ``reference = min(box, p5 of the jobs)``, the box term multiplied;
* ``regression``: two significant figures, ``TARGET_MULTIPLE`` x the reference,
  raised (latency) or lowered (throughput) until it clears the worst job by
  ``CLEARANCE``; inside the 2-3x band; no looser than the product target.
  ``ANCHOR_ON_WORST_JOB`` rows anchor on the worst job at the 3x end instead.
* where clearing the worst job would leave the band only because the
  reference is the p95 to the last digit, the reference is rounded up (down,
  for throughput) to the nearest three-figure value that brings that
  ``regression`` inside it, by at most ``MAX_REFERENCE_ROUNDING``.

A row that cannot meet all three is printed ``STOP`` with the reason and no
value, and the command exits 3. The mapping from a recorded row to a table row is read from the call
sites in ``tests/performance/`` (``row_mapper``), not written down here, so it
cannot fall behind a renamed label. This mode imports the table, so it runs
from a checkout of the repository with its dependencies installed; it reads
the git history (``git show``) and nothing over the network.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import math
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

LATENCY = "latency_seconds"
THROUGHPUT = "throughput_per_second"

Key = Tuple[str, str, str]  # (nodeid, label, profile)


def iter_rows(paths: Iterable[Path]) -> Iterable[dict]:
    """Yield every JSON row under ``paths``; a malformed line raises."""
    for root in paths:
        files = sorted(root.rglob("*.jsonl")) if root.is_dir() else [root]
        for file in files:
            for number, line in enumerate(
                file.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"{file}:{number}: not JSON ({error})") from error


def normalised(row: dict) -> float:
    """The row's statistic on the reference machine (see module docstring)."""
    ratio = row.get("raw_ratio")
    if isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio > 0:
        scale = float(ratio)
    else:
        scale = float(row["scale"])
    observed = float(row["observed"])
    if row["metric"] == LATENCY:
        return observed / scale
    if row["metric"] == THROUGHPUT:
        return observed * scale
    raise ValueError(f"unknown metric {row['metric']!r}")


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty sequence."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarise(rows: Iterable[dict]) -> Dict[Key, Dict[str, float]]:
    """Group rows by ``(nodeid, label, profile)`` and reduce each group."""
    groups: Dict[Key, List[float]] = defaultdict(list)
    ratios: Dict[Key, List[float]] = defaultdict(list)
    runs: Dict[Key, set] = defaultdict(set)
    meta: Dict[Key, Tuple[str, float, str]] = {}
    for row in rows:
        key = (row["nodeid"], row["label"], row.get("profile", "unknown"))
        groups[key].append(normalised(row))
        runs[key].add((row.get("run_id"), row.get("run_attempt")))
        if "raw_ratio" in row:
            ratios[key].append(float(row["raw_ratio"]))
        meta[key] = (row["metric"], float(row["budget"]), row["kind"])
    out: Dict[Key, Dict[str, float]] = {}
    for key, values in groups.items():
        metric, budget, kind = meta[key]
        out[key] = {
            "n": len(values),
            "runs": len(runs[key]),
            "min": min(values),
            "median": statistics.median(values),
            "p90": percentile(values, 0.90),
            "max": max(values),
            "budget": budget,
            "min_raw_ratio": min(ratios[key]) if ratios[key] else float("nan"),
            "max_raw_ratio": max(ratios[key]) if ratios[key] else float("nan"),
        }
        out[key]["metric"] = metric  # type: ignore[assignment]
        out[key]["kind"] = kind  # type: ignore[assignment]
    return out


def render(summary: Dict[Key, Dict[str, float]]) -> str:
    """One line per group, sorted so a profile's rows sit together."""
    lines = [
        "profile\tn\truns\tmin\tmedian\tp90\tmax\tbudget(kind)\traw_ratio[min..max]"
        "\tnodeid :: label"
    ]
    for (nodeid, label, profile), s in sorted(
        summary.items(), key=lambda item: (item[0][2], item[0][0], item[0][1])
    ):
        lines.append(
            f"{profile}\t{int(s['n'])}\t{int(s['runs'])}\t{s['min']:.6g}\t{s['median']:.6g}"
            f"\t{s['p90']:.6g}\t{s['max']:.6g}\t{s['budget']:.6g}({s['kind']})"
            f"\t[{s['min_raw_ratio']:.2f}..{s['max_raw_ratio']:.2f}]"
            f"\t{nodeid} :: {label}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------ --propose (#1910)

REPO_ROOT = Path(__file__).resolve().parents[2]
PERFORMANCE_DIR = REPO_ROOT / "tests" / "performance"
TABLE_PATH = "tests/performance/budgets.py"

#: The per-PR job profiles a re-anchor reads. The nightly judges the product
#: target on an isolated runner, a different question. Until #2090 the per-PR
#: rows came from Test Standalone and Test Cloud (``standalone``, ``cloud``).
PROPOSE_PROFILES = ("performance",)

#: Where ``regression`` starts, as a multiple of ``reference``: the middle of
#: the 2-3x band the #1556 ruling fixed.
TARGET_MULTIPLE = 2.5

#: The margin the chosen ``regression`` must keep over the WORST CI job, not
#: only over the p95 the reference is taken at (#1916 review, Amendment A1).
CLEARANCE = 1.5

#: The multiple for a row in ``ANCHOR_ON_WORST_JOB``: the 3x end of the band.
WORST_JOB_MULTIPLE = 3.0

#: How far the reference may be rounded past its p95 (p5) so that a two-figure
#: ``regression`` inside the band clears the worst job (#1940 ruling on
#: ``CONTEXT_COPY``): to the nearest three-figure value that does, and by at
#: most this factor. Further than that, the row STOPs.
MAX_REFERENCE_ROUNDING = 1.05

#: The development box's measured ratio to the calibration reference when the
#: ``--box-rev`` table was taken (``tests/performance/budgets.py`` header).
DEFAULT_BOX_FACTOR = 3.53

#: The comparison helpers whose call sites name a row and a label.
_HELPERS = ("assert_latency_within", "assert_throughput_at_least")


def _sig(value: float, direction: str, figures: int) -> float:
    """``value`` at ``figures`` significant figures, rounded ``up``, ``down``
    or to the ``nearest``."""
    exponent = math.floor(math.log10(value)) - (figures - 1)
    step = 10.0**exponent
    scaled = value / step
    # 1e-9 absorbs the float error of the division, so a value that IS a
    # number of that many figures is never pushed to the next one.
    if direction == "up":
        digits = math.ceil(scaled - 1e-9)
    elif direction == "down":
        digits = math.floor(scaled + 1e-9)
    else:
        digits = round(scaled)
    return float(f"{digits * step:.{figures}g}")


def _sig2(value: float, direction: str) -> float:
    """``value`` at two significant figures (a ``regression``)."""
    return _sig(value, direction, 2)


@dataclass(frozen=True)
class Proposal:
    """One row's re-anchor. ``regression`` is None when the row STOPs."""

    row: str
    throughput: bool
    jobs: int
    old_reference: float
    old_regression: float
    product_target: float
    box: float
    ci: float
    worst_job: float
    reference: float
    source: str
    regression: float | None
    stop: str
    min_raw_ratio: float
    max_raw_ratio: float
    rounded_by: float = 1.0

    @property
    def multiple(self) -> float:
        if self.regression is None:
            return float("nan")
        if self.throughput:
            return self.reference / self.regression
        return self.regression / self.reference

    @property
    def clearance(self) -> float:
        """How far the anchor sits beyond the worst job (> 1 passes it)."""
        if self.regression is None:
            return float("nan")
        if self.throughput:
            return self.worst_job / self.regression
        return self.regression / self.worst_job


def propose_row(
    row: str,
    job_values: Sequence[float],
    *,
    throughput: bool,
    old_reference: float,
    old_regression: float,
    product_target: float,
    box_factor: float = DEFAULT_BOX_FACTOR,
    anchor_on_worst: bool = False,
    raw_ratios: Sequence[float] = (),
) -> Proposal:
    """The re-anchor the rule in the module docstring gives one row.

    ``job_values`` holds one normalised statistic per CI job (already the
    worst of that job's comparisons for the row).
    """
    if not job_values:
        raise ValueError(f"{row}: no CI jobs to anchor on")
    if throughput:
        box = old_reference * box_factor
        ci = percentile(job_values, 0.05)
        worst = min(job_values)
    else:
        box = old_reference / box_factor
        ci = percentile(job_values, 0.95)
        worst = max(job_values)

    if anchor_on_worst:
        reference, source = worst, "worst"
        multiple, rounding = WORST_JOB_MULTIPLE, "down"
    elif throughput:
        reference, source = (box, "box") if box <= ci else (ci, "ci")
        multiple, rounding = TARGET_MULTIPLE, "nearest"
    else:
        reference, source = (box, "box") if box >= ci else (ci, "ci")
        multiple, rounding = TARGET_MULTIPLE, "nearest"

    stop = ""
    rounded_by = 1.0
    if throughput:
        candidate = _sig2(reference / multiple, rounding)
        candidate = min(candidate, _sig2(worst / CLEARANCE, "down"))
        if reference / candidate > 3.0 + 1e-9:
            lowered = _sig(candidate * 3.0, "down", 3)
            if reference / lowered <= MAX_REFERENCE_ROUNDING:
                rounded_by, reference = lowered / reference, lowered
        if reference / candidate > 3.0 + 1e-9:
            stop = (
                f"clearing the worst job by {CLEARANCE}x needs a floor "
                f"{reference / candidate:.2f}x below the reference, past the band"
            )
        elif candidate < product_target:
            stop = f"the floor {candidate:g}/s is below the product target"
    else:
        candidate = _sig2(reference * multiple, rounding)
        candidate = max(candidate, _sig2(worst * CLEARANCE, "up"))
        if candidate / reference > 3.0 + 1e-9:
            raised = _sig(candidate / 3.0, "up", 3)
            if raised / reference <= MAX_REFERENCE_ROUNDING:
                rounded_by, reference = raised / reference, raised
        if candidate / reference > 3.0 + 1e-9:
            stop = (
                f"clearing the worst job by {CLEARANCE}x needs "
                f"{candidate / reference:.2f}x the reference, past the band"
            )
        elif candidate > product_target:
            stop = f"the anchor {candidate:g}s is looser than the product target"

    return Proposal(
        row=row,
        throughput=throughput,
        jobs=len(job_values),
        old_reference=old_reference,
        old_regression=old_regression,
        product_target=product_target,
        box=box,
        ci=ci,
        worst_job=worst,
        reference=float(f"{reference:.5g}"),
        source=source,
        regression=None if stop else candidate,
        stop=stop,
        min_raw_ratio=min(raw_ratios) if raw_ratios else float("nan"),
        max_raw_ratio=max(raw_ratios) if raw_ratios else float("nan"),
        rounded_by=rounded_by,
    )


def _test_name(nodeid: str) -> str:
    return nodeid.split("::")[-1].split("[")[0]


def row_mapper(
    table: Mapping[str, object], performance_dir: Path = PERFORMANCE_DIR
) -> Callable[[dict], str]:
    """A function from a recorded row to the name of its budget row.

    A test that owns one budget maps by its name. A test that owns two maps
    each comparison by the label written at its call site, read from the
    source: ``assert_latency_within(value, ROW, "label")``.
    """
    by_test: Dict[str, List[str]] = defaultdict(list)
    for name, budget in table.items():
        by_test[budget.test].append(name)  # type: ignore[attr-defined]
    by_label: Dict[Tuple[str, str], str] = {}
    for path in sorted(performance_dir.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in _HELPERS
                    and len(call.args) >= 3
                    and isinstance(call.args[1], ast.Name)
                    and isinstance(call.args[2], ast.Constant)
                    and isinstance(call.args[2].value, str)
                ):
                    by_label[(node.name, call.args[2].value)] = call.args[1].id

    def row_of(record: dict) -> str:
        test = _test_name(record["nodeid"])
        owned = by_test.get(test, [])
        if len(owned) == 1:
            return owned[0]
        key = (test, record["label"])
        if key in by_label and by_label[key] in owned:
            return by_label[key]
        raise KeyError(f"no budget row for {record['nodeid']} :: {record['label']}")

    return row_of


def job_statistics(
    rows: Iterable[dict],
    row_of: Callable[[dict], str],
    throughput_rows: Iterable[str],
    profiles: Sequence[str] = PROPOSE_PROFILES,
) -> Dict[str, Tuple[List[float], List[float]]]:
    """Per budget row: (one normalised statistic per CI job, every raw_ratio).

    A job is ``(run_id, run_attempt, profile)``; its statistic is the WORST of
    its comparisons for the row (largest latency, smallest throughput).
    """
    throughput_rows = set(throughput_rows)
    per_job: Dict[str, Dict[tuple, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    ratios: Dict[str, List[float]] = defaultdict(list)
    for record in rows:
        if record.get("profile") not in profiles:
            continue
        name = row_of(record)
        job = (record.get("run_id"), record.get("run_attempt"), record["profile"])
        per_job[name][job].append(normalised(record))
        if isinstance(record.get("raw_ratio"), (int, float)):
            ratios[name].append(float(record["raw_ratio"]))
    out: Dict[str, Tuple[List[float], List[float]]] = {}
    for name, jobs in per_job.items():
        pick = min if name in throughput_rows else max
        out[name] = ([pick(values) for values in jobs.values()], ratios[name])
    return out


def _load_table(source: str, label: str):
    """Import a ``budgets.py`` source text as a module and return its table."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "budgets_at_rev.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"_budgets_{label}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def propose_table(
    rows: Iterable[dict],
    table: Mapping[str, object],
    box_table: Mapping[str, object],
    *,
    anchor_on_worst: Iterable[str] = (),
    box_factor: float = DEFAULT_BOX_FACTOR,
    performance_dir: Path = PERFORMANCE_DIR,
) -> List[Proposal]:
    """``propose_row`` for every row of ``table`` (see the module docstring)."""
    from tests.wallclock.budgets import ThroughputBudget

    anchor_on_worst = set(anchor_on_worst)
    throughput_rows = {n for n, b in table.items() if isinstance(b, ThroughputBudget)}
    stats = job_statistics(rows, row_mapper(table, performance_dir), throughput_rows)
    proposals = []
    for name, budget in table.items():
        if name not in stats:
            raise ValueError(f"{name}: no per-PR rows recorded for it")
        old = box_table[name]
        values, ratios = stats[name]
        proposals.append(
            propose_row(
                name,
                values,
                throughput=name in throughput_rows,
                old_reference=old.reference,  # type: ignore[attr-defined]
                old_regression=old.regression,  # type: ignore[attr-defined]
                product_target=budget.product_target,  # type: ignore[attr-defined]
                box_factor=box_factor,
                anchor_on_worst=name in anchor_on_worst,
                raw_ratios=ratios,
            )
        )
    return proposals


def render_proposals(proposals: Sequence[Proposal]) -> str:
    """The proposal table, one row per budget, ready for a PR body."""
    lines = [
        "| row | jobs | old reference | old regression | box term | CI p95 (p5) "
        "| worst job | new reference (from) | new regression | multiple "
        "| clearance | raw_ratio min..max |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in proposals:
        stopped = p.regression is None
        regression = "STOP: " + p.stop if stopped else f"{p.regression:.3g}"
        multiple = "-" if stopped else f"{p.multiple:.2f}x"
        clearance = "-" if stopped else f"{p.clearance:.2f}x"
        source = p.source
        if p.rounded_by != 1.0:
            source += f", rounded {'up' if p.rounded_by > 1 else 'down'} "
            source += f"{abs(p.rounded_by - 1) * 100:.1f}%"
        lines.append(
            f"| {p.row} | {p.jobs} | {p.old_reference:.5g} | {p.old_regression:.3g} "
            f"| {p.box:.4g} | {p.ci:.4g} | {p.worst_job:.4g} "
            f"| {p.reference:.5g} ({source}) | {regression} | {multiple} "
            f"| {clearance} | {p.min_raw_ratio:.2f}..{p.max_raw_ratio:.2f} |"
        )
    return "\n".join(lines)


def _propose_main(args) -> int:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.performance import budgets as table_module

    source = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{args.box_rev}:{TABLE_PATH}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    box_table = _load_table(source, "box").ALL_BUDGETS
    proposals = propose_table(
        iter_rows(args.paths),
        table_module.ALL_BUDGETS,
        box_table,
        anchor_on_worst=table_module.ANCHOR_ON_WORST_JOB,
        box_factor=args.box_factor,
    )
    print(render_proposals(proposals))
    return 3 if any(p.regression is None for p in proposals) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--propose",
        action="store_true",
        help="print the re-anchor of tests/performance/budgets.py (docstring)",
    )
    parser.add_argument(
        "--box-rev",
        help="with --propose: the git revision whose table gives the box term",
    )
    parser.add_argument(
        "--box-factor",
        type=float,
        default=DEFAULT_BOX_FACTOR,
        help="with --propose: the box's ratio to the calibration reference",
    )
    args = parser.parse_args(argv)
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        print(f"no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    if args.propose:
        if not args.box_rev:
            parser.error("--propose needs --box-rev")
        return _propose_main(args)
    summary = summarise(iter_rows(args.paths))
    if not summary:
        # Empty input is not "nothing regressed".
        print("no rows found", file=sys.stderr)
        return 1
    print(render(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Summarise wall-clock rows collected from many CI runs (#1910).

Why this exists
---------------

``tests/performance/budgets.py`` was anchored on a development box, and a
re-anchor from CI needs the statistic every timed comparison saw, on enough
green runs, normalised by that run's calibration. The per-PR jobs (Test
Standalone, Test Cloud) run ``pytest -n logical`` with no ``-s``, so a
passing test's stdout never reaches their logs; the rows are therefore
recorded where they are computed (``record.py``) and uploaded as the
``wallclock-rows-<profile>`` artifacts. This script is the join.

Operator steps (no network access happens in this script)::

    for run in $(gh run list --workflow ci-cd.yml --branch main \\
            --status success --limit 30 --json databaseId -q '.[].databaseId'); do
      gh run download "$run" --pattern 'wallclock-rows-*' --dir "rows/$run"
    done
    # nightly (isolated runner) rows, from benchmarks.yml:
    gh run download <run> --name benchmark-results-absolute --dir rows/nightly-<run>
    python -I tests/wallclock/collect.py rows/

Every ``*.jsonl`` under the given paths is read, recursively.

What it prints
--------------

Per ``(nodeid, label)`` and per job profile: the sample count, and the
min / median / p90 / max of the NORMALISED statistic.

* latency rows: ``observed / scale`` -- the time the call would have taken
  on the reference machine, which is what a ``Budget`` anchor is written in.
* throughput rows: ``observed * scale``, the same normalisation inverted.

``scale`` is the calibration the run APPLIED (floored at 1.0, pinned to 1.0
in absolute mode). The row's ``raw_ratio`` is the unfloored machine ratio;
it is shown as its own column set so that a row from a runner FASTER than
the reference (``raw_ratio`` < 1, scale clamped at 1.0) is visible rather
than silently pooled. Profiles are never pooled with each other.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

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
    """The row's statistic with the applied calibration taken back out."""
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
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def summarise(rows: Iterable[dict]) -> Dict[Key, Dict[str, float]]:
    """Group rows by ``(nodeid, label, profile)`` and reduce each group."""
    groups: Dict[Key, List[float]] = defaultdict(list)
    ratios: Dict[Key, List[float]] = defaultdict(list)
    meta: Dict[Key, Tuple[str, float, str]] = {}
    for row in rows:
        key = (row["nodeid"], row["label"], row.get("profile", "unknown"))
        groups[key].append(normalised(row))
        if "raw_ratio" in row:
            ratios[key].append(float(row["raw_ratio"]))
        meta[key] = (row["metric"], float(row["budget"]), row["kind"])
    out: Dict[Key, Dict[str, float]] = {}
    for key, values in groups.items():
        metric, budget, kind = meta[key]
        out[key] = {
            "n": len(values),
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
        "profile\tn\tmin\tmedian\tp90\tmax\tbudget(kind)\traw_ratio[min..max]"
        "\tnodeid :: label"
    ]
    for (nodeid, label, profile), s in sorted(
        summary.items(), key=lambda item: (item[0][2], item[0][0], item[0][1])
    ):
        lines.append(
            f"{profile}\t{int(s['n'])}\t{s['min']:.6g}\t{s['median']:.6g}"
            f"\t{s['p90']:.6g}\t{s['max']:.6g}\t{s['budget']:.6g}({s['kind']})"
            f"\t[{s['min_raw_ratio']:.2f}..{s['max_raw_ratio']:.2f}]"
            f"\t{nodeid} :: {label}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        print(f"no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    summary = summarise(iter_rows(args.paths))
    if not summary:
        # Empty input is not "nothing regressed".
        print("no rows found", file=sys.stderr)
        return 1
    print(render(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

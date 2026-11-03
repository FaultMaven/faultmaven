"""Write tests/ci_shard_durations.json from the logs of finished CI runs.

    python scripts/ci_shard_durations.py RUN_ID [RUN_ID ...]

``tests/ci_shard.py`` splits Test Standalone and Test Cloud into slices by
expected time: each file's selected test count times its measured seconds per
test. This measures those seconds from the slice jobs' logs (``gh`` must be
authenticated; the repository's logs are public) and rewrites the file.

How a file's time is read: the slice jobs run ``pytest -n logical --dist
loadfile -v``, so each xdist worker runs whole files one at a time and the
log carries one timestamped line per test, ``[gwN] [ P%] PASSED path::name``.
On one worker, the time between consecutive lines is charged to the file of
the later line, counting from ``N workers [M items]`` (collection done). That
includes module-fixture setup and everything else a file costs, not only test
bodies, and needs nothing in CI beyond the log it already writes.

Pass several runs: runner speed varies ~1.8x between runners, and averaging
across runs is what evens it out. Only successful slice jobs are read. A
stale file costs balance, never correctness: every test still runs in exactly
one slice, and a file with no measurement is costed at the mean rate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "tests" / "ci_shard_durations.json"
SLICE_JOB = re.compile(r"^Test (Standalone|Cloud) \(slice \d+/\d+\)$")

_STAMP = r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"
_SESSION = re.compile(_STAMP + r" \d+ workers? \[\d+ items?\]")
_REPORT = re.compile(
    _STAMP + r" \[gw(\d+)\] \[\s*\d+%\] "
    r"(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR) ([^\s:]+\.py)::"
)

#: (seconds, tests) per file.
Costs = Dict[str, Tuple[float, int]]


def _seconds(stamp: str) -> float:
    """An Actions log timestamp (seven fractional digits) as epoch seconds."""
    whole, _, frac = stamp.rstrip("Z").partition(".")
    base = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return base.timestamp() + (float("0." + frac) if frac else 0.0)


def parse_log(text: str) -> Costs:
    """Per-file (seconds, tests) from one slice job's log."""
    seconds: Dict[str, float] = defaultdict(float)
    tests: Dict[str, int] = defaultdict(int)
    start = None
    last: Dict[str, float] = {}
    for raw in text.splitlines():
        # The logs API's text carries a BOM at its start and another where it
        # joins two stored chunks: mid-step, and on every slice log read so
        # far it fell on a report line, which `match` would otherwise miss.
        line = raw.lstrip("\ufeff")
        session = _SESSION.match(line)
        if session:
            start, last = _seconds(session.group(1)), {}
            continue
        report = _REPORT.match(line)
        if not report or start is None:
            continue
        at, worker, path = _seconds(report.group(1)), report.group(2), report.group(3)
        seconds[path] += at - last.get(worker, start)
        tests[path] += 1
        last[worker] = at
    return {path: (seconds[path], tests[path]) for path in seconds}


def combine(logs: Iterable[Costs]) -> Tuple[float, Dict[str, float]]:
    """(the mean seconds per test, each file's seconds per test) over all logs."""
    seconds: Dict[str, float] = defaultdict(float)
    tests: Dict[str, int] = defaultdict(int)
    for costs in logs:
        for path, (s, n) in costs.items():
            seconds[path] += s
            tests[path] += n
    if not tests:
        raise SystemExit(
            "no test lines found: has the slice jobs' pytest output changed?"
        )
    default = sum(seconds.values()) / sum(tests.values())
    return default, {path: seconds[path] / tests[path] for path in sorted(tests)}


def render(default: float, rates: Mapping[str, float], runs: List[int]) -> str:
    def sig(x: float) -> float:
        return float(f"{x:.4g}")

    return (
        json.dumps(
            {
                "about": (
                    "Seconds per selected test, by file, on CI's slice runners. "
                    "Written by scripts/ci_shard_durations.py, read by "
                    "tests/ci_shard.py; 'default' costs a file with no entry."
                ),
                "runs": runs,
                "default": sig(default),
                "files": {path: sig(rate) for path, rate in sorted(rates.items())},
            },
            indent=1,
        )
        + "\n"
    )


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def slice_logs(repo: str, run: int) -> List[Costs]:
    jobs = json.loads(
        _gh(
            "api",
            f"repos/{repo}/actions/runs/{run}/jobs?per_page=100",
            "--jq",
            "[.jobs[] | {id, name, conclusion}]",
        )
    )
    chosen = [
        j for j in jobs if SLICE_JOB.match(j["name"]) and j["conclusion"] == "success"
    ]
    if not chosen:
        raise SystemExit(f"run {run}: no successful slice jobs")
    logs = []
    for job in chosen:
        costs = parse_log(_gh("api", f"repos/{repo}/actions/jobs/{job['id']}/logs"))
        if not costs:
            raise SystemExit(f"run {run}, {job['name']}: no test lines in its log")
        logs.append(costs)
        print(f"  run {run}: {job['name']}: {len(costs)} files", file=sys.stderr)
    return logs


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=int, help="CI run ids (ci-cd.yml)")
    parser.add_argument("--repo", default="FaultMaven/faultmaven")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    logs = [costs for run in args.runs for costs in slice_logs(args.repo, run)]
    default, rates = combine(logs)
    args.output.write_text(render(default, rates, sorted(args.runs)), encoding="utf-8")
    print(
        f"wrote {args.output}: {len(rates)} files, default {default:.4g} s/test",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Measure pytest-xdist against the serial required test jobs, from CI logs (#1594).

The question #1594 asks has two halves, and a wall-clock number answers only
one of them:

1. **Is it faster?** The pytest phase of each run, read from the session's own
   summary line (``=== 12 failed, 15662 passed in 1640.80s (0:27:20) ===``) --
   never from the job's start/stop stamps, which include install time.
2. **Is it stable?** The failing set was recorded *shifting between runs* under
   whole-suite xdist, so a single xdist run is not evidence of anything. The
   report diffs every xdist run's failure list against the serial runs' and
   against each other, and names each test that fails only under xdist.

Everything is read from CI, by commit, because a local run measures the box
(``main`` fails a handful locally while CI is green on the same commit)::

    # fetch every relevant check run on a commit, then report
    python scripts/xdist_measurement.py fetch <sha> --out /tmp/xdist
    python scripts/xdist_measurement.py report /tmp/xdist/manifest.json

    # or parse one saved log
    python scripts/xdist_measurement.py parse job.log

A run whose log carries no summary line (cancelled at its cap, crashed before
the footer) is reported as ``incomplete`` -- never as zero failures.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = "FaultMaven/faultmaven"

# `gh api .../actions/jobs/<id>/logs` prefixes every line with an ISO stamp;
# `gh run view --log` additionally prefixes "<job>\t<step>\t". Both are
# stripped, as is the BOM GitHub puts on the first line and any ANSI colour.
_PREFIX_RE = re.compile(
    r"^(?:[^\t\n]*\t[^\t\n]*\t)?\ufeff?(?:\d{4}-\d\d-\d\dT[0-9:.]+Z ?)?"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# `=== 3 failed, 15662 passed, 12 skipped in 1640.80s (0:27:20) ===`
_SUMMARY_RE = re.compile(
    r"^=+ (?P<counts>.+?) in (?P<secs>[0-9.]+)s(?: \([0-9:]+\))? =+$"
)
_COUNT_RE = re.compile(r"(\d+) ([a-z]+)")
_SHORT_SUMMARY_HEADER_RE = re.compile(r"^=+ short test summary info =+$")
_OUTCOME_LINE_RE = re.compile(r"^(?P<outcome>FAILED|ERROR) (?P<rest>.+)$")
_WORKERS_RE = re.compile(r"^created: (\d+)/(\d+) workers?$")
_MARKER_RE = re.compile(r"^XDIST_MEASURE (?P<key>[a-z_]+)=(?P<value>\S*)$")
# actions/checkout names the commit it checked out; the required jobs carry no
# XDIST_MEASURE marker, so this is where their commit comes from.
_CHECKOUT_RE = re.compile(r"^HEAD is now at (?P<sha>[0-9a-f]{7,40}) ")

# Check-run names this measurement reads. The required jobs are the serial
# baseline CI already runs; the measurement workflow names its arms.
_REQUIRED_RE = re.compile(r"^Test (?P<suite>Standalone|Cloud)$")
_MEASURE_RE = re.compile(
    r"^xdist Measurement (?P<suite>Standalone|Cloud) \((?P<arm>serial|xdist-[a-z0-9]+)\)$"
)


@dataclass
class RunResult:
    """What one pytest session's log says about itself."""

    complete: bool
    seconds: float | None = None
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    workers: int | None = None
    nproc: int | None = None
    commit: str | None = None
    arm: str | None = None


def strip_line(raw: str) -> str:
    """One log line with GitHub's prefixes and ANSI colour removed."""
    return _ANSI_RE.sub("", _PREFIX_RE.sub("", raw, count=1)).rstrip("\r\n")


def split_nodeid(rest: str) -> str:
    """The node id from ``<nodeid> - <message>``, bracket-aware.

    A parametrize id may itself contain `` - `` (``test_x[a - b]``), so the
    separator is the first `` - `` OUTSIDE square brackets. Unbalanced brackets
    fall back to the first separator.
    """
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(depth - 1, 0)
        elif depth == 0 and rest.startswith(" - ", i):
            return rest[:i]
    if depth:
        head, sep, _ = rest.partition(" - ")
        return head if sep else rest
    return rest


def parse_log(text: str) -> RunResult:
    """Read one job log: the summary line, the failure list, the worker count."""
    lines = [strip_line(line) for line in text.splitlines()]
    result = RunResult(complete=False)
    failures: set[str] = set()
    in_short_summary = False

    for line in lines:
        marker = _MARKER_RE.match(line)
        if marker:
            key, value = marker["key"], marker["value"]
            if key == "nproc" and value.isdigit():
                result.nproc = int(value)
            elif key == "commit":
                result.commit = value
            elif key == "arm":
                result.arm = value
            continue

        checkout = _CHECKOUT_RE.match(line)
        if checkout and result.commit is None:
            result.commit = checkout["sha"]
            continue

        workers = _WORKERS_RE.match(line)
        if workers:
            result.workers = int(workers.group(2))
            continue

        if _SHORT_SUMMARY_HEADER_RE.match(line):
            in_short_summary = True
            continue

        summary = _SUMMARY_RE.match(line)
        if summary:
            result.complete = True
            result.seconds = float(summary["secs"])
            result.counts = {
                word: int(n) for n, word in _COUNT_RE.findall(summary["counts"])
            }
            in_short_summary = False
            continue

        if in_short_summary:
            outcome = _OUTCOME_LINE_RE.match(line)
            if outcome:
                failures.add(split_nodeid(outcome["rest"]))

    result.failures = sorted(failures)
    return result


def classify(check_run_name: str) -> tuple[str, str] | None:
    """``(suite, arm)`` for a check run this measurement reads, else None.

    The required jobs are serial runs of the same code and count as the
    ``serial-required`` arm; every ``xdist-*`` arm is an xdist run.
    """
    required = _REQUIRED_RE.match(check_run_name)
    if required:
        return required["suite"].lower(), "serial-required"
    measure = _MEASURE_RE.match(check_run_name)
    if measure:
        return measure["suite"].lower(), measure["arm"]
    return None


def is_xdist(arm: str) -> bool:
    return arm.startswith("xdist")


@dataclass
class SuiteDiff:
    """The failure-list comparison for one suite."""

    suite: str
    serial_failures: list[str]
    xdist_only_every_run: list[str]
    xdist_only_some_runs: list[str]
    serial_only: list[str]
    xdist_runs_agree: bool
    xdist_runs: int
    serial_runs: int
    incomplete: list[str]


def diff_suite(suite: str, runs: list[dict]) -> SuiteDiff:
    """Compare the failure sets of one suite's complete runs.

    ``runs`` are manifest entries carrying ``arm``, ``name`` and a parsed
    ``result``. An incomplete run is listed and excluded: its missing failures
    are unknown, not absent.
    """
    complete = [r for r in runs if r["result"]["complete"]]
    incomplete = sorted(r["label"] for r in runs if not r["result"]["complete"])
    serial = [set(r["result"]["failures"]) for r in complete if not is_xdist(r["arm"])]
    xdist = [set(r["result"]["failures"]) for r in complete if is_xdist(r["arm"])]

    serial_union = set().union(*serial) if serial else set()
    xdist_union = set().union(*xdist) if xdist else set()
    xdist_every = set.intersection(*xdist) if xdist else set()

    only_xdist = xdist_union - serial_union
    return SuiteDiff(
        suite=suite,
        serial_failures=sorted(serial_union),
        xdist_only_every_run=sorted(only_xdist & xdist_every),
        xdist_only_some_runs=sorted(only_xdist - xdist_every),
        serial_only=sorted(serial_union - xdist_union),
        xdist_runs_agree=all(s == xdist[0] for s in xdist),
        xdist_runs=len(xdist),
        serial_runs=len(serial),
        incomplete=incomplete,
    )


def _fmt_secs(secs: float | None) -> str:
    if secs is None:
        return "incomplete"
    whole = int(round(secs))
    return f"{whole // 60}m{whole % 60:02d}s ({secs:.1f}s)"


def _failed(result: dict) -> int:
    counts = result["counts"]
    return counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)


def render(entries: list[dict]) -> str:
    """Markdown: one row per run, then per-suite speedup and failure diff."""
    out = [
        "| Suite | Arm | Check run | Commit | nproc | Workers | pytest phase "
        "| Passed | Failed+Error |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    ordered = sorted(
        entries, key=lambda e: (e["suite"], is_xdist(e["arm"]), e["arm"], e["id"])
    )
    for e in ordered:
        r = e["result"]
        out.append(
            f"| {e['suite']} | {e['arm']} | {e['id']} | {(r['commit'] or e.get('head_sha') or '?')[:9]} "
            f"| {r['nproc'] or '-'} | {r['workers'] or 1} | {_fmt_secs(r['seconds'])} "
            f"| {r['counts'].get('passed', '-') if r['complete'] else '-'} "
            f"| {_failed(r) if r['complete'] else '-'} |"
        )

    for suite in sorted({e["suite"] for e in entries}):
        runs = [e for e in entries if e["suite"] == suite]
        serial_secs = [
            e["result"]["seconds"]
            for e in runs
            if not is_xdist(e["arm"]) and e["result"]["complete"]
        ]
        xdist_secs = [
            e["result"]["seconds"]
            for e in runs
            if is_xdist(e["arm"]) and e["result"]["complete"]
        ]
        out.append("")
        out.append(f"### {suite}")
        if serial_secs and xdist_secs:
            s, x = statistics.median(serial_secs), statistics.median(xdist_secs)
            out.append(
                f"- median pytest phase: serial {_fmt_secs(s)} over {len(serial_secs)} run(s), "
                f"xdist {_fmt_secs(x)} over {len(xdist_secs)} run(s) -- "
                f"**{s / x:.2f}x**, {100 * (s - x) / s:.0f}% less wall clock"
            )
        d = diff_suite(suite, runs)
        out.append(
            f"- xdist failure lists identical across {d.xdist_runs} complete run(s): "
            f"**{'yes' if d.xdist_runs_agree else 'NO'}**"
        )
        if d.incomplete:
            out.append(
                f"- incomplete (excluded, failures unknown): {', '.join(d.incomplete)}"
            )
        for title, items in (
            ("fails in serial run(s)", d.serial_failures),
            ("fails only under xdist, in EVERY xdist run", d.xdist_only_every_run),
            (
                "fails only under xdist, in SOME xdist runs (unstable)",
                d.xdist_only_some_runs,
            ),
            ("fails serially but in no xdist run", d.serial_only),
        ):
            out.append(f"- {title}: {len(items)}")
            out.extend(f"  - `{t}`" for t in items)
    return "\n".join(out) + "\n"


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def fetch(commit: str, out_dir: Path, repo: str = REPO) -> list[dict]:
    """Download every relevant check run's log on ``commit``; write a manifest.

    Asks for check runs BY COMMIT with ``filter=all``, so a re-run attempt is
    a further sample rather than silently replacing the first.
    """
    raw = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/commits/{commit}/check-runs?filter=all&per_page=100",
        "--jq",
        ".check_runs[] | {id, name, head_sha, status, conclusion, started_at}",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for line in raw.splitlines():
        run = json.loads(line)
        kind = classify(run["name"])
        if kind is None or run["status"] != "completed":
            continue
        suite, arm = kind
        path = out_dir / f"{run['id']}.log"
        path.write_text(_gh("api", f"repos/{repo}/actions/jobs/{run['id']}/logs"))
        entries.append(
            {
                **run,
                "suite": suite,
                "arm": arm,
                "label": f"{run['name']} #{run['id']}",
                "path": str(path),
            }
        )
    (out_dir / "manifest.json").write_text(json.dumps(entries, indent=2))
    return entries


def load_manifest(path: Path) -> list[dict]:
    entries = json.loads(path.read_text())
    for e in entries:
        e["result"] = asdict(parse_log(Path(e["path"]).read_text()))
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_parse = sub.add_parser("parse", help="parse one job log to JSON")
    p_parse.add_argument("log", type=Path)
    p_fetch = sub.add_parser("fetch", help="download the check-run logs on a commit")
    p_fetch.add_argument("commit")
    p_fetch.add_argument("--out", type=Path, required=True)
    p_fetch.add_argument("--repo", default=REPO)
    p_report = sub.add_parser("report", help="markdown table + failure diff")
    p_report.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "parse":
        print(json.dumps(asdict(parse_log(args.log.read_text())), indent=2))
    elif args.cmd == "fetch":
        entries = fetch(args.commit, args.out, args.repo)
        print(f"{len(entries)} check run(s) -> {args.out / 'manifest.json'}")
    else:
        sys.stdout.write(render(load_manifest(args.manifest)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

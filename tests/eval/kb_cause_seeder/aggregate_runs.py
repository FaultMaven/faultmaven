#!/usr/bin/env python3
"""Aggregate KB cause-seeder eval runs — held-rate over EXERCISED runs.

A single flag-ON run is noisy: an LLM-driven scenario only *exercises* a given
assertion when the model reaches the state it is about, so a lucky run can green
every gate while a sibling run breaches one. `run_seed_eval.py --json PATH`
records each assertion as HELD / BREACHED / NOT_EXERCISED; this averages a batch
of those JSON files so the enabling decision reads the honest picture:

    held-rate = HELD / (HELD + BREACHED)     # NOT_EXERCISED excluded

A soundness gate that is exercised-and-held on every run it fired is the bar;
an engagement measurement that is often NOT_EXERCISED tells you the scenario
rarely stressed the engine, not that the engine is safe.

Usage:
    python aggregate_runs.py run1.json run2.json ...
    python aggregate_runs.py results/*.json
"""

import json
import sys
from collections import defaultdict

HELD, BREACHED, NOT_EXERCISED = "HELD", "BREACHED", "NOT_EXERCISED"


def load(paths):
    runs = []
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        d["_path"] = p
        runs.append(d)
    return runs


def tally_mode(mode_runs):
    """Held/breached/not-exercised counts per assertion for one mode's runs.

    Pure — no I/O. Returns an ordered list (first-seen order preserved) of
    ``{name, kind, held, breached, not_exercised, exercised}`` where
    ``exercised = held + breached`` (the held-rate denominator; NOT-EXERCISED
    runs are excluded, which is the whole point)."""
    order = []
    kind = {}
    # Count by state defensively (a hand-authored recorded-run JSON could carry
    # an unexpected state string; degrade rather than KeyError the whole batch).
    tally = defaultdict(lambda: defaultdict(int))
    for r in mode_runs:
        for a in r.get("assertions", []):
            name = a["name"]
            if name not in tally:
                order.append(name)
            kind[name] = a.get("kind", "gate")
            tally[name][a.get("state", NOT_EXERCISED)] += 1
    rows = []
    for name in order:
        t = tally[name]
        rows.append(
            {
                "name": name,
                "kind": kind[name],
                "held": t.get(HELD, 0),
                "breached": t.get(BREACHED, 0),
                "not_exercised": t.get(NOT_EXERCISED, 0),
                "exercised": t.get(HELD, 0) + t.get(BREACHED, 0),
            }
        )
    return rows


def summarize(runs):
    """Group runs by mode and tally each. Pure — the reporting shape the CLI
    prints and the unit test asserts on."""
    by_mode = defaultdict(list)
    for r in runs:
        by_mode[r.get("metadata", {}).get("mode", "?")].append(r)
    out = {}
    for mode, mode_runs in by_mode.items():
        out[mode] = {
            "n_runs": len(mode_runs),
            "n_pass": sum(1 for r in mode_runs if r.get("result") == "pass"),
            "n_crash": sum(
                1
                for r in mode_runs
                if r.get("metadata", {}).get("crashed_at_turn") is not None
            ),
            "n_seeding": sum(
                1 for r in mode_runs if r.get("metadata", {}).get("seeding_observed")
            ),
            "assertions": tally_mode(mode_runs),
        }
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    runs = load(sys.argv[1:])

    commits = {r.get("metadata", {}).get("commit") for r in runs}
    providers = {r.get("metadata", {}).get("provider") for r in runs}
    flags = {r.get("metadata", {}).get("flag_env") for r in runs}
    print(
        f"runs={len(runs)}  commits={sorted(c for c in commits if c)}  "
        f"providers={sorted(p for p in providers if p)}  flag_env={sorted(f for f in flags if f)}"
    )
    if len(commits) > 1 or len(providers) > 1 or len(flags) > 1:
        print("  ⚠ heterogeneous batch (commit/provider/flag differ across runs)")

    summary = summarize(runs)
    for mode in sorted(summary):
        s = summary[mode]
        print(f"\n=== mode={mode}  ({s['n_runs']} runs) ===")
        print(
            f"run result: {s['n_pass']}/{s['n_runs']} pass (no breached gate)  "
            f"seeding_observed: {s['n_seeding']}/{s['n_runs']}  "
            f"crashed: {s['n_crash']}/{s['n_runs']}"
        )
        print("  assertion  (held / exercised)  [not-exercised]")
        for row in s["assertions"]:
            rate = f"{row['held']}/{row['exercised']}" if row["exercised"] else "0/0"
            flag = ""
            if row["kind"] == "gate" and row["breached"]:
                flag = "  <-- BREACHED"
            elif row["kind"] == "gate" and row["exercised"] == 0:
                flag = "  <-- never exercised"
            print(
                f"    [{row['kind'][:4]}] held {rate:>7}   n/ex {row['not_exercised']:>2}   "
                f"{row['name']}{flag}"
            )


if __name__ == "__main__":
    main()

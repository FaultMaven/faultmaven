"""Replay the stored corpus through fix verification, before and after #1906 2a.

#1906 Phase 2a binds every post-fix observation to the subject it names
(``rechecks`` → the problem's verifying rows or the cause pool), and #2145
dates a row by its data's upload turn. Every stored row predates 2a, so none
names anything: under 2a's enforcement (ruled 2026-10-31, Q1 (a)) no stored
case can resolve. That is the change, not a measurement, so two switches
isolate what the rules do:

- ``--mode unnamed-as-today``: a row that names nothing is read by its
  category, as before 2a (a symptom row observes the problem, a causal_absence
  row the cause). What remains is #2145's dating (ruling Q1's requirement).
  Today's persistence also needed two shared words with the problem frame; this
  switch counts every post-fix symptom row, so it is the stricter reading.
- ``--mode simulated-naming``: each case's verifying row is recorded by the
  verifying-turn fallback (exactly one symptom row written in the turn the
  symptom claim landed). Only rows observed after a RECORDED fix are named,
  as the model is asked to: a symptom_absence row names the verifying row,
  a causal_absence row the first row supporting the confirmation pool alone
  (ROOT-only, one cluster). Persistence rows stay unnamed, so the figure is
  an UPPER BOUND on what naming earns: a perfect model, no failure ever
  named. It shows whether the rules admit the systemd floor.

Per case it reports the two legs (cause rows, symptom rows), every fix window's
state (FIX_FAILED outcomes), readiness, and the fallback's outcome.

Not collected by pytest (no ``test_`` filename). Opens the corpus READ-ONLY
(``mode=ro``) and writes only the ``--out`` file. From the repo root::

    python tests/eval/fix_verification/replay_subjects.py --out after.json
    python tests/eval/fix_verification/replay_subjects.py \\
        --mode unnamed-as-today --out today.json
    python tests/eval/fix_verification/replay_subjects.py \\
        --mode simulated-naming --out named.json
    # "before": the same script, importing another checkout (origin/main)
    python tests/eval/fix_verification/replay_subjects.py \\
        --tree /path/to/main-checkout --out before.json
    python tests/eval/fix_verification/replay_subjects.py \\
        --report before.json after.json today.json named.json

Corpus override: ``FM_CASES_DB``.
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB = os.environ.get("FM_CASES_DB", str(REPO_ROOT / "data" / "faultmaven.db"))

#: The cases the plan names, beside the systemd floor corpus.
NAMED = {
    "case_f21d4c98d325": "#2145: dating removes the symptom leg",
    "case_d252bedf6642": "the window-0 face",
    "case_ff3ed2d2df16": "#2036 c1021-1 link shape",
}


def scenario(title: str) -> str:
    """The critique probe's scenario grouping, by title."""
    t = (title or "").lower()
    if re.search(r"systemd|execstart|billing|scrape target|unit", t):
        return "systemd"
    if re.search(r"503|gateway|rejected|user calls|tls", t):
        return "gateway"
    return "other"


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _corpus_index(path: str) -> list[dict]:
    """Case ids, titles, states and the evidence each turn added (the
    verifying-turn fallback reads it), straight from the read-only corpus."""
    with contextlib.closing(_ro(path)) as conn:
        conn.row_factory = sqlite3.Row
        out = []
        for r in conn.execute("select case_id, title, state, metadata from cases"):
            meta = json.loads(r["metadata"] or "{}")
            verifying_turns = [
                t
                for t in meta.get("turn_history") or []
                if "symptom_verified" in (t.get("milestones_completed") or [])
            ]
            out.append(
                {
                    "case_id": r["case_id"],
                    "title": r["title"] or "",
                    "state": r["state"],
                    "verifying_turn_rows": (
                        list(verifying_turns[0].get("evidence_added") or [])
                        if verifying_turns
                        else None
                    ),
                }
            )
        return out


async def _load_cases(path: str, ids: list[str]) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
        SQLiteCaseRepository,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    cases = {}
    async with async_sessionmaker(engine)() as session:
        repo = SQLiteCaseRepository(session)
        for case_id in ids:
            cases[case_id] = await repo.get(case_id)
    await engine.dispose()
    return cases


def _unnamed_as_today() -> None:
    """A row naming nothing is read by its category (the pre-2a proxy)."""
    from faultmaven.core.investigation import cause_assurance as ca
    from faultmaven.modules.case.contracts import EvidenceCategory

    bound = ca.SubjectIndex.subject

    def subject(self, row, windows=False):
        if (
            getattr(row, "rechecks_evidence_id", None) is None
            and getattr(row, "collected_by", None) != ca.ENGINE_EVIDENCE_AUTHOR
        ):
            category = getattr(row, "category", None)
            if category in (
                EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                EvidenceCategory.SYMPTOM_EVIDENCE,
            ):
                return ca.SUBJECT_PROBLEM
            if category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:
                return ca.SUBJECT_CAUSE
        return bound(self, row, windows)

    ca.SubjectIndex.subject = subject


def _fallback(case, verifying_turn_rows) -> str | None:
    """The verifying-turn fallback's outcome for a stored case."""
    from faultmaven.modules.case.contracts import EvidenceCategory

    if verifying_turn_rows is None:
        return None
    symptom = [
        e.evidence_id
        for e in case.evidence
        if e.evidence_id in set(verifying_turn_rows)
        and e.category == EvidenceCategory.SYMPTOM_EVIDENCE
    ]
    return symptom[0] if len(symptom) == 1 else None


def _simulate_naming(case, verifying: str | None) -> None:
    from faultmaven.core.investigation.cause_assurance import (
        ENGINE_EVIDENCE_AUTHOR,
        fix_application_turn,
        subject_index,
    )
    from faultmaven.modules.case.contracts import EvidenceCategory

    if case.problem_verification is not None and verifying:
        case.problem_verification.verifying_evidence_ids = [verifying]
    fix_turn = fix_application_turn(case)
    if fix_turn is None:
        return  # no recorded fix: nothing is a post-fix observation
    subjects = subject_index(case)
    _problem, cause = subjects.bindable_rows()
    support = cause[0].evidence_id if cause else None
    for row in case.evidence:
        if row.collected_by == ENGINE_EVIDENCE_AUTHOR:
            continue
        if subjects.observed(row) < fix_turn:
            continue
        if row.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE and verifying:
            row.record_rechecks(verifying, named=verifying)
        elif row.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE and support:
            row.record_rechecks(support, named=support)


def _measure(case) -> dict:
    from faultmaven.core.investigation.cause_assurance import (
        cause_elimination_rows,
        fix_windows,
        symptom_elimination_rows,
    )
    from faultmaven.core.investigation.terminal_transitions import (
        assess_resolution_readiness,
    )

    cause_rows = cause_elimination_rows(case)
    return {
        "legs": [len(cause_rows), len(symptom_elimination_rows(case, cause_rows))],
        "windows": [w.state for w in fix_windows(case)],
        "readiness": assess_resolution_readiness(case).verdict,
    }


def replay(tree: Path, mode: str) -> list[dict]:
    sys.path.insert(0, str(tree))
    import faultmaven

    assert Path(faultmaven.__file__).is_relative_to(
        tree
    ), f"imported faultmaven from {faultmaven.__file__}, not from {tree}"
    import importlib

    has_subjects = hasattr(
        importlib.import_module("faultmaven.core.investigation.cause_assurance"),
        "subject_index",
    )
    if mode != "after" and not has_subjects:
        raise SystemExit(f"--mode {mode} needs a tree with #1906 2a")
    if mode == "unnamed-as-today":
        _unnamed_as_today()
    index = _corpus_index(DB)
    cases = asyncio.run(_load_cases(DB, [r["case_id"] for r in index]))
    rows = []
    for entry in index:
        case = cases.get(entry["case_id"])
        if case is None:
            continue
        fallback = _fallback(case, entry["verifying_turn_rows"])
        if mode == "simulated-naming":
            _simulate_naming(case, fallback)
        rows.append(
            {
                "case_id": entry["case_id"],
                "scenario": scenario(entry["title"]),
                "state": entry["state"],
                "verified": entry["verifying_turn_rows"] is not None,
                "fallback": fallback,
                **_measure(case),
            }
        )
    label = mode if has_subjects else "main"
    return [{"label": label, **r} for r in rows]


def _cell(row: dict | None) -> str:
    if row is None:
        return "-"
    failed = sum(1 for s in row["windows"] if s == "failed")
    return f"{row['legs'][0]}/{row['legs'][1]} {row['readiness']}" + (
        f" F{failed}" if failed else ""
    )


def report(paths: list[str]) -> None:
    runs = []
    for p in paths:
        data = json.loads(Path(p).read_text())
        runs.append((data[0]["label"], {r["case_id"]: r for r in data}))
    labels = [label for label, _ in runs]
    print("Cell: cause rows / symptom rows, readiness, F<n> = FIX_FAILED windows.\n")
    print("| case | scenario | fallback | " + " | ".join(labels) + " |")
    print("|---|---|---|" + "---|" * len(labels))
    base = runs[0][1]
    shown = [
        cid
        for cid, r in sorted(base.items(), key=lambda kv: (kv[1]["scenario"], kv[0]))
        if r["scenario"] == "systemd" or cid in NAMED
    ]
    for cid in shown:
        r = base[cid]
        fb = r["fallback"] or ("none" if r["verified"] else "not verified")
        note = f" ({NAMED[cid]})" if cid in NAMED else ""
        cells = " | ".join(_cell(run.get(cid)) for _, run in runs)
        print(f"| `{cid}`{note} | {r['scenario']} | {fb} | {cells} |")
    print("\nCorpus summary (all cases):\n")
    print(
        "| run | cases | READY | with a cause leg | with both legs | FIX_FAILED windows |"
    )
    print("|---|---|---|---|---|---|")
    for label, run in runs:
        rows = list(run.values())
        print(
            f"| {label} | {len(rows)} | "
            f"{sum(r['readiness'] == 'ready' for r in rows)} | "
            f"{sum(r['legs'][0] > 0 for r in rows)} | "
            f"{sum(r['legs'][1] > 0 for r in rows)} | "
            f"{sum(s == 'failed' for r in rows for s in r['windows'])} |"
        )
    verified = [r for r in base.values() if r["verified"]]
    print(
        f"\nFallback: {sum(r['fallback'] is not None for r in verified)} of "
        f"{len(verified)} verified cases record a verifying row; none for "
        + ", ".join(f"`{r['case_id']}`" for r in verified if r["fallback"] is None)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--mode",
        choices=["after", "unnamed-as-today", "simulated-naming"],
        default="after",
    )
    parser.add_argument("--out")
    parser.add_argument("--report", nargs="+")
    args = parser.parse_args()
    if args.report:
        report(args.report)
        return
    rows = replay(args.tree.resolve(), args.mode)
    Path(args.out).write_text(json.dumps(rows, indent=1))
    print(f"{len(rows)} cases -> {args.out}")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # a lingering aiosqlite thread can hold the interpreter

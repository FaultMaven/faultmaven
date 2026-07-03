#!/usr/bin/env python3
"""Print the both-arms grounding baseline over the live case store (Phase 0b/0d).

Loads every hydrated Case from the store, runs
``grounding_metrics.compute_grounding_baseline`` for both ratified projections
(``all`` and ``terminal``), and prints the counts. This is the "before" of the
Part-A delta: pre-Part-A it should read ``grounded_roots``/``runbook_arm``/
``deductive_arm`` all == 0 over a NON-zero denominator (cases that retrieved a
matchable runbook). That trustworthy zero is the acceptance baseline — and the
same read is the acceptance check that would have caught #593's unwired
deductive arm.

Denominator note (R3): the population is cases with ``runbook_retrieved`` set
(the pre-verdict retrieval marker written by the runbook Cause matcher), NOT the
verdict-gated ``differential_runbook_ids``. Cases created before that marker
shipped carry ``runbook_retrieved == False`` and are correctly excluded — so a
population of 0 here means "no marked cases yet", a mis-scope signal, not a
grounding reading. Drive a handful of matching-runbook cases through the matcher
first to populate the marker.

Read-only. Usage (from the repo root):  python scripts/grounding_baseline.py
Override the store with ``DATABASE_URL=sqlite+aiosqlite:////abs/path/faultmaven.db``
when running from elsewhere (e.g. a worktree) — otherwise the default resolves the
DB relative to the current directory and a wrong cwd silently reads an empty store.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow running from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faultmaven.core.investigation.grounding_metrics import (  # noqa: E402
    GroundingBaseline,
    compute_grounding_baseline,
)
from faultmaven.infrastructure.persistence.database import get_db_session  # noqa: E402
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (  # noqa: E402
    SQLiteCaseRepository,
)

_PAGE = 200


async def _load_all_cases(session) -> list:
    """Page every case id out of the store, then FULLY hydrate each via ``get``.

    ``list`` returns a projection WITHOUT the causal graph (``case.causal_nodes ==
    {}``), so counting grounded roots over it reads a false zero — the counter is
    drift-locked and correct, it was just being fed under-hydrated cases. ``get``
    hydrates the full causal graph (nodes + evidence links), which is what
    ``count_grounded_roots`` needs.

    Re-fetching per case is an N+1 fan-out, accepted here because this is an
    occasional offline diagnostic at baseline scale. If it ever needs to scale, the
    efficient path is a bulk causal-graph loader (mirroring the repo's
    ``_load_evidence_for_cases_bulk``) that hydrates the ``list`` projection in
    place — NOT bloating ``list`` itself, which must stay a listing view.
    """
    repo = SQLiteCaseRepository(session)
    case_ids: list[str] = []
    offset = 0
    while True:
        page, total = await repo.list(limit=_PAGE, offset=offset)
        case_ids.extend(c.case_id for c in page)
        # Advance by the WINDOW, not len(page): ``list`` drops an undeserializable
        # row while ``total`` (COUNT(*)) still counts it, so a len-based step would
        # re-page that window's tail (duplicate ids) and never stop on an
        # all-dropped-but-not-final page. Stepping by _PAGE over [0, total) visits
        # every row position exactly once.
        offset += _PAGE
        if offset >= total:
            break
    # Dedup defensively: ``list`` orders by updated_at with no stable tiebreaker, so
    # a tie straddling a page boundary could repeat an id — which must not
    # double-count the baseline.
    case_ids = list(dict.fromkeys(case_ids))
    cases: list = []
    for case_id in case_ids:
        # A listed id should always resolve; skip a race-deleted one defensively.
        case = await repo.get(case_id)
        if case is not None:
            cases.append(case)
    return cases


def _print_baseline(label: str, b: GroundingBaseline) -> None:
    print(f"\n  [{label}]  (scope={b.scope})")
    print(f"    population (retrieved a matchable runbook) : {b.population}")
    print(f"    cases_grounded                             : {b.cases_grounded}")
    print(f"    grounded_rate                              : {b.grounded_rate:.3f}")
    print(f"    grounded_roots (union)                     : {b.grounded_roots}")
    print(f"      runbook_arm_roots                        : {b.runbook_arm_roots}")
    print(f"      deductive_arm_roots                      : {b.deductive_arm_roots}")
    print(f"    runbook_links_fired (leading indicator)    : {b.runbook_links_fired}")


async def _main() -> None:
    async with get_db_session() as session:
        cases = await _load_all_cases(session)

    marked = sum(1 for c in cases if c.runbook_retrieved)
    print("=" * 68)
    print("  GROUNDING BASELINE  (Phase 0b — pre-Part-A read)")
    print("=" * 68)
    print(f"  total cases in store        : {len(cases)}")
    print(f"  cases with runbook_retrieved: {marked}")

    # Materialize once above; pass the list to both scopes (compute_grounding_baseline
    # consumes its argument exactly once, so a shared list — not a generator — is safe).
    _print_baseline("all-INVESTIGATING", compute_grounding_baseline(cases, scope="all"))
    _print_baseline(
        "terminal-only", compute_grounding_baseline(cases, scope="terminal")
    )

    if marked == 0:
        print(
            "\n  NOTE: population is 0 (no case carries runbook_retrieved yet). This is a\n"
            "  mis-scope signal, not a grounding reading — drive matching-runbook cases\n"
            "  through the matcher (ENABLE_RUNBOOK_CAUSE_MATCHER=true) to populate it."
        )
    print()


if __name__ == "__main__":
    asyncio.run(_main())

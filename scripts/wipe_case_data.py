#!/usr/bin/env python3
"""Wipe all case-derived data from the local SQLite DB, preserving KB
runbooks (knowledge_items), auth/user/org tables, and config.

Pre-production cleanup utility for eval baselines. The server MUST be
stopped before running (WAL + a live connection = corruption risk).

Child-first delete order with foreign_keys explicitly OFF — direct
sqlite3 connections default to FK off, so cascade won't fire; order is
authoritative here.
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "faultmaven.db"

# Child -> parent order. cases last.
CASE_TABLES = [
    "agent_tool_calls",
    "agent_executions",
    "evidence_need_fulfillment",
    "evidence_needs",
    "hypothesis_evidence",
    "hypotheses",
    "solutions",
    "evidence",
    "uploaded_files",
    "reports",
    "conversion_drafts",
    "conversion_jobs",
    "case_actions",
    "case_checkpoints",
    "case_entities",
    "case_messages",
    "case_tags",
    "investigation_sessions",
    "knowledge_suggestions",  # case-bound (case_id FK); not a runbook
    "cases",
]

PRESERVE_CHECK = ["knowledge_items", "users", "organizations", "roles"]


def main(dry_run: bool) -> None:
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=OFF")

    print(f"DB: {DB}")
    print("\nBEFORE:")
    before = {}
    for t in CASE_TABLES + PRESERVE_CHECK:
        before[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {before[t]}")

    if dry_run:
        print("\n[DRY RUN] no rows deleted. Re-run with --commit to wipe.")
        conn.close()
        return

    deleted = {}
    for t in CASE_TABLES:
        cur = conn.execute(f"DELETE FROM {t}")
        deleted[t] = cur.rowcount
    conn.commit()

    print("\nDELETED:")
    for t in CASE_TABLES:
        print(f"  {t}: {deleted[t]}")

    print("\nAFTER (case tables should be 0; preserved tables unchanged):")
    for t in CASE_TABLES + PRESERVE_CHECK:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n}")

    # Reclaim space + reset WAL.
    conn.execute("VACUUM")
    conn.close()
    print("\nVACUUM done. Case data wiped; KB + auth preserved.")


if __name__ == "__main__":
    main(dry_run="--commit" not in sys.argv)

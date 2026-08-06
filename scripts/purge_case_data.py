#!/usr/bin/env python3
"""Purge all case/investigation data while preserving users and knowledge base.

Deletes:
  - All cases (FK CASCADE cleans evidence, hypotheses, solutions, messages,
    uploaded_files, actions, tags, checkpoints, entities, sessions,
    hypothesis_evidence, reports)
  - All investigation_sessions
  - Evidence files on disk  (data/evidence/)
  - Evidence vector embeddings (data/chroma-evidence/)

Preserves:
  - Users, enterprises, organizations, teams, roles, permissions
  - Knowledge items, knowledge suggestions, conversion jobs/drafts
  - Knowledge files on disk  (data/knowledge/)
  - KB vector embeddings      (data/chroma-kb/)
  - LLM config overrides

Usage:
    source .venv/bin/activate
    python scripts/purge_case_data.py           # dry-run (default)
    python scripts/purge_case_data.py --confirm  # actually delete
"""

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Project root: one level up from scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "faultmaven.db"
EVIDENCE_DIR = PROJECT_ROOT / "data" / "evidence"
CHROMA_EVIDENCE_DIR = PROJECT_ROOT / "data" / "chroma-evidence"


def get_counts(conn: sqlite3.Connection) -> dict:
    """Snapshot row counts for case-domain and preserved tables."""
    tables = {
        # Case domain (will be purged)
        "cases": "SELECT COUNT(*) FROM cases",
        "evidence": "SELECT COUNT(*) FROM evidence",
        "hypotheses": "SELECT COUNT(*) FROM hypotheses",
        "solutions": "SELECT COUNT(*) FROM solutions",
        "case_messages": "SELECT COUNT(*) FROM case_messages",
        "uploaded_files": "SELECT COUNT(*) FROM uploaded_files",
        "case_actions": "SELECT COUNT(*) FROM case_actions",
        "case_tags": "SELECT COUNT(*) FROM case_tags",
        "case_checkpoints": "SELECT COUNT(*) FROM case_checkpoints",
        "case_entities": "SELECT COUNT(*) FROM case_entities",
        "hypothesis_evidence": "SELECT COUNT(*) FROM hypothesis_evidence",
        "investigation_sessions": "SELECT COUNT(*) FROM investigation_sessions",
        "reports": "SELECT COUNT(*) FROM reports",
        # Preserved
        "knowledge_items": "SELECT COUNT(*) FROM knowledge_items",
        "knowledge_suggestions": "SELECT COUNT(*) FROM knowledge_suggestions",
        "users": "SELECT COUNT(*) FROM users",
    }
    counts = {}
    for name, sql in tables.items():
        try:
            counts[name] = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            counts[name] = "N/A"
    return counts


def dir_size_mb(path: Path) -> str:
    """Human-readable directory size."""
    if not path.exists():
        return "0 MB (not found)"
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / (1024 * 1024):.1f} MB"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete data. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()
    dry_run = not args.confirm

    print("=" * 60)
    print("  FaultMaven — Purge Case Data")
    print("=" * 60)
    print()

    if not DB_PATH.exists():
        print(f"✗ Database not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")

    # --- Before snapshot ---
    before = get_counts(conn)

    print("Current state:")
    print(f"  Cases:                  {before['cases']}")
    print(f"  Evidence rows:          {before['evidence']}")
    print(f"  Hypotheses:             {before['hypotheses']}")
    print(f"  Solutions:              {before['solutions']}")
    print(f"  Messages:               {before['case_messages']}")
    print(f"  Uploaded files (DB):    {before['uploaded_files']}")
    print(f"  Investigation sessions: {before['investigation_sessions']}")
    print(f"  Reports:                {before['reports']}")
    print()
    print(f"  Evidence files on disk:     {dir_size_mb(EVIDENCE_DIR)}")
    print(f"  Chroma evidence vectors:    {dir_size_mb(CHROMA_EVIDENCE_DIR)}")
    print()
    print("Preserved (untouched):")
    print(f"  Knowledge items:        {before['knowledge_items']}")
    print(f"  Knowledge suggestions:  {before['knowledge_suggestions']}")
    print(f"  Users:                  {before['users']}")
    print()

    if before["cases"] == 0 and not EVIDENCE_DIR.exists() and not CHROMA_EVIDENCE_DIR.exists():
        print("✓ Nothing to purge — already clean.")
        conn.close()
        return 0

    if dry_run:
        print("── DRY RUN ──  No data will be deleted.")
        print("Run with --confirm to actually purge.")
        conn.close()
        return 0

    # --- Purge ---
    print("Purging case data...")
    print()

    # 1. Database (CASCADE handles all child tables)
    conn.execute("DELETE FROM cases")
    conn.execute("DELETE FROM investigation_sessions")
    conn.commit()
    print("  ✓ Database: cases + child tables purged")

    # 2. Evidence files on disk
    if EVIDENCE_DIR.exists():
        for child in EVIDENCE_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        print("  ✓ Filesystem: data/evidence/ contents removed")
    else:
        print("  – Filesystem: data/evidence/ not found (skip)")

    # 3. ChromaDB evidence vectors
    if CHROMA_EVIDENCE_DIR.exists():
        shutil.rmtree(CHROMA_EVIDENCE_DIR)
        # Recreate empty dir so bootstrap doesn't warn
        CHROMA_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        print("  ✓ Filesystem: data/chroma-evidence/ reset")
    else:
        print("  – Filesystem: data/chroma-evidence/ not found (skip)")

    # --- After snapshot ---
    after = get_counts(conn)
    conn.close()

    print()
    print("After purge:")
    print(f"  Cases:              {after['cases']}")
    print(f"  Knowledge items:    {after['knowledge_items']}  (preserved ✓)")
    print(f"  Users:              {after['users']}  (preserved ✓)")
    print()
    print("✓ Purge complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

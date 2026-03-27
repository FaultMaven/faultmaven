#!/usr/bin/env python3
"""
Backfill Missing closed_at Timestamps in Cases Table

This script addresses a critical bug where closed cases may not have proper
closed_at timestamps in their metadata. This causes incorrect cleanup behavior
where cases are aged based on updated_at instead of closed_at.

The script:
1. Finds all closed/resolved cases missing closed_at in metadata
2. Sets closed_at = updated_at (best approximation)
3. Logs all backfilled cases for audit trail

Usage:
    # Dry-run mode (shows what would be changed, no actual changes)
    python scripts/backfill_closed_at_timestamps.py --dry-run

    # Apply changes
    python scripts/backfill_closed_at_timestamps.py

    # Specific database
    python scripts/backfill_closed_at_timestamps.py --database cases

Exit Codes:
    0: Success (backfill complete or no missing timestamps)
    1: Missing timestamps found (dry-run mode)
    2: Error occurred
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


async def get_cases_missing_closed_at(session: AsyncSession) -> List[Dict[str, Any]]:
    """
    Find all closed/resolved cases missing closed_at in metadata.

    Args:
        session: Database session

    Returns:
        List of cases with case_id, status, updated_at, and metadata
    """
    # Query for closed/resolved cases
    # Check if metadata is NULL or doesn't contain closed_at
    query = text("""
        SELECT
            case_id,
            status,
            updated_at,
            case_metadata
        FROM cases
        WHERE status IN ('closed', 'resolved')
        AND (
            case_metadata IS NULL
            OR case_metadata::jsonb -> 'closed_at' IS NULL
        )
        ORDER BY updated_at ASC
    """)

    result = await session.execute(query)
    rows = result.fetchall()

    cases = []
    for row in rows:
        # Parse JSONB metadata
        metadata = json.loads(row[3]) if row[3] else {}

        cases.append({
            "case_id": row[0],
            "status": row[1],
            "updated_at": row[2],
            "metadata": metadata,
        })

    return cases


async def backfill_case_closed_at(
    session: AsyncSession,
    case_id: str,
    closed_at: datetime,
    dry_run: bool = False
) -> bool:
    """
    Backfill closed_at timestamp for a single case.

    Args:
        session: Database session
        case_id: Case ID to update
        closed_at: Timestamp to set as closed_at
        dry_run: If True, don't actually make changes

    Returns:
        True if updated, False if skipped
    """
    if dry_run:
        return True

    # Update metadata with closed_at timestamp
    # Use PostgreSQL JSONB operators to update nested field
    query = text("""
        UPDATE cases
        SET case_metadata = COALESCE(case_metadata, '{}'::jsonb) || jsonb_build_object('closed_at', :closed_at::text)
        WHERE case_id = :case_id
    """)

    await session.execute(
        query,
        {
            "case_id": case_id,
            "closed_at": closed_at.isoformat(),
        }
    )

    return True


def get_database_url(db_option: str = None) -> str:
    """Get database URL from environment variables."""
    if db_option == "auth":
        url = os.getenv("AUTH_DB_URL")
        if url:
            return url

        host = os.getenv("AUTH_DB_HOST", "localhost")
        port = os.getenv("AUTH_DB_PORT", "5432")
        name = os.getenv("AUTH_DB_NAME", "auth_db")
        user = os.getenv("AUTH_DB_USER", "postgres")
        password = os.getenv("AUTH_DB_PASSWORD", "")
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"

    elif db_option == "cases":
        url = os.getenv("CASES_DB_URL")
        if url:
            return url

        host = os.getenv("CASES_DB_HOST", "localhost")
        port = os.getenv("CASES_DB_PORT", "5432")
        name = os.getenv("CASES_DB_NAME", "cases_db")
        user = os.getenv("CASES_DB_USER", "postgres")
        password = os.getenv("CASES_DB_PASSWORD", "")
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"

    url = os.getenv("DATABASE_URL")
    if url:
        return url

    sqlite_path = project_root / "faultmaven.db"
    return f"sqlite+aiosqlite:///{sqlite_path}"


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill missing closed_at timestamps in cases table"
    )
    parser.add_argument(
        "--database",
        choices=["auth", "cases"],
        help="Specific database to check (typically 'cases')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without making actual changes",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="backfill_closed_at_audit.log",
        help="Path to audit log file (default: backfill_closed_at_audit.log)",
    )
    args = parser.parse_args()

    try:
        database_url = get_database_url(args.database)
        engine = create_async_engine(database_url, echo=False)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Open audit log file
        log_path = Path(args.log_file)
        log_file = open(log_path, "a", encoding="utf-8")

        try:
            async with async_session() as session:
                print(f"Checking database: {database_url.split('@')[-1] if '@' in database_url else database_url}")
                if args.dry_run:
                    print("MODE: DRY-RUN (no actual changes will be made)")
                print()

                # Find cases missing closed_at
                cases = await get_cases_missing_closed_at(session)

                if not cases:
                    print("✓ No cases missing closed_at timestamps.")
                    print("  All closed/resolved cases have proper timestamps.")
                    return 0

                print(f"Found {len(cases)} cases missing closed_at timestamps")
                print("=" * 80)

                # Log header
                timestamp = datetime.now(timezone.utc).isoformat()
                log_file.write(f"\n{'=' * 80}\n")
                log_file.write(f"Backfill Run: {timestamp}\n")
                log_file.write(f"Mode: {'DRY-RUN' if args.dry_run else 'APPLY'}\n")
                log_file.write(f"Cases Found: {len(cases)}\n")
                log_file.write(f"{'=' * 80}\n\n")

                # Backfill each case
                backfilled_count = 0
                for i, case in enumerate(cases, 1):
                    case_id = case["case_id"]
                    status = case["status"]
                    updated_at = case["updated_at"]

                    # Use updated_at as best approximation for closed_at
                    closed_at = updated_at

                    print(f"[{i}/{len(cases)}] Case: {case_id}")
                    print(f"  Status: {status}")
                    print(f"  Updated At: {updated_at}")
                    print(f"  Setting closed_at: {closed_at}")

                    # Log to audit file
                    log_file.write(f"Case ID: {case_id}\n")
                    log_file.write(f"  Status: {status}\n")
                    log_file.write(f"  Updated At: {updated_at}\n")
                    log_file.write(f"  Backfilled closed_at: {closed_at}\n\n")

                    # Backfill
                    success = await backfill_case_closed_at(
                        session, case_id, closed_at, args.dry_run
                    )

                    if success:
                        backfilled_count += 1
                        if not args.dry_run:
                            print(f"  ✓ Backfilled")
                        else:
                            print(f"  [DRY-RUN] Would backfill")

                    print("-" * 80)

                # Commit transaction
                if not args.dry_run:
                    await session.commit()
                    print(f"\n✓ Successfully backfilled {backfilled_count} cases")
                else:
                    print(f"\n[DRY-RUN] Would backfill {backfilled_count} cases")

                print(f"\nAudit log written to: {log_path.absolute()}")

                if args.dry_run:
                    print("\nRun without --dry-run to apply changes:")
                    print("  python scripts/backfill_closed_at_timestamps.py")
                    return 1
                else:
                    print("\nNext steps:")
                    print("  1. Review audit log for backfilled cases")
                    print("  2. Run tests to verify cleanup behavior:")
                    print("     pytest tests/unit/infrastructure/persistence/test_database_case_repository.py -v")
                    return 0

        finally:
            log_file.close()
            await engine.dispose()

    except Exception as e:
        print(f"\n✗ Error backfilling timestamps: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

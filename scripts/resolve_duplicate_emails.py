#!/usr/bin/env python3
"""
Resolve Duplicate Email Addresses in Users Table

This script resolves duplicate email addresses by either:
1. Keeping the oldest user and soft-deleting the rest (default)
2. Interactive mode: Let admin choose which user to keep

Usage:
    # Dry-run mode (shows what would be changed, no actual changes)
    python scripts/resolve_duplicate_emails.py --dry-run

    # Automatic mode (keeps oldest, soft-deletes rest)
    python scripts/resolve_duplicate_emails.py --auto

    # Interactive mode (choose which to keep)
    python scripts/resolve_duplicate_emails.py --interactive

Exit Codes:
    0: Success (duplicates resolved or none found)
    1: Duplicates found but not resolved (dry-run mode)
    2: Error occurred
"""
import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


async def get_duplicates(session: AsyncSession) -> list[dict]:
    """Get all duplicate email groups."""
    query = text(
        """
        SELECT
            LOWER(email) as normalized_email,
            COUNT(*) as count,
            array_agg(user_id ORDER BY created_at ASC) as user_ids,
            array_agg(email ORDER BY created_at ASC) as emails,
            array_agg(username ORDER BY created_at ASC) as usernames,
            array_agg(created_at ORDER BY created_at ASC) as created_dates,
            array_agg(is_active ORDER BY created_at ASC) as is_active_flags
        FROM users
        WHERE deleted_at IS NULL
        GROUP BY LOWER(email)
        HAVING COUNT(*) > 1
        ORDER BY LOWER(email)
    """
    )

    result = await session.execute(query)
    rows = result.fetchall()

    duplicates = []
    for row in rows:
        duplicates.append(
            {
                "normalized_email": row[0],
                "count": row[1],
                "user_ids": row[2],
                "emails": row[3],
                "usernames": row[4],
                "created_dates": row[5],
                "is_active_flags": row[6],
            }
        )

    return duplicates


async def soft_delete_users(
    session: AsyncSession, user_ids: list[str], dry_run: bool = False
) -> int:
    """
    Soft-delete users by setting deleted_at timestamp.

    Args:
        session: Database session
        user_ids: List of user IDs to soft-delete
        dry_run: If True, don't actually make changes

    Returns:
        Number of users soft-deleted
    """
    if not user_ids:
        return 0

    if dry_run:
        print(f"  [DRY-RUN] Would soft-delete {len(user_ids)} users: {user_ids}")
        return len(user_ids)

    now = datetime.now(UTC)
    query = text(
        """
        UPDATE users
        SET deleted_at = :deleted_at, updated_at = :updated_at
        WHERE user_id = ANY(:user_ids)
        AND deleted_at IS NULL
    """
    )

    result = await session.execute(
        query, {"deleted_at": now, "updated_at": now, "user_ids": user_ids}
    )
    await session.commit()

    return result.rowcount


def print_duplicate_group(group: dict, index: int) -> None:
    """Print information about a duplicate group."""
    print(f"\nDuplicate Group #{index}:")
    print(f"  Email: {group['normalized_email']}")
    print(f"  Total Users: {group['count']}")
    print("\n  Users:")

    for i in range(len(group["user_ids"])):
        status = "ACTIVE" if group["is_active_flags"][i] else "INACTIVE"
        oldest_marker = " (OLDEST)" if i == 0 else ""
        print(f"    [{i + 1}] {status}{oldest_marker}")
        print(f"        User ID: {group['user_ids'][i]}")
        print(f"        Email: {group['emails'][i]}")
        print(f"        Username: {group['usernames'][i]}")
        print(f"        Created: {group['created_dates'][i]}")
        print()


async def resolve_auto(
    session: AsyncSession, duplicates: list[dict], dry_run: bool = False
) -> int:
    """
    Automatically resolve duplicates by keeping oldest user.

    Args:
        session: Database session
        duplicates: List of duplicate groups
        dry_run: If True, don't actually make changes

    Returns:
        Total number of users soft-deleted
    """
    total_deleted = 0

    for i, group in enumerate(duplicates, 1):
        print_duplicate_group(group, i)

        # Keep oldest (index 0), soft-delete the rest
        keep_user_id = group["user_ids"][0]
        delete_user_ids = group["user_ids"][1:]

        print(f"  Decision: Keep user {keep_user_id} (oldest)")
        print(f"           Soft-delete: {delete_user_ids}")

        deleted = await soft_delete_users(session, delete_user_ids, dry_run)
        total_deleted += deleted

        if not dry_run:
            print(f"  ✓ Soft-deleted {deleted} users")
        print("-" * 80)

    return total_deleted


async def resolve_interactive(
    session: AsyncSession, duplicates: list[dict], dry_run: bool = False
) -> int:
    """
    Interactively resolve duplicates.

    Args:
        session: Database session
        duplicates: List of duplicate groups
        dry_run: If True, don't actually make changes

    Returns:
        Total number of users soft-deleted
    """
    total_deleted = 0

    for i, group in enumerate(duplicates, 1):
        print_duplicate_group(group, i)

        # Prompt user to choose which to keep
        while True:
            choice = input(
                f"  Which user to KEEP? [1-{group['count']} or 's' to skip]: "
            ).strip()

            if choice.lower() == "s":
                print("  Skipped.")
                break

            try:
                choice_idx = int(choice) - 1
                if 0 <= choice_idx < group["count"]:
                    keep_user_id = group["user_ids"][choice_idx]
                    delete_user_ids = [
                        uid
                        for j, uid in enumerate(group["user_ids"])
                        if j != choice_idx
                    ]

                    print(f"  Decision: Keep user {keep_user_id}")
                    print(f"           Soft-delete: {delete_user_ids}")

                    deleted = await soft_delete_users(session, delete_user_ids, dry_run)
                    total_deleted += deleted

                    if not dry_run:
                        print(f"  ✓ Soft-deleted {deleted} users")
                    break
                else:
                    print(f"  Invalid choice. Enter 1-{group['count']} or 's'.")
            except ValueError:
                print(f"  Invalid input. Enter 1-{group['count']} or 's'.")

        print("-" * 80)

    return total_deleted


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
        description="Resolve duplicate email addresses in users table"
    )
    parser.add_argument(
        "--database",
        choices=["auth", "cases"],
        help="Specific database to check (auth or cases)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without making actual changes",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Automatically keep oldest user and soft-delete rest",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactively choose which user to keep for each duplicate",
    )
    args = parser.parse_args()

    # Validate arguments
    if not (args.dry_run or args.auto or args.interactive):
        print(
            "Error: Must specify --dry-run, --auto, or --interactive", file=sys.stderr
        )
        return 2

    if sum([args.auto, args.interactive]) > 1:
        print("Error: Cannot specify both --auto and --interactive", file=sys.stderr)
        return 2

    try:
        database_url = get_database_url(args.database)
        engine = create_async_engine(database_url, echo=False)
        async_session = sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        try:
            async with async_session() as session:
                print(
                    f"Resolving duplicates in: {database_url.split('@')[-1] if '@' in database_url else database_url}"
                )
                if args.dry_run:
                    print("MODE: DRY-RUN (no actual changes will be made)")
                print()

                duplicates = await get_duplicates(session)

                if not duplicates:
                    print("✓ No duplicate email addresses found.")
                    return 0

                print(f"Found {len(duplicates)} duplicate email groups")
                print("=" * 80)

                if args.auto or args.dry_run:
                    total_deleted = await resolve_auto(
                        session, duplicates, args.dry_run
                    )
                else:  # interactive
                    total_deleted = await resolve_interactive(
                        session, duplicates, args.dry_run
                    )

                print("\n" + "=" * 80)
                if args.dry_run:
                    print(f"DRY-RUN: Would soft-delete {total_deleted} users")
                    print("\nRun without --dry-run to apply changes:")
                    print("  python scripts/resolve_duplicate_emails.py --auto")
                    return 1
                else:
                    print(f"✓ Successfully soft-deleted {total_deleted} users")
                    print("\nNext steps:")
                    print("  1. Run scripts/check_duplicate_emails.py to verify")
                    print("  2. Apply email uniqueness constraint migration:")
                    print("     alembic upgrade head")
                    return 0

        finally:
            await engine.dispose()

    except Exception as e:
        print(f"\n✗ Error resolving duplicates: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
"""
Check for Duplicate Email Addresses in Users Table

This script checks for duplicate email addresses (case-insensitive) in the users table.
It's used before applying the email uniqueness constraint migration to identify
any data conflicts that need to be resolved.

Usage:
    python scripts/check_duplicate_emails.py [--database auth|cases]

Exit Codes:
    0: No duplicates found
    1: Duplicates found (prints details)
    2: Error occurred
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


async def check_duplicates(database_url: str) -> tuple[bool, list[dict]]:
    """
    Check for duplicate email addresses in users table.

    Args:
        database_url: Database connection URL

    Returns:
        Tuple of (has_duplicates, duplicate_records)
    """
    engine = create_async_engine(database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as session:
            # Query for duplicate emails (case-insensitive)
            query = text(
                """
                SELECT
                    LOWER(email) as normalized_email,
                    COUNT(*) as count,
                    array_agg(user_id) as user_ids,
                    array_agg(email) as emails,
                    array_agg(username) as usernames,
                    array_agg(created_at) as created_dates
                FROM users
                GROUP BY LOWER(email)
                HAVING COUNT(*) > 1
                ORDER BY COUNT(*) DESC, LOWER(email)
            """
            )

            result = await session.execute(query)
            rows = result.fetchall()

            if not rows:
                return False, []

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
                    }
                )

            return True, duplicates

    finally:
        await engine.dispose()


def print_duplicates(duplicates: list[dict]) -> None:
    """Print duplicate email report."""
    print("\n" + "=" * 80)
    print("DUPLICATE EMAIL ADDRESSES FOUND")
    print("=" * 80)
    print(f"\nTotal duplicate email groups: {len(duplicates)}\n")

    for i, dup in enumerate(duplicates, 1):
        print(f"Duplicate Group #{i}:")
        print(f"  Email: {dup['normalized_email']}")
        print(f"  Count: {dup['count']}")
        print("\n  Conflicting Users:")

        for j in range(len(dup["user_ids"])):
            print(f"    [{j + 1}] User ID: {dup['user_ids'][j]}")
            print(f"        Email: {dup['emails'][j]}")
            print(f"        Username: {dup['usernames'][j]}")
            print(f"        Created: {dup['created_dates'][j]}")
            print()

        print("-" * 80)

    print("\nACTION REQUIRED:")
    print("1. Review duplicate users and determine which to keep")
    print("2. Run scripts/resolve_duplicate_emails.py to merge/delete duplicates")
    print("3. Then apply the email uniqueness constraint migration")
    print()


def get_database_url(db_option: str = None) -> str:
    """
    Get database URL from environment variables.

    Args:
        db_option: 'auth' or 'cases' to select specific database

    Returns:
        Database connection URL
    """
    if db_option == "auth":
        url = os.getenv("AUTH_DB_URL")
        if url:
            return url

        # Build from components
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

        # Build from components
        host = os.getenv("CASES_DB_HOST", "localhost")
        port = os.getenv("CASES_DB_PORT", "5432")
        name = os.getenv("CASES_DB_NAME", "cases_db")
        user = os.getenv("CASES_DB_USER", "postgres")
        password = os.getenv("CASES_DB_PASSWORD", "")
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"

    # Default to DATABASE_URL
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    # Default to SQLite for development
    sqlite_path = project_root / "faultmaven.db"
    return f"sqlite+aiosqlite:///{sqlite_path}"


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Check for duplicate email addresses in users table"
    )
    parser.add_argument(
        "--database",
        choices=["auth", "cases"],
        help="Specific database to check (auth or cases)",
    )
    args = parser.parse_args()

    try:
        database_url = get_database_url(args.database)
        print(
            f"Checking database: {database_url.split('@')[-1] if '@' in database_url else database_url}"
        )
        print("Scanning for duplicate emails...\n")

        has_duplicates, duplicates = await check_duplicates(database_url)

        if has_duplicates:
            print_duplicates(duplicates)
            return 1
        else:
            print("✓ No duplicate email addresses found.")
            print("  Database is ready for email uniqueness constraint migration.")
            return 0

    except Exception as e:
        print(f"\n✗ Error checking for duplicates: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

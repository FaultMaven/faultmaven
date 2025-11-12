#!/usr/bin/env python3
"""
Cleanup script to remove corrupt cases from Redis.

This script scans all cases in Redis and deletes any with invalid status values.
Valid statuses: consulting, investigating, resolved, closed

Usage:
    source .venv/bin/activate  # Activate virtual environment first
    python scripts/cleanup_corrupt_cases.py
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path to import faultmaven modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from faultmaven.container import container


async def main():
    """Run the cleanup operation"""
    print("=" * 60)
    print("Corrupt Case Cleanup Utility")
    print("=" * 60)
    print()
    print("This will scan Redis and delete cases with invalid status values.")
    print("Valid statuses: consulting, investigating, resolved, closed")
    print()

    # Initialize container
    await container.initialize()

    # Get case store from global container
    case_store = container.get_case_store()

    if case_store is None:
        print("=" * 60)
        print("✗ Error: Could not initialize case store")
        print("Make sure Redis is running and configured correctly")
        print("=" * 60)
        return 1

    print("Starting cleanup...")
    print()

    try:
        # Run cleanup
        stats = await case_store.cleanup_corrupt_cases()

        print()
        print("=" * 60)
        print("Cleanup Results:")
        print("=" * 60)
        print(f"Cases scanned:  {stats['scanned']}")
        print(f"Cases deleted:  {stats['deleted']}")
        print(f"Errors:         {stats['errors']}")
        print()

        if stats['deleted'] > 0:
            print(f"✓ Successfully cleaned up {stats['deleted']} corrupt case(s)")
        else:
            print("✓ No corrupt cases found - database is clean!")

    except Exception as e:
        print()
        print("=" * 60)
        print(f"✗ Error during cleanup: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if case_store:
            await case_store.close()

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

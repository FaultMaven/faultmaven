#!/usr/bin/env python3
"""List All User Accounts

This script lists all registered users in the FaultMaven system,
showing their usernames, emails, roles, and IDs.

Usage:
    python scripts/auth/list_users.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.container import container
from datetime import datetime


async def main():
    """List all users"""
    print("=" * 80)
    print("FaultMaven User Accounts")
    print("=" * 80)

    # Initialize container
    print("\nInitializing...")
    await container.initialize()

    # Get user store
    user_store = container.get_user_store()
    if not user_store:
        print("❌ Failed to get user store from container")
        return False

    # Get all users
    print("\nFetching users from Redis...")
    users = await user_store.list_users(limit=1000)  # Get up to 1000 users
    total_count = await user_store.count_users()

    print(f"\nFound {total_count} user(s):\n")

    if not users:
        print("No users found in the system.")
        print("\nTo create a user:")
        print("  1. Use the registration endpoint:")
        print("     curl -X POST http://localhost:8000/api/v1/auth/dev-register \\")
        print("       -H 'Content-Type: application/json' \\")
        print("       -d '{\"username\": \"myuser\"}'")
        print()
        print("  2. Or use the create_user.py script:")
        print("     python scripts/auth/create_user.py")
        return True

    # Display users in a formatted table
    print(f"{'#':<4} {'USERNAME':<20} {'EMAIL':<30} {'ROLES':<20} {'USER_ID'}")
    print("-" * 100)

    for idx, user in enumerate(users, 1):
        roles_str = ', '.join(user.roles if user.roles else ['none'])
        is_admin = 'admin' in (user.roles or [])

        # Add visual indicator for admins
        admin_indicator = "👑 " if is_admin else "   "

        print(f"{admin_indicator}{idx:<4} {user.username:<20} {user.email:<30} {roles_str:<20} {user.user_id}")

    print("\n" + "=" * 80)
    print(f"Total: {total_count} user(s)")

    # Count admins
    admin_count = sum(1 for u in users if 'admin' in (u.roles or []))
    regular_count = total_count - admin_count

    print(f"  Admins: {admin_count}")
    print(f"  Regular users: {regular_count}")
    print("=" * 80)

    return True


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

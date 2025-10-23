#!/usr/bin/env python3
"""Demote Admin to Regular User

This script removes the 'admin' role from a user account.

Usage:
    python scripts/auth/demote_from_admin.py username
    python scripts/auth/demote_from_admin.py bob
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.container import container


async def demote_from_admin(username: str):
    """Remove admin role from user"""
    print("=" * 80)
    print("Demote Admin to Regular User")
    print("=" * 80)

    # Initialize container
    print("\nInitializing...")
    await container.initialize()

    # Get user store
    user_store = container.get_user_store()
    if not user_store:
        print("❌ Failed to get user store from container")
        return False

    # Find user
    print(f"\nLooking up user '{username}'...")
    user = await user_store.get_user_by_username(username)
    if not user:
        print(f"❌ User '{username}' not found")
        print("\nTo see all users, run:")
        print("  python scripts/auth/list_users.py")
        return False

    print(f"✅ Found user: {user.user_id}")
    print(f"   Email: {user.email}")
    print(f"   Current roles: {user.roles}")

    # Check if user is admin
    if 'admin' not in user.roles:
        print(f"\n⚠️  User '{username}' is not an admin!")
        return True

    # Remove admin role
    print(f"\nRemoving 'admin' role from user '{username}'...")
    user.roles = [role for role in user.roles if role != 'admin']

    # Ensure user still has 'user' role
    if 'user' not in user.roles:
        user.roles.append('user')

    # Update user
    try:
        user = await user_store.update_user(user)
        print("✅ Admin role removed successfully!")
        print()
        print(f"Updated roles: {user.roles}")
        print()
        print(f"User '{username}' can no longer:")
        print("  ❌ Upload documents to Global KB")
        print("  ❌ Update Global KB documents")
        print("  ❌ Delete Global KB documents")
        print("  ❌ Perform bulk operations on Global KB")
        print()
        print(f"User '{username}' can still:")
        print("  ✅ Search Global KB")
        print("  ✅ Manage their own User KB")
        print()
        return True

    except Exception as e:
        print(f"❌ Failed to demote user: {e}")
        return False


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python scripts/auth/demote_from_admin.py <username>")
        print()
        print("Example:")
        print("  python scripts/auth/demote_from_admin.py bob")
        print()
        print("To see all users:")
        print("  python scripts/auth/list_users.py")
        sys.exit(1)

    username = sys.argv[1]
    success = asyncio.run(demote_from_admin(username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

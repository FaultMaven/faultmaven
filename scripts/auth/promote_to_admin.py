#!/usr/bin/env python3
"""Promote User to Admin

This script adds the 'admin' role to an existing user account.

Usage:
    python scripts/auth/promote_to_admin.py username
    python scripts/auth/promote_to_admin.py alice
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.container import container


async def promote_to_admin(username: str):
    """Promote user to admin"""
    print("=" * 80)
    print("Promote User to Admin")
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

    # Check if already admin
    if 'admin' in user.roles:
        print(f"\n⚠️  User '{username}' is already an admin!")
        return True

    # Add admin role
    print(f"\nAdding 'admin' role to user '{username}'...")
    if 'user' not in user.roles:
        user.roles = ['user', 'admin']
    else:
        user.roles.append('admin')

    # Update user
    try:
        user = await user_store.update_user(user)
        print("✅ User promoted to admin successfully!")
        print()
        print(f"Updated roles: {user.roles}")
        print()
        print(f"User '{username}' can now:")
        print("  ✅ Upload documents to Global KB")
        print("  ✅ Update Global KB documents")
        print("  ✅ Delete Global KB documents")
        print("  ✅ Perform bulk operations on Global KB")
        print()
        return True

    except Exception as e:
        print(f"❌ Failed to promote user: {e}")
        return False


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python scripts/auth/promote_to_admin.py <username>")
        print()
        print("Example:")
        print("  python scripts/auth/promote_to_admin.py alice")
        print()
        print("To see all users:")
        print("  python scripts/auth/list_users.py")
        sys.exit(1)

    username = sys.argv[1]
    success = asyncio.run(promote_to_admin(username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

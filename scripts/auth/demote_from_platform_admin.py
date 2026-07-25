#!/usr/bin/env python3
"""Demote Platform Admin to Regular User

This script removes the 'platform_admin' role from a user account.

That role is the DEPLOYMENT-scoped operator role (ADR-012 D9). Removing it
revokes cross-tenant reach. It is NOT the organization-scoped 'admin' role,
which is tenant-bounded; this script leaves that one untouched, so a user who
also holds 'admin' keeps full authority inside their own organization.

Usage:
    python scripts/auth/demote_from_platform_admin.py username
    python scripts/auth/demote_from_platform_admin.py bob
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.container import container
from faultmaven.modules.auth.contracts import PLATFORM_ADMIN_ROLE


async def demote_from_platform_admin(username: str):
    """Remove platform_admin role from user"""
    print("=" * 80)
    print("Demote Platform Admin to Regular User")
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

    # Check if user is a platform admin
    if PLATFORM_ADMIN_ROLE not in (user.roles or []):
        print(f"\n⚠️  User '{username}' is not a platform admin!")
        return True

    # Remove platform_admin role
    print(f"\nRemoving '{PLATFORM_ADMIN_ROLE}' role from user '{username}'...")
    user.roles = [role for role in user.roles if role != PLATFORM_ADMIN_ROLE]

    # Ensure user still has 'user' role
    if "user" not in user.roles:
        user.roles.append("user")

    # Update user
    try:
        user = await user_store.update_user(user)
        print("✅ Platform admin role removed successfully!")
        print()
        print(f"Updated roles: {user.roles}")
        print()
        print(f"User '{username}' can no longer:")
        print("  ❌ List cases across all users and organizations")
        print("  ❌ Administer user accounts")
        print("  ❌ View or change LLM configuration")
        print("  ❌ Manage the Global KB (upload, update, delete, bulk ops)")
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
        print("Usage: python scripts/auth/demote_from_platform_admin.py <username>")
        print()
        print("Example:")
        print("  python scripts/auth/demote_from_platform_admin.py bob")
        print()
        print("To see all users:")
        print("  python scripts/auth/list_users.py")
        sys.exit(1)

    username = sys.argv[1]
    success = asyncio.run(demote_from_platform_admin(username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

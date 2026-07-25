#!/usr/bin/env python3
"""Promote User to Platform Admin

This script adds the 'platform_admin' role to an existing user account.

That role is the DEPLOYMENT-scoped operator role (ADR-012 D9) — it grants
cross-tenant reach: the admin case list, user administration, LLM configuration
and Global KB management. It is NOT the organization-scoped 'admin' role, which
is tenant-bounded; this script does not grant or remove that one.

Usage:
    python scripts/auth/promote_to_platform_admin.py username
    python scripts/auth/promote_to_platform_admin.py alice
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.container import container
from faultmaven.modules.auth.contracts import PLATFORM_ADMIN_ROLE


async def promote_to_platform_admin(username: str):
    """Promote user to platform admin"""
    print("=" * 80)
    print("Promote User to Platform Admin")
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

    # Check if already a platform admin
    if PLATFORM_ADMIN_ROLE in user.roles:
        print(f"\n⚠️  User '{username}' is already a platform admin!")
        return True

    # Add platform_admin role
    print(f"\nAdding '{PLATFORM_ADMIN_ROLE}' role to user '{username}'...")
    roles = list(user.roles or [])
    if "user" not in roles:
        roles.insert(0, "user")
    roles.append(PLATFORM_ADMIN_ROLE)
    user.roles = roles

    # Update user
    try:
        user = await user_store.update_user(user)
        print("✅ User promoted to platform admin successfully!")
        print()
        print(f"Updated roles: {user.roles}")
        print()
        print(f"User '{username}' can now:")
        print("  ✅ List cases across all users and organizations")
        print("  ✅ Administer user accounts")
        print("  ✅ View and change LLM configuration")
        print("  ✅ Manage the Global KB (upload, update, delete, bulk ops)")
        print()
        return True

    except Exception as e:
        print(f"❌ Failed to promote user: {e}")
        return False


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python scripts/auth/promote_to_platform_admin.py <username>")
        print()
        print("Example:")
        print("  python scripts/auth/promote_to_platform_admin.py alice")
        print()
        print("To see all users:")
        print("  python scripts/auth/list_users.py")
        sys.exit(1)

    username = sys.argv[1]
    success = asyncio.run(promote_to_platform_admin(username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

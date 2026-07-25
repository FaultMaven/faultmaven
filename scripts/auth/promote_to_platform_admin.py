#!/usr/bin/env python3
"""Promote User to Platform Admin

This script adds the 'platform_admin' role to an existing user account.

That role is the DEPLOYMENT-scoped operator role (ADR-012 D9) — it grants
cross-tenant reach: the admin case list, user administration, LLM configuration
and Global KB management. It is distinct from the organization-scoped 'admin'
role, which is tenant-bounded, but an operator needs authority in its own org
too, so this grants the full operator set (see PLATFORM_ADMIN_ROLE_SET).

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
from faultmaven.modules.auth.contracts import (
    PLATFORM_ADMIN_ROLE,
    PLATFORM_ADMIN_ROLE_SET,
)


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

    # Grant the full operator role set, so an account promoted here is
    # identical to one created with `create_user.py --role platform_admin`.
    missing = [r for r in PLATFORM_ADMIN_ROLE_SET if r not in (user.roles or [])]
    print(f"\nGranting operator roles {missing} to user '{username}'...")
    user.roles = list(user.roles or []) + missing

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

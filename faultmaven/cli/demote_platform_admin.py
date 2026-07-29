"""Demote Platform Admin to Regular User

This script removes the 'platform_admin' role from a user account.

That role is the DEPLOYMENT-scoped operator role (ADR-012 D9). Removing it
revokes cross-tenant reach. It is NOT the organization-scoped 'admin' role,
which is tenant-bounded; this script leaves that one untouched, so a user who
also holds 'admin' keeps full authority inside their own organization.

Usage (``fm-demote-platform-admin``, installed with the package):
    fm-demote-platform-admin username
    fm-demote-platform-admin bob

In a Kubernetes deployment, run it in the API pod:
    kubectl exec -it deploy/faultmaven-api -- fm-demote-platform-admin bob
"""

import asyncio
import sys

from faultmaven.bootstrap.data_init import DEFAULT_ADMIN_USERNAME
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
        print("\nTo see all users, run (from a source checkout):")
        print("  python scripts/auth/list_users.py")
        return False

    print(f"✅ Found user: {user.user_id}")
    print(f"   Email: {user.email}")
    print(f"   Current roles: {user.roles}")

    # Check if user is a platform admin
    if PLATFORM_ADMIN_ROLE not in (user.roles or []):
        print(f"\n⚠️  User '{username}' is not a platform admin!")
        return True

    # The bootstrap account is re-granted the operator roles on every startup
    # (a standalone deployment with no operator is unusable), so demoting it
    # does not survive a restart. Say so rather than reporting a success that
    # quietly reverts.
    if user.username == DEFAULT_ADMIN_USERNAME:
        print(
            f"\n⚠️  '{username}' is the bootstrap operator account. Startup"
            " re-grants its operator roles, so this demotion will be undone by"
            " the next restart."
        )
        confirm = input("Demote anyway? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("❌ Cancelled")
            return False

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
        print("Usage: fm-demote-platform-admin <username>")
        print()
        print("Example:")
        print("  fm-demote-platform-admin bob")
        print()
        print("To see all users (from a source checkout):")
        print("  python scripts/auth/list_users.py")
        sys.exit(1)

    username = sys.argv[1]
    success = asyncio.run(demote_from_platform_admin(username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

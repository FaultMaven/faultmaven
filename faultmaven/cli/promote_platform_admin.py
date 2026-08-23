"""Promote User to Platform Admin

This script adds the 'platform_admin' role to an existing user account.

That role is the DEPLOYMENT-scoped operator role (ADR-012 D9) — it grants
cross-tenant reach: the admin case list, user administration, LLM configuration
and Global KB management. It is distinct from the organization-scoped 'admin'
role, which is tenant-bounded, but an operator needs authority in its own org
too, so this grants the full operator set (see PLATFORM_ADMIN_ROLE_SET).

Usage (``fm-promote-platform-admin``, installed with the package):
    fm-promote-platform-admin username
    fm-promote-platform-admin alice

In a Kubernetes deployment, run it in the API pod:
    kubectl exec -it deploy/faultmaven-api -- fm-promote-platform-admin alice
"""

import argparse
import asyncio
import sys

from faultmaven.bootstrap.data_init import assign_operator_roles
from faultmaven.container import container
from faultmaven.exceptions import UserLookupFailed

#: How to enumerate accounts. ``list_users.py`` is a checkout-only dev script
#: (it is deliberately not a console entrypoint), so a pod needs the API.
_HOW_TO_LIST_USERS = (
    "To see all users:\n"
    "  in a deployment:  GET /api/v1/admin/users   (needs a platform-admin token)\n"
    "  from a checkout:  python scripts/auth/list_users.py"
)


async def promote_to_platform_admin(username: str) -> bool:
    """Promote user to platform admin. Returns True on success."""
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
    try:
        user = await user_store.get_user_by_username(username)
    except UserLookupFailed as exc:
        # Not "not found": the store did not answer (#1043). Saying "not found"
        # here would send an operator hunting for the right username while the
        # real fault — an unavailable user store — stayed invisible, and the
        # promotion they came to make had not happened.
        print(
            f"❌ The username lookup for '{username}' FAILED — this is not "
            "'user not found'."
        )
        print("   Whether the account exists is unknown, and nothing was changed.")
        print(f"   Underlying error: {exc}")
        print("   Check the API logs and the database, then re-run.")
        return False
    if not user:
        print(f"❌ User '{username}' not found")
        print()
        print(_HOW_TO_LIST_USERS)
        return False

    print(f"✅ Found user: {user.user_id}")
    print(f"   Email: {user.email}")
    print(f"   Current roles: {user.roles}")

    # Delegate the grant to the one writer of it (bootstrap's startup re-grant
    # calls the same helper), so a hand-promoted operator and a startup-restored
    # one end up with identical roles. It grants the whole PLATFORM_ADMIN_ROLE_SET
    # — including the org-scoped 'admin' an operator needs inside its own
    # organization — and repairs a partially-granted account rather than
    # reporting "already an admin" and leaving the hole.
    # It also writes the audit row (it is the single writer of the grant, so it
    # is the single auditor of it — see fm#1050), and hands back the audit
    # failure rather than raising, because by then the grant is already durable.
    try:
        user, granted, audit_error = await assign_operator_roles(
            user_store, user, invoked_via="fm-promote-platform-admin"
        )
    except Exception as e:
        print(f"❌ Failed to promote user: {e}")
        return False

    if not granted:
        print(f"\n⚠️  User '{username}' already holds every operator role.")
        return True

    print(f"\nGranted operator roles {granted} to user '{username}'.")

    if audit_error is not None:
        print()
        print(f"❌ The roles WERE granted, but the audit record failed: {audit_error}")
        print(
            "   The privilege change is live and unrecorded. Re-running will "
            "NOT repair it —\n"
            "   the grant is idempotent, so a second run adds nothing and "
            "audits nothing.\n"
            "   Record it by hand and investigate why "
            "operator_access_audit is unwritable."
        )
        return False

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


def main() -> None:
    """Console entrypoint (``fm-promote-platform-admin``)."""
    parser = argparse.ArgumentParser(
        prog="fm-promote-platform-admin",
        description=(
            "Grant an existing user the deployment-operator role set "
            "(ADR-012 D9): user + admin + platform_admin."
        ),
        epilog=_HOW_TO_LIST_USERS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("username", help="Username of the account to promote")
    args = parser.parse_args()

    success = asyncio.run(promote_to_platform_admin(args.username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

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

import argparse
import asyncio
import sys

from faultmaven.bootstrap.data_init import DEFAULT_ADMIN_USERNAME
from faultmaven.cli._operator_role_audit import record_operator_role_change
from faultmaven.container import container
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.modules.auth.contracts import PLATFORM_ADMIN_ROLE

#: How to enumerate accounts. ``list_users.py`` is a checkout-only dev script
#: (it is deliberately not a console entrypoint), so a pod needs the API.
_HOW_TO_LIST_USERS = (
    "To see all users:\n"
    "  in a deployment:  GET /api/v1/admin/users   (needs a platform-admin token)\n"
    "  from a checkout:  python scripts/auth/list_users.py"
)


async def demote_from_platform_admin(username: str) -> bool:
    """Remove platform_admin role from user. Returns True on success."""
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
        print()
        print(_HOW_TO_LIST_USERS)
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

        # Outstanding access tokens still carry `platform_admin` in their claims
        # until they expire, so the demotion is not in force until the user's
        # revocation watermark moves. The HTTP role paths already do this
        # (`user_service.remove_role`); this one did not, leaving a demoted
        # operator with working cross-tenant reach for the remainder of the
        # token's life (fm#1050).
        #
        # Unlike `fm-remove-org-member`, a failure here does NOT refuse the
        # operation. That command refuses because removing a membership without
        # revoking leaves a user with access they should no longer have, and not
        # removing it is the safer half. Here the directions reverse: the role is
        # already gone from the account, so failing closed would mean restoring
        # `platform_admin` permanently to avoid a bounded window of at most one
        # access-token lifetime. Report it and let the operator decide.
        auth_service = container.get_auth_service()
        if auth_service is None:
            print(
                "⚠️  No auth service is available, so outstanding tokens were "
                "NOT revoked.\n"
                f"   '{username}' keeps platform-admin reach until every token "
                "issued before now expires."
            )
        else:
            try:
                revoked_before = await auth_service.revoke_user_tokens(user.user_id)
                print(
                    f"✅ Tokens revoked: every token for {username} issued at or "
                    f"before {revoked_before.isoformat()} is now invalid."
                )
            except Exception as revoke_error:
                print(
                    f"⚠️  The role WAS removed, but token revocation failed: "
                    f"{revoke_error}\n"
                    f"   '{username}' keeps platform-admin reach until every "
                    "token issued before now expires."
                )
            print()

        try:
            await record_operator_role_change(
                action=OperatorAction.ROLE_REVOKED,
                user=user,
                roles_changed=[PLATFORM_ADMIN_ROLE],
                invoked_via="fm-demote-platform-admin",
            )
        except Exception as audit_error:
            print(
                f"❌ The role WAS removed, but the audit record failed: "
                f"{audit_error}\n"
                "   The privilege change is live and unrecorded. Re-running "
                "will NOT repair it —\n"
                "   the account no longer holds the role, so a second run "
                "removes nothing and audits nothing."
            )
            return False

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


def main() -> None:
    """Console entrypoint (``fm-demote-platform-admin``)."""
    parser = argparse.ArgumentParser(
        prog="fm-demote-platform-admin",
        description=(
            "Remove the deployment-scoped platform_admin role from a user. "
            "The organization-scoped 'admin' role is left in place."
        ),
        epilog=_HOW_TO_LIST_USERS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("username", help="Username of the account to demote")
    args = parser.parse_args()

    success = asyncio.run(demote_from_platform_admin(args.username))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

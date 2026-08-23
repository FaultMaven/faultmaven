"""Demote a platform admin back to a regular user — the inverse of promotion.

``platform_admin`` is the DEPLOYMENT-scoped operator role (ADR-012 D9), and
removing it revokes cross-tenant reach. But a promotion grants more than that
one role: ``PLATFORM_ADMIN_ROLE_SET`` is ``user`` + org ``admin`` +
``platform_admin``, because an operator needs authority inside its own
organization too and ``platform_admin`` grants none by construction.

**This used to remove only ``platform_admin`` (#1040 item 3).** So
promote-then-demote left the account holding the org-scoped ``admin`` it did not
hold beforehand — deployment reach revoked, in-org authority silently added and
kept. That was latent only because org roles enforce nothing at the API surface
yet; it becomes a real privilege residue the moment they do, and by then the
grant is old enough that nobody remembers where it came from.

It now removes everything a promotion adds (``OPERATOR_GRANTED_ROLES``, derived
from the same constant the grant uses so the two cannot drift again), leaving
the base ``user`` marker so the account stays usable.

**If the account held org ``admin`` before it was ever promoted**, this removes
that too — nothing records which grant it came from. Pass ``--keep-org-admin``
when you know it did, or re-grant it afterwards through the org-role API
(``POST /api/v1/admin/users/{id}/roles``), which is where org roles belong and
which is itself audited.

Usage (``fm-demote-platform-admin``, installed with the package):
    fm-demote-platform-admin username
    fm-demote-platform-admin bob --keep-org-admin

In a Kubernetes deployment, run it in the API pod:
    kubectl exec -it deploy/faultmaven-api -- fm-demote-platform-admin bob
"""

import argparse
import asyncio
import sys

from faultmaven.bootstrap.data_init import DEFAULT_ADMIN_USERNAME
from faultmaven.cli._operator_role_audit import record_operator_role_change
from faultmaven.container import container
from faultmaven.exceptions import UserLookupFailed
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.modules.auth.contracts import (
    BASE_USER_ROLE,
    OPERATOR_GRANTED_ROLES,
    PLATFORM_ADMIN_ROLE,
)

#: How to enumerate accounts. ``list_users.py`` is a checkout-only dev script
#: (it is deliberately not a console entrypoint), so a pod needs the API.
_HOW_TO_LIST_USERS = (
    "To see all users:\n"
    "  in a deployment:  GET /api/v1/admin/users   (needs a platform-admin token)\n"
    "  from a checkout:  python scripts/auth/list_users.py"
)


async def demote_from_platform_admin(
    username: str, *, keep_org_admin: bool = False
) -> bool:
    """Remove the operator roles from a user. Returns True on success.

    Args:
        username: Account to demote.
        keep_org_admin: Leave the org-scoped ``admin`` role in place. Use it when
            the account held that role independently of any promotion — this
            command cannot tell, because nothing records which grant a role came
            from (see the module docstring).
    """
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
    try:
        user = await user_store.get_user_by_username(username)
    except UserLookupFailed as exc:
        # Not "not found": the store did not answer (#1043). Saying "not found"
        # here would send an operator hunting for the right username while the
        # real fault — an unavailable user store — stayed invisible, and the
        # demotion they came to make had not happened.
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

    # Remove exactly what a promotion grants, so the two are inverses (#1040
    # item 3). Derived from PLATFORM_ADMIN_ROLE_SET rather than restated here —
    # restating it is how the asymmetry arose in the first place.
    to_remove = [
        role
        for role in OPERATOR_GRANTED_ROLES
        if not (keep_org_admin and role != PLATFORM_ADMIN_ROLE)
    ]
    removed = [role for role in to_remove if role in (user.roles or [])]

    print(
        f"\nRemoving {', '.join(repr(r) for r in to_remove)} from user '{username}'..."
    )
    if keep_org_admin:
        print("   (--keep-org-admin: the org-scoped 'admin' role is left in place)")
    user.roles = [role for role in user.roles if role not in to_remove]

    # Leave a usable account: the base marker grants nothing, but an empty role
    # list is not a state anything else in the system expects.
    if BASE_USER_ROLE not in user.roles:
        user.roles.append(BASE_USER_ROLE)

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
                # What was actually taken away, not what was aimed at: an
                # account that already lacked the org role must not leave a
                # trail claiming it was revoked.
                roles_changed=removed,
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
            "Remove the operator roles from a user — the inverse of "
            "fm-promote-platform-admin. Removes the deployment-scoped "
            "platform_admin AND the organization-scoped admin that a promotion "
            "grants alongside it; pass --keep-org-admin to leave the latter."
        ),
        epilog=_HOW_TO_LIST_USERS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("username", help="Username of the account to demote")
    parser.add_argument(
        "--keep-org-admin",
        action="store_true",
        help=(
            "Leave the organization-scoped 'admin' role in place. Use it when "
            "the account held that role independently of any promotion — "
            "nothing records which grant a role came from, so this command "
            "cannot tell"
        ),
    )
    args = parser.parse_args()

    success = asyncio.run(
        demote_from_platform_admin(args.username, keep_org_admin=args.keep_org_admin)
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

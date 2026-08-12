"""Remove a user from an organization and end their live sessions (#874).

The operator counterpart of the Cloud admin console's
``DELETE /api/v1/admin/organization/members/{user_id}``, and the replacement for
the two-step SQL procedure in ``docs/operations/sso-org-provisioning.md``
("Revoking access for one user").

Why a command and not two SQL statements
----------------------------------------
Membership is checked at **login** only, so deleting the
``organization_members`` row stops future logins from being member-scoped but
leaves every outstanding token working until it expires. The runbook therefore
told operators to also bump the user's revocation watermark — a second step,
remembered by hand, on the one procedure whose entire purpose is to cut off
access. This command runs the paired operation
(:class:`~faultmaven.modules.auth.domain.services.organization_membership_service.OrganizationMembershipService`),
so the removal and the revocation cannot come apart.

Usage (``fm-remove-org-member``, installed with the package)
------------------------------------------------------------
    fm-remove-org-member --organization-id <org-id> --user alice --dry-run
    fm-remove-org-member --organization-id <org-id> --user alice --yes

In a Kubernetes deployment, run it in the API pod::

    kubectl exec -it deploy/faultmaven-api -- \\
        fm-remove-org-member --organization-id <org-id> --user alice --yes

``--user`` accepts a username, an email address, or a user id. The organization
is addressed by **id**, not slug: the tenant context is set to that id so the
command runs under the pod's own RLS-scoped application role (migration 018)
rather than needing the RLS-exempt owner DSN. A slug lookup would have to read
``organizations`` across tenants, which that role cannot do — and should not.
``fm-provision-sso-org`` prints the organization id when it provisions a tenant.

Why it refuses under in-process FakeRedis
-----------------------------------------
The revocation watermark lives in the deployment-wide Redis store (#767). In a
standalone deployment that store is **FakeRedis, private to one process** — a
watermark this command writes would live and die inside the CLI process and the
running API would never see it. The command would print success while every
token stayed valid, which is worse than not running at all, so it refuses.
Cloud, where organizations and this procedure actually apply, requires real
Redis (``fakeredis_or_fail``), so the guard never fires there.

Exit codes
----------
0 success (or dry-run), 1 refused / not found / nothing written, 2 the
membership was removed but the revocation did NOT land (re-run to finish it —
the operation is idempotent).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = (
    "Remove a user from an organization and revoke their outstanding tokens, "
    "as one operation."
)

#: Exit code for the half-state: membership gone, tokens still live. Distinct
#: from a plain refusal (1) because it is the one outcome that leaves the
#: deployment in a state an operator MUST come back to.
EXIT_REVOCATION_INCOMPLETE = 2


def _revocation_store_is_process_local(store) -> bool:
    """True if this revocation store is backed by in-process FakeRedis.

    A positive identification of the known-broken case, not a proof of health:
    an unrecognised store shape is left alone rather than refused, so a future
    store implementation does not become an outage in this command.
    """
    from faultmaven.infrastructure.redis_client import is_fakeredis

    client = getattr(store, "redis", None)
    return client is not None and is_fakeredis(client)


async def _resolve_user(user_store, identifier: str):
    """Resolve a username, email, or user id to a user — in that order.

    Username first because it is what an operator reads off the runbook and the
    admin console; the id lookup is last so a username that happens to look like
    an id still resolves as a username.
    """
    for lookup in (
        user_store.get_user_by_username,
        user_store.get_user_by_email,
        user_store.get_user,
    ):
        user = await lookup(identifier)
        if user is not None:
            return user
    return None


async def remove_org_member(
    *, organization_id: str, user_identifier: str, dry_run: bool
) -> int:
    """Run the paired removal. Returns the process exit code."""
    from faultmaven.config.tenant_context import set_current_org_id
    from faultmaven.container import container
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (
        SessionlessOrganizationRepository,
    )
    from faultmaven.modules.auth.domain.services.organization_membership_service import (
        MembershipRemovalIncomplete,
        OrganizationMembershipService,
    )

    print("=" * 80)
    print("Remove Organization Member")
    print("=" * 80)

    print("\nInitializing...")
    await container.initialize()

    auth_service = container.get_auth_service()
    if auth_service is None:
        print(
            "\n❌ No auth service is available, so tokens cannot be revoked. "
            "Refusing to remove the membership: an unrevoked removal leaves the "
            "user's outstanding tokens valid."
        )
        return 1

    revocation_store = container.get_service("token_revocation_store")
    if revocation_store is None:
        print(
            "\n❌ No token revocation store is wired, so the watermark cannot be "
            "written. Refusing to remove the membership."
        )
        return 1
    if _revocation_store_is_process_local(revocation_store):
        print(
            "\n❌ The token revocation store is in-process FakeRedis. A watermark "
            "written here would be invisible to the running API, so this command "
            "would report success while every token stayed valid.\n"
            "   This procedure applies to multi-tenant (Cloud) deployments, which "
            "run a real Redis. Point REDIS_URL / REDIS_HOST at the deployment's "
            "Redis and re-run."
        )
        return 1

    # RLS (migration 018) scopes organizations and organization_members by
    # `app.current_org_id`. Bind it to the target org so the lookups and the
    # DELETE below run under the pod's own application role.
    set_current_org_id(organization_id)

    orgs = SessionlessOrganizationRepository()
    organization = await orgs.get_organization(organization_id)
    if organization is None:
        print(
            f"\n❌ No organization '{organization_id}' is visible.\n"
            "   Check the id (it is an id, not a slug), and note that a deleted "
            "organization does not resolve."
        )
        return 1

    user_store = container.get_user_store()
    if user_store is None:
        print("\n❌ Failed to get user store from container")
        return 1

    user = await _resolve_user(user_store, user_identifier)
    if user is None:
        print(
            f"\n❌ No user matches '{user_identifier}' "
            "(tried username, then email, then user id)."
        )
        return 1

    current_role_id = await orgs.get_member_role(organization_id, user.user_id)

    print(f"\nOrganization: {organization.name} ({organization_id})")
    print(f"User:         {user.username} <{user.email}> ({user.user_id})")
    if current_role_id is None:
        # Not an error: this is exactly the state a half-completed previous run
        # leaves behind, and finishing it is what the revocation below does.
        print(
            "Membership:   none (already removed) — the revocation will still be "
            "written, which is how an interrupted removal is completed."
        )
    else:
        print(f"Membership:   present (role_id {current_role_id})")

    if dry_run:
        print(
            "\nDry run — nothing was written. Re-run with --yes to remove the "
            "membership and revoke this user's outstanding tokens."
        )
        return 0

    service = OrganizationMembershipService(
        organization_repository=orgs, auth_service=auth_service
    )
    try:
        result = await service.remove_member(organization_id, user.user_id)
    except MembershipRemovalIncomplete as exc:
        print(f"\n❌ {exc}")
        return EXIT_REVOCATION_INCOMPLETE

    if result.membership_removed:
        print("\n✅ Membership removed.")
    else:
        print("\n✅ No membership row to remove (it was already gone).")
    print(
        f"✅ Tokens revoked: every token for {user.username} issued at or before "
        f"{result.revoked_before.isoformat()} is now invalid."
    )
    return 0


def main() -> None:
    """Console entrypoint (``fm-remove-org-member``)."""
    parser = argparse.ArgumentParser(
        prog="fm-remove-org-member",
        description=_SUMMARY,
        epilog=(
            "Membership is verified at login only, so removing the row is not "
            "enough on its own — this command bumps the user's revocation "
            "watermark in the same operation so outstanding tokens die with the "
            "membership."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--organization-id",
        required=True,
        help="Organization id to remove the user from (an id, not a slug)",
    )
    parser.add_argument(
        "--user",
        required=True,
        help="User to remove: username, email address, or user id",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and exit without writing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the write (required; the user is signed out of every session)",
    )
    args = parser.parse_args()

    # Refuse before touching anything: a run with neither flag is an operator
    # who has not yet decided, and this write signs a user out everywhere. The
    # check sits here, ahead of container initialisation and any database
    # connection, so the refusal costs nothing and cannot half-run.
    if not args.dry_run and not args.yes:
        print(
            "❌ Refusing to run without --yes. This removes the user's membership "
            "and signs them out of every active session.\n"
            "   Use --dry-run first to see what would change."
        )
        sys.exit(1)

    sys.exit(
        asyncio.run(
            remove_org_member(
                organization_id=args.organization_id,
                user_identifier=args.user,
                dry_run=args.dry_run,
            )
        )
    )


if __name__ == "__main__":
    main()

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

**A non-member is refused.** ``users`` is not tenant-scoped, so the lookup finds
any account in the deployment — including one in a different organization. A
mistyped ``--organization-id`` would otherwise remove nothing and revoke an
unrelated tenant's user, ending every session they have. Finishing a genuinely
interrupted run is the explicit ``--finish-interrupted`` case.

**This ends sessions; under SSO it does not by itself end access.** The IdP
login path re-adds membership just-in-time, so a user who can still authenticate
at the identity provider is silently re-added on their next login. Deprovision
them at the IdP first — see the runbook section this command is documented in.

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
| 0 | success, or a dry run |
| 1 | refused: not found, not a member, nothing that can revoke — nothing written |
| 2 | argparse usage error (a bad flag), reserved by argparse — nothing written |
| 3 | the membership was removed but the revocation did NOT land; re-run with
     ``--finish-interrupted`` |
| 4 | the revocation landed but the membership row was not deleted; the row may
     survive and with it access on the next login |
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

#: Exit code for the half-state: membership gone, tokens still live. The one
#: outcome that leaves the deployment in a state an operator MUST come back to,
#: so it gets its own code. Deliberately **not** 2 — argparse exits 2 on any
#: usage error, and a mistyped flag must not be readable as "the membership was
#: removed but the revocation did not land".
EXIT_REVOCATION_INCOMPLETE = 3

#: Exit code for the other anomaly: the revocation landed but the membership row
#: was not deleted, though this command had just read it as present. Tokens are
#: dead, the row survives, and the user is back in on their next login.
EXIT_MEMBERSHIP_NOT_REMOVED = 4


def _revocation_store_unusable(store) -> str | None:
    """Why this revocation store cannot end a session, or None if it can.

    Two known-broken shapes, both of which would let the command delete the
    membership and only then fail to revoke — the half-state the preflight
    exists to prevent, detectable before the write:

    * **in-process FakeRedis** — a watermark written here lives and dies inside
      the CLI process, invisible to the running API;
    * **no client at all** — the store was built without one, so writing the
      watermark raises on a ``None``.

    An otherwise unrecognised store shape is left alone rather than refused, so a
    future store implementation does not become an outage in this command.
    """
    from faultmaven.infrastructure.redis_client import is_fakeredis

    if not hasattr(store, "redis"):
        return None
    client = store.redis
    if client is None:
        return (
            "the token revocation store has no client, so writing the watermark "
            "would fail after the membership had already been deleted"
        )
    if is_fakeredis(client):
        return (
            "the token revocation store is in-process FakeRedis. A watermark "
            "written here would be invisible to the running API, so this command "
            "would report success while every token stayed valid.\n"
            "   This procedure applies to multi-tenant (Cloud) deployments, which "
            "run a real Redis. Point REDIS_URL / REDIS_HOST at the deployment's "
            "Redis and re-run"
        )
    return None


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
    *,
    organization_id: str,
    user_identifier: str,
    dry_run: bool,
    finish_interrupted: bool = False,
) -> int:
    """Run the paired removal. Returns the process exit code."""
    from faultmaven.config.tenant_context import set_current_org_id
    from faultmaven.container import container
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (
        SessionlessOrganizationRepository,
    )
    from faultmaven.modules.auth.domain.services.organization_membership_service import (
        MembershipRemovalIncomplete,
        MembershipRemovalMisscoped,
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
    unusable = _revocation_store_unusable(revocation_store)
    if unusable is not None:
        print(f"\n❌ Refusing to remove the membership: {unusable}.")
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
        # The user store swallows lookup errors and returns None, so "absent" and
        # "the lookup failed" arrive here identically. Say so rather than assert
        # the account does not exist — during an incident the difference is the
        # difference between a typo and an unavailable database.
        print(
            f"\n❌ No user matches '{user_identifier}' "
            "(tried username, then email, then user id).\n"
            "   A failed lookup reads the same as an absent account here; if the "
            "account should exist, check the API logs for a database error before "
            "assuming the identifier is wrong."
        )
        return 1

    current_role_id = await orgs.get_member_role(organization_id, user.user_id)

    print(f"\nOrganization: {organization.name} ({organization_id})")
    print(f"User:         {user.username} <{user.email}> ({user.user_id})")

    if current_role_id is None:
        # `users` is NOT tenant-scoped (migration 018), so the lookup above finds
        # any account in the deployment — including one belonging to a different
        # organization. Revoking unconditionally here would sign that unrelated
        # user out everywhere on a mistyped --organization-id, having removed
        # nothing. So a non-member is refused by default; finishing a genuinely
        # interrupted run is the explicit --finish-interrupted case.
        if not finish_interrupted:
            print(
                f"\n❌ {user.username} is not a member of this organization, so "
                "there is nothing to remove.\n"
                "   Refusing rather than revoking: this is what a mistyped "
                "--organization-id looks like, and revoking anyway would end every "
                "session of a user in some other tenant.\n"
                "   If a previous run removed the membership but failed to revoke "
                "(exit "
                f"{EXIT_REVOCATION_INCOMPLETE}), re-run with --finish-interrupted "
                "to write the outstanding revocation."
            )
            return 1
        print(
            "Membership:   none — finishing an interrupted removal, so only the "
            "revocation will be written."
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
    except MembershipRemovalMisscoped as exc:
        print(f"\n❌ {exc}")
        return 1
    except MembershipRemovalIncomplete as exc:
        print(f"\n❌ {exc}")
        return EXIT_REVOCATION_INCOMPLETE

    if not result.membership_removed and current_role_id is not None:
        # This command read the membership as present moments ago and the delete
        # still matched nothing — a concurrent change, or a condition that hides
        # the row from the delete. Reporting success here would tell an operator
        # that access is cut while the row survives and only the tokens died.
        print(
            f"\n⚠️  The membership was present when this run started (role_id "
            f"{current_role_id}) but the delete matched no row.\n"
            f"   Tokens WERE revoked (before {result.revoked_before.isoformat()}), "
            "so current sessions are dead — but the membership may survive, and "
            "with it access on the next login. Verify the row is gone before "
            "treating this user as removed."
        )
        return EXIT_MEMBERSHIP_NOT_REMOVED

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
    parser.add_argument(
        "--finish-interrupted",
        action="store_true",
        help=(
            "Write the revocation for a user who is already not a member — the "
            "recovery path after a run that removed the membership but failed to "
            f"revoke (exit {EXIT_REVOCATION_INCOMPLETE})"
        ),
    )
    args = parser.parse_args()

    # --dry-run with --yes is a usage error, not a preference. The two
    # invocations differ by one flag, so an operator editing the previous command
    # can end up with both — and silently taking the dry-run branch would exit 0
    # and read as "access cut" when nothing was written.
    if args.dry_run and args.yes:
        parser.error(
            "--dry-run and --yes are mutually exclusive: pass --dry-run to preview, "
            "--yes to write."
        )

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
                finish_interrupted=args.finish_interrupted,
            )
        )
    )


if __name__ == "__main__":
    main()

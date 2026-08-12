"""Organization-membership removal, paired with per-user token revocation (#874).

The problem this exists to close
--------------------------------
Organization membership is verified at **login** only. The resolved tenant is
minted into the token (``organization_id``) and nothing re-checks
``organization_members`` on the request path. So deleting a membership row stops
*future* logins from being member-scoped while leaving every outstanding token
working until it expires — up to ``JWT_REFRESH_TOKEN_EXPIRY_DAYS``. "Removed from
the organization" has to mean "outstanding tokens die", and the only mechanism
that ends a live session is the per-user revocation watermark
(``AuthService.revoke_user_tokens``, the single deployment-wide store, #767/#825).

Before this service the pairing was a *convention*: the runbook
(``docs/operations/sso-org-provisioning.md``) told an operator to do both, and
nothing enforced it. A convention that has to be remembered at 3am, on the one
procedure whose whole purpose is to cut off access, is not a control.

Why the pairing lives here and not in the caller
------------------------------------------------
``IOrganizationRepository.remove_member`` is core; its callers are not. The Cloud
admin console (``faultmaven_cloud``, ADR-010 D7) drives it from the composed
proprietary module, and an operator drives it from a pod. Wiring revocation into
any one of those leaves the others unpaired and leaves the repository method a
footgun for the next caller. One operation in core that does both — and which
every writer goes through — makes "removed ⇒ revoked" a property of the
operation rather than a step each caller must remember.

``tests/unit/modules/auth/test_membership_removal_is_paired.py`` is the tripwire
that keeps it that way inside this repository; it cannot see the Cloud repo, so
the composed module has its own obligation to call this service rather than the
repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from faultmaven.config.tenant_context import get_current_org_id
from faultmaven.models.interfaces_user import IOrganizationRepository

logger = logging.getLogger(__name__)


class MembershipRemovalMisscoped(Exception):
    """The bound tenant context does not name the organization being written to.

    ``organization_members`` is RLS-tenanted (migration 018) and the DELETE is
    filtered by ``app.current_org_id``, so a call whose context names a
    *different* organization deletes nothing — and ``remove_member`` reports that
    as ``rowcount == 0``, indistinguishable from "was not a member". The caller
    would then read a silent no-op as "already removed" while the membership
    survives and only the tokens were revoked, putting the user back in on their
    next login.

    Raised **before** either write, so nothing has happened when it surfaces.
    """


class MembershipRemovalIncomplete(Exception):
    """The membership row was deleted but the revocation watermark was NOT written.

    The dangerous half-state, named rather than swallowed: the user is out of the
    organization yet their outstanding tokens still work. Raised only after the
    delete has landed, so it always means "half done" — never "nothing happened".

    Recovery is to re-run the same call: :meth:`OrganizationMembershipService.remove_member`
    is idempotent and bumps the watermark even when there is no row left to
    delete, so a second attempt completes the operation rather than reporting
    "not a member" and leaving the tokens alive.
    """


@dataclass(frozen=True)
class MembershipRemovalResult:
    """What a removal actually did.

    Attributes:
        membership_removed: True if a row was deleted. False means the user was
            already not a member — the watermark was still bumped (see
            :meth:`OrganizationMembershipService.remove_member`), so this
            distinguishes "removed now" from "finished a previous half-run"
            without either being an error.
        revoked_before: The revocation instant. Every token for this user issued
            at or before it is now invalid.
    """

    membership_removed: bool
    revoked_before: datetime


class OrganizationMembershipService:
    """Remove a user from an organization and end their live sessions, as one step."""

    def __init__(
        self,
        organization_repository: IOrganizationRepository,
        auth_service,
    ):
        """
        Args:
            organization_repository: Owns the ``organization_members`` row.
            auth_service: Owns the revocation watermark
                (``revoke_user_tokens``). Required — a membership service that
                could be constructed without one would silently reintroduce the
                unpaired removal this class exists to prevent.
        """
        if auth_service is None:
            raise ValueError(
                "OrganizationMembershipService requires an auth_service: without "
                "one it cannot revoke, and an unrevoked removal leaves live "
                "tokens for a user who is no longer a member (#874)."
            )
        self._orgs = organization_repository
        self._auth_service = auth_service

    async def remove_member(
        self, organization_id: str, user_id: str
    ) -> MembershipRemovalResult:
        """Remove ``user_id`` from ``organization_id`` and revoke their tokens.

        Order is **delete, then revoke**, matching every other revoking write in
        this codebase (``UserService.reset_password`` carries the reasoning):
        revoking first opens a window in which a login mints a token with an
        ``iat`` *after* the watermark and *before* the delete — a token carrying
        the membership this call is removing, which then survives the very
        revocation meant to kill it. Under SSO the window is worse than
        theoretical: a JIT login in the gap re-adds the membership row outright.

        The watermark is bumped **even when no row was deleted**. That is what
        makes a failed run recoverable: if the delete landed and the revocation
        did not, re-running finds no row and must still revoke, or the retry
        would report "not a member" and leave the tokens alive. The cost is that
        removing a non-member ends that user's sessions; they can log in again,
        which is strictly better than the alternative failure.

        Args:
            organization_id: Organization to remove the membership from.
            user_id: User to remove.

        Returns:
            :class:`MembershipRemovalResult` — whether a row was deleted, and the
            instant before which this user's tokens are now invalid.

        Raises:
            MembershipRemovalMisscoped: The bound tenant context names a
                different organization, so the delete would silently match
                nothing. Raised before any write.
            MembershipRemovalIncomplete: The delete landed but the watermark was
                not written. The caller must NOT report the removal as complete;
                re-running finishes it.
        """
        # The DELETE is RLS-filtered by the bound context, so a mismatch makes it
        # a no-op that reports as "was not a member". Checking here rather than
        # trusting each caller is the point of being the chokepoint: a caller
        # that forgot to bind gets an error, not a silent success.
        bound_org_id = get_current_org_id()
        if bound_org_id != organization_id:
            raise MembershipRemovalMisscoped(
                f"Refusing to remove a membership in organization {organization_id} "
                f"while the tenant context is bound to {bound_org_id}: the delete is "
                "RLS-filtered by the bound context and would match nothing, which is "
                "indistinguishable from 'was not a member'. Bind the context to the "
                "target organization (set_current_org_id) before calling."
            )

        membership_removed = await self._orgs.remove_member(organization_id, user_id)

        try:
            revoked_before = await self._auth_service.revoke_user_tokens(user_id)
        except Exception as exc:
            # The delete has already landed. Report the half-state precisely —
            # "revocation failed" alone would read as "nothing happened" and
            # invite a caller to retry the wrong thing, or to report the removal
            # as done. (#767 posture: never report a revocation that did not land.)
            logger.error(
                "Membership removed but token revocation FAILED — outstanding "
                "tokens remain valid",
                extra={
                    "organization_id": organization_id,
                    "user_id": user_id,
                    "membership_removed": membership_removed,
                },
            )
            raise MembershipRemovalIncomplete(
                f"User {user_id} was removed from organization {organization_id}, "
                f"but their token revocation watermark could not be written: {exc}. "
                "Their outstanding tokens are STILL VALID. Re-run the removal — it "
                "is idempotent and will bump the watermark even though the "
                "membership row is already gone."
            ) from exc

        logger.info(
            "Organization membership removed and tokens revoked",
            extra={
                "organization_id": organization_id,
                "user_id": user_id,
                "membership_removed": membership_removed,
                "revoked_before": revoked_before.isoformat(),
            },
        )
        return MembershipRemovalResult(
            membership_removed=membership_removed,
            revoked_before=revoked_before,
        )

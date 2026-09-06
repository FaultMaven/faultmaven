"""Organization-membership writes, paired with per-user token revocation (#874, #1042).

The problem this exists to close
--------------------------------
Organization membership **and the member's role** are verified at **login** only.
Both are minted into the token (``organization_id``, ``roles``) and nothing
re-reads ``organization_members`` on the request path. So writing that row stops
*future* logins from carrying the old answer while leaving every outstanding
token working until it expires — up to ``JWT_REFRESH_TOKEN_EXPIRY_DAYS``.

Two writes have that shape, and both are here:

* **removal** (#874) — "removed from the organization" has to mean "outstanding
  tokens die";
* **role change** (#1042) — "demoted from admin" has to mean "the elevated token
  dies", or a demoted admin keeps admin claims for the life of their refresh
  token.

The only mechanism that ends a live session is the per-user revocation watermark
(``AuthService.revoke_user_tokens``, the single deployment-wide store, #767/#825).

Before this service the pairing was a *convention*: the runbook
(``docs/operations/sso-org-provisioning.md``) told an operator to do both, and
nothing enforced it. A convention that has to be remembered at 3am, on the one
procedure whose whole purpose is to cut off access, is not a control.

Why the pairing lives here and not in the caller
------------------------------------------------
``IOrganizationRepository.remove_member`` / ``.update_member_role`` are core;
their callers are not. The Cloud admin console (``faultmaven_cloud``, ADR-010 D7)
drives them from the composed proprietary module, and an operator drives removal
from a pod. Wiring revocation into any one of those leaves the others unpaired
and leaves the repository methods a footgun for the next caller. One operation in
core that does both — and which every writer goes through — makes "written ⇒
revoked" a property of the operation rather than a step each caller must
remember.

``tests/unit/modules/auth/test_membership_writes_are_paired.py`` is the tripwire
that keeps it that way inside this repository, for both writes.

Why role change revokes unconditionally
---------------------------------------
A **promotion** does not need revocation for safety: the outstanding token is
*less* privileged than the new state and self-corrects on the next mint. Only a
demotion is dangerous. Revoking on some role changes and not others would mean
this service had to decide which direction a given ``role_id`` move is — a
question with no stable answer once roles stop being a three-element total order,
and one whose wrong answer is a silent privilege leak. Revoking every time costs
a re-login after a promotion and cannot be got wrong.

Not yet every writer
--------------------
The tripwire cannot see ``faultmaven-cloud``, so the composed module carries its
own obligation to call this service rather than the repositories, and the state
of the two paths differs:

* ``DELETE /api/v1/admin/organization/members/{user_id}`` **is** wired
  (faultmaven-cloud#17, merged);
* ``PATCH`` — the role change — is written against this service but **not yet
  merged**. Until it is, changing a member's role *through the admin console*
  still writes the row without revoking, and a demoted admin keeps elevated
  claims until their refresh token expires. That is the bug #1042 exists to
  close, still open on the API path.

Note also that ``set_member_role`` has **no caller in this repository** — unlike
``remove_member``, which ``fm-remove-org-member`` drives. Its only production
caller lives in the Cloud repo, which is precisely the caller the tripwire
cannot see.

Say this from evidence rather than intent. A docstring that describes the
intended end state would let the next reader assume a containment control is in
place while it is not — and this is the file they would check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from faultmaven.config.tenant_context import get_current_enterprise_id
from faultmaven.models.interfaces_user import IOrganizationRepository

logger = logging.getLogger(__name__)


class MembershipWriteMisscoped(Exception):
    """The bound tenant context does not name the organization being written to.

    ``organization_members`` is RLS-tenanted on ``enterprise_id`` (ADR-017 D1)
    and every write is filtered by ``app.current_enterprise_id``, so a call whose
    context names a *different* enterprise matches no row — and the repository
    reports that as ``rowcount == 0``, indistinguishable from "was not a member".
    The caller would then read a silent no-op as a completed write while the row
    survives untouched and only the tokens were revoked.

    The check is on the ENTERPRISE rather than the organization because that is
    what the policy filters by: an organization is a row *inside* a tenant, and
    two organizations of one enterprise are equally reachable from one binding.

    Always raised **before** either write, so nothing has happened when it
    surfaces. Subclassed per operation so a caller can report which write it
    refused to make; catch this base to handle both.
    """


class MembershipRemovalMisscoped(MembershipWriteMisscoped):
    """A removal was refused because the tenant context names another enterprise.

    The DELETE would have matched nothing and reported as "was not a member",
    putting the user back in on their next login while the caller believed
    access was cut.
    """


class MembershipRoleChangeMisscoped(MembershipWriteMisscoped):
    """A role change was refused: the tenant context names another enterprise.

    The UPDATE would have matched nothing and reported as "not a member", so a
    demotion would read as "already handled" while the member kept the role the
    call was taking away.
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


class MembershipRoleChangeIncomplete(Exception):
    """The role was changed but the revocation watermark was NOT written.

    The role-change half-state (#1042), and the one that matters: on a
    **demotion** the stored role is now the lesser one while the member's
    outstanding tokens still carry the elevated ``roles`` claim. The console
    shows them demoted; the API still treats them as what they were.

    Recovery is to re-run the same call with the same target role:
    :meth:`OrganizationMembershipService.set_member_role` is idempotent — setting
    a role to the value it already holds still matches the row — and it revokes
    even when no row matched, so the retry completes the operation instead of
    reporting "not a member" and leaving the elevated tokens alive.
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


@dataclass(frozen=True)
class MembershipRoleChangeResult:
    """What a role change actually did.

    Attributes:
        role_changed: True if a membership row matched and was written. False
            means the user is not a member of this organization — the watermark
            was still bumped (see
            :meth:`OrganizationMembershipService.set_member_role`), so this
            distinguishes "role set now" from "finished a previous half-run"
            without either being an error. Note it reports *matched*, not
            *differed*: setting a role to the value it already holds returns
            True, because the row was there and the write landed.
        revoked_before: The revocation instant. Every token for this user issued
            at or before it is now invalid — including any still carrying the
            role this call replaced.
    """

    role_changed: bool
    revoked_before: datetime


class OrganizationMembershipService:
    """Write an organization membership and end the member's live sessions, as one step."""

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
                unpaired writes this class exists to prevent (#874, #1042).
        """
        if auth_service is None:
            raise ValueError(
                "OrganizationMembershipService requires an auth_service: without "
                "one it cannot revoke, and an unrevoked membership write leaves "
                "live tokens carrying the membership or role it just changed "
                "(#874, #1042)."
            )
        self._orgs = organization_repository
        self._auth_service = auth_service

    def _require_bound_to(
        self,
        enterprise_id: str,
        *,
        write: str,
        misscoped: type[MembershipWriteMisscoped],
    ) -> None:
        """Refuse a write the bound tenant context would silently turn into a no-op.

        Every ``organization_members`` write is RLS-filtered by the bound
        context, so a mismatch makes it match nothing — and the repository
        reports that identically to "was not a member". Checking here rather
        than trusting each caller is the point of being the chokepoint: a caller
        that forgot to bind gets an error, not a silent success.
        """
        bound_enterprise_id = get_current_enterprise_id()
        if bound_enterprise_id != enterprise_id:
            raise misscoped(
                f"Refusing to {write} in enterprise {enterprise_id} while the "
                f"tenant context is bound to {bound_enterprise_id}: the write is "
                "RLS-filtered by the bound context and would match nothing, which "
                "is indistinguishable from 'was not a member'. Bind the context to "
                "the target enterprise (set_current_enterprise_id) before calling."
            )

    async def _revoke_or_report_half_state(
        self,
        *,
        organization_id: str,
        user_id: str,
        incomplete: type[Exception],
        what_landed: str,
        still_valid: str,
        retry_hint: str,
        log_extra: dict,
    ) -> datetime:
        """Bump the watermark, or raise ``incomplete`` naming the half-state.

        The write has already landed by the time this runs, so a bare
        "revocation failed" would read as "nothing happened" and invite a caller
        to retry the wrong thing — or to report the operation as done. (#767
        posture: never report a revocation that did not land.)
        """
        try:
            return await self._auth_service.revoke_user_tokens(user_id)
        except Exception as exc:
            logger.error(
                "Membership write landed but token revocation FAILED — "
                "outstanding tokens remain valid",
                extra={
                    "organization_id": organization_id,
                    "user_id": user_id,
                    **log_extra,
                },
            )
            raise incomplete(
                f"{what_landed}, but their token revocation watermark could not "
                f"be written: {exc}. {still_valid} {retry_hint}"
            ) from exc

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
        self._require_bound_to(
            organization_id,
            write="remove a membership",
            misscoped=MembershipRemovalMisscoped,
        )

        membership_removed = await self._orgs.remove_member(organization_id, user_id)

        revoked_before = await self._revoke_or_report_half_state(
            organization_id=organization_id,
            user_id=user_id,
            incomplete=MembershipRemovalIncomplete,
            what_landed=(
                f"User {user_id} was removed from organization {organization_id}"
            ),
            still_valid="Their outstanding tokens are STILL VALID.",
            retry_hint=(
                "Re-run the removal — it is idempotent and will bump the "
                "watermark even though the membership row is already gone."
            ),
            log_extra={"membership_removed": membership_removed},
        )

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

    async def set_member_role(
        self, organization_id: str, user_id: str, role_id: str
    ) -> MembershipRoleChangeResult:
        """Set ``user_id``'s role in ``organization_id`` and revoke their tokens (#1042).

        The unfixed sibling of :meth:`remove_member`. The member's role is minted
        into the ``roles`` claim at login and never re-read on the request path,
        so writing ``organization_members.role_id`` on its own leaves a demoted
        admin holding **elevated** claims until their refresh token expires. This
        is the org-scoped axis of the pairing ``UserService.assign_role`` /
        ``remove_role`` already do on the deployment-scoped one.

        Named ``set_member_role`` rather than ``update_member_role`` on purpose:
        the repository method it wraps has the latter name, and keeping them
        distinct is what lets the tripwire read every ``.update_member_role(``
        call site as an unpaired repository write with no ambiguity about the
        receiver.

        Order is **write, then revoke**, for the same reason removal deletes
        first: revoking first opens a window in which a login mints a token whose
        ``iat`` is past the watermark but which still reads the *old* role — a
        token that survives the very revocation meant to kill it, carrying
        exactly the privilege this call is taking away.

        Revocation is **unconditional**, including on a promotion and including
        when no row matched — see the module docstring for why direction is not
        this service's question to answer, and :meth:`remove_member` for why
        revoking on a no-match is what makes an interrupted run finishable.

        Args:
            organization_id: Organization the membership belongs to.
            user_id: Member whose role is being set.
            role_id: Role to store. This is the stable ``role_id`` (see
                ``faultmaven.models.rbac_seed.SYSTEM_ROLE_IDS``), not a role
                name; resolving a name is the caller's job, and doing it here
                would put policy in the chokepoint. An unknown id fails in the
                repository *before* the revocation, so it leaves no half-state.

        Returns:
            :class:`MembershipRoleChangeResult` — whether a row matched, and the
            instant before which this user's tokens are now invalid.

        Raises:
            MembershipRoleChangeMisscoped: The bound tenant context names a
                different organization, so the update would silently match
                nothing. Raised before any write.
            MembershipRoleChangeIncomplete: The role was written but the
                watermark was not. The caller must NOT report the change as
                complete — on a demotion the member still holds the old role in
                every outstanding token. Re-running finishes it.
        """
        self._require_bound_to(
            organization_id,
            write="change a member's role",
            misscoped=MembershipRoleChangeMisscoped,
        )

        role_changed = await self._orgs.update_member_role(
            organization_id, user_id, role_id
        )

        revoked_before = await self._revoke_or_report_half_state(
            organization_id=organization_id,
            user_id=user_id,
            incomplete=MembershipRoleChangeIncomplete,
            what_landed=(
                f"User {user_id}'s role in organization {organization_id} was set "
                f"to {role_id}"
            ),
            still_valid=(
                "Their outstanding tokens are STILL VALID and still carry the "
                "PREVIOUS role — if this was a demotion, they still hold it."
            ),
            retry_hint=(
                "Re-run the same role change — it is idempotent and will bump "
                "the watermark even though the role is already stored."
            ),
            log_extra={"role_id": role_id, "role_changed": role_changed},
        )

        logger.info(
            "Organization member role set and tokens revoked",
            extra={
                "organization_id": organization_id,
                "user_id": user_id,
                "role_id": role_id,
                "role_changed": role_changed,
                "revoked_before": revoked_before.isoformat(),
            },
        )
        return MembershipRoleChangeResult(
            role_changed=role_changed,
            revoked_before=revoked_before,
        )

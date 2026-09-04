"""WorkOS AuthKit implementation of the SSO identity provider seam.

This is the only module that talks to the ``workos`` SDK, and the import is
deferred to :meth:`WorkOSIdentityProvider.from_config` so the module stays
import-safe (and unit-testable with a fake client) even where the SDK is not
installed. The DI factory constructs it only in cloud/oauth deployments with
WorkOS configured; standalone never installs the SDK.

See ADR-015 (WorkOS AuthKit cloud identity).
"""

from __future__ import annotations

from typing import Any

import jwt as jwt_lib
import structlog

from faultmaven.modules.auth.contracts import (
    ISSOIdentityProvider,
    ISSOTenantRetirementProvider,
    RetiredIdPOrganization,
    SSOIdentity,
)
from faultmaven.modules.auth.exceptions import (
    SSOAuthenticationError,
    SSOProvisioningError,
)

logger = structlog.get_logger(__name__)

PROVIDER_NAME = "workos"


def _conflict_errors() -> tuple[type[BaseException], ...]:
    """The WorkOS error classes a duplicate unique field can arrive as.

    Resolved lazily, because this module must stay import-safe without the SDK
    (standalone never installs it) — the same reason ``from_config`` defers its
    import.
    """
    from workos import ConflictError, UnprocessableEntityError

    return (ConflictError, UnprocessableEntityError)


def _membership_statuses() -> list[str]:
    """Every membership state the SDK's enum defines, as wire values.

    Derived from the enum rather than spelled, so a state added by a future SDK
    is included automatically instead of silently narrowing the retry check.
    """
    from workos.organization_membership import (
        UserManagementOrganizationMembershipStatuses,
    )

    return [s.value for s in UserManagementOrganizationMembershipStatuses]


def _organization_id_of(organization: Any) -> str | None:
    """The organization's id, or None when it is absent or not a usable string.

    One extraction shared by the create and the lookup: they read the same field
    off the same model, and two copies of "is this a usable id?" is one copy too
    many.
    """
    organization_id = getattr(organization, "id", None)
    if not isinstance(organization_id, str) or not organization_id:
        return None
    return organization_id


# AuthKit is WorkOS's hosted, connection-agnostic login (SSO / social / password
# selected on the WorkOS side). Passing provider="authkit" yields the hosted page.
_AUTHKIT_PROVIDER = "authkit"


class WorkOSIdentityProvider(ISSOIdentityProvider, ISSOTenantRetirementProvider):
    """Hosted-login provider backed by WorkOS AuthKit (User Management).

    Implements two ports. :class:`ISSOIdentityProvider` is what the login flow
    holds; :class:`ISSOTenantRetirementProvider` is the operator-only teardown,
    kept separate so no login-path double ever acquires a destructive method.
    """

    def __init__(self, *, client: Any, redirect_uri: str) -> None:
        self._client = client
        self._redirect_uri = redirect_uri

    @classmethod
    def from_config(
        cls, *, api_key: str, client_id: str, redirect_uri: str
    ) -> WorkOSIdentityProvider:
        """Construct against a real WorkOS client.

        Imports the ``workos`` SDK lazily so importing this module never requires
        the dependency; only actually building a live provider does.
        """
        from workos import WorkOSClient

        client = WorkOSClient(api_key=api_key, client_id=client_id)
        return cls(client=client, redirect_uri=redirect_uri)

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAME

    def build_authorization_url(self, *, state: str) -> str:
        return self._client.user_management.get_authorization_url(
            provider=_AUTHKIT_PROVIDER,
            redirect_uri=self._redirect_uri,
            state=state,
        )

    def exchange_code(self, code: str) -> SSOIdentity:
        try:
            response = self._client.user_management.authenticate_with_code(code=code)
            return self._to_identity(response)
        except Exception as exc:
            # External SDK boundary: any exchange failure (WorkOSError, network,
            # unexpected response shape) is an auth failure. Never surface provider
            # detail to the caller — the callback must not become an error oracle.
            logger.warning("workos_code_exchange_failed", error=type(exc).__name__)
            raise SSOAuthenticationError("SSO code exchange failed") from exc

    def build_logout_url(
        self, *, provider_session_id: str, return_to: str | None = None
    ) -> str | None:
        if not provider_session_id:
            return None
        try:
            # ``return_to`` must be registered under Logout redirects in the
            # WorkOS dashboard; an unregistered value is refused rather than
            # honoured. Omitting it falls back to WorkOS's configured default
            # Logout URI, which is why the caller passes one explicitly — the
            # default is a dashboard setting nothing in this repo can assert.
            kwargs: dict[str, Any] = {"session_id": provider_session_id}
            if return_to:
                kwargs["return_to"] = return_to
            return self._client.user_management.get_logout_url(**kwargs)
        except Exception as exc:
            # Never raise from logout: the caller has already torn down the
            # FaultMaven session, and an exception here would surface as a
            # failed logout on a request that already succeeded in the part
            # that matters. Degrades to "AuthKit session outlives ours".
            logger.warning("workos_logout_url_failed", error=type(exc).__name__)
            return None

    def provision_personal_organization(
        self, *, provider_user_id: str, external_id: str, name: str
    ) -> str:
        """Get-or-create the WorkOS organization holding this one member (#1045).

        Two calls, each made idempotent by a different mechanism, in the order
        that makes a retry safe:

        1. **The organization**, keyed on ``external_id``. WorkOS enforces
           uniqueness on that field, so ``get_organization_by_external_id``
           finds whatever a previous attempt created and a lost create race is
           resolved by re-reading rather than by minting a second organization.
           The read comes first because the overwhelmingly common retry is "we
           already made it".
        2. **The membership**, keyed on the (user, organization) pair. WorkOS
           refuses a duplicate; a refusal is confirmed by listing the
           memberships rather than trusted, so "already a member" and "the write
           was rejected for another reason" cannot be conflated.

        Every failure becomes :class:`SSOProvisioningError` with no provider
        detail attached — this runs inside an unauthenticated callback, which
        must not become an error oracle.
        """
        organization_id = self._ensure_organization(external_id=external_id, name=name)
        self._ensure_membership(
            provider_user_id=provider_user_id, organization_id=organization_id
        )
        return organization_id

    def _ensure_organization(self, *, external_id: str, name: str) -> str:
        """Return the id of the organization carrying ``external_id``."""
        existing = self._organization_by_external_id(external_id)
        if existing is not None:
            return existing

        try:
            created = self._client.organizations.create_organization(
                name=name, external_id=external_id
            )
        except _conflict_errors() as exc:
            # A concurrent first login for the same subject won the create.
            # Both derived the same external id, so the winner's organization is
            # the one this login wants — read it back rather than fail.
            #
            # BOTH conflict classes are caught. A duplicate unique field is a
            # 409 in some WorkOS surfaces and a 422 in others, and which one a
            # duplicate ``external_id`` produces is NOT verified against the
            # live API (see the PR body). Catching only one would turn the
            # common retry into a permanent refusal, and catching both costs
            # nothing: the recovery is a re-read that either finds the winner or
            # re-raises.
            recovered = self._organization_by_external_id(external_id)
            if recovered is not None:
                return recovered
            logger.warning(
                "workos_personal_org_conflict_unresolved", error=type(exc).__name__
            )
            raise SSOProvisioningError(
                "Personal organization could not be provisioned"
            ) from exc
        except Exception as exc:
            logger.warning(
                "workos_personal_org_create_failed", error=type(exc).__name__
            )
            raise SSOProvisioningError(
                "Personal organization could not be provisioned"
            ) from exc

        organization_id = _organization_id_of(created)
        if organization_id is None:
            # An SDK that returned something without an id would otherwise let a
            # falsy value through into the mapping row.
            logger.warning("workos_personal_org_create_returned_no_id")
            raise SSOProvisioningError("Personal organization could not be provisioned")
        return organization_id

    def _organization_by_external_id(self, external_id: str) -> str | None:
        """Return the organization id for ``external_id``, or None if absent.

        ``None`` means *absent*, and only absent. Any other failure raises, so a
        provider outage cannot be read as "no organization yet" and answered by
        creating a duplicate.
        """
        from workos import NotFoundError

        try:
            found = self._client.organizations.get_organization_by_external_id(
                external_id
            )
        except NotFoundError:
            return None
        except Exception as exc:
            logger.warning(
                "workos_personal_org_lookup_failed", error=type(exc).__name__
            )
            raise SSOProvisioningError(
                "Personal organization could not be provisioned"
            ) from exc
        return _organization_id_of(found)

    def _ensure_membership(
        self, *, provider_user_id: str, organization_id: str
    ) -> None:
        """Make the subject a member of the organization; raise if it is not."""
        try:
            self._client.organization_membership.create_organization_membership(
                user_id=provider_user_id, organization_id=organization_id
            )
            return
        except Exception as exc:
            # Could be "already a member" (the retry case) or a genuine refusal.
            # Do not guess from the exception type — ask.
            if self._is_member(
                provider_user_id=provider_user_id, organization_id=organization_id
            ):
                return
            logger.warning(
                "workos_personal_org_membership_failed", error=type(exc).__name__
            )
            raise SSOProvisioningError(
                "Personal organization membership could not be established"
            ) from exc

    def _is_member(self, *, provider_user_id: str, organization_id: str) -> bool:
        """Whether the subject already holds a membership, in ANY state.

        ``statuses`` is passed explicitly and covers every value the enum
        defines. The SDK's default lists ``active`` only, so a ``pending`` or
        ``inactive`` membership left by an earlier attempt would read as "not a
        member" — and since the create that follows a refusal is the same create
        that was refused, every retry would refuse again, permanently. The retry
        has to tolerate every state the first attempt can legitimately leave.

        Fail-closed on any error: unknown is not "already a member".
        """
        try:
            page = self._client.organization_membership.list_organization_memberships(
                organization_id=organization_id,
                user_id=provider_user_id,
                statuses=_membership_statuses(),
                limit=1,
            )
        except Exception as exc:
            logger.warning(
                "workos_personal_org_membership_check_failed",
                error=type(exc).__name__,
            )
            return False
        return bool(getattr(page, "data", None))

    # -- operator teardown (ISSOTenantRetirementProvider) -------------------- #

    def retire_personal_organization(
        self, *, provider_org_id: str
    ) -> RetiredIdPOrganization:
        """Remove the memberships and the organization named by ``provider_org_id``.

        Addressed by the **recorded** id, never by one re-derived from the
        subject. The derived ``external_id`` is a function of the subject, so a
        later tenant of the same subject answers to it as well — a retirement
        aimed at a retired predecessor would then delete the live successor's
        organization, and report success for it.

        Idempotent in both steps, and honest about which one did anything: an
        organization that is already gone comes back ``organization_absent``,
        never ``organization_deleted``. Only a call that FAILED raises.
        """
        memberships_deleted, absent = self._delete_memberships(
            organization_id=provider_org_id
        )
        if absent:
            return RetiredIdPOrganization(
                organization_absent=True,
                memberships_deleted=0,
                organization_deleted=False,
            )
        deleted = self._delete_organization(provider_org_id)
        return RetiredIdPOrganization(
            organization_absent=not deleted,
            memberships_deleted=memberships_deleted,
            organization_deleted=deleted,
        )

    def _delete_memberships(self, *, organization_id: str) -> tuple[int, bool]:
        """Delete the organization's memberships. Returns (deleted, absent).

        ``statuses`` covers every value the SDK's enum defines, for the same
        reason the provisioning check does: the default lists ``active`` only, so
        a ``pending`` or ``inactive`` membership left by an earlier attempt would
        be invisible here and survive a retirement that reported success.

        A ``NotFoundError`` from the listing means the organization itself is
        gone — reported as absent rather than as zero memberships, so the caller
        does not then try to delete it and call that a success.
        """
        from workos import NotFoundError

        try:
            page = self._client.organization_membership.list_organization_memberships(
                organization_id=organization_id,
                statuses=_membership_statuses(),
                limit=100,
            )
        except NotFoundError:
            return 0, True
        except Exception as exc:
            logger.warning(
                "workos_personal_org_membership_list_failed",
                error=type(exc).__name__,
            )
            raise SSOProvisioningError(
                "Personal organization memberships could not be listed"
            ) from exc

        deleted = 0
        for membership in getattr(page, "data", None) or []:
            membership_id = getattr(membership, "id", None)
            if not isinstance(membership_id, str) or not membership_id:
                continue
            try:
                self._client.organization_membership.delete_organization_membership(
                    membership_id
                )
            except NotFoundError:
                # Already gone: a completed step, and not counted as one this
                # run performed.
                continue
            except Exception as exc:
                logger.warning(
                    "workos_personal_org_membership_delete_failed",
                    error=type(exc).__name__,
                )
                raise SSOProvisioningError(
                    "Personal organization membership could not be removed"
                ) from exc
            deleted += 1
        return deleted, False

    def _delete_organization(self, organization_id: str) -> bool:
        """True when this call deleted it; False when it was already gone."""
        from workos import NotFoundError

        try:
            self._client.organizations.delete_organization(organization_id)
        except NotFoundError:
            return False
        except Exception as exc:
            logger.warning(
                "workos_personal_org_delete_failed", error=type(exc).__name__
            )
            raise SSOProvisioningError(
                "Personal organization could not be removed"
            ) from exc
        return True

    def revoke_session(self, *, provider_session_id: str) -> bool:
        if not provider_session_id:
            return False
        try:
            self._client.user_management.revoke_session(session_id=provider_session_id)
            return True
        except Exception as exc:
            # Same contract as build_logout_url: the local token is already
            # revoked by the time this runs, so a provider failure must degrade
            # to "the IdP session outlived ours", never to a failed logout.
            logger.warning("workos_revoke_session_failed", error=type(exc).__name__)
            return False

    def _to_identity(self, response: Any) -> SSOIdentity:
        user = response.user
        return SSOIdentity(
            provider=PROVIDER_NAME,
            provider_user_id=user.id,
            email=user.email,
            email_verified=bool(user.email_verified),
            display_name=_display_name(user),
            organization_id=getattr(response, "organization_id", None),
            provider_session_id=_session_id_of(getattr(response, "access_token", None)),
        )


def _session_id_of(access_token: Any) -> str | None:
    """Read the WorkOS session id (``sid``) out of the AuthKit access token.

    WorkOS does not return the session id as its own field; it is a claim inside
    the access token the code exchange returns, and it is what ``get_logout_url``
    requires.

    Decoded **without signature verification, deliberately**. This token arrived
    over TLS as the direct response to our own server-side exchange, and nothing
    here is an authorization decision — the claim is an opaque handle we hand
    straight back to WorkOS. FaultMaven's own session is minted separately from
    the identity, never from this token.

    Returns ``None`` for anything unreadable. A missing session id costs
    single-logout, which is strictly better than costing the login.
    """
    if not isinstance(access_token, str) or not access_token:
        return None
    try:
        claims = jwt_lib.decode(
            access_token,
            options={"verify_signature": False, "verify_exp": False},
        )
    except Exception as exc:
        logger.warning("workos_access_token_undecodable", error=type(exc).__name__)
        return None
    sid = claims.get("sid")
    return sid if isinstance(sid, str) and sid else None


def _display_name(user: Any) -> str | None:
    """Best available human name: WorkOS ``name``, else first + last, else None."""
    name = getattr(user, "name", None)
    if name:
        return name
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    joined = " ".join(part for part in parts if part)
    return joined or None

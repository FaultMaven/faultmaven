"""Service-account credential provisioning (ADR-012 D10).

At ``AUTH_MODE=oauth`` passwordless dev-login is not mounted, so a non-human
actor — today the single global Slack service account — has no way to obtain a
token. D10's decision is to reuse the existing ``refresh_token`` grant rather
than build a client-credentials/M2M flow: an operator mints one initial refresh
token out of band, the agent presents it to refresh, and each refresh rotates
the credential forward. No human interaction after the bootstrap.

The window is *sliding*, not absolute: every refresh mints a fresh
``JWT_REFRESH_TOKEN_EXPIRY_DAYS``-day token, so a continuously running agent never
expires. An agent that stays down longer than that window — or that loses its
rotated token — is locked out, and re-running this provisioning step is the
recovery path.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import jwt

from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    account_may_hold_credentials,
    capture_state_read_at,
)

# There are exactly two kinds of account (ADR-017 D6): a human, and an agent
# acting for an integration. A team is a group of accounts, never an account,
# and the vocabulary that called this one a team is retired from every
# identifier and every document — it is the naming that produced the conflation
# ADR-017 untangles.
SERVICE_ACCOUNT_KIND = "service"
INDIVIDUAL_ACCOUNT_KIND = "individual"
VALID_ACCOUNT_KINDS = frozenset({INDIVIDUAL_ACCOUNT_KIND, SERVICE_ACCOUNT_KIND})

#: Which integration a service account serves. A separate attribute from the
#: kind, so a second integration is a new value here rather than a third account
#: kind — which is what "slack" as an account_kind would have forced.
SLACK_SERVICE_CHANNEL = "slack"
VALID_SERVICE_CHANNELS = frozenset({SLACK_SERVICE_CHANNEL})


class ServiceAccountProvisioningError(Exception):
    """Raised when a service-account credential cannot be issued."""


@dataclass(frozen=True)
class ProvisionedCredential:
    """The outcome of a provisioning run.

    Attributes:
        user: The service account the credential belongs to.
        refresh_token: The initial refresh token. Sensitive — returned once and
            never persisted server-side (refresh tokens are stateless JWTs; only
            revocations are stored).
        expires_at: When this specific token expires if it is never used.
        account_created: True if the account did not exist and was created.
        account_kind_corrected: True if the account existed with the wrong
            ``account_kind`` or ``service_channel`` and was corrected.
    """

    user: DevUser
    refresh_token: str
    expires_at: Optional[datetime]
    account_created: bool
    account_kind_corrected: bool


async def provision_service_account_credential(
    *,
    username: str,
    user_store: Any,
    token_generator: Any,
    account_kind: str = SERVICE_ACCOUNT_KIND,
    service_channel: Optional[str] = SLACK_SERVICE_CHANNEL,
    enterprise_id: Optional[str] = None,
) -> ProvisionedCredential:
    """Ensure a service account exists and mint it an initial refresh token.

    Idempotent with respect to the account: re-running against an existing
    account does not disturb it (beyond correcting a wrong ``account_kind``) and
    simply issues another credential. Re-running is therefore the documented
    lockout-recovery path.

    Note that issuing a new credential does NOT revoke a previously issued one:
    both remain valid until used or expired. That is deliberate — revoking on
    mint would make recovery destructive if an operator provisions while the
    agent is still running happily.

    Under a multi-tenant deployment ``enterprise_id`` is **required**: the
    credential's whole tenancy travels in its own claim chain (the ``users``
    table has no organization column), so a credential minted without an
    enterprise is
    dead on arrival.

    Args:
        username: Service account username (interim: ``slack-agent``).
        user_store: DevUser store (``get_user_by_username``/``create_user``/
            ``update_user``). Duck-typed so the domain layer stays free of an
            infrastructure import.
        token_generator: The deployment's JWT generator, whose
            ``generate_refresh_token`` must be the same one the request path
            validates with — otherwise the credential will not verify.
        account_kind: ADR-017 D6 account kind to enforce ('individual' or
            'service').
        service_channel: Which integration a 'service' account serves
            ('slack'); forced to ``None`` for an individual.
        enterprise_id: FaultMaven enterprise the credential acts within.
            Required under ``TENANT_PROVIDER=multi``, refused under
            single-tenant (which has exactly one tenant), and never the
            Standalone sentinel.

    Returns:
        ProvisionedCredential carrying the once-only refresh token.

    Raises:
        ServiceAccountProvisioningError: If the account is unusable (inactive),
            the requested enterprise is not one this deployment can mint for,
            or the credential cannot be minted.
    """
    if not username or not username.strip():
        raise ServiceAccountProvisioningError("username is required")
    username = username.strip()

    # A blank or whitespace-only flag value is *no* enterprise, not an empty
    # one — otherwise `-o ''` would slip past the multi-tenant requirement below
    # and mint the org-less credential it exists to prevent. Validated before the
    # user store is touched, so a refusal creates nothing.
    enterprise_id = (enterprise_id or "").strip() or None
    _validate_enterprise(enterprise_id)

    # Case derivation matches the channel exactly, so an unvalidated typo
    # ('Slack') would be persisted, reported as a successful correction, and
    # then silently stamp every case the account opens with the wrong source.
    # The kind now carries a CHECK constraint as well; the channel does not, so
    # this is the only gate it has.
    if account_kind not in VALID_ACCOUNT_KINDS:
        raise ServiceAccountProvisioningError(
            f"Unknown account_kind {account_kind!r}; "
            f"expected one of {sorted(VALID_ACCOUNT_KINDS)}"
        )
    if account_kind == INDIVIDUAL_ACCOUNT_KIND:
        # A human serves no integration. Refusing rather than silently dropping
        # the value keeps "which channel is this?" answerable from the row.
        service_channel = None
    elif service_channel not in VALID_SERVICE_CHANNELS:
        raise ServiceAccountProvisioningError(
            f"Unknown service_channel {service_channel!r}; "
            f"expected one of {sorted(VALID_SERVICE_CHANNELS)}"
        )

    if token_generator is None:
        raise ServiceAccountProvisioningError(
            "No JWT token generator is configured. Service-account credentials "
            "are an OAuth-mode concern: set AUTH_MODE=oauth and OAUTH_ENABLED=true "
            "(local mode still has dev-login)."
        )

    account_created = False
    account_kind_corrected = False

    # #831: before the account is read (or created).
    state_read_at = capture_state_read_at()

    user = await user_store.get_user_by_username(username)
    if user is None:
        user = await user_store.create_user(
            username=username,
            display_name=f"{username} (service account)",
            account_kind=account_kind,
            service_channel=service_channel,
        )
        account_created = True
    elif (
        getattr(user, "account_kind", None) != account_kind
        or getattr(user, "service_channel", None) != service_channel
    ):
        # An account provisioned before ADR-017, or one demoted by a code path
        # that round-tripped it through a model without these fields. Both are
        # corrected together: the channel alone decides the derived case
        # source, so an account carrying the right kind and a lost channel
        # would stamp every case it opens as a copilot case.
        user.account_kind = account_kind
        user.service_channel = service_channel
        user = await user_store.update_user(user)
        account_kind_corrected = True

    if not account_may_hold_credentials(user):
        # A refresh reloads the user and rejects inactive accounts, so a
        # credential minted here would be dead on arrival. Reactivating is not
        # this tool's call to make.
        raise ServiceAccountProvisioningError(
            f"Account '{username}' is inactive; a credential issued for it would "
            "be rejected on first use. Reactivate the account first."
        )

    if enterprise_id is not None:
        # Stamp the tenant on the user object the generator reads. Required
        # rather than optional: the repository model has no organization column
        # and ``DevUser.__post_init__`` stamps the Standalone sentinel on every
        # user the store returns, which under multi-tenant resolves to the empty
        # claim. Mirrors `/auth/refresh` step 2b (#869, #873).
        setattr(user, "enterprise_id", enterprise_id)

    refresh_token = await token_generator.generate_refresh_token(
        user, state_read_at=state_read_at
    )
    if not refresh_token:
        raise ServiceAccountProvisioningError(
            f"Token generator returned no refresh token for '{username}'"
        )

    return ProvisionedCredential(
        user=user,
        refresh_token=refresh_token,
        expires_at=_expiry_of(refresh_token),
        account_created=account_created,
        account_kind_corrected=account_kind_corrected,
    )


def _validate_enterprise(enterprise_id: Optional[str]) -> None:
    """Refuse a tenant this deployment cannot mint a live credential for.

    Refusing here — at mint — is half of the #850 invariant, whose other half is
    ``bind_request_enterprise_context`` refusing the same shapes at bind time. An
    operator finding out at provisioning time is the whole point: the failure is
    otherwise invisible until the agent's first API call is rejected.

    Args:
        enterprise_id: Normalized enterprise id (``None`` when omitted).

    Raises:
        ServiceAccountProvisioningError: If the id is the Standalone sentinel, if
            a multi-tenant deployment was given no enterprise, or if a
            single-tenant deployment was given one.
    """
    # Deferred: tenancy config pulls in settings, which must not be imported at
    # auth-module import time (same discipline as ``resolve_enterprise_claim``).
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )
    from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

    if enterprise_id == SingleTenantProvider.DEFAULT_ENTERPRISE_ID:
        raise ServiceAccountProvisioningError(
            f"enterprise_id {enterprise_id!r} is the Standalone sentinel enterprise, "
            "which identifies the single-tenant deployment itself and is not a "
            "tenant. Pass the tenant's own enterprise id, or omit the flag on a "
            "single-tenant deployment."
        )

    is_multi_tenant = requested_tenant_provider() == BUILTIN_MULTI

    if is_multi_tenant and enterprise_id is None:
        raise ServiceAccountProvisioningError(
            "This deployment is multi-tenant (TENANT_PROVIDER=multi), so a "
            "service-account credential must name the enterprise it acts "
            "within: an unanchored credential resolves to an empty enterprise "
            "claim and every request it makes is refused at "
            "bind_request_enterprise_context. Re-run with "
            "--enterprise-id <enterprise-id> (the id fm-provision-sso-org reported for "
            "that tenant, or the one in your operator records)."
        )

    if not is_multi_tenant and enterprise_id is not None:
        raise ServiceAccountProvisioningError(
            "This deployment is single-tenant, which has exactly one tenant, so "
            f"enterprise_id {enterprise_id!r} cannot be honoured. Omit the "
            "--enterprise-id flag."
        )


def _expiry_of(token: str) -> Optional[datetime]:
    """Read ``exp`` off the minted token.

    Read from the token itself rather than recomputed from settings, so what is
    reported is what was actually issued. Signature verification is irrelevant
    here — we just signed it.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None

    exp = claims.get("exp")
    if exp is None:
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)

"""Service-account credential provisioning (ADR-012 D10).

At ``AUTH_MODE=oauth`` passwordless dev-login is not mounted, so a non-human
actor — today the single global Slack service account — has no way to obtain a
token. D10's decision is to reuse the existing ``refresh_token`` grant rather
than build a client-credentials/M2M flow: an operator mints one initial refresh
token out of band, the agent presents it to refresh, and each refresh rotates
the credential forward. No human interaction after the bootstrap.

The window is *sliding*, not absolute: every refresh mints a fresh
``JWT_REFRESH_TOKEN_EXPIRY``-day token, so a continuously running agent never
expires. An agent that stays down longer than that window — or that loses its
rotated token — is locked out, and re-running this provisioning step is the
recovery path.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import jwt

from faultmaven.modules.auth.domain.models.auth import DevUser

# ADR-012 account kinds. 'slack' is the service account that owns a workspace's
# cases; 'individual' is a human.
SERVICE_ACCOUNT_KIND = "slack"


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
            account_kind and was corrected.
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

    Args:
        username: Service account username (interim: ``slack-agent``).
        user_store: DevUser store (``get_user_by_username``/``create_user``/
            ``update_user``). Duck-typed so the domain layer stays free of an
            infrastructure import.
        token_generator: The deployment's JWT generator, whose
            ``generate_refresh_token`` must be the same one the request path
            validates with — otherwise the credential will not verify.
        account_kind: ADR-012 account kind to enforce on the account.

    Returns:
        ProvisionedCredential carrying the once-only refresh token.

    Raises:
        ServiceAccountProvisioningError: If the account is unusable (inactive)
            or the credential cannot be minted.
    """
    if not username or not username.strip():
        raise ServiceAccountProvisioningError("username is required")
    username = username.strip()

    if token_generator is None:
        raise ServiceAccountProvisioningError(
            "No JWT token generator is configured. Service-account credentials "
            "are an OAuth-mode concern: set AUTH_MODE=oauth and OAUTH_ENABLED=true "
            "(local mode still has dev-login)."
        )

    account_created = False
    account_kind_corrected = False

    user = await user_store.get_user_by_username(username)
    if user is None:
        user = await user_store.create_user(
            username=username,
            display_name=f"{username} (service account)",
            account_kind=account_kind,
        )
        account_created = True
    elif getattr(user, "account_kind", None) != account_kind:
        # An account provisioned before ADR-012, or one demoted by a code path
        # that round-tripped it through a model without account_kind.
        user.account_kind = account_kind
        user = await user_store.update_user(user)
        account_kind_corrected = True

    if not user.is_active:
        # A refresh reloads the user and rejects inactive accounts, so a
        # credential minted here would be dead on arrival. Reactivating is not
        # this tool's call to make.
        raise ServiceAccountProvisioningError(
            f"Account '{username}' is inactive; a credential issued for it would "
            "be rejected on first use. Reactivate the account first."
        )

    refresh_token = await token_generator.generate_refresh_token(user)
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

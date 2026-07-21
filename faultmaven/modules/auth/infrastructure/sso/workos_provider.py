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

import structlog

from faultmaven.modules.auth.contracts import ISSOIdentityProvider, SSOIdentity
from faultmaven.modules.auth.exceptions import SSOAuthenticationError

logger = structlog.get_logger(__name__)

PROVIDER_NAME = "workos"

# AuthKit is WorkOS's hosted, connection-agnostic login (SSO / social / password
# selected on the WorkOS side). Passing provider="authkit" yields the hosted page.
_AUTHKIT_PROVIDER = "authkit"


class WorkOSIdentityProvider(ISSOIdentityProvider):
    """Hosted-login provider backed by WorkOS AuthKit (User Management)."""

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

    def _to_identity(self, response: Any) -> SSOIdentity:
        user = response.user
        return SSOIdentity(
            provider=PROVIDER_NAME,
            provider_user_id=user.id,
            email=user.email,
            email_verified=bool(user.email_verified),
            display_name=_display_name(user),
            organization_id=getattr(response, "organization_id", None),
        )


def _display_name(user: Any) -> str | None:
    """Best available human name: WorkOS ``name``, else first + last, else None."""
    name = getattr(user, "name", None)
    if name:
        return name
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    joined = " ".join(part for part in parts if part)
    return joined or None

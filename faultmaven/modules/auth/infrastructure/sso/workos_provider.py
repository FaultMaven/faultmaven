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

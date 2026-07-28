"""End-to-end credential lifecycle for a service account (ADR-012 D10).

Exercises the real RS256 generator and the real OAuth service against real
``AuthSettings`` — no settings stubs. A ``Mock()`` settings object previously
hid a live defect here: the OAuth service read a field that only existed on
``SecuritySettings``, so every ``grant_type=refresh_token`` request raised
AttributeError → HTTP 500 in oauth mode while the unit tests stayed green.

What the Slack agent depends on, pinned here:

1. A provisioned credential authenticates (it validates as a refresh token).
2. Refreshing returns an access token AND a rotated refresh token.
3. The presented token is single-use — replaying it fails.
4. The rotated token works, so the window slides indefinitely.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from faultmaven.config.settings import AuthSettings, SecuritySettings, TenantProvider
from faultmaven.models.exceptions import InvalidGrantError
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    RS256JWTTokenGenerator,
)
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.domain.services.service_account_provisioning import (
    provision_service_account_credential,
)
from faultmaven.providers.tenancy import factory as tenancy_factory
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio

CLIENT_ID = "faultmaven-copilot"
REAL_ORG = "22222222-2222-2222-2222-222222222222"


def _rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


class _UserStore:
    """DevUser store, standing in for DatabaseUserStore.

    The composition root passes the user *store* as the OAuth service's
    ``user_repository``, so one object serves both roles here — as in production.
    """

    def __init__(self):
        self.users: dict[str, DevUser] = {}

    async def get_user_by_username(self, username: str) -> DevUser | None:
        for user in self.users.values():
            if user.username == username:
                return user
        return None

    async def create_user(
        self, username, email=None, display_name=None, account_kind="individual"
    ) -> DevUser:
        user = DevUser(
            user_id=f"user-{username}",
            username=username,
            email=email or f"{username}@faultmaven.example",
            display_name=display_name or username,
            created_at=datetime.now(timezone.utc),
            roles=["user"],
            account_kind=account_kind,
        )
        self.users[user.user_id] = user
        return user

    async def update_user(self, user: DevUser) -> DevUser:
        self.users[user.user_id] = user
        return user

    async def get(self, user_id: str) -> DevUser | None:
        return self.users.get(user_id)


@pytest.fixture
def auth_settings() -> AuthSettings:
    return AuthSettings(auth_mode="oauth", oauth_enabled=True)


@pytest.fixture
def revocation_store() -> InMemoryRevocationStore:
    return InMemoryRevocationStore()


@pytest.fixture
def token_generator(revocation_store) -> RS256JWTTokenGenerator:
    private_pem, public_pem = _rsa_keypair()
    security = SecuritySettings()
    return RS256JWTTokenGenerator(
        private_key=private_pem,
        public_key=public_pem,
        revocation_store=revocation_store,
        settings=security,
        issuer=security.jwt_issuer,
        audience=security.jwt_audience,
    )


@pytest.fixture
def user_store() -> _UserStore:
    return _UserStore()


@pytest.fixture
def oauth_service(user_store, token_generator, auth_settings) -> OAuthServiceImpl:
    return OAuthServiceImpl(
        code_repository=None,
        user_repository=user_store,
        token_generator=token_generator,
        settings=auth_settings,
    )


@pytest.fixture
def as_tenant_provider(monkeypatch):
    """Drive the real ``TenantProvider`` enum through the real coercion path."""

    def _apply(provider: TenantProvider):
        settings = MagicMock()
        settings.providers.tenant_provider = provider
        monkeypatch.setattr(tenancy_factory, "get_settings", lambda: settings)

    return _apply


@pytest.fixture
async def credential(user_store, token_generator):
    return await provision_service_account_credential(
        username="slack-agent",
        user_store=user_store,
        token_generator=token_generator,
    )


class TestServiceAccountRefreshLifecycle:
    async def test_provisioned_credential_validates(self, credential, token_generator):
        """The minted credential is a valid refresh token for the account."""
        claims = await token_generator.validate_refresh_token(credential.refresh_token)

        assert claims is not None
        assert claims["sub"] == "user-slack-agent"
        assert claims["type"] == "refresh"

    async def test_refresh_returns_access_and_rotated_refresh(
        self, credential, oauth_service
    ):
        """The exchange the agent runs on every renewal cycle."""
        tokens = await oauth_service.refresh_access_token(
            refresh_token=credential.refresh_token,
            client_id=CLIENT_ID,
        )

        assert tokens.access_token
        assert tokens.refresh_token
        assert tokens.refresh_token != credential.refresh_token
        assert tokens.user_id == "user-slack-agent"

    async def test_presented_token_is_single_use(self, credential, oauth_service):
        """Replaying the presented token fails — this is why the agent must
        persist the rotated token write-before-use, and why concurrent refreshes
        across replicas revoke each other."""
        await oauth_service.refresh_access_token(
            refresh_token=credential.refresh_token,
            client_id=CLIENT_ID,
        )

        with pytest.raises(InvalidGrantError):
            await oauth_service.refresh_access_token(
                refresh_token=credential.refresh_token,
                client_id=CLIENT_ID,
            )

    async def test_window_slides_across_successive_refreshes(
        self, credential, oauth_service
    ):
        """Each rotated token refreshes again, so a running agent never expires."""
        token = credential.refresh_token

        for _ in range(3):
            tokens = await oauth_service.refresh_access_token(
                refresh_token=token,
                client_id=CLIENT_ID,
            )
            assert tokens.refresh_token != token
            token = tokens.refresh_token

    async def test_access_token_authenticates_as_the_service_account(
        self, credential, oauth_service, token_generator
    ):
        """The access token the agent ends up calling the API with is valid."""
        tokens = await oauth_service.refresh_access_token(
            refresh_token=credential.refresh_token,
            client_id=CLIENT_ID,
        )

        claims = await token_generator.validate_access_token(tokens.access_token)

        assert claims is not None
        assert claims["sub"] == "user-slack-agent"
        assert claims["type"] == "access"

    async def test_a_deactivated_account_cannot_refresh(
        self, credential, oauth_service, user_store
    ):
        """Deactivation must stop the credential renewing.

        Per-user revocation (#769) invalidates the tokens an account already
        holds, but this liveness check is what stops a refresh credential on a
        deactivated account from minting new access tokens forever on a
        sliding window. The two are independent controls.
        """
        user_store.users["user-slack-agent"].is_active = False

        with pytest.raises(InvalidGrantError):
            await oauth_service.refresh_access_token(
                refresh_token=credential.refresh_token,
                client_id=CLIENT_ID,
            )

    async def test_the_replacement_is_minted_before_the_old_one_is_revoked(
        self, credential, oauth_service, token_generator
    ):
        """Revoking first would leave a caller holding a revoked credential and
        no replacement if the mint failed — a lockout only an operator can undo.
        """
        order: list[str] = []
        real_mint = token_generator.generate_refresh_token
        real_revoke = token_generator.revoke_refresh_token

        async def mint(user):
            order.append("mint")
            return await real_mint(user)

        async def revoke(token):
            order.append("revoke")
            return await real_revoke(token)

        token_generator.generate_refresh_token = mint
        token_generator.revoke_refresh_token = revoke

        await oauth_service.refresh_access_token(
            refresh_token=credential.refresh_token,
            client_id=CLIENT_ID,
        )

        assert order == ["mint", "revoke"]


class TestMultiTenantCredentialChain:
    """The tenant survives the whole chain under multi-tenant (#873).

    Mint and rotation are separate code paths — provisioning stamps the org on
    the user before minting, the oauth refresh grant re-attaches the presented
    token's claim before re-minting. Either one missing leaves the credential
    org-less, which under multi-tenant is refused at
    ``bind_request_org_context``. This walks the whole chain the Slack agent
    walks, end to end.
    """

    async def test_the_organization_rides_mint_and_rotation(
        self, as_tenant_provider, user_store, token_generator, oauth_service
    ):
        as_tenant_provider(TenantProvider.MULTI)

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=user_store,
            token_generator=token_generator,
            organization_id=REAL_ORG,
        )
        minted = jwt.decode(
            credential.refresh_token, options={"verify_signature": False}
        )
        assert minted["organization_id"] == REAL_ORG

        tokens = await oauth_service.refresh_access_token(
            refresh_token=credential.refresh_token,
            client_id=CLIENT_ID,
        )

        for token in (tokens.access_token, tokens.refresh_token):
            claims = jwt.decode(token, options={"verify_signature": False})
            assert claims["organization_id"] == REAL_ORG

"""Service-account credential provisioning (ADR-012 D10).

Covers the backend half of D10: an operator can mint a service account an
initial refresh token that authenticates under AUTH_MODE=oauth, and re-running
the step is a safe recovery path.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import TenantProvider
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    HS256JWTTokenGenerator,
)
from faultmaven.modules.auth.domain.services.service_account_provisioning import (
    ServiceAccountProvisioningError,
    provision_service_account_credential,
)
from faultmaven.providers.tenancy import factory as tenancy_factory
from tests.utils import InMemoryRevocationStore

pytestmark = pytest.mark.asyncio

SECRET = "test-secret-key-that-is-long-enough-for-hs256"
REAL_ORG = "22222222-2222-2222-2222-222222222222"


def _user(username: str = "slack-agent", **overrides) -> DevUser:
    defaults = dict(
        user_id="user-123",
        username=username,
        email=f"{username}@faultmaven.example",
        display_name=username,
        created_at=datetime.now(timezone.utc),
        roles=["user"],
        account_kind="service",
        service_channel="slack",
    )
    defaults.update(overrides)
    return DevUser(**defaults)


class _FakeUserStore:
    """Minimal DevUser store: only what provisioning touches."""

    def __init__(self, existing: DevUser | None = None):
        self.users = {existing.username: existing} if existing else {}
        self.created: list[dict] = []
        self.updated: list[DevUser] = []

    async def get_user_by_username(self, username: str) -> DevUser | None:
        return self.users.get(username)

    async def create_user(
        self,
        username,
        email=None,
        display_name=None,
        account_kind="individual",
        service_channel=None,
    ) -> DevUser:
        self.created.append(
            {
                "username": username,
                "account_kind": account_kind,
                "service_channel": service_channel,
            }
        )
        user = _user(
            username,
            account_kind=account_kind,
            service_channel=service_channel,
            display_name=display_name,
        )
        self.users[username] = user
        return user

    async def update_user(self, user: DevUser) -> DevUser:
        self.updated.append(user)
        self.users[user.username] = user
        return user


def _token_generator(expires_in_days: int = 7):
    """A generator that mints a real, decodable refresh JWT."""

    async def generate_refresh_token(user, *, state_read_at):
        return jwt.encode(
            {
                "sub": user.user_id,
                "type": "refresh",
                "exp": datetime.now(timezone.utc) + timedelta(days=expires_in_days),
            },
            SECRET,
            algorithm="HS256",
        )

    return SimpleNamespace(generate_refresh_token=generate_refresh_token)


def _real_token_generator() -> HS256JWTTokenGenerator:
    """The real generator, so the ``organization_id`` claim comes from the real
    ``resolve_enterprise_claim`` rather than from a stub that could agree with
    a broken implementation."""
    return HS256JWTTokenGenerator(
        secret_key=SECRET,
        revocation_store=InMemoryRevocationStore(),
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


@pytest.fixture
def as_tenant_provider(monkeypatch):
    """Drive the real ``TenantProvider`` enum through the real coercion path.

    Patching ``get_settings`` (rather than ``requested_tenant_provider``) keeps
    ``coerce_provider_name`` in the loop, so a rename of the enum member breaks
    this test instead of silently passing a dead gate.
    """

    def _apply(provider: TenantProvider):
        settings = MagicMock()
        settings.providers.tenant_provider = provider
        monkeypatch.setattr(tenancy_factory, "get_settings", lambda: settings)

    return _apply


class TestProvisioning:
    async def test_creates_missing_account_as_service_account(self):
        """A fresh install has no slack-agent; it is created as a SERVICE account
        serving the 'slack' channel (ADR-017 D6).

        Two fields, not one: there are exactly two account kinds, and which
        integration a service account serves is a separate attribute — so a
        second integration is a new channel rather than a third kind, and it is
        the channel that decides the derived ``cases.source``.
        """
        store = _FakeUserStore()

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.account_created is True
        assert store.created == [
            {
                "username": "slack-agent",
                "account_kind": "service",
                "service_channel": "slack",
            }
        ]
        assert credential.user.account_kind == "service"
        assert credential.user.service_channel == "slack"

    async def test_mints_a_usable_refresh_token(self):
        """The credential is a refresh token for that account, with an expiry."""
        store = _FakeUserStore(_user())

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        claims = jwt.decode(credential.refresh_token, SECRET, algorithms=["HS256"])
        assert claims["sub"] == "user-123"
        assert claims["type"] == "refresh"
        assert credential.expires_at is not None
        assert credential.expires_at > datetime.now(timezone.utc)

    async def test_existing_account_is_reused_not_recreated(self):
        """Re-running is the lockout-recovery path: it must not disturb the account.

        The account keeps its user_id, so historical Slack cases (owned by
        user_id) stay attached to it.
        """
        store = _FakeUserStore(_user())

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.account_created is False
        assert store.created == []
        assert store.updated == []
        assert credential.user.user_id == "user-123"

    async def test_corrects_a_wrong_account_kind(self):
        """An account recorded as 'individual' is corrected, not left divergent."""
        store = _FakeUserStore(_user(account_kind="individual", service_channel=None))

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.account_kind_corrected is True
        assert store.updated[0].account_kind == "service"
        assert store.updated[0].service_channel == "slack"

    async def test_corrects_a_lost_service_channel(self):
        """The kind alone is not enough, and this is the half that bites.

        An account carrying the right kind and a NULL channel signs in, opens
        cases, and every one of them is stamped 'copilot' — permanently, because
        ``cases.source`` is immutable. The correction has to reach the channel.
        """
        store = _FakeUserStore(_user(account_kind="service", service_channel=None))

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.account_kind_corrected is True
        assert store.updated[0].service_channel == "slack"

    async def test_an_unknown_service_channel_is_refused(self):
        """The channel has no CHECK constraint, so this is its only gate.

        A typo ('Slack') would be persisted, reported as a successful
        correction, and then stamp every case the account opens as a copilot
        case.
        """
        store = _FakeUserStore()

        with pytest.raises(ServiceAccountProvisioningError, match="service_channel"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=store,
                token_generator=_token_generator(),
                service_channel="Slack",
            )

        assert store.created == []

    async def test_an_individual_serves_no_channel(self):
        """A human is not an integration, so the channel is forced to NULL
        rather than silently kept — "which channel is this?" has to stay
        answerable from the row."""
        store = _FakeUserStore()

        credential = await provision_service_account_credential(
            username="alice",
            user_store=store,
            token_generator=_token_generator(),
            account_kind="individual",
        )

        assert credential.user.account_kind == "individual"
        assert credential.user.service_channel is None
        assert store.created[0]["service_channel"] is None

    async def test_refuses_an_inactive_account(self):
        """A refresh reloads the user and rejects inactive accounts.

        Minting for one would hand the operator a credential that dies on first
        use — fail loudly at provisioning time instead.
        """
        store = _FakeUserStore(_user(is_active=False))

        with pytest.raises(ServiceAccountProvisioningError, match="inactive"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=store,
                token_generator=_token_generator(),
            )

    async def test_refuses_when_no_generator_is_configured(self):
        """Local mode registers no RS256 generator; say so instead of crashing."""
        with pytest.raises(ServiceAccountProvisioningError, match="AUTH_MODE=oauth"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=_FakeUserStore(_user()),
                token_generator=None,
            )

    async def test_does_not_revoke_previously_issued_credentials(self):
        """Recovery must not be destructive to a still-healthy running agent."""
        generator = _token_generator()
        generator.revoke_refresh_token = AsyncMock()

        await provision_service_account_credential(
            username="slack-agent",
            user_store=_FakeUserStore(_user()),
            token_generator=generator,
        )

        generator.revoke_refresh_token.assert_not_awaited()


class TestAccountKindValidation:
    async def test_rejects_an_unknown_account_kind(self):
        """The kind names one of exactly two things (ADR-017 D6), and a value
        outside that pair would be persisted, reported as a successful
        correction, and then read by everything that asks what this account is.

        The column carries a CHECK constraint as well; this refuses at the door,
        so the operator is told which value was wrong rather than reading an
        integrity error.
        """
        store = _FakeUserStore(_user())

        with pytest.raises(
            ServiceAccountProvisioningError, match="Unknown account_kind"
        ):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=store,
                token_generator=_token_generator(),
                account_kind="Service",  # capitalised typo
            )

        assert store.updated == []

    async def test_accepts_individual(self):
        credential = await provision_service_account_credential(
            username="someone",
            user_store=_FakeUserStore(),
            token_generator=_token_generator(),
            account_kind="individual",
        )

        assert credential.user.account_kind == "individual"


class TestOrganizationClaim:
    """The credential must carry its tenant (#873).

    ``DevUser.__post_init__`` stamps the Standalone sentinel on every user the
    store returns, and under multi-tenant the sentinel resolves to the *empty*
    claim — so a credential minted from a store-loaded user is refused at
    ``bind_request_enterprise_context`` on its very first request. The organization has
    to be stamped on the user before minting, exactly as `/auth/refresh` does.
    """

    async def test_multi_tenant_credential_carries_the_organization(
        self, as_tenant_provider
    ):
        as_tenant_provider(TenantProvider.MULTI)
        store = _FakeUserStore(_user())
        assert store.users["slack-agent"].enterprise_id == STANDALONE_ENTERPRISE_ID

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_real_token_generator(),
            enterprise_id=REAL_ORG,
        )

        assert _claims(credential.refresh_token)["enterprise_id"] == REAL_ORG

    async def test_a_surrounding_whitespace_only_organization_is_not_an_organization(
        self, as_tenant_provider
    ):
        """`-o ''` (or a shell-mangled value) must not read as "an org was given"
        and slip past the multi-tenant requirement."""
        as_tenant_provider(TenantProvider.MULTI)

        with pytest.raises(ServiceAccountProvisioningError, match="multi-tenant"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=_FakeUserStore(_user()),
                token_generator=_real_token_generator(),
                enterprise_id="   ",
            )

    async def test_organization_is_trimmed_before_it_reaches_the_claim(
        self, as_tenant_provider
    ):
        as_tenant_provider(TenantProvider.MULTI)

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=_FakeUserStore(_user()),
            token_generator=_real_token_generator(),
            enterprise_id=f"  {REAL_ORG}\n",
        )

        assert _claims(credential.refresh_token)["enterprise_id"] == REAL_ORG

    async def test_multi_tenant_refuses_without_an_organization(
        self, as_tenant_provider
    ):
        """An org-less credential is dead on arrival — say so at mint time, not
        by silent rejection of the agent's first API call."""
        as_tenant_provider(TenantProvider.MULTI)
        store = _FakeUserStore(_user())

        with pytest.raises(ServiceAccountProvisioningError, match="--enterprise-id"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=store,
                token_generator=_real_token_generator(),
            )

        # Refused before the store was touched.
        assert store.created == []
        assert store.updated == []

    @pytest.mark.parametrize("provider", [TenantProvider.SINGLE, TenantProvider.MULTI])
    async def test_the_standalone_sentinel_is_refused_in_every_mode(
        self, provider, as_tenant_provider
    ):
        """#850: the sentinel identifies the single-tenant *deployment* and is
        never a tenant. Refuse it at mint as well as at bind."""
        as_tenant_provider(provider)
        store = _FakeUserStore(_user())

        with pytest.raises(ServiceAccountProvisioningError, match="sentinel"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=store,
                token_generator=_real_token_generator(),
                enterprise_id=STANDALONE_ENTERPRISE_ID,
            )

        assert store.created == []
        assert store.updated == []

    async def test_single_tenant_refuses_an_organization(self, as_tenant_provider):
        """One tenant means there is nothing to choose; an id here is a
        misunderstanding worth surfacing rather than ignoring."""
        as_tenant_provider(TenantProvider.SINGLE)

        with pytest.raises(ServiceAccountProvisioningError, match="single-tenant"):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=_FakeUserStore(_user()),
                token_generator=_real_token_generator(),
                enterprise_id=REAL_ORG,
            )

    async def test_single_tenant_without_an_organization_is_unchanged(
        self, as_tenant_provider
    ):
        """Standalone regression guard: the sentinel claim still rides."""
        as_tenant_provider(TenantProvider.SINGLE)

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=_FakeUserStore(_user()),
            token_generator=_real_token_generator(),
        )

        assert (
            _claims(credential.refresh_token)["enterprise_id"]
            == STANDALONE_ENTERPRISE_ID
        )

"""Service-account credential provisioning (ADR-012 D10).

Covers the backend half of D10: an operator can mint a service account an
initial refresh token that authenticates under AUTH_MODE=oauth, and re-running
the step is a safe recovery path.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest

from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.service_account_provisioning import (
    ServiceAccountProvisioningError,
    provision_service_account_credential,
)

pytestmark = pytest.mark.asyncio

SECRET = "test-secret-key-that-is-long-enough-for-hs256"


def _user(username: str = "slack-agent", **overrides) -> DevUser:
    defaults = dict(
        user_id="user-123",
        username=username,
        email=f"{username}@faultmaven.example",
        display_name=username,
        created_at=datetime.now(timezone.utc),
        roles=["user"],
        account_kind="slack",
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
        self, username, email=None, display_name=None, account_kind="individual"
    ) -> DevUser:
        self.created.append({"username": username, "account_kind": account_kind})
        user = _user(username, account_kind=account_kind, display_name=display_name)
        self.users[username] = user
        return user

    async def update_user(self, user: DevUser) -> DevUser:
        self.updated.append(user)
        self.users[user.username] = user
        return user


def _token_generator(expires_in_days: int = 7):
    """A generator that mints a real, decodable refresh JWT."""

    async def generate_refresh_token(user):
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


class TestProvisioning:
    async def test_creates_missing_account_as_service_account(self):
        """A fresh install has no slack-agent; it is created as account_kind='slack'."""
        store = _FakeUserStore()

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.account_created is True
        assert store.created == [{"username": "slack-agent", "account_kind": "slack"}]
        assert credential.user.account_kind == "slack"

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
        store = _FakeUserStore(_user(account_kind="individual"))

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.account_kind_corrected is True
        assert store.updated[0].account_kind == "slack"

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
        """``cases.source`` matches 'slack' exactly and the column has no CHECK,
        so a typo would be persisted, reported as a successful correction, and
        then stamp every case the account opens with the wrong source."""
        store = _FakeUserStore(_user())

        with pytest.raises(
            ServiceAccountProvisioningError, match="Unknown account_kind"
        ):
            await provision_service_account_credential(
                username="slack-agent",
                user_store=store,
                token_generator=_token_generator(),
                account_kind="Slack",  # capitalised typo
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

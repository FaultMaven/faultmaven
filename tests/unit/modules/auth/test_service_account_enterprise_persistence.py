"""A service account's enterprise has to be a ROW, not an in-memory stamp.

``fm-provision-service-account --enterprise-id X`` mints a credential carrying
X. What it did not do was persist X: ``create_user`` had no enterprise parameter,
so the repository fell back to the Standalone sentinel, and the provisioning then
set the attribute on the object it happened to be holding — which nothing wrote
back.

Under ``TENANT_PROVIDER=multi`` that is fatal on the FIRST refresh. The refresh
paths mint the ``enterprise_id`` claim from ``users.enterprise_id``, which holds
the sentinel; ``usable_tenant_id`` refuses the sentinel under multi, so the claim
is empty and ``bind_request_enterprise_context`` answers 403 to every request the
agent makes for the rest of its life. The credential works exactly once, on the
access token minted beside it, and then the account is bricked.

The existing coverage passed because its fake ``create_user`` returned the very
object the provisioning went on to mutate. This module's store persists a row and
answers ``get`` from the row, which is what a database does.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.service_account_provisioning import (
    provision_service_account_credential,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

REAL_ENTERPRISE = "22222222-2222-2222-2222-222222222222"
USERNAME = "slack-agent"


class _PersistingUserStore:
    """A store that keeps ROWS and answers from them, the way a database does.

    The distinction is the whole point of this module: a fake that hands back
    the same object the caller then mutates cannot tell "the enterprise was
    persisted" from "the enterprise was set on an object nobody wrote".
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def _hydrate(self, row: dict) -> DevUser:
        # A fresh object each time, from the stored columns only.
        return DevUser(
            user_id=row["user_id"],
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"],
            created_at=row["created_at"],
            enterprise_id=row["enterprise_id"],
            account_kind=row["account_kind"],
            service_channel=row["service_channel"],
        )

    async def get_user_by_username(self, username: str):
        row = self.rows.get(username)
        return self._hydrate(row) if row else None

    async def create_user(
        self,
        username,
        email=None,
        display_name=None,
        account_kind="individual",
        service_channel=None,
        *,
        enterprise_id,
    ):
        self.rows[username] = {
            "user_id": f"user_{username}",
            "username": username,
            "email": email or f"{username}@faultmaven.example",
            "display_name": display_name or username,
            "created_at": datetime.now(UTC),
            "enterprise_id": enterprise_id,
            "account_kind": account_kind,
            "service_channel": service_channel,
        }
        return self._hydrate(self.rows[username])

    async def update_user(self, user: DevUser) -> DevUser:
        row = self.rows[user.username]
        row["account_kind"] = user.account_kind
        row["service_channel"] = user.service_channel
        row["enterprise_id"] = user.enterprise_id
        return self._hydrate(row)


class _RecordingGenerator:
    """Mints nothing real; records the account it was asked to mint for."""

    def __init__(self) -> None:
        self.minted_for: list[DevUser] = []

    async def generate_refresh_token(self, user, *, state_read_at=None):
        self.minted_for.append(user)
        return "refresh-token"


@pytest.fixture
def multi_tenant(monkeypatch):
    from faultmaven.providers.tenancy import factory

    monkeypatch.setattr(
        factory, "requested_tenant_provider", lambda: factory.BUILTIN_MULTI
    )
    return factory


async def test_the_enterprise_reaches_the_stored_row(multi_tenant):
    """The row is what the refresh path reads, so the row is what must carry it."""
    store = _PersistingUserStore()
    generator = _RecordingGenerator()

    await provision_service_account_credential(
        username=USERNAME,
        user_store=store,
        token_generator=generator,
        service_channel="slack",
        enterprise_id=REAL_ENTERPRISE,
    )

    assert store.rows[USERNAME]["enterprise_id"] == REAL_ENTERPRISE, (
        "the account was persisted under the Standalone sentinel; under multi "
        "its next refresh mints an empty enterprise claim and every request "
        "after it is 403"
    )


async def test_a_reprovision_of_an_existing_account_moves_the_stored_row(multi_tenant):
    """The second run is the one an operator actually makes.

    An account created before the enterprise was known — or under a different
    one — must end up on the enterprise the operator named, in the row, not only
    in the token this run mints.
    """
    store = _PersistingUserStore()
    await store.create_user(
        USERNAME,
        account_kind="service",
        service_channel="slack",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
    )

    await provision_service_account_credential(
        username=USERNAME,
        user_store=store,
        token_generator=_RecordingGenerator(),
        service_channel="slack",
        enterprise_id=REAL_ENTERPRISE,
    )

    assert store.rows[USERNAME]["enterprise_id"] == REAL_ENTERPRISE


async def test_the_minted_credential_still_carries_the_enterprise(multi_tenant):
    """The control: persisting it must not stop the token from carrying it."""
    store = _PersistingUserStore()
    generator = _RecordingGenerator()

    await provision_service_account_credential(
        username=USERNAME,
        user_store=store,
        token_generator=generator,
        service_channel="slack",
        enterprise_id=REAL_ENTERPRISE,
    )

    assert generator.minted_for[-1].enterprise_id == REAL_ENTERPRISE


# ---------------------------------------------------------------------------
# A5b — a missing isolation key is a caller bug, not a sentinel to substitute
# ---------------------------------------------------------------------------


def test_the_user_repository_refuses_to_invent_an_enterprise():
    """``user.enterprise_id or DEFAULT_ENTERPRISE_ID`` was a silent widening.

    Under ``multi`` the sentinel is not a tenant, so a row written with it is a
    row no session can reach — and the substitution happened where the caller
    could not see it. A ``None`` isolation key means the caller did not resolve
    one; surfacing that at the call site is the only place it can be fixed.
    """
    from faultmaven.infrastructure.persistence.user_repository import (
        PostgreSQLUserRepository,
        User,
    )

    repository = PostgreSQLUserRepository(SimpleNamespace())
    now = datetime.now(UTC)
    user = User(
        user_id="user_1",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        enterprise_id=None,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ValueError, match="enterprise"):
        repository._domain_to_dict(user)


def test_the_organization_repository_refuses_to_invent_an_enterprise():
    """The same substitution, on the billing roster's parent."""
    from faultmaven.infrastructure.persistence.organization_repository import (
        PostgreSQLOrganizationRepository,
    )
    from faultmaven.models.interfaces_user import Organization

    repository = PostgreSQLOrganizationRepository(SimpleNamespace())
    now = datetime.now(UTC)
    organization = Organization(
        organization_id="org_1",
        name="Acme",
        slug="acme",
        enterprise_id=None,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ValueError, match="enterprise"):
        import asyncio

        asyncio.run(repository.create_organization(organization))

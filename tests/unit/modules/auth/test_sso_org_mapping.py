"""Multi-tenant SSO login lands a user in their mapped enterprise (#869, ADR-017).

Under ``TENANT_PROVIDER=multi`` the SSO callback has to decide *which tenant*
the login belongs to before it touches anything tenant-scoped. ADR-017 D9 moved
that tenant one tier up — an IdP organization now maps to an **enterprise** —
and these tests pin the decision and its failure modes:

* the IdP's organization is required and must be mapped — an unknown IdP org is
  a fail-closed login (``sso_org_unmapped``), never a just-in-time tenant;
* the mapped enterprise is bound as the request's tenant *before* the user
  lookup, so every read and write below runs inside its RLS scope;
* the account's **anchor** is what a login establishes, and only that:
  ``users.enterprise_id``, one column, no roster table. An account belonging to
  a different enterprise fails the login closed rather than being moved;
* **no organization membership is written at all.** An organization is a
  billing target created by payment (ADR-017 D5), so a sign-in cannot know of
  one and must not invent one — and the service is constructed with no
  organization repository, so a path that tried could not reach one;
* the enterprise claim reaches both minted tokens, which is what
  ``bind_request_enterprise_context`` scopes the session by;
* single-tenant runs none of it — the mapping repository is never consulted.

The tenancy switch is driven through the real ``TenantProvider`` enum and the
real ``coerce_provider_name``/``requested_tenant_provider`` path (only
``get_settings`` is patched), so renaming the enum member breaks these tests
instead of silently passing a dead gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import fakeredis.aioredis as fakeredis
import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import TenantProvider
from faultmaven.config.tenant_context import (
    get_current_enterprise_id,
    set_current_enterprise_id,
)
from faultmaven.models.interfaces_user import Enterprise
from faultmaven.modules.auth.contracts import (
    ISSOIdentityProvider,
    ISSOOrgMappingRepository,
    SSOIdentity,
)
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_FAILED,
    ERROR_ORG_UNMAPPED,
    SSOLoginService,
)
from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
    SSOEphemeralStore,
)
from faultmaven.providers.tenancy import factory as tenancy_factory

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"

DASHBOARD_URL = "https://app.faultmaven.test"

IDP_ORG = "org_01HWORKOS"
FM_ENTERPRISE = "33333333-3333-3333-3333-333333333333"
OTHER_ENTERPRISE = "44444444-4444-4444-4444-444444444444"

IDENTITY = SSOIdentity(
    provider="workos",
    provider_user_id="user_wos_123",
    email="alex@example.com",
    email_verified=True,
    display_name="Alex Example",
    organization_id=IDP_ORG,
)

IDENTITY_NO_ORG = SSOIdentity(
    provider="workos",
    provider_user_id="user_wos_123",
    email="alex@example.com",
    email_verified=True,
    display_name="Alex Example",
)


# =============================================================================
# Fixtures & fakes
# =============================================================================


@pytest.fixture(autouse=True)
def restore_tenant_context():
    """Keep a bound enterprise from leaking into the next test."""
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
def as_tenant_provider(monkeypatch):
    """Drive the real ``TenantProvider`` enum through the real coercion path."""

    def _apply(provider: TenantProvider):
        settings = MagicMock()
        settings.providers.tenant_provider = provider
        monkeypatch.setattr(tenancy_factory, "get_settings", lambda: settings)

    return _apply


@pytest.fixture
def store():
    return SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True))


def make_enterprise(
    *,
    enterprise_id: str = FM_ENTERPRISE,
    deleted_at=None,
) -> Enterprise:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Enterprise(
        enterprise_id=enterprise_id,
        name="Acme Corp",
        slug="acme",
        created_at=now,
        updated_at=now,
        deleted_at=deleted_at,
    )


def make_user(user_id="u-1", **overrides):
    user = SimpleNamespace(
        user_id=user_id,
        username="alex",
        email="alex@example.com",
        display_name="Alex Example",
        enterprise_id=FM_ENTERPRISE,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_active=True,
        is_email_verified=True,
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_login_at=None,
        deleted_at=None,
        sso_provider="workos",
        sso_provider_id="user_wos_123",
        roles=["user"],
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


class FakeProvider(ISSOIdentityProvider):
    def __init__(self, identity=IDENTITY):
        self.identity = identity

    @property
    def provider_name(self) -> str:
        return "workos"

    def build_authorization_url(self, *, state: str) -> str:
        return f"https://authkit.test/authorize?state={state}"

    def exchange_code(self, code: str) -> SSOIdentity:
        return self.identity

    def provision_personal_organization(
        self, *, provider_user_id: str, external_id: str, name: str
    ) -> str:
        """Not exercised here — personal enterprises have their own module.

        Present because the port is abstract: a provider that cannot mint a
        personal organization must fail at construction, not on a user's first
        sign-up. A fake that stubbed it into silence would defeat that, so this
        raises instead — and a mapped login that reached it would fail loudly.
        """
        raise NotImplementedError("not part of this module's scope")


class FakeMappingRepository(ISSOOrgMappingRepository):
    """Records every lookup so tests can assert it was (or was not) consulted."""

    def __init__(self, mappings: dict[tuple[str, str], str] | None = None):
        self.mappings = (
            mappings if mappings is not None else {("workos", IDP_ORG): FM_ENTERPRISE}
        )
        self.calls: list[tuple[str, str]] = []

    async def get_enterprise_id(self, provider: str, provider_org_id: str):
        self.calls.append((provider, provider_org_id))
        return self.mappings.get((provider, provider_org_id))


#: Distinguishes "caller wants the default enterprise" from "caller wants the
#: row to be missing" — ``None`` means the latter.
_DEFAULT_ENTERPRISE = object()


class FakeEnterpriseRepository:
    """The enterprise port the login reads, and the only tenant port it has."""

    def __init__(self, enterprise=_DEFAULT_ENTERPRISE):
        self._enterprise: Enterprise | None = (
            make_enterprise() if enterprise is _DEFAULT_ENTERPRISE else enterprise
        )
        self.lookups_bound_to: list[str] = []
        self.domain_calls: list[str] = []

    async def get_enterprise(self, enterprise_id: str):
        # Records the tenant bound at the moment of the (RLS-scoped) read.
        self.lookups_bound_to.append(get_current_enterprise_id())
        if self._enterprise is None:
            return None
        if self._enterprise.enterprise_id != enterprise_id:
            return None
        return self._enterprise

    async def get_or_create_for_domain(self, *, domain: str, name: str, slug: str):
        """The sign-up arm's port. A mapped login must never reach it."""
        self.domain_calls.append(domain)
        raise AssertionError("the mapped branch must not derive an enterprise")


class FakeUserRepository:
    def __init__(self, users_by_subject=None, users_by_id=None):
        self.users_by_subject = users_by_subject or {}
        self.users_by_id = users_by_id or {}
        self.created = []
        self.updated = []
        self.subject_lookups_bound_to: list[str] = []

    def _all_users(self):
        seen = {}
        for user in (
            list(self.users_by_subject.values())
            + list(self.users_by_id.values())
            + self.created
        ):
            seen[id(user)] = user
        return list(seen.values())

    async def get_by_sso(self, provider, provider_id):
        self.subject_lookups_bound_to.append(get_current_enterprise_id())
        return self.users_by_subject.get((provider, provider_id))

    async def get(self, user_id):
        return self.users_by_id.get(user_id)

    async def get_by_username(self, username):
        for user in self._all_users():
            if user.username.lower() == username.lower():
                return user
        return None

    async def get_by_email(self, email):
        for user in self._all_users():
            if user.email.lower() == email.lower():
                return user
        return None

    async def create(self, user):
        self.created.append(user)
        self.users_by_subject[(user.sso_provider, user.sso_provider_id)] = user
        self.users_by_id[user.user_id] = user
        return user

    async def update(self, user):
        self.updated.append(user)
        return user


class FakeTokenGenerator:
    async def generate_access_token(self, user, *, state_read_at):
        return f"access-{user.user_id}"

    async def generate_refresh_token(self, user, *, state_read_at):
        return f"refresh-{user.user_id}"


class FakeSessionService:
    async def create_session(self, user_id, client_id=None, metadata=None):
        return SimpleNamespace(session_id=f"sess-{user_id}"), False


def build_service(
    store,
    *,
    provider=None,
    users=None,
    mappings=None,
    enterprises=None,
    wire_tenant_repositories=True,
):
    return SSOLoginService(
        identity_provider=provider or FakeProvider(),
        ephemeral_store=store,
        user_repository=users if users is not None else FakeUserRepository(),
        token_generator=FakeTokenGenerator(),
        session_service=FakeSessionService(),
        dashboard_url=DASHBOARD_URL,
        access_token_expires_in=3600,
        org_mapping_repository=(
            (mappings if mappings is not None else FakeMappingRepository())
            if wire_tenant_repositories
            else None
        ),
        enterprise_repository=(
            (enterprises if enterprises is not None else FakeEnterpriseRepository())
            if wire_tenant_repositories
            else None
        ),
    )


def redirect_params(url: str) -> dict:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == DASHBOARD_URL
    assert parts.path == "/auth/sso/callback"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


async def run_callback(service):
    start = await service.begin_login(None)
    return await service.complete_callback(
        code="authkit-code",
        state=start.state,
        error=None,
        browser_state=start.state,
    )


# =============================================================================
# Resolution failures — the login never reaches a tenant
# =============================================================================


@pytest.mark.unit
@pytest.mark.security
async def test_missing_idp_org_fails_closed_before_any_user_lookup(
    store, as_tenant_provider
):
    """With the sign-up switch off — its default — an org-less identity is
    refused before anything tenant-scoped is touched."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    mappings = FakeMappingRepository()
    service = build_service(
        store, provider=FakeProvider(IDENTITY_NO_ORG), users=users, mappings=mappings
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert "code" not in params
    # No tenant decided ⇒ nothing tenant-scoped was touched.
    assert users.subject_lookups_bound_to == []
    assert mappings.calls == []


@pytest.mark.unit
@pytest.mark.security
async def test_unmapped_idp_org_fails_closed_with_the_same_slug(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    mappings = FakeMappingRepository(mappings={})
    service = build_service(store, users=users, mappings=mappings)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    # Consulted with exactly the provider key and the IdP's org id.
    assert mappings.calls == [("workos", IDP_ORG)]
    assert users.subject_lookups_bound_to == []


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    "enterprise",
    [
        pytest.param(None, id="row-missing"),
        pytest.param(
            make_enterprise(deleted_at=datetime(2026, 6, 1, tzinfo=UTC)),
            id="soft-deleted",
        ),
    ],
)
async def test_mapped_but_unavailable_enterprise_is_a_generic_failure(
    store, as_tenant_provider, enterprise
):
    """A mapping pointing at a missing/retired tenant is an operator problem —
    the browser gets the generic slug, not a probe into tenant state."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    service = build_service(
        store, users=users, enterprises=FakeEnterpriseRepository(enterprise=enterprise)
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []


@pytest.mark.unit
def test_the_retired_enterprise_gate_is_live_from_the_database_row_up():
    """Close the loop on the test above: the guard reads ``deleted_at`` off the
    domain object, so the repository's mapper has to carry it. A mapper that
    dropped the column would leave that guard permanently true — a gate that
    can never fire."""
    from faultmaven.infrastructure.persistence.enterprise_repository import (
        _model_to_domain,
    )
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = EnterpriseModel(
        enterprise_id=FM_ENTERPRISE,
        name="Acme Corp",
        slug="acme",
        # Server defaults are not applied to an in-memory row, and the mapper
        # coerces these into a typed domain object — so they are spelled here
        # rather than left to the database.
        plan_tier="free",
        max_members=5,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )

    assert _model_to_domain(row).deleted_at is None
    row.deleted_at = now
    assert _model_to_domain(row).deleted_at == now


@pytest.mark.unit
@pytest.mark.security
async def test_unwired_tenant_repositories_fail_closed_under_multi(
    store, as_tenant_provider
):
    """A composition that forgot to wire the tenant repositories must refuse,
    not fall through to an unscoped login."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    service = build_service(store, users=users, wire_tenant_repositories=False)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []


@pytest.mark.unit
@pytest.mark.security
async def test_a_mapping_pointing_at_the_standalone_sentinel_is_refused(
    store, as_tenant_provider
):
    """fm#850 on the operator-provisioned path.

    Under multi-tenant the Standalone id identifies the deployment, not a
    tenant, and a mapping row an operator typed wrong must not pool a customer
    into it.
    """
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprise=make_enterprise(enterprise_id=STANDALONE_ENTERPRISE_ID)
    )
    service = build_service(
        store,
        users=users,
        mappings=FakeMappingRepository(
            mappings={("workos", IDP_ORG): STANDALONE_ENTERPRISE_ID}
        ),
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # Refused before the sentinel was ever bound as this request's tenant.
    assert enterprises.lookups_bound_to == []
    assert users.subject_lookups_bound_to == []


# =============================================================================
# The happy path — bound tenant, anchored account, nothing else
# =============================================================================


@pytest.mark.unit
async def test_mapped_enterprise_is_bound_before_the_user_lookup(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    enterprises = FakeEnterpriseRepository()
    service = build_service(store, users=users, enterprises=enterprises)

    params = redirect_params(await run_callback(service))

    assert "error" not in params
    # The enterprise row was read inside its own tenant scope...
    assert enterprises.lookups_bound_to == [FM_ENTERPRISE]
    # ...and so was the user lookup that follows it.
    assert users.subject_lookups_bound_to == [FM_ENTERPRISE]


@pytest.mark.unit
async def test_the_completion_code_carries_no_tenant_claim(store, as_tenant_provider):
    """The enterprise claim is minted from ``users.enterprise_id`` at exchange
    time (ADR-017 D9), so nothing tenant-shaped rides the completion code.

    An organization would be the wrong thing to put there twice over: it is
    billing attribution, and a reader that found the tenant in it would be
    reading exactly the conflation this campaign undid.
    """
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    service = build_service(store, users=users)

    params = redirect_params(await run_callback(service))

    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": "u-1"}


@pytest.mark.unit
async def test_a_returning_account_already_anchored_here_is_left_alone(
    store, as_tenant_provider
):
    """The anchor is idempotent: an account already in this enterprise is not
    re-written on every sign-in."""
    as_tenant_provider(TenantProvider.MULTI)
    user = make_user()
    users = FakeUserRepository(users_by_subject={("workos", "user_wos_123"): user})
    service = build_service(store, users=users)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    assert user.enterprise_id == FM_ENTERPRISE
    # ``update`` still runs for the profile sync, but the anchor mover wrote
    # nothing of its own — the value is unchanged.
    assert all(u.enterprise_id == FM_ENTERPRISE for u in users.updated)


@pytest.mark.unit
@pytest.mark.security
async def test_no_organization_membership_is_written_by_a_login(
    store, as_tenant_provider
):
    """ADR-017 D5: an organization is created by payment, never by a sign-in.

    Structural rather than asserted after the fact — the service takes no
    organization repository at all, so a re-introduced membership write would
    have nothing to write through and would fail at construction.
    """
    import inspect

    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    service = build_service(store, users=users)

    assert "code" in redirect_params(await run_callback(service))
    assert not hasattr(service, "_organizations")
    parameters = inspect.signature(SSOLoginService.__init__).parameters
    assert "organization_repository" not in parameters


@pytest.mark.unit
@pytest.mark.security
async def test_enterprise_mismatch_fails_the_login_closed(store, as_tenant_provider):
    """Moving an account between enterprises is an operator action, never an
    implicit consequence of an IdP claim."""
    as_tenant_provider(TenantProvider.MULTI)
    user = make_user(enterprise_id=OTHER_ENTERPRISE)
    users = FakeUserRepository(users_by_subject={("workos", "user_wos_123"): user})
    service = build_service(store, users=users)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert user.enterprise_id == OTHER_ENTERPRISE
    assert users.updated == []


@pytest.mark.unit
@pytest.mark.security
async def test_an_anchor_write_failure_fails_the_login_closed(
    store, as_tenant_provider
):
    """An in-memory anchor the rest of the callback would act on is not an
    anchor: every later step would address a tenant the database does not
    agree the account is in.

    Driven through an account whose anchor has to be **set** — the JIT path
    creates its account with the anchor already on it, so the mover
    short-circuits there and this direction has no other way in.
    """
    as_tenant_provider(TenantProvider.MULTI)
    user = make_user(enterprise_id=None)
    users = FakeUserRepository(users_by_subject={("workos", "user_wos_123"): user})

    async def failing_update(_user):
        raise RuntimeError("database gone")

    users.update = failing_update  # type: ignore[method-assign]
    service = build_service(store, users=users)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert "code" not in params
    # The in-memory value was rolled back, so nothing downstream can act on a
    # tenant the database does not hold.
    assert user.enterprise_id is None


# =============================================================================
# JIT provisioning under multi-tenant
# =============================================================================


@pytest.mark.unit
async def test_jit_user_is_anchored_to_the_mapped_enterprise(store, as_tenant_provider):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    service = build_service(store, users=users)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    created = users.created[0]
    # Not the standalone default the repository would otherwise fall back to,
    # which under multi would pool every JIT account into the sentinel tenant.
    assert created.enterprise_id == FM_ENTERPRISE
    assert created.roles == ["user"]
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": created.user_id}


# =============================================================================
# Single-tenant is untouched
# =============================================================================


@pytest.mark.unit
async def test_single_tenant_never_consults_the_mapping(store, as_tenant_provider):
    as_tenant_provider(TenantProvider.SINGLE)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    mappings = FakeMappingRepository()
    enterprises = FakeEnterpriseRepository()
    service = build_service(
        store, users=users, mappings=mappings, enterprises=enterprises
    )

    params = redirect_params(await run_callback(service))

    assert "error" not in params
    assert mappings.calls == []
    assert enterprises.lookups_bound_to == []
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": "u-1"}


@pytest.mark.unit
async def test_single_tenant_jit_keeps_the_repository_enterprise_fallback(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.SINGLE)
    users = FakeUserRepository()
    service = build_service(store, users=users)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    # Left unset so PostgreSQLUserRepository._domain_to_dict applies the
    # standalone default enterprise, exactly as before this feature.
    assert users.created[0].enterprise_id is None


@pytest.mark.unit
async def test_single_tenant_ignores_an_idp_supplied_org(store, as_tenant_provider):
    """The identity carries an IdP org; single-tenant must not act on it."""
    as_tenant_provider(TenantProvider.SINGLE)
    mappings = FakeMappingRepository(mappings={})
    service = build_service(store, mappings=mappings)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    assert mappings.calls == []


# =============================================================================
# The claim reaches the token (exchange)
# =============================================================================


def _rs256_generator():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        RS256JWTTokenGenerator,
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    generator = RS256JWTTokenGenerator(
        private_key=private_pem,
        public_key=public_pem,
        revocation_store=MagicMock(),
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    return generator, public_pem


@pytest.mark.unit
@pytest.mark.security
async def test_exchange_mints_tokens_carrying_the_enterprise_and_no_organization(
    store, as_tenant_provider
):
    """End of the chain: the enterprise the callback anchored the account to is
    the isolation claim in both tokens, which is what
    ``bind_request_enterprise_context`` scopes the session by.

    The organization claim is **absent**, and that is the assertion that matters
    beside it: nobody pays for this account (ADR-017 D5), and a reader that
    found a tenant in the organization claim would be reading the conflation
    this campaign undid.
    """
    import jwt as pyjwt

    as_tenant_provider(TenantProvider.MULTI)
    generator, public_pem = _rs256_generator()

    from faultmaven.infrastructure.persistence.user_repository import (
        User as RepositoryUser,
    )

    now = datetime.now(UTC)
    repo_user = RepositoryUser(
        user_id="u-1",
        username="alex",
        email="alex@example.com",
        display_name="Alex Example",
        enterprise_id=FM_ENTERPRISE,
        sso_provider="workos",
        sso_provider_id="user_wos_123",
        created_at=now,
        updated_at=now,
        roles=["user"],
    )
    # A user read back from the database never carries an organization.
    assert repo_user.organization_id is None

    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): repo_user},
        users_by_id={"u-1": repo_user},
    )
    service = SSOLoginService(
        identity_provider=FakeProvider(),
        ephemeral_store=store,
        user_repository=users,
        token_generator=generator,
        session_service=FakeSessionService(),
        dashboard_url=DASHBOARD_URL,
        access_token_expires_in=3600,
        org_mapping_repository=FakeMappingRepository(),
        enterprise_repository=FakeEnterpriseRepository(),
    )

    code = redirect_params(await run_callback(service))["code"]
    result = await service.exchange(code)

    assert result is not None
    for token in (result.access_token, result.refresh_token):
        claims = pyjwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
        assert claims["enterprise_id"] == FM_ENTERPRISE
        assert "organization_id" not in claims

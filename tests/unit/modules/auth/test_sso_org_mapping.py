"""Multi-tenant SSO login lands a user in their mapped organization (#869).

Under ``TENANT_PROVIDER=multi`` the SSO callback has to decide *which tenant*
the login belongs to before it touches anything tenant-scoped. These tests pin
that decision and its failure modes:

* the IdP's organization is required and must be mapped — an unknown IdP org is
  a fail-closed login (``sso_org_unmapped``), never a just-in-time tenant;
* the mapped organization is bound as the request's tenant *before* the user
  lookup, so every read and write below runs inside its RLS scope;
* membership is additive and idempotent, and a membership write that fails
  takes the login down with it rather than leaving an org-less session;
* the resolved organization rides the completion code into the minted tokens;
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

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.settings import TenantProvider
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.exceptions import ConflictError
from faultmaven.models.interfaces_user import Organization
from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
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
FM_ORG = "22222222-2222-2222-2222-222222222222"
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
    """Keep a bound organization from leaking into the next test."""
    yield
    set_current_org_id(STANDALONE_ORG_ID)


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


def make_organization(
    *,
    organization_id: str = FM_ORG,
    enterprise_id: str = FM_ENTERPRISE,
    is_active: bool = True,
    deleted_at=None,
) -> Organization:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Organization(
        organization_id=organization_id,
        enterprise_id=enterprise_id,
        name="Acme Corp",
        slug="acme",
        is_active=is_active,
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


class FakeMappingRepository(ISSOOrgMappingRepository):
    """Records every lookup so tests can assert it was (or was not) consulted."""

    def __init__(self, mappings: dict[tuple[str, str], str] | None = None):
        self.mappings = (
            mappings if mappings is not None else {("workos", IDP_ORG): FM_ORG}
        )
        self.calls: list[tuple[str, str]] = []

    async def get_organization_id(self, provider: str, provider_org_id: str):
        self.calls.append((provider, provider_org_id))
        return self.mappings.get((provider, provider_org_id))


#: Distinguishes "caller wants the default organization" from "caller wants the
#: row to be missing" — ``None`` means the latter.
_DEFAULT_ORG = object()


class FakeOrgRepository:
    """Minimal IOrganizationRepository surface used by the login path."""

    def __init__(
        self,
        organization=_DEFAULT_ORG,
        *,
        members: dict[tuple[str, str], str] | None = None,
        add_member_error: Exception | None = None,
    ):
        self._organization: Organization | None = (
            make_organization() if organization is _DEFAULT_ORG else organization
        )
        self.members = dict(members or {})
        self.add_member_error = add_member_error
        self.added: list[tuple[str, str, str]] = []
        self.org_lookups_bound_to: list[str] = []

    async def get_organization(self, organization_id: str):
        # Records the tenant bound at the moment of the (RLS-scoped) read.
        self.org_lookups_bound_to.append(get_current_org_id())
        if self._organization is None:
            return None
        if self._organization.organization_id != organization_id:
            return None
        return self._organization

    async def get_member_role(self, organization_id: str, user_id: str):
        return self.members.get((organization_id, user_id))

    async def add_member(self, organization_id: str, user_id: str, role_id: str):
        self.added.append((organization_id, user_id, role_id))
        if self.add_member_error is not None:
            raise self.add_member_error
        self.members[(organization_id, user_id)] = role_id
        return True


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
        self.subject_lookups_bound_to.append(get_current_org_id())
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
    orgs=None,
    wire_org_repositories=True,
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
            if wire_org_repositories
            else None
        ),
        organization_repository=(
            (orgs if orgs is not None else FakeOrgRepository())
            if wire_org_repositories
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
    "organization",
    [
        pytest.param(None, id="row-missing"),
        pytest.param(make_organization(is_active=False), id="deactivated"),
        pytest.param(
            make_organization(deleted_at=datetime(2026, 6, 1, tzinfo=UTC)),
            id="soft-deleted",
        ),
    ],
)
async def test_mapped_but_unavailable_org_is_a_generic_failure(
    store, as_tenant_provider, organization
):
    """A mapping pointing at a missing/disabled tenant is an operator problem —
    the browser gets the generic slug, not a probe into tenant state."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    service = build_service(
        store, users=users, orgs=FakeOrgRepository(organization=organization)
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []


@pytest.mark.unit
def test_the_deactivated_org_gate_is_live_from_the_database_row_up():
    """Close the loop on the test above: the guard reads ``is_active`` off the
    domain object, so the repository's mapper has to carry it. A mapper that
    dropped the column would leave that guard permanently true — a gate that
    can never fire."""
    from faultmaven.infrastructure.persistence.models import OrganizationModel
    from faultmaven.infrastructure.persistence.organization_repository import (
        _model_to_domain,
    )

    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = OrganizationModel(
        organization_id=FM_ORG,
        enterprise_id=FM_ENTERPRISE,
        name="Acme Corp",
        slug="acme",
        is_active=False,
        created_at=now,
        updated_at=now,
    )

    assert _model_to_domain(row).is_active is False
    row.is_active = True
    assert _model_to_domain(row).is_active is True


@pytest.mark.unit
@pytest.mark.security
async def test_unwired_org_repositories_fail_closed_under_multi(
    store, as_tenant_provider
):
    """A composition that forgot to wire the mapping repositories must refuse,
    not fall through to an org-less login."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    service = build_service(store, users=users, wire_org_repositories=False)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []


# =============================================================================
# The happy path — bound tenant, membership, claim on the completion code
# =============================================================================


@pytest.mark.unit
async def test_mapped_org_is_bound_before_the_user_lookup_and_rides_the_code(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    orgs = FakeOrgRepository(members={(FM_ORG, "u-1"): SYSTEM_ROLE_IDS[Role.MEMBER]})
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert "error" not in params
    # The organization row was read inside its own tenant scope...
    assert orgs.org_lookups_bound_to == [FM_ORG]
    # ...and so was the user lookup that follows it.
    assert users.subject_lookups_bound_to == [FM_ORG]
    # The completion code carries the tenant into the exchange.
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": "u-1", "organization_id": FM_ORG}


@pytest.mark.unit
async def test_existing_member_is_not_re_added(store, as_tenant_provider):
    """Membership is idempotent: an existing member's role is left alone."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    orgs = FakeOrgRepository(members={(FM_ORG, "u-1"): SYSTEM_ROLE_IDS[Role.ADMIN]})
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    assert orgs.added == []
    # The admin was not silently demoted to member.
    assert orgs.members[(FM_ORG, "u-1")] == SYSTEM_ROLE_IDS[Role.ADMIN]


@pytest.mark.unit
async def test_returning_non_member_is_added_with_the_member_role(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    orgs = FakeOrgRepository()
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    assert orgs.added == [(FM_ORG, "u-1", SYSTEM_ROLE_IDS[Role.MEMBER])]


@pytest.mark.unit
async def test_membership_insert_race_is_treated_as_already_a_member(
    store, as_tenant_provider
):
    """A concurrent login that won the insert produced the outcome we wanted."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )

    class RacingOrgRepository(FakeOrgRepository):
        async def add_member(self, organization_id, user_id, role_id):
            self.added.append((organization_id, user_id, role_id))
            # The winner's row landed between our read and our write.
            self.members[(organization_id, user_id)] = role_id
            raise ConflictError("membership already exists")

    orgs = RacingOrgRepository()
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    assert orgs.added == [(FM_ORG, "u-1", SYSTEM_ROLE_IDS[Role.MEMBER])]


@pytest.mark.unit
@pytest.mark.security
async def test_membership_write_failure_fails_the_login_closed(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={("workos", "user_wos_123"): make_user()}
    )
    orgs = FakeOrgRepository(add_member_error=RuntimeError("database gone"))
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert "code" not in params


@pytest.mark.unit
@pytest.mark.security
async def test_enterprise_mismatch_fails_the_login_closed(store, as_tenant_provider):
    """Moving an account between enterprises is an operator action, never an
    implicit consequence of an IdP claim."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository(
        users_by_subject={
            ("workos", "user_wos_123"): make_user(enterprise_id=OTHER_ENTERPRISE)
        }
    )
    orgs = FakeOrgRepository()
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert orgs.added == []


# =============================================================================
# JIT provisioning under multi-tenant
# =============================================================================


@pytest.mark.unit
async def test_jit_user_is_anchored_to_the_mapped_orgs_enterprise(
    store, as_tenant_provider
):
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    orgs = FakeOrgRepository()
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert "code" in params
    created = users.created[0]
    # Not the standalone default the repository would otherwise fall back to.
    assert created.enterprise_id == FM_ENTERPRISE
    assert orgs.added == [(FM_ORG, created.user_id, SYSTEM_ROLE_IDS[Role.MEMBER])]
    payload = await store.consume_login(params["code"])
    assert payload.pop("state_read_at") > 0  # epoch seconds (#831)
    assert payload == {"user_id": created.user_id, "organization_id": FM_ORG}


@pytest.mark.unit
@pytest.mark.security
async def test_jit_membership_failure_does_not_leave_an_orgless_login(
    store, as_tenant_provider
):
    """The account exists after this, but the login fails — the next attempt
    heals it, because the membership ensure is idempotent."""
    as_tenant_provider(TenantProvider.MULTI)
    users = FakeUserRepository()
    orgs = FakeOrgRepository(add_member_error=RuntimeError("database gone"))
    service = build_service(store, users=users, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert len(users.created) == 1


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
    orgs = FakeOrgRepository()
    service = build_service(store, users=users, mappings=mappings, orgs=orgs)

    params = redirect_params(await run_callback(service))

    assert "error" not in params
    assert mappings.calls == []
    assert orgs.org_lookups_bound_to == []
    assert orgs.added == []
    # No organization on the completion payload: single-tenant tokens get the
    # Standalone sentinel from resolve_organization_claim instead.
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
async def test_exchange_mints_access_and_refresh_tokens_carrying_the_org(
    store, as_tenant_provider
):
    """End of the chain: the org resolved at callback time is in both tokens,
    so ``bind_request_org_context`` scopes the session to the right tenant."""
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
        organization_repository=FakeOrgRepository(
            members={(FM_ORG, "u-1"): SYSTEM_ROLE_IDS[Role.MEMBER]}
        ),
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
        assert claims["organization_id"] == FM_ORG

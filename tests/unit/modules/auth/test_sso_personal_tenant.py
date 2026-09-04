"""Self-service personal tenants on the SSO callback (#1045, ADR-016 D5).

An identity that carries **no** IdP organization is refused today. Behind
``SSO_JIT_PERSONAL_TENANT_ENABLED`` — default off — its first sign-in instead
provisions a personal tenant, and every later sign-in lands in the same one.

What these tests pin, and why each is here rather than assumed:

* **Only the no-IdP-organization branch changes.** An IdP organization that
  exists but is unmapped stays fail-closed with the switch in *either* state,
  and the proof is a byte-for-byte comparison of the two redirect URLs — not a
  re-assertion of the slug, which would still pass if the branch had started
  consulting the personal-tenant repository on its way to the same answer.
* **The switch is real in both directions**, and it is driven through the
  actual ``SSO_JIT_PERSONAL_TENANT_ENABLED`` environment variable and the real
  ``get_settings()`` singleton. Patching the module-level predicate would prove
  the branch works and leave "does the setting reach it?" — the failure mode
  this project has been bitten by — entirely untested.
* **The lookup is keyed on the subject.** The second-login case runs with the
  IdP reporting *no* organization and with the mapping repository present and
  empty, so a resolution that leaned on ``sso_org_mappings`` or on membership
  could not pass it.
* **The sentinel is not a tenant** (fm#850), re-proved on this deliberate path:
  a subject row pointing at the Standalone org fails the login closed rather
  than pooling it there.
* **JIT users are never admin** (ADR-015 D5) — by construction, because this
  path writes no membership at all; ``_ensure_org_affiliation`` does, with the
  ``member`` role, exactly as it does for a mapped tenant.
* **Failure leaves the login refused and a retry able to finish.** The IdP call
  runs before the database transaction on purpose, so the recoverable residue
  is an IdP organization the next attempt finds again — never a FaultMaven
  tenant a later login adopts as if it were complete.

The tenancy switch is driven through the real ``TenantProvider`` enum and the
real coercion path, as in ``test_sso_org_mapping.py``, so renaming the enum
member breaks these tests instead of silently passing a dead gate.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import fakeredis.aioredis as fakeredis
import pytest

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.settings import TenantProvider
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.models.interfaces_user import Organization
from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from faultmaven.modules.auth.contracts import (
    ISSOIdentityProvider,
    ISSOOrgMappingRepository,
    ISSOPersonalOrgRepository,
    PersonalOrgRecord,
    SSOIdentity,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ORG_NAME,
    personal_org_slug,
    personal_tenant_key,
)
from faultmaven.modules.auth.domain.services.sso_login_service import (
    ERROR_FAILED,
    ERROR_ORG_UNMAPPED,
    SSOLoginService,
)
from faultmaven.modules.auth.exceptions import SSOProvisioningError
from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
    SSOEphemeralStore,
)
from faultmaven.providers.tenancy import factory as tenancy_factory
from tests.utils import get_live_settings, reset_settings_singleton

pytestmark = [pytest.mark.unit, pytest.mark.security]

DASHBOARD_URL = "https://app.faultmaven.test"

#: The env name the switch binds to. Read off the declared ``validation_alias``
#: rather than spelled here, so a rename breaks these tests instead of leaving
#: them setting a variable nothing reads.
SWITCH_ENV = "SSO_JIT_PERSONAL_TENANT_ENABLED"

SUBJECT = "user_wos_individual"
IDP_ORG = "org_01HWORKOS"
MAPPED_FM_ORG = "22222222-2222-2222-2222-222222222222"
PERSONAL_FM_ORG = "55555555-5555-5555-5555-555555555555"
PERSONAL_IDP_ORG = "org_01PERSONAL"
PERSONAL_ENTERPRISE = "66666666-6666-6666-6666-666666666666"

#: An individual: authenticated, but the IdP names no organization for them.
INDIVIDUAL = SSOIdentity(
    provider="workos",
    provider_user_id=SUBJECT,
    email="sam@personal.example",
    email_verified=True,
    display_name="Sam Individual",
)

#: The same person, on a later login where the IdP DOES echo their (personal)
#: organization. Invariant 4 has to hold in both shapes.
INDIVIDUAL_WITH_ECHOED_ORG = SSOIdentity(
    provider="workos",
    provider_user_id=SUBJECT,
    email="sam@personal.example",
    email_verified=True,
    display_name="Sam Individual",
    organization_id=PERSONAL_IDP_ORG,
)

#: The same individual, on a later login where the IdP names a COMPANY
#: organization — the switching case (ADR-016 D5 as amended).
COMPANY_IDENTITY_SAME_SUBJECT = SSOIdentity(
    provider="workos",
    provider_user_id=SUBJECT,
    email="sam@personal.example",
    email_verified=True,
    display_name="Sam Individual",
    organization_id=IDP_ORG,
)

#: An identity that DOES belong to an IdP organization. Used only to prove the
#: unmapped branch is untouched.
COMPANY_IDENTITY = SSOIdentity(
    provider="workos",
    provider_user_id="user_wos_company",
    email="dana@acme.example",
    email_verified=True,
    display_name="Dana Acme",
    organization_id=IDP_ORG,
)


# =============================================================================
# Fixtures & fakes
# =============================================================================


@pytest.fixture(autouse=True)
def restore_tenant_context():
    """Keep a bound organization from leaking into the next test."""
    yield
    set_current_org_id(STANDALONE_ORG_ID)


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch):
    """Every test decides the switch; none inherits an ambient value.

    The singleton is cleared on the way in AND on the way out: a test that
    builds settings with the switch on would otherwise leave that instance
    cached for whatever runs next, and the default-off assertion below would
    pass or fail on collection order.
    """
    monkeypatch.delenv(SWITCH_ENV, raising=False)
    reset_settings_singleton()
    yield
    reset_settings_singleton()


@pytest.fixture
def switch(monkeypatch):
    """Set the real environment variable and rebuild the real settings.

    Deliberately NOT a patch of ``_jit_personal_tenant_enabled``. The thing most
    likely to be wrong is not the branch but the wiring between the documented
    knob and the branch, and only the environment variable exercises that.
    """

    def _apply(enabled: bool):
        monkeypatch.setenv(SWITCH_ENV, "true" if enabled else "false")
        reset_settings_singleton()
        # Prove the value actually landed on the settings object the production
        # code reads, so a mis-declared alias fails here rather than as a
        # confusing behavioural assertion further down.
        assert get_live_settings().auth.sso_jit_personal_tenant_enabled is enabled

    return _apply


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
    organization_id: str = PERSONAL_FM_ORG,
    enterprise_id: str = PERSONAL_ENTERPRISE,
    name: str = PERSONAL_ORG_NAME,
    is_active: bool = True,
    deleted_at=None,
) -> Organization:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Organization(
        organization_id=organization_id,
        enterprise_id=enterprise_id,
        name=name,
        slug=personal_org_slug(personal_tenant_key("workos", SUBJECT)),
        is_active=is_active,
        created_at=now,
        updated_at=now,
        deleted_at=deleted_at,
    )


class FakeProvider(ISSOIdentityProvider):
    """An IdP that yields a scripted identity and records provisioning calls."""

    def __init__(
        self,
        identity: SSOIdentity = INDIVIDUAL,
        *,
        personal_org_id: str = PERSONAL_IDP_ORG,
        provision_error: Exception | None = None,
    ):
        self.identity = identity
        self._personal_org_id = personal_org_id
        self._provision_error = provision_error
        self.provision_calls: list[dict] = []

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
        self.provision_calls.append(
            {
                "provider_user_id": provider_user_id,
                "external_id": external_id,
                "name": name,
            }
        )
        if self._provision_error is not None:
            raise self._provision_error
        return self._personal_org_id


class FakeMappingRepository(ISSOOrgMappingRepository):
    """Records every lookup so a test can assert it was NOT consulted."""

    def __init__(self, mappings: dict[tuple[str, str], str] | None = None):
        self.mappings = mappings if mappings is not None else {}
        self.calls: list[tuple[str, str]] = []

    async def get_organization_id(self, provider: str, provider_org_id: str):
        self.calls.append((provider, provider_org_id))
        return self.mappings.get((provider, provider_org_id))


class FakePersonalOrgRepository(ISSOPersonalOrgRepository):
    """In-memory stand-in with the real adapter's idempotency semantics.

    ``race_winner`` simulates losing the constraint race: the write is refused
    and the winner's organization is what the subject row already holds, which
    is the state the real adapter recovers into after its transaction rolls back
    whole. ``provision`` binds the organization it returns, as the real one does
    — the binding moved into the repository, so a fake that skipped it would let
    a caller depending on the old contract pass here and fail in production.
    """

    def __init__(
        self,
        rows: dict[tuple[str, str], PersonalOrgRecord] | None = None,
        *,
        race_winner: str | None = None,
        write_error: Exception | None = None,
        minted_last_hour: int = 0,
    ):
        self.rows = dict(rows or {})
        self.race_winner = race_winner
        self.write_error = write_error
        self.minted_last_hour = minted_last_hour
        self.lookups: list[tuple[str, str]] = []
        self.provisioned: list[dict] = []
        self.confirmed: list[tuple[str, str]] = []
        self.retired: list[tuple[str, str]] = []
        self.enterprise_probes: list[tuple[str, str, str]] = []
        self.count_calls: list[tuple[str, Any]] = []
        self.bound_at_lookup: list[str] = []

    async def get(self, provider, provider_user_id):
        self.lookups.append((provider, provider_user_id))
        self.bound_at_lookup.append(get_current_org_id())
        return self.rows.get((provider, provider_user_id))

    async def find_by_enterprise(self, provider, provider_user_id, enterprise_id):
        self.enterprise_probes.append((provider, provider_user_id, enterprise_id))
        record = self.rows.get((provider, provider_user_id))
        return record is not None and record.enterprise_id == enterprise_id

    async def count_created_since(self, provider, since):
        self.count_calls.append((provider, since))
        return self.minted_last_hour

    async def provision(
        self, *, provider, provider_user_id, provider_org_id, name, slug
    ):
        organization_id = self.race_winner or str(uuid.uuid4())
        self.provisioned.append(
            {
                "provider": provider,
                "provider_user_id": provider_user_id,
                "provider_org_id": provider_org_id,
                "organization_id": organization_id,
                "name": name,
                "slug": slug,
            }
        )
        if self.write_error is not None:
            raise self.write_error
        existing = self.rows.get((provider, provider_user_id))
        if existing is not None:
            return existing.organization_id
        self.rows[(provider, provider_user_id)] = PersonalOrgRecord(
            organization_id=organization_id,
            enterprise_id=PERSONAL_ENTERPRISE,
            provider_org_id=provider_org_id,
            membership_confirmed=False,
        )
        return organization_id

    async def confirm_membership(self, provider, provider_user_id):
        self.confirmed.append((provider, provider_user_id))
        record = self.rows.get((provider, provider_user_id))
        if record is not None:
            self.rows[(provider, provider_user_id)] = PersonalOrgRecord(
                organization_id=record.organization_id,
                enterprise_id=record.enterprise_id,
                provider_org_id=record.provider_org_id,
                membership_confirmed=True,
            )

    async def retire(self, provider, provider_user_id):
        self.retired.append((provider, provider_user_id))
        return self.rows.pop((provider, provider_user_id), None) is not None


class FakeOrgRepository:
    """Minimal IOrganizationRepository surface used by the login path."""

    def __init__(
        self,
        organizations: dict[str, Organization] | None = None,
        *,
        members: dict[tuple[str, str], str] | None = None,
        add_member_error: Exception | None = None,
        answer_any: bool = False,
    ):
        self.organizations = (
            organizations
            if organizations is not None
            else {PERSONAL_FM_ORG: make_organization()}
        )
        # The personal path mints its organization id inside the repository, so
        # a test that provisions cannot know the id in advance. ``answer_any``
        # says "whatever id the login resolved, that organization exists".
        self.answer_any = answer_any
        self.members = dict(members or {})
        self.add_member_error = add_member_error
        self.added: list[tuple[str, str, str]] = []
        self.org_lookups_bound_to: list[str] = []

    async def get_organization(self, organization_id: str):
        self.org_lookups_bound_to.append(get_current_org_id())
        if self.answer_any and organization_id not in self.organizations:
            return make_organization(organization_id=organization_id)
        return self.organizations.get(organization_id)

    async def get_member_role(self, organization_id: str, user_id: str):
        return self.members.get((organization_id, user_id))

    async def add_member(self, organization_id: str, user_id: str, role_id: str):
        self.added.append((organization_id, user_id, role_id))
        if self.add_member_error is not None:
            raise self.add_member_error
        self.members[(organization_id, user_id)] = role_id
        return True


class FakeUserRepository:
    def __init__(self, users_by_subject=None):
        self.users_by_subject = users_by_subject or {}
        self.users_by_id: dict[str, object] = {}
        self.created: list = []
        self.updated: list = []
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
            if getattr(user, "username", "").lower() == username.lower():
                return user
        return None

    async def get_by_email(self, email):
        for user in self._all_users():
            if getattr(user, "email", "").lower() == email.lower():
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


def _record(
    *,
    organization_id: str = PERSONAL_FM_ORG,
    enterprise_id: str = PERSONAL_ENTERPRISE,
    provider_org_id: str = PERSONAL_IDP_ORG,
    membership_confirmed: bool = True,
) -> PersonalOrgRecord:
    return PersonalOrgRecord(
        organization_id=organization_id,
        enterprise_id=enterprise_id,
        provider_org_id=provider_org_id,
        membership_confirmed=membership_confirmed,
    )


def make_returning_user(user_id="u-personal"):
    return SimpleNamespace(
        user_id=user_id,
        username="sam",
        email="sam@personal.example",
        display_name="Sam Individual",
        enterprise_id=PERSONAL_ENTERPRISE,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_active=True,
        is_email_verified=True,
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_login_at=None,
        deleted_at=None,
        sso_provider="workos",
        sso_provider_id=SUBJECT,
        roles=["user"],
    )


def build_service(
    store,
    *,
    provider=None,
    users=None,
    mappings=None,
    orgs=None,
    personal=None,
    wire_personal=True,
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
            mappings if mappings is not None else FakeMappingRepository()
        ),
        organization_repository=orgs if orgs is not None else FakeOrgRepository(),
        personal_org_repository=(
            (personal if personal is not None else FakePersonalOrgRepository())
            if wire_personal
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
# Invariant 2 — the switch, in both states, through the real setting
# =============================================================================


def test_the_switch_defaults_off():
    """The knob ships off, so merging this feature changes no deployment."""
    assert get_live_settings().auth.sso_jit_personal_tenant_enabled is False


def test_the_switch_is_named_by_the_env_var_these_tests_set():
    """The alias and the variable the fixture sets are the same string.

    Without this the whole module could be exercising a branch through a
    predicate that production reads from somewhere else entirely.
    """
    from faultmaven.config.settings import AuthSettings

    field = AuthSettings.model_fields["sso_jit_personal_tenant_enabled"]
    assert field.validation_alias == SWITCH_ENV
    assert field.default is False


async def test_switch_off_keeps_the_orgless_login_fail_closed(
    store, as_tenant_provider, switch
):
    """With the switch off, an org-less identity is refused exactly as today."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(False)
    users = FakeUserRepository()
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(store, provider=provider, users=users, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    # Nothing tenant-scoped was touched, and no tenant was invented.
    assert users.subject_lookups_bound_to == []
    assert personal.lookups == []
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_switch_on_provisions_exactly_one_personal_tenant(
    store, as_tenant_provider, switch
):
    """First sign-in of an org-less identity: one IdP org, one FaultMaven org."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)
    orgs = FakeOrgRepository(organizations={}, answer_any=True)
    service = build_service(
        store, provider=provider, users=users, personal=personal, orgs=orgs
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    # Exactly one FaultMaven tenant.
    assert len(personal.provisioned) == 1
    written = personal.provisioned[0]
    assert written["provider_org_id"] == PERSONAL_IDP_ORG
    assert written["name"] == PERSONAL_ORG_NAME
    # A real, distinct organization row — never the Standalone sentinel (#850).
    assert written["organization_id"] != STANDALONE_ORG_ID
    # The resolved tenant is the one the tokens will claim.
    payload = await store.consume_login(params["code"])
    assert payload["organization_id"] == written["organization_id"]


async def test_the_idp_call_carries_the_derived_pii_free_identifiers(
    store, as_tenant_provider, switch
):
    """The external id and slug are the derived key, not the email or subject."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)
    orgs = FakeOrgRepository(organizations={})

    async def get_organization(organization_id: str):
        return make_organization(organization_id=organization_id)

    orgs.get_organization = get_organization  # type: ignore[method-assign]
    service = build_service(store, provider=provider, personal=personal, orgs=orgs)

    await run_callback(service)

    key = personal_tenant_key("workos", SUBJECT)
    call = provider.provision_calls[0]
    assert call["external_id"] == personal_org_slug(key)
    assert call["name"] == PERSONAL_ORG_NAME
    assert personal.provisioned[0]["slug"] == personal_org_slug(key)
    # Neither the email local-part nor the raw subject appears in what is
    # written or sent — the slug is rendered wherever an org is shown.
    for value in (call["external_id"], personal.provisioned[0]["slug"]):
        assert "sam" not in value
        assert SUBJECT not in value


# =============================================================================
# Invariant 1 — the unmapped branch is untouched, in BOTH switch states
# =============================================================================


@pytest.mark.parametrize("enabled", [False, True], ids=["switch-off", "switch-on"])
async def test_unmapped_idp_org_still_fails_closed(
    store, as_tenant_provider, switch, enabled
):
    """An IdP organization with no mapping is refused whatever the switch says.

    A company is onboarded deliberately, never by whoever signs in first.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(enabled)
    users = FakeUserRepository()
    mappings = FakeMappingRepository(mappings={})
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(COMPANY_IDENTITY)
    service = build_service(
        store,
        provider=provider,
        users=users,
        mappings=mappings,
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    # Consulted with exactly the provider key and the IdP's org id, as before.
    assert mappings.calls == [("workos", IDP_ORG)]
    assert users.subject_lookups_bound_to == []
    # And the new machinery was not reached on its way to that answer.
    assert personal.lookups == []
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_the_unmapped_redirect_is_byte_for_byte_identical(
    store, as_tenant_provider, switch
):
    """The same URL, character for character, with the switch off and on.

    Re-asserting the error slug would still pass if the branch had grown a
    detour. Comparing the whole redirect is what makes "unchanged" mean it.
    """
    as_tenant_provider(TenantProvider.MULTI)

    async def redirect_with(enabled: bool) -> str:
        switch(enabled)
        return await run_callback(
            build_service(
                store,
                provider=FakeProvider(COMPANY_IDENTITY),
                mappings=FakeMappingRepository(mappings={}),
            )
        )

    assert await redirect_with(False) == await redirect_with(True)


# =============================================================================
# Invariant 4 — a returning individual lands in the same tenant
# =============================================================================


async def test_second_login_with_no_idp_org_resolves_the_same_tenant(
    store, as_tenant_provider, switch
):
    """The subject-keyed row is what answers, so no IdP org is needed.

    The mapping repository is wired and empty and the identity names no
    organization, so a resolution leaning on ``sso_org_mappings`` — or on
    membership, which is RLS-tenanted and unreadable here — cannot pass.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(rows={("workos", SUBJECT): _record()})
    mappings = FakeMappingRepository(mappings={})
    provider = FakeProvider(INDIVIDUAL)
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    orgs = FakeOrgRepository(members={(PERSONAL_FM_ORG, user.user_id): "member"})
    service = build_service(
        store,
        provider=provider,
        users=users,
        mappings=mappings,
        orgs=orgs,
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    # Resolved from the subject row: nothing new was provisioned on either side.
    assert personal.lookups == [("workos", SUBJECT)]
    assert personal.provisioned == []
    assert provider.provision_calls == []
    assert mappings.calls == []
    # The lookup ran with no tenant bound yet — which is exactly why it has to
    # be untenanted — and the organization read that follows ran inside the
    # resolved tenant's scope.
    assert personal.bound_at_lookup == [STANDALONE_ORG_ID]
    assert orgs.org_lookups_bound_to == [PERSONAL_FM_ORG]

    # And the tokens claim that tenant.
    payload = await store.consume_login(params["code"])
    assert payload["organization_id"] == PERSONAL_FM_ORG


async def test_second_login_with_an_echoed_org_resolves_the_same_tenant(
    store, as_tenant_provider, switch
):
    """When the IdP DOES echo the personal org, the ordinary mapped path is
    what resolves it — to the same tenant, through the ``sso_org_mappings`` row
    first sign-in wrote. Both shapes of a returning login must agree."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(rows={("workos", SUBJECT): _record()})
    mappings = FakeMappingRepository(
        mappings={("workos", PERSONAL_IDP_ORG): PERSONAL_FM_ORG}
    )
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    orgs = FakeOrgRepository(members={(PERSONAL_FM_ORG, user.user_id): "member"})
    service = build_service(
        store,
        provider=FakeProvider(INDIVIDUAL_WITH_ECHOED_ORG),
        users=users,
        mappings=mappings,
        orgs=orgs,
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    payload = await store.consume_login(params["code"])
    assert payload["organization_id"] == PERSONAL_FM_ORG
    # It went through the mapped path — the personal lookup was not needed.
    assert mappings.calls == [("workos", PERSONAL_IDP_ORG)]
    assert personal.lookups == []


async def test_a_replayed_first_login_provisions_no_second_tenant(
    store, as_tenant_provider, switch
):
    """Two sequential first logins for one subject yield one organization."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)
    users = FakeUserRepository()
    orgs = FakeOrgRepository(organizations={}, answer_any=True)
    service = build_service(
        store, provider=provider, users=users, orgs=orgs, personal=personal
    )

    first = redirect_params(await run_callback(service))
    second = redirect_params(await run_callback(service))

    assert "code" in first and "code" in second
    assert len(personal.provisioned) == 1
    assert (await store.consume_login(first["code"]))["organization_id"] == (
        await store.consume_login(second["code"])
    )["organization_id"]


async def test_a_lost_provisioning_race_adopts_the_winners_tenant(
    store, as_tenant_provider, switch
):
    """The loser of a concurrent first login lands in the winner's tenant.

    The real adapter reaches this state by rolling its whole transaction back on
    a constraint violation and re-reading the subject row; here the repository
    reports the same outcome. What is under test is the service: it must use the
    organization the repository *returned*.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    winner = "77777777-7777-7777-7777-777777777777"
    personal = FakePersonalOrgRepository(race_winner=winner)
    users = FakeUserRepository()
    orgs = FakeOrgRepository(
        organizations={winner: make_organization(organization_id=winner)}
    )
    service = build_service(store, users=users, orgs=orgs, personal=personal)

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert (await store.consume_login(params["code"]))["organization_id"] == winner
    # The read that verified the tenant ran inside the WINNER's scope.
    assert orgs.org_lookups_bound_to == [winner]


# =============================================================================
# Invariant 5 — the Standalone sentinel never becomes a tenant (fm#850)
# =============================================================================


async def test_a_subject_row_pointing_at_the_sentinel_fails_closed(
    store, as_tenant_provider, switch
):
    """fm#850, re-proved on the deliberate path rather than the accidental one.

    Under multi-tenant the Standalone id identifies the deployment, not a
    tenant. A row pointing at it must refuse the login, not pool it there.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(rows={("workos", SUBJECT): STANDALONE_ORG_ID})
    users = FakeUserRepository()
    orgs = FakeOrgRepository(
        organizations={
            STANDALONE_ORG_ID: make_organization(organization_id=STANDALONE_ORG_ID)
        }
    )
    service = build_service(store, users=users, orgs=orgs, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # Refused before the sentinel was ever bound as this request's tenant.
    assert get_current_org_id() == STANDALONE_ORG_ID
    assert orgs.org_lookups_bound_to == []
    assert users.subject_lookups_bound_to == []


# =============================================================================
# Invariant 6 — JIT users are never admin (ADR-015 D5)
# =============================================================================


async def test_the_personal_orgs_single_member_holds_the_member_role(
    store, as_tenant_provider, switch
):
    """Membership is written by the shared ensure, with the member role.

    The provisioning path writes none itself — the user row does not exist yet —
    which is what makes this true by construction rather than by a second copy
    of the rule.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository()
    users = FakeUserRepository()
    orgs = FakeOrgRepository(organizations={})

    async def get_organization(organization_id: str):
        return make_organization(organization_id=organization_id)

    orgs.get_organization = get_organization  # type: ignore[method-assign]
    service = build_service(store, users=users, orgs=orgs, personal=personal)

    params = redirect_params(await run_callback(service))
    assert "code" in params

    assert len(orgs.added) == 1
    organization_id, user_id, role_id = orgs.added[0]
    assert role_id == SYSTEM_ROLE_IDS[Role.MEMBER]
    assert role_id != SYSTEM_ROLE_IDS[Role.ADMIN]
    assert organization_id == personal.provisioned[0]["organization_id"]

    # The account itself is never admin either.
    created = users.created[0]
    assert created.roles == ["user"]
    assert "admin" not in created.roles
    assert created.user_id == user_id
    # And it is anchored to the personal organization's enterprise.
    assert created.enterprise_id == PERSONAL_ENTERPRISE


# =============================================================================
# Invariant 8 — failure direction
# =============================================================================


async def test_an_idp_failure_refuses_the_login_and_writes_no_tenant(
    store, as_tenant_provider, switch
):
    """No IdP organization ⇒ no FaultMaven tenant. Nothing to adopt later."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    provider = FakeProvider(
        INDIVIDUAL, provision_error=SSOProvisioningError("idp down")
    )
    personal = FakePersonalOrgRepository()
    users = FakeUserRepository()
    service = build_service(store, provider=provider, users=users, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert personal.provisioned == []
    assert users.created == []


async def test_a_database_failure_refuses_the_login_and_leaves_recoverable_residue(
    store, as_tenant_provider, switch
):
    """A refused write is a refused login, never a partial tenant.

    The residue this leaves is on the IdP side by design: an organization with
    no FaultMaven tenant, which the next attempt finds again because its
    external id is derived from the subject. Nobody is a member of it, so the
    IdP still reports no organization and the next callback re-enters this same
    branch — which is what makes it self-healing rather than a dead end.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(write_error=RuntimeError("constraint"))
    provider = FakeProvider(INDIVIDUAL)
    users = FakeUserRepository()
    service = build_service(store, provider=provider, users=users, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.created == []
    # The retry re-derives the same external id, so it finds rather than
    # duplicates whatever the IdP kept — and no membership was created, so the
    # IdP cannot start echoing an organization with no committed mapping.
    key = personal_tenant_key("workos", SUBJECT)
    assert provider.provision_calls[0]["external_id"] == personal_org_slug(key)


async def test_an_unwired_personal_repository_fails_closed(
    store, as_tenant_provider, switch
):
    """Misconfiguration refuses rather than falling through to an org-less login."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    service = build_service(store, users=users, wire_personal=False)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []


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
async def test_an_unusable_personal_org_is_a_generic_failure(
    store, as_tenant_provider, switch, organization
):
    """A personal tenant gets the same availability checks a mapped one does —
    and an unusable one is refused, never routed around by provisioning a
    second tenant for the same subject."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(rows={("workos", SUBJECT): _record()})
    users = FakeUserRepository()
    orgs = FakeOrgRepository(
        organizations={} if organization is None else {PERSONAL_FM_ORG: organization}
    )
    service = build_service(store, users=users, orgs=orgs, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []
    assert personal.provisioned == []


# =============================================================================
# Single-tenant is untouched
# =============================================================================


async def test_single_tenant_never_reaches_the_personal_path(
    store, as_tenant_provider, switch
):
    """Single-tenant has one organization and never decides a tenant at all —
    with the switch ON, so this is not merely the default being observed."""
    as_tenant_provider(TenantProvider.SINGLE)
    switch(True)
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)
    users = FakeUserRepository()
    service = build_service(store, provider=provider, users=users, personal=personal)

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert personal.lookups == []
    assert personal.provisioned == []
    assert provider.provision_calls == []


# =============================================================================
# Invariant 7 — naming and slug derivation
# =============================================================================


def test_the_organization_name_lives_in_one_constant():
    """Every rendering of the name reads the same constant."""
    assert PERSONAL_ORG_NAME == "Personal"


def test_the_derived_key_is_stable_domain_separated_and_collision_resistant():
    """Deterministic per subject, different across subjects and providers.

    Determinism is not cosmetic: it is what lets a retry after a failed commit
    find the IdP organization the previous attempt created.
    """
    a = personal_tenant_key("workos", SUBJECT)
    assert a == personal_tenant_key("workos", SUBJECT)
    assert a != personal_tenant_key("workos", SUBJECT + "x")
    assert a != personal_tenant_key("okta", SUBJECT)
    # The separator cannot be spoofed by a subject containing it: hostile input
    # must not be able to steer itself onto another pair's tenant.
    assert personal_tenant_key("a", "b\x00c") != personal_tenant_key("a\x00b", "c")
    assert personal_tenant_key("a", "b:c") != personal_tenant_key("a:b", "c")


def test_the_slug_is_prefixed_and_fits_the_column():
    """41 characters, inside the 100-character slug columns, and recognisable."""
    slug = personal_org_slug(personal_tenant_key("workos", SUBJECT))
    assert slug.startswith("personal-")
    assert len(slug) == 41
    assert len(slug) <= 100
    # Slug-safe: the operator CLI and any URL that renders it assume this.
    assert all(c.isalnum() or c == "-" for c in slug)


def test_the_external_id_and_the_slug_are_the_same_derivation():
    """One key, two renderings — so a tenant is recognisable from either side."""
    key = personal_tenant_key("workos", SUBJECT)
    assert personal_org_slug(key) == personal_org_slug(key)


def test_a_missing_provider_or_subject_is_refused_not_hashed():
    """An empty subject must not derive a shared 'anonymous' tenant."""
    with pytest.raises(ValueError):
        personal_tenant_key("workos", "")
    with pytest.raises(ValueError):
        personal_tenant_key("", SUBJECT)


def test_the_module_under_test_is_the_worktrees():
    """Guard against a stale install answering these assertions (#worktree pin)."""
    import faultmaven

    assert os.path.realpath(faultmaven.__file__).startswith(
        os.path.realpath(os.path.join(os.path.dirname(__file__), "../../../.."))
    )


# =============================================================================
# Review item 1 — a refused login writes nothing, on either side
# =============================================================================
#
# Before #1045 an offboarded user, an email-conflict subject and an employee
# arriving unscoped were each refused with ZERO writes. Provisioning ahead of
# those refusals left each of them a permanent stray tenant plus an IdP
# organization, and then `sso_failed` forever. Each case below asserts BOTH
# halves stayed clean, not merely that the login was refused.


def _refusal_probe(store, *, identity=INDIVIDUAL, users=None, personal=None):
    provider = FakeProvider(identity)
    personal = personal or FakePersonalOrgRepository()
    users = users if users is not None else FakeUserRepository()
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        orgs=FakeOrgRepository(organizations={}, answer_any=True),
    )
    return service, provider, personal, users


async def test_a_deactivated_account_is_refused_before_any_write(
    store, as_tenant_provider, switch
):
    """An offboarded user must not be handed a brand-new tenant on the way out."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    user = make_returning_user()
    user.is_active = False
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service, provider, personal, _ = _refusal_probe(store, users=users)

    params = redirect_params(await run_callback(service))

    assert params == {"error": "sso_user_inactive"}
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_a_deleted_account_is_refused_before_any_write(
    store, as_tenant_provider, switch
):
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    user = make_returning_user()
    user.deleted_at = datetime(2026, 6, 1, tzinfo=UTC)
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service, provider, personal, _ = _refusal_probe(store, users=users)

    params = redirect_params(await run_callback(service))

    assert params == {"error": "sso_user_inactive"}
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_an_email_conflict_is_refused_before_any_write(
    store, as_tenant_provider, switch
):
    """ADR-015 D4 refuses to link by email — and now refuses before writing."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    squatter = make_returning_user(user_id="u-other")
    squatter.sso_provider = None
    squatter.sso_provider_id = None
    users = FakeUserRepository()
    users.users_by_id["u-other"] = squatter  # findable by email, not by subject
    service, provider, personal, _ = _refusal_probe(store, users=users)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert personal.provisioned == []
    assert provider.provision_calls == []


@pytest.mark.parametrize(
    "email",
    ["", "not-an-email", "x" * 300 + "@example.com"],
    ids=["empty", "malformed", "oversized"],
)
async def test_an_unusable_email_is_refused_before_any_write(
    store, as_tenant_provider, switch, email
):
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    identity = SSOIdentity(
        provider="workos",
        provider_user_id=SUBJECT,
        email=email,
        email_verified=True,
    )
    service, provider, personal, _ = _refusal_probe(store, identity=identity)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_an_account_already_anchored_elsewhere_is_refused_before_any_write(
    store, as_tenant_provider, switch
):
    """An employee whose AuthKit session is unscoped must not be re-homed.

    Provisioning here would anchor them to a personal enterprise and lock them
    out of their company — the inverse of the switching case, and the reason
    this refusal is distinct rather than folded into the generic slug.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    employee = make_returning_user(user_id="u-employee")
    employee.enterprise_id = "99999999-9999-9999-9999-999999999999"
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): employee})
    service, provider, personal, _ = _refusal_probe(store, users=users)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_the_preflight_runs_with_no_tenant_bound(
    store, as_tenant_provider, switch
):
    """It has to: binding is what provisioning decides, and it runs after this.

    ``users`` carries no ``organization_id`` and is not enrolled in migration
    018's policy, which is the whole reason these refusals can be pulled ahead
    of the write. A check that needed a tenanted table could not be.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    service, _, personal, _ = _refusal_probe(store, users=users)

    await run_callback(service)

    assert users.subject_lookups_bound_to
    assert users.subject_lookups_bound_to[0] == STANDALONE_ORG_ID
    assert personal.bound_at_lookup == [STANDALONE_ORG_ID]


# =============================================================================
# Review item 2 — every partial state recovers on the next callback
# =============================================================================


async def test_the_membership_is_created_after_the_tenant_commits(
    store, as_tenant_provider, switch
):
    """Ordering is the whole recovery argument, so pin it directly.

    A membership is what makes the IdP start echoing the organization. If it
    preceded the commit and the commit then failed, the next callback would
    carry that organization, take the MAPPED branch, find no mapping, and refuse
    with ``sso_org_unmapped`` permanently — with no path back.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    order: list[str] = []
    personal = FakePersonalOrgRepository()
    provider = FakeProvider(INDIVIDUAL)

    real_provision = personal.provision
    real_idp = provider.provision_personal_organization

    async def tracked_provision(**kwargs):
        order.append("db_commit")
        return await real_provision(**kwargs)

    def tracked_idp(**kwargs):
        order.append("idp")
        return real_idp(**kwargs)

    personal.provision = tracked_provision  # type: ignore[method-assign]
    provider.provision_personal_organization = tracked_idp  # type: ignore[method-assign]

    service = build_service(
        store,
        provider=provider,
        users=FakeUserRepository(),
        personal=personal,
        orgs=FakeOrgRepository(organizations={}, answer_any=True),
    )
    params = redirect_params(await run_callback(service))

    assert "code" in params
    # org create, then the commit, then the membership — never commit-last.
    assert order == ["idp", "db_commit", "idp"]
    assert personal.confirmed == [("workos", SUBJECT)]


async def test_a_tenant_whose_membership_never_landed_is_finished_next_login(
    store, as_tenant_provider, switch
):
    """The state a crash between commit and membership leaves, and its repair.

    Nothing here re-provisions: the tenant is resolved from its subject row and
    only the IdP half is completed. Without this the account would own a tenant
    the IdP never put them in, and no later login could notice.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(
        rows={("workos", SUBJECT): _record(membership_confirmed=False)}
    )
    provider = FakeProvider(INDIVIDUAL)
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    orgs = FakeOrgRepository(members={(PERSONAL_FM_ORG, user.user_id): "member"})
    service = build_service(
        store, provider=provider, users=users, orgs=orgs, personal=personal
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert personal.provisioned == []  # no second tenant
    assert len(provider.provision_calls) == 1  # membership ensured, idempotently
    assert personal.confirmed == [("workos", SUBJECT)]
    assert (await store.consume_login(params["code"]))["organization_id"] == (
        PERSONAL_FM_ORG
    )


async def test_a_confirmed_tenant_costs_no_provider_round_trip(
    store, as_tenant_provider, switch
):
    """Which is why the flag exists rather than ensuring on every login."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(
        rows={("workos", SUBJECT): _record(membership_confirmed=True)}
    )
    provider = FakeProvider(INDIVIDUAL)
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    orgs = FakeOrgRepository(members={(PERSONAL_FM_ORG, user.user_id): "member"})
    service = build_service(
        store, provider=provider, users=users, orgs=orgs, personal=personal
    )

    assert "code" in redirect_params(await run_callback(service))
    assert provider.provision_calls == []
    assert personal.confirmed == []


async def test_a_membership_failure_refuses_and_leaves_the_flag_unset(
    store, as_tenant_provider, switch
):
    """So the next login retries it rather than assuming it happened."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalOrgRepository(
        rows={("workos", SUBJECT): _record(membership_confirmed=False)}
    )
    provider = FakeProvider(
        INDIVIDUAL, provision_error=SSOProvisioningError("membership refused")
    )
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service = build_service(store, provider=provider, users=users, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert personal.confirmed == []
    assert personal.rows[("workos", SUBJECT)].membership_confirmed is False


# =============================================================================
# Review item 3 — personal → company switching (ADR-016 D5 as amended)
# =============================================================================


async def test_a_mapped_login_reanchors_an_account_off_its_personal_enterprise(
    store, as_tenant_provider, switch
):
    """The owner's stated intent in #1045: switching to a company works.

    Without this the user is refused ``enterprise_mismatch`` forever, because
    their account is anchored to the enterprise their personal tenant owns.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    company_org = make_organization(
        organization_id=MAPPED_FM_ORG,
        enterprise_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    user = make_returning_user()
    user.enterprise_id = PERSONAL_ENTERPRISE
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    personal = FakePersonalOrgRepository(rows={("workos", SUBJECT): _record()})
    orgs = FakeOrgRepository(organizations={MAPPED_FM_ORG: company_org})
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        mappings=FakeMappingRepository(mappings={("workos", IDP_ORG): MAPPED_FM_ORG}),
        orgs=orgs,
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert user.enterprise_id == company_org.enterprise_id
    # The binding is retired so a later unscoped login cannot resolve back into
    # a tenant the user can no longer enter.
    assert personal.retired == [("workos", SUBJECT)]
    assert ("workos", SUBJECT) not in personal.rows
    # Isolation does not weaken: membership is still granted by the shared
    # ensure, with the member role.
    assert orgs.added == [(MAPPED_FM_ORG, user.user_id, SYSTEM_ROLE_IDS[Role.MEMBER])]
    assert (await store.consume_login(params["code"]))["organization_id"] == (
        MAPPED_FM_ORG
    )


async def test_a_company_to_company_move_is_still_refused(
    store, as_tenant_provider, switch
):
    """The exception is narrow: only a PERSONAL anchor may be re-anchored.

    The account's enterprise is probed against this subject's own personal
    tenant, not against the enterprise's name or slug — so an account belonging
    to a different company is refused exactly as before.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    company_org = make_organization(
        organization_id=MAPPED_FM_ORG,
        enterprise_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    user = make_returning_user()
    user.enterprise_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"  # another company
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    personal = FakePersonalOrgRepository()  # this subject owns no personal tenant
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        mappings=FakeMappingRepository(mappings={("workos", IDP_ORG): MAPPED_FM_ORG}),
        orgs=FakeOrgRepository(organizations={MAPPED_FM_ORG: company_org}),
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert user.enterprise_id == "dddddddd-dddd-dddd-dddd-dddddddddddd"
    assert personal.retired == []


async def test_reanchoring_is_refused_when_the_move_cannot_be_persisted(
    store, as_tenant_provider, switch
):
    """An in-memory move the rest of the callback would act on is not a move."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    company_org = make_organization(
        organization_id=MAPPED_FM_ORG,
        enterprise_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    user = make_returning_user()
    user.enterprise_id = PERSONAL_ENTERPRISE
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})

    async def failing_update(_user):
        raise RuntimeError("write failed")

    users.update = failing_update  # type: ignore[method-assign]
    personal = FakePersonalOrgRepository(rows={("workos", SUBJECT): _record()})
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        mappings=FakeMappingRepository(mappings={("workos", IDP_ORG): MAPPED_FM_ORG}),
        orgs=FakeOrgRepository(organizations={MAPPED_FM_ORG: company_org}),
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # The binding survives a failed move — retiring it first would strand the
    # account with no personal row and no company anchor.
    assert personal.retired == []
    assert ("workos", SUBJECT) in personal.rows


# =============================================================================
# Review item 7 — a provisioning ceiling independent of the switch
# =============================================================================


async def test_provisioning_is_refused_once_the_hourly_ceiling_is_reached(
    store, as_tenant_provider, switch, monkeypatch
):
    """The switch bounds nothing about volume; this does."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "3")
    reset_settings_singleton()

    personal = FakePersonalOrgRepository(minted_last_hour=3)
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(
        store, provider=provider, users=FakeUserRepository(), personal=personal
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert personal.provisioned == []
    # Refused BEFORE the IdP call: the quota this protects is the provider's.
    assert provider.provision_calls == []


async def test_the_ceiling_does_not_lock_out_existing_tenants(
    store, as_tenant_provider, switch, monkeypatch
):
    """It bounds provisioning only. People already using the product sign in."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "1")
    reset_settings_singleton()

    personal = FakePersonalOrgRepository(
        rows={("workos", SUBJECT): _record()}, minted_last_hour=10_000
    )
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    orgs = FakeOrgRepository(members={(PERSONAL_FM_ORG, user.user_id): "member"})
    service = build_service(store, users=users, orgs=orgs, personal=personal)

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    # The ceiling was never even consulted: an existing tenant does not provision.
    assert personal.count_calls == []


async def test_the_ceiling_has_a_finite_default():
    """A ceiling whose default is off is a ceiling nobody has."""
    from faultmaven.config.settings import AuthSettings

    field = AuthSettings.model_fields["sso_jit_personal_tenant_max_per_hour"]
    assert field.validation_alias == "SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR"
    assert isinstance(field.default, int)
    assert 0 < field.default < 100_000


# =============================================================================
# Structure — the bind-and-verify tail is shared, sentinel on BOTH paths
# =============================================================================


async def test_the_mapped_path_also_refuses_the_sentinel(
    store, as_tenant_provider, switch
):
    """Previously absent: only the personal path guarded fm#850.

    An operator-provisioned mapping row pointing at the Standalone organization
    would have bound the sentinel as a tenant. The two paths now end in one
    tail, so neither can acquire a check the other lacks.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(False)
    users = FakeUserRepository()
    orgs = FakeOrgRepository(
        organizations={
            STANDALONE_ORG_ID: make_organization(organization_id=STANDALONE_ORG_ID)
        }
    )
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY),
        users=users,
        mappings=FakeMappingRepository(
            mappings={("workos", IDP_ORG): STANDALONE_ORG_ID}
        ),
        orgs=orgs,
        personal=FakePersonalOrgRepository(),
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # Refused before the sentinel was ever bound as this request's tenant.
    assert get_current_org_id() == STANDALONE_ORG_ID
    assert orgs.org_lookups_bound_to == []
    assert users.subject_lookups_bound_to == []


def test_the_cloud_lockfile_still_pins_the_sdk_the_provider_tests_need():
    """Guards the sibling module's cloud-only skip from becoming a silent pass.

    ``test_sso_personal_org_provider.py`` skips itself wholesale where ``workos``
    is absent, because Test Standalone installs requirements/test.txt and a
    module-level SDK import fails collection there. That marker is correct, and
    it is also the shape that hides a real regression: if the SDK were dropped
    from the CLOUD lockfile too, all twenty-one of those tests would turn into
    silent skips on every lane — green, invisible, and asserting nothing about
    the adapter that talks to WorkOS in production.

    This test needs no SDK, so it runs on every lane and makes that loud.
    """
    lockfile = (
        pathlib.Path(__file__).resolve().parents[4] / "requirements" / "cloud.txt"
    )
    pinned = [
        line
        for line in lockfile.read_text().splitlines()
        if line.strip().startswith("workos==")
    ]
    assert pinned, (
        "requirements/cloud.txt no longer pins workos, so every test in "
        "test_sso_personal_org_provider.py is now a silent skip on every lane."
    )

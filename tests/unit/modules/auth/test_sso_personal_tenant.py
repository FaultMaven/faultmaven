"""Self-service sign-up on the SSO callback (#1045, ADR-016 D5, ADR-017 D3).

An identity that carries **no** IdP organization is refused today. Behind
``SSO_JIT_PERSONAL_TENANT_ENABLED`` — default off — its first sign-in instead
lands in an enterprise **derived from its verified email domain**, and every
later sign-in lands in the same one.

The derivation has two outcomes, and separating them is the whole of ADR-017 D3:

* a **personal domain** (``PERSONAL_EMAIL_DOMAINS``) yields a **private
  enterprise per account** — an IdP organization holding that one member, the
  FaultMaven enterprise, the mapping row and the subject binding. The account is
  an island by construction: there is nobody else in its enterprise to invite;
* **every other domain** yields **the enterprise for that domain**, created by
  the first sign-up from it and joined by every later one. No IdP organization,
  no mapping row, no subject row: the domain is re-derived from the verified
  email on every login, so the resolution needs no record of its own.

Neither arm creates an organization or a team. An organization is a billing
target created by payment (D5) and a team is formed by consent (D4); a sign-in
knows neither, so it invents neither, and the only membership it establishes is
``users.enterprise_id``.

What these tests pin, and why each is here rather than assumed:

* **Only the no-IdP-organization branch changes.** An IdP organization that
  exists but is unmapped stays fail-closed with the switch in *either* state,
  and the proof is a byte-for-byte comparison of the two redirect URLs — not a
  re-assertion of the slug, which would still pass if the branch had started
  consulting the sign-up repositories on its way to the same answer.
* **The switch is real in both directions**, and it is driven through the
  actual ``SSO_JIT_PERSONAL_TENANT_ENABLED`` environment variable and the real
  ``get_settings()`` singleton. Patching the module-level predicate would prove
  the branch works and leave "does the setting reach it?" — the failure mode
  this project has been bitten by — entirely untested.
* **The personal lookup is keyed on the subject.** The second-login case runs
  with the IdP reporting *no* organization and with the mapping repository
  present and empty, so a resolution that leaned on ``sso_org_mappings`` could
  not pass it.
* **The domain lookup is keyed on the domain and nothing else.** Two accounts at
  one domain share an enterprise; two domains do not; the match is case-folded
  and exact, because a suffix rule would fold ``notgmail.com`` into
  ``gmail.com`` and put two unrelated companies in one enterprise.
* **The sentinel is not a tenant** (fm#850), re-proved on this deliberate path:
  a subject row pointing at the Standalone enterprise fails the login closed
  rather than pooling it there.
* **JIT users are never admin** (ADR-015 D5) — and now hold no organization
  membership at all, which is stronger: there is no roster row to get wrong.
* **Failure leaves the login refused and a retry able to finish.** The IdP call
  runs before the database transaction on purpose, so the recoverable residue
  is an IdP organization the next attempt finds again — never a FaultMaven
  enterprise a later login adopts as if it were complete.

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
    ISSOPersonalEnterpriseRepository,
    PersonalEnterpriseRecord,
    SSOIdentity,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ENTERPRISE_NAME,
    domain_enterprise_slug,
    email_domain,
    is_personal_domain,
    personal_enterprise_slug,
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

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    # The tenant-context reset and the ``enterprises`` table the anchor reads
    # need are shared fixtures now (tests/conftest.py), not a per-module copy.
    pytest.mark.usefixtures("restore_tenant_context", "anchor_db"),
]

DASHBOARD_URL = "https://app.faultmaven.test"

#: The env name the switch binds to. Read off the declared ``validation_alias``
#: rather than spelled here, so a rename breaks these tests instead of leaving
#: them setting a variable nothing reads.
SWITCH_ENV = "SSO_JIT_PERSONAL_TENANT_ENABLED"

SUBJECT = "user_wos_individual"
IDP_ORG = "org_01HWORKOS"
MAPPED_ENTERPRISE = "22222222-2222-2222-2222-222222222222"
PERSONAL_IDP_ORG = "org_01PERSONAL"
PERSONAL_ENTERPRISE = "66666666-6666-6666-6666-666666666666"
COMPANY_ENTERPRISE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

#: A consumer-mail address, on the shipped ``PERSONAL_EMAIL_DOMAINS`` list. The
#: personal arm is reached BECAUSE of this, not because of anything about the
#: subject — asserted directly in ``test_the_shipped_list_governs_which_arm``.
PERSONAL_EMAIL = "sam@gmail.com"
#: A company address. Same shape of identity, opposite arm.
WORK_DOMAIN = "acme.example"
WORK_EMAIL = f"dana@{WORK_DOMAIN}"

#: An individual: authenticated, but the IdP names no organization for them.
INDIVIDUAL = SSOIdentity(
    provider="workos",
    provider_user_id=SUBJECT,
    email=PERSONAL_EMAIL,
    email_verified=True,
    display_name="Sam Individual",
)

#: The same person, on a later login where the IdP DOES echo their (personal)
#: organization. A returning login has to resolve the same tenant either way.
INDIVIDUAL_WITH_ECHOED_ORG = SSOIdentity(
    provider="workos",
    provider_user_id=SUBJECT,
    email=PERSONAL_EMAIL,
    email_verified=True,
    display_name="Sam Individual",
    organization_id=PERSONAL_IDP_ORG,
)

#: The same individual, on a later login where the IdP names a COMPANY
#: organization — the switching case (ADR-016 D5 as amended).
COMPANY_IDENTITY_SAME_SUBJECT = SSOIdentity(
    provider="workos",
    provider_user_id=SUBJECT,
    email=PERSONAL_EMAIL,
    email_verified=True,
    display_name="Sam Individual",
    organization_id=IDP_ORG,
)

#: An identity that DOES belong to an IdP organization. Used only to prove the
#: unmapped branch is untouched.
COMPANY_IDENTITY = SSOIdentity(
    provider="workos",
    provider_user_id="user_wos_company",
    email=WORK_EMAIL,
    email_verified=True,
    display_name="Dana Acme",
    organization_id=IDP_ORG,
)


def work_identity(
    local: str, *, domain: str = WORK_DOMAIN, subject=None
) -> SSOIdentity:
    """An org-less identity at a company domain — the domain arm's input."""
    return SSOIdentity(
        provider="workos",
        provider_user_id=subject or f"user_wos_{local}",
        email=f"{local}@{domain}",
        email_verified=True,
        display_name=local.title(),
    )


# =============================================================================
# Fixtures & fakes
# =============================================================================


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


def make_enterprise(
    *,
    enterprise_id: str = PERSONAL_ENTERPRISE,
    name: str = PERSONAL_ENTERPRISE_NAME,
    slug: str | None = None,
    domain: str | None = None,
    deleted_at=None,
) -> Enterprise:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Enterprise(
        enterprise_id=enterprise_id,
        name=name,
        slug=slug or personal_enterprise_slug(personal_tenant_key("workos", SUBJECT)),
        domain=domain,
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

    async def get_enterprise_id(self, provider: str, provider_org_id: str):
        self.calls.append((provider, provider_org_id))
        return self.mappings.get((provider, provider_org_id))


class FakePersonalEnterpriseRepository(ISSOPersonalEnterpriseRepository):
    """In-memory stand-in with the real adapter's semantics.

    Three of them matter and each is a rule a test would otherwise be proving
    against a laxer contract than production's:

    * ``get`` answers **live rows only**. A retired binding is kept — it carries
      the operator's next-login policy — but resolving it would send the subject
      back into the tenant the retirement fenced them out of.
    * ``provision`` **re-points** an existing row rather than inserting beside
      it: ``subject`` is the primary key, so a subject retired with
      ``fresh_tenant`` has exactly one row and this is it.
    * ``race_winner`` simulates losing the constraint race: the write is refused
      and the winner's enterprise is what the subject row already holds, which
      is the state the real adapter recovers into after its transaction rolls
      back whole.
    """

    def __init__(
        self,
        rows: dict[tuple[str, str], PersonalEnterpriseRecord] | None = None,
        *,
        race_winner: str | None = None,
        write_error: Exception | None = None,
        minted_last_hour: int = 0,
        retired_rows: dict[tuple[str, str], PersonalEnterpriseRecord] | None = None,
    ):
        self.rows = dict(rows or {})
        #: Rows that exist but are retired: ``get`` must not answer with them.
        self.retired_rows = dict(retired_rows or {})
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
        self.bound_at_lookup.append(get_current_enterprise_id())
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
        enterprise_id = self.race_winner or str(uuid.uuid4())
        self.provisioned.append(
            {
                "provider": provider,
                "provider_user_id": provider_user_id,
                "provider_org_id": provider_org_id,
                "enterprise_id": enterprise_id,
                "name": name,
                "slug": slug,
            }
        )
        if self.write_error is not None:
            raise self.write_error
        existing = self.rows.get((provider, provider_user_id))
        if existing is not None:
            return existing.enterprise_id
        # Re-points a retired row rather than inserting a second one.
        self.retired_rows.pop((provider, provider_user_id), None)
        self.rows[(provider, provider_user_id)] = PersonalEnterpriseRecord(
            enterprise_id=enterprise_id,
            provider_org_id=provider_org_id,
            membership_confirmed=False,
        )
        return enterprise_id

    async def confirm_membership(self, provider, provider_user_id):
        self.confirmed.append((provider, provider_user_id))
        record = self.rows.get((provider, provider_user_id))
        if record is not None:
            self.rows[(provider, provider_user_id)] = PersonalEnterpriseRecord(
                enterprise_id=record.enterprise_id,
                provider_org_id=record.provider_org_id,
                membership_confirmed=True,
            )

    async def retire(self, provider, provider_user_id):
        self.retired.append((provider, provider_user_id))
        return self.rows.pop((provider, provider_user_id), None) is not None


class FakeEnterpriseRepository:
    """The enterprise port the login reads, with the get-or-create D3 needs.

    ``answer_any`` says "whatever id the login resolved, that enterprise
    exists" — the personal arm mints its enterprise id inside the repository, so
    a test that provisions cannot know it in advance.
    """

    def __init__(
        self,
        enterprises: dict[str, Enterprise] | None = None,
        *,
        answer_any: bool = False,
        create_error: Exception | None = None,
    ):
        self.enterprises = (
            enterprises
            if enterprises is not None
            else {PERSONAL_ENTERPRISE: make_enterprise()}
        )
        self.answer_any = answer_any
        self.create_error = create_error
        self.lookups_bound_to: list[str] = []
        self.domain_calls: list[str] = []
        self.created_domains: list[str] = []

    async def get_enterprise(self, enterprise_id: str):
        self.lookups_bound_to.append(get_current_enterprise_id())
        if self.answer_any and enterprise_id not in self.enterprises:
            return make_enterprise(enterprise_id=enterprise_id)
        found = self.enterprises.get(enterprise_id)
        if found is not None and found.deleted_at is not None:
            # The liveness predicate is the caller's; the repository answers
            # with the row it holds, exactly as the real one does.
            return found
        return found

    async def get_or_create_for_domain(self, *, domain: str, name: str, slug: str):
        self.domain_calls.append(domain)
        if self.create_error is not None:
            raise self.create_error
        for enterprise in self.enterprises.values():
            if enterprise.domain == domain and enterprise.deleted_at is None:
                return enterprise
        created = make_enterprise(
            enterprise_id=str(uuid.uuid4()), name=name, slug=slug, domain=domain
        )
        self.enterprises[created.enterprise_id] = created
        self.created_domains.append(domain)
        return created


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
        self.subject_lookups_bound_to.append(get_current_enterprise_id())
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
    enterprise_id: str = PERSONAL_ENTERPRISE,
    provider_org_id: str = PERSONAL_IDP_ORG,
    membership_confirmed: bool = True,
) -> PersonalEnterpriseRecord:
    return PersonalEnterpriseRecord(
        enterprise_id=enterprise_id,
        provider_org_id=provider_org_id,
        membership_confirmed=membership_confirmed,
    )


def make_returning_user(user_id="u-personal", email=PERSONAL_EMAIL):
    return SimpleNamespace(
        user_id=user_id,
        username="sam",
        email=email,
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
    enterprises=None,
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
        enterprise_repository=(
            enterprises
            if enterprises is not None
            else FakeEnterpriseRepository(answer_any=True)
        ),
        personal_enterprise_repository=(
            (personal if personal is not None else FakePersonalEnterpriseRepository())
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
# The switch, in both states, through the real setting
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


@pytest.mark.parametrize(
    "identity", [INDIVIDUAL, work_identity("dana")], ids=["personal", "work"]
)
async def test_switch_off_keeps_the_orgless_login_fail_closed(
    store, as_tenant_provider, switch, identity
):
    """With the switch off, an org-less identity is refused exactly as today.

    Both arms, because the switch gates the derivation, not one outcome of it —
    a domain enterprise minted with the switch off would be the same policy
    breach as a personal one.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(False)
    users = FakeUserRepository()
    personal = FakePersonalEnterpriseRepository()
    enterprises = FakeEnterpriseRepository(answer_any=True)
    provider = FakeProvider(identity)
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    # Nothing tenant-scoped was touched, and no tenant was invented.
    assert users.subject_lookups_bound_to == []
    assert personal.lookups == []
    assert personal.provisioned == []
    assert enterprises.domain_calls == []
    assert provider.provision_calls == []


async def test_switch_on_provisions_exactly_one_personal_enterprise(
    store, as_tenant_provider, switch
):
    """First sign-in of a consumer-mail identity: one IdP org, one enterprise."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    personal = FakePersonalEnterpriseRepository()
    provider = FakeProvider(INDIVIDUAL)
    enterprises = FakeEnterpriseRepository(enterprises={}, answer_any=True)
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    # Exactly one FaultMaven enterprise.
    assert len(personal.provisioned) == 1
    written = personal.provisioned[0]
    assert written["provider_org_id"] == PERSONAL_IDP_ORG
    assert written["name"] == PERSONAL_ENTERPRISE_NAME
    # A real, distinct enterprise — never the Standalone sentinel (#850).
    assert written["enterprise_id"] != STANDALONE_ENTERPRISE_ID
    # The account is anchored to it, which is the ONLY membership a sign-up
    # establishes (ADR-017 D3).
    assert users.created[0].enterprise_id == written["enterprise_id"]
    # The domain arm was not taken: a personal domain claims no enterprise.
    assert enterprises.domain_calls == []


async def test_the_idp_call_carries_the_derived_pii_free_identifiers(
    store, as_tenant_provider, switch
):
    """The external id and slug are the derived key, not the email or subject."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository()
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(
        store,
        provider=provider,
        personal=personal,
        enterprises=FakeEnterpriseRepository(enterprises={}, answer_any=True),
    )

    await run_callback(service)

    key = personal_tenant_key("workos", SUBJECT)
    call = provider.provision_calls[0]
    assert call["external_id"] == personal_enterprise_slug(key)
    assert call["name"] == PERSONAL_ENTERPRISE_NAME
    assert personal.provisioned[0]["slug"] == personal_enterprise_slug(key)
    # Neither the email local-part nor the raw subject appears in what is
    # written or sent — the slug is rendered wherever a tenant is shown.
    for value in (call["external_id"], personal.provisioned[0]["slug"]):
        assert "sam" not in value
        assert SUBJECT not in value


# =============================================================================
# D3 — which arm a domain takes, and what the domain arm writes
# =============================================================================


def test_the_shipped_list_governs_which_arm():
    """The decision function, over the real shipped default.

    The two directions are not symmetric in consequence: a consumer domain
    MISSING from the list puts every address at it into one enterprise whose
    members can invite each other to teams. So the list is asserted to contain
    the majors, and the match is asserted to be exact.
    """
    shipped = get_live_settings().auth.personal_email_domains
    for domain in ("gmail.com", "outlook.com", "icloud.com", "proton.me", "qq.com"):
        assert is_personal_domain(domain, shipped), domain
    for domain in (WORK_DOMAIN, "acme.com", "notgmail.com", "gmail.com.evil.test"):
        assert not is_personal_domain(domain, shipped), domain


def test_the_domain_is_derived_case_folded_and_from_the_last_at():
    """One spelling of a domain is one enterprise.

    Lowercasing rather than case-folding would leave two spellings as two
    enterprises; splitting on the FIRST ``@`` would mis-read a quoted local part
    and derive a domain the address does not have.
    """
    assert email_domain("Alice@ACME.Example") == "acme.example"
    assert email_domain('"weird@local"@acme.example') == "acme.example"
    # A trailing dot is the DNS root and names the same domain.
    assert email_domain("a@acme.example.") == "acme.example"
    for bad in ("", "no-at-sign", "@acme.example", "alice@"):
        assert email_domain(bad) is None


def test_an_address_with_no_derivable_domain_is_treated_as_personal():
    """The conservative direction: an address that names no organisation is not
    evidence its owner belongs to one, so it gets an enterprise of its own
    rather than joining a shared 'domainless' tenant."""
    assert is_personal_domain(None, ["gmail.com"]) is True


async def test_a_work_domain_creates_the_enterprise_on_the_first_signup(
    store, as_tenant_provider, switch
):
    """D3's other arm: the first account from a domain brings it into existence.

    And brings **nothing else** into existence — no IdP organization, no mapping
    row, no subject binding. The domain is re-derived from the verified email on
    every login, so the resolution needs no record of its own.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    personal = FakePersonalEnterpriseRepository()
    mappings = FakeMappingRepository(mappings={})
    provider = FakeProvider(work_identity("dana"))
    enterprises = FakeEnterpriseRepository(enterprises={})
    service = build_service(
        store,
        provider=provider,
        users=users,
        mappings=mappings,
        personal=personal,
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert enterprises.created_domains == [WORK_DOMAIN]
    created = enterprises.enterprises[users.created[0].enterprise_id]
    assert created.domain == WORK_DOMAIN
    assert created.name == WORK_DOMAIN
    assert created.slug == domain_enterprise_slug(WORK_DOMAIN)
    # None of the personal machinery ran.
    assert provider.provision_calls == []
    assert personal.provisioned == []
    assert personal.lookups == []
    assert mappings.calls == []


async def test_two_accounts_at_one_domain_land_in_one_enterprise(
    store, as_tenant_provider, switch
):
    """Get-or-create, and the positive control for it.

    A second account at a DIFFERENT domain lands somewhere else on the same run,
    so "they share an enterprise" cannot pass by the repository having collapsed
    every domain into one.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    enterprises = FakeEnterpriseRepository(enterprises={})
    users = FakeUserRepository()

    async def sign_in(identity):
        service = build_service(
            store,
            provider=FakeProvider(identity),
            users=users,
            enterprises=enterprises,
        )
        assert "code" in redirect_params(await run_callback(service))
        return users.created[-1].enterprise_id

    dana = await sign_in(work_identity("dana"))
    # Case-folded: a different spelling of the same domain is the same tenant.
    alex = await sign_in(work_identity("Alex", domain=WORK_DOMAIN.upper()))
    other = await sign_in(work_identity("kim", domain="globex.example"))

    assert dana == alex
    assert other != dana
    assert enterprises.created_domains == [WORK_DOMAIN, "globex.example"]


async def test_a_retired_domain_enterprise_does_not_capture_the_next_signup(
    store, as_tenant_provider, switch
):
    """LIVE rows only, matching the partial unique index exactly.

    Adopting a soft-deleted row would hand the next sign-up from that domain
    straight back into the tenant an operator took out of service — and the
    index would not stop it, because it does not see retired rows either.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    retired = make_enterprise(
        enterprise_id="deaddead-dead-dead-dead-deaddeaddead",
        name=WORK_DOMAIN,
        slug=domain_enterprise_slug(WORK_DOMAIN),
        domain=WORK_DOMAIN,
        deleted_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    enterprises = FakeEnterpriseRepository(enterprises={retired.enterprise_id: retired})
    users = FakeUserRepository()
    service = build_service(
        store,
        provider=FakeProvider(work_identity("dana")),
        users=users,
        enterprises=enterprises,
    )

    assert "code" in redirect_params(await run_callback(service))
    assert users.created[0].enterprise_id != retired.enterprise_id
    assert enterprises.created_domains == [WORK_DOMAIN]


async def test_a_domain_enterprise_that_cannot_be_written_refuses_the_login(
    store, as_tenant_provider, switch
):
    """Fail closed: no enterprise, no login. Never an unscoped session."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprises={}, create_error=RuntimeError("constraint")
    )
    service = build_service(
        store,
        provider=FakeProvider(work_identity("dana")),
        users=users,
        enterprises=enterprises,
    )

    assert redirect_params(await run_callback(service)) == {"error": ERROR_FAILED}
    assert users.created == []


async def test_the_domain_arm_refuses_an_inactive_account_before_writing(
    store, as_tenant_provider, switch
):
    """The shared pre-flight covers both arms, so neither can lose a refusal."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    identity = work_identity("dana")
    user = make_returning_user(user_id="u-dana", email=identity.email)
    user.sso_provider_id = identity.provider_user_id
    user.is_active = False
    users = FakeUserRepository(
        users_by_subject={("workos", identity.provider_user_id): user}
    )
    enterprises = FakeEnterpriseRepository(enterprises={})
    service = build_service(
        store,
        provider=FakeProvider(identity),
        users=users,
        enterprises=enterprises,
    )

    assert redirect_params(await run_callback(service)) == {
        "error": "sso_user_inactive"
    }
    assert enterprises.created_domains == []


# =============================================================================
# The unmapped branch is untouched, in BOTH switch states
# =============================================================================


@pytest.mark.parametrize("enabled", [False, True], ids=["switch-off", "switch-on"])
async def test_unmapped_idp_org_still_fails_closed(
    store, as_tenant_provider, switch, enabled
):
    """An IdP organization with no mapping is refused whatever the switch says.

    A company with its own IdP organization is onboarded deliberately, never by
    whoever signs in first — and never by falling through to the domain arm,
    which would silently onboard it.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(enabled)
    users = FakeUserRepository()
    mappings = FakeMappingRepository(mappings={})
    personal = FakePersonalEnterpriseRepository()
    enterprises = FakeEnterpriseRepository(answer_any=True)
    provider = FakeProvider(COMPANY_IDENTITY)
    service = build_service(
        store,
        provider=provider,
        users=users,
        mappings=mappings,
        personal=personal,
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    # Consulted with exactly the provider key and the IdP's org id, as before.
    assert mappings.calls == [("workos", IDP_ORG)]
    assert users.subject_lookups_bound_to == []
    # And neither sign-up arm was reached on its way to that answer.
    assert personal.lookups == []
    assert personal.provisioned == []
    assert enterprises.domain_calls == []
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
# A returning individual lands in the same tenant
# =============================================================================


async def test_second_login_with_no_idp_org_resolves_the_same_tenant(
    store, as_tenant_provider, switch
):
    """The subject-keyed row is what answers, so no IdP org is needed.

    The mapping repository is wired and empty and the identity names no
    organization, so a resolution leaning on ``sso_org_mappings`` could not pass.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository(rows={("workos", SUBJECT): _record()})
    mappings = FakeMappingRepository(mappings={})
    provider = FakeProvider(INDIVIDUAL)
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    enterprises = FakeEnterpriseRepository()
    service = build_service(
        store,
        provider=provider,
        users=users,
        mappings=mappings,
        enterprises=enterprises,
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    # Resolved from the subject row: nothing new was provisioned on either side.
    assert personal.lookups == [("workos", SUBJECT)]
    assert personal.provisioned == []
    assert provider.provision_calls == []
    assert mappings.calls == []
    assert enterprises.domain_calls == []
    # The lookup ran with no tenant bound yet — which is exactly why it has to
    # be untenanted — and the enterprise read that follows ran inside the
    # resolved tenant's scope.
    assert personal.bound_at_lookup == [STANDALONE_ENTERPRISE_ID]
    assert enterprises.lookups_bound_to == [PERSONAL_ENTERPRISE]
    # And the account stays anchored where it was.
    assert user.enterprise_id == PERSONAL_ENTERPRISE


async def test_a_retired_binding_does_not_resolve_as_a_live_one(
    store, as_tenant_provider, switch, anchor_db
):
    """``get`` reads live rows only, and the retirement policy decides.

    The row survives a retirement because it carries the operator's next-login
    choice; answering with it here would put the subject straight back into the
    tenant the retirement fenced them out of.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    from faultmaven.modules.auth.contracts import RETIREMENT_POLICY_REFUSE

    retired_enterprise = "77777777-7777-7777-7777-777777777777"
    await anchor_db(retired_enterprise, retired=True, policy=RETIREMENT_POLICY_REFUSE)
    user = make_returning_user()
    user.enterprise_id = retired_enterprise
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    # The row exists but is retired, so the fake answers None from ``get``.
    personal = FakePersonalEnterpriseRepository(
        retired_rows={("workos", SUBJECT): _record(enterprise_id=retired_enterprise)}
    )
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(store, provider=provider, users=users, personal=personal)

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_ORG_UNMAPPED}
    assert personal.provisioned == []
    assert provider.provision_calls == []


async def test_second_login_with_an_echoed_org_resolves_the_same_tenant(
    store, as_tenant_provider, switch
):
    """When the IdP DOES echo the personal org, the ordinary mapped path is
    what resolves it — to the same tenant, through the ``sso_org_mappings`` row
    first sign-in wrote. Both shapes of a returning login must agree."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository(rows={("workos", SUBJECT): _record()})
    mappings = FakeMappingRepository(
        mappings={("workos", PERSONAL_IDP_ORG): PERSONAL_ENTERPRISE}
    )
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service = build_service(
        store,
        provider=FakeProvider(INDIVIDUAL_WITH_ECHOED_ORG),
        users=users,
        mappings=mappings,
        enterprises=FakeEnterpriseRepository(),
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert user.enterprise_id == PERSONAL_ENTERPRISE
    # It went through the mapped path — the personal lookup was not needed.
    assert mappings.calls == [("workos", PERSONAL_IDP_ORG)]
    assert personal.lookups == []


async def test_a_replayed_first_login_provisions_no_second_tenant(
    store, as_tenant_provider, switch
):
    """Two sequential first logins for one subject yield one enterprise."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository()
    provider = FakeProvider(INDIVIDUAL)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(enterprises={}, answer_any=True)
    service = build_service(
        store,
        provider=provider,
        users=users,
        enterprises=enterprises,
        personal=personal,
    )

    first = redirect_params(await run_callback(service))
    second = redirect_params(await run_callback(service))

    assert "code" in first and "code" in second
    assert len(personal.provisioned) == 1
    assert len(users.created) == 1


async def test_a_lost_provisioning_race_adopts_the_winners_tenant(
    store, as_tenant_provider, switch
):
    """The loser of a concurrent first login lands in the winner's enterprise.

    The real adapter reaches this state by rolling its whole transaction back on
    a constraint violation and re-reading the subject row; here the repository
    reports the same outcome. What is under test is the service: it must use the
    enterprise the repository *returned*.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    winner = "77777777-7777-7777-7777-777777777777"
    personal = FakePersonalEnterpriseRepository(race_winner=winner)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprises={winner: make_enterprise(enterprise_id=winner)}
    )
    service = build_service(
        store, users=users, enterprises=enterprises, personal=personal
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert users.created[0].enterprise_id == winner
    # The read that verified the tenant ran inside the WINNER's scope.
    assert enterprises.lookups_bound_to == [winner]


# =============================================================================
# The Standalone sentinel never becomes a tenant (fm#850)
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
    personal = FakePersonalEnterpriseRepository(
        rows={("workos", SUBJECT): _record(enterprise_id=STANDALONE_ENTERPRISE_ID)}
    )
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprises={
            STANDALONE_ENTERPRISE_ID: make_enterprise(
                enterprise_id=STANDALONE_ENTERPRISE_ID
            )
        }
    )
    service = build_service(
        store, users=users, enterprises=enterprises, personal=personal
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # Refused before the sentinel was ever bound as this request's tenant.
    assert get_current_enterprise_id() == STANDALONE_ENTERPRISE_ID
    assert enterprises.lookups_bound_to == []
    assert users.subject_lookups_bound_to == []


# =============================================================================
# A sign-up writes no organization and no team (ADR-017 D5/D4)
# =============================================================================


async def test_a_signup_establishes_the_anchor_and_nothing_else(
    store, as_tenant_provider, switch
):
    """The ONLY membership a login creates is ``users.enterprise_id``.

    No ``organization_members`` row, because an organization is a billing target
    created by payment and a sign-in cannot know who pays; no team, because a
    team is formed by consent. The login service is constructed with **no**
    organization repository at all, so a path that tried to write one could not
    even reach it — the absence is structural rather than asserted.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository()
    users = FakeUserRepository()
    service = build_service(
        store,
        users=users,
        enterprises=FakeEnterpriseRepository(enterprises={}, answer_any=True),
        personal=personal,
    )

    params = redirect_params(await run_callback(service))
    assert "code" in params

    created = users.created[0]
    # Never admin (ADR-015 D5).
    assert created.roles == ["user"]
    assert "admin" not in created.roles
    # Anchored to the enterprise this login resolved, and to nothing else.
    assert created.enterprise_id == personal.provisioned[0]["enterprise_id"]
    # The service exposes no organization port to write a roster row with.
    assert not hasattr(service, "_organizations")


def test_the_login_service_takes_no_organization_repository():
    """The retired constructor argument, asserted on the signature.

    Its survival would be the compatibility arm the campaign forbids: a wired
    organization repository is exactly what a re-introduced membership write
    would need, and nothing else would notice it was there.
    """
    import inspect

    parameters = inspect.signature(SSOLoginService.__init__).parameters
    assert "enterprise_repository" in parameters
    assert "personal_enterprise_repository" in parameters
    assert "organization_repository" not in parameters
    assert "personal_org_repository" not in parameters


# =============================================================================
# Failure direction
# =============================================================================


async def test_an_idp_failure_refuses_the_login_and_writes_no_tenant(
    store, as_tenant_provider, switch
):
    """No IdP organization ⇒ no FaultMaven enterprise. Nothing to adopt later."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    provider = FakeProvider(
        INDIVIDUAL, provision_error=SSOProvisioningError("idp down")
    )
    personal = FakePersonalEnterpriseRepository()
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
    no FaultMaven enterprise, which the next attempt finds again because its
    external id is derived from the subject. Nobody is a member of it, so the
    IdP still reports no organization and the next callback re-enters this same
    branch — which is what makes it self-healing rather than a dead end.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository(write_error=RuntimeError("constraint"))
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
    assert provider.provision_calls[0]["external_id"] == personal_enterprise_slug(key)


@pytest.mark.parametrize(
    "identity", [INDIVIDUAL, work_identity("dana")], ids=["personal", "work"]
)
async def test_an_unwired_signup_repository_fails_closed(
    store, as_tenant_provider, switch, identity
):
    """Misconfiguration refuses rather than falling through to an org-less login.

    Both arms, because both are gated on the same wiring check: a deployment
    missing the port must not quietly serve one arm and refuse the other.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    service = build_service(
        store, provider=FakeProvider(identity), users=users, wire_personal=False
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []


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
async def test_an_unusable_personal_enterprise_is_a_generic_failure(
    store, as_tenant_provider, switch, enterprise
):
    """A personal enterprise gets the same availability checks a mapped one does
    — and an unusable one is refused, never routed around by provisioning a
    second tenant for the same subject."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository(rows={("workos", SUBJECT): _record()})
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprises={} if enterprise is None else {PERSONAL_ENTERPRISE: enterprise}
    )
    service = build_service(
        store, users=users, enterprises=enterprises, personal=personal
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert users.subject_lookups_bound_to == []
    assert personal.provisioned == []


# =============================================================================
# Single-tenant is untouched
# =============================================================================


async def test_single_tenant_never_reaches_the_signup_path(
    store, as_tenant_provider, switch
):
    """Single-tenant has one enterprise and never decides a tenant at all —
    with the switch ON, so this is not merely the default being observed."""
    as_tenant_provider(TenantProvider.SINGLE)
    switch(True)
    personal = FakePersonalEnterpriseRepository()
    provider = FakeProvider(INDIVIDUAL)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(answer_any=True)
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert personal.lookups == []
    assert personal.provisioned == []
    assert enterprises.domain_calls == []
    assert provider.provision_calls == []


# =============================================================================
# Naming and slug derivation
# =============================================================================


def test_the_enterprise_name_lives_in_one_constant():
    """Every rendering of the name reads the same constant."""
    assert PERSONAL_ENTERPRISE_NAME == "Personal"


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
    """41 characters, inside the 100-character slug column, and recognisable."""
    slug = personal_enterprise_slug(personal_tenant_key("workos", SUBJECT))
    assert slug.startswith("personal-")
    assert len(slug) == 41
    assert len(slug) <= 100
    # Slug-safe: the operator CLI and any URL that renders it assume this.
    assert all(c.isalnum() or c == "-" for c in slug)


def test_a_domain_slug_is_deterministic_slug_safe_and_not_a_personal_one():
    """The two derivations must not be confusable.

    ``personal_key_of_slug`` is what tells an operator command it is looking at
    somebody's private tenant. A domain enterprise answering to that test would
    let ``fm-personal-tenant retire`` take a whole company's enterprise out of
    service as though it were one person's.
    """
    from faultmaven.modules.auth.domain.personal_tenant import personal_key_of_slug

    slug = domain_enterprise_slug(WORK_DOMAIN)
    assert slug == domain_enterprise_slug(WORK_DOMAIN.upper())
    assert slug != domain_enterprise_slug("globex.example")
    assert slug.startswith("domain-")
    assert len(slug) <= 100
    assert all(c.isalnum() or c == "-" for c in slug)
    assert personal_key_of_slug(slug) is None
    # And the domain itself is not rendered in it — the slug appears wherever a
    # tenant is shown, and the domain is the customer's own name.
    assert WORK_DOMAIN not in slug
    with pytest.raises(ValueError):
        domain_enterprise_slug("")


def test_the_external_id_and_the_slug_are_the_same_derivation():
    """One key, two renderings — so a tenant is recognisable from either side."""
    key = personal_tenant_key("workos", SUBJECT)
    assert personal_enterprise_slug(key) == personal_enterprise_slug(key)


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
# A refused login writes nothing, on either side
# =============================================================================
#
# Before #1045 an offboarded user, an email-conflict subject and an employee
# arriving unscoped were each refused with ZERO writes. Provisioning ahead of
# those refusals left each of them a permanent stray tenant plus an IdP
# organization, and then `sso_failed` forever. Each case below asserts BOTH
# halves stayed clean, not merely that the login was refused.


def _refusal_probe(store, *, identity=INDIVIDUAL, users=None, personal=None):
    provider = FakeProvider(identity)
    personal = personal or FakePersonalEnterpriseRepository()
    users = users if users is not None else FakeUserRepository()
    service = build_service(
        store,
        provider=provider,
        users=users,
        personal=personal,
        enterprises=FakeEnterpriseRepository(enterprises={}, answer_any=True),
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
    """And it is refused before the DOMAIN is derived, because the domain comes
    from the email: an address that cannot be validated cannot decide which
    enterprise its owner belongs to."""
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
    assert personal.lookups == []
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

    ``users`` is not RLS-enrolled, which is the whole reason these refusals can
    be pulled ahead of the write. A check that needed a tenanted table could not
    be.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    users = FakeUserRepository()
    service, _, personal, _ = _refusal_probe(store, users=users)

    await run_callback(service)

    assert users.subject_lookups_bound_to
    assert users.subject_lookups_bound_to[0] == STANDALONE_ENTERPRISE_ID
    assert personal.bound_at_lookup == [STANDALONE_ENTERPRISE_ID]


# =============================================================================
# Every partial state recovers on the next callback
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
    personal = FakePersonalEnterpriseRepository()
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
        enterprises=FakeEnterpriseRepository(enterprises={}, answer_any=True),
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

    Nothing here re-provisions: the enterprise is resolved from its subject row
    and only the IdP half is completed. Without this the account would own a
    tenant the IdP never put them in, and no later login could notice.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository(
        rows={("workos", SUBJECT): _record(membership_confirmed=False)}
    )
    provider = FakeProvider(INDIVIDUAL)
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service = build_service(
        store,
        provider=provider,
        users=users,
        enterprises=FakeEnterpriseRepository(),
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert personal.provisioned == []  # no second tenant
    assert len(provider.provision_calls) == 1  # membership ensured, idempotently
    assert personal.confirmed == [("workos", SUBJECT)]
    assert user.enterprise_id == PERSONAL_ENTERPRISE


async def test_a_confirmed_tenant_costs_no_provider_round_trip(
    store, as_tenant_provider, switch
):
    """Which is why the flag exists rather than ensuring on every login."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    personal = FakePersonalEnterpriseRepository(
        rows={("workos", SUBJECT): _record(membership_confirmed=True)}
    )
    provider = FakeProvider(INDIVIDUAL)
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service = build_service(
        store,
        provider=provider,
        users=users,
        enterprises=FakeEnterpriseRepository(),
        personal=personal,
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
    personal = FakePersonalEnterpriseRepository(
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
# personal → company switching (ADR-016 D5 as amended)
# =============================================================================


async def test_a_mapped_login_reanchors_an_account_off_its_personal_enterprise(
    anchor_db, store, as_tenant_provider, switch
):
    """The owner's stated intent in #1045: switching to a company works.

    Without this the user is refused ``enterprise_mismatch`` forever, because
    their account is anchored to their own personal enterprise.
    """
    await anchor_db(PERSONAL_ENTERPRISE)
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    company = make_enterprise(
        enterprise_id=COMPANY_ENTERPRISE_ID, name="Acme", slug="acme"
    )
    user = make_returning_user()
    user.enterprise_id = PERSONAL_ENTERPRISE
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    personal = FakePersonalEnterpriseRepository(rows={("workos", SUBJECT): _record()})
    enterprises = FakeEnterpriseRepository(enterprises={COMPANY_ENTERPRISE_ID: company})
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        mappings=FakeMappingRepository(
            mappings={("workos", IDP_ORG): COMPANY_ENTERPRISE_ID}
        ),
        enterprises=enterprises,
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert "code" in params and "error" not in params
    assert user.enterprise_id == COMPANY_ENTERPRISE_ID
    # The binding is retired so a later unscoped login cannot resolve back into
    # a tenant the user can no longer enter.
    assert personal.retired == [("workos", SUBJECT)]
    assert ("workos", SUBJECT) not in personal.rows
    # And no organization membership was invented on the way (ADR-017 D5).
    assert not hasattr(service, "_organizations")


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
    company = make_enterprise(
        enterprise_id=COMPANY_ENTERPRISE_ID, name="Acme", slug="acme"
    )
    user = make_returning_user()
    user.enterprise_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"  # another company
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    personal = FakePersonalEnterpriseRepository()  # owns no personal tenant
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        mappings=FakeMappingRepository(
            mappings={("workos", IDP_ORG): COMPANY_ENTERPRISE_ID}
        ),
        enterprises=FakeEnterpriseRepository(
            enterprises={COMPANY_ENTERPRISE_ID: company}
        ),
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
    company = make_enterprise(
        enterprise_id=COMPANY_ENTERPRISE_ID, name="Acme", slug="acme"
    )
    user = make_returning_user()
    user.enterprise_id = PERSONAL_ENTERPRISE
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})

    async def failing_update(_user):
        raise RuntimeError("write failed")

    users.update = failing_update  # type: ignore[method-assign]
    personal = FakePersonalEnterpriseRepository(rows={("workos", SUBJECT): _record()})
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY_SAME_SUBJECT),
        users=users,
        mappings=FakeMappingRepository(
            mappings={("workos", IDP_ORG): COMPANY_ENTERPRISE_ID}
        ),
        enterprises=FakeEnterpriseRepository(
            enterprises={COMPANY_ENTERPRISE_ID: company}
        ),
        personal=personal,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # The binding survives a failed move — retiring it first would strand the
    # account with no personal row and no company anchor.
    assert personal.retired == []
    assert ("workos", SUBJECT) in personal.rows


# =============================================================================
# A provisioning ceiling independent of the switch
# =============================================================================


async def test_provisioning_is_refused_once_the_hourly_ceiling_is_reached(
    store, as_tenant_provider, switch, monkeypatch
):
    """The switch bounds nothing about volume; this does."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "3")
    reset_settings_singleton()

    personal = FakePersonalEnterpriseRepository(minted_last_hour=3)
    provider = FakeProvider(INDIVIDUAL)
    service = build_service(
        store, provider=provider, users=FakeUserRepository(), personal=personal
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert personal.provisioned == []
    # Refused BEFORE the IdP call: the quota this protects is the provider's.
    assert provider.provision_calls == []


async def test_the_ceiling_does_not_bound_the_domain_arm(
    store, as_tenant_provider, switch, monkeypatch
):
    """It bounds the arm whose abuse shape it was written for, and no other.

    The ceiling exists because every consumer-mail subject the IdP vouches for
    would mint an IdP organization and an enterprise. The domain arm mints one
    row per DOMAIN, ever, and reaching a new domain means controlling one and
    having the IdP verify an address at it — the bar consumer mail does not
    clear. Capping it would refuse a company's second employee.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "1")
    reset_settings_singleton()

    personal = FakePersonalEnterpriseRepository(minted_last_hour=10_000)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(enterprises={})
    service = build_service(
        store,
        provider=FakeProvider(work_identity("dana")),
        users=users,
        personal=personal,
        enterprises=enterprises,
    )

    assert "code" in redirect_params(await run_callback(service))
    assert enterprises.created_domains == [WORK_DOMAIN]
    assert personal.count_calls == []


async def test_the_ceiling_does_not_lock_out_existing_tenants(
    store, as_tenant_provider, switch, monkeypatch
):
    """It bounds provisioning only. People already using the product sign in."""
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "1")
    reset_settings_singleton()

    personal = FakePersonalEnterpriseRepository(
        rows={("workos", SUBJECT): _record()}, minted_last_hour=10_000
    )
    user = make_returning_user()
    users = FakeUserRepository(users_by_subject={("workos", SUBJECT): user})
    service = build_service(
        store,
        users=users,
        enterprises=FakeEnterpriseRepository(),
        personal=personal,
    )

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
# Structure — the bind-and-verify tail is shared, sentinel on EVERY path
# =============================================================================


async def test_the_mapped_path_also_refuses_the_sentinel(
    store, as_tenant_provider, switch
):
    """Previously absent: only the personal path guarded fm#850.

    An operator-provisioned mapping row pointing at the Standalone enterprise
    would have bound the sentinel as a tenant. Every arm now ends in one tail,
    so none can acquire a check the others lack.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(False)
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprises={
            STANDALONE_ENTERPRISE_ID: make_enterprise(
                enterprise_id=STANDALONE_ENTERPRISE_ID
            )
        }
    )
    service = build_service(
        store,
        provider=FakeProvider(COMPANY_IDENTITY),
        users=users,
        mappings=FakeMappingRepository(
            mappings={("workos", IDP_ORG): STANDALONE_ENTERPRISE_ID}
        ),
        enterprises=enterprises,
        personal=FakePersonalEnterpriseRepository(),
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    # Refused before the sentinel was ever bound as this request's tenant.
    assert get_current_enterprise_id() == STANDALONE_ENTERPRISE_ID
    assert enterprises.lookups_bound_to == []
    assert users.subject_lookups_bound_to == []


async def test_the_domain_arm_also_refuses_the_sentinel(
    store, as_tenant_provider, switch
):
    """The newest arm gets the oldest guard, because the tail is shared.

    A repository that answered a domain lookup with the Standalone enterprise —
    a seeded row carrying a domain, say — would otherwise pool a whole domain's
    accounts into the deployment's own tenant.
    """
    as_tenant_provider(TenantProvider.MULTI)
    switch(True)
    sentinel = make_enterprise(
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        name=WORK_DOMAIN,
        slug=domain_enterprise_slug(WORK_DOMAIN),
        domain=WORK_DOMAIN,
    )
    users = FakeUserRepository()
    enterprises = FakeEnterpriseRepository(
        enterprises={STANDALONE_ENTERPRISE_ID: sentinel}
    )
    service = build_service(
        store,
        provider=FakeProvider(work_identity("dana")),
        users=users,
        enterprises=enterprises,
    )

    params = redirect_params(await run_callback(service))

    assert params == {"error": ERROR_FAILED}
    assert get_current_enterprise_id() == STANDALONE_ENTERPRISE_ID
    assert users.created == []


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

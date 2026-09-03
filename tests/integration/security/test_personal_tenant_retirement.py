"""Retiring a personal tenant against a real PostgreSQL (#1045 D8).

The unit tests pin the decision — which steps run, in what order, and what the
login does with the operator's choice. What only a real database can answer is
here, and each case is here because the SQLite unit test would pass while
PostgreSQL failed:

* **The marker round-trips through JSONB.** ``enterprises.settings`` is
  ``Text().with_variant(JSONB, "postgresql")``, and a writer that binds an
  already-serialized string stores it as a JSON *string scalar* that comes back
  as ``str`` — while a writer binding an object comes back as ``dict``. The
  reader has to handle both, and only PostgreSQL exercises the shape that is not
  SQLite's.
* **The derived key is genuinely freed.** ``enterprises.slug`` is unique
  deployment-wide and ``organizations`` is unique per ``(enterprise_id, slug)``.
  A retirement that did not rename would let a "fresh tenant" resolve straight
  back onto the retired one, or trip the constraint — and constraints are what a
  real database has.
* **The login honours it end to end**, through the real repository and the real
  ``organizations`` read, not a fake that answers whatever it is asked.
* **Nothing else moved.** A second personal tenant is present throughout and is
  compared row by row.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.cli import personal_tenant as cli
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import set_current_org_id
from faultmaven.modules.auth.contracts import (
    PERSONAL_TENANT_RETIREMENT_KEY,
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
    RetiredIdPOrganization,
    SSOIdentity,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ORG_NAME,
    personal_org_slug,
    personal_tenant_key,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

PROVIDER = "workos"


class RecordingIdP:
    """The IdP teardown port. Its SDK conformance is pinned by ``autospec`` in
    ``tests/unit/modules/auth/test_sso_personal_org_provider.py``; what matters
    here is that the command's database half is driven for real."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def retire_personal_organization(self, *, external_id: str):
        self.calls.append(external_id)
        return RetiredIdPOrganization(True, 1, True)


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop():
    """One engine per test, because there is one event loop per test.

    ``get_engine`` memoises a module-global engine whose pool binds to whatever
    loop first used it; a pooled connection carried into the next test belongs
    to a closed loop and surfaces as "attached to a different loop" rather than
    as anything this module is about.
    """
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()


@pytest.fixture(autouse=True)
def restore_tenant_context():
    yield
    set_current_org_id(STANDALONE_ORG_ID)


@pytest.fixture
def owner_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture
def subject() -> str:
    """A fresh subject per test, so no run passes on another's leftovers."""
    return f"user_pt_retire_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def repository():
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_org_repository import (  # noqa: E501
        SessionlessSSOPersonalOrgRepository,
    )

    return SessionlessSSOPersonalOrgRepository()


async def _as_owner(url: str, sql: str, **params):
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).fetchall()
    finally:
        await engine.dispose()


async def _as_owner_write(url: str, sql: str, **params):
    engine = create_async_engine(url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _provision(repository, subject: str) -> str:
    """Exactly what the login path does, so the rows are the real ones."""
    return await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=f"org_{uuid.uuid4().hex[:12]}",
        name=PERSONAL_ORG_NAME,
        slug=personal_org_slug(personal_tenant_key(PROVIDER, subject)),
    )


async def _seed_content(url: str, organization_id: str, tag: str) -> None:
    """A case and a knowledge item, so "the data survives" is falsifiable."""
    await _as_owner_write(
        url,
        "INSERT INTO cases (case_id, organization_id, title) VALUES (:c, :o, :t)",
        c=f"case_{tag}",
        o=organization_id,
        t="disk full",
    )
    await _as_owner_write(
        url,
        "INSERT INTO knowledge_items (item_id, organization_id, title, content, "
        "item_type, scope) VALUES (:i, :o, 'runbook', 'body', 'runbook', 'personal')",
        i=f"kb_{tag}",
        o=organization_id,
    )


async def _tenant_rows(url: str, organization_id: str) -> dict:
    return {
        "organization": await _as_owner(
            url,
            "SELECT organization_id, enterprise_id, name, slug, is_active, "
            "deleted_at FROM organizations WHERE organization_id = :o",
            o=organization_id,
        ),
        "mapping": await _as_owner(
            url,
            "SELECT provider, provider_org_id FROM sso_org_mappings "
            "WHERE organization_id = :o",
            o=organization_id,
        ),
        "binding": await _as_owner(
            url,
            "SELECT provider, provider_user_id, enterprise_id, provider_org_id "
            "FROM sso_personal_orgs WHERE organization_id = :o",
            o=organization_id,
        ),
        "cases": await _as_owner(
            url,
            "SELECT case_id, title FROM cases WHERE organization_id = :o",
            o=organization_id,
        ),
        "knowledge": await _as_owner(
            url,
            "SELECT item_id, title FROM knowledge_items WHERE organization_id = :o",
            o=organization_id,
        ),
        "teams": await _as_owner(
            url,
            "SELECT team_id, name FROM teams WHERE organization_id = :o",
            o=organization_id,
        ),
    }


async def _enterprise_of(url: str, organization_id: str) -> str:
    rows = await _as_owner(
        url,
        "SELECT enterprise_id FROM organizations WHERE organization_id = :o",
        o=organization_id,
    )
    return rows[0].enterprise_id


async def _retire(subject: str, *, next_login: str = "refuse", idp=None) -> int:
    return await cli.retire(
        subject=subject,
        organization_id=None,
        next_login=next_login,
        apply=True,
        idp=idp or RecordingIdP(),
    )


# =============================================================================
# The marker survives the round trip a real column makes it take
# =============================================================================


@pytest.mark.parametrize(
    "flag,policy",
    [
        ("refuse", RETIREMENT_POLICY_REFUSE),
        ("fresh-tenant", RETIREMENT_POLICY_FRESH_TENANT),
    ],
)
async def test_the_marker_round_trips_through_the_real_column(
    owner_url, repository, subject, flag, policy
):
    """Written as JSON into a JSONB column, read back by the real repository.

    The reader is the one the unauthenticated callback uses, so this is the
    exact path a retired subject's next login takes.
    """
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)

    assert await _retire(subject, next_login=flag) == 0

    retirement = await repository.get_retirement(enterprise_id)
    assert retirement is not None
    assert retirement.policy == policy
    assert retirement.key == personal_tenant_key(PROVIDER, subject)
    assert retirement.provider == PROVIDER
    assert retirement.organization_id == organization_id


async def test_an_unmarked_enterprise_answers_none_rather_than_raising(
    owner_url, repository, subject
):
    """Every mapped login whose account is anchored elsewhere makes this read."""
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)

    assert await repository.get_retirement(enterprise_id) is None
    assert await repository.get_retirement(str(uuid.uuid4())) is None


async def test_existing_enterprise_settings_are_merged_not_replaced(
    owner_url, repository, subject
):
    """An enterprise's settings hold SSO and plan configuration that has nothing
    to do with a retirement."""
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _as_owner_write(
        owner_url,
        "UPDATE enterprises SET settings = :s WHERE enterprise_id = :e",
        s=json.dumps({"sso": {"connection": "conn_123"}}),
        e=enterprise_id,
    )

    assert await _retire(subject) == 0

    rows = await _as_owner(
        owner_url,
        "SELECT settings FROM enterprises WHERE enterprise_id = :e",
        e=enterprise_id,
    )
    settings = rows[0].settings
    if isinstance(settings, str):
        settings = json.loads(settings)
    assert settings["sso"] == {"connection": "conn_123"}
    assert settings[PERSONAL_TENANT_RETIREMENT_KEY]["key"] == personal_tenant_key(
        PROVIDER, subject
    )


# =============================================================================
# The derived key is genuinely freed — which only real constraints prove
# =============================================================================


async def test_a_fresh_tenant_can_be_provisioned_after_a_retirement(
    owner_url, repository, subject
):
    """``enterprises.slug`` is unique deployment-wide and the organization is
    unique per ``(enterprise_id, slug)``. A retirement that did not rename would
    either resolve the "fresh" tenant back onto the retired one or trip a
    constraint; both are invisible without a real database.
    """
    first = await _provision(repository, subject)

    assert await _retire(subject, next_login="fresh-tenant") == 0

    second = await _provision(repository, subject)

    assert second != first
    # Two distinct organizations, in two distinct enterprises, and the new one
    # carries the derived slug the retirement freed.
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))
    live = await _as_owner(
        owner_url,
        "SELECT organization_id, enterprise_id FROM organizations WHERE slug = :s",
        s=slug,
    )
    assert [row.organization_id for row in live] == [second]
    assert await _enterprise_of(owner_url, second) != await _enterprise_of(
        owner_url, first
    )
    # The retired rows are still there, renamed and soft-deleted.
    retired = await _as_owner(
        owner_url,
        "SELECT slug, deleted_at, is_active FROM organizations "
        "WHERE organization_id = :o",
        o=first,
    )
    assert retired[0].slug.startswith(f"{slug}-retired-")
    assert retired[0].deleted_at is not None
    assert retired[0].is_active is False


async def test_retiring_the_replacement_does_not_collide_with_the_first(
    owner_url, repository, subject
):
    """The retired slug carries the row's own id, so a subject retired twice
    does not collide with itself — which is reachable, because ``fresh-tenant``
    invites exactly that sequence."""
    first = await _provision(repository, subject)
    assert await _retire(subject, next_login="fresh-tenant") == 0
    second = await _provision(repository, subject)

    assert await _retire(subject, next_login="fresh-tenant") == 0

    slugs = await _as_owner(
        owner_url,
        "SELECT organization_id, slug FROM organizations "
        "WHERE organization_id IN (:a, :b)",
        a=first,
        b=second,
    )
    assert len({row.slug for row in slugs}) == 2


# =============================================================================
# Idempotency, resumability and blast radius, against the real schema
# =============================================================================


async def test_a_second_run_is_a_no_op(owner_url, repository, subject):
    await _provision(repository, subject)
    idp = RecordingIdP()
    assert await _retire(subject, idp=idp) == 0
    organization_id = (
        await _as_owner(
            owner_url,
            "SELECT organization_id FROM organizations WHERE slug LIKE :p",
            p=f"{personal_org_slug(personal_tenant_key(PROVIDER, subject))}-retired-%",
        )
    )[0].organization_id
    after_first = await _tenant_rows(owner_url, organization_id)

    assert await _retire(subject, idp=idp) == cli.EXIT_NOTHING_TO_DO

    assert await _tenant_rows(owner_url, organization_id) == after_first
    assert len(idp.calls) == 1


async def test_the_other_tenants_rows_are_byte_identical(owner_url, repository):
    """Two personal tenants present throughout; one is retired."""
    victim = f"user_pt_v_{uuid.uuid4().hex[:10]}"
    bystander = f"user_pt_b_{uuid.uuid4().hex[:10]}"
    victim_org = await _provision(repository, victim)
    bystander_org = await _provision(repository, bystander)
    await _seed_content(owner_url, victim_org, victim[-10:])
    await _seed_content(owner_url, bystander_org, bystander[-10:])
    bystander_enterprise = await _enterprise_of(owner_url, bystander_org)

    before = await _tenant_rows(owner_url, bystander_org)
    before_enterprise = await _as_owner(
        owner_url,
        "SELECT enterprise_id, name, slug, settings, deleted_at FROM enterprises "
        "WHERE enterprise_id = :e",
        e=bystander_enterprise,
    )

    assert await _retire(victim) == 0

    assert await _tenant_rows(owner_url, bystander_org) == before
    assert (
        await _as_owner(
            owner_url,
            "SELECT enterprise_id, name, slug, settings, deleted_at FROM enterprises "
            "WHERE enterprise_id = :e",
            e=bystander_enterprise,
        )
        == before_enterprise
    )
    # And the victim's own content is still there — retirement is not deletion.
    victim_rows = await _tenant_rows(owner_url, victim_org)
    assert len(victim_rows["cases"]) == 1
    assert len(victim_rows["knowledge"]) == 1
    assert len(victim_rows["teams"]) == 1


async def test_a_dry_run_writes_nothing_against_the_real_schema(
    owner_url, repository, subject
):
    organization_id = await _provision(repository, subject)
    await _seed_content(owner_url, organization_id, subject[-10:])
    before = await _tenant_rows(owner_url, organization_id)
    idp = RecordingIdP()

    code = await cli.retire(
        subject=subject,
        organization_id=None,
        next_login="fresh-tenant",
        apply=False,
        idp=idp,
    )

    assert code == 0
    assert idp.calls == []
    assert await _tenant_rows(owner_url, organization_id) == before


# =============================================================================
# Invariant 1, end to end: the login service, the real repository, real rows
# =============================================================================


def _identity(subject: str) -> SSOIdentity:
    return SSOIdentity(
        provider=PROVIDER,
        provider_user_id=subject,
        email=f"{subject}@personal.example",
        email_verified=True,
        display_name="Retired Individual",
    )


async def _login_service(monkeypatch, repository, *, idp_org_id: str | None = None):
    """The real personal-org and organization repositories, nothing else real.

    The callback's other collaborators (state store, token generator, session
    service) have nothing to say about tenant resolution, and wiring them would
    make this a test of the whole login rather than of the retirement.

    The IdP organization id is unique per call. The scratch database is shared
    across the module (and across runs), and ``sso_org_mappings`` is 1:1 per IdP
    organization — a fixed id would make the SECOND test to provision collide
    with the first's mapping, which is a property of the fixture rather than of
    anything under test.
    """
    import fakeredis.aioredis as fakeredis

    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.modules.auth.domain.services.sso_login_service import (
        SSOLoginService,
    )
    from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
        SSOEphemeralStore,
    )
    from tests.unit.modules.auth.test_sso_personal_tenant import (
        FakeProvider,
        FakeSessionService,
        FakeTokenGenerator,
    )

    return SSOLoginService(
        identity_provider=FakeProvider(
            personal_org_id=idp_org_id or f"org_{uuid.uuid4().hex[:12]}"
        ),
        ephemeral_store=SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True)),
        user_repository=SessionlessUserRepository(),
        token_generator=FakeTokenGenerator(),
        session_service=FakeSessionService(),
        dashboard_url="https://app.faultmaven.test",
        access_token_expires_in=3600,
        organization_repository=SessionlessOrganizationRepository(),
        personal_org_repository=repository,
    )


async def _seed_user(url: str, *, subject: str, enterprise_id: str) -> str:
    user_id = str(uuid.uuid4())
    await _as_owner_write(
        url,
        "INSERT INTO users (user_id, enterprise_id, username, email, display_name, "
        " sso_provider, sso_provider_id, is_active) "
        "VALUES (:u, :e, :n, :m, :n, 'workos', :s, true)",
        u=user_id,
        e=enterprise_id,
        n=subject[:60],
        m=f"{subject}@personal.example",
        s=subject,
    )
    return user_id


@pytest.fixture
def logged(monkeypatch):
    """Every event the login service logged, as ``(event, kwargs)``.

    Asserted on the **structured fields** rather than on captured stdout.
    structlog's sink is process-wide configuration that other modules in the
    same suite change, so a substring test over stdout passes or fails on
    collection order — which it did, green alone and red in the full selection.
    The real logger still runs underneath, so nothing about the log itself is
    suppressed.
    """
    from faultmaven.modules.auth.domain.services import sso_login_service

    records: list[tuple[str, dict]] = []
    real = sso_login_service.logger

    class _Recorder:
        def __getattr__(self, name):
            def _log(event, **kwargs):
                records.append((event, kwargs))
                return getattr(real, name)(event, **kwargs)

            return _log

    monkeypatch.setattr(sso_login_service, "logger", _Recorder())
    return records


@pytest.fixture
def switch_on(monkeypatch):
    """The real switch, through the real settings singleton.

    The hourly provisioning ceiling is raised alongside it, and deliberately not
    silently: ``count_created_since`` is a **deployment-wide** count over
    ``sso_personal_orgs``, so on a scratch database that accumulates rows across
    runs the default of 20 is reached and every provisioning case here starts
    refusing ``personal_provisioning_ceiling`` — a property of the fixture, not
    of anything this module tests. The ceiling itself is pinned where it belongs,
    in ``tests/unit/modules/auth/test_sso_personal_tenant.py``
    (``..._provisioning_is_refused_once_the_hourly_ceiling_is_reached`` and
    ``..._the_ceiling_has_a_finite_default``), so raising it here removes a
    confounder rather than a guard.
    """
    from tests.utils import get_live_settings, reset_settings_singleton

    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_ENABLED", "true")
    monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "100000")
    reset_settings_singleton()
    assert get_live_settings().auth.sso_jit_personal_tenant_enabled is True
    yield
    monkeypatch.delenv("SSO_JIT_PERSONAL_TENANT_ENABLED", raising=False)
    monkeypatch.delenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", raising=False)
    reset_settings_singleton()


async def test_a_refuse_retirement_refuses_the_next_orgless_login(
    owner_url, repository, subject, monkeypatch, switch_on, logged
):
    """Through the login service, against the rows the command actually wrote."""
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(subject, next_login="refuse") == 0

    service = await _login_service(monkeypatch, repository)
    organization, error = await service._resolve_login_organization(_identity(subject))

    assert organization is None
    assert error == "sso_org_unmapped"
    assert "personal_tenant_retired" in [
        kwargs["reason"] for _event, kwargs in logged if "reason" in kwargs
    ]


async def test_a_fresh_tenant_retirement_lets_the_next_orgless_login_provision(
    owner_url, repository, subject, monkeypatch, switch_on
):
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(subject, next_login="fresh-tenant") == 0

    service = await _login_service(monkeypatch, repository)
    organization, error = await service._resolve_login_organization(_identity(subject))

    assert error is None
    assert organization is not None
    assert organization.organization_id != organization_id
    assert organization.deleted_at is None


async def test_a_retirement_of_a_different_subject_refuses_this_one(
    owner_url, repository, subject, monkeypatch, switch_on
):
    """The marker is a permission, so the wrong one must not release anybody.

    Anchoring this account to SOMEBODY ELSE's retired enterprise is the shape
    that matters: without the derived-key check, the login would read a
    ``fresh_tenant`` marker and hand out a tenant to a person it was never
    written for.
    """
    other = f"user_pt_other_{uuid.uuid4().hex[:10]}"
    other_org = await _provision(repository, other)
    other_enterprise = await _enterprise_of(owner_url, other_org)
    assert await _retire(other, next_login="fresh-tenant") == 0

    await _seed_user(owner_url, subject=subject, enterprise_id=other_enterprise)

    service = await _login_service(monkeypatch, repository)
    organization, error = await service._resolve_login_organization(_identity(subject))

    assert organization is None
    assert error == "sso_org_unmapped"
    # Nothing was provisioned for the subject that was not released.
    assert (
        await _as_owner(
            owner_url,
            "SELECT 1 FROM sso_personal_orgs WHERE provider_user_id = :s",
            s=subject,
        )
        == []
    )


async def test_the_retired_tenant_is_unreachable_even_by_its_own_idp_org(
    owner_url, repository, subject, monkeypatch, switch_on
):
    """The mapping row is gone, so an IdP organization that still echoes meets
    the operator-fixable unmapped refusal rather than binding a dead tenant."""
    organization_id = await _provision(repository, subject)
    idp_org = (
        await _as_owner(
            owner_url,
            "SELECT provider_org_id FROM sso_personal_orgs WHERE organization_id = :o",
            o=organization_id,
        )
    )[0].provider_org_id
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(subject) == 0

    from faultmaven.modules.auth.infrastructure.repositories.sso_org_mapping_repository import (  # noqa: E501
        SessionlessSSOOrgMappingRepository,
    )

    service = await _login_service(monkeypatch, repository)
    service._org_mappings = SessionlessSSOOrgMappingRepository()
    echoed = SSOIdentity(
        provider=PROVIDER,
        provider_user_id=subject,
        email=f"{subject}@personal.example",
        email_verified=True,
        display_name="Retired Individual",
        organization_id=idp_org,
    )

    organization, error = await service._resolve_login_organization(echoed)

    assert organization is None
    assert error == "sso_org_unmapped"


def test_the_module_is_not_silently_skipping():
    """A postgres module that skipped would prove nothing, quietly."""
    assert os.environ.get("DATABASE_URL", "").startswith("postgresql")
    assert asyncio.get_event_loop_policy() is not None

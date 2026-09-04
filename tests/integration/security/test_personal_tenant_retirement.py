"""Retiring a personal tenant against a real PostgreSQL (#1045 D8).

The unit tests pin the decision — which steps run, in what order, and what the
login does with the operator's choice. What only a real database can answer is
here, and each case is here because a SQLite unit test would pass while
PostgreSQL failed, or because the fact under test *is* a constraint:

* **The derived slug is genuinely reusable.** A retired tenant keeps its slug,
  and the subject's next tenant derives exactly the same one. That works only
  because migration 052 made both uniqueness rules partial on
  ``deleted_at IS NULL`` — the previous design renamed the slug instead, and the
  rename is what produced an ambiguous ``LIKE`` lookup once a subject had two
  retired tenants. Here the constraints are real.
* **The typed retirement state round-trips**, and the login reads it through the
  real repositories rather than a fake that answers whatever it is asked.
* **A refresh chain cannot outlive the tenant** — the guard reads a real
  soft-deleted organization row.
* **Nothing else moved.** A second personal tenant is present throughout and is
  compared row by row.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.cli import personal_tenant as cli
from faultmaven.modules.auth.contracts import (
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
    SSOIdentity,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    PERSONAL_ORG_NAME,
    personal_org_slug,
    personal_tenant_key,
)
from tests.conftest import RecordingIdP, RecordingRevoker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.usefixtures("restore_tenant_context"),
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

PROVIDER = "workos"


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop():
    """One engine per test, because there is one event loop per test."""
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()


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


async def _provision(repository, subject: str, *, provider_org_id=None) -> str:
    """Exactly what the login path does, so the rows are the real ones."""
    return await repository.provision(
        provider=PROVIDER,
        provider_user_id=subject,
        provider_org_id=provider_org_id or f"org_{uuid.uuid4().hex[:12]}",
        name=PERSONAL_ORG_NAME,
        slug=personal_org_slug(personal_tenant_key(PROVIDER, subject)),
    )


async def _idp_org_of(url: str, organization_id: str) -> str:
    rows = await _as_owner(
        url,
        "SELECT provider_org_id FROM sso_org_mappings WHERE organization_id = :o",
        o=organization_id,
    )
    return rows[0].provider_org_id


async def _enterprise_of(url: str, organization_id: str) -> str:
    rows = await _as_owner(
        url,
        "SELECT enterprise_id FROM organizations WHERE organization_id = :o",
        o=organization_id,
    )
    return rows[0].enterprise_id


async def _seed_user(url: str, *, subject: str, enterprise_id: str | None) -> str:
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


async def _seed_content(url: str, organization_id: str, tag: str) -> None:
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


async def _retire(owner_url, organization_id, *, policy="refuse", idp=None):
    return await cli.retire(
        subject=None,
        organization_id=organization_id,
        next_login=policy,
        apply=True,
        idp=idp or RecordingIdP([await _idp_org_of(owner_url, organization_id)]),
        auth_service=RecordingRevoker(),
    )


# =============================================================================
# The typed state round-trips, and the constraints are what make it work
# =============================================================================


@pytest.mark.parametrize(
    "flag,policy",
    [
        ("refuse", RETIREMENT_POLICY_REFUSE),
        ("fresh-tenant", RETIREMENT_POLICY_FRESH_TENANT),
    ],
)
async def test_the_retirement_is_two_typed_columns(
    owner_url, repository, subject, flag, policy
):
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)

    assert await _retire(owner_url, organization_id, policy=flag) == 0

    row = (
        await _as_owner(
            owner_url,
            "SELECT deleted_at, personal_tenant_retirement, slug FROM enterprises "
            "WHERE enterprise_id = :e",
            e=enterprise_id,
        )
    )[0]
    assert row.deleted_at is not None
    assert row.personal_tenant_retirement == policy
    # No rename: the retired enterprise keeps the slug derived from the subject.
    assert row.slug == personal_org_slug(personal_tenant_key(PROVIDER, subject))


async def test_the_policy_column_refuses_a_value_the_code_has_no_branch_for(
    owner_url, repository, subject
):
    """A CHECK constraint, not a convention: a hand-written UPDATE must not be
    able to invent a third policy the login cannot interpret."""
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)

    with pytest.raises(Exception) as exc:
        await _as_owner_write(
            owner_url,
            "UPDATE enterprises SET personal_tenant_retirement = 'delete_everything' "
            "WHERE enterprise_id = :e",
            e=enterprise_id,
        )
    assert "personal_tenant_retirement" in str(exc.value)


async def test_a_fresh_tenant_reuses_the_derived_slug_the_retired_one_keeps(
    owner_url, repository, subject
):
    """Only a real database can answer this: it is a uniqueness constraint.

    ``enterprises.slug`` is unique deployment-wide and ``organizations`` unique
    per ``(enterprise_id, slug)`` — but both partial on ``deleted_at IS NULL``
    since migration 052. Without the partial predicate this insert fails and the
    previous design's slug rename is forced back.
    """
    first = await _provision(repository, subject)
    first_ent = await _enterprise_of(owner_url, first)
    await _seed_user(owner_url, subject=subject, enterprise_id=first_ent)

    assert await _retire(owner_url, first, policy="fresh-tenant") == 0

    second = await _provision(repository, subject)

    assert second != first
    slug = personal_org_slug(personal_tenant_key(PROVIDER, subject))
    live = await _as_owner(
        owner_url,
        "SELECT organization_id FROM organizations WHERE slug = :s "
        "AND deleted_at IS NULL",
        s=slug,
    )
    assert [row.organization_id for row in live] == [second]
    # Both tenants carry the same slug; only their liveness differs.
    both = await _as_owner(
        owner_url,
        "SELECT organization_id, slug FROM organizations "
        "WHERE organization_id IN (:a, :b)",
        a=first,
        b=second,
    )
    assert {row.slug for row in both} == {slug}


async def test_two_retired_tenants_of_one_subject_are_both_addressable(
    owner_url, repository, subject
):
    """The ambiguity the rename produced, made impossible by addressing by id.

    Retire, provision again, retire again: two retired tenants share the derived
    slug, and each is still reachable by its own organization id.
    """
    first = await _provision(repository, subject)
    await _seed_user(
        owner_url, subject=subject, enterprise_id=await _enterprise_of(owner_url, first)
    )
    assert await _retire(owner_url, first, policy="fresh-tenant") == 0
    second = await _provision(repository, subject)
    assert await _retire(owner_url, second, policy="fresh-tenant") == 0

    for organization_id in (first, second):
        rows = await _as_owner(
            owner_url,
            "SELECT deleted_at FROM organizations WHERE organization_id = :o",
            o=organization_id,
        )
        assert rows[0].deleted_at is not None


# =============================================================================
# Blast radius and survival
# =============================================================================


async def test_the_other_tenants_rows_are_byte_identical(owner_url, repository):
    victim = f"user_pt_v_{uuid.uuid4().hex[:10]}"
    bystander = f"user_pt_b_{uuid.uuid4().hex[:10]}"
    victim_org = await _provision(repository, victim)
    bystander_org = await _provision(repository, bystander)
    await _seed_content(owner_url, victim_org, victim[-10:])
    await _seed_content(owner_url, bystander_org, bystander[-10:])
    bystander_ent = await _enterprise_of(owner_url, bystander_org)
    await _seed_user(owner_url, subject=bystander, enterprise_id=bystander_ent)
    await _seed_user(
        owner_url,
        subject=victim,
        enterprise_id=await _enterprise_of(owner_url, victim_org),
    )

    before = await _tenant_rows(owner_url, bystander_org)
    before_ent = await _as_owner(
        owner_url,
        "SELECT enterprise_id, slug, deleted_at, personal_tenant_retirement "
        "FROM enterprises WHERE enterprise_id = :e",
        e=bystander_ent,
    )
    before_users = await _as_owner(
        owner_url,
        "SELECT user_id, enterprise_id FROM users WHERE enterprise_id = :e",
        e=bystander_ent,
    )

    assert await _retire(owner_url, victim_org, policy="fresh-tenant") == 0

    assert await _tenant_rows(owner_url, bystander_org) == before
    assert (
        await _as_owner(
            owner_url,
            "SELECT enterprise_id, slug, deleted_at, personal_tenant_retirement "
            "FROM enterprises WHERE enterprise_id = :e",
            e=bystander_ent,
        )
        == before_ent
    )
    assert (
        await _as_owner(
            owner_url,
            "SELECT user_id, enterprise_id FROM users WHERE enterprise_id = :e",
            e=bystander_ent,
        )
        == before_users
    )
    victim_rows = await _tenant_rows(owner_url, victim_org)
    assert len(victim_rows["cases"]) == 1
    assert len(victim_rows["knowledge"]) == 1
    assert len(victim_rows["teams"]) == 1


async def test_a_dry_run_writes_nothing_against_the_real_schema(
    owner_url, repository, subject
):
    organization_id = await _provision(repository, subject)
    await _seed_user(
        owner_url,
        subject=subject,
        enterprise_id=await _enterprise_of(owner_url, organization_id),
    )
    await _seed_content(owner_url, organization_id, subject[-10:])
    before = await _tenant_rows(owner_url, organization_id)
    idp = RecordingIdP([await _idp_org_of(owner_url, organization_id)])

    code = await cli.retire(
        subject=None,
        organization_id=organization_id,
        next_login="fresh-tenant",
        apply=False,
        idp=idp,
        auth_service=RecordingRevoker(),
    )

    assert code == 0
    assert idp.calls == []
    assert await _tenant_rows(owner_url, organization_id) == before


# =============================================================================
# The login, through the real repositories and real rows
# =============================================================================


def _identity(subject: str, organization_id: str | None = None) -> SSOIdentity:
    return SSOIdentity(
        provider=PROVIDER,
        provider_user_id=subject,
        email=f"{subject}@personal.example",
        email_verified=True,
        display_name="Retired Individual",
        organization_id=organization_id,
    )


async def _login_service(repository):
    """The real personal-org and organization repositories, nothing else real."""
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
        identity_provider=FakeProvider(personal_org_id=f"org_{uuid.uuid4().hex[:12]}"),
        ephemeral_store=SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True)),
        user_repository=SessionlessUserRepository(),
        token_generator=FakeTokenGenerator(),
        session_service=FakeSessionService(),
        dashboard_url="https://app.faultmaven.test",
        access_token_expires_in=3600,
        organization_repository=SessionlessOrganizationRepository(),
        personal_org_repository=repository,
    )


@pytest.fixture
def switch_on(monkeypatch):
    """The real switch, through the real settings singleton.

    The hourly ceiling is raised alongside it, and deliberately not silently:
    ``count_created_since`` is a deployment-wide count over ``sso_personal_orgs``,
    so on a scratch database that accumulates rows every provisioning case here
    would refuse ``personal_provisioning_ceiling`` — a property of the fixture,
    not of anything under test. The ceiling is pinned where it belongs, in
    ``tests/unit/modules/auth/test_sso_personal_tenant.py``.
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
    owner_url, repository, subject, switch_on, capsys
):
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(owner_url, organization_id, policy="refuse") == 0

    service = await _login_service(repository)
    organization, error = await service._resolve_login_organization(_identity(subject))

    assert organization is None
    assert error == "sso_org_unmapped"
    assert "personal_tenant_retired" in capsys.readouterr().out


async def test_a_fresh_tenant_retirement_lets_the_next_orgless_login_provision(
    owner_url, repository, subject, switch_on
):
    organization_id = await _provision(repository, subject)
    enterprise_id = await _enterprise_of(owner_url, organization_id)
    await _seed_user(owner_url, subject=subject, enterprise_id=enterprise_id)
    assert await _retire(owner_url, organization_id, policy="fresh-tenant") == 0

    # The anchor really is NULL — the state that releases provisioning.
    assert (
        await _as_owner(
            owner_url,
            "SELECT enterprise_id FROM users WHERE sso_provider_id = :s",
            s=subject,
        )
    )[0].enterprise_id is None

    service = await _login_service(repository)
    organization, error = await service._resolve_login_organization(_identity(subject))

    assert error is None
    assert organization is not None
    assert organization.organization_id != organization_id
    assert organization.deleted_at is None


async def test_the_retired_tenant_is_unreachable_even_by_its_own_idp_org(
    owner_url, repository, subject, switch_on
):
    """The mapping row is gone, so an IdP organization that still echoes meets
    the operator-fixable unmapped refusal rather than binding a dead tenant."""
    organization_id = await _provision(repository, subject)
    idp_org = await _idp_org_of(owner_url, organization_id)
    await _seed_user(
        owner_url,
        subject=subject,
        enterprise_id=await _enterprise_of(owner_url, organization_id),
    )
    assert await _retire(owner_url, organization_id) == 0

    from faultmaven.modules.auth.infrastructure.repositories.sso_org_mapping_repository import (  # noqa: E501
        SessionlessSSOOrgMappingRepository,
    )

    service = await _login_service(repository)
    service._org_mappings = SessionlessSSOOrgMappingRepository()

    organization, error = await service._resolve_login_organization(
        _identity(subject, idp_org)
    )

    assert organization is None
    assert error == "sso_org_unmapped"


# =============================================================================
# R5 — a refresh chain cannot outlive the tenant
# =============================================================================


async def test_a_refresh_is_refused_for_a_retired_tenant(
    owner_url, repository, subject
):
    """The second leg of the fence, read off a real soft-deleted row.

    The revocation watermark stops the chain that exists at retirement time;
    this stops any chain that presents the retired tenant's organization claim
    afterwards. The **request** path is out of scope here — this pins the
    predicate both refresh surfaces call.
    """
    from faultmaven.infrastructure.persistence.organization_liveness import (
        organization_id_is_usable,
    )

    organization_id = await _provision(repository, subject)
    await _seed_user(
        owner_url,
        subject=subject,
        enterprise_id=await _enterprise_of(owner_url, organization_id),
    )
    assert await organization_id_is_usable(organization_id) is True

    assert await _retire(owner_url, organization_id) == 0

    assert await organization_id_is_usable(organization_id) is False
    # An absent claim is a different condition with its own handling, and must
    # not be turned into a refusal here.
    assert await organization_id_is_usable(None) is True


def test_the_module_is_not_silently_skipping():
    assert os.environ.get("DATABASE_URL", "").startswith("postgresql")

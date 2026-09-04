"""The turn cap against a real PostgreSQL under RLS (ADR-016 D5.3).

The unit module pins the *decision* through fakes of the two ports. This module
pins the three things only a real database can answer:

* the ledger can be **written by the application role**, whose RLS policy
  applies ``USING`` as ``WITH CHECK`` (migration 018's pattern, which migration
  052 enrols the new table into) — so a row stamped with anything but the bound
  tenant is *rejected*, not merely hidden;
* one tenant's ledger row is **invisible** to another, so "counting is per
  organization" is a property of the database and not only of the predicate this
  code passes;
* the reservation is **atomic** — twenty concurrent turns at a cap of five admit
  exactly five. A read-then-write pair would admit far more, silently, in the
  direction that costs money.

It also drives the real ``CapPolicyResolver`` over the real repositories, so the
two ports the policy depends on are exercised against the schema rather than
against a fake of it.

Why as a limited role
---------------------
PostgreSQL exempts superusers and table owners from RLS, so run as the migration
role every assertion below would pass whether or not the policy existed. The
role setup is shared with the personal-tenant probe through ``conftest.py``;
``test_the_role_under_test_is_actually_subject_to_rls`` proves the posture
before anything else asserts on it.

Every "it worked" and every "nothing was written" is read back **as the owner**,
so no pass can come from RLS hiding either a success or residue.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import set_current_org_id
from faultmaven.infrastructure.protection import tenant_turn_cap as cap
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    SqlTurnLedger,
    TenantTurnCapExceeded,
    TurnCapService,
)
from tests.integration.security.conftest import (
    DEFAULT_ENTERPRISE_ID,
    create_limited_role,
    drop_limited_role,
    limited_url,
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

_LIMITED_ROLE = f"fm_cap_probe_{uuid.uuid4().hex[:8]}"
_LIMITED_PW = "fm_cap_probe_pw"


def _limited_url(superuser_url: str) -> str:
    return limited_url(superuser_url, _LIMITED_ROLE, _LIMITED_PW)


@pytest.fixture(scope="module")
def limited_role_env():
    """Point the persistence layer at the limited role for this module only.

    Restored wholesale in teardown: the ``-m postgres`` lane runs sibling
    modules that read ``DATABASE_URL`` expecting the SUPERUSER url, and leaking
    the limited one would make them measure RLS as the wrong role.
    """
    superuser_url = os.environ["DATABASE_URL"]
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "DEPLOYMENT_MODE", "TENANT_PROVIDER")
    }

    asyncio.run(create_limited_role(superuser_url, _LIMITED_ROLE, _LIMITED_PW))

    os.environ["DATABASE_URL"] = _limited_url(superuser_url)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"

    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.utils import reset_settings_singleton

    reset_settings_singleton()
    reset_engine()

    yield superuser_url

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_singleton()
    reset_engine()
    asyncio.run(drop_limited_role(superuser_url, _LIMITED_ROLE))


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop(limited_role_env):
    """One engine per test, because there is one event loop per test."""
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
def service():
    """The shipped wiring: both real repositories and the SQL ledger.

    ``multi_tenant`` is pinned true rather than read from the environment: the
    fixture above sets ``TENANT_PROVIDER=multi``, and pinning it here means a
    later change to that fixture cannot quietly turn every case in this module
    into the single-tenant short-circuit — which would pass, and prove nothing.
    """
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_org_repository import (  # noqa: E501
        SessionlessSSOPersonalOrgRepository,
    )

    return TurnCapService(
        CapPolicyResolver(
            SessionlessSSOPersonalOrgRepository(),
            SessionlessOrganizationRepository(),
            multi_tenant=lambda: True,
        ),
        SqlTurnLedger(),
    )


async def _as_owner(superuser_url: str, sql: str, **params):
    """Read or write as the OWNER — RLS-exempt, so it sees residue and successes."""
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


async def _make_org(superuser_url: str, *, personal: bool, override=None) -> str:
    organization_id = str(uuid.uuid4())
    slug = f"cap-{organization_id[:8]}"
    await _as_owner(
        superuser_url,
        "INSERT INTO organizations "
        "(organization_id, enterprise_id, name, slug, is_active, daily_turn_cap) "
        "VALUES (:o, :e, :n, :s, true, :c)",
        o=organization_id,
        e=DEFAULT_ENTERPRISE_ID,
        n=f"Cap probe {slug}",
        s=slug,
        c=override,
    )
    if personal:
        await _as_owner(
            superuser_url,
            "INSERT INTO sso_personal_orgs "
            "(provider, provider_user_id, organization_id, provider_org_id, "
            "enterprise_id) VALUES ('workos', :u, :o, :p, :e)",
            u=f"user_{uuid.uuid4().hex[:12]}",
            o=organization_id,
            p=f"org_{uuid.uuid4().hex[:12]}",
            e=DEFAULT_ENTERPRISE_ID,
        )
    return organization_id


async def _ledger_as_owner(superuser_url: str, organization_id: str, day=None):
    rows = await _as_owner(
        superuser_url,
        "SELECT turn_count FROM organization_turn_usage "
        "WHERE organization_id = :o AND usage_date = :d",
        o=organization_id,
        d=day or cap.utc_day(),
    )
    return rows[0][0] if rows else None


# =============================================================================
# The posture is what we think it is
# =============================================================================


async def test_the_role_under_test_is_actually_subject_to_rls(limited_role_env):
    """If RLS were bypassed, every assertion below would be vacuous."""
    engine = create_async_engine(_limited_url(limited_role_env), future=True)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
            assert row.rolsuper is False
            assert row.rolbypassrls is False

            owns = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                        "AND tableowner = current_user"
                    )
                )
            ).scalar()
            assert owns == 0, "the role owns tables, so RLS would not apply"

            enabled = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE relname = 'organization_turn_usage'"
                    )
                )
            ).scalar()
            assert enabled is True, (
                "migration 052 did not enrol the ledger in RLS, so one tenant's "
                "usage row is readable by every other tenant"
            )
    finally:
        await engine.dispose()


async def test_the_ledger_is_three_columns(limited_role_env):
    """``created_at``/``updated_at`` are absent on purpose.

    Every write after the day's first arrives through ``ON CONFLICT DO UPDATE``,
    which does not fire SQLAlchemy's ``onupdate`` — so a timestamp here would
    freeze at the first turn of the day while looking like it tracked the last.
    """
    rows = await _as_owner(
        limited_role_env,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'organization_turn_usage' ORDER BY column_name",
    )
    assert [r[0] for r in rows] == ["organization_id", "turn_count", "usage_date"]


async def test_a_mis_bound_ledger_write_is_refused_by_the_policy(limited_role_env):
    """The policy refuses, rather than merely hides, a row for another tenant."""
    mine = await _make_org(limited_role_env, personal=False)
    theirs = await _make_org(limited_role_env, personal=False)

    engine = create_async_engine(_limited_url(limited_role_env), future=True)
    try:
        async with engine.begin() as conn:
            # ``set_config(..., is_local => true)`` rather than ``SET LOCAL``:
            # the latter takes no bind parameter, and interpolating the id would
            # make this the one place in the module that builds SQL by string.
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"), {"o": mine}
            )
            with pytest.raises(Exception) as raised:
                await conn.execute(
                    text(
                        "INSERT INTO organization_turn_usage "
                        "(organization_id, usage_date, turn_count) "
                        "VALUES (:o, CURRENT_DATE, 1)"
                    ),
                    {"o": theirs},
                )
            assert "row-level security" in str(raised.value).lower()
    finally:
        await engine.dispose()


# =============================================================================
# The policy, resolved through the real ports
# =============================================================================


async def test_a_personal_tenant_is_refused_at_its_cap(limited_role_env, service):
    organization_id = await _make_org(limited_role_env, personal=True, override=3)
    set_current_org_id(organization_id)

    for expected in (1, 2, 3):
        assert (await service.reserve(organization_id)).used == expected

    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(organization_id)

    assert raised.value.limit == 3
    assert raised.value.used == 3
    assert "3" in raised.value.user_message
    assert "UTC" in raised.value.user_message


async def test_a_refused_turn_writes_nothing(limited_role_env, service):
    """Read back as the OWNER, so this cannot be RLS hiding a row."""
    organization_id = await _make_org(limited_role_env, personal=True, override=1)
    set_current_org_id(organization_id)

    await service.reserve(organization_id)
    assert await _ledger_as_owner(limited_role_env, organization_id) == 1

    for _ in range(4):
        with pytest.raises(TenantTurnCapExceeded):
            await service.reserve(organization_id)

    assert await _ledger_as_owner(limited_role_env, organization_id) == 1


async def test_the_kind_is_read_through_the_sso_port(limited_role_env, service):
    """A personal organization resolves as personal without a second lookup rule."""
    from faultmaven.modules.auth.infrastructure.repositories.sso_personal_org_repository import (  # noqa: E501
        SessionlessSSOPersonalOrgRepository,
    )

    personal = await _make_org(limited_role_env, personal=True)
    company = await _make_org(limited_role_env, personal=False)
    port = SessionlessSSOPersonalOrgRepository()

    set_current_org_id(personal)
    assert await port.is_personal_organization(personal) is True
    assert await port.is_personal_organization(company) is False


async def test_the_override_is_read_through_the_organization_repository(
    limited_role_env,
):
    """``daily_turn_cap`` must survive the mapper AND the writer.

    Read-only support would look correct until the first unrelated
    ``update_organization`` silently reverted the operator's override.
    """
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )

    organization_id = await _make_org(limited_role_env, personal=True, override=7)
    set_current_org_id(organization_id)
    repository = SessionlessOrganizationRepository()

    organization = await repository.get_organization(organization_id)
    assert organization.daily_turn_cap == 7

    organization.daily_turn_cap = 12
    assert await repository.update_organization(organization)
    assert (await repository.get_organization(organization_id)).daily_turn_cap == 12

    # And an unrelated update does not revert it.
    organization = await repository.get_organization(organization_id)
    organization.description = "renamed"
    await repository.update_organization(organization)
    assert (await repository.get_organization(organization_id)).daily_turn_cap == 12


async def test_a_soft_deleted_organization_does_not_resolve(limited_role_env):
    """The ``deleted_at`` filter the CLI now inherits instead of going around.

    ``fm-set-turn-cap`` used to run its own SELECT with no such predicate, so it
    would happily set a spend control on a tenant that had been deleted — and
    report success. Going through the repository is what fixes it, and this is
    the assertion that the repository really does filter, on the real schema.
    """
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )

    organization_id = await _make_org(limited_role_env, personal=True, override=9)
    set_current_org_id(organization_id)
    repository = SessionlessOrganizationRepository()
    assert await repository.get_organization(organization_id) is not None

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET deleted_at = NOW() WHERE organization_id = :o",
        o=organization_id,
    )
    assert await repository.get_organization(organization_id) is None


async def test_counting_is_per_organization_and_the_row_is_invisible(
    limited_role_env, service
):
    first = await _make_org(limited_role_env, personal=True, override=2)
    second = await _make_org(limited_role_env, personal=True, override=2)

    set_current_org_id(first)
    await service.reserve(first)
    await service.reserve(first)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(first)

    set_current_org_id(second)
    assert (await service.reserve(second)).used == 1

    engine = create_async_engine(_limited_url(limited_role_env), future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"),
                {"o": second},
            )
            visible = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM organization_turn_usage "
                        "WHERE organization_id = :o"
                    ),
                    {"o": first},
                )
            ).scalar()
            assert visible == 0
    finally:
        await engine.dispose()

    assert await _ledger_as_owner(limited_role_env, first) == 2
    assert await _ledger_as_owner(limited_role_env, second) == 1


async def test_counting_is_per_utc_day(limited_role_env, service):
    organization_id = await _make_org(limited_role_env, personal=True, override=2)
    set_current_org_id(organization_id)

    yesterday = datetime.now(UTC) - timedelta(days=1)
    await service.reserve(organization_id, now=yesterday)
    await service.reserve(organization_id, now=yesterday)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(organization_id, now=yesterday)

    assert (await service.reserve(organization_id)).used == 1
    assert (
        await _ledger_as_owner(
            limited_role_env, organization_id, cap.utc_day(yesterday)
        )
        == 2
    )
    assert await _ledger_as_owner(limited_role_env, organization_id) == 1


async def test_a_company_tenant_with_no_override_is_never_refused(
    limited_role_env, service
):
    organization_id = await _make_org(limited_role_env, personal=False)
    set_current_org_id(organization_id)

    for expected in range(1, 41):
        reservation = await service.reserve(organization_id)
        assert reservation.used == expected
        assert reservation.limit is None
        assert reservation.source == "company_uncapped"

    assert await _ledger_as_owner(limited_role_env, organization_id) == 40


async def test_an_override_raises_one_tenants_cap_without_a_restart(
    limited_role_env, service
):
    capped = await _make_org(limited_role_env, personal=True, override=1)
    sibling = await _make_org(limited_role_env, personal=True, override=1)

    set_current_org_id(capped)
    await service.reserve(capped)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(capped)

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET daily_turn_cap = 4 WHERE organization_id = :o",
        o=capped,
    )

    assert (await service.reserve(capped)).used == 2

    set_current_org_id(sibling)
    await service.reserve(sibling)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(sibling)


async def test_clearing_a_tenants_cap_lets_it_past_the_default(
    limited_role_env, service
):
    organization_id = await _make_org(limited_role_env, personal=True, override=2)
    set_current_org_id(organization_id)

    await service.reserve(organization_id)
    await service.reserve(organization_id)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(organization_id)

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET daily_turn_cap = 0 WHERE organization_id = :o",
        o=organization_id,
    )

    reservation = await service.reserve(organization_id)
    assert reservation.limit is None
    assert reservation.source == "override_unlimited"


async def test_the_column_refuses_a_negative_cap(limited_role_env):
    """ "Unlimited" has one spelling, and the database keeps it that way."""
    organization_id = await _make_org(limited_role_env, personal=True)
    with pytest.raises(Exception) as raised:
        await _as_owner(
            limited_role_env,
            "UPDATE organizations SET daily_turn_cap = -1 WHERE organization_id = :o",
            o=organization_id,
        )
    assert "daily_turn_cap" in str(raised.value)


# =============================================================================
# The reservation is atomic
# =============================================================================


async def test_concurrent_turns_at_the_boundary_admit_exactly_the_limit(
    limited_role_env, service
):
    """A read-then-write pair would admit far more, and say nothing about it.

    Counts asserted on all three — admitted, refused, and the ledger — because
    "five succeeded" alone would also hold if the ledger had been driven to
    twenty by writers whose results were discarded.
    """
    organization_id = await _make_org(limited_role_env, personal=True, override=5)
    set_current_org_id(organization_id)

    async def _attempt():
        try:
            await service.reserve(organization_id)
            return "admitted"
        except TenantTurnCapExceeded:
            return "refused"

    outcomes = await asyncio.gather(*(_attempt() for _ in range(20)))

    assert outcomes.count("admitted") == 5, outcomes
    assert outcomes.count("refused") == 15, outcomes
    assert await _ledger_as_owner(limited_role_env, organization_id) == 5


# =============================================================================
# Failure direction
# =============================================================================


async def test_an_unreadable_tenant_kind_is_capped_at_the_default(
    limited_role_env, service
):
    """Fail closed, staged the way a deployment actually meets it.

    ``SELECT`` on ``sso_personal_orgs`` revoked from the application role — a
    permissions or migration-order mistake — on a **company** organization,
    which is uncapped when the question is answerable. A pass can therefore only
    come from the fail-closed branch.
    """
    organization_id = await _make_org(limited_role_env, personal=False)
    set_current_org_id(organization_id)

    await _as_owner(
        limited_role_env,
        f"REVOKE SELECT ON sso_personal_orgs FROM {_LIMITED_ROLE}",
    )
    try:
        policy = await service._resolver.resolve(organization_id)
        assert policy.limit == 30
        assert policy.source == "indeterminate"
    finally:
        await _as_owner(
            limited_role_env,
            f"GRANT SELECT ON sso_personal_orgs TO {_LIMITED_ROLE}",
        )


async def test_an_unreadable_override_is_the_default_not_no_override(
    limited_role_env, service
):
    """The inversion the review caught, on the real schema.

    ``SELECT`` on ``organizations`` revoked, again on a COMPANY organization:
    read as "no override" this would resolve **uncapped**, so an override of 50
    would be silently lifted by a transient permissions fault.
    """
    organization_id = await _make_org(limited_role_env, personal=False, override=50)
    set_current_org_id(organization_id)

    await _as_owner(
        limited_role_env,
        f"REVOKE SELECT ON organizations FROM {_LIMITED_ROLE}",
    )
    try:
        policy = await service._resolver.resolve(organization_id)
        assert policy.limit == 30
        assert policy.source == "indeterminate"
    finally:
        await _as_owner(
            limited_role_env,
            f"GRANT SELECT ON organizations TO {_LIMITED_ROLE}",
        )

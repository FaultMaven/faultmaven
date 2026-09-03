"""The turn cap against a real PostgreSQL under RLS (ADR-016 D5.3).

The unit module pins the *decision* — which tenants are capped, what an override
does, which way ambiguity falls. This module pins the three things only a real
database can answer:

* the ledger can actually be **written by the application role**, whose RLS
  policy applies ``USING`` as ``WITH CHECK`` on every write (migration 018's
  pattern, which migration 052 enrols the new table into) — so a row stamped
  with anything but the bound tenant is *rejected*, not merely hidden;
* one tenant's ledger row is **invisible** to another, so "counting is per
  organization" is a property of the database and not only of the predicate this
  code happens to pass;
* the reservation is **atomic** — twenty concurrent turns at a cap of five admit
  exactly five. A read-then-write pair would admit far more, and the failure
  would be silent: the count would simply be wrong, in the direction that costs
  money.

Why as a limited role
---------------------
PostgreSQL exempts superusers and table owners from RLS. Run as the migration
role, every assertion below would pass whether or not the policy existed — the
module would be testing a system nobody deploys. So it creates a role with
exactly the grants ``02-create-rls-app-role.sql`` gives ``faultmaven_app`` and
drives the real mechanism through it, and
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
_DROP_ROLE_SQL = f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_LIMITED_ROLE}') THEN
    DROP OWNED BY {_LIMITED_ROLE};
    DROP ROLE {_LIMITED_ROLE};
  END IF;
END $$;
"""

#: The default enterprise migration 006 seeds. Used as the FK target so the
#: fixtures create organizations without inventing a tier.
DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


def _limited_url(superuser_url: str) -> str:
    from sqlalchemy.engine import make_url

    return (
        make_url(superuser_url)
        .set(username=_LIMITED_ROLE, password=_LIMITED_PW)
        .render_as_string(hide_password=False)
    )


async def _create_limited_role(superuser_url: str) -> None:
    """A role with the deployed ``faultmaven_app`` grants and no ownership."""
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
            await conn.exec_driver_sql(_DROP_ROLE_SQL)
            await conn.exec_driver_sql(
                f"CREATE ROLE {_LIMITED_ROLE} LOGIN PASSWORD '{_LIMITED_PW}' "
                "NOSUPERUSER NOBYPASSRLS"
            )
            await conn.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{dbname}" TO {_LIMITED_ROLE}'
            )
            await conn.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {_LIMITED_ROLE}"
            )
            await conn.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f"TO {_LIMITED_ROLE}"
            )
            await conn.exec_driver_sql(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_LIMITED_ROLE}"
            )
    finally:
        await engine.dispose()


async def _drop_limited_role(superuser_url: str) -> None:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_DROP_ROLE_SQL)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def limited_role_env():
    """Point the persistence layer at the limited role for this module only.

    Restored wholesale in teardown: the ``-m postgres`` lane runs sibling modules
    that read ``DATABASE_URL`` expecting the SUPERUSER url, and leaking the
    limited one would make them measure RLS as the wrong role and quietly stop
    proving anything.
    """
    superuser_url = os.environ["DATABASE_URL"]
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "DEPLOYMENT_MODE", "TENANT_PROVIDER")
    }

    asyncio.run(_create_limited_role(superuser_url))

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
    asyncio.run(_drop_limited_role(superuser_url))


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop(limited_role_env):
    """One engine per test, because there is one event loop per test.

    ``get_engine`` memoises a module-global engine whose pool binds to whatever
    loop first used it; a pooled connection carried into the next test belongs to
    a closed loop and surfaces as "attached to a different loop" rather than as
    anything this module means to measure.
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


async def _as_owner(superuser_url: str, sql: str, **params):
    """Read or write as the OWNER — RLS-exempt, so it sees residue and successes alike."""
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


async def _make_org(superuser_url: str, *, personal: bool, override=None) -> str:
    """Create one organization as the owner, optionally personal and/or capped."""
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
            "(provider, provider_user_id, organization_id, provider_org_id, enterprise_id) "
            "VALUES ('workos', :u, :o, :p, :e)",
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


async def test_a_mis_bound_ledger_write_is_refused_by_the_policy(limited_role_env):
    """The policy refuses, rather than merely hides, a row for another tenant.

    Without this the isolation assertions below could pass on a system where
    writes are unconstrained and reads happen to be filtered — which is not the
    guarantee migration 018's pattern makes.
    """
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
# Invariant 1 — a personal tenant at its cap is refused; nothing else is
# =============================================================================


async def test_a_personal_tenant_is_refused_at_its_cap(limited_role_env):
    organization_id = await _make_org(limited_role_env, personal=True, override=3)
    set_current_org_id(organization_id)

    for expected in (1, 2, 3):
        assert (await cap.reserve_turn(organization_id)).used == expected

    with pytest.raises(cap.TenantTurnCapExceeded) as raised:
        await cap.reserve_turn(organization_id)

    assert raised.value.limit == 3
    assert raised.value.used == 3
    assert "3" in raised.value.user_message
    assert "UTC" in raised.value.user_message


async def test_a_refused_turn_writes_nothing(limited_role_env):
    """Read back as the OWNER, so "nothing was written" cannot be RLS hiding it."""
    organization_id = await _make_org(limited_role_env, personal=True, override=1)
    set_current_org_id(organization_id)

    await cap.reserve_turn(organization_id)
    assert await _ledger_as_owner(limited_role_env, organization_id) == 1

    for _ in range(4):
        with pytest.raises(cap.TenantTurnCapExceeded):
            await cap.reserve_turn(organization_id)

    assert await _ledger_as_owner(limited_role_env, organization_id) == 1


async def test_counting_is_per_organization_and_the_row_is_invisible_to_the_other(
    limited_role_env,
):
    """Both halves: the count does not leak, and neither does the row."""
    first = await _make_org(limited_role_env, personal=True, override=2)
    second = await _make_org(limited_role_env, personal=True, override=2)

    set_current_org_id(first)
    await cap.reserve_turn(first)
    await cap.reserve_turn(first)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(first)

    # The second tenant has its own, untouched allowance.
    set_current_org_id(second)
    assert (await cap.reserve_turn(second)).used == 1

    # And bound to the second tenant, the first tenant's row is not readable.
    engine = create_async_engine(_limited_url(limited_role_env), future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"), {"o": second}
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

    # Both rows exist; only the tenant boundary hid one of them.
    assert await _ledger_as_owner(limited_role_env, first) == 2
    assert await _ledger_as_owner(limited_role_env, second) == 1


async def test_counting_is_per_utc_day(limited_role_env):
    organization_id = await _make_org(limited_role_env, personal=True, override=2)
    set_current_org_id(organization_id)

    yesterday = datetime.now(UTC) - timedelta(days=1)
    await cap.reserve_turn(organization_id, now=yesterday)
    await cap.reserve_turn(organization_id, now=yesterday)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(organization_id, now=yesterday)

    assert (await cap.reserve_turn(organization_id)).used == 1
    assert (
        await _ledger_as_owner(
            limited_role_env, organization_id, cap.utc_day(yesterday)
        )
        == 2
    )
    assert await _ledger_as_owner(limited_role_env, organization_id) == 1


# =============================================================================
# Invariant 2 — a company tenant with no override is never refused
# =============================================================================


async def test_a_company_tenant_with_no_override_is_never_refused(limited_role_env):
    """Well past the personal default, so applying it to everyone would fail here."""
    organization_id = await _make_org(limited_role_env, personal=False)
    set_current_org_id(organization_id)

    for expected in range(1, 41):
        reservation = await cap.reserve_turn(organization_id)
        assert reservation.used == expected
        assert reservation.limit is None
        assert reservation.source == "company_uncapped"

    assert await _ledger_as_owner(limited_role_env, organization_id) == 40


# =============================================================================
# Invariant 3 — an override moves one tenant, live
# =============================================================================


async def test_an_override_raises_one_tenants_cap_without_a_restart(limited_role_env):
    capped = await _make_org(limited_role_env, personal=True, override=1)
    sibling = await _make_org(limited_role_env, personal=True, override=1)

    set_current_org_id(capped)
    await cap.reserve_turn(capped)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(capped)

    # The operator raises this tenant's cap. Same process, no reconfiguration.
    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET daily_turn_cap = 4 WHERE organization_id = :o",
        o=capped,
    )

    assert (await cap.reserve_turn(capped)).used == 2

    # The sibling is untouched and still at one.
    set_current_org_id(sibling)
    await cap.reserve_turn(sibling)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(sibling)


async def test_clearing_a_tenants_cap_lets_it_past_the_default(limited_role_env):
    """``--unlimited`` stores 0, and 0 means uncapped for a personal tenant too."""
    organization_id = await _make_org(limited_role_env, personal=True, override=2)
    set_current_org_id(organization_id)

    await cap.reserve_turn(organization_id)
    await cap.reserve_turn(organization_id)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(organization_id)

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET daily_turn_cap = 0 WHERE organization_id = :o",
        o=organization_id,
    )

    reservation = await cap.reserve_turn(organization_id)
    assert reservation.limit is None
    assert reservation.source == "override_unlimited"


async def test_the_column_refuses_a_negative_cap(limited_role_env):
    """ "Unlimited" has one spelling, and the database is what keeps it that way."""
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
    limited_role_env,
):
    """A read-then-write pair would admit far more, and say nothing about it.

    Twenty concurrent reservations against a cap of five: exactly five are
    admitted, fifteen are refused, and the ledger stands at five. The counts are
    asserted on all three, because "five succeeded" alone would also hold if the
    ledger had been driven to twenty by writers whose results were discarded.
    """
    organization_id = await _make_org(limited_role_env, personal=True, override=5)
    set_current_org_id(organization_id)

    async def _attempt():
        try:
            await cap.reserve_turn(organization_id)
            return "admitted"
        except cap.TenantTurnCapExceeded:
            return "refused"

    outcomes = await asyncio.gather(*(_attempt() for _ in range(20)))

    assert outcomes.count("admitted") == 5, outcomes
    assert outcomes.count("refused") == 15, outcomes
    assert await _ledger_as_owner(limited_role_env, organization_id) == 5


# =============================================================================
# Invariant 5 — ambiguity caps at the default
# =============================================================================


async def test_an_unreadable_tenant_kind_is_capped_at_the_default(limited_role_env):
    """Fail closed, on the real database rather than on a dropped SQLite table.

    Staged by revoking the limited role's access to ``sso_personal_orgs``, which
    is the shape a permissions or migration-order mistake actually takes in a
    deployment: the table is there, the app cannot read it. The organization
    under test is a COMPANY one — uncapped when the question is answerable — so
    a pass here can only come from the fail-closed branch.
    """
    organization_id = await _make_org(limited_role_env, personal=False)
    set_current_org_id(organization_id)

    await _as_owner(
        limited_role_env,
        f"REVOKE SELECT ON sso_personal_orgs FROM {_LIMITED_ROLE}",
    )
    try:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            policy = await cap.resolve_policy(session, organization_id)
        assert policy.limit == 30
        assert policy.source == "indeterminate"
    finally:
        await _as_owner(
            limited_role_env,
            f"GRANT SELECT ON sso_personal_orgs TO {_LIMITED_ROLE}",
        )

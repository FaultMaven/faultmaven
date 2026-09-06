"""The turn cap against a real PostgreSQL under RLS (ADR-016 D5.3).

The unit module pins the *decision* through fakes of the two ports. This module
pins the three things only a real database can answer:

* the ledger can be **written by the application role**, whose RLS policy
  applies ``USING`` as ``WITH CHECK`` — so a row stamped with anything but the
  bound enterprise is *rejected*, not merely hidden;
* one tenant's ledger row is **invisible** to another, so "counting is per
  billing subject" is a property of the database and not only of the predicate
  this code passes;
* the reservation is **atomic** — twenty concurrent turns at a cap of five admit
  exactly five. A read-then-write pair would admit far more, silently, in the
  direction that costs money.

It also drives the real ``CapPolicyResolver`` over the real organization
repository, so the one port the policy still depends on is exercised against the
schema rather than against a fake of it. Under ADR-017 D5 there is no second
port: "personal" means "no organization", which is a property of the subject
rather than a row in a table, so the kind question needs no lookup at all.

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

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.infrastructure.protection import tenant_turn_cap as cap
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    SUBJECT_ACCOUNT,
    SUBJECT_ORGANIZATION,
    BillingSubject,
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
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


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

    return TurnCapService(
        CapPolicyResolver(
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


async def _make_org(superuser_url: str, *, override=None) -> BillingSubject:
    """An ORGANIZATION billing subject: somebody is paying for this account."""
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
    return BillingSubject(SUBJECT_ORGANIZATION, organization_id)


def _make_account() -> BillingSubject:
    """An ACCOUNT billing subject: nobody is paying, so it gets the default.

    No row is written. That is the whole point of ADR-017 D5's re-statement —
    "personal" stopped being a row in ``sso_personal_orgs`` and became the
    absence of an organization, so there is nothing to insert and nothing whose
    unreadability could invert the policy.
    """
    return BillingSubject(SUBJECT_ACCOUNT, f"user_{uuid.uuid4().hex[:12]}")


async def _ledger_as_owner(superuser_url: str, subject: BillingSubject, day=None):
    rows = await _as_owner(
        superuser_url,
        "SELECT turn_count FROM turn_usage "
        "WHERE billing_subject_kind = :k AND billing_subject_id = :i "
        "AND usage_date = :d",
        k=subject.kind,
        i=subject.subject_id,
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
                        "WHERE relname = 'turn_usage'"
                    )
                )
            ).scalar()
            assert enabled is True, (
                "the baseline did not enrol the ledger in RLS, so one tenant's "
                "usage row is readable by every other tenant"
            )
    finally:
        await engine.dispose()


async def test_the_ledger_carries_the_subject_key_and_no_timestamps(limited_role_env):
    """``created_at``/``updated_at`` are absent on purpose.

    Every write after the day's first arrives through ``ON CONFLICT DO UPDATE``,
    which does not fire SQLAlchemy's ``onupdate`` — so a timestamp here would
    freeze at the first turn of the day while looking like it tracked the last.

    The columns that ARE here are the subject key (ADR-017 D5) plus the
    enterprise every tenant-scoped table carries for RLS. The enterprise is not
    part of the KEY: two accounts of one enterprise in different organizations
    are charged separately, which is the whole reason the key moved.
    """
    rows = await _as_owner(
        limited_role_env,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'turn_usage' ORDER BY column_name",
    )
    assert [r[0] for r in rows] == [
        "billing_subject_id",
        "billing_subject_kind",
        "enterprise_id",
        "turn_count",
        "usage_date",
    ]


async def test_a_mis_bound_ledger_write_is_refused_by_the_policy(limited_role_env):
    """The policy refuses, rather than merely hides, a row for another tenant."""
    subject = _make_account()

    engine = create_async_engine(_limited_url(limited_role_env), future=True)
    try:
        async with engine.begin() as conn:
            # ``set_config(..., is_local => true)`` rather than ``SET LOCAL``:
            # the latter takes no bind parameter, and interpolating the id would
            # make this the one place in the module that builds SQL by string.
            await conn.execute(
                text("SELECT set_config('app.current_enterprise_id', :e, true)"),
                {"e": DEFAULT_ENTERPRISE_ID},
            )
            with pytest.raises(Exception) as raised:
                await conn.execute(
                    text(
                        "INSERT INTO turn_usage (enterprise_id, "
                        "billing_subject_kind, billing_subject_id, usage_date, "
                        "turn_count) VALUES (:e, :k, :i, CURRENT_DATE, 1)"
                    ),
                    {
                        "e": "ent_someone_else",
                        "k": subject.kind,
                        "i": subject.subject_id,
                    },
                )
            assert "row-level security" in str(raised.value).lower()
    finally:
        await engine.dispose()


# =============================================================================
# The policy, resolved through the real ports
# =============================================================================


async def test_a_capped_subject_is_refused_at_its_cap(limited_role_env, service):
    subject = await _make_org(limited_role_env, override=3)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    for expected in (1, 2, 3):
        assert (await service.reserve(subject)).used == expected

    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(subject)

    assert raised.value.limit == 3
    assert raised.value.used == 3
    assert "3" in raised.value.user_message
    assert "UTC" in raised.value.user_message


async def test_a_refused_turn_writes_nothing(limited_role_env, service):
    """Read back as the OWNER, so this cannot be RLS hiding a row."""
    subject = await _make_org(limited_role_env, override=1)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    await service.reserve(subject)
    assert await _ledger_as_owner(limited_role_env, subject) == 1

    for _ in range(4):
        with pytest.raises(TenantTurnCapExceeded):
            await service.reserve(subject)

    assert await _ledger_as_owner(limited_role_env, subject) == 1


async def test_the_kind_is_the_subject_itself_and_needs_no_lookup(
    limited_role_env, service
):
    """An ACCOUNT subject is capped; an ORGANIZATION subject is not.

    The replacement for the old ``sso_personal_orgs`` question (ADR-017 D5).
    Asserted against the real resolver and the real repository, and asserted in
    BOTH directions on the same run: "the account is capped" alone would also
    hold if the resolver had stopped distinguishing them and capped everything.
    """
    account = _make_account()
    company = await _make_org(limited_role_env)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    personal_policy = await service._resolver.resolve(account)
    assert personal_policy.limit == 30
    assert personal_policy.source == "default_personal"

    company_policy = await service._resolver.resolve(company)
    assert company_policy.limit is None
    assert company_policy.source == "company_uncapped"


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

    subject = await _make_org(limited_role_env, override=7)
    organization_id = subject.subject_id
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)
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

    organization_id = (await _make_org(limited_role_env, override=9)).subject_id
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)
    repository = SessionlessOrganizationRepository()
    assert await repository.get_organization(organization_id) is not None

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET deleted_at = NOW() WHERE organization_id = :o",
        o=organization_id,
    )
    assert await repository.get_organization(organization_id) is None


async def test_counting_is_per_subject_and_another_tenants_row_is_invisible(
    limited_role_env, service
):
    """Two halves, and they are two different guarantees.

    Per-SUBJECT counting is an application property: two subjects inside ONE
    enterprise keep separate ledgers, which is what stops a company's two cost
    centres sharing one allowance. Invisibility is a database property, and it
    is keyed on the ENTERPRISE — so it is checked against a subject in a
    different one.
    """
    first = await _make_org(limited_role_env, override=2)
    second = await _make_org(limited_role_env, override=2)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    await service.reserve(first)
    await service.reserve(first)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(first)

    # Same enterprise, different billing subject: its own allowance.
    assert (await service.reserve(second)).used == 1

    engine = create_async_engine(_limited_url(limited_role_env), future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_enterprise_id', :e, true)"),
                {"e": "ent_somewhere_else"},
            )
            visible = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM turn_usage "
                        "WHERE billing_subject_id = :i"
                    ),
                    {"i": first.subject_id},
                )
            ).scalar()
            assert visible == 0
    finally:
        await engine.dispose()

    assert await _ledger_as_owner(limited_role_env, first) == 2
    assert await _ledger_as_owner(limited_role_env, second) == 1


async def test_counting_is_per_utc_day(limited_role_env, service):
    subject = await _make_org(limited_role_env, override=2)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    yesterday = datetime.now(UTC) - timedelta(days=1)
    await service.reserve(subject, now=yesterday)
    await service.reserve(subject, now=yesterday)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(subject, now=yesterday)

    assert (await service.reserve(subject)).used == 1
    assert (
        await _ledger_as_owner(limited_role_env, subject, cap.utc_day(yesterday)) == 2
    )
    assert await _ledger_as_owner(limited_role_env, subject) == 1


async def test_a_company_tenant_with_no_override_is_never_refused(
    limited_role_env, service
):
    subject = await _make_org(limited_role_env)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    for expected in range(1, 41):
        reservation = await service.reserve(subject)
        assert reservation.used == expected
        assert reservation.limit is None
        assert reservation.source == "company_uncapped"

    assert await _ledger_as_owner(limited_role_env, subject) == 40


async def test_an_override_raises_one_tenants_cap_without_a_restart(
    limited_role_env, service
):
    capped = await _make_org(limited_role_env, override=1)
    sibling = await _make_org(limited_role_env, override=1)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    await service.reserve(capped)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(capped)

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET daily_turn_cap = 4 WHERE organization_id = :o",
        o=capped.subject_id,
    )

    assert (await service.reserve(capped)).used == 2

    await service.reserve(sibling)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(sibling)


async def test_clearing_a_tenants_cap_lets_it_past_the_default(
    limited_role_env, service
):
    subject = await _make_org(limited_role_env, override=2)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    await service.reserve(subject)
    await service.reserve(subject)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(subject)

    await _as_owner(
        limited_role_env,
        "UPDATE organizations SET daily_turn_cap = 0 WHERE organization_id = :o",
        o=subject.subject_id,
    )

    reservation = await service.reserve(subject)
    assert reservation.limit is None
    assert reservation.source == "override_unlimited"


async def test_the_column_refuses_a_negative_cap(limited_role_env):
    """ "Unlimited" has one spelling, and the database keeps it that way."""
    subject = await _make_org(limited_role_env)
    with pytest.raises(Exception) as raised:
        await _as_owner(
            limited_role_env,
            "UPDATE organizations SET daily_turn_cap = -1 WHERE organization_id = :o",
            o=subject.subject_id,
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
    subject = await _make_org(limited_role_env, override=5)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    async def _attempt():
        try:
            await service.reserve(subject)
            return "admitted"
        except TenantTurnCapExceeded:
            return "refused"

    outcomes = await asyncio.gather(*(_attempt() for _ in range(20)))

    assert outcomes.count("admitted") == 5, outcomes
    assert outcomes.count("refused") == 15, outcomes
    assert await _ledger_as_owner(limited_role_env, subject) == 5


# =============================================================================
# Failure direction
# =============================================================================


async def test_an_unreadable_override_is_the_default_not_no_override(
    limited_role_env, service
):
    """The inversion the review caught, on the real schema.

    ``SELECT`` on ``organizations`` revoked, on an ORGANIZATION subject: read as
    "no override" this would resolve **uncapped**, so an override of 50 would be
    silently lifted by a transient permissions fault.

    This is now the ONLY fail-closed branch the resolver has. Its sibling — "the
    tenant's kind could not be read" — is gone with the table it read: under
    ADR-017 D5 the kind IS the subject, so there is no lookup left to fail and
    no branch left to invert.
    """
    subject = await _make_org(limited_role_env, override=50)
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)

    await _as_owner(
        limited_role_env,
        f"REVOKE SELECT ON organizations FROM {_LIMITED_ROLE}",
    )
    try:
        policy = await service._resolver.resolve(subject)
        assert policy.limit == 30
        assert policy.source == "indeterminate"
    finally:
        await _as_owner(
            limited_role_env,
            f"GRANT SELECT ON organizations TO {_LIMITED_ROLE}",
        )


async def test_no_billing_subject_is_capped_at_the_default_and_writes_nothing(
    limited_role_env, service
):
    """Fail closed on an actor there is nobody to charge.

    Unreachable through the front door — an authenticated request always has an
    account — and guarded anyway, because this must not be the place that
    decides an unidentifiable actor is free. The refusal is
    ``TenantTurnCapUnavailable`` rather than ``TenantTurnCapExceeded``: telling
    somebody their daily allowance is spent when there is no allowance to spend
    would be a false statement about their own account.
    """
    from faultmaven.infrastructure.protection.tenant_turn_cap import (
        TenantTurnCapUnavailable,
    )

    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)
    assert (await service._resolver.resolve(None)).limit == 30

    with pytest.raises(TenantTurnCapUnavailable):
        await service.reserve(None)

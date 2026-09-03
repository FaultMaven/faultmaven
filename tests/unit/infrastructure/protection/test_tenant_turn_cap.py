"""The per-tenant turn cap's decision, against a real database (ADR-016 D5.3).

The mechanism is small and every one of its rules is a claim someone will rely
on, so this module pins each of them separately: which tenants are capped, what
an override does, which direction ambiguity falls in, and — the one that makes
the cap a cap rather than a suggestion — that a refused turn consumes nothing.

Why a real SQLite engine rather than a mocked session
-----------------------------------------------------
The enforcement *is* one SQL statement: ``INSERT … ON CONFLICT … DO UPDATE SET
turn_count = turn_count + 1 WHERE turn_count < :cap RETURNING turn_count``. A
mocked session would assert that this module built the statement it builds,
which is a restatement rather than a test — the interesting behaviour (an empty
RETURNING at the boundary, and nothing written) lives in the database. The
PostgreSQL half of the same behaviour, under RLS and with real concurrency, is
``tests/integration/security/test_tenant_turn_cap.py``; this half is what the
standalone lane can run.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
    OrganizationTurnUsageModel,
    SSOPersonalOrgModel,
)
from faultmaven.infrastructure.protection import tenant_turn_cap as cap

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
PERSONAL_ORG = "11111111-1111-1111-1111-111111111111"
COMPANY_ORG = "22222222-2222-2222-2222-222222222222"
OTHER_PERSONAL_ORG = "44444444-4444-4444-4444-444444444444"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def factory(engine):
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=ENTERPRISE_ID,
                name="Acme",
                slug="acme",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        for org_id, slug in (
            (PERSONAL_ORG, "personal-a"),
            (COMPANY_ORG, "acme-corp"),
            (OTHER_PERSONAL_ORG, "personal-b"),
        ):
            session.add(
                OrganizationModel(
                    organization_id=org_id,
                    enterprise_id=ENTERPRISE_ID,
                    name=slug,
                    slug=slug,
                    is_active=True,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        # Two personal tenants and one company organization. "Personal" is the
        # existence of this row and nothing else — the same rule the login path
        # writes and the enforcement reads, rather than a flag invented here.
        for subject, org in (("user_a", PERSONAL_ORG), ("user_b", OTHER_PERSONAL_ORG)):
            session.add(
                SSOPersonalOrgModel(
                    provider="workos",
                    provider_user_id=subject,
                    organization_id=org,
                    provider_org_id=f"org_{subject}",
                    enterprise_id=ENTERPRISE_ID,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        await session.commit()
    yield maker


@pytest.fixture(autouse=True)
def bind_sessions(monkeypatch, factory):
    """Point ``reserve_turn`` at this test's engine.

    ``reserve_turn`` opens its own session through
    ``persistence.database.get_db_session``; it is imported inside the function,
    so patching the module attribute is what the call actually resolves.
    """
    from faultmaven.infrastructure.persistence import database

    @contextlib.asynccontextmanager
    async def _session():
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(database, "get_db_session", _session)
    return _session


async def _set_override(factory, organization_id, value):
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE organizations SET daily_turn_cap = :v "
                "WHERE organization_id = :o"
            ),
            {"v": value, "o": organization_id},
        )
        await session.commit()


async def _ledger(factory, organization_id, day=None):
    async with factory() as session:
        return (
            await session.execute(
                select(OrganizationTurnUsageModel.turn_count).where(
                    OrganizationTurnUsageModel.organization_id == organization_id,
                    OrganizationTurnUsageModel.usage_date == (day or cap.utc_day()),
                )
            )
        ).scalar_one_or_none()


# =============================================================================
# The day boundary the refusal message promises
# =============================================================================


def test_the_charged_day_is_the_utc_calendar_day():
    """A local-midnight boundary would reset the wrong tenants at the wrong time."""
    # 23:30 in a +05:30 zone is the PREVIOUS UTC day, and that is the day the
    # turn is charged to.
    local = datetime(
        2026, 9, 4, 23, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    assert cap.utc_day(local) == date(2026, 9, 4)
    assert cap.utc_day(datetime(2026, 9, 4, 0, 5, tzinfo=timezone.utc)) == date(
        2026, 9, 4
    )


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 4, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 23, 59, 59, tzinfo=timezone.utc),
    ],
)
def test_the_reset_instant_is_always_the_next_utc_midnight(moment):
    """Strictly after the moment, and never more than a day away."""
    reset = cap.next_utc_midnight(moment)
    assert reset > moment
    assert reset - moment <= timedelta(days=1)
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)


def test_the_message_names_the_limit_and_when_it_comes_back():
    """The refusal has to be actionable by the person reading it, not just correct."""
    refusal = cap.TenantTurnCapExceeded(
        organization_id=PERSONAL_ORG,
        limit=30,
        used=30,
        reset_at=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc),
    )
    message = refusal.user_message
    assert "30" in message
    assert "00:00 UTC" in message
    assert "05 Sep 2026" in message
    # And it says what still works, because "you have hit a limit" with no scope
    # reads as "the product is down".
    assert "knowledge base" in message


def test_the_retry_header_and_the_message_name_the_same_instant():
    """A client sleeping on Retry-After must wake when the sentence says it will."""
    reset = datetime.now(timezone.utc) + timedelta(seconds=90)
    refusal = cap.TenantTurnCapExceeded(
        organization_id=PERSONAL_ORG, limit=1, used=1, reset_at=reset
    )
    assert 89 <= refusal.retry_after_seconds <= 92


def test_a_reset_already_past_still_asks_for_a_positive_wait():
    """``Retry-After: 0`` invites an immediate retry loop; the floor is one second."""
    refusal = cap.TenantTurnCapExceeded(
        organization_id=PERSONAL_ORG,
        limit=1,
        used=1,
        reset_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert refusal.retry_after_seconds >= 1


# =============================================================================
# Which tenants are capped
# =============================================================================


async def test_a_personal_tenant_with_no_override_takes_the_deployment_default(factory):
    async with factory() as session:
        policy = await cap.resolve_policy(session, PERSONAL_ORG)
    assert policy.limit == 30
    assert policy.source == "default_personal"


async def test_a_company_organization_with_no_override_is_uncapped(factory):
    """Invariant 2. The cap bounds self-service sign-up, not customers."""
    async with factory() as session:
        policy = await cap.resolve_policy(session, COMPANY_ORG)
    assert policy.limit is None
    assert policy.source == "company_uncapped"


async def test_a_company_tenant_is_never_refused_however_many_turns_it_takes(factory):
    """Invariant 2, at the reservation rather than at the policy.

    Well past the personal default, so a regression that applied the default to
    every tenant would be caught rather than merely made more likely.
    """
    for expected in range(1, 41):
        reservation = await cap.reserve_turn(COMPANY_ORG)
        assert reservation.used == expected
        assert reservation.limit is None
    # And the usage is still recorded, because the default is tuned against what
    # ordinary tenants actually do.
    assert await _ledger(factory, COMPANY_ORG) == 40


async def test_an_override_caps_a_company_organization(factory):
    await _set_override(factory, COMPANY_ORG, 2)
    await cap.reserve_turn(COMPANY_ORG)
    await cap.reserve_turn(COMPANY_ORG)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(COMPANY_ORG)


async def test_an_override_of_zero_means_uncapped(factory):
    await _set_override(factory, PERSONAL_ORG, cap.UNLIMITED_OVERRIDE)
    async with factory() as session:
        policy = await cap.resolve_policy(session, PERSONAL_ORG)
    assert policy.limit is None
    assert policy.source == "override_unlimited"


async def test_clearing_the_override_returns_the_tenant_to_the_policy(factory):
    """``--clear`` and ``--unlimited`` are different actions on a personal tenant."""
    await _set_override(factory, PERSONAL_ORG, 500)
    async with factory() as session:
        assert (await cap.resolve_policy(session, PERSONAL_ORG)).limit == 500
    await _set_override(factory, PERSONAL_ORG, None)
    async with factory() as session:
        policy = await cap.resolve_policy(session, PERSONAL_ORG)
    assert policy.limit == 30
    assert policy.source == "default_personal"


async def test_an_override_moves_only_the_tenant_it_names(factory):
    """Invariant 3, the isolation half."""
    await _set_override(factory, PERSONAL_ORG, 1)
    await cap.reserve_turn(PERSONAL_ORG)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(PERSONAL_ORG)

    # The sibling personal tenant is untouched and still on the default.
    for _ in range(5):
        await cap.reserve_turn(OTHER_PERSONAL_ORG)
    assert await _ledger(factory, OTHER_PERSONAL_ORG) == 5


async def test_an_override_takes_effect_on_the_next_turn_with_no_restart(factory):
    """Invariant 3, the liveness half.

    The override is read from the row on every turn rather than cached in the
    process, so an operator raising a tenant's cap does not need a redeploy —
    and this asserts it inside one running process, which a restart-based test
    could not distinguish from a cached value being rebuilt.
    """
    await _set_override(factory, PERSONAL_ORG, 1)
    await cap.reserve_turn(PERSONAL_ORG)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(PERSONAL_ORG)

    await _set_override(factory, PERSONAL_ORG, 3)

    reservation = await cap.reserve_turn(PERSONAL_ORG)
    assert reservation.used == 2
    assert reservation.limit == 3


async def test_a_cap_lowered_below_the_standing_count_refuses_and_reports_the_truth(
    factory,
):
    """The log must not rename an over-limit day as an exactly-at-limit one."""
    await _set_override(factory, PERSONAL_ORG, 5)
    for _ in range(5):
        await cap.reserve_turn(PERSONAL_ORG)
    await _set_override(factory, PERSONAL_ORG, 2)

    with pytest.raises(cap.TenantTurnCapExceeded) as raised:
        await cap.reserve_turn(PERSONAL_ORG)
    assert raised.value.limit == 2
    assert raised.value.used == 5


# =============================================================================
# The default is a setting somebody can actually change
# =============================================================================


def test_the_default_is_named_by_the_environment_variable_operators_set():
    """A field whose declared alias drifts from the documented name is unreachable."""
    from faultmaven.config.settings import AgentSettings

    field = AgentSettings.model_fields["tenant_daily_turn_cap"]
    assert field.validation_alias == "TENANT_DAILY_TURN_CAP"
    assert field.default == 30


async def test_the_default_reaches_the_enforcement(monkeypatch, factory):
    """Through the real settings singleton, not a patched module predicate.

    Patching ``_default_limit`` would leave the wiring — env var → field →
    ``get_settings().agent`` → this call — untested, which is the failure mode
    this repo has been bitten by.
    """
    from faultmaven.config.settings import reset_settings

    monkeypatch.setenv("TENANT_DAILY_TURN_CAP", "3")
    reset_settings()
    try:
        for _ in range(3):
            await cap.reserve_turn(PERSONAL_ORG)
        with pytest.raises(cap.TenantTurnCapExceeded) as raised:
            await cap.reserve_turn(PERSONAL_ORG)
        assert raised.value.limit == 3
    finally:
        monkeypatch.delenv("TENANT_DAILY_TURN_CAP", raising=False)
        reset_settings()


# =============================================================================
# Counting
# =============================================================================


async def test_a_refused_turn_consumes_nothing(factory):
    """Invariant 1's sharpest half.

    A cap that charged for its own refusals would be indistinguishable from one
    that worked, right up until an operator raised it and found the day already
    spent.
    """
    await _set_override(factory, PERSONAL_ORG, 2)
    await cap.reserve_turn(PERSONAL_ORG)
    await cap.reserve_turn(PERSONAL_ORG)
    assert await _ledger(factory, PERSONAL_ORG) == 2

    for _ in range(5):
        with pytest.raises(cap.TenantTurnCapExceeded):
            await cap.reserve_turn(PERSONAL_ORG)

    assert await _ledger(factory, PERSONAL_ORG) == 2


async def test_counting_is_per_organization(factory):
    """Invariant 1. One tenant's usage must not spend another's allowance."""
    await cap.reserve_turn(PERSONAL_ORG)
    await cap.reserve_turn(PERSONAL_ORG)
    await cap.reserve_turn(OTHER_PERSONAL_ORG)

    assert await _ledger(factory, PERSONAL_ORG) == 2
    assert await _ledger(factory, OTHER_PERSONAL_ORG) == 1


async def test_counting_is_per_utc_day(factory):
    """Invariant 1. Yesterday's turns must not be charged against today."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    await _set_override(factory, PERSONAL_ORG, 2)

    await cap.reserve_turn(PERSONAL_ORG, now=yesterday)
    await cap.reserve_turn(PERSONAL_ORG, now=yesterday)
    with pytest.raises(cap.TenantTurnCapExceeded):
        await cap.reserve_turn(PERSONAL_ORG, now=yesterday)

    # A new UTC day is a new allowance, in its own row.
    today = await cap.reserve_turn(PERSONAL_ORG)
    assert today.used == 1
    assert await _ledger(factory, PERSONAL_ORG, cap.utc_day(yesterday)) == 2
    assert await _ledger(factory, PERSONAL_ORG) == 1


# =============================================================================
# Failure direction
# =============================================================================


async def test_an_indeterminate_tenant_kind_is_capped_at_the_default(
    monkeypatch, factory
):
    """Invariant 5. Unknown means personal, not unlimited.

    Staged as the live shape of the branch: the personal-tenant lookup fails
    (a half-migrated deployment where ``sso_personal_orgs`` is not there yet).
    The other direction would hand every tenant an uncapped day the moment that
    table became unreadable, which is exactly the state a partial rollout is in.
    """
    async with factory() as session:
        await session.execute(text("DROP TABLE sso_personal_orgs"))
        await session.commit()

    # A COMPANY organization, which is uncapped when the question is answerable.
    async with factory() as session:
        policy = await cap.resolve_policy(session, COMPANY_ORG)
    assert policy.limit == 30, "an unanswerable kind must fall back to the default"
    assert policy.source == "indeterminate"


async def test_a_ledger_failure_refuses_rather_than_serving_uncounted(factory):
    """Invariant 5's other half: a cap that cannot be applied refuses the turn."""
    async with factory() as session:
        await session.execute(text("DROP TABLE organization_turn_usage"))
        await session.commit()

    with pytest.raises(cap.TenantTurnCapUnavailable):
        await cap.reserve_turn(PERSONAL_ORG)


async def test_the_two_refusals_are_distinguishable(factory):
    """They are not the same event and must not render as the same message.

    Telling somebody their daily allowance is spent when the ledger merely
    failed to write is a false statement about their own account, and it sends
    them away until midnight for a fault that may clear in seconds.
    """
    assert issubclass(cap.TenantTurnCapExceeded, cap.TenantTurnCapError)
    assert issubclass(cap.TenantTurnCapUnavailable, cap.TenantTurnCapError)
    assert not issubclass(cap.TenantTurnCapUnavailable, cap.TenantTurnCapExceeded)

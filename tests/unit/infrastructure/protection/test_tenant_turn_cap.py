"""The turn cap's decision, through its ports (ADR-016 D5.3).

The mechanism is small and every rule in it is a claim someone will rely on, so
each is pinned separately: which tenants are capped, what an override does,
which direction each ambiguity falls in, and — the one that makes it a cap
rather than a suggestion — that a refused turn consumes nothing.

Driven through the real :class:`CapPolicyResolver` and the real
:class:`TurnCapService` against fakes of the two ports and the in-memory ledger.
That is the point of the ledger being a port: these cases exercise the
enforcement rather than a stand-in for it, and they need no database to do it.
The properties that only a real database can answer — the atomic reservation
under concurrency, and RLS — are asserted against PostgreSQL in
``tests/integration/security/test_tenant_turn_cap.py``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from faultmaven.infrastructure.protection import tenant_turn_cap as cap
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    InMemoryTurnLedger,
    TenantTurnCapExceeded,
    TenantTurnCapUnavailable,
    TurnCapService,
)

pytestmark = pytest.mark.unit

PERSONAL = "org-personal"
OTHER_PERSONAL = "org-personal-2"
COMPANY = "org-company"


class FakePersonalOrgs:
    """The one question the resolver asks of the auth port."""

    def __init__(self, personal=(), error: Exception | None = None):
        self.personal = set(personal)
        self.error = error
        self.asked: list[str] = []

    async def is_personal_organization(self, organization_id):
        self.asked.append(organization_id)
        if self.error is not None:
            raise self.error
        return organization_id in self.personal


class FakeOrganizations:
    """The one question the resolver asks of the organization repository."""

    def __init__(self, caps=None, error: Exception | None = None):
        self.caps = dict(caps or {})
        self.error = error
        self.asked: list[str] = []

    async def get_organization(self, organization_id):
        self.asked.append(organization_id)
        if self.error is not None:
            raise self.error
        if organization_id not in self.caps:
            return None
        return SimpleNamespace(
            organization_id=organization_id,
            daily_turn_cap=self.caps[organization_id],
        )


def _service(
    *,
    personal=(PERSONAL, OTHER_PERSONAL),
    caps=None,
    multi=True,
    personal_error=None,
    org_error=None,
    ledger=None,
    default=30,
):
    orgs = FakeOrganizations(
        caps={PERSONAL: None, OTHER_PERSONAL: None, COMPANY: None, **(caps or {})},
        error=org_error,
    )
    people = FakePersonalOrgs(personal, error=personal_error)
    resolver = CapPolicyResolver(
        people,
        orgs,
        default_limit=lambda: default,
        multi_tenant=lambda: multi,
    )
    return (
        TurnCapService(resolver, ledger or InMemoryTurnLedger()),
        resolver,
        people,
        orgs,
    )


# =============================================================================
# The day boundary the refusal message promises
# =============================================================================


def test_the_charged_day_is_the_utc_calendar_day():
    """A local-midnight boundary would reset the wrong tenants at the wrong time."""
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
    reset = cap.next_utc_midnight(moment)
    assert reset > moment
    assert reset - moment <= timedelta(days=1)
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)


async def test_the_charged_day_and_the_reset_come_from_one_clock_reading():
    """One ``now`` per reservation, or the refusal can name the wrong midnight.

    Sampled twice, a turn refused at 23:59:59.98 is charged to day D by the
    first reading and told to come back at the midnight after D+1 by the second
    — a whole extra day's wait produced by nothing but the clock ticking
    between two calls. The service therefore takes ``now`` once; this drives it
    at the boundary and asserts the two agree.
    """
    service, *_ = _service(caps={PERSONAL: 1})
    # Deliberately far from the wall clock. An earlier version of this case used
    # a date near "today", and a mutation replacing ``next_utc_midnight(moment)``
    # with ``next_utc_midnight()`` still passed — the real clock happened to
    # land on the same day. The instant under test has to be one the ambient
    # clock cannot coincide with, or the assertion is about nothing.
    edge = datetime(2031, 3, 17, 23, 59, 59, 980000, tzinfo=timezone.utc)

    await service.reserve(PERSONAL, now=edge)
    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(PERSONAL, now=edge)

    # Charged to the 17th, so the allowance returns at the midnight opening the
    # 18th — derived from the SAME reading, not from a second one.
    assert raised.value.reset_at == datetime(2031, 3, 18, tzinfo=timezone.utc)


def test_the_message_names_the_limit_and_when_it_comes_back():
    refusal = TenantTurnCapExceeded(
        organization_id=PERSONAL,
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


def test_the_wait_goes_through_the_shared_window_math():
    """One rounding rule in the codebase, not two that can disagree."""
    reset = datetime.now(timezone.utc) + timedelta(seconds=90)
    refusal = TenantTurnCapExceeded(
        organization_id=PERSONAL, limit=1, used=1, reset_at=reset
    )
    assert 89 <= refusal.retry_after_seconds <= 91


def test_a_reset_already_past_still_asks_for_a_positive_wait():
    """``Retry-After: 0`` invites an immediate retry loop; the floor is one."""
    refusal = TenantTurnCapExceeded(
        organization_id=PERSONAL,
        limit=1,
        used=1,
        reset_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert refusal.retry_after_seconds >= 1


# =============================================================================
# Which tenants are capped
# =============================================================================


async def test_single_tenant_is_uncapped_without_touching_a_port():
    """A self-hosted install pays for its own compute — and may not be migrated.

    The assertion that matters is not just "uncapped": it is that **neither
    port was asked and the ledger was never written**. A deployment running
    with ``RUN_STARTUP_MIGRATIONS=false`` has no ledger table, and a policy that
    queried before deciding would lose every turn to a 503 about a usage
    allowance it could never have earned.
    """
    ledger = InMemoryTurnLedger()
    service, _, people, orgs = _service(multi=False, ledger=ledger)

    for _ in range(100):
        reservation = await service.reserve(PERSONAL)
        assert reservation.limit is None
        assert reservation.source == "single_tenant"

    assert people.asked == []
    assert orgs.asked == []
    assert await ledger.usage(PERSONAL, cap.utc_day()) == 0


async def test_a_personal_tenant_takes_the_deployment_default():
    _, resolver, *_ = _service()
    policy = await resolver.resolve(PERSONAL)
    assert policy.limit == 30
    assert policy.source == "default_personal"


async def test_a_company_organization_with_no_override_is_uncapped():
    """Invariant 2. The cap bounds self-service sign-up, not customers."""
    _, resolver, *_ = _service()
    policy = await resolver.resolve(COMPANY)
    assert policy.limit is None
    assert policy.source == "company_uncapped"


async def test_a_company_tenant_is_never_refused_however_many_turns_it_takes():
    """Well past the personal default, so applying it to everyone fails here."""
    service, _, _, _ = _service()
    ledger_service = service
    for expected in range(1, 41):
        reservation = await ledger_service.reserve(COMPANY)
        assert reservation.used == expected
        assert reservation.limit is None


async def test_an_override_caps_a_company_organization():
    service, *_ = _service(caps={COMPANY: 2})
    await service.reserve(COMPANY)
    await service.reserve(COMPANY)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(COMPANY)


async def test_an_override_of_zero_means_uncapped():
    _, resolver, *_ = _service(caps={PERSONAL: cap.UNLIMITED_OVERRIDE})
    policy = await resolver.resolve(PERSONAL)
    assert policy.limit is None
    assert policy.source == "override_unlimited"


async def test_an_override_moves_only_the_tenant_it_names():
    service, *_ = _service(caps={PERSONAL: 1})
    await service.reserve(PERSONAL)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(PERSONAL)

    for _ in range(5):
        await service.reserve(OTHER_PERSONAL)


async def test_an_override_takes_effect_on_the_next_turn_with_no_restart():
    """The override is read from the port on every turn, not cached."""
    service, _, _, orgs = _service(caps={PERSONAL: 1})
    await service.reserve(PERSONAL)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(PERSONAL)

    orgs.caps[PERSONAL] = 3

    reservation = await service.reserve(PERSONAL)
    assert reservation.used == 2
    assert reservation.limit == 3


async def test_a_cap_lowered_below_the_standing_count_reports_the_truth():
    """The log must not rename an over-limit day as an exactly-at-limit one."""
    service, _, _, orgs = _service(caps={PERSONAL: 5})
    for _ in range(5):
        await service.reserve(PERSONAL)
    orgs.caps[PERSONAL] = 2

    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(PERSONAL)
    assert raised.value.limit == 2
    assert raised.value.used == 5


# =============================================================================
# The default is a setting somebody can actually change
# =============================================================================


def test_the_default_is_named_by_the_environment_variable_operators_set():
    from faultmaven.config.settings import AgentSettings

    field = AgentSettings.model_fields["tenant_daily_turn_cap"]
    assert field.validation_alias == "TENANT_DAILY_TURN_CAP"
    assert field.default == 30


async def test_the_default_reaches_the_enforcement(monkeypatch):
    """Through the real settings singleton, not a patched module predicate."""
    from faultmaven.config.settings import reset_settings

    monkeypatch.setenv("TENANT_DAILY_TURN_CAP", "3")
    reset_settings()
    try:
        resolver = CapPolicyResolver(
            FakePersonalOrgs([PERSONAL]),
            FakeOrganizations({PERSONAL: None}),
            multi_tenant=lambda: True,
        )
        service = TurnCapService(resolver, InMemoryTurnLedger())
        for _ in range(3):
            await service.reserve(PERSONAL)
        with pytest.raises(TenantTurnCapExceeded) as raised:
            await service.reserve(PERSONAL)
        assert raised.value.limit == 3
    finally:
        monkeypatch.delenv("TENANT_DAILY_TURN_CAP", raising=False)
        reset_settings()


# =============================================================================
# Counting
# =============================================================================


async def test_a_refused_turn_consumes_nothing():
    """A cap that charged for its own refusals would be indistinguishable from
    one that worked, right up until an operator raised it and found the day
    already spent."""
    ledger = InMemoryTurnLedger()
    service, *_ = _service(caps={PERSONAL: 2}, ledger=ledger)
    await service.reserve(PERSONAL)
    await service.reserve(PERSONAL)
    assert await ledger.usage(PERSONAL, cap.utc_day()) == 2

    for _ in range(5):
        with pytest.raises(TenantTurnCapExceeded):
            await service.reserve(PERSONAL)

    assert await ledger.usage(PERSONAL, cap.utc_day()) == 2


async def test_counting_is_per_organization():
    ledger = InMemoryTurnLedger()
    service, *_ = _service(ledger=ledger)
    await service.reserve(PERSONAL)
    await service.reserve(PERSONAL)
    await service.reserve(OTHER_PERSONAL)

    today = cap.utc_day()
    assert await ledger.usage(PERSONAL, today) == 2
    assert await ledger.usage(OTHER_PERSONAL, today) == 1


async def test_counting_is_per_utc_day():
    ledger = InMemoryTurnLedger()
    service, *_ = _service(caps={PERSONAL: 2}, ledger=ledger)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)

    await service.reserve(PERSONAL, now=yesterday)
    await service.reserve(PERSONAL, now=yesterday)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(PERSONAL, now=yesterday)

    today = await service.reserve(PERSONAL)
    assert today.used == 1
    assert await ledger.usage(PERSONAL, cap.utc_day(yesterday)) == 2
    assert await ledger.usage(PERSONAL, cap.utc_day()) == 1


# =============================================================================
# Failure direction
# =============================================================================


async def test_an_unreadable_tenant_kind_is_capped_at_the_default():
    """Unknown means personal, not unlimited.

    Asked of a COMPANY organization — uncapped when the question is answerable
    — so a pass can only come from the fail-closed branch.
    """
    _, resolver, *_ = _service(personal_error=RuntimeError("relation does not exist"))
    policy = await resolver.resolve(COMPANY)
    assert policy.limit == 30
    assert policy.source == "indeterminate"


async def test_an_unreadable_override_is_the_default_not_no_override():
    """The inversion the review caught, and the reason this branch exists.

    Reading a failed override lookup as "no override" drops a COMPANY tenant —
    which is uncapped without one — straight to uncapped, so the failure is
    invisible. Worse, a company tenant carrying an explicit cap of 50 would be
    un-capped by a transient read error. The direction is: unreadable ⇒ the
    default cap.
    """
    _, resolver, *_ = _service(org_error=RuntimeError("connection reset"))
    policy = await resolver.resolve(COMPANY)
    assert policy.limit == 30
    assert policy.source == "indeterminate"


async def test_a_ledger_failure_refuses_rather_than_serving_uncounted():
    class BrokenLedger(InMemoryTurnLedger):
        async def reserve(self, organization_id, day, limit):
            raise RuntimeError("no such table: organization_turn_usage")

    service, *_ = _service(ledger=BrokenLedger())
    with pytest.raises(TenantTurnCapUnavailable):
        await service.reserve(PERSONAL)


async def test_a_multi_tenant_request_with_no_bound_tenant_is_capped():
    """This must not be the place that decides an unscoped request is free."""
    _, resolver, *_ = _service()
    policy = await resolver.resolve("")
    assert policy.limit == 30
    assert policy.source == "indeterminate"


def test_the_two_refusals_are_distinguishable():
    """They are not the same event and must not render as the same message."""
    assert issubclass(TenantTurnCapExceeded, cap.TenantTurnCapError)
    assert issubclass(TenantTurnCapUnavailable, cap.TenantTurnCapError)
    assert not issubclass(TenantTurnCapUnavailable, TenantTurnCapExceeded)


async def test_the_refusal_is_logged_with_the_organization_and_the_count(caplog):
    """The operator's next action — raise this cap, or leave it — needs both."""
    service, *_ = _service(caps={PERSONAL: 2})
    await service.reserve(PERSONAL)
    await service.reserve(PERSONAL)

    with caplog.at_level(logging.INFO, logger=cap.logger.name):
        with pytest.raises(TenantTurnCapExceeded):
            await service.reserve(PERSONAL)

    lines = [record.getMessage() for record in caplog.records]
    named = [line for line in lines if PERSONAL in line]
    assert named, f"no log line names the organization: {lines}"
    assert any("2/2" in line for line in named), named
    assert any("override" in line for line in named), named


# =============================================================================
# The in-memory ledger is the same contract as the SQL one
# =============================================================================


async def test_the_in_memory_ledger_refuses_without_writing():
    """Otherwise the unit cases above would be proving a laxer contract."""
    ledger = InMemoryTurnLedger()
    day = cap.utc_day()
    assert await ledger.reserve(PERSONAL, day, 1) == 1
    assert await ledger.reserve(PERSONAL, day, 1) is None
    assert await ledger.usage(PERSONAL, day) == 1


async def test_the_in_memory_ledger_counts_without_a_ceiling():
    ledger = InMemoryTurnLedger()
    day = cap.utc_day()
    for expected in range(1, 6):
        assert await ledger.reserve(COMPANY, day, None) == expected

"""The turn cap's decision, through its port (ADR-016 D5.3, ADR-017 D5).

The mechanism is small and every rule in it is a claim someone will rely on, so
each is pinned separately: which subjects are capped, what an override does,
which direction each ambiguity falls in, and — the one that makes it a cap
rather than a suggestion — that a refused turn consumes nothing.

**What a turn is charged to is a billing subject**, not a tenant: the
organization when the account has one, and the account itself when it has none.
That is the whole of what "personal" means under ADR-017 D5 — an account nobody
pays for — so the kind question needs no table and, unlike the
``sso_personal_orgs`` lookup it replaces, has no unreadable branch that could
invert the policy. The enterprise is deliberately not the subject: it isolates
and does not pay, and metering it would give two departments of one company one
allowance they never agreed to share.

Driven through the real :class:`CapPolicyResolver` and the real
:class:`TurnCapService` against a fake of the one remaining port and the
in-memory ledger. That is the point of the ledger being a port: these cases
exercise the enforcement rather than a stand-in for it, and they need no
database to do it. The properties that only a real database can answer — the
atomic reservation under concurrency, and RLS — are asserted against PostgreSQL
in ``tests/integration/security/test_tenant_turn_cap.py``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from faultmaven.infrastructure.protection import tenant_turn_cap as cap
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    SUBJECT_ACCOUNT,
    SUBJECT_ORGANIZATION,
    BillingSubject,
    CapPolicyResolver,
    InMemoryTurnLedger,
    TenantTurnCapExceeded,
    TenantTurnCapUnavailable,
    TurnCapService,
    billing_subject_for,
)

pytestmark = pytest.mark.unit

#: An account nobody pays for — the ADR-017 D5 replacement for "a personal
#: organization". No row anywhere describes it; being in no organization IS the
#: description.
ACCOUNT = BillingSubject(SUBJECT_ACCOUNT, "user-alice")
OTHER_ACCOUNT = BillingSubject(SUBJECT_ACCOUNT, "user-bob")
COMPANY = BillingSubject(SUBJECT_ORGANIZATION, "org-company")
OTHER_COMPANY = BillingSubject(SUBJECT_ORGANIZATION, "org-company-2")


class FakeOrganizations:
    """The one question the resolver still asks of a repository."""

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


def _service(*, caps=None, multi=True, org_error=None, ledger=None, default=30):
    orgs = FakeOrganizations(
        caps={
            COMPANY.subject_id: None,
            OTHER_COMPANY.subject_id: None,
            **(caps or {}),
        },
        error=org_error,
    )
    resolver = CapPolicyResolver(
        orgs,
        default_limit=lambda: default,
        multi_tenant=lambda: multi,
    )
    return TurnCapService(resolver, ledger or InMemoryTurnLedger()), resolver, orgs


# =============================================================================
# Who a turn is charged to
# =============================================================================


def test_the_subject_is_the_organization_when_the_account_has_one():
    """The billing subject, in one function, so no call site derives its own."""
    subject = billing_subject_for("org-acme", "user-alice")
    assert subject == BillingSubject(SUBJECT_ORGANIZATION, "org-acme")
    assert subject.is_account is False


def test_the_subject_is_the_account_when_nobody_pays_for_it():
    """ADR-017 D5's re-statement: "personal" is the absence of an organization.

    Asserted beside the case above rather than alone, because "the account is
    the subject" would also hold if the organization had stopped being read at
    all — which is exactly the failure that would meter a paying customer
    against the self-service allowance.
    """
    subject = billing_subject_for(None, "user-alice")
    assert subject == BillingSubject(SUBJECT_ACCOUNT, "user-alice")
    assert subject.is_account is True
    # An empty string is an absent organization, not an organization named "".
    assert billing_subject_for("", "user-alice") == subject


def test_there_is_no_subject_when_there_is_nobody_to_charge():
    """``None`` rather than a sentinel: the resolver caps it, it does not
    silently become a tenant of its own."""
    assert billing_subject_for(None, None) is None
    assert billing_subject_for("", "") is None


def test_the_two_subject_kinds_are_the_two_the_column_permits():
    """A third kind would be a schema change, not a code one — the
    ``turn_usage.billing_subject_kind`` CHECK spells exactly these two."""
    assert {SUBJECT_ORGANIZATION, SUBJECT_ACCOUNT} == {"organization", "account"}


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
    service, *_ = _service(caps={COMPANY.subject_id: 1})
    # Deliberately far from the wall clock. An earlier version of this case used
    # a date near "today", and a mutation replacing ``next_utc_midnight(moment)``
    # with ``next_utc_midnight()`` still passed — the real clock happened to
    # land on the same day. The instant under test has to be one the ambient
    # clock cannot coincide with, or the assertion is about nothing.
    edge = datetime(2031, 3, 17, 23, 59, 59, 980000, tzinfo=timezone.utc)

    await service.reserve(COMPANY, now=edge)
    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(COMPANY, now=edge)

    # Charged to the 17th, so the allowance returns at the midnight opening the
    # 18th — derived from the SAME reading, not from a second one.
    assert raised.value.reset_at == datetime(2031, 3, 18, tzinfo=timezone.utc)


def test_the_message_names_the_limit_and_when_it_comes_back():
    refusal = TenantTurnCapExceeded(
        subject=ACCOUNT,
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
    refusal = TenantTurnCapExceeded(subject=ACCOUNT, limit=1, used=1, reset_at=reset)
    assert 89 <= refusal.retry_after_seconds <= 91


def test_a_reset_already_past_still_asks_for_a_positive_wait():
    """``Retry-After: 0`` invites an immediate retry loop; the floor is one."""
    refusal = TenantTurnCapExceeded(
        subject=ACCOUNT,
        limit=1,
        used=1,
        reset_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert refusal.retry_after_seconds >= 1


# =============================================================================
# Which subjects are capped
# =============================================================================


async def test_single_tenant_is_uncapped_without_touching_a_port():
    """A self-hosted install pays for its own compute — and may not be migrated.

    The assertion that matters is not just "uncapped": it is that **the port was
    never asked and the ledger was never written**. A deployment running with
    ``RUN_STARTUP_MIGRATIONS=false`` has no ledger table, and a policy that
    queried before deciding would lose every turn to a 503 about a usage
    allowance it could never have earned.
    """
    ledger = InMemoryTurnLedger()
    service, _, orgs = _service(multi=False, ledger=ledger)

    for _ in range(100):
        reservation = await service.reserve(ACCOUNT)
        assert reservation.limit is None
        assert reservation.source == "single_tenant"

    assert orgs.asked == []
    assert await ledger.usage(ACCOUNT, cap.utc_day()) == 0


async def test_an_account_subject_takes_the_deployment_default():
    """And answers it without a lookup: there is no row that could fail to read.

    ``orgs.asked == []`` is the half that distinguishes this from the mechanism
    it replaces. ``sso_personal_orgs`` had to be consulted to learn that a
    tenant was personal, so an unreadable table inverted the answer; "in no
    organization" is carried by the subject itself.
    """
    _, resolver, orgs = _service()
    policy = await resolver.resolve(ACCOUNT)
    assert policy.limit == 30
    assert policy.source == "default_personal"
    assert orgs.asked == []


async def test_a_company_organization_with_no_override_is_uncapped():
    """The cap bounds self-service sign-up, not customers."""
    _, resolver, *_ = _service()
    policy = await resolver.resolve(COMPANY)
    assert policy.limit is None
    assert policy.source == "company_uncapped"


async def test_an_account_is_capped_where_a_company_is_not_on_the_same_run():
    """Both directions together: "the account is capped" alone would also hold
    if the resolver had stopped distinguishing them and capped everything."""
    service, *_ = _service(default=2)

    await service.reserve(ACCOUNT)
    await service.reserve(ACCOUNT)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(ACCOUNT)

    for expected in range(1, 21):
        assert (await service.reserve(COMPANY)).used == expected


async def test_a_company_tenant_is_never_refused_however_many_turns_it_takes():
    """Well past the personal default, so applying it to everyone fails here."""
    service, *_ = _service()
    for expected in range(1, 41):
        reservation = await service.reserve(COMPANY)
        assert reservation.used == expected
        assert reservation.limit is None


async def test_an_override_caps_a_company_organization():
    service, *_ = _service(caps={COMPANY.subject_id: 2})
    await service.reserve(COMPANY)
    await service.reserve(COMPANY)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(COMPANY)


async def test_an_override_of_zero_means_uncapped():
    _, resolver, *_ = _service(caps={COMPANY.subject_id: cap.UNLIMITED_OVERRIDE})
    policy = await resolver.resolve(COMPANY)
    assert policy.limit is None
    assert policy.source == "override_unlimited"


async def test_an_override_moves_only_the_subject_it_names():
    service, *_ = _service(caps={COMPANY.subject_id: 1})
    await service.reserve(COMPANY)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(COMPANY)

    for _ in range(5):
        await service.reserve(OTHER_COMPANY)


async def test_an_override_takes_effect_on_the_next_turn_with_no_restart():
    """The override is read from the port on every turn, not cached."""
    service, _, orgs = _service(caps={COMPANY.subject_id: 1})
    await service.reserve(COMPANY)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(COMPANY)

    orgs.caps[COMPANY.subject_id] = 3

    reservation = await service.reserve(COMPANY)
    assert reservation.used == 2
    assert reservation.limit == 3


async def test_a_cap_lowered_below_the_standing_count_reports_the_truth():
    """The log must not rename an over-limit day as an exactly-at-limit one."""
    service, _, orgs = _service(caps={COMPANY.subject_id: 5})
    for _ in range(5):
        await service.reserve(COMPANY)
    orgs.caps[COMPANY.subject_id] = 2

    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(COMPANY)
    assert raised.value.limit == 2
    assert raised.value.used == 5


async def test_an_organizations_override_does_not_reach_an_account_of_the_same_id():
    """The kind is part of the key, not decoration.

    An account and an organization could in principle carry the same id string,
    and if the kind were dropped from the lookup an operator's override on one
    would silently govern the other — and their ledgers would merge.
    """
    ledger = InMemoryTurnLedger()
    shared_id = "same-id"
    service, resolver, _ = _service(caps={shared_id: 1}, ledger=ledger)
    organization = BillingSubject(SUBJECT_ORGANIZATION, shared_id)
    account = BillingSubject(SUBJECT_ACCOUNT, shared_id)

    assert (await resolver.resolve(organization)).limit == 1
    assert (await resolver.resolve(account)).limit == 30

    await service.reserve(organization)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(organization)

    day = cap.utc_day()
    assert await ledger.usage(organization, day) == 1
    assert await ledger.usage(account, day) == 0
    await service.reserve(account)
    assert await ledger.usage(organization, day) == 1


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
            FakeOrganizations({COMPANY.subject_id: None}),
            multi_tenant=lambda: True,
        )
        service = TurnCapService(resolver, InMemoryTurnLedger())
        for _ in range(3):
            await service.reserve(ACCOUNT)
        with pytest.raises(TenantTurnCapExceeded) as raised:
            await service.reserve(ACCOUNT)
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
    service, *_ = _service(default=2, ledger=ledger)
    await service.reserve(ACCOUNT)
    await service.reserve(ACCOUNT)
    assert await ledger.usage(ACCOUNT, cap.utc_day()) == 2

    for _ in range(5):
        with pytest.raises(TenantTurnCapExceeded):
            await service.reserve(ACCOUNT)

    assert await ledger.usage(ACCOUNT, cap.utc_day()) == 2


async def test_counting_is_per_billing_subject():
    ledger = InMemoryTurnLedger()
    service, *_ = _service(ledger=ledger)
    await service.reserve(ACCOUNT)
    await service.reserve(ACCOUNT)
    await service.reserve(OTHER_ACCOUNT)
    await service.reserve(COMPANY)

    today = cap.utc_day()
    assert await ledger.usage(ACCOUNT, today) == 2
    assert await ledger.usage(OTHER_ACCOUNT, today) == 1
    assert await ledger.usage(COMPANY, today) == 1


async def test_counting_is_per_utc_day():
    ledger = InMemoryTurnLedger()
    service, *_ = _service(default=2, ledger=ledger)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)

    await service.reserve(ACCOUNT, now=yesterday)
    await service.reserve(ACCOUNT, now=yesterday)
    with pytest.raises(TenantTurnCapExceeded):
        await service.reserve(ACCOUNT, now=yesterday)

    today = await service.reserve(ACCOUNT)
    assert today.used == 1
    assert await ledger.usage(ACCOUNT, cap.utc_day(yesterday)) == 2
    assert await ledger.usage(ACCOUNT, cap.utc_day()) == 1


# =============================================================================
# Failure direction
# =============================================================================


async def test_an_unreadable_override_is_the_default_not_no_override():
    """The inversion the review caught, and the reason this branch exists.

    Reading a failed override lookup as "no override" drops a COMPANY subject —
    which is uncapped without one — straight to uncapped, so the failure is
    invisible. Worse, a company carrying an explicit cap of 50 would be
    un-capped by a transient read error. The direction is: unreadable ⇒ the
    default cap.

    Asked of a COMPANY, which is uncapped when the question is answerable, so a
    pass can only come from the fail-closed branch.
    """
    _, resolver, *_ = _service(org_error=RuntimeError("connection reset"))
    policy = await resolver.resolve(COMPANY)
    assert policy.limit == 30
    assert policy.source == "indeterminate"


async def test_an_unreadable_override_still_refuses_at_the_default(caplog):
    """End to end, not only at the resolver: the fail-closed policy has to reach
    the enforcement, or it is a value nothing acts on."""
    service, *_ = _service(org_error=RuntimeError("connection reset"), default=2)
    await service.reserve(COMPANY)
    await service.reserve(COMPANY)
    with pytest.raises(TenantTurnCapExceeded) as raised:
        await service.reserve(COMPANY)
    assert raised.value.limit == 2
    assert raised.value.source == "indeterminate"


async def test_a_ledger_failure_refuses_rather_than_serving_uncounted():
    class BrokenLedger(InMemoryTurnLedger):
        async def reserve(self, subject, day, limit):
            raise RuntimeError("no such table: turn_usage")

    service, *_ = _service(ledger=BrokenLedger())
    with pytest.raises(TenantTurnCapUnavailable):
        await service.reserve(ACCOUNT)


async def test_a_multi_tenant_turn_with_no_subject_is_capped_then_refused():
    """This must not be the place that decides an unidentifiable turn is free.

    Two halves, because either alone fails open in a different way: the policy
    caps rather than un-caps, and the service then refuses rather than serving
    the turn uncounted — there is nothing to count it against.
    """
    service, resolver, _ = _service()
    policy = await resolver.resolve(None)
    assert policy.limit == 30
    assert policy.source == "indeterminate"

    with pytest.raises(TenantTurnCapUnavailable):
        await service.reserve(None)


def test_the_two_refusals_are_distinguishable():
    """They are not the same event and must not render as the same message."""
    assert issubclass(TenantTurnCapExceeded, cap.TenantTurnCapError)
    assert issubclass(TenantTurnCapUnavailable, cap.TenantTurnCapError)
    assert not issubclass(TenantTurnCapUnavailable, TenantTurnCapExceeded)


async def test_the_refusal_is_logged_with_the_subject_and_the_count(caplog):
    """The operator's next action — raise this subject's cap, or leave it —
    needs both, and needs to know which KIND of subject it is looking at."""
    service, *_ = _service(caps={COMPANY.subject_id: 2})
    await service.reserve(COMPANY)
    await service.reserve(COMPANY)

    with caplog.at_level(logging.INFO, logger=cap.logger.name):
        with pytest.raises(TenantTurnCapExceeded):
            await service.reserve(COMPANY)

    lines = [record.getMessage() for record in caplog.records]
    named = [line for line in lines if COMPANY.subject_id in line]
    assert named, f"no log line names the subject: {lines}"
    assert any(SUBJECT_ORGANIZATION in line for line in named), named
    assert any("2/2" in line for line in named), named
    assert any("override" in line for line in named), named


# =============================================================================
# The unwired fallback, which must not be a hole
# =============================================================================


async def test_an_unwired_cap_is_uncapped_under_single_tenant(monkeypatch):
    """A service built without a cap must behave like one built with the real
    thing on a deployment the cap does not bound."""
    monkeypatch.setattr(cap, "_is_multi_tenant", lambda: False)
    reservation = await cap.UnconfiguredTurnCap().reserve(ACCOUNT)
    assert reservation.limit is None
    assert reservation.source == "single_tenant"


async def test_an_unwired_cap_refuses_under_multi_tenant(monkeypatch):
    """The only shape where a bill exists. Failing closed turns a wiring mistake
    into a visible refusal rather than an invisible hole."""
    monkeypatch.setattr(cap, "_is_multi_tenant", lambda: True)
    with pytest.raises(TenantTurnCapUnavailable):
        await cap.UnconfiguredTurnCap().reserve(ACCOUNT)


# =============================================================================
# The in-memory ledger is the same contract as the SQL one
# =============================================================================


async def test_the_in_memory_ledger_refuses_without_writing():
    """Otherwise the unit cases above would be proving a laxer contract."""
    ledger = InMemoryTurnLedger()
    day = cap.utc_day()
    assert await ledger.reserve(ACCOUNT, day, 1) == 1
    assert await ledger.reserve(ACCOUNT, day, 1) is None
    assert await ledger.usage(ACCOUNT, day) == 1


async def test_the_in_memory_ledger_counts_without_a_ceiling():
    ledger = InMemoryTurnLedger()
    day = cap.utc_day()
    for expected in range(1, 6):
        assert await ledger.reserve(COMPANY, day, None) == expected

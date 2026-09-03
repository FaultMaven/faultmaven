"""``fm-set-turn-cap`` — the operator's live control over one tenant's cap.

The cap's *default* moves only with a redeploy. A single tenant's cap is a row,
and this command writes it, so "raise or clear a tenant's cap without a
redeploy" is a claim about this file. Three groups of assertions:

* **the three write modes are three different actions.** ``--clear`` returns a
  tenant to the deployment policy; ``--unlimited`` takes the cap off. On a
  company organization they happen to coincide today, which is exactly why they
  must not share a spelling — a later change to the company default would
  silently reinterpret every ``--clear`` an operator meant as "uncapped".
* **the refusals fire before anything is written.** A spend control is not a
  thing to change by accident, and ``--dry-run`` silently winning over ``--yes``
  would exit 0 reading as "the cap moved".
* **what it reports is what the enforcement will decide.** The command renders
  the effective cap through the same three-valued rule
  ``tenant_turn_cap.resolve_policy`` applies, so an operator is not reading a
  second, drifting description of the policy.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.cli import set_turn_cap
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
    OrganizationTurnUsageModel,
    SSOPersonalOrgModel,
)

pytestmark = pytest.mark.unit

ENTERPRISE_ID = "33333333-3333-3333-3333-333333333333"
PERSONAL_ORG = "11111111-1111-1111-1111-111111111111"
COMPANY_ORG = "22222222-2222-2222-2222-222222222222"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
        for org_id, slug in ((PERSONAL_ORG, "personal"), (COMPANY_ORG, "acme-corp")):
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
        session.add(
            SSOPersonalOrgModel(
                provider="workos",
                provider_user_id="user_a",
                organization_id=PERSONAL_ORG,
                provider_org_id="org_a",
                enterprise_id=ENTERPRISE_ID,
                created_at=_now(),
                updated_at=_now(),
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def bind_sessions(monkeypatch, factory):
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


async def _override(factory, organization_id):
    async with factory() as session:
        return (
            await session.execute(
                select(OrganizationModel.daily_turn_cap).where(
                    OrganizationModel.organization_id == organization_id
                )
            )
        ).scalar_one_or_none()


async def _run(**kwargs):
    defaults = {"show_only": False, "dry_run": False, "new_value": None}
    defaults.update(kwargs)
    return await set_turn_cap.set_turn_cap(**defaults)


# =============================================================================
# The three write modes
# =============================================================================


async def test_setting_a_cap_writes_the_override(factory):
    assert await _run(organization_id=PERSONAL_ORG, new_value=200) == 0
    assert await _override(factory, PERSONAL_ORG) == 200


async def test_unlimited_writes_the_explicit_zero(factory):
    """The one spelling of "no cap for this tenant", whatever kind it is."""
    assert await _run(organization_id=PERSONAL_ORG, new_value=0) == 0
    assert await _override(factory, PERSONAL_ORG) == 0

    from faultmaven.infrastructure.protection.tenant_turn_cap import resolve_policy

    async with factory() as session:
        policy = await resolve_policy(session, PERSONAL_ORG)
    assert policy.limit is None


async def test_clear_removes_the_override_and_is_not_unlimited(factory):
    """The distinction that bites on a personal tenant.

    Cleared, it goes back to the deployment default — NOT to uncapped. A command
    that spelled both as one action would silently un-cap every personal tenant
    an operator meant to return to the default.
    """
    from faultmaven.infrastructure.protection.tenant_turn_cap import resolve_policy

    assert await _run(organization_id=PERSONAL_ORG, new_value=500) == 0
    assert await _run(organization_id=PERSONAL_ORG, new_value=None) == 0
    assert await _override(factory, PERSONAL_ORG) is None

    async with factory() as session:
        policy = await resolve_policy(session, PERSONAL_ORG)
    assert policy.limit == 30
    assert policy.source == "default_personal"


async def test_a_dry_run_writes_nothing(factory):
    assert await _run(organization_id=PERSONAL_ORG, new_value=7, dry_run=True) == 0
    assert await _override(factory, PERSONAL_ORG) is None


async def test_show_writes_nothing_even_with_a_value_in_hand(factory):
    assert await _run(organization_id=PERSONAL_ORG, new_value=7, show_only=True) == 0
    assert await _override(factory, PERSONAL_ORG) is None


async def test_an_unknown_organization_is_refused_without_writing(factory):
    assert await _run(organization_id="no-such-org", new_value=5) == 1


# =============================================================================
# What it reports is what the enforcement decides
# =============================================================================


async def test_it_reports_the_effective_cap_and_todays_usage(factory, capsys):
    """An operator reading this must see the number the next turn will meet."""
    from faultmaven.infrastructure.protection.tenant_turn_cap import utc_day

    async with factory() as session:
        session.add(
            OrganizationTurnUsageModel(
                organization_id=PERSONAL_ORG, usage_date=utc_day(), turn_count=17
            )
        )
        await session.commit()

    await _run(organization_id=PERSONAL_ORG, show_only=True)
    out = capsys.readouterr().out

    assert "personal tenant" in out
    assert "30 turns/day" in out
    assert "17 turns" in out


async def test_a_company_organization_reads_as_uncapped(factory, capsys):
    await _run(organization_id=COMPANY_ORG, show_only=True)
    out = capsys.readouterr().out
    assert "company" in out
    assert "uncapped" in out


@pytest.mark.parametrize(
    "override,is_personal,expected",
    [
        (None, True, "30 turns/day"),
        (None, False, "uncapped"),
        (0, True, "uncapped"),
        (0, False, "uncapped"),
        (7, True, "7 turns/day"),
        (7, False, "7 turns/day"),
    ],
)
def test_the_rendering_covers_every_cell_of_the_policy_table(
    override, is_personal, expected
):
    """Six states, six renderings, so no cell can go unworded."""
    assert expected in set_turn_cap._describe(override, is_personal, 30)


# =============================================================================
# The refusals, before anything is written
# =============================================================================


def _parse(argv, monkeypatch):
    """Run ``main()`` with ``argv`` and return the exit code it chose."""
    import sys

    monkeypatch.setattr(sys, "argv", ["fm-set-turn-cap", *argv])
    with pytest.raises(SystemExit) as raised:
        set_turn_cap.main()
    return raised.value.code


def test_a_write_without_yes_is_refused(monkeypatch):
    """This changes what a tenant is allowed to spend; it needs a decision."""
    assert _parse(["--organization-id", PERSONAL_ORG, "--cap", "5"], monkeypatch) == 1


def test_dry_run_and_yes_together_is_a_usage_error(monkeypatch):
    """Not a preference. Silently taking the dry-run branch would exit 0.

    The two invocations differ by one flag, so an operator editing the previous
    command can end up with both — and reading "the cap moved" off a run that
    wrote nothing is the whole failure.
    """
    assert (
        _parse(
            ["--organization-id", PERSONAL_ORG, "--cap", "5", "--dry-run", "--yes"],
            monkeypatch,
        )
        == 2
    )


def test_a_cap_of_zero_is_refused_and_names_the_flag_that_means_it(monkeypatch, capsys):
    """Zero has its own spelling; accepting it here would give it two."""
    assert (
        _parse(["--organization-id", PERSONAL_ORG, "--cap", "0", "--yes"], monkeypatch)
        == 2
    )
    assert "--unlimited" in capsys.readouterr().err


def test_a_negative_cap_is_refused(monkeypatch):
    assert (
        _parse(["--organization-id", PERSONAL_ORG, "--cap", "-1", "--yes"], monkeypatch)
        == 2
    )


def test_exactly_one_mode_is_required(monkeypatch):
    """No mode at all, and two modes at once, are both usage errors."""
    assert _parse(["--organization-id", PERSONAL_ORG], monkeypatch) == 2
    assert (
        _parse(
            ["--organization-id", PERSONAL_ORG, "--clear", "--unlimited", "--yes"],
            monkeypatch,
        )
        == 2
    )


def test_show_needs_no_confirmation(monkeypatch):
    """A read must not be gated behind the flag that guards a write.

    ``asyncio.run`` is stubbed rather than let run: this asserts which BRANCH
    ``main`` takes, and the command's own ``asyncio.run`` cannot be called from
    inside a running loop. The stub closes the coroutine it is handed so the
    "never awaited" warning does not mask a real one.
    """
    captured = {}

    def _fake_run(coroutine):
        captured["ran"] = True
        coroutine.close()
        return 0

    monkeypatch.setattr(set_turn_cap.asyncio, "run", _fake_run)
    assert _parse(["--organization-id", PERSONAL_ORG, "--show"], monkeypatch) == 0
    assert captured.get("ran"), "--show was refused before it reached the read"


async def test_the_column_refuses_a_negative_override_if_one_ever_reached_it(factory):
    """The CLI is not the only writer, so the constraint is checked too."""
    async with factory() as session:
        with pytest.raises(Exception):
            await session.execute(
                text(
                    "UPDATE organizations SET daily_turn_cap = -5 "
                    "WHERE organization_id = :o"
                ),
                {"o": PERSONAL_ORG},
            )
            await session.commit()

"""``fm-set-turn-cap`` — the operator's live control over one tenant's cap.

The cap's *default* moves only with a redeploy. A single tenant's cap is a row,
and this command writes it, so "raise or clear a tenant's cap without a
redeploy" is a claim about this file.

Everything goes through the same ports the enforcement uses — the
``CapPolicyResolver``, the organization repository, the ledger — which is what
these cases are shaped around:

* **what ``--show`` prints is the resolver's verdict**, not a second rendering
  of the policy that can drift from it;
* **the write goes through ``update_organization``**, so an override survives
  the mapper and the writer both;
* **the three write modes are three different actions.** ``--clear`` returns a
  tenant to the deployment policy; ``--unlimited`` takes the cap off. On a
  company organization they coincide today, which is exactly why they must not
  share a spelling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.cli import set_turn_cap
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicy,
    CapPolicyResolver,
    InMemoryTurnLedger,
    utc_day,
)

pytestmark = pytest.mark.unit

PERSONAL = "org-personal"
COMPANY = "org-company"


class FakeOrganizations:
    """The repository port, with the ``deleted_at`` filter it really applies."""

    def __init__(self, rows):
        self.rows = {
            org_id: SimpleNamespace(
                organization_id=org_id, name=f"Org {org_id}", daily_turn_cap=cap
            )
            for org_id, cap in rows.items()
        }
        self.updates: list[tuple[str, object]] = []

    async def get_organization(self, organization_id):
        return self.rows.get(organization_id)

    async def update_organization(self, organization):
        if organization.organization_id not in self.rows:
            return False
        self.rows[organization.organization_id] = organization
        self.updates.append((organization.organization_id, organization.daily_turn_cap))
        return True


class FakePersonalOrgs:
    def __init__(self, personal=()):
        self.personal = set(personal)

    async def is_personal_organization(self, organization_id):
        return organization_id in self.personal


def _wiring(rows=None, personal=(PERSONAL,), ledger=None):
    organizations = FakeOrganizations(
        rows if rows is not None else {PERSONAL: None, COMPANY: None}
    )
    resolver = CapPolicyResolver(
        FakePersonalOrgs(personal),
        organizations,
        default_limit=lambda: 30,
        multi_tenant=lambda: True,
    )
    return organizations, resolver, ledger or InMemoryTurnLedger()


async def _run(organization_id=PERSONAL, **kwargs):
    organizations, resolver, ledger = kwargs.pop("wiring", None) or _wiring()
    defaults = {"show_only": False, "dry_run": False, "new_value": None}
    defaults.update(kwargs)
    code = await set_turn_cap.set_turn_cap(
        organization_id=organization_id,
        resolver=resolver,
        organizations=organizations,
        ledger=ledger,
        **defaults,
    )
    return code, organizations


# =============================================================================
# The three write modes
# =============================================================================


async def test_setting_a_cap_writes_the_override_through_the_repository():
    code, organizations = await _run(new_value=200)
    assert code == 0
    assert organizations.updates == [(PERSONAL, 200)]
    assert organizations.rows[PERSONAL].daily_turn_cap == 200


async def test_unlimited_writes_the_explicit_zero():
    code, organizations = await _run(new_value=0)
    assert code == 0
    assert organizations.rows[PERSONAL].daily_turn_cap == 0


async def test_clear_removes_the_override_and_is_not_unlimited():
    """The distinction that bites on a personal tenant."""
    wiring = _wiring({PERSONAL: 500}, personal=(PERSONAL,))
    code, organizations = await _run(new_value=None, wiring=wiring)
    assert code == 0
    assert organizations.rows[PERSONAL].daily_turn_cap is None

    # Cleared, it is back on the DEFAULT — not uncapped.
    policy = await wiring[1].resolve(PERSONAL)
    assert policy.limit == 30
    assert policy.source == "default_personal"


async def test_a_dry_run_writes_nothing():
    code, organizations = await _run(new_value=7, dry_run=True)
    assert code == 0
    assert organizations.updates == []


async def test_show_writes_nothing():
    code, organizations = await _run(new_value=7, show_only=True)
    assert code == 0
    assert organizations.updates == []


async def test_an_unknown_organization_is_refused_without_writing():
    code, organizations = await _run(organization_id="no-such-org", new_value=5)
    assert code == 1
    assert organizations.updates == []


async def test_a_write_that_matches_no_row_is_reported_as_a_failure():
    """Reporting success would tell an operator a spend control moved when it
    did not."""

    class Vanishing(FakeOrganizations):
        async def update_organization(self, organization):
            return False

    organizations = Vanishing({PERSONAL: None})
    resolver = CapPolicyResolver(
        FakePersonalOrgs((PERSONAL,)),
        organizations,
        default_limit=lambda: 30,
        multi_tenant=lambda: True,
    )
    code = await set_turn_cap.set_turn_cap(
        organization_id=PERSONAL,
        new_value=5,
        show_only=False,
        dry_run=False,
        resolver=resolver,
        organizations=organizations,
        ledger=InMemoryTurnLedger(),
    )
    assert code == 1


# =============================================================================
# What it reports is what the enforcement decides
# =============================================================================


async def test_it_reports_the_resolvers_verdict_and_todays_usage(capsys):
    ledger = InMemoryTurnLedger()
    for _ in range(17):
        await ledger.reserve(PERSONAL, utc_day(), None)

    await _run(show_only=True, wiring=_wiring(ledger=ledger))
    out = capsys.readouterr().out

    assert "30 turns/day" in out
    assert "personal tenant" in out
    assert "17 turns" in out


async def test_a_company_organization_reads_as_uncapped(capsys):
    await _run(organization_id=COMPANY, show_only=True)
    out = capsys.readouterr().out
    assert "uncapped" in out
    assert "company organization" in out


@pytest.mark.parametrize(
    "source,limit,expected",
    [
        ("single_tenant", None, "single-tenant"),
        ("override_unlimited", None, "uncapped"),
        ("override", 7, "7 turns/day"),
        ("default_personal", 30, "30 turns/day"),
        ("company_uncapped", None, "uncapped"),
        ("indeterminate", 30, "fail-closed"),
        ("cleared", None, "deployment policy"),
    ],
)
def test_every_source_the_resolver_can_answer_with_has_words(source, limit, expected):
    """A source with no rendering would print a format string at an operator."""
    assert expected in set_turn_cap._describe(CapPolicy(limit=limit, source=source))


# =============================================================================
# The refusals, before anything is written
# =============================================================================


def _parse(argv, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["fm-set-turn-cap", *argv])
    with pytest.raises(SystemExit) as raised:
        set_turn_cap.main()
    return raised.value.code


def test_a_write_without_yes_is_refused(monkeypatch):
    assert _parse(["--organization-id", PERSONAL, "--cap", "5"], monkeypatch) == 1


def test_dry_run_and_yes_together_is_a_usage_error(monkeypatch):
    assert (
        _parse(
            ["--organization-id", PERSONAL, "--cap", "5", "--dry-run", "--yes"],
            monkeypatch,
        )
        == 2
    )


def test_a_cap_of_zero_is_refused_and_names_the_flag_that_means_it(monkeypatch, capsys):
    assert (
        _parse(["--organization-id", PERSONAL, "--cap", "0", "--yes"], monkeypatch) == 2
    )
    assert "--unlimited" in capsys.readouterr().err


def test_a_negative_cap_is_refused(monkeypatch):
    assert (
        _parse(["--organization-id", PERSONAL, "--cap", "-1", "--yes"], monkeypatch)
        == 2
    )


def test_exactly_one_mode_is_required(monkeypatch):
    assert _parse(["--organization-id", PERSONAL], monkeypatch) == 2
    assert (
        _parse(
            ["--organization-id", PERSONAL, "--clear", "--unlimited", "--yes"],
            monkeypatch,
        )
        == 2
    )


def test_show_needs_no_confirmation(monkeypatch):
    """A read must not be gated behind the flag that guards a write."""
    captured = {}

    def _fake_run(coroutine):
        captured["ran"] = True
        coroutine.close()
        return 0

    monkeypatch.setattr(set_turn_cap.asyncio, "run", _fake_run)
    assert _parse(["--organization-id", PERSONAL, "--show"], monkeypatch) == 0
    assert captured.get("ran"), "--show was refused before it reached the read"


@pytest.mark.parametrize(
    "flags,expected",
    [(["--cap", "9"], 9), (["--unlimited"], 0), (["--clear"], None)],
)
def test_each_mode_carries_its_value_into_the_write(monkeypatch, flags, expected):
    """``--clear`` and ``--show`` both store nothing; only one of them writes.

    Pinned because the value expression collapsed to one line: an ``if
    args.show`` arm that computed the same ``None`` the ``--clear`` arm does was
    dead, and removing dead code is only safe if what replaced it is checked.
    """
    seen = {}

    def _fake_run(coroutine):
        coroutine.close()
        return 0

    def _capture(**kwargs):
        seen.update(kwargs)

        async def _noop():
            return 0

        return _noop()

    monkeypatch.setattr(set_turn_cap, "set_turn_cap", _capture)
    monkeypatch.setattr(set_turn_cap.asyncio, "run", _fake_run)
    _parse(["--organization-id", PERSONAL, *flags, "--yes"], monkeypatch)
    assert seen["new_value"] == expected
    assert seen["show_only"] is False

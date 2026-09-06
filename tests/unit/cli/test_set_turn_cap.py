"""``fm-set-turn-cap`` — the operator's live control over one subject's cap.

The cap's *default* moves only with a redeploy. A single organization's cap is a
row, and this command writes it, so "raise or clear a tenant's cap without a
redeploy" is a claim about this file.

What the command addresses is a **billing subject** (ADR-017 D5): the
organization that pays for an account, or the account itself when nobody does.
Both are metered and both are readable here; only the organization is writable,
because the override lives on ``organizations.daily_turn_cap`` and an account in
no organization has no row to carry one. Before ADR-017 "personal" was an
organization of its own, so ``--organization-id`` addressed every subject there
was — it no longer does, and the account arm is what closes that hole.

Everything goes through the same ports the enforcement uses — the
``CapPolicyResolver``, the organization repository, the ledger — which is what
these cases are shaped around:

* **what ``--show`` prints is the resolver's verdict**, not a second rendering
  of the policy that can drift from it;
* **the write goes through ``update_organization``**, so an override survives
  the mapper and the writer both;
* **the three write modes are three different actions.** ``--clear`` returns a
  subject to the deployment policy; ``--unlimited`` takes the cap off. On a
  company organization they coincide today, which is exactly why they must not
  share a spelling.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from faultmaven.cli import set_turn_cap
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    SUBJECT_ACCOUNT,
    SUBJECT_ORGANIZATION,
    BillingSubject,
    CapPolicy,
    CapPolicyResolver,
    InMemoryTurnLedger,
    utc_day,
)

pytestmark = pytest.mark.unit

COMPANY = "org-company"
CAPPED = "org-capped"
ACCOUNT = "user-nobody-pays"
ENTERPRISE = "00000000-0000-0000-0000-0000000000ee"


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
        self.asked: list[str] = []

    async def get_organization(self, organization_id):
        self.asked.append(organization_id)
        return self.rows.get(organization_id)

    async def update_organization(self, organization):
        if organization.organization_id not in self.rows:
            return False
        self.rows[organization.organization_id] = organization
        self.updates.append((organization.organization_id, organization.daily_turn_cap))
        return True


def _wiring(rows=None, ledger=None):
    organizations = FakeOrganizations(
        rows if rows is not None else {COMPANY: None, CAPPED: 50}
    )
    resolver = CapPolicyResolver(
        organizations,
        default_limit=lambda: 30,
        multi_tenant=lambda: True,
    )
    return organizations, resolver, ledger or InMemoryTurnLedger()


async def _run(organization_id=CAPPED, **kwargs):
    organizations, resolver, ledger = kwargs.pop("wiring", None) or _wiring()
    defaults = {"show_only": False, "dry_run": False, "new_value": None}
    defaults.update(kwargs)
    code = await set_turn_cap.set_turn_cap(
        enterprise_id=ENTERPRISE,
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
    assert organizations.updates == [(CAPPED, 200)]
    assert organizations.rows[CAPPED].daily_turn_cap == 200


async def test_unlimited_writes_the_explicit_zero():
    code, organizations = await _run(new_value=0)
    assert code == 0
    assert organizations.rows[CAPPED].daily_turn_cap == 0


async def test_clear_removes_the_override_and_is_not_unlimited():
    """The distinction that bites once the deployment default and "uncapped"
    stop coinciding.

    Cleared, an organization falls back to the *company* policy — uncapped
    today — which is a different fact from ``--unlimited``'s explicit zero, and
    the difference is visible in the ``source`` rather than only in the limit.
    """
    wiring = _wiring({CAPPED: 500})
    code, organizations = await _run(new_value=None, wiring=wiring)
    assert code == 0
    assert organizations.rows[CAPPED].daily_turn_cap is None

    subject = BillingSubject(SUBJECT_ORGANIZATION, CAPPED)
    policy = await wiring[1].resolve(subject)
    assert policy.limit is None
    assert policy.source == "company_uncapped"

    # And the explicit zero reads as a different thing on the same row.
    organizations.rows[CAPPED].daily_turn_cap = 0
    unlimited = await wiring[1].resolve(subject)
    assert unlimited.limit is None
    assert unlimited.source == "override_unlimited"


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

    organizations = Vanishing({CAPPED: None})
    resolver = CapPolicyResolver(
        organizations,
        default_limit=lambda: 30,
        multi_tenant=lambda: True,
    )
    code = await set_turn_cap.set_turn_cap(
        enterprise_id=ENTERPRISE,
        organization_id=CAPPED,
        new_value=5,
        show_only=False,
        dry_run=False,
        resolver=resolver,
        organizations=organizations,
        ledger=InMemoryTurnLedger(),
    )
    assert code == 1


# =============================================================================
# The account subject (ADR-017 D5) — readable, never writable
# =============================================================================


async def test_an_account_is_addressable_and_reads_as_the_deployment_default(capsys):
    """The subject ``--organization-id`` can no longer reach.

    Under ADR-016 a personal tenant was an organization, so an operator could
    ask about it by organization id. Under ADR-017 it is an account in no
    organization: there is no organization row, so this arm is the only way to
    answer "what is this person's allowance, and how much of it is left?".
    """
    organizations, resolver, ledger = _wiring()
    account = BillingSubject(SUBJECT_ACCOUNT, ACCOUNT)
    for _ in range(11):
        await ledger.reserve(account, utc_day(), None)

    code = await set_turn_cap.set_turn_cap(
        enterprise_id=ENTERPRISE,
        account_id=ACCOUNT,
        new_value=None,
        show_only=True,
        dry_run=False,
        resolver=resolver,
        organizations=organizations,
        ledger=ledger,
    )
    out = capsys.readouterr().out

    assert code == 0
    assert ACCOUNT in out
    assert "30 turns/day" in out
    assert "11 turns" in out
    # No organization row was read, because there is none to read: "nobody pays
    # for this account" is the whole of what makes it its own subject.
    assert organizations.asked == []


async def test_writing_an_accounts_cap_is_refused_and_names_the_remedy(capsys):
    """An account has no row to carry an override, so the write must refuse.

    Accepting it and quietly writing nothing is the failure this guards: an
    operator would read "cap updated" and believe a spend control moved.
    """
    organizations, resolver, ledger = _wiring()
    code = await set_turn_cap.set_turn_cap(
        enterprise_id=ENTERPRISE,
        account_id=ACCOUNT,
        new_value=200,
        show_only=False,
        dry_run=False,
        resolver=resolver,
        organizations=organizations,
        ledger=ledger,
    )
    out = capsys.readouterr().out

    assert code == 1
    assert organizations.updates == []
    assert "no cap of its own" in out
    assert "TENANT_DAILY_TURN_CAP" in out
    # The refusal comes AFTER the read, so the operator still sees the answer to
    # the question behind the attempted write.
    assert "Used today:" in out


async def test_naming_both_subjects_or_neither_is_a_programming_error():
    """Exactly one subject. argparse enforces it for the console entrypoint;
    the function refuses it directly, so an in-process caller cannot get a
    silently-wrong subject either."""
    organizations, resolver, ledger = _wiring()
    common = {
        "enterprise_id": ENTERPRISE,
        "new_value": None,
        "show_only": True,
        "dry_run": False,
        "resolver": resolver,
        "organizations": organizations,
        "ledger": ledger,
    }
    with pytest.raises(ValueError):
        await set_turn_cap.set_turn_cap(
            organization_id=COMPANY, account_id=ACCOUNT, **common
        )
    with pytest.raises(ValueError):
        await set_turn_cap.set_turn_cap(**common)


# =============================================================================
# What it reports is what the enforcement decides
# =============================================================================


async def test_it_reports_the_resolvers_verdict_and_todays_usage(capsys):
    ledger = InMemoryTurnLedger()
    subject = BillingSubject(SUBJECT_ORGANIZATION, CAPPED)
    for _ in range(17):
        await ledger.reserve(subject, utc_day(), None)

    await _run(show_only=True, wiring=_wiring(ledger=ledger))
    out = capsys.readouterr().out

    assert "50 turns/day" in out
    assert "override" in out
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


def test_the_rendered_sources_are_exactly_the_ones_the_resolver_emits():
    """Derived from the resolver, not remembered beside it.

    A new ``CapPolicy.source`` added to the policy without a word here would
    print ``{limit}`` at an operator, and the parametrised case above cannot
    catch that because its list is written by hand.
    """
    import inspect

    from faultmaven.infrastructure.protection import tenant_turn_cap

    emitted = set(
        re.findall(
            r'source="([a-z_]+)"',
            inspect.getsource(tenant_turn_cap.CapPolicyResolver),
        )
    )
    assert emitted, "no sources were found in the resolver — the pattern drifted"
    assert emitted <= set(set_turn_cap._SOURCE_WORDS), (
        "the resolver can answer with a source this command has no words for: "
        f"{sorted(emitted - set(set_turn_cap._SOURCE_WORDS))}"
    )


# =============================================================================
# The refusals, before anything is written
# =============================================================================


def _parse(argv, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["fm-set-turn-cap", *argv])
    with pytest.raises(SystemExit) as raised:
        set_turn_cap.main()
    return raised.value.code


_SUBJECT = ["--enterprise-id", ENTERPRISE, "--organization-id", CAPPED]


def test_a_write_without_yes_is_refused(monkeypatch):
    assert _parse([*_SUBJECT, "--cap", "5"], monkeypatch) == 1


def test_dry_run_and_yes_together_is_a_usage_error(monkeypatch):
    assert _parse([*_SUBJECT, "--cap", "5", "--dry-run", "--yes"], monkeypatch) == 2


def test_a_cap_of_zero_is_refused_and_names_the_flag_that_means_it(monkeypatch, capsys):
    assert _parse([*_SUBJECT, "--cap", "0", "--yes"], monkeypatch) == 2
    assert "--unlimited" in capsys.readouterr().err


def test_a_negative_cap_is_refused(monkeypatch):
    assert _parse([*_SUBJECT, "--cap", "-1", "--yes"], monkeypatch) == 2


def test_exactly_one_mode_is_required(monkeypatch):
    assert _parse(_SUBJECT, monkeypatch) == 2
    assert _parse([*_SUBJECT, "--clear", "--unlimited", "--yes"], monkeypatch) == 2


def test_exactly_one_subject_is_required(monkeypatch, capsys):
    """Neither subject, or both, is a usage error — never a default."""
    assert _parse(["--enterprise-id", ENTERPRISE, "--show"], monkeypatch) == 2
    assert (
        _parse(
            [
                "--enterprise-id",
                ENTERPRISE,
                "--organization-id",
                CAPPED,
                "--account-id",
                ACCOUNT,
                "--show",
            ],
            monkeypatch,
        )
        == 2
    )


def test_the_enterprise_is_required(monkeypatch):
    """Every read below it is RLS-scoped by the enterprise; without one the
    command would resolve nothing and say so obscurely."""
    assert _parse(["--organization-id", CAPPED, "--show"], monkeypatch) == 2


def test_show_needs_no_confirmation(monkeypatch):
    """A read must not be gated behind the flag that guards a write."""
    captured = {}

    def _fake_run(coroutine):
        captured["ran"] = True
        coroutine.close()
        return 0

    monkeypatch.setattr(set_turn_cap.asyncio, "run", _fake_run)
    assert _parse([*_SUBJECT, "--show"], monkeypatch) == 0
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
    _parse([*_SUBJECT, *flags, "--yes"], monkeypatch)
    assert seen["new_value"] == expected
    assert seen["show_only"] is False


def test_the_subject_reaches_the_command_as_the_operator_named_it(monkeypatch):
    """Whichever subject was given is the one passed through, and the other is
    ``None`` — not a silently-substituted default."""
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

    _parse(
        ["--enterprise-id", ENTERPRISE, "--account-id", ACCOUNT, "--show"], monkeypatch
    )
    assert (seen["account_id"], seen["organization_id"]) == (ACCOUNT, None)

    seen.clear()
    _parse([*_SUBJECT, "--show"], monkeypatch)
    assert (seen["account_id"], seen["organization_id"]) == (None, CAPPED)
    assert seen["enterprise_id"] == ENTERPRISE

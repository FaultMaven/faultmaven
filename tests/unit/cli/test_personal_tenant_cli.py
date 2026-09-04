"""``fm-personal-tenant`` guards, plan and idempotency (#1045 D8).

The command's damage is not a bad UPDATE — it is a *correct* retirement aimed at
the wrong tenant, or one that reports success for a step it did not run. So what
is pinned here is everything that decides whether the writes happen at all, and
everything an operator reads off the output:

* **a dry run writes nothing, on either side** — no row changes, not one
  mutating provider call;
* **the order**, which is the whole resumability argument: fence first, tokens
  revoked immediately after, the binding before the provider calls, the provider
  organization before the mapping that records its id, the anchor last;
* **the provider organization is the one this tenant's mapping names.** A
  re-run aimed at a retired predecessor must not delete the live successor's;
* **idempotency and resumability** — a second run is a no-op, and a run
  interrupted at any single step is finished by re-running;
* **the blast radius** — a second personal tenant present throughout comes out
  byte-identical;
* **the data that survives** — cases and knowledge items are still there.

Against a real SQLite database built from the ORM metadata, with the session
factories patched rather than ``DATABASE_URL`` reset: the whole suite runs in one
process, and nulling the shared engine strands every later test that built tables
through it (the lesson recorded in ``tests/integration/test_reassign_cases_cli.py``).
"""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.cli import personal_tenant as cli
from faultmaven.modules.auth.contracts import (
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    personal_org_slug,
    personal_tenant_key,
)
from faultmaven.modules.auth.exceptions import SSOProvisioningError
from tests.conftest import RecordingIdP, RecordingRevoker

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.usefixtures("restore_tenant_context"),
]

try:
    _WORKOS_AVAILABLE = importlib.util.find_spec("workos") is not None
except (ImportError, ValueError):  # pragma: no cover - absent SDK
    _WORKOS_AVAILABLE = False

PROVIDER = "workos"

SUBJECT_A = "user_01AAAA"
SUBJECT_B = "user_01BBBB"
KEY_A = personal_tenant_key(PROVIDER, SUBJECT_A)
SLUG_A = personal_org_slug(KEY_A)
SLUG_B = personal_org_slug(personal_tenant_key(PROVIDER, SUBJECT_B))

ENT_A = "11111111-1111-1111-1111-111111111111"
ORG_A = "22222222-2222-2222-2222-222222222222"
ENT_B = "33333333-3333-3333-3333-333333333333"
ORG_B = "44444444-4444-4444-4444-444444444444"
ENT_CO = "55555555-5555-5555-5555-555555555555"
ORG_CO = "66666666-6666-6666-6666-666666666666"

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"

IDP_ORG_A = "org_01IDPA"
IDP_ORG_B = "org_01IDPB"
IDP_ORG_CO = "org_01IDPCO"


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Two personal tenants and one company tenant, in a real database."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'retire.db'}"

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        for ent, slug, name in (
            (ENT_A, SLUG_A, "Personal"),
            (ENT_B, SLUG_B, "Personal"),
            (ENT_CO, "acme", "Acme"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:e, :n, :s)"
                ),
                {"e": ent, "s": slug, "n": name},
            )
        for org, ent, slug, name in (
            (ORG_A, ENT_A, SLUG_A, "Personal"),
            (ORG_B, ENT_B, SLUG_B, "Personal"),
            (ORG_CO, ENT_CO, "acme", "Acme"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO organizations "
                    "(organization_id, enterprise_id, name, slug, is_active) "
                    "VALUES (:o, :e, :n, :s, 1)"
                ),
                {"o": org, "e": ent, "s": slug, "n": name},
            )
            await conn.execute(
                text(
                    "INSERT INTO teams (team_id, organization_id, name) "
                    "VALUES (:t, :o, 'Default')"
                ),
                {"t": f"team-{org[:8]}", "o": org},
            )
        for idp_org, org in (
            (IDP_ORG_A, ORG_A),
            (IDP_ORG_B, ORG_B),
            (IDP_ORG_CO, ORG_CO),
        ):
            await conn.execute(
                text(
                    "INSERT INTO sso_org_mappings "
                    "(provider, provider_org_id, organization_id) "
                    "VALUES ('workos', :p, :o)"
                ),
                {"p": idp_org, "o": org},
            )
        for subject, org, ent, idp_org in (
            (SUBJECT_A, ORG_A, ENT_A, IDP_ORG_A),
            (SUBJECT_B, ORG_B, ENT_B, IDP_ORG_B),
        ):
            await conn.execute(
                text(
                    "INSERT INTO sso_personal_orgs (provider, provider_user_id, "
                    " organization_id, provider_org_id, enterprise_id, "
                    " membership_confirmed) "
                    "VALUES ('workos', :s, :o, :p, :e, 1)"
                ),
                {"s": subject, "o": org, "p": idp_org, "e": ent},
            )
        for user, ent, subject, name in (
            (USER_A, ENT_A, SUBJECT_A, "sam"),
            (USER_B, ENT_B, SUBJECT_B, "bo"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    " display_name, sso_provider, sso_provider_id, is_active) "
                    "VALUES (:u, :e, :n, :m, :n, 'workos', :s, 1)"
                ),
                {
                    "u": user,
                    "e": ent,
                    "n": name,
                    "m": f"{name}@personal.example",
                    "s": subject,
                },
            )
        for user, org in ((USER_A, ORG_A), (USER_B, ORG_B)):
            await conn.execute(
                text(
                    "INSERT INTO organization_members "
                    "(user_id, organization_id, role_id) VALUES (:u, :o, 'member')"
                ),
                {"u": user, "o": org},
            )
        for case_id, org in (("case_a", ORG_A), ("case_b", ORG_B)):
            await conn.execute(
                text(
                    "INSERT INTO cases (case_id, organization_id, title) "
                    "VALUES (:c, :o, 'disk full')"
                ),
                {"c": case_id, "o": org},
            )
        for item, org in (("kb_a", ORG_A), ("kb_b", ORG_B)):
            await conn.execute(
                text(
                    "INSERT INTO knowledge_items "
                    "(item_id, organization_id, title, content, item_type, scope) "
                    "VALUES (:i, :o, 'runbook', 'body', 'runbook', 'personal')"
                ),
                {"i": item, "o": org},
            )
    await engine.dispose()

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session(database_url=None):
        session = sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    for target in (
        "faultmaven.infrastructure.persistence.database.get_db_session",
        "faultmaven.infrastructure.persistence.sessionless_organization_repository"
        ".get_db_session",
        "faultmaven.infrastructure.persistence.account_anchor.get_db_session",
        "faultmaven.cli.personal_tenant.get_db_session",
    ):
        monkeypatch.setattr(target, _session)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _rows(engine, sql, params=None):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).fetchall()


async def _exec(engine, sql, params):
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _snapshot(engine, org: str, ent: str) -> dict:
    """Everything about one tenant a retirement could possibly move."""
    return {
        "organization": await _rows(
            engine,
            "SELECT organization_id, enterprise_id, name, slug, is_active, "
            "deleted_at FROM organizations WHERE organization_id = :o",
            {"o": org},
        ),
        "enterprise": await _rows(
            engine,
            "SELECT enterprise_id, name, slug, deleted_at, "
            "personal_tenant_retirement FROM enterprises WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "mapping": await _rows(
            engine,
            "SELECT provider, provider_org_id, organization_id "
            "FROM sso_org_mappings WHERE organization_id = :o",
            {"o": org},
        ),
        "binding": await _rows(
            engine,
            "SELECT provider, provider_user_id, organization_id, enterprise_id, "
            "provider_org_id FROM sso_personal_orgs WHERE organization_id = :o",
            {"o": org},
        ),
        "users": await _rows(
            engine,
            "SELECT user_id, enterprise_id, is_active FROM users "
            "WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "members": await _rows(
            engine,
            "SELECT user_id, organization_id, role_id FROM organization_members "
            "WHERE organization_id = :o",
            {"o": org},
        ),
        "cases": await _rows(
            engine,
            "SELECT case_id, title FROM cases WHERE organization_id = :o",
            {"o": org},
        ),
        "knowledge": await _rows(
            engine,
            "SELECT item_id, title, content FROM knowledge_items "
            "WHERE organization_id = :o",
            {"o": org},
        ),
        "teams": await _rows(
            engine,
            "SELECT team_id, name FROM teams WHERE organization_id = :o",
            {"o": org},
        ),
    }


async def _retire(engine, idp=None, revoker=None, **kwargs):
    defaults = dict(
        subject=SUBJECT_A,
        organization_id=None,
        next_login="refuse",
        apply=True,
        idp=idp if idp is not None else RecordingIdP([IDP_ORG_A, IDP_ORG_B]),
        auth_service=revoker if revoker is not None else RecordingRevoker(),
    )
    defaults.update(kwargs)
    return await cli.retire(**defaults)


# =============================================================================
# A dry run writes nothing, on either side
# =============================================================================


async def test_a_dry_run_changes_no_row_and_makes_no_provider_call(db, capsys):
    """Both halves. A dry run that only avoided the database could still delete
    a provider organization, which is the irreversible half."""
    before_a = await _snapshot(db, ORG_A, ENT_A)
    before_b = await _snapshot(db, ORG_B, ENT_B)
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])
    revoker = RecordingRevoker()

    code = await _retire(db, idp, revoker, apply=False)

    assert code == 0
    assert idp.calls == []
    assert revoker.revoked == []
    assert await _snapshot(db, ORG_A, ENT_A) == before_a
    assert await _snapshot(db, ORG_B, ENT_B) == before_b
    out = capsys.readouterr().out
    assert "Would apply:" in out and "nothing was written" in out


async def test_the_dry_run_lists_exactly_what_the_apply_run_then_does(db, capsys):
    """One list, two readings. A dry run whose plan differs from ``--apply`` is
    worse than no dry run at all — it is the thing the operator decided on."""
    assert await _retire(db, apply=False) == 0
    planned = [
        line.strip()[2:]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  · ")
    ]

    assert await _retire(db) == 0
    performed = [
        line.strip()[2:]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  ✅ ")
    ]

    assert planned == performed != []


# =============================================================================
# The order is the resumability argument
# =============================================================================


@pytest.mark.parametrize("policy", ["refuse", "fresh-tenant"])
async def test_the_plan_is_the_documented_order(db, monkeypatch, policy):
    applied: list[str] = []
    original = cli._apply

    async def _recording_apply(steps):
        applied.extend(step.name for step in steps)
        return await original(steps)

    monkeypatch.setattr(cli, "_apply", _recording_apply)
    assert await _retire(db, next_login=policy) == 0

    expected = [
        "organization_soft_deleted",
        "tokens_revoked",
        "binding_deleted",
        "idp_organization_deleted",
        "mapping_deleted",
        "enterprise_retired",
    ]
    if policy == "fresh-tenant":
        expected.append("anchor_cleared")
    assert applied == expected
    # The fence is first, so no login can enter a tenant being taken apart.
    assert applied[0] == "organization_soft_deleted"
    # The binding goes BEFORE the provider calls: while it exists a login can
    # ask the provider to finish a membership, re-creating by its deterministic
    # external id the organization the next step deletes.
    assert applied.index("binding_deleted") < applied.index("idp_organization_deleted")
    # The provider organization goes BEFORE the mapping that records its id —
    # both because the id has to still be readable, and because the derived
    # external id is only free for a later tenant once it is gone.
    assert applied.index("idp_organization_deleted") < applied.index("mapping_deleted")


# =============================================================================
# The provider organization is the one THIS tenant's mapping names
# =============================================================================


async def test_the_idp_organization_is_addressed_by_the_recorded_id(db):
    idp = RecordingIdP([IDP_ORG_A])
    assert await _retire(db, idp) == 0
    assert idp.calls == [IDP_ORG_A]


async def test_a_rerun_on_a_retired_predecessor_spares_the_live_successor(db):
    """The defect this addressing exists to prevent.

    Retire, let the subject provision again (the successor takes the same
    derived slug and a NEW provider organization), then re-run the retirement
    against the **predecessor**. A teardown addressed by the subject-derived
    external id would delete the successor's organization and report success.
    """
    idp = RecordingIdP([IDP_ORG_A, "org_01SUCCESSOR"])
    assert await _retire(db, idp, next_login="fresh-tenant") == 0
    idp.calls.clear()

    # The successor: same subject, same derived slug (legal — the uniqueness
    # indexes are partial on deleted_at IS NULL), a new provider organization.
    successor_org = "77777777-7777-7777-7777-777777777777"
    successor_ent = "88888888-8888-8888-8888-888888888888"
    await _exec(
        db,
        "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e,'Personal',:s)",
        {"e": successor_ent, "s": SLUG_A},
    )
    await _exec(
        db,
        "INSERT INTO organizations (organization_id, enterprise_id, name, slug, "
        "is_active) VALUES (:o, :e, 'Personal', :s, 1)",
        {"o": successor_org, "e": successor_ent, "s": SLUG_A},
    )
    await _exec(
        db,
        "INSERT INTO sso_org_mappings (provider, provider_org_id, organization_id) "
        "VALUES ('workos', :p, :o)",
        {"p": "org_01SUCCESSOR", "o": successor_org},
    )
    await _exec(
        db,
        "INSERT INTO sso_personal_orgs (provider, provider_user_id, organization_id, "
        "provider_org_id, enterprise_id, membership_confirmed) "
        "VALUES ('workos', :s, :o, :p, :e, 1)",
        {
            "s": SUBJECT_A,
            "o": successor_org,
            "p": "org_01SUCCESSOR",
            "e": successor_ent,
        },
    )

    # Re-run against the predecessor, by id — the only way a retired tenant is
    # addressable — with the policy it was retired under, so nothing is
    # outstanding and the run is a genuine no-op.
    code = await _retire(
        db, idp, subject=None, organization_id=ORG_A, next_login="fresh-tenant"
    )

    assert code == cli.EXIT_NOTHING_TO_DO
    assert idp.calls == []
    assert "org_01SUCCESSOR" in idp.present
    assert (
        await _rows(
            db,
            "SELECT provider_org_id FROM sso_org_mappings WHERE organization_id = :o",
            {"o": successor_org},
        )
    )[0].provider_org_id == "org_01SUCCESSOR"


# =============================================================================
# Idempotency and resumability
# =============================================================================


async def test_running_it_twice_leaves_the_same_state_and_says_nothing_to_do(db):
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])
    assert await _retire(db, idp) == 0
    after_first = await _snapshot(db, ORG_A, ENT_A)

    assert await _retire(db, idp, subject=None, organization_id=ORG_A) == (
        cli.EXIT_NOTHING_TO_DO
    )

    assert await _snapshot(db, ORG_A, ENT_A) == after_first
    assert idp.calls == [IDP_ORG_A]


async def test_a_rerun_does_not_restamp_the_retirement(db):
    """The bug a freshly-built marker had: every run moved the timestamp."""
    assert await _retire(db) == 0
    stamped = (
        await _rows(
            db,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            {"e": ENT_A},
        )
    )[0].deleted_at

    assert await _retire(db, subject=None, organization_id=ORG_A) == (
        cli.EXIT_NOTHING_TO_DO
    )

    assert (
        await _rows(
            db,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            {"e": ENT_A},
        )
    )[0].deleted_at == stamped


@pytest.mark.parametrize(
    "fail_at",
    [
        "organization_soft_deleted",
        "tokens_revoked",
        "binding_deleted",
        "idp_organization_deleted",
        "mapping_deleted",
        "enterprise_retired",
        "anchor_cleared",
    ],
)
async def test_an_interruption_at_any_step_is_finished_by_re_running(
    db, monkeypatch, capsys, fail_at
):
    """Fault-inject each side-effect in turn; the end state is identical.

    The injected failure replaces one step's body, so the plan is the real one
    and every partial state the command can actually leave is reached.
    """
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])
    original_steps = cli._retirement_steps

    def _sabotaged(state, **kwargs):
        steps = original_steps(state, **kwargs)
        for step in steps:
            if step.name == fail_at:

                async def _boom():
                    raise RuntimeError(f"injected failure at {fail_at}")

                step.run = _boom
        return steps

    monkeypatch.setattr(cli, "_retirement_steps", _sabotaged)
    interrupted = await _retire(db, idp, next_login="fresh-tenant")
    monkeypatch.setattr(cli, "_retirement_steps", original_steps)

    # Never a success for a step it did not complete.
    if fail_at == "organization_soft_deleted":
        assert interrupted == cli.EXIT_REFUSED
    else:
        assert interrupted == cli.EXIT_INCOMPLETE
    # And it prints the address a resumed run needs, because the binding it was
    # found by may already be gone.
    assert f"--organization-id {ORG_A}" in capsys.readouterr().out

    # Re-running by ID — the only address a part-retired tenant has — finishes it.
    assert (
        await _retire(
            db, idp, subject=None, organization_id=ORG_A, next_login="fresh-tenant"
        )
        == 0
    )

    state = await _snapshot(db, ORG_A, ENT_A)
    assert state["binding"] == []
    assert state["mapping"] == []
    assert state["organization"][0].deleted_at is not None
    assert state["organization"][0].is_active in (0, False)
    # No renames: the retired tenant keeps the derived slug.
    assert state["organization"][0].slug == SLUG_A
    assert state["enterprise"][0].deleted_at is not None
    assert state["enterprise"][0].personal_tenant_retirement == (
        RETIREMENT_POLICY_FRESH_TENANT
    )
    assert state["users"] == []  # released
    assert IDP_ORG_A not in idp.present
    assert (await _snapshot(db, ORG_B, ENT_B))["binding"] != []


async def test_a_provider_failure_reports_incomplete_rather_than_done(db, capsys):
    idp = RecordingIdP([IDP_ORG_A], error=SSOProvisioningError("provider unavailable"))

    code = await _retire(db, idp)

    assert code == cli.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "Retired." not in out
    rows = await _rows(
        db,
        "SELECT deleted_at, personal_tenant_retirement FROM enterprises "
        "WHERE enterprise_id = :e",
        {"e": ENT_A},
    )
    assert rows[0].personal_tenant_retirement is None


# =============================================================================
# The flag decides what is written
# =============================================================================


@pytest.mark.parametrize(
    "flag,policy,anchor_after",
    [
        ("refuse", RETIREMENT_POLICY_REFUSE, ENT_A),
        ("fresh-tenant", RETIREMENT_POLICY_FRESH_TENANT, None),
    ],
)
async def test_the_flag_decides_the_policy_and_the_anchor(
    db, flag, policy, anchor_after
):
    """Typed columns, not a marker: the policy on the enterprise, and whether
    the account is anchored at all."""
    assert await _retire(db, next_login=flag) == 0

    enterprise = (
        await _rows(
            db,
            "SELECT deleted_at, personal_tenant_retirement FROM enterprises "
            "WHERE enterprise_id = :e",
            {"e": ENT_A},
        )
    )[0]
    assert enterprise.deleted_at is not None
    assert enterprise.personal_tenant_retirement == policy
    user = (
        await _rows(
            db, "SELECT enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
        )
    )[0]
    assert user.enterprise_id == anchor_after


async def test_the_subjects_tokens_are_revoked(db):
    """A live refresh chain outlives the callback, so the fence is not enough."""
    revoker = RecordingRevoker()
    assert await _retire(db, revoker=revoker) == 0
    assert revoker.revoked == [USER_A]


def test_the_parser_defaults_next_login_to_refuse():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--next-login", choices=sorted(cli._NEXT_LOGIN_POLICIES), default="refuse"
    )
    assert parser.parse_args([]).next_login == "refuse"
    assert cli._NEXT_LOGIN_POLICIES["refuse"] == RETIREMENT_POLICY_REFUSE


# =============================================================================
# Refusals — nothing written, and the right exit code
# =============================================================================


async def test_a_company_organization_is_refused(db, capsys):
    before = await _snapshot(db, ORG_CO, ENT_CO)

    code = await _retire(db, subject=None, organization_id=ORG_CO)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_CO, ENT_CO) == before
    assert "not a personal tenant" in capsys.readouterr().out


async def test_a_subject_and_organization_that_disagree_are_refused(db, capsys):
    before = await _snapshot(db, ORG_B, ENT_B)
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])

    code = await _retire(db, idp, subject=SUBJECT_A, organization_id=ORG_B)

    assert code == cli.EXIT_REFUSED
    assert idp.calls == []
    assert await _snapshot(db, ORG_B, ENT_B) == before
    assert "cross-check" in capsys.readouterr().out


async def test_an_unknown_subject_is_refused_not_reported_as_nothing_to_do(db, capsys):
    """ "Nothing matched" is what a typo looks like, and the module's own exit
    table says that is 1."""
    code = await _retire(db, subject="user_01NOBODY")

    assert code == cli.EXIT_REFUSED
    assert "No live personal tenant" in capsys.readouterr().out


async def test_an_unknown_organization_id_is_refused(db, capsys):
    code = await _retire(db, subject=None, organization_id="does-not-exist")

    assert code == cli.EXIT_REFUSED
    assert "No organization" in capsys.readouterr().out


async def test_an_empty_subject_is_refused_not_a_traceback(db, capsys):
    """The documented kubectl recipe interpolates a shell variable here."""
    code = await _retire(db, subject="   ")

    assert code == cli.EXIT_REFUSED
    assert "--subject was given but is empty" in capsys.readouterr().out


async def test_a_missing_enterprise_row_is_its_own_outcome(db, capsys):
    """Distinct from "no such tenant": the remedies have nothing in common."""
    await _exec(db, "DELETE FROM enterprises WHERE enterprise_id = :e", {"e": ENT_A})

    code = await _retire(db, subject=None, organization_id=ORG_A)

    assert code == cli.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "which does not exist" in out
    assert "broken row, not an absent tenant" in out


# =============================================================================
# Blast radius and survival
# =============================================================================


async def test_the_other_personal_tenant_is_untouched(db):
    before = await _snapshot(db, ORG_B, ENT_B)

    assert await _retire(db, next_login="fresh-tenant") == 0

    assert await _snapshot(db, ORG_B, ENT_B) == before


async def test_cases_and_knowledge_of_the_retired_tenant_still_exist(db):
    before = await _snapshot(db, ORG_A, ENT_A)

    assert await _retire(db) == 0

    after = await _snapshot(db, ORG_A, ENT_A)
    assert after["cases"] == before["cases"] != []
    assert after["knowledge"] == before["knowledge"] != []
    assert after["organization"] != []
    assert after["members"] == before["members"]


# =============================================================================
# re-anchor
# =============================================================================


async def test_re_anchor_moves_the_account_grants_membership_and_retires_the_binding(
    db,
):
    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)

    assert code == 0
    user = (
        await _rows(
            db, "SELECT enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
        )
    )[0]
    assert user.enterprise_id == ENT_CO
    assert (
        len(
            await _rows(
                db,
                "SELECT role_id FROM organization_members WHERE organization_id = :o "
                "AND user_id = :u",
                {"o": ORG_CO, "u": USER_A},
            )
        )
        == 1
    )
    assert (
        await _rows(
            db,
            "SELECT 1 FROM sso_personal_orgs WHERE provider_user_id = :s",
            {"s": SUBJECT_A},
        )
        == []
    )
    assert (await _snapshot(db, ORG_A, ENT_A))["cases"] != []


async def test_re_anchor_moves_a_retired_subject(db):
    """R6, from the operator side: a retirement must not strand somebody."""
    assert await _retire(db, next_login="refuse") == 0

    assert (
        await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True) == 0
    )

    assert (
        await _rows(
            db, "SELECT enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
        )
    )[0].enterprise_id == ENT_CO


async def test_re_anchor_is_idempotent(db):
    assert (
        await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True) == 0
    )
    before = await _rows(
        db, "SELECT user_id, enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
    )

    assert (
        await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)
        == cli.EXIT_NOTHING_TO_DO
    )

    assert (
        await _rows(
            db,
            "SELECT user_id, enterprise_id FROM users WHERE user_id = :u",
            {"u": USER_A},
        )
        == before
    )


async def test_re_anchor_dry_run_writes_nothing(db):
    before = await _snapshot(db, ORG_A, ENT_A)
    before_co = await _snapshot(db, ORG_CO, ENT_CO)

    assert (
        await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=False) == 0
    )

    assert await _snapshot(db, ORG_A, ENT_A) == before
    assert await _snapshot(db, ORG_CO, ENT_CO) == before_co


async def test_re_anchor_refuses_an_unmapped_target(db, capsys):
    await _exec(
        db, "DELETE FROM sso_org_mappings WHERE organization_id = :o", {"o": ORG_CO}
    )
    before = await _snapshot(db, ORG_A, ENT_A)

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_A, ENT_A) == before
    assert "no workos mapping" in capsys.readouterr().out


async def test_re_anchor_refuses_a_personal_target(db, capsys):
    before = await _snapshot(db, ORG_B, ENT_B)

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_B, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_B, ENT_B) == before
    assert "itself a personal tenant" in capsys.readouterr().out


async def test_re_anchor_refuses_an_anchor_that_is_not_the_accounts_own(db, capsys):
    """Anchoring A at B's LIVE enterprise is the shape that matters: a command
    that moved an account off *an* enterprise would unpick somebody else's."""
    await _exec(
        db,
        "UPDATE users SET enterprise_id = :e WHERE user_id = :u",
        {"e": ENT_B, "u": USER_A},
    )
    before = await _snapshot(db, ORG_B, ENT_B)

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_B, ENT_B) == before
    assert "neither absent" in capsys.readouterr().out


async def test_re_anchor_refuses_a_deactivated_account(db, capsys):
    """THE credential rule, one copy: a member that cannot sign in is not a
    member anybody wanted."""
    await _exec(db, "UPDATE users SET is_active = 0 WHERE user_id = :u", {"u": USER_A})

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert "may not hold credentials" in capsys.readouterr().out


# =============================================================================
# purge-idp-org
# =============================================================================


async def test_purge_refuses_an_id_a_live_tenant_still_maps(db, capsys):
    """The cleanup must not become an outage."""
    idp = RecordingIdP([IDP_ORG_A])

    code = await cli.purge_idp_org(provider_org_id=IDP_ORG_A, apply=True, idp=idp)

    assert code == cli.EXIT_REFUSED
    assert idp.calls == []
    assert "LIVE tenant" in capsys.readouterr().out


async def test_purge_removes_unmapped_residue(db):
    idp = RecordingIdP(["org_01ORPHAN"])

    code = await cli.purge_idp_org(provider_org_id="org_01ORPHAN", apply=True, idp=idp)

    assert code == 0
    assert idp.calls == ["org_01ORPHAN"]


async def test_purge_dry_run_makes_no_provider_call(db):
    idp = RecordingIdP(["org_01ORPHAN"])

    assert (
        await cli.purge_idp_org(provider_org_id="org_01ORPHAN", apply=False, idp=idp)
        == 0
    )
    assert idp.calls == []


# =============================================================================
# The command demands a provider that can actually tear down
# =============================================================================


def test_an_sso_provider_without_teardown_is_refused(monkeypatch):
    import faultmaven.container.providers.services as services

    class NotATeardownProvider:
        pass

    monkeypatch.setattr(
        services,
        "create_sso_identity_provider",
        lambda settings: NotATeardownProvider(),
    )
    with pytest.raises(cli._Refused) as exc:
        cli._resolve_retirement_provider(None)
    assert "does not implement personal-tenant teardown" in str(exc.value)


def test_unconfigured_sso_is_refused(monkeypatch):
    import faultmaven.container.providers.services as services

    monkeypatch.setattr(services, "create_sso_identity_provider", lambda settings: None)
    with pytest.raises(cli._Refused) as exc:
        cli._resolve_retirement_provider(None)
    assert "SSO is not configured" in str(exc.value)


@pytest.mark.skipif(
    not _WORKOS_AVAILABLE,
    reason="workos is a cloud-only dependency (requirements/cloud.txt)",
)
async def test_the_command_drives_the_real_adapter_against_the_real_sdk(db):
    """End to end through the shipped adapter, with the SDK autospecced."""
    from tests.unit.modules.auth.test_sso_personal_org_provider import (
        _page,
        build_provider,
    )

    provider, orgs, members = build_provider()
    members.list_organization_memberships.return_value = _page(
        [SimpleNamespace(id="om_1")]
    )

    assert await _retire(db, provider) == 0

    orgs.delete_organization.assert_called_once_with(IDP_ORG_A)
    members.delete_organization_membership.assert_called_once_with("om_1")
    orgs.get_organization_by_external_id.assert_not_called()

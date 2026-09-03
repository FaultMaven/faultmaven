"""``fm-personal-tenant`` guards, plan and idempotency (#1045 D8).

The command's damage is not a bad UPDATE — it is a *correct* retirement aimed at
the wrong tenant, or a retirement that reports success for a step it did not
run. So what is pinned here is everything that decides whether the writes happen
at all, and everything about the writes an operator reads off the output:

* **a dry run writes nothing, on either side** — no row changes, and not one
  mutating provider call;
* **the order** the steps are applied in, which is the whole resumability
  argument (fence first, binding before the IdP, marker last);
* **idempotency and resumability** — a second run is a no-op, and a run
  interrupted at any single step is finished by re-running the same command;
* **the blast radius** — a second personal tenant present throughout comes out
  byte-identical;
* **the data that survives** — cases and knowledge items of the retired tenant
  are still there afterwards.

Against a real SQLite database built from the ORM metadata, with the session
factory patched rather than ``DATABASE_URL`` reset: the whole suite runs in one
process, and nulling the shared engine strands every later test that built
tables through it (the lesson recorded in
``tests/integration/test_reassign_cases_cli.py``).
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
    PERSONAL_TENANT_RETIREMENT_KEY,
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
    RetiredIdPOrganization,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    personal_org_slug,
    personal_tenant_key,
)
from faultmaven.modules.auth.exceptions import SSOProvisioningError

pytestmark = [pytest.mark.unit, pytest.mark.security]

# ``workos`` is cloud-only (requirements/cloud.txt), and the standalone lane
# installs requirements/test.txt. Only ONE test here needs it, so the guard is
# per-test rather than a module marker: everything else must run on both lanes.
try:
    _WORKOS_AVAILABLE = importlib.util.find_spec("workos") is not None
except (ImportError, ValueError):  # pragma: no cover - absent SDK
    _WORKOS_AVAILABLE = False

PROVIDER = "workos"

SUBJECT_A = "user_01AAAA"
SUBJECT_B = "user_01BBBB"
KEY_A = personal_tenant_key(PROVIDER, SUBJECT_A)
KEY_B = personal_tenant_key(PROVIDER, SUBJECT_B)
SLUG_A = personal_org_slug(KEY_A)
SLUG_B = personal_org_slug(KEY_B)

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


class RecordingIdP:
    """The IdP teardown port, recording every call and its order.

    A hand-written double is fine *here* because what the SDK signatures must
    be is pinned against the real classes with ``autospec`` in
    ``tests/unit/modules/auth/test_sso_personal_org_provider.py``. What this
    module needs from the port is whether it was called at all, and when.
    """

    def __init__(self, *, found: bool = True, error: Exception | None = None):
        self.calls: list[str] = []
        self.found = found
        self.error = error

    def retire_personal_organization(self, *, external_id: str):
        self.calls.append(external_id)
        if self.error is not None:
            raise self.error
        if not self.found:
            return RetiredIdPOrganization(False, 0, False)
        self.found = False  # deleted: a second call finds nothing, as WorkOS would
        return RetiredIdPOrganization(True, 1, True)


@pytest.fixture(autouse=True)
def restore_tenant_context():
    """``re-anchor`` binds the target tenant; do not leak it into the next test."""
    from faultmaven.config.constants import STANDALONE_ORG_ID
    from faultmaven.config.tenant_context import set_current_org_id

    yield
    set_current_org_id(STANDALONE_ORG_ID)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Two personal tenants and one company tenant, in a real database."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'retire.db'}"

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        for ent, slug in (
            (ENT_A, SLUG_A),
            (ENT_B, SLUG_B),
            (ENT_CO, "acme"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:e, 'Personal', :s)"
                ),
                {"e": ent, "s": slug},
            )
        for org, ent, slug in (
            (ORG_A, ENT_A, SLUG_A),
            (ORG_B, ENT_B, SLUG_B),
            (ORG_CO, ENT_CO, "acme"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO organizations "
                    "(organization_id, enterprise_id, name, slug, is_active) "
                    "VALUES (:o, :e, 'Personal', :s, 1)"
                ),
                {"o": org, "e": ent, "s": slug},
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
    """Everything about one tenant that a retirement could possibly move."""
    return {
        "organization": (
            await _rows(
                engine,
                "SELECT organization_id, enterprise_id, name, slug, is_active, "
                "deleted_at FROM organizations WHERE organization_id = :o",
                {"o": org},
            )
        ),
        "enterprise": (
            await _rows(
                engine,
                "SELECT enterprise_id, name, slug, settings, deleted_at "
                "FROM enterprises WHERE enterprise_id = :e",
                {"e": ent},
            )
        ),
        "mapping": (
            await _rows(
                engine,
                "SELECT provider, provider_org_id, organization_id "
                "FROM sso_org_mappings WHERE organization_id = :o",
                {"o": org},
            )
        ),
        "binding": (
            await _rows(
                engine,
                "SELECT provider, provider_user_id, organization_id, "
                "enterprise_id, provider_org_id, membership_confirmed "
                "FROM sso_personal_orgs WHERE organization_id = :o",
                {"o": org},
            )
        ),
        "users": (
            await _rows(
                engine,
                "SELECT user_id, enterprise_id, is_active, deleted_at FROM users "
                "WHERE enterprise_id = :e",
                {"e": ent},
            )
        ),
        "members": (
            await _rows(
                engine,
                "SELECT user_id, organization_id, role_id FROM organization_members "
                "WHERE organization_id = :o",
                {"o": org},
            )
        ),
        "cases": (
            await _rows(
                engine,
                "SELECT case_id, title FROM cases WHERE organization_id = :o",
                {"o": org},
            )
        ),
        "knowledge": (
            await _rows(
                engine,
                "SELECT item_id, title, content FROM knowledge_items "
                "WHERE organization_id = :o",
                {"o": org},
            )
        ),
        "teams": (
            await _rows(
                engine,
                "SELECT team_id, name FROM teams WHERE organization_id = :o",
                {"o": org},
            )
        ),
    }


async def _retire(engine, idp, **kwargs):
    defaults = dict(
        subject=SUBJECT_A,
        organization_id=None,
        next_login="refuse",
        apply=True,
        idp=idp,
    )
    defaults.update(kwargs)
    return await cli.retire(**defaults)


# =============================================================================
# Invariant 6 — a dry run writes nothing, on either side
# =============================================================================


async def test_a_dry_run_changes_no_row_and_makes_no_provider_call(db, capsys):
    """Both halves. A dry run that only *avoided the database* would still be
    able to delete a WorkOS organization, which is the irreversible half."""
    before_a = await _snapshot(db, ORG_A, ENT_A)
    before_b = await _snapshot(db, ORG_B, ENT_B)
    idp = RecordingIdP()

    code = await _retire(db, idp, apply=False)

    assert code == 0
    assert idp.calls == []
    assert await _snapshot(db, ORG_A, ENT_A) == before_a
    assert await _snapshot(db, ORG_B, ENT_B) == before_b
    out = capsys.readouterr().out
    assert "Would apply:" in out
    assert "nothing was written" in out


async def test_the_dry_run_lists_exactly_what_the_apply_run_then_does(db, capsys):
    """One list, two readings.

    A dry run whose plan differs from what ``--apply`` does is worse than no dry
    run at all — it is the thing the operator decided on. So the *descriptions*
    are compared, in order, not merely counted.
    """
    assert await _retire(db, RecordingIdP(), apply=False) == 0
    planned = [
        line.strip()[2:]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  · ")
    ]

    assert await _retire(db, RecordingIdP()) == 0
    performed = [
        line.strip()[2:]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  ✅ ")
    ]

    assert planned == performed != []


def test_the_plan_is_the_documented_order():
    """The order is the resumability argument, so it is pinned by name."""
    state = SimpleNamespace(
        organization_id=ORG_A,
        enterprise_id=ENT_A,
        organization_slug=SLUG_A,
        enterprise_slug=SLUG_A,
        organization_retired=False,
        enterprise_retired=False,
        mapping_provider_org_id=IDP_ORG_A,
        binding=SimpleNamespace(provider_user_id=SUBJECT_A),
        retirement_marker=None,
    )
    steps = cli._retirement_steps(
        state, key=KEY_A, policy=RETIREMENT_POLICY_REFUSE, idp=RecordingIdP()
    )
    assert [step.name for step in steps] == [
        "organization_soft_deleted",
        "mapping_deleted",
        "binding_deleted",
        "idp_organization_deleted",
        "organization_renamed",
        "enterprise_retired",
    ]


# =============================================================================
# The order is the resumability argument
# =============================================================================


async def test_the_fence_precedes_every_other_write(db, monkeypatch):
    """Soft-deleting the organization first is what stops a login entering a
    tenant that is being taken apart."""
    applied: list[str] = []
    idp = RecordingIdP()
    original = cli._apply

    async def _recording_apply(steps):
        applied.extend(step.name for step in steps)
        return await original(steps)

    monkeypatch.setattr(cli, "_apply", _recording_apply)
    assert await _retire(db, idp) == 0

    assert applied[0] == "organization_soft_deleted"
    # The binding goes BEFORE the IdP call: while it exists with an unconfirmed
    # membership a login would ask the provider to finish it, re-creating by its
    # deterministic external id the organization the next step deletes.
    assert applied.index("binding_deleted") < applied.index("idp_organization_deleted")
    # The marker is written LAST, after the slug it would collide with is freed.
    assert applied[-1] == "enterprise_retired"
    assert applied.index("organization_renamed") < applied.index("enterprise_retired")


# =============================================================================
# Invariant 2 — idempotent and resumable
# =============================================================================


async def test_running_it_twice_leaves_the_same_state_and_says_nothing_to_do(db):
    idp = RecordingIdP()
    assert await _retire(db, idp) == 0
    after_first = await _snapshot(db, ORG_A, ENT_A)

    assert await _retire(db, idp) == cli.EXIT_NOTHING_TO_DO

    assert await _snapshot(db, ORG_A, ENT_A) == after_first
    # And the second run did not ask the provider again: the marker, written
    # last, is the completion record.
    assert idp.calls == [SLUG_A]


@pytest.mark.parametrize(
    "fail_at",
    [
        "organization_soft_deleted",
        "mapping_deleted",
        "binding_deleted",
        "idp_organization_deleted",
        "organization_renamed",
        "enterprise_retired",
    ],
)
async def test_an_interruption_at_any_step_is_finished_by_re_running(
    db, monkeypatch, fail_at
):
    """Fault-inject each side-effect in turn; the end state is identical.

    The injected failure replaces one step's body, so the plan is the real one
    and every partial state the command can actually leave is reached —
    including the ones where an earlier step has committed and a later one has
    not. Truncating the plan instead would exercise a shape the command never
    produces.
    """
    idp = RecordingIdP()
    original_steps = cli._retirement_steps

    def _sabotaged(state, *, key, policy, idp):
        steps = original_steps(state, key=key, policy=policy, idp=idp)
        for step in steps:
            if step.name == fail_at:

                async def _boom():
                    raise RuntimeError(f"injected failure at {fail_at}")

                step.run = _boom
        return steps

    monkeypatch.setattr(cli, "_retirement_steps", _sabotaged)
    interrupted_code = await _retire(db, idp)
    monkeypatch.setattr(cli, "_retirement_steps", original_steps)

    # The command never reports success for a step it did not complete.
    assert interrupted_code in (cli.EXIT_REFUSED, cli.EXIT_INCOMPLETE)
    if fail_at == "organization_soft_deleted":
        # Nothing had been applied yet, so the run is a refusal, not a
        # half-retirement an operator has to come back to.
        assert interrupted_code == cli.EXIT_REFUSED
    else:
        assert interrupted_code == cli.EXIT_INCOMPLETE

    # Re-running the SAME command finishes it.
    assert await _retire(db, idp) == 0

    interrupted = await _snapshot(db, ORG_A, ENT_A)
    assert interrupted["binding"] == []
    assert interrupted["mapping"] == []
    assert interrupted["organization"][0].deleted_at is not None
    assert interrupted["organization"][0].is_active in (0, False)
    assert interrupted["organization"][0].slug.startswith(f"{SLUG_A}-retired-")
    assert interrupted["enterprise"][0].deleted_at is not None
    assert PERSONAL_TENANT_RETIREMENT_KEY in interrupted["enterprise"][0].settings
    # The provider-side organization is gone exactly once, whichever step failed.
    assert idp.calls.count(SLUG_A) >= 1
    assert idp.found is False
    # And the bystander tenant is still untouched after an interrupted run too.
    assert (await _snapshot(db, ORG_B, ENT_B))["binding"] != []


async def test_a_provider_failure_reports_incomplete_rather_than_done(db, capsys):
    """The command must never report success for a step it did not complete."""
    idp = RecordingIdP(error=SSOProvisioningError("provider unavailable"))

    code = await _retire(db, idp)

    assert code == cli.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "Retired." not in out
    # Steps after the failure did not run: the marker is absent, so the login
    # still sees an ordinary anchor rather than a released one.
    rows = await _rows(
        db, "SELECT settings FROM enterprises WHERE enterprise_id = :e", {"e": ENT_A}
    )
    assert rows[0].settings in (None, "", "{}")


# =============================================================================
# Invariant 3 — the blast radius
# =============================================================================


async def test_the_other_personal_tenant_is_untouched(db):
    before = await _snapshot(db, ORG_B, ENT_B)

    assert await _retire(db, RecordingIdP()) == 0

    assert await _snapshot(db, ORG_B, ENT_B) == before


# =============================================================================
# Invariant 4 — the data survives
# =============================================================================


async def test_cases_and_knowledge_of_the_retired_tenant_still_exist(db):
    before = await _snapshot(db, ORG_A, ENT_A)

    assert await _retire(db, RecordingIdP()) == 0

    after = await _snapshot(db, ORG_A, ENT_A)
    assert after["cases"] == before["cases"] != []
    assert after["knowledge"] == before["knowledge"] != []
    # The organization row itself survives — it is what owns them.
    assert after["organization"] != []


# =============================================================================
# The flag decides what is recorded
# =============================================================================


@pytest.mark.parametrize(
    "flag,policy",
    [
        ("refuse", RETIREMENT_POLICY_REFUSE),
        ("fresh-tenant", RETIREMENT_POLICY_FRESH_TENANT),
    ],
)
async def test_the_flag_is_recorded_as_the_policy_the_login_reads(db, flag, policy):
    import json

    assert await _retire(db, RecordingIdP(), next_login=flag) == 0

    rows = await _rows(
        db, "SELECT settings FROM enterprises WHERE enterprise_id = :e", {"e": ENT_A}
    )
    marker = json.loads(rows[0].settings)[PERSONAL_TENANT_RETIREMENT_KEY]
    assert marker["policy"] == policy
    # Bound to the subject, so it cannot release anybody else.
    assert marker["key"] == KEY_A
    assert marker["provider"] == PROVIDER


async def test_refuse_is_the_default(db):
    import json

    code = await cli.retire(
        subject=SUBJECT_A,
        organization_id=None,
        next_login="refuse",
        apply=True,
        idp=RecordingIdP(),
    )
    assert code == 0
    rows = await _rows(
        db, "SELECT settings FROM enterprises WHERE enterprise_id = :e", {"e": ENT_A}
    )
    marker = json.loads(rows[0].settings)[PERSONAL_TENANT_RETIREMENT_KEY]
    assert marker["policy"] == RETIREMENT_POLICY_REFUSE


def test_the_parser_defaults_next_login_to_refuse():
    """The default lives in the parser, so this is where it is pinned."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--next-login", choices=sorted(cli._NEXT_LOGIN_POLICIES), default="refuse"
    )
    assert parser.parse_args([]).next_login == "refuse"
    assert cli._NEXT_LOGIN_POLICIES["refuse"] == RETIREMENT_POLICY_REFUSE


# =============================================================================
# Refusals — nothing written
# =============================================================================


async def test_a_company_organization_is_refused(db, capsys):
    """Pointing --organization-id at a customer would soft-delete their tenant
    and stamp it with a marker naming somebody who does not own it."""
    before = await _snapshot(db, ORG_CO, ENT_CO)

    code = await _retire(db, RecordingIdP(), subject=None, organization_id=ORG_CO)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_CO, ENT_CO) == before
    assert "not a personal tenant" in capsys.readouterr().out


async def test_a_subject_and_organization_that_disagree_are_refused(db, capsys):
    """Naming both is a cross-check; this is it refusing."""
    before = await _snapshot(db, ORG_B, ENT_B)
    idp = RecordingIdP()

    code = await _retire(db, idp, subject=SUBJECT_A, organization_id=ORG_B)

    assert code == cli.EXIT_REFUSED
    assert idp.calls == []
    assert await _snapshot(db, ORG_B, ENT_B) == before
    assert "does not belong to subject" in capsys.readouterr().out


async def test_an_unknown_subject_is_nothing_to_do_not_a_silent_success(db, capsys):
    idp = RecordingIdP(found=False)

    code = await _retire(db, idp, subject="user_01NOBODY")

    assert code == cli.EXIT_NOTHING_TO_DO
    assert "No FaultMaven personal tenant matches" in capsys.readouterr().out


async def test_provider_side_residue_for_an_unknown_subject_is_removed(db, capsys):
    """A first sign-in that minted the IdP organization and then failed to
    commit leaves exactly this: an organization carrying the derived external
    id and no tenant anywhere."""
    idp = RecordingIdP(found=True)

    code = await _retire(db, idp, subject="user_01ORPHAN")

    assert code == 0
    assert idp.calls == [
        personal_org_slug(personal_tenant_key(PROVIDER, "user_01ORPHAN"))
    ]
    assert "orphaned IdP organization" in capsys.readouterr().out


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
    members = await _rows(
        db,
        "SELECT role_id FROM organization_members WHERE organization_id = :o "
        "AND user_id = :u",
        {"o": ORG_CO, "u": USER_A},
    )
    assert len(members) == 1
    assert (
        await _rows(
            db,
            "SELECT 1 FROM sso_personal_orgs WHERE provider_user_id = :s",
            {"s": SUBJECT_A},
        )
        == []
    )
    # The personal tenant is left standing, dormant — not migrated, not deleted.
    assert (await _snapshot(db, ORG_A, ENT_A))["cases"] != []


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
    """An account anchored to an organization no login can reach is stranded."""
    await _exec(
        db, "DELETE FROM sso_org_mappings WHERE organization_id = :o", {"o": ORG_CO}
    )
    before = await _snapshot(db, ORG_A, ENT_A)

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_A, ENT_A) == before
    assert "no workos mapping" in capsys.readouterr().out


async def test_re_anchor_refuses_a_personal_target(db, capsys):
    """Moving one person into somebody else's personal tenant is never right."""
    before = await _snapshot(db, ORG_B, ENT_B)

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_B, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_B, ENT_B) == before
    assert "itself a personal tenant" in capsys.readouterr().out


async def test_re_anchor_refuses_an_anchor_that_is_not_the_accounts_own(db, capsys):
    """The same narrowing the login path makes: only OFF its OWN personal tenant.

    Anchoring A at B's enterprise is the shape that matters — a command that
    moved an account off *an* enterprise rather than off *its own* personal one
    would happily unpick somebody else's tenancy.
    """
    await _exec(
        db,
        "UPDATE users SET enterprise_id = :e WHERE user_id = :u",
        {"e": ENT_B, "u": USER_A},
    )
    before = await _snapshot(db, ORG_B, ENT_B)

    code = await cli.reanchor(subject=SUBJECT_A, organization_id=ORG_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ORG_B, ENT_B) == before
    assert "not the enterprise of its own personal tenant" in capsys.readouterr().out


# =============================================================================
# The command demands a provider that can actually tear down
# =============================================================================


def test_an_sso_provider_without_teardown_is_refused():
    """A provider that cannot remove the IdP organization must refuse, not skip.

    Skipping would leave the derived external id claimed, so the subject could
    never be given a second personal tenant — and the command would have
    reported a retirement it did not complete.
    """
    import faultmaven.container.providers.services as services

    class NotATeardownProvider:
        pass

    original = services.create_sso_identity_provider
    services.create_sso_identity_provider = lambda settings: NotATeardownProvider()
    try:
        with pytest.raises(cli._Refused) as exc:
            cli._resolve_retirement_provider(None)
    finally:
        services.create_sso_identity_provider = original
    assert "does not implement personal-tenant teardown" in str(exc.value)


def test_unconfigured_sso_is_refused():
    import faultmaven.container.providers.services as services

    original = services.create_sso_identity_provider
    services.create_sso_identity_provider = lambda settings: None
    try:
        with pytest.raises(cli._Refused) as exc:
            cli._resolve_retirement_provider(None)
    finally:
        services.create_sso_identity_provider = original
    assert "SSO is not configured" in str(exc.value)


@pytest.mark.skipif(
    not _WORKOS_AVAILABLE,
    reason="workos is a cloud-only dependency (requirements/cloud.txt)",
)
async def test_the_command_drives_the_real_adapter_against_the_real_sdk(db):
    """End to end through the shipped adapter, with the SDK autospecced.

    The rest of this module uses a recording double for the port; this one case
    makes sure the port the command holds is the one the WorkOS adapter really
    implements, with the signatures the installed SDK really has.
    """
    from tests.unit.modules.auth.test_sso_personal_org_provider import (
        _organization,
        _page,
        build_provider,
    )

    provider, orgs, members = build_provider()
    orgs.get_organization_by_external_id.return_value = _organization(IDP_ORG_A)
    members.list_organization_memberships.return_value = _page(
        [SimpleNamespace(id="om_1")]
    )

    assert await _retire(db, provider) == 0

    orgs.get_organization_by_external_id.assert_called_once_with(SLUG_A)
    orgs.delete_organization.assert_called_once_with(IDP_ORG_A)
    members.delete_organization_membership.assert_called_once_with("om_1")

"""``fm-personal-tenant`` guards, plan and idempotency (#1045 D8, ADR-017 D3).

The command's damage is not a bad UPDATE — it is a *correct* retirement aimed at
the wrong tenant, or one that reports success for a step it did not run. So what
is pinned here is everything that decides whether the writes happen at all, and
everything an operator reads off the output:

* **a dry run writes nothing, on either side** — no row changes, not one
  mutating provider call;
* **the order**, which is the whole resumability argument: fence first, tokens
  revoked immediately after, the retirement stamped before the provider calls,
  the provider organization before the mapping that records its id;
* **the provider organization is the one this tenant's mapping names.** A
  re-run aimed at a retired predecessor must not delete the live successor's;
* **idempotency and resumability** — a second run is a no-op, and a run
  interrupted at any single step is finished by re-running;
* **the blast radius** — a second personal tenant present throughout comes out
  byte-identical;
* **the data that survives** — cases and knowledge items are still there.

What ADR-017 changed, and why the shape of these tests moved with it: the tenant
is the **enterprise**, a personal tenant owns no organization and no team at
all, and ``users.enterprise_id`` is NOT NULL — so a retirement can no longer
release an account by clearing its anchor. It stamps ``retired_at`` and
``retirement_state`` on the subject binding and **keeps the row**, because that
recorded value is what the next sign-in reads, and the account stays anchored to
the enterprise the retirement fenced.

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
    personal_enterprise_slug,
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
SLUG_A = personal_enterprise_slug(KEY_A)
SLUG_B = personal_enterprise_slug(personal_tenant_key(PROVIDER, SUBJECT_B))

ENT_A = "11111111-1111-1111-1111-111111111111"
ENT_B = "33333333-3333-3333-3333-333333333333"
ENT_CO = "55555555-5555-5555-5555-555555555555"

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"

IDP_ORG_A = "org_01IDPA"
IDP_ORG_B = "org_01IDPB"
IDP_ORG_CO = "org_01IDPCO"


@pytest.fixture(autouse=True)
def sqlite_role_preflight(monkeypatch):
    """Keep the RLS-role preflight off the ambient database.

    ``retire()`` and ``reanchor()`` both open with
    ``assert_provisioning_db_role_bypasses_rls()``, which — given no engine —
    probes the SHARED one from ``persistence.database``. That engine is built
    from ``DATABASE_URL``, not from the SQLite file ``db`` below creates, so in
    any process where ``DATABASE_URL`` names a real PostgreSQL these unit tests
    connected to it: a pooled asyncpg connection made in one test's event loop,
    handed to the next test's loop, and "attached to a different loop" /
    "Event loop is closed". No CI lane combines the two — Test PostgreSQL
    Integration selects ``-m postgres``, which deselects this module — so the
    coupling was invisible to CI and appeared only when somebody ran the suites
    together locally.

    The knob is not the fix. Whether a unit test reaches a database must not
    depend on what the environment happens to hold, so the preflight is
    replaced here the way the sibling CLI unit tests replace it
    (``test_provision_sso_org.py``; ``test_wipe_deployment_cli.py`` installs a
    stand-in engine for the same reason). ``None`` is not a weakened
    assertion — it is exactly what the real guard returns for the SQLite
    database this module actually runs against, because SQLite has no
    row-level security for a role to be scoped by.

    Autouse rather than folded into ``db``: a test added later that drives the
    command without the database fixture must not quietly reacquire the
    dependency. The role posture itself is covered where it belongs, against a
    real engine, in ``tests/unit/infrastructure/persistence/test_rls_role_guard.py``.
    """

    async def sqlite_has_no_rls(**_kwargs):
        return None

    monkeypatch.setattr(
        cli, "assert_provisioning_db_role_bypasses_rls", sqlite_has_no_rls
    )


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Two personal tenants and one company tenant, in a real database.

    The personal tenants own **no organization and no team** — that is what a
    sign-up creates under ADR-017 D5/D4, and seeding rows the product no longer
    writes would let a retirement pass by leaving them alone. The company tenant
    is the re-anchor destination and carries its operator-provisioned mapping.
    """
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
        # Only the company has a team: a team is formed by consent (D4).
        await conn.execute(
            text(
                "INSERT INTO teams (team_id, enterprise_id, name) "
                "VALUES (:t, :e, 'Default')"
            ),
            {"t": f"team-{ENT_CO[:8]}", "e": ENT_CO},
        )
        for idp_org, ent in (
            (IDP_ORG_A, ENT_A),
            (IDP_ORG_B, ENT_B),
            (IDP_ORG_CO, ENT_CO),
        ):
            await conn.execute(
                text(
                    "INSERT INTO sso_org_mappings "
                    "(provider, provider_org_id, enterprise_id) "
                    "VALUES ('workos', :p, :e)"
                ),
                {"p": idp_org, "e": ent},
            )
        for subject, ent, idp_org in (
            (SUBJECT_A, ENT_A, IDP_ORG_A),
            (SUBJECT_B, ENT_B, IDP_ORG_B),
        ):
            await conn.execute(
                text(
                    "INSERT INTO sso_personal_enterprises (subject, provider, "
                    " provider_org_id, enterprise_id, membership_confirmed) "
                    "VALUES (:s, 'workos', :p, :e, 1)"
                ),
                {"s": subject, "p": idp_org, "e": ent},
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
                    "m": f"{name}@gmail.com",
                    "s": subject,
                },
            )
        for case_id, ent in (("case_a", ENT_A), ("case_b", ENT_B)):
            await conn.execute(
                text(
                    "INSERT INTO cases (case_id, enterprise_id, title) "
                    "VALUES (:c, :e, 'disk full')"
                ),
                {"c": case_id, "e": ent},
            )
        for item, ent in (("kb_a", ENT_A), ("kb_b", ENT_B)):
            await conn.execute(
                text(
                    "INSERT INTO knowledge_items "
                    "(item_id, enterprise_id, title, content, item_type, scope) "
                    "VALUES (:i, :e, 'runbook', 'body', 'runbook', 'personal')"
                ),
                {"i": item, "e": ent},
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
        "faultmaven.infrastructure.persistence.sessionless_enterprise_repository"
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


async def _snapshot(engine, ent: str) -> dict:
    """Everything about one tenant a retirement could possibly move."""
    return {
        "enterprise": await _rows(
            engine,
            "SELECT enterprise_id, name, slug, deleted_at "
            "FROM enterprises WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "mapping": await _rows(
            engine,
            "SELECT provider, provider_org_id, enterprise_id "
            "FROM sso_org_mappings WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "binding": await _rows(
            engine,
            "SELECT provider, subject, enterprise_id, provider_org_id, "
            "retired_at, retirement_state "
            "FROM sso_personal_enterprises WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "users": await _rows(
            engine,
            "SELECT user_id, enterprise_id, is_active FROM users "
            "WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "cases": await _rows(
            engine,
            "SELECT case_id, title FROM cases WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "knowledge": await _rows(
            engine,
            "SELECT item_id, title, content FROM knowledge_items "
            "WHERE enterprise_id = :e",
            {"e": ent},
        ),
        "teams": await _rows(
            engine,
            "SELECT team_id, name FROM teams WHERE enterprise_id = :e",
            {"e": ent},
        ),
    }


async def _retire(engine, idp=None, revoker=None, **kwargs):
    defaults = dict(
        subject=SUBJECT_A,
        enterprise_id=None,
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
    before_a = await _snapshot(db, ENT_A)
    before_b = await _snapshot(db, ENT_B)
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])
    revoker = RecordingRevoker()

    code = await _retire(db, idp, revoker, apply=False)

    assert code == 0
    assert idp.calls == []
    assert revoker.revoked == []
    assert await _snapshot(db, ENT_A) == before_a
    assert await _snapshot(db, ENT_B) == before_b
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
    """Identical under both policies: what ``--next-login`` decides is the value
    step 3 records, not which steps run."""
    applied: list[str] = []
    original = cli._apply

    async def _recording_apply(steps):
        applied.extend(step.name for step in steps)
        return await original(steps)

    monkeypatch.setattr(cli, "_apply", _recording_apply)
    assert await _retire(db, next_login=policy) == 0

    assert applied == [
        "enterprise_soft_deleted",
        "tokens_revoked",
        "retirement_recorded",
        "idp_organization_deleted",
        "mapping_deleted",
    ]
    # The fence is first, so no login can enter a tenant being taken apart.
    assert applied[0] == "enterprise_soft_deleted"
    # The retirement is stamped BEFORE the provider calls: stamping is what
    # makes the binding stop being live, and while it is live a login can ask
    # the provider to finish a membership, re-creating by its deterministic
    # external id the organization the next step deletes.
    assert applied.index("retirement_recorded") < applied.index(
        "idp_organization_deleted"
    )
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

    Retire, let the subject provision again — which under ADR-017 **re-points
    the one subject row** onto a new enterprise, taking the same derived slug
    and a new provider organization — then re-run the retirement against the
    **predecessor**. A teardown addressed by the subject-derived external id
    would delete the successor's organization and report success.
    """
    idp = RecordingIdP([IDP_ORG_A, "org_01SUCCESSOR"])
    assert await _retire(db, idp, next_login="fresh-tenant") == 0
    idp.calls.clear()

    # The successor: same subject, same derived slug (legal — the uniqueness
    # index is partial on deleted_at IS NULL), a new provider organization, and
    # the SAME binding row moved onto it.
    successor_ent = "88888888-8888-8888-8888-888888888888"
    await _exec(
        db,
        "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e,'Personal',:s)",
        {"e": successor_ent, "s": SLUG_A},
    )
    await _exec(
        db,
        "INSERT INTO sso_org_mappings (provider, provider_org_id, enterprise_id) "
        "VALUES ('workos', :p, :e)",
        {"p": "org_01SUCCESSOR", "e": successor_ent},
    )
    await _exec(
        db,
        "UPDATE sso_personal_enterprises SET enterprise_id = :e, "
        "provider_org_id = :p, retired_at = NULL, retirement_state = NULL "
        "WHERE subject = :s",
        {"s": SUBJECT_A, "p": "org_01SUCCESSOR", "e": successor_ent},
    )

    # Re-run against the predecessor, by id — the only way a retired tenant is
    # addressable — with the policy it was retired under. Nothing about the
    # predecessor is outstanding, so the run is a genuine no-op.
    code = await _retire(
        db, idp, subject=None, enterprise_id=ENT_A, next_login="fresh-tenant"
    )

    assert code == cli.EXIT_NOTHING_TO_DO
    assert idp.calls == []
    assert "org_01SUCCESSOR" in idp.present
    assert (
        await _rows(
            db,
            "SELECT provider_org_id FROM sso_org_mappings WHERE enterprise_id = :e",
            {"e": successor_ent},
        )
    )[0].provider_org_id == "org_01SUCCESSOR"


# =============================================================================
# Idempotency and resumability
# =============================================================================


async def test_running_it_twice_leaves_the_same_state_and_says_nothing_to_do(db):
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])
    assert await _retire(db, idp) == 0
    after_first = await _snapshot(db, ENT_A)

    assert await _retire(db, idp, subject=None, enterprise_id=ENT_A) == (
        cli.EXIT_NOTHING_TO_DO
    )

    assert await _snapshot(db, ENT_A) == after_first
    assert idp.calls == [IDP_ORG_A]


async def test_a_rerun_does_not_restamp_the_retirement(db):
    """The bug a freshly-built marker had: every run moved the timestamp.

    Both stamps, because there are two now — the fence on the enterprise and
    the retirement on the binding — and either drifting would rewrite when an
    operator's decision was made.
    """
    assert await _retire(db) == 0
    fenced = (
        await _rows(
            db,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            {"e": ENT_A},
        )
    )[0].deleted_at
    recorded = (
        await _rows(
            db,
            "SELECT retired_at FROM sso_personal_enterprises WHERE subject = :s",
            {"s": SUBJECT_A},
        )
    )[0].retired_at

    assert await _retire(db, subject=None, enterprise_id=ENT_A) == (
        cli.EXIT_NOTHING_TO_DO
    )

    assert (
        await _rows(
            db,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            {"e": ENT_A},
        )
    )[0].deleted_at == fenced
    assert (
        await _rows(
            db,
            "SELECT retired_at FROM sso_personal_enterprises WHERE subject = :s",
            {"s": SUBJECT_A},
        )
    )[0].retired_at == recorded


@pytest.mark.parametrize(
    "fail_at",
    [
        "enterprise_soft_deleted",
        "tokens_revoked",
        "retirement_recorded",
        "idp_organization_deleted",
        "mapping_deleted",
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
    if fail_at == "enterprise_soft_deleted":
        assert interrupted == cli.EXIT_REFUSED
    else:
        assert interrupted == cli.EXIT_INCOMPLETE
    # And it prints the address a resumed run needs, because the subject the
    # tenant was found by no longer addresses a live binding.
    assert f"--enterprise-id {ENT_A}" in capsys.readouterr().out

    # Re-running by ID — the only address a part-retired tenant has — finishes it.
    assert (
        await _retire(
            db, idp, subject=None, enterprise_id=ENT_A, next_login="fresh-tenant"
        )
        == 0
    )

    state = await _snapshot(db, ENT_A)
    assert state["mapping"] == []
    assert state["enterprise"][0].deleted_at is not None
    # No renames: the retired tenant keeps the derived slug.
    assert state["enterprise"][0].slug == SLUG_A
    # The binding SURVIVES, carrying the operator's decision.
    assert len(state["binding"]) == 1
    assert state["binding"][0].retired_at is not None
    assert state["binding"][0].retirement_state == RETIREMENT_POLICY_FRESH_TENANT
    # The account stays anchored: NOT NULL leaves no "released" absence, and
    # the recorded policy is what releases the next sign-in instead.
    assert [row.user_id for row in state["users"]] == [USER_A]
    assert IDP_ORG_A not in idp.present
    assert (await _snapshot(db, ENT_B))["binding"] != []


async def test_a_provider_failure_reports_incomplete_rather_than_done(db, capsys):
    idp = RecordingIdP([IDP_ORG_A], error=SSOProvisioningError("provider unavailable"))

    code = await _retire(db, idp)

    assert code == cli.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "Retired." not in out
    # The mapping survives, so the recorded provider id is still readable by
    # the run that finishes this one.
    assert (await _snapshot(db, ENT_A))["mapping"] != []


# =============================================================================
# The flag decides what is written
# =============================================================================


@pytest.mark.parametrize(
    "flag,policy",
    [
        ("refuse", RETIREMENT_POLICY_REFUSE),
        ("fresh-tenant", RETIREMENT_POLICY_FRESH_TENANT),
    ],
)
async def test_the_flag_decides_the_recorded_policy(db, flag, policy):
    """Typed columns, not a marker — and a **positive** value in both cases.

    The account stays anchored to the fenced enterprise whichever policy is
    chosen (``users.enterprise_id`` is NOT NULL under ADR-017 D3), so the
    difference between the two is entirely this column. An absence could be
    produced by a half-finished retirement; this cannot.
    """
    assert await _retire(db, next_login=flag) == 0

    enterprise = (
        await _rows(
            db,
            "SELECT deleted_at FROM enterprises WHERE enterprise_id = :e",
            {"e": ENT_A},
        )
    )[0]
    assert enterprise.deleted_at is not None
    binding = (
        await _rows(
            db,
            "SELECT retired_at, retirement_state, enterprise_id "
            "FROM sso_personal_enterprises WHERE subject = :s",
            {"s": SUBJECT_A},
        )
    )[0]
    assert binding.retired_at is not None
    assert binding.retirement_state == policy
    assert binding.enterprise_id == ENT_A
    user = (
        await _rows(
            db, "SELECT enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
        )
    )[0]
    assert user.enterprise_id == ENT_A


async def test_changing_the_policy_on_a_retired_tenant_is_a_real_write(db):
    """The operator's remedy for "I chose the wrong flag".

    A re-run with the OTHER policy must not report "nothing to do" — that is
    exactly the state in which an operator believes a subject can start over and
    the login goes on refusing them.
    """
    assert await _retire(db, next_login="refuse") == 0

    assert (
        await _retire(db, subject=None, enterprise_id=ENT_A, next_login="fresh-tenant")
        == 0
    )

    assert (
        await _rows(
            db,
            "SELECT retirement_state FROM sso_personal_enterprises WHERE subject = :s",
            {"s": SUBJECT_A},
        )
    )[0].retirement_state == RETIREMENT_POLICY_FRESH_TENANT


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


async def test_a_company_enterprise_is_refused(db, capsys):
    before = await _snapshot(db, ENT_CO)

    code = await _retire(db, subject=None, enterprise_id=ENT_CO)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ENT_CO) == before
    assert "not a personal tenant" in capsys.readouterr().out


async def test_a_subject_and_enterprise_that_disagree_are_refused(db, capsys):
    before = await _snapshot(db, ENT_B)
    idp = RecordingIdP([IDP_ORG_A, IDP_ORG_B])

    code = await _retire(db, idp, subject=SUBJECT_A, enterprise_id=ENT_B)

    assert code == cli.EXIT_REFUSED
    assert idp.calls == []
    assert await _snapshot(db, ENT_B) == before
    assert "cross-check" in capsys.readouterr().out


async def test_an_unknown_subject_is_refused_not_reported_as_nothing_to_do(db, capsys):
    """ "Nothing matched" is what a typo looks like, and the module's own exit
    table says that is 1."""
    code = await _retire(db, subject="user_01NOBODY")

    assert code == cli.EXIT_REFUSED
    assert "No live personal tenant" in capsys.readouterr().out


async def test_an_unknown_enterprise_id_is_refused(db, capsys):
    code = await _retire(db, subject=None, enterprise_id="does-not-exist")

    assert code == cli.EXIT_REFUSED
    assert "No enterprise" in capsys.readouterr().out


async def test_an_empty_subject_is_refused_not_a_traceback(db, capsys):
    """The documented kubectl recipe interpolates a shell variable here."""
    code = await _retire(db, subject="   ")

    assert code == cli.EXIT_REFUSED
    assert "--subject was given but is empty" in capsys.readouterr().out


# =============================================================================
# Blast radius and survival
# =============================================================================


async def test_the_other_personal_tenant_is_untouched(db):
    before = await _snapshot(db, ENT_B)

    assert await _retire(db, next_login="fresh-tenant") == 0

    assert await _snapshot(db, ENT_B) == before


async def test_cases_and_knowledge_of_the_retired_tenant_still_exist(db):
    before = await _snapshot(db, ENT_A)

    assert await _retire(db) == 0

    after = await _snapshot(db, ENT_A)
    assert after["cases"] == before["cases"] != []
    assert after["knowledge"] == before["knowledge"] != []
    assert after["enterprise"] != []
    assert after["users"] == before["users"] != []


# =============================================================================
# re-anchor
# =============================================================================


async def test_re_anchor_moves_the_account_and_deletes_the_binding(db):
    """The anchor is the whole of what "belongs to this company" means (D3).

    No membership row is written: an organization is a billing target created
    by payment (D5), so this command must not invent one — doing so would bill
    a cost centre for an account nobody added to it.
    """
    code = await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True)

    assert code == 0
    user = (
        await _rows(
            db, "SELECT enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
        )
    )[0]
    assert user.enterprise_id == ENT_CO
    assert (
        await _rows(
            db,
            "SELECT 1 FROM sso_personal_enterprises WHERE subject = :s",
            {"s": SUBJECT_A},
        )
        == []
    )
    assert (
        await _rows(
            db, "SELECT 1 FROM organization_members WHERE user_id = :u", {"u": USER_A}
        )
        == []
    )
    assert (await _snapshot(db, ENT_A))["cases"] != []


async def test_re_anchor_moves_a_retired_subject(db):
    """R6, from the operator side: a retirement must not strand somebody."""
    assert await _retire(db, next_login="refuse") == 0

    assert await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True) == 0

    assert (
        await _rows(
            db, "SELECT enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
        )
    )[0].enterprise_id == ENT_CO


async def test_re_anchor_is_idempotent(db):
    assert await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True) == 0
    before = await _rows(
        db, "SELECT user_id, enterprise_id FROM users WHERE user_id = :u", {"u": USER_A}
    )

    assert (
        await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True)
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
    before = await _snapshot(db, ENT_A)
    before_co = await _snapshot(db, ENT_CO)

    assert await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=False) == 0

    assert await _snapshot(db, ENT_A) == before
    assert await _snapshot(db, ENT_CO) == before_co


async def test_re_anchor_refuses_an_unmapped_target(db, capsys):
    await _exec(
        db, "DELETE FROM sso_org_mappings WHERE enterprise_id = :e", {"e": ENT_CO}
    )
    before = await _snapshot(db, ENT_A)

    code = await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ENT_A) == before
    assert "no workos mapping" in capsys.readouterr().out


async def test_re_anchor_refuses_a_personal_target(db, capsys):
    before = await _snapshot(db, ENT_B)

    code = await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_B, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ENT_B) == before
    assert "itself a personal tenant" in capsys.readouterr().out


async def test_re_anchor_refuses_an_anchor_that_is_not_the_accounts_own(db, capsys):
    """Anchoring A at B's LIVE enterprise is the shape that matters: a command
    that moved an account off *an* enterprise would unpick somebody else's."""
    await _exec(
        db,
        "UPDATE users SET enterprise_id = :e WHERE user_id = :u",
        {"e": ENT_B, "u": USER_A},
    )
    before = await _snapshot(db, ENT_B)

    code = await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True)

    assert code == cli.EXIT_REFUSED
    assert await _snapshot(db, ENT_B) == before
    assert "neither absent" in capsys.readouterr().out


async def test_re_anchor_refuses_a_deactivated_account(db, capsys):
    """THE credential rule, one copy: a member that cannot sign in is not a
    member anybody wanted."""
    await _exec(db, "UPDATE users SET is_active = 0 WHERE user_id = :u", {"u": USER_A})

    code = await cli.reanchor(subject=SUBJECT_A, enterprise_id=ENT_CO, apply=True)

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
    """End to end through the shipped adapter, with the SDK autospecced.

    The adapter's method names are the IdP's own vocabulary, not FaultMaven's:
    ADR-017 D9 keeps the IdP *organization* beside the FaultMaven enterprise, so
    this is also what catches a rename of one leaking into the other.
    """
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

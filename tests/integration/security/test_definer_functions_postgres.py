"""What a ``SECURITY DEFINER`` function's body resolves to, on a real PostgreSQL.

A definer function runs with its OWNER's rights, so every name in its body — a
relation, a function, an operator — must resolve to the object its author meant,
whoever calls it and whatever objects they have created. The chain has four:
the baseline's two trigger guards (``organization_members_last_admin_guard``,
``team_members_same_enterprise_guard``) and revision 003's two operator reads
(``admin_case_metadata_page``, ``admin_case_metadata_count``). Revision 005
re-creates them with ``search_path = pg_catalog, pg_temp`` and every relation
schema-qualified. What only a real database can show:

* **A planted operator or function is never run.** PostgreSQL resolves a
  function or operator name by argument types before search-path position: an
  exact match in ANY schema on the path wins over a ``pg_catalog`` candidate
  that needs an implicit cast. The bodies compare ``character varying`` columns
  with ``text`` (``k.state = p_state``) and with each other
  (``team_id = NEW.team_id``), and ``pg_catalog`` has no exact ``=`` for either
  shape. With ``public`` on the path, a role allowed to create in ``public`` —
  every role, by default, before PostgreSQL 15 — could define one there and
  have the body run it with the owner's rights.
* **Re-creating them changed their settings and bodies and nothing else.**
  ``CREATE OR REPLACE`` keeps each function's OID, so its owner, its grants
  (003's ``EXECUTE`` for ``faultmaven_app`` and none for ``PUBLIC``), its
  comment and the triggers that call it carry over. Stepping down restores the
  settings each function had before.

Each test migrates a database of its own. An operator planted in ``public``
reaches every session of its database, so it is never planted in the one the
rest of the lane shares — and dropping the database is what removes it, however
the test ends.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from tests.integration.security.conftest import (
    create_limited_role,
    drop_role_sql,
    limited_url,
)
from tests.utils import (
    seed_enterprises,
    seed_organizations,
    seed_teams,
    seed_users,
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

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
#: ``005_definer_search_path_without_public``, which re-creates the four.
_REVISION = "1c5a2ad13a65"
#: ``004_definer_trigger_hardening``, the revision 005 parents onto.
_PARENT_REVISION = "14d4bfdd406e"
#: The deployment's runtime role, which revision 003 grants EXECUTE by name.
_RUNTIME_ROLE = "faultmaven_app"
_PW = "fm_definer_probe_pw"

#: Every definer function the chain creates, by name.
_DEFINER_FUNCTIONS = (
    "admin_case_metadata_count",
    "admin_case_metadata_page",
    "organization_members_last_admin_guard",
    "team_members_same_enterprise_guard",
)
_GUARD_TRIGGERS = ("organization_members_last_admin", "team_members_same_enterprise")
_ADMIN = SYSTEM_ROLE_IDS[Role.ADMIN]
_MEMBER = SYSTEM_ROLE_IDS[Role.MEMBER]

#: What a role allowed to create in ``public`` can plant there: the two ``=``
#: shapes the bodies compare with, and ``lower(character varying)``, the
#: textbook case of a function ``pg_catalog`` only matches through a cast. Each
#: raises, naming itself and the role it ran as, so a body that resolved to one
#: fails with the evidence in its message. Plain ``CREATE``: planting into a
#: database that already holds them is a test bug, not something to paper over.
_PLANTED = (
    """
    CREATE FUNCTION public.fm_planted_eq_varchar_text(character varying, text)
    RETURNS boolean LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'planted =(character varying, text) ran as %', current_user;
    END
    $$
    """,
    """
    CREATE OPERATOR public.= (
        LEFTARG = character varying,
        RIGHTARG = text,
        FUNCTION = public.fm_planted_eq_varchar_text
    )
    """,
    """
    CREATE FUNCTION public.fm_planted_eq_varchar_varchar(
        character varying, character varying
    )
    RETURNS boolean LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'planted =(character varying, character varying) ran as %',
            current_user;
    END
    $$
    """,
    """
    CREATE OPERATOR public.= (
        LEFTARG = character varying,
        RIGHTARG = character varying,
        FUNCTION = public.fm_planted_eq_varchar_varchar
    )
    """,
    """
    CREATE FUNCTION public.lower(character varying)
    RETURNS text LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'planted lower(character varying) ran as %', current_user;
    END
    $$
    """,
)


def _alembic(url: str, command: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=url)
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@asynccontextmanager
async def _database_of_its_own(prefix: str, *, runtime_role: bool = False):
    """A fresh database on the lane's cluster, dropped however the test ends.

    ``runtime_role`` makes sure ``faultmaven_app`` exists while it does, so a
    migration's grant to it runs. Roles are cluster-wide: it is created only if
    absent, and dropped only if created here.
    """
    superuser_url = os.environ["DATABASE_URL"]
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(superuser_url, isolation_level="AUTOCOMMIT")
    created_runtime_role = False
    try:
        async with admin.connect() as conn:
            if runtime_role:
                created_runtime_role = not (
                    await conn.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"
                        ),
                        {"r": _RUNTIME_ROLE},
                    )
                ).scalar()
                if created_runtime_role:
                    await conn.execute(text(f"CREATE ROLE {_RUNTIME_ROLE} NOLOGIN"))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield (
            make_url(superuser_url)
            .set(database=name)
            .render_as_string(hide_password=False)
        )
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            if created_runtime_role:
                await conn.execute(text(f"DROP ROLE IF EXISTS {_RUNTIME_ROLE}"))
        await admin.dispose()


async def _execute(url: str, *statements: str, bind: str | None = None, **params):
    """Run ``statements`` in one transaction on a fresh connection and return the
    last one's rows. The connection is new each time, so no plan cached before a
    plant can answer for a body after it."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                if bind is not None:
                    await conn.execute(
                        text(
                            "SELECT set_config('app.current_enterprise_id', :e, true)"
                        ),
                        {"e": bind},
                    )
                result = None
                for statement in statements:
                    result = await conn.execute(text(statement), params)
                return (
                    result.all() if result is not None and result.returns_rows else []
                )
    finally:
        await engine.dispose()


async def test_a_planted_operator_or_function_never_runs_inside_a_definer_function():
    """Every role may create in ``public`` here, as every role may on
    PostgreSQL 14 and earlier. A limited role — the deployed runtime grants and
    nothing it owns — plants ``=`` operators and a ``lower`` in ``public``, then
    drives each definer function: both case-metadata reads, an insert the
    membership guard admits and one it refuses, a demotion the last-admin guard
    admits and one it refuses.

    The caller's own statements run under ``search_path = pg_catalog``, so the
    only place a plant could be resolved is inside a definer body, under the
    function's own setting. A positive control first proves the plants are live
    wherever ``public`` IS searched — without it, a plant PostgreSQL never
    chose would pass this test for the wrong reason.
    """
    async with _database_of_its_own("fm_definer_plant") as owner_url:
        result = _alembic(owner_url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]

        ent_a, ent_b = f"ent_a_{uuid.uuid4().hex[:8]}", f"ent_b_{uuid.uuid4().hex[:8]}"
        team_a, org_a = (
            f"team_a_{uuid.uuid4().hex[:8]}",
            f"org_a_{uuid.uuid4().hex[:8]}",
        )
        member, stranger = "member_a", "stranger_b"
        first_admin, last_admin = "admin_a_1", "admin_a_2"
        case_id = f"case_{uuid.uuid4().hex[:8]}"
        owner = create_async_engine(owner_url)
        try:
            async with AsyncSession(bind=owner, expire_on_commit=False) as session:
                await seed_enterprises(session, [ent_a, ent_b])
                await seed_teams(session, [team_a], enterprise_id=ent_a)
                await seed_organizations(session, [org_a], enterprise_id=ent_a)
                await seed_users(
                    session, [member, first_admin, last_admin], enterprise_id=ent_a
                )
                await seed_users(session, [stranger], enterprise_id=ent_b)
                for admin in (first_admin, last_admin):
                    await session.execute(
                        text(
                            "INSERT INTO public.organization_members "
                            "(user_id, organization_id, enterprise_id, role_id) "
                            "VALUES (:u, :o, :e, :r)"
                        ),
                        {"u": admin, "o": org_a, "e": ent_a, "r": _ADMIN},
                    )
                await session.execute(
                    text(
                        "INSERT INTO public.cases (case_id, enterprise_id, title) "
                        "VALUES (:c, :e, 'planted-operator probe')"
                    ),
                    {"c": case_id, "e": ent_a},
                )
                await session.execute(
                    text(
                        "INSERT INTO public.resource_shares (share_id, resource_type, "
                        "resource_id, scope_type, scope_id, enterprise_id) "
                        "VALUES (:s, 'case', :c, 'team', :t, :e)"
                    ),
                    {"s": f"share_{case_id}", "c": case_id, "t": team_a, "e": ent_a},
                )
                # PostgreSQL 14 and earlier: every role may create in public.
                await session.execute(text("GRANT CREATE ON SCHEMA public TO PUBLIC"))
                await session.commit()
        finally:
            await owner.dispose()

        role = f"fm_definer_planter_{uuid.uuid4().hex[:8]}"
        try:
            await create_limited_role(owner_url, role, _PW)
            caller = limited_url(owner_url, role, _PW)
            await _execute(caller, *_PLANTED)

            # Positive control: under a path that includes public, PostgreSQL
            # picks every plant over pg_catalog's candidates.
            for probe, plant in (
                (
                    "SELECT CAST('x' AS character varying) = CAST('x' AS text)",
                    "=(character varying, text)",
                ),
                (
                    "SELECT CAST('x' AS character varying) "
                    "= CAST('x' AS character varying)",
                    "=(character varying, character varying)",
                ),
                (
                    "SELECT lower(CAST('X' AS character varying))",
                    "lower(character varying)",
                ),
            ):
                with pytest.raises(DBAPIError) as caught:
                    await _execute(caller, "SET LOCAL search_path = public", probe)
                assert f"planted {plant} ran as {role}" in str(caught.value)

            confined = "SET LOCAL search_path = pg_catalog"
            calls = {
                "count": (
                    confined,
                    "SELECT public.admin_case_metadata_count('inquiry', 'copilot')",
                ),
                "page": (
                    confined,
                    "SELECT case_id, shared_team_ids FROM public.admin_case_metadata_page"
                    "('inquiry', 'copilot', 10, 0)",
                ),
                "admit member": (
                    confined,
                    "INSERT INTO public.team_members (user_id, team_id) "
                    f"VALUES ('{member}', '{team_a}')",
                ),
                "refuse stranger": (
                    confined,
                    "INSERT INTO public.team_members (user_id, team_id) "
                    f"VALUES ('{stranger}', '{team_a}')",
                ),
                "demote one of two admins": (
                    confined,
                    f"UPDATE public.organization_members SET role_id = '{_MEMBER}' "
                    f"WHERE user_id = '{first_admin}' AND organization_id = '{org_a}'",
                ),
                "demote the last admin": (
                    confined,
                    f"UPDATE public.organization_members SET role_id = '{_MEMBER}' "
                    f"WHERE user_id = '{last_admin}' AND organization_id = '{org_a}'",
                ),
            }
            outcomes = {}
            for label, statements in calls.items():
                try:
                    outcomes[label] = await _execute(caller, *statements, bind=ent_a)
                except DBAPIError as exc:
                    outcomes[label] = exc

            # What the admitted writes left, read back as the owner.
            members = await _execute(
                owner_url,
                confined,
                "SELECT user_id FROM public.team_members WHERE team_id = :t",
                t=team_a,
            )
            roles = await _execute(
                owner_url,
                confined,
                "SELECT user_id, role_id FROM public.organization_members "
                "WHERE organization_id = :o ORDER BY user_id",
                o=org_a,
            )
        finally:
            # The database goes with the context; the role is cluster-wide.
            await _execute(owner_url, drop_role_sql(role))

    hijacked = {
        label: str(outcome.orig)
        for label, outcome in outcomes.items()
        if isinstance(outcome, DBAPIError) and "planted" in str(outcome)
    }
    assert not hijacked, f"a definer body ran a planted object: {hijacked}"

    # And each function still did its job.
    assert [tuple(row) for row in outcomes["count"]] == [(1,)]
    assert [tuple(row) for row in outcomes["page"]] == [(case_id, [team_a])]
    assert outcomes["admit member"] == []
    assert isinstance(outcomes["refuse stranger"], DBAPIError)
    assert "same enterprise" in str(outcomes["refuse stranger"])
    assert [tuple(row) for row in members] == [(member,)]
    assert outcomes["demote one of two admins"] == []
    assert isinstance(outcomes["demote the last admin"], DBAPIError)
    assert "would be left with no admin" in str(outcomes["demote the last admin"])
    assert [tuple(row) for row in roles] == [
        (first_admin, _MEMBER),
        (last_admin, _ADMIN),
    ]


async def _definer_catalog(url: str) -> dict:
    """Per definer function: its whole ``pg_proc`` row except the two things
    revision 005 changes (``proconfig``, ``prosrc``), its settings, its comment
    and who may execute it; per guard trigger, its whole ``pg_trigger`` row."""
    rows = await _execute(
        url,
        "SELECT p.proname, to_jsonb(p) - 'proconfig' - 'prosrc', "
        "coalesce(p.proconfig, '{}'), obj_description(p.oid, 'pg_proc'), "
        "EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, "
        "acldefault('f', p.proowner))) a WHERE a.grantee = 0), "
        "has_function_privilege(:runtime, p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = ANY(:names)",
        runtime=_RUNTIME_ROLE,
        names=list(_DEFINER_FUNCTIONS),
    )
    triggers = await _execute(
        url,
        "SELECT tgname, to_jsonb(t) FROM pg_trigger t WHERE tgname = ANY(:names)",
        names=list(_GUARD_TRIGGERS),
    )
    return {
        "functions": {
            name: {
                "row": row,
                "config": list(config),
                "comment": comment,
                "public_may_execute": public_may,
                "runtime_may_execute": runtime_may,
            }
            for name, row, config, comment, public_may, runtime_may in rows
        },
        "triggers": dict(triggers),
    }


async def test_revision_005_re_creates_each_function_in_place():
    """From 004 to 005 and back, with ``faultmaven_app`` present so 003's grant
    to it runs. Up: each function keeps its OID and every catalog attribute but
    its settings and body — owner, grants, comment, signature, result type,
    volatility — and both triggers still call the same functions. Down: each
    function's settings are exactly what 004 left. Up again: 005's state,
    exactly.
    """
    async with _database_of_its_own("fm_definer_updown", runtime_role=True) as url:
        result = _alembic(url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        before = await _definer_catalog(url)

        result = _alembic(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        after = await _definer_catalog(url)

        result = _alembic(url, f"downgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        stepped_down = await _definer_catalog(url)

        result = _alembic(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        again = await _definer_catalog(url)

    assert sorted(before["functions"]) == sorted(_DEFINER_FUNCTIONS)
    assert sorted(before["triggers"]) == sorted(_GUARD_TRIGGERS)
    # What 004 left, so the comparisons below compare something.
    for name in _DEFINER_FUNCTIONS:
        assert before["functions"][name]["config"] == [
            "search_path=pg_catalog, public, pg_temp",
            "row_security=off",
        ], name

    for name in _DEFINER_FUNCTIONS:
        old, new = before["functions"][name], after["functions"][name]
        assert new["row"] == old["row"], f"{name}: more than settings and body moved"
        assert new["comment"] == old["comment"], name
        assert new["config"] == [
            "search_path=pg_catalog, pg_temp",
            "row_security=off",
        ], name
        assert stepped_down["functions"][name]["config"] == old["config"], name
        assert stepped_down["functions"][name]["row"] == old["row"], name
        assert again["functions"][name] == new, name
    assert after["triggers"] == before["triggers"]
    assert stepped_down["triggers"] == before["triggers"]

    for name in ("admin_case_metadata_page", "admin_case_metadata_count"):
        function = after["functions"][name]
        assert function["public_may_execute"] is False, f"{name}: PUBLIC may execute"
        assert function["runtime_may_execute"] is True, f"{name}: lost its grant"

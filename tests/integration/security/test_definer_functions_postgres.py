"""What a ``SECURITY DEFINER`` function's body resolves to, on a real PostgreSQL.

A definer function runs with its OWNER's rights, so every name in its body — a
relation, a type, a function, an operator — must resolve to the object its
author meant, whoever calls it and whatever objects they have created. The
chain has four: the baseline's two trigger guards
(``organization_members_last_admin_guard``,
``team_members_same_enterprise_guard``) and revision 003's two operator reads
(``admin_case_metadata_page``, ``admin_case_metadata_count``). Revision 005
re-creates them with ``search_path = pg_catalog, pg_temp`` and every relation
schema-qualified. What only a real database can show:

* **A planted operator or aggregate is never run.** PostgreSQL resolves a
  function or operator name by argument types before search-path position: an
  exact match in ANY schema on the path wins over a ``pg_catalog`` candidate
  that needs an implicit cast or is polymorphic. The bodies compare
  ``character varying`` columns with ``text`` and with each other, and
  aggregate with ``array_agg`` over ``text``, ``integer`` and ``boolean`` —
  shapes for which ``pg_catalog`` has no exact match. With ``public`` on the
  path, a role allowed to create in ``public`` could define one there and have
  the body run it with the owner's rights.
* **Re-creating them changed their settings and bodies and nothing else.**
  ``CREATE OR REPLACE`` keeps each function's OID, so its owner, its grants,
  its comment and the triggers that call it carry over. Stepping down restores
  the settings revision 004 left. A function that is not where 001 and 003
  created it is refused, not created afresh.
* **``PUBLIC`` loses ``CREATE`` on ``public``** where the migrating role can
  take it away and keep its own, and is warned about where it cannot.

Each test migrates a database of its own. An operator planted in ``public``
reaches every session of its database, so it is never planted in the one the
rest of the lane shares — and dropping the database is what removes it, however
the test ends.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from tests.integration.security.conftest import (
    PROJECT_ROOT,
    RUNTIME_ROLE,
    alembic_on,
    create_limited_role,
    database_of_its_own,
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

#: ``005_definer_search_path_without_public``, which re-creates the four.
_REVISION = "1c5a2ad13a65"
#: ``004_definer_trigger_hardening``, the revision 005 parents onto.
_PARENT_REVISION = "14d4bfdd406e"
_MIGRATION = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20261001_1500_1c5a2ad13a65_005_definer_search_path_without_public.py"
)
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

#: A caller's own statements run under this path, so the only place a plant
#: could be resolved is inside a definer body, under the function's own setting.
_CONFINED = "SET LOCAL search_path = pg_catalog, pg_temp"

#: What a role allowed to create in ``public`` plants there: an exact match for
#: every shape a body uses that ``pg_catalog`` matches only through a cast or a
#: polymorphic signature. Each one counts its calls on a sequence of its own —
#: which a rollback does not undo, so a refused write still leaves the count —
#: records the role it ran as, and returns what ``pg_catalog``'s would, so a
#: body that resolved to it still behaves and only the record gives it away.
_PLANTS = (
    "eq_varchar_text",
    "eq_varchar_varchar",
    "array_agg_text",
    "array_agg_integer",
    "array_agg_boolean",
)


def _planting() -> list[str]:
    statements = [
        "CREATE TABLE public.fm_planted_calls (plant text NOT NULL, ran_as name NOT NULL)",
        "GRANT INSERT ON public.fm_planted_calls TO PUBLIC",
    ]
    for plant in _PLANTS:
        statements += [
            f"CREATE SEQUENCE public.fm_calls_{plant}",
            f"GRANT USAGE ON SEQUENCE public.fm_calls_{plant} TO PUBLIC",
        ]
    statements.append("""
        CREATE FUNCTION public.fm_planted_record(p_plant text)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_catalog.nextval(
                pg_catalog.concat('public.fm_calls_', p_plant)::pg_catalog.regclass
            );
            INSERT INTO public.fm_planted_calls VALUES (p_plant, current_user);
        END
        $$
        """)
    for plant, right in (
        ("eq_varchar_text", "text"),
        ("eq_varchar_varchar", "varchar"),
    ):
        statements += [
            f"""
            CREATE FUNCTION public.fm_planted_{plant}(a character varying, b {right})
            RETURNS boolean LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM public.fm_planted_record('{plant}');
                RETURN a::pg_catalog.text OPERATOR(pg_catalog.=) b::pg_catalog.text;
            END
            $$
            """,
            f"""
            CREATE OPERATOR public.= (
                LEFTARG = character varying,
                RIGHTARG = {right},
                FUNCTION = public.fm_planted_{plant}
            )
            """,
        ]
    for kind in ("text", "integer", "boolean"):
        statements += [
            f"""
            CREATE FUNCTION public.fm_planted_array_agg_{kind}(s {kind}[], v {kind})
            RETURNS {kind}[] LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM public.fm_planted_record('array_agg_{kind}');
                RETURN pg_catalog.array_append(s, v);
            END
            $$
            """,
            f"""
            CREATE AGGREGATE public.array_agg({kind}) (
                SFUNC = public.fm_planted_array_agg_{kind},
                STYPE = {kind}[]
            )
            """,
        ]
    return statements


#: The positive control: under a path that includes ``public``, each plant is
#: what PostgreSQL picks — and it answers as ``pg_catalog``'s would.
_PROBES = (
    (
        "eq_varchar_text",
        "SELECT CAST('x' AS character varying) = CAST('x' AS text)",
        True,
    ),
    (
        "eq_varchar_varchar",
        "SELECT CAST('x' AS character varying) = CAST('x' AS character varying)",
        True,
    ),
    (
        "array_agg_text",
        "SELECT array_agg(v ORDER BY v) FROM (VALUES ('b'::text), ('a')) AS t(v)",
        ["a", "b"],
    ),
    (
        "array_agg_integer",
        "SELECT array_agg(v ORDER BY v) FROM (VALUES (2), (1)) AS t(v)",
        [1, 2],
    ),
    (
        "array_agg_boolean",
        "SELECT array_agg(v ORDER BY v) FROM (VALUES (true), (false)) AS t(v)",
        [False, True],
    ),
)


async def _execute(
    url: str,
    *statements: str,
    confined: bool = False,
    bind: str | None = None,
    **params,
):
    """Run ``statements`` in one transaction on a fresh connection and return the
    last one's rows. ``confined`` sets the path before anything else runs;
    ``bind`` binds the enterprise after it, through ``pg_catalog``. The
    connection is new each time, so no plan cached before a plant can answer
    for a body after it."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                if confined:
                    await conn.execute(text(_CONFINED))
                if bind is not None:
                    await conn.execute(
                        text(
                            "SELECT pg_catalog.set_config("
                            "'app.current_enterprise_id', :fm_bind, true)"
                        ),
                        {"fm_bind": bind},
                    )
                result = None
                for statement in statements:
                    result = await conn.execute(text(statement), params)
                return (
                    result.all() if result is not None and result.returns_rows else []
                )
    finally:
        await engine.dispose()


async def _plant_record(url: str) -> tuple[dict, set]:
    """How many times each plant has run, and as whom."""
    counts = await _execute(
        url,
        " UNION ALL ".join(
            f"SELECT '{plant}', (SELECT CASE WHEN is_called THEN last_value "
            f"ELSE 0 END FROM public.fm_calls_{plant})"
            for plant in _PLANTS
        ),
        confined=True,
    )
    ran_as = await _execute(
        url,
        "SELECT DISTINCT plant, ran_as::text FROM public.fm_planted_calls",
        confined=True,
    )
    return dict(counts), {tuple(row) for row in ran_as}


async def test_a_planted_operator_or_aggregate_never_runs_inside_a_definer_function():
    """Every role may create in ``public`` here, as on a cluster initialised by
    PostgreSQL 14 or earlier (granted after migrating, because revision 005
    takes it away from ``PUBLIC`` where it can). A limited role — the deployed
    runtime grants and nothing it owns — plants ``=`` operators and
    ``array_agg`` aggregates in ``public``, then drives each definer function:
    both case-metadata reads, an insert the membership guard admits and one it
    refuses, a demotion the last-admin guard admits and one it refuses.

    A positive control first proves every plant is what PostgreSQL picks
    wherever ``public`` IS searched — without it, a plant PostgreSQL never chose
    would pass this test for the wrong reason.
    """
    async with database_of_its_own(
        os.environ["DATABASE_URL"], "fm_definer_plant"
    ) as owner_url:
        result = alembic_on(owner_url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]

        ent_a, ent_b = f"ent_a_{uuid.uuid4().hex[:8]}", f"ent_b_{uuid.uuid4().hex[:8]}"
        team_a, org_a = (
            f"team_a_{uuid.uuid4().hex[:8]}",
            f"org_a_{uuid.uuid4().hex[:8]}",
        )
        member, stranger = "member_a", "stranger_b"
        first_admin, last_admin = "admin_a_1", "admin_a_2"
        case_id = f"case_{uuid.uuid4().hex[:8]}"
        # One turn, so the page aggregates turn numbers and asides as well as
        # team ids.
        history = {"turn_history": [{"turn_number": 1, "outcome": "progress"}]}
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
                        "INSERT INTO public.cases "
                        "(case_id, enterprise_id, title, metadata) "
                        "VALUES (:c, :e, 'planted-operator probe', CAST(:m AS jsonb))"
                    ),
                    {"c": case_id, "e": ent_a, "m": json.dumps(history)},
                )
                await session.execute(
                    text(
                        "INSERT INTO public.resource_shares (share_id, resource_type, "
                        "resource_id, scope_type, scope_id, enterprise_id) "
                        "VALUES (:s, 'case', :c, 'team', :t, :e)"
                    ),
                    {"s": f"share_{case_id}", "c": case_id, "t": team_a, "e": ent_a},
                )
                await session.execute(text("GRANT CREATE ON SCHEMA public TO PUBLIC"))
                await session.commit()
        finally:
            await owner.dispose()

        role = f"fm_definer_planter_{uuid.uuid4().hex[:8]}"
        try:
            await create_limited_role(owner_url, role, _PW)
            caller = limited_url(owner_url, role, _PW)
            await _execute(caller, *_planting())

            for plant, probe, expected in _PROBES:
                calls_before, _ = await _plant_record(owner_url)
                rows = await _execute(caller, "SET LOCAL search_path = public", probe)
                calls_after, ran_as = await _plant_record(owner_url)
                assert [tuple(row) for row in rows] == [(expected,)], plant
                assert calls_after[plant] > calls_before[plant], (
                    f"PostgreSQL did not pick the planted {plant} with public on "
                    "the path, so this test could not see a body pick it either"
                )
                assert (plant, role) in ran_as
            control_calls, control_ran_as = await _plant_record(owner_url)

            calls = {
                "count": "SELECT public.admin_case_metadata_count('inquiry', 'copilot')",
                "page": (
                    "SELECT case_id, turn_numbers, turn_is_out_of_band, "
                    "shared_team_ids FROM public.admin_case_metadata_page"
                    "('inquiry', 'copilot', 10, 0)"
                ),
                "admit member": (
                    "INSERT INTO public.team_members (user_id, team_id) "
                    f"VALUES ('{member}', '{team_a}')"
                ),
                "refuse stranger": (
                    "INSERT INTO public.team_members (user_id, team_id) "
                    f"VALUES ('{stranger}', '{team_a}')"
                ),
                "demote one of two admins": (
                    f"UPDATE public.organization_members SET role_id = '{_MEMBER}' "
                    f"WHERE user_id = '{first_admin}' AND organization_id = '{org_a}'"
                ),
                "demote the last admin": (
                    f"UPDATE public.organization_members SET role_id = '{_MEMBER}' "
                    f"WHERE user_id = '{last_admin}' AND organization_id = '{org_a}'"
                ),
            }
            outcomes = {}
            for label, statement in calls.items():
                try:
                    outcomes[label] = await _execute(
                        caller, statement, confined=True, bind=ent_a
                    )
                except DBAPIError as exc:
                    outcomes[label] = exc

            definer_calls, definer_ran_as = await _plant_record(owner_url)
            # What the admitted writes left, read back as the owner.
            members = await _execute(
                owner_url,
                "SELECT user_id FROM public.team_members WHERE team_id = :t",
                confined=True,
                t=team_a,
            )
            roles = await _execute(
                owner_url,
                "SELECT user_id, role_id FROM public.organization_members "
                "WHERE organization_id = :o ORDER BY user_id",
                confined=True,
                o=org_a,
            )
        finally:
            # The database goes with the context; the role is cluster-wide.
            await _execute(owner_url, drop_role_sql(role), confined=True)

    ran = {
        plant: definer_calls[plant] - control_calls[plant]
        for plant in _PLANTS
        if definer_calls[plant] != control_calls[plant]
    }
    assert not ran, (
        f"definer bodies ran planted objects {ran} times, as "
        f"{sorted(definer_ran_as - control_ran_as)}"
    )

    # And each function still did its job.
    assert [tuple(row) for row in outcomes["count"]] == [(1,)]
    assert [tuple(row) for row in outcomes["page"]] == [
        (case_id, [1], [False], [team_a])
    ]
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
        runtime=RUNTIME_ROLE,
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


#: What revision 004 left on all four.
_PARENT_CONFIG = ["search_path=pg_catalog, public, pg_temp", "row_security=off"]


async def test_revision_005_re_creates_each_function_in_place():
    """From 004 to 005 and back, with ``faultmaven_app`` present so 003's grant
    to it runs. Up: each function keeps its OID and every catalog attribute but
    its settings and body — owner, grants, comment, signature, result type,
    volatility — and both triggers still call the same functions. Down: each
    function's settings are exactly what 004 left. Up again: 005's state,
    exactly.
    """
    async with database_of_its_own(
        os.environ["DATABASE_URL"], "fm_definer_updown", runtime_role=True
    ) as url:
        result = alembic_on(url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        before = await _definer_catalog(url)

        result = alembic_on(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        after = await _definer_catalog(url)

        result = alembic_on(url, f"downgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        stepped_down = await _definer_catalog(url)

        result = alembic_on(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        again = await _definer_catalog(url)

    assert sorted(before["functions"]) == sorted(_DEFINER_FUNCTIONS)
    assert sorted(before["triggers"]) == sorted(_GUARD_TRIGGERS)
    # What 004 left, so the comparisons below compare something.
    for name in _DEFINER_FUNCTIONS:
        assert before["functions"][name]["config"] == _PARENT_CONFIG, name

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


async def test_revision_005_refuses_a_function_it_would_otherwise_create():
    """``CREATE OR REPLACE`` on a function that is not there CREATES it — with
    PostgreSQL's default grant, ``EXECUTE`` for ``PUBLIC``, and no comment. On a
    database missing one of the four, the upgrade fails naming it, and leaves
    everything as it was."""
    async with database_of_its_own(
        os.environ["DATABASE_URL"], "fm_definer_missing", runtime_role=True
    ) as url:
        result = alembic_on(url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        await _execute(
            url, "DROP FUNCTION public.admin_case_metadata_count(text, text)"
        )

        result = alembic_on(url, f"upgrade {_REVISION}")
        catalog = await _definer_catalog(url)
        (version,) = await _execute(url, "SELECT version_num FROM alembic_version")

    assert result.returncode != 0, "the upgrade created a function it should refuse"
    assert (
        "re-creates functions that do not exist: "
        "public.admin_case_metadata_count(text, text)"
    ) in result.stderr
    assert sorted(catalog["functions"]) == sorted(
        set(_DEFINER_FUNCTIONS) - {"admin_case_metadata_count"}
    )
    for name, function in catalog["functions"].items():
        assert function["config"] == _PARENT_CONFIG, name
    assert tuple(version) == (_PARENT_REVISION,)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_rev_005", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _public_may_create(url: str) -> bool:
    ((may,),) = await _execute(
        url, "SELECT has_schema_privilege('public', 'public', 'CREATE')"
    )
    return may


async def _with_role(superuser_url: str, role: str, body):
    """Run ``body(role)`` with a login role that exists only meanwhile."""
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url, f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER"
    )
    try:
        return await body(role)
    finally:
        await _execute(superuser_url, drop_role_sql(role))


async def test_revision_005_closes_public_to_public_when_the_migrator_owns_it():
    """A database whose owner is the migrating role, not a superuser, on which
    ``PUBLIC`` holds ``CREATE`` on ``public`` — PostgreSQL 14's default. Revision
    005 takes it away; the migrating role, which holds the schema owner's
    privileges, can still create in ``public`` as the next revision will need;
    and stepping down does not hand it back."""
    superuser_url = os.environ["DATABASE_URL"]

    async def migrate_as(role: str):
        async with database_of_its_own(
            superuser_url, "fm_definer_close", owner=role
        ) as su_url:
            # Before the chain runs: PostgreSQL 14's grant on a database 16 made.
            await _execute(su_url, "GRANT CREATE ON SCHEMA public TO PUBLIC")
            owner_url = limited_url(su_url, role, _PW)
            steps = {}
            for step, command in (
                ("to 004", f"upgrade {_PARENT_REVISION}"),
                ("to 005", f"upgrade {_REVISION}"),
                ("down to 004", f"downgrade {_PARENT_REVISION}"),
            ):
                result = alembic_on(owner_url, command)
                assert result.returncode == 0, (step, result.stderr[-2000:])
                steps[step] = await _public_may_create(su_url)
                if step == "to 005":
                    await _execute(
                        owner_url,
                        "CREATE TABLE public.fm_created_after_005 (x integer)",
                        "DROP TABLE public.fm_created_after_005",
                    )
            return steps

    steps = await _with_role(
        superuser_url, f"fm_definer_owner_{uuid.uuid4().hex[:8]}", migrate_as
    )
    assert steps == {"to 004": True, "to 005": False, "down to 004": False}


async def test_revision_005_warns_when_the_migrator_cannot_close_public():
    """The migrating role neither owns schema ``public`` nor is a superuser: it
    creates there only through ``PUBLIC``'s grant, so it cannot take that grant
    away. The upgrade succeeds, ``PUBLIC`` keeps ``CREATE``, and the statement
    that would have revoked it raises a WARNING naming the command for the
    schema's owner to run."""
    import asyncpg  # the PostgreSQL lane's driver; absent from the standalone one

    superuser_url = os.environ["DATABASE_URL"]
    migration = _load_migration()

    async def migrate_as(role: str):
        async with database_of_its_own(superuser_url, "fm_definer_warn") as su_url:
            await _execute(su_url, "GRANT CREATE ON SCHEMA public TO PUBLIC")
            migrator_url = limited_url(su_url, role, _PW)
            result = alembic_on(migrator_url, "upgrade head")
            may_create = await _public_may_create(su_url)

            # The revision's own statement again, as the migrating role, with
            # the server's messages to the client collected.
            messages = []
            connection = await asyncpg.connect(
                make_url(migrator_url)
                .set(drivername="postgresql")
                .render_as_string(hide_password=False)
            )
            try:
                connection.add_log_listener(
                    lambda _conn, message: messages.append(message)
                )
                await connection.execute(migration._REVOKE_OR_WARN)
                # Listeners are called from the event loop: let it run them.
                await asyncio.sleep(0)
            finally:
                await connection.close()
            return result, may_create, messages

    result, may_create, messages = await _with_role(
        superuser_url, f"fm_definer_migrator_{uuid.uuid4().hex[:8]}", migrate_as
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert may_create is True
    warnings = [m for m in messages if m.severity == "WARNING"]
    assert len(warnings) == 1, [m.message for m in messages]
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC;" in warnings[0].message

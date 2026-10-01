"""Shared scaffolding for the PostgreSQL-under-RLS probes.

Two modules here need the same thing: a role that RLS actually applies to.
PostgreSQL exempts superusers and table owners, so a probe run as the migration
role would pass whether or not a policy existed — it would be testing a system
nobody deploys. Both ``test_personal_tenant_provisioning`` and
``test_tenant_turn_cap`` therefore create a role with exactly the grants
``02-create-rls-app-role.sql`` gives ``faultmaven_app`` and drive the real code
through it.

That setup was written out twice, byte for byte. It lives here now because the
copies are the kind that drift silently: a grant added to one and not the other
changes what the *other* module proves without failing anything.

The modules that migrate a database of their own — to step a revision down
and up, or to plant objects no other test may meet — share ``alembic_on`` and
``database_of_its_own`` for the same reason.

What is deliberately NOT shared: each module keeps its own module-scoped
environment fixture. The role name has to be unique per module (both create and
drop roles, and the ``-m postgres`` lane runs them in one session), and the
teardown that restores ``DATABASE_URL`` is what stops one module's limited role
leaking into the next module's idea of what it is measuring.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.settings import set_env_var

#: The checkout under test, which ``alembic`` runs from.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The deployment's runtime role, which revision 003 grants ``EXECUTE`` by name.
RUNTIME_ROLE = "faultmaven_app"

#: The default enterprise migration 006 seeds. Used as the FK target so probes
#: create organizations without inventing a tier.
DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


def drop_role_sql(role: str) -> str:
    """Idempotent DROP for a probe role, safe to run before CREATE."""
    return f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    DROP OWNED BY {role};
    DROP ROLE {role};
  END IF;
END $$;
"""


def limited_url(superuser_url: str, role: str, password: str) -> str:
    """``superuser_url`` re-pointed at the limited role.

    ``render_as_string(hide_password=False)`` rather than ``str()``: the latter
    masks the password as ``***``, which fails authentication with a message
    naming the role — a confusing way to learn that a URL was stringified.
    """
    return (
        make_url(superuser_url)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


#: The functions revision ``003_admin_case_metadata`` grants the runtime role
#: ``EXECUTE`` on — explicitly, because they are not executable by ``PUBLIC``.
CASE_METADATA_FUNCTIONS = (
    "admin_case_metadata_page(text, text, bigint, bigint)",
    "admin_case_metadata_count(text, text)",
)


def grant_case_metadata_sql(role: str) -> str:
    """The ``EXECUTE`` grant revision 003 gives ``faultmaven_app``, for ``role``.

    Guarded on the functions existing, as the migration's own grant is guarded
    on the role existing, so a database at an earlier revision still works.
    """
    grants = "\n".join(
        f"    IF to_regprocedure('{signature}') IS NOT NULL THEN\n"
        f"        GRANT EXECUTE ON FUNCTION {signature} TO {role};\n"
        "    END IF;"
        for signature in CASE_METADATA_FUNCTIONS
    )
    return f"DO $$ BEGIN\n{grants}\nEND $$;"


async def create_limited_role(
    superuser_url: str,
    role: str,
    password: str,
    *,
    grant_case_metadata: bool = True,
) -> None:
    """A role with the deployed ``faultmaven_app`` grants and no ownership.

    ``grant_case_metadata=False`` leaves out the one grant the deployment makes
    by name — ``EXECUTE`` on the cross-enterprise case metadata functions — to
    stand for a deployment whose runtime role was never granted it.
    """
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
            await conn.exec_driver_sql(drop_role_sql(role))
            await conn.exec_driver_sql(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                "NOSUPERUSER NOBYPASSRLS"
            )
            await conn.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{dbname}" TO {role}'
            )
            await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            await conn.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                f"TO {role}"
            )
            await conn.exec_driver_sql(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"
            )
            if grant_case_metadata:
                await conn.exec_driver_sql(grant_case_metadata_sql(role))
    finally:
        await engine.dispose()


async def drop_limited_role(superuser_url: str, role: str) -> None:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(drop_role_sql(role))
    finally:
        await engine.dispose()


def alembic_on(url: str, command: str) -> subprocess.CompletedProcess:
    """Run ``alembic <command>`` against ``url`` — that database and no other.

    ``DATABASE_URL`` is set as its only spelling: pydantic-settings binds it in
    any letter case, so an exported ``database_url`` left beside it could win
    and point the run at the lane's shared database. ``PYTHONPATH`` leads with
    the checkout, so ``alembic/env.py`` imports the tree under test.
    """
    env = os.environ.copy()
    set_env_var(env, "DATABASE_URL", url)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if part
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *shlex.split(command)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@asynccontextmanager
async def database_of_its_own(
    superuser_url: str,
    prefix: str,
    *,
    owner: str | None = None,
    runtime_role: bool = False,
):
    """A fresh database on the lane's cluster, dropped however the test ends.

    Yields ``superuser_url`` re-pointed at it. ``owner`` makes another role the
    database's owner. ``runtime_role`` makes sure ``faultmaven_app`` exists
    while the database does, so a migration's grant to it runs; roles are
    cluster-wide, so it is created only if absent and dropped only if created
    here. Use one for anything that downgrades the schema or plants objects in
    it: the lane's shared database is every other module's too.
    """
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
                        {"r": RUNTIME_ROLE},
                    )
                ).scalar()
                if created_runtime_role:
                    await conn.execute(text(f"CREATE ROLE {RUNTIME_ROLE} NOLOGIN"))
            owned_by = f' OWNER "{owner}"' if owner else ""
            await conn.execute(text(f'CREATE DATABASE "{name}"{owned_by}'))
        yield (
            make_url(superuser_url)
            .set(database=name)
            .render_as_string(hide_password=False)
        )
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            if created_runtime_role:
                await conn.execute(text(f"DROP ROLE IF EXISTS {RUNTIME_ROLE}"))
        await admin.dispose()

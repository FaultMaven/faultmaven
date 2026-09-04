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

What is deliberately NOT shared: each module keeps its own module-scoped
environment fixture. The role name has to be unique per module (both create and
drop roles, and the ``-m postgres`` lane runs them in one session), and the
teardown that restores ``DATABASE_URL`` is what stops one module's limited role
leaking into the next module's idea of what it is measuring.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

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


async def create_limited_role(superuser_url: str, role: str, password: str) -> None:
    """A role with the deployed ``faultmaven_app`` grants and no ownership."""
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
    finally:
        await engine.dispose()


async def drop_limited_role(superuser_url: str, role: str) -> None:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(drop_role_sql(role))
    finally:
        await engine.dispose()

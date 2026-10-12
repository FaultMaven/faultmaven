"""Revision 015 on a real PostgreSQL: a case's creator cannot be deleted (#1923).

``015_case_creator_restrict`` re-adds ``cases_user_id_fkey`` ``ON DELETE
RESTRICT``. What only a real database can show:

* the stored key is ``RESTRICT`` (``pg_constraint.confdeltype = 'r'``), and the
  downgrade restores ``SET NULL`` (``'n'``);
* a raw ``DELETE FROM users`` of a creator is refused, and of a driver is not —
  the driver's cases go back to their creators (ADR-020 D3, unchanged);
* a bulk ``DELETE FROM users WHERE enterprise_id = …`` is refused whole;
* deleting the ENTERPRISE still removes its cases and users together — the
  cascade reaches both tables in one statement;
* the refusal holds across tenants: a session that row-level security binds to
  another enterprise counts none of the creator's cases, and its delete is
  refused anyway, because referential checks bypass row-level security.

The SQLite half — the trigger that stands in for the key there — is
``TestCaseCreatorRestrictRevision`` in
``tests/integration/test_alembic_migrations.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.security.conftest import (
    alembic_on,
    create_limited_role,
    database_of_its_own,
    drop_role_sql,
    limited_url,
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

#: ``015_case_creator_restrict``.
_REVISION = "9904ee0cad86"
#: The revision 015 parents onto.
_PARENT_REVISION = "9b3e5c71d2a4"  # pragma: allowlist secret
_PW = "fm_case_creator_probe_pw"


async def _execute(url: str, *statements: str) -> list:
    """Run ``statements`` in one transaction; return the last one's rows."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows: list = []
            for statement in statements:
                result = await conn.execute(text(statement))
                rows = list(result.fetchall()) if result.returns_rows else []
            return rows
    finally:
        await engine.dispose()


async def _seed(url: str) -> dict:
    """Two enterprises. In A: a CREATOR whose case a DRIVER drives, and a
    bystander with no case. B is empty. Returns the ids."""
    tag = uuid.uuid4().hex[:8]
    ids = {
        "a": f"ent_a_{tag}",
        "b": f"ent_b_{tag}",
        "creator": f"u_creator_{tag}",
        "driver": f"u_driver_{tag}",
        "bystander": f"u_bystander_{tag}",
        "case": f"case_{tag}",
    }
    statements = [
        f"INSERT INTO enterprises (enterprise_id, name, slug) "
        f"VALUES ('{e}', '{e}', '{e}')"
        for e in (ids["a"], ids["b"])
    ]
    statements += [
        f"INSERT INTO users (user_id, enterprise_id, username, email, display_name) "
        f"VALUES ('{ids[who]}', '{ids['a']}', '{ids[who]}', "
        f"'{ids[who]}@example.com', 'U')"
        for who in ("creator", "driver", "bystander")
    ]
    statements.append(
        f"INSERT INTO cases (case_id, enterprise_id, user_id, driver_id, title) "
        f"VALUES ('{ids['case']}', '{ids['a']}', '{ids['creator']}', "
        f"'{ids['driver']}', 't')"
    )
    await _execute(url, *statements)
    return ids


async def _delete_type(url: str) -> str:
    ((kind,),) = await _execute(
        url,
        "SELECT confdeltype::text FROM pg_constraint "
        "WHERE conname = 'cases_user_id_fkey' AND conrelid = 'cases'::regclass",
    )
    return kind


async def _case(url: str, case_id: str) -> list:
    return await _execute(
        url, f"SELECT user_id, driver_id FROM cases WHERE case_id = '{case_id}'"
    )


async def test_the_key_refuses_a_creator_and_releases_a_driver_and_round_trips():
    superuser_url = os.environ["DATABASE_URL"]
    async with database_of_its_own(superuser_url, "fm_015_restrict") as url:
        result = alembic_on(url, f"upgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _delete_type(url) == "n"

        result = alembic_on(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _delete_type(url) == "r"
        ids = await _seed(url)

        with pytest.raises(IntegrityError, match="cases_user_id_fkey"):
            await _execute(url, f"DELETE FROM users WHERE user_id = '{ids['creator']}'")
        # A bulk delete that would take the creator is refused whole.
        with pytest.raises(IntegrityError, match="cases_user_id_fkey"):
            await _execute(url, f"DELETE FROM users WHERE enterprise_id = '{ids['a']}'")
        assert await _execute(
            url, f"SELECT count(*) FROM users WHERE enterprise_id = '{ids['a']}'"
        ) == [(3,)]

        # The driver goes, and the case goes back to its creator (ADR-020 D3).
        await _execute(url, f"DELETE FROM users WHERE user_id = '{ids['driver']}'")
        assert await _case(url, ids["case"]) == [(ids["creator"], None)]
        # An account that created nothing goes as before.
        await _execute(url, f"DELETE FROM users WHERE user_id = '{ids['bystander']}'")

        # The downgrade restores SET NULL: the creator's delete nulls the column.
        result = alembic_on(url, f"downgrade {_PARENT_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _delete_type(url) == "n"
        await _execute(url, f"DELETE FROM users WHERE user_id = '{ids['creator']}'")
        assert await _case(url, ids["case"]) == [(None, None)]

        result = alembic_on(url, f"upgrade {_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _delete_type(url) == "r"


async def test_deleting_the_enterprise_still_removes_its_cases_and_users():
    superuser_url = os.environ["DATABASE_URL"]
    async with database_of_its_own(superuser_url, "fm_015_cascade") as url:
        result = alembic_on(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await _delete_type(url) == "r"
        ids = await _seed(url)

        await _execute(
            url, f"DELETE FROM enterprises WHERE enterprise_id = '{ids['a']}'"
        )

        assert await _execute(
            url, f"SELECT count(*) FROM users WHERE enterprise_id = '{ids['a']}'"
        ) == [(0,)]
        assert await _case(url, ids["case"]) == []


async def test_the_refusal_holds_for_a_session_bound_to_another_tenant():
    """A runtime-shaped role bound to enterprise B: row-level security hides
    A's case from its count, and its delete of A's creator is refused anyway."""
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_015_limited_{uuid.uuid4().hex[:8]}"
    async with database_of_its_own(superuser_url, "fm_015_rls") as url:
        result = alembic_on(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        ids = await _seed(url)
        await create_limited_role(url, role, _PW)
        try:
            role_url = limited_url(url, role, _PW)
            bind_b = f"SET LOCAL app.current_enterprise_id = '{ids['b']}'"

            # Positive control: the count the route asks is blind here.
            seen = await _execute(
                role_url,
                bind_b,
                f"SELECT count(*) FROM cases WHERE user_id = '{ids['creator']}'",
            )
            assert seen == [(0,)], "row-level security does not filter this role"

            with pytest.raises(IntegrityError, match="cases_user_id_fkey"):
                await _execute(
                    role_url,
                    bind_b,
                    f"DELETE FROM users WHERE user_id = '{ids['creator']}'",
                )
            assert await _case(url, ids["case"]) == [(ids["creator"], ids["driver"])]
        finally:
            await _execute(url, drop_role_sql(role))

"""A case's hard delete through both SQL repositories (#1016).

On SQLite always, and on PostgreSQL when ``DATABASE_URL`` names one (the
``-m postgres`` lane), there through a role row-level security applies to:

- ``delete`` removes the case's ``resource_shares`` rows, keeps another case's,
  and writes one ``case_deleted`` audit row whose ``details`` parse;
- the three statements are ONE transaction: a refused audit insert keeps both
  the row and its shares;
- an absent case answers ``False`` and audits nothing;
- a save of a copy loaded before the delete is refused and brings nothing
  back, while a brand-new case still inserts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import Case, CaseDeletion
from faultmaven.modules.case.exceptions import StaleCaseException

pytestmark = [pytest.mark.integration]

_PG = os.environ.get("DATABASE_URL", "").startswith("postgresql")
_ROLE, _PW = "fm_case_delete_1016", "fm_case_delete_1016_pw"


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(not _PG, reason="set DATABASE_URL to a PG instance"),
            ],
        ),
    ]
)
async def world(request, tmp_path: Path):
    """``admin`` seeds and reads back unscoped; ``repo_in`` builds the dialect
    repository over ``session`` (PostgreSQL: the limited role, bound to the
    enterprise per transaction as the app's ``begin`` listener binds it)."""
    enterprise = f"ent_{uuid4().hex[:8]}"
    creator = f"u_{uuid4().hex[:8]}"
    if request.param == "sqlite":
        path = tmp_path / "delete.db"
        sync = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(sync)
        sync.dispose()
        admin = create_async_engine(f"sqlite+aiosqlite:///{path}")

        @event.listens_for(admin.sync_engine, "connect")
        def _fk_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        scoped = admin
        from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
            SQLiteCaseRepository as Repository,
        )
    else:
        from tests.integration.security.conftest import (
            create_limited_role,
            drop_limited_role,
            limited_url,
        )

        superuser_url = os.environ["DATABASE_URL"]
        await create_limited_role(superuser_url, _ROLE, _PW)
        admin = create_async_engine(superuser_url)
        scoped = create_async_engine(limited_url(superuser_url, _ROLE, _PW))

        @event.listens_for(scoped.sync_engine, "begin")
        def _bind(conn):
            conn.execute(
                text("SELECT set_config('app.current_enterprise_id', :e, true)"),
                {"e": enterprise},
            )

        from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
            PostgreSQLHybridCaseRepository as Repository,
        )

    async with admin.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e, :e, :e)"
            ),
            {"e": enterprise},
        )
        await conn.execute(
            text(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name) VALUES (:u, :e, :u, :m, :u)"
            ),
            {"u": creator, "e": enterprise, "m": f"{creator}@example.com"},
        )
    Session = async_sessionmaker(scoped, expire_on_commit=False)

    class World:
        pass

    w = World()
    w.enterprise, w.creator, w.admin = enterprise, creator, admin

    async def call(method, *args, **kwargs):
        async with Session() as session:
            return await getattr(Repository(session), method)(*args, **kwargs)

    async def rows(sql, **params):
        async with admin.connect() as conn:
            return (await conn.execute(text(sql), params)).fetchall()

    async def new_case(**extra):
        return await call(
            "save",
            Case(user_id=creator, enterprise_id=enterprise, title="Disk full", **extra),
        )

    async def share(case_id, team="team_a"):
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO resource_shares (share_id, resource_type, "
                    "resource_id, scope_type, scope_id, enterprise_id) VALUES "
                    "(:s, 'case', :c, 'team', :t, :e)"
                ),
                {"s": uuid4().hex[:32], "c": case_id, "t": team, "e": enterprise},
            )

    def deletion(case_id, actor=creator):
        return CaseDeletion(case_id, enterprise, actor, cleared={"uploads": 2})

    w.call, w.rows, w.new_case, w.share, w.deletion = (
        call,
        rows,
        new_case,
        share,
        deletion,
    )
    try:
        yield w
    finally:
        async with admin.begin() as conn:
            for table in ("user_audit_log", "resource_shares", "cases", "users"):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE enterprise_id = :e"),
                    {"e": enterprise},
                )
            await conn.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise},
            )
        await admin.dispose()
        if scoped is not admin:
            await scoped.dispose()
            await drop_limited_role(superuser_url, _ROLE)


async def _audit_rows(world, case_id):
    return await world.rows(
        "SELECT event_type, event_category, user_id, resource_type, details "
        "FROM user_audit_log WHERE resource_id = :c",
        c=case_id,
    )


async def test_delete_takes_the_shares_and_writes_one_audit_row(world):
    doomed, other = await world.new_case(), await world.new_case()
    await world.share(doomed.case_id)
    await world.share(doomed.case_id, "team_b")
    await world.share(other.case_id)

    assert await world.call(
        "delete", doomed.case_id, deletion=world.deletion(doomed.case_id)
    )

    assert await world.call("get", doomed.case_id) is None
    shares = await world.rows("SELECT resource_id FROM resource_shares")
    assert [r[0] for r in shares if r[0] in (doomed.case_id, other.case_id)] == [
        other.case_id
    ]
    ((event_type, category, actor, resource_type, details),) = await _audit_rows(
        world, doomed.case_id
    )
    assert (event_type, category, actor, resource_type) == (
        "case_deleted",
        "data_access",
        world.creator,
        "case",
    )
    parsed = json.loads(details)
    # The two share rows its own transaction removed are counted (#1016).
    assert parsed["cleared"] == {"uploads": 2, "resource_shares": 2}
    assert set(parsed["not_cleared"]) == {
        "idempotency_replay_cache",
        "llm_response_cache",
    }


async def test_a_refused_audit_row_keeps_the_case_and_its_shares(world):
    case = await world.new_case()
    await world.share(case.case_id)

    # An actor no account holds: the audit row's foreign key refuses it, after
    # both deletes ran inside the same transaction.
    with pytest.raises(Exception, match="Failed to delete case") as refused:
        await world.call(
            "delete", case.case_id, deletion=world.deletion(case.case_id, "u_ghost")
        )
    assert "foreign key" in str(refused.value.__cause__).lower()

    assert await world.call("get", case.case_id) is not None
    assert await world.rows(
        "SELECT 1 FROM resource_shares WHERE resource_id = :c", c=case.case_id
    )
    assert await _audit_rows(world, case.case_id) == []


async def test_an_absent_case_answers_false_and_audits_nothing(world):
    assert not await world.call(
        "delete", "case_neverstored", deletion=world.deletion("case_neverstored")
    )
    assert await _audit_rows(world, "case_neverstored") == []


async def test_a_save_after_the_delete_does_not_bring_the_case_back(world):
    stored = await world.new_case()
    loaded = await world.call("get", stored.case_id)  # a turn's copy
    await world.call("delete", stored.case_id, deletion=world.deletion(stored.case_id))

    loaded.title = "written by a turn that loaded before the delete"
    with pytest.raises(StaleCaseException):
        await world.call("save", loaded)

    assert (
        await world.rows("SELECT 1 FROM cases WHERE case_id = :c", c=stored.case_id)
        == []
    )
    fresh = await world.new_case()
    assert await world.call("get", fresh.case_id) is not None


async def test_an_unshared_case_audits_no_share_rows(world):
    case = await world.new_case()
    assert await world.call(
        "delete", case.case_id, deletion=world.deletion(case.case_id)
    )
    ((*_, details),) = await _audit_rows(world, case.case_id)
    assert json.loads(details)["cleared"]["resource_shares"] == 0

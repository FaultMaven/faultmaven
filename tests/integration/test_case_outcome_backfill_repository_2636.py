"""The case outcome backfill's write, through both SQL repositories (#2636).

``fm-backfill-case-outcome`` writes the record with
``ICaseRepository.save_progress``: the ``progress`` blob alone, compare-and-swap
on ``version``. A full-row save would stamp ``updated_at``, and both
repositories order a case list by it, so a backfill through ``save`` re-sorted
every user's list. Each check runs on SQLite (always) and on PostgreSQL (when
``DATABASE_URL`` names one, as CI's postgres job does), through the production
``SessionlessCaseRepository`` (its ``get_db_session`` replaced by one with the
production shape over the test's database, as #1882's test does), which picks
the dialect repository and its real SQL:

- a backfilled case keeps its ``updated_at``, and the list keeps its order;
- the write bumps ``version`` and changes no other column;
- a copy holding an older version is refused with a version conflict.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.cli.backfill_case_outcome import backfill_case_outcome
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import Case
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)

pytestmark = [pytest.mark.integration]

_PG = os.environ.get("DATABASE_URL", "").startswith("postgresql")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not _PG, reason="PostgreSQL arm: set DATABASE_URL to a PG instance"
                ),
            ],
        ),
    ]
)
async def world(request, tmp_path, monkeypatch):
    """The production case repository on a real database, with an enterprise
    and an account of its own."""
    if request.param == "sqlite":
        path = tmp_path / "outcome.db"
        sync = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(sync)
        sync.dispose()
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    else:
        engine = create_async_engine(os.environ["DATABASE_URL"])
    enterprise, user = f"ent_{uuid4().hex[:8]}", f"u_{uuid4().hex[:8]}"
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        await session.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:e, :e, :e)"
            ),
            {"e": enterprise},
        )
        await session.execute(
            text(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name) VALUES (:u, :e, :u, :m, :u)"
            ),
            {"u": user, "e": enterprise, "m": f"{user}@example.com"},
        )
        await session.commit()

    @asynccontextmanager
    async def _get_db_session():
        session = Session()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(sessionless_module, "get_db_session", _get_db_session)
    try:
        yield _World(Session, SessionlessCaseRepository(), enterprise, user)
    finally:
        async with Session() as session:
            for table in ("cases", "users", "enterprises"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE enterprise_id = :e"),
                    {"e": enterprise},
                )
            await session.commit()
        await engine.dispose()


class _World:
    """The production wrapper, with the backfill's sweep scoped to this
    test's own cases (a shared PostgreSQL holds other tests' rows): ``get``
    and ``save_progress`` are the wrapper's own."""

    def __init__(self, Session, repository, enterprise, user):
        self.Session, self.repository = Session, repository
        self.enterprise, self.user = enterprise, user

    async def list_all_case_ids(self):
        listed, _total = await self.repository.list(user_id=self.user)
        return [c.case_id for c in listed]

    async def get(self, case_id):
        return await self.repository.get(case_id)

    async def save_progress(self, case):
        return await self.repository.save_progress(case)

    async def stamp(self, case_id, updated_at):
        async with self.Session() as session:
            await session.execute(
                text(
                    "UPDATE cases SET created_at = :c0, updated_at = :t "
                    "WHERE case_id = :c"
                ),
                {"c0": _CREATED, "t": updated_at, "c": case_id},
            )
            await session.commit()

    async def columns(self, case_id):
        async with self.Session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT updated_at, last_activity_at, title, state, "
                        "version FROM cases WHERE case_id = :c"
                    ),
                    {"c": case_id},
                )
            ).one()
        return tuple(row)


_CREATED = datetime(2025, 12, 1, tzinfo=timezone.utc)


async def _stored(world, count=3):
    """``count`` stored cases, each last updated a day apart, oldest first."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = []
    for i in range(count):
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=world.user,
            enterprise_id=world.enterprise,
            title=f"Case {i}",
        )
        await world.repository.save(case)
        await world.stamp(case.case_id, base + timedelta(days=i))
        ids.append(case.case_id)
    return ids


async def test_a_backfilled_case_keeps_its_updated_at_and_the_list_its_order(world):
    ids = await _stored(world)
    before = {i: await world.columns(i) for i in ids}
    order = await world.list_all_case_ids()
    assert order == list(reversed(ids)), "premise: listed by updated_at"

    with patch(
        "faultmaven.providers.tenancy.factory.requested_tenant_provider",
        return_value="single",
    ):
        assert (
            await backfill_case_outcome(
                enterprise_id=None, dry_run=False, repository=world
            )
            == 0
        )

    assert await world.list_all_case_ids() == order
    for case_id, (updated, active, title, state, version) in before.items():
        assert await world.columns(case_id) == (
            updated,
            active,
            title,
            state,
            version + 1,
        )
        assert (await world.get(case_id)).progress.outcome is not None


async def test_a_copy_holding_an_older_version_is_refused(world):
    (case_id,) = await _stored(world, count=1)
    first = await world.get(case_id)
    second = await world.get(case_id)
    await world.save_progress(first)
    with pytest.raises(StaleCaseException):
        await world.save_progress(second)
    assert (await world.get(case_id)).version == second.version + 1

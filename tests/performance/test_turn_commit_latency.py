"""The turn's one commit fits its reserve on SQLite (#1882).

``TURN_COMMIT_RESERVE_SECONDS`` is the end of the turn budget kept back for the
commit, sized from the measured p99 of that commit times a safety factor. This
measures the commit on a file-backed SQLite through the production wrapper
(``SessionlessCaseRepository``: one session per call, commit on exit) and prints
the numbers. The p99 is judged against ``TURN_COMMIT_P99`` in ``budgets.py``: a
pull request asserts its regression anchor, the ``FM_BENCHMARK_ABSOLUTE`` nightly
the product target, which is the reserve itself. The PostgreSQL measurement is in
``tests/integration/test_turn_rows_commit_with_case_postgres_1882.py``.

‼ Every comparison goes through ``assert_latency_within`` (#1557).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from tests.turn_commit_latency import (
    investigating_case,
    measure_turn_commits,
    percentile,
    summarize,
)
from tests.utils import seed_enterprises
from tests.wallclock import assert_latency_within

from .budgets import TURN_COMMIT_P99

ENTERPRISE = "ent_1882_latency"


@pytest.fixture
async def sessionless(tmp_path, monkeypatch):
    """The production wrapper over a file-backed SQLite with foreign keys on."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fm.db'}")

    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        await seed_enterprises(session, [ENTERPRISE])
        await session.commit()

    @asynccontextmanager
    async def _get_db_session():
        session = sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(sessionless_module, "get_db_session", _get_db_session)
    yield SessionlessCaseRepository()
    await eng.dispose()


@pytest.mark.performance
async def test_the_turn_commit_fits_its_reserve(sessionless):
    timings = []
    for _ in range(3):
        case = investigating_case(ENTERPRISE)
        await sessionless.save(case)
        timings += await measure_turn_commits(sessionless, case)

    print(f"\nSQLite turn commit: {summarize(timings)}")
    assert_latency_within(
        percentile(timings, 99),
        TURN_COMMIT_P99,
        "p99 of a turn's one commit (SQLite)",
        detail=summarize(timings),
    )

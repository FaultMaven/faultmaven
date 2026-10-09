"""The turn's one commit fits its reserve on SQLite (#1882).

``TURN_COMMIT_RESERVE_SECONDS`` is the end of the turn budget kept back for the
commit, sized from the measured p99 of that commit times a safety factor. This
measures the commit on a file-backed SQLite through the production wrapper
(``SessionlessCaseRepository``: one session per call, commit on exit), prints
the numbers, and judges them against ``TURN_COMMIT`` in ``budgets.py`` — one
statistic per mode, because the two modes ask two questions (#1902):

* a pull request asks "did the commit get more expensive?", and judges the
  **p50** against the row's regression anchor. In Test Standalone this runs
  beside the whole suite under xdist, where a neighbour's fsync puts seconds
  into a p99 that the CPU calibration cannot see (#1902's three red runs);
  the median does not move for that and does move for a costlier commit.
* the ``FM_BENCHMARK_ABSOLUTE`` nightly asks "does the commit fit its
  reserve?", and judges the **p99** against the reserve itself. That job runs
  ``tests/performance/`` alone, without xdist, so the tail is the commit's.

The PostgreSQL measurement is in
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
from tests.wallclock import absolute_mode, assert_latency_within

from .budgets import TURN_COMMIT

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
    # The mode picks the statistic; ``asserted_target`` picks the number it is
    # held to (the reserve under absolute mode, the regression anchor
    # otherwise). Both halves are in the module docstring and the row's.
    pct = 99 if absolute_mode() else 50
    assert_latency_within(
        percentile(timings, pct),
        TURN_COMMIT,
        f"p{pct} of a turn's one commit (SQLite)",
        detail=summarize(timings),
    )

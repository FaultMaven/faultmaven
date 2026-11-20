"""Every ``Case`` field survives a save and a load through the SQLite case
repository (#2945). The guard and its exemption map: ``case_roundtrip_guard``."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.integration.modules.case.case_roundtrip_guard import (
    assert_every_field_round_trips,
    roundtrip_fixtures,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'cases.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_every_case_field_round_trips_on_sqlite(session_factory):
    async def save(case):
        async with session_factory() as session:
            return await SQLiteCaseRepository(session).save(case)

    async def get_fresh(case_id):
        async with session_factory() as session:
            return await SQLiteCaseRepository(session).get(case_id)

    fixtures = roundtrip_fixtures(
        f"ent_{uuid4().hex[:8]}",
        f"org_{uuid4().hex[:8]}",
        f"user_{uuid4().hex[:8]}",
        f"user_{uuid4().hex[:8]}",
    )
    await assert_every_field_round_trips(save, get_fresh, fixtures)

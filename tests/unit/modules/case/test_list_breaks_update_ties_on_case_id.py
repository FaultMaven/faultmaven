"""The case list breaks ``updated_at`` ties on ``case_id`` (SQLite).

A bulk statement stamps one transaction time on many rows, and ``ORDER BY
updated_at DESC`` alone leaves their order to the engine — so a page boundary
among them can move between two reads, serving a case twice or never. The
PostgreSQL repository and the cross-enterprise operator list order the same
way; that half is proven on PostgreSQL in
``tests/integration/security/test_admin_case_metadata_postgres.py``.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)

pytestmark = pytest.mark.unit

_OWNER = "user_tie_order"
_ENTERPRISE = "ent_tie_order"
_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


async def test_a_page_boundary_among_tied_rows_is_stable(
    tmp_path, case_schema_template
):
    shutil.copyfile(case_schema_template(_ENTERPRISE, _OWNER), tmp_path / "ties.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ties.db'}")
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            repo = SQLiteCaseRepository(session)
            # Saved in DESCENDING id order, so insertion order is not the
            # order the list promises.
            ids = [f"case_{n:012d}" for n in range(6)]
            for case_id in reversed(ids):
                await repo.save(
                    Case(
                        case_id=case_id,
                        user_id=_OWNER,
                        enterprise_id=_ENTERPRISE,
                        title=case_id,
                        created_at=_AT,
                        last_activity_at=_AT,
                        updated_at=_AT,
                    )
                )
            # One instant for all of them, as a bulk statement would stamp.
            same = (
                await session.execute(text("SELECT updated_at FROM cases LIMIT 1"))
            ).scalar()
            await session.execute(text("UPDATE cases SET updated_at = :t"), {"t": same})
            await session.commit()

            pages = []
            for offset, limit in ((0, 4), (4, 2)):
                cases, total = await repo.list(user_id=None, limit=limit, offset=offset)
                pages.append([case.case_id for case in cases])
    finally:
        await engine.dispose()

    assert total == 6
    assert pages == [ids[:4], ids[4:]]

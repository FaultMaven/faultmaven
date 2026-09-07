"""Turn-sequence resilience at the persistence boundary (SQLite repo).

Pins the two halves that live in the repository:
* **prevention** — a save never persists ``current_turn`` ahead of
  ``turn_history`` (the drift that wedged cases), without touching the in-memory
  object;
* **load-heal** — a case persisted with a gap (e.g. wedged before this fix)
  reconciles on load instead of bricking.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)


@pytest.fixture(scope="function")
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session


@pytest.fixture
def repo(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


def _tp(n: int) -> TurnProgress:
    return TurnProgress(
        turn_number=n, outcome=TurnOutcome.CONVERSATION, progress_made=False
    )


def _case(turns: list[int], current_turn: int) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="org",
        title="t",
        description="d",
        state=CaseState.INQUIRY,
    )
    case.turn_history = [_tp(n) for n in turns]
    case.current_turn = current_turn
    return case


@pytest.mark.asyncio
async def test_save_never_persists_current_turn_ahead_of_history(repo):
    """Prevention: an intermediate-save shape (current_turn ahead of the last
    recorded turn) must persist the DERIVED counter, not the advanced one."""

    case = _case([1, 2], current_turn=3)  # turn 3 in flight, not yet recorded
    await repo.save(case)

    # In-memory object is untouched (in-flight business logic unaffected)...
    assert case.current_turn == 3
    # ...but the persisted/reloaded counter is derived from turn_history.
    reloaded = await repo.get(case.case_id)
    assert reloaded.current_turn == 2
    assert [t.turn_number for t in reloaded.turn_history] == [1, 2]


@pytest.mark.asyncio
async def test_happy_path_roundtrip_is_unchanged(repo):
    """Regression: a consistent case round-trips with identical values and no
    synthetic turns (proves the fix is inert on healthy data)."""

    case = _case([1, 2, 3], current_turn=3)
    await repo.save(case)
    reloaded = await repo.get(case.case_id)

    assert reloaded.current_turn == 3
    assert [t.turn_number for t in reloaded.turn_history] == [1, 2, 3]
    assert TurnOutcome.SKIPPED.value not in [
        t.outcome.value for t in reloaded.turn_history
    ]


@pytest.mark.asyncio
async def test_wedged_case_heals_across_a_turn_instead_of_bricking(repo):
    """A case whose next turn would create a gap [1,3] saves (heals) rather than
    raising the old fatal 'Turn numbers must be sequential' error."""

    case = _case([1], current_turn=1)
    await repo.save(case)

    # Simulate the next turn appending number 3 onto history [1] (the drift).
    case.turn_history.append(_tp(3))
    case.current_turn = 3
    await repo.save(case)  # must not raise

    reloaded = await repo.get(case.case_id)
    nums = [t.turn_number for t in reloaded.turn_history]
    assert nums == [1, 2, 3]
    assert reloaded.turn_history[1].outcome == TurnOutcome.SKIPPED
    assert reloaded.current_turn == 3


@pytest.mark.asyncio
async def test_inmemory_repo_also_heals_a_gap():
    """The fix must cover the in-memory repo too (used when DATABASE_URL is
    unset/':memory:'), not just the SQL backends."""

    repo = InMemoryCaseRepository()
    case = _case([1], current_turn=1)
    await repo.save(case)
    case.turn_history.append(_tp(3))
    case.current_turn = 3
    await repo.save(case)  # must not wedge

    reloaded = await repo.get(case.case_id)
    assert [t.turn_number for t in reloaded.turn_history] == [1, 2, 3]
    assert reloaded.turn_history[1].outcome == TurnOutcome.SKIPPED
    assert reloaded.current_turn == 3

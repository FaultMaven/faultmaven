"""A turn's reports and checkpoints commit in the case's own save, or not at all (#1882).

``ICaseRepository.save(case, reports=..., checkpoints=...)`` writes the rows in
the case's transaction, after the case and before the commit. These tests read
the database back through a SECOND session after each save, so what they see
is what committed, not what the writing session still holds.

The PostgreSQL twin, with the RLS tenant check, is
``tests/integration/test_turn_rows_commit_with_case_postgres_1882.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.checkpoint_service import CheckpointService
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    RepositoryException as InMemoryRepositoryException,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    RepositoryException,
    SQLiteCaseRepository,
)
from tests.utils import seed_enterprises

pytestmark = [pytest.mark.unit]

ENTERPRISE = "ent_1882"


@pytest.fixture
async def engine(tmp_path):
    """A file-backed SQLite with the production connection PRAGMAs that
    matter here (foreign keys on), so a second session sees only what
    committed."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fm.db'}")

    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await seed_enterprises(session, [ENTERPRISE])
        await session.commit()
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def repo(sessions):
    async with sessions() as session:
        yield SQLiteCaseRepository(session)


async def _committed(sessions, case_id: str) -> dict:
    """What a fresh session reads for ``case_id``: the case's version and
    title, its message count, and its report and checkpoint rows."""
    async with sessions() as s:
        case_row = (
            await s.execute(
                text("SELECT version, title FROM cases WHERE case_id = :c"),
                {"c": case_id},
            )
        ).fetchone()
        messages = (
            await s.execute(
                text("SELECT COUNT(*) FROM case_messages WHERE case_id = :c"),
                {"c": case_id},
            )
        ).scalar()
        reports = (
            await s.execute(
                text("SELECT report_id, enterprise_id FROM reports WHERE case_id = :c"),
                {"c": case_id},
            )
        ).fetchall()
        checkpoints = (
            await s.execute(
                text(
                    "SELECT checkpoint_id, enterprise_id FROM case_checkpoints "
                    "WHERE case_id = :c"
                ),
                {"c": case_id},
            )
        ).fetchall()
    return {
        "version": case_row[0] if case_row else None,
        "title": case_row[1] if case_row else None,
        "messages": messages,
        "reports": {r[0]: r[1] for r in reports},
        "checkpoints": {c[0]: c[1] for c in checkpoints},
    }


def _case() -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        enterprise_id=ENTERPRISE,
        title="Turn commit case",
        state=CaseState.INQUIRY,
    )


def _report(case: Case, report_type: ReportType = ReportType.CLOSURE_SUMMARY):
    return CaseReport(
        case_id=case.case_id,
        report_type=report_type,
        title="Closure Summary: Turn commit case",
        content="# Closure summary",
        generation_status=ReportStatus.COMPLETED,
        generated_at=datetime.now(timezone.utc).isoformat(),
        generation_time_ms=1,
    )


def _checkpoint(case: Case, to_state: str = "closed"):
    return CheckpointService.capture(
        case,
        trigger="pre_case_action",
        metadata={"from_state": case.state.value, "to_state": to_state},
    )


def _add_message(case: Case, text_: str) -> None:
    case.messages.append(
        {
            "turn_number": case.current_turn,
            "role": "user",
            "content": text_,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        }
    )


@pytest.mark.asyncio
async def test_case_report_and_checkpoint_commit_together(repo, sessions):
    case = _case()
    report, checkpoint = _report(case), _checkpoint(case)

    await repo.save(case, reports=[report], checkpoints=[checkpoint])

    got = await _committed(sessions, case.case_id)
    assert got["version"] == 1
    # Each row carries the case's enterprise, read from the case row the same
    # transaction just wrote.
    assert got["reports"] == {report.report_id: ENTERPRISE}
    assert got["checkpoints"] == {checkpoint.checkpoint_id: ENTERPRISE}


@pytest.mark.asyncio
async def test_a_stale_case_commits_no_row(repo, sessions):
    case = _case()
    await repo.save(case)
    async with sessions() as other:
        winner = await SQLiteCaseRepository(other).get(case.case_id)
        winner.title = "Saved by another request"
        await SQLiteCaseRepository(other).save(winner)

    # ``case`` still holds version 1; the row is at 2.
    _add_message(case, "this turn's message")
    with pytest.raises(StaleCaseException):
        await repo.save(case, reports=[_report(case)], checkpoints=[_checkpoint(case)])

    got = await _committed(sessions, case.case_id)
    assert got["version"] == 2
    assert got["title"] == "Saved by another request"
    assert got["messages"] == 0
    assert got["reports"] == {}
    assert got["checkpoints"] == {}


@pytest.mark.asyncio
async def test_a_checkpoint_id_collision_raises_and_commits_nothing(repo, sessions):
    case = _case()
    first = _checkpoint(case)
    await repo.save(case, checkpoints=[first])

    # The same site, same turn, same target: the same id. Loud, never skipped.
    case.title = "Retitled by the colliding turn"
    _add_message(case, "the colliding turn's message")
    again = _checkpoint(case)
    assert again.checkpoint_id == first.checkpoint_id
    with pytest.raises(RepositoryException):
        await repo.save(case, reports=[_report(case)], checkpoints=[again])

    got = await _committed(sessions, case.case_id)
    assert got["version"] == 1
    assert got["title"] == "Turn commit case"
    assert got["messages"] == 0
    assert got["reports"] == {}
    assert list(got["checkpoints"]) == [first.checkpoint_id]


@pytest.mark.asyncio
async def test_a_duplicate_checkpoint_in_one_save_commits_nothing(repo, sessions):
    case = _case()
    checkpoint = _checkpoint(case)
    with pytest.raises(RepositoryException):
        await repo.save(case, checkpoints=[checkpoint, checkpoint])

    got = await _committed(sessions, case.case_id)
    assert got["version"] is None
    assert got["checkpoints"] == {}


@pytest.mark.asyncio
async def test_a_report_the_table_refuses_commits_nothing(repo, sessions):
    """The report rows are inside the transaction too: one the table refuses
    (``reports_type_check`` admits no runbook) takes the case down with it."""
    case = _case()
    await repo.save(case)

    case.title = "Retitled by the failing turn"
    with pytest.raises(RepositoryException):
        await repo.save(
            case,
            reports=[_report(case, ReportType.RUNBOOK)],
            checkpoints=[_checkpoint(case)],
        )

    got = await _committed(sessions, case.case_id)
    assert got["version"] == 1
    assert got["title"] == "Turn commit case"
    assert got["reports"] == {}
    assert got["checkpoints"] == {}


@pytest.mark.asyncio
async def test_a_row_for_another_case_is_refused_before_any_write(repo, sessions):
    case, stranger = _case(), _case()
    with pytest.raises(ValueError, match="other cases"):
        await repo.save(case, reports=[_report(stranger)])
    assert (await _committed(sessions, case.case_id))["version"] is None


@pytest.mark.asyncio
async def test_a_plain_save_writes_no_row(repo, sessions):
    """Every existing caller passes neither keyword: no behaviour change."""
    case = _case()
    await repo.save(case)
    got = await _committed(sessions, case.case_id)
    assert got["version"] == 1
    assert got["reports"] == {} and got["checkpoints"] == {}


class TestInMemoryParity:
    """The in-memory repository keeps the same contract, so a unit test on it
    sees what a test on SQLite would."""

    @pytest.mark.asyncio
    async def test_rows_are_stored_with_the_case(self):
        repo = InMemoryCaseRepository()
        case = _case()
        report, checkpoint = _report(case), _checkpoint(case)
        await repo.save(case, reports=[report], checkpoints=[checkpoint])
        assert await repo.get_report(report.report_id) is report
        assert await repo.get_checkpoint(checkpoint.checkpoint_id) is checkpoint

    @pytest.mark.asyncio
    async def test_stale_stores_nothing(self):
        repo = InMemoryCaseRepository()
        case = _case()
        await repo.save(case)
        stale = case.model_copy()
        await repo.save(case)  # the stored case moves to version 2
        report, checkpoint = _report(stale), _checkpoint(stale)
        with pytest.raises(StaleCaseException):
            await repo.save(stale, reports=[report], checkpoints=[checkpoint])
        assert await repo.get_report(report.report_id) is None
        assert await repo.get_checkpoint(checkpoint.checkpoint_id) is None

    @pytest.mark.asyncio
    async def test_collision_stores_nothing(self):
        repo = InMemoryCaseRepository()
        case = _case()
        first = _checkpoint(case)
        await repo.save(case, checkpoints=[first])
        version = case.version
        report = _report(case)
        with pytest.raises(InMemoryRepositoryException):
            await repo.save(case, reports=[report], checkpoints=[_checkpoint(case)])
        assert case.version == version
        assert await repo.get_report(report.report_id) is None
        with pytest.raises(InMemoryRepositoryException):
            await repo.create_checkpoint(_checkpoint(case))

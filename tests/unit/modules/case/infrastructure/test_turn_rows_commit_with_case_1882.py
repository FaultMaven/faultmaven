"""A turn's reports and checkpoints commit in the case's own save, or not at all (#1882).

``ICaseRepository.save(case, reports=..., checkpoints=...)`` writes the rows in
the case's transaction, after the case and before the commit. These tests read
the database back through a SECOND session after each save, so what they see
is what committed, not what the writing session still holds.

The PostgreSQL twin, with the RLS tenant check, is
``tests/integration/test_turn_rows_commit_with_case_postgres_1882.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.checkpoint_service import CheckpointService
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    RepositoryException as InMemoryRepositoryException,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
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


@pytest.fixture
def sessionless(sessions, monkeypatch):
    """The production wrapper, over this test's database.

    ``get_db_session`` is replaced by one with the production context
    manager's shape (commit on exit, rollback on an exception, close), so what
    is under test is the wrapper's own forwarding, one session per call.
    """

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
    return SessionlessCaseRepository()


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


class TestThroughTheSessionlessWrapper:
    """``SessionlessCaseRepository`` is what the container wires
    (``container/providers/infrastructure.py``): it must forward the rows."""

    @pytest.mark.asyncio
    async def test_rows_commit_with_the_case(self, sessionless, sessions):
        case = _case()
        report, checkpoint = _report(case), _checkpoint(case)

        await sessionless.save(case, reports=[report], checkpoints=[checkpoint])

        got = await _committed(sessions, case.case_id)
        assert got["version"] == 1
        assert got["reports"] == {report.report_id: ENTERPRISE}
        assert got["checkpoints"] == {checkpoint.checkpoint_id: ENTERPRISE}

    @pytest.mark.asyncio
    async def test_a_refused_row_commits_nothing(self, sessionless, sessions):
        case = _case()
        await sessionless.save(case)

        case.title = "Retitled by the failing turn"
        with pytest.raises(RepositoryException):
            await sessionless.save(
                case,
                reports=[_report(case, ReportType.RUNBOOK)],
                checkpoints=[_checkpoint(case)],
            )

        got = await _committed(sessions, case.case_id)
        assert got["version"] == 1
        assert got["title"] == "Turn commit case"
        assert got["reports"] == {} and got["checkpoints"] == {}


class TestAFailedSaveLeavesTheObjectAsItWas:
    """A save that does not commit puts back what it stamped on the object
    (``version``, ``updated_at``, ``disposition_eligibility``), so the same
    object saves on the next attempt instead of being refused as stale over a
    version the database never took."""

    @pytest.mark.asyncio
    async def test_a_refused_row_then_a_resave_succeeds(self, repo, sessions):
        case = _case()
        await repo.save(case)
        version, updated_at = case.version, case.updated_at

        case.title = "Retitled by the failing turn"
        with pytest.raises(RepositoryException):
            await repo.save(case, reports=[_report(case, ReportType.RUNBOOK)])

        assert (case.version, case.updated_at) == (version, updated_at)
        await repo.save(case)
        got = await _committed(sessions, case.case_id)
        assert got["version"] == 2
        assert got["title"] == "Retitled by the failing turn"

    @pytest.mark.asyncio
    async def test_a_stale_save_leaves_version_and_clock_alone(self, repo, sessions):
        case = _case()
        await repo.save(case)
        async with sessions() as other:
            winner = await SQLiteCaseRepository(other).get(case.case_id)
            await SQLiteCaseRepository(other).save(winner)
        version, updated_at = case.version, case.updated_at

        with pytest.raises(StaleCaseException):
            await repo.save(case)

        assert (case.version, case.updated_at) == (version, updated_at)

    @pytest.mark.asyncio
    async def test_in_memory_matches(self):
        repo = InMemoryCaseRepository()
        case = _case()
        first = _checkpoint(case)
        await repo.save(case, checkpoints=[first])
        version, updated_at = case.version, case.updated_at

        with pytest.raises(InMemoryRepositoryException):
            await repo.save(case, checkpoints=[_checkpoint(case)])

        assert (case.version, case.updated_at) == (version, updated_at)
        await repo.save(case)
        assert case.version == version + 1


# ---------------------------------------------------------------------------
# Orphan checkpoints from a turn that never committed (#1882, PR-2)
# ---------------------------------------------------------------------------


def _at_turn(case: Case, turn: int) -> None:
    """Move ``case`` to ``turn`` the way a turn does: the clock and its record."""
    case.current_turn = turn
    case.turn_history.append(
        TurnProgress(
            turn_number=turn, progress_made=False, outcome=TurnOutcome.CONVERSATION
        )
    )


async def _checkpoint_rows(sessions, case_id: str) -> list[tuple]:
    async with sessions() as s:
        return (
            await s.execute(
                text(
                    "SELECT checkpoint_id, turn_number, snapshot_hash "
                    "FROM case_checkpoints WHERE case_id = :c ORDER BY turn_number"
                ),
                {"c": case_id},
            )
        ).fetchall()


async def _committed_at_turn_two_with_an_orphan_at_three(repo):
    """The pre-#1882 wedge, built as it happened: the case committed at turn 2,
    then turn 3's checkpoint committed in its own transaction (as
    ``create_checkpoint`` did, mid-turn) and turn 3 never committed."""
    case = _case()
    _at_turn(case, 1)
    _at_turn(case, 2)
    await repo.save(case)

    stale_attempt = case.model_copy(deep=True)
    _at_turn(stale_attempt, 3)
    _add_message(stale_attempt, "the attempt that never committed")
    orphan = _checkpoint(stale_attempt, to_state="investigating")
    await repo.create_checkpoint(orphan)
    return case, orphan


class TestOrphanCheckpointsFromATurnThatNeverCommitted:
    """Every save deletes, inside its own transaction and before the case row,
    the case's checkpoint rows above the COMMITTED turn. The retry of a turn
    whose checkpoint committed on its own (before #1882) then commits instead of
    colliding on the checkpoint's deterministic id, forever."""

    @pytest.mark.asyncio
    async def test_a_retried_turn_commits_over_an_orphan_checkpoint(
        self, repo, sessions
    ):
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(repo)
        _at_turn(case, 3)
        _add_message(case, "the retried turn")
        retried = _checkpoint(case, to_state="investigating")
        assert retried.checkpoint_id == orphan.checkpoint_id

        await repo.save(case, checkpoints=[retried])

        got = await _committed(sessions, case.case_id)
        assert got["version"] == 2
        assert got["messages"] == 1
        assert got["checkpoints"] == {retried.checkpoint_id: ENTERPRISE}
        rows = await _checkpoint_rows(sessions, case.case_id)
        # The row is the retry's snapshot, not the orphan's.
        assert [(r[1], r[2]) for r in rows] == [(3, retried.snapshot_hash)]
        assert retried.snapshot_hash != orphan.snapshot_hash

    @pytest.mark.asyncio
    async def test_through_the_sessionless_wrapper(self, sessionless, sessions):
        """The production wrapper: each call its own session, as in a deployment."""
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(sessionless)
        _at_turn(case, 3)
        retried = _checkpoint(case, to_state="investigating")

        await sessionless.save(case, checkpoints=[retried])

        rows = await _checkpoint_rows(sessions, case.case_id)
        assert [(r[0], r[2]) for r in rows] == [
            (orphan.checkpoint_id, retried.snapshot_hash)
        ]

    @pytest.mark.asyncio
    async def test_a_retried_turn_without_a_checkpoint_removes_the_orphan(
        self, repo, sessions
    ):
        """Otherwise the orphan would sit at the now-committed turn 3, looking
        like that turn's own snapshot."""
        case, _ = await _committed_at_turn_two_with_an_orphan_at_three(repo)
        _at_turn(case, 3)
        await repo.save(case)
        assert await _checkpoint_rows(sessions, case.case_id) == []

    @pytest.mark.asyncio
    async def test_the_cleanup_keeps_a_committed_turns_checkpoint(self, repo, sessions):
        """Control: a checkpoint at or below the committed turn is that turn's
        own, and later saves (one outside a turn, then the next turn) keep it."""
        case = _case()
        _at_turn(case, 1)
        kept = _checkpoint(case, to_state="investigating")
        await repo.save(case, checkpoints=[kept])
        case.title = "Renamed outside a turn"
        await repo.save(case)
        _at_turn(case, 2)
        await repo.save(case)

        got = await _committed(sessions, case.case_id)
        assert got["checkpoints"] == {kept.checkpoint_id: ENTERPRISE}

    @pytest.mark.asyncio
    async def test_a_refused_retry_leaves_the_orphan_and_commits_nothing(
        self, repo, sessions
    ):
        """The cleanup is part of the save's transaction: a save that fails
        rolls the delete back with everything else."""
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(repo)
        _at_turn(case, 3)
        with pytest.raises(RepositoryException):
            await repo.save(case, reports=[_report(case, ReportType.RUNBOOK)])

        got = await _committed(sessions, case.case_id)
        assert got["version"] == 1
        assert got["checkpoints"] == {orphan.checkpoint_id: ENTERPRISE}

    @pytest.mark.asyncio
    async def test_in_memory_matches(self):
        repo = InMemoryCaseRepository()
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(repo)
        _at_turn(case, 3)
        retried = _checkpoint(case, to_state="investigating")

        await repo.save(case, checkpoints=[retried])

        assert await repo.get_checkpoint(orphan.checkpoint_id) is retried

    @pytest.mark.asyncio
    async def test_in_memory_a_refused_retry_keeps_the_orphan(self):
        repo = InMemoryCaseRepository()
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(repo)
        stale = case.model_copy(deep=True)
        stale.version += 1  # a version the store never took
        _at_turn(stale, 3)
        with pytest.raises(StaleCaseException):
            await repo.save(stale, checkpoints=[_checkpoint(stale, "investigating")])
        assert await repo.get_checkpoint(orphan.checkpoint_id) is orphan

"""A turn's reports and checkpoints commit in the case's own save, on PostgreSQL under RLS (#1882).

The SQLite twin is
``tests/unit/modules/case/infrastructure/test_turn_rows_commit_with_case_1882.py``.
This one adds what SQLite cannot show: the rows are written as a
non-superuser, non-owner role (a superuser or the table owner bypasses RLS),
through a session whose ``begin`` listener binds the tenant exactly as
``infrastructure/persistence/database.py`` does, once per transaction. The
report and checkpoint INSERTs run inside the case's transaction, so they are
written under the tenant that BEGIN bound, and carry the case's enterprise.

What committed is read back as the superuser, which sees every row whatever
the policies say, so a row hidden by RLS cannot pass for a row never written.

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv-cloud/bin/alembic upgrade head
    .venv-cloud/bin/pytest tests/integration/test_turn_rows_commit_with_case_postgres_1882.py -v
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.config.tenant_context import (
    _current_enterprise_id,
    get_current_enterprise_id,
)
from faultmaven.core.investigation.checkpoint_service import CheckpointService
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
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
    PostgreSQLHybridCaseRepository,
    RepositoryException,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from tests.turn_commit_latency import (
    TURNS,
    investigating_case,
    measure_turn_commits,
    summarize,
)
from tests.utils import seed_enterprises

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

# Unique per worker process so parallel runs don't collide on the role name.
_LIMITED_ROLE = f"fm_turn_commit_{uuid4().hex[:8]}"
_LIMITED_PW = "fm_turn_commit_pw"
_DROP_ROLE_SQL = f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_LIMITED_ROLE}') THEN
    DROP OWNED BY {_LIMITED_ROLE};
    DROP ROLE {_LIMITED_ROLE};
  END IF;
END $$;
"""


@pytest.fixture
async def superuser_engine():
    """The migration role: owns the tables, so it reads past RLS."""
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert engine.dialect.name == "postgresql"
    yield engine
    await engine.dispose()


@pytest.fixture
async def enterprises(superuser_engine):
    """Two tenant enterprises; their cases (and, by cascade, every report and
    checkpoint row) are removed afterwards."""
    ent_a, ent_b = f"ent_a_{uuid4().hex[:8]}", f"ent_b_{uuid4().hex[:8]}"
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        await seed_enterprises(session, [ent_a, ent_b])
        await session.commit()
    yield ent_a, ent_b
    async with superuser_engine.begin() as conn:
        for enterprise_id in (ent_a, ent_b):
            await conn.execute(
                text("DELETE FROM cases WHERE enterprise_id = :e"), {"e": enterprise_id}
            )
            await conn.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise_id},
            )


@pytest.fixture
async def tenant_sessions(superuser_engine):
    """Sessions as a non-superuser, non-owner role, tenant-bound per
    transaction by the same ``begin`` listener production installs."""
    async with superuser_engine.begin() as conn:
        dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
        await conn.exec_driver_sql(_DROP_ROLE_SQL)
        await conn.exec_driver_sql(
            f"CREATE ROLE {_LIMITED_ROLE} LOGIN PASSWORD '{_LIMITED_PW}' NOSUPERUSER"
        )
        await conn.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{dbname}" TO {_LIMITED_ROLE}'
        )
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {_LIMITED_ROLE}")
        await conn.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {_LIMITED_ROLE}"
        )
        # As production's app role has them (the enterprise-infra init script):
        # a terminal transition appends a ``case_actions`` row, keyed by a
        # sequence.
        await conn.exec_driver_sql(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_LIMITED_ROLE}"
        )

    limited_url = make_url(os.environ["DATABASE_URL"]).set(
        username=_LIMITED_ROLE, password=_LIMITED_PW
    )
    engine = create_async_engine(limited_url, future=True)

    @event.listens_for(engine.sync_engine, "begin")
    def _scope_tenant_per_transaction(conn):
        conn.execute(
            text(
                "SELECT set_config('app.current_enterprise_id', :enterprise_id, true)"
            ),
            {"enterprise_id": get_current_enterprise_id()},
        )

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()
    async with superuser_engine.begin() as conn:
        await conn.exec_driver_sql(_DROP_ROLE_SQL)


@pytest.fixture
def sessionless(tenant_sessions, monkeypatch):
    """The production wrapper over the tenant-bound limited role.

    ``get_db_session`` is replaced by one with the production context
    manager's shape (commit on exit, rollback on an exception, close); the
    dialect is detected from the session, so the wrapper picks the PostgreSQL
    repository exactly as it does in production.
    """

    @asynccontextmanager
    async def _get_db_session():
        session = tenant_sessions()
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


@contextmanager
def tenant(enterprise_id: str):
    token = _current_enterprise_id.set(enterprise_id)
    try:
        yield
    finally:
        _current_enterprise_id.reset(token)


async def _committed(superuser_engine, case_id: str) -> dict:
    """Every row for ``case_id``, read past RLS."""
    async with superuser_engine.connect() as conn:
        case_row = (
            await conn.execute(
                text("SELECT version, title FROM cases WHERE case_id = :c"),
                {"c": case_id},
            )
        ).fetchone()
        messages = (
            await conn.execute(
                text("SELECT COUNT(*) FROM case_messages WHERE case_id = :c"),
                {"c": case_id},
            )
        ).scalar()
        reports = (
            await conn.execute(
                text("SELECT report_id, enterprise_id FROM reports WHERE case_id = :c"),
                {"c": case_id},
            )
        ).fetchall()
        checkpoints = (
            await conn.execute(
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


def _case(enterprise_id: str) -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        enterprise_id=enterprise_id,
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
async def test_case_report_and_checkpoint_commit_together_under_the_tenant(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    report, checkpoint = _report(case), _checkpoint(case)

    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, reports=[report], checkpoints=[checkpoint]
            )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 1
    assert got["reports"] == {report.report_id: ent_a}
    assert got["checkpoints"] == {checkpoint.checkpoint_id: ent_a}


@pytest.mark.asyncio
async def test_the_rows_are_invisible_to_another_enterprise(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, ent_b = enterprises
    case = _case(ent_a)
    report, checkpoint = _report(case), _checkpoint(case)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, reports=[report], checkpoints=[checkpoint]
            )

    async def _seen(enterprise_id: str) -> tuple[int, int]:
        with tenant(enterprise_id):
            async with tenant_sessions() as session:
                repo = PostgreSQLHybridCaseRepository(session)
                return (
                    len(await repo.get_reports(case.case_id, include_history=True)),
                    len(await repo.get_checkpoints(case.case_id)),
                )

    assert await _seen(ent_b) == (0, 0)
    # Positive control: the owner's tenant sees both, so the zero above is the
    # policy and not a row that never landed.
    assert await _seen(ent_a) == (1, 1)


@pytest.mark.asyncio
async def test_a_stale_case_commits_no_row(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)
        async with tenant_sessions() as other:
            winner = await PostgreSQLHybridCaseRepository(other).get(case.case_id)
            winner.title = "Saved by another request"
            await PostgreSQLHybridCaseRepository(other).save(winner)

        _add_message(case, "this turn's message")
        async with tenant_sessions() as session:
            with pytest.raises(StaleCaseException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case, reports=[_report(case)], checkpoints=[_checkpoint(case)]
                )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 2
    assert got["title"] == "Saved by another request"
    assert got["messages"] == 0
    assert got["reports"] == {}
    assert got["checkpoints"] == {}


@pytest.mark.asyncio
async def test_a_checkpoint_id_collision_raises_and_commits_nothing(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    first = _checkpoint(case)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, checkpoints=[first]
            )

        case.title = "Retitled by the colliding turn"
        _add_message(case, "the colliding turn's message")
        again = _checkpoint(case)
        assert again.checkpoint_id == first.checkpoint_id
        async with tenant_sessions() as session:
            with pytest.raises(RepositoryException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case, reports=[_report(case)], checkpoints=[again]
                )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 1
    assert got["title"] == "Turn commit case"
    assert got["messages"] == 0
    assert got["reports"] == {}
    assert list(got["checkpoints"]) == [first.checkpoint_id]


@pytest.mark.asyncio
async def test_a_report_the_table_refuses_commits_nothing(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)

        case.title = "Retitled by the failing turn"
        async with tenant_sessions() as session:
            with pytest.raises(RepositoryException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case,
                    reports=[_report(case, ReportType.RUNBOOK)],
                    checkpoints=[_checkpoint(case)],
                )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 1
    assert got["title"] == "Turn commit case"
    assert got["reports"] == {}
    assert got["checkpoints"] == {}


@pytest.mark.asyncio
async def test_a_captured_checkpoint_fits_its_column(
    superuser_engine, enterprises, tenant_sessions
):
    """``case_checkpoints.checkpoint_id`` is VARCHAR(36). The readable id this
    replaced was 40+ characters, and PostgreSQL refused every one of them
    (StringDataRightTruncation), which the old ``create_checkpoint`` logged
    and dropped. SQLite does not enforce the width, so only this test can see
    it."""
    ent_a, _ = enterprises
    case = _case(ent_a)
    captured = CheckpointService.capture(
        case,
        trigger="pre_case_action",
        metadata={"from_state": "inquiry", "to_state": "investigating"},
    )
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, checkpoints=[captured]
            )
    got = await _committed(superuser_engine, case.case_id)
    assert got["checkpoints"] == {captured.checkpoint_id: ent_a}


@pytest.mark.asyncio
async def test_through_the_sessionless_wrapper_rows_commit_with_the_case(
    superuser_engine, enterprises, sessionless
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    report, checkpoint = _report(case), _checkpoint(case)
    with tenant(ent_a):
        await sessionless.save(case, reports=[report], checkpoints=[checkpoint])

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 1
    assert got["reports"] == {report.report_id: ent_a}
    assert got["checkpoints"] == {checkpoint.checkpoint_id: ent_a}


@pytest.mark.asyncio
async def test_through_the_sessionless_wrapper_a_refused_row_commits_nothing(
    superuser_engine, enterprises, sessionless
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        await sessionless.save(case)
        case.title = "Retitled by the failing turn"
        with pytest.raises(RepositoryException):
            await sessionless.save(
                case,
                reports=[_report(case, ReportType.RUNBOOK)],
                checkpoints=[_checkpoint(case)],
            )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 1
    assert got["title"] == "Turn commit case"
    assert got["reports"] == {} and got["checkpoints"] == {}


@pytest.mark.asyncio
async def test_a_failed_save_leaves_the_object_as_it_was(
    superuser_engine, enterprises, tenant_sessions
):
    """The rolled-back save puts back ``version`` and ``updated_at``, so the
    same object re-saves instead of raising a stale 409 over a version the
    database never took."""
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)
        version, updated_at = case.version, case.updated_at

        case.title = "Retitled by the failing turn"
        async with tenant_sessions() as session:
            with pytest.raises(RepositoryException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case, reports=[_report(case, ReportType.RUNBOOK)]
                )
        assert (case.version, case.updated_at) == (version, updated_at)

        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 2
    assert got["title"] == "Retitled by the failing turn"


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


async def _checkpoint_rows(superuser_engine, case_id: str) -> list[tuple]:
    async with superuser_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT checkpoint_id, turn_number, snapshot_hash "
                    "FROM case_checkpoints WHERE case_id = :c ORDER BY turn_number"
                ),
                {"c": case_id},
            )
        ).fetchall()


async def _committed_at_turn_two_with_an_orphan_at_three(tenant_sessions, ent):
    """The pre-#1882 wedge, built as it happened: the case committed at turn 2,
    then turn 3's checkpoint committed in its own transaction (as
    ``create_checkpoint`` did, mid-turn) and turn 3 never committed."""
    case = _case(ent)
    _at_turn(case, 1)
    _at_turn(case, 2)
    async with tenant_sessions() as session:
        await PostgreSQLHybridCaseRepository(session).save(case)

    stale_attempt = case.model_copy(deep=True)
    _at_turn(stale_attempt, 3)
    _add_message(stale_attempt, "the attempt that never committed")
    orphan = _checkpoint(stale_attempt, to_state="investigating")
    async with tenant_sessions() as session:
        await PostgreSQLHybridCaseRepository(session).create_checkpoint(orphan)
    return case, orphan


@pytest.mark.asyncio
async def test_a_retried_turn_commits_over_an_orphan_checkpoint(
    superuser_engine, enterprises, tenant_sessions
):
    """The retry of turn 3 takes the same checkpoint (same id) and commits: the
    orphan is deleted in the retry's own transaction, under the tenant, before
    the insert. Without the cleanup this raised on the primary key, on every
    attempt."""
    ent_a, _ = enterprises
    with tenant(ent_a):
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(
            tenant_sessions, ent_a
        )
        _at_turn(case, 3)
        _add_message(case, "the retried turn")
        retried = _checkpoint(case, to_state="investigating")
        assert retried.checkpoint_id == orphan.checkpoint_id
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, checkpoints=[retried]
            )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 2
    assert got["messages"] == 1
    assert got["checkpoints"] == {retried.checkpoint_id: ent_a}
    rows = await _checkpoint_rows(superuser_engine, case.case_id)
    # The row is the retry's snapshot, not the orphan's.
    assert [(r[1], r[2]) for r in rows] == [(3, retried.snapshot_hash)]
    assert retried.snapshot_hash != orphan.snapshot_hash


@pytest.mark.asyncio
async def test_a_retried_turn_without_a_checkpoint_removes_the_orphan(
    superuser_engine, enterprises, tenant_sessions
):
    """Otherwise the orphan would sit at the now-committed turn 3, looking like
    that turn's own snapshot."""
    ent_a, _ = enterprises
    with tenant(ent_a):
        case, _ = await _committed_at_turn_two_with_an_orphan_at_three(
            tenant_sessions, ent_a
        )
        _at_turn(case, 3)
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)

    assert await _checkpoint_rows(superuser_engine, case.case_id) == []


@pytest.mark.asyncio
async def test_the_cleanup_keeps_a_committed_turns_checkpoint(
    superuser_engine, enterprises, tenant_sessions
):
    """Control: a checkpoint at or below the committed turn is that turn's
    own, and a later save (a non-turn save at the same turn, then the next
    turn) keeps it."""
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        _at_turn(case, 1)
        kept = _checkpoint(case, to_state="investigating")
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case, checkpoints=[kept])
        case.title = "Renamed outside a turn"
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)
        _at_turn(case, 2)
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)

    got = await _committed(superuser_engine, case.case_id)
    assert got["checkpoints"] == {kept.checkpoint_id: ent_a}


@pytest.mark.asyncio
async def test_a_refused_retry_leaves_the_orphan_and_commits_nothing(
    superuser_engine, enterprises, tenant_sessions
):
    """The cleanup is part of the save's transaction: a save that fails rolls
    the delete back with everything else."""
    ent_a, _ = enterprises
    with tenant(ent_a):
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(
            tenant_sessions, ent_a
        )
        _at_turn(case, 3)
        async with tenant_sessions() as session:
            with pytest.raises(RepositoryException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case, reports=[_report(case, ReportType.RUNBOOK)]
                )

    got = await _committed(superuser_engine, case.case_id)
    assert got["version"] == 1
    assert got["checkpoints"] == {orphan.checkpoint_id: ent_a}


@pytest.mark.asyncio
async def test_the_cleanup_cannot_reach_another_enterprises_rows(
    superuser_engine, enterprises, tenant_sessions
):
    """RLS: a save bound to enterprise B, naming a case id that is A's, deletes
    none of A's rows. The DELETE's own predicate cannot be bypassed by a wrong
    case id, because the policy hides the rows from it."""
    ent_a, ent_b = enterprises
    with tenant(ent_a):
        case, orphan = await _committed_at_turn_two_with_an_orphan_at_three(
            tenant_sessions, ent_a
        )
    from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.saving import (
        _delete_uncommitted_checkpoints,
    )

    with tenant(ent_b):
        async with tenant_sessions() as session:
            await _delete_uncommitted_checkpoints(session, case.case_id)
            await session.commit()

    got = await _committed(superuser_engine, case.case_id)
    assert got["checkpoints"] == {orphan.checkpoint_id: ent_a}


# ---------------------------------------------------------------------------
# The PostgreSQL measurement the commit reserve is sized from (#1882, R8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_postgresql_turn_commit_is_measured(enterprises, sessionless):
    """The turn's one commit through the production wrapper, under the limited
    role with RLS bound per transaction, timed over three grown cases, and
    printed: these are the numbers ``TURN_COMMIT_RESERVE_SECONDS`` is sized
    from (with the SQLite twin's, ``tests/performance/test_turn_commit_latency.py``).

    It judges no clock — an integration test may not (#1579); the SQLite twin is
    the judged one. What it does assert is that every commit landed: one case
    version per turn, each case closed with its report and checkpoint."""
    ent_a, _ = enterprises
    timings = []
    with tenant(ent_a):
        for _ in range(3):
            case = investigating_case(ent_a)
            await sessionless.save(case)
            timings += await measure_turn_commits(sessionless, case)
            assert case.version == 1 + TURNS
            assert case.state == CaseState.CLOSED

    print(f"\nPostgreSQL turn commit: {summarize(timings)}")
    assert len(timings) == 3 * TURNS

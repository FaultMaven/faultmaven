"""The PostgreSQL case save names a COMMIT whose outcome is unknown (#2813).

A COMMIT that raises may have landed (the driver's command timeout, a
connection lost mid-COMMIT, a FATAL termination), unless its SQLSTATE refuses
the transaction (a serialization failure, a deferred constraint), which rolled
back. Only the first is ``CommitOutcomeUnknown``, which the turn route answers 500
``TURN_OUTCOME_UNKNOWN``; a failure before the COMMIT never is.

The real ``PostgreSQLHybridCaseRepository.save`` over an in-memory SQLite
session, with only ``db.commit`` replaced: every statement before it runs.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.exceptions import CommitOutcomeUnknown, walk_cause_chain
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
    PostgreSQLHybridCaseRepository,
    RepositoryException,
)
from tests.case_builders import make_case

pytestmark = pytest.mark.unit


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


class _DriverError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"driver error {code}")
        self.sqlstate = code


def _driver_failure(code: str) -> DBAPIError:
    """The shape SQLAlchemy's asyncpg dialect raises: the driver's error is
    ``orig.__cause__`` (``db_errors``)."""
    orig = Exception("dbapi")
    orig.__cause__ = _DriverError(code)
    return DBAPIError("COMMIT", {}, orig)


async def _save_with_commit_raising(session, failure: BaseException) -> BaseException:
    repo = PostgreSQLHybridCaseRepository(session)

    async def _commit():
        raise failure

    session.commit = _commit
    with pytest.raises(RepositoryException) as raised:
        await repo.save(make_case(user_id="user_2813", enterprise_id="ent_2813"))
    return raised.value


def _names_unknown(failure: BaseException) -> bool:
    return any(isinstance(c, CommitOutcomeUnknown) for c in walk_cause_chain(failure))


@pytest.mark.parametrize(
    "failure, why",
    [
        (TimeoutError(), "the driver's command timeout"),
        (ConnectionResetError(), "the socket dropped mid-COMMIT"),
        (
            _driver_failure("08003"),
            "asyncpg's ConnectionDoesNotExistError is raised on the client side",
        ),
        (
            _driver_failure("57P01"),
            "admin_shutdown can be sent after the commit record is durable",
        ),
        (_driver_failure("XX000"), "an internal error says nothing of the commit"),
    ],
)
async def test_a_commit_not_known_to_roll_back_is_named(session, failure, why):
    raised = await _save_with_commit_raising(session, failure)

    assert _names_unknown(raised), why
    assert raised.__cause__.__cause__ is failure, "the original cause is kept"


@pytest.mark.parametrize("code", ["40001", "40P01", "23505", "25P02", "57014"])
async def test_a_commit_the_server_refused_is_not(session, code):
    """A SQLSTATE that refuses the transaction means it rolled back."""
    raised = await _save_with_commit_raising(session, _driver_failure(code))

    assert not _names_unknown(raised)


async def test_a_failure_before_the_commit_is_not(session):
    repo = PostgreSQLHybridCaseRepository(session)
    commits = []

    async def _commit():
        commits.append(1)

    async def _fails(*_args, **_kwargs):
        raise TimeoutError()

    session.commit = _commit
    session.execute = _fails
    with pytest.raises(RepositoryException) as raised:
        await repo.save(make_case(user_id="user_2813", enterprise_id="ent_2813"))

    assert isinstance(raised.value.__cause__, TimeoutError), "a statement failed"
    assert not _names_unknown(raised.value)
    assert commits == []

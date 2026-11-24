"""An unreachable database is named at the session, by position (#2670).

``get_db_session`` raises ``DatabaseUnavailable`` for a connect failure (any
``ConnectionError``, an unresolvable name, a Unix socket with no server, an
unreachable host, an ``OSError`` with no errno, a SQLSTATE saying the server
takes no connection) only while nothing reached the server:
before the session's transaction began and before anything on it committed.
The same driver error later is a dropped connection whose work may have landed,
and passes through as itself, as does anything with ``CommitOutcomeUnknown`` on
its chain and a configuration fault (``28P01``, ``3D000``, a
``PermissionError``).

The regression runs through a closed loopback port; the table raises each
shape synthetically (no name is resolved). Past ``after_begin`` the stand-in is
SQLite: a real statement begins the transaction, then the failure is raised.
"""

from __future__ import annotations

import errno
import importlib.util
import socket

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from faultmaven.exceptions import (
    CommitOutcomeUnknown,
    DatabaseTimeout,
    DatabaseUnavailable,
    is_database_unavailable,
)
from faultmaven.infrastructure.persistence import database

pytestmark = pytest.mark.unit

requires_asyncpg = pytest.mark.skipif(
    importlib.util.find_spec("asyncpg") is None,
    reason="asyncpg is not installed (the cloud and test extras carry it)",
)


@pytest.fixture
async def fresh_engine(monkeypatch):
    """No engine built yet; whatever a test builds is disposed afterwards."""
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_engine_bounds", None)
    monkeypatch.setattr(database, "_session_factory", None)
    yield
    if database._engine is not None:
        await database._engine.dispose()


def closed_loopback_port() -> int:
    """A loopback port this test bound and closed: nothing listens on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _DriverError(Exception):
    """A driver exception carrying the SQLSTATE PostgreSQL sent, as asyncpg
    raises it unwrapped at pool checkout."""

    def __init__(self, code):
        super().__init__("driver")
        self.sqlstate = code


def _dbapi_error(driver_error) -> DBAPIError:
    """SQLAlchemy's shape: the driver's exception is ``orig.__cause__``."""
    orig = Exception("dbapi")
    orig.__cause__ = driver_error
    return DBAPIError("SELECT 1", {}, orig)


def _chained(*links: BaseException) -> BaseException:
    """``links[0]`` raised from ``links[1]`` raised from ..."""
    for outer, inner in zip(links, links[1:]):
        outer.__cause__ = inner
    return links[0]


async def _out_of_a_session(exc: BaseException, *, before=None) -> BaseException:
    """What ``get_db_session`` raises for ``exc`` raised in its body, after
    ``before(session)`` ran there."""
    database.get_engine("sqlite+aiosqlite:///:memory:")
    with pytest.raises(BaseException) as raised:
        async with database.get_db_session() as session:
            if before is not None:
                await before(session)
            raise exc
    return raised.value


@requires_asyncpg
async def test_a_refused_connection_is_named_database_unavailable(fresh_engine):
    """The regression: on ``main`` the read raised a bare
    ``ConnectionRefusedError``, which every route answered 500."""
    database.get_engine(
        f"postgresql+asyncpg://fm@127.0.0.1:{closed_loopback_port()}/fm"
    )

    with pytest.raises(DatabaseUnavailable) as raised:
        async with database.get_db_session() as session:
            await session.execute(text("SELECT 1"))

    assert isinstance(raised.value.__cause__, ConnectionRefusedError)


_CONNECT_FAILURES = {
    "refused": lambda: ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
    "reset": lambda: ConnectionResetError(errno.ECONNRESET, "reset"),
    "aborted": lambda: ConnectionAbortedError(errno.ECONNABORTED, "aborted"),
    "broken pipe": lambda: BrokenPipeError(errno.EPIPE, "broken pipe"),
    "a Unix socket with no server": lambda: FileNotFoundError(
        errno.ENOENT, "No such file or directory"
    ),
    "every address failed": lambda: OSError(
        "Multiple exceptions: [Errno 111] Connect call failed ('::1', 5432), "
        "[Errno 111] Connect call failed ('127.0.0.1', 5432)"
    ),
    "unresolvable": lambda: socket.gaierror(socket.EAI_NONAME, "unknown"),
    "host unreachable": lambda: OSError(errno.EHOSTUNREACH, "no route"),
    "network unreachable": lambda: OSError(errno.ENETUNREACH, "no network"),
    "08001 unwrapped": lambda: _DriverError("08001"),
    "57P03 unwrapped": lambda: _DriverError("57P03"),
    **{
        f"{code} on a DBAPIError": (lambda code=code: _dbapi_error(_DriverError(code)))
        for code in ("08001", "08006", "57P01", "57P02", "57P03", "53300")
    },
    "wrapped by a repository": lambda: _chained(
        RuntimeError("Failed to get case"), ConnectionRefusedError()
    ),
}


class TestBeforeTheTransactionBegan:
    @pytest.mark.parametrize("make", _CONNECT_FAILURES.values(), ids=_CONNECT_FAILURES)
    async def test_a_connect_failure_is_database_unavailable(self, fresh_engine, make):
        original = make()
        raised = await _out_of_a_session(original)

        assert isinstance(raised, DatabaseUnavailable)
        assert raised.__cause__ is original

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda: _dbapi_error(_DriverError("28P01")), id="28P01"),
            pytest.param(lambda: _dbapi_error(_DriverError("3D000")), id="3D000"),
            pytest.param(lambda: _DriverError("28P01"), id="28P01 unwrapped"),
            pytest.param(lambda: OSError(errno.EACCES, "denied"), id="another errno"),
            pytest.param(
                lambda: PermissionError(errno.EACCES, "denied"), id="PermissionError"
            ),
            pytest.param(
                lambda: PermissionError("no errno"), id="PermissionError, no errno"
            ),
            pytest.param(
                lambda: _chained(
                    CommitOutcomeUnknown("commit"), ConnectionResetError()
                ),
                id="CommitOutcomeUnknown on the chain",
            ),
            pytest.param(
                lambda: DatabaseUnavailable("already named"), id="already named"
            ),
        ],
    )
    async def test_anything_else_passes_through_as_itself(self, fresh_engine, make):
        original = make()

        assert await _out_of_a_session(original) is original

    async def test_a_timeout_keeps_its_precedence(self, fresh_engine):
        original = _chained(TimeoutError(), ConnectionResetError())

        assert isinstance(await _out_of_a_session(original), DatabaseTimeout)


async def _began(session) -> None:
    await session.execute(text("SELECT 1"))


async def _committed(session) -> None:
    await session.execute(text("SELECT 1"))
    await session.commit()


class TestOnceTheSessionReachedTheServer:
    @pytest.mark.parametrize(
        "before", [_began, _committed], ids=["after begin", "after a commit"]
    )
    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(ConnectionResetError, id="reset"),
            pytest.param(ConnectionRefusedError, id="refused on re-checkout"),
            pytest.param(
                lambda: _dbapi_error(_DriverError("08003")),
                id="08003 connection does not exist",
            ),
        ],
    )
    async def test_a_connect_shape_stays_raw(self, fresh_engine, before, make):
        original = make()

        assert await _out_of_a_session(original, before=before) is original


class TestThePredicate:
    @pytest.mark.parametrize(
        "exc",
        [
            DatabaseTimeout("t"),
            DatabaseUnavailable("u"),
            _chained(RuntimeError("wrapped"), DatabaseUnavailable("u")),
        ],
        ids=["timeout", "unavailable", "wrapped"],
    )
    def test_a_named_database_failure_is_unavailable(self, exc):
        assert is_database_unavailable(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            _chained(DatabaseTimeout("t"), CommitOutcomeUnknown("c")),
            _chained(
                RuntimeError("w"), DatabaseUnavailable("u"), CommitOutcomeUnknown("c")
            ),
            ConnectionRefusedError(),
            RuntimeError("a bug"),
        ],
        ids=["commit timeout", "commit outcome under it", "unnamed", "other"],
    )
    def test_anything_else_is_not(self, exc):
        assert not is_database_unavailable(exc)

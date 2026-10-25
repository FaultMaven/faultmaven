"""A connection whose connect-time setup is interrupted is closed by the pool (#2258).

SQLAlchemy's pool runs the ``connect`` events after the creator returns: the
SQLite dialect's own setup (``create_function`` for REGEXP and floor) and every
listener we register. On the aiosqlite adapter each step is an awaited round
trip, so a task cancellation can land in one: a turn cancelled at its ceiling,
on the production SQLite engine, whose ``NullPool`` opens a connection per
checkout. Before SQLAlchemy 2.0.53 (sqlalchemy#13548, with the cancellation
regression of that fix closed in 2.0.55, #13678) nothing closed that
connection: the connected ``aiosqlite.Connection`` and its worker thread were
orphaned until the collector reported them, which failed the suite's warning
gate in CI (#2250). ``pyproject.toml`` pins the floor that closes it.

The listener here stands in for any interrupted step of that setup.
"""

import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.util import await_only

pytestmark = pytest.mark.unit


def _engine(tmp_path, *, interrupt: bool):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-2250.db'}", poolclass=NullPool
    )
    opened = []

    @event.listens_for(engine.sync_engine, "connect")
    def _setup(dbapi_connection, _connection_record):
        # The aiosqlite.Connection the pool just created.
        opened.append(dbapi_connection.driver_connection)
        if interrupt:
            raise asyncio.CancelledError()

    return engine, opened


def _is_open(aiosqlite_connection) -> bool:
    # aiosqlite clears its sqlite3 handle when the connection is closed.
    return aiosqlite_connection._connection is not None


async def test_an_interrupted_connect_setup_closes_the_new_connection(tmp_path):
    engine, opened = _engine(tmp_path, interrupt=True)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        assert len(opened) == 1, "the connect listener never ran"
        assert not _is_open(opened[0]), (
            "the pool left the connection whose setup was interrupted open: it "
            "is orphaned, with its worker thread, until the collector finds it"
        )
    finally:
        await engine.dispose()


async def test_an_uninterrupted_connect_keeps_its_connection(tmp_path):
    """Positive control: the probe sees an open connection when nothing failed."""
    engine, opened = _engine(tmp_path, interrupt=False)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            assert len(opened) == 1
            assert _is_open(opened[0])
        # NullPool closes it on return.
        assert not _is_open(opened[0])
    finally:
        await engine.dispose()


async def test_a_real_cancellation_inside_the_connect_setup_closes_it(tmp_path):
    """The observed failure: ``task.cancel()`` landing in an await of the setup."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-2258.db'}", poolclass=NullPool
    )
    opened = []
    entered = asyncio.Event()
    never = asyncio.Event()

    @event.listens_for(engine.sync_engine, "connect")
    def _setup(dbapi_connection, _connection_record):
        opened.append(dbapi_connection.driver_connection)
        entered.set()
        # A round trip of the setup, as create_function and the PRAGMAs are.
        await_only(never.wait())

    async def _check_out():
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    try:
        task = asyncio.ensure_future(_check_out())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert len(opened) == 1
        assert not _is_open(
            opened[0]
        ), "the cancelled checkout left its connection open"
    finally:
        await engine.dispose()


async def test_a_cancellation_during_the_close_of_a_failed_setup_still_propagates(
    tmp_path, monkeypatch
):
    """What 2.0.55 (sqlalchemy#13678) adds over 2.0.53, and so what pins the floor.

    The setup fails with an ordinary error; the task is then cancelled while the
    pool closes the connection. Before 2.0.55 the cancellation was logged and
    swallowed, and the setup's error propagated instead, so a cancelled turn
    carried on through ``except Exception`` paths.
    """
    engine, opened = _engine(tmp_path, interrupt=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _setup_fails(_dbapi_connection, _connection_record):
        raise RuntimeError("the setup failed")

    dialect = engine.sync_engine.dialect
    real_terminate = dialect.do_terminate

    def _terminate_then_cancelled(dbapi_connection):
        real_terminate(dbapi_connection)
        raise asyncio.CancelledError()

    monkeypatch.setattr(dialect, "do_terminate", _terminate_then_cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        assert not _is_open(opened[0])
    finally:
        monkeypatch.undo()
        await engine.dispose()

"""A connection whose connect-time setup is interrupted is closed by the pool (#2250).

SQLAlchemy's pool runs the ``connect`` events after the creator returns: the
SQLite dialect's own setup (``create_function`` for REGEXP and floor) and every
listener we register. On the aiosqlite adapter each step is an awaited round
trip, so a task cancellation can land in one: a turn cancelled at its ceiling,
on the production SQLite engine, whose ``NullPool`` opens a connection per
checkout. Before SQLAlchemy 2.0.53 (sqlalchemy#13548, with the cancellation
regression of that fix closed in 2.0.55, #13678) nothing closed that
connection: the connected ``aiosqlite.Connection`` and its worker thread were
orphaned until the collector reported them, which failed the suite's warning
gate in CI. ``pyproject.toml`` pins the floor that closes it.

The listener here stands in for any interrupted step of that setup.
"""

import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

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

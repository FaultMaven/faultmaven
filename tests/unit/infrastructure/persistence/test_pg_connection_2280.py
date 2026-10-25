"""A PostgreSQL that stops answering fails a command instead of holding it (#2280).

Measured on #2280 against a paused ``postgres:16``: asyncpg answers a timed-out
or cancelled command by sending a CancelRequest that a silent server never
answers, and every later operation on the connection (SQLAlchemy's rollback and
close among them) waits for it. ``pg_connection`` aborts the connection instead.

No live server: ``tests/silent_postgres.SilentPostgres`` is a loopback
stand-in that completes the handshake, then never answers, and counts the
CancelRequests it receives. Every wait on it is bounded by the test itself
with ``asyncio.wait``, which never cancels what it waits on: cancelling a stock
connection's command is exactly what hangs.

The tests that drive asyncpg skip where it is not installed; the settings, the
socket options and the SQLite arm run everywhere.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from faultmaven.config.settings import DatabaseSettings
from faultmaven.exceptions import DatabaseTimeout
from faultmaven.infrastructure.persistence import database, pg_connection
from tests.silent_postgres import SilentPostgres
from tests.utils import reset_settings_singleton

pytestmark = pytest.mark.unit

requires_asyncpg = pytest.mark.skipif(
    importlib.util.find_spec("asyncpg") is None,
    reason="asyncpg is not installed (the cloud and test extras carry it)",
)

TIMEOUT_KEYS = (
    "DATABASE_COMMAND_TIMEOUT_SECONDS",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS",
    "DATABASE_CLIENT_CONNECTION_CHECK_INTERVAL_SECONDS",
    "REVOCATION_READ_TIMEOUT_SECONDS",
)

PG_URL = "postgresql+asyncpg://fm@127.0.0.1:5432/fm"


@pytest.fixture
def database_env(monkeypatch):
    """The timeout settings at their defaults, plus ``apply(**env)`` to set some."""
    for key in TIMEOUT_KEYS:
        monkeypatch.delenv(key, raising=False)
    reset_settings_singleton()

    def apply(**values) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        reset_settings_singleton()

    yield apply
    monkeypatch.undo()
    reset_settings_singleton()


@pytest.fixture
async def fresh_engine(monkeypatch):
    """No engine built yet; whatever a test builds is disposed afterwards."""
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_engine_bounds", None)
    monkeypatch.setattr(database, "_session_factory", None)
    yield
    if database._engine is not None:
        await database._engine.dispose()


def _bounds(**overrides) -> SimpleNamespace:
    """The settings ``pg_connection`` reads, at their defaults unless overridden."""
    values = {
        "database_command_timeout_seconds": 30.0,
        "database_connect_timeout_seconds": 10.0,
        "database_statement_timeout_margin_seconds": 2.0,
        "database_client_connection_check_interval_seconds": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _outcome(awaitable, within: float):
    """Run ``awaitable`` for at most ``within`` seconds WITHOUT cancelling it.

    Returns the task and whether it finished. A task still pending is the
    caller's to clean up after the stand-in has closed its side.
    """
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=within)
    return task, bool(done)


async def _settle(tasks) -> None:
    """Cancel what is still pending and collect every outcome, so nothing is left unretrieved."""
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()


# ============================================================
# The settings
# ============================================================


class TestTheSettings:
    def test_the_defaults(self, database_env):
        db = DatabaseSettings()

        assert db.database_command_timeout_seconds == 30.0
        assert db.database_connect_timeout_seconds == 10.0
        assert db.database_statement_timeout_margin_seconds == 2.0
        assert db.database_client_connection_check_interval_seconds == 0.0
        assert db.revocation_read_timeout_seconds == 5.0

    def test_each_reads_its_environment_variable(self, database_env):
        database_env(
            DATABASE_COMMAND_TIMEOUT_SECONDS=45,
            DATABASE_CONNECT_TIMEOUT_SECONDS=4,
            DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS=3,
            DATABASE_CLIENT_CONNECTION_CHECK_INTERVAL_SECONDS=1.5,
            REVOCATION_READ_TIMEOUT_SECONDS=2,
        )
        db = DatabaseSettings()

        assert db.database_command_timeout_seconds == 45.0
        assert db.database_connect_timeout_seconds == 4.0
        assert db.database_statement_timeout_margin_seconds == 3.0
        assert db.database_client_connection_check_interval_seconds == 1.5
        assert db.revocation_read_timeout_seconds == 2.0

    def test_a_margin_at_or_above_the_command_timeout_fails_startup(self, database_env):
        database_env(
            DATABASE_COMMAND_TIMEOUT_SECONDS=2,
            DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS=2,
        )
        with pytest.raises(ValidationError, match="must be less than"):
            DatabaseSettings()

    def test_any_margin_stands_while_the_command_timeout_is_off(self, database_env):
        database_env(
            DATABASE_COMMAND_TIMEOUT_SECONDS=0,
            DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS=2,
        )
        assert DatabaseSettings().database_command_timeout_seconds == 0.0

    @pytest.mark.parametrize("key", TIMEOUT_KEYS)
    def test_a_negative_value_fails_startup(self, database_env, key):
        database_env(**{key: -1})
        with pytest.raises(ValidationError):
            DatabaseSettings()


# ============================================================
# The bounds and the connect args
# ============================================================


class TestTheBounds:
    def test_the_statement_timeout_is_the_command_timeout_less_the_margin(self):
        bounds = pg_connection.pg_bounds(PG_URL, _bounds())

        assert bounds == pg_connection.PgBounds(
            command_timeout_seconds=30.0,
            connect_timeout_seconds=10.0,
            statement_timeout_ms=28_000,
            client_connection_check_interval_ms=None,
        )

    def test_zero_disables_each_bound(self):
        bounds = pg_connection.pg_bounds(
            PG_URL,
            _bounds(
                database_command_timeout_seconds=0.0,
                database_connect_timeout_seconds=0.0,
            ),
        )

        assert bounds.command_timeout_seconds is None
        assert bounds.connect_timeout_seconds is None
        assert (
            bounds.statement_timeout_ms is None
        ), "no server bound without a client one"

    def test_the_check_interval_is_set_only_when_opted_in(self):
        bounds = pg_connection.pg_bounds(
            PG_URL, _bounds(database_client_connection_check_interval_seconds=1.5)
        )

        assert bounds.client_connection_check_interval_ms == 1500

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite+aiosqlite:///./data/faultmaven.db",
            "sqlite+aiosqlite:///:memory:",
            "postgresql+psycopg://fm@db/fm",
        ],
    )
    def test_no_connect_args_for_any_other_driver(self, url):
        assert pg_connection.pg_connect_args(url, _bounds()) == {}
        assert pg_connection.pg_bounds(url, _bounds()).command_timeout_seconds is None


@requires_asyncpg
class TestTheConnectArgs:
    def test_the_values_come_from_settings(self):
        args = pg_connection.pg_connect_args(
            PG_URL,
            _bounds(
                database_command_timeout_seconds=12.0,
                database_connect_timeout_seconds=4.0,
            ),
        )

        assert args == {
            "connection_class": pg_connection.abort_on_cancel_connection_class(),
            "timeout": 4.0,
            "command_timeout": 12.0,
        }

    def test_zero_disables_both(self):
        """asyncpg refuses ``command_timeout=0`` and reads a missing connect
        ``timeout`` as 60 s, so off is: no key, and ``None``."""
        args = pg_connection.pg_connect_args(
            PG_URL,
            _bounds(
                database_command_timeout_seconds=0.0,
                database_connect_timeout_seconds=0.0,
            ),
        )

        assert "command_timeout" not in args
        assert args["timeout"] is None
        assert args["connection_class"].__name__ == "AbortOnCancelConnection"

    @pytest.mark.parametrize(
        ("query", "setting"),
        [
            ("command_timeout=60", "DATABASE_COMMAND_TIMEOUT_SECONDS"),
            ("timeout=5", "DATABASE_CONNECT_TIMEOUT_SECONDS"),
        ],
    )
    def test_a_url_that_sets_a_bound_itself_is_refused(self, query, setting):
        """``connect_args`` would silently override the URL's value."""
        with pytest.raises(ValueError, match=setting):
            pg_connection.pg_connect_args(f"{PG_URL}?{query}", _bounds())


# ============================================================
# The private asyncpg hook
# ============================================================


@requires_asyncpg
class TestTheAsyncpgHookStillExists:
    def test_the_cancel_hook_and_the_state_it_touches(self):
        """The abort class overrides ``_cancel_current_command`` and touches
        ``_aborted`` and ``_transport``. An asyncpg release that renames any of
        them leaves the override dead, and the stock cancel back in force: this
        is where that upgrade fails."""
        import asyncpg

        assert callable(getattr(asyncpg.Connection, "_cancel_current_command", None))
        assert {"_aborted", "_transport"} <= set(asyncpg.Connection.__slots__)

    def test_the_statement_cleanup_hook_and_the_state_it_touches(self):
        """The abort class also overrides ``_cleanup_stmts`` and reads
        ``_stmts_to_close`` and ``_protocol``; renamed, the override would be
        dead and asyncpg's unbounded Close back in force."""
        import asyncpg

        assert callable(getattr(asyncpg.Connection, "_cleanup_stmts", None))
        assert {"_stmts_to_close", "_protocol"} <= set(asyncpg.Connection.__slots__)


# ============================================================
# Against a server that stops answering
# ============================================================


async def _connect(server: SilentPostgres, connection_class, how: str):
    import asyncpg

    extra = {"command_timeout": 0.2} if how == "command_timeout" else {}
    return await asyncpg.connect(
        host=server.host,
        port=server.port,
        user="fm",
        database="fm",
        ssl=False,
        connection_class=connection_class,
        **extra,
    )


def _command(conn, how: str):
    query = conn.fetchrow("SELECT 1")
    return query if how == "command_timeout" else asyncio.wait_for(query, 0.2)


@requires_asyncpg
@pytest.mark.parametrize("how", ["command_timeout", "deadline"])
class TestAgainstASilentServer:
    async def test_the_stock_connection_hangs_after_a_timeout(self, how):
        """The shape the abort class exists to remove, pinned so the stand-in
        is known to reproduce it: after the timeout, the next operation and the
        close wait on a CancelRequest the server never answers."""
        import asyncpg

        leftovers = []
        async with SilentPostgres() as server:
            conn = await _connect(server, asyncpg.Connection, how)
            query, done = await _outcome(_command(conn, how), within=5)
            assert done and isinstance(query.exception(), TimeoutError)

            next_op, next_done = await _outcome(conn.execute("ROLLBACK"), within=0.5)
            close, close_done = await _outcome(conn.close(), within=0.5)
            leftovers += [next_op, close]

            assert not next_done, "the next operation waited on the cancel"
            assert not close_done, "the close waited on the cancel"
            assert server.cancel_requests == 1
        await _settle(leftovers)
        conn.terminate()

    async def test_the_abort_connection_fails_at_once_and_sends_no_cancel(self, how):
        leftovers = []
        async with SilentPostgres() as server:
            conn = await _connect(
                server, pg_connection.abort_on_cancel_connection_class(), how
            )
            query, done = await _outcome(_command(conn, how), within=5)
            assert done and isinstance(query.exception(), TimeoutError)

            next_op, next_done = await _outcome(conn.execute("ROLLBACK"), within=0.5)
            close, close_done = await _outcome(conn.close(), within=0.5)
            leftovers += [next_op, close]
            await asyncio.sleep(0.2)  # room for a CancelRequest to arrive, if sent

            assert next_done, "the next operation did not wait"
            import asyncpg

            assert isinstance(next_op.exception(), asyncpg.InterfaceError)
            assert close_done and close.exception() is None
            assert conn.is_closed()
            assert server.cancel_requests == 0
        await _settle(leftovers)


async def _queue_a_statement_for_close(conn) -> None:
    """Prepare a named statement and drop it: asyncpg queues its Close, sent
    after the next prepare (what SQLAlchemy's statement-cache eviction does)."""
    statement = await conn.prepare("SELECT 1")
    del statement
    gc.collect()
    assert conn._stmts_to_close, "positive control: a Close is queued"


@requires_asyncpg
class TestAnEvictedStatementsClose:
    """asyncpg closes evicted prepared statements with no timeout at all. A
    server that froze between a prepare's reply and that Close held the read
    with no bound, whatever the command timeout."""

    async def test_the_stock_connection_waits_on_the_close_unbounded(self):
        import asyncpg

        leftovers = []
        async with SilentPostgres(answer_prepares=True) as server:
            conn = await _connect(server, asyncpg.Connection, "command_timeout")
            await _queue_a_statement_for_close(conn)

            prepare, done = await _outcome(conn.prepare("SELECT 2"), within=1.0)
            leftovers.append(prepare)

            assert server.closes == 1
            assert not done, "the Close waited past five command timeouts"
        await _settle(leftovers)
        conn.terminate()

    async def test_the_abort_connection_bounds_it_by_the_command_timeout(self):
        leftovers = []
        async with SilentPostgres(answer_prepares=True) as server:
            conn = await _connect(
                server,
                pg_connection.abort_on_cancel_connection_class(),
                "command_timeout",
            )
            await _queue_a_statement_for_close(conn)

            prepare, done = await _outcome(conn.prepare("SELECT 2"), within=1.0)
            leftovers.append(prepare)
            await asyncio.sleep(0.2)  # room for a CancelRequest to arrive, if sent

            assert server.closes == 1
            assert done, "the Close is still waiting"
            assert isinstance(prepare.exception(), TimeoutError)
            assert conn.is_closed()
            assert server.cancel_requests == 0
        await _settle(leftovers)


# ============================================================
# A database timeout is named at the session
# ============================================================


class _DriverError(Exception):
    """A driver exception carrying the SQLSTATE PostgreSQL sent."""

    def __init__(self, code):
        super().__init__("driver")
        self.sqlstate = code


def _dbapi_error(driver_error) -> DBAPIError:
    """The shape SQLAlchemy's asyncpg dialect raises: the driver's exception is
    ``orig.__cause__`` (``db_errors``' two hops)."""
    orig = Exception("dbapi")
    orig.__cause__ = driver_error
    return DBAPIError("SELECT pg_sleep(5)", {}, orig)


def _wrapped_by_a_repository(exc: BaseException) -> Exception:
    """As every case repository does: ``RepositoryException(...) from e``."""
    try:
        raise exc
    except BaseException as cause:
        try:
            raise RuntimeError("Failed to get case") from cause
        except RuntimeError as wrapper:
            return wrapper


async def _out_of_a_session(exc: BaseException) -> BaseException:
    database.get_engine("sqlite+aiosqlite:///:memory:")
    with pytest.raises(BaseException) as raised:
        async with database.get_db_session():
            raise exc
    return raised.value


class TestADatabaseTimeoutIsNamed:
    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda: TimeoutError(), id="the driver's timeout"),
            pytest.param(lambda: PoolTimeoutError("pool"), id="the pool's timeout"),
            pytest.param(
                lambda: _dbapi_error(_DriverError("57014")),
                id="the server's statement_timeout",
            ),
            pytest.param(
                lambda: _wrapped_by_a_repository(_dbapi_error(_DriverError("57014"))),
                id="the server's statement_timeout, wrapped",
            ),
        ],
    )
    async def test_each_shape_comes_out_as_a_database_timeout(
        self, database_env, fresh_engine, make
    ):
        original = make()
        raised = await _out_of_a_session(original)

        assert isinstance(raised, DatabaseTimeout)
        assert raised.__cause__ is original

    @requires_asyncpg
    async def test_asyncpgs_own_query_canceled_error(self, database_env, fresh_engine):
        import asyncpg

        cancelled = asyncpg.exceptions.QueryCanceledError(
            "canceling statement due to statement timeout"
        )
        raised = await _out_of_a_session(_dbapi_error(cancelled))

        assert isinstance(raised, DatabaseTimeout)

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda: RuntimeError("a bug"), id="not the database's"),
            pytest.param(
                lambda: _dbapi_error(_DriverError("23505")), id="another SQLSTATE"
            ),
            pytest.param(lambda: DatabaseTimeout("already named"), id="already named"),
        ],
    )
    async def test_anything_else_passes_through_as_itself(
        self, database_env, fresh_engine, make
    ):
        original = make()

        assert await _out_of_a_session(original) is original


@requires_asyncpg
class TestTheEngine:
    async def test_a_read_against_a_silent_server_fails_within_the_command_timeout(
        self, database_env, fresh_engine
    ):
        """Through the engine ``get_engine`` builds and ``get_db_session``:
        the read fails, nothing waits on a cancel, and the pool keeps no
        connection checked out. Without the bounds this read never returns."""
        database_env(
            DATABASE_COMMAND_TIMEOUT_SECONDS=0.3,
            DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS=0.1,
        )

        async def read():
            async with database.get_db_session() as session:
                return (await session.execute(text("SELECT 1"))).scalar()

        leftovers = []
        async with SilentPostgres() as server:
            engine = database.get_engine(
                f"postgresql+asyncpg://fm@{server.host}:{server.port}/fm"
            )
            task, done = await _outcome(read(), within=5)
            leftovers.append(task)
            await asyncio.sleep(0.2)

            assert done, "the read is still waiting on a silent server"
            assert isinstance(task.exception(), TimeoutError)
            assert server.cancel_requests == 0
            assert engine.pool.checkedout() == 0
        await _settle(leftovers)


# ============================================================
# The engine's listeners
# ============================================================


class _RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params))


@requires_asyncpg
class TestTheBeginListener:
    def test_it_sets_the_statement_timeout_beside_the_tenant(
        self, database_env, fresh_engine
    ):
        """One SELECT, transaction-scoped: the enterprise GUC and the server's
        statement_timeout (the command timeout less the margin, in ms)."""
        engine = database.get_engine(PG_URL)  # lazy: never connects
        recorder = _RecordingConnection()

        listeners = list(engine.sync_engine.dispatch.begin)
        for listener in listeners:
            listener(recorder)

        assert len(listeners) == 1
        [(statement, params)] = recorder.statements
        assert statement == (
            "SELECT set_config('app.current_enterprise_id', :enterprise_id, true), "
            "set_config('statement_timeout', :statement_timeout, true)"
        )
        assert params["statement_timeout"] == "28000"
        assert params["enterprise_id"]

    def test_it_sets_the_check_interval_when_opted_in(self, database_env, fresh_engine):
        database_env(DATABASE_CLIENT_CONNECTION_CHECK_INTERVAL_SECONDS=1)
        engine = database.get_engine(PG_URL)
        recorder = _RecordingConnection()

        for listener in engine.sync_engine.dispatch.begin:
            listener(recorder)

        [(statement, params)] = recorder.statements
        assert "set_config('client_connection_check_interval', " in statement
        assert params["client_connection_check_interval"] == "1000"

    def test_with_the_command_timeout_off_only_the_tenant_is_set(
        self, database_env, fresh_engine
    ):
        database_env(DATABASE_COMMAND_TIMEOUT_SECONDS=0)
        engine = database.get_engine(PG_URL)
        recorder = _RecordingConnection()

        for listener in engine.sync_engine.dispatch.begin:
            listener(recorder)

        assert recorder.statements == [
            (
                "SELECT set_config('app.current_enterprise_id', :enterprise_id, true)",
                {"enterprise_id": recorder.statements[0][1]["enterprise_id"]},
            )
        ]

    def test_the_engine_reports_the_bounds_it_was_built_with(
        self, database_env, fresh_engine
    ):
        database.get_engine(PG_URL)

        assert database.active_database_bounds() == pg_connection.PgBounds(
            command_timeout_seconds=30.0,
            connect_timeout_seconds=10.0,
            statement_timeout_ms=28_000,
            client_connection_check_interval_ms=None,
        )

    def test_the_tcp_liveness_hook_is_on_the_connect_event(
        self, database_env, fresh_engine
    ):
        engine = database.get_engine(PG_URL)

        # A pool event: registered on the engine, it lands on the pool.
        assert pg_connection.enable_tcp_liveness in list(
            engine.sync_engine.pool.dispatch.connect
        )


class TestTheSqliteEngine:
    async def test_no_server_timeout_and_no_bounds(self, database_env, fresh_engine):
        """None of the PostgreSQL bounds applies: no begin listener, no
        statement_timeout in what a session sends, nothing reported."""
        engine = database.get_engine("sqlite+aiosqlite:///:memory:")
        sent = []

        def record(_conn, _cursor, statement, *_rest):
            sent.append(statement)

        from sqlalchemy import event

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        async with database.get_db_session() as session:
            await session.execute(text("SELECT 1"))

        assert list(engine.sync_engine.dispatch.begin) == []
        assert not any("statement_timeout" in s or "set_config" in s for s in sent)
        assert database.active_database_bounds() is None


# ============================================================
# TCP liveness on the socket
# ============================================================


def _dbapi_connection_on(sock) -> SimpleNamespace:
    """The shape the ``connect`` event hands the hook: SQLAlchemy's adapted
    connection, whose ``driver_connection`` is asyncpg's, whose transport holds
    the socket."""
    transport = SimpleNamespace(
        get_extra_info=lambda name: sock if name == "socket" else None
    )
    return SimpleNamespace(driver_connection=SimpleNamespace(_transport=transport))


@pytest.mark.skipif(
    not hasattr(socket, "TCP_USER_TIMEOUT"), reason="TCP_USER_TIMEOUT is Linux-only"
)
class TestTcpLiveness:
    @pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
    def test_an_inet_socket_gets_keepalive_and_a_user_timeout(self, family):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            pg_connection.enable_tcp_liveness(_dbapi_connection_on(sock), None)

            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
            assert (
                sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 30_000
            )

    def test_a_unix_socket_is_left_alone(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            pg_connection.enable_tcp_liveness(_dbapi_connection_on(sock), None)

            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0

    def test_a_connection_with_no_socket_is_left_alone(self):
        pg_connection.enable_tcp_liveness(_dbapi_connection_on(None), None)

"""Bounded PostgreSQL connections: what keeps a silent server from holding a caller (#2280).

A PostgreSQL that stops answering once a connection is open (a frozen server, a
dropped route, a proxy whose upstream went away) used to hold every request
that read it, with no bound. Three things were missing, and this module owns
all three for the engine ``database.get_engine`` builds and for the at-head
check in ``bootstrap/data_init``:

1. **Abort, never cancel** (:func:`abort_on_cancel_connection_class`). asyncpg
   answers a timed-out or cancelled command by sending a CancelRequest on a NEW
   connection, with no timeout, and resolves the cancel waiter only when the
   server's ReadyForQuery arrives. Every later operation on the connection
   (SQLAlchemy's rollback and close among them) waits for that first. Neither a
   lost connection nor ``terminate()`` resolves it once the CancelRequest is in
   flight, so against a silent server nothing on the client could free the
   caller. The connection class here aborts the transport instead and resolves
   the waiter at once: the caller fails immediately, no CancelRequest is sent,
   and the pool discards the connection.
2. **Bounded commands and connects** (:func:`pg_connect_args`): asyncpg's
   ``command_timeout`` and connect ``timeout``, from
   ``DATABASE_COMMAND_TIMEOUT_SECONDS`` and ``DATABASE_CONNECT_TIMEOUT_SECONDS``.
   The server's ``statement_timeout`` sits below the command timeout
   (:func:`pg_bounds`, set per transaction by the engine's ``begin``
   listener), so a live but slow server cancels its own statement first and the
   connection survives.
3. **TCP liveness on idle pooled connections** (:func:`enable_tcp_liveness`):
   keepalive and ``TCP_USER_TIMEOUT``, so a connection whose packets are lost
   while it sits in the pool is found dead. It does nothing for a frozen server
   (the kernel still answers) or behind a proxy; item 1 covers those.

asyncpg is in the ``cloud`` extra only, so it is imported lazily: this module
imports, and a SQLite deployment runs, without it.

**Private asyncpg API.** The connection class overrides
``Connection._cancel_current_command`` and sets ``_aborted`` / reads
``_transport``; asyncpg has no public hook that decides whether a cancel is
sent. It also overrides ``Connection._cleanup_stmts`` (reading
``_stmts_to_close`` and ``_protocol``), whose Close of evicted statements
asyncpg sends with no timeout at all. asyncpg stays pinned in the lockfiles, and
``tests/unit/infrastructure/persistence/test_pg_connection_2280.py`` pins the
method and the behaviour against a loopback stand-in for a frozen server, so an
upgrade that moves them fails there.
"""

from __future__ import annotations

import functools
import logging
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from faultmaven.config.settings import DatabaseSettings

logger = logging.getLogger(__name__)


# ============================================================
# 1. Abort, never cancel
# ============================================================


@functools.cache
def abort_on_cancel_connection_class() -> type:
    """``AbortOnCancelConnection``, an ``asyncpg.Connection`` that never sends a CancelRequest.

    Built on first call, because asyncpg is imported only then. Cached, so every
    engine gets the same class.
    """
    import asyncpg

    class AbortOnCancelConnection(asyncpg.Connection):  # type: ignore[misc]
        """A timed-out or cancelled command aborts the connection instead of cancelling it.

        asyncpg calls ``_cancel_current_command`` synchronously from its
        protocol's ``_request_cancel``: on ``command_timeout``, and when the
        task awaiting a command is cancelled (``asyncio.timeout``,
        ``wait_for``). Marking the connection aborted makes every later
        operation, the rollback and close included, fail or finish at once;
        aborting the transport ends the socket; resolving the waiter releases
        whatever was waiting on the cancel to be sent.
        """

        __slots__ = ()

        def _cancel_current_command(self, waiter: Any) -> None:
            self._aborted = True
            self._transport.abort()
            if not waiter.done():
                waiter.set_result(None)

        async def _cleanup_stmts(self) -> None:
            """Close evicted prepared statements under the command timeout.

            asyncpg's own closes them with ``NO_TIMEOUT``. It runs after a new
            prepare whenever a statement was evicted (SQLAlchemy keeps its own
            cache of them per connection), so a server that froze between the
            prepare's reply and the Close's held the read unbounded. ``None``
            is asyncpg's spelling of "the connection's command timeout": on
            expiry it aborts the connection like any other command, and the
            statements go with it.
            """
            to_close = self._stmts_to_close
            self._stmts_to_close = set()
            for stmt in to_close:
                await self._protocol.close_statement(stmt, None)

    return AbortOnCancelConnection


# ============================================================
# 2. Bounded commands, connects and statements
# ============================================================

#: asyncpg connect keywords this module sets from settings, and the setting
#: that owns each. A ``DATABASE_URL`` query carrying one is refused rather than
#: overridden: SQLAlchemy merges ``connect_args`` over the URL's query, so the
#: URL's value would be dropped without a word, and an operator who wrote it
#: would read a bound on ``GET /admin/config/status`` they never chose.
_OWNED_CONNECT_KEYWORDS = {
    "command_timeout": "DATABASE_COMMAND_TIMEOUT_SECONDS",
    "timeout": "DATABASE_CONNECT_TIMEOUT_SECONDS",
}


def _bound(seconds: float) -> Optional[float]:
    """A timeout setting as asyncpg takes it: ``None`` when ``0`` disables it."""
    return float(seconds) if seconds > 0 else None


def _milliseconds(seconds: float) -> int:
    return max(int(seconds * 1000), 1)


def is_asyncpg_url(database_url: str) -> bool:
    """Whether ``database_url`` names PostgreSQL through asyncpg, the async driver used here."""
    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001 - an unparseable URL is not PostgreSQL here
        return False
    return url.get_backend_name() == "postgresql" and url.get_driver_name() == "asyncpg"


@dataclass(frozen=True)
class PgBounds:
    """The bounds a PostgreSQL engine runs with. ``None`` means no bound is set.

    The two client bounds are asyncpg's, so a URL that is not asyncpg gets
    neither. The two server settings are applied per transaction by the
    engine's ``begin`` listener, in milliseconds (PostgreSQL's unit); with
    ``None`` the server's own value stands.
    """

    command_timeout_seconds: Optional[float]
    connect_timeout_seconds: Optional[float]
    statement_timeout_ms: Optional[int]
    client_connection_check_interval_ms: Optional[int]


def pg_bounds(database_url: str, db_settings: "DatabaseSettings") -> PgBounds:
    """The bounds an engine on ``database_url`` gets from settings.

    ``statement_timeout`` is the command timeout less its margin, and is not set
    while the command timeout is off: an operator who turned the client bound
    off for a long statement wants no server bound either. It is set per
    transaction rather than as a startup parameter, which a pooler such as
    PgBouncer refuses or drops.
    """
    command = db_settings.database_command_timeout_seconds
    margin = db_settings.database_statement_timeout_margin_seconds
    interval = db_settings.database_client_connection_check_interval_seconds
    asyncpg_url = is_asyncpg_url(database_url)
    return PgBounds(
        command_timeout_seconds=_bound(command) if asyncpg_url else None,
        connect_timeout_seconds=(
            _bound(db_settings.database_connect_timeout_seconds)
            if asyncpg_url
            else None
        ),
        statement_timeout_ms=_milliseconds(command - margin) if command > 0 else None,
        client_connection_check_interval_ms=(
            _milliseconds(interval) if interval > 0 else None
        ),
    )


def pg_connect_args(
    database_url: str, db_settings: "DatabaseSettings"
) -> dict[str, Any]:
    """The asyncpg ``connect_args`` that bound a connection; ``{}`` for any other database.

    - ``connection_class``: :func:`abort_on_cancel_connection_class`.
    - ``command_timeout``: ``DATABASE_COMMAND_TIMEOUT_SECONDS``; left out when
      ``0`` (asyncpg refuses ``0``, and its own default is no timeout).
    - ``timeout`` (connect): ``DATABASE_CONNECT_TIMEOUT_SECONDS``; ``None`` when
      ``0``, which asyncpg reads as no bound (its own default is 60 s).

    Raises:
        ValueError: The URL's query sets one of these itself (see
            ``_OWNED_CONNECT_KEYWORDS``).
    """
    if not is_asyncpg_url(database_url):
        return {}
    query = make_url(database_url).query
    clashing = sorted(key for key in _OWNED_CONNECT_KEYWORDS if key in query)
    if clashing:
        names = ", ".join(
            f"{key} (set {_OWNED_CONNECT_KEYWORDS[key]} instead)" for key in clashing
        )
        raise ValueError(
            f"DATABASE_URL sets {names} in its query. These connection bounds "
            "come from settings only (#2280); remove them from the URL."
        )
    bounds = pg_bounds(database_url, db_settings)
    args: dict[str, Any] = {
        "connection_class": abort_on_cancel_connection_class(),
        "timeout": bounds.connect_timeout_seconds,
    }
    if bounds.command_timeout_seconds is not None:
        args["command_timeout"] = bounds.command_timeout_seconds
    return args


# ============================================================
# 3. TCP liveness on idle pooled connections
# ============================================================

#: (level, option name, value). Names, not constants, because not every
#: platform defines every option (``TCP_USER_TIMEOUT`` is Linux-only); one a
#: platform lacks is skipped. Found dead after 30 s idle plus 3 unanswered
#: probes 10 s apart, and unacknowledged data fails the socket after 30 s.
TCP_LIVENESS_OPTIONS: tuple[tuple[int, str, int], ...] = (
    (socket.SOL_SOCKET, "SO_KEEPALIVE", 1),
    (socket.IPPROTO_TCP, "TCP_KEEPIDLE", 30),
    (socket.IPPROTO_TCP, "TCP_KEEPINTVL", 10),
    (socket.IPPROTO_TCP, "TCP_KEEPCNT", 3),
    (socket.IPPROTO_TCP, "TCP_USER_TIMEOUT", 30_000),  # milliseconds
)

_INET_FAMILIES = (socket.AF_INET, socket.AF_INET6)


def enable_tcp_liveness(dbapi_connection: Any, _connection_record: Any) -> None:
    """The engine's ``connect`` listener: TCP liveness options on an INET socket.

    A UNIX-domain socket (``host=/var/run/postgresql``) is left alone, as is a
    connection whose socket cannot be found. A refused option is logged, never
    raised: liveness is a backstop, and failing the connect over it would turn
    a backstop into an outage.
    """
    transport = getattr(dbapi_connection.driver_connection, "_transport", None)
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None or sock.family not in _INET_FAMILIES:
        return
    for level, name, value in TCP_LIVENESS_OPTIONS:
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(level, option, value)
        except OSError as exc:
            logger.warning(
                "Could not set %s=%s on a PostgreSQL socket: %s", name, value, exc
            )

"""A loopback stand-in for a PostgreSQL that stops answering (#2280).

It completes asyncpg's startup handshake, then never answers again: it reads
every byte a client sends and replies to none, and the kernel still
acknowledges the packets. That is what a frozen server (``docker pause``) looks
like from the client, measured on #2280 against a paused ``postgres:16``. It
also accepts CancelRequests, counts them, and never answers or closes them,
which is what a frozen postmaster does with one.

With ``answer_prepares=True`` it answers each prepare (Parse, Describe and
Flush, as asyncpg sends them) as a statement with no parameters and no rows,
and goes silent from the first Close it receives, counting it: a server that
froze between a prepare's reply and the Close of an evicted statement.

A test binds it on ``127.0.0.1`` port 0, so the network guard admits the
connection (``tests/network_guard.py``: a port the test process bound itself).

Leaving the context closes every connection from the server side, which is
also what unblocks a client still waiting on one: run it before cancelling the
client's leftover tasks.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Optional

#: The 32-bit codes a client sends in place of a protocol version.
_SSL_REQUEST = 80877103
_CANCEL_REQUEST = 80877102

#: Frontend message types the prepare answer reads.
_PARSE, _FLUSH, _CLOSE = b"P", b"H", b"C"


def _message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!i", len(payload) + 4) + payload


def _parameter(name: str, value: str) -> bytes:
    return _message(b"S", name.encode() + b"\0" + value.encode() + b"\0")


#: AuthenticationOk, the parameters asyncpg reads, BackendKeyData, ReadyForQuery.
_HANDSHAKE = (
    _message(b"R", struct.pack("!i", 0))
    + _parameter("server_version", "16.0")
    + _parameter("client_encoding", "UTF8")
    + _parameter("integer_datetimes", "on")
    + _parameter("standard_conforming_strings", "on")
    + _message(b"K", struct.pack("!ii", 4242, 2280))
    + _message(b"Z", b"I")
)


class SilentPostgres:
    """``async with SilentPostgres() as server:`` then connect to ``server.port``."""

    host = "127.0.0.1"

    def __init__(self, *, answer_prepares: bool = False) -> None:
        self.answer_prepares = answer_prepares
        self.port: Optional[int] = None
        self.cancel_requests = 0
        self.closes = 0
        self.sessions = 0
        self._server: Optional[asyncio.Server] = None
        self._handlers: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def __aenter__(self) -> "SilentPostgres":
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        self._writers.add(writer)
        try:
            length, code = struct.unpack("!ii", await reader.readexactly(8))
            if code == _SSL_REQUEST:
                writer.write(b"N")
                await writer.drain()
                length, code = struct.unpack("!ii", await reader.readexactly(8))
            if code == _CANCEL_REQUEST:
                self.cancel_requests += 1
                await asyncio.Event().wait()  # never answered, never closed
            await reader.readexactly(length - 8)  # the rest of the StartupMessage
            self.sessions += 1
            writer.write(_HANDSHAKE)
            await writer.drain()
            if self.answer_prepares:
                await self._answer_prepares_until_a_close(reader, writer)
            while await reader.read(65536):  # silent from here on
                pass
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _answer_prepares_until_a_close(self, reader, writer) -> None:
        parsed = False
        while True:
            kind = await reader.readexactly(1)
            (length,) = struct.unpack("!i", await reader.readexactly(4))
            await reader.readexactly(length - 4)
            if kind == _CLOSE:
                self.closes += 1
                return
            if kind == _PARSE:
                parsed = True
            elif kind == _FLUSH and parsed:
                parsed = False
                writer.write(  # ParseComplete, no parameters, no rows
                    _message(b"1", b"")
                    + _message(b"t", struct.pack("!h", 0))
                    + _message(b"n", b"")
                )
                await writer.drain()

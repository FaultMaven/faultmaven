"""The Opik SDK at the ping interval ``opik_sdk.configure`` applies to a backend
URL with no path (#2573), on the real SDK against a stub on a port this test
binds.

- A trace parked after a failed send is re-sent at the monitor's next ping once
  the backend is back. At 999999 s the monitor never pinged again, so a replica
  that lost the backend once parked every later trace for its life.
- At that interval the monitor's own ping can race the case delete's flush.
  The eraser reads delivery from the parked count, not from the monitor's flag,
  and holding the SDK's replay lock, so no replay of the monitor's is half done.

The monitor's clock is frozen and moved by the test, so it pings exactly when a
test moves it one interval on, and never on its own.
"""

import asyncio
import atexit
import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

opik = pytest.importorskip("opik")

import tenacity  # noqa: E402
from opik import httpx_client  # noqa: E402
from opik.healthcheck import connection_monitor  # noqa: E402
from opik.rest_client_configurator import retry_decorator  # noqa: E402

from faultmaven.config.settings import ObservabilitySettings  # noqa: E402
from faultmaven.exceptions import ServiceException  # noqa: E402
from faultmaven.infrastructure.observability import opik_sdk  # noqa: E402
from faultmaven.infrastructure.observability.trace_erasure import (  # noqa: E402
    LLMTraceEraser,
)

pytestmark = pytest.mark.unit

MARK = "parked-trace-2573"
CASE = "case_aabb20262573"
ENTERPRISE = "00000000-0000-0000-0000-000000000002"
EMPTY_PAGE = json.dumps({"page": 1, "size": 0, "total": 0, "content": []}).encode()


class _Backend:
    """One listening socket for the whole test. ``accepting`` False holds each
    write unanswered past the client's read timeout, the failure the SDK parks
    a message on; ``ping_ok`` False answers the ping 503. Every other read is an
    empty page. ``on_delivery`` runs before a write carrying ``MARK`` is
    answered."""

    def __init__(self):
        self.accepting = True
        self.ping_ok = True
        self.on_delivery = None
        self.received = threading.Event()
        self.released = threading.Event()
        backend = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_):
                pass

            def _answer(self, status, data=b""):
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/is-alive/ping"):
                    return self._answer(200 if backend.ping_ok else 503, b"{}")
                self._answer(200, EMPTY_PAGE)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.headers.get("content-encoding") == "gzip":
                    body = gzip.decompress(body)
                if not backend.accepting:
                    backend.released.wait(10)
                    return
                if MARK.encode() in body:
                    if backend.on_delivery is not None:
                        backend.on_delivery()
                    backend.received.set()
                self._answer(204)

            do_PATCH = do_PUT = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def release(self):
        """Answer every write again, and the ones held."""
        self.accepting = True
        self.released.set()

    def stop(self):
        self.release()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def _until(condition, what):
    """Bounded: a hang fails, never passes."""
    for _ in range(1000):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"{what} never happened")


@pytest.fixture
def backend():
    backend = _Backend()
    yield backend
    backend.stop()


@pytest.fixture
def clock(monkeypatch):
    """The monitor's clock, frozen: ``clock.now`` moves only when a test moves it."""
    clock = SimpleNamespace(now=time.time())
    monkeypatch.setattr(
        connection_monitor, "time", SimpleNamespace(time=lambda: clock.now)
    )
    return clock


@pytest.fixture
def client(backend, clock, monkeypatch):
    """The process's client, built as a traced call builds it after
    ``opik_sdk.configure`` points the SDK at the stub, once it has pinged."""
    import opik.config
    from opik.api_objects import opik_client

    for name in (
        "OPIK_URL_OVERRIDE",
        "OPIK_PROJECT_NAME",
        "OPIK_WORKSPACE",
        "OPIK_API_KEY",
        "OPIK_CONNECTION_MONITOR_PING_INTERVAL",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("OPIK_TRACK_DISABLE", raising=False)
    # The replay thread ticks this often (the SDK's 0.3 s), so a held write's
    # read timeout leaves a wide margin.
    monkeypatch.setenv("OPIK_REPLAY_TICK_INTERVAL", "0.02")
    monkeypatch.setattr(opik.config, "_SESSION_CACHE_DICT", {})
    monkeypatch.setattr(opik_sdk, "_record", opik_sdk.backend())
    monkeypatch.setattr(
        opik_sdk, "_track_disable_prior_value", opik_sdk._TRACK_DISABLE_UNRECORDED
    )
    # One attempt per send, failing at a short read timeout: the SDK's retries
    # and its 100 s read timeout are not what is tested.
    monkeypatch.setattr(
        retry_decorator,
        "opik_rest_retry",
        tenacity.retry(stop=tenacity.stop_after_attempt(1), reraise=True),
    )
    monkeypatch.setattr(httpx_client, "READ_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(opik_client, "_global_singleton", None)
    settings = SimpleNamespace(
        observability=ObservabilitySettings(
            opik_enabled=True, opik_url_override=backend.url
        )
    )
    record = opik_sdk.configure(settings)
    assert record.ping_interval_s == opik_sdk.SDK_PING_INTERVAL_S

    client = opik_sdk.cached_client()
    atexit.unregister(client.end)
    monitor = opik_sdk.replay_monitor(client)
    assert monitor.ping_interval == opik_sdk.SDK_PING_INTERVAL_S
    _until(lambda: monitor.last_beat == clock.now, "the monitor's first ping")
    yield client
    backend.release()
    client.end(timeout=1)


def _ping(clock):
    """One interval of the SDK's own passes: the monitor pings at its next tick."""
    clock.now += opik_sdk.SDK_PING_INTERVAL_S


def _park(backend, client):
    """One send of a trace fails, so the SDK parks it."""
    backend.accepting = False
    client.trace(name=MARK)
    client.flush(timeout=10)
    assert opik_sdk.connection_state() == ("disconnected", 1)


def _eraser(client):
    return LLMTraceEraser("faultmaven", client_factory=lambda: client)


async def _until_async(condition, what):
    """``_until`` with the loop free, so a future can complete."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} never happened")


class _WatchedLock:
    """The SDK's replay lock, reporting when a thread starts waiting on it."""

    def __init__(self, lock):
        self._lock, self.waited_on = lock, threading.Event()

    def acquire(self, *args, **kwargs):
        self.waited_on.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()


def test_a_parked_trace_is_resent_at_the_next_ping(backend, client, clock):
    _park(backend, client)
    backend.accepting = True

    _ping(clock)

    _until(backend.received.is_set, "the replay of the parked trace")
    _until(
        lambda: opik_sdk.connection_state() == ("connected", 0),
        "the parked count clearing",
    )


class TestAPingRacingTheDeletesFlush:
    async def test_a_failed_ping_while_the_replay_is_delivered_lets_it_proceed(
        self, backend, client, clock
    ):
        """The monitor's ping fails mid-flush and lowers the flag, while the
        replayed trace is delivered: nothing is parked, so the delete runs."""
        monitor = opik_sdk.replay_monitor(client)
        _park(backend, client)

        def ping_fails_mid_send():
            backend.ping_ok = False
            _ping(clock)
            _until(lambda: not monitor.has_server_connection, "the failed ping")

        backend.on_delivery = ping_fails_mid_send
        backend.accepting = True

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 0

        assert backend.received.is_set()
        assert opik_sdk.connection_state() == ("disconnected", 0)

    async def test_a_good_ping_before_the_monitors_replay_refuses(
        self, backend, client, clock, monkeypatch
    ):
        """The flush's send fails, then, after the delete's second flush, a good
        ping raises the flag before the monitor replays the trace, which would
        land after the search."""
        monitor = opik_sdk.replay_monitor(client)
        manager = client._streamer._fallback_replay_manager
        decided, flushes = threading.Event(), []
        real_replay, real_flush = manager._replay_failed_messages, client.flush

        def replay():
            if threading.current_thread() is manager:
                decided.wait(10)
            real_replay()

        def flush(timeout=None):
            flushed = real_flush(timeout=timeout)
            flushes.append(1)
            if len(flushes) == 2:
                _ping(clock)
                _until(lambda: monitor.has_server_connection, "the good ping")
            return flushed

        monkeypatch.setattr(manager, "_replay_failed_messages", replay)
        monkeypatch.setattr(client, "flush", flush)
        backend.accepting = False
        client.trace(name=MARK)

        try:
            with pytest.raises(ServiceException, match="llm_traces.*unreachable"):
                await _eraser(client).erase_case(CASE, ENTERPRISE)
            assert opik_sdk.connection_state() == ("connected", 1)
        finally:
            backend.accepting = True
            decided.set()
        _until(backend.received.is_set, "the monitor's replay")

    async def test_the_delete_waits_out_a_replay_that_has_marked_its_traces_in_progress(
        self, backend, client, clock, monkeypatch
    ):
        """The monitor's replay has marked the parked trace in progress (the
        count reads 0) and not yet queued it: the delete waits for its replay lock
        and returns only after the trace is delivered."""
        manager = client._streamer._fallback_replay_manager
        db = manager._db_manager
        lock = _WatchedLock(opik_sdk.replay_mutex(client))
        monkeypatch.setattr(opik_sdk, "replay_mutex", lambda client: lock)
        marked, release = threading.Event(), threading.Event()
        real = db._replay_messages

        def replay_messages(db_messages, replay_callback):
            if threading.current_thread() is manager:
                marked.set()
                release.wait(10)
            return real(db_messages=db_messages, replay_callback=replay_callback)

        monkeypatch.setattr(db, "_replay_messages", replay_messages)
        _park(backend, client)
        backend.accepting = True
        order = []
        backend.on_delivery = lambda: order.append("delivered")
        _ping(clock)
        assert marked.wait(10)
        assert opik_sdk.parked_count(client) == 0

        erase = asyncio.ensure_future(_eraser(client).erase_case(CASE, ENTERPRISE))
        try:
            await _until_async(
                lambda: lock.waited_on.is_set() or erase.done(), "the delete's wait"
            )
        finally:
            release.set()
        order.append(f"erase returned {await erase}")

        assert order == ["delivered", "erase returned 0"]

    async def test_a_backend_back_during_the_delete_leaves_nothing_stranded(
        self, backend, client, clock, monkeypatch
    ):
        """The backend comes back during the delete's second flush and the
        monitor's recovery replay runs then. That replay only tries the SDK's
        replay lock, so the delete must not hold it across a flush: it would
        replay nothing, and the trace would stay parked on a connected pod."""
        manager = client._streamer._fallback_replay_manager
        recovered, flushes = threading.Event(), []
        real_replay, real_flush = manager._replay_failed_messages, client.flush

        def replay():
            real_replay()
            if threading.current_thread() is manager:
                recovered.set()

        def flush(timeout=None):
            flushes.append(1)
            if len(flushes) == 2:
                backend.accepting = True
                _ping(clock)
                recovered.wait(10)
            return real_flush(timeout=timeout)

        _park(backend, client)
        monkeypatch.setattr(manager, "_replay_failed_messages", replay)
        monkeypatch.setattr(client, "flush", flush)

        (outcome,) = await asyncio.gather(
            _eraser(client).erase_case(CASE, ENTERPRISE), return_exceptions=True
        )
        for _ in range(3):
            _ping(clock)

        assert recovered.is_set() and len(flushes) == 2
        _until(backend.received.is_set, "the parked trace's delivery")
        assert opik_sdk.connection_state() == ("connected", 0)
        assert outcome == 0

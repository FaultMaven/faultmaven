"""A trace parked after a failed send is re-sent once the backend is back and
the SDK's connection monitor pings, at the interval ``opik_sdk.configure`` sets
(#2573). On the real Opik SDK against a stub on a port this test binds.

At 999999 s the monitor never pinged again, so a replica that lost the backend
once parked every later trace for the rest of its life.
"""

import atexit
import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

opik = pytest.importorskip("opik")

import tenacity  # noqa: E402
from opik.api_objects import opik_client  # noqa: E402
from opik.config import OpikConfig  # noqa: E402
from opik.rest_client_configurator import retry_decorator  # noqa: E402

from faultmaven.config.settings import ObservabilitySettings  # noqa: E402
from faultmaven.infrastructure.observability import opik_sdk  # noqa: E402

pytestmark = pytest.mark.unit

MARK = "parked-trace-2573"
SDK_DEFAULT = OpikConfig.model_fields["connection_monitor_ping_interval"].default


class _Backend:
    """Answers every GET (the ping included) 200 and every write 204, and records
    the write bodies. HTTP/1.0, so a stopped server's port is really closed."""

    def __init__(self, port=0):
        self.bodies: list = []
        self.received = threading.Event()
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
                self._answer(200, b'{"healthy":true}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.headers.get("content-encoding") == "gzip":
                    body = gzip.decompress(body)
                backend.bodies.append(body)
                if MARK.encode() in body:
                    backend.received.set()
                self._answer(204)

            do_PATCH = do_PUT = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()
        self.port = self._server.server_address[1]

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()
            self._server = None


def _until(condition, what):
    """Bounded: a hang fails, never passes."""
    for _ in range(1000):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"{what} never happened")


@pytest.fixture
def backends():
    started: list = []
    yield started
    for backend in started:
        backend.stop()


@pytest.fixture
def client(backends, monkeypatch):
    """The process's client, built as a traced call builds it after
    ``opik_sdk.configure`` points the SDK at the stub; ended after."""
    import opik.config

    backends.append(_Backend())
    for name in (
        "OPIK_URL_OVERRIDE",
        "OPIK_PROJECT_NAME",
        "OPIK_WORKSPACE",
        "OPIK_API_KEY",
        "OPIK_CONNECTION_MONITOR_PING_INTERVAL",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("OPIK_TRACK_DISABLE", raising=False)
    monkeypatch.setattr(opik.config, "_SESSION_CACHE_DICT", {})
    monkeypatch.setattr(opik_sdk, "_record", opik_sdk.backend())
    monkeypatch.setattr(
        opik_sdk, "_track_disable_prior_value", opik_sdk._TRACK_DISABLE_UNRECORDED
    )
    # One attempt per send: the failed send needs no 11 s of SDK retries.
    monkeypatch.setattr(
        retry_decorator,
        "opik_rest_retry",
        tenacity.retry(stop=tenacity.stop_after_attempt(1), reraise=True),
    )
    monkeypatch.setattr(opik_client, "_global_singleton", None)
    url = f"http://127.0.0.1:{backends[0].port}"
    settings = SimpleNamespace(
        observability=ObservabilitySettings(opik_enabled=True, opik_url_override=url)
    )
    record = opik_sdk.configure(settings)
    assert record.configured and record.ping_interval_s == opik_sdk.PING_INTERVAL_S

    client = opik_sdk.cached_client()
    atexit.unregister(client.end)
    yield client
    client.end(timeout=1)


def test_a_parked_trace_is_resent_at_the_next_ping(backends, client):
    monitor = opik_sdk.replay_monitor(client)
    assert monitor.ping_interval == SDK_DEFAULT
    _until(lambda: monitor.last_beat > float("-inf"), "the monitor's first ping")
    port = backends[0].port
    backends[0].stop()

    client.trace(name=MARK)
    client.flush(timeout=10)
    assert opik_sdk.connection_state() == ("disconnected", 1)

    backends.append(_Backend(port))
    # One interval of the SDK's default passes since the last ping, no sleep.
    monitor.last_beat -= SDK_DEFAULT

    _until(backends[1].received.is_set, "the replay of the parked trace")
    _until(
        lambda: opik_sdk.connection_state() == ("connected", 0),
        "the parked count clearing",
    )

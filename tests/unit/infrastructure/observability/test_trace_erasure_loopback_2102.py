"""The case delete's eraser on the real Opik SDK, against a stub on a port this
test binds (#2102). No live backend and no model call: what a real backend does
with these requests is still untested.

The fake client in ``test_trace_erasure_2102.py`` could not show any of this: the
SDK's parser swallowing a bad line, ``flush`` returning True over a parked trace,
or what a failed call puts on the wire.
"""

import atexit
import gzip
import json
import threading
import time
import typing
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

opik = pytest.importorskip("opik")

from opik.api_objects import opik_client  # noqa: E402
from opik.rest_api.spans.types.span_search_stream_request_public_exclude_item import (  # noqa: E402
    SpanSearchStreamRequestPublicExcludeItem,
)
from opik.rest_api.traces.types.trace_search_stream_request_public_exclude_item import (  # noqa: E402
    TraceSearchStreamRequestPublicExcludeItem,
)

from faultmaven.config.tenant_context import set_current_enterprise_id  # noqa: E402
from faultmaven.exceptions import ServiceException  # noqa: E402
from faultmaven.infrastructure.llm import router as router_module  # noqa: E402
from faultmaven.infrastructure.llm.providers.base import LLMResponse  # noqa: E402
from faultmaven.infrastructure.observability import (  # noqa: E402
    trace_erasure,
    tracing,
)
from faultmaven.infrastructure.observability.trace_erasure import (  # noqa: E402
    SPAN_EXCLUDE,
    TRACE_EXCLUDE,
    TRACE_PAGE_SIZE,
    LLMTraceEraser,
)
from tests.model_content import vetted  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("restore_tenant_context")]

PROJECT = "fm-loopback"
CASE = "case_aabb20262102"
ENTERPRISE = "00000000-0000-0000-0000-000000000002"
BODY = "Provider 400: invalid request near 'a line of the case evidence'"
ERROR_LINE = b'{"code":500,"message":"Streaming operation timed out"}\n'


def _ndjson(rows) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class _Backend:
    """The REST calls the SDK makes. HTTP/1.0, so a closed port is really closed:
    a kept-alive connection would go on answering after the server stopped."""

    def __init__(self):
        self.trace_stream = lambda request: b""
        self.span_stream = lambda request: b""
        self.searches = {"traces": [], "spans": []}
        self.deleted: list = []
        self.traces: list = []
        self.spans: list = []
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
                self._answer(200, b'{"message":"ok"}')

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.headers.get("content-encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw or b"{}")
                if self.path.endswith("/traces/search"):
                    backend.searches["traces"].append(body)
                    return self._answer(200, backend.trace_stream(body))
                if self.path.endswith("/spans/search"):
                    backend.searches["spans"].append(body)
                    return self._answer(200, backend.span_stream(body))
                if self.path.endswith("/traces/delete"):
                    backend.deleted.append(body["ids"])
                elif self.path.endswith("/traces/batch"):
                    backend.traces.extend(body["traces"])
                elif self.path.endswith("/spans/batch"):
                    backend.spans.extend(body["spans"])
                self._answer(204)

            do_PATCH = do_PUT = do_POST

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()
            self._server = None

    def answer_from_what_it_ingested(self):
        def stream(request):
            case = next(f["value"] for f in request["filters"] if f["key"] == "case_id")
            ids = {
                t["id"]
                for t in self.traces
                if (t.get("metadata") or {}).get("case_id") == case
            }
            return _ndjson({"id": i} for i in sorted(ids))

        self.trace_stream = stream


@pytest.fixture
def backend():
    backend = _Backend()
    yield backend
    backend.stop()


@pytest.fixture
def client(backend, monkeypatch):
    """A real client on the stub, the process's own, ended with every thread it
    started: no test leaves one behind."""
    monkeypatch.setenv("OPIK_CONNECTION_MONITOR_PING_INTERVAL", "999999")
    monkeypatch.delenv("OPIK_TRACK_DISABLE", raising=False)
    monkeypatch.setattr(tracing, "_tracing_configured", True)
    already_running = set(threading.enumerate())

    client = opik_client.Opik(
        project_name=PROJECT,
        workspace="default",
        host=backend.url,
        _show_misconfiguration_message=False,
    )
    atexit.unregister(client.end)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm-trace-erase")
    monkeypatch.setattr(trace_erasure, "_ERASE_EXECUTOR", pool)
    monkeypatch.setattr(opik_client, "_global_singleton", client)
    opik.set_tracing_active(True)

    yield client

    client.end(timeout=1)
    pool.shutdown(wait=True)
    backend.stop()
    opik.reset_tracing_to_config_default()
    started = [t for t in threading.enumerate() if t not in already_running]
    for thread in started:
        thread.join(timeout=10)
    assert [t.name for t in started if t.is_alive()] == []


def _eraser(client, project=PROJECT) -> LLMTraceEraser:
    return LLMTraceEraser(project, client_factory=lambda: client)


async def _route(*, fails=False):
    """The real ``route()`` under the real ``opik.track``. The registry stands in
    for the providers, so no model is called."""
    registry = MagicMock()
    registry.get_available_providers.return_value = ["p"]
    registry.get_fallback_chain.return_value = ["p"]
    registry.route_request = AsyncMock(
        side_effect=RuntimeError(BODY) if fails else None,
        return_value=LLMResponse(
            content="the answer",
            confidence=0.9,
            provider="p",
            model="m",
            tokens_used=1,
            response_time_ms=1,
        ),
    )
    set_current_enterprise_id(ENTERPRISE)
    with (
        patch.object(router_module, "get_registry", return_value=registry),
        patch.object(router_module, "_opik_tracing_enabled", return_value=True),
    ):
        await router_module.LLMRouter().route(
            prompt=vetted("the prompt"), model="m", case_id=CASE
        )


def _after_the_first_ping(client):
    """The monitor, once it has pinged. It pings on its first tick and, at this
    interval, never again. Until then a test does not own the flag: a good ping
    restores it, and one that lands on a stopped backend lowers it mid-erase."""
    monitor = client._streamer._fallback_replay_manager._monitor
    deadline = time.monotonic() + 10
    while monitor.last_beat == float("-inf"):
        assert time.monotonic() < deadline, "the monitor never pinged"
        time.sleep(0.01)
    return monitor


def _lower_the_flag(client):
    """What one failed send does (``ReplayManager.message_sent_failed``). The real
    blip costs the SDK's retries, about 11 s."""
    _after_the_first_ping(client).connection_failed(failure_reason="a blip")


class TestTheRead:
    @pytest.mark.parametrize("stream", ["trace_stream", "span_stream"])
    async def test_a_bad_line_in_a_200_stream_fails(self, backend, client, stream):
        setattr(backend, stream, lambda _: ERROR_LINE)

        with pytest.raises(ServiceException, match="llm_traces.*not a row"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert backend.deleted == []

    async def test_a_full_page_with_a_bad_line_claims_no_partial_success(
        self, backend, client
    ):
        backend.trace_stream = lambda request: (
            _ndjson({"id": f"t{i}"} for i in range(request["limit"] - 1)) + ERROR_LINE
        )

        with pytest.raises(ServiceException, match="llm_traces.*not a row"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert backend.deleted == []

    async def test_it_pages_across_a_full_page(self, backend, client):
        ids = [f"t{i}" for i in range(TRACE_PAGE_SIZE + 5)]

        def pages(request):
            cursor = request["last_retrieved_id"]
            start = ids.index(cursor) + 1 if cursor else 0
            return _ndjson({"id": i} for i in ids[start : start + request["limit"]])

        backend.trace_stream = pages

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == len(ids)

        assert [i for chunk in backend.deleted for i in chunk] == ids
        assert max(len(chunk) for chunk in backend.deleted) <= 1000
        assert [s["last_retrieved_id"] for s in backend.searches["traces"]] == [
            None,
            ids[TRACE_PAGE_SIZE - 1],
        ]

    async def test_a_trace_found_only_by_its_span_is_deleted(self, backend, client):
        backend.span_stream = lambda _: _ndjson([{"id": "s1", "trace_id": "t9"}])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        assert backend.deleted == [["t9"]]

    async def test_the_requests_as_sent(self, backend, client):
        await _eraser(client).erase_case(CASE, ENTERPRISE)

        filters = [
            {
                "field": "metadata",
                "operator": "=",
                "key": key,
                "value": value,
                "type": "dictionary",
            }
            for key, value in (("case_id", CASE), ("enterprise_id", ENTERPRISE))
        ]
        for kind, exclude in (("traces", TRACE_EXCLUDE), ("spans", SPAN_EXCLUDE)):
            assert backend.searches[kind] == [
                {
                    "project_name": PROJECT,
                    "filters": filters,
                    "last_retrieved_id": None,
                    "limit": TRACE_PAGE_SIZE,
                    "exclude": exclude,
                }
            ]

    @pytest.mark.parametrize(
        "excluded, admitted",
        [
            (TRACE_EXCLUDE, TraceSearchStreamRequestPublicExcludeItem),
            (SPAN_EXCLUDE, SpanSearchStreamRequestPublicExcludeItem),
        ],
        ids=["traces", "spans"],
    )
    def test_every_field_the_sdk_admits_is_excluded(self, excluded, admitted):
        literal, _ = typing.get_args(admitted)
        assert sorted(excluded) == sorted(typing.get_args(literal))

    async def test_no_project_name_sends_the_clients_project(self, backend, client):
        await _eraser(client, project=None).erase_case(CASE, ENTERPRISE)

        assert backend.searches["traces"][0]["project_name"] == PROJECT


class TestWhatThisPodHolds:
    async def test_a_parked_trace_is_replayed_and_deleted(self, backend, client):
        backend.answer_from_what_it_ingested()
        _lower_the_flag(client)
        await _route()
        assert client.flush(timeout=20) is True
        assert not [t for t in backend.traces if t.get("metadata")]  # it is parked

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        (tagged,) = [t for t in backend.traces if t.get("metadata")]
        assert backend.deleted == [[tagged["id"]]]

    @pytest.mark.slow
    async def test_a_backend_that_is_down_fails_the_replay(self, backend, client):
        """About 11 s: the SDK's own retries of the replayed send."""
        _lower_the_flag(client)
        await _route()
        backend.stop()

        with pytest.raises(ServiceException, match="llm_traces.*unreachable"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

    async def test_a_backend_that_is_down_fails_the_search(self, backend, client):
        _after_the_first_ping(client)
        backend.stop()

        with pytest.raises(httpx.ConnectError):
            await _eraser(client).erase_case(CASE, ENTERPRISE)


class TestTheTag:
    async def test_a_failed_call_has_both_tags_on_the_wire_beside_its_error(
        self, backend, client
    ):
        with pytest.raises(RuntimeError):
            await _route(fails=True)
        client.flush(timeout=20)

        owner = {"case_id": CASE, "enterprise_id": ENTERPRISE}
        (trace,) = [t for t in backend.traces if t.get("error_info")]
        (span,) = [s for s in backend.spans if s.get("error_info")]
        for recorded in (trace, span):
            assert recorded["error_info"]["message"] == BODY
            assert recorded["metadata"] == owner
        assert trace["thread_id"] == CASE

"""The case delete's eraser on the real Opik SDK, against a stub on a port this
test binds (#2102), and the draft and document deletes' (#2575, ``TestAConversion``). No live backend and no model call: what a real backend does
with these requests is still untested.

The stub answers the paged list endpoints as the backend's source at
``comet-ml/opik`` ``f356a884`` builds them: it counts the filtered rows, then
reads a page of them by offset, newest id first, and answers an error as a
status. The fake client in ``test_trace_erasure_2102.py`` could not show what
the SDK sends, ``flush`` returning True over a parked trace, or what a failed
call puts on the wire.
"""

import asyncio
import atexit
import gzip
import json
import threading
import typing
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

opik = pytest.importorskip("opik")

from opik.api_objects import opik_client  # noqa: E402
from opik.healthcheck.connection_monitor import OpikConnectionMonitor  # noqa: E402
from opik.rest_api.core.api_error import ApiError  # noqa: E402
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
OTHER = "case_ccdd20262102"
ENTERPRISE = "00000000-0000-0000-0000-000000000002"
BODY = "Provider 400: invalid request near 'a line of the case evidence'"

#: Set by the connection monitor's first tick, which is its one ping at the
#: interval these tests run with (``client`` clears it).
_first_tick = threading.Event()


class _Backend:
    """The REST calls the SDK makes. HTTP/1.0, so a closed port is really closed:
    a kept-alive connection would go on answering after the server stopped."""

    def __init__(self):
        #: Per kind, what a page request answers instead of the stored rows:
        #: ``(query) -> (status, body)``.
        self.page = {"traces": None, "spans": None}
        self.reads = {"traces": [], "spans": []}
        self.deleted: list = []
        self.traces: list = []
        self.spans: list = []
        self.stored = {"traces": {}, "spans": {}}
        backend = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_):
                pass

            def _answer(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                kind = url.path.rstrip("/").rsplit("/", 1)[-1]
                if kind not in backend.reads:
                    return self._answer(200, {"message": "ok"})
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                backend.reads[kind].append(query)
                answer = backend.page[kind] or backend.serve
                self._answer(*answer(query, kind))

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.headers.get("content-encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw or b"{}")
                if self.path.endswith("/traces/delete"):
                    backend.delete(body["ids"])
                elif self.path.endswith("/traces/batch"):
                    backend.ingest("traces", body["traces"])
                elif self.path.endswith("/spans/batch"):
                    backend.ingest("spans", body["spans"])
                self._answer(204)

            do_PATCH = do_PUT = do_POST

        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"

    def ingest(self, kind, rows):
        with self._lock:
            (self.traces if kind == "traces" else self.spans).extend(rows)
            for row in rows:
                stored = self.stored[kind].setdefault(row["id"], {})
                stored.update({k: v for k, v in row.items() if v is not None})

    def delete(self, ids):
        with self._lock:
            self.deleted.append(ids)
            for trace_id in ids:
                self.stored["traces"].pop(trace_id, None)
            for span_id, span in list(self.stored["spans"].items()):
                if span["trace_id"] in ids:
                    del self.stored["spans"][span_id]

    def serve(self, query, kind):
        """Count the filtered rows, then read a page of them by offset."""
        wanted = {f["key"]: f["value"] for f in json.loads(query["filters"])}
        with self._lock:
            rows = [
                row
                for row in self.stored[kind].values()
                if all(
                    (row.get("metadata") or {}).get(k) == v for k, v in wanted.items()
                )
            ]
        rows.sort(key=lambda row: row["id"], reverse=True)
        page, size = int(query["page"]), int(query["size"])
        content = [
            {k: row[k] for k in ("id", "trace_id", "start_time") if k in row}
            for row in rows[(page - 1) * size : page * size]
        ]
        return 200, _page_body(content, total=len(rows), page=page)

    def remaining(self, case):
        """What is stored for ``case`` and holds content."""
        with self._lock:
            return [
                row
                for row in self.stored["spans"].values()
                if (row.get("metadata") or {}).get("case_id") == case
                and row.get("input")
            ]

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()
            self._server = None


def _page_body(content, total, page=1):
    return {"page": page, "size": len(content), "total": total, "content": content}


def _rows(start, stop):
    return [
        {"id": f"t{i:05d}", "start_time": "2026-11-06T00:00:00Z"}
        for i in range(start, stop)
    ]


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
    real_tick = OpikConnectionMonitor.tick

    def tick(self):
        status = real_tick(self)
        _first_tick.set()
        return status

    monkeypatch.setattr(OpikConnectionMonitor, "tick", tick)
    _first_tick.clear()
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


async def _route(*, case_id=CASE, conversion_id=None, fails=False):
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
            prompt=vetted(f"the prompt of {case_id or conversion_id}"),
            model="m",
            case_id=case_id,
            conversion_id=conversion_id,
        )


def _after_the_first_ping(client):
    """The monitor, once it has pinged. It pings on its first tick and, at this
    interval, never again. Until then a test does not own the flag: a good ping
    restores it, and one that lands on a stopped backend lowers it mid-erase."""
    assert _first_tick.wait(10), "the monitor never ticked"
    return client._streamer._fallback_replay_manager._monitor


def _lower_the_flag(client):
    """What one failed send does (``ReplayManager.message_sent_failed``). The real
    blip costs the SDK's retries, about 11 s."""
    _after_the_first_ping(client).connection_failed(failure_reason="a blip")


async def _until(condition):
    """Yield to the loop until ``condition`` holds. A hang fails, never passes."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


class TestTheRead:
    @pytest.mark.slow
    async def test_a_500_page_fails_with_nothing_deleted(self, backend, client):
        """About 11 s: the SDK retries a 500 twice before it raises."""
        backend.page["traces"] = lambda query, _: (
            (200, _page_body(_rows(0, TRACE_PAGE_SIZE), total=TRACE_PAGE_SIZE + 5))
            if query["page"] == "1"
            else (500, {"errors": ["a query error"]})
        )

        with pytest.raises(
            ServiceException, match=f"llm_traces for case {CASE}: ApiError$"
        ) as refused:
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        # Wrapped by its class (#2575): its text carries the backend's body.
        assert isinstance(refused.value.__cause__, ApiError)
        assert refused.value.__cause__.status_code == 500
        assert "a query error" not in str(refused.value)
        assert backend.deleted == []
        assert [q["page"] for q in backend.reads["traces"]] == ["1", "2", "2", "2"]

    @pytest.mark.parametrize(
        "served, total",
        [(_rows(0, TRACE_PAGE_SIZE - 1), TRACE_PAGE_SIZE + 5), (_rows(0, 2), 3)],
        ids=["a-short-page-before-the-total", "a-total-above-the-rows-served"],
    )
    async def test_rows_short_of_the_total_fail_with_nothing_deleted(
        self, backend, client, served, total
    ):
        backend.page["traces"] = lambda *_: (200, _page_body(served, total=total))

        with pytest.raises(ServiceException, match="llm_traces.*disagree"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert backend.deleted == []

    async def test_it_pages_across_two_pages(self, backend, client):
        ids = [row["id"] for row in _rows(0, TRACE_PAGE_SIZE + 5)]
        backend.page["traces"] = lambda query, _: (
            200,
            _page_body(
                _rows(0, TRACE_PAGE_SIZE + 5)[
                    (int(query["page"]) - 1)
                    * TRACE_PAGE_SIZE : int(query["page"])
                    * TRACE_PAGE_SIZE
                ],
                total=len(ids),
            ),
        )

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == len(ids)

        assert [i for chunk in backend.deleted for i in chunk] == ids
        assert max(len(chunk) for chunk in backend.deleted) <= 1000
        assert [q["page"] for q in backend.reads["traces"]] == ["1", "2"]

    async def test_a_trace_found_only_by_its_span_is_deleted(self, backend, client):
        span = {"id": "s1", "trace_id": "t9", "start_time": "2026-11-06T00:00:00Z"}
        backend.page["spans"] = lambda *_: (200, _page_body([span], total=1))

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        assert backend.deleted == [["t9"]]

    async def test_the_requests_as_sent(self, backend, client):
        await _eraser(client).erase_case(CASE, ENTERPRISE)

        filters = [
            {"field": "metadata", "key": key, "operator": "=", "value": value}
            for key, value in (("case_id", CASE), ("enterprise_id", ENTERPRISE))
        ]
        for kind, exclude in (("traces", TRACE_EXCLUDE), ("spans", SPAN_EXCLUDE)):
            (query,) = backend.reads[kind]
            sent = [
                {k: f[k] for k in ("field", "key", "operator", "value")}
                for f in json.loads(query.pop("filters"))
            ]
            assert sent == filters
            assert json.loads(query.pop("exclude")) == exclude
            assert query == {
                "page": "1",
                "size": str(TRACE_PAGE_SIZE),
                "project_name": PROJECT,
            }

    @pytest.mark.parametrize(
        "excluded, admitted",
        [
            (TRACE_EXCLUDE, TraceSearchStreamRequestPublicExcludeItem),
            (SPAN_EXCLUDE, SpanSearchStreamRequestPublicExcludeItem),
        ],
        ids=["traces", "spans"],
    )
    def test_every_field_the_sdk_admits_is_excluded_but_start_time(
        self, excluded, admitted
    ):
        """The SDK's page parse requires ``start_time``: excluding it would fail
        every delete."""
        literal, _ = typing.get_args(admitted)
        assert sorted([*excluded, "start_time"]) == sorted(typing.get_args(literal))

    async def test_no_project_name_sends_the_clients_project(self, backend, client):
        await _eraser(client, project=None).erase_case(CASE, ENTERPRISE)

        assert backend.reads["traces"][0]["project_name"] == PROJECT


def _hold_the_first_replay(monkeypatch, client):
    """Hold the first replay inside the SDK's own mutex until released."""
    database = client._streamer._fallback_replay_manager.database_manager
    replaying, release = threading.Event(), threading.Event()
    real = database._replay_failed_messages_impl

    def held(callback):
        if not replaying.is_set():
            replaying.set()
            release.wait(20)
        return real(callback)

    monkeypatch.setattr(database, "_replay_failed_messages_impl", held)
    return replaying, release


class _WatchedLock:
    """The module's lock, telling a test when a second delete waits on it."""

    def __init__(self):
        self._lock, self.waited_on = threading.Lock(), threading.Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.waited_on.set()
            self._lock.acquire()

    def __exit__(self, *_):
        self._lock.release()


class TestWhatThisPodHolds:
    async def test_a_parked_trace_is_replayed_and_deleted(self, backend, client):
        _lower_the_flag(client)
        await _route()
        assert client.flush(timeout=20) is True
        assert not backend.stored["traces"]  # it is parked

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        assert len(backend.deleted) == 1
        assert backend.remaining(CASE) == [] and backend.stored["traces"] == {}

    async def test_two_deletes_on_one_pod_each_replay_their_own(
        self, backend, client, monkeypatch
    ):
        """The SDK's replay is a non-blocking mutex: the second delete's flush
        would return at once while the first replays its traces."""
        lock = _WatchedLock()
        monkeypatch.setattr(trace_erasure, "_REPLAY_LOCK", lock)
        _lower_the_flag(client)
        for case_id in (CASE, OTHER):
            await _route(case_id=case_id)
        assert client.flush(timeout=20) is True
        replaying, release = _hold_the_first_replay(monkeypatch, client)

        first = asyncio.ensure_future(_eraser(client).erase_case(CASE, ENTERPRISE))
        await _until(replaying.is_set)
        second = asyncio.ensure_future(_eraser(client).erase_case(OTHER, ENTERPRISE))
        try:
            await _until(lambda: lock.waited_on.is_set() or second.done())
        finally:
            release.set()

        assert await asyncio.gather(first, second) == [1, 1]
        client.flush(timeout=20)
        assert backend.remaining(CASE) == [] and backend.remaining(OTHER) == []

    async def test_a_retry_while_a_timed_out_attempt_replays_finds_its_traces(
        self, backend, client, monkeypatch
    ):
        lock = _WatchedLock()
        monkeypatch.setattr(trace_erasure, "_REPLAY_LOCK", lock)
        _lower_the_flag(client)
        await _route()
        assert client.flush(timeout=20) is True
        replaying, release = _hold_the_first_replay(monkeypatch, client)
        monkeypatch.setattr(trace_erasure, "TRACE_ERASE_TIMEOUT_SECONDS", 0.05)

        try:
            attempt = asyncio.ensure_future(
                _eraser(client).erase_case(CASE, ENTERPRISE)
            )
            await _until(replaying.is_set)
            with pytest.raises(ServiceException, match="llm_traces.*timed out"):
                await attempt
            monkeypatch.setattr(trace_erasure, "TRACE_ERASE_TIMEOUT_SECONDS", 60)
            retry = asyncio.ensure_future(_eraser(client).erase_case(CASE, ENTERPRISE))
            await _until(lambda: lock.waited_on.is_set() or retry.done())
        finally:
            release.set()

        assert await retry == 1
        client.flush(timeout=20)
        assert backend.remaining(CASE) == []

    @pytest.mark.slow
    async def test_a_backend_that_is_down_fails_the_replay(self, backend, client):
        """About 11 s: the SDK's own retries of the replayed send."""
        _lower_the_flag(client)
        await _route()
        backend.stop()

        with pytest.raises(ServiceException, match="llm_traces.*unreachable"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

    @pytest.mark.slow
    async def test_a_backend_that_is_down_fails_the_read(self, backend, client):
        """About 11 s: the SDK retries a refused connection twice."""
        _after_the_first_ping(client)
        backend.stop()

        with pytest.raises(
            ServiceException, match=f"llm_traces for case {CASE}: ConnectError$"
        ) as refused:
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert isinstance(refused.value.__cause__, httpx.ConnectError)


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


CONVERSION = "conv_aabb20262575"
OTHER_CONVERSION = "conv_ccdd20262575"


def _content_of(backend, conversion_id):
    """The spans stored for ``conversion_id`` that hold content."""
    with backend._lock:
        return [
            row
            for row in backend.stored["spans"].values()
            if (row.get("metadata") or {}).get("conversion_id") == conversion_id
            and row.get("input")
        ]


class TestAConversion:
    """#2575: an upload's calls carry no case. The conversion's tag finds them
    exactly as the case's does."""

    async def test_its_traces_go_and_another_conversions_stay(self, backend, client):
        for conversion_id in (CONVERSION, OTHER_CONVERSION):
            await _route(case_id=None, conversion_id=conversion_id)
        assert client.flush(timeout=20) is True
        assert len(_content_of(backend, CONVERSION)) == 1

        assert await _eraser(client).erase_conversion(CONVERSION, ENTERPRISE) == 1

        (query,) = backend.reads["spans"]
        assert [(f["key"], f["value"]) for f in json.loads(query["filters"])] == [
            ("conversion_id", CONVERSION),
            ("enterprise_id", ENTERPRISE),
        ]
        assert _content_of(backend, CONVERSION) == []
        assert len(_content_of(backend, OTHER_CONVERSION)) == 1

    async def test_a_failed_call_has_the_conversion_on_the_wire(self, backend, client):
        with pytest.raises(RuntimeError):
            await _route(case_id=None, conversion_id=CONVERSION, fails=True)
        client.flush(timeout=20)

        owner = {"conversion_id": CONVERSION, "enterprise_id": ENTERPRISE}
        (trace,) = [t for t in backend.traces if t.get("error_info")]
        (span,) = [s for s in backend.spans if s.get("error_info")]
        for recorded in (trace, span):
            assert recorded["error_info"]["message"] == BODY
            assert recorded["metadata"] == owner

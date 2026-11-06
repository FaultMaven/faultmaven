"""The case delete purges a case's LLM traces, or fails and says so (#2102).

Driven with a fake client, as opik is not in the dev install. The real SDK runs
in ``test_trace_erasure_loopback_2102.py``, which is where a drift from this fake
is caught. What a backend does with these calls is untested: there is none here.
"""

import json
import logging
import sys
import threading
import types
from types import SimpleNamespace

import httpx
import pytest

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability import trace_erasure, tracing
from faultmaven.infrastructure.observability.trace_erasure import (
    SPAN_EXCLUDE,
    TRACE_EXCLUDE,
    TRACE_PAGE_SIZE,
    LLMTraceEraser,
)

pytestmark = pytest.mark.unit

CASE = "case_aabb20262102"
ENTERPRISE = "00000000-0000-0000-0000-000000000002"
FILTER = f'metadata.case_id = "{CASE}" AND metadata.enterprise_id = "{ENTERPRISE}"'
OPTIONS = {"timeout_in_seconds": 15}


def _ndjson(rows) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class _FakeMonitor:
    def __init__(self, connected: bool):
        self.has_server_connection = connected
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.has_server_connection = True


class _FakeClient:
    """Serves rows a page at a time, as the backend does. ``trace_stream`` and
    ``span_stream`` replace the served page with exact chunks."""

    project_name = "the-clients-project"

    def __init__(
        self, traces=(), spans=(), flushed=True, connected=True, send_fails=False
    ):
        self.trace_rows = [{"id": i} for i in traces]
        self.span_rows = [{"id": f"s{n}", "trace_id": t} for n, t in enumerate(spans)]
        self.trace_stream = self.span_stream = None
        self.flushed, self.send_fails = flushed, send_fails
        self.monitor = _FakeMonitor(connected)
        self._streamer = SimpleNamespace(
            _fallback_replay_manager=SimpleNamespace(_monitor=self.monitor)
        )
        self.flushes: list = []
        self.trace_searches: list = []
        self.span_searches: list = []
        self.deleted: list = []
        self.delete_options: list = []
        self.rest_client = SimpleNamespace(
            traces=SimpleNamespace(
                search_traces=self.search_traces, delete_traces=self.delete_traces
            ),
            spans=SimpleNamespace(search_spans=self.search_spans),
        )

    def flush(self, timeout=None):
        self.flushes.append(
            SimpleNamespace(
                timeout=timeout,
                connected=self.monitor.has_server_connection,
                thread=threading.current_thread().name,
            )
        )
        if self.send_fails:
            self.monitor.has_server_connection = False
        return self.flushed

    @staticmethod
    def _page(rows, kwargs):
        cursor = kwargs["last_retrieved_id"]
        start = (
            1 + next(i for i, r in enumerate(rows) if r["id"] == cursor)
            if cursor
            else 0
        )
        return iter([_ndjson(rows[start : start + kwargs["limit"]])])

    def search_traces(self, **kwargs):
        self.trace_searches.append(kwargs)
        return (
            iter(self.trace_stream)
            if self.trace_stream
            else self._page(self.trace_rows, kwargs)
        )

    def search_spans(self, **kwargs):
        self.span_searches.append(kwargs)
        return (
            iter(self.span_stream)
            if self.span_stream
            else self._page(self.span_rows, kwargs)
        )

    def delete_traces(self, *, ids, request_options=None):
        self.deleted.append(list(ids))
        self.delete_options.append(request_options)


def _eraser(client, project="faultmaven") -> LLMTraceEraser:
    return LLMTraceEraser(project, client_factory=lambda: client)


def _no_client():
    raise AssertionError("no client may be built")


@pytest.fixture(autouse=True)
def backend_configured(monkeypatch):
    """``init_opik_tracing`` reached a backend. The SDK's filter parser is replaced,
    as opik is absent from the dev venv; the loopback file pins what it sends."""
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(tracing, "_tracing_configured", True)
    monkeypatch.setattr(
        trace_erasure, "_parsed_filters", lambda text, entity: (entity, text)
    )


class TestTheErase:
    async def test_it_deletes_the_union_of_what_both_searches_found(self, caplog):
        client = _FakeClient(traces=["t1", "t2"], spans=["t2", "t3", "t3"])

        with caplog.at_level(logging.INFO, logger=trace_erasure.__name__):
            assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 3

        assert client.deleted == [["t1", "t2", "t3"]]
        assert client.delete_options == [OPTIONS]
        assert caplog.text.count(f"Deleted 3 traces of case {CASE}") == 1

    async def test_a_span_whose_trace_lost_its_tag_is_still_found(self):
        client = _FakeClient(spans=["t9"])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        assert client.deleted == [["t9"]]

    async def test_the_searches_filter_on_both_ids_and_read_ids_alone(self):
        client = _FakeClient(traces=["t1"])

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        shared = {
            "project_name": "faultmaven",
            "limit": TRACE_PAGE_SIZE,
            "last_retrieved_id": None,
            "request_options": OPTIONS,
        }
        assert client.trace_searches == [
            {**shared, "filters": ("traces", FILTER), "exclude": TRACE_EXCLUDE}
        ]
        assert client.span_searches == [
            {**shared, "filters": ("spans", FILTER), "exclude": SPAN_EXCLUDE}
        ]

    async def test_no_project_name_reads_the_clients_project(self):
        """The raw REST call would send a null: the SDK's resolver is not on it."""
        client = _FakeClient()

        await _eraser(client, project=None).erase_case(CASE, ENTERPRISE)

        assert client.trace_searches[0]["project_name"] == "the-clients-project"
        assert client.span_searches[0]["project_name"] == "the-clients-project"

    async def test_deletes_are_sent_in_chunks_of_a_thousand(self):
        client = _FakeClient(traces=[f"t{i}" for i in range(1500)])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1500

        assert [len(chunk) for chunk in client.deleted] == [1000, 500]

    async def test_it_pages_until_a_short_page(self):
        ids = [f"t{i}" for i in range(TRACE_PAGE_SIZE + 5)]
        client = _FakeClient(traces=ids)

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == len(ids)

        assert [s["last_retrieved_id"] for s in client.trace_searches] == [
            None,
            ids[TRACE_PAGE_SIZE - 1],
        ]

    async def test_spans_page_by_their_own_id_not_their_trace_id(self):
        ids = [f"t{i}" for i in range(TRACE_PAGE_SIZE + 5)]
        client = _FakeClient(spans=ids)

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == len(ids)

        assert [s["last_retrieved_id"] for s in client.span_searches] == [
            None,
            f"s{TRACE_PAGE_SIZE - 1}",
        ]

    async def test_an_exactly_full_page_is_not_taken_for_the_last(self):
        ids = [f"t{i}" for i in range(TRACE_PAGE_SIZE)]
        client = _FakeClient(traces=ids)

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == len(ids)

        assert len(client.trace_searches) == 2

    async def test_nothing_found_deletes_nothing_and_searches_once_each(self):
        client = _FakeClient()

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 0

        assert client.deleted == []
        assert (len(client.trace_searches), len(client.span_searches)) == (1, 1)

    async def test_nothing_searches_again_after_the_delete(self):
        """A read only to count and log could fail a delete that succeeded."""
        client = _FakeClient(traces=["t1"], spans=["t1"])

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert (len(client.trace_searches), len(client.span_searches)) == (1, 1)

    async def test_a_row_split_across_chunks_is_read_whole(self):
        client = _FakeClient()
        client.trace_stream = [
            b'{"id":"t',
            b'1"}\r\n{"id":"t2"}',
            b"\n",
            b'{"id":"t3"}',
        ]

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 3

        assert client.deleted == [["t1", "t2", "t3"]]

    async def test_it_runs_on_its_own_pool(self):
        """A hang must not hold a slot of the pool the PII sanitizer uses."""
        client = _FakeClient()

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.flushes[0].thread.startswith("llm-trace-erase")


class TestItFailsClosed:
    @pytest.mark.parametrize(
        "stream, line",
        [
            ("trace_stream", b'{"code":500,"message":"Streaming timed out"}\n'),
            ("span_stream", b'{"code":500,"message":"Streaming timed out"}\n'),
            ("trace_stream", b'{"id":"t1"}\n{"id":"t2"'),
            ("trace_stream", b"not json\n"),
            ("trace_stream", b'["t1"]\n'),
            ("trace_stream", b'{"id":null}\n'),
            ("span_stream", b'{"id":"s1"}\n'),
            ("span_stream", b'{"trace_id":"t1"}\n'),
        ],
        ids=[
            "error-in-traces",
            "error-in-spans",
            "truncated",
            "not-json",
            "not-a-dict",
            "null-id",
            "span-with-no-trace-id",
            "span-with-no-cursor",
        ],
    )
    async def test_a_line_that_is_not_a_row_fails_with_nothing_deleted(
        self, stream, line
    ):
        client = _FakeClient(traces=["t1"])
        setattr(client, stream, [line])

        with pytest.raises(ServiceException, match="llm_traces.*not a row"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.deleted == []

    @pytest.mark.parametrize("call", ["search_traces", "search_spans", "delete_traces"])
    async def test_an_unreachable_backend_propagates(self, call):
        client = _FakeClient(traces=["t1"])

        def unreachable(**_):
            raise httpx.ConnectError("[Errno 111] Connection refused")

        rest = client.rest_client
        rest.traces.search_traces = (
            unreachable if call == "search_traces" else rest.traces.search_traces
        )
        rest.spans.search_spans = (
            unreachable if call == "search_spans" else rest.spans.search_spans
        )
        rest.traces.delete_traces = (
            unreachable if call == "delete_traces" else rest.traces.delete_traces
        )

        with pytest.raises(httpx.ConnectError):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

    async def test_an_erase_that_outlasts_its_budget_refuses(self, monkeypatch):
        release = threading.Event()
        client = _FakeClient()
        client.rest_client.traces.search_traces = lambda **_: release.wait() and []
        monkeypatch.setattr(trace_erasure, "TRACE_ERASE_TIMEOUT_SECONDS", 0.01)

        try:
            with pytest.raises(ServiceException, match="llm_traces.*timed out"):
                await _eraser(client).erase_case(CASE, ENTERPRISE)
        finally:
            release.set()

        assert client.deleted == []

    def test_a_worker_past_its_deadline_stops_reading(self, monkeypatch):
        """Nothing else would stop a worker that outlived the wait, on a backend
        that keeps answering full pages. Called directly: the wait is not what
        is under test."""
        client = _FakeClient()
        served = []

        def endless(**kwargs):
            served.append(kwargs)
            assert len(served) < 3, "the worker kept paging past its deadline"
            return iter([_ndjson({"id": f"t{i}"} for i in range(kwargs["limit"]))])

        client.rest_client.traces.search_traces = endless
        monkeypatch.setattr(trace_erasure, "TRACE_ERASE_TIMEOUT_SECONDS", -1)

        with pytest.raises(ServiceException, match="llm_traces.*time limit"):
            _eraser(client)._erase(CASE, ENTERPRISE)

        assert client.deleted == []

    @pytest.mark.parametrize(
        "case_id, enterprise_id",
        [
            ('case" OR metadata.x = "y', ENTERPRISE),
            (CASE, 'ent" OR metadata.x = "y'),
            ("case_a\n", ENTERPRISE),
            ("", ENTERPRISE),
        ],
        ids=["case-quote", "enterprise-quote", "trailing-newline", "empty"],
    )
    async def test_an_id_outside_the_alphabet_is_refused_before_any_call(
        self, case_id, enterprise_id
    ):
        eraser = LLMTraceEraser("faultmaven", client_factory=_no_client)

        with pytest.raises(ServiceException, match="not a valid id"):
            await eraser.erase_case(case_id, enterprise_id)


class TestWhatThisPodHolds:
    """After a connection failure the SDK parks traces, and ``flush`` replays them
    only while its monitor's flag is True (#2573)."""

    async def test_a_flush_that_does_not_finish_refuses_before_the_search(self):
        client = _FakeClient(traces=["t1"], flushed=False)

        with pytest.raises(ServiceException, match="llm_traces.*did not reach"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.trace_searches == [] and client.deleted == []

    async def test_a_lowered_flag_is_reset_so_the_flush_replays(self):
        client = _FakeClient(connected=False)

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.flushes[0].connected is True
        assert client.flushes[0].timeout == 20
        assert client.monitor.resets == 1

    async def test_a_raised_flag_is_left_alone(self):
        client = _FakeClient()

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.monitor.resets == 0

    async def test_a_send_that_fails_during_the_flush_refuses_before_the_search(self):
        client = _FakeClient(traces=["t1"], connected=False, send_fails=True)

        with pytest.raises(ServiceException, match="llm_traces.*unreachable"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.trace_searches == [] and client.deleted == []

    @pytest.mark.parametrize(
        "changed",
        [
            {"_streamer": SimpleNamespace()},
            {
                "_streamer": SimpleNamespace(
                    _fallback_replay_manager=SimpleNamespace(
                        _monitor=SimpleNamespace(has_server_connection=False)
                    )
                )
            },
        ],
        ids=["no-monitor", "no-reset"],
    )
    async def test_an_sdk_that_changed_fails_closed_naming_the_store(self, changed):
        client = _FakeClient(traces=["t1"])
        vars(client).update(changed)

        with pytest.raises(ServiceException, match="llm_traces.*SDK"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.deleted == []


class TestTheGateReadsTheRecordedOutcome:
    async def test_a_backend_that_did_not_initialize_refuses_with_no_client(
        self, monkeypatch
    ):
        monkeypatch.setattr(tracing, "_tracing_configured", False)
        eraser = LLMTraceEraser("faultmaven", client_factory=_no_client)

        with pytest.raises(ServiceException, match="did not initialize"):
            await eraser.erase_case(CASE, ENTERPRISE)

    async def test_the_sdks_switch_off_still_purges(self, monkeypatch):
        """``OPIK_TRACK_DISABLE=true`` stops new spans; the old traces remain."""
        fake = types.ModuleType("opik")
        fake.is_tracing_active = lambda: False
        monkeypatch.setitem(sys.modules, "opik", fake)
        assert tracing.tracing_is_effective() is False
        client = _FakeClient(traces=["t1"])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        assert client.deleted == [["t1"]]


async def test_the_default_client_is_the_one_the_decorator_shares(monkeypatch):
    client = _FakeClient(traces=["t1"])
    module = types.ModuleType("opik.api_objects.opik_client")
    module.get_client_cached = lambda: client
    for name, stand_in in (
        ("opik", types.ModuleType("opik")),
        ("opik.api_objects", types.ModuleType("opik.api_objects")),
        ("opik.api_objects.opik_client", module),
    ):
        monkeypatch.setitem(sys.modules, name, stand_in)

    assert await LLMTraceEraser("faultmaven").erase_case(CASE, ENTERPRISE) == 1

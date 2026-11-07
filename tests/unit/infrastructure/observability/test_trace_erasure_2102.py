"""The case delete purges a case's LLM traces, or fails and says so (#2102); the
draft and document deletes purge a conversion's the same way (#2575,
``TestAConversionsTraces``).

Driven with a fake client, as opik is not in the dev install. The real SDK runs
in ``test_trace_erasure_loopback_2102.py``, which is where a drift from this fake
is caught. What a backend does with these calls is untested: there is none here.
"""

import asyncio
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
    UnavailableTraceEraser,
)

pytestmark = pytest.mark.unit

CASE = "case_aabb20262102"
ENTERPRISE = "00000000-0000-0000-0000-000000000002"
FILTER = f'metadata.case_id = "{CASE}" AND metadata.enterprise_id = "{ENTERPRISE}"'
OPTIONS = {"timeout_in_seconds": 15}


_COUNTED = object()


def _page(rows, total=_COUNTED, **_):
    """A page as the backend builds it: counted first, then read."""
    rows = list(rows)
    return SimpleNamespace(
        size=len(rows), total=len(rows) if total is _COUNTED else total, content=rows
    )


class _FakeMonitor:
    def __init__(self, connected: bool):
        self.has_server_connection = connected
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.has_server_connection = True


class _FakeClient:
    """Serves the paged list endpoints over its rows. ``trace_page`` and
    ``span_page`` replace what a page request answers."""

    project_name = "the-clients-project"

    def __init__(
        self, traces=(), spans=(), flushed=True, connected=True, send_fails=False
    ):
        self.trace_rows = [SimpleNamespace(id=i) for i in traces]
        self.span_rows = [
            SimpleNamespace(id=f"s{n}", trace_id=t) for n, t in enumerate(spans)
        ]
        self.trace_page = self.span_page = None
        self.flushed, self.send_fails = flushed, send_fails
        self.monitor = _FakeMonitor(connected)
        self._streamer = SimpleNamespace(
            _fallback_replay_manager=SimpleNamespace(_monitor=self.monitor)
        )
        self.flushes: list = []
        self.trace_reads: list = []
        self.span_reads: list = []
        self.deleted: list = []
        self.delete_options: list = []
        self.rest_client = SimpleNamespace(
            traces=SimpleNamespace(
                get_traces_by_project=self.get_traces_by_project,
                delete_traces=self.delete_traces,
            ),
            spans=SimpleNamespace(get_spans_by_project=self.get_spans_by_project),
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
    def _serve(rows, kwargs):
        start = (kwargs["page"] - 1) * kwargs["size"]
        return _page(rows[start : start + kwargs["size"]], total=len(rows))

    def get_traces_by_project(self, **kwargs):
        self.trace_reads.append(kwargs)
        if self.trace_page:
            return self.trace_page(**kwargs)
        return self._serve(self.trace_rows, kwargs)

    def get_spans_by_project(self, **kwargs):
        self.span_reads.append(kwargs)
        if self.span_page:
            return self.span_page(**kwargs)
        return self._serve(self.span_rows, kwargs)

    def delete_traces(self, *, ids, request_options=None):
        self.deleted.append(list(ids))
        self.delete_options.append(request_options)


def _eraser(client, project="faultmaven") -> LLMTraceEraser:
    return LLMTraceEraser(project, client_factory=lambda: client)


def _no_client():
    raise AssertionError("no client may be built")


async def _until(condition):
    """Yield to the loop until ``condition`` holds. A hang fails, never passes."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


class _WatchedLock:
    """The module's lock, telling a test when a second delete waits on it."""

    def __init__(self, arrived: threading.Event):
        self._lock, self._arrived = threading.Lock(), arrived

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._arrived.set()
            self._lock.acquire()

    def __exit__(self, *_):
        self._lock.release()


@pytest.fixture(autouse=True)
def backend_configured(monkeypatch):
    """``init_opik_tracing`` reached a backend. The SDK's filter parser is replaced,
    as opik is absent from the dev venv; the loopback file pins what it sends."""
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(tracing, "_tracing_configured", True)
    monkeypatch.setattr(
        trace_erasure, "_parsed_filters", lambda text, entity: f"{entity}:{text}"
    )


class TestTheErase:
    async def test_it_deletes_the_union_of_what_both_reads_found(self, caplog):
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

    async def test_the_reads_filter_on_both_ids_and_exclude_the_content(self):
        client = _FakeClient(traces=["t1"])

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        shared = {
            "project_name": "faultmaven",
            "page": 1,
            "size": TRACE_PAGE_SIZE,
            "request_options": OPTIONS,
        }
        assert client.trace_reads == [
            {
                **shared,
                "filters": f"traces:{FILTER}",
                "exclude": json.dumps(TRACE_EXCLUDE),
            }
        ]
        assert client.span_reads == [
            {
                **shared,
                "filters": f"spans:{FILTER}",
                "exclude": json.dumps(SPAN_EXCLUDE),
            }
        ]

    async def test_no_project_name_reads_the_clients_project(self):
        """The raw REST call would send a null: the SDK's resolver is not on it."""
        client = _FakeClient()

        await _eraser(client, project=None).erase_case(CASE, ENTERPRISE)

        assert client.trace_reads[0]["project_name"] == "the-clients-project"
        assert client.span_reads[0]["project_name"] == "the-clients-project"

    async def test_deletes_are_sent_in_chunks_of_a_thousand(self):
        client = _FakeClient(traces=[f"t{i}" for i in range(1500)])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1500

        assert [len(chunk) for chunk in client.deleted] == [1000, 500]

    @pytest.mark.parametrize("extra", [5, 0], ids=["past-a-page", "an-exact-page"])
    async def test_it_reads_every_page_until_the_total(self, extra):
        ids = [f"t{i}" for i in range(TRACE_PAGE_SIZE + extra)]
        spans = [f"t{i}" for i in range(TRACE_PAGE_SIZE + extra)]
        client = _FakeClient(traces=ids, spans=spans)

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == len(ids)

        pages = [1, 2] if extra else [1]
        assert [r["page"] for r in client.trace_reads] == pages
        assert [r["page"] for r in client.span_reads] == pages

    async def test_nothing_found_deletes_nothing_and_reads_once_each(self):
        client = _FakeClient()

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 0

        assert client.deleted == []
        assert (len(client.trace_reads), len(client.span_reads)) == (1, 1)

    async def test_nothing_reads_again_after_the_delete(self):
        """A read only to count and log could fail a delete that succeeded."""
        client = _FakeClient(traces=["t1"], spans=["t1"])

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert (len(client.trace_reads), len(client.span_reads)) == (1, 1)

    async def test_it_runs_on_its_own_pool(self):
        """A hang must not hold a slot of the pool the PII sanitizer uses."""
        client = _FakeClient()

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.flushes[0].thread.startswith("llm-trace-erase")


def _row(i):
    return SimpleNamespace(id=f"t{i}")


class TestItFailsClosed:
    @pytest.mark.parametrize(
        "kind, page",
        [
            ("trace_page", lambda **_: _page([_row(1)], total=None)),
            ("trace_page", lambda **_: _page([_row(1), _row(2)], total=3)),
            ("trace_page", lambda **_: _page([_row(1), _row(2)], total=1)),
            ("trace_page", lambda **_: _page([], total=1)),
            (
                "trace_page",
                lambda page, **_: _page(
                    map(_row, range(TRACE_PAGE_SIZE - 1)), total=TRACE_PAGE_SIZE + 5
                ),
            ),
            (
                "trace_page",
                lambda page, **_: _page(
                    map(_row, range(TRACE_PAGE_SIZE if page == 1 else 5)),
                    total=TRACE_PAGE_SIZE + page * 5,
                ),
            ),
            ("trace_page", lambda **_: _page([SimpleNamespace(id=None)])),
            ("span_page", lambda **_: _page([SimpleNamespace(id="s", trace_id="")])),
            ("span_page", lambda **_: _page([SimpleNamespace(trace_id="t1")], total=2)),
        ],
        ids=[
            "no-total",
            "total-above-the-rows",
            "rows-above-the-total",
            "an-empty-page-before-the-total",
            "a-short-page-before-the-total",
            "a-total-that-moves",
            "a-row-without-its-id",
            "a-span-without-its-trace-id",
            "spans-short-of-their-total",
        ],
    )
    async def test_a_page_that_disagrees_with_its_total_deletes_nothing(
        self, kind, page
    ):
        client = _FakeClient(traces=["t1"])
        setattr(client, kind, page)

        with pytest.raises(ServiceException, match="llm_traces.*disagree"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.deleted == []

    @pytest.mark.parametrize(
        "call", ["get_traces_by_project", "get_spans_by_project", "delete_traces"]
    )
    async def test_an_unreachable_backend_refuses_naming_the_error_class(self, call):
        """The SDK's own error, wrapped once here for every caller: its text
        can carry the backend's response body (#2575)."""
        client = _FakeClient(traces=["t1"])

        def unreachable(**_):
            raise httpx.ConnectError("[Errno 111] Connection refused")

        rest = client.rest_client
        for api in (rest.traces, rest.spans):
            if hasattr(api, call):
                setattr(api, call, unreachable)

        with pytest.raises(
            ServiceException, match=f"llm_traces for case {CASE}: ConnectError$"
        ) as refused:
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert isinstance(refused.value.__cause__, httpx.ConnectError)

    async def test_an_erase_that_outlasts_its_budget_refuses(self, monkeypatch):
        release = threading.Event()
        client = _FakeClient()
        client.trace_page = lambda **_: release.wait() and _page([])
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

        def endless(page, **_):
            assert page < 3, "the worker kept paging past its deadline"
            return _page(map(_row, range(TRACE_PAGE_SIZE)), total=10**9)

        client.trace_page = endless
        monkeypatch.setattr(trace_erasure, "TRACE_ERASE_TIMEOUT_SECONDS", -1)

        with pytest.raises(ServiceException, match="llm_traces.*time limit"):
            _eraser(client)._erase(CASE, ENTERPRISE, "case")

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

    async def test_a_flush_that_does_not_finish_refuses_before_the_read(self):
        client = _FakeClient(traces=["t1"], flushed=False)

        with pytest.raises(ServiceException, match="llm_traces.*did not reach"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.trace_reads == [] and client.deleted == []

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

    async def test_a_send_that_fails_during_the_flush_refuses_before_the_read(self):
        client = _FakeClient(traces=["t1"], connected=False, send_fails=True)

        with pytest.raises(ServiceException, match="llm_traces.*unreachable"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.trace_reads == [] and client.deleted == []

    async def test_two_deletes_on_one_pod_deliver_one_at_a_time(self, monkeypatch):
        """The SDK's replay is a non-blocking mutex: a flush that overlaps another
        returns at once, with its case's traces still parked."""
        arrived, release = threading.Event(), threading.Event()
        monkeypatch.setattr(trace_erasure, "_REPLAY_LOCK", _WatchedLock(arrived))
        in_flush: list = []
        overlaps: list = []
        clients = [_FakeClient(traces=["t1"]), _FakeClient(traces=["t2"])]
        for client in clients:

            def flush(timeout=None, real=client.flush):
                in_flush.append(1)
                overlaps.append(len(in_flush))
                if len(in_flush) > 1:
                    arrived.set()
                release.wait(10)
                in_flush.pop()
                return real(timeout)

            client.flush = flush
        first = asyncio.ensure_future(_eraser(clients[0]).erase_case(CASE, ENTERPRISE))
        await _until(lambda: overlaps or first.done())
        assert overlaps, f"the first delete ended before its flush: {first!r}"
        second = asyncio.ensure_future(_eraser(clients[1]).erase_case(CASE, ENTERPRISE))

        try:
            assert await asyncio.to_thread(arrived.wait, 10)
        finally:
            release.set()

        assert await asyncio.gather(first, second) == [1, 1]
        assert max(overlaps) == 1

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


CONVERSION = "conv_aabb20262575"


class TestAConversionsTraces:
    """#2575: the case's erase, found by the conversion's tag instead."""

    async def test_its_requests_are_the_case_erases_but_for_the_owner_key(self, caplog):
        by_case = _FakeClient(traces=["t1"], spans=["t1", "t2"])
        by_conversion = _FakeClient(traces=["t1"], spans=["t1", "t2"])

        await _eraser(by_case).erase_case(CONVERSION, ENTERPRISE)
        with caplog.at_level(logging.INFO, logger=trace_erasure.__name__):
            assert (
                await _eraser(by_conversion).erase_conversion(CONVERSION, ENTERPRISE)
                == 2
            )

        for reads in ("trace_reads", "span_reads"):
            assert getattr(by_conversion, reads) == [
                {
                    **read,
                    "filters": read["filters"].replace(
                        "metadata.case_id", "metadata.conversion_id"
                    ),
                }
                for read in getattr(by_case, reads)
            ]
        assert 'metadata.conversion_id = "' in by_conversion.trace_reads[0]["filters"]
        assert by_conversion.deleted == by_case.deleted == [["t1", "t2"]]
        assert f"Deleted 2 traces of conversion {CONVERSION}" in caplog.text

    @pytest.mark.parametrize(
        "conversion_id",
        ['conv" OR metadata.x = "y', "conv_a\n", ""],
        ids=["quote", "trailing-newline", "empty"],
    )
    async def test_an_id_outside_the_alphabet_is_refused_before_any_call(
        self, conversion_id
    ):
        eraser = LLMTraceEraser("faultmaven", client_factory=_no_client)

        with pytest.raises(ServiceException, match="not a valid id"):
            await eraser.erase_conversion(conversion_id, ENTERPRISE)

    async def test_a_refusal_names_the_conversion_and_deletes_nothing(self):
        client = _FakeClient(traces=["t1"], flushed=False)

        with pytest.raises(
            ServiceException,
            match=f"llm_traces for conversion {CONVERSION}: queued traces",
        ):
            await _eraser(client).erase_conversion(CONVERSION, ENTERPRISE)

        assert client.trace_reads == [] and client.deleted == []

    async def test_an_absent_sdk_refuses_naming_the_conversion(self):
        eraser = UnavailableTraceEraser("the Opik SDK is not installed")

        with pytest.raises(
            ServiceException, match=f"llm_traces for conversion {CONVERSION}: the"
        ):
            await eraser.erase_conversion(CONVERSION, ENTERPRISE)

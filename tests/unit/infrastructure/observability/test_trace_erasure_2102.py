"""The case delete purges a case's LLM traces, or fails and says so (#2102).

Driven with a fake client: a real ``opik.Opik()`` starts network threads. The
last test reads the pinned SDK where it is installed, to catch a drift from the
fake. What the backend does with these calls is untested: there is none here.
"""

import inspect
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability import trace_erasure, tracing
from faultmaven.infrastructure.observability.trace_erasure import (
    TRACE_SEARCH_MAX_RESULTS,
    LLMTraceEraser,
)

pytestmark = pytest.mark.unit

CASE = "case_aabb20262102"
ENTERPRISE = "00000000-0000-0000-0000-000000000002"


class _FakeClient:
    """Answers the first search with ``found`` and any later one with ``after``."""

    def __init__(self, found=(), after=(), flushed=True):
        self.found, self.after, self.flushed = list(found), list(after), flushed
        self.flush_timeouts: list = []
        self.searches: list[dict] = []
        self.deleted: list[list[str]] = []
        self.request_options: list[dict] = []
        self.rest_client = SimpleNamespace(
            traces=SimpleNamespace(delete_traces=self._delete_traces)
        )

    def flush(self, timeout=None):
        self.flush_timeouts.append(timeout)
        return self.flushed

    def search_traces(self, **kwargs):
        self.searches.append(kwargs)
        ids = self.found if len(self.searches) == 1 else self.after
        return [SimpleNamespace(id=i) for i in ids]

    def _delete_traces(self, *, ids, request_options=None):
        self.deleted.append(list(ids))
        self.request_options.append(request_options)


def _eraser(client) -> LLMTraceEraser:
    return LLMTraceEraser("faultmaven", client_factory=lambda: client)


def _no_client():
    raise AssertionError("no client may be built")


@pytest.fixture(autouse=True)
def backend_configured(monkeypatch):
    """``init_opik_tracing`` reached a backend; opik is absent from the dev venv."""
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(tracing, "_tracing_configured", True)


class TestTheErase:
    async def test_it_deletes_what_the_search_found_and_counts_it(self):
        client = _FakeClient(found=["t1", "t2", "t1", "t3", "t2"])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 3

        assert client.deleted == [["t1", "t2", "t3"]]
        assert client.flush_timeouts == [20]
        assert client.request_options == [{"timeout_in_seconds": 15}]

    async def test_the_search_filters_on_both_ids_and_leaves_the_content_behind(self):
        client = _FakeClient(found=["t1"])

        await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.searches[0] == {
            "project_name": "faultmaven",
            "filter_string": (
                f'metadata.case_id = "{CASE}" '
                f'AND metadata.enterprise_id = "{ENTERPRISE}"'
            ),
            "max_results": TRACE_SEARCH_MAX_RESULTS,
            "exclude": ["input", "output"],
        }

    async def test_deletes_are_sent_in_chunks_of_a_thousand(self):
        client = _FakeClient(found=[f"t{i}" for i in range(2500)])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 2500

        assert [len(chunk) for chunk in client.deleted] == [1000, 1000, 500]

    async def test_nothing_found_deletes_nothing(self):
        client = _FakeClient()

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 0

        assert client.deleted == []
        assert len(client.searches) == 1

    async def test_traces_still_visible_are_counted_once_and_never_chased(self):
        """The backend deletes asynchronously: a loop could re-find them forever."""
        client = _FakeClient(found=["t1", "t2"], after=["t1"])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 2

        assert len(client.searches) == 2
        assert client.deleted == [["t1", "t2"]]


class TestItFailsClosed:
    async def test_a_flush_that_does_not_finish_refuses_before_the_search(self):
        """A trace still queued would land after the search and survive it."""
        client = _FakeClient(found=["t1"], flushed=False)

        with pytest.raises(ServiceException, match="did not reach the backend"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert client.searches == [] and client.deleted == []

    @pytest.mark.parametrize("failing", ["search_traces", "_delete_traces"])
    async def test_an_unreachable_backend_propagates(self, failing):
        client = _FakeClient(found=["t1"])

        def unreachable(**_):
            raise ConnectionError("backend down")

        setattr(client, failing, unreachable)
        client.rest_client.traces.delete_traces = client._delete_traces

        with pytest.raises(ConnectionError):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

    async def test_a_full_result_deletes_it_all_and_then_refuses(self):
        """The search stops at its limit, so more may remain. The row stays and
        a retry takes the next ones."""
        ids = [f"t{i}" for i in range(TRACE_SEARCH_MAX_RESULTS)]
        client = _FakeClient(found=ids)

        with pytest.raises(ServiceException, match="more may remain"):
            await _eraser(client).erase_case(CASE, ENTERPRISE)

        assert [i for chunk in client.deleted for i in chunk] == ids

    async def test_an_erase_that_outlasts_its_budget_refuses(self, monkeypatch):
        release = threading.Event()
        client = _FakeClient()
        client.search_traces = lambda **_: release.wait() and []
        monkeypatch.setattr(trace_erasure, "TRACE_ERASE_TIMEOUT_SECONDS", 0.01)

        try:
            with pytest.raises(ServiceException, match="timed out"):
                await _eraser(client).erase_case(CASE, ENTERPRISE)
        finally:
            release.set()

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
        client = _FakeClient(found=["t1"])

        assert await _eraser(client).erase_case(CASE, ENTERPRISE) == 1

        assert client.deleted == [["t1"]]


class TestTheDefaultClient:
    async def test_it_is_the_client_the_decorator_shares(self, monkeypatch):
        client = _FakeClient(found=["t1"])
        module = types.ModuleType("opik.api_objects.opik_client")
        module.get_client_cached = lambda: client
        for name, stand_in in (
            ("opik", types.ModuleType("opik")),
            ("opik.api_objects", types.ModuleType("opik.api_objects")),
            ("opik.api_objects.opik_client", module),
        ):
            monkeypatch.setitem(sys.modules, name, stand_in)

        assert await LLMTraceEraser("faultmaven").erase_case(CASE, ENTERPRISE) == 1

    def test_the_pinned_sdk_has_what_the_eraser_calls(self):
        """Read, never constructed. Skipped where the SDK is not installed."""
        opik_client = pytest.importorskip("opik.api_objects.opik_client")
        from opik.api_objects.opik_query_language import OpikQueryLanguage
        from opik.rest_api.traces.client import TracesClient

        assert callable(opik_client.get_client_cached)
        assert "timeout" in inspect.signature(opik_client.Opik.flush).parameters
        search = inspect.signature(opik_client.Opik.search_traces).parameters
        assert {"project_name", "filter_string", "max_results", "exclude"} <= set(
            search
        )
        delete = inspect.signature(TracesClient.delete_traces).parameters
        assert {"ids", "request_options"} <= set(delete)
        OpikQueryLanguage(
            f'metadata.case_id = "{CASE}" AND metadata.enterprise_id = "{ENTERPRISE}"'
        )

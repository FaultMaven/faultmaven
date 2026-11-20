"""The operator's purge of the traces that started before a cutoff (#2700).

Driven with a fake backend, as opik is not in the dev install: it filters by
start time, deletes a trace at once and its spans only when the purge waits, as
the backend's listener does after answering. The real SDK on a loopback port is
in ``test_trace_erasure_loopback_2102.py``, which pins the filter on the wire.
"""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability import opik_sdk, trace_erasure
from faultmaven.infrastructure.observability.trace_erasure import (
    PURGE_SETTLE_POLLS,
    SPAN_EXCLUDE,
    TRACE_EXCLUDE,
    TRACE_PAGE_SIZE,
    TraceCount,
    purge_traces_before,
)

pytestmark = pytest.mark.unit

PROJECT = "faultmaven"
CUTOFF = datetime(2026, 11, 18, 5, 0, tzinfo=timezone(timedelta(hours=5)))
STAMP = "2026-11-18T00:00:00.000000Z"
OLD, NEW = "2026-11-01T00:00:00.000000Z", "2026-11-19T00:00:00.000000Z"


class _Status(Exception):
    """An SDK error carrying the backend's status, as ``ApiError`` does."""

    def __init__(self, status_code):
        self.status_code = status_code


class _Backend:
    """Traces ``{id: start}``; spans ``(trace_id, start)``. A deleted trace's
    spans go at the next ``sleep``, unless its trace id is ``lost``: then that
    delete's span cascade never runs, and only a second delete clears them. The
    workspace lists a project whose name contains ``PROJECT``, and with
    ``project`` the project itself."""

    def __init__(self, traces=(), spans=(), errors=0, lost=(), project=True):
        self.traces = dict(traces)
        self.spans = list(spans)
        self.errors, self.lost, self.project = errors, set(lost), project
        self.pending: set = set()
        self.reads: list = []
        self.deleted: list = []
        self.delete_options: list = []
        self.sleeps = 0
        listed = [SimpleNamespace(id="p-staging", name=f"{PROJECT}-staging")]
        if project:
            listed.append(SimpleNamespace(id="p-faultmaven", name=PROJECT))
        self.project_name = "the-clients-project"
        self.rest_client = SimpleNamespace(
            traces=SimpleNamespace(
                get_traces_by_project=lambda **kw: self._page("traces", kw),
                delete_traces=self._delete,
            ),
            spans=SimpleNamespace(
                get_spans_by_project=lambda **kw: self._page("spans", kw)
            ),
            projects=SimpleNamespace(
                find_projects=lambda **_: SimpleNamespace(
                    total=len(listed), content=listed
                )
            ),
        )

    def flush(self, **_):
        raise AssertionError("a command has no queue to deliver")

    def _page(self, entity, kw):
        self.reads.append({"entity": entity, **kw})
        text = kw["filters"].split(":", 1)[1]
        if text == "error_info is_not_empty":
            rows = [
                SimpleNamespace(id=f"e{n}", trace_id=f"e{n}")
                for n in range(self.errors)
            ]
        else:
            (cutoff,) = re.fullmatch(r'start_time < "(.+)"', text).groups()
            if entity == "traces":
                rows = [
                    SimpleNamespace(id=i) for i, s in self.traces.items() if s < cutoff
                ]
            else:
                rows = [
                    SimpleNamespace(trace_id=t) for t, s in self.spans if s < cutoff
                ]
        start = (kw["page"] - 1) * kw["size"]
        return SimpleNamespace(
            total=len(rows), content=rows[start : start + kw["size"]]
        )

    def _delete(self, *, ids, request_options):
        self.deleted.append(list(ids))
        self.delete_options.append(request_options)
        for trace_id in ids:
            self.traces.pop(trace_id, None)
            if trace_id in self.lost:
                self.lost.discard(trace_id)
            else:
                self.pending.add(trace_id)

    def sleep(self, _seconds):
        self.sleeps += 1
        self.spans = [(t, s) for t, s in self.spans if t not in self.pending]
        self.pending.clear()


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """The SDK's filter parser, as opik is absent here."""
    monkeypatch.setattr(
        opik_sdk, "parsed_filters", lambda text, entity: f"{entity}:{text}"
    )


def _purge(backend, delete=True, cutoff=CUTOFF):
    return purge_traces_before(
        cutoff,
        delete=delete,
        project_name=PROJECT,
        client_factory=lambda: backend,
        sleep=backend.sleep,
    )


def _old_traces(n):
    return {f"t{i:05d}": OLD for i in range(n)}


class TestTheDryRun:
    def test_it_counts_traces_and_spans_with_one_row_reads_and_deletes_nothing(self):
        backend = _Backend(
            traces={**_old_traces(3), "new": NEW},
            spans=[("t00000", OLD), ("t00001", OLD), ("new", NEW)],
        )

        result = _purge(backend, delete=False)

        assert (result.before, result.after, result.error_text) == (
            TraceCount(3, 2),
            None,
            None,
        )
        assert backend.deleted == [] and result.cutoff == STAMP
        assert [(r["entity"], r["page"], r["size"]) for r in backend.reads] == [
            ("traces", 1, 1),
            ("spans", 1, 1),
        ]

    def test_the_filter_is_the_cutoff_in_utc_and_every_read_is_of_the_project(self):
        backend = _Backend()

        _purge(backend, delete=False)

        traces, spans = backend.reads
        assert traces["filters"] == f'traces:start_time < "{STAMP}"'
        assert spans["filters"] == f'spans:start_time < "{STAMP}"'
        assert (traces["exclude"], spans["exclude"]) == (
            trace_erasure.json.dumps(TRACE_EXCLUDE),
            trace_erasure.json.dumps(SPAN_EXCLUDE),
        )
        assert {r["project_name"] for r in backend.reads} == {PROJECT}

    def test_a_time_without_an_offset_is_refused_before_any_client(self):
        def no_client():
            raise AssertionError("no client may be built")

        with pytest.raises(ValueError, match="UTC offset"):
            purge_traces_before(
                datetime(2026, 11, 18), delete=False, client_factory=no_client
            )

    def test_a_project_the_backend_does_not_have_is_reported_not_counted(self):
        """A mistyped project, or another workspace, is not an empty one."""
        backend = _Backend(project=False)

        result = _purge(backend)

        assert result.missing and result.after is None
        assert backend.reads == [] and backend.deleted == []


class TestTheDelete:
    def test_only_what_started_before_the_cutoff_goes(self):
        backend = _Backend(
            traces={**_old_traces(2), "new": NEW},
            spans=[("t00000", OLD), ("orphan", OLD), ("new", NEW)],
        )

        result = _purge(backend)

        assert result.before == TraceCount(2, 2)
        assert result.after == result.error_text == TraceCount(0, 0)
        assert backend.traces == {"new": NEW}
        assert backend.spans == [("new", NEW)]
        sent = {i for chunk in backend.deleted for i in chunk}
        assert sent == {"t00000", "t00001", "orphan"}
        assert {r["project_name"] for r in backend.reads} == {PROJECT}

    def test_each_delete_names_the_project_so_the_backend_confines_it(self):
        backend = _Backend(traces=_old_traces(1), spans=[("t00000", OLD)])

        _purge(backend)

        assert backend.delete_options == [
            {
                "timeout_in_seconds": 15,
                "additional_body_parameters": {"project_id": "p-faultmaven"},
            }
        ]

    def test_it_reads_one_page_deletes_it_and_reads_page_one_again(self):
        backend = _Backend(traces=_old_traces(TRACE_PAGE_SIZE + 500))

        _purge(backend)

        pages = [r for r in backend.reads if r["size"] == TRACE_PAGE_SIZE]
        assert {r["page"] for r in pages} == {1}
        assert [len(chunk) for chunk in backend.deleted] == [1000, 1000, 500]
        assert backend.traces == {}

    def test_spans_the_backend_deletes_later_are_waited_for_not_resent(self):
        """Their trace ids come back on every span page until the backend's
        cascade runs: a page of ids already sent is not progress."""
        backend = _Backend(
            traces=_old_traces(10),
            spans=[(t, OLD) for t in _old_traces(10) for _ in range(200)],
        )

        result = _purge(backend)

        assert result.after == TraceCount(0, 0) and backend.sleeps == 1
        assert backend.deleted == [sorted(_old_traces(10))]
        assert result.sent == 10

    def test_spans_whose_delete_was_lost_are_cleared_by_sending_it_again(self):
        backend = _Backend(
            traces=_old_traces(1), spans=[("t00000", OLD)], lost=["t00000"]
        )

        result = _purge(backend)

        assert result.after == TraceCount(0, 0)
        assert backend.deleted == [["t00000"], ["t00000"]]

    def test_spans_that_never_go_end_the_wait_with_what_is_left(self):
        backend = _Backend(spans=[("stuck", OLD)])
        backend.lost = type("Always", (set,), {"__contains__": lambda *_: True})()

        result = _purge(backend)

        assert result.after == TraceCount(0, 1)
        assert backend.sleeps == PURGE_SETTLE_POLLS

    def test_a_second_run_finds_and_deletes_nothing(self):
        backend = _Backend(traces=_old_traces(2), spans=[("t00000", OLD)])
        _purge(backend)
        backend.deleted.clear()

        result = _purge(backend)

        assert result.before == result.after == TraceCount(0, 0)
        assert backend.deleted == []

    def test_error_text_is_counted_project_wide_after_the_delete(self):
        backend = _Backend(traces=_old_traces(1), errors=2)

        result = _purge(backend)

        assert result.error_text == TraceCount(2, 2)
        assert backend.reads[-1]["filters"] == "spans:error_info is_not_empty"

    def test_a_backend_error_is_named_by_its_class_only(self):
        backend = _Backend(traces=_old_traces(1))

        def refused(**_):
            raise RuntimeError("the backend's body")

        backend.rest_client.traces.delete_traces = refused

        with pytest.raises(
            ServiceException, match=f"before {STAMP}: RuntimeError$"
        ) as raised:
            _purge(backend)

        assert "body" not in str(raised.value)

    @pytest.mark.parametrize(
        "error, rerun",
        [
            (_Status(503), True),
            (RuntimeError(), True),
            (_Status(429), True),
            (_Status(408), True),
            (_Status(403), False),
        ],
        ids=["5xx", "no-status", "rate-limited", "request-timeout", "4xx"],
    )
    def test_a_failure_after_a_delete_went_out_asks_for_a_rerun_unless_4xx(
        self, error, rerun
    ):
        backend = _Backend(traces=_old_traces(1500))
        real = backend.rest_client.traces.delete_traces

        def second_fails(**kwargs):
            if backend.deleted:
                raise error
            real(**kwargs)

        backend.rest_client.traces.delete_traces = second_fails

        with pytest.raises(ServiceException) as raised:
            _purge(backend)

        assert raised.value.details == {"rerun": rerun}

    def test_a_failure_before_any_delete_asks_for_no_rerun(self):
        backend = _Backend(traces=_old_traces(1))
        backend.rest_client.traces.get_traces_by_project = lambda **_: _raise(
            _Status(503)
        )

        with pytest.raises(ServiceException) as raised:
            _purge(backend)

        assert raised.value.details == {"rerun": False}

    def test_a_first_delete_that_times_out_may_have_gone_out(self):
        backend = _Backend(traces=_old_traces(1))
        backend.rest_client.traces.delete_traces = lambda **_: _raise(TimeoutError())

        with pytest.raises(ServiceException) as raised:
            _purge(backend)

        assert raised.value.details == {"rerun": True}

    def test_a_trace_id_first_sent_while_waiting_is_counted(self):
        """An orphan span hidden behind spans the backend has not deleted yet
        is first sent by the wait's re-send."""
        backend = _Backend(
            traces=_old_traces(1), spans=[("t00000", OLD)] * 2000 + [("orphan", OLD)]
        )

        result = _purge(backend)

        assert result.after == TraceCount(0, 0)
        assert result.sent == 2
        assert {i for chunk in backend.deleted for i in chunk} == {"t00000", "orphan"}


def _raise(error):
    raise error

"""The purge by start time on the real Opik SDK, against the stub of
``test_trace_erasure_loopback_2102.py`` on a port this test binds (#2700). No
live backend: what a real one does with the filter is the issue's pin-1 run.

The stub answers these reads by start time here, as the backend does for a
``date_time`` filter. What the SDK puts on the wire is what is pinned: the
backend parses the value as an ``Instant`` after decoding it twice, so an
offset's ``+`` would arrive as a space.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("opik")

from faultmaven.infrastructure.observability.trace_erasure import (  # noqa: E402
    TraceCount,
    purge_traces_before,
)
from tests.unit.infrastructure.observability.test_trace_erasure_loopback_2102 import (  # noqa: E402,F401
    PROJECT,
    _page_body,
    backend,
    client,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("restore_tenant_context")]

STAMP = "2026-11-18T00:00:00.000000Z"
OLD, NEW = "2026-11-01T00:00:00.000000Z", "2026-11-19T00:00:00.000000Z"


def _by_time(backend):
    """A page of the stored rows the filter's start time or error test admits."""

    def answer(query, kind):
        (wanted,) = json.loads(query["filters"])
        with backend._lock:
            rows = [
                row
                for row in backend.stored[kind].values()
                if (
                    row["start_time"] < wanted["value"]
                    if wanted["field"] == "start_time"
                    else bool(row.get("error_info"))
                )
            ]
        page, size = int(query["page"]), int(query["size"])
        content = [
            {k: row[k] for k in ("id", "trace_id", "start_time") if k in row}
            for row in rows[(page - 1) * size : page * size]
        ]
        return 200, _page_body(content, total=len(rows), page=page)

    return answer


def _seed(backend):
    for trace_id, start in (("t-old", OLD), ("t-new", NEW)):
        backend.stored["traces"][trace_id] = {"id": trace_id, "start_time": start}
    for span_id, trace_id, start in (
        ("s-old", "t-old", OLD),
        ("s-orphan", "t-gone", OLD),
        ("s-new", "t-new", NEW),
    ):
        backend.stored["spans"][span_id] = {
            "id": span_id,
            "trace_id": trace_id,
            "start_time": start,
        }
    backend.page["traces"] = backend.page["spans"] = _by_time(backend)


def _purge(client, delete):
    cutoff = datetime(2026, 11, 18, 5, 0, tzinfo=timezone(timedelta(hours=5)))
    return purge_traces_before(
        cutoff,
        delete=delete,
        project_name=PROJECT,
        client_factory=lambda: client,
        sleep=lambda _: None,
    )


def test_the_filter_on_the_wire_is_the_cutoff_in_utc(backend, client):
    _seed(backend)

    result = _purge(client, delete=False)

    assert result.before == TraceCount(1, 2)
    for kind in ("traces", "spans"):
        (query,) = backend.reads[kind]
        assert json.loads(query["filters"]) == [
            {
                "field": "start_time",
                "key": "",
                "type": "date_time",
                "operator": "<",
                "value": STAMP,
            }
        ]
        assert (query["page"], query["size"], query["project_name"]) == (
            "1",
            "1",
            PROJECT,
        )
    assert backend.deleted == []


def test_only_what_started_before_the_cutoff_is_deleted(backend, client):
    _seed(backend)

    result = _purge(client, delete=True)

    assert result.after == result.error_text == TraceCount(0, 0)
    assert sorted(i for chunk in backend.deleted for i in chunk) == ["t-gone", "t-old"]
    assert list(backend.stored["traces"]) == ["t-new"]
    assert list(backend.stored["spans"]) == ["s-new"]

"""The case delete's eraser for the LLM traces held in Opik (#2102).

``LLMRouter`` tags each trace and span that records a case's prompt or response
with the case and its enterprise; this finds those traces by the tags and deletes
them. Anything that stands in the way raises, which keeps the case row so a retry
can finish: there is no queued purge. It runs first of the case's erasers, so a
trace backend that is down leaves the rest of the case untouched. What a delete
can still miss is in ``docs/operations/case-deletion.md``.
"""

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Iterator, List, Optional

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability.tracing import trace_backend_configured

logger = logging.getLogger(__name__)

STORE_NAME = "llm_traces"

#: The only thing keeping an id from rewriting the filter: OQL's parser has no
#: escape for a quote inside a value. Case ids are ``case_<12 hex>``, enterprise
#: ids UUIDs.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

#: The SDK's own page size. A page shorter than this is the last one.
TRACE_PAGE_SIZE = 2000
TRACE_DELETE_CHUNK_SIZE = 1000
#: The whole erase, and what ends a very long case: each REST timeout bounds one
#: read, not a streamed response, and the number of pages is the case's to set.
TRACE_ERASE_TIMEOUT_SECONDS = 60

#: Every field each search can leave out, so no case content comes back into
#: memory: the read wants ids alone. ``id`` and a span's ``trace_id`` cannot be
#: excluded. Pinned against the SDK's enums.
TRACE_EXCLUDE = (
    "name start_time end_time input output metadata tags error_info usage "
    "created_at created_by last_updated_by feedback_scores span_feedback_scores "
    "comments guardrails_validations total_estimated_cost span_count "
    "llm_span_count has_tool_spans duration ttft thread_id visibility_mode "
    "providers experiment source"
).split()
SPAN_EXCLUDE = (
    "name type start_time end_time input output metadata model provider tags "
    "usage error_info created_at created_by last_updated_by feedback_scores "
    "comments total_estimated_cost total_estimated_cost_version duration ttft "
    "source"
).split()

#: Its own pool: on the default executor a hung backend would hold the slots the
#: PII sanitizer's ``asyncio.to_thread`` needs.
_ERASE_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="llm-trace-erase"
)


def _cached_client() -> Any:
    """The client ``@opik.track`` writes through, so a flush drains its queue."""
    from opik.api_objects.opik_client import get_client_cached

    return get_client_cached()


def _parsed_filters(filter_string: str, entity_type: str) -> Any:
    """The SDK's own parser, so what goes on the wire is the SDK's shape."""
    from opik.api_objects.helpers import parse_filter_expressions
    from opik.rest_api.types.span_filter_public import SpanFilterPublic
    from opik.rest_api.types.trace_filter_public import TraceFilterPublic

    return parse_filter_expressions(
        filter_string,
        parsed_item_class=(
            TraceFilterPublic if entity_type == "traces" else SpanFilterPublic
        ),
        entity_type=entity_type,
    )


def _refused(case_id: str, reason: str) -> ServiceException:
    return ServiceException(f"Cannot clear {STORE_NAME} for case {case_id}: {reason}")


class LLMTraceEraser:
    """Deletes the traces tagged with a case and its enterprise."""

    store_name = STORE_NAME

    def __init__(
        self,
        project_name: Optional[str],
        client_factory: Callable[[], Any] = _cached_client,
        flush_timeout_seconds: int = 20,
        request_timeout_seconds: int = 15,
    ):
        self.project_name = project_name
        self._client_factory = client_factory
        self._flush_timeout_seconds = flush_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        # Not ``tracing_is_effective()``: with the SDK's switch off spans stop,
        # the traces already recorded remain, and the client still reaches them.
        if not trace_backend_configured():
            raise _refused(case_id, "the trace backend did not initialize")
        for value in (case_id, enterprise_id):
            if not _SAFE_ID.fullmatch(value):
                raise ServiceException(
                    f"Cannot clear {STORE_NAME}: {value!r} is not a valid id"
                )
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _ERASE_EXECUTOR, self._erase, case_id, enterprise_id
                ),
                TRACE_ERASE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            # The worker can outlive this wait, in a slot of its own pool: its
            # reading stops at its deadline, and each call has its own timeout.
            # Whatever it deletes meanwhile a retry would have deleted.
            raise _refused(case_id, "timed out") from exc

    def _erase(self, case_id: str, enterprise_id: str) -> int:
        """The SDK's blocking calls, off the event loop."""
        deadline = time.monotonic() + TRACE_ERASE_TIMEOUT_SECONDS
        client = self._client_factory()
        self._deliver_queued(client, case_id)

        filter_string = (
            f'metadata.case_id = "{case_id}" '
            f'AND metadata.enterprise_id = "{enterprise_id}"'
        )
        # The tag and the content travel in separate batches, so a dropped trace
        # batch leaves a content span on an untagged trace: the spans find it.
        rest = client.rest_client
        options = {"timeout_in_seconds": self._request_timeout_seconds}
        shared = {
            "project_name": self.project_name or client.project_name,
            "limit": TRACE_PAGE_SIZE,
            "request_options": options,
        }

        def read_traces(cursor: Optional[str]) -> Iterable[bytes]:
            return rest.traces.search_traces(
                filters=_parsed_filters(filter_string, "traces"),
                exclude=TRACE_EXCLUDE,
                last_retrieved_id=cursor,
                **shared,
            )

        def read_spans(cursor: Optional[str]) -> Iterable[bytes]:
            return rest.spans.search_spans(
                filters=_parsed_filters(filter_string, "spans"),
                exclude=SPAN_EXCLUDE,
                last_retrieved_id=cursor,
                **shared,
            )

        ids = list(
            dict.fromkeys(
                _read_ids(read_traces, "id", case_id, deadline)
                + _read_ids(read_spans, "trace_id", case_id, deadline)
            )
        )
        for start in range(0, len(ids), TRACE_DELETE_CHUNK_SIZE):
            rest.traces.delete_traces(
                ids=ids[start : start + TRACE_DELETE_CHUNK_SIZE],
                request_options=options,
            )
        logger.info("Deleted %d traces of case %s", len(ids), case_id)
        return len(ids)

    def _deliver_queued(self, client: Any, case_id: str) -> None:
        """Send what this pod holds for the backend, so the search can find it.

        ``flush`` returning True is not delivery. After one connection failure
        the SDK parks every later trace in a local database with no send, and
        replays them only while its monitor's flag is True, which otherwise
        waits for a ping that ``init_opik_tracing`` spaces 999999 s apart. So the
        flag is reset here, through the SDK's internals as it has no public
        accessor. The ping interval stays as it is: a non-200 ping would park
        every trace on every pod (#2573). After the flush, a flag that is False
        again means a send failed, so the backend is unreachable.
        """
        try:
            monitor = client._streamer._fallback_replay_manager._monitor
            if not monitor.has_server_connection:
                monitor.reset()
            flushed = client.flush(timeout=self._flush_timeout_seconds)
            connected = monitor.has_server_connection
        except AttributeError as exc:
            raise _refused(case_id, "the Opik SDK's replay path has changed") from exc
        if not connected:
            raise _refused(case_id, "a send failed: the backend is unreachable")
        # A trace still queued here would land after the search and survive it.
        if not flushed:
            raise _refused(case_id, "queued traces did not reach the backend")


def _read_ids(
    read_page: Callable[[Optional[str]], Iterable[bytes]],
    row_key: str,
    case_id: str,
    deadline: float,
) -> List[str]:
    """``row_key`` of every row, paging until a short page."""
    ids: List[str] = []
    cursor: Optional[str] = None
    while True:
        rows = 0
        for row in _rows(read_page(cursor), row_key, case_id, deadline):
            ids.append(row[row_key])
            cursor = row["id"]
            rows += 1
        if rows < TRACE_PAGE_SIZE:
            return ids


def _rows(
    stream: Iterable[bytes], row_key: str, case_id: str, deadline: float
) -> Iterator[dict]:
    """The stream's rows, buffered across chunks as the SDK's parser does.

    Unlike that parser this raises on any line that is not a row. It logs and
    skips one, so a backend error inside a 200 stream reads as the end of the
    results and a delete would report success with the case's traces in place.
    """
    buffer = b""
    for chunk in stream:
        # Stops a worker that outlived ``wait_for``, as nothing else would stop
        # a slow stream or a backend that keeps answering full pages.
        if time.monotonic() > deadline:
            raise _refused(case_id, "ran past its time limit")
        *lines, buffer = (buffer + chunk).split(b"\n")
        for line in lines:
            yield _row(line, row_key, case_id)
    if buffer:
        yield _row(buffer, row_key, case_id)


def _row(line: bytes, row_key: str, case_id: str) -> dict:
    try:
        row = json.loads(line)
    except ValueError:
        row = None
    # ``id`` as well: it is the next page's cursor, and without one paging would
    # start over. The line itself stays out of the message, as it may be content.
    if not (isinstance(row, dict) and row.get(row_key) and row.get("id")):
        raise _refused(case_id, "the backend's stream held a line that is not a row")
    return row

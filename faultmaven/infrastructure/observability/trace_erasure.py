"""The eraser for the LLM traces held in Opik, of a case (#2102) or of a
document conversion (#2575).

``LLMRouter`` tags each trace and span that records a case's prompt or response
with the case and its enterprise, and each that carries an uploaded document's
text with its conversion and enterprise; this finds those traces by the tags and
deletes them. Anything that stands in the way raises, which keeps the row being
deleted so a retry can finish: there is no queued purge. Each delete runs it
before any row changes, so a trace backend that is down leaves the case, draft
or document untouched. What a delete can still miss is in
``docs/operations/case-deletion.md``.
"""

import asyncio
import functools
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability.tracing import trace_backend_configured
from faultmaven.utils.exception_name import exception_class_name

logger = logging.getLogger(__name__)

STORE_NAME = "llm_traces"

#: The only thing keeping an id from rewriting the filter: OQL's parser has no
#: escape for a quote inside a value. Case ids are ``case_<12 hex>``, conversion
#: ids ``conv_<12 hex>``, enterprise ids UUIDs.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

#: The metadata key each kind of owner is tagged with (``router._opik_owner``).
_OWNER_KEYS = {"case": "case_id", "conversion": "conversion_id"}

TRACE_PAGE_SIZE = 2000
TRACE_DELETE_CHUNK_SIZE = 1000
#: The whole erase, and what ends a very long case: the number of pages is the
#: case's to set.
TRACE_ERASE_TIMEOUT_SECONDS = 60

#: Every field each read can leave out, so no case content comes back into
#: memory. ``id`` and a span's ``trace_id`` cannot be excluded, and the SDK's page
#: parse requires ``start_time``. Pinned against the SDK's enums.
TRACE_EXCLUDE = (
    "name end_time input output metadata tags error_info usage "
    "created_at created_by last_updated_by feedback_scores span_feedback_scores "
    "comments guardrails_validations total_estimated_cost span_count "
    "llm_span_count has_tool_spans duration ttft thread_id visibility_mode "
    "providers experiment source"
).split()
SPAN_EXCLUDE = (
    "name type end_time input output metadata model provider tags "
    "usage error_info created_at created_by last_updated_by feedback_scores "
    "comments total_estimated_cost total_estimated_cost_version duration ttft "
    "source"
).split()

#: Its own pool: on the default executor a hung backend would hold the slots the
#: PII sanitizer's ``asyncio.to_thread`` needs.
_ERASE_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="llm-trace-erase"
)
#: One delivery at a time on this pod. The SDK's replay is a non-blocking mutex,
#: so a second delete's flush would return at once, its case's traces parked.
_REPLAY_LOCK = threading.Lock()


def _cached_client() -> Any:
    """The client ``@opik.track`` writes through, so a flush drains its queue."""
    from opik.api_objects.opik_client import get_client_cached

    return get_client_cached()


def _parsed_filters(filter_string: str, entity_type: str) -> Optional[str]:
    """The JSON string the SDK's own export sends these endpoints."""
    from opik.api_objects.opik_query_language import OpikQueryLanguage

    parser = getattr(OpikQueryLanguage, f"for_{entity_type}")
    return parser(filter_string).parsed_filters


def _refused(owner: str, reason: str) -> ServiceException:
    return ServiceException(f"Cannot clear {STORE_NAME} for {owner}: {reason}")


class LLMTraceEraser:
    """Deletes the traces tagged with a case, or a conversion, and its
    enterprise."""

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
        return await self._erase_owned("case", case_id, enterprise_id)

    async def erase_conversion(self, conversion_id: str, enterprise_id: str) -> int:
        """The traces of every model call that carried the conversion's document
        (#2575): its triage, its analysis, and each mode's generation."""
        return await self._erase_owned("conversion", conversion_id, enterprise_id)

    async def _erase_owned(self, kind: str, owner_id: str, enterprise_id: str) -> int:
        owner = f"{kind} {owner_id}"
        # Not ``tracing_is_effective()``: with the SDK's switch off spans stop,
        # the traces already recorded remain, and the client still reaches them.
        if not trace_backend_configured():
            raise _refused(owner, "the trace backend did not initialize")
        for value in (owner_id, enterprise_id):
            if not _SAFE_ID.fullmatch(value):
                raise ServiceException(
                    f"Cannot clear {STORE_NAME}: {value!r} is not a valid id"
                )
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _ERASE_EXECUTOR, self._erase, owner_id, enterprise_id, kind
                ),
                TRACE_ERASE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            # The worker can outlive this wait, in a slot of its own pool: its
            # reading stops at its deadline, and each call has its own timeout.
            # Whatever it deletes meanwhile a retry would have deleted.
            raise _refused(owner, "timed out") from exc
        except ServiceException:
            raise
        except Exception as exc:
            # The SDK's own errors, a refused connection or a 5xx whose text
            # carries the backend's response body (#2575): named by class only,
            # so no caller logs or answers that text.
            raise _refused(owner, exception_class_name(exc)) from exc

    def _erase(self, owner_id: str, enterprise_id: str, kind: str) -> int:
        """The SDK's blocking calls, off the event loop."""
        owner = f"{kind} {owner_id}"
        deadline = time.monotonic() + TRACE_ERASE_TIMEOUT_SECONDS
        client = self._client_factory()
        with _REPLAY_LOCK:
            self._deliver_queued(client, owner)

        filter_string = (
            f'metadata.{_OWNER_KEYS[kind]} = "{owner_id}" '
            f'AND metadata.enterprise_id = "{enterprise_id}"'
        )
        # The tag and the content travel in separate batches, so a dropped trace
        # batch leaves a content span on an untagged trace: the spans find it.
        # The paged endpoints, not the streaming search: a stream that fails
        # mid-query ends as cleanly as one that is done.
        rest = client.rest_client
        options = {"timeout_in_seconds": self._request_timeout_seconds}
        found: List[str] = []
        for get_page, entity, exclude, row_key in (
            (rest.traces.get_traces_by_project, "traces", TRACE_EXCLUDE, "id"),
            (rest.spans.get_spans_by_project, "spans", SPAN_EXCLUDE, "trace_id"),
        ):
            read_page = functools.partial(
                get_page,
                project_name=self.project_name or client.project_name,
                filters=_parsed_filters(filter_string, entity),
                exclude=json.dumps(exclude),
                size=TRACE_PAGE_SIZE,
                request_options=options,
            )
            found += _read_ids(read_page, row_key, owner, deadline)
        ids = list(dict.fromkeys(found))
        for start in range(0, len(ids), TRACE_DELETE_CHUNK_SIZE):
            rest.traces.delete_traces(
                ids=ids[start : start + TRACE_DELETE_CHUNK_SIZE],
                request_options=options,
            )
        logger.info("Deleted %d traces of %s", len(ids), owner)
        return len(ids)

    def _deliver_queued(self, client: Any, owner: str) -> None:
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
            raise _refused(owner, "the Opik SDK's replay path has changed") from exc
        if not connected:
            raise _refused(owner, "a send failed: the backend is unreachable")
        # A trace still queued here would land after the search and survive it.
        if not flushed:
            raise _refused(owner, "queued traces did not reach the backend")


class UnavailableTraceEraser:
    """Bound where traces cannot be erased: a backend configured without the
    Opik SDK, or a knowledge service no composition gave an eraser. Every
    delete that needs a purge fails instead of skipping it."""

    store_name = STORE_NAME

    def __init__(self, reason: str):
        self.reason = reason

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        raise _refused(f"case {case_id}", self.reason)

    async def erase_conversion(self, conversion_id: str, enterprise_id: str) -> int:
        raise _refused(f"conversion {conversion_id}", self.reason)


def _read_ids(
    read_page: Callable[..., Any], row_key: str, owner: str, deadline: float
) -> List[str]:
    """``row_key`` of every row, until the rows read reach the ``total`` the
    backend counted before it answered. A page that disagrees with it, or a row
    without its id, fails the delete."""
    ids: List[str] = []
    total = None
    while total is None or len(ids) < total:
        # Stops a worker that outlived ``wait_for`` on an owner of endless pages.
        if time.monotonic() > deadline:
            raise _refused(owner, "ran past its time limit")
        page = read_page(page=len(ids) // TRACE_PAGE_SIZE + 1)
        total = page.total if total is None else total
        rows = [getattr(row, row_key) for row in page.content or []]
        if not (
            isinstance(total, int)
            and page.total == total
            and all(rows)
            and len(rows) == min(TRACE_PAGE_SIZE, total - len(ids))
        ):
            raise _refused(owner, "the backend's pages disagree with their total")
        ids += rows
    return ids

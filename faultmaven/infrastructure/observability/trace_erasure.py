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

:func:`purge_traces_before` is the operator's purge by start time
(``fm-purge-traces``, #2700), through the same read and delete.
"""

import asyncio
import functools
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability import opik_sdk
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
#: The purge's wait for the spans of the traces it deleted: the backend deletes
#: them after it has answered the trace delete (Opik's ``TraceDeletedListener``).
PURGE_SETTLE_POLLS = 20
PURGE_SETTLE_SECONDS = 3.0

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


def _refused(owner: str, reason: str) -> ServiceException:
    return ServiceException(f"Cannot clear {STORE_NAME} for {owner}: {reason}")


class LLMTraceEraser:
    """Deletes the traces tagged with a case, or a conversion, and its
    enterprise."""

    store_name = STORE_NAME

    def __init__(
        self,
        project_name: Optional[str],
        client_factory: Callable[[], Any] = opik_sdk.cached_client,
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
        if not opik_sdk.trace_backend_configured():
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
        deleted = _Project(
            client.rest_client,
            self.project_name or client.project_name,
            {"timeout_in_seconds": self._request_timeout_seconds},
            owner,
        ).read_and_delete(
            filter_string,
            lambda read_page, row_key: _read_ids(read_page, row_key, owner, deadline),
        )
        if deleted is None:
            return 0
        logger.info("Deleted %d traces of %s", deleted, owner)
        return deleted

    def _deliver_queued(self, client: Any, owner: str) -> None:
        """Send what this pod holds for the backend, so the search can find it.

        ``flush`` returning True is not delivery. After one connection failure
        the SDK parks every later trace in a local database with no send, and
        replays them only while its monitor's flag is True, which otherwise
        waits for a ping ``opik_sdk.PING_INTERVAL_S`` apart. So the flag is
        reset here, through the SDK's internals as it has no public accessor.
        The ping interval stays as it is: a non-200 ping would park every trace
        on every pod (#2573). After the flush, a flag that is False again means
        a send failed, so the backend is unreachable.
        """
        try:
            monitor = opik_sdk.replay_monitor(client)
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
    """Bound where traces cannot be erased: a backend named in settings that
    this process did not configure (no SDK, a configuration that raised, or a
    composition that never configured), or a knowledge service no composition
    gave an eraser. Every delete that needs a purge fails instead of skipping it."""

    store_name = STORE_NAME

    def __init__(self, reason: str):
        self.reason = reason

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        raise _refused(f"case {case_id}", self.reason)

    async def erase_conversion(self, conversion_id: str, enterprise_id: str) -> int:
        raise _refused(f"conversion {conversion_id}", self.reason)


@dataclass(frozen=True)
class _Project:
    """The reads and the delete of one project's traces, refused as ``owner``.
    With ``project_id``, the backend confines each delete to the project too."""

    rest: Any
    name: str
    options: dict
    owner: str
    project_id: Optional[str] = None

    def read_each(
        self, filter_string: str, read: Callable[[Callable[..., Any], str], Any]
    ) -> Optional[List[Any]]:
        """``read(read_page, row_key)`` over the traces, then the spans, that
        ``filter_string`` matches, every content field excluded; None when the
        backend has no such project. The paged endpoints, not the streaming
        search: a stream that fails mid-query ends as cleanly as one that is
        done."""
        rest, results = self.rest, []
        for get_page, entity, exclude, row_key in (
            (rest.traces.get_traces_by_project, "traces", TRACE_EXCLUDE, "id"),
            (rest.spans.get_spans_by_project, "spans", SPAN_EXCLUDE, "trace_id"),
        ):
            read_page = functools.partial(
                get_page,
                project_name=self.name,
                filters=opik_sdk.parsed_filters(filter_string, entity),
                exclude=json.dumps(exclude),
                request_options=self.options,
            )
            try:
                results.append(read(read_page, row_key))
            except Exception as exc:
                # Only the first read's 404 can mean the project does not exist.
                if not (entity == "traces" and opik_sdk.is_not_found(exc)):
                    raise
                if not _no_such_project(rest, self.name, self.options):
                    raise
                logger.info(
                    "No trace project %r: nothing of %s to delete",
                    self.name,
                    self.owner,
                )
                return None
        return results

    def read_and_delete(
        self,
        filter_string: str,
        read: Callable[[Callable[..., Any], str], List[str]],
        sent: Optional[set] = None,
        resend: bool = False,
    ) -> Optional[int]:
        """Delete, in chunks, the traces ``read`` finds by the traces' ids and
        the spans' trace ids: a dropped trace batch leaves a content span on an
        untagged trace, which the spans find. Every id comes from a read of this
        project. Adds each chunk to ``sent`` before sending it, and skips the
        ids already there unless ``resend``. The ids sent, or None for no
        project."""
        found = self.read_each(filter_string, read)
        if found is None:
            return None
        ids = list(dict.fromkeys(i for rows in found for i in rows))
        if sent is not None and not resend:
            ids = [i for i in ids if i not in sent]
        options = self.options
        if self.project_id is not None:
            # Else the delete, and its span cascade, reach across the workspace.
            body = {"project_id": self.project_id}
            options = {**options, "additional_body_parameters": body}
        for start in range(0, len(ids), TRACE_DELETE_CHUNK_SIZE):
            chunk = ids[start : start + TRACE_DELETE_CHUNK_SIZE]
            if sent is not None:
                sent.update(chunk)
            self.rest.traces.delete_traces(ids=chunk, request_options=options)
        return len(ids)


@dataclass(frozen=True)
class TraceCount:
    traces: int
    spans: int


@dataclass(frozen=True)
class TracePurge:
    """What :func:`purge_traces_before` counted in ``project``."""

    project: str
    #: The cutoff as sent: UTC, ``%Y-%m-%dT%H:%M:%S.%fZ``.
    cutoff: str
    before: TraceCount
    #: The rest of a delete: None on a dry run.
    after: Optional[TraceCount] = None
    #: A delete's last check, with no time filter: what in the project still
    #: carries an error's text, which no trace written since #2698 does. A
    #: call that succeeded carries none, so it cannot show the rest.
    error_text: Optional[TraceCount] = None
    #: Distinct trace ids sent for deletion.
    sent: int = 0
    #: The backend has no project of that name: nothing was counted.
    missing: bool = False


def purge_traces_before(
    cutoff: datetime,
    *,
    delete: bool,
    project_name: Optional[str] = None,
    client_factory: Callable[[], Any] = opik_sdk.cached_client,
    request_timeout_seconds: int = 15,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = lambda line: None,
) -> TracePurge:
    """Count, and with ``delete`` delete, the traces and spans that started
    before ``cutoff`` in the project (#2700): an operator's purge of what
    #2698's release left with content. Blocking, with no deadline: each request
    has its timeout.

    It reads one page of ids and deletes the ids it has not sent, until a page
    holds none. The backend deletes a trace's spans after it has answered, so
    the recount waits up to ``PURGE_SETTLE_POLLS`` times, sending the delete
    again for the trace ids of spans still there. Nothing is sent from this
    process's queue: a command records no trace. Running it again deletes
    nothing it already deleted. A failure raises with ``details["rerun"]``:
    True once a delete may have gone out, unless the answer was a 4xx other
    than 408 or 429.
    """
    if cutoff.utcoffset() is None:
        raise ValueError("the cutoff needs a UTC offset: without one it is no instant")
    # The backend parses the value as an Instant after decoding it twice, so an
    # offset's "+" arrives as a space: UTC and "Z" only.
    stamp = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    owner = f"traces started before {stamp}"
    sent: set = set()
    try:
        client = client_factory()
        name = project_name or client.project_name
        options = {"timeout_in_seconds": request_timeout_seconds}
        project_id = _project_id(client.rest_client, name, options, owner)
        if project_id is None:
            return TracePurge(name, stamp, TraceCount(0, 0), missing=True)
        target = _Project(client.rest_client, name, options, owner, project_id)
        before_cutoff = f'start_time < "{stamp}"'

        def count(filter_string: str) -> TraceCount:
            found = target.read_each(filter_string, _total)
            return TraceCount(0, 0) if found is None else TraceCount(*found)

        def one_page(read_page: Callable[..., Any], row_key: str) -> List[str]:
            page = read_page(page=1, size=TRACE_PAGE_SIZE)
            rows = [getattr(row, row_key) for row in page.content or []]
            if not all(rows):
                raise _refused(owner, "a row came back without its id")
            return rows

        before, none = count(before_cutoff), TraceCount(0, 0)
        if not delete:
            return TracePurge(name, stamp, before)
        # A span the backend has not deleted yet names a trace already sent:
        # only new ids are progress, and the wait below handles the rest.
        while target.read_and_delete(before_cutoff, one_page, sent):
            progress(f"Sent {len(sent)} trace ids for deletion")
        after = count(before_cutoff)
        for poll in range(PURGE_SETTLE_POLLS):
            if after == none:
                break
            if poll:
                # What one wait did not clear: send their trace ids again.
                target.read_and_delete(before_cutoff, one_page, sent, resend=True)
            sleep(PURGE_SETTLE_SECONDS)
            after = count(before_cutoff)
        error_text = count("error_info is_not_empty")
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        final = isinstance(status, int) and 400 <= status < 500
        rerun = bool(sent) and not (final and status not in (408, 429))
        # An SDK error is named by class only: its text carries the backend's body.
        refusal = exc if isinstance(exc, ServiceException) else None
        refusal = refusal or _refused(owner, exception_class_name(exc))
        raise ServiceException(str(refusal), {"rerun": rerun}) from exc
    return TracePurge(name, stamp, before, after, error_text, len(sent))


def _project_id(rest: Any, project: str, options: dict, owner: str) -> Optional[str]:
    """The id of the project named exactly ``project``, or None when the
    backend lists every partial match and none is it (#2588's check)."""
    page = rest.projects.find_projects(
        name=project, size=TRACE_PAGE_SIZE, request_options=options
    )
    listed = page.content or []
    for listed_project in listed:
        if listed_project.name == project:
            return listed_project.id
    if page.total != len(listed):
        raise _refused(owner, "the backend's project list disagrees with its total")
    return None


def _total(read_page: Callable[..., Any], row_key: str) -> int:
    """The ``total`` a page of one row carries: the count, with no ids read."""
    total = read_page(page=1, size=1).total
    if not isinstance(total, int):
        raise ServiceException(f"Cannot count {STORE_NAME}: a page carried no total")
    return total


def _no_such_project(rest: Any, project: str, options: dict) -> bool:
    """#2588's positive check, after a 404 for ``project``: the backend that
    answered lists its projects, and none is named exactly ``project``. Its name
    filter matches partially and ignoring case, and a page holds 10 by default,
    so one page of the module's size is read, and it is conclusive only when it
    holds every match (``total``). Anything else raises or answers False, and
    the delete fails.

    What it cannot tell: a URL or workspace changed after the traces were
    written, which points the check at a backend that never held them (#2581).
    """
    page = rest.projects.find_projects(
        name=project, size=TRACE_PAGE_SIZE, request_options=options
    )
    listed = page.content or []
    return page.total == len(listed) and all(p.name != project for p in listed)


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
        page = read_page(page=len(ids) // TRACE_PAGE_SIZE + 1, size=TRACE_PAGE_SIZE)
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

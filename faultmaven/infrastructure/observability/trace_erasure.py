"""The case delete's eraser for the LLM traces held in Opik (#2102).

``LLMRouter._update_opik_span`` tags each trace that records a case's prompt or
response with the case and its enterprise; this finds those traces by the tags
and deletes them. Anything that stands in the way raises, which keeps the case
row so a retry can finish: there is no queued purge. What a delete can still
miss is in ``docs/operations/case-deletion.md``.
"""

import asyncio
import logging
import re
from typing import Any, Callable, List, Optional

from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability.tracing import trace_backend_configured

logger = logging.getLogger(__name__)

#: The only thing keeping an id from rewriting the filter: OQL's parser has no
#: escape for a quote inside a value. Case ids are ``case_<12 hex>``, enterprise
#: ids UUIDs.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

#: One search's limit (the SDK pages internally). A case can outgrow it, so a
#: full result fails the delete rather than reading as done.
TRACE_SEARCH_MAX_RESULTS = 5000
TRACE_DELETE_CHUNK_SIZE = 1000
#: The whole erase, because ``search_traces`` takes no request timeout and the
#: SDK's client bounds each read, not a streamed response.
TRACE_ERASE_TIMEOUT_SECONDS = 60


def _cached_client() -> Any:
    """The client ``@opik.track`` writes through, so a flush drains its queue."""
    from opik.api_objects.opik_client import get_client_cached

    return get_client_cached()


class LLMTraceEraser:
    """Deletes the traces tagged with a case and its enterprise."""

    store_name = "llm_traces"

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
            raise ServiceException(
                f"Cannot clear {self.store_name} for case {case_id}: "
                "the trace backend did not initialize"
            )
        for value in (case_id, enterprise_id):
            if not _SAFE_ID.fullmatch(value):
                raise ServiceException(
                    f"Cannot clear {self.store_name}: {value!r} is not a valid id"
                )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._erase, case_id, enterprise_id),
                TRACE_ERASE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            # The worker may outlive this wait. It only deletes this case's
            # traces, so that is harmless.
            raise ServiceException(
                f"Cannot clear {self.store_name} for case {case_id}: timed out"
            ) from exc

    def _erase(self, case_id: str, enterprise_id: str) -> int:
        """The SDK's blocking calls, off the event loop."""
        client = self._client_factory()
        # A trace still queued here would land after the search and survive.
        if not client.flush(timeout=self._flush_timeout_seconds):
            raise ServiceException(
                f"Cannot clear {self.store_name} for case {case_id}: "
                "queued traces did not reach the backend"
            )
        found = self._search(client, case_id, enterprise_id)
        ids = list(dict.fromkeys(trace.id for trace in found))
        for start in range(0, len(ids), TRACE_DELETE_CHUNK_SIZE):
            client.rest_client.traces.delete_traces(
                ids=ids[start : start + TRACE_DELETE_CHUNK_SIZE],
                request_options={"timeout_in_seconds": self._request_timeout_seconds},
            )
        if len(found) >= TRACE_SEARCH_MAX_RESULTS:
            raise ServiceException(
                f"Cannot clear {self.store_name} for case {case_id}: deleted "
                f"{len(ids)} traces and more may remain; a retry takes the next"
            )
        if ids:
            # Counted once and only logged: the backend deletes asynchronously,
            # so a loop on this could re-find the same ids and never end.
            visible = len(self._search(client, case_id, enterprise_id))
            logger.info(
                "Deleted %d traces of case %s; %d still visible to a search",
                len(ids),
                case_id,
                visible,
            )
        return len(ids)

    def _search(self, client: Any, case_id: str, enterprise_id: str) -> List[Any]:
        """The ids are what is wanted: the case's content stays out of memory."""
        return client.search_traces(
            project_name=self.project_name,
            filter_string=(
                f'metadata.case_id = "{case_id}" '
                f'AND metadata.enterprise_id = "{enterprise_id}"'
            ),
            max_results=TRACE_SEARCH_MAX_RESULTS,
            exclude=["input", "output"],
        )

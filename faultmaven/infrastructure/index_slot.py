"""The KB indexing slot: one encode at a time per host, admitted against the
request window (#2055).

KB upload, update and verify index inside the request, so behind an ingress
read timeout a document that takes longer is cut off after all the work. And
nothing serialised the encodes: two uploads on one pod shared its CPUs, and
both overran. This module is both halves.

**The slot.** Every KB indexing encode runs on one single-thread executor per
process, and takes an exclusive ``flock`` on a file in the container's temp
directory first. The file is per container, which is the scope of the CPU
quota, so the worker processes of one pod (the ``enterprise`` preset runs
four) take turns too. Query and evidence embeds do not use the slot.

**The tickets.** :class:`IndexAdmission` holds this process's tickets and is
touched only on the event loop's thread. :meth:`IndexAdmission.admit` is
synchronous, so the check and the reservation are one step. With a window set
it refuses, before anything is written, a document whose warm estimate alone
exceeds the window (413) or that the queue ahead of it leaves no room for (503
+ ``Retry-After``). With a window of 0 it refuses nothing, and the embedding
guard alone bounds the embed.

A ticket is released when its encode's thread settles, or when its encode was
cancelled before it started, and never because its caller gave up: an encode
that has started cannot be stopped, and the next one would share its CPUs.
"""

import asyncio
import fcntl
import logging
import math
import os
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

from faultmaven.infrastructure.embedding_guard import (
    EMBED_BATCH_LOAD_SECONDS,
    INDEX_CORE_SECONDS_PER_TOKEN,
    index_cores,
    index_estimate_seconds,
)
from faultmaven.models.exceptions import IndexAdmissionRefused, KnowledgeBaseError

logger = logging.getLogger(__name__)

#: One worker: one KB indexing encode at a time in this process.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb-index")

#: The host lock. ``tempfile.gettempdir()`` is the container's own temp
#: directory, the scope its CPU quota covers.
HOST_LOCK_PATH = os.path.join(tempfile.gettempdir(), "faultmaven-kb-index.lock")

#: The least a running encode counts as still ahead of a new request, so an
#: encode past its estimate (overrun, or hung) never reads as 0 s.
RUNNING_FLOOR_SECONDS = 10.0

#: The clock every ticket reads. Monotonic; tests substitute it.
_clock: Callable[[], float] = time.monotonic

_host_lock_warned = False


def now() -> float:
    """The slot's clock, for a caller that budgets against the same window."""
    return _clock()


def _encode_under_host_lock(
    encode: Callable[[List[str]], Optional[List[List[float]]]],
    texts: List[str],
    started: Callable[[], None],
) -> Optional[List[List[float]]]:
    """The slot's job, on its worker thread: the host lock, then the encode.
    Closing the descriptor releases the lock, when the encode returns or
    raises. A lock file that cannot be opened or locked is logged once, and the
    encode runs without it."""
    global _host_lock_warned
    fd: Optional[int] = None
    try:
        fd = os.open(HOST_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
            fd = None
        if not _host_lock_warned:
            _host_lock_warned = True
            logger.warning(
                "KB indexing host lock %s unavailable (%s): encodes in this "
                "process run without it",
                HOST_LOCK_PATH,
                type(exc).__name__,
            )
    try:
        started()
        return encode(texts)
    finally:
        if fd is not None:
            os.close(fd)


def _call_on_loop(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]):
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:  # the loop is closed: nothing else touches the tickets
        callback()


class IndexTicket:
    """One admitted KB indexing encode: its chunks, token count and estimate.

    An async context manager. Leaving it releases the ticket only if nothing
    was submitted; a submitted encode releases it when its thread settles.
    """

    def __init__(
        self,
        admission: "IndexAdmission",
        chunks: List[str],
        tokens: Optional[int],
        estimate_seconds: float,
    ) -> None:
        self._admission = admission
        self.chunks = chunks
        self.tokens = tokens
        self.estimate_seconds = estimate_seconds
        self.started_at: Optional[float] = None
        #: Its guard bound, set when it submits; None before.
        self.guard_seconds: Optional[float] = None
        self._future: Optional[Future] = None

    async def __aenter__(self) -> "IndexTicket":
        return self

    async def __aexit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        """Release the ticket if nothing was submitted; a submitted encode
        releases it when its thread settles. Idempotent."""
        if self._future is None:
            self._admission._release(self)

    def remaining_estimate(self, at: float) -> float:
        """What this ticket still charges the queue: its estimate while queued;
        once running, what is left of it, never under the floor."""
        if self.started_at is None:
            return self.estimate_seconds
        return max(
            self.estimate_seconds - (at - self.started_at), RUNNING_FLOOR_SECONDS
        )

    def outstanding_bound(self, at: float) -> float:
        """How much longer its guard may hold the slot: 0 before it submits."""
        if self.guard_seconds is None:
            return 0.0
        if self.started_at is None:
            return self.guard_seconds
        return max(0.0, self.guard_seconds - (at - self.started_at))

    def run(self, texts: List[str], *, guard_seconds: float) -> Tuple[Future, float]:
        """Submit the encode to the slot. Returns its future and the guard
        bounds still outstanding ahead of it, which the caller's wait adds to
        its own."""
        from faultmaven.infrastructure.model_cache import model_cache

        if self._future is not None:
            raise RuntimeError("an index ticket runs one encode")
        loop = asyncio.get_running_loop()
        at = _clock()
        ahead = sum(
            other.outstanding_bound(at)
            for other in self._admission._tickets
            if other is not self
        )
        self.guard_seconds = guard_seconds
        self._future = _EXECUTOR.submit(
            _encode_under_host_lock,
            model_cache.embed_texts_sync,
            texts,
            lambda: _call_on_loop(loop, self._mark_started),
        )
        self._future.add_done_callback(
            lambda _done: _call_on_loop(loop, lambda: self._admission._release(self))
        )
        return self._future, ahead

    def _mark_started(self) -> None:
        if self.started_at is None:
            self.started_at = _clock()


class IndexAdmission:
    """This process's KB indexing tickets, on the event loop's thread."""

    def __init__(self) -> None:
        self._tickets: List[IndexTicket] = []

    def pending_seconds(self) -> float:
        """The estimate of the indexing held ahead of a new request."""
        at = _clock()
        return sum(ticket.remaining_estimate(at) for ticket in self._tickets)

    def retry_after_seconds(self) -> int:
        """When the indexing held now should be done: the pending estimate,
        rounded up, and at least a second."""
        return max(1, math.ceil(self.pending_seconds()))

    def queued(self) -> int:
        """Tickets held in this process, the running encode's included."""
        return len(self._tickets)

    def encoding_for_seconds(self) -> Optional[float]:
        """How long the running encode has run, or None when none runs."""
        at = _clock()
        ages = [
            at - ticket.started_at
            for ticket in self._tickets
            if ticket.started_at is not None
        ]
        return max(ages) if ages else None

    def admit(
        self,
        chunks: List[str],
        tokens: Optional[int],
        *,
        window_seconds: float,
        size_limit_seconds: Optional[float] = None,
    ) -> IndexTicket:
        """A ticket for indexing ``chunks``, or a refusal before any write.

        Args:
            tokens: The chunks' BGE-M3 token count; None when it was not
                counted (no window) or could not be (no tokenizer).
            window_seconds: What the request may spend; 0 refuses nothing.
            size_limit_seconds: The window a document alone must fit,
                when the caller has spent part of it already (verify-batch);
                defaults to ``window_seconds``.

        Raises:
            IndexAdmissionRefused: 413 when the warm estimate alone exceeds the
                limit; 503, with ``Retry-After`` the pending estimate, when the
                pending estimate plus this ticket's exceeds the window.
            KnowledgeBaseError: ``KNOWLEDGE_EMBEDDER_UNAVAILABLE``, a window is
                set and the tokenizer could not count: nothing can be
                estimated, and the embed would fail the same way.
        """
        if not window_seconds or window_seconds <= 0:
            return self._issue(chunks, tokens)
        if tokens is None:
            raise KnowledgeBaseError(
                "Indexing unavailable: the embedding model's tokenizer could not "
                "be loaded",
                error_code="KNOWLEDGE_EMBEDDER_UNAVAILABLE",
            )
        warm = index_estimate_seconds(tokens)
        limit = window_seconds if size_limit_seconds is None else size_limit_seconds
        if warm > limit:
            most = int(limit * index_cores() / INDEX_CORE_SECONDS_PER_TOKEN)
            raise IndexAdmissionRefused(
                f"This document is {tokens:,} tokens, and this server can index "
                f"at most {most:,} tokens in one request. Split it into smaller "
                "documents. Nothing was saved.",
                status_code=413,
                retry_after_seconds=None,
            )
        if self.pending_seconds() + warm + self._load_charge() > window_seconds:
            wait = self.retry_after_seconds()
            raise IndexAdmissionRefused(
                "Other indexing on this server leaves no room for this document "
                f"within this request. Nothing was saved; retry in about {wait} "
                "seconds.",
                status_code=503,
                retry_after_seconds=wait,
            )
        return self._issue(chunks, tokens)

    def admit_forced(self, chunks: List[str], tokens: Optional[int]) -> IndexTicket:
        """A ticket that is never refused: the boot repair's, which still
        encodes on the slot."""
        return self._issue(chunks, tokens)

    def _load_charge(self) -> float:
        """A cold model's load, charged to the first ticket only."""
        from faultmaven.infrastructure.model_cache import model_cache

        if model_cache.peek_bge_m3_model() is None and not self._tickets:
            return EMBED_BATCH_LOAD_SECONDS
        return 0.0

    def _issue(self, chunks: List[str], tokens: Optional[int]) -> IndexTicket:
        estimate = index_estimate_seconds(tokens) if tokens is not None else 0.0
        ticket = IndexTicket(self, chunks, tokens, estimate + self._load_charge())
        self._tickets.append(ticket)
        return ticket

    def _release(self, ticket: IndexTicket) -> None:
        if ticket in self._tickets:
            self._tickets.remove(ticket)


#: The process's admission.
index_admission = IndexAdmission()

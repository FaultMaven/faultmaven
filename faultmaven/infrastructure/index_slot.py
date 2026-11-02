"""The indexing slots: one encode at a time, and KB indexing admitted against
the request window (#2055).

KB upload, update and verify index inside the request, so behind an ingress
read timeout a document that takes longer is cut off after all the work. And
nothing serialised the encodes: two uploads on one pod shared its CPUs, and
both overran. This module is both halves, and what an encode costs here.

**Two slots, one encode each.** Every KB indexing encode runs on one
single-thread executor per process (:data:`index_admission`), and takes an
exclusive ``flock`` on a file in the container's temp directory first. The file
is per container, which is the scope of the CPU quota, so the worker processes
of one pod take turns too. Evidence encodes run on a second single-thread
executor (:data:`evidence_slot`), with no host lock: a re-trigger queues behind
an encode still running rather than starting beside it, and a KB encode does
not wait for one. Query embeds use neither.

**The tickets.** A slot holds its tickets and is touched only on the event
loop's thread. A ticket is released when its encode's thread settles, or when
its encode never started, and never because its caller gave up: an encode that
has started cannot be stopped, and the next one would share its CPUs. A caller
that gives up while its job waits for the host lock stops it before it encodes.

**Admission.** :meth:`IndexAdmission.admit` is synchronous, so the check and
the reservation are one step. With a window set it refuses, before anything is
written, a document too large for the window (413), one the queue ahead leaves
no room for (503), one that fits only once the model has loaded (503, and the
load starts), and every document while an encode is past its time bound (503).
With a window of 0 it refuses nothing, and the embedding guard alone bounds
the embed.
"""

import asyncio
import fcntl
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, List, Optional, Tuple

from faultmaven.models.exceptions import IndexAdmissionRefused, KnowledgeBaseError

logger = logging.getLogger(__name__)

# A cold BGE-M3 load: 60-120s (`model_cache.py`, `PRELOAD_MODELS`, a supported
# opt-out precisely because operators accept that latency), 180s with headroom.
# The embedding guard's batch bound covers it, because a write is not cheap to
# repeat (see ``embedding_guard``); admission charges it to the first ticket a
# cold model sees, and a cold-band refusal's load is estimated at it.
EMBED_BATCH_LOAD_SECONDS = 180.0

# Core-seconds BGE-M3 spends encoding one token. Measured on the image under
# ``--cpus=2 --network none``, model warm, torch pinned to 2 threads (what
# ``configure_inference_threads`` pins on Cloud), chunked by ``ContentChunker``
# (round-49 probe, #2055 comment 6542538393):
#
#   | document                       | chars  | chunks | tokens | embed | ms/token |
#   |--------------------------------|--------|--------|--------|-------|----------|
#   | shipped runbooks, concatenated | 96,737 | 61     | 29,746 | 373 s | 12.5     |
#   | gateway log extract            | 99,500 | 49     | 45,861 | 504 s | 11.0     |
#
# 12.5 ms a token on 2 cores is 25 core-ms, the worse row. Tokens predicted the
# cost within 14%; bytes under-counted log text by 1.3x; and the 61-chunk
# document took less time than the 49-chunk one.
INDEX_CORE_SECONDS_PER_TOKEN = 0.025

# The most cores the rate is credited with. It was measured on 2; encode does
# not scale linearly past a few threads, so a bigger node is not assumed faster.
INDEX_MAX_CORES = 4.0

#: The least a started encode counts as still ahead of a new request.
RUNNING_FLOOR_SECONDS = 10.0

#: ``Retry-After`` while an encode is past its time bound: its thread settles,
#: or the process restarts, at a time no estimate gives.
HUNG_RETRY_SECONDS = 60

#: The host lock. ``tempfile.gettempdir()`` is the container's own temp
#: directory, the scope its CPU quota covers.
HOST_LOCK_PATH = os.path.join(tempfile.gettempdir(), "faultmaven-kb-index.lock")

_KB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb-index")
_EVIDENCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="evidence-index"
)

#: The clock every ticket reads. Monotonic; tests substitute it.
_clock: Callable[[], float] = time.monotonic


def now() -> float:
    """The slot's clock, for a caller that budgets against the same window."""
    return _clock()


def index_cores() -> float:
    """The cores an encode gets: the container's CPU quota (a fraction counts as
    a fraction), else the node's core count, capped at ``INDEX_MAX_CORES``."""
    from faultmaven.infrastructure.model_cache import _cgroup_cpu_quota

    return min(_cgroup_cpu_quota() or float(os.cpu_count() or 1), INDEX_MAX_CORES)


def index_estimate_seconds(tokens: int) -> float:
    """Seconds a warm model takes to encode ``tokens`` BGE-M3 tokens here."""
    return max(tokens, 0) * INDEX_CORE_SECONDS_PER_TOKEN / index_cores()


def index_max_tokens(seconds: float) -> int:
    """The most tokens a warm model encodes here in ``seconds``: the one integer
    every admission decision, and the 413's sentence, compare against."""
    return math.floor(seconds * index_cores() / INDEX_CORE_SECONDS_PER_TOKEN)


def refusal(reason: str, *, wait: int = 0, tokens: int = 0, most: int = 0):
    """The one place each refusal's sentence is written, for ``admit`` and for
    the 503 handler. ``reason``: ``too_large`` (413), ``busy``, ``loading`` or
    ``hung`` (503)."""
    sentences = {
        "too_large": (
            f"This document is {tokens:,} tokens, and this server can index at "
            f"most {most:,} tokens in one request. Split it into smaller "
            "documents. Nothing was saved."
        ),
        "busy": (
            "Other indexing on this server leaves no room for this document "
            f"within this request. Nothing was saved; retry in about {wait} "
            "seconds."
        ),
        "loading": (
            "The indexing model is loading on this server. Nothing was saved; "
            f"retry in about {wait} seconds."
        ),
        "hung": (
            "Indexing on this server is stuck past its time bound. Nothing was "
            f"saved; retry in about {wait} seconds."
        ),
    }
    return IndexAdmissionRefused(
        sentences[reason],
        status_code=413 if reason == "too_large" else 503,
        reason=reason,
        retry_after_seconds=None if reason == "too_large" else wait,
    )


def _call_on_loop(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]):
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:  # the loop is closed: nothing else touches the tickets
        callback()


class IndexTicket:
    """One encode on a slot: its chunks, token count and estimate.

    An async context manager. Leaving it releases the ticket only if nothing
    was submitted; a submitted job releases it when its thread settles.
    """

    def __init__(
        self,
        slot: "EncodeSlot",
        chunks: List[str],
        tokens: Optional[int],
        estimate_seconds: float,
    ) -> None:
        self._slot = slot
        self.chunks = chunks
        self.tokens = tokens
        self.estimate_seconds = estimate_seconds
        self.started_at: Optional[float] = None
        #: Its guard bound, set when it submits; None before.
        self.guard_seconds: Optional[float] = None
        self._future: Optional[Future] = None
        # Between the job, once it holds the host lock, and a caller giving up.
        self._gate = threading.Lock()
        self._abandoned = self._encoding = False

    async def __aenter__(self) -> "IndexTicket":
        return self

    async def __aexit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        """Release the ticket if nothing was submitted; a submitted job
        releases it when its thread settles. Idempotent."""
        if self._future is None:
            self._slot._release(self)

    def _elapsed(self, at: float) -> Optional[float]:
        return None if self.started_at is None else at - self.started_at

    def remaining_estimate(self, at: float) -> float:
        """What this ticket still charges the queue: its estimate while queued;
        once started, what is left of its estimate, then of its guard bound,
        never under the floor."""
        elapsed = self._elapsed(at)
        if elapsed is None:
            return self.estimate_seconds
        left = self.estimate_seconds - elapsed
        if left < 0:
            left = (self.guard_seconds or 0.0) - elapsed
        return max(left, RUNNING_FLOOR_SECONDS)

    def hung(self, at: float) -> bool:
        """Started, and past its own guard bound."""
        elapsed = self._elapsed(at)
        return elapsed is not None and elapsed > (self.guard_seconds or 0.0)

    def outstanding_bound(self, at: float) -> float:
        """How much longer its guard may hold the slot: 0 before it submits."""
        if self.guard_seconds is None:
            return 0.0
        elapsed = self._elapsed(at)
        return self.guard_seconds - min(elapsed or 0.0, self.guard_seconds)

    def run(self, texts: List[str], *, guard_seconds: float) -> Tuple[Future, float]:
        """Submit the encode. Returns its future and the guard bounds still
        outstanding ahead of it, which the caller's wait adds to its own."""
        from faultmaven.infrastructure.model_cache import model_cache

        encode = model_cache.embed_texts_sync
        return self._submit(lambda: encode(texts), guard_seconds)

    def _submit(self, work: Callable[[], Any], guard_seconds: float):
        if self._future is not None:
            raise RuntimeError("an index ticket runs one job")
        loop = asyncio.get_running_loop()
        at = _clock()
        ahead = sum(
            other.outstanding_bound(at)
            for other in self._slot._tickets
            if other is not self
        )
        self.guard_seconds = guard_seconds
        self._future = self._slot._executor.submit(
            self._slot._job, self, work, lambda: _call_on_loop(loop, self._started)
        )
        self._future.add_done_callback(
            lambda _done: _call_on_loop(loop, lambda: self._slot._release(self))
        )
        return self._future, ahead

    def abandon(self) -> bool:
        """The caller gave up. True when the encode will never run: it was
        still queued, or still waiting for the host lock, and is stopped. False
        when it is encoding, and holds the slot until its thread settles."""
        if self._future is None or self._future.cancel():
            return True
        with self._gate:
            self._abandoned = True
            return not self._encoding

    def _begin(self) -> bool:
        """On the slot's thread, once it holds the host lock: go on, unless the
        caller gave up meanwhile."""
        with self._gate:
            self._encoding = not self._abandoned
            return self._encoding

    def _started(self) -> None:
        if self.started_at is None:
            self.started_at = _clock()


class EncodeSlot:
    """One encode at a time: a single-thread executor and its tickets, held on
    the event loop's thread; with ``host_lock``, under the host lock too."""

    def __init__(self, executor: ThreadPoolExecutor, *, host_lock: bool) -> None:
        self._executor = executor
        self._host_lock = host_lock
        self._tickets: List[IndexTicket] = []
        #: False once this process could not open or lock the host lock file.
        self.host_lock_available = True
        #: True while the slot's thread is blocked in ``flock``.
        self.waiting_for_host_lock = False

    def admit_forced(self, chunks: List[str], tokens: Optional[int]) -> IndexTicket:
        """A ticket that is never refused: the boot repair's on the KB slot,
        every evidence encode's."""
        return self._issue(chunks, tokens)

    def pending_seconds(self) -> float:
        """The estimate of the work held ahead of a new request."""
        at = _clock()
        return sum(ticket.remaining_estimate(at) for ticket in self._tickets)

    def queued(self) -> int:
        """Tickets held, the running job's included."""
        return len(self._tickets)

    def encoding_for_seconds(self) -> Optional[float]:
        """How long the running job has run, or None when none runs."""
        at = _clock()
        ages = [at - t.started_at for t in self._tickets if t.started_at is not None]
        return max(ages) if ages else None

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

    def _job(self, ticket: IndexTicket, work: Callable[[], Any], started) -> Any:
        """On the slot's thread: the host lock if this slot takes one, then the
        work, unless the caller gave up while it waited. Closing the descriptor
        releases the lock, when the work returns or raises."""
        fd = self._lock_host() if self._host_lock else None
        try:
            if not ticket._begin():
                return None
            started()
            return work()
        finally:
            if fd is not None:
                os.close(fd)

    def _lock_host(self) -> Optional[int]:
        fd: Optional[int] = None
        try:
            fd = os.open(HOST_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
            self.waiting_for_host_lock = True
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            finally:
                self.waiting_for_host_lock = False
            return fd
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            if self.host_lock_available:
                logger.warning(
                    "KB indexing host lock %s unavailable (%s): encodes in this "
                    "process run without it",
                    HOST_LOCK_PATH,
                    type(exc).__name__,
                )
            self.host_lock_available = False
            return None


class IndexAdmission(EncodeSlot):
    """The KB indexing slot, and its admission against the request window."""

    def __init__(self) -> None:
        super().__init__(_KB_EXECUTOR, host_lock=True)
        self._loading: Optional[IndexTicket] = None

    def retry_after_seconds(self, reason: str = "busy") -> int:
        """The 503's wait, from the queue as it stands: the stuck encode's
        fixed wait, the model load's remaining estimate, or the pending
        estimate; rounded up, and at least a second."""
        if reason == "hung":
            return HUNG_RETRY_SECONDS
        if reason == "loading" and self._loading is not None:
            seconds = self._loading.remaining_estimate(_clock())
        else:
            seconds = self.pending_seconds()
        return max(1, math.ceil(seconds))

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
            size_limit_seconds: The window a document alone must fit, when the
                caller has spent part of it already (verify-batch); defaults
                to ``window_seconds``.

        Raises:
            IndexAdmissionRefused: 413 above ``index_max_tokens`` of the limit.
                503 while an encode is past its time bound (``hung``); when the
                document fits only once the cold model has loaded, which starts
                the load (``loading``); when the queue leaves it no room
                (``busy``).
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
        most = index_max_tokens(
            window_seconds if size_limit_seconds is None else size_limit_seconds
        )
        if tokens > most:
            raise refusal("too_large", tokens=tokens, most=most)
        at = _clock()
        if any(ticket.hung(at) for ticket in self._tickets):
            raise refusal("hung", wait=self.retry_after_seconds("hung"))
        pending = self.pending_seconds()
        if tokens <= index_max_tokens(window_seconds - pending - self._load_charge()):
            return self._issue(chunks, tokens)
        loading = self._loading.remaining_estimate(at) if self._loading else 0.0
        if self._cold() and tokens <= index_max_tokens(
            window_seconds - pending + loading
        ):
            self._start_load()
            raise refusal("loading", wait=self.retry_after_seconds("loading"))
        raise refusal("busy", wait=self.retry_after_seconds())

    @staticmethod
    def _cold() -> bool:
        from faultmaven.infrastructure.model_cache import model_cache

        return model_cache.peek_bge_m3_model() is None

    def _start_load(self) -> None:
        """Start the model load on the slot, once: a forced ticket the queue
        counts at a load's estimate."""
        from faultmaven.infrastructure.model_cache import model_cache

        if self._loading is not None:
            return
        self._loading = IndexTicket(self, [], None, EMBED_BATCH_LOAD_SECONDS)
        self._tickets.append(self._loading)
        self._loading._submit(
            lambda: model_cache.get_bge_m3_model("index-admission"),
            EMBED_BATCH_LOAD_SECONDS,
        )

    def _release(self, ticket: IndexTicket) -> None:
        super()._release(ticket)
        if ticket is self._loading:
            self._loading = None


#: The process's KB indexing slot and admission.
index_admission = IndexAdmission()

#: The process's evidence slot: no window and no host lock.
evidence_slot = EncodeSlot(_EVIDENCE_EXECUTOR, host_lock=False)

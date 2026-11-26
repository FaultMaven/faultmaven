"""The indexing slots: one encode at a time, and KB indexing admitted against
the request window (#2055).

KB upload, update and verify index inside the request, so behind an ingress
read timeout a document that takes longer is cut off after all the work. And
nothing serialised the encodes: two uploads on one pod shared its CPUs, and
both overran. This module is both halves, and what an encode costs here.

**One order, one encode at a time (#2920).** Both slots' encodes, KB and
evidence, are driven from one single-thread executor per process in submit
order. It takes an exclusive ``flock`` on a file in the container's temp
directory first (the host lock), and a KB encode then
runs in an ``encode_worker`` process its phase bounds kill (Principle 14,
#2851): the thread waits on a file lock and a worker, which is I/O. The file is
per container, which is the scope of the CPU quota, so the worker processes of
one pod take turns too. An evidence encode runs in this process. Query embeds
use neither.

**Scope: this process's tickets.** ``flock`` only excludes the other
processes, whose tickets nothing here counts. An evidence encode submitted between a KB
ticket's ``admit`` and its ``run`` is counted by its wait bound, not its
window. A stuck evidence encode cannot be killed and holds the host lock: the
other processes' writes wait in ``flock`` (``queued_too_long``) or are ``busy``.

**The tickets.** Both slots hold one list, which ``ahead``, admission's room,
``Retry-After``, ``hung`` and the status count, touched only on the event
loop's thread. A ticket is released when its encode's thread settles, or when
its encode never started. A caller that gives up while its job waits for the
host lock stops it before it encodes; one that gives up mid-encode kills a KB
worker, and the thread settles once it is reaped. An evidence encode that has
started cannot be stopped, and holds its slot until its thread settles.

**Admission.** :meth:`IndexAdmission.admit` is synchronous, so the check and
the reservation are one step. Every KB ticket is charged a worker's load
(``WORKER_LOAD_SECONDS``). With a window set it refuses, before anything is
written, a document too large for the window less that charge and a token
count's allowance (413), one the queue ahead leaves no room for (503), and
every document while an encode is past its time bound (503). On an empty
queue, with at most the allowance spent on the count (no wait for a slot, no
run past the allowance), a document is admitted or refused 413. With a window of 0 it
refuses nothing, and the embedding guard alone bounds the embed.
"""

import asyncio
import base64
import contextvars
import fcntl
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, List, Optional, Tuple

from faultmaven.infrastructure import encode_worker, worker_process
from faultmaven.models.exceptions import EmbedderUnavailable, IndexAdmissionRefused
from faultmaven.utils.exception_name import exception_log_name

logger = logging.getLogger(__name__)

# The embedding guard's constant term: a worker's BGE-M3 load, 180 s. Measured
# in the image at 2 CPUs a cold worker loaded in 14.1-15.5 s (#2851), so this
# is about 12x, the headroom of a hang bound: a slower core, a sub-1 CPU quota
# or a load beside other work stays inside it. The guard covers it,
# because a write is not cheap to repeat (see ``embedding_guard``).
EMBED_BATCH_LOAD_SECONDS = 180.0

#: What admission charges every KB ticket for its worker's load (#2851): 1.45x
#: the worst of seven runs in the image at 2 CPUs, spawn to the first batch of
#: four encoded (19.3-20.7 s cold, the weights evicted; 17.4-19.1 s warm). The
#: load is mostly one core, so this is wall seconds, not core-seconds, and the
#: disk read is under 1 s of it. A slower core, a CPU quota under 1 or a load
#: beside other work can take longer, and the encode overruns the window.
WORKER_LOAD_SECONDS = 30.0

#: What the 413 line allows for an uncontended token count, which the KB
#: routes spend from the window before admission (#2851): spawn to answer,
#: tokenizer load included, measured 1.4-1.8 s (the largest admitted runbook,
#: in the image at 2 CPUs). So on an empty queue only a count that waited for
#: a slot, or ran past its 2 s allowance (beside another process's encode, or under
#: a quota below 2 cores), leaves a document under the line too little room.
COUNT_ALLOWANCE_SECONDS = 2.0

#: The guard's bound less the worker's phases: the time to kill and reap it
#: (4-8 ms measured), so a ticket never reads ``hung`` while its worker is
#: reaped.
REAP_MARGIN_SECONDS = 5.0

#: Spawn to ``ready`` (0.29-0.40 s measured), and each transfer of the texts
#: or the vectors, for an encode worker.
ENCODE_START_SECONDS = 10.0
ENCODE_TRANSFER_SECONDS = 10.0

#: ``RLIMIT_AS`` of an encode worker, whatever its thread count: four texts at
#: the 8,192-token cap, the most one batch holds, peaked at a VmPeak of
#: 6,159 MiB with no CPU quota (4 threads), and 7,190 and 7,566 MiB at 2 and 4
#: CPUs (measured in the image, #2851), 1.6x below this.
ENCODE_MEMORY_BYTES = 12 * 1024 * 1024 * 1024

#: The encode worker. Always this value: tests point it at one of their own.
_ENCODE_WORKER = encode_worker.__name__

#: One vector's float32 values.
_DIMENSIONS = 1024

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

#: The one encode order's thread, both slots' (#2920).
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index-encode")

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


def encode_threads() -> int:
    """An encode worker's threads: ``index_cores()`` rounded up."""
    return math.ceil(index_cores())


def encode_cpu_seconds(guard_seconds: float) -> int:
    """``RLIMIT_CPU`` of an encode worker: its guard bound on every thread,
    and 10 s more, so the phase bounds end it first."""
    return math.ceil(guard_seconds * encode_threads()) + 10


def encode_output_limit_bytes(texts: int) -> int:
    """What the parent reads from an encode worker: the vectors' base64, and
    1 MiB of frames and log records."""
    return 4 * math.ceil(texts * _DIMENSIONS * 4 / 3) + 1024 * 1024


def encode_phases(guard_seconds: float) -> worker_process.Phases:
    """An encode worker's steps, summing to its guard bound less the reap
    margin: the work gets what the start and the transfers leave."""
    fixed = ENCODE_START_SECONDS + 2 * ENCODE_TRANSFER_SECONDS + REAP_MARGIN_SECONDS
    return worker_process.Phases(
        start=ENCODE_START_SECONDS,
        request=ENCODE_TRANSFER_SECONDS,
        work=guard_seconds - fixed,
        result=ENCODE_TRANSFER_SECONDS,
    )


class ModelNotLoaded(RuntimeError):
    """An encode whose model would not load (#2461): from a worker whose failure
    is ``encode_worker.MODEL_NOT_LOADED``, or the evidence slot's in-process
    encode. It reaches the guard through the ticket's future."""


def encode_in_worker(
    ticket: "IndexTicket", texts: List[str], guard_seconds: float
) -> Optional[List[List[float]]]:
    """On the KB slot's thread: one ``encode_worker`` over ``texts``, its
    vectors, or None when it gave none (logged with how it ended). Raises
    ``TimeoutError`` past its phases, ``ModelNotLoaded`` when the model would
    not load."""
    return asyncio.run(_encode_in_worker(ticket, texts, guard_seconds))


async def _encode_in_worker(
    ticket: "IndexTicket", texts: List[str], guard_seconds: float
) -> Optional[List[List[float]]]:
    if not ticket._attach(asyncio.current_task()):
        return None  # its caller gave up before this task existed
    try:
        end = await worker_process.run(
            _ENCODE_WORKER,
            {"threads": encode_threads()},
            memory_bytes=ENCODE_MEMORY_BYTES,
            cpu_seconds=encode_cpu_seconds(guard_seconds),
            output_limit_bytes=encode_output_limit_bytes(len(texts)),
            phases=encode_phases(guard_seconds),
            parts=tuple(texts),
        )
    except worker_process.WorkerFailed as e:
        logger.error("KB encode worker gave no vectors: %s", exception_log_name(e))
        return None
    except asyncio.CancelledError:
        return None  # its caller gave up: the worker is reaped
    if end.timed_out:
        raise TimeoutError(f"KB encode worker passed its {end.phase} phase bound")
    if end.failure == encode_worker.MODEL_NOT_LOADED:
        raise ModelNotLoaded(end.failure)
    if end.result is None:
        logger.error(
            "KB encode worker gave no vectors: ended in %s, exit %s%s",
            end.phase,
            end.code,
            (
                " (past its memory limit)"
                if end.code == worker_process.OUT_OF_MEMORY_EXIT
                else ""
            ),
        )
        return None
    return _vectors(end.result, len(texts))


def _vectors(parts: List[str], texts: int) -> Optional[List[List[float]]]:
    """The worker's answer as ``texts`` vectors, or None (logged) for any other
    shape: a short or misshapen answer is never written."""
    import numpy as np

    try:
        raw = base64.b64decode(parts[0], validate=True) if len(parts) == 1 else b""
    except ValueError:
        raw = b""
    if len(raw) != texts * _DIMENSIONS * 4:
        logger.error("KB encode worker answered %d bytes for %d texts", len(raw), texts)
        return None
    values = np.frombuffer(raw, dtype="<f4").reshape(texts, _DIMENSIONS)
    return values.tolist()


def refusal(reason: str, *, wait: int = 0, tokens: int = 0, most: int = 0):
    """The one place each refusal's sentence is written, for ``admit`` and for
    the 503 handler. ``reason``: ``too_large`` (413), ``busy`` or ``hung``
    (503)."""
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
    """One encode on a slot: its chunks, token count, estimate and principal
    (the user who asked, whose slot a token count takes).

    An async context manager. Leaving it releases the ticket only if nothing
    was submitted; a submitted job releases it when its thread settles.
    """

    def __init__(
        self,
        slot: "EncodeSlot",
        chunks: List[str],
        tokens: Optional[int],
        estimate_seconds: float,
        principal: Optional[str],
    ) -> None:
        self._slot = slot
        self.chunks = chunks
        self.tokens = tokens
        self.estimate_seconds = estimate_seconds
        self.principal = principal
        self.started_at: Optional[float] = None
        #: Its guard bound, set when it submits; None before.
        self.guard_seconds: Optional[float] = None
        self._future: Optional[Future] = None
        # Between the job, once it holds the host lock, and a caller giving up.
        self._gate = threading.Lock()
        self._abandoned = self._encoding = False
        # The worker's task, on the slot thread's own loop, once it exists.
        self._task: Optional["asyncio.Task[Any]"] = None

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
        encode = self._slot._encode
        return self._submit(lambda: encode(self, texts, guard_seconds), guard_seconds)

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
        self._future = _EXECUTOR.submit(
            contextvars.copy_context().run,
            self._slot._job,
            self,
            work,
            lambda: _call_on_loop(loop, self._started),
        )
        self._future.add_done_callback(
            lambda _done: _call_on_loop(loop, lambda: self._slot._release(self))
        )
        return self._future, ahead

    def abandon(self) -> bool:
        """The caller gave up. True when the encode will never run: it was
        still queued, or still waiting for the host lock, and is stopped. False
        when it is encoding: a worker's task is cancelled, which reaps it, and
        the slot is held until its thread settles."""
        if self._future is None or self._future.cancel():
            return True
        with self._gate:
            self._abandoned = True
            if self._task is not None:
                _call_on_loop(self._task.get_loop(), self._task.cancel)
            return not self._encoding

    def _attach(self, task: "asyncio.Task[Any]") -> bool:
        """On the slot's thread, the worker's task as its first step: False
        when the caller gave up before it existed, which ``abandon`` could not
        cancel."""
        with self._gate:
            self._task = task
            return not self._abandoned

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
    """One kind of encode in this process's one order, whose state is the
    class's, shared by both slots (#2920)."""

    _tickets: List[IndexTicket] = []
    #: False while the latest attempt here to open or lock the host lock failed.
    host_lock_available = True
    #: True while the order's thread is blocked in ``flock``.
    waiting_for_host_lock = False

    #: What each ticket is charged beyond its estimate.
    load_seconds = 0.0

    def admit_forced(
        self, chunks: List[str], tokens: Optional[int], *, principal=None
    ) -> IndexTicket:
        """A ticket that is never refused: the boot repair's on the KB slot,
        every evidence encode's (no principal: it counts no tokens)."""
        return self._issue(chunks, tokens, principal)

    def _encode(
        self, ticket: IndexTicket, texts: List[str], guard_seconds: float
    ) -> Optional[List[List[float]]]:
        """The encode, on the slot's thread: in this process."""
        from faultmaven.infrastructure.model_cache import model_cache

        vectors = model_cache.embed_texts_sync(texts)
        if vectors is None:
            raise ModelNotLoaded(encode_worker.MODEL_NOT_LOADED)
        return vectors

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

    def _issue(
        self, chunks: List[str], tokens: Optional[int], principal: Optional[str]
    ) -> IndexTicket:
        estimate = index_estimate_seconds(tokens) if tokens is not None else 0.0
        ticket = IndexTicket(
            self, chunks, tokens, estimate + self.load_seconds, principal
        )
        self._tickets.append(ticket)
        return ticket

    def _release(self, ticket: IndexTicket) -> None:
        if ticket in self._tickets:
            self._tickets.remove(ticket)

    def _job(self, ticket: IndexTicket, work: Callable[[], Any], started) -> Any:
        """On the order's thread: the host lock, then the
        work, unless the caller gave up while it waited. Closing the descriptor
        releases the lock, when the work returns or raises."""
        fd = self._lock_host()
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
            EncodeSlot.waiting_for_host_lock = True
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            finally:
                EncodeSlot.waiting_for_host_lock = False
            EncodeSlot.host_lock_available = True
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
            EncodeSlot.host_lock_available = False
            return None


class IndexAdmission(EncodeSlot):
    """The KB indexing slot, and its admission against the request window."""

    load_seconds = WORKER_LOAD_SECONDS

    def _encode(
        self, ticket: IndexTicket, texts: List[str], guard_seconds: float
    ) -> Optional[List[List[float]]]:
        """The encode, on the slot's thread: in a worker."""
        return encode_in_worker(ticket, texts, guard_seconds)

    def retry_after_seconds(self, reason: str = "busy") -> int:
        """The 503's wait, from the queue as it stands: the stuck encode's
        fixed wait, or the pending estimate; rounded up, and at least a
        second."""
        if reason == "hung":
            return HUNG_RETRY_SECONDS
        return max(1, math.ceil(self.pending_seconds()))

    def admit(
        self,
        chunks: List[str],
        tokens: Optional[int],
        *,
        principal: str,
        window_seconds: float,
        size_limit_seconds: Optional[float] = None,
    ) -> IndexTicket:
        """A ticket for indexing ``chunks``, or a refusal before any write.

        Args:
            tokens: The chunks' BGE-M3 token count; None when it was not
                counted (no window) or could not be.
            principal: The user who asked.
            window_seconds: What the request may spend; 0 refuses nothing.
            size_limit_seconds: The window a document alone must fit, when the
                caller has spent part of it already (verify-batch's earlier
                drafts, or the token count); defaults to ``window_seconds``. A
                remaining window shorter than it less the count allowance can
                answer ``busy`` on an empty queue, by design.

        Raises:
            IndexAdmissionRefused: 413 above ``index_max_tokens`` of the limit
                less ``WORKER_LOAD_SECONDS`` and ``COUNT_ALLOWANCE_SECONDS``. 503 while an encode is past its
                time bound (``hung``), or when the queue leaves it no room
                (``busy``).
            EmbedderUnavailable: ``worker_failed``, a window is set and the
                count gave no answer: nothing can be estimated.
        """
        if not window_seconds or window_seconds <= 0:
            return self._issue(chunks, tokens, principal)
        if tokens is None:
            raise EmbedderUnavailable(
                "Indexing unavailable: this document's tokens were not counted",
                reason="worker_failed",
            )
        limit = window_seconds if size_limit_seconds is None else size_limit_seconds
        most = index_max_tokens(limit - WORKER_LOAD_SECONDS - COUNT_ALLOWANCE_SECONDS)
        if tokens > most:
            raise refusal("too_large", tokens=tokens, most=most)
        at = _clock()
        if any(ticket.hung(at) for ticket in self._tickets):
            raise refusal("hung", wait=self.retry_after_seconds("hung"))
        room = window_seconds - self.pending_seconds() - WORKER_LOAD_SECONDS
        if tokens <= index_max_tokens(room):
            return self._issue(chunks, tokens, principal)
        raise refusal("busy", wait=self.retry_after_seconds())


#: The process's KB indexing slot and admission.
index_admission = IndexAdmission()

#: The process's evidence slot: no window, in the same order.
evidence_slot = EncodeSlot()

"""One answer, for every vector store, to "the embedder would not load".

Every retrieval path here embeds the query with BGE-M3 before it can search
anything, and each one had invented its own answer for an unavailable model:
``[]`` from the knowledge base store, ``[]`` from the generic KB store, a silent
switch to ChromaDB's default embedding from the case evidence store. None of
those are neutral values — downstream they are consumed as *answers*, and reach
the investigating model as a claim about what the index holds ("the knowledge
base has nothing", "this case has no matching evidence"). That is absence of
evidence rendered as evidence of absence, which the investigation then reasons
from (#941, #943).

The correct answer is the same everywhere, so it lives in one place rather than
being restated per store: raise ``KnowledgeBaseError``. A caller that genuinely
wants to tolerate an unavailable embedder has to catch it, which makes
tolerating it opt-IN and visible at the call site instead of a default buried in
an adapter.

**Every query-embedding site routes through here** — ``KnowledgeVectorStore``,
``CaseVectorStore`` and ``RunbookKnowledgeBase.search_by_text`` on the live
paths, plus ``ChromaDBVectorStore``, whose ``search`` has no caller today (every
vector ``search`` call site passes ``collection_name=``, which it does not
accept: they call ``IKnowledgeVectorStore`` and ``CaseVectorStore``) and is
covered so it is not a trap for the next one.

``search_by_text`` is why this is a module and not a method. It refused
correctly, with its own copy of the logic — and the copy had silently dropped
the time bound. A site that hand-rolls this is a bug even when its refusal looks
right.

The write side is here for the same reason, and covers both indexing embeds:
``KnowledgeService._index_document_in_vector_store`` (the one #953 was about)
and evidence vectorization (``core/preprocessing/vector_storage.py``, live via
the ``vectorize_file`` agent tool), which hand-rolled its own answer with no
time bound until #2055. Since then the write side also owns three things the
query side does not (#2055): the indexing RATE (measured, then learned), the
refusal of a batch that cannot finish inside its caller's budget before any
embed starts (:func:`index_budget_refusal`), and the one queue indexing embeds
run in, one at a time. Query embeds are not queued.

The two bounds are deliberately DIFFERENT numbers, and this module is where
that stays visible. A query may give up before a cold load finishes, because
the load continues in the background and the retry finds it warm; an index may
not, because it fails a write rather than a read. Same policy, same error
codes, different tolerance — see the constants below.

**Call these BEFORE entering** ``BaseExternalClient.call_external``. The
embedding is a local model call, not the ChromaDB round-trip the retry and
circuit-breaker policy exists for: raising inside that wrapper burns the full
retry budget on a model that cannot recover in seconds, and charges ChromaDB's
circuit breaker for an embedder fault. Being outside ``call_external`` also
puts it outside ``call_external``'s timeout, which is why it carries its own —
without one a cold BGE-M3 load hangs the tool call all the way to the outer
turn budget.
"""

import asyncio
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.models.exceptions import IndexBudgetRefused, KnowledgeBaseError

logger = logging.getLogger(__name__)

# Bound on a single query embedding. This ADDS to the ChromaDB budget rather
# than sharing it, so a search's worst case is embed + query, not one 10s
# ceiling for both. Matched to call_external's 10s for consistency.
#
# This is SHORTER than a cold BGE-M3 load takes (60-120s, see
# ``ModelCache.peek_bge_m3_model``), and deliberately so: a search that gives up
# fast is recoverable — the load continues on its worker thread and warms the
# cache, so the retry succeeds. A search is cheap to repeat.
EMBED_TIMEOUT_SECONDS = 10.0

# The batch equivalent does NOT reuse that number, because a write is not cheap
# to repeat. Indexing is the last step before a document is saved, so a bound
# that fires during a HEALTHY cold load doesn't degrade a read — it fails the
# write, and on the publish path takes the freshly-created row down with it.
#
# So the constant term has to cover the load this repo documents rather than the
# one the query path is willing to skip: 60-120s cold (`model_cache.py`,
# `PRELOAD_MODELS`, which is a supported opt-out precisely because operators
# accept that latency). 180s is that worst case plus headroom.
EMBED_BATCH_LOAD_SECONDS = 180.0

# The indexing rate, in core-seconds per UTF-8 byte (#2055). Measured on the
# dev host on 2026-10-27, in `ghcr.io/faultmaven/faultmaven:latest` with no
# network, the model loaded through `ModelCache`, one warm-up, then 32 shipped
# pack chunks per bucket (`ContentChunker` over the pack: median 1,773 chars):
#
#   mean chars / tokens   2 CPUs: s/chunk, ms/char   4 CPUs: s/chunk, ms/char
#   525 / 170             1.48, 2.82                 0.81, 1.54
#   941 / 296             2.81, 2.99                 1.66, 1.76
#   1,781 / 551           5.62, 3.16                 3.20, 1.80
#   2,674 / 822           9.05, 3.39                 5.04, 1.89
#
# A chunk's cost varies 6x with its length and a character's 1.2x, so the unit
# is the byte, not the chunk. The text was ASCII Markdown (a character is a
# byte); bytes over-count CJK and log text, the safe direction. The worst bucket
# costs 7.6 core-ms per byte at both CPU counts, and scaling held to 4 cores:
# past 4 no further speed-up is assumed. This is a FLOOR. The live rate is
# learned from completed batches and is never taken to be faster than this.
#
# Caveat: no probed chunk exceeded 3,100 chars. `ContentChunker` keeps a single
# line longer than its 3,000-char cap whole (`_split_by_sentences`) and BGE-M3
# reads up to 8,192 tokens of it. Up to that point attention costs more per byte
# than this rate (the learned rate rises to it); past it the truncated bytes
# cost nothing and are over-counted.
INDEX_CORE_SECONDS_PER_BYTE = 7.6e-3

#: The most cores the rate was measured at.
INDEX_MEASURED_MAX_CORES = 4.0

#: The newest batch's weight in the learned rate.
INDEX_RATE_EWMA_WEIGHT = 0.2


class _LearnedRate:
    """The indexing rate this process has observed, floored at the measured one."""

    def __init__(self) -> None:
        self.core_seconds_per_byte = INDEX_CORE_SECONDS_PER_BYTE
        self.samples = 0

    def learn(self, elapsed: float, nbytes: int, cores: float) -> None:
        if nbytes <= 0 or elapsed <= 0:
            return
        observed = elapsed * cores / nbytes
        blended = (
            INDEX_RATE_EWMA_WEIGHT * observed
            + (1 - INDEX_RATE_EWMA_WEIGHT) * self.core_seconds_per_byte
        )
        self.core_seconds_per_byte = max(blended, INDEX_CORE_SECONDS_PER_BYTE)
        self.samples += 1


_rate = _LearnedRate()


def index_cores() -> float:
    """The cores the rate is divided by: the CPU quota, capped at the measured range."""
    from faultmaven.infrastructure.model_cache import inference_cores

    return min(inference_cores(), INDEX_MEASURED_MAX_CORES)


def _byte_count(texts: List[str]) -> int:
    return sum(len(text.encode("utf-8", "surrogatepass")) for text in texts)


def estimated_index_seconds(texts: List[str]) -> float:
    """How long embedding ``texts`` should take here, warm, at the learned rate."""
    return _rate.core_seconds_per_byte * _byte_count(texts) / index_cores()


def batch_embed_timeout(texts: List[str]) -> float:
    """The bound for embedding ``texts`` in one batch: twice the estimate, plus a load.

    Proportional to the work and NOT capped at some round number: the bound
    turns "never returns" into "returns an error", and a ceiling tighter than
    what a large document legitimately costs would make that document
    permanently un-editable. Twice the estimate is what makes the refusal and
    the guard one policy (#2055): a batch :func:`index_budget_refusal` admitted
    is cut off only if the real rate is more than twice the learned one. Under
    #952 a timeout raises before the destructive delete, so the two stores
    still describe the same text; a process killed mid-swap is not covered by
    that, or by anything short of 2PC.
    """
    return EMBED_BATCH_LOAD_SECONDS + 2 * estimated_index_seconds(texts)


def request_window_seconds() -> Optional[float]:
    """``INDEX_REQUEST_WINDOW_SECONDS`` as a budget; None when it is 0 (refuse nothing)."""
    from faultmaven.config.settings import get_settings

    window = get_settings().knowledge.index_request_window_seconds
    return float(window) if window else None


class _IndexQueue:
    """The one queue indexing embeds run in, per event loop.

    Two encodes at once oversubscribe the pod's CPU quota (#1260), so they run
    one at a time. The slot is held until the ENCODE settles, not until its
    caller gives up: a timed-out encode keeps running on its worker thread, and
    starting the next beside it is the oversubscription the slot prevents.
    """

    def __init__(self) -> None:
        self.slot = asyncio.Semaphore(1)
        self.waiting: Dict[object, float] = {}
        self.running: Optional[Tuple[float, float]] = None  # (estimate, started)
        self.warming: Optional[asyncio.Future] = None

    def remaining_seconds(self) -> float:
        """The estimated seconds of work queued and running ahead of a new batch."""
        ahead = sum(self.waiting.values())
        if self.running is not None:
            estimate, started = self.running
            ahead += max(estimate - (time.monotonic() - started), 0.0)
        return ahead

    async def run(
        self, ticket: object, texts: List[str]
    ) -> Optional[List[List[float]]]:
        from faultmaven.infrastructure.model_cache import model_cache

        try:
            await self.slot.acquire()
        finally:
            estimate = self.waiting.pop(ticket, 0.0)
        nbytes, cores = _byte_count(texts), index_cores()
        # A batch that pays a cold load is not a sample of the indexing rate.
        warm = model_cache.peek_bge_m3_model() is not None
        started = time.monotonic()
        self.running = (estimate, started)

        def _settled(encode: asyncio.Future) -> None:
            self.running = None
            self.slot.release()
            if warm and not encode.cancelled() and encode.exception() is None:
                if encode.result() is not None:
                    _rate.learn(time.monotonic() - started, nbytes, cores)

        try:
            encode = asyncio.ensure_future(model_cache.aembed_texts(texts))
        except BaseException:
            self.running = None
            self.slot.release()
            raise
        encode.add_done_callback(_settled)
        return await asyncio.shield(encode)


_queues: Dict[asyncio.AbstractEventLoop, _IndexQueue] = {}


def _queue_for(loop: asyncio.AbstractEventLoop) -> _IndexQueue:
    # Not a WeakKeyDictionary: a queue's semaphore refers to its loop, which
    # would keep the key alive. A closed loop's queue is dropped instead.
    for closed in [other for other in _queues if other.is_closed()]:
        del _queues[closed]
    queue = _queues.get(loop)
    if queue is None:
        queue = _queues[loop] = _IndexQueue()
    return queue


def _queue() -> Optional[_IndexQueue]:
    try:
        return _queue_for(asyncio.get_running_loop())
    except RuntimeError:  # no running loop: nothing is queued
        return None


def _warm_up(queue: _IndexQueue) -> None:
    """Start loading BGE-M3 unless this loop already is."""
    from faultmaven.infrastructure.model_cache import model_cache

    if queue.warming is None or queue.warming.done():
        queue.warming = asyncio.ensure_future(
            asyncio.to_thread(model_cache.get_bge_m3_model, "warmup")
        )


def _work_ahead() -> Tuple[float, bool]:
    """Seconds of indexing before a new batch could start, and whether a model
    load is part of them (the model is not resident)."""
    from faultmaven.infrastructure.model_cache import model_cache

    queue = _queue()
    ahead = queue.remaining_seconds() if queue is not None else 0.0
    cold = model_cache.peek_bge_m3_model() is None
    return ahead + (EMBED_BATCH_LOAD_SECONDS if cold else 0.0), cold


def index_retry_after_seconds() -> int:
    """When indexing queued now, and a model load if one is due, should clear:
    a busy refusal's ``Retry-After``, read from the queue as it stands."""
    return max(math.ceil(_work_ahead()[0]), 1)


def index_budget_refusal(
    texts: List[str], *, budget_seconds: Optional[float]
) -> Optional[IndexBudgetRefused]:
    """The refusal for a batch that cannot finish inside ``budget_seconds``, or None.

    The budget is the CALLER's: the request window on the KB upload, update and
    verify paths, the turn's budget for evidence vectorization, None for a
    caller with no deadline, which nothing refuses and the guard alone bounds.
    413 when the batch alone exceeds it warm; 503 when the work queued ahead,
    or a model still to load (whose load this starts), leaves too little.
    """
    if budget_seconds is None:
        return None
    nbytes = _byte_count(texts)
    estimate = estimated_index_seconds(texts)
    if estimate > budget_seconds:
        seconds_per_byte = _rate.core_seconds_per_byte / index_cores()
        limit_kb = max(int(budget_seconds / seconds_per_byte / 1024), 0)
        return IndexBudgetRefused(
            "This document is too large to index in one request (about "
            f"{math.ceil(nbytes / 1024)} KB; the limit on this deployment is "
            f"about {limit_kb} KB). Split it into smaller documents and upload "
            "each."
        )
    ahead, cold = _work_ahead()
    if ahead + estimate <= budget_seconds:
        return None
    queue = _queue()
    if cold and queue is not None:
        _warm_up(queue)
    wait = max(math.ceil(ahead), 1)
    return IndexBudgetRefused(
        f"Indexing is {'warming up' if cold else 'busy with other documents'}, "
        f"so this document cannot be indexed within this request. Retry in "
        f"about {wait} seconds.",
        retry_after_seconds=wait,
    )


def check_index_budget(texts: List[str], *, budget_seconds: Optional[float]) -> None:
    """Raise :func:`index_budget_refusal`'s refusal, if there is one."""
    refusal = index_budget_refusal(texts, budget_seconds=budget_seconds)
    if refusal is not None:
        raise refusal


def indexing_status() -> Dict[str, Any]:
    """The indexing window and rate in force, for ``GET /admin/config/status``."""
    return {
        "request_window_seconds": int(request_window_seconds() or 0),
        "cores": index_cores(),
        "measured_core_seconds_per_byte": INDEX_CORE_SECONDS_PER_BYTE,
        "learned_core_seconds_per_byte": _rate.core_seconds_per_byte,
        "learned_samples": _rate.samples,
    }


async def embed_query_or_raise(
    query: str,
    *,
    subject: str,
    operation: str,
    log: Optional[logging.Logger] = None,
) -> List[float]:
    """Embed a search query with BGE-M3, raising rather than degrading.

    Args:
        query: The text to embed.
        subject: What is unavailable, in the caller's own words, for the
            message the model and the operator both see — e.g. "Knowledge base
            search", "Case evidence search". Each store states its own truth;
            no store gets to describe another's contents.
        operation: Call-site name for the log line (e.g. "search",
            "keyword_search").
        log: Logger to report on. Defaults to this module's.

    Returns:
        The 1024-dim BGE-M3 query vector.

    Raises:
        KnowledgeBaseError: The embedding model timed out
            (``KNOWLEDGE_EMBEDDER_TIMEOUT``) or could not be loaded
            (``KNOWLEDGE_EMBEDDER_UNAVAILABLE``). Never an empty result, and
            never a query issued in a different embedding space.

    The timeout bounds the *caller*, not the work: ``aembed_query`` runs the
    load on a worker thread via ``asyncio.to_thread``, which cannot be
    cancelled, so a timed-out load keeps running to completion in the
    background (and populates the cache, so a later call may find it warm).
    What this guarantees is that the search returns — not that the load stops.
    """
    from faultmaven.infrastructure.model_cache import model_cache

    log = log or logger

    try:
        query_embedding = await asyncio.wait_for(
            model_cache.aembed_query(query), timeout=EMBED_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        log.error(
            f"BGE-M3 embedding timed out after {EMBED_TIMEOUT_SECONDS}s "
            f"for {operation}"
        )
        raise KnowledgeBaseError(
            f"{subject} unavailable: embedding the query timed out after "
            f"{EMBED_TIMEOUT_SECONDS}s",
            error_code="KNOWLEDGE_EMBEDDER_TIMEOUT",
        )

    if query_embedding is None:
        log.error(f"BGE-M3 model unavailable for {operation}")
        raise KnowledgeBaseError(
            f"{subject} unavailable: the embedding model could not be loaded",
            error_code="KNOWLEDGE_EMBEDDER_UNAVAILABLE",
        )

    return query_embedding


async def embed_texts_or_raise(
    texts: List[str],
    *,
    subject: str,
    operation: str,
    log: Optional[logging.Logger] = None,
) -> List[List[float]]:
    """Embed a BATCH of texts for indexing, raising rather than degrading.

    The write-side counterpart to :func:`embed_query_or_raise`. It exists for
    the same reason that one does: the indexing path had its own hand-rolled
    ``if embeddings is None: raise``, which refused correctly but carried no
    time bound at all — ``aembed_texts`` handled "the model is unavailable" and
    not "the model never returns" (#953). One module owning both halves is what
    keeps the query and index paths from drifting into two policies.

    Args:
        texts: Chunk texts to embed. An empty list embeds to an empty list —
            callers reject unchunkable content before reaching here, because
            ``collection.add(embeddings=[], ...)`` errors downstream.
        subject: What is unavailable, in the caller's own words, for the
            message the operator sees.
        operation: Call-site name for the log line (e.g. "update_document").
        log: Logger to report on. Defaults to this module's.

    The encode waits its turn in the one indexing queue, and the bound covers
    the wait too: :func:`batch_embed_timeout` for this batch, plus twice the
    estimate of the work ahead of it. The batch joins the queue before this
    function first awaits, so a caller that ran :func:`check_index_budget`
    with nothing awaited in between was checked against the queue it joins.

    There is no opt-out. #953 asked whether the boot repair path should stay
    unbounded on the grounds that ``kb_init``'s per-boot chunk budget already
    governs it; it should not, because that budget is checked BETWEEN rows. It
    bounds how much work a boot starts, not how long one call may take, so a
    single non-returning load never reaches the next budget check — it hangs
    the awaited startup lifespan until the probe kills the pod. The repair loop
    already catches per-row exceptions and defers the row to the next boot, so
    a bound there costs a retry and buys the crashloop back.

    Returns:
        One 1024-dim BGE-M3 vector per input text, in order.

    Raises:
        KnowledgeBaseError: The embedding timed out
            (``KNOWLEDGE_EMBEDDER_TIMEOUT``) or the model could not be loaded
            (``KNOWLEDGE_EMBEDDER_UNAVAILABLE``). Never a short/empty vector
            list for a non-empty input — a caller that wrote those would index
            a document against someone else's chunks.

    Same caveat as the query path: the bound is on the CALLER, not the work.
    ``aembed_texts`` runs on a worker thread via ``asyncio.to_thread``, which
    cannot be cancelled, so a timed-out batch keeps embedding in the background
    (and warms the model cache for the retry). What this guarantees is that the
    caller returns — not that the encode stops.
    """
    log = log or logger

    queue = _queue_for(asyncio.get_running_loop())
    timeout = batch_embed_timeout(texts) + 2 * queue.remaining_seconds()
    ticket = object()
    queue.waiting[ticket] = estimated_index_seconds(texts)
    try:
        embeddings = await asyncio.wait_for(queue.run(ticket, texts), timeout=timeout)
    except asyncio.TimeoutError:
        kb = math.ceil(_byte_count(texts) / 1024)
        log.error(
            f"BGE-M3 batch embedding timed out after {timeout:.0f}s for "
            f"{operation} ({len(texts)} texts, {kb} KB)"
        )
        raise KnowledgeBaseError(
            f"{subject} unavailable: embedding {len(texts)} chunks ({kb} KB) "
            f"timed out after {timeout:.0f}s, twice its estimate at this "
            f"deployment's indexing rate plus a model load",
            error_code="KNOWLEDGE_EMBEDDER_TIMEOUT",
        )
    finally:
        queue.waiting.pop(ticket, None)

    if embeddings is None:
        log.error(f"BGE-M3 model unavailable for {operation}")
        raise KnowledgeBaseError(
            f"{subject} unavailable: the embedding model could not be loaded",
            error_code="KNOWLEDGE_EMBEDDER_UNAVAILABLE",
        )

    return embeddings

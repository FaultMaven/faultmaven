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

The write side is here for the same reason, and every indexing embed now
routes through it (#2055): ``embed_texts_or_raise`` bounds the KB indexer
(``KnowledgeService._index_document_in_vector_store``, the one #953 was about),
which runs its encode on the KB indexing slot (``infrastructure.index_slot``),
and the case evidence indexer (``core/preprocessing/vector_storage.py``, live
via the ``vectorize_file`` agent tool), which runs on the default executor. The
bound is sized from the batch's BGE-M3 token count, not its chunk count: a
chunk can hold 50 tokens or 3,000, and the per-chunk rule allowed a
97k-character runbook 302 s of the 373 s it took (#2055).

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
import os
from typing import Any, List, Optional, Sequence

from faultmaven.models.exceptions import KnowledgeBaseError

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

# One-shot flag: the byte fallback is logged once per process, not per embed.
_byte_fallback_logged = False


def index_cores() -> float:
    """The cores an encode gets: the container's CPU quota (a fraction counts as
    a fraction), else the node's core count, capped at ``INDEX_MAX_CORES``."""
    from faultmaven.infrastructure.model_cache import _cgroup_cpu_quota

    return min(_cgroup_cpu_quota() or float(os.cpu_count() or 1), INDEX_MAX_CORES)


def index_estimate_seconds(tokens: int) -> float:
    """Seconds a warm model takes to encode ``tokens`` BGE-M3 tokens here."""
    return max(tokens, 0) * INDEX_CORE_SECONDS_PER_TOKEN / index_cores()


def batch_embed_timeout(tokens: int) -> float:
    """The bound for embedding a batch of ``tokens`` BGE-M3 tokens.

    A cold load plus twice the measured estimate. Deliberately proportional to
    the work and NOT capped at some round number. The bound exists to turn
    "never returns" into "returns an error", not to enforce a latency SLA: the
    request window does that, up front (``infrastructure.index_slot``). A
    ceiling tighter than what a large document legitimately costs would make
    that document permanently un-editable, which trades a hang for a silent
    capability loss.

    Under #952 the two stores are left describing the same text on every path
    that RETURNS, including this one, since a timeout raises before any write.
    A process killed mid-swap is not covered by that, or by anything short of
    2PC.
    """
    return EMBED_BATCH_LOAD_SECONDS + 2.0 * index_estimate_seconds(tokens)


def _count_index_tokens_sync(texts: List[str]) -> Optional[int]:
    from faultmaven.infrastructure.model_cache import model_cache

    try:
        loaded = model_cache.get_bge_m3_tokenizer()
        if loaded is None:
            return None
        tokenizer, max_length = loaded
        if not texts:
            return 0
        encoded = tokenizer(
            texts, add_special_tokens=True, truncation=True, max_length=max_length
        )["input_ids"]
        counts = [len(ids) for ids in encoded]
    except Exception as exc:
        logger.debug("BGE-M3 token count failed: %s", type(exc).__name__)
        return None
    return sum(counts) if len(counts) == len(texts) else None


async def count_index_tokens(texts: Sequence[str]) -> Optional[int]:
    """BGE-M3 tokens in ``texts``, counted the way the encode sees them.

    The model's own tokenizer, special tokens included and each text truncated
    at the model's ``max_seq_length``, summed. Off the event loop. None when the
    tokenizer cannot load (``ModelCache.get_bge_m3_tokenizer``), which never
    touches the network.
    """
    return await asyncio.to_thread(_count_index_tokens_sync, list(texts))


async def _guard_tokens(texts: List[str], known: Optional[int]) -> int:
    """The token count the guard sizes its bound from: ``known``, else a count,
    else UTF-8 bytes plus the two special tokens per text. A token spans at
    least a byte of text (bar SentencePiece's word marker before a text's first
    word), so that is an upper bound to within a token per text, which the
    bound's 2x margin covers. A loose bound is safe for a hang bound; the
    fallback is logged once."""
    global _byte_fallback_logged
    if known is not None:
        return known
    counted = await count_index_tokens(texts)
    if counted is not None:
        return counted
    if not _byte_fallback_logged:
        _byte_fallback_logged = True
        logger.warning(
            "BGE-M3 tokenizer unavailable: the indexing time bound counts UTF-8 "
            "bytes instead of tokens, an upper bound"
        )
    return sum(len(text.encode("utf-8")) + 2 for text in texts)


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
    ticket: Optional[Any] = None,
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
        ticket: The KB indexing slot's ``IndexTicket``
            (``infrastructure.index_slot``, which imports this module):
            the encode runs on the slot, one at a time per host, and the bound
            also covers the guard bounds of the encodes queued ahead of it.
            None (evidence vectorization) runs on the default executor, bounded
            by its own estimate alone.

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
            (``KNOWLEDGE_EMBEDDER_TIMEOUT``), while queued behind other
            indexing or mid-encode, or the model could not be loaded
            (``KNOWLEDGE_EMBEDDER_UNAVAILABLE``). Never a short/empty vector
            list for a non-empty input — a caller that wrote those would index
            a document against someone else's chunks.

    Same caveat as the query path: the bound is on the CALLER, not the work.
    An encode that has started runs on its thread to completion, which nothing
    can cancel. A caller that gives up while still queued cancels its encode,
    so it never runs. A slot encode keeps the slot until its thread settles,
    whatever its caller did. What this guarantees is that the caller returns,
    not that the encode stops.
    """
    from faultmaven.infrastructure.model_cache import model_cache

    log = log or logger

    tokens = await _guard_tokens(texts, ticket.tokens if ticket is not None else None)
    timeout = batch_embed_timeout(tokens)
    future = None
    if ticket is None:
        bound = timeout
        work = model_cache.aembed_texts(texts)
    else:
        future, ahead = ticket.run(texts, guard_seconds=timeout)
        bound = timeout + ahead
        work = asyncio.wrap_future(future)
    try:
        embeddings = await asyncio.wait_for(work, timeout=bound)
    except asyncio.TimeoutError:
        # ``cancel`` succeeds only on an encode that never started.
        if future is not None and future.cancel():
            log.error(
                f"BGE-M3 batch embedding for {operation} waited {bound:.0f}s "
                f"for other indexing on this server and never started"
            )
            raise KnowledgeBaseError(
                f"{subject} unavailable: waited {bound:.0f}s for other indexing "
                f"on this server",
                error_code="KNOWLEDGE_EMBEDDER_TIMEOUT",
            )
        log.error(
            f"BGE-M3 batch embedding timed out after {bound:.0f}s for "
            f"{operation} ({len(texts)} texts, {tokens} tokens)"
        )
        raise KnowledgeBaseError(
            f"{subject} unavailable: embedding {len(texts)} chunks timed "
            f"out after {bound:.0f}s",
            error_code="KNOWLEDGE_EMBEDDER_TIMEOUT",
        )
    except asyncio.CancelledError:
        # A queued encode of a request that went away never runs.
        if future is not None:
            future.cancel()
        raise

    if embeddings is None:
        log.error(f"BGE-M3 model unavailable for {operation}")
        raise KnowledgeBaseError(
            f"{subject} unavailable: the embedding model could not be loaded",
            error_code="KNOWLEDGE_EMBEDDER_UNAVAILABLE",
        )

    return embeddings

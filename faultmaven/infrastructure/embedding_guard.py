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
paths, plus ``ChromaDBVectorStore``, whose ``search`` has no caller today (all
three ``IVectorStore.search`` call sites pass ``collection_name=``, which its
signature does not accept) and is covered so it is not a trap for the next one.

``search_by_text`` is why this is a module and not a method. It refused
correctly, with its own copy of the logic — and the copy had silently dropped
the time bound. A site that hand-rolls this is a bug even when its refusal looks
right.

The write side is here for the same reason. ``embed_texts_or_raise`` bounds the
BATCH embed that indexing does, which had refused an unavailable model correctly
in its own hand-rolled copy but carried no time bound at all — the asymmetry
#953 found.

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
from typing import List, Optional

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

# Per-text allowance on top of the load budget.
#
# Anchored to the worst case this repo has actually measured rather than picked:
# the pre-embedded KB pack exists because embedding its 1244 chunks on a
# CPU-limited pod takes "tens of minutes" (~1s/chunk — see
# ``_index_document_in_vector_store``'s ``prechunked`` docstring). This doubles
# that for headroom, so the bound only fires on work that is not progressing.
EMBED_BATCH_PER_TEXT_SECONDS = 2.0


def batch_embed_timeout(text_count: int) -> float:
    """The bound for embedding ``text_count`` texts in one batch.

    Deliberately proportional to the work and NOT capped at some round number.
    The bound exists to turn "never returns" into "returns an error" — not to
    enforce a latency SLA. A ceiling tighter than what a large document
    legitimately costs would make that document permanently un-editable, which
    trades a hang for a silent capability loss.

    The consequence is that a big enough document can outlast the caller's own
    client timeout. That is a bounded, visible failure: the client gives up, the
    server does not hang forever, and (#952) the document is left consistent
    either way.
    """
    return EMBED_BATCH_LOAD_SECONDS + EMBED_BATCH_PER_TEXT_SECONDS * max(text_count, 0)


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
    from faultmaven.infrastructure.model_cache import model_cache

    log = log or logger

    timeout = batch_embed_timeout(len(texts))
    try:
        embeddings = await asyncio.wait_for(
            model_cache.aembed_texts(texts), timeout=timeout
        )
    except asyncio.TimeoutError:
        log.error(
            f"BGE-M3 batch embedding timed out after {timeout}s for "
            f"{operation} ({len(texts)} texts)"
        )
        raise KnowledgeBaseError(
            f"{subject} unavailable: embedding {len(texts)} chunks timed "
            f"out after {timeout}s",
            error_code="KNOWLEDGE_EMBEDDER_TIMEOUT",
        )

    if embeddings is None:
        log.error(f"BGE-M3 model unavailable for {operation}")
        raise KnowledgeBaseError(
            f"{subject} unavailable: the embedding model could not be loaded",
            error_code="KNOWLEDGE_EMBEDDER_UNAVAILABLE",
        )

    return embeddings

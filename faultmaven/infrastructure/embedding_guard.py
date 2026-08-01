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
paths, plus ``ChromaDBVectorStore``, whose ``search`` has no caller today (the
two that call an ``IVectorStore`` pass ``collection_name=``, which its signature
does not accept) and is covered so it is not a trap for the next one.

``search_by_text`` is why this is a module and not a method. It refused
correctly, with its own copy of the logic — and the copy had silently dropped
the time bound. A site that hand-rolls this is a bug even when its refusal looks
right.

**Call this BEFORE entering** ``BaseExternalClient.call_external``. The
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
EMBED_TIMEOUT_SECONDS = 10.0


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

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
which runs its encode in a worker from the KB indexing slot
(``infrastructure.index_slot``, #2851), and the case evidence indexer
(``core/preprocessing/vector_storage.py``, live via the ``vectorize_file``
agent tool), whose encodes run on the evidence slot, in the KB slot's order,
never on the default executor. A KB bound is sized from the batch's BGE-M3
tokens, counted in a worker and priced by each chunk's length (#2920; an
evidence bound from its UTF-8 bytes, #2851), not its chunk count: a
chunk can hold 50 tokens or 3,000, and the per-chunk rule allowed a
97k-character runbook 302 s of the 373 s it took (#2055).

The two bounds are deliberately DIFFERENT numbers, and this module is where
that stays visible. A query may give up before a cold load finishes, because
the load continues in the background and the retry finds it warm; an index may
not, because it fails a write rather than a read. Same policy, same error
codes, different tolerance — see ``EMBED_TIMEOUT_SECONDS`` below and
``index_slot.EMBED_BATCH_LOAD_SECONDS``.

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
from typing import Any, List, Optional, Sequence

from faultmaven.infrastructure import index_slot, token_count_worker, worker_process
from faultmaven.infrastructure.model_cache import BGE_M3_MAX_SEQ_LENGTH, priced_tokens
from faultmaven.models.exceptions import EmbedderUnavailable, KnowledgeBaseError
from faultmaven.utils.exception_name import exception_log_name

logger = logging.getLogger(__name__)

# Bound on a single query embedding. This ADDS to the ChromaDB budget rather
# than sharing it, so a search's worst case is embed + query, not one 10s
# ceiling for both. Matched to call_external's 10s for consistency.
#
# This is SHORTER than a BGE-M3 load takes (about 15 s measured in the image at
# 2 CPUs, #2851; see ``ModelCache.peek_bge_m3_model``), and deliberately so: a
# search that gives up fast is recoverable — the load continues on its worker
# thread and warms the cache, so the retry succeeds. A search is cheap to
# repeat.
EMBED_TIMEOUT_SECONDS = 10.0

# The batch equivalent does NOT reuse that number, because a write is not cheap
# to repeat: a bound that fires during a HEALTHY cold load fails the write. So
# its constant term covers the load the query path is willing to skip
# (``index_slot.EMBED_BATCH_LOAD_SECONDS``, beside the measured rate the slot's
# admission shares).

# One-shot flag: the byte fallback is logged once per process, not per embed.
_byte_fallback_logged = False

#: Token counts at once, in workers under the admission rule (#2851): each
#: peaked at 336 MiB of address space in use counting 10 MB of runbook text.
TOKEN_COUNT_SLOTS = 2

#: The Rust tokenizer's threads in each count worker.
TOKEN_COUNT_THREADS = 2

#: ``RLIMIT_AS`` of a count worker. Counting 10 MB of pack text in the image,
#: its pool pinned, its VmPeak was 593 MiB at 2 CPUs and 621 MiB with no CPU
#: quota (#2851).
TOKEN_COUNT_MEMORY_BYTES = 1024 * 1024 * 1024

#: What the parent reads from a count worker: its count.
TOKEN_COUNT_OUTPUT_LIMIT_BYTES = 1024 * 1024

#: Each step of a count: spawn to ``ready`` (0.3-0.4 s measured), each
#: transfer, and the work: loading the tokenizer (1.0-1.1 s) and counting 10 MB
#: of pack text (4.3 s at 2 CPUs), with an order of magnitude of headroom.
TOKEN_COUNT_PHASES = worker_process.Phases(
    start=10.0, request=10.0, work=60.0, result=10.0
)

#: A count's hang bound: its steps' sum, which is also how long it waits for a
#: slot. Past it the count is unavailable; there is no busy answer.
TOKEN_COUNT_HANG_SECONDS = (
    TOKEN_COUNT_PHASES.start
    + TOKEN_COUNT_PHASES.request
    + TOKEN_COUNT_PHASES.work
    + TOKEN_COUNT_PHASES.result
)

#: The count worker. Always this value: tests point it at one of their own.
_TOKEN_COUNT_WORKER = token_count_worker.__name__

_count_slots_state: Optional[tuple] = None


def batch_embed_timeout(tokens: int) -> float:
    """The bound for embedding a batch of ``tokens`` priced BGE-M3 tokens.

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
    return (
        index_slot.EMBED_BATCH_LOAD_SECONDS
        + 2.0 * index_slot.index_estimate_seconds(tokens)
    )


async def count_index_tokens(
    texts: Sequence[str], *, principal: str
) -> Optional[tuple[int, int]]:
    """BGE-M3 tokens in ``texts``, counted the way the encode sees them, and
    priced by each text's length (#2920), as ``(tokens, priced)``, in a
    ``token_count_worker`` its phase bounds kill (Principle 14, #2851).

    The worker runs under ``TOKEN_COUNT_SLOTS`` slots keyed by ``principal``,
    the user who asked. A count waits for a slot for its hang bound at most.
    None when the worker gave no count: any end but an answer, or an answer
    that is not two decimal counts, the priced one at least the count.

    Raises:
        EmbedderUnavailable: ``queued_too_long``, no slot freed within the
            wait (#2461).
    """
    global _count_slots_state
    if not isinstance(principal, str) or not principal:
        raise ValueError("a token count runs for the user who asked: none given")
    texts = list(texts)
    if not texts:
        return 0, 0
    _count_slots_state = worker_process.loop_slots(
        _count_slots_state, TOKEN_COUNT_SLOTS
    )
    slots = _count_slots_state[1]
    slot_key = worker_process.admission_principal(principal)
    try:
        async with asyncio.timeout(TOKEN_COUNT_HANG_SECONDS):
            await slots.acquire(*slot_key)
    except TimeoutError:
        logger.warning("BGE-M3 token count found no slot free")
        raise EmbedderUnavailable(
            "Indexing unavailable: no token count slot freed within the wait",
            reason="queued_too_long",
        ) from None
    try:
        end = await worker_process.run(
            _TOKEN_COUNT_WORKER,
            {"threads": TOKEN_COUNT_THREADS},
            memory_bytes=TOKEN_COUNT_MEMORY_BYTES,
            cpu_seconds=math.ceil(TOKEN_COUNT_PHASES.work * TOKEN_COUNT_THREADS) + 10,
            output_limit_bytes=TOKEN_COUNT_OUTPUT_LIMIT_BYTES,
            phases=TOKEN_COUNT_PHASES,
            parts=tuple(texts),
        )
    except worker_process.WorkerFailed as e:
        logger.warning("BGE-M3 token count did not answer: %s", exception_log_name(e))
        return None
    finally:
        slots.release(*slot_key)
    if end.result is None:
        logger.warning(
            "BGE-M3 token count did not answer: ended in %s, exit %s, timed out %s",
            end.phase,
            end.code,
            end.timed_out,
        )
        return None
    fields = end.result[0].split(" ") if len(end.result) == 1 else []
    if len(fields) == 2 and all(field.isdecimal() for field in fields):
        tokens, priced = int(fields[0]), int(fields[1])
        return (tokens, priced) if priced >= tokens else None
    return None


async def _guard_tokens(texts: List[str], ticket: Optional[Any]) -> int:
    """The priced tokens the guard sizes its bound from: a KB ticket's, else a
    count's under its principal; else each text's UTF-8 bytes plus 2, priced on
    a KB ticket up to the cap, plain for evidence (Q2). Each token spans a byte
    or more (bar the word marker before a text's first word): an upper bound to within a
    token per text, which the bound's 2x margin covers. A loose bound is safe
    for a hang bound. An evidence encode (no ticket) always takes it, so it
    never waits behind KB counts for a slot (#2851); a KB count that gave no
    answer is logged where it ended, and this fallback once."""
    global _byte_fallback_logged
    if ticket is not None and ticket.tokens is not None:
        return ticket.tokens
    if ticket is not None:
        try:
            counted = await count_index_tokens(texts, principal=ticket.principal)
        except EmbedderUnavailable:  # no count slot freed: logged there
            counted = None
        if counted is not None:
            return counted[1]
        if not _byte_fallback_logged:
            _byte_fallback_logged = True
            logger.warning(
                "A KB token count gave no answer: the indexing time bound counts "
                "UTF-8 bytes instead of tokens, an upper bound"
            )
    tokens = [len(text.encode("utf-8")) + 2 for text in texts]
    capped = [priced_tokens(min(n, BGE_M3_MAX_SEQ_LENGTH)) for n in tokens]
    return sum(capped) if ticket is not None else sum(tokens)


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
    time bound at all — the batch embed handled "the model is unavailable" and
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
            (``infrastructure.index_slot``): the encode runs in a worker from
            that slot, one at a time per host. None (evidence vectorization)
            takes a ticket on the evidence slot, one encode at a time per
            host, in this process, its bound sized by bytes. Either way
            the bound also covers the guard bounds of the encodes queued ahead.

    There is no opt-out. #953 asked whether the boot repair path should stay
    unbounded on the grounds that ``kb_init``'s per-boot chunk budget already
    governs it; it should not, because that budget is checked BETWEEN rows. It
    bounds how much work a boot starts, not how long one call may take, so a
    single non-returning load never reaches the next budget check — it hangs
    the awaited startup lifespan until the probe kills the pod. The repair
    catches each row's exceptions and defers the row to the next boot, so a
    bound there costs a retry and buys the crashloop back.

    Returns:
        One 1024-dim BGE-M3 vector per input text, in order.

    Raises:
        EmbedderUnavailable: ``KNOWLEDGE_EMBEDDER_TIMEOUT`` while queued behind
            other indexing (``queued_too_long``) or mid-encode
            (``encode_timeout``); ``KNOWLEDGE_EMBEDDER_UNAVAILABLE`` when the
            model could not be loaded (``model_unavailable``) or the encode
            ended without vectors (``worker_failed``). Never a short/empty
            vector list for a non-empty input — a caller that wrote those would
            index a document against someone else's chunks.

    A KB encode runs in a worker whose phases end before this bound
    (``index_slot.REAP_MARGIN_SECONDS``): past them it is killed and this
    raises ``KNOWLEDGE_EMBEDDER_TIMEOUT``, and a caller that goes away kills
    it too (#2851). An evidence encode runs on the order's thread, which
    nothing can cancel: there the bound is on the CALLER, and the encode keeps
    the order until its thread settles. Either way a caller that gives up while
    its encode is still queued, or still waiting for the host lock, stops it,
    so it never runs.
    """
    log = log or logger

    tokens = await _guard_tokens(texts, ticket)
    timeout = batch_embed_timeout(tokens)
    if ticket is None:
        async with index_slot.evidence_slot.admit_forced(texts, tokens) as evidence:
            embeddings = await _encode_on(
                evidence, texts, timeout, subject, operation, log
            )
    else:
        embeddings = await _encode_on(ticket, texts, timeout, subject, operation, log)

    if embeddings is None:
        log.error(f"BGE-M3 batch embedding for {operation} ended without vectors")
        raise EmbedderUnavailable(
            f"{subject} unavailable: the encode ended without vectors",
            reason="worker_failed",
        )

    return embeddings


async def _encode_on(
    ticket: Any,
    texts: List[str],
    timeout: float,
    subject: str,
    operation: str,
    log: logging.Logger,
) -> Optional[List[List[float]]]:
    """Run the encode on ``ticket``'s slot and wait for it, bounded by its own
    guard plus the guard bounds still outstanding ahead of it."""
    future, ahead = ticket.run(texts, guard_seconds=timeout)
    bound = timeout + ahead
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=bound)
    except asyncio.TimeoutError:
        if ticket.abandon():  # still queued, or still waiting for the host lock
            log.error(
                f"BGE-M3 batch embedding for {operation} waited {bound:.0f}s "
                f"for other indexing on this server and never started"
            )
            raise EmbedderUnavailable(
                f"{subject} unavailable: waited {bound:.0f}s for other indexing "
                f"on this server",
                reason="queued_too_long",
            )
        log.error(
            f"BGE-M3 batch embedding timed out after {bound:.0f}s for "
            f"{operation} ({len(texts)} texts)"
        )
        raise EmbedderUnavailable(
            f"{subject} unavailable: embedding {len(texts)} chunks timed "
            f"out after {bound:.0f}s",
            reason="encode_timeout",
        )
    except index_slot.ModelNotLoaded:
        log.error(f"BGE-M3 model unavailable for {operation}")
        raise EmbedderUnavailable(
            f"{subject} unavailable: the embedding model could not be loaded",
            reason="model_unavailable",
        )
    except asyncio.CancelledError:
        ticket.abandon()  # a request that went away: its queued encode never runs
        raise

"""
Runbook Knowledge Base - Similarity Search over the Live KB Collection

Runbook deduplication ("does the KB already hold a runbook like this?") reads
what the live KB writer writes. Every published runbook — the shipped pack
(``bootstrap/kb_init.py``), uploads (``KnowledgeService.upload_document``), and
the case-conversion flywheel (``ConversionService``) — reaches ChromaDB through
``KnowledgeService._index_document_in_vector_store`` as N chunk rows carrying
``document_type``, ``scope``, ``owner_id``, ``title``, ``parent_document_id``.
This class queries those rows directly: ``document_type == "runbook"`` is the
artifact discriminator and the caller-supplied KB scope filter (built with
``build_kb_scope_filter``) is the visibility predicate.

Until fm#1030 this class filtered on ``report_type == "runbook"``, a key that
only its own — dead — write half stamped. No live writer wrote it, so every
dedup query could only return ``[]`` and every resolution proposed a new
runbook regardless of what the KB held. The write half is gone: publishing IS
``ingest_runbook``, and dedup reads the rows publishing writes.

Architecture Reference: docs/architecture/knowledge-and-ai/runbook-dedup.md
"""

import logging
from typing import Any, Dict, List

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.embedding_guard import embed_query_or_raise
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.report import RunbookMatch

logger = logging.getLogger(__name__)

#: Error code for "the closest candidates could not be read". Named once and
#: imported by both consumers rather than spelled as a literal in three files:
#: the whole point of the code is that callers branch on it, so a typo on one
#: side silently restores the misleading generic message.
RESULTS_UNREADABLE_CODE = "RUNBOOK_RESULTS_UNREADABLE"


class RunbookKnowledgeBase(BaseExternalClient):
    """
    Similarity search for published runbooks, used by both dedup callers
    (the terminal-transition suggestion and the report-recommendation route).

    Read-only. Runbooks are published through ``KnowledgeService.ingest_runbook``
    — this class has no write half, so there is no second writer whose metadata
    can drift from what this reader expects.
    """

    MIN_SIMILARITY_THRESHOLD = 0.65  # Minimum 65% similarity

    #: One runbook is N chunk rows, so a chunk-level fetch of ``top_k`` can be
    #: entirely filled by ONE long runbook — the hazard the KB pre-fetch also
    #: guards against (``milestone_engine._search_kb_for_runbooks``). Fetch
    #: deeper, then collapse to distinct runbooks.
    CHUNK_FETCH_MULTIPLIER = 3

    def __init__(self, vector_store: ChromaDBVectorStore):
        """
        Initialize runbook knowledge base.

        Args:
            vector_store: The KB vector store (``faultmaven_kb`` collection —
                the same collection the live writer populates; runbooks have
                no collection of their own).
        """
        super().__init__(
            client_name="runbook_knowledge_base",
            service_name="RunbookKB",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30,
        )

        self.vector_store = vector_store

    @classmethod
    def over_kb_collection(cls, kb_chromadb_client: Any) -> "RunbookKnowledgeBase":
        """A dedup reader bound BY CONSTRUCTION to the KB writer's collection.

        The production KB writer is ``KnowledgeVectorStore.add_documents``,
        which targets the hardcoded ``KB_COLLECTION`` — NOT the
        settings-derived collection name a bare ``ChromaDBVectorStore``
        binds. A reader bound to the settings name diverges from the writer
        the moment ``CHROMADB_COLLECTION`` is overridden, and a
        reader/writer split silently reinstates the empty-result dedup this
        class was fixed for (fm#1030). Binding the reader to the same
        constant the writer uses closes that by construction rather than by
        coincidence of defaults.
        """
        from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
            KB_COLLECTION,
        )

        return cls(
            vector_store=ChromaDBVectorStore(
                client=kb_chromadb_client, collection_name=KB_COLLECTION
            )
        )

    async def search_by_text(
        self,
        query_text: str,
        *,
        scope_filter: Dict[str, Any],
        top_k: int = 5,
        min_similarity: float = MIN_SIMILARITY_THRESHOLD,
    ) -> List[RunbookMatch]:
        """Find published runbooks similar to ``query_text``, within a scope.

        The text entry point to :meth:`search_runbooks`: embeds the query with
        BGE-M3 (off the event loop) and delegates. One implementation serves
        both dedup callers so the refusal semantics cannot drift apart (#944:
        both callers previously concluded "no similar runbook exists" from
        searches that never ran).

        Args:
            query_text: Natural-language description of the problem.
            scope_filter: The principal's KB read scope, built by the CALLER
                with ``build_kb_scope_filter`` (global ∪ owned ∪ team-shared).
                Which principal governs is the caller's decision: the API
                route scopes on the requester, the engine on the case owner.

        Raises:
            KnowledgeBaseError: If the embedding model is unavailable or its
                load exceeds ``EMBED_TIMEOUT_SECONDS``; also anything
                :meth:`search_runbooks` raises, which this method delegates to
                and does not catch (``RUNBOOK_SEARCH_UNSCOPED``,
                ``RUNBOOK_SEARCH_FAILED``, ``RUNBOOK_RESULTS_UNREADABLE``). The
                caller must NOT render any of them as "no similar runbooks
                found" — that is the affirmative negative this method exists to
                end.
        """
        # Refuse an unscoped search BEFORE spending the embedding budget.
        # search_runbooks carries the same guard for callers that arrive with
        # a vector; both refuse rather than degrade (#944).
        self._require_scope(scope_filter)

        # The shared embed guard, with a time bound. Both callers sit under a
        # caller-side deadline they can exhaust: the runbook dedup in
        # terminal_transitions runs inside the 120s per-turn budget, and the
        # report-recommendation endpoint under the HTTP/ingress budget. A cold
        # BGE-M3 load is 60-120s, so an unbounded await here consumes that
        # whole budget instead of refusing in 10s.
        #
        # Consequence, accepted: a terminal transition during a cold start
        # refuses, and the resolution carries the "dedup could not run" caveat
        # rather than blocking on the load. The timed-out load does run to
        # completion and warm the cache, so the refusals stop once it finishes
        # — but not only the first is affected: ``get_bge_m3_model`` loads under
        # a ``threading.Lock``, so every embed attempt arriving during the
        # window blocks on that lock and exhausts its own 10s bound too.
        query_embedding = await embed_query_or_raise(
            query_text,
            subject="Runbook similarity search",
            operation="runbook_search_by_text",
            log=logger,
        )

        return await self.search_runbooks(
            query_embedding=query_embedding,
            scope_filter=scope_filter,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    @staticmethod
    def _require_scope(scope_filter: Any) -> None:
        """Refuse a falsy/absent scope filter rather than querying unscoped.

        A correctly built scope filter always contains at least the global arm
        (``build_kb_scope_filter`` cannot return an empty mapping), so a falsy
        one means the caller never resolved whose scope governs. Searching
        anyway would resolve by similarity alone — an id-free path with no
        predicate keeping one principal's personal runbooks out of another's
        results. Refusing is also what keeps a refused search from being
        reported as a finding: every caller reads ``[]`` as "the KB holds
        nothing similar" and turns it into "generate a new runbook" (#944).
        """
        if not scope_filter:
            logger.warning("Refusing unscoped runbook search: no scope filter")
            raise KnowledgeBaseError(
                "Runbook similarity search requires the searching principal's "
                "KB read scope; refusing to search without one",
                error_code="RUNBOOK_SEARCH_UNSCOPED",
            )

    async def search_runbooks(
        self,
        query_embedding: List[float],
        *,
        scope_filter: Dict[str, Any],
        top_k: int = 5,
        min_similarity: float = MIN_SIMILARITY_THRESHOLD,
    ) -> List[RunbookMatch]:
        """
        Search published runbooks by semantic similarity, within a scope.

        Queries the live KB chunk rows (``document_type == "runbook"`` under
        ``scope_filter``), then collapses chunks to distinct runbooks: one
        runbook is N chunk rows, so results are grouped by
        ``parent_document_id`` taking the MAX chunk similarity per runbook,
        sorted descending, truncated to ``top_k``.

        Args:
            query_embedding: Query embedding vector (1024-dim for BGE-M3)
            scope_filter: The principal's KB read scope (see
                :meth:`search_by_text`); REQUIRED and refused when falsy
            top_k: Number of distinct runbooks to return (default 5)
            min_similarity: Minimum chunk similarity threshold (default 0.65)

        Returns:
            List of RunbookMatch objects sorted by similarity score (descending)

        Raises:
            KnowledgeBaseError: ``RUNBOOK_SEARCH_UNSCOPED`` if no scope filter
                was supplied, ``RUNBOOK_SEARCH_FAILED`` if the vector query
                itself failed, or ``RUNBOOK_RESULTS_UNREADABLE`` if rows matched
                but the best of them carried no usable identity. None may be
                rendered as "no similar runbooks found": callers turn an empty
                list into "generate a new runbook", so a search that could not
                be completed or could not be read must not answer as one that
                was (#944).
        """
        self._require_scope(scope_filter)

        # Chunk-level fetch depth — see CHUNK_FETCH_MULTIPLIER.
        fetch_k = top_k * self.CHUNK_FETCH_MULTIPLIER

        # ``$and`` needs >= 2 operands (ChromaDB >= 1.0 also rejects the
        # multi-key implicit-AND form outright), which the document_type
        # operand plus the scope filter always satisfy. ``scope_filter``
        # composes as ONE operand whether it is a bare single condition (the
        # global-only case) or an ``$or`` of arms.
        where_clause: Dict[str, Any] = {
            "$and": [{"document_type": "runbook"}, scope_filter]
        }

        async def _query():
            """The external call, and ONLY the external call.

            Result parsing used to live in here too. It must not: parsing is
            pure CPU that cannot fail transiently, but everything inside this
            function runs under ``call_external``'s retry-and-circuit-breaker
            policy. A deterministic parse failure was therefore retried three
            times with backoff and recorded as three RunbookKB failures — and
            five of those open this client's breaker, blocking the healthy
            reads that share it (#912). Retries belong to the query; the parse
            gets none.
            """
            # A failure here is NOT "no similar runbooks" — returning [] made
            # every caller conclude the KB holds nothing similar and propose a
            # new runbook, so duplicates accumulated on every resolution
            # (#944). Surfaced as a typed error so a caller that wants to
            # tolerate it must say so.
            try:
                results = await self.vector_store.query_by_embedding(
                    query_embedding=query_embedding,
                    where=where_clause,
                    top_k=fetch_k,
                )
            except Exception as e:
                logger.error(f"ChromaDB runbook query failed: {e}")
                raise KnowledgeBaseError(
                    f"Runbook similarity search failed: {e}",
                    error_code="RUNBOOK_SEARCH_FAILED",
                ) from e

            return results

        results = await self.call_external(
            operation_name="search_runbooks",
            call_func=_query,
            timeout=5.0,  # 5 seconds for vector search
            retries=2,
        )

        return self._parse(results, top_k=top_k, min_similarity=min_similarity)

    def _parse(
        self, results: Any, *, top_k: int, min_similarity: float
    ) -> List[RunbookMatch]:
        """Collapse chunk rows into distinct runbook matches, honestly.

        Chunk scores that cleared the similarity threshold but could NOT be
        turned into a match — a missing identity key, or a value the model
        refuses — are tracked, and the whole result set is refused when the
        best of them outranks every readable match. The caller does not
        consume the list, it consumes the TOP match, so what makes an answer
        trustworthy is not "did we read most rows" but "did we read the best
        one" (#944).
        """
        unreadable_scores: List[float] = []
        # Best chunk per runbook. One runbook = N chunk rows; the runbook's
        # similarity is its best chunk's (best-chunk-max detects OVERLAP, which
        # is why the ``reuse`` verdict is withheld upstream — see
        # docs/architecture/knowledge-and-ai/runbook-dedup.md).
        best_by_item: Dict[str, RunbookMatch] = {}

        if not results or "ids" not in results or not results["ids"]:
            logger.debug("No runbooks found matching query")
            return []

        # ChromaDB returns nested lists: [[id1, id2, ...]]
        ids_list = results["ids"][0] if results["ids"] else []
        distances_list = results["distances"][0] if results.get("distances") else []
        metadatas_list = results["metadatas"][0] if results.get("metadatas") else []

        for i, chunk_id in enumerate(ids_list):
            # Convert distance to similarity score. Embeddings are normalized,
            # so cosine distance ≈ 2(1 - similarity).
            distance = distances_list[i] if i < len(distances_list) else 1.0
            similarity = max(0.0, 1.0 - (distance / 2.0))

            # Rows read successfully and then correctly discarded as
            # dissimilar are neither matches nor unreadable.
            if similarity < min_similarity:
                continue

            metadata = metadatas_list[i] if i < len(metadatas_list) else {}

            # Identity is read with ``[]``, never ``.get(default)`` — the #912
            # rule: a default does not read as "missing", it reads as a fact.
            # The live writer stamps ``parent_document_id`` and ``scope`` on
            # every chunk, but ``title`` is truthiness-gated at write
            # (``VectorMetadata.to_chroma_metadata``), so an empty-titled
            # document genuinely produces matched chunks with no readable
            # identity: the unreadable class shrinks but does not vanish.
            try:
                match = RunbookMatch(
                    item_id=metadata["parent_document_id"],
                    title=metadata["title"],
                    scope=metadata["scope"],
                    similarity_score=similarity,
                )
            except Exception as e:
                unreadable_scores.append(similarity)
                logger.warning(
                    f"Runbook chunk {chunk_id} carries no usable identity "
                    f"({e!r}), skipping it",
                    extra={
                        "chunk_id": chunk_id,
                        "similarity": similarity,
                        "metadata_keys": sorted(metadata),
                    },
                )
                continue

            current = best_by_item.get(match.item_id)
            if current is None or match.similarity_score > current.similarity_score:
                best_by_item[match.item_id] = match

        matches = sorted(
            best_by_item.values(), key=lambda m: m.similarity_score, reverse=True
        )[:top_k]

        # Refuse when an unread candidate could have been the answer.
        #
        # "Some rows were skipped" is not by itself a reason to fail — a
        # skipped row that scores below a match we DID read cannot change
        # the verdict, because every caller consumes the top match and
        # nothing else. What is not survivable is an unread candidate
        # scoring STRICTLY ABOVE the best readable one: the caller then gets
        # a similarity number presented as the best available, computed from
        # a set whose actual best was discarded. Both dedup callers turn
        # that into "generate a new runbook", stating a fact about the KB's
        # contents that the search did not establish — the #944 collapse,
        # reached through the partial case rather than the empty one.
        #
        # A TIE is deliberately not a refusal. Every caller decides on the
        # top score, so at equal similarity the readable row produces the
        # same verdict and names a runbook that really does exist at that
        # score — nothing in the answer is unestablished. Refusing there
        # would fail a correct answer over a row that could not have
        # changed it.
        #
        # The empty case needs no branch of its own: the sentinel makes
        # "nothing readable" the degenerate instance of the same rule, where
        # any unread candidate is strictly the best.
        best_readable = matches[0].similarity_score if matches else -1.0
        unreadable = len(unreadable_scores)
        best_unreadable = max(unreadable_scores, default=0.0)
        if unreadable and best_unreadable > best_readable:
            # States only what is established. ``unreadable`` counts
            # candidates that MET the threshold and could not be read — not
            # the number of rows matched, since rows read successfully and
            # then correctly discarded as dissimilar are neither.
            raise KnowledgeBaseError(
                f"{unreadable} runbook chunk(s) scoring up to "
                f"{best_unreadable:.0%} met the similarity threshold but "
                f"could not be read, outranking every readable match; "
                f"refusing to report a best match from a result set whose "
                f"strongest candidate was unreadable. Re-index the affected "
                f"rows — retrying the search cannot clear this.",
                error_code=RESULTS_UNREADABLE_CODE,
            )

        logger.info(
            f"Found {len(matches)} similar runbooks",
            extra={
                "metric": "runbook.dedup_top_similarity",
                "top_similarity": (matches[0].similarity_score if matches else 0.0),
                "min_threshold": min_similarity,
            },
        )

        return matches

"""
Knowledge Base Vector Store

ChromaDB adapter for the unified KB collection (faultmaven_kb).
All scopes (global, team, personal) share one collection with metadata-based
scope filtering. This is distinct from CaseVectorStore, which prefixes "case_"
and is designed for ephemeral per-case evidence collections.

Scope safety invariant: queries against faultmaven_kb MUST include a scope
filter in the `where` clause. Unscoped queries are rejected with ValueError
to prevent cross-tenant data leaks.

Hybrid search: Two-stage retrieval + reranking pipeline:
  Stage 1 — Recall: Casts a wide net with two parallel retrievals:
    a) Pure vector search (semantic similarity)
    b) Keyword-constrained vector search (exact identifier matches)
  Stage 2 — Rerank: Scores candidates using multiple signals:
    - Term overlap (how many query terms appear in the chunk)
    - Metadata match (domain/service alignment with query context)
    - Status signal (lifecycle: verified > in-review > draft > stale > deprecated)
    - Staleness penalty (older content scores lower)
    - Scope preference (personal > team > global tiebreaking)
"""

import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from chromadb.errors import NotFoundError

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.embedding_guard import embed_query_or_raise
from faultmaven.infrastructure.vector_similarity import (
    cosine_from_chroma_distance,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.vector_metadata import VectorMetadata

logger = logging.getLogger(__name__)

# Minimum keyword length for extraction
MIN_KEYWORD_LENGTH = 3

# Patterns that indicate identifier-like tokens — these benefit most from
# exact keyword matching because embeddings often miss them
IDENTIFIER_PATTERNS = [
    re.compile(r"[A-Z]{2,}[-_]\d+"),  # Error codes: ERR-500, SQLSTATE-42000
    re.compile(r"\d{3,}"),  # Status codes: 503, 404
    re.compile(r"[a-z]+-[a-z]+-[a-z]+"),  # Service names: payment-gateway-svc
    re.compile(r"[A-Z][a-z]+[A-Z]"),  # CamelCase: CrashLoopBackOff
    re.compile(r"/[a-z/_-]+"),  # File paths: /var/log/syslog
    re.compile(r"\w+\.\w+\.\w+"),  # Dotted names: java.lang.OutOfMemoryError
]

# Scope tier ordering for tiebreaking (lower = higher priority)
SCOPE_PRIORITY = {"personal": 0, "team": 1, "global": 2}

# Reranker weights — how much each signal contributes to final score
# Default reranker weights (natural language queries)
RERANK_WEIGHT_VECTOR = 0.40
RERANK_WEIGHT_TERM_OVERLAP = 0.25
RERANK_WEIGHT_METADATA = 0.20
RERANK_WEIGHT_FRESHNESS = 0.15

# Shifted weights for identifier-heavy queries (error codes, service names)
RERANK_WEIGHT_VECTOR_ID = 0.25
RERANK_WEIGHT_TERM_OVERLAP_ID = 0.40
RERANK_WEIGHT_METADATA_ID = 0.20
RERANK_WEIGHT_FRESHNESS_ID = 0.15

# Staleness decay: score = 1 / (1 + days/HALF_LIFE)
STALENESS_HALF_LIFE_DAYS = 365

# Collection name that requires scope filtering
KB_COLLECTION = "faultmaven_kb"

# Keys that indicate a scope filter is present in a where clause. Team
# visibility is no longer a metadata key — it is resolved to an id allowlist
# (parent_document_id ∈ {...}) from the share table (ADR-013 §D4). The KB read
# filter always carries `scope` (the global arm), so the guard still bites.
SCOPE_FILTER_KEYS = {"scope", "owner_id", "organization_id", "parent_document_id"}

# Common English stop words for term overlap scoring
_STOP_WORDS = frozenset(
    {
        "the",
        "is",
        "at",
        "which",
        "on",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "with",
        "to",
        "for",
        "of",
        "not",
        "no",
        "can",
        "how",
        "what",
        "why",
        "when",
        "where",
        "do",
        "does",
        "did",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "be",
        "been",
        "being",
        "this",
        "that",
        "from",
        "by",
        "it",
        "its",
        "my",
        "our",
        "your",
        "their",
    }
)


def _flatten_filter_keys(where: dict) -> set:
    """Extract all filter keys from a ChromaDB where clause, including nested $or/$and."""
    keys = set()
    for k, v in where.items():
        if k in ("$or", "$and") and isinstance(v, list):
            for clause in v:
                if isinstance(clause, dict):
                    keys.update(_flatten_filter_keys(clause))
        else:
            keys.add(k)
    return keys


def _tokenize(text: str) -> List[str]:
    """Split text into lowercased tokens, stripping punctuation."""
    return [
        t
        for t in re.split(r"[\s,;:!?.()\[\]{}<>\"']+", text.lower())
        if t and len(t) >= 2
    ]


class KnowledgeVectorStore(BaseExternalClient):
    """Vector store for the unified KB collection (faultmaven_kb).

    All KB scopes (global, team, personal) share one ChromaDB collection.
    Scope isolation is enforced via metadata filtering at query time.

    **Scope safety invariant:** Queries against faultmaven_kb MUST include
    a scope filter (one of ``SCOPE_FILTER_KEYS``) in the `where` clause.
    Unscoped queries raise ValueError to prevent cross-tenant data leaks.
    This converts a fail-open risk into a fail-closed guarantee.

    Case evidence collections (case_{case_id}) are exempt from this check
    since they are already scoped by case ownership.
    """

    def __init__(self, client):
        """Initialize knowledge vector store.

        Args:
            client: ChromaDB client instance (PersistentClient or HttpClient).
        """
        super().__init__(
            client_name="knowledge_vector_store",
            service_name="KnowledgeVectorStore",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=60,
        )

        self.client = client
        self.logger.info("KnowledgeVectorStore initialized (permanent KB collections)")

    def _get_or_create_collection(self, collection_name: str):
        """Get or create a KB collection by exact name."""
        metadata = {
            "type": "knowledge_base",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            collection = self.client.get_or_create_collection(
                name=collection_name, metadata=metadata
            )
            self.logger.debug(f"KB collection ready: {collection_name}")
            return collection
        except Exception as e:
            self.logger.error(
                f"Failed to get/create KB collection {collection_name}: {e}"
            )
            raise

    def _enforce_scope_invariant(
        self, collection_name: str, where: Optional[Dict[str, Any]]
    ) -> None:
        """Reject unscoped queries against the KB collection.

        The unified KB collection contains documents from all scopes
        (global, team, personal). Querying it without a scope filter
        would leak data across tenants. This invariant makes that
        impossible — unscoped queries crash loudly instead of silently
        returning cross-tenant data.

        Case evidence collections (case_*) are exempt.
        """
        if collection_name != KB_COLLECTION:
            return  # Not the KB collection — no scope check needed

        if not where:
            raise ValueError(
                f"KB queries require scope filter — refusing unscoped search "
                f"on '{collection_name}'. Pass a where clause containing one "
                f"of {SCOPE_FILTER_KEYS}."
            )

        filter_keys = _flatten_filter_keys(where)
        if not filter_keys & SCOPE_FILTER_KEYS:
            raise ValueError(
                f"KB queries require scope filter — where clause {where} "
                f"does not contain any of {SCOPE_FILTER_KEYS}. "
                f"Refusing unscoped search on '{collection_name}'."
            )

    async def _embed_query_or_raise(self, query: str, operation: str) -> List[float]:
        """Embed a query with BGE-M3, raising rather than degrading to ``[]``.

        An unavailable embedder is NOT an empty knowledge base. See
        :mod:`faultmaven.infrastructure.embedding_guard`, which holds the one
        implementation every vector store shares — the case evidence store had
        its own, differently-wrong answer to this until #941, and the point of
        a single implementation is that they cannot drift apart again.
        """
        return await embed_query_or_raise(
            query,
            subject="Knowledge base search",
            operation=operation,
            log=self.logger,
        )

    async def search(
        self,
        collection_name: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None,
        query_embedding: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar documents in a KB collection.

        Args:
            collection_name: Exact ChromaDB collection name (no prefix added).
            query: Search query text.
            k: Number of results to return.
            where: ChromaDB metadata filters. **Required** for faultmaven_kb
                   collection (must include a SCOPE_FILTER_KEYS filter).
            query_embedding: Precomputed vector for ``query``. Supplied by
                   callers that already embedded the same text (see
                   :meth:`hybrid_search`) so one query is not embedded twice.
                   When None the query is embedded here, as before.

        Returns:
            List of matching documents with content, metadata, and scores.

        Raises:
            ValueError: If querying faultmaven_kb without a scope filter.
            KnowledgeBaseError: If the embedding model is unavailable. NOT an
                empty result — see :meth:`_embed_query_or_raise`.
        """
        self._enforce_scope_invariant(collection_name, where)

        # Embed BEFORE entering call_external: the embedding is a local model
        # call, not the ChromaDB round-trip that the retry/circuit-breaker
        # policy exists for. Raising in the wrapper would burn the full retry
        # budget on a model that cannot recover in seconds, and would charge
        # ChromaDB's circuit breaker for an embedder fault.
        if query_embedding is None:
            query_embedding = await self._embed_query_or_raise(query, "search")

        async def _search_wrapper():
            collection = self._get_or_create_collection(collection_name)

            query_params = {
                "query_embeddings": [query_embedding],
                "n_results": k,
                "include": ["documents", "metadatas", "distances"],
            }

            if where:
                query_params["where"] = where

            results = collection.query(**query_params)

            formatted_results = []
            if results["ids"] and results["ids"][0]:
                for i in range(len(results["ids"][0])):
                    formatted_results.append(
                        {
                            "id": results["ids"][0][i],
                            "content": results["documents"][0][i],
                            "metadata": (
                                results["metadatas"][0][i]
                                if results["metadatas"][0]
                                else {}
                            ),
                            "score": cosine_from_chroma_distance(
                                results["distances"][0][i]
                            ),
                        }
                    )

            self.logger.debug(
                f"KB search on '{collection_name}' returned "
                f"{len(formatted_results)} results",
                extra={
                    "collection": collection_name,
                    "query_len": len(query),
                    "results": len(formatted_results),
                },
            )

            return formatted_results

        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )

    # =========================================================================
    # Hybrid Search: Retrieval + Reranking Pipeline
    # =========================================================================

    async def hybrid_search(
        self,
        collection_name: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None,
        context_metadata: Optional[Dict[str, str]] = None,
        filter_mode: str = "soft",
    ) -> List[Dict[str, Any]]:
        """Two-stage hybrid search: broad recall followed by reranking.

        Stage 1 — Recall: Retrieves candidates from two sources:
          a) Pure vector search (cosine similarity on embeddings)
          b) Keyword-constrained vector search (requires exact keyword
             presence via ChromaDB where_document, then ranks by cosine).
             This catches identifier matches (error codes, service names)
             that pure embedding search misses.

        Stage 2 — Rerank: Scores each candidate chunk on four signals:
          - Vector similarity (original cosine score, 40%)
          - Term overlap (what fraction of query terms appear in chunk, 25%)
          - Metadata match (domain/service alignment + verification, 20%)
          - Freshness (staleness decay based on last_updated, 15%)
        Ties broken by scope priority: personal > team > global.

        Args:
            collection_name: ChromaDB collection name.
            query: Search query text.
            k: Number of results to return.
            where: ChromaDB metadata filters (scope filter required for KB).
            context_metadata: Optional case context (domain, service) for
                metadata-aware reranking. When provided, chunks matching
                the case's domain/service score higher.
            filter_mode: How to apply context_metadata:
                "soft" (default) — boost matching chunks in reranker only.
                "hard" — add domain/service to the ChromaDB where clause
                as a pre-filter, so non-matching chunks never enter Stage 1.
                Use "hard" when extension context is high-confidence.

        Returns:
            Top-k results sorted by reranked score.
        """
        self._enforce_scope_invariant(collection_name, where)

        # Hard filter mode: inject context_metadata into the where clause
        if filter_mode == "hard" and context_metadata:
            where = self._apply_hard_metadata_filter(where, context_metadata)

        # --- Stage 1: Recall ---
        # Cast a wide net: retrieve k*3 from vector, k*2 from keyword-constrained
        recall_k = max(k * 3, 15)  # At least 15 candidates for meaningful reranking

        # One embedding serves the whole sweep. The vector recall and every
        # keyword-constrained probe search the SAME query text, so the vector is
        # loop-invariant; embedding it per probe repeated an identical local
        # model call (~1.2-2.3s each on CPU) and dominated Stage-1 latency.
        query_embedding = await self._embed_query_or_raise(query, "hybrid search")

        vector_results = await self.search(
            collection_name=collection_name,
            query=query,
            k=recall_k,
            where=where,
            query_embedding=query_embedding,
        )

        # Keyword-constrained retrieval for identifier-heavy queries
        keywords = self._extract_search_keywords(query)
        keyword_results: List[Dict[str, Any]] = []
        if keywords:
            keyword_results = await self._keyword_constrained_search(
                collection_name=collection_name,
                query_embedding=query_embedding,
                keywords=keywords,
                k=k * 2,
                where=where,
            )

        # Deduplicate: merge keyword results into vector results (keep higher score)
        candidates = self._deduplicate_candidates(vector_results, keyword_results)

        if not candidates:
            return []

        # --- Stage 2: Rerank ---
        query_terms = self._extract_query_terms(query)
        reranked = self._rerank(
            candidates=candidates,
            query_terms=query_terms,
            query=query,
            context_metadata=context_metadata or {},
        )

        self.logger.debug(
            f"Hybrid search: {len(vector_results)} vector + "
            f"{len(keyword_results)} keyword → {len(candidates)} candidates "
            f"→ {min(k, len(reranked))} reranked",
            extra={
                "collection": collection_name,
                "vector_count": len(vector_results),
                "keyword_count": len(keyword_results),
                "candidate_count": len(candidates),
                "final_count": min(k, len(reranked)),
            },
        )

        return reranked[:k]

    # ---- Stage 1 helpers ----

    @staticmethod
    def _apply_hard_metadata_filter(
        where: Optional[Dict[str, Any]],
        context_metadata: Dict[str, str],
    ) -> Dict[str, Any]:
        """Inject context_metadata as hard pre-filters into the where clause.

        Adds domain and/or service from case context as additional $and
        conditions on the existing scope filter. This narrows the candidate
        set before ANN search — irrelevant domains never enter Stage 1.
        """
        hard_conditions = []
        if "domain" in context_metadata:
            hard_conditions.append({"domain": context_metadata["domain"]})
        if "service" in context_metadata:
            hard_conditions.append({"service": context_metadata["service"]})

        if not hard_conditions:
            return where or {}

        if where:
            return {"$and": [where] + hard_conditions}
        return (
            {"$and": hard_conditions}
            if len(hard_conditions) > 1
            else hard_conditions[0]
        )

    async def _keyword_constrained_search(
        self,
        collection_name: str,
        query_embedding: List[float],
        keywords: List[str],
        k: int,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Vector search constrained to documents containing specific keywords.

        For each extracted keyword, runs a vector search with ChromaDB's
        where_document pre-filter requiring that keyword's presence.
        This is NOT BM25 — it's a binary keyword gate on top of vector ranking.
        The value is catching identifier matches that embeddings miss.
        """
        all_results: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        for keyword in keywords[:3]:
            try:
                results = await self._single_keyword_search(
                    collection_name=collection_name,
                    query_embedding=query_embedding,
                    keyword=keyword,
                    k=k,
                    where=where,
                )
                for result in results:
                    if result["id"] not in seen_ids:
                        seen_ids.add(result["id"])
                        all_results.append(result)
            except KnowledgeBaseError:
                # Retrieval is unavailable, not unproductive. Degrading to the
                # results gathered so far would report a partial keyword sweep
                # as a complete one — the affirmative negative this campaign
                # closes. A per-keyword miss is tolerable; a dead embedder is
                # not, so it must not be caught by the handler below (#943).
                raise
            except Exception as e:
                logger.debug(f"Keyword-constrained search for '{keyword}' failed: {e}")

        return all_results

    async def _single_keyword_search(
        self,
        collection_name: str,
        query_embedding: List[float],
        keyword: str,
        k: int,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Vector search filtered to documents containing a specific keyword.

        Takes the query VECTOR rather than the query text. Every keyword in a
        sweep constrains the same query, so the embedding is loop-invariant:
        embedding here re-ran an identical local model call per keyword. The
        embedder-unavailable failure now surfaces from the single embed in
        :meth:`hybrid_search`, before any keyword is probed.
        """

        async def _search_wrapper():
            collection = self._get_or_create_collection(collection_name)

            query_params = {
                "query_embeddings": [query_embedding],
                "n_results": k,
                "include": ["documents", "metadatas", "distances"],
                "where_document": {"$contains": keyword},
            }

            if where:
                query_params["where"] = where

            results = collection.query(**query_params)

            formatted = []
            if results["ids"] and results["ids"][0]:
                for i in range(len(results["ids"][0])):
                    formatted.append(
                        {
                            "id": results["ids"][0][i],
                            "content": results["documents"][0][i],
                            "metadata": (
                                results["metadatas"][0][i]
                                if results["metadatas"][0]
                                else {}
                            ),
                            "score": cosine_from_chroma_distance(
                                results["distances"][0][i]
                            ),
                        }
                    )
            return formatted

        return await self.call_external(
            operation_name="keyword_search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0,
        )

    @staticmethod
    def _extract_search_keywords(query: str) -> List[str]:
        """Extract meaningful keywords for keyword-constrained search.

        Prioritizes identifier-like tokens (error codes, service names, paths)
        which benefit most from exact matching. Filters stop words and short tokens.
        """
        tokens = re.split(r"[\s,;]+", query)
        keywords = []

        for token in tokens:
            clean = token.strip("?!.()\"'")
            if len(clean) < MIN_KEYWORD_LENGTH:
                continue
            if clean.lower() in _STOP_WORDS:
                continue
            keywords.append(clean)

        # Sort: identifier-like tokens first (highest keyword search value)
        def identifier_score(token: str) -> int:
            return sum(1 for p in IDENTIFIER_PATTERNS if p.search(token))

        keywords.sort(key=identifier_score, reverse=True)
        return keywords

    @staticmethod
    def _deduplicate_candidates(
        vector_results: List[Dict[str, Any]],
        keyword_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge two result lists, keeping the higher-scoring entry for duplicates."""
        by_id: Dict[str, Dict[str, Any]] = {}

        for result in vector_results:
            by_id[result["id"]] = result

        for result in keyword_results:
            rid = result["id"]
            if rid not in by_id or result["score"] > by_id[rid]["score"]:
                by_id[rid] = result

        return list(by_id.values())

    # ---- Stage 2: Reranker ----

    @staticmethod
    def _extract_query_terms(query: str) -> List[str]:
        """Extract non-stop-word terms from query for term overlap scoring."""
        return [t for t in _tokenize(query) if t not in _STOP_WORDS]

    @classmethod
    def _rerank(
        cls,
        candidates: List[Dict[str, Any]],
        query_terms: List[str],
        context_metadata: Dict[str, str],
        query: str = "",
    ) -> List[Dict[str, Any]]:
        """Score and sort candidates using multiple retrieval signals.

        Signals:
          1. Vector similarity (original cosine score from ChromaDB)
          2. Term overlap (fraction of query terms found in chunk content)
          3. Metadata match (domain/service alignment + verification level)
          4. Freshness (staleness penalty based on last_updated)

        Dynamic weights: When the query contains identifier-like tokens
        (error codes, CamelCase, dotted names), term overlap weight shifts
        from 25% to 40% and vector similarity drops from 40% to 25%. This
        makes lexical matches dominate for exact-identifier queries.

        Final score = weighted sum of all signals.
        Ties broken by scope priority: personal > team > global.
        """
        # Select weights based on query characteristics
        has_identifiers = (
            any(p.search(query) for p in IDENTIFIER_PATTERNS) if query else False
        )
        if has_identifiers:
            w_vector = RERANK_WEIGHT_VECTOR_ID
            w_term = RERANK_WEIGHT_TERM_OVERLAP_ID
            w_meta = RERANK_WEIGHT_METADATA_ID
            w_fresh = RERANK_WEIGHT_FRESHNESS_ID
        else:
            w_vector = RERANK_WEIGHT_VECTOR
            w_term = RERANK_WEIGHT_TERM_OVERLAP
            w_meta = RERANK_WEIGHT_METADATA
            w_fresh = RERANK_WEIGHT_FRESHNESS

        scored: List[tuple] = []

        for candidate in candidates:
            content = candidate.get("content", "")
            metadata = candidate.get("metadata", {})

            # Signal 1: Vector similarity — TRUE cosine, because `search` and
            # `_single_keyword_search` convert through
            # cosine_from_chroma_distance. The weights above are only
            # meaningful against that scale: they blend this with three
            # genuine 0-1 signals, so feeding it the raw `1 - distance`
            # (= 2*cos - 1) silently doubled this signal's slope and made
            # RERANK_WEIGHT_VECTOR describe a blend it did not configure
            # (#1072). Any future change to what `score` carries must come
            # back here.
            vector_score = candidate.get("score", 0.0)

            # Signal 2: Term overlap — what fraction of query terms are in this chunk
            term_overlap = cls._compute_term_overlap(query_terms, content)

            # Signal 3: Metadata match — domain/service alignment + verification
            metadata_score = cls._compute_metadata_score(metadata, context_metadata)

            # Signal 4: Freshness — penalize stale content
            freshness_score = cls._compute_freshness_score(metadata)

            # Weighted combination
            final_score = (
                w_vector * vector_score
                + w_term * term_overlap
                + w_meta * metadata_score
                + w_fresh * freshness_score
            )

            # Scope priority for tiebreaking (lower = better)
            scope = metadata.get("scope", "global")
            scope_tiebreak = -SCOPE_PRIORITY.get(scope, 2)

            scored.append((final_score, scope_tiebreak, candidate))

        # Sort descending by score, then by scope priority
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        return [entry[2] for entry in scored]

    @staticmethod
    def _compute_term_overlap(query_terms: List[str], content: str) -> float:
        """Fraction of query terms that appear in the chunk content.

        Not BM25 — no term frequency or inverse document frequency.
        Simple binary overlap: each query term is either present or absent.
        This is a lightweight reranking signal, not a retrieval method.
        """
        if not query_terms:
            return 0.0

        content_lower = content.lower()
        hits = sum(1 for term in query_terms if term in content_lower)
        return hits / len(query_terms)

    @staticmethod
    def _compute_metadata_score(
        chunk_metadata: Dict[str, Any],
        context_metadata: Dict[str, str],
    ) -> float:
        """Score based on metadata alignment and runbook lifecycle status.

        Components:
          - Domain match: +0.3 if chunk domain matches case context domain
          - Service match: +0.3 if chunk service matches case context service
          - Status boost/penalty (frontmatter lifecycle values):
              verified → +0.4, in-review → +0.1
              stale → -0.2, draft → -0.1, deprecated → -0.3

        Domain/service matching is case-insensitive and whitespace-trimmed:
        the case-side value is free-text (e.g. the LLM's ``affected_services``
        entry "PostgreSQL"), while chunk frontmatter is curated ("postgresql").
        A raw ``==`` would miss the most common real-world alignment.
        """
        score = 0.0

        # Domain/service match against case context (if provided). Normalize
        # both sides so free-text case context matches curated frontmatter.
        if context_metadata:
            ctx_domain = context_metadata.get("domain", "").strip().lower()
            ctx_service = context_metadata.get("service", "").strip().lower()

            chunk_domain = str(chunk_metadata.get("domain", "")).strip().lower()
            chunk_service = str(chunk_metadata.get("service", "")).strip().lower()

            if ctx_domain and chunk_domain == ctx_domain:
                score += 0.3
            if ctx_service and chunk_service == ctx_service:
                score += 0.3

        # Runbook lifecycle status → trust signal.
        # Frontmatter status values: verified | in-review | draft | stale | deprecated
        status = chunk_metadata.get("status", "")
        if status == "verified":
            score += 0.4
        elif status == "in-review":
            score += 0.1
        elif status == "stale":
            score -= 0.2
        elif status == "draft":
            score -= 0.1
        elif status == "deprecated":
            score -= 0.3

        return max(0.0, min(1.0, score))

    @staticmethod
    def _compute_freshness_score(metadata: Dict[str, Any]) -> float:
        """Freshness score with decay based on age.

        Uses a half-life decay: score = 1 / (1 + age_days / HALF_LIFE).
        Content with no last_updated gets a neutral score of 0.5.
        """
        last_updated = metadata.get("last_updated")
        if not last_updated:
            return 0.5  # Neutral — no data, don't penalize or boost

        try:
            if "T" in str(last_updated):
                updated_dt = datetime.fromisoformat(
                    str(last_updated).replace("Z", "+00:00")
                )
            else:
                updated_dt = datetime.strptime(str(last_updated), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )

            age_days = max(0, (datetime.now(timezone.utc) - updated_dt).days)
            return 1.0 / (1.0 + age_days / STALENESS_HALF_LIFE_DAYS)

        except (ValueError, TypeError):
            return 0.5  # Parse failure — neutral

    # =========================================================================
    # Document Management
    # =========================================================================

    async def add_documents(
        self,
        documents: List[Dict[str, Any]],
        embeddings: Optional[List[List[float]]] = None,
        collection_name: str = KB_COLLECTION,
    ) -> None:
        """Add documents to a KB collection.

        Args:
            documents: List of dicts with 'id', 'content', and optional 'metadata'.
            embeddings: Pre-computed embedding vectors, one per document.
            collection_name: Exact ChromaDB collection name.

        Raises:
            ValueError: If any document's metadata is not a dict (or None), or
                carries a key ``VectorMetadata`` does not declare. The schema
                is an allowlist, so an undeclared key would otherwise be
                dropped in silence and read back later as the reader's
                fallback (#912).
        """
        # Refused OUTSIDE call_external, deliberately (fm#1035). A malformed
        # metadata dict is a deterministic programming error, not a ChromaDB
        # failure: raised inside the wrapper it would burn the retry budget
        # and count towards the circuit breaker this store shares with the KB
        # read path — enough bad writes would open it and fail healthy KB
        # searches too.
        #
        # ONE normalization, used by the guard AND the write below. "No
        # metadata" means {} whether the key is absent or present-but-None —
        # two expressions differing by a `.get` default would let the shape
        # the guard waves through (metadata: None) become an AttributeError
        # inside the wrapper, which is the breaker-poisoning failure this
        # placement exists to prevent. A non-dict is refused here for the
        # same reason: it must never reach `.items()`.
        #
        # Refusal only: the inline sanitization below stays this writer's
        # storage semantics — the guard fixes which KEYS a row may carry, not
        # how values are encoded.
        metadatas: List[Dict[str, Any]] = []
        for doc in documents:
            md = doc.get("metadata")
            if md is None:
                md = {}
            if not isinstance(md, dict):
                raise ValueError(
                    f"Vector metadata for document {doc.get('id')!r} must be "
                    f"a dict or None, got {type(md).__name__} — refusing "
                    f"before the external-call machinery sees it."
                )
            VectorMetadata.reject_undeclared_keys(md)
            metadatas.append(md)

        # `id` and `content` are read out here for the same reason the metadata
        # is: a document missing either raises a deterministic KeyError, and
        # every raise inside the wrapper below is retried and charged to the
        # breaker this store shares with the KB read path. Reading all three
        # fields in one place is also what keeps them from drifting apart —
        # the null-metadata hole this guard closed existed because the check
        # and the write derived the same field through two expressions.
        try:
            ids = [doc["id"] for doc in documents]
            contents = [doc["content"] for doc in documents]
        except KeyError as e:
            raise ValueError(
                f"Vector document is missing required field {e} — refusing "
                f"before the external-call machinery sees it."
            ) from e

        async def _add_wrapper():
            collection = self._get_or_create_collection(collection_name)

            sanitized_metadatas = []
            for md in metadatas:
                sanitized = {}
                for k, v in md.items():
                    if v is None:
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        sanitized[k] = v
                    else:
                        sanitized[k] = str(v)
                sanitized_metadatas.append(sanitized)

            add_kwargs: Dict[str, Any] = dict(
                ids=ids, documents=contents, metadatas=sanitized_metadatas
            )
            if embeddings is not None:
                add_kwargs["embeddings"] = embeddings
            collection.add(**add_kwargs)

            self.logger.info(
                f"Added {len(documents)} documents to KB collection "
                f"'{collection_name}'",
                extra={
                    "collection": collection_name,
                    "doc_count": len(documents),
                },
            )

        await self.call_external(
            operation_name="add_documents",
            call_func=_add_wrapper,
            timeout=30.0,
            retries=2,
            retry_delay=2.0,
        )

    async def get_document_count(self, collection_name: str) -> int:
        """Get number of documents in a KB collection."""

        async def _count_wrapper():
            try:
                collection = self.client.get_collection(name=collection_name)
                count = collection.count()
                self.logger.debug(
                    f"KB collection '{collection_name}' has {count} documents"
                )
                return count
            except Exception:
                return 0

        return await self.call_external(
            operation_name="get_document_count",
            call_func=_count_wrapper,
            timeout=5.0,
            retries=1,
            retry_delay=1.0,
        )

    async def delete_documents_by_parent_id(
        self,
        parent_document_id: str,
        collection_name: str = KB_COLLECTION,
    ) -> int:
        """Delete all chunks of a parent document from a KB collection.

        The KB write path (``add_documents``) tags every chunk with
        ``metadata.parent_document_id``; this is its inverse — the delete half of
        the document lifecycle. Without it the re-ingest/prune paths in
        ``bootstrap.kb_init`` could only drop the SQL row, leaving the vectors
        orphaned in ChromaDB (the index drift the reconcile pass cleans up).

        Returns the number of chunks deleted (0 if the parent has no chunks).
        """

        async def _delete_wrapper() -> int:
            collection = self._get_or_create_collection(collection_name)
            results = collection.get(
                where={"parent_document_id": parent_document_id},
                include=[],
            )
            chunk_ids = results.get("ids", [])
            if not chunk_ids:
                return 0
            collection.delete(ids=chunk_ids)
            self.logger.info(
                f"Deleted {len(chunk_ids)} chunks for parent "
                f"{parent_document_id} from KB collection '{collection_name}'"
            )
            return len(chunk_ids)

        return await self.call_external(
            operation_name="delete_documents_by_parent_id",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )

    async def delete_documents_by_parents(
        self,
        parent_document_ids: Iterable[str],
        collection_name: str = KB_COLLECTION,
    ) -> int:
        """Delete all chunks of MANY parents in one ChromaDB round-trip.

        The reconcile pass can find a large batch of orphaned parents at once
        (119 in the 2026-06 drift); a per-parent loop would issue one delete call
        each. ChromaDB ``where`` supports ``$in``, so the whole batch resolves to a
        single get (for the chunk-id set + count) plus a single delete.

        Returns the number of chunks deleted (0 if the batch matches nothing).
        """
        ids = list(parent_document_ids)
        if not ids:
            return 0

        async def _delete_wrapper() -> int:
            collection = self._get_or_create_collection(collection_name)
            results = collection.get(
                where={"parent_document_id": {"$in": ids}},
                include=[],
            )
            chunk_ids = results.get("ids", [])
            if not chunk_ids:
                return 0
            collection.delete(ids=chunk_ids)
            self.logger.info(
                f"Deleted {len(chunk_ids)} chunks across {len(ids)} parent(s) "
                f"from KB collection '{collection_name}'"
            )
            return len(chunk_ids)

        return await self.call_external(
            operation_name="delete_documents_by_parents",
            call_func=_delete_wrapper,
            timeout=15.0,
            retries=2,
            retry_delay=1.0,
        )

    async def list_parent_document_ids(
        self,
        collection_name: str = KB_COLLECTION,
    ) -> Set[str]:
        """Return the distinct ``parent_document_id``s present in a collection.

        Lets the bootstrap reconcile pass compare the vector index against the
        ``knowledge_items`` rows (the source of truth) to find drift in either
        direction — vectors with no row (orphans to delete) and rows with no
        vectors (orphans to warn about).

        Derives the parent from each chunk **id** (``{item_id}_chunk_{N}``) rather
        than pulling every chunk's full metadata payload — ``include=[]`` returns
        only ids, so the per-boot cost scales with id count, not metadata size, and
        a chunk missing the ``parent_document_id`` metadata key is still resolved
        (item_ids never contain ``_chunk_``).
        """

        async def _list_wrapper() -> Set[str]:
            try:
                collection = self.client.get_collection(name=collection_name)
            except NotFoundError:
                # Collection absent → nothing indexed yet. Any OTHER error
                # (transport, corruption) propagates to call_external rather than
                # being silently flattened to an empty index.
                return set()
            results = collection.get(include=[])
            parents: Set[str] = set()
            for chunk_id in results.get("ids", []) or []:
                if not isinstance(chunk_id, str):
                    continue
                parents.add(chunk_id.rsplit("_chunk_", 1)[0])
            return parents

        return await self.call_external(
            operation_name="list_parent_document_ids",
            call_func=_list_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0,
        )

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
    - Verification level (verified > community > experimental)
    - Staleness penalty (older content scores lower)
    - Scope preference (personal > team > global tiebreaking)
"""

import asyncio
import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from faultmaven.infrastructure.base_client import BaseExternalClient

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

# Keys that indicate a scope filter is present in a where clause
SCOPE_FILTER_KEYS = {"scope", "owner_id", "team_id", "organization_id"}

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
    a scope filter (scope, owner_id, or team_id) in the `where` clause.
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
                f"on '{collection_name}'. Pass a where clause containing "
                f"'scope', 'owner_id', or 'team_id'."
            )

        filter_keys = _flatten_filter_keys(where)
        if not filter_keys & SCOPE_FILTER_KEYS:
            raise ValueError(
                f"KB queries require scope filter — where clause {where} "
                f"does not contain any of {SCOPE_FILTER_KEYS}. "
                f"Refusing unscoped search on '{collection_name}'."
            )

    async def search(
        self,
        collection_name: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar documents in a KB collection.

        Args:
            collection_name: Exact ChromaDB collection name (no prefix added).
            query: Search query text.
            k: Number of results to return.
            where: ChromaDB metadata filters. **Required** for faultmaven_kb
                   collection (must include scope/owner_id/team_id filter).

        Returns:
            List of matching documents with content, metadata, and scores.

        Raises:
            ValueError: If querying faultmaven_kb without a scope filter.
        """
        self._enforce_scope_invariant(collection_name, where)

        async def _search_wrapper():
            from faultmaven.infrastructure.model_cache import model_cache

            collection = self._get_or_create_collection(collection_name)

            # Use explicit BGE-M3 embedding (1024 dims) to match stored vectors.
            # Model lookup may lazy-load; encode() is CPU-bound. Offload to a
            # worker thread so the event loop stays responsive.
            bge_model = await asyncio.to_thread(model_cache.get_bge_m3_model)
            if bge_model is None:
                self.logger.error("BGE-M3 model unavailable for search")
                return []

            query_embedding = await asyncio.to_thread(
                lambda: bge_model.encode(query).tolist()
            )

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
                            "score": 1.0 - results["distances"][0][i],
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

        vector_results = await self.search(
            collection_name=collection_name,
            query=query,
            k=recall_k,
            where=where,
        )

        # Keyword-constrained retrieval for identifier-heavy queries
        keywords = self._extract_search_keywords(query)
        keyword_results: List[Dict[str, Any]] = []
        if keywords:
            keyword_results = await self._keyword_constrained_search(
                collection_name=collection_name,
                query=query,
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
        query: str,
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
                    query=query,
                    keyword=keyword,
                    k=k,
                    where=where,
                )
                for result in results:
                    if result["id"] not in seen_ids:
                        seen_ids.add(result["id"])
                        all_results.append(result)
            except Exception as e:
                logger.debug(f"Keyword-constrained search for '{keyword}' failed: {e}")

        return all_results

    async def _single_keyword_search(
        self,
        collection_name: str,
        query: str,
        keyword: str,
        k: int,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Vector search filtered to documents containing a specific keyword."""

        async def _search_wrapper():
            from faultmaven.infrastructure.model_cache import model_cache

            collection = self._get_or_create_collection(collection_name)

            bge_model = await asyncio.to_thread(model_cache.get_bge_m3_model)
            if bge_model is None:
                self.logger.error("BGE-M3 model unavailable for keyword search")
                return []

            query_embedding = await asyncio.to_thread(
                lambda: bge_model.encode(query).tolist()
            )

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
                            "score": 1.0 - results["distances"][0][i],
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

            # Signal 1: Vector similarity (already 0-1 from ChromaDB)
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
        """Score based on metadata alignment and verification level.

        Components:
          - Domain match: +0.3 if chunk domain matches case context domain
          - Service match: +0.3 if chunk service matches case context service
          - Verification: +0.0 (experimental) / +0.2 (community) / +0.4 (verified)
          - Status penalty: -0.3 if deprecated, -0.1 if draft
        """
        score = 0.0

        # Domain/service match against case context (if provided)
        if context_metadata:
            ctx_domain = context_metadata.get("domain", "")
            ctx_service = context_metadata.get("service", "")

            if ctx_domain and chunk_metadata.get("domain") == ctx_domain:
                score += 0.3
            if ctx_service and chunk_metadata.get("service") == ctx_service:
                score += 0.3

        # Verification level boost
        status = chunk_metadata.get("status", "")
        if status == "verified":
            score += 0.4
        elif status == "community":
            score += 0.2
        # experimental/draft/no status = 0.0

        # Status penalty
        if status == "deprecated":
            score -= 0.3
        elif status == "draft":
            score -= 0.1

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
        collection_name: str,
        documents: List[Dict[str, Any]],
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        """Add documents to a KB collection.

        Args:
            collection_name: Exact ChromaDB collection name.
            documents: List of dicts with 'id', 'content', and optional 'metadata'.
            embeddings: Pre-computed embedding vectors, one per document.
        """

        async def _add_wrapper():
            collection = self._get_or_create_collection(collection_name)

            ids = [doc["id"] for doc in documents]
            contents = [doc["content"] for doc in documents]
            metadatas = [doc.get("metadata", {}) for doc in documents]

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

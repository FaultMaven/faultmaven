"""
Knowledge Base Vector Store

ChromaDB adapter for the unified KB collection (faultmaven_kb).
All scopes (global, team, personal) share one collection with metadata-based
scope filtering. This is distinct from CaseVectorStore, which prefixes "case_"
and is designed for ephemeral per-case evidence collections.

Scope safety invariant: queries against faultmaven_kb MUST include a scope
filter in the `where` clause. Unscoped queries are rejected with ValueError
to prevent cross-tenant data leaks.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient

logger = logging.getLogger(__name__)

# Collection name that requires scope filtering
KB_COLLECTION = "faultmaven_kb"

# Keys that indicate a scope filter is present in a where clause
SCOPE_FILTER_KEYS = {"scope", "owner_id", "team_id"}


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
            collection = self._get_or_create_collection(collection_name)

            query_params = {
                "query_texts": [query],
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

    async def add_documents(
        self,
        collection_name: str,
        documents: List[Dict[str, Any]],
    ) -> None:
        """Add documents to a KB collection.

        Args:
            collection_name: Exact ChromaDB collection name.
            documents: List of dicts with 'id', 'content', and optional 'metadata'.
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

            collection.add(ids=ids, documents=contents, metadatas=sanitized_metadatas)

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

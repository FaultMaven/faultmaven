"""
Knowledge Base Vector Store

ChromaDB adapter for knowledge base collections (Global, Team, Personal).
Uses collection names exactly as provided by KBConfig — no prefix manipulation.

This is distinct from CaseVectorStore, which prepends "case_" and is designed
for ephemeral per-case evidence collections. Knowledge collections are permanent
and use names like "global_kb", "team_{id}_kb", "user_{id}_kb".
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient

logger = logging.getLogger(__name__)


class KnowledgeVectorStore(BaseExternalClient):
    """Vector store for permanent knowledge base collections.

    Unlike CaseVectorStore (which prepends "case_" to all collection names),
    this store uses collection names as-is from KBConfig. This ensures that
    ingestion and retrieval use the same collection names.

    Collection names are determined by KBConfig implementations:
    - GlobalKBConfig: "global_kb"
    - TeamKBConfig: "team_{team_id}_kb"
    - UserKBConfig: "user_{user_id}_kb"
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
            where: Optional ChromaDB metadata filters.

        Returns:
            List of matching documents with content, metadata, and scores.
        """

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

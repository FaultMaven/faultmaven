"""Vector Store Service for managing embeddings using ChromaDB.

This service provides vector storage and similarity search capabilities
using ChromaDB for the knowledge module's RAG system.

Receives a shared ChromaDB client via constructor injection (Principle 5).
All collections live in the same ChromaDB instance as faultmaven_kb and case_* collections.
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import VectorStoreConnectionError, VectorStoreOperationError

logger = logging.getLogger(__name__)


class VectorStoreService:
    """Service for managing vector embeddings using ChromaDB.

    Handles vector storage and retrieval with:
    - Cosine similarity metric for semantic search
    - HNSW index for fast approximate nearest neighbor search
    - Metadata filtering for organization-level isolation

    The ChromaDB client is injected — works with both PersistentClient (local)
    and HttpClient (cloud) transparently.
    """

    def __init__(
        self,
        client,
        collection_name: str = "knowledge_items",
    ):
        """Initialize vector store service.

        Args:
            client: ChromaDB client instance (PersistentClient or HttpClient).
            collection_name: Name of ChromaDB collection for knowledge items.
        """
        self.client = client
        self.collection_name = collection_name

        try:
            self.collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            logger.info(
                f"VectorStoreService initialized with collection '{collection_name}'"
            )

        except Exception as e:
            logger.error(f"Failed to initialize vector store collection: {e}")
            raise VectorStoreConnectionError(
                f"Failed to initialize ChromaDB collection: {e}",
                details={
                    "collection_name": collection_name,
                    "error_type": type(e).__name__,
                },
            ) from e

    async def add_item(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str,
    ) -> None:
        """Add knowledge item to vector store."""
        try:
            sanitized_metadata = self._sanitize_metadata(metadata)

            self.collection.upsert(
                ids=[item_id],
                embeddings=[embedding],
                metadatas=[sanitized_metadata],
                documents=[document],
            )

        except Exception as e:
            logger.error(f"Failed to add item {item_id} to vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to add item to vector store: {e}",
                details={"item_id": item_id, "error_type": type(e).__name__},
            ) from e

    async def add_items_batch(
        self,
        items: List[Dict[str, Any]],
        batch_size: int = 100,
    ) -> None:
        """Add multiple items in batches."""
        if not items:
            return

        try:
            for i in range(0, len(items), batch_size):
                batch = items[i : i + batch_size]

                ids = [item["item_id"] for item in batch]
                embeddings = [item["embedding"] for item in batch]
                metadatas = [
                    self._sanitize_metadata(item["metadata"]) for item in batch
                ]
                documents = [item["document"] for item in batch]

                self.collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    metadatas=metadatas,
                    documents=documents,
                )

        except Exception as e:
            logger.error(f"Failed to add batch to vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to add batch to vector store: {e}",
                details={"batch_count": len(items), "error_type": type(e).__name__},
            ) from e

    async def search_similar(
        self,
        query_embedding: List[float],
        organization_id: str,
        n_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar items using cosine similarity."""
        try:
            conditions = [{"organization_id": organization_id}]

            if filters:
                for key, value in filters.items():
                    if value is not None:
                        if isinstance(value, (str, int, float, bool)):
                            conditions.append({key: value})
                        elif hasattr(value, "value"):
                            conditions.append({key: value.value})

            if len(conditions) > 1:
                where = {"$and": conditions}
            else:
                where = conditions[0]

            count = self.collection.count()
            if count == 0:
                return []

            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(n_results, count),
                where=where,
                include=["metadatas", "documents", "distances"],
            )

            formatted_results = []
            if results and results["ids"] and results["ids"][0]:
                for i, item_id in enumerate(results["ids"][0]):
                    formatted_results.append(
                        {
                            "item_id": item_id,
                            "distance": (
                                results["distances"][0][i]
                                if results["distances"]
                                else None
                            ),
                            "metadata": (
                                results["metadatas"][0][i]
                                if results["metadatas"]
                                else {}
                            ),
                            "document": (
                                results["documents"][0][i]
                                if results["documents"]
                                else ""
                            ),
                        }
                    )

            return formatted_results

        except Exception as e:
            logger.error(f"Failed to search vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to search vector store: {e}",
                details={
                    "organization_id": organization_id,
                    "n_results": n_results,
                    "error_type": type(e).__name__,
                },
            ) from e

    async def delete_item(self, item_id: str) -> None:
        """Delete item from vector store."""
        try:
            self.collection.delete(ids=[item_id])
        except Exception as e:
            logger.error(f"Failed to delete item {item_id} from vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to delete item from vector store: {e}",
                details={"item_id": item_id, "error_type": type(e).__name__},
            ) from e

    async def delete_items_batch(self, item_ids: List[str]) -> None:
        """Delete multiple items from vector store."""
        if not item_ids:
            return

        try:
            self.collection.delete(ids=item_ids)
        except Exception as e:
            logger.error(f"Failed to delete batch from vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to delete batch from vector store: {e}",
                details={"item_count": len(item_ids), "error_type": type(e).__name__},
            ) from e

    async def update_item(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str,
    ) -> None:
        """Update item in vector store."""
        try:
            sanitized_metadata = self._sanitize_metadata(metadata)
            self.collection.upsert(
                ids=[item_id],
                embeddings=[embedding],
                metadatas=[sanitized_metadata],
                documents=[document],
            )
        except Exception as e:
            logger.error(f"Failed to update item {item_id} in vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to update item in vector store: {e}",
                details={"item_id": item_id, "error_type": type(e).__name__},
            ) from e

    async def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Get item from vector store by ID."""
        try:
            result = self.collection.get(
                ids=[item_id],
                include=["embeddings", "metadatas", "documents"],
            )

            if not result["ids"]:
                return None

            embedding = None
            if result.get("embeddings") is not None and len(result["embeddings"]) > 0:
                emb = result["embeddings"][0]
                embedding = emb.tolist() if hasattr(emb, "tolist") else list(emb)

            metadata = {}
            if result.get("metadatas") is not None and len(result["metadatas"]) > 0:
                metadata = result["metadatas"][0] or {}

            document = ""
            if result.get("documents") is not None and len(result["documents"]) > 0:
                document = result["documents"][0] or ""

            return {
                "item_id": result["ids"][0],
                "embedding": embedding,
                "metadata": metadata,
                "document": document,
            }

        except Exception as e:
            logger.error(f"Failed to get item {item_id} from vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to get item from vector store: {e}",
                details={"item_id": item_id, "error_type": type(e).__name__},
            ) from e

    async def get_collection_stats(self) -> Dict[str, Any]:
        """Get collection statistics."""
        try:
            count = self.collection.count()
            return {
                "collection_name": self.collection_name,
                "item_count": count,
                "metadata": self.collection.metadata or {},
            }
        except Exception as e:
            logger.error(f"Failed to get collection stats: {e}")
            raise VectorStoreOperationError(
                f"Failed to get collection stats: {e}",
                details={"error_type": type(e).__name__},
            ) from e

    async def clear_collection(self) -> None:
        """Clear all items from the collection."""
        try:
            self.client.delete_collection(self.collection_name)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            logger.error(f"Failed to clear collection: {e}")
            raise VectorStoreOperationError(
                f"Failed to clear collection: {e}",
                details={"error_type": type(e).__name__},
            ) from e

    async def delete_by_organization(self, organization_id: str) -> int:
        """Delete all items for an organization."""
        try:
            result = self.collection.get(
                where={"organization_id": organization_id},
                include=[],
            )

            if not result["ids"]:
                return 0

            self.collection.delete(ids=result["ids"])
            return len(result["ids"])

        except Exception as e:
            logger.error(
                f"Failed to delete items for organization {organization_id}: {e}"
            )
            raise VectorStoreOperationError(
                f"Failed to delete items for organization: {e}",
                details={
                    "organization_id": organization_id,
                    "error_type": type(e).__name__,
                },
            ) from e

    def _sanitize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize metadata for ChromaDB compatibility."""
        sanitized = {}
        for key, value in metadata.items():
            if value is None:
                continue
            elif isinstance(value, (str, int, float, bool)):
                sanitized[key] = value
            elif hasattr(value, "value"):
                sanitized[key] = value.value
            elif isinstance(value, (list, dict)):
                continue
            else:
                sanitized[key] = str(value)
        return sanitized

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check."""
        base_health = {"service": "vector_store_service", "status": "healthy"}

        try:
            stats = await self.get_collection_stats()
            store_status = "healthy"
        except Exception as e:
            store_status = "unhealthy"
            stats = {}
            base_health["error"] = str(e)

        base_health.update(
            {
                "store_status": store_status,
                "collection_name": self.collection_name,
                "item_count": stats.get("item_count", 0),
            }
        )

        return base_health

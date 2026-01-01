"""Vector Store Service for managing embeddings using ChromaDB.

This service provides vector storage and similarity search capabilities
using ChromaDB for the RAG (Retrieval-Augmented Generation) system.

Features:
- Persistent ChromaDB storage with HNSW index
- Cosine similarity metric for vector search
- Metadata filtering for organization isolation
- Batch operations for efficient bulk indexing
- Collection statistics and management

Usage:
    from faultmaven.services.vector_store_service import VectorStoreService

    service = VectorStoreService(persist_directory="./chroma_data")
    await service.add_item(item_id, embedding, metadata, document)
    results = await service.search_similar(query_embedding, org_id, n_results=10)
"""

import logging
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings

from faultmaven.services.base import BaseService
from faultmaven.exceptions import (
    VectorStoreConnectionError,
    VectorStoreOperationError,
)


logger = logging.getLogger(__name__)


class VectorStoreService(BaseService):
    """Service for managing vector embeddings using ChromaDB.

    This service handles vector storage and retrieval with:
    - Persistent client for data durability
    - Cosine similarity metric for semantic search
    - HNSW index for fast approximate nearest neighbor search
    - Metadata filtering for organization-level isolation

    Attributes:
        client: ChromaDB PersistentClient
        collection: ChromaDB collection for knowledge items
        collection_name: Name of the collection
        persist_directory: Directory for ChromaDB persistence
    """

    def __init__(
        self,
        collection_name: str = "knowledge_items",
        persist_directory: str = "./chroma_data",
    ):
        """Initialize ChromaDB client.

        Args:
            collection_name: Name of ChromaDB collection
            persist_directory: Directory for ChromaDB persistence
        """
        super().__init__("vector_store_service")
        self.collection_name = collection_name
        self.persist_directory = persist_directory

        try:
            # Initialize persistent client
            self.client = chromadb.PersistentClient(
                path=persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )

            # Get or create collection with cosine similarity
            self.collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            logger.info(
                f"Initialized vector store with collection '{collection_name}' "
                f"at '{persist_directory}'"
            )

        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            raise VectorStoreConnectionError(
                f"Failed to initialize ChromaDB: {e}",
                details={
                    "persist_directory": persist_directory,
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
        """Add knowledge item to vector store.

        Args:
            item_id: Unique item identifier
            embedding: Embedding vector (1536 dimensions)
            metadata: Item metadata (organization_id, item_type, category, etc.)
            document: Original text content

        Raises:
            VectorStoreOperationError: If add operation fails
        """
        return await self.execute_operation(
            "add_item",
            self._add_item_impl,
            item_id,
            embedding,
            metadata,
            document,
        )

    async def _add_item_impl(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str,
    ) -> None:
        """Internal implementation of add_item."""
        try:
            # Sanitize metadata - ChromaDB only supports string, int, float, bool
            sanitized_metadata = self._sanitize_metadata(metadata)

            # Upsert to handle both new and existing items
            self.collection.upsert(
                ids=[item_id],
                embeddings=[embedding],
                metadatas=[sanitized_metadata],
                documents=[document],
            )

            self.log_metric(
                "vector_item_added",
                1,
                unit="count",
                tags={"collection": self.collection_name},
            )

        except Exception as e:
            logger.error(f"Failed to add item {item_id} to vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to add item to vector store: {e}",
                details={
                    "item_id": item_id,
                    "error_type": type(e).__name__,
                },
            ) from e

    async def add_items_batch(
        self,
        items: List[Dict[str, Any]],
        batch_size: int = 100,
    ) -> None:
        """Add multiple items in batches.

        Args:
            items: List of dicts with keys: item_id, embedding, metadata, document
            batch_size: Number of items per batch

        Raises:
            VectorStoreOperationError: If batch add operation fails
        """
        return await self.execute_operation(
            "add_items_batch",
            self._add_items_batch_impl,
            items,
            batch_size,
        )

    async def _add_items_batch_impl(
        self,
        items: List[Dict[str, Any]],
        batch_size: int,
    ) -> None:
        """Internal implementation of batch add."""
        if not items:
            return

        try:
            # Process in batches
            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]

                ids = [item["item_id"] for item in batch]
                embeddings = [item["embedding"] for item in batch]
                metadatas = [
                    self._sanitize_metadata(item["metadata"])
                    for item in batch
                ]
                documents = [item["document"] for item in batch]

                self.collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    metadatas=metadatas,
                    documents=documents,
                )

                self.log_metric(
                    "vector_batch_added",
                    len(batch),
                    unit="count",
                    tags={
                        "collection": self.collection_name,
                        "batch_index": i // batch_size,
                    },
                )

        except Exception as e:
            logger.error(f"Failed to add batch to vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to add batch to vector store: {e}",
                details={
                    "batch_count": len(items),
                    "error_type": type(e).__name__,
                },
            ) from e

    async def search_similar(
        self,
        query_embedding: List[float],
        organization_id: str,
        n_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar items using cosine similarity.

        Args:
            query_embedding: Query embedding vector
            organization_id: Filter by organization
            n_results: Number of results to return
            filters: Additional metadata filters (item_type, category, etc.)

        Returns:
            List of results with item_id, distance, metadata, document

        Raises:
            VectorStoreOperationError: If search operation fails
        """
        return await self.execute_operation(
            "search_similar",
            self._search_similar_impl,
            query_embedding,
            organization_id,
            n_results,
            filters,
        )

    async def _search_similar_impl(
        self,
        query_embedding: List[float],
        organization_id: str,
        n_results: int,
        filters: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Internal implementation of similarity search."""
        try:
            # Build where clause for filtering
            # ChromaDB requires $and operator for multiple conditions
            conditions = [{"organization_id": organization_id}]

            if filters:
                # Handle additional filters
                for key, value in filters.items():
                    if value is not None:
                        if isinstance(value, (str, int, float, bool)):
                            conditions.append({key: value})
                        elif hasattr(value, 'value'):
                            # Handle enums
                            conditions.append({key: value.value})

            # Use $and if multiple conditions, otherwise use simple dict
            if len(conditions) > 1:
                where = {"$and": conditions}
            else:
                where = conditions[0]

            # Check if collection is empty
            count = self.collection.count()
            if count == 0:
                return []

            # Query with embedding
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(n_results, count),
                where=where,
                include=["metadatas", "documents", "distances"],
            )

            # Format results
            formatted_results = []
            if results and results["ids"] and results["ids"][0]:
                for i, item_id in enumerate(results["ids"][0]):
                    formatted_results.append({
                        "item_id": item_id,
                        "distance": results["distances"][0][i] if results["distances"] else None,
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "document": results["documents"][0][i] if results["documents"] else "",
                    })

            self.log_metric(
                "vector_search_performed",
                1,
                unit="count",
                tags={
                    "collection": self.collection_name,
                    "results_count": len(formatted_results),
                },
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
        """Delete item from vector store.

        Args:
            item_id: Item identifier to delete

        Raises:
            VectorStoreOperationError: If delete operation fails
        """
        return await self.execute_operation(
            "delete_item",
            self._delete_item_impl,
            item_id,
        )

    async def _delete_item_impl(self, item_id: str) -> None:
        """Internal implementation of delete_item."""
        try:
            self.collection.delete(ids=[item_id])

            self.log_metric(
                "vector_item_deleted",
                1,
                unit="count",
                tags={"collection": self.collection_name},
            )

        except Exception as e:
            logger.error(f"Failed to delete item {item_id} from vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to delete item from vector store: {e}",
                details={
                    "item_id": item_id,
                    "error_type": type(e).__name__,
                },
            ) from e

    async def delete_items_batch(self, item_ids: List[str]) -> None:
        """Delete multiple items from vector store.

        Args:
            item_ids: List of item identifiers to delete

        Raises:
            VectorStoreOperationError: If batch delete operation fails
        """
        return await self.execute_operation(
            "delete_items_batch",
            self._delete_items_batch_impl,
            item_ids,
        )

    async def _delete_items_batch_impl(self, item_ids: List[str]) -> None:
        """Internal implementation of batch delete."""
        if not item_ids:
            return

        try:
            self.collection.delete(ids=item_ids)

            self.log_metric(
                "vector_items_deleted",
                len(item_ids),
                unit="count",
                tags={"collection": self.collection_name},
            )

        except Exception as e:
            logger.error(f"Failed to delete batch from vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to delete batch from vector store: {e}",
                details={
                    "item_count": len(item_ids),
                    "error_type": type(e).__name__,
                },
            ) from e

    async def update_item(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str,
    ) -> None:
        """Update item in vector store.

        Args:
            item_id: Item identifier to update
            embedding: Updated embedding vector
            metadata: Updated metadata
            document: Updated document text

        Raises:
            VectorStoreOperationError: If update operation fails
        """
        return await self.execute_operation(
            "update_item",
            self._update_item_impl,
            item_id,
            embedding,
            metadata,
            document,
        )

    async def _update_item_impl(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str,
    ) -> None:
        """Internal implementation of update_item."""
        try:
            sanitized_metadata = self._sanitize_metadata(metadata)

            # Use upsert to update (or create if not exists)
            self.collection.upsert(
                ids=[item_id],
                embeddings=[embedding],
                metadatas=[sanitized_metadata],
                documents=[document],
            )

            self.log_metric(
                "vector_item_updated",
                1,
                unit="count",
                tags={"collection": self.collection_name},
            )

        except Exception as e:
            logger.error(f"Failed to update item {item_id} in vector store: {e}")
            raise VectorStoreOperationError(
                f"Failed to update item in vector store: {e}",
                details={
                    "item_id": item_id,
                    "error_type": type(e).__name__,
                },
            ) from e

    async def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Get item from vector store by ID.

        Args:
            item_id: Item identifier

        Returns:
            Item dict with id, embedding, metadata, document, or None if not found

        Raises:
            VectorStoreOperationError: If get operation fails
        """
        return await self.execute_operation(
            "get_item",
            self._get_item_impl,
            item_id,
        )

    async def _get_item_impl(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Internal implementation of get_item."""
        try:
            result = self.collection.get(
                ids=[item_id],
                include=["embeddings", "metadatas", "documents"],
            )

            if not result["ids"]:
                return None

            # Handle numpy arrays from ChromaDB - convert to list if needed
            embedding = None
            if result.get("embeddings") is not None and len(result["embeddings"]) > 0:
                emb = result["embeddings"][0]
                # Convert numpy array to list if necessary
                embedding = emb.tolist() if hasattr(emb, 'tolist') else list(emb)

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
                details={
                    "item_id": item_id,
                    "error_type": type(e).__name__,
                },
            ) from e

    async def get_collection_stats(self) -> Dict[str, Any]:
        """Get collection statistics.

        Returns:
            Dictionary with collection statistics (count, etc.)

        Raises:
            VectorStoreOperationError: If stats retrieval fails
        """
        return await self.execute_operation(
            "get_collection_stats",
            self._get_collection_stats_impl,
        )

    async def _get_collection_stats_impl(self) -> Dict[str, Any]:
        """Internal implementation of get_collection_stats."""
        try:
            count = self.collection.count()

            return {
                "collection_name": self.collection_name,
                "persist_directory": self.persist_directory,
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
        """Clear all items from the collection.

        This is primarily for testing purposes.

        Raises:
            VectorStoreOperationError: If clear operation fails
        """
        return await self.execute_operation(
            "clear_collection",
            self._clear_collection_impl,
        )

    async def _clear_collection_impl(self) -> None:
        """Internal implementation of clear_collection."""
        try:
            # Delete and recreate collection
            self.client.delete_collection(self.collection_name)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            self.log_business_event(
                "vector_collection_cleared",
                severity="warning",
                data={"collection": self.collection_name},
            )

        except Exception as e:
            logger.error(f"Failed to clear collection: {e}")
            raise VectorStoreOperationError(
                f"Failed to clear collection: {e}",
                details={"error_type": type(e).__name__},
            ) from e

    async def delete_by_organization(self, organization_id: str) -> int:
        """Delete all items for an organization.

        Args:
            organization_id: Organization identifier

        Returns:
            Number of items deleted

        Raises:
            VectorStoreOperationError: If delete operation fails
        """
        return await self.execute_operation(
            "delete_by_organization",
            self._delete_by_organization_impl,
            organization_id,
        )

    async def _delete_by_organization_impl(self, organization_id: str) -> int:
        """Internal implementation of delete_by_organization."""
        try:
            # Get all items for the organization
            result = self.collection.get(
                where={"organization_id": organization_id},
                include=[],
            )

            if not result["ids"]:
                return 0

            # Delete all found items
            self.collection.delete(ids=result["ids"])
            deleted_count = len(result["ids"])

            self.log_metric(
                "vector_items_deleted_by_org",
                deleted_count,
                unit="count",
                tags={
                    "collection": self.collection_name,
                    "organization_id": organization_id,
                },
            )

            return deleted_count

        except Exception as e:
            logger.error(f"Failed to delete items for organization {organization_id}: {e}")
            raise VectorStoreOperationError(
                f"Failed to delete items for organization: {e}",
                details={
                    "organization_id": organization_id,
                    "error_type": type(e).__name__,
                },
            ) from e

    def _sanitize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize metadata for ChromaDB compatibility.

        ChromaDB only supports string, int, float, bool values in metadata.

        Args:
            metadata: Original metadata dict

        Returns:
            Sanitized metadata dict
        """
        sanitized = {}
        for key, value in metadata.items():
            if value is None:
                continue
            elif isinstance(value, (str, int, float, bool)):
                sanitized[key] = value
            elif hasattr(value, 'value'):
                # Handle enums
                sanitized[key] = value.value
            elif isinstance(value, (list, dict)):
                # Skip complex types - could also serialize to JSON string
                continue
            else:
                # Convert to string as fallback
                sanitized[key] = str(value)
        return sanitized

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check for the vector store service.

        Returns:
            Dictionary with health status
        """
        base_health = await super().health_check()

        try:
            stats = await self._get_collection_stats_impl()
            store_status = "healthy"
        except Exception as e:
            store_status = "unhealthy"
            stats = {}
            base_health["error"] = str(e)

        base_health.update({
            "store_status": store_status,
            "collection_name": self.collection_name,
            "persist_directory": self.persist_directory,
            "item_count": stats.get("item_count", 0),
        })

        return base_health

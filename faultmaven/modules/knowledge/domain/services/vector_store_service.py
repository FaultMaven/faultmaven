"""Vector Store Service for managing embeddings using IVectorBackend.

This service provides vector storage and similarity search capabilities
using the IVectorBackend interface for the RAG (Retrieval-Augmented Generation) system.

Features:
- Backend-agnostic vector storage via IVectorBackend interface
- Cosine similarity metric for vector search
- Metadata filtering for organization isolation
- Batch operations for efficient bulk indexing
- Collection statistics and management

Principle 1 Compliance: No direct chromadb imports - uses IVectorBackend.

Usage:
    from faultmaven.modules.knowledge.domain.services.vector_store_service import VectorStoreService

    # With injected backend (preferred)
    service = VectorStoreService(vector_backend=my_backend)

    # With factory fallback
    service = VectorStoreService(collection_name="knowledge_items")

    await service.add_item(item_id, embedding, metadata, document)
    results = await service.search_similar(query_embedding, org_id, n_results=10)
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.services.base import BaseService
from faultmaven.exceptions import (
    VectorStoreConnectionError,
    VectorStoreOperationError,
)
from faultmaven.infrastructure.vector.base import (
    IVectorBackend,
    VectorDocument,
    VectorSearchResult,
)


logger = logging.getLogger(__name__)


class VectorStoreService(BaseService):
    """Service for managing vector embeddings using IVectorBackend.

    This service handles vector storage and retrieval with:
    - Backend-agnostic operations via IVectorBackend interface
    - Cosine similarity metric for semantic search
    - Metadata filtering for organization-level isolation

    Principle 1 Compliance: Uses IVectorBackend interface instead of direct
    ChromaDB imports, making it deployment-agnostic.

    Attributes:
        _backend: IVectorBackend implementation
        collection_name: Name of the collection
    """

    def __init__(
        self,
        vector_backend: Optional[IVectorBackend] = None,
        collection_name: str = "knowledge_items",
    ):
        """Initialize VectorStoreService with vector backend.

        Args:
            vector_backend: IVectorBackend implementation. If None, uses factory.
            collection_name: Name of collection to use
        """
        super().__init__("vector_store_service")
        self.collection_name = collection_name

        try:
            # Use injected backend or get from factory (Principle 1: Deployment Agnostic)
            if vector_backend is not None:
                self._backend = vector_backend
                logger.info(
                    f"Using injected vector backend: {vector_backend.get_backend_type().value}"
                )
            else:
                # Fall back to factory for backwards compatibility
                from faultmaven.infrastructure.vector.factory import get_vector_backend
                self._backend = get_vector_backend()
                logger.info(
                    f"Using factory vector backend: {self._backend.get_backend_type().value}"
                )

            logger.info(
                f"Initialized vector store service with collection '{collection_name}'"
            )

        except Exception as e:
            logger.error(f"Failed to initialize vector backend: {e}")
            raise VectorStoreConnectionError(
                f"Failed to initialize vector backend: {e}",
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
            # Sanitize metadata
            sanitized_metadata = self._sanitize_metadata(metadata)

            # Create VectorDocument and upsert
            vector_doc = VectorDocument(
                id=item_id,
                content=document,
                embedding=embedding,
                metadata=sanitized_metadata,
            )

            await self._backend.upsert([vector_doc], collection=self.collection_name)

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

                # Convert to VectorDocuments
                vector_docs = []
                for item in batch:
                    vector_docs.append(VectorDocument(
                        id=item["item_id"],
                        content=item["document"],
                        embedding=item["embedding"],
                        metadata=self._sanitize_metadata(item["metadata"]),
                    ))

                await self._backend.upsert(vector_docs, collection=self.collection_name)

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
            # Build filter dict for IVectorBackend
            filter_dict = {"organization_id": organization_id}

            if filters:
                for key, value in filters.items():
                    if value is not None:
                        if isinstance(value, (str, int, float, bool)):
                            filter_dict[key] = value
                        elif hasattr(value, 'value'):
                            # Handle enums
                            filter_dict[key] = value.value

            # Check collection count
            count = await self._backend.count(collection=self.collection_name)
            if count == 0:
                return []

            # Search via IVectorBackend interface
            results: List[VectorSearchResult] = await self._backend.search(
                query_embedding=query_embedding,
                top_k=min(n_results, count),
                collection=self.collection_name,
                filter=filter_dict,
            )

            # Format results for compatibility
            formatted_results = []
            for result in results:
                formatted_results.append({
                    "item_id": result.id,
                    "distance": 1 - result.score,  # Convert score to distance
                    "metadata": result.metadata,
                    "document": result.content,
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
            await self._backend.delete([item_id], collection=self.collection_name)

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
            await self._backend.delete(item_ids, collection=self.collection_name)

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
            vector_doc = VectorDocument(
                id=item_id,
                content=document,
                embedding=embedding,
                metadata=sanitized_metadata,
            )

            await self._backend.upsert([vector_doc], collection=self.collection_name)

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
            docs = await self._backend.get([item_id], collection=self.collection_name)

            if not docs:
                return None

            doc = docs[0]
            return {
                "item_id": doc.id,
                "embedding": doc.embedding,
                "metadata": doc.metadata or {},
                "document": doc.content,
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
            count = await self._backend.count(collection=self.collection_name)

            return {
                "collection_name": self.collection_name,
                "backend_type": self._backend.get_backend_type().value,
                "item_count": count,
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
            await self._backend.delete_collection(self.collection_name)
            await self._backend.create_collection(self.collection_name)

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
            # Note: IVectorBackend doesn't have a direct "get by filter" method
            # This is a limitation - proper implementation would need backend extension
            # For now, we return 0 as this operation requires backend-specific code
            logger.warning(
                f"delete_by_organization not fully supported with IVectorBackend interface. "
                f"Consider using backend-specific implementation for org {organization_id}"
            )

            self.log_metric(
                "vector_items_deleted_by_org",
                0,
                unit="count",
                tags={
                    "collection": self.collection_name,
                    "organization_id": organization_id,
                },
            )

            return 0

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
        """Sanitize metadata for vector backend compatibility.

        Vector backends typically only support string, int, float, bool values.

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
            backend_health = await self._backend.health_check()
            store_status = backend_health.get("status", "healthy")
        except Exception as e:
            store_status = "unhealthy"
            stats = {}
            base_health["error"] = str(e)

        base_health.update({
            "store_status": store_status,
            "collection_name": self.collection_name,
            "backend_type": self._backend.get_backend_type().value,
            "item_count": stats.get("item_count", 0),
        })

        return base_health

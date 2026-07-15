"""Knowledge Item Indexing Service.

Orchestrates indexing of knowledge items into the vector store: embedding
generation via the embedding service, vector-store writes, usage tracking, and
indexing statistics. Query-time retrieval is handled elsewhere
(``KnowledgeVectorStore`` for the agent's ``answer_from_kb`` path and
``KnowledgeService.search_knowledge``); this service does not perform search.

Features:
- Knowledge item indexing (embedding generation + vector store)
- Usage tracking (mark items as retrieved)
- Indexing statistics

Usage:
    from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService

    service = KnowledgeSearchService(knowledge_repo, embedding_service, vector_store)
    await service.index_items_batch(items)
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import (
    EmbeddingException,
    KnowledgeBaseException,
    VectorStoreException,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
)

logger = logging.getLogger(__name__)


class KnowledgeSearchService:
    """Service for indexing knowledge items into the vector store.

    This service orchestrates the indexing workflow:
    - Indexing: Generate embeddings and add to vector store
    - Usage tracking: Mark items as retrieved
    - Indexing statistics

    Query-time retrieval is not performed here — see ``KnowledgeVectorStore``
    and ``KnowledgeService.search_knowledge``.

    Attributes:
        knowledge_repo: Repository for knowledge item persistence
        embedding_service: Service for generating embeddings
        vector_store: Service for vector storage
    """

    def __init__(
        self,
        knowledge_repo: KnowledgeItemRepository,
        embedding_service: Any,
        vector_store: Any,
    ):
        """Initialize knowledge indexing service.

        Args:
            knowledge_repo: Repository for knowledge item persistence
            embedding_service: Service for generating embeddings (required)
            vector_store: Service for vector storage (required)

        Raises:
            ValueError: If required dependencies are not provided
        """
        self.knowledge_repo = knowledge_repo

        # Require explicit dependency injection
        if embedding_service is None:
            raise ValueError("embedding_service is required for KnowledgeSearchService")
        if vector_store is None:
            raise ValueError("vector_store is required for KnowledgeSearchService")

        self.embedding_service = embedding_service
        self.vector_store = vector_store

    async def index_item(self, item: KnowledgeItem) -> None:
        """Index a knowledge item (generate embedding + add to vector store).

        Workflow:
        1. Generate embedding for item.content
        2. Update item.embedding_vector
        3. Save item to repository
        4. Add to vector store with metadata

        Args:
            item: Knowledge item to index

        Raises:
            EmbeddingException: If embedding generation fails
            VectorStoreException: If vector store operation fails
            KnowledgeBaseException: If repository operation fails
        """
        return await self._index_item_impl(item)

    async def _index_item_impl(self, item: KnowledgeItem) -> None:
        """Internal implementation of index_item."""
        # Step 1: Generate embedding
        try:
            # Combine title and content for better embedding
            text_to_embed = f"{item.title}\n\n{item.content}"
            embedding = await self.embedding_service.generate_embedding(text_to_embed)
        except EmbeddingException:
            raise
        except Exception as e:
            raise KnowledgeBaseException(
                f"Failed to generate embedding for item {item.item_id}: {e}",
                details={"item_id": item.item_id, "error_type": type(e).__name__},
            ) from e

        # Step 2: Update item with embedding
        item.set_embedding(
            vector=embedding,
            model=self.embedding_service.model,
            version=item.embedding_version,
        )

        # Step 3: Save to repository
        try:
            await self.knowledge_repo.update(item)
        except Exception as e:
            raise KnowledgeBaseException(
                f"Failed to save item with embedding: {e}",
                details={"item_id": item.item_id, "error_type": type(e).__name__},
            ) from e

        # Step 4: Add to vector store
        try:
            metadata = {
                "organization_id": item.organization_id,
                "item_type": item.item_type.value,
                "category": item.category or "",
                "is_published": item.is_published,
                "language": item.language,
            }

            await self.vector_store.add_item(
                item_id=item.item_id,
                embedding=embedding,
                metadata=metadata,
                document=text_to_embed,
            )
        except VectorStoreException:
            raise
        except Exception as e:
            raise KnowledgeBaseException(
                f"Failed to add item to vector store: {e}",
                details={"item_id": item.item_id, "error_type": type(e).__name__},
            ) from e

        logger.info("Business event: knowledge_item_indexed")

    async def index_items_batch(
        self,
        items: List[KnowledgeItem],
        batch_size: int = 50,
    ) -> Dict[str, Any]:
        """Index multiple knowledge items in batches.

        Args:
            items: List of knowledge items to index
            batch_size: Number of items per batch for embedding generation

        Returns:
            Dictionary with indexing statistics

        Raises:
            EmbeddingException: If embedding generation fails
            VectorStoreException: If vector store operation fails
        """
        return await self._index_items_batch_impl(items, batch_size)

    async def _index_items_batch_impl(
        self,
        items: List[KnowledgeItem],
        batch_size: int,
    ) -> Dict[str, Any]:
        """Internal implementation of batch indexing."""
        if not items:
            return {"processed": 0, "succeeded": 0, "failed": 0}

        succeeded = 0
        failed = 0
        failed_items: List[str] = []

        # Process in batches
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]

            # Generate embeddings for batch
            texts = [f"{item.title}\n\n{item.content}" for item in batch]

            try:
                embeddings = await self.embedding_service.generate_embeddings_batch(
                    texts
                )
            except Exception as e:
                logger.error(f"Failed to generate batch embeddings: {e}", exc_info=True)
                failed += len(batch)
                failed_items.extend([item.item_id for item in batch])
                continue

            # Process each item
            vector_store_items = []
            for j, (item, embedding) in enumerate(zip(batch, embeddings)):
                try:
                    # Update item with embedding
                    item.set_embedding(
                        vector=embedding,
                        model=self.embedding_service.model,
                        version=item.embedding_version,
                    )
                    await self.knowledge_repo.update(item)

                    # Prepare for vector store batch add
                    metadata = {
                        "organization_id": item.organization_id,
                        "item_type": item.item_type.value,
                        "category": item.category or "",
                        "is_published": item.is_published,
                        "language": item.language,
                    }
                    vector_store_items.append(
                        {
                            "item_id": item.item_id,
                            "embedding": embedding,
                            "metadata": metadata,
                            "document": texts[j],
                        }
                    )
                    succeeded += 1
                except Exception as e:
                    logger.error(
                        f"Failed to index item {item.item_id}: {e}", exc_info=True
                    )
                    failed += 1
                    failed_items.append(item.item_id)

            # Add to vector store in batch
            if vector_store_items:
                try:
                    await self.vector_store.add_items_batch(vector_store_items)
                except Exception as e:
                    logger.error(
                        f"Failed to add batch to vector store: {e}", exc_info=True
                    )
                    # Items are already in repo, just log the error

        logger.info("Metric: batch_indexing_completed")

        return {
            "processed": len(items),
            "succeeded": succeeded,
            "failed": failed,
            "failed_items": failed_items[:10] if failed_items else [],
        }

    async def reindex_item(self, item_id: str) -> None:
        """Regenerate embedding and reindex an item.

        Args:
            item_id: ID of item to reindex

        Raises:
            KnowledgeBaseException: If item not found
            EmbeddingException: If embedding generation fails
            VectorStoreException: If vector store operation fails
        """
        return await self._reindex_item_impl(item_id)

    async def _reindex_item_impl(self, item_id: str) -> None:
        """Internal implementation of reindex_item."""
        # Fetch item
        item = await self.knowledge_repo.get_by_id(item_id)
        if not item:
            raise KnowledgeBaseException(
                f"Item not found: {item_id}",
                details={"item_id": item_id},
            )

        # Increment embedding version
        item.embedding_version += 1

        # Index the item
        await self._index_item_impl(item)

        logger.info("Business event: knowledge_item_reindexed")

    async def delete_item(self, item_id: str) -> bool:
        """Delete item from repository and vector store.

        Args:
            item_id: ID of item to delete

        Returns:
            True if item was deleted, False if not found

        Raises:
            VectorStoreException: If vector store operation fails
            KnowledgeBaseException: If repository operation fails
        """
        return await self._delete_item_impl(item_id)

    async def _delete_item_impl(self, item_id: str) -> bool:
        """Internal implementation of delete_item."""
        # Delete from vector store first
        try:
            await self.vector_store.delete_item(item_id)
        except Exception as e:
            logger.warning(f"Failed to delete from vector store (may not exist): {e}")

        # Delete from repository
        try:
            deleted = await self.knowledge_repo.delete(item_id)
        except Exception as e:
            raise KnowledgeBaseException(
                f"Failed to delete item from repository: {e}",
                details={"item_id": item_id, "error_type": type(e).__name__},
            ) from e

        if deleted:
            logger.info("Business event: knowledge_item_deleted")

        return deleted

    async def get_indexing_stats(
        self,
        organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get indexing statistics.

        Args:
            organization_id: Optional filter by organization

        Returns:
            Dictionary with indexing statistics:
            - total_items: Total items in repository
            - indexed_items: Items with embeddings
            - pending_items: Items without embeddings
            - vector_store_count: Items in vector store
        """
        return await self._get_indexing_stats_impl(organization_id)

    async def _get_indexing_stats_impl(
        self,
        organization_id: Optional[str],
    ) -> Dict[str, Any]:
        """Internal implementation of get_indexing_stats."""
        # Get vector store stats
        try:
            vector_stats = await self.vector_store.get_collection_stats()
            vector_count = vector_stats.get("item_count", 0)
        except Exception as e:
            logger.warning(f"Failed to get vector store stats: {e}")
            vector_count = -1

        # Get repository stats
        if organization_id:
            try:
                total_items = await self.knowledge_repo.count_by_organization_id(
                    organization_id
                )
                pending_items = await self.knowledge_repo.get_items_without_embeddings(
                    organization_id=organization_id,
                    limit=10000,
                )
                pending_count = len(pending_items)
            except Exception as e:
                logger.warning(f"Failed to get repository stats: {e}")
                total_items = -1
                pending_count = -1
        else:
            # No organization filter - return vector store stats only
            total_items = -1
            pending_count = -1

        indexed_count = (
            total_items - pending_count
            if total_items >= 0 and pending_count >= 0
            else -1
        )

        return {
            "organization_id": organization_id,
            "total_items": total_items,
            "indexed_items": indexed_count,
            "pending_items": pending_count,
            "vector_store_count": vector_count,
            "embedding_model": self.embedding_service.model,
            "tokens_used": self.embedding_service.get_total_tokens(),
        }

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check for the knowledge search service.

        Returns:
            Dictionary with health status
        """
        base_health = {"service": "knowledge_search_service", "status": "healthy"}

        # Check embedding service
        try:
            embedding_health = await self.embedding_service.health_check()
            embedding_status = embedding_health.get("api_status", "unknown")
        except Exception as e:
            embedding_status = "unhealthy"
            base_health["embedding_error"] = str(e)

        # Check vector store
        try:
            vector_health = await self.vector_store.health_check()
            vector_status = vector_health.get("store_status", "unknown")
        except Exception as e:
            vector_status = "unhealthy"
            base_health["vector_store_error"] = str(e)

        base_health.update(
            {
                "embedding_status": embedding_status,
                "vector_store_status": vector_status,
                "overall_status": (
                    "healthy"
                    if embedding_status == "healthy" and vector_status == "healthy"
                    else "degraded"
                ),
            }
        )

        return base_health

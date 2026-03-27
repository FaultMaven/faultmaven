"""Knowledge Search Service for semantic and hybrid search.

This service orchestrates the full search workflow for the RAG system,
combining embedding generation, vector similarity search, and text search.

Features:
- Semantic search using vector similarity
- Hybrid search combining semantic + full-text results
- Knowledge item indexing (embedding generation + vector store)
- Usage tracking (mark items as retrieved)
- Indexing statistics

Usage:
    from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService

    service = KnowledgeSearchService(knowledge_repo, embedding_service, vector_store)
    results = await service.semantic_search("error handling", organization_id, n_results=10)
    results = await service.hybrid_search("database timeout", organization_id)
"""

import logging
from typing import Any

from faultmaven.exceptions import (
    EmbeddingException,
    KnowledgeBaseException,
    VectorStoreException,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepository,
)

logger = logging.getLogger(__name__)


class KnowledgeSearchService:
    """Service for searching knowledge base with hybrid search.

    This service orchestrates the full search workflow:
    - Semantic search: Generate query embedding, search vector store
    - Hybrid search: Combine semantic + text search with weighted scores
    - Indexing: Generate embeddings and add to vector store
    - Usage tracking: Mark items as retrieved

    Attributes:
        knowledge_repo: Repository for knowledge item persistence
        embedding_service: Service for generating embeddings
        vector_store: Service for vector storage and search
        semantic_weight: Default weight for semantic search in hybrid mode
        text_weight: Default weight for text search in hybrid mode
    """

    def __init__(
        self,
        knowledge_repo: KnowledgeItemRepository,
        embedding_service: Any,
        vector_store: Any,
        semantic_weight: float = 0.7,
        text_weight: float = 0.3,
    ):
        """Initialize knowledge search service.

        Args:
            knowledge_repo: Repository for knowledge item persistence
            embedding_service: Service for generating embeddings (required)
            vector_store: Service for vector storage and search (required)
            semantic_weight: Default weight for semantic search (0.0-1.0)
            text_weight: Default weight for text search (0.0-1.0)

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

        self.semantic_weight = semantic_weight
        self.text_weight = text_weight

    async def semantic_search(
        self,
        query: str,
        organization_id: str,
        n_results: int = 10,
        item_type: KnowledgeItemType | None = None,
        category: str | None = None,
        mark_retrieved: bool = True,
    ) -> list[tuple[KnowledgeItem, float]]:
        """Semantic search using vector similarity.

        Workflow:
        1. Generate embedding for query
        2. Search vector store for similar items
        3. Fetch full KnowledgeItem objects from repository
        4. Mark items as retrieved (usage tracking)
        5. Return sorted by similarity

        Args:
            query: Search query text
            organization_id: Organization to search within
            n_results: Number of results to return
            item_type: Optional filter by item type
            category: Optional filter by category
            mark_retrieved: Whether to mark items as retrieved

        Returns:
            List of (KnowledgeItem, similarity_score) tuples sorted by similarity

        Raises:
            EmbeddingException: If embedding generation fails
            VectorStoreException: If vector search fails
            KnowledgeBaseException: If item retrieval fails
        """
        return await self._semantic_search_impl(
            query,
            organization_id,
            n_results,
            item_type,
            category,
            mark_retrieved,
        )

    async def _semantic_search_impl(
        self,
        query: str,
        organization_id: str,
        n_results: int,
        item_type: KnowledgeItemType | None,
        category: str | None,
        mark_retrieved: bool,
    ) -> list[tuple[KnowledgeItem, float]]:
        """Internal implementation of semantic search."""
        # Step 1: Generate embedding for query
        try:
            query_embedding = await self.embedding_service.generate_embedding(query)
        except EmbeddingException:
            raise
        except Exception as e:
            raise KnowledgeBaseException(
                f"Failed to generate query embedding: {e}",
                details={"query": query[:100], "error_type": type(e).__name__},
            ) from e

        # Step 2: Build filters
        filters = {}
        if item_type:
            filters["item_type"] = item_type.value
        if category:
            filters["category"] = category

        # Step 3: Search vector store
        try:
            vector_results = await self.vector_store.search_similar(
                query_embedding=query_embedding,
                organization_id=organization_id,
                n_results=n_results,
                filters=filters if filters else None,
            )
        except VectorStoreException:
            raise
        except Exception as e:
            raise KnowledgeBaseException(
                f"Failed to search vector store: {e}",
                details={
                    "organization_id": organization_id,
                    "error_type": type(e).__name__,
                },
            ) from e

        # Step 4: Fetch full KnowledgeItem objects
        results: list[tuple[KnowledgeItem, float]] = []
        for result in vector_results:
            item_id = result["item_id"]
            distance = result.get("distance", 0.0)
            # Convert distance to similarity (for cosine: similarity = 1 - distance)
            similarity = 1.0 - distance if distance is not None else 0.0

            try:
                item = await self.knowledge_repo.get_by_id(item_id)
                if item:
                    # Step 5: Mark as retrieved
                    if mark_retrieved:
                        item.mark_retrieved()
                        await self.knowledge_repo.update(item)

                    results.append((item, similarity))
            except Exception as e:
                logger.warning(f"Failed to fetch item {item_id}: {e}")
                continue

        logger.info("Metric: semantic_search_performed")

        return results

    async def hybrid_search(
        self,
        query: str,
        organization_id: str,
        n_results: int = 10,
        semantic_weight: float | None = None,
        text_weight: float | None = None,
        item_type: KnowledgeItemType | None = None,
        category: str | None = None,
        mark_retrieved: bool = True,
    ) -> list[tuple[KnowledgeItem, float]]:
        """Hybrid search combining semantic + full-text search.

        Uses weighted score combination:
        - Semantic similarity: 70% (default)
        - Full-text relevance: 30% (default)

        Args:
            query: Search query text
            organization_id: Organization to search within
            n_results: Number of results to return
            semantic_weight: Weight for semantic similarity (0.0-1.0)
            text_weight: Weight for full-text search (0.0-1.0)
            item_type: Optional filter by item type
            category: Optional filter by category
            mark_retrieved: Whether to mark items as retrieved

        Returns:
            List of (KnowledgeItem, combined_score) tuples sorted by score

        Raises:
            EmbeddingException: If embedding generation fails
            VectorStoreException: If vector search fails
            KnowledgeBaseException: If item retrieval fails
        """
        return await self._hybrid_search_impl(
            query,
            organization_id,
            n_results,
            semantic_weight,
            text_weight,
            item_type,
            category,
            mark_retrieved,
        )

    async def _hybrid_search_impl(
        self,
        query: str,
        organization_id: str,
        n_results: int,
        semantic_weight: float | None,
        text_weight: float | None,
        item_type: KnowledgeItemType | None,
        category: str | None,
        mark_retrieved: bool,
    ) -> list[tuple[KnowledgeItem, float]]:
        """Internal implementation of hybrid search."""
        # Use default weights if not provided
        sem_weight = (
            semantic_weight if semantic_weight is not None else self.semantic_weight
        )
        txt_weight = text_weight if text_weight is not None else self.text_weight

        # Normalize weights
        total_weight = sem_weight + txt_weight
        if total_weight > 0:
            sem_weight = sem_weight / total_weight
            txt_weight = txt_weight / total_weight

        # Get semantic search results (fetch more for better coverage)
        semantic_results = await self._semantic_search_impl(
            query=query,
            organization_id=organization_id,
            n_results=n_results * 2,  # Fetch more for merging
            item_type=item_type,
            category=category,
            mark_retrieved=False,  # We'll mark later
        )

        # Get text search results
        try:
            text_results = await self.knowledge_repo.search_by_text(
                organization_id=organization_id,
                query=query,
                item_type=item_type,
                limit=n_results * 2,
            )
        except Exception as e:
            logger.warning(f"Text search failed, using semantic only: {e}")
            text_results = []

        # Combine results with weighted scores
        item_scores: dict[str, dict[str, Any]] = {}

        # Add semantic results with normalized scores
        max_semantic_score = max((s for _, s in semantic_results), default=1.0)
        for item, score in semantic_results:
            normalized_score = (
                score / max_semantic_score if max_semantic_score > 0 else 0
            )
            item_scores[item.item_id] = {
                "item": item,
                "semantic_score": normalized_score * sem_weight,
                "text_score": 0.0,
            }

        # Add text results with position-based scores
        # Text search doesn't return scores, so use position-based scoring
        for i, item in enumerate(text_results):
            position_score = 1.0 - (i / max(len(text_results), 1))
            if item.item_id in item_scores:
                item_scores[item.item_id]["text_score"] = position_score * txt_weight
            else:
                item_scores[item.item_id] = {
                    "item": item,
                    "semantic_score": 0.0,
                    "text_score": position_score * txt_weight,
                }

        # Calculate combined scores and sort
        combined_results: list[tuple[KnowledgeItem, float]] = []
        for data in item_scores.values():
            combined_score = data["semantic_score"] + data["text_score"]
            combined_results.append((data["item"], combined_score))

        # Sort by combined score descending
        combined_results.sort(key=lambda x: x[1], reverse=True)

        # Limit to n_results
        combined_results = combined_results[:n_results]

        # Mark as retrieved
        if mark_retrieved:
            for item, _ in combined_results:
                try:
                    item.mark_retrieved()
                    await self.knowledge_repo.update(item)
                except Exception as e:
                    logger.warning(
                        f"Failed to mark item {item.item_id} as retrieved: {e}"
                    )

        logger.info("Metric: hybrid_search_performed")

        return combined_results

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
        items: list[KnowledgeItem],
        batch_size: int = 50,
    ) -> dict[str, Any]:
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
        items: list[KnowledgeItem],
        batch_size: int,
    ) -> dict[str, Any]:
        """Internal implementation of batch indexing."""
        if not items:
            return {"processed": 0, "succeeded": 0, "failed": 0}

        succeeded = 0
        failed = 0
        failed_items: list[str] = []

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
            for j, (item, embedding) in enumerate(zip(batch, embeddings, strict=False)):
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
        organization_id: str | None = None,
    ) -> dict[str, Any]:
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
        organization_id: str | None,
    ) -> dict[str, Any]:
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

    async def health_check(self) -> dict[str, Any]:
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

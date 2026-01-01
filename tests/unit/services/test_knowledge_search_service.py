"""Unit tests for KnowledgeSearchService.

Tests cover:
- Semantic search workflow
- Hybrid search with weighted scores
- Item indexing
- Batch indexing
- Reindexing
- Deletion
- Indexing statistics
- Error handling
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import List, Dict, Any

from faultmaven.modules.knowledge.domain.services.search_service import KnowledgeSearchService
from faultmaven.modules.knowledge.domain.services.embedding_service import EmbeddingService
from faultmaven.modules.knowledge.domain.services.vector_store_service import VectorStoreService
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    InMemoryKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
    EMBEDDING_DIMENSIONS,
)
from faultmaven.exceptions import (
    EmbeddingGenerationError,
    VectorStoreOperationError,
    KnowledgeBaseException,
)


# Test fixtures
@pytest.fixture
def mock_embedding_service():
    """Create mock embedding service."""
    service = MagicMock(spec=EmbeddingService)
    service.model = "text-embedding-3-small"
    service.dimensions = EMBEDDING_DIMENSIONS
    service.generate_embedding = AsyncMock(return_value=create_embedding())
    service.generate_embeddings_batch = AsyncMock(
        side_effect=lambda texts: [create_embedding() for _ in texts]
    )
    service.get_total_tokens = MagicMock(return_value=0)
    service.health_check = AsyncMock(return_value={"api_status": "healthy"})
    return service


@pytest.fixture
def mock_vector_store():
    """Create mock vector store service."""
    store = MagicMock(spec=VectorStoreService)
    store.add_item = AsyncMock()
    store.add_items_batch = AsyncMock()
    store.search_similar = AsyncMock(return_value=[])
    store.delete_item = AsyncMock()
    store.get_collection_stats = AsyncMock(return_value={"item_count": 0})
    store.health_check = AsyncMock(return_value={"store_status": "healthy"})
    return store


@pytest.fixture
def knowledge_repo():
    """Create in-memory knowledge item repository."""
    return InMemoryKnowledgeItemRepository()


@pytest.fixture
def search_service(knowledge_repo, mock_embedding_service, mock_vector_store):
    """Create knowledge search service with mocked dependencies."""
    return KnowledgeSearchService(
        knowledge_repo=knowledge_repo,
        embedding_service=mock_embedding_service,
        vector_store=mock_vector_store,
        semantic_weight=0.7,
        text_weight=0.3,
    )


def create_embedding(value: float = 0.1) -> List[float]:
    """Create test embedding vector."""
    return [value] * EMBEDDING_DIMENSIONS


def create_knowledge_item(
    item_id: str = "item_1",
    organization_id: str = "org_1",
    title: str = "Test Item",
    content: str = "Test content for the knowledge item.",
    item_type: KnowledgeItemType = KnowledgeItemType.FAQ,
    category: str = "general",
    is_published: bool = True,
    embedding_vector: List[float] = None,
) -> KnowledgeItem:
    """Create test knowledge item."""
    return KnowledgeItem(
        item_id=item_id,
        organization_id=organization_id,
        title=title,
        content=content,
        item_type=item_type,
        category=category,
        is_published=is_published,
        embedding_vector=embedding_vector,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# =============================================================================
# Test: semantic_search() - Success Cases
# =============================================================================

class TestSemanticSearchSuccess:
    """Tests for successful semantic search."""

    @pytest.mark.asyncio
    async def test_semantic_search_returns_results(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test semantic search returns matching items."""
        # Create and store test items
        items = [
            create_knowledge_item(item_id=f"item_{i}", organization_id="org_1")
            for i in range(3)
        ]
        for item in items:
            await knowledge_repo.create(item)

        # Mock vector store results
        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "item_0", "distance": 0.1, "metadata": {}, "document": ""},
            {"item_id": "item_1", "distance": 0.2, "metadata": {}, "document": ""},
        ])

        results = await search_service.semantic_search(
            query="test query",
            organization_id="org_1",
            n_results=10,
        )

        assert len(results) == 2
        assert results[0][0].item_id == "item_0"
        assert results[0][1] == 0.9  # 1 - 0.1 distance

    @pytest.mark.asyncio
    async def test_semantic_search_with_item_type_filter(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test semantic search with item type filter."""
        await knowledge_repo.create(
            create_knowledge_item(item_id="faq_1", item_type=KnowledgeItemType.FAQ)
        )

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "faq_1", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        results = await search_service.semantic_search(
            query="test",
            organization_id="org_1",
            item_type=KnowledgeItemType.FAQ,
        )

        assert len(results) == 1
        # Verify filter was passed to vector store
        call_kwargs = mock_vector_store.search_similar.call_args.kwargs
        assert call_kwargs["filters"]["item_type"] == "faq"

    @pytest.mark.asyncio
    async def test_semantic_search_with_category_filter(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test semantic search with category filter."""
        await knowledge_repo.create(
            create_knowledge_item(item_id="net_1", category="networking")
        )

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "net_1", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        results = await search_service.semantic_search(
            query="test",
            organization_id="org_1",
            category="networking",
        )

        call_kwargs = mock_vector_store.search_similar.call_args.kwargs
        assert call_kwargs["filters"]["category"] == "networking"

    @pytest.mark.asyncio
    async def test_semantic_search_marks_items_retrieved(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test that search marks items as retrieved."""
        item = create_knowledge_item(item_id="retrieve_test")
        await knowledge_repo.create(item)

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "retrieve_test", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        await search_service.semantic_search(
            query="test",
            organization_id="org_1",
            mark_retrieved=True,
        )

        updated = await knowledge_repo.get_by_id("retrieve_test")
        assert updated.view_count == 1
        assert updated.last_retrieved_at is not None

    @pytest.mark.asyncio
    async def test_semantic_search_skip_mark_retrieved(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test semantic search without marking as retrieved."""
        item = create_knowledge_item(item_id="no_mark_test")
        await knowledge_repo.create(item)

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "no_mark_test", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        await search_service.semantic_search(
            query="test",
            organization_id="org_1",
            mark_retrieved=False,
        )

        updated = await knowledge_repo.get_by_id("no_mark_test")
        assert updated.view_count == 0

    @pytest.mark.asyncio
    async def test_semantic_search_empty_results(
        self, search_service, mock_vector_store
    ):
        """Test semantic search with no results."""
        mock_vector_store.search_similar = AsyncMock(return_value=[])

        results = await search_service.semantic_search(
            query="nonexistent",
            organization_id="org_1",
        )

        assert results == []


# =============================================================================
# Test: hybrid_search() - Success Cases
# =============================================================================

class TestHybridSearchSuccess:
    """Tests for successful hybrid search."""

    @pytest.mark.asyncio
    async def test_hybrid_search_combines_results(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test hybrid search combines semantic and text results."""
        # Create items
        item1 = create_knowledge_item(item_id="semantic_hit", content="Semantic match")
        item2 = create_knowledge_item(item_id="text_hit", content="Text search match")
        await knowledge_repo.create(item1)
        await knowledge_repo.create(item2)

        # Mock vector store (semantic) results
        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "semantic_hit", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        results = await search_service.hybrid_search(
            query="match",
            organization_id="org_1",
            n_results=10,
        )

        # Should include results from both semantic and text search
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_hybrid_search_custom_weights(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test hybrid search with custom weights."""
        item = create_knowledge_item(item_id="weight_test", content="test content")
        await knowledge_repo.create(item)

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "weight_test", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        results = await search_service.hybrid_search(
            query="test",
            organization_id="org_1",
            semantic_weight=0.9,
            text_weight=0.1,
        )

        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_hybrid_search_respects_n_results(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test hybrid search respects n_results limit."""
        for i in range(10):
            await knowledge_repo.create(
                create_knowledge_item(item_id=f"limit_{i}", content="test content")
            )

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": f"limit_{i}", "distance": 0.1 * i, "metadata": {}, "document": ""}
            for i in range(10)
        ])

        results = await search_service.hybrid_search(
            query="test",
            organization_id="org_1",
            n_results=3,
        )

        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_hybrid_search_marks_retrieved(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test hybrid search marks items as retrieved."""
        item = create_knowledge_item(item_id="hybrid_retrieve")
        await knowledge_repo.create(item)

        mock_vector_store.search_similar = AsyncMock(return_value=[
            {"item_id": "hybrid_retrieve", "distance": 0.1, "metadata": {}, "document": ""},
        ])

        await search_service.hybrid_search(
            query="test",
            organization_id="org_1",
            mark_retrieved=True,
        )

        updated = await knowledge_repo.get_by_id("hybrid_retrieve")
        assert updated.view_count == 1


# =============================================================================
# Test: index_item()
# =============================================================================

class TestIndexItem:
    """Tests for item indexing."""

    @pytest.mark.asyncio
    async def test_index_item_success(
        self, search_service, knowledge_repo, mock_embedding_service, mock_vector_store
    ):
        """Test successful item indexing."""
        item = create_knowledge_item(item_id="index_test")
        await knowledge_repo.create(item)

        await search_service.index_item(item)

        # Verify embedding was generated
        mock_embedding_service.generate_embedding.assert_called_once()

        # Verify item was updated with embedding
        updated = await knowledge_repo.get_by_id("index_test")
        assert updated.has_embedding()

        # Verify item was added to vector store
        mock_vector_store.add_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_item_combines_title_and_content(
        self, search_service, knowledge_repo, mock_embedding_service, mock_vector_store
    ):
        """Test that indexing combines title and content."""
        item = create_knowledge_item(
            item_id="combine_test",
            title="Important Title",
            content="Detailed content here",
        )
        await knowledge_repo.create(item)

        await search_service.index_item(item)

        call_args = mock_embedding_service.generate_embedding.call_args[0]
        assert "Important Title" in call_args[0]
        assert "Detailed content here" in call_args[0]

    @pytest.mark.asyncio
    async def test_index_item_sets_embedding_model(
        self, search_service, knowledge_repo, mock_embedding_service, mock_vector_store
    ):
        """Test that indexing sets the embedding model."""
        item = create_knowledge_item(item_id="model_test")
        await knowledge_repo.create(item)

        await search_service.index_item(item)

        updated = await knowledge_repo.get_by_id("model_test")
        assert updated.embedding_model == "text-embedding-3-small"

    @pytest.mark.asyncio
    async def test_index_item_includes_metadata(
        self, search_service, knowledge_repo, mock_embedding_service, mock_vector_store
    ):
        """Test that indexing includes proper metadata."""
        item = create_knowledge_item(
            item_id="meta_test",
            organization_id="org_123",
            item_type=KnowledgeItemType.TROUBLESHOOTING_GUIDE,
            category="networking",
        )
        await knowledge_repo.create(item)

        await search_service.index_item(item)

        call_kwargs = mock_vector_store.add_item.call_args.kwargs
        metadata = call_kwargs["metadata"]
        assert metadata["organization_id"] == "org_123"
        assert metadata["item_type"] == "troubleshooting_guide"
        assert metadata["category"] == "networking"


# =============================================================================
# Test: index_items_batch()
# =============================================================================

class TestIndexItemsBatch:
    """Tests for batch indexing."""

    @pytest.mark.asyncio
    async def test_index_items_batch_success(
        self, search_service, knowledge_repo, mock_embedding_service, mock_vector_store
    ):
        """Test successful batch indexing."""
        items = [
            create_knowledge_item(item_id=f"batch_{i}")
            for i in range(5)
        ]
        for item in items:
            await knowledge_repo.create(item)

        result = await search_service.index_items_batch(items)

        assert result["processed"] == 5
        assert result["succeeded"] == 5
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_index_items_batch_empty_list(self, search_service):
        """Test batch indexing with empty list."""
        result = await search_service.index_items_batch([])

        assert result["processed"] == 0
        assert result["succeeded"] == 0
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_index_items_batch_with_embedding_failure(
        self, search_service, knowledge_repo, mock_embedding_service
    ):
        """Test batch indexing handles embedding failures."""
        items = [create_knowledge_item(item_id=f"fail_{i}") for i in range(3)]
        for item in items:
            await knowledge_repo.create(item)

        mock_embedding_service.generate_embeddings_batch = AsyncMock(
            side_effect=EmbeddingGenerationError("API error")
        )

        result = await search_service.index_items_batch(items)

        assert result["failed"] == 3


# =============================================================================
# Test: reindex_item()
# =============================================================================

class TestReindexItem:
    """Tests for item reindexing."""

    @pytest.mark.asyncio
    async def test_reindex_item_success(
        self, search_service, knowledge_repo, mock_embedding_service, mock_vector_store
    ):
        """Test successful item reindexing."""
        item = create_knowledge_item(
            item_id="reindex_test",
            embedding_vector=create_embedding(0.1),
        )
        await knowledge_repo.create(item)
        original_version = item.embedding_version

        await search_service.reindex_item("reindex_test")

        updated = await knowledge_repo.get_by_id("reindex_test")
        assert updated.embedding_version == original_version + 1

    @pytest.mark.asyncio
    async def test_reindex_item_not_found(self, search_service):
        """Test reindexing non-existent item raises error."""
        with pytest.raises(KnowledgeBaseException):
            await search_service.reindex_item("nonexistent")


# =============================================================================
# Test: delete_item()
# =============================================================================

class TestDeleteItem:
    """Tests for item deletion."""

    @pytest.mark.asyncio
    async def test_delete_item_success(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test successful item deletion."""
        item = create_knowledge_item(item_id="delete_test")
        await knowledge_repo.create(item)

        result = await search_service.delete_item("delete_test")

        assert result is True
        mock_vector_store.delete_item.assert_called_once_with("delete_test")
        assert await knowledge_repo.get_by_id("delete_test") is None

    @pytest.mark.asyncio
    async def test_delete_item_not_found(self, search_service, mock_vector_store):
        """Test deleting non-existent item."""
        result = await search_service.delete_item("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_item_handles_vector_store_error(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test delete handles vector store errors gracefully."""
        item = create_knowledge_item(item_id="vs_error_test")
        await knowledge_repo.create(item)

        mock_vector_store.delete_item = AsyncMock(
            side_effect=Exception("Vector store error")
        )

        # Should still delete from repo even if vector store fails
        result = await search_service.delete_item("vs_error_test")
        assert result is True


# =============================================================================
# Test: get_indexing_stats()
# =============================================================================

class TestGetIndexingStats:
    """Tests for indexing statistics."""

    @pytest.mark.asyncio
    async def test_get_indexing_stats_success(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test getting indexing statistics."""
        # Create items with and without embeddings
        item_with_embedding = create_knowledge_item(
            item_id="with_emb",
            embedding_vector=create_embedding(),
        )
        item_without_embedding = create_knowledge_item(
            item_id="without_emb",
            embedding_vector=None,
        )
        await knowledge_repo.create(item_with_embedding)
        await knowledge_repo.create(item_without_embedding)

        mock_vector_store.get_collection_stats = AsyncMock(
            return_value={"item_count": 1}
        )

        stats = await search_service.get_indexing_stats(organization_id="org_1")

        assert stats["organization_id"] == "org_1"
        assert stats["total_items"] == 2
        assert stats["pending_items"] == 1
        assert stats["indexed_items"] == 1
        assert stats["vector_store_count"] == 1

    @pytest.mark.asyncio
    async def test_get_indexing_stats_empty_org(
        self, search_service, mock_vector_store
    ):
        """Test stats for organization with no items."""
        mock_vector_store.get_collection_stats = AsyncMock(
            return_value={"item_count": 0}
        )

        stats = await search_service.get_indexing_stats(organization_id="empty_org")

        assert stats["total_items"] == 0
        assert stats["pending_items"] == 0


# =============================================================================
# Test: Error Handling
# =============================================================================

class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_semantic_search_embedding_error(
        self, search_service, mock_embedding_service
    ):
        """Test semantic search handles embedding generation errors."""
        mock_embedding_service.generate_embedding = AsyncMock(
            side_effect=EmbeddingGenerationError("API error")
        )

        with pytest.raises(EmbeddingGenerationError):
            await search_service.semantic_search(
                query="test",
                organization_id="org_1",
            )

    @pytest.mark.asyncio
    async def test_semantic_search_vector_store_error(
        self, search_service, mock_vector_store
    ):
        """Test semantic search handles vector store errors."""
        mock_vector_store.search_similar = AsyncMock(
            side_effect=VectorStoreOperationError("Search failed")
        )

        with pytest.raises(VectorStoreOperationError):
            await search_service.semantic_search(
                query="test",
                organization_id="org_1",
            )

    @pytest.mark.asyncio
    async def test_index_item_embedding_error(
        self, search_service, knowledge_repo, mock_embedding_service
    ):
        """Test index_item handles embedding generation errors."""
        item = create_knowledge_item(item_id="emb_error")
        await knowledge_repo.create(item)

        mock_embedding_service.generate_embedding = AsyncMock(
            side_effect=EmbeddingGenerationError("API error")
        )

        with pytest.raises(EmbeddingGenerationError):
            await search_service.index_item(item)

    @pytest.mark.asyncio
    async def test_index_item_vector_store_error(
        self, search_service, knowledge_repo, mock_vector_store
    ):
        """Test index_item handles vector store errors."""
        item = create_knowledge_item(item_id="vs_error")
        await knowledge_repo.create(item)

        mock_vector_store.add_item = AsyncMock(
            side_effect=VectorStoreOperationError("Add failed")
        )

        with pytest.raises(VectorStoreOperationError):
            await search_service.index_item(item)


# =============================================================================
# Test: Health Check
# =============================================================================

class TestHealthCheck:
    """Tests for health check."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(
        self, search_service, mock_embedding_service, mock_vector_store
    ):
        """Test health check when all services are healthy."""
        health = await search_service.health_check()

        assert health["status"] == "healthy"
        assert health["embedding_status"] == "healthy"
        assert health["vector_store_status"] == "healthy"
        assert health["overall_status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_check_degraded_embedding(
        self, search_service, mock_embedding_service, mock_vector_store
    ):
        """Test health check with degraded embedding service."""
        mock_embedding_service.health_check = AsyncMock(
            return_value={"api_status": "unhealthy"}
        )

        health = await search_service.health_check()

        assert health["embedding_status"] == "unhealthy"
        assert health["overall_status"] == "degraded"

    @pytest.mark.asyncio
    async def test_health_check_degraded_vector_store(
        self, search_service, mock_embedding_service, mock_vector_store
    ):
        """Test health check with degraded vector store."""
        mock_vector_store.health_check = AsyncMock(
            return_value={"store_status": "unhealthy"}
        )

        health = await search_service.health_check()

        assert health["vector_store_status"] == "unhealthy"
        assert health["overall_status"] == "degraded"

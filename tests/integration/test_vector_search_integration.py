"""Integration tests for Vector Search.

Tests cover:
- End-to-end semantic search workflow
- Hybrid search combining results
- Indexing job processing
- Organization isolation
- Multiple organization scenarios
- Concurrent operations
"""

import shutil
import tempfile
from datetime import UTC, datetime, timezone
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.jobs.knowledge_indexing_job import KnowledgeIndexingJob
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.embedding_service import (
    EmbeddingService,
)
from faultmaven.modules.knowledge.domain.services.search_service import (
    KnowledgeSearchService,
)
from faultmaven.modules.knowledge.domain.services.vector_store_service import (
    VectorStoreService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    InMemoryKnowledgeItemRepository,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def temp_chroma_dir():
    """Create temporary directory for ChromaDB."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_embedding_service():
    """Create mock embedding service that returns deterministic embeddings."""
    service = MagicMock(spec=EmbeddingService)
    service.model = "text-embedding-3-small"
    service.dimensions = EMBEDDING_DIMENSIONS

    def create_deterministic_embedding(text: str) -> list[float]:
        """Create embedding based on text hash for consistency."""
        import hashlib

        hash_val = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        base_val = (hash_val % 1000) / 1000.0
        return [base_val + (i % 100) / 10000 for i in range(EMBEDDING_DIMENSIONS)]

    service.generate_embedding = AsyncMock(
        side_effect=lambda text: create_deterministic_embedding(text)
    )
    service.generate_embeddings_batch = AsyncMock(
        side_effect=lambda texts: [create_deterministic_embedding(t) for t in texts]
    )
    service.get_total_tokens = MagicMock(return_value=0)
    service.health_check = AsyncMock(return_value={"api_status": "healthy"})
    return service


@pytest.fixture
def vector_store(temp_chroma_dir):
    """Create real vector store with temp storage."""
    import chromadb

    client = chromadb.PersistentClient(path=temp_chroma_dir)
    return VectorStoreService(
        client=client,
        collection_name="integration_test",
    )


@pytest.fixture
def knowledge_repo():
    """Create in-memory knowledge repository."""
    return InMemoryKnowledgeItemRepository()


@pytest.fixture
def search_service(knowledge_repo, mock_embedding_service, vector_store):
    """Create knowledge search service with real vector store."""
    return KnowledgeSearchService(
        knowledge_repo=knowledge_repo,
        embedding_service=mock_embedding_service,
        vector_store=vector_store,
        semantic_weight=0.7,
        text_weight=0.3,
    )


@pytest.fixture
def indexing_job(knowledge_repo, search_service):
    """Create indexing job."""
    return KnowledgeIndexingJob(
        knowledge_repo=knowledge_repo,
        search_service=search_service,
        batch_size=10,
    )


def create_knowledge_item(
    item_id: str,
    organization_id: str = "org_1",
    title: str = "Test Item",
    content: str = "Test content",
    item_type: KnowledgeItemType = KnowledgeItemType.FAQ,
    category: str = "general",
) -> KnowledgeItem:
    """Create knowledge item for testing."""
    return KnowledgeItem(
        item_id=item_id,
        organization_id=organization_id,
        title=title,
        content=content,
        item_type=item_type,
        category=category,
        is_published=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# =============================================================================
# Test: End-to-End Semantic Search
# =============================================================================


class TestE2ESemanticSearch:
    """End-to-end tests for semantic search workflow."""

    @pytest.mark.asyncio
    async def test_e2e_semantic_search_full_workflow(
        self, search_service, knowledge_repo
    ):
        """Test complete semantic search workflow."""
        # Step 1: Create knowledge items
        items = [
            create_knowledge_item(
                item_id="python_error",
                title="Python ImportError",
                content="How to fix ImportError in Python: check your PYTHONPATH and module installation.",
            ),
            create_knowledge_item(
                item_id="java_error",
                title="Java NullPointerException",
                content="How to fix NullPointerException in Java: check for null before accessing.",
            ),
            create_knowledge_item(
                item_id="database_timeout",
                title="Database Connection Timeout",
                content="How to handle database connection timeouts: increase timeout, use connection pooling.",
            ),
        ]

        for item in items:
            await knowledge_repo.create(item)

        # Step 2: Index items
        for item in items:
            await search_service.index_item(item)

        # Step 3: Search for relevant items
        results = await search_service.semantic_search(
            query="How to fix Python ImportError",
            organization_id="org_1",
            n_results=10,
        )

        # Step 4: Verify results
        assert len(results) >= 1
        # First result should be the most relevant
        assert any(r[0].item_id == "python_error" for r in results)

    @pytest.mark.asyncio
    async def test_e2e_semantic_search_with_filters(
        self, search_service, knowledge_repo
    ):
        """Test semantic search with item type and category filters."""
        items = [
            create_knowledge_item(
                item_id="faq_network",
                item_type=KnowledgeItemType.FAQ,
                category="networking",
                content="Network FAQ content",
            ),
            create_knowledge_item(
                item_id="guide_network",
                item_type=KnowledgeItemType.TROUBLESHOOTING_GUIDE,
                category="networking",
                content="Network guide content",
            ),
            create_knowledge_item(
                item_id="faq_database",
                item_type=KnowledgeItemType.FAQ,
                category="database",
                content="Database FAQ content",
            ),
        ]

        for item in items:
            await knowledge_repo.create(item)
            await search_service.index_item(item)

        # Search with item_type filter
        results = await search_service.semantic_search(
            query="network",
            organization_id="org_1",
            item_type=KnowledgeItemType.FAQ,
        )

        assert len(results) >= 1
        assert all(r[0].item_type == KnowledgeItemType.FAQ for r in results)

    @pytest.mark.asyncio
    async def test_e2e_semantic_search_usage_tracking(
        self, search_service, knowledge_repo
    ):
        """Test that search tracks item usage."""
        item = create_knowledge_item(item_id="track_usage")
        await knowledge_repo.create(item)
        await search_service.index_item(item)

        # Search multiple times
        for _ in range(3):
            await search_service.semantic_search(
                query="track usage",
                organization_id="org_1",
                mark_retrieved=True,
            )

        # Verify view count
        updated = await knowledge_repo.get_by_id("track_usage")
        assert updated.view_count == 3


# =============================================================================
# Test: Hybrid Search
# =============================================================================


class TestHybridSearch:
    """Tests for hybrid search combining semantic and text results."""

    @pytest.mark.asyncio
    async def test_hybrid_search_combines_results(self, search_service, knowledge_repo):
        """Test hybrid search merges semantic and text results."""
        items = [
            create_knowledge_item(
                item_id="semantic_match",
                title="Error handling best practices",
                content="Guide to handling errors in production systems",
            ),
            create_knowledge_item(
                item_id="text_match",
                title="database connection",
                content="How to establish database connection in Python",
            ),
        ]

        for item in items:
            await knowledge_repo.create(item)
            await search_service.index_item(item)

        results = await search_service.hybrid_search(
            query="database connection",
            organization_id="org_1",
            n_results=10,
        )

        assert len(results) >= 1
        # Should find the text match
        item_ids = [r[0].item_id for r in results]
        assert "text_match" in item_ids

    @pytest.mark.asyncio
    async def test_hybrid_search_score_combination(
        self, search_service, knowledge_repo
    ):
        """Test hybrid search combines scores correctly."""
        item = create_knowledge_item(
            item_id="combo_test",
            content="Test content for score combination",
        )
        await knowledge_repo.create(item)
        await search_service.index_item(item)

        results = await search_service.hybrid_search(
            query="score combination",
            organization_id="org_1",
            semantic_weight=0.5,
            text_weight=0.5,
        )

        assert len(results) >= 1
        # Score should be between 0 and 1
        assert 0 <= results[0][1] <= 1

    @pytest.mark.asyncio
    async def test_hybrid_search_weight_parameters(
        self, search_service, knowledge_repo
    ):
        """Test that weight parameters affect results."""
        items = [
            create_knowledge_item(
                item_id="semantic_strong",
                content="This has strong semantic relevance to the query",
            ),
            create_knowledge_item(
                item_id="text_strong",
                content="exact keyword match database timeout",
            ),
        ]

        for item in items:
            await knowledge_repo.create(item)
            await search_service.index_item(item)

        # With high semantic weight
        sem_results = await search_service.hybrid_search(
            query="database timeout",
            organization_id="org_1",
            semantic_weight=0.9,
            text_weight=0.1,
        )

        # With high text weight
        txt_results = await search_service.hybrid_search(
            query="database timeout",
            organization_id="org_1",
            semantic_weight=0.1,
            text_weight=0.9,
        )

        # Results may differ based on weights
        assert len(sem_results) >= 1
        assert len(txt_results) >= 1


# =============================================================================
# Test: Indexing Job
# =============================================================================


class TestIndexingJob:
    """Tests for background indexing job."""

    @pytest.mark.asyncio
    async def test_indexing_job_processes_unindexed_items(
        self, indexing_job, knowledge_repo
    ):
        """Test that indexing job processes items without embeddings."""
        # Create items without embeddings
        items = [create_knowledge_item(item_id=f"unindexed_{i}") for i in range(5)]
        for item in items:
            await knowledge_repo.create(item)

        # Run indexing job
        result = await indexing_job.run(organization_id="org_1")

        assert result["processed"] == 5
        assert result["succeeded"] == 5
        assert result["failed"] == 0

        # Verify items now have embeddings
        for item in items:
            updated = await knowledge_repo.get_by_id(item.item_id)
            assert updated.has_embedding()

    @pytest.mark.asyncio
    async def test_indexing_job_skips_indexed_items(
        self, indexing_job, knowledge_repo, search_service
    ):
        """Test that indexing job skips already indexed items."""
        # Create and index an item
        item = create_knowledge_item(item_id="already_indexed")
        await knowledge_repo.create(item)
        await search_service.index_item(item)

        # Create unindexed item
        unindexed = create_knowledge_item(item_id="needs_indexing")
        await knowledge_repo.create(unindexed)

        # Run indexing job
        result = await indexing_job.run(organization_id="org_1")

        # Should only process the unindexed item
        assert result["processed"] == 1

    @pytest.mark.asyncio
    async def test_indexing_job_respects_max_items(self, indexing_job, knowledge_repo):
        """Test that indexing job respects max_items limit."""
        # Create many items
        for i in range(20):
            await knowledge_repo.create(create_knowledge_item(item_id=f"many_{i}"))

        # Run with limit
        result = await indexing_job.run(
            organization_id="org_1",
            max_items=5,
        )

        assert result["processed"] == 5

    @pytest.mark.asyncio
    async def test_indexing_job_returns_statistics(self, indexing_job, knowledge_repo):
        """Test that indexing job returns proper statistics."""
        for i in range(3):
            await knowledge_repo.create(create_knowledge_item(item_id=f"stats_{i}"))

        result = await indexing_job.run(organization_id="org_1")

        assert "job_id" in result
        assert "processed" in result
        assert "succeeded" in result
        assert "failed" in result
        assert "duration_seconds" in result
        assert "items_per_second" in result
        assert "status" in result


# =============================================================================
# Test: Organization Isolation
# =============================================================================


class TestOrganizationIsolation:
    """Tests for organization-level data isolation."""

    @pytest.mark.asyncio
    async def test_search_filters_by_organization(self, search_service, knowledge_repo):
        """Test that search only returns items from the specified organization."""
        items = [
            create_knowledge_item(
                item_id="org1_item",
                organization_id="org_1",
                content="Organization 1 content",
            ),
            create_knowledge_item(
                item_id="org2_item",
                organization_id="org_2",
                content="Organization 2 content",
            ),
        ]

        for item in items:
            await knowledge_repo.create(item)
            await search_service.index_item(item)

        # Search for org_1
        results = await search_service.semantic_search(
            query="content",
            organization_id="org_1",
        )

        assert len(results) == 1
        assert results[0][0].organization_id == "org_1"

    @pytest.mark.asyncio
    async def test_indexing_job_filters_by_organization(
        self, indexing_job, knowledge_repo
    ):
        """Test that indexing job only processes items from specified org."""
        # Create items for different orgs
        for i in range(3):
            await knowledge_repo.create(
                create_knowledge_item(
                    item_id=f"org1_{i}",
                    organization_id="org_1",
                )
            )
        for i in range(2):
            await knowledge_repo.create(
                create_knowledge_item(
                    item_id=f"org2_{i}",
                    organization_id="org_2",
                )
            )

        # Run for org_1 only
        result = await indexing_job.run(organization_id="org_1")

        assert result["processed"] == 3

    @pytest.mark.asyncio
    async def test_hybrid_search_respects_organization(
        self, search_service, knowledge_repo
    ):
        """Test hybrid search respects organization boundaries."""
        await knowledge_repo.create(
            create_knowledge_item(
                item_id="shared_content_org1",
                organization_id="org_1",
                content="shared content keyword",
            )
        )
        await knowledge_repo.create(
            create_knowledge_item(
                item_id="shared_content_org2",
                organization_id="org_2",
                content="shared content keyword",
            )
        )

        for item_id in ["shared_content_org1", "shared_content_org2"]:
            item = await knowledge_repo.get_by_id(item_id)
            await search_service.index_item(item)

        results = await search_service.hybrid_search(
            query="shared content",
            organization_id="org_1",
        )

        assert all(r[0].organization_id == "org_1" for r in results)


# =============================================================================
# Test: Multi-Organization Scenarios
# =============================================================================


class TestMultiOrgScenarios:
    """Tests for multi-organization scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_orgs_independent_search(
        self, search_service, knowledge_repo
    ):
        """Test that multiple orgs can search independently."""
        orgs = ["org_a", "org_b", "org_c"]

        for org in orgs:
            for i in range(3):
                item = create_knowledge_item(
                    item_id=f"{org}_item_{i}",
                    organization_id=org,
                    content=f"Content for {org} item {i}",
                )
                await knowledge_repo.create(item)
                await search_service.index_item(item)

        # Each org should find only their items
        for org in orgs:
            results = await search_service.semantic_search(
                query="content",
                organization_id=org,
            )

            assert len(results) == 3
            assert all(r[0].organization_id == org for r in results)

    @pytest.mark.asyncio
    async def test_delete_org_items_doesnt_affect_others(
        self, search_service, knowledge_repo, vector_store
    ):
        """Test that deleting org items doesn't affect other orgs."""
        # Create items for two orgs
        for org in ["org_keep", "org_delete"]:
            for i in range(2):
                item = create_knowledge_item(
                    item_id=f"{org}_{i}",
                    organization_id=org,
                )
                await knowledge_repo.create(item)
                await search_service.index_item(item)

        # Delete org_delete items
        await vector_store.delete_by_organization("org_delete")

        # org_keep should still work
        results = await search_service.semantic_search(
            query="test",
            organization_id="org_keep",
        )

        assert len(results) == 2


# =============================================================================
# Test: Concurrent Operations
# =============================================================================


class TestConcurrentOperations:
    """Tests for concurrent operations."""

    @pytest.mark.asyncio
    async def test_concurrent_indexing(self, search_service, knowledge_repo):
        """Test concurrent item indexing."""
        import asyncio

        items = [create_knowledge_item(item_id=f"concurrent_{i}") for i in range(10)]
        for item in items:
            await knowledge_repo.create(item)

        # Index concurrently
        await asyncio.gather(*[search_service.index_item(item) for item in items])

        # Verify all indexed
        for item in items:
            updated = await knowledge_repo.get_by_id(item.item_id)
            assert updated.has_embedding()

    @pytest.mark.asyncio
    async def test_concurrent_search_and_index(self, search_service, knowledge_repo):
        """Test concurrent search and indexing operations."""
        import asyncio

        # Create initial items
        for i in range(5):
            item = create_knowledge_item(item_id=f"initial_{i}")
            await knowledge_repo.create(item)
            await search_service.index_item(item)

        # Create items to index
        new_items = [create_knowledge_item(item_id=f"new_{i}") for i in range(5)]
        for item in new_items:
            await knowledge_repo.create(item)

        async def search_task():
            return await search_service.semantic_search(
                query="test",
                organization_id="org_1",
            )

        async def index_task(item):
            await search_service.index_item(item)

        # Run concurrently
        results = await asyncio.gather(
            search_task(),
            *[index_task(item) for item in new_items],
            return_exceptions=True,
        )

        # Should complete without errors
        assert not any(isinstance(r, Exception) for r in results)


# =============================================================================
# Test: Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_search_with_empty_query(self, search_service, knowledge_repo):
        """Test search handles empty query gracefully."""
        item = create_knowledge_item(item_id="empty_query_test")
        await knowledge_repo.create(item)
        await search_service.index_item(item)

        # Empty query should still work
        results = await search_service.semantic_search(
            query="   ",  # Whitespace-only query
            organization_id="org_1",
        )

        # May return results based on random embedding
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_with_special_characters(self, search_service, knowledge_repo):
        """Test search handles special characters."""
        item = create_knowledge_item(
            item_id="special_chars",
            content="Error: [ERROR-123] Failed with $$$",
        )
        await knowledge_repo.create(item)
        await search_service.index_item(item)

        results = await search_service.semantic_search(
            query="[ERROR-123]",
            organization_id="org_1",
        )

        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_reindex_updates_vector_store(
        self, search_service, knowledge_repo, vector_store
    ):
        """Test that reindexing updates the vector store."""
        item = create_knowledge_item(
            item_id="reindex_test",
            content="Original content",
        )
        await knowledge_repo.create(item)
        await search_service.index_item(item)

        # Update content
        item.content = "Updated content completely different"
        await knowledge_repo.update(item)

        # Reindex
        await search_service.reindex_item("reindex_test")

        # Verify in vector store
        stored = await vector_store.get_item("reindex_test")
        assert stored is not None

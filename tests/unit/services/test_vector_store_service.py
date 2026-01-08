"""Unit tests for VectorStoreService.

Tests cover:
- Add item operations
- Batch add operations
- Similarity search
- Delete operations
- Update operations
- Collection statistics
- Organization isolation
- Metadata filtering
"""

import pytest
import tempfile
import shutil
from typing import List, Dict, Any
from unittest.mock import MagicMock, patch

from faultmaven.modules.knowledge.domain.services.vector_store_service import VectorStoreService
from faultmaven.exceptions import (
    VectorStoreConnectionError,
    VectorStoreOperationError,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import EMBEDDING_DIMENSIONS


# Test fixtures
@pytest.fixture
def temp_chroma_dir():
    """Create temporary directory for ChromaDB persistence."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def vector_store(temp_chroma_dir):
    """Create vector store service with temporary storage."""
    return VectorStoreService(
        collection_name="test_collection",
        persist_directory=temp_chroma_dir,
    )


def create_embedding(value: float = 0.1) -> List[float]:
    """Create test embedding vector."""
    return [value] * EMBEDDING_DIMENSIONS


def create_item_data(
    item_id: str = "item_1",
    organization_id: str = "org_1",
    item_type: str = "faq",
    category: str = "general",
    content: str = "Test content",
) -> Dict[str, Any]:
    """Create test item data for vector store."""
    return {
        "item_id": item_id,
        "embedding": create_embedding(),
        "metadata": {
            "organization_id": organization_id,
            "item_type": item_type,
            "category": category,
            "is_published": True,
            "language": "en",
        },
        "document": content,
    }


# =============================================================================
# Test: Initialization
# =============================================================================

class TestVectorStoreInitialization:
    """Tests for vector store initialization."""

    def test_initialization_creates_collection(self, temp_chroma_dir):
        """Test that initialization creates a collection."""
        store = VectorStoreService(
            collection_name="init_test",
            persist_directory=temp_chroma_dir,
        )

        assert store.collection is not None
        assert store.collection_name == "init_test"

    def test_initialization_with_invalid_path(self):
        """Test initialization with ChromaDB failure raises error."""
        with patch("faultmaven.modules.knowledge.domain.services.vector_store_service.chromadb.PersistentClient") as mock_client:
            mock_client.side_effect = Exception("Failed to initialize ChromaDB")
            with pytest.raises(VectorStoreConnectionError):
                VectorStoreService(
                    collection_name="test",
                    persist_directory="/invalid/path",
                )

    def test_multiple_initializations_same_collection(self, temp_chroma_dir):
        """Test multiple initializations access same collection."""
        store1 = VectorStoreService(
            collection_name="shared",
            persist_directory=temp_chroma_dir,
        )
        store2 = VectorStoreService(
            collection_name="shared",
            persist_directory=temp_chroma_dir,
        )

        assert store1.collection_name == store2.collection_name


# =============================================================================
# Test: add_item()
# =============================================================================

class TestAddItem:
    """Tests for adding items to vector store."""

    @pytest.mark.asyncio
    async def test_add_item_success(self, vector_store):
        """Test successful item addition."""
        item_data = create_item_data()

        await vector_store.add_item(
            item_id=item_data["item_id"],
            embedding=item_data["embedding"],
            metadata=item_data["metadata"],
            document=item_data["document"],
        )

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 1

    @pytest.mark.asyncio
    async def test_add_item_with_all_metadata_types(self, vector_store):
        """Test adding item with various metadata types."""
        await vector_store.add_item(
            item_id="meta_test",
            embedding=create_embedding(),
            metadata={
                "organization_id": "org_1",
                "string_field": "value",
                "int_field": 42,
                "float_field": 3.14,
                "bool_field": True,
            },
            document="Test document",
        )

        item = await vector_store.get_item("meta_test")
        assert item is not None
        assert item["metadata"]["string_field"] == "value"
        assert item["metadata"]["int_field"] == 42

    @pytest.mark.asyncio
    async def test_add_item_upserts_existing(self, vector_store):
        """Test that adding existing item updates it (upsert)."""
        item_id = "upsert_test"

        # Add initial item
        await vector_store.add_item(
            item_id=item_id,
            embedding=create_embedding(0.1),
            metadata={"organization_id": "org_1", "version": "1"},
            document="Version 1",
        )

        # Add same item with different data
        await vector_store.add_item(
            item_id=item_id,
            embedding=create_embedding(0.2),
            metadata={"organization_id": "org_1", "version": "2"},
            document="Version 2",
        )

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 1  # Still only 1 item

        item = await vector_store.get_item(item_id)
        assert item["metadata"]["version"] == "2"
        assert item["document"] == "Version 2"

    @pytest.mark.asyncio
    async def test_add_item_with_special_characters_in_id(self, vector_store):
        """Test adding item with special characters in ID."""
        item_id = "item-with_special.chars:123"

        await vector_store.add_item(
            item_id=item_id,
            embedding=create_embedding(),
            metadata={"organization_id": "org_1"},
            document="Test",
        )

        item = await vector_store.get_item(item_id)
        assert item is not None
        assert item["item_id"] == item_id

    @pytest.mark.asyncio
    async def test_add_item_sanitizes_metadata(self, vector_store):
        """Test that complex metadata types are sanitized."""
        await vector_store.add_item(
            item_id="sanitize_test",
            embedding=create_embedding(),
            metadata={
                "organization_id": "org_1",
                "list_field": [1, 2, 3],  # Should be skipped
                "dict_field": {"nested": "value"},  # Should be skipped
                "none_field": None,  # Should be skipped
            },
            document="Test",
        )

        item = await vector_store.get_item("sanitize_test")
        assert "list_field" not in item["metadata"]
        assert "dict_field" not in item["metadata"]
        assert "none_field" not in item["metadata"]


# =============================================================================
# Test: add_items_batch()
# =============================================================================

class TestAddItemsBatch:
    """Tests for batch item addition."""

    @pytest.mark.asyncio
    async def test_add_items_batch_success(self, vector_store):
        """Test successful batch addition."""
        items = [
            create_item_data(item_id=f"batch_{i}", organization_id="org_1")
            for i in range(5)
        ]

        await vector_store.add_items_batch(items)

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 5

    @pytest.mark.asyncio
    async def test_add_items_batch_empty_list(self, vector_store):
        """Test batch with empty list does nothing."""
        await vector_store.add_items_batch([])

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 0

    @pytest.mark.asyncio
    async def test_add_items_batch_large_batch(self, vector_store):
        """Test batch with many items."""
        items = [
            create_item_data(item_id=f"large_{i}", organization_id="org_1")
            for i in range(250)
        ]

        await vector_store.add_items_batch(items, batch_size=100)

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 250

    @pytest.mark.asyncio
    async def test_add_items_batch_from_multiple_orgs(self, vector_store):
        """Test batch with items from different organizations."""
        items = [
            create_item_data(item_id="org1_1", organization_id="org_1"),
            create_item_data(item_id="org2_1", organization_id="org_2"),
            create_item_data(item_id="org1_2", organization_id="org_1"),
        ]

        await vector_store.add_items_batch(items)

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 3


# =============================================================================
# Test: search_similar()
# =============================================================================

class TestSearchSimilar:
    """Tests for similarity search."""

    @pytest.mark.asyncio
    async def test_search_similar_returns_results(self, vector_store):
        """Test that search returns relevant results."""
        # Add test items
        for i in range(5):
            await vector_store.add_item(
                item_id=f"search_{i}",
                embedding=create_embedding(0.1 * i),
                metadata={"organization_id": "org_1", "item_type": "faq"},
                document=f"Document {i}",
            )

        results = await vector_store.search_similar(
            query_embedding=create_embedding(0.0),
            organization_id="org_1",
            n_results=3,
        )

        assert len(results) == 3
        for result in results:
            assert "item_id" in result
            assert "distance" in result
            assert "metadata" in result
            assert "document" in result

    @pytest.mark.asyncio
    async def test_search_similar_filters_by_organization(self, vector_store):
        """Test that search filters by organization."""
        await vector_store.add_item(
            item_id="org1_item",
            embedding=create_embedding(0.1),
            metadata={"organization_id": "org_1"},
            document="Org 1 document",
        )
        await vector_store.add_item(
            item_id="org2_item",
            embedding=create_embedding(0.1),
            metadata={"organization_id": "org_2"},
            document="Org 2 document",
        )

        results = await vector_store.search_similar(
            query_embedding=create_embedding(0.1),
            organization_id="org_1",
            n_results=10,
        )

        assert len(results) == 1
        assert results[0]["item_id"] == "org1_item"

    @pytest.mark.asyncio
    async def test_search_similar_with_item_type_filter(self, vector_store):
        """Test search with item_type filter."""
        await vector_store.add_item(
            item_id="faq_item",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1", "item_type": "faq"},
            document="FAQ",
        )
        await vector_store.add_item(
            item_id="guide_item",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1", "item_type": "troubleshooting_guide"},
            document="Guide",
        )

        results = await vector_store.search_similar(
            query_embedding=create_embedding(),
            organization_id="org_1",
            n_results=10,
            filters={"item_type": "faq"},
        )

        assert len(results) == 1
        assert results[0]["metadata"]["item_type"] == "faq"

    @pytest.mark.asyncio
    async def test_search_similar_with_category_filter(self, vector_store):
        """Test search with category filter."""
        await vector_store.add_item(
            item_id="network_item",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1", "category": "networking"},
            document="Network doc",
        )
        await vector_store.add_item(
            item_id="database_item",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1", "category": "database"},
            document="Database doc",
        )

        results = await vector_store.search_similar(
            query_embedding=create_embedding(),
            organization_id="org_1",
            n_results=10,
            filters={"category": "networking"},
        )

        assert len(results) == 1
        assert results[0]["metadata"]["category"] == "networking"

    @pytest.mark.asyncio
    async def test_search_similar_empty_collection(self, vector_store):
        """Test search on empty collection returns empty list."""
        results = await vector_store.search_similar(
            query_embedding=create_embedding(),
            organization_id="org_1",
            n_results=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_search_similar_n_results_limit(self, vector_store):
        """Test that n_results limits the number of results."""
        for i in range(10):
            await vector_store.add_item(
                item_id=f"limit_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Doc {i}",
            )

        results = await vector_store.search_similar(
            query_embedding=create_embedding(),
            organization_id="org_1",
            n_results=3,
        )

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_similar_returns_sorted_by_distance(self, vector_store):
        """Test that results are sorted by distance."""
        # Add items with increasing distance from query
        await vector_store.add_item(
            item_id="far",
            embedding=create_embedding(1.0),
            metadata={"organization_id": "org_1"},
            document="Far",
        )
        await vector_store.add_item(
            item_id="close",
            embedding=create_embedding(0.0),
            metadata={"organization_id": "org_1"},
            document="Close",
        )
        await vector_store.add_item(
            item_id="medium",
            embedding=create_embedding(0.5),
            metadata={"organization_id": "org_1"},
            document="Medium",
        )

        results = await vector_store.search_similar(
            query_embedding=create_embedding(0.0),
            organization_id="org_1",
            n_results=3,
        )

        # Results should be sorted by distance (ascending)
        distances = [r["distance"] for r in results]
        assert distances == sorted(distances)


# =============================================================================
# Test: delete_item()
# =============================================================================

class TestDeleteItem:
    """Tests for deleting items."""

    @pytest.mark.asyncio
    async def test_delete_item_success(self, vector_store):
        """Test successful item deletion."""
        await vector_store.add_item(
            item_id="delete_test",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1"},
            document="To be deleted",
        )

        await vector_store.delete_item("delete_test")

        item = await vector_store.get_item("delete_test")
        assert item is None

    @pytest.mark.asyncio
    async def test_delete_item_not_found(self, vector_store):
        """Test deleting non-existent item doesn't raise error."""
        await vector_store.delete_item("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_delete_item_updates_count(self, vector_store):
        """Test that deletion updates item count."""
        await vector_store.add_item(
            item_id="count_test",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1"},
            document="Test",
        )

        stats_before = await vector_store.get_collection_stats()
        assert stats_before["item_count"] == 1

        await vector_store.delete_item("count_test")

        stats_after = await vector_store.get_collection_stats()
        assert stats_after["item_count"] == 0


# =============================================================================
# Test: delete_items_batch()
# =============================================================================

class TestDeleteItemsBatch:
    """Tests for batch deletion."""

    @pytest.mark.asyncio
    async def test_delete_items_batch_success(self, vector_store):
        """Test successful batch deletion."""
        for i in range(5):
            await vector_store.add_item(
                item_id=f"batch_del_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Doc {i}",
            )

        await vector_store.delete_items_batch(
            [f"batch_del_{i}" for i in range(3)]
        )

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 2

    @pytest.mark.asyncio
    async def test_delete_items_batch_empty_list(self, vector_store):
        """Test batch deletion with empty list."""
        await vector_store.delete_items_batch([])  # Should not raise


# =============================================================================
# Test: update_item()
# =============================================================================

class TestUpdateItem:
    """Tests for updating items."""

    @pytest.mark.asyncio
    async def test_update_item_success(self, vector_store):
        """Test successful item update."""
        await vector_store.add_item(
            item_id="update_test",
            embedding=create_embedding(0.1),
            metadata={"organization_id": "org_1", "version": "1"},
            document="Original",
        )

        await vector_store.update_item(
            item_id="update_test",
            embedding=create_embedding(0.2),
            metadata={"organization_id": "org_1", "version": "2"},
            document="Updated",
        )

        item = await vector_store.get_item("update_test")
        assert item["metadata"]["version"] == "2"
        assert item["document"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_item_creates_if_not_exists(self, vector_store):
        """Test update creates item if it doesn't exist (upsert)."""
        await vector_store.update_item(
            item_id="new_item",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1"},
            document="New document",
        )

        item = await vector_store.get_item("new_item")
        assert item is not None


# =============================================================================
# Test: get_item()
# =============================================================================

class TestGetItem:
    """Tests for getting items by ID."""

    @pytest.mark.asyncio
    async def test_get_item_success(self, vector_store):
        """Test getting existing item."""
        await vector_store.add_item(
            item_id="get_test",
            embedding=create_embedding(),
            metadata={"organization_id": "org_1", "key": "value"},
            document="Test document",
        )

        item = await vector_store.get_item("get_test")

        assert item is not None
        assert item["item_id"] == "get_test"
        assert item["document"] == "Test document"
        assert item["metadata"]["key"] == "value"
        assert item["embedding"] is not None

    @pytest.mark.asyncio
    async def test_get_item_not_found(self, vector_store):
        """Test getting non-existent item returns None."""
        item = await vector_store.get_item("nonexistent")

        assert item is None


# =============================================================================
# Test: get_collection_stats()
# =============================================================================

class TestGetCollectionStats:
    """Tests for collection statistics."""

    @pytest.mark.asyncio
    async def test_get_collection_stats_empty(self, vector_store):
        """Test stats for empty collection."""
        stats = await vector_store.get_collection_stats()

        assert stats["collection_name"] == "test_collection"
        assert stats["item_count"] == 0

    @pytest.mark.asyncio
    async def test_get_collection_stats_with_items(self, vector_store):
        """Test stats after adding items."""
        for i in range(10):
            await vector_store.add_item(
                item_id=f"stats_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Doc {i}",
            )

        stats = await vector_store.get_collection_stats()

        assert stats["item_count"] == 10


# =============================================================================
# Test: clear_collection()
# =============================================================================

class TestClearCollection:
    """Tests for clearing collection."""

    @pytest.mark.asyncio
    async def test_clear_collection_success(self, vector_store):
        """Test clearing all items from collection."""
        for i in range(5):
            await vector_store.add_item(
                item_id=f"clear_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Doc {i}",
            )

        await vector_store.clear_collection()

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 0


# =============================================================================
# Test: delete_by_organization()
# =============================================================================

class TestDeleteByOrganization:
    """Tests for organization-level deletion."""

    @pytest.mark.asyncio
    async def test_delete_by_organization_success(self, vector_store):
        """Test deleting all items for an organization."""
        # Add items for two organizations
        for i in range(3):
            await vector_store.add_item(
                item_id=f"org1_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Org 1 Doc {i}",
            )
        for i in range(2):
            await vector_store.add_item(
                item_id=f"org2_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_2"},
                document=f"Org 2 Doc {i}",
            )

        deleted = await vector_store.delete_by_organization("org_1")

        assert deleted == 3

        stats = await vector_store.get_collection_stats()
        assert stats["item_count"] == 2

    @pytest.mark.asyncio
    async def test_delete_by_organization_empty(self, vector_store):
        """Test deleting from organization with no items."""
        deleted = await vector_store.delete_by_organization("nonexistent_org")

        assert deleted == 0


# =============================================================================
# Test: Health Check
# =============================================================================

class TestHealthCheck:
    """Tests for health check."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, vector_store):
        """Test health check returns healthy status."""
        health = await vector_store.health_check()

        assert health["status"] == "healthy"
        assert health["store_status"] == "healthy"
        assert health["collection_name"] == "test_collection"

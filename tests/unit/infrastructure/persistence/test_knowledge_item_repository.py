"""Unit tests for Knowledge Item Repository.

Tests both InMemoryKnowledgeItemRepository and the repository interface.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    InMemoryKnowledgeItemRepository,
)

# The org-owned sample item is defined once in tests.utils so a change to the
# model's tenancy invariants (#770) lands on every suite at once. Aliased to
# the local name this module has always used.
from tests.utils import generate_item_id, generate_org_id
from tests.utils import make_org_knowledge_item as create_sample_item


def create_valid_embedding(value: float = 0.1) -> list:
    """Create a valid embedding vector of correct dimensions."""
    return [value] * EMBEDDING_DIMENSIONS


# ============================================================
# Create Operation Tests
# ============================================================


class TestInMemoryRepositoryCreate:
    """Tests for create operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_create_item_success(self, repository):
        """Test successful item creation."""
        item = create_sample_item()

        result = await repository.create(item)

        assert result.item_id == item.item_id
        assert result.enterprise_id == item.enterprise_id
        assert result.title == item.title
        assert result.content == item.content
        assert result.item_type == item.item_type

    @pytest.mark.asyncio
    async def test_create_item_with_all_fields(self, repository):
        """Test creation with all fields populated."""
        embedding = create_valid_embedding()
        item = create_sample_item(
            category="networking",
            tags=["network", "connectivity"],
            embedding_vector=embedding,
            helpful_count=10,
            not_helpful_count=2,
            metadata={"environment": "production"},
        )

        result = await repository.create(item)

        assert result.category == "networking"
        assert result.tags == ["network", "connectivity"]
        assert result.embedding_vector == embedding
        assert result.helpful_count == 10
        assert result.not_helpful_count == 2
        assert result.metadata == {"environment": "production"}

    @pytest.mark.asyncio
    async def test_create_duplicate_id_fails(self, repository):
        """Test creating item with duplicate ID fails."""
        item_id = generate_item_id()
        item1 = create_sample_item(item_id=item_id)
        item2 = create_sample_item(item_id=item_id)

        await repository.create(item1)

        with pytest.raises(ValueError, match="already exists"):
            await repository.create(item2)

    @pytest.mark.asyncio
    async def test_create_returns_copy(self, repository):
        """Test that create returns a copy, not the original."""
        item = create_sample_item()
        result = await repository.create(item)

        # Modify original
        item.title = "Modified Title"

        # Result should not be affected
        retrieved = await repository.get_by_id(result.item_id)
        assert retrieved.title != "Modified Title"


# ============================================================
# Get By ID Tests
# ============================================================


class TestInMemoryRepositoryGetById:
    """Tests for get_by_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_get_by_id_found(self, repository):
        """Test retrieving existing item."""
        item = create_sample_item()
        await repository.create(item)

        result = await repository.get_by_id(item.item_id)

        assert result is not None
        assert result.item_id == item.item_id
        assert result.title == item.title

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, repository):
        """Test retrieving non-existent item returns None."""
        result = await repository.get_by_id("ki_nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_returns_copy(self, repository):
        """Test that get_by_id returns a copy."""
        item = create_sample_item()
        await repository.create(item)

        result1 = await repository.get_by_id(item.item_id)
        result1.title = "Modified"

        result2 = await repository.get_by_id(item.item_id)
        assert result2.title != "Modified"


# ============================================================
# Update Tests
# ============================================================


class TestInMemoryRepositoryUpdate:
    """Tests for update operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_update_item_success(self, repository):
        """Test successful item update."""
        item = create_sample_item()
        await repository.create(item)

        item.title = "Updated Title"
        item.content = "Updated content"

        result = await repository.update(item)

        assert result.title == "Updated Title"
        assert result.content == "Updated content"

    @pytest.mark.asyncio
    async def test_update_updates_timestamp(self, repository):
        """Test that update modifies updated_at."""
        item = create_sample_item()
        await repository.create(item)

        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.title = "New Title"
        result = await repository.update(item)

        assert result.updated_at > original_updated

    @pytest.mark.asyncio
    async def test_update_not_found_fails(self, repository):
        """Test updating non-existent item fails."""
        item = create_sample_item()

        with pytest.raises(ValueError, match="not found"):
            await repository.update(item)

    @pytest.mark.asyncio
    async def test_update_all_fields(self, repository):
        """Test updating all mutable fields."""
        item = create_sample_item()
        await repository.create(item)

        embedding = create_valid_embedding(0.5)
        item.title = "New Title"
        item.content = "New Content"
        item.category = "database"
        item.tags = ["sql", "query"]
        item.embedding_vector = embedding
        item.is_published = False
        item.view_count = 100
        item.helpful_count = 80
        item.not_helpful_count = 20

        result = await repository.update(item)

        assert result.title == "New Title"
        assert result.content == "New Content"
        assert result.category == "database"
        assert result.tags == ["sql", "query"]
        assert result.embedding_vector == embedding
        assert result.is_published is False
        assert result.view_count == 100
        assert result.helpful_count == 80
        assert result.not_helpful_count == 20


# ============================================================
# Delete Tests
# ============================================================


class TestInMemoryRepositoryDelete:
    """Tests for delete operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_delete_item_success(self, repository):
        """Test successful item deletion."""
        item = create_sample_item()
        await repository.create(item)

        result = await repository.delete(item.item_id)

        assert result is True
        assert await repository.get_by_id(item.item_id) is None

    @pytest.mark.asyncio
    async def test_delete_not_found(self, repository):
        """Test deleting non-existent item returns False."""
        result = await repository.delete("ki_nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_others(self, repository):
        """Test that deleting one item doesn't affect others."""
        item1 = create_sample_item()
        item2 = create_sample_item()
        await repository.create(item1)
        await repository.create(item2)

        await repository.delete(item1.item_id)

        assert await repository.get_by_id(item2.item_id) is not None


# ============================================================
# List By Organization Tests
# ============================================================


class TestInMemoryRepositoryListByOrganization:
    """Tests for list_by_enterprise_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_list_by_enterprise_empty(self, repository):
        """Test listing items for enterprise with no items."""
        result = await repository.list_by_enterprise_id("org_empty")

        assert result == []

    @pytest.mark.asyncio
    async def test_list_by_enterprise_multiple(self, repository):
        """Test listing multiple items for an enterprise."""
        enterprise_id = generate_org_id()

        items = [
            create_sample_item(enterprise_id=enterprise_id),
            create_sample_item(enterprise_id=enterprise_id),
            create_sample_item(enterprise_id=enterprise_id),
        ]
        for item in items:
            await repository.create(item)

        result = await repository.list_by_enterprise_id(enterprise_id)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_by_enterprise_only_matching(self, repository):
        """Test that only items for the specified enterprise are returned."""
        org_id1 = generate_org_id()
        org_id2 = generate_org_id()

        await repository.create(create_sample_item(enterprise_id=org_id1))
        await repository.create(create_sample_item(enterprise_id=org_id1))
        await repository.create(create_sample_item(enterprise_id=org_id2))

        result = await repository.list_by_enterprise_id(org_id1)

        assert len(result) == 2
        assert all(i.enterprise_id == org_id1 for i in result)

    @pytest.mark.asyncio
    async def test_list_by_enterprise_ordered_by_created_at_desc(self, repository):
        """Test items are ordered by created_at descending."""
        enterprise_id = generate_org_id()
        base_time = datetime.now(timezone.utc)

        i1 = create_sample_item(
            enterprise_id=enterprise_id, created_at=base_time - timedelta(hours=2)
        )
        i2 = create_sample_item(
            enterprise_id=enterprise_id, created_at=base_time - timedelta(hours=1)
        )
        i3 = create_sample_item(enterprise_id=enterprise_id, created_at=base_time)

        await repository.create(i1)
        await repository.create(i2)
        await repository.create(i3)

        result = await repository.list_by_enterprise_id(enterprise_id)

        assert result[0].item_id == i3.item_id
        assert result[1].item_id == i2.item_id
        assert result[2].item_id == i1.item_id

    @pytest.mark.asyncio
    async def test_list_by_enterprise_with_item_type_filter(self, repository):
        """Test filtering by item_type."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                item_type=KnowledgeItemType.FAQ,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                item_type=KnowledgeItemType.RUNBOOK,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                item_type=KnowledgeItemType.FAQ,
            )
        )

        result = await repository.list_by_enterprise_id(
            enterprise_id,
            item_type=KnowledgeItemType.FAQ,
        )

        assert len(result) == 2
        assert all(i.item_type == KnowledgeItemType.FAQ for i in result)

    @pytest.mark.asyncio
    async def test_list_by_enterprise_with_category_filter(self, repository):
        """Test filtering by category."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, category="networking")
        )
        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, category="database")
        )
        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, category="networking")
        )

        result = await repository.list_by_enterprise_id(
            enterprise_id, category="networking"
        )

        assert len(result) == 2
        assert all(i.category == "networking" for i in result)

    @pytest.mark.asyncio
    async def test_list_by_enterprise_respects_is_published(self, repository):
        """Test that is_published filter is respected."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, is_published=True)
        )
        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, is_published=False)
        )
        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, is_published=True)
        )

        # Default is is_published=True
        result = await repository.list_by_enterprise_id(enterprise_id)
        assert len(result) == 2

        # Explicitly query unpublished
        result_unpublished = await repository.list_by_enterprise_id(
            enterprise_id, is_published=False
        )
        assert len(result_unpublished) == 1

    @pytest.mark.asyncio
    async def test_list_by_enterprise_pagination(self, repository):
        """Test pagination with limit and offset."""
        enterprise_id = generate_org_id()

        for i in range(10):
            await repository.create(create_sample_item(enterprise_id=enterprise_id))

        page1 = await repository.list_by_enterprise_id(enterprise_id, limit=3, offset=0)
        page2 = await repository.list_by_enterprise_id(enterprise_id, limit=3, offset=3)
        page3 = await repository.list_by_enterprise_id(enterprise_id, limit=3, offset=6)
        page4 = await repository.list_by_enterprise_id(enterprise_id, limit=3, offset=9)

        assert len(page1) == 3
        assert len(page2) == 3
        assert len(page3) == 3
        assert len(page4) == 1


# ============================================================
# Tag Search Tests
# ============================================================


class TestInMemoryRepositoryTagSearch:
    """Tests for search_by_tags operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_search_by_tags_match_any(self, repository):
        """Test tag search with match_any (default)."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python", "debugging"],
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["java", "networking"],
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python", "api"],
            )
        )

        result = await repository.search_by_tags(
            enterprise_id, ["python", "networking"]
        )

        assert len(result) == 3  # All items match at least one tag

    @pytest.mark.asyncio
    async def test_search_by_tags_match_all(self, repository):
        """Test tag search with match_all=True."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python", "debugging"],
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python", "debugging", "advanced"],
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python"],
            )
        )

        result = await repository.search_by_tags(
            enterprise_id,
            ["python", "debugging"],
            match_all=True,
        )

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_search_by_tags_empty_list(self, repository):
        """Test search with empty tags list returns empty."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, tags=["test"])
        )

        result = await repository.search_by_tags(enterprise_id, [])

        assert result == []

    @pytest.mark.asyncio
    async def test_search_by_tags_no_matches(self, repository):
        """Test search returns empty when no tags match."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, tags=["python"])
        )

        result = await repository.search_by_tags(enterprise_id, ["java", "rust"])

        assert result == []

    @pytest.mark.asyncio
    async def test_search_by_tags_only_published(self, repository):
        """Test tag search only returns published items."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python"],
                is_published=True,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                tags=["python"],
                is_published=False,
            )
        )

        result = await repository.search_by_tags(enterprise_id, ["python"])

        assert len(result) == 1
        assert result[0].is_published is True

    @pytest.mark.asyncio
    async def test_search_by_tags_respects_limit(self, repository):
        """Test tag search respects limit parameter."""
        enterprise_id = generate_org_id()

        for i in range(10):
            await repository.create(
                create_sample_item(
                    enterprise_id=enterprise_id,
                    tags=["common"],
                )
            )

        result = await repository.search_by_tags(enterprise_id, ["common"], limit=5)

        assert len(result) == 5


# ============================================================
# Items Without Embeddings Tests
# ============================================================


class TestInMemoryRepositoryItemsWithoutEmbeddings:
    """Tests for get_items_without_embeddings operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_get_items_without_embeddings(self, repository):
        """Test getting items without embeddings."""
        enterprise_id = generate_org_id()

        embedding = create_valid_embedding()
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                embedding_vector=None,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                embedding_vector=embedding,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                embedding_vector=None,
            )
        )

        result = await repository.get_items_without_embeddings(enterprise_id)

        assert len(result) == 2
        assert all(not i.has_embedding() for i in result)

    @pytest.mark.asyncio
    async def test_get_items_without_embeddings_only_published(self, repository):
        """Test only published items without embeddings are returned."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                embedding_vector=None,
                is_published=True,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                embedding_vector=None,
                is_published=False,
            )
        )

        result = await repository.get_items_without_embeddings(enterprise_id)

        assert len(result) == 1
        assert result[0].is_published is True

    @pytest.mark.asyncio
    async def test_get_items_without_embeddings_ordered_oldest_first(self, repository):
        """Test items are ordered by created_at ascending (oldest first)."""
        enterprise_id = generate_org_id()
        base_time = datetime.now(timezone.utc)

        i1 = create_sample_item(enterprise_id=enterprise_id, created_at=base_time)
        i2 = create_sample_item(
            enterprise_id=enterprise_id, created_at=base_time - timedelta(hours=2)
        )
        i3 = create_sample_item(
            enterprise_id=enterprise_id, created_at=base_time - timedelta(hours=1)
        )

        await repository.create(i1)
        await repository.create(i2)
        await repository.create(i3)

        result = await repository.get_items_without_embeddings(enterprise_id)

        assert result[0].item_id == i2.item_id  # Oldest
        assert result[1].item_id == i3.item_id
        assert result[2].item_id == i1.item_id  # Newest

    @pytest.mark.asyncio
    async def test_get_items_without_embeddings_respects_limit(self, repository):
        """Test get_items_without_embeddings respects limit."""
        enterprise_id = generate_org_id()

        for i in range(10):
            await repository.create(create_sample_item(enterprise_id=enterprise_id))

        result = await repository.get_items_without_embeddings(enterprise_id, limit=5)

        assert len(result) == 5


# ============================================================
# Count Tests
# ============================================================


class TestInMemoryRepositoryCount:
    """Tests for count_by_enterprise_id operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_count_by_enterprise_zero(self, repository):
        """Test counting items for enterprise with none."""
        result = await repository.count_by_enterprise_id("org_empty")

        assert result == 0

    @pytest.mark.asyncio
    async def test_count_by_enterprise_multiple(self, repository):
        """Test counting multiple items."""
        enterprise_id = generate_org_id()

        for _ in range(5):
            await repository.create(create_sample_item(enterprise_id=enterprise_id))

        result = await repository.count_by_enterprise_id(enterprise_id)

        assert result == 5

    @pytest.mark.asyncio
    async def test_count_by_enterprise_only_matching(self, repository):
        """Test count only includes matching enterprise."""
        org_id1 = generate_org_id()
        org_id2 = generate_org_id()

        await repository.create(create_sample_item(enterprise_id=org_id1))
        await repository.create(create_sample_item(enterprise_id=org_id1))
        await repository.create(create_sample_item(enterprise_id=org_id2))

        result = await repository.count_by_enterprise_id(org_id1)

        assert result == 2

    @pytest.mark.asyncio
    async def test_count_by_enterprise_with_type_filter(self, repository):
        """Test counting with item_type filter."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                item_type=KnowledgeItemType.FAQ,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                item_type=KnowledgeItemType.FAQ,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                item_type=KnowledgeItemType.RUNBOOK,
            )
        )

        result = await repository.count_by_enterprise_id(
            enterprise_id,
            item_type=KnowledgeItemType.FAQ,
        )

        assert result == 2

    @pytest.mark.asyncio
    async def test_count_includes_unpublished(self, repository):
        """Test count includes unpublished items."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, is_published=True)
        )
        await repository.create(
            create_sample_item(enterprise_id=enterprise_id, is_published=False)
        )

        result = await repository.count_by_enterprise_id(enterprise_id)

        assert result == 2


# ============================================================
# Most Helpful Tests
# ============================================================


class TestInMemoryRepositoryMostHelpful:
    """Tests for get_most_helpful operation."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_get_most_helpful_ordered_by_score(self, repository):
        """Test items are ordered by helpfulness score."""
        enterprise_id = generate_org_id()

        # Create items with different helpfulness scores
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=10,
                not_helpful_count=0,  # Score: 1.0
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=5,
                not_helpful_count=5,  # Score: 0.5
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=8,
                not_helpful_count=2,  # Score: 0.8
            )
        )

        result = await repository.get_most_helpful(enterprise_id)

        assert len(result) == 3
        assert result[0].get_helpfulness_score() == 1.0
        assert result[1].get_helpfulness_score() == 0.8
        assert result[2].get_helpfulness_score() == 0.5

    @pytest.mark.asyncio
    async def test_get_most_helpful_respects_minimum_threshold(self, repository):
        """Test items below feedback threshold are excluded."""
        enterprise_id = generate_org_id()

        # Below threshold (default is 3)
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=2,
                not_helpful_count=0,
            )
        )
        # At threshold
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=3,
                not_helpful_count=0,
            )
        )
        # Above threshold
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=10,
                not_helpful_count=0,
            )
        )

        result = await repository.get_most_helpful(enterprise_id)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_most_helpful_only_published(self, repository):
        """Test only published items are returned."""
        enterprise_id = generate_org_id()

        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=10,
                is_published=True,
            )
        )
        await repository.create(
            create_sample_item(
                enterprise_id=enterprise_id,
                helpful_count=10,
                is_published=False,
            )
        )

        result = await repository.get_most_helpful(enterprise_id)

        assert len(result) == 1
        assert result[0].is_published is True

    @pytest.mark.asyncio
    async def test_get_most_helpful_respects_limit(self, repository):
        """Test get_most_helpful respects limit."""
        enterprise_id = generate_org_id()

        for i in range(10):
            await repository.create(
                create_sample_item(
                    enterprise_id=enterprise_id,
                    helpful_count=10,
                )
            )

        result = await repository.get_most_helpful(enterprise_id, limit=5)

        assert len(result) == 5


# ============================================================
# Clear and Helper Tests
# ============================================================


class TestInMemoryRepositoryClear:
    """Tests for clear helper method."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_clear_removes_all(self, repository):
        """Test that clear removes all items."""
        enterprise_id = generate_org_id()

        for _ in range(5):
            await repository.create(create_sample_item(enterprise_id=enterprise_id))

        repository.clear()

        result = await repository.count_by_enterprise_id(enterprise_id)
        assert result == 0


class TestInMemoryRepositoryDeleteItemsForOrganization:
    """Tests for delete_items_for_enterprise helper method."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_delete_items_for_enterprise(self, repository):
        """Test deleting all items for an enterprise."""
        enterprise_id = generate_org_id()

        for _ in range(3):
            await repository.create(create_sample_item(enterprise_id=enterprise_id))

        count = repository.delete_items_for_enterprise(enterprise_id)

        assert count == 3
        assert await repository.count_by_enterprise_id(enterprise_id) == 0

    @pytest.mark.asyncio
    async def test_delete_items_for_enterprise_preserves_others(self, repository):
        """Test that deleting for one enterprise preserves other enterprises."""
        org_id1 = generate_org_id()
        org_id2 = generate_org_id()

        await repository.create(create_sample_item(enterprise_id=org_id1))
        await repository.create(create_sample_item(enterprise_id=org_id2))

        repository.delete_items_for_enterprise(org_id1)

        assert await repository.count_by_enterprise_id(org_id1) == 0
        assert await repository.count_by_enterprise_id(org_id2) == 1


# ============================================================
# Metadata Tests
# ============================================================


class TestInMemoryRepositoryMetadata:
    """Tests for metadata handling."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_create_with_metadata(self, repository):
        """Test creating item with metadata."""
        item = create_sample_item(
            metadata={"environment": "production", "priority": "high"}
        )

        result = await repository.create(item)

        assert result.metadata == {"environment": "production", "priority": "high"}

    @pytest.mark.asyncio
    async def test_update_metadata(self, repository):
        """Test updating item metadata."""
        item = create_sample_item(metadata={"key": "value"})
        await repository.create(item)

        item.metadata = {"new_key": "new_value", "another": 123}
        result = await repository.update(item)

        assert result.metadata == {"new_key": "new_value", "another": 123}

    @pytest.mark.asyncio
    async def test_metadata_none(self, repository):
        """Test item with None metadata."""
        item = create_sample_item(metadata=None)
        await repository.create(item)

        result = await repository.get_by_id(item.item_id)

        assert result.metadata is None

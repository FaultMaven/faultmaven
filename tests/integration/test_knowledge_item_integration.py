"""Integration tests for Knowledge Item Repository.

Tests the full knowledge item lifecycle with real database operations.

Run with:
    pytest tests/integration/test_knowledge_item_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - PostgreSQL for production testing (optional, requires DATABASE_URL)
"""

import os
import pytest
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database import reset_engine
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
    InMemoryKnowledgeItemRepository,
)
from faultmaven.infrastructure.persistence.repository_factory import (
    reset_inmemory_knowledge_item_repository,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
from tests.utils import generate_item_id, generate_org_id
    KnowledgeItem,
    KnowledgeItemType,
    EMBEDDING_DIMENSIONS,
)
from tests.utils import generate_item_id, generate_org_id


# ============================================================
# Test Fixtures
# ============================================================


def create_valid_embedding(value: float = 0.1) -> list:
    """Create a valid embedding vector of correct dimensions."""
    return [value] * EMBEDDING_DIMENSIONS


@pytest.fixture(scope="function")
async def test_engine():
    """Create test engine with in-memory SQLite."""
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    reset_engine()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()
    reset_engine()


@pytest.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test session."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
async def repository(test_session) -> DatabaseKnowledgeItemRepository:
    """Create DatabaseKnowledgeItemRepository with test session."""
    return DatabaseKnowledgeItemRepository(test_session)


@pytest.fixture(scope="function")
def inmemory_repository() -> InMemoryKnowledgeItemRepository:
    """Create fresh InMemoryKnowledgeItemRepository."""
    reset_inmemory_knowledge_item_repository()
    return InMemoryKnowledgeItemRepository()


def create_sample_item(
    item_id: str = None,
    organization_id: str = None,
    title: str = "Sample Knowledge Item",
    content: str = "This is sample content for the knowledge item.",
    item_type: KnowledgeItemType = KnowledgeItemType.TROUBLESHOOTING_GUIDE,
    category: str = None,
    tags: list = None,
    embedding_vector: list = None,
    is_published: bool = True,
    view_count: int = 0,
    helpful_count: int = 0,
    not_helpful_count: int = 0,
    created_at: datetime = None,
    metadata: dict = None,
) -> KnowledgeItem:
    """Create a sample knowledge item for testing."""
    return KnowledgeItem(
        item_id=item_id or generate_item_id(),
        organization_id=organization_id or generate_org_id(),
        title=title,
        content=content,
        item_type=item_type,
        category=category,
        tags=tags or [],
        embedding_vector=embedding_vector,
        is_published=is_published,
        view_count=view_count,
        helpful_count=helpful_count,
        not_helpful_count=not_helpful_count,
        created_at=created_at or datetime.now(timezone.utc),
        metadata=metadata,
    )


# ============================================================
# Full Item Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_item_lifecycle(repository: DatabaseKnowledgeItemRepository):
    """Test create -> update -> retrieve -> delete flow."""
    # Step 1: Create item
    item = create_sample_item(
        title="How to fix connection timeout",
        content="Follow these steps to troubleshoot connection timeouts...",
        category="networking",
        tags=["timeout", "connection", "troubleshooting"],
    )
    created = await repository.create(item)

    assert created.item_id == item.item_id
    assert created.title == "How to fix connection timeout"
    assert created.category == "networking"
    assert created.tags == ["timeout", "connection", "troubleshooting"]

    # Step 2: Retrieve item
    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved is not None
    assert retrieved.item_id == item.item_id

    # Step 3: Update item
    item.title = "Updated: How to fix connection timeout"
    item.helpful_count = 10
    updated = await repository.update(item)
    assert updated.title == "Updated: How to fix connection timeout"
    assert updated.helpful_count == 10

    # Step 4: Delete item
    deleted = await repository.delete(item.item_id)
    assert deleted is True

    # Step 5: Verify deleted
    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_item_with_embedding_lifecycle(repository: DatabaseKnowledgeItemRepository):
    """Test item lifecycle with embedding vector."""
    embedding = create_valid_embedding(0.5)
    item = create_sample_item(
        embedding_vector=embedding,
    )
    await repository.create(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.has_embedding()
    assert retrieved.embedding_vector == embedding
    assert retrieved.get_embedding_dimensions() == EMBEDDING_DIMENSIONS

    # Update embedding
    new_embedding = create_valid_embedding(0.8)
    item.embedding_vector = new_embedding
    await repository.update(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.embedding_vector == new_embedding


# ============================================================
# Full-Text Search Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_text_search_relevance(repository: DatabaseKnowledgeItemRepository):
    """Test full-text search returns relevant results."""
    org_id = generate_org_id()

    # Create items with different content
    await repository.create(create_sample_item(
        organization_id=org_id,
        title="How to configure SSL certificates",
        content="This guide explains SSL/TLS certificate configuration.",
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        title="Database backup procedures",
        content="Follow these steps to backup your database.",
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        title="SSL troubleshooting guide",
        content="Common SSL errors and how to fix them.",
    ))

    # Search for SSL
    result = await repository.search_by_text(org_id, "SSL")

    assert len(result) == 2
    assert all("ssl" in r.title.lower() or "ssl" in r.content.lower() for r in result)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_text_search_case_insensitive(repository: DatabaseKnowledgeItemRepository):
    """Test full-text search is case insensitive."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        title="DATABASE Connection Issues",
        content="Troubleshooting DATABASE connections.",
    ))

    # Search with different cases
    result_lower = await repository.search_by_text(org_id, "database")
    result_upper = await repository.search_by_text(org_id, "DATABASE")
    result_mixed = await repository.search_by_text(org_id, "DaTaBasE")

    assert len(result_lower) == 1
    assert len(result_upper) == 1
    assert len(result_mixed) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_text_search_with_item_type_filter(repository: DatabaseKnowledgeItemRepository):
    """Test full-text search with item type filter."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        title="Error handling in API",
        content="How to handle errors in API calls.",
        item_type=KnowledgeItemType.API_DOCUMENTATION,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        title="Error handling best practices",
        content="Best practices for error handling.",
        item_type=KnowledgeItemType.BEST_PRACTICE,
    ))

    result = await repository.search_by_text(
        org_id,
        "error",
        item_type=KnowledgeItemType.API_DOCUMENTATION,
    )

    assert len(result) == 1
    assert result[0].item_type == KnowledgeItemType.API_DOCUMENTATION


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_text_search_only_published(repository: DatabaseKnowledgeItemRepository):
    """Test full-text search only returns published items."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        title="Published error guide",
        is_published=True,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        title="Unpublished error guide",
        is_published=False,
    ))

    result = await repository.search_by_text(org_id, "error")

    assert len(result) == 1
    assert result[0].is_published is True


# ============================================================
# Tag Search Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tag_search_match_all(repository: DatabaseKnowledgeItemRepository):
    """Test tag search with match_all=True."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        tags=["python", "debugging"],
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        tags=["python", "debugging", "advanced"],
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        tags=["python"],
    ))

    result = await repository.search_by_tags(
        org_id,
        ["python", "debugging"],
        match_all=True,
    )

    assert len(result) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tag_search_match_any(repository: DatabaseKnowledgeItemRepository):
    """Test tag search with match_all=False (any tag matches)."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        tags=["python"],
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        tags=["java"],
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        tags=["rust"],
    ))

    result = await repository.search_by_tags(
        org_id,
        ["python", "java"],
        match_all=False,
    )

    assert len(result) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tag_search_preserves_order(repository: DatabaseKnowledgeItemRepository):
    """Test tag search returns items ordered by created_at DESC."""
    org_id = generate_org_id()
    base_time = datetime.now(timezone.utc)

    for i in range(5):
        await repository.create(create_sample_item(
            organization_id=org_id,
            tags=["common"],
            created_at=base_time - timedelta(hours=i),
        ))

    result = await repository.search_by_tags(org_id, ["common"])

    assert len(result) == 5
    for i in range(len(result) - 1):
        assert result[i].created_at >= result[i + 1].created_at


# ============================================================
# Embedding Management Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_items_without_embeddings(repository: DatabaseKnowledgeItemRepository):
    """Test retrieval of items needing embeddings."""
    org_id = generate_org_id()
    embedding = create_valid_embedding()

    # Create items with and without embeddings
    await repository.create(create_sample_item(
        organization_id=org_id,
        embedding_vector=None,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        embedding_vector=embedding,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        embedding_vector=None,
    ))

    result = await repository.get_items_without_embeddings(org_id)

    assert len(result) == 2
    assert all(not i.has_embedding() for i in result)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_items_without_embeddings_ordered_oldest_first(
    repository: DatabaseKnowledgeItemRepository,
):
    """Test items without embeddings are ordered oldest first."""
    org_id = generate_org_id()
    base_time = datetime.now(timezone.utc)

    for i in range(5):
        await repository.create(create_sample_item(
            organization_id=org_id,
            created_at=base_time - timedelta(hours=i),
        ))

    result = await repository.get_items_without_embeddings(org_id)

    assert len(result) == 5
    for i in range(len(result) - 1):
        assert result[i].created_at <= result[i + 1].created_at


@pytest.mark.asyncio
@pytest.mark.integration
async def test_embedding_update_workflow(repository: DatabaseKnowledgeItemRepository):
    """Test typical embedding update workflow."""
    org_id = generate_org_id()

    # Create item without embedding
    item = create_sample_item(organization_id=org_id)
    await repository.create(item)

    # Get items needing embeddings
    items_needing_embedding = await repository.get_items_without_embeddings(org_id)
    assert len(items_needing_embedding) == 1

    # Generate and set embedding
    embedding = create_valid_embedding(0.5)
    item.set_embedding(embedding, model="text-embedding-3-small", version=1)
    await repository.update(item)

    # Verify no more items need embeddings
    items_needing_embedding = await repository.get_items_without_embeddings(org_id)
    assert len(items_needing_embedding) == 0

    # Verify embedding persisted
    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.has_embedding()
    assert retrieved.embedding_vector == embedding


# ============================================================
# Helpfulness Scoring Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_most_helpful_ranking(repository: DatabaseKnowledgeItemRepository):
    """Test most helpful items ranking."""
    org_id = generate_org_id()

    # Create items with various helpful/not_helpful ratios
    await repository.create(create_sample_item(
        organization_id=org_id,
        helpful_count=10,
        not_helpful_count=0,  # Score: 1.0
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        helpful_count=8,
        not_helpful_count=2,  # Score: 0.8
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        helpful_count=5,
        not_helpful_count=5,  # Score: 0.5
    ))
    # Below threshold (default 3)
    await repository.create(create_sample_item(
        organization_id=org_id,
        helpful_count=2,
        not_helpful_count=0,  # Only 2 votes
    ))

    result = await repository.get_most_helpful(org_id)

    assert len(result) == 3  # Excludes item below threshold
    assert result[0].get_helpfulness_score() == 1.0
    assert result[1].get_helpfulness_score() == 0.8
    assert result[2].get_helpfulness_score() == 0.5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_most_helpful_only_published(repository: DatabaseKnowledgeItemRepository):
    """Test most helpful only returns published items."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        helpful_count=10,
        is_published=True,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        helpful_count=10,
        is_published=False,
    ))

    result = await repository.get_most_helpful(org_id)

    assert len(result) == 1
    assert result[0].is_published is True


# ============================================================
# Usage Tracking Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_usage_tracking_persistence(repository: DatabaseKnowledgeItemRepository):
    """Test usage tracking persists correctly."""
    item = create_sample_item()
    await repository.create(item)

    # Mark as retrieved and helpful
    item.mark_retrieved()
    item.mark_helpful()
    item.mark_helpful()
    item.mark_not_helpful()
    await repository.update(item)

    # Reload from database
    retrieved = await repository.get_by_id(item.item_id)

    assert retrieved.view_count == 1
    assert retrieved.helpful_count == 2
    assert retrieved.not_helpful_count == 1
    assert retrieved.last_retrieved_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_usage_updates(repository: DatabaseKnowledgeItemRepository):
    """Test multiple usage updates accumulate correctly."""
    item = create_sample_item()
    await repository.create(item)

    for _ in range(10):
        item.mark_retrieved()

    for _ in range(5):
        item.mark_helpful()

    await repository.update(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.view_count == 10
    assert retrieved.helpful_count == 5


# ============================================================
# List and Filter Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_by_organization_with_filters(repository: DatabaseKnowledgeItemRepository):
    """Test listing items with multiple filters."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        item_type=KnowledgeItemType.FAQ,
        category="networking",
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        item_type=KnowledgeItemType.FAQ,
        category="database",
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        item_type=KnowledgeItemType.RUNBOOK,
        category="networking",
    ))

    # Filter by type
    faqs = await repository.list_by_organization_id(
        org_id,
        item_type=KnowledgeItemType.FAQ,
    )
    assert len(faqs) == 2

    # Filter by category
    networking = await repository.list_by_organization_id(
        org_id,
        category="networking",
    )
    assert len(networking) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_pagination(repository: DatabaseKnowledgeItemRepository):
    """Test pagination works correctly."""
    org_id = generate_org_id()

    for i in range(25):
        await repository.create(create_sample_item(organization_id=org_id))

    page1 = await repository.list_by_organization_id(org_id, limit=10, offset=0)
    page2 = await repository.list_by_organization_id(org_id, limit=10, offset=10)
    page3 = await repository.list_by_organization_id(org_id, limit=10, offset=20)

    assert len(page1) == 10
    assert len(page2) == 10
    assert len(page3) == 5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_ordering_by_created_at(repository: DatabaseKnowledgeItemRepository):
    """Test items are ordered by created_at descending."""
    org_id = generate_org_id()
    base_time = datetime.now(timezone.utc)

    for i in range(10):
        await repository.create(create_sample_item(
            organization_id=org_id,
            created_at=base_time - timedelta(hours=i),
        ))

    items = await repository.list_by_organization_id(org_id)

    for i in range(len(items) - 1):
        assert items[i].created_at >= items[i + 1].created_at


# ============================================================
# Count Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_count_with_type_filter(repository: DatabaseKnowledgeItemRepository):
    """Test count with item type filter."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        item_type=KnowledgeItemType.FAQ,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        item_type=KnowledgeItemType.FAQ,
    ))
    await repository.create(create_sample_item(
        organization_id=org_id,
        item_type=KnowledgeItemType.RUNBOOK,
    ))

    faq_count = await repository.count_by_organization_id(
        org_id,
        item_type=KnowledgeItemType.FAQ,
    )
    total_count = await repository.count_by_organization_id(org_id)

    assert faq_count == 2
    assert total_count == 3


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_duplicate_id_fails(repository: DatabaseKnowledgeItemRepository):
    """Test creating item with duplicate ID fails."""
    item = create_sample_item()
    await repository.create(item)

    duplicate = create_sample_item(item_id=item.item_id)

    with pytest.raises(ValueError, match="already exists"):
        await repository.create(duplicate)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_non_existent_fails(repository: DatabaseKnowledgeItemRepository):
    """Test updating non-existent item fails."""
    item = create_sample_item()
    # Don't create it

    with pytest.raises(ValueError, match="not found"):
        await repository.update(item)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_non_existent_returns_false(repository: DatabaseKnowledgeItemRepository):
    """Test deleting non-existent item returns False."""
    result = await repository.delete("ki_nonexistent_123")
    assert result is False


# ============================================================
# Metadata Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_metadata_persistence(repository: DatabaseKnowledgeItemRepository):
    """Test that metadata is persisted correctly."""
    item = create_sample_item(
        metadata={
            "source": "documentation",
            "version": "1.2.3",
            "tags": ["important", "verified"],
            "nested": {"key": "value"},
        }
    )
    await repository.create(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.metadata == {
        "source": "documentation",
        "version": "1.2.3",
        "tags": ["important", "verified"],
        "nested": {"key": "value"},
    }


@pytest.mark.asyncio
@pytest.mark.integration
async def test_metadata_update(repository: DatabaseKnowledgeItemRepository):
    """Test updating metadata."""
    item = create_sample_item(metadata={"initial": "value"})
    await repository.create(item)

    item.metadata = {"updated": "data", "new_key": 123}
    await repository.update(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.metadata == {"updated": "data", "new_key": 123}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_null_metadata(repository: DatabaseKnowledgeItemRepository):
    """Test item with null metadata."""
    item = create_sample_item(metadata=None)
    await repository.create(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.metadata is None or retrieved.metadata == {}


# ============================================================
# Publication State Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_publish_unpublish_workflow(repository: DatabaseKnowledgeItemRepository):
    """Test publish and unpublish workflow."""
    org_id = generate_org_id()
    item = create_sample_item(organization_id=org_id, is_published=True)
    await repository.create(item)

    # Item is visible in published list
    published = await repository.list_by_organization_id(org_id, is_published=True)
    assert len(published) == 1

    # Unpublish
    item.unpublish()
    await repository.update(item)

    # Item no longer in published list
    published = await repository.list_by_organization_id(org_id, is_published=True)
    assert len(published) == 0

    # Item visible in unpublished list
    unpublished = await repository.list_by_organization_id(org_id, is_published=False)
    assert len(unpublished) == 1

    # Republish
    item.publish()
    await repository.update(item)

    published = await repository.list_by_organization_id(org_id, is_published=True)
    assert len(published) == 1


# ============================================================
# Organization Isolation Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_organization_isolation(repository: DatabaseKnowledgeItemRepository):
    """Test items are properly isolated by organization."""
    org_id1 = generate_org_id()
    org_id2 = generate_org_id()

    # Create items for each org
    for _ in range(3):
        await repository.create(create_sample_item(organization_id=org_id1))
    for _ in range(5):
        await repository.create(create_sample_item(organization_id=org_id2))

    # Each org should only see their items
    org1_items = await repository.list_by_organization_id(org_id1)
    org2_items = await repository.list_by_organization_id(org_id2)

    assert len(org1_items) == 3
    assert len(org2_items) == 5
    assert all(i.organization_id == org_id1 for i in org1_items)
    assert all(i.organization_id == org_id2 for i in org2_items)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_respects_organization(repository: DatabaseKnowledgeItemRepository):
    """Test search only returns items from specified organization."""
    org_id1 = generate_org_id()
    org_id2 = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id1,
        title="Common error handling",
    ))
    await repository.create(create_sample_item(
        organization_id=org_id2,
        title="Common error patterns",
    ))

    result = await repository.search_by_text(org_id1, "error")

    assert len(result) == 1
    assert result[0].organization_id == org_id1


# ============================================================
# InMemory Repository Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inmemory_full_lifecycle(inmemory_repository):
    """Test in-memory repository full lifecycle."""
    item = create_sample_item()

    # Create
    created = await inmemory_repository.create(item)
    assert created.item_id == item.item_id

    # Read
    retrieved = await inmemory_repository.get_by_id(item.item_id)
    assert retrieved is not None

    # Update
    item.title = "Updated title"
    updated = await inmemory_repository.update(item)
    assert updated.title == "Updated title"

    # Delete
    deleted = await inmemory_repository.delete(item.item_id)
    assert deleted is True

    # Verify deleted
    assert await inmemory_repository.get_by_id(item.item_id) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inmemory_organization_delete(inmemory_repository):
    """Test in-memory delete all items for organization."""
    org_id = generate_org_id()

    for _ in range(5):
        await inmemory_repository.create(create_sample_item(organization_id=org_id))

    count = inmemory_repository.delete_items_for_organization(org_id)
    assert count == 5

    remaining = await inmemory_repository.count_by_organization_id(org_id)
    assert remaining == 0


# ============================================================
# Edge Cases
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_large_content(repository: DatabaseKnowledgeItemRepository):
    """Test item with large content."""
    large_content = "x" * 100000
    item = create_sample_item(content=large_content)
    await repository.create(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert len(retrieved.content) == 100000


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unicode_content(repository: DatabaseKnowledgeItemRepository):
    """Test item with unicode content."""
    unicode_content = "This is unicode: こんにちは 你好 مرحبا"
    item = create_sample_item(
        title="Unicode Title こんにちは",
        content=unicode_content,
    )
    await repository.create(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert retrieved.title == "Unicode Title こんにちは"
    assert retrieved.content == unicode_content


@pytest.mark.asyncio
@pytest.mark.integration
async def test_special_characters_in_search(repository: DatabaseKnowledgeItemRepository):
    """Test search with special characters."""
    org_id = generate_org_id()

    await repository.create(create_sample_item(
        organization_id=org_id,
        title="Error: 'NoneType' has no attribute",
        content="Common Python error when accessing None.",
    ))

    result = await repository.search_by_text(org_id, "NoneType")
    assert len(result) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_all_item_types(repository: DatabaseKnowledgeItemRepository):
    """Test creating items with all possible types."""
    org_id = generate_org_id()

    for item_type in KnowledgeItemType:
        await repository.create(create_sample_item(
            organization_id=org_id,
            item_type=item_type,
        ))

    count = await repository.count_by_organization_id(org_id)
    assert count == len(KnowledgeItemType)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_many_tags(repository: DatabaseKnowledgeItemRepository):
    """Test item with many tags."""
    org_id = generate_org_id()
    tags = [f"tag_{i}" for i in range(50)]

    item = create_sample_item(organization_id=org_id, tags=tags)
    await repository.create(item)

    retrieved = await repository.get_by_id(item.item_id)
    assert len(retrieved.tags) == 50
    assert retrieved.tags == tags

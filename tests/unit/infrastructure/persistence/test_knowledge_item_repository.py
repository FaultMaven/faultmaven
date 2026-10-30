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
    """Tests for the targeted update — the repository's only write after
    creation (there is no full-row update, #1966)."""

    @pytest.fixture
    def repository(self):
        """Create fresh repository for each test."""
        return InMemoryKnowledgeItemRepository()

    @pytest.mark.asyncio
    async def test_update_item_success(self, repository):
        """Test successful item update."""
        item = create_sample_item()
        await repository.create(item)

        assert await repository.update_fields(
            item.item_id, {"title": "Updated Title", "content": "Updated content"}
        )

        result = await repository.get_by_id(item.item_id)
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

        await repository.update_fields(item.item_id, {"title": "New Title"})

        assert (await repository.get_by_id(item.item_id)).updated_at > original_updated

    @pytest.mark.asyncio
    async def test_update_not_found_reports_false(self, repository):
        """Updating a non-existent item matches no row: reported, not raised."""
        item = create_sample_item()

        assert await repository.update_fields(item.item_id, {"title": "x"}) is False

    @pytest.mark.asyncio
    async def test_update_all_fields(self, repository):
        """Test updating all mutable fields."""
        item = create_sample_item()
        await repository.create(item)

        embedding = create_valid_embedding(0.5)
        await repository.update_fields(
            item.item_id,
            {
                "title": "New Title",
                "content": "New Content",
                "category": "database",
                "tags": ["sql", "query"],
                "embedding_vector": embedding,
                "is_published": False,
                "view_count": 100,
                "helpful_count": 80,
                "not_helpful_count": 20,
            },
        )

        result = await repository.get_by_id(item.item_id)
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
# Deferred commit (#1966)
# ============================================================


@pytest.fixture(params=["database", "in_memory"])
async def deferring_repository(request):
    """Both implementations of the ABC, so the deferred-commit contract is the
    same on each. The database one is real SQLite."""
    if request.param == "in_memory":
        yield InMemoryKnowledgeItemRepository()
        return

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield DatabaseKnowledgeItemRepository(session)
    await engine.dispose()


class TestDeferredCommit:
    """``delete``/``update_fields`` with ``commit=False`` leave the write open
    for the caller to commit or roll back once it knows whether it wants it —
    which is how ``KnowledgeService.delete_document`` holds the row change
    across its vector delete (#1966)."""

    @pytest.mark.asyncio
    async def test_a_deferred_delete_is_undone_by_rollback(self, deferring_repository):
        repo = deferring_repository
        item = await repo.create(create_sample_item())

        assert await repo.delete(item.item_id, commit=False) is True
        await repo.rollback()

        assert await repo.get_by_id(item.item_id) is not None

    @pytest.mark.asyncio
    async def test_a_deferred_delete_lands_on_commit(self, deferring_repository):
        repo = deferring_repository
        item = await repo.create(create_sample_item())

        assert await repo.delete(item.item_id, commit=False) is True
        await repo.commit()

        assert await repo.get_by_id(item.item_id) is None

    @pytest.mark.asyncio
    async def test_a_deferred_targeted_write_is_undone_by_rollback(
        self, deferring_repository
    ):
        repo = deferring_repository
        item = await repo.create(create_sample_item())

        assert await repo.update_fields(
            item.item_id, {"is_published": False}, commit=False
        )
        await repo.rollback()

        assert (await repo.get_by_id(item.item_id)).is_published is True

    @pytest.mark.asyncio
    async def test_a_deferred_targeted_write_lands_on_commit(
        self, deferring_repository
    ):
        repo = deferring_repository
        item = await repo.create(create_sample_item())

        await repo.update_fields(item.item_id, {"is_published": False}, commit=False)
        await repo.commit()

        assert (await repo.get_by_id(item.item_id)).is_published is False

    @pytest.mark.asyncio
    async def test_a_write_that_matches_no_row_is_reported_not_raised(
        self, deferring_repository
    ):
        """The caller decides from the outcome whether to commit at all, so
        zero rows matched (absent, a concurrent delete, an RLS refusal) is an
        answer, not an exception."""
        repo = deferring_repository
        ghost = create_sample_item()

        assert (
            await repo.update_fields(
                ghost.item_id, {"is_published": False}, commit=False
            )
            is False
        )
        assert await repo.delete(ghost.item_id, commit=False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "zone",
    [timezone.utc, timezone(timedelta(hours=5, minutes=30)), None],
    ids=["utc", "+05:30", "naive-utc"],
)
async def test_a_delete_guarded_by_created_at_spares_another_row(
    deferring_repository, zone
):
    """A writer undoing its own INSERT removes only the row it stamped, the
    instant it stamped in whatever zone it is named (#2378)."""
    repo = deferring_repository
    item = create_sample_item()
    await repo.create(item)
    stamped = item.created_at.astimezone(timezone.utc)
    same = stamped.replace(tzinfo=None) if zone is None else stamped.astimezone(zone)

    other = stamped - timedelta(microseconds=1)
    assert await repo.delete(item.item_id, created_at=other) is False
    assert await repo.get_by_id(item.item_id) is not None
    assert await repo.delete(item.item_id, created_at=same) is True
    assert await repo.get_by_id(item.item_id) is None


@pytest.mark.asyncio
async def test_a_delete_drops_a_deferred_update_to_its_row(deferring_repository):
    repo = deferring_repository
    item = await repo.create(create_sample_item())

    await repo.update_fields(item.item_id, {"is_published": False}, commit=False)
    assert await repo.delete(item.item_id) is True
    await repo.commit()

    assert await repo.get_by_id(item.item_id) is None


class TestIncompleteIngest:
    """A row whose ingest has not completed (#2394), on both implementations:
    every listing and visibility reader hides it from everyone, its owner
    included; the trusted reads return it; it completes only through the
    guarded flip, and the sweep's delete takes it only while incomplete."""

    @staticmethod
    async def _two(repo):
        """A complete and an incomplete item, alike in all else: one owner and
        enterprise (the owner arm is the one both implementations model),
        tagged, unembedded and with enough feedback for every reader below."""
        enterprise = generate_org_id()
        items = []
        for complete in (True, False):
            item = create_sample_item(
                enterprise_id=enterprise, tags=["t"], helpful_count=3
            )
            item.owner_id = "owner-2394"
            item.ingest_complete = complete
            items.append(await repo.create(item))
        return items

    @pytest.mark.asyncio
    async def test_every_listing_hides_it_and_the_trusted_reads_return_it(
        self, deferring_repository
    ):
        repo = deferring_repository
        done, pending = await self._two(repo)
        enterprise = done.enterprise_id
        who = dict(enterprise_id=enterprise, user_id="owner-2394")

        listed = await repo.list_for_inventory(**who)
        assert [i.item_id for i in listed] == [done.item_id]
        assert await repo.get_visible_by_id(pending.item_id, **who) is None
        assert await repo.get_visible_by_id(done.item_id, **who) is not None
        assert await repo.visible_item_ids([done.item_id, pending.item_id], **who) == {
            done.item_id
        }
        # Nor does KB retrieval admit its chunks (#2397).
        assert await repo.admitted_item_ids([done.item_id, pending.item_id]) == {
            done.item_id
        }
        for found in (
            await repo.search_by_tags(enterprise, ["t"]),
            await repo.get_items_without_embeddings(enterprise),
            await repo.get_most_helpful(enterprise),
        ):
            assert [i.item_id for i in found] == [done.item_id]

        assert (await repo.get_by_id(pending.item_id)).ingest_complete is False

    @pytest.mark.asyncio
    async def test_the_flip_is_guarded_by_the_stamp_and_lands_on_commit(
        self, deferring_repository
    ):
        repo = deferring_repository
        item = create_sample_item()
        item.ingest_complete = False
        await repo.create(item)
        other = item.created_at - timedelta(microseconds=1)

        assert await repo.mark_ingest_complete(item.item_id, created_at=other) is False
        assert await repo.mark_ingest_complete(item.item_id, created_at=item.created_at)
        await repo.rollback()
        assert (await repo.get_by_id(item.item_id)).ingest_complete is False

        assert await repo.mark_ingest_complete(item.item_id, created_at=item.created_at)
        await repo.commit()
        assert (await repo.get_by_id(item.item_id)).ingest_complete is True
        assert (
            await repo.mark_ingest_complete(item.item_id, created_at=item.created_at)
            is False
        )
        assert (
            await repo.mark_ingest_complete("ki_absent", created_at=item.created_at)
            is False
        )

    @pytest.mark.asyncio
    async def test_the_sweeps_delete_spares_a_complete_row(self, deferring_repository):
        repo = deferring_repository
        done, pending = await self._two(repo)

        for item in (done, pending):
            assert await repo.delete(
                item.item_id, created_at=item.created_at, only_incomplete=True
            ) is (item is pending)

        assert await repo.get_by_id(done.item_id) is not None
        assert await repo.get_by_id(pending.item_id) is None


class TestTargetedWrite:
    """``update_fields`` writes only the fields it names (#1966): it cannot
    revert another writer's change to a column it does not name."""

    @pytest.mark.asyncio
    async def test_only_the_named_fields_move(self, deferring_repository):
        repo = deferring_repository
        item = await repo.create(create_sample_item(tags=["a"]))
        await repo.update_fields(item.item_id, {"is_published": False})

        assert await repo.update_fields(
            item.item_id, {"content": "new body", "tags": ["b"]}
        )

        stored = await repo.get_by_id(item.item_id)
        assert stored.content == "new body" and stored.tags == ["b"]
        assert stored.is_published is False, "an edit put is_published back"
        assert stored.title == item.title

    @pytest.mark.asyncio
    async def test_metadata_round_trips_and_touch_false_keeps_updated_at(
        self, deferring_repository
    ):
        repo = deferring_repository
        item = await repo.create(create_sample_item())
        before = (await repo.get_by_id(item.item_id)).updated_at

        await repo.update_fields(
            item.item_id, {"metadata": {"pack_content_hash": "abc"}}, touch=False
        )

        stored = await repo.get_by_id(item.item_id)
        assert stored.metadata == {"pack_content_hash": "abc"}
        assert stored.updated_at == before

    @pytest.mark.asyncio
    async def test_admitted_ids_are_the_complete_published_rows(
        self, deferring_repository
    ):
        """``admitted_item_ids``, the KB retrieval admission rule (#2397): a
        complete, published row is admitted; an unpublished one and an absent
        id are not; no id asks nothing. SQLite has no roles, so
        ``may_update`` is always true."""
        repo = deferring_repository
        live = await repo.create(create_sample_item())
        retired = await repo.create(create_sample_item())
        await repo.update_fields(retired.item_id, {"is_published": False})

        assert await repo.may_update() is True
        assert await repo.admitted_item_ids(
            [live.item_id, retired.item_id, "ki_absent", ""]
        ) == {live.item_id}
        assert await repo.admitted_item_ids([]) == set()

    @pytest.mark.asyncio
    async def test_an_unknown_field_is_refused(self, deferring_repository):
        repo = deferring_repository
        item = await repo.create(create_sample_item())

        with pytest.raises(ValueError, match="owner_id"):
            await repo.update_fields(item.item_id, {"owner_id": "someone"})
        with pytest.raises(ValueError):
            await repo.update_fields(item.item_id, {})


# ============================================================
# List By Organization Tests
# ============================================================


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

        # Read back through a surviving query rather than a count method: the
        # per-enterprise list/count/delete trio had no production caller and
        # went with the review round, so a helper test is the wrong place to
        # keep one alive.
        assert await repository.search_by_tags(enterprise_id, ["sample"]) == []


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

        await repository.update_fields(
            item.item_id, {"metadata": {"new_key": "new_value", "another": 123}}
        )

        result = await repository.get_by_id(item.item_id)
        assert result.metadata == {"new_key": "new_value", "another": 123}

    @pytest.mark.asyncio
    async def test_metadata_none(self, repository):
        """Test item with None metadata."""
        item = create_sample_item(metadata=None)
        await repository.create(item)

        result = await repository.get_by_id(item.item_id)

        assert result.metadata is None

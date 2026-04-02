"""Unit tests for KnowledgeItem domain model.

Tests the KnowledgeItem dataclass, validation, and helper methods.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItem,
    KnowledgeItemType,
)
from tests.utils import generate_item_id, generate_org_id


def create_sample_item(
    item_id: str = None,
    organization_id: str = None,
    title: str = "Sample Knowledge Item",
    content: str = "This is sample content for the knowledge item.",
    item_type: KnowledgeItemType = KnowledgeItemType.TROUBLESHOOTING_GUIDE,
    **kwargs,
) -> KnowledgeItem:
    """Create a sample knowledge item for testing."""
    return KnowledgeItem(
        item_id=item_id or generate_item_id(),
        organization_id=organization_id or generate_org_id(),
        title=title,
        content=content,
        item_type=item_type,
        **kwargs,
    )


def create_valid_embedding(value: float = 0.1) -> list:
    """Create a valid embedding vector of correct dimensions."""
    return [value] * EMBEDDING_DIMENSIONS


# ============================================================
# Model Creation Tests
# ============================================================


class TestKnowledgeItemCreation:
    """Tests for KnowledgeItem creation."""

    def test_create_with_required_fields(self):
        """Test creating item with only required fields."""
        item = create_sample_item()

        assert item.item_id is not None
        assert item.organization_id is not None
        assert item.title == "Sample Knowledge Item"
        assert item.content == "This is sample content for the knowledge item."
        assert item.item_type == KnowledgeItemType.TROUBLESHOOTING_GUIDE

    def test_create_with_all_fields(self):
        """Test creating item with all fields populated."""
        embedding = create_valid_embedding()
        created = datetime.now(timezone.utc) - timedelta(days=1)
        updated = datetime.now(timezone.utc)
        last_retrieved = datetime.now(timezone.utc)

        item = KnowledgeItem(
            item_id="ki_test123",
            organization_id="org_test456",
            title="Complete Item",
            content="Complete content",
            item_type=KnowledgeItemType.FAQ,
            category="networking",
            tags=["network", "connectivity"],
            embedding_model="bge-m3",
            embedding_vector=embedding,
            embedding_version=2,
            source_url="https://example.com/docs",
            author="John Doe",
            language="en",
            view_count=100,
            helpful_count=80,
            not_helpful_count=20,
            last_retrieved_at=last_retrieved,
            is_published=True,
            created_at=created,
            updated_at=updated,
            metadata={"key": "value"},
        )

        assert item.item_id == "ki_test123"
        assert item.organization_id == "org_test456"
        assert item.title == "Complete Item"
        assert item.content == "Complete content"
        assert item.item_type == KnowledgeItemType.FAQ
        assert item.category == "networking"
        assert item.tags == ["network", "connectivity"]
        assert item.embedding_model == "bge-m3"
        assert item.embedding_vector == embedding
        assert item.embedding_version == 2
        assert item.source_url == "https://example.com/docs"
        assert item.author == "John Doe"
        assert item.language == "en"
        assert item.view_count == 100
        assert item.helpful_count == 80
        assert item.not_helpful_count == 20
        assert item.last_retrieved_at == last_retrieved
        assert item.is_published is True
        assert item.created_at == created
        assert item.updated_at == updated
        assert item.metadata == {"key": "value"}

    def test_default_values(self):
        """Test default values are set correctly."""
        item = create_sample_item()

        assert item.category is None
        assert item.tags == []
        assert item.embedding_model == "bge-m3"
        assert item.embedding_vector is None
        assert item.embedding_version == 1
        assert item.source_url is None
        assert item.author is None
        assert item.language == "en"
        assert item.view_count == 0
        assert item.helpful_count == 0
        assert item.not_helpful_count == 0
        assert item.last_retrieved_at is None
        assert item.is_published is True
        assert item.metadata is None
        assert item.created_at is not None
        assert item.updated_at is not None


# ============================================================
# Validation Tests
# ============================================================


class TestKnowledgeItemValidation:
    """Tests for KnowledgeItem validation."""

    def test_empty_item_id_fails(self):
        """Test that empty item_id raises ValueError."""
        with pytest.raises(ValueError, match="item_id is required"):
            KnowledgeItem(
                item_id="",
                organization_id="org_test",
                title="Test",
                content="Content",
                item_type=KnowledgeItemType.FAQ,
            )

    def test_none_item_id_fails(self):
        """Test that None item_id raises ValueError."""
        with pytest.raises(ValueError, match="item_id is required"):
            KnowledgeItem(
                item_id=None,
                organization_id="org_test",
                title="Test",
                content="Content",
                item_type=KnowledgeItemType.FAQ,
            )

    def test_empty_organization_id_fails(self):
        """Test that empty organization_id raises ValueError."""
        with pytest.raises(ValueError, match="organization_id is required"):
            KnowledgeItem(
                item_id="ki_test",
                organization_id="",
                title="Test",
                content="Content",
                item_type=KnowledgeItemType.FAQ,
            )

    def test_empty_title_fails(self):
        """Test that empty title raises ValueError."""
        with pytest.raises(ValueError, match="title is required"):
            KnowledgeItem(
                item_id="ki_test",
                organization_id="org_test",
                title="",
                content="Content",
                item_type=KnowledgeItemType.FAQ,
            )

    def test_empty_content_fails(self):
        """Test that empty content raises ValueError."""
        with pytest.raises(ValueError, match="content is required"):
            KnowledgeItem(
                item_id="ki_test",
                organization_id="org_test",
                title="Test",
                content="",
                item_type=KnowledgeItemType.FAQ,
            )

    def test_invalid_item_type_fails(self):
        """Test that invalid item_type raises ValueError."""
        with pytest.raises(ValueError, match="item_type must be a KnowledgeItemType"):
            KnowledgeItem(
                item_id="ki_test",
                organization_id="org_test",
                title="Test",
                content="Content",
                item_type="invalid_type",
            )

    def test_negative_view_count_fails(self):
        """Test that negative view_count raises ValueError."""
        with pytest.raises(ValueError, match="view_count cannot be negative"):
            create_sample_item(view_count=-1)

    def test_negative_helpful_count_fails(self):
        """Test that negative helpful_count raises ValueError."""
        with pytest.raises(ValueError, match="helpful_count cannot be negative"):
            create_sample_item(helpful_count=-1)

    def test_negative_not_helpful_count_fails(self):
        """Test that negative not_helpful_count raises ValueError."""
        with pytest.raises(ValueError, match="not_helpful_count cannot be negative"):
            create_sample_item(not_helpful_count=-1)

    def test_zero_embedding_version_fails(self):
        """Test that embedding_version < 1 raises ValueError."""
        with pytest.raises(ValueError, match="embedding_version must be at least 1"):
            create_sample_item(embedding_version=0)

    def test_negative_embedding_version_fails(self):
        """Test that negative embedding_version raises ValueError."""
        with pytest.raises(ValueError, match="embedding_version must be at least 1"):
            create_sample_item(embedding_version=-1)

    def test_empty_embedding_vector_fails(self):
        """Test that empty embedding_vector raises ValueError."""
        with pytest.raises(ValueError, match="embedding_vector cannot be empty"):
            create_sample_item(embedding_vector=[])

    def test_wrong_dimension_embedding_fails(self):
        """Test that wrong dimension embedding raises ValueError."""
        with pytest.raises(
            ValueError, match=f"must have {EMBEDDING_DIMENSIONS} dimensions"
        ):
            create_sample_item(embedding_vector=[0.1, 0.2, 0.3])

    def test_valid_embedding_dimensions(self):
        """Test that correct dimension embedding passes."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding)
        assert item.embedding_vector == embedding

    def test_empty_language_fails(self):
        """Test that empty language raises ValueError."""
        with pytest.raises(ValueError, match="language must be a valid ISO 639-1"):
            create_sample_item(language="")

    def test_too_long_language_fails(self):
        """Test that too long language raises ValueError."""
        with pytest.raises(ValueError, match="language must be a valid ISO 639-1"):
            create_sample_item(language="extremely_long_language_code")


# ============================================================
# KnowledgeItemType Enum Tests
# ============================================================


class TestKnowledgeItemType:
    """Tests for KnowledgeItemType enum."""

    def test_all_types_exist(self):
        """Test all expected item types exist."""
        assert KnowledgeItemType.TROUBLESHOOTING_GUIDE == "troubleshooting_guide"
        assert KnowledgeItemType.ERROR_PATTERN == "error_pattern"
        assert KnowledgeItemType.SOLUTION_TEMPLATE == "solution_template"
        assert KnowledgeItemType.API_DOCUMENTATION == "api_documentation"
        assert KnowledgeItemType.CONFIGURATION_GUIDE == "configuration_guide"
        assert KnowledgeItemType.BEST_PRACTICE == "best_practice"
        assert KnowledgeItemType.FAQ == "faq"
        assert KnowledgeItemType.RUNBOOK == "runbook"

    def test_type_count(self):
        """Test there are exactly 8 item types."""
        assert len(KnowledgeItemType) == 8

    def test_create_with_each_type(self):
        """Test creating item with each type."""
        for item_type in KnowledgeItemType:
            item = create_sample_item(item_type=item_type)
            assert item.item_type == item_type


# ============================================================
# Usage Tracking Tests
# ============================================================


class TestUsageTracking:
    """Tests for usage tracking methods."""

    def test_mark_retrieved_increments_view_count(self):
        """Test mark_retrieved increments view_count."""
        item = create_sample_item(view_count=5)

        item.mark_retrieved()

        assert item.view_count == 6

    def test_mark_retrieved_updates_last_retrieved_at(self):
        """Test mark_retrieved updates last_retrieved_at."""
        item = create_sample_item()
        assert item.last_retrieved_at is None

        item.mark_retrieved()

        assert item.last_retrieved_at is not None
        assert item.last_retrieved_at.tzinfo is not None

    def test_mark_retrieved_updates_updated_at(self):
        """Test mark_retrieved updates updated_at timestamp."""
        item = create_sample_item()
        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.mark_retrieved()

        assert item.updated_at > original_updated

    def test_mark_helpful_increments_count(self):
        """Test mark_helpful increments helpful_count."""
        item = create_sample_item(helpful_count=10)

        item.mark_helpful()

        assert item.helpful_count == 11

    def test_mark_helpful_updates_timestamp(self):
        """Test mark_helpful updates updated_at."""
        item = create_sample_item()
        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.mark_helpful()

        assert item.updated_at > original_updated

    def test_mark_not_helpful_increments_count(self):
        """Test mark_not_helpful increments not_helpful_count."""
        item = create_sample_item(not_helpful_count=5)

        item.mark_not_helpful()

        assert item.not_helpful_count == 6

    def test_mark_not_helpful_updates_timestamp(self):
        """Test mark_not_helpful updates updated_at."""
        item = create_sample_item()
        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.mark_not_helpful()

        assert item.updated_at > original_updated


# ============================================================
# Helpfulness Score Tests
# ============================================================


class TestHelpfulnessScore:
    """Tests for helpfulness score calculation."""

    def test_no_feedback_returns_neutral(self):
        """Test that no feedback returns 0.5 (neutral)."""
        item = create_sample_item(helpful_count=0, not_helpful_count=0)

        assert item.get_helpfulness_score() == 0.5

    def test_all_helpful_returns_one(self):
        """Test that all helpful returns 1.0."""
        item = create_sample_item(helpful_count=10, not_helpful_count=0)

        assert item.get_helpfulness_score() == 1.0

    def test_all_not_helpful_returns_zero(self):
        """Test that all not helpful returns 0.0."""
        item = create_sample_item(helpful_count=0, not_helpful_count=10)

        assert item.get_helpfulness_score() == 0.0

    def test_half_helpful_returns_half(self):
        """Test that 50/50 returns 0.5."""
        item = create_sample_item(helpful_count=5, not_helpful_count=5)

        assert item.get_helpfulness_score() == 0.5

    def test_80_percent_helpful(self):
        """Test 80% helpfulness ratio."""
        item = create_sample_item(helpful_count=80, not_helpful_count=20)

        assert item.get_helpfulness_score() == 0.8

    def test_get_total_feedback(self):
        """Test get_total_feedback returns sum of counts."""
        item = create_sample_item(helpful_count=25, not_helpful_count=15)

        assert item.get_total_feedback() == 40


# ============================================================
# Embedding Tests
# ============================================================


class TestEmbedding:
    """Tests for embedding methods."""

    def test_has_embedding_false_when_none(self):
        """Test has_embedding returns False when vector is None."""
        item = create_sample_item(embedding_vector=None)

        assert item.has_embedding() is False

    def test_has_embedding_true_when_present(self):
        """Test has_embedding returns True when vector exists."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding)

        assert item.has_embedding() is True

    def test_get_embedding_dimensions_zero_when_none(self):
        """Test get_embedding_dimensions returns 0 when vector is None."""
        item = create_sample_item(embedding_vector=None)

        assert item.get_embedding_dimensions() == 0

    def test_get_embedding_dimensions_returns_length(self):
        """Test get_embedding_dimensions returns vector length."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding)

        assert item.get_embedding_dimensions() == EMBEDDING_DIMENSIONS

    def test_set_embedding_success(self):
        """Test set_embedding sets vector correctly."""
        item = create_sample_item()
        embedding = create_valid_embedding(0.5)

        item.set_embedding(embedding, model="custom-model", version=3)

        assert item.embedding_vector == embedding
        assert item.embedding_model == "custom-model"
        assert item.embedding_version == 3

    def test_set_embedding_wrong_dimensions_fails(self):
        """Test set_embedding with wrong dimensions raises ValueError."""
        item = create_sample_item()

        with pytest.raises(ValueError, match="must have"):
            item.set_embedding([0.1, 0.2, 0.3])

    def test_set_embedding_updates_timestamp(self):
        """Test set_embedding updates updated_at."""
        item = create_sample_item()
        original_updated = item.updated_at
        embedding = create_valid_embedding()

        import time

        time.sleep(0.001)

        item.set_embedding(embedding)

        assert item.updated_at > original_updated

    def test_clear_embedding(self):
        """Test clear_embedding sets vector to None."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding)
        assert item.has_embedding()

        item.clear_embedding()

        assert item.embedding_vector is None
        assert item.has_embedding() is False

    def test_needs_reindexing_when_no_embedding(self):
        """Test needs_reindexing returns True when no embedding."""
        item = create_sample_item()

        assert item.needs_reindexing(1) is True

    def test_needs_reindexing_when_older_version(self):
        """Test needs_reindexing returns True when embedding version is older."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding, embedding_version=1)

        assert item.needs_reindexing(2) is True

    def test_needs_reindexing_when_current_version(self):
        """Test needs_reindexing returns False when embedding is current."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding, embedding_version=2)

        assert item.needs_reindexing(2) is False

    def test_needs_reindexing_when_newer_version(self):
        """Test needs_reindexing returns False when embedding is newer."""
        embedding = create_valid_embedding()
        item = create_sample_item(embedding_vector=embedding, embedding_version=3)

        assert item.needs_reindexing(2) is False


# ============================================================
# Tag Tests
# ============================================================


class TestTags:
    """Tests for tag management methods."""

    def test_add_tag_success(self):
        """Test adding a new tag."""
        item = create_sample_item(tags=[])

        result = item.add_tag("python")

        assert result is True
        assert "python" in item.tags

    def test_add_tag_duplicate_returns_false(self):
        """Test adding duplicate tag returns False."""
        item = create_sample_item(tags=["python"])

        result = item.add_tag("python")

        assert result is False
        assert item.tags.count("python") == 1

    def test_add_tag_updates_timestamp(self):
        """Test add_tag updates updated_at."""
        item = create_sample_item()
        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.add_tag("newtag")

        assert item.updated_at > original_updated

    def test_remove_tag_success(self):
        """Test removing an existing tag."""
        item = create_sample_item(tags=["python", "debugging"])

        result = item.remove_tag("python")

        assert result is True
        assert "python" not in item.tags
        assert "debugging" in item.tags

    def test_remove_tag_not_found_returns_false(self):
        """Test removing non-existent tag returns False."""
        item = create_sample_item(tags=["python"])

        result = item.remove_tag("java")

        assert result is False

    def test_has_tag_true(self):
        """Test has_tag returns True when tag exists."""
        item = create_sample_item(tags=["python", "debugging"])

        assert item.has_tag("python") is True

    def test_has_tag_false(self):
        """Test has_tag returns False when tag doesn't exist."""
        item = create_sample_item(tags=["python"])

        assert item.has_tag("java") is False

    def test_has_any_tags_true(self):
        """Test has_any_tags returns True when any tag matches."""
        item = create_sample_item(tags=["python", "debugging"])

        assert item.has_any_tags(["java", "python"]) is True

    def test_has_any_tags_false(self):
        """Test has_any_tags returns False when no tags match."""
        item = create_sample_item(tags=["python", "debugging"])

        assert item.has_any_tags(["java", "rust"]) is False

    def test_has_any_tags_empty_list(self):
        """Test has_any_tags with empty list returns False."""
        item = create_sample_item(tags=["python"])

        assert item.has_any_tags([]) is False

    def test_has_all_tags_true(self):
        """Test has_all_tags returns True when all tags present."""
        item = create_sample_item(tags=["python", "debugging", "networking"])

        assert item.has_all_tags(["python", "debugging"]) is True

    def test_has_all_tags_false(self):
        """Test has_all_tags returns False when not all tags present."""
        item = create_sample_item(tags=["python", "debugging"])

        assert item.has_all_tags(["python", "networking"]) is False

    def test_has_all_tags_empty_list(self):
        """Test has_all_tags with empty list returns True."""
        item = create_sample_item(tags=["python"])

        assert item.has_all_tags([]) is True


# ============================================================
# Publication Tests
# ============================================================


class TestPublication:
    """Tests for publication methods."""

    def test_publish_sets_is_published_true(self):
        """Test publish sets is_published to True."""
        item = create_sample_item(is_published=False)

        item.publish()

        assert item.is_published is True

    def test_unpublish_sets_is_published_false(self):
        """Test unpublish sets is_published to False."""
        item = create_sample_item(is_published=True)

        item.unpublish()

        assert item.is_published is False

    def test_publish_updates_timestamp(self):
        """Test publish updates updated_at."""
        item = create_sample_item(is_published=False)
        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.publish()

        assert item.updated_at > original_updated


# ============================================================
# Touch Method Tests
# ============================================================


class TestTouch:
    """Tests for touch method."""

    def test_touch_updates_timestamp(self):
        """Test touch updates updated_at to current time."""
        item = create_sample_item()
        original_updated = item.updated_at

        import time

        time.sleep(0.001)

        item.touch()

        assert item.updated_at > original_updated

    def test_touch_uses_utc(self):
        """Test touch uses UTC timezone."""
        item = create_sample_item()

        item.touch()

        assert item.updated_at.tzinfo == timezone.utc


# ============================================================
# Repr Tests
# ============================================================


class TestRepr:
    """Tests for __repr__ method."""

    def test_repr_includes_item_id(self):
        """Test __repr__ includes item_id."""
        item = create_sample_item(item_id="ki_test123")

        repr_str = repr(item)

        assert "ki_test123" in repr_str

    def test_repr_includes_title(self):
        """Test __repr__ includes title."""
        item = create_sample_item(title="Test Title")

        repr_str = repr(item)

        assert "Test Title" in repr_str

    def test_repr_includes_item_type(self):
        """Test __repr__ includes item_type."""
        item = create_sample_item(item_type=KnowledgeItemType.FAQ)

        repr_str = repr(item)

        assert "faq" in repr_str

    def test_repr_includes_is_published(self):
        """Test __repr__ includes is_published."""
        item = create_sample_item(is_published=True)

        repr_str = repr(item)

        assert "is_published=True" in repr_str


# ============================================================
# Edge Case Tests
# ============================================================


class TestEdgeCases:
    """Tests for edge cases."""

    def test_large_content(self):
        """Test creating item with large content."""
        large_content = "x" * 100000
        item = create_sample_item(content=large_content)

        assert len(item.content) == 100000

    def test_unicode_content(self):
        """Test creating item with unicode content."""
        unicode_content = "This is unicode: こんにちは 你好 مرحبا"
        item = create_sample_item(content=unicode_content)

        assert item.content == unicode_content

    def test_special_characters_in_title(self):
        """Test creating item with special characters in title."""
        special_title = "Error: 'NoneType' has no attribute <foo>"
        item = create_sample_item(title=special_title)

        assert item.title == special_title

    def test_max_counts(self):
        """Test creating item with very large counts."""
        item = create_sample_item(
            view_count=1000000000,
            helpful_count=500000000,
            not_helpful_count=500000000,
        )

        assert item.view_count == 1000000000
        assert item.get_helpfulness_score() == 0.5

    def test_metadata_with_nested_objects(self):
        """Test creating item with nested metadata."""
        metadata = {
            "level1": {
                "level2": {
                    "level3": ["a", "b", "c"],
                },
            },
            "array": [1, 2, {"nested": True}],
        }
        item = create_sample_item(metadata=metadata)

        assert item.metadata == metadata

    def test_multiple_languages(self):
        """Test creating items with different language codes."""
        languages = ["en", "es", "fr", "de", "zh", "ja", "ar", "pt"]

        for lang in languages:
            item = create_sample_item(language=lang)
            assert item.language == lang

    def test_all_categories(self):
        """Test various category values."""
        categories = [
            "networking",
            "database",
            "authentication",
            "api",
            "infrastructure",
        ]

        for category in categories:
            item = create_sample_item(category=category)
            assert item.category == category

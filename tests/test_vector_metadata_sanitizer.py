"""Tests for VectorMetadataSanitizer.

Property-like tests for metadata sanitization:
- None handling
- Nested dict flattening
- Max string length enforcement
- Type coercion
- Edge cases
"""

import pytest
from datetime import datetime, date
from uuid import UUID, uuid4


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sanitizer():
    """Create a sanitizer with default settings."""
    from faultmaven.infrastructure.vector.sanitizer import VectorMetadataSanitizer
    return VectorMetadataSanitizer()


@pytest.fixture
def chroma_sanitizer():
    """Create a sanitizer configured for ChromaDB."""
    from faultmaven.infrastructure.vector.sanitizer import create_chroma_sanitizer
    return create_chroma_sanitizer()


@pytest.fixture
def pinecone_sanitizer():
    """Create a sanitizer configured for Pinecone."""
    from faultmaven.infrastructure.vector.sanitizer import create_pinecone_sanitizer
    return create_pinecone_sanitizer()


# =============================================================================
# None Handling Tests
# =============================================================================

class TestNoneHandling:
    """Tests for None value handling."""

    def test_sanitize_none_input(self, sanitizer):
        """Test that None input returns empty dict."""
        result = sanitizer.sanitize(None)
        assert result == {}

    def test_sanitize_removes_none_values(self, sanitizer):
        """Test that None values are removed from metadata."""
        metadata = {
            "title": "Test",
            "empty": None,
            "description": None,
        }
        result = sanitizer.sanitize(metadata)

        assert "title" in result
        assert "empty" not in result
        assert "description" not in result

    def test_sanitize_removes_deeply_nested_none(self, sanitizer):
        """Test that None in nested structures is handled."""
        metadata = {
            "nested": {
                "value": None,
                "other": "keep",
            }
        }
        result = sanitizer.sanitize(metadata)

        assert "nested.other" in result
        assert "nested.value" not in result

    def test_empty_string_not_treated_as_none(self, sanitizer):
        """Test that empty strings are removed (not same as None)."""
        metadata = {
            "empty_string": "",
            "whitespace": "   ",
            "valid": "value",
        }
        result = sanitizer.sanitize(metadata)

        assert "valid" in result
        assert "empty_string" not in result
        assert "whitespace" not in result


# =============================================================================
# Nested Dict Flattening Tests
# =============================================================================

class TestNestedFlattening:
    """Tests for nested dictionary flattening."""

    def test_flatten_simple_nested(self, sanitizer):
        """Test flattening of simple nested dict."""
        metadata = {
            "level1": {
                "level2": "value",
            }
        }
        result = sanitizer.sanitize(metadata)

        assert "level1.level2" in result
        assert result["level1.level2"] == "value"

    def test_flatten_multiple_levels(self, sanitizer):
        """Test flattening of multiple nesting levels."""
        metadata = {
            "a": {
                "b": {
                    "c": "deep",
                }
            }
        }
        result = sanitizer.sanitize(metadata)

        assert "a.b.c" in result
        assert result["a.b.c"] == "deep"

    def test_max_nesting_depth(self, sanitizer):
        """Test that max nesting depth is enforced."""
        # Create deeply nested dict
        metadata = {"l1": {"l2": {"l3": {"l4": {"l5": "too deep"}}}}}
        result = sanitizer.sanitize(metadata)

        # Default max depth is 3, so l4+ should be stringified
        assert "l1.l2.l3" in result

    def test_mixed_nested_and_flat(self, sanitizer):
        """Test handling of mixed nested and flat keys."""
        metadata = {
            "flat": "value",
            "nested": {
                "inner": "nested_value",
            },
            "another_flat": 42,
        }
        result = sanitizer.sanitize(metadata)

        assert result["flat"] == "value"
        assert result["nested.inner"] == "nested_value"
        assert result["another_flat"] == 42


# =============================================================================
# Max String Length Tests
# =============================================================================

class TestMaxStringLength:
    """Tests for string length enforcement."""

    def test_long_string_truncated(self):
        """Test that long strings are truncated."""
        from faultmaven.infrastructure.vector.sanitizer import VectorMetadataSanitizer

        sanitizer = VectorMetadataSanitizer(max_string_length=50)
        metadata = {
            "long_text": "x" * 100,
        }
        result = sanitizer.sanitize(metadata)

        assert len(result["long_text"]) == 50

    def test_short_string_unchanged(self, sanitizer):
        """Test that short strings are not modified."""
        metadata = {
            "short": "hello",
        }
        result = sanitizer.sanitize(metadata)

        assert result["short"] == "hello"

    def test_key_length_limited(self, sanitizer):
        """Test that key length is limited."""
        long_key = "a" * 100
        metadata = {long_key: "value"}
        result = sanitizer.sanitize(metadata)

        # Key should be truncated to 64 chars
        keys = list(result.keys())
        assert all(len(k) <= 64 for k in keys)


# =============================================================================
# Type Coercion Tests
# =============================================================================

class TestTypeCoercion:
    """Tests for type conversion."""

    def test_primitives_preserved(self, sanitizer):
        """Test that primitive types are preserved."""
        metadata = {
            "string": "text",
            "integer": 42,
            "float": 3.14,
            "boolean": True,
        }
        result = sanitizer.sanitize(metadata)

        assert result["string"] == "text"
        assert result["integer"] == 42
        assert result["float"] == 3.14
        assert result["boolean"] is True

    def test_datetime_converted(self, sanitizer):
        """Test that datetime is converted to ISO string."""
        dt = datetime(2025, 1, 2, 12, 0, 0)
        metadata = {"timestamp": dt}
        result = sanitizer.sanitize(metadata)

        assert result["timestamp"] == "2025-01-02T12:00:00"

    def test_date_converted(self, sanitizer):
        """Test that date is converted to ISO string."""
        d = date(2025, 1, 2)
        metadata = {"date": d}
        result = sanitizer.sanitize(metadata)

        assert result["date"] == "2025-01-02"

    def test_uuid_converted(self, sanitizer):
        """Test that UUID is converted to string."""
        uid = uuid4()
        metadata = {"id": uid}
        result = sanitizer.sanitize(metadata)

        assert result["id"] == str(uid)

    def test_list_to_string_default(self, chroma_sanitizer):
        """Test that lists are converted to comma-separated strings (Chroma)."""
        metadata = {"tags": ["error", "database", "timeout"]}
        result = chroma_sanitizer.sanitize(metadata)

        assert result["tags"] == "error,database,timeout"

    def test_list_preserved_pinecone(self, pinecone_sanitizer):
        """Test that lists are preserved for Pinecone."""
        metadata = {"tags": ["error", "database", "timeout"]}
        result = pinecone_sanitizer.sanitize(metadata)

        assert result["tags"] == ["error", "database", "timeout"]

    def test_bytes_decoded(self, sanitizer):
        """Test that bytes are decoded to string."""
        metadata = {"data": b"hello world"}
        result = sanitizer.sanitize(metadata)

        assert result["data"] == "hello world"

    def test_complex_object_stringified(self, sanitizer):
        """Test that complex objects are stringified."""
        class CustomObject:
            def __str__(self):
                return "custom_value"

        metadata = {"custom": CustomObject()}
        result = sanitizer.sanitize(metadata)

        assert result["custom"] == "custom_value"


# =============================================================================
# Edge Cases Tests
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and special values."""

    def test_nan_removed(self, sanitizer):
        """Test that NaN float is removed."""
        metadata = {"value": float("nan")}
        result = sanitizer.sanitize(metadata)

        assert "value" not in result

    def test_infinity_removed(self, sanitizer):
        """Test that infinity is removed."""
        metadata = {
            "positive_inf": float("inf"),
            "negative_inf": float("-inf"),
        }
        result = sanitizer.sanitize(metadata)

        assert "positive_inf" not in result
        assert "negative_inf" not in result

    def test_empty_dict_input(self, sanitizer):
        """Test that empty dict returns empty dict."""
        result = sanitizer.sanitize({})
        assert result == {}

    def test_special_characters_in_key(self, sanitizer):
        """Test that special characters in keys are handled."""
        metadata = {
            "key with spaces": "value1",
            "key-with-dashes": "value2",
            "key.with.dots": "value3",
        }
        result = sanitizer.sanitize(metadata)

        # Spaces become underscores, dashes become underscores
        assert "key_with_spaces" in result
        assert "key_with_dashes" in result
        assert "key.with.dots" in result

    def test_empty_list_removed(self, sanitizer):
        """Test that empty lists are removed."""
        metadata = {"empty_list": []}
        result = sanitizer.sanitize(metadata)

        assert "empty_list" not in result

    def test_list_with_none_items(self, chroma_sanitizer):
        """Test that None items in lists are filtered."""
        metadata = {"tags": ["a", None, "b", None]}
        result = chroma_sanitizer.sanitize(metadata)

        assert result["tags"] == "a,b"

    def test_reserved_keys_not_modified(self, sanitizer):
        """Test that reserved keys are preserved."""
        # Note: Reserved keys like 'id' should be handled carefully
        metadata = {"id": "doc123", "title": "Test"}
        result = sanitizer.sanitize(metadata)

        assert result.get("id") == "doc123"

    def test_internal_keys_removed(self, sanitizer):
        """Test that internal keys are removed."""
        metadata = {
            "__internal": "secret",
            "__private": "hidden",
            "public": "visible",
        }
        result = sanitizer.sanitize(metadata)

        assert "__internal" not in result
        assert "__private" not in result
        assert "public" in result


# =============================================================================
# Convenience Function Tests
# =============================================================================

class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_sanitize_metadata_function(self):
        """Test the sanitize_metadata convenience function."""
        from faultmaven.infrastructure.vector.sanitizer import sanitize_metadata

        metadata = {
            "title": "Test",
            "nested": {"key": "value"},
            "none_value": None,
        }
        result = sanitize_metadata(metadata)

        assert "title" in result
        assert "nested.key" in result
        assert "none_value" not in result

    def test_create_chroma_sanitizer(self):
        """Test create_chroma_sanitizer factory."""
        from faultmaven.infrastructure.vector.sanitizer import create_chroma_sanitizer

        sanitizer = create_chroma_sanitizer()
        assert sanitizer.preserve_lists is False

    def test_create_pinecone_sanitizer(self):
        """Test create_pinecone_sanitizer factory."""
        from faultmaven.infrastructure.vector.sanitizer import create_pinecone_sanitizer

        sanitizer = create_pinecone_sanitizer()
        assert sanitizer.preserve_lists is True


# =============================================================================
# Cross-Backend Compatibility Tests
# =============================================================================

class TestCrossBackendCompatibility:
    """Tests to ensure sanitized metadata works with both backends."""

    def test_sanitized_metadata_is_json_serializable(self, sanitizer):
        """Test that sanitized metadata can be JSON serialized."""
        import json

        metadata = {
            "title": "Test",
            "count": 42,
            "rate": 3.14,
            "active": True,
            "timestamp": datetime.now(),
            "nested": {"key": "value"},
        }
        result = sanitizer.sanitize(metadata)

        # Should not raise
        json_str = json.dumps(result)
        assert isinstance(json_str, str)

    def test_sanitized_values_are_primitives(self, sanitizer):
        """Test that all sanitized values are primitive types."""
        metadata = {
            "complex": {"nested": {"deep": "value"}},
            "date": datetime.now(),
            "uuid": uuid4(),
        }
        result = sanitizer.sanitize(metadata)

        for key, value in result.items():
            assert isinstance(value, (str, int, float, bool, list)), \
                f"Key {key} has non-primitive type: {type(value)}"

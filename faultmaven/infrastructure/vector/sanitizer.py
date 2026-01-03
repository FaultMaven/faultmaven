"""Vector metadata sanitizer for cross-backend portability.

Ensures metadata is compatible with both Chroma and Pinecone by:
- Removing None values
- Flattening nested structures
- Enforcing max string length
- Converting non-primitive types to strings
- Handling edge cases (empty values, special characters)

Usage:
    from faultmaven.infrastructure.vector.sanitizer import VectorMetadataSanitizer

    sanitizer = VectorMetadataSanitizer()
    clean_metadata = sanitizer.sanitize(raw_metadata)
"""

import logging
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Set, Union
from uuid import UUID


logger = logging.getLogger(__name__)


# Default limits for metadata values
DEFAULT_MAX_STRING_LENGTH = 1000
DEFAULT_MAX_LIST_ITEMS = 100
DEFAULT_MAX_NESTED_DEPTH = 3


class VectorMetadataSanitizer:
    """Sanitizes metadata for vector database compatibility.

    Both Chroma and Pinecone have restrictions on metadata:
    - Chroma: Supports str, int, float, bool (no None, no nested dicts)
    - Pinecone: Supports str, int, float, bool, list of str (no None)

    This sanitizer normalizes metadata to work across both backends:
    - Removes None values
    - Flattens nested dicts with dot notation
    - Converts lists to comma-separated strings (or keeps for Pinecone)
    - Enforces string length limits
    - Converts complex types to strings

    Attributes:
        max_string_length: Maximum length for string values
        max_list_items: Maximum items in list values
        max_nested_depth: Maximum depth for nested dict flattening
        preserve_lists: If True, keeps lists (for Pinecone); else converts to strings
    """

    # Reserved metadata keys that should never be modified
    RESERVED_KEYS: Set[str] = {"id", "_id", "document_id", "embedding"}

    # Keys that should be removed (internal use only)
    INTERNAL_KEYS: Set[str] = {"__internal", "__private", "_vector"}

    def __init__(
        self,
        max_string_length: int = DEFAULT_MAX_STRING_LENGTH,
        max_list_items: int = DEFAULT_MAX_LIST_ITEMS,
        max_nested_depth: int = DEFAULT_MAX_NESTED_DEPTH,
        preserve_lists: bool = False,
    ):
        """Initialize the sanitizer.

        Args:
            max_string_length: Maximum length for string values
            max_list_items: Maximum items in list values
            max_nested_depth: Maximum depth for nested dict flattening
            preserve_lists: If True, keeps lists as-is (Pinecone); else converts to strings
        """
        self.max_string_length = max_string_length
        self.max_list_items = max_list_items
        self.max_nested_depth = max_nested_depth
        self.preserve_lists = preserve_lists

    def sanitize(self, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Sanitize metadata for vector database compatibility.

        Args:
            metadata: Raw metadata dictionary (may contain None, nested dicts, etc.)

        Returns:
            Clean metadata dictionary safe for any vector backend

        Example:
            >>> sanitizer = VectorMetadataSanitizer()
            >>> sanitizer.sanitize({
            ...     "title": "Error Log",
            ...     "nested": {"key": "value"},
            ...     "empty": None,
            ...     "tags": ["error", "database"],
            ... })
            {'title': 'Error Log', 'nested.key': 'value', 'tags': 'error,database'}
        """
        if metadata is None:
            return {}

        if not isinstance(metadata, dict):
            logger.warning(f"Metadata is not a dict, got {type(metadata)}")
            return {}

        # Flatten nested structures
        flattened = self._flatten(metadata)

        # Sanitize each value
        result: Dict[str, Any] = {}
        for key, value in flattened.items():
            # Skip internal keys
            if key in self.INTERNAL_KEYS:
                continue

            # Skip None values
            if value is None:
                continue

            # Clean the key
            clean_key = self._sanitize_key(key)
            if not clean_key:
                continue

            # Clean the value
            clean_value = self._sanitize_value(value)
            if clean_value is not None:
                result[clean_key] = clean_value

        return result

    def _flatten(
        self,
        data: Dict[str, Any],
        prefix: str = "",
        depth: int = 0,
    ) -> Dict[str, Any]:
        """Flatten nested dictionaries with dot notation.

        Args:
            data: Dictionary to flatten
            prefix: Current key prefix
            depth: Current nesting depth

        Returns:
            Flattened dictionary
        """
        result: Dict[str, Any] = {}

        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key

            if isinstance(value, dict) and depth < self.max_nested_depth:
                # Recursively flatten nested dicts
                nested = self._flatten(value, full_key, depth + 1)
                result.update(nested)
            else:
                result[full_key] = value

        return result

    def _sanitize_key(self, key: str) -> Optional[str]:
        """Sanitize a metadata key.

        Args:
            key: Raw key string

        Returns:
            Sanitized key, or None if invalid
        """
        if not key or not isinstance(key, str):
            return None

        # Remove leading/trailing whitespace
        clean_key = key.strip()

        # Replace problematic characters
        clean_key = clean_key.replace(" ", "_")
        clean_key = clean_key.replace("-", "_")

        # Remove non-alphanumeric except underscore and dot
        clean_key = "".join(
            c for c in clean_key if c.isalnum() or c in "_."
        )

        # Ensure key is not empty after cleaning
        if not clean_key:
            return None

        # Limit key length
        if len(clean_key) > 64:
            clean_key = clean_key[:64]

        return clean_key

    def _sanitize_value(self, value: Any) -> Optional[Union[str, int, float, bool, List[str]]]:
        """Sanitize a metadata value.

        Args:
            value: Raw value of any type

        Returns:
            Sanitized value (primitive type), or None if cannot be sanitized
        """
        # Handle None
        if value is None:
            return None

        # Handle primitives directly
        if isinstance(value, bool):
            return value

        if isinstance(value, int):
            return value

        if isinstance(value, float):
            # Handle special float values
            if value != value:  # NaN check
                return None
            if value == float("inf") or value == float("-inf"):
                return None
            return value

        # Handle strings
        if isinstance(value, str):
            return self._sanitize_string(value)

        # Handle lists
        if isinstance(value, (list, tuple, set)):
            return self._sanitize_list(value)

        # Handle datetime
        if isinstance(value, datetime):
            return value.isoformat()

        if isinstance(value, date):
            return value.isoformat()

        # Handle UUID
        if isinstance(value, UUID):
            return str(value)

        # Handle bytes
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")[:self.max_string_length]
            except UnicodeDecodeError:
                return None

        # Handle dicts (shouldn't happen after flattening, but just in case)
        if isinstance(value, dict):
            # Convert to string representation
            import json
            try:
                return json.dumps(value)[:self.max_string_length]
            except (TypeError, ValueError):
                return str(value)[:self.max_string_length]

        # Handle other objects
        try:
            return str(value)[:self.max_string_length]
        except Exception:
            return None

    def _sanitize_string(self, value: str) -> Optional[str]:
        """Sanitize a string value.

        Args:
            value: String to sanitize

        Returns:
            Sanitized string, or None if empty after sanitization
        """
        # Handle empty strings
        if not value:
            return None

        # Strip whitespace
        clean = value.strip()
        if not clean:
            return None

        # Enforce length limit
        if len(clean) > self.max_string_length:
            clean = clean[:self.max_string_length]

        return clean

    def _sanitize_list(self, value: Union[list, tuple, set]) -> Optional[Union[str, List[str]]]:
        """Sanitize a list value.

        Args:
            value: List to sanitize

        Returns:
            Comma-separated string (if preserve_lists=False) or list of strings
        """
        # Convert to list and sanitize each item
        items: List[str] = []
        for item in value:
            if item is None:
                continue
            str_item = str(item).strip()
            if str_item:
                items.append(str_item)

        # Limit number of items
        items = items[:self.max_list_items]

        if not items:
            return None

        if self.preserve_lists:
            return items
        else:
            # Join as comma-separated string
            return ",".join(items)


# Global sanitizer instance with default settings
default_sanitizer = VectorMetadataSanitizer()


def sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convenience function to sanitize metadata with default settings.

    Args:
        metadata: Raw metadata dictionary

    Returns:
        Sanitized metadata dictionary
    """
    return default_sanitizer.sanitize(metadata)


def create_chroma_sanitizer() -> VectorMetadataSanitizer:
    """Create sanitizer configured for ChromaDB.

    ChromaDB doesn't support lists in metadata, so they are joined as strings.
    """
    return VectorMetadataSanitizer(
        max_string_length=1000,
        preserve_lists=False,  # ChromaDB doesn't support lists
    )


def create_pinecone_sanitizer() -> VectorMetadataSanitizer:
    """Create sanitizer configured for Pinecone.

    Pinecone supports lists of strings in metadata.
    """
    return VectorMetadataSanitizer(
        max_string_length=1000,
        preserve_lists=True,  # Pinecone supports lists
    )

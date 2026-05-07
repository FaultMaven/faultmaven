"""Knowledge Item domain model.

Represents a knowledge base item for the RAG (Retrieval-Augmented Generation) system.
Knowledge items are indexed documents with embeddings for vector search,
supporting case deflection and agent augmentation.

Design Reference: TASK-009 Knowledge Item Repository Pattern
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional, Union

# BGE-M3 produces 1024-dimensional vectors (canonical embedding model).
# Note: the legacy EmbeddingService/KnowledgeSearchService path referenced
# text-embedding-3-small at 1536 dims — that path is superseded.
EMBEDDING_DIMENSIONS = 1024


class VerificationLevel(IntEnum):
    """Verification levels for knowledge items.

    Higher values indicate more trust and visibility in search results.

    Levels:
        EXPERIMENTAL (0): AI-generated or unreviewed content
        COMMUNITY (1): Peer-reviewed or community-validated content
        ADMIN_VERIFIED (2): Admin-verified, gold standard content
    """

    EXPERIMENTAL = 0
    COMMUNITY = 1
    ADMIN_VERIFIED = 2


class KnowledgeItemType(str, Enum):
    """Types of knowledge items.

    Categorizes the type of knowledge content stored in the knowledge base.
    """

    TROUBLESHOOTING_GUIDE = "troubleshooting_guide"
    ERROR_PATTERN = "error_pattern"
    SOLUTION_TEMPLATE = "solution_template"
    API_DOCUMENTATION = "api_documentation"
    CONFIGURATION_GUIDE = "configuration_guide"
    BEST_PRACTICE = "best_practice"
    FAQ = "faq"
    RUNBOOK = "runbook"


class KnowledgeScope(str, Enum):
    """Visibility scope for a KnowledgeItem.

    Mirrors the `knowledge_items.scope` CHECK constraint in the ORM. The
    enum subclasses `str` so existing comparisons like `scope == "personal"`
    continue to work without consumer-side changes.

    Values:
        PERSONAL: Visible only to one user (requires owner_id).
        TEAM: Visible to one team (requires team_id).
        ORGANIZATION: Visible across one workspace (organization_id is
            the only ownership marker; owner_id and team_id are optional).
        GLOBAL: Platform-wide built-in runbooks (FaultMaven-shipped only).
    """

    PERSONAL = "personal"
    TEAM = "team"
    ORGANIZATION = "organization"
    GLOBAL = "global"


@dataclass
class KnowledgeItem:
    """Knowledge base item for RAG system.

    Represents an indexed document or knowledge snippet with embeddings
    for semantic search and retrieval.

    Attributes:
        item_id: Unique identifier for the knowledge item
        organization_id: Organization that owns this knowledge item
        title: Title of the knowledge item
        content: Full text content of the knowledge item
        item_type: Type of knowledge (troubleshooting_guide, faq, etc.)
        category: Optional category for grouping (networking, database, etc.)
        tags: List of tags for filtering and search
        embedding_model: Name of the embedding model used
        embedding_vector: Vector representation for similarity search
        embedding_version: Version for tracking re-indexing needs
        source_url: Original documentation URL if applicable
        author: Author of the knowledge item
        language: ISO 639-1 language code
        view_count: Number of times item was retrieved
        helpful_count: Number of times marked as helpful
        not_helpful_count: Number of times marked as not helpful
        last_retrieved_at: Timestamp of last retrieval
        is_published: Whether item is visible for retrieval
        created_at: Record creation timestamp
        updated_at: Record update timestamp
        metadata: Additional item-specific metadata (JSON)
    """

    item_id: str
    organization_id: str
    title: str
    content: str
    item_type: KnowledgeItemType
    scope: Union[KnowledgeScope, str] = KnowledgeScope.GLOBAL
    owner_id: Optional[str] = None
    team_id: Optional[str] = None

    # Categorization
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    # Vector search
    embedding_model: str = "bge-m3"
    embedding_vector: Optional[List[float]] = None
    embedding_version: int = 1

    # Metadata
    source_url: Optional[str] = None
    author: Optional[str] = None
    language: str = "en"

    # Verification status
    verification_level: int = (
        0  # VerificationLevel enum value (0=experimental, 1=community, 2=admin_verified)
    )
    verification_reason: Optional[str] = (
        None  # Why this level? (e.g., "Reviewed by admin on 2026-01-15")
    )
    verified_by: Optional[str] = None  # User ID who verified
    verified_at: Optional[datetime] = None  # When verification occurred

    # Lineage tracking (for suggestions that became knowledge items)
    source_suggestion_id: Optional[str] = None  # Link back to original suggestion

    # Usage tracking
    view_count: int = 0
    helpful_count: int = 0
    not_helpful_count: int = 0
    last_retrieved_at: Optional[datetime] = None

    # Lifecycle
    is_published: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        """Validate knowledge item data."""
        if not self.item_id:
            raise ValueError("item_id is required")
        if not self.organization_id:
            raise ValueError("organization_id is required")
        if not self.title:
            raise ValueError("title is required")
        if not self.content:
            raise ValueError("content is required")
        if not isinstance(self.item_type, KnowledgeItemType):
            raise ValueError(
                f"item_type must be a KnowledgeItemType, got {type(self.item_type)}"
            )

        # Coerce string scope to enum (ValueError if not in the allowed set).
        # Done here so callers can pass either KnowledgeScope.PERSONAL or
        # the literal "personal" without extra ceremony.
        if isinstance(self.scope, str) and not isinstance(self.scope, KnowledgeScope):
            try:
                self.scope = KnowledgeScope(self.scope)
            except ValueError as e:
                raise ValueError(
                    f"scope must be one of "
                    f"{[s.value for s in KnowledgeScope]}, got {self.scope!r}"
                ) from e
        if self.scope == KnowledgeScope.PERSONAL and not self.owner_id:
            raise ValueError("owner_id is required for personal scope")
        if self.scope == KnowledgeScope.TEAM and not self.team_id:
            raise ValueError("team_id is required for team scope")
        # ORGANIZATION and GLOBAL scope: organization_id is the ownership
        # marker (already required as a base field above). No additional
        # owner_id / team_id required.

        # Tag values must not contain commas (SQLite stores tags
        # comma-separated; PG stores as TEXT[]). The cross-dialect
        # round-trip is lossless only if commas don't appear in values.
        for tag in self.tags:
            if "," in tag:
                raise ValueError(
                    f"tag values must not contain commas (got {tag!r}); "
                    "commas would break the SQLite serialization round-trip"
                )

        # Validate verification level (0=experimental, 1=community, 2=admin_verified)
        if self.verification_level < 0 or self.verification_level > 2:
            raise ValueError(
                f"verification_level must be 0, 1, or 2, got {self.verification_level}"
            )

        # Validate counts
        if self.view_count < 0:
            raise ValueError("view_count cannot be negative")
        if self.helpful_count < 0:
            raise ValueError("helpful_count cannot be negative")
        if self.not_helpful_count < 0:
            raise ValueError("not_helpful_count cannot be negative")

        # Validate embedding version
        if self.embedding_version < 1:
            raise ValueError("embedding_version must be at least 1")

        # Validate embedding dimensions if present
        if self.embedding_vector is not None:
            if len(self.embedding_vector) == 0:
                raise ValueError("embedding_vector cannot be empty")
            if len(self.embedding_vector) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"embedding_vector must have {EMBEDDING_DIMENSIONS} dimensions, "
                    f"got {len(self.embedding_vector)}"
                )

        # Validate language code (basic ISO 639-1 validation)
        if not self.language or len(self.language) < 2 or len(self.language) > 8:
            raise ValueError("language must be a valid ISO 639-1 code (2-8 characters)")

    def mark_retrieved(self) -> None:
        """Record that this item was retrieved.

        Increments view_count and updates last_retrieved_at timestamp.
        """
        self.view_count += 1
        self.last_retrieved_at = datetime.now(timezone.utc)
        self.touch()

    def mark_helpful(self) -> None:
        """Mark item as helpful by incrementing helpful_count."""
        self.helpful_count += 1
        self.touch()

    def mark_not_helpful(self) -> None:
        """Mark item as not helpful by incrementing not_helpful_count."""
        self.not_helpful_count += 1
        self.touch()

    def get_helpfulness_score(self) -> float:
        """Calculate helpfulness ratio (0.0 to 1.0).

        Returns:
            Ratio of helpful votes to total votes.
            Returns 0.5 (neutral) if no feedback has been received.
        """
        total_feedback = self.helpful_count + self.not_helpful_count
        if total_feedback == 0:
            return 0.5
        return self.helpful_count / total_feedback

    def get_verification_status(self) -> str:
        """Get verification status as a string for API responses.

        Returns:
            'verified' if admin_verified (level 2)
            'community' if community reviewed (level 1)
            'experimental' for unverified content (level 0)
        """
        if self.verification_level >= VerificationLevel.ADMIN_VERIFIED:
            return "verified"
        elif self.verification_level >= VerificationLevel.COMMUNITY:
            return "community"
        return "experimental"

    def set_verification(
        self,
        level: int,
        reason: str,
        verified_by: str,
    ) -> None:
        """Set verification status for this item.

        Args:
            level: Verification level (0=experimental, 1=community, 2=admin_verified)
            reason: Reason for this verification level
            verified_by: User ID who performed the verification

        Raises:
            ValueError: If level is not 0, 1, or 2
        """
        if level < 0 or level > 2:
            raise ValueError(f"verification_level must be 0, 1, or 2, got {level}")

        self.verification_level = level
        self.verification_reason = reason
        self.verified_by = verified_by
        self.verified_at = datetime.now(timezone.utc)
        self.touch()

    def is_verified(self) -> bool:
        """Check if item is admin-verified (gold standard).

        Returns:
            True if verification_level is ADMIN_VERIFIED (2).
        """
        return self.verification_level >= VerificationLevel.ADMIN_VERIFIED

    def has_embedding(self) -> bool:
        """Check if item has an embedding vector.

        Returns:
            True if embedding_vector is not None and has elements.
        """
        return self.embedding_vector is not None and len(self.embedding_vector) > 0

    def get_embedding_dimensions(self) -> int:
        """Get embedding vector dimensions.

        Returns:
            Number of dimensions in the embedding vector, or 0 if no embedding.
        """
        return len(self.embedding_vector) if self.embedding_vector else 0

    def needs_reindexing(self, current_version: int) -> bool:
        """Check if item needs re-indexing based on version.

        Args:
            current_version: The current expected embedding version.

        Returns:
            True if item's embedding_version is older than current_version
            or if item has no embedding.
        """
        if not self.has_embedding():
            return True
        return self.embedding_version < current_version

    def set_embedding(
        self,
        vector: List[float],
        model: str = "bge-m3",
        version: int = 1,
    ) -> None:
        """Set the embedding vector for this item.

        Args:
            vector: The embedding vector (must be EMBEDDING_DIMENSIONS dimensions).
            model: Name of the embedding model used.
            version: Version number for the embedding.

        Raises:
            ValueError: If vector has wrong dimensions.
        """
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding_vector must have {EMBEDDING_DIMENSIONS} dimensions, "
                f"got {len(vector)}"
            )
        self.embedding_vector = vector
        self.embedding_model = model
        self.embedding_version = version
        self.touch()

    def clear_embedding(self) -> None:
        """Clear the embedding vector (marks item for re-indexing)."""
        self.embedding_vector = None
        self.touch()

    def add_tag(self, tag: str) -> bool:
        """Add a tag to the item if not already present.

        Args:
            tag: Tag to add.

        Returns:
            True if tag was added, False if already present.
        """
        if tag not in self.tags:
            self.tags.append(tag)
            self.touch()
            return True
        return False

    def remove_tag(self, tag: str) -> bool:
        """Remove a tag from the item.

        Args:
            tag: Tag to remove.

        Returns:
            True if tag was removed, False if not present.
        """
        if tag in self.tags:
            self.tags.remove(tag)
            self.touch()
            return True
        return False

    def has_tag(self, tag: str) -> bool:
        """Check if item has a specific tag.

        Args:
            tag: Tag to check for.

        Returns:
            True if tag is present.
        """
        return tag in self.tags

    def has_any_tags(self, tags: List[str]) -> bool:
        """Check if item has any of the specified tags.

        Args:
            tags: List of tags to check for.

        Returns:
            True if item has at least one of the specified tags.
        """
        return any(tag in self.tags for tag in tags)

    def has_all_tags(self, tags: List[str]) -> bool:
        """Check if item has all of the specified tags.

        Args:
            tags: List of tags to check for.

        Returns:
            True if item has all of the specified tags.
        """
        return all(tag in self.tags for tag in tags)

    def publish(self) -> None:
        """Publish the item (make visible for retrieval)."""
        self.is_published = True
        self.touch()

    def unpublish(self) -> None:
        """Unpublish the item (hide from retrieval)."""
        self.is_published = False
        self.touch()

    def get_total_feedback(self) -> int:
        """Get total number of feedback responses.

        Returns:
            Sum of helpful_count and not_helpful_count.
        """
        return self.helpful_count + self.not_helpful_count

    def touch(self) -> None:
        """Update the updated_at timestamp to current time."""
        self.updated_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return (
            f"KnowledgeItem(item_id={self.item_id!r}, "
            f"title={self.title!r}, "
            f"item_type={self.item_type.value!r}, "
            f"is_published={self.is_published})"
        )

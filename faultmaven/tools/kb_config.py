"""
KB Configuration Strategy Interface

Defines the contract for KB-specific behavior.
Each knowledge base type provides its own implementation.

Design Principle: Adding new KB type = create new config, zero changes to DocumentQATool.
"""

from abc import ABC, abstractmethod
from typing import Optional


class KBConfig(ABC):
    """
    Abstract interface for KB-specific configuration.

    Each KB type (Case Evidence, User KB, Global KB, etc.) provides
    its own implementation defining:
    - Collection naming pattern
    - Metadata formatting
    - Citation style
    - System prompt
    - Cache TTL
    - Response formatting

    This enables DocumentQATool to remain KB-neutral.
    """

    @abstractmethod
    def get_collection_name(self, scope_id: Optional[str]) -> str:
        """
        Get ChromaDB collection name for this KB type.

        Args:
            scope_id: Scoping identifier (case_id, user_id, etc.) or None

        Returns:
            Collection name (e.g., "case_123", "user_456_kb", "global_kb")

        Raises:
            ValueError: If scope_id required but not provided
        """
        pass

    @abstractmethod
    def format_chunk_metadata(self, metadata: dict, score: float) -> str:
        """
        Format chunk metadata for context display.

        Args:
            metadata: Chunk metadata from vector store
            score: Similarity score

        Returns:
            Formatted metadata string (e.g., "Source: app.log, Line: 42, Score: 0.95")
        """
        pass

    @abstractmethod
    def extract_source_name(self, metadata: dict) -> str:
        """
        Extract source name from chunk metadata.

        Args:
            metadata: Chunk metadata from vector store

        Returns:
            Source name (e.g., "app.log", "Database Runbook", "KB-1234")
        """
        pass

    @abstractmethod
    def get_citation_format(self) -> str:
        """
        Get citation format guidance for synthesis prompt.

        Returns:
            Citation format description (e.g., "line numbers and timestamps")
        """
        pass

    @abstractmethod
    def format_response(
        self,
        answer: str,
        sources: list,
        chunk_count: int,
        confidence: float
    ) -> str:
        """
        Format final response for agent consumption.

        Args:
            answer: Synthesis LLM answer
            sources: List of source names
            chunk_count: Number of chunks used
            confidence: Average similarity score

        Returns:
            Formatted response string with citations
        """
        pass

    @property
    @abstractmethod
    def requires_scope_id(self) -> bool:
        """
        Does this KB type require a scope_id parameter?

        Returns:
            True if scope_id required (case_id, user_id, etc.)
            False if no scoping (global KB)
        """
        pass

    @property
    @abstractmethod
    def cache_ttl(self) -> int:
        """
        Cache duration in seconds for this KB type.

        Returns:
            TTL in seconds (e.g., 3600 for 1 hour, 86400 for 24 hours)
        """
        pass

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """
        Synthesis LLM system prompt for this KB type.

        Returns:
            System prompt string defining synthesis style
            (e.g., forensic, procedural, educational)
        """
        pass

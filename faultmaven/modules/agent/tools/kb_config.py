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
    - Relevance threshold (opt-in noise-floor refuse)

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
        self, answer: str, sources: list, chunk_count: int, confidence: float
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
    def search_mode(self) -> str:
        """Search strategy for this KB type.

        Returns:
            "vector" for pure vector similarity (default).
            "hybrid" for two-stage retrieval + reranking (vector + keyword
                recall, then multi-signal reranking).
        """
        return "vector"

    @property
    def relevance_threshold(self) -> Optional[float]:
        """Minimum top-chunk score required to invoke synthesis.

        Score scale is cosine similarity (1.0 - chroma_distance), range
        [-1, 1]. Off-topic queries land near 0 (orthogonal); on-topic
        queries score positively.

        Returns:
            Float in [0, 1] — if no chunk's score reaches this, the tool
                returns "no relevant content" without calling the synthesis
                LLM. Prevents grounding answers in off-topic chunks when the
                KB doesn't cover the queried topic.
            None — disable the check (always synthesize). Use for stores
                where returning the closest available content is always
                desirable (e.g. case evidence for forensic analysis).
        """
        return None

    @property
    @abstractmethod
    def empty_result_message(self) -> str:
        """Message for a search that ran successfully and matched nothing.

        Abstract on purpose. This text is the tool's claim about what the
        store holds, so each KB must state its own — the previous shared
        default emitted case-evidence causes ("no files have been uploaded to
        this case") for knowledge-base queries, naming a cause for a subsystem
        the query never touched (#943).

        It may only describe an EMPTY store. It must never explain a failure:
        a search that could not run raises ``KnowledgeBaseError`` and never
        reaches this path.
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

"""Vector backend interface for portable vector operations.

This module defines the interface for vector database backends that support
document storage, similarity search, and metadata filtering.

Design Goals:
- Backend neutrality: Same interface for Chroma and Pinecone
- Metadata portability: Consistent metadata handling across backends
- Automatic sanitization: All upserts go through VectorMetadataSanitizer

Usage:
    from faultmaven.infrastructure.vector import get_vector_backend

    backend = get_vector_backend()
    await backend.upsert(documents)
    results = await backend.search(query_vector, top_k=10)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union
import logging


logger = logging.getLogger(__name__)


class VectorBackendType(str, Enum):
    """Vector backend type."""

    CHROMA = "chroma"
    PINECONE = "pinecone"


@dataclass
class VectorDocument:
    """Document for vector storage.

    Attributes:
        id: Unique document identifier
        content: Document text content
        embedding: Optional pre-computed embedding vector
        metadata: Document metadata (will be sanitized)
    """

    id: str
    content: str
    embedding: Optional[List[float]] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class VectorSearchResult:
    """Search result from vector query.

    Attributes:
        id: Document identifier
        content: Document text content
        score: Similarity score (higher = more similar)
        metadata: Document metadata
    """

    id: str
    content: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorCollectionInfo:
    """Information about a vector collection.

    Attributes:
        name: Collection name
        count: Number of documents
        dimension: Embedding dimension
        metadata: Additional collection metadata
    """

    name: str
    count: int
    dimension: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class IVectorBackend(ABC):
    """Interface for vector database backends.

    All implementations must sanitize metadata before upserting to ensure
    cross-backend compatibility. Use VectorMetadataSanitizer for this.

    Implementations:
        - ChromaVectorBackend: ChromaDB with embedded collection management
        - PineconeVectorBackend: Pinecone with namespace support
    """

    @abstractmethod
    async def upsert(
        self,
        documents: List[VectorDocument],
        collection: Optional[str] = None,
    ) -> int:
        """Upsert documents into the vector store.

        Documents with existing IDs will be updated; new IDs will be inserted.
        Metadata is automatically sanitized before storage.

        Args:
            documents: List of documents to upsert
            collection: Optional collection/namespace name

        Returns:
            Number of documents upserted

        Example:
            docs = [
                VectorDocument(
                    id="doc1",
                    content="Error log content",
                    metadata={"type": "log", "severity": "error"},
                )
            ]
            count = await backend.upsert(docs)
        """
        pass

    @abstractmethod
    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 10,
        collection: Optional[str] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[VectorSearchResult]:
        """Search for similar documents by embedding.

        Args:
            query_embedding: Query vector
            top_k: Number of results to return
            collection: Optional collection/namespace name
            filter: Optional metadata filter

        Returns:
            List of search results ordered by similarity (descending)

        Example:
            results = await backend.search(
                query_embedding=embedding_vector,
                top_k=5,
                filter={"type": "log"},
            )
        """
        pass

    @abstractmethod
    async def search_by_text(
        self,
        query_text: str,
        top_k: int = 10,
        collection: Optional[str] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[VectorSearchResult]:
        """Search for similar documents by text query.

        Generates embedding from query text, then performs similarity search.

        Args:
            query_text: Text query
            top_k: Number of results to return
            collection: Optional collection/namespace name
            filter: Optional metadata filter

        Returns:
            List of search results ordered by similarity (descending)
        """
        pass

    @abstractmethod
    async def delete(
        self,
        ids: List[str],
        collection: Optional[str] = None,
    ) -> int:
        """Delete documents by ID.

        Args:
            ids: List of document IDs to delete
            collection: Optional collection/namespace name

        Returns:
            Number of documents deleted
        """
        pass

    @abstractmethod
    async def get(
        self,
        ids: List[str],
        collection: Optional[str] = None,
    ) -> List[VectorDocument]:
        """Get documents by ID.

        Args:
            ids: List of document IDs to retrieve
            collection: Optional collection/namespace name

        Returns:
            List of documents (empty list for missing IDs)
        """
        pass

    @abstractmethod
    async def count(
        self,
        collection: Optional[str] = None,
    ) -> int:
        """Count documents in collection.

        Args:
            collection: Optional collection/namespace name

        Returns:
            Number of documents
        """
        pass

    @abstractmethod
    async def create_collection(
        self,
        name: str,
        dimension: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Create a new collection.

        Args:
            name: Collection name
            dimension: Embedding dimension (required for some backends)
            metadata: Optional collection metadata

        Returns:
            True if created, False if already exists
        """
        pass

    @abstractmethod
    async def delete_collection(
        self,
        name: str,
    ) -> bool:
        """Delete a collection.

        Args:
            name: Collection name

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def list_collections(self) -> List[VectorCollectionInfo]:
        """List all collections.

        Returns:
            List of collection information
        """
        pass

    @abstractmethod
    def get_backend_type(self) -> VectorBackendType:
        """Get the backend type.

        Returns:
            VectorBackendType enum value
        """
        pass

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Check backend health.

        Returns:
            Health status dictionary
        """
        pass

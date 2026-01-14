"""Vector backends for FaultMaven.

This package provides portable vector database backends with automatic
metadata sanitization for cross-backend compatibility.

Vector Backend Selection:
    Set VECTOR_BACKEND environment variable:
    - chroma (default): ChromaDB for local or cloud deployment
    - pinecone: Pinecone for cloud-native vector search

Usage:
    from faultmaven.infrastructure.vector import get_vector_backend

    backend = get_vector_backend()

    # Upsert documents (metadata is auto-sanitized)
    await backend.upsert([
        VectorDocument(
            id="doc1",
            content="Document content",
            metadata={"type": "log", "severity": "error"},
        )
    ])

    # Search
    results = await backend.search(query_embedding, top_k=10)

Pinecone Configuration (when VECTOR_BACKEND=pinecone):
    - PINECONE_API_KEY: Required API key
    - PINECONE_INDEX: Index name (default: faultmaven)
    - PINECONE_ENVIRONMENT: Environment (default: us-east-1)
    - PINECONE_DIMENSION: Embedding dimension (default: 1536)

Metadata Sanitization:
    All metadata is automatically sanitized before upsert to ensure
    compatibility across backends:
    - None values are removed
    - Nested dicts are flattened with dot notation
    - Non-primitive types are converted to strings
    - String lengths are enforced
"""

from faultmaven.infrastructure.vector.base import (
    IVectorBackend,
    VectorBackendType,
    VectorCollectionInfo,
    VectorDocument,
    VectorSearchResult,
)
from faultmaven.infrastructure.vector.chroma import ChromaVectorBackend
from faultmaven.infrastructure.vector.factory import (
    get_vector_backend,
    reset_vector_backend,
)
from faultmaven.infrastructure.vector.sanitizer import (
    VectorMetadataSanitizer,
    create_chroma_sanitizer,
    create_pinecone_sanitizer,
    sanitize_metadata,
)

# Pinecone backend is optional (requires pinecone-client)
try:
    from faultmaven.infrastructure.vector.pinecone import PineconeVectorBackend

    PINECONE_AVAILABLE = True
except ImportError:
    PineconeVectorBackend = None  # type: ignore
    PINECONE_AVAILABLE = False

__all__ = [
    # Interface and types
    "IVectorBackend",
    "VectorBackendType",
    "VectorDocument",
    "VectorSearchResult",
    "VectorCollectionInfo",
    # Factory
    "get_vector_backend",
    "reset_vector_backend",
    # Sanitizer
    "VectorMetadataSanitizer",
    "sanitize_metadata",
    "create_chroma_sanitizer",
    "create_pinecone_sanitizer",
    # Implementations
    "ChromaVectorBackend",
    "PineconeVectorBackend",
    "PINECONE_AVAILABLE",
]

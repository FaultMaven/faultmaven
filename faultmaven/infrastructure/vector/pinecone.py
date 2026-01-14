"""Pinecone vector backend implementation.

Provides Pinecone vector storage with automatic metadata sanitization
and namespace support.

Usage:
    backend = PineconeVectorBackend(
        api_key="your-api-key",
        index_name="documents",
        environment="us-east-1",
    )
    await backend.upsert(documents)

Configuration:
    Required environment variables:
    - PINECONE_API_KEY: Pinecone API key
    - PINECONE_INDEX: Index name
    - PINECONE_ENVIRONMENT: Pinecone environment (e.g., "us-east-1")
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.vector.base import (
    IVectorBackend,
    VectorBackendType,
    VectorCollectionInfo,
    VectorDocument,
    VectorSearchResult,
)
from faultmaven.infrastructure.vector.sanitizer import create_pinecone_sanitizer


logger = logging.getLogger(__name__)


# Feature detection for pinecone
try:
    from pinecone import Pinecone, ServerlessSpec

    PINECONE_AVAILABLE = True
except ImportError:
    PINECONE_AVAILABLE = False
    Pinecone = None
    ServerlessSpec = None
    logger.debug("pinecone not installed - Pinecone backend unavailable")


class PineconeVectorBackend(IVectorBackend):
    """Pinecone vector backend implementation.

    Uses Pinecone for cloud-native vector storage with automatic metadata
    sanitization and namespace support.

    Namespaces are used as "collections" - each namespace is isolated
    within the same index.

    Attributes:
        index_name: Pinecone index name
        default_namespace: Default namespace (collection equivalent)
        dimension: Embedding dimension (required for Pinecone)
    """

    def __init__(
        self,
        api_key: str,
        index_name: str,
        environment: Optional[str] = None,
        dimension: int = 1536,
        default_namespace: str = "default",
        metric: str = "cosine",
    ):
        """Initialize Pinecone backend.

        Args:
            api_key: Pinecone API key
            index_name: Index name
            environment: Pinecone environment (e.g., "us-east-1")
            dimension: Embedding dimension (default: 1536 for OpenAI)
            default_namespace: Default namespace
            metric: Distance metric ("cosine", "euclidean", "dotproduct")

        Raises:
            ImportError: If pinecone is not installed
        """
        if not PINECONE_AVAILABLE:
            raise ImportError(
                "pinecone is required for Pinecone backend. "
                "Install with: pip install pinecone-client"
            )

        self.index_name = index_name
        self.dimension = dimension
        self.default_namespace = default_namespace
        self.metric = metric
        self._sanitizer = create_pinecone_sanitizer()

        # Initialize Pinecone client
        self._pc = Pinecone(api_key=api_key)

        # Get or create index
        if index_name not in [idx.name for idx in self._pc.list_indexes()]:
            logger.info(f"Creating Pinecone index: {index_name}")
            self._pc.create_index(
                name=index_name,
                dimension=dimension,
                metric=metric,
                spec=ServerlessSpec(
                    cloud="aws",
                    region=environment or "us-east-1",
                ),
            )

        self._index = self._pc.Index(index_name)
        logger.info(f"Pinecone backend connected to index: {index_name}")

    async def upsert(
        self,
        documents: List[VectorDocument],
        collection: Optional[str] = None,
    ) -> int:
        """Upsert documents with sanitized metadata.

        Args:
            documents: List of documents to upsert
            collection: Optional namespace (collection equivalent)

        Returns:
            Number of documents upserted
        """
        if not documents:
            return 0

        namespace = collection or self.default_namespace

        vectors = []
        for doc in documents:
            if not doc.embedding:
                logger.warning(f"Document {doc.id} has no embedding, skipping")
                continue

            # Sanitize metadata before upsert
            clean_metadata = self._sanitizer.sanitize(doc.metadata or {})
            # Add content to metadata for retrieval
            clean_metadata["_content"] = doc.content[:1000]  # Limit content length

            vectors.append(
                {
                    "id": doc.id,
                    "values": doc.embedding,
                    "metadata": clean_metadata,
                }
            )

        if not vectors:
            return 0

        # Upsert in batches (Pinecone limit: 100 vectors per request)
        batch_size = 100
        total_upserted = 0
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i : i + batch_size]
            self._index.upsert(vectors=batch, namespace=namespace)
            total_upserted += len(batch)

        logger.debug(f"Upserted {total_upserted} documents to namespace {namespace}")
        return total_upserted

    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 10,
        collection: Optional[str] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[VectorSearchResult]:
        """Search by embedding vector.

        Args:
            query_embedding: Query vector
            top_k: Number of results
            collection: Optional namespace
            filter: Optional metadata filter

        Returns:
            List of search results
        """
        namespace = collection or self.default_namespace

        query_kwargs = {
            "vector": query_embedding,
            "top_k": top_k,
            "namespace": namespace,
            "include_metadata": True,
        }

        if filter:
            # Sanitize filter values
            clean_filter = self._sanitizer.sanitize(filter)
            if clean_filter:
                query_kwargs["filter"] = clean_filter

        results = self._index.query(**query_kwargs)

        search_results = []
        for match in results.get("matches", []):
            metadata = match.get("metadata", {})
            content = metadata.pop("_content", "")

            search_results.append(
                VectorSearchResult(
                    id=match["id"],
                    content=content,
                    score=match.get("score", 0.0),
                    metadata=metadata,
                )
            )

        return search_results

    async def search_by_text(
        self,
        query_text: str,
        top_k: int = 10,
        collection: Optional[str] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[VectorSearchResult]:
        """Search by text query.

        Note: Pinecone requires pre-computed embeddings. This method
        requires an external embedding function to be configured.

        Args:
            query_text: Text query
            top_k: Number of results
            collection: Optional namespace
            filter: Optional metadata filter

        Returns:
            List of search results

        Raises:
            NotImplementedError: If no embedding function is configured
        """
        raise NotImplementedError(
            "PineconeVectorBackend.search_by_text requires external embedding generation. "
            "Use search() with a pre-computed query_embedding instead."
        )

    async def delete(
        self,
        ids: List[str],
        collection: Optional[str] = None,
    ) -> int:
        """Delete documents by ID.

        Args:
            ids: List of document IDs
            collection: Optional namespace

        Returns:
            Number of documents deleted
        """
        if not ids:
            return 0

        namespace = collection or self.default_namespace
        self._index.delete(ids=ids, namespace=namespace)

        logger.debug(f"Deleted {len(ids)} documents from namespace {namespace}")
        return len(ids)

    async def get(
        self,
        ids: List[str],
        collection: Optional[str] = None,
    ) -> List[VectorDocument]:
        """Get documents by ID.

        Args:
            ids: List of document IDs
            collection: Optional namespace

        Returns:
            List of documents
        """
        if not ids:
            return []

        namespace = collection or self.default_namespace
        results = self._index.fetch(ids=ids, namespace=namespace)

        documents = []
        for doc_id, data in results.get("vectors", {}).items():
            metadata = data.get("metadata", {})
            content = metadata.pop("_content", "")

            documents.append(
                VectorDocument(
                    id=doc_id,
                    content=content,
                    embedding=data.get("values"),
                    metadata=metadata,
                )
            )

        return documents

    async def count(
        self,
        collection: Optional[str] = None,
    ) -> int:
        """Count documents in namespace.

        Args:
            collection: Optional namespace

        Returns:
            Number of documents
        """
        namespace = collection or self.default_namespace
        stats = self._index.describe_index_stats()

        namespaces = stats.get("namespaces", {})
        if namespace in namespaces:
            return namespaces[namespace].get("vector_count", 0)

        return 0

    async def create_collection(
        self,
        name: str,
        dimension: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Create a new namespace (Pinecone uses namespaces as collections).

        Note: Pinecone namespaces are created automatically on first upsert.
        This method is a no-op but returns True for API compatibility.

        Args:
            name: Namespace name
            dimension: Embedding dimension (ignored - uses index dimension)
            metadata: Optional metadata (ignored - Pinecone doesn't support namespace metadata)

        Returns:
            True (namespaces are auto-created)
        """
        logger.debug(f"Namespace {name} will be created on first upsert")
        return True

    async def delete_collection(
        self,
        name: str,
    ) -> bool:
        """Delete a namespace.

        Args:
            name: Namespace name

        Returns:
            True if deleted
        """
        try:
            self._index.delete(delete_all=True, namespace=name)
            logger.info(f"Deleted namespace: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete namespace {name}: {e}")
            return False

    async def list_collections(self) -> List[VectorCollectionInfo]:
        """List all namespaces.

        Returns:
            List of namespace information
        """
        stats = self._index.describe_index_stats()

        return [
            VectorCollectionInfo(
                name=name,
                count=info.get("vector_count", 0),
                dimension=self.dimension,
            )
            for name, info in stats.get("namespaces", {}).items()
        ]

    def get_backend_type(self) -> VectorBackendType:
        """Get the backend type.

        Returns:
            VectorBackendType.PINECONE
        """
        return VectorBackendType.PINECONE

    async def health_check(self) -> Dict[str, Any]:
        """Check Pinecone health.

        Returns:
            Health status dictionary
        """
        try:
            stats = self._index.describe_index_stats()
            return {
                "status": "healthy",
                "backend": "pinecone",
                "index_name": self.index_name,
                "dimension": self.dimension,
                "total_vectors": stats.get("total_vector_count", 0),
                "namespaces_count": len(stats.get("namespaces", {})),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "pinecone",
                "error": str(e),
            }

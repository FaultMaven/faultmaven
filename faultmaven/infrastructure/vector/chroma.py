"""ChromaDB vector backend implementation.

Provides ChromaDB vector storage with automatic metadata sanitization
and embedding generation.

Usage:
    backend = ChromaVectorBackend(
        persist_directory="./data/chroma",
        collection_name="documents",
    )
    await backend.upsert(documents)
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
from faultmaven.infrastructure.vector.sanitizer import create_chroma_sanitizer

logger = logging.getLogger(__name__)


# Feature detection for chromadb
try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    chromadb = None
    ChromaSettings = None
    logger.debug("chromadb not installed - Chroma backend unavailable")


class ChromaVectorBackend(IVectorBackend):
    """ChromaDB vector backend implementation.

    Uses ChromaDB for local or hosted vector storage with automatic
    metadata sanitization and optional embedding generation.

    Attributes:
        persist_directory: Directory for persistent storage (None for ephemeral)
        default_collection: Default collection name
        embedding_function: Optional embedding function for text-to-vector
    """

    def __init__(
        self,
        persist_directory: Optional[str] = None,
        default_collection: str = "documents",
        host: Optional[str] = None,
        port: Optional[int] = None,
        embedding_function: Optional[Any] = None,
    ):
        """Initialize ChromaDB backend.

        Args:
            persist_directory: Directory for persistent storage
            default_collection: Default collection name
            host: ChromaDB server host (for client mode)
            port: ChromaDB server port (for client mode)
            embedding_function: Optional chromadb embedding function

        Raises:
            ImportError: If chromadb is not installed
        """
        if not CHROMADB_AVAILABLE:
            raise ImportError(
                "chromadb is required for Chroma backend. "
                "Install with: pip install chromadb"
            )

        self.persist_directory = persist_directory
        self.default_collection = default_collection
        self.embedding_function = embedding_function
        self._sanitizer = create_chroma_sanitizer()

        # Initialize client
        if host and port:
            # HTTP client mode
            self._client = chromadb.HttpClient(host=host, port=port)
            logger.info(f"ChromaDB HTTP client connected to {host}:{port}")
        elif persist_directory:
            # Persistent local mode
            settings = ChromaSettings(
                chroma_db_impl="duckdb+parquet",
                persist_directory=persist_directory,
                anonymized_telemetry=False,
            )
            self._client = chromadb.Client(settings)
            logger.info(f"ChromaDB persistent client at {persist_directory}")
        else:
            # Ephemeral mode
            self._client = chromadb.Client()
            logger.info("ChromaDB ephemeral client initialized")

        # Collection cache
        self._collections: Dict[str, Any] = {}

    def _get_collection(self, name: Optional[str] = None) -> Any:
        """Get or create a ChromaDB collection.

        Args:
            name: Collection name (uses default if not specified)

        Returns:
            ChromaDB collection
        """
        collection_name = name or self.default_collection

        if collection_name not in self._collections:
            kwargs = {"name": collection_name}
            if self.embedding_function:
                kwargs["embedding_function"] = self.embedding_function

            self._collections[collection_name] = self._client.get_or_create_collection(
                **kwargs
            )

        return self._collections[collection_name]

    async def upsert(
        self,
        documents: List[VectorDocument],
        collection: Optional[str] = None,
    ) -> int:
        """Upsert documents with sanitized metadata.

        Args:
            documents: List of documents to upsert
            collection: Optional collection name

        Returns:
            Number of documents upserted
        """
        if not documents:
            return 0

        coll = self._get_collection(collection)

        ids = []
        contents = []
        embeddings = []
        metadatas = []

        for doc in documents:
            ids.append(doc.id)
            contents.append(doc.content)

            # Sanitize metadata before upsert
            clean_metadata = self._sanitizer.sanitize(doc.metadata)
            metadatas.append(clean_metadata)

            if doc.embedding:
                embeddings.append(doc.embedding)

        # Upsert with or without embeddings
        if embeddings and len(embeddings) == len(documents):
            coll.upsert(
                ids=ids,
                documents=contents,
                embeddings=embeddings,
                metadatas=metadatas,
            )
        else:
            coll.upsert(
                ids=ids,
                documents=contents,
                metadatas=metadatas,
            )

        logger.debug(f"Upserted {len(documents)} documents to {coll.name}")
        return len(documents)

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
            collection: Optional collection name
            filter: Optional metadata filter

        Returns:
            List of search results
        """
        coll = self._get_collection(collection)

        query_kwargs = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
        }

        if filter:
            # Sanitize filter values
            clean_filter = self._sanitizer.sanitize(filter)
            if clean_filter:
                query_kwargs["where"] = clean_filter

        results = coll.query(**query_kwargs)

        search_results = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                # Convert distance to similarity score (1 - distance for cosine)
                distance = results["distances"][0][i] if results.get("distances") else 0
                score = 1.0 - distance  # Assuming cosine distance

                search_results.append(
                    VectorSearchResult(
                        id=doc_id,
                        content=(
                            results["documents"][0][i]
                            if results.get("documents")
                            else ""
                        ),
                        score=score,
                        metadata=(
                            results["metadatas"][0][i]
                            if results.get("metadatas")
                            else {}
                        ),
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

        Args:
            query_text: Text query
            top_k: Number of results
            collection: Optional collection name
            filter: Optional metadata filter

        Returns:
            List of search results
        """
        coll = self._get_collection(collection)

        query_kwargs = {
            "query_texts": [query_text],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
        }

        if filter:
            clean_filter = self._sanitizer.sanitize(filter)
            if clean_filter:
                query_kwargs["where"] = clean_filter

        results = coll.query(**query_kwargs)

        search_results = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results.get("distances") else 0
                score = 1.0 - distance

                search_results.append(
                    VectorSearchResult(
                        id=doc_id,
                        content=(
                            results["documents"][0][i]
                            if results.get("documents")
                            else ""
                        ),
                        score=score,
                        metadata=(
                            results["metadatas"][0][i]
                            if results.get("metadatas")
                            else {}
                        ),
                    )
                )

        return search_results

    async def delete(
        self,
        ids: List[str],
        collection: Optional[str] = None,
    ) -> int:
        """Delete documents by ID.

        Args:
            ids: List of document IDs
            collection: Optional collection name

        Returns:
            Number of documents deleted
        """
        if not ids:
            return 0

        coll = self._get_collection(collection)
        coll.delete(ids=ids)

        logger.debug(f"Deleted {len(ids)} documents from {coll.name}")
        return len(ids)

    async def get(
        self,
        ids: List[str],
        collection: Optional[str] = None,
    ) -> List[VectorDocument]:
        """Get documents by ID.

        Args:
            ids: List of document IDs
            collection: Optional collection name

        Returns:
            List of documents
        """
        if not ids:
            return []

        coll = self._get_collection(collection)
        results = coll.get(
            ids=ids,
            include=["documents", "metadatas", "embeddings"],
        )

        documents = []
        if results and results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                documents.append(
                    VectorDocument(
                        id=doc_id,
                        content=(
                            results["documents"][i] if results.get("documents") else ""
                        ),
                        embedding=(
                            results["embeddings"][i]
                            if results.get("embeddings")
                            else None
                        ),
                        metadata=(
                            results["metadatas"][i]
                            if results.get("metadatas")
                            else None
                        ),
                    )
                )

        return documents

    async def count(
        self,
        collection: Optional[str] = None,
    ) -> int:
        """Count documents in collection.

        Args:
            collection: Optional collection name

        Returns:
            Number of documents
        """
        coll = self._get_collection(collection)
        return coll.count()

    async def create_collection(
        self,
        name: str,
        dimension: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Create a new collection.

        Args:
            name: Collection name
            dimension: Embedding dimension (not used by ChromaDB)
            metadata: Optional collection metadata

        Returns:
            True if created, False if already exists
        """
        try:
            # Check if exists
            try:
                self._client.get_collection(name)
                return False  # Already exists
            except Exception:
                pass  # Doesn't exist, create it

            kwargs = {"name": name}
            if self.embedding_function:
                kwargs["embedding_function"] = self.embedding_function
            if metadata:
                kwargs["metadata"] = self._sanitizer.sanitize(metadata)

            coll = self._client.create_collection(**kwargs)
            self._collections[name] = coll
            logger.info(f"Created collection: {name}")
            return True

        except Exception as e:
            logger.error(f"Failed to create collection {name}: {e}")
            return False

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
        try:
            self._client.delete_collection(name)
            self._collections.pop(name, None)
            logger.info(f"Deleted collection: {name}")
            return True
        except Exception:
            return False

    async def list_collections(self) -> List[VectorCollectionInfo]:
        """List all collections.

        Returns:
            List of collection information
        """
        collections = self._client.list_collections()

        return [
            VectorCollectionInfo(
                name=coll.name,
                count=coll.count(),
                metadata=coll.metadata,
            )
            for coll in collections
        ]

    def get_backend_type(self) -> VectorBackendType:
        """Get the backend type.

        Returns:
            VectorBackendType.CHROMA
        """
        return VectorBackendType.CHROMA

    async def health_check(self) -> Dict[str, Any]:
        """Check ChromaDB health.

        Returns:
            Health status dictionary
        """
        try:
            # Try to list collections as a health check
            collections = self._client.list_collections()
            return {
                "status": "healthy",
                "backend": "chroma",
                "collections_count": len(collections),
                "persist_directory": self.persist_directory,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "chroma",
                "error": str(e),
            }

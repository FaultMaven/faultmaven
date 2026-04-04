"""
ChromaDB implementation of IVectorStore interface.

Global KB vector store — operates on a single named collection.
Receives a shared ChromaDB client via constructor injection (Principle 5).
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.models.interfaces import IVectorStore

logger = logging.getLogger(__name__)


class ChromaDBVectorStore(BaseExternalClient, IVectorStore):
    """ChromaDB implementation of the IVectorStore interface.

    Operates on a single named collection (global KB).
    The ChromaDB client is injected — works with both PersistentClient (local)
    and HttpClient (cloud) transparently.
    """

    def __init__(self, client, collection_name: str = "faultmaven_kb"):
        """Initialize ChromaDB vector store.

        Args:
            client: ChromaDB client instance (PersistentClient or HttpClient).
            collection_name: Collection name for global KB.
        """
        super().__init__(
            client_name="chromadb_vector_store",
            service_name="ChromaDB",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=60,
        )

        self.client = client
        self.collection_name = collection_name

        try:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "FaultMaven knowledge base"},
            )
            self.logger.info(
                f"ChromaDB collection ready",
                extra={"collection": self.collection_name},
            )
        except Exception as e:
            self.logger.error(f"Failed to connect to ChromaDB: {e}")
            raise

    async def add_documents(
        self,
        documents: List[Dict],
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        """Add documents to the vector store."""

        async def _add_wrapper():
            ids = [doc["id"] for doc in documents]
            contents = [doc["content"] for doc in documents]
            raw_metadatas = [doc.get("metadata", {}) for doc in documents]

            # Normalize metadata via canonical schema
            from faultmaven.models.vector_metadata import VectorMetadata

            metadatas: List[Dict] = []
            for md in raw_metadatas:
                try:
                    vm = VectorMetadata(
                        **{
                            k: md.get(k)
                            for k in VectorMetadata.model_fields
                            if k in md or md.get(k) is not None
                        }
                    )
                    metadatas.append(vm.to_chroma_metadata())
                except Exception:
                    sanitized: Dict = {}
                    for k, v in (md or {}).items():
                        if v is None:
                            continue
                        if isinstance(v, (str, int, float, bool)):
                            sanitized[k] = v
                        else:
                            try:
                                sanitized[k] = str(v)
                            except Exception:
                                continue
                    metadatas.append(sanitized)

            add_kwargs: Dict = dict(ids=ids, documents=contents, metadatas=metadatas)
            if embeddings is not None:
                add_kwargs["embeddings"] = embeddings
            self.collection.add(**add_kwargs)

            self.logger.info(
                f"Added documents to vector store",
                extra={"count": len(documents), "collection": self.collection_name},
            )

        await self.call_external(
            operation_name="add_documents",
            call_func=_add_wrapper,
            timeout=30.0,
            retries=2,
            retry_delay=2.0,
        )

    async def search(
        self, query: str, k: int = 5, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict]:
        """Search for similar documents using explicit BGE-M3 embedding."""

        async def _search_wrapper():
            from faultmaven.infrastructure.model_cache import model_cache

            bge_model = model_cache.get_bge_m3_model()
            if bge_model is None:
                self.logger.error("BGE-M3 model unavailable for search")
                return []

            query_embedding = bge_model.encode(query).tolist()

            query_params = {
                "query_embeddings": [query_embedding],
                "n_results": k,
                "include": ["documents", "metadatas", "distances"],
            }
            if filters is not None:
                query_params["where"] = filters

            results = self.collection.query(**query_params)

            formatted_results = []
            for i in range(len(results["ids"][0])):
                formatted_results.append(
                    {
                        "id": results["ids"][0][i],
                        "content": results["documents"][0][i],
                        "metadata": results["metadatas"][0][i],
                        "score": 1.0 - results["distances"][0][i],
                    }
                )

            self.logger.debug(f"Found {len(formatted_results)} similar documents")
            return formatted_results

        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )

    async def query_by_embedding(
        self,
        query_embedding: List[float],
        where: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """Query vector store using pre-computed embedding."""

        async def _query_wrapper():
            query_params = {
                "query_embeddings": [query_embedding],
                "n_results": top_k,
                "include": ["documents", "metadatas", "distances"],
            }

            if where:
                query_params["where"] = where

            results = self.collection.query(**query_params)

            self.logger.debug(
                f"Embedding query returned {len(results.get('ids', [[]])[0])} results",
                extra={"top_k": top_k, "has_filters": where is not None},
            )

            return results

        return await self.call_external(
            operation_name="query_by_embedding",
            call_func=_query_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )

    async def count(self) -> int:
        """Return the number of documents in the collection."""

        async def _count_wrapper():
            return self.collection.count()

        return await self.call_external(
            operation_name="count",
            call_func=_count_wrapper,
            timeout=5.0,
            retries=1,
            retry_delay=1.0,
        )

    async def delete_documents(self, ids: List[str]) -> None:
        """Delete documents by IDs."""

        async def _delete_wrapper():
            self.collection.delete(ids=ids)
            self.logger.info(f"Deleted {len(ids)} documents from vector store")

        await self.call_external(
            operation_name="delete_documents",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )

    async def delete_documents_by_parent_id(self, parent_document_id: str) -> int:
        """Delete all chunks belonging to a parent document."""
        deleted_count = 0

        async def _delete_by_parent_wrapper():
            nonlocal deleted_count
            results = self.collection.get(
                where={"parent_document_id": parent_document_id},
                include=[],
            )
            chunk_ids = results.get("ids", [])
            if chunk_ids:
                self.collection.delete(ids=chunk_ids)
                deleted_count = len(chunk_ids)
                self.logger.info(
                    f"Deleted {deleted_count} chunks for parent {parent_document_id}"
                )

        await self.call_external(
            operation_name="delete_documents_by_parent_id",
            call_func=_delete_by_parent_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )
        return deleted_count

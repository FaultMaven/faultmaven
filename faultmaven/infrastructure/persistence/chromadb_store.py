"""
A read-only ChromaDB store over one named collection.

Registered as ``container.vector_store`` over the KB collection, where it is
the reader behind runbook dedup (``RunbookKnowledgeBase``) and the vector-store
health check. It writes nothing: the only writer of the KB collection is
``KnowledgeVectorStore``, which refuses a chunk with no tenant stamp (#1168,
#1782). Receives a shared ChromaDB client via constructor injection
(Principle 5).
"""

import logging
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.chroma_client import blocking_executor_for
from faultmaven.infrastructure.embedding_guard import embed_query_or_raise
from faultmaven.infrastructure.vector_similarity import (
    cosine_from_chroma_distance,
)

logger = logging.getLogger(__name__)


def create_persistent_client(path: str) -> Any:
    """Open the process-wide ChromaDB client for a local persist path.

    Its one caller is the container's ``_create_chromadb_client``
    (``container/providers/infrastructure.py``), which opens the KB tree and
    the evidence tree through it whenever the store is local.

    chromadb caches one System per path and refuses a second client for that
    path whose ``Settings`` differ *in any field* ("An instance of Chroma
    already exists for <path> with different settings"). So every field is
    pinned rather than inherited, and the client is identified by its path and
    nothing else: a second open of the same path in the same process shares
    the cached System instead of crashing far from its cause (#823). Widening
    a field here is silent — it grants a capability to every client of every
    path at once.
    """
    import os

    import chromadb
    from chromadb.config import Settings as ChromaSettings

    os.makedirs(path, exist_ok=True)
    return chromadb.PersistentClient(
        path=path,
        settings=ChromaSettings(
            anonymized_telemetry=False,
            # No caller resets a collection — wiping the KB is a deliberate
            # operator act, not an API the app should hold open.
            allow_reset=False,
            # chromadb's Settings picks ``environment`` up from the ambient
            # ENVIRONMENT variable — the same one that names OUR deployment
            # environment — and it counts towards client identity. Left to the
            # env, a process that reads ENVIRONMENT differently between two
            # clients for one path gets the refusal above. Pinned, the client
            # is identified by its path and nothing else.
            environment="",
            # chromadb 1.5.8's own default for a local client, pinned for the
            # same reason. ``Settings`` reads ``CHROMA_API_IMPL`` (and the
            # server host and port) from the environment and from ``.env``,
            # so left to them, this "persistent" client could be an HTTP
            # client built outside the one door, with no request timeout
            # and no credential (#2056).
            chroma_api_impl="chromadb.api.rust.RustBindingsAPI",
            # The server host and port, pinned empty for the same reason: read
            # from CHROMA_SERVER_HOST / CHROMA_SERVER_HTTP_PORT or ``.env``,
            # they leave a local client claiming a server, and
            # ``is_server_backed`` (the startup refusal of several processes
            # over a local store, GET /admin/config/status) reads the host to
            # tell the two apart (#2269).
            chroma_server_host=None,
            chroma_server_http_port=None,
        ),
    )


class ChromaDBVectorStore(BaseExternalClient):
    """Read-only ChromaDB store over a single named collection (global KB).

    It searches, queries by embedding and counts; it has no write method. The
    ChromaDB client is injected — works with both PersistentClient (local) and
    HttpClient (cloud) transparently.
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
        # On the HttpClient arm every ChromaDB call runs in the chroma-http
        # executor, so a slow server never holds the event loop (#2056). A local
        # PersistentClient's calls stay on the loop thread as before, local work
        # outside #2056's scope. An HTTP client built outside the door is
        # refused here.
        self.blocking_executor = blocking_executor_for(client)

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

    async def search(
        self, query: str, k: int = 5, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict]:
        """Search for similar documents using explicit BGE-M3 embedding.

        Raises:
            KnowledgeBaseError: If the embedding model is unavailable. NOT an
                empty result — a caller that reads ``[]`` as "the knowledge
                base holds nothing" would be reporting a failure to search as a
                finding about the KB's contents (#941).
        """
        # Embedded BEFORE call_external: a local model call is not the ChromaDB
        # round-trip the retry/circuit-breaker policy exists for.
        query_embedding = await embed_query_or_raise(
            query,
            subject="Knowledge base search",
            operation="search",
            log=self.logger,
        )

        def _search_wrapper():
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
                        "score": cosine_from_chroma_distance(
                            results["distances"][0][i]
                        ),
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

        def _query_wrapper():
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

        def _count_wrapper():
            return self.collection.count()

        return await self.call_external(
            operation_name="count",
            call_func=_count_wrapper,
            timeout=5.0,
            retries=1,
            retry_delay=1.0,
        )

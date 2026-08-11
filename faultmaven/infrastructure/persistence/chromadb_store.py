"""
ChromaDB implementation of IVectorStore interface.

Global KB vector store — operates on a single named collection.
Receives a shared ChromaDB client via constructor injection (Principle 5).
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.embedding_guard import embed_query_or_raise
from faultmaven.models.interfaces import IVectorStore

logger = logging.getLogger(__name__)


def create_persistent_client(path: str) -> Any:
    """Open the process-wide ChromaDB client for a local persist path.

    chromadb caches one System per path and refuses a second client for that
    path whose ``Settings`` differ *in any field* ("An instance of Chroma
    already exists for <path> with different settings"). The container's KB
    client and ``KnowledgeIngester`` both open ``data/chroma-kb``, so every
    persistent client in the process is built here, with one set of settings —
    two spellings of the same path is a startup crash, and was a cross-test
    failure that surfaced hundreds of lines away from its cause (#823).

    That constraint pulls towards granting whatever any call site asked for.
    Do the opposite: reconciling call sites takes the NARROWER capability, and
    every field is pinned rather than inherited, so the client is identified by
    its path and nothing else. Widening here is silent — it grants a capability
    to every caller of every path at once.
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
            # operator act, not an API the app should hold open. The ingester
            # asked for allow_reset=True and the container's KB client did not;
            # taking the ingester's value would have handed the KB client a
            # destructive capability it never had, for nobody's benefit.
            allow_reset=False,
            # chromadb's Settings picks ``environment`` up from the ambient
            # ENVIRONMENT variable — the same one that names OUR deployment
            # environment — and it counts towards client identity. Left to the
            # env, a process that reads ENVIRONMENT differently between two
            # clients for one path gets the refusal above. Pinned, the client
            # is identified by its path and nothing else.
            environment="",
        ),
    )


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

    @staticmethod
    def _normalize_metadata(md: Optional[Dict]) -> Dict:
        """Put one metadata dict through the canonical ``VectorMetadata`` schema.

        ``VectorMetadata`` is an ALLOWLIST: it declares the keys a row in this
        collection may carry, and anything else it simply does not copy. That
        used to happen silently, which is how a writer could build a dict, watch
        the write succeed, and store none of it — ``index_runbook`` stamped
        ``case_id``/``case_title``/``runbook_source`` on every runbook and
        ChromaDB received none of them, so ``search_runbooks`` reconstructed
        each one from ``.get()`` defaults (#912). A silently dropped key is
        indistinguishable from a stored one at the call site: the write returns
        success either way, and the loss only surfaces as a wrong value at read
        time, arbitrarily later and somewhere else.

        So an undeclared key is refused rather than dropped. It is always a
        programming error — the writer believes the store holds something it
        does not — and the fix is a one-line schema addition, so failing at the
        first write turns a silent data-loss bug into an immediate, local one.

        The check runs BEFORE the model is constructed, so the fallback below
        can only ever see allowlisted KEYS. Note what that does and does not
        buy: the two paths agree on which keys a row may carry, not on how the
        values are encoded. The fallback stringifies rather than normalizing, so
        a dict that fails validation still stores ``tags`` as ``"['a', 'b']"``
        instead of ``"a,b"`` and a ``datetime`` in Python's default format
        instead of ISO-8601 — and ``search_runbooks`` splits ``tags`` on commas.
        Pre-existing, and out of scope here; do not read this guard as making
        the two paths interchangeable.
        """
        from faultmaven.models.vector_metadata import VectorMetadata

        md = md or {}
        # The schema owns this rule, so writers that must check earlier than
        # this (``index_runbook`` has its own retry wrapper around the call
        # into here) enforce the same one rather than a second copy of it.
        VectorMetadata.reject_undeclared_keys(md)

        try:
            return VectorMetadata(**md).to_chroma_metadata()
        except ValidationError:
            # A VALUE the schema could not coerce (a malformed timestamp, say),
            # never an unknown key — those were refused above. Degrading to a
            # type-sanitized copy keeps one bad field from failing the whole
            # write; the keys are allowlisted either way.
            sanitized: Dict = {}
            for k, v in md.items():
                if v is None:
                    continue
                if isinstance(v, (str, int, float, bool)):
                    sanitized[k] = v
                else:
                    try:
                        sanitized[k] = str(v)
                    except Exception:
                        continue
            return sanitized

    async def add_documents(
        self,
        documents: List[Dict],
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        """Add documents to the vector store.

        Raises:
            ValueError: If any metadata dict carries a key ``VectorMetadata``
                does not declare. See :meth:`_normalize_metadata`.
        """
        ids = [doc["id"] for doc in documents]
        contents = [doc["content"] for doc in documents]

        # Normalized OUTSIDE call_external, deliberately. This is pure CPU with
        # no external dependency, and the refusal below is a programming error,
        # not a ChromaDB failure: raising it inside the wrapper would burn the
        # retry budget on a deterministic failure and, worse, count towards the
        # circuit breaker — five bad writes would then open the breaker and
        # block the *healthy* KB writes sharing this client.
        metadatas = [
            self._normalize_metadata(doc.get("metadata", {})) for doc in documents
        ]

        async def _add_wrapper():
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

        async def _search_wrapper():
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

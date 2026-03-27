"""
Case-Specific Vector Store for Session Working Memory

Creates ChromaDB collections per case_id for user-uploaded documents
and extracted evidence. Collections are lifecycle-managed — created on
demand, deleted when the case closes.

Receives a shared ChromaDB client via constructor injection (Principle 5).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient

logger = logging.getLogger(__name__)


class CaseVectorStore(BaseExternalClient):
    """Case-specific vector store for Session-Specific RAG.

    Creates ChromaDB collections per case_id with lifecycle-based cleanup.
    The ChromaDB client is injected — works with both PersistentClient (local)
    and HttpClient (cloud) transparently.
    """

    COLLECTION_PREFIX = "case_"

    def __init__(self, client):
        """Initialize case vector store.

        Args:
            client: ChromaDB client instance (PersistentClient or HttpClient).
        """
        super().__init__(
            client_name="case_vector_store",
            service_name="CaseVectorStore",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=60,
        )

        self.client = client
        self.logger.info("CaseVectorStore initialized (lifecycle-based cleanup)")

    def _get_collection_name(self, case_id: str) -> str:
        """Get collection name for a case."""
        return f"{self.COLLECTION_PREFIX}{case_id}"

    def _get_or_create_collection(self, case_id: str):
        """Get or create ChromaDB collection for a case."""
        collection_name = self._get_collection_name(case_id)

        metadata = {
            "case_id": case_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            collection = self.client.get_or_create_collection(
                name=collection_name, metadata=metadata
            )
            self.logger.debug(f"Collection ready: {collection_name}")
            return collection
        except Exception as e:
            self.logger.error(f"Failed to get/create collection {collection_name}: {e}")
            raise

    async def add_documents(
        self, case_id: str, documents: List[Dict[str, Any]]
    ) -> None:
        """Add documents to case-specific collection."""

        async def _add_wrapper():
            collection = self._get_or_create_collection(case_id)

            ids = [doc["id"] for doc in documents]
            contents = [doc["content"] for doc in documents]
            metadatas = [doc.get("metadata", {}) for doc in documents]

            # Sanitize metadata (ChromaDB requires simple types)
            sanitized_metadatas = []
            for md in metadatas:
                sanitized = {}
                for k, v in md.items():
                    if v is None:
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        sanitized[k] = v
                    else:
                        sanitized[k] = str(v)
                sanitized_metadatas.append(sanitized)

            collection.add(ids=ids, documents=contents, metadatas=sanitized_metadatas)

            self.logger.info(
                f"Added {len(documents)} documents to case {case_id}",
                extra={"case_id": case_id, "doc_count": len(documents)},
            )

        await self.call_external(
            operation_name="add_documents",
            call_func=_add_wrapper,
            timeout=30.0,
            retries=2,
            retry_delay=2.0,
        )

    async def search(
        self,
        case_id: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar documents in case-specific collection."""

        async def _search_wrapper():
            collection = self._get_or_create_collection(case_id)

            query_params = {
                "query_texts": [query],
                "n_results": k,
                "include": ["documents", "metadatas", "distances"],
            }

            if where:
                query_params["where"] = where

            results = collection.query(**query_params)

            formatted_results = []
            if results["ids"] and results["ids"][0]:
                for i in range(len(results["ids"][0])):
                    formatted_results.append(
                        {
                            "id": results["ids"][0][i],
                            "content": results["documents"][0][i],
                            "metadata": (
                                results["metadatas"][0][i]
                                if results["metadatas"][0]
                                else {}
                            ),
                            "score": 1.0 - results["distances"][0][i],
                        }
                    )

            self.logger.debug(
                f"Case {case_id} search returned {len(formatted_results)} results",
                extra={
                    "case_id": case_id,
                    "query_len": len(query),
                    "results": len(formatted_results),
                },
            )

            return formatted_results

        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0,
        )

    async def delete_case_collection(self, case_id: str) -> None:
        """Delete entire case collection (called when case closes/archives)."""

        async def _delete_wrapper():
            collection_name = self._get_collection_name(case_id)

            try:
                self.client.delete_collection(name=collection_name)
                self.logger.info(
                    f"Deleted case collection: {collection_name}",
                    extra={"case_id": case_id, "collection": collection_name},
                )
            except Exception as e:
                self.logger.debug(
                    f"Collection {collection_name} not found or already deleted: {e}"
                )

        await self.call_external(
            operation_name="delete_case_collection",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0,
        )

    async def cleanup_orphaned_collections(self, active_case_ids: List[str]) -> int:
        """Clean up case collections without corresponding active cases."""

        async def _cleanup_wrapper():
            deleted_count = 0

            try:
                collections = self.client.list_collections()

                expected_collections = {
                    self._get_collection_name(case_id) for case_id in active_case_ids
                }

                for collection in collections:
                    if not collection.name.startswith(self.COLLECTION_PREFIX):
                        continue

                    if collection.name not in expected_collections:
                        try:
                            self.client.delete_collection(name=collection.name)
                            deleted_count += 1

                            case_id = collection.name[len(self.COLLECTION_PREFIX) :]
                            self.logger.info(
                                f"Cleaned up orphaned case collection: {collection.name}",
                                extra={
                                    "collection": collection.name,
                                    "case_id": case_id,
                                    "reason": "no_active_case",
                                },
                            )
                        except Exception as e:
                            self.logger.warning(
                                f"Failed to delete orphaned collection {collection.name}: {e}"
                            )
                            continue

                if deleted_count > 0:
                    self.logger.info(
                        f"Cleanup complete: deleted {deleted_count} orphaned case collections"
                    )
                else:
                    self.logger.debug("No orphaned case collections to clean up")

                return deleted_count

            except Exception as e:
                self.logger.error(f"Error during orphaned collection cleanup: {e}")
                raise

        return await self.call_external(
            operation_name="cleanup_orphaned_collections",
            call_func=_cleanup_wrapper,
            timeout=60.0,
            retries=1,
            retry_delay=5.0,
        )

    async def get_case_document_count(self, case_id: str) -> int:
        """Get number of documents in case collection."""

        async def _count_wrapper():
            collection_name = self._get_collection_name(case_id)

            try:
                collection = self.client.get_collection(name=collection_name)
                count = collection.count()
                self.logger.debug(f"Case {case_id} has {count} documents")
                return count
            except Exception:
                return 0

        return await self.call_external(
            operation_name="get_case_document_count",
            call_func=_count_wrapper,
            timeout=5.0,
            retries=1,
            retry_delay=1.0,
        )

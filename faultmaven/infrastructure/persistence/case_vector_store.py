"""
Case-Specific Vector Store for Session Working Memory

Implements Session-Specific RAG by creating ChromaDB collections per case_id.
These collections store user-uploaded documents and extracted evidence for the
duration of the case, then get cleaned up when the case closes.

Architecture:
- Each case gets its own ChromaDB collection: `case_{case_id}`
- Collections are created on-demand when first document is added
- Lifecycle-based cleanup: Collections deleted when case closes/archives
- Supports document addition, semantic search, and deletion
- Used by answer_from_document tool in QA sub-agent

Example flow:
1. User uploads PDF → add_document(case_id, doc) → creates `case_abc123`
2. User asks "What does page 5 say?" → search(case_id, query) → retrieves chunks
3. QA sub-agent synthesizes answer using synthesis LLM
4. Case closes → CaseService.close_case() → deletes `case_abc123` collection
"""

from typing import List, Dict, Optional, Any
from datetime import datetime, timezone, timedelta
import time
import chromadb
from chromadb.config import Settings
from urllib.parse import urlparse
import logging

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.base_client import BaseExternalClient


logger = logging.getLogger(__name__)


class CaseVectorStore(BaseExternalClient):
    """
    Case-specific vector store for Session-Specific RAG.

    Creates ChromaDB collections per case_id with lifecycle-based cleanup.
    Each collection stores user-uploaded documents and evidence for one case.
    Collections are deleted when the case closes or archives.
    """

    # Collection name format: case_{case_id}
    COLLECTION_PREFIX = "case_"

    def __init__(self):
        """Initialize case vector store."""
        super().__init__(
            client_name="case_vector_store",
            service_name="CaseVectorStore",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=60
        )

        # Get ChromaDB configuration from unified settings
        settings = get_settings()
        chromadb_url = settings.database.chromadb_url
        chromadb_token = settings.database.chromadb_api_key.get_secret_value() if settings.database.chromadb_api_key else None

        # Parse host/port from URL
        parsed = urlparse(chromadb_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Initialize ChromaDB client with optional auth settings
        settings_kwargs = {}
        if chromadb_token:
            try:
                from importlib import import_module
                import_module("chromadb.auth.token")
                settings_kwargs.update({
                    "chroma_client_auth_provider": "chromadb.auth.token.TokenAuthClientProvider",
                    "chroma_client_auth_credentials": chromadb_token,
                })
            except Exception:
                pass

        try:
            self.client = chromadb.HttpClient(
                host=host,
                port=port,
                settings=Settings(**settings_kwargs) if settings_kwargs else Settings()
            )
            self.logger.info("CaseVectorStore initialized (lifecycle-based cleanup)")
        except Exception as e:
            self.logger.error(f"Failed to initialize ChromaDB client: {e}")
            raise

    def _get_collection_name(self, case_id: str) -> str:
        """Get collection name for a case"""
        return f"{self.COLLECTION_PREFIX}{case_id}"

    def _get_or_create_collection(self, case_id: str) -> chromadb.Collection:
        """
        Get or create ChromaDB collection for a case.

        Args:
            case_id: Case identifier

        Returns:
            ChromaDB collection instance
        """
        collection_name = self._get_collection_name(case_id)

        # Store case metadata (no TTL - lifecycle-based cleanup)
        metadata = {
            "case_id": case_id,
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        try:
            collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata=metadata
            )
            self.logger.debug(f"Collection ready: {collection_name}")
            return collection
        except Exception as e:
            self.logger.error(f"Failed to get/create collection {collection_name}: {e}")
            raise

    async def add_documents(
        self,
        case_id: str,
        documents: List[Dict[str, Any]]
    ) -> None:
        """
        Add documents to case-specific collection.

        Args:
            case_id: Case identifier
            documents: List of dicts with keys:
                - id: Document ID (required)
                - content: Document text (required)
                - metadata: Optional metadata dict
        """
        async def _add_wrapper():
            collection = self._get_or_create_collection(case_id)

            ids = [doc['id'] for doc in documents]
            contents = [doc['content'] for doc in documents]
            metadatas = [doc.get('metadata', {}) for doc in documents]

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

            collection.add(
                ids=ids,
                documents=contents,
                metadatas=sanitized_metadatas
            )

            self.logger.info(
                f"Added {len(documents)} documents to case {case_id}",
                extra={"case_id": case_id, "doc_count": len(documents)}
            )

        await self.call_external(
            operation_name="add_documents",
            call_func=_add_wrapper,
            timeout=30.0,
            retries=2,
            retry_delay=2.0
        )

    async def search(
        self,
        case_id: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for similar documents in case-specific collection.

        Args:
            case_id: Case identifier
            query: Search query text
            k: Number of results to return
            where: Optional metadata filters

        Returns:
            List of dicts with keys:
                - id: Document ID
                - content: Document text
                - metadata: Metadata dict
                - score: Similarity score (0.0-1.0)
        """
        async def _search_wrapper():
            collection = self._get_or_create_collection(case_id)

            query_params = {
                "query_texts": [query],
                "n_results": k,
                "include": ["documents", "metadatas", "distances"]
            }

            if where:
                query_params["where"] = where

            results = collection.query(**query_params)

            # Format results
            formatted_results = []
            if results['ids'] and results['ids'][0]:
                for i in range(len(results['ids'][0])):
                    formatted_results.append({
                        'id': results['ids'][0][i],
                        'content': results['documents'][0][i],
                        'metadata': results['metadatas'][0][i] if results['metadatas'][0] else {},
                        'score': 1.0 - results['distances'][0][i]  # Convert distance to similarity
                    })

            self.logger.debug(
                f"Case {case_id} search returned {len(formatted_results)} results",
                extra={"case_id": case_id, "query_len": len(query), "results": len(formatted_results)}
            )

            return formatted_results

        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )

    async def delete_case_collection(self, case_id: str) -> None:
        """
        Delete entire case collection (called when case closes/archives).

        This is the primary cleanup method, called by CaseService when a case
        transitions to CLOSED or ARCHIVED status.

        Args:
            case_id: Case identifier
        """
        async def _delete_wrapper():
            collection_name = self._get_collection_name(case_id)

            try:
                self.client.delete_collection(name=collection_name)
                self.logger.info(
                    f"Deleted case collection: {collection_name}",
                    extra={"case_id": case_id, "collection": collection_name}
                )
            except Exception as e:
                # Collection might not exist - that's OK
                self.logger.debug(f"Collection {collection_name} not found or already deleted: {e}")

        await self.call_external(
            operation_name="delete_case_collection",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0
        )

    async def cleanup_orphaned_collections(self, active_case_ids: List[str]) -> int:
        """
        Clean up case collections that don't have corresponding active cases.

        This is a safety net for collections that weren't properly deleted when
        cases closed. Should be called periodically by a background task.

        Args:
            active_case_ids: List of currently active case IDs from CaseStore

        Returns:
            Number of orphaned collections deleted
        """
        async def _cleanup_wrapper():
            deleted_count = 0

            try:
                # List all case collections
                collections = self.client.list_collections()

                # Create set of expected collection names for active cases
                expected_collections = {
                    self._get_collection_name(case_id)
                    for case_id in active_case_ids
                }

                for collection in collections:
                    # Only process case collections
                    if not collection.name.startswith(self.COLLECTION_PREFIX):
                        continue

                    # If collection is not for an active case, it's orphaned
                    if collection.name not in expected_collections:
                        try:
                            self.client.delete_collection(name=collection.name)
                            deleted_count += 1

                            # Extract case_id from collection name
                            case_id = collection.name[len(self.COLLECTION_PREFIX):]

                            self.logger.info(
                                f"Cleaned up orphaned case collection: {collection.name}",
                                extra={
                                    "collection": collection.name,
                                    "case_id": case_id,
                                    "reason": "no_active_case"
                                }
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
            retry_delay=5.0
        )

    async def get_case_document_count(self, case_id: str) -> int:
        """
        Get number of documents in case collection.

        Args:
            case_id: Case identifier

        Returns:
            Number of documents in collection
        """
        async def _count_wrapper():
            collection_name = self._get_collection_name(case_id)

            try:
                collection = self.client.get_collection(name=collection_name)
                count = collection.count()
                self.logger.debug(f"Case {case_id} has {count} documents")
                return count
            except Exception:
                # Collection doesn't exist
                return 0

        return await self.call_external(
            operation_name="get_case_document_count",
            call_func=_count_wrapper,
            timeout=5.0,
            retries=1,
            retry_delay=1.0
        )

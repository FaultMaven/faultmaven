"""
User Knowledge Base Vector Store

Implements user-scoped persistent knowledge base for runbooks and procedures.
Unlike CaseVectorStore (ephemeral, lifecycle-based cleanup), UserKBVectorStore
is permanent and grows with user's documented knowledge.

Architecture:
- Each user gets their own ChromaDB collection: `user_kb_{user_id}`
- Collections are created on-demand when first document is added
- Documents persist indefinitely until explicitly deleted by user
- Supports document addition, listing, semantic search, and deletion
- Used by answer_from_user_kb tool in agent

Example flow:
1. User uploads runbook → add_document(user_id, doc) → creates `user_kb_alice`
2. User asks "How do I handle DB timeouts?" → search(user_id, query) → retrieves chunks
3. Agent synthesizes answer using synthesis LLM
4. User deletes old runbook → delete_document(user_id, doc_id)
"""

from typing import List, Dict, Optional, Any
from datetime import datetime, timezone
import chromadb
from chromadb.config import Settings
from urllib.parse import urlparse
import logging

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.base_client import BaseExternalClient


logger = logging.getLogger(__name__)


class UserKBVectorStore(BaseExternalClient):
    """
    User-scoped knowledge base vector store for persistent runbooks.

    Creates ChromaDB collections per user_id with permanent storage.
    Each collection stores user's runbooks, procedures, and documentation.
    Documents persist until explicitly deleted by the user.
    """

    # Collection name format: user_kb_{user_id}
    COLLECTION_PREFIX = "user_kb_"

    def __init__(self):
        """Initialize user KB vector store."""
        super().__init__(
            client_name="user_kb_vector_store",
            service_name="UserKBVectorStore",
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
            self.logger.info("UserKBVectorStore initialized (permanent storage)")
        except Exception as e:
            self.logger.error(f"Failed to initialize ChromaDB client: {e}")
            raise

    def _get_collection_name(self, user_id: str) -> str:
        """Get collection name for a user"""
        return f"{self.COLLECTION_PREFIX}{user_id}"

    def _get_or_create_collection(self, user_id: str) -> chromadb.Collection:
        """
        Get or create ChromaDB collection for a user.

        Args:
            user_id: User identifier

        Returns:
            ChromaDB collection instance
        """
        collection_name = self._get_collection_name(user_id)

        # Store user metadata (permanent - no TTL)
        metadata = {
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "type": "user_knowledge_base"
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
        user_id: str,
        documents: List[Dict[str, Any]]
    ) -> None:
        """
        Add documents to user's knowledge base.

        Args:
            user_id: User identifier
            documents: List of dicts with keys:
                - id: Document ID (required)
                - content: Document text (required)
                - metadata: Optional metadata dict (title, category, tags, etc.)
        """
        async def _add_wrapper():
            collection = self._get_or_create_collection(user_id)

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
                f"Added {len(documents)} documents to user KB {user_id}",
                extra={"user_id": user_id, "doc_count": len(documents)}
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
        user_id: str,
        query: str,
        k: int = 5,
        where: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Search user's knowledge base.

        Args:
            user_id: User identifier
            query: Search query text
            k: Number of results to return
            where: Optional metadata filters (e.g., {"category": "database"})

        Returns:
            List of dicts with keys:
                - id: Document ID
                - content: Document text
                - metadata: Metadata dict
                - score: Similarity score (0.0-1.0)
        """
        async def _search_wrapper():
            collection = self._get_or_create_collection(user_id)

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
                f"User {user_id} KB search returned {len(formatted_results)} results",
                extra={"user_id": user_id, "query_len": len(query), "results": len(formatted_results)}
            )

            return formatted_results

        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )

    async def list_documents(
        self,
        user_id: str,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List all documents in user's knowledge base.

        Args:
            user_id: User identifier
            limit: Maximum number of documents to return (None = all)
            offset: Number of documents to skip

        Returns:
            List of dicts with keys:
                - id: Document ID
                - metadata: Metadata dict (filename, title, category, etc.)
                - created_at: Upload timestamp
        """
        async def _list_wrapper():
            collection = self._get_or_create_collection(user_id)

            # Get all documents (ChromaDB doesn't support direct pagination)
            results = collection.get(
                include=["metadatas"]
            )

            documents = []
            if results['ids']:
                for i, doc_id in enumerate(results['ids']):
                    metadata = results['metadatas'][i] if results['metadatas'] else {}
                    documents.append({
                        'id': doc_id,
                        'metadata': metadata,
                        'created_at': metadata.get('uploaded_at', metadata.get('created_at'))
                    })

            # Apply pagination
            if offset > 0:
                documents = documents[offset:]
            if limit is not None:
                documents = documents[:limit]

            self.logger.debug(
                f"Listed {len(documents)} documents from user {user_id} KB",
                extra={"user_id": user_id, "doc_count": len(documents)}
            )

            return documents

        return await self.call_external(
            operation_name="list_documents",
            call_func=_list_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )

    async def delete_document(self, user_id: str, doc_id: str) -> None:
        """
        Delete a specific document from user's knowledge base.

        Args:
            user_id: User identifier
            doc_id: Document ID to delete
        """
        async def _delete_wrapper():
            collection = self._get_or_create_collection(user_id)

            try:
                collection.delete(ids=[doc_id])
                self.logger.info(
                    f"Deleted document {doc_id} from user {user_id} KB",
                    extra={"user_id": user_id, "doc_id": doc_id}
                )
            except Exception as e:
                # Document might not exist - that's OK
                self.logger.debug(f"Document {doc_id} not found or already deleted: {e}")

        await self.call_external(
            operation_name="delete_document",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0
        )

    async def delete_user_collection(self, user_id: str) -> None:
        """
        Delete entire user KB collection (GDPR compliance - user account deletion).

        This should only be called when a user account is permanently deleted.

        Args:
            user_id: User identifier
        """
        async def _delete_wrapper():
            collection_name = self._get_collection_name(user_id)

            try:
                self.client.delete_collection(name=collection_name)
                self.logger.info(
                    f"Deleted user KB collection: {collection_name}",
                    extra={"user_id": user_id, "collection": collection_name}
                )
            except Exception as e:
                # Collection might not exist - that's OK
                self.logger.debug(f"Collection {collection_name} not found or already deleted: {e}")

        await self.call_external(
            operation_name="delete_user_collection",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=1,
            retry_delay=1.0
        )

    async def get_document_count(self, user_id: str) -> int:
        """
        Get total number of documents in user's KB.

        Args:
            user_id: User identifier

        Returns:
            Number of documents
        """
        async def _count_wrapper():
            collection = self._get_or_create_collection(user_id)
            return collection.count()

        return await self.call_external(
            operation_name="get_document_count",
            call_func=_count_wrapper,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )

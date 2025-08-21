"""
ChromaDB implementation of IVectorStore interface.

This module provides a ChromaDB-based vector store that implements
the IVectorStore interface for consistent vector database operations.
"""

from typing import List, Dict, Optional
import os
from urllib.parse import urlparse
import chromadb
from chromadb.config import Settings
from faultmaven.models.interfaces import IVectorStore
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.config.config import config
import logging


class ChromaDBVectorStore(BaseExternalClient, IVectorStore):
    """ChromaDB implementation of the IVectorStore interface"""
    
    def __init__(self):
        """Initialize ChromaDB client with K8s service configuration"""
        super().__init__(
            client_name="chromadb_vector_store",
            service_name="ChromaDB",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=60
        )
        
        # Get ChromaDB configuration from environment or centralized config
        chromadb_url = os.getenv("CHROMADB_URL", config.chromadb.url)
        chromadb_token = os.getenv("CHROMADB_API_KEY", config.chromadb.api_key)

        # Parse host/port from URL
        parsed = urlparse(chromadb_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Initialize ChromaDB client with optional auth settings
        settings_kwargs = {}
        if chromadb_token:
            # Attempt to set auth provider only if available, without binding local 'chromadb'
            try:
                from importlib import import_module
                import_module("chromadb.auth.token")
                settings_kwargs.update({
                    "chroma_client_auth_provider": "chromadb.auth.token.TokenAuthClientProvider",
                    "chroma_client_auth_credentials": chromadb_token,
                })
            except Exception:
                # Proceed without explicit auth configuration
                pass

        try:
            self.client = chromadb.HttpClient(
                host=host,
                port=port,
                settings=Settings(**settings_kwargs) if settings_kwargs else Settings()
            )
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error(f"Failed to initialize ChromaDB HTTP client: {e}")
            raise
        
        # Get or create collection
        self.collection_name = os.getenv("CHROMADB_COLLECTION", config.chromadb.collection_name)
        try:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "FaultMaven knowledge base"}
            )
            if hasattr(self, 'logger'):
                # INFO: collection creation/connect events
                self.logger.info(
                    f"ChromaDB collection ready",
                    extra={"collection": self.collection_name}
                )
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error(f"❌ Failed to connect to ChromaDB: {e}")
            raise
    
    async def add_documents(self, documents: List[Dict]) -> None:
        """
        Add documents to the vector store.
        
        Args:
            documents: List of documents with 'id', 'content', and 'metadata' keys
        """
        async def _add_wrapper():
            ids = [doc['id'] for doc in documents]
            contents = [doc['content'] for doc in documents]
            raw_metadatas = [doc.get('metadata', {}) for doc in documents]

            # Normalize metadata via canonical schema
            from faultmaven.models.vector_metadata import VectorMetadata
            metadatas: List[Dict] = []
            for md in raw_metadatas:
                try:
                    vm = VectorMetadata(
                        title=md.get('title'),
                        document_type=md.get('document_type'),
                        tags=md.get('tags', []),
                        source_url=md.get('source_url'),
                        created_at=md.get('created_at'),
                        updated_at=md.get('updated_at'),
                    )
                    metadatas.append(vm.to_chroma_metadata())
                except Exception:
                    # Fallback sanitizer
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
            
            self.collection.add(
                ids=ids,
                documents=contents,
                metadatas=metadatas
            )
            
            # INFO: embedding counts
            if hasattr(self, 'logger'):
                self.logger.info(
                    f"Added documents to vector store",
                    extra={"count": len(documents), "collection": self.collection_name}
                )
            # DEBUG: semantic indexing details
            if hasattr(self, 'logger'):
                self.logger.debug(
                    f"Semantic indexing completed for ids={ids}"
                )
        
        await self.call_external(
            operation_name="add_documents",
            call_func=_add_wrapper,
            timeout=30.0,
            retries=2,
            retry_delay=2.0
        )
    
    async def search(self, query: str, k: int = 5) -> List[Dict]:
        """
        Search for similar documents in the vector store.
        
        Args:
            query: Search query text
            k: Number of results to return
            
        Returns:
            List of similar documents with scores
        """
        async def _search_wrapper():
            results = self.collection.query(
                query_texts=[query],
                n_results=k,
                include=["documents", "metadatas", "distances"]
            )
            
            # Format results according to interface contract
            formatted_results = []
            for i in range(len(results['ids'][0])):
                formatted_results.append({
                    'id': results['ids'][0][i],
                    'content': results['documents'][0][i],
                    'metadata': results['metadatas'][0][i],
                    'score': 1.0 - results['distances'][0][i]  # Convert distance to similarity
                })
            
            self.logger.debug(f"Found {len(formatted_results)} similar documents")
            return formatted_results
        
        return await self.call_external(
            operation_name="search",
            call_func=_search_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def delete_documents(self, ids: List[str]) -> None:
        """Delete documents by IDs"""
        async def _delete_wrapper():
            self.collection.delete(ids=ids)
            self.logger.info(f"Deleted {len(ids)} documents from vector store")
        
        await self.call_external(
            operation_name="delete_documents",
            call_func=_delete_wrapper,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
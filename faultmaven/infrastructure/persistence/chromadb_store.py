"""
ChromaDB implementation of IVectorStore interface.

This module provides a ChromaDB-based vector store that implements
the IVectorStore interface for consistent vector database operations.
"""

from typing import List, Dict, Optional
import os
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
        # Use environment variables directly to support testing environment changes
        chromadb_url = os.getenv("CHROMADB_URL", config.chromadb.url)
        chromadb_token = os.getenv("CHROMADB_API_KEY", config.chromadb.api_key)
        
        # Parse host from URL properly
        host = chromadb_url.replace("http://", "").replace("https://", "").split(":")[0]
        
        # Initialize ChromaDB client
        self.client = chromadb.HttpClient(
            host=host,
            port=30080,
            settings=Settings(
                chroma_client_auth_provider="chromadb.auth.token.TokenAuthClientProvider",
                chroma_client_auth_credentials=chromadb_token
            )
        )
        
        # Get or create collection
        self.collection_name = os.getenv("CHROMADB_COLLECTION", config.chromadb.collection_name)
        try:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "FaultMaven knowledge base"}
            )
            if hasattr(self, 'logger'):
                self.logger.info(f"✅ Connected to ChromaDB collection: {self.collection_name}")
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
            metadatas = [doc.get('metadata', {}) for doc in documents]
            
            self.collection.add(
                ids=ids,
                documents=contents,
                metadatas=metadatas
            )
            
            self.logger.info(f"Added {len(documents)} documents to vector store")
        
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
"""
ChromaDB Client with BaseExternalClient integration.

Provides a ChromaDB client that inherits from BaseExternalClient for unified
logging, retry logic, circuit breaker patterns, and comprehensive error handling.
"""

import os
import chromadb
from chromadb.config import Settings
from typing import Any, Dict, List, Optional, Union
from faultmaven.infrastructure.base_client import BaseExternalClient


class ChromaDBClient(BaseExternalClient):
    """
    ChromaDB client with BaseExternalClient integration.
    
    This class wraps the ChromaDB client with unified infrastructure logging,
    retry logic, and circuit breaker protection for all vector database operations.
    """
    
    def __init__(
        self,
        chroma_persist_directory: Optional[str] = None,
        chromadb_url: Optional[str] = None,
        chromadb_host: Optional[str] = None,
        chromadb_port: Optional[int] = None,
        chromadb_auth_token: Optional[str] = None
    ):
        """
        Initialize ChromaDB client with BaseExternalClient integration.
        
        Args:
            chroma_persist_directory: Local persistence directory
            chromadb_url: Complete ChromaDB URL
            chromadb_host: ChromaDB host
            chromadb_port: ChromaDB port
            chromadb_auth_token: Authentication token
        """
        # Initialize BaseExternalClient
        super().__init__(
            client_name="chromadb_client",
            service_name="ChromaDB",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for vector DB
            circuit_breaker_timeout=60    # Standard timeout for recovery
        )
        
        # Build configuration from parameters and environment
        config = self._build_config(
            chroma_persist_directory,
            chromadb_url,
            chromadb_host, 
            chromadb_port,
            chromadb_auth_token
        )
        
        # Create the underlying ChromaDB client
        self._client = self._create_client(config)
        
        # Log initialization
        self.logger.log_event(
            event_type="system",
            event_name="chromadb_client_initialized",
            severity="info",
            data={
                "client_type": config["client_type"],
                "host": config.get("host"),
                "port": config.get("port"),
                "persist_directory": config.get("persist_directory")
            }
        )
        
        # Store configuration for health checks
        self._config = config
    
    def _build_config(
        self,
        chroma_persist_directory: Optional[str],
        chromadb_url: Optional[str], 
        chromadb_host: Optional[str],
        chromadb_port: Optional[int],
        chromadb_auth_token: Optional[str]
    ) -> Dict[str, Any]:
        """Build ChromaDB configuration from various sources."""
        
        # Priority: explicit parameters > environment variables > defaults
        config = {}
        
        # 1. Check for explicit URL parameter
        if chromadb_url:
            config.update({
                "client_type": "http",
                "url": chromadb_url,
                "host": chromadb_url.replace("http://", "").replace("https://", "").split(":")[0],
                "port": int(chromadb_url.split(":")[-1])
            })
            return config
        
        # 2. Check environment URL
        env_url = os.getenv('CHROMADB_URL')
        if env_url:
            config.update({
                "client_type": "http",
                "url": env_url,
                "host": env_url.replace("http://", "").replace("https://", "").split(":")[0],
                "port": int(env_url.split(":")[-1])
            })
            return config
            
        # 3. Build from individual parameters and environment
        host = chromadb_host or os.getenv('CHROMADB_HOST', 'chromadb.faultmaven.local')
        port = chromadb_port or int(os.getenv('CHROMADB_PORT', '30080'))
        token = chromadb_auth_token or os.getenv('CHROMADB_AUTH_TOKEN', 'faultmaven-dev-chromadb-2025')
        persist_dir = chroma_persist_directory or os.getenv('CHROMADB_PERSIST_DIR', './chroma_db')
        
        # Determine client type based on host
        if host == 'localhost' or host.startswith('127.'):
            # Local development with persistent client
            config.update({
                "client_type": "persistent",
                "persist_directory": persist_dir
            })
        else:
            # K8s cluster or external HTTP client
            config.update({
                "client_type": "http",
                "host": host,
                "port": port,
                "auth_token": token
            })
        
        return config
    
    def _create_client(self, config: Dict[str, Any]):
        """Create the appropriate ChromaDB client based on configuration."""
        
        if config["client_type"] == "persistent":
            self.logger.info(f"Creating ChromaDB PersistentClient at {config['persist_directory']}")
            return chromadb.PersistentClient(
                path=config["persist_directory"],
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
        else:
            self.logger.info(f"Creating ChromaDB HttpClient at {config['host']}:{config['port']}")
            settings = Settings(
                anonymized_telemetry=False,
                allow_reset=True
            )
            
            # Add authentication if provided
            if config.get("auth_token"):
                settings.chroma_client_auth_provider = "chromadb.auth.token_authn.TokenAuthClientProvider"
                settings.chroma_client_auth_credentials = config["auth_token"]
            
            return chromadb.HttpClient(
                host=config["host"],
                port=config["port"],
                settings=settings
            )
    
    async def get_or_create_collection(self, name: str, metadata: Optional[Dict[str, Any]] = None):
        """Get or create a collection with external call wrapping."""
        return await self.call_external(
            operation_name="get_or_create_collection",
            call_func=self._client.get_or_create_collection,
            name=name,
            metadata=metadata,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def get_collection(self, name: str):
        """Get a collection with external call wrapping."""
        return await self.call_external(
            operation_name="get_collection",
            call_func=self._client.get_collection,
            name=name,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def delete_collection(self, name: str) -> None:
        """Delete a collection with external call wrapping."""
        await self.call_external(
            operation_name="delete_collection",
            call_func=self._client.delete_collection,
            name=name,
            timeout=10.0,
            retries=1,
            retry_delay=2.0
        )
    
    async def list_collections(self) -> List[Any]:
        """List all collections with external call wrapping."""
        return await self.call_external(
            operation_name="list_collections",
            call_func=self._client.list_collections,
            timeout=5.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def heartbeat(self) -> int:
        """Check ChromaDB heartbeat with external call wrapping."""
        return await self.call_external(
            operation_name="heartbeat",
            call_func=self._client.heartbeat,
            timeout=5.0,
            retries=1,
            retry_delay=1.0
        )
    
    async def get_version(self) -> str:
        """Get ChromaDB version with external call wrapping."""
        return await self.call_external(
            operation_name="get_version",
            call_func=self._client.get_version,
            timeout=5.0,
            retries=1,
            retry_delay=1.0
        )
    
    async def reset(self) -> bool:
        """Reset ChromaDB (development only) with external call wrapping."""
        return await self.call_external(
            operation_name="reset",
            call_func=self._client.reset,
            timeout=30.0,
            retries=1,
            retry_delay=5.0
        )
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check for ChromaDB client.
        
        Returns:
            Dictionary containing health status and metrics
        """
        base_health = await super().health_check()
        
        # Add ChromaDB-specific health data
        try:
            # Test basic connectivity
            heartbeat = await self.heartbeat()
            version = await self.get_version()
            collections = await self.list_collections()
            
            chromadb_health = {
                "heartbeat": heartbeat,
                "version": version,
                "collection_count": len(collections),
                "client_type": self._config["client_type"]
            }
            
            # Add client-specific details
            if self._config["client_type"] == "http":
                chromadb_health.update({
                    "host": self._config["host"],
                    "port": self._config["port"],
                    "auth_enabled": bool(self._config.get("auth_token"))
                })
            else:
                chromadb_health.update({
                    "persist_directory": self._config["persist_directory"]
                })
            
            base_health.update({
                "chromadb_specific": chromadb_health,
                "status": "healthy"
            })
            
        except Exception as e:
            base_health.update({
                "chromadb_specific": {"error": str(e)},
                "status": "unhealthy"
            })
        
        return base_health
    
    def __getattr__(self, name: str) -> Any:
        """
        Delegate unknown attributes to the underlying ChromaDB client.
        
        This allows access to ChromaDB methods not explicitly wrapped while
        still maintaining the BaseExternalClient functionality for the
        most common operations.
        """
        if hasattr(self._client, name):
            return getattr(self._client, name)
        raise AttributeError(f"ChromaDBClient has no attribute '{name}'")


class ChromaDBCollection(BaseExternalClient):
    """
    ChromaDB collection wrapper with BaseExternalClient integration.
    
    This class wraps ChromaDB collection operations with unified logging,
    retry logic, and circuit breaker protection.
    """
    
    def __init__(self, collection, collection_name: str):
        """
        Initialize ChromaDB collection wrapper.
        
        Args:
            collection: The ChromaDB collection instance
            collection_name: Name of the collection for logging
        """
        # Initialize BaseExternalClient
        super().__init__(
            client_name=f"chromadb_collection_{collection_name}",
            service_name="ChromaDB_Collection",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=60
        )
        
        self._collection = collection
        self.collection_name = collection_name
        
        # Log initialization
        self.logger.log_event(
            event_type="system",
            event_name="chromadb_collection_initialized",
            severity="info",
            data={"collection_name": collection_name}
        )
    
    async def add(
        self,
        ids: List[str],
        documents: List[str],
        embeddings: Optional[List[List[float]]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Add documents to collection with external call wrapping."""
        await self.call_external(
            operation_name="add_documents",
            call_func=self._collection.add,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            timeout=30.0,  # Longer timeout for embedding operations
            retries=2,
            retry_delay=2.0
        )
    
    async def query(
        self,
        query_texts: Optional[List[str]] = None,
        query_embeddings: Optional[List[List[float]]] = None,
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
        where_document: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Query collection with external call wrapping."""
        return await self.call_external(
            operation_name="query_documents",
            call_func=self._collection.query,
            query_texts=query_texts,
            query_embeddings=query_embeddings,
            n_results=n_results,
            where=where,
            where_document=where_document,
            include=include,
            timeout=20.0,  # Longer timeout for search operations
            retries=2,
            retry_delay=1.0
        )
    
    async def get(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        where_document: Optional[Dict[str, Any]] = None,
        sort: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Get documents from collection with external call wrapping."""
        return await self.call_external(
            operation_name="get_documents",
            call_func=self._collection.get,
            ids=ids,
            where=where,
            where_document=where_document,
            sort=sort,
            limit=limit,
            offset=offset,
            include=include,
            timeout=15.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def update(
        self,
        ids: List[str],
        embeddings: Optional[List[List[float]]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
        documents: Optional[List[str]] = None
    ) -> None:
        """Update documents in collection with external call wrapping."""
        await self.call_external(
            operation_name="update_documents",
            call_func=self._collection.update,
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
            timeout=20.0,
            retries=2,
            retry_delay=2.0
        )
    
    async def upsert(
        self,
        ids: List[str],
        documents: List[str],
        embeddings: Optional[List[List[float]]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Upsert documents in collection with external call wrapping."""
        await self.call_external(
            operation_name="upsert_documents",
            call_func=self._collection.upsert,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            timeout=30.0,
            retries=2,
            retry_delay=2.0
        )
    
    async def delete(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        where_document: Optional[Dict[str, Any]] = None
    ) -> None:
        """Delete documents from collection with external call wrapping."""
        await self.call_external(
            operation_name="delete_documents",
            call_func=self._collection.delete,
            ids=ids,
            where=where,
            where_document=where_document,
            timeout=15.0,
            retries=1,
            retry_delay=2.0
        )
    
    async def count(self) -> int:
        """Count documents in collection with external call wrapping."""
        return await self.call_external(
            operation_name="count_documents",
            call_func=self._collection.count,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def peek(self, limit: int = 10) -> Dict[str, Any]:
        """Peek at collection contents with external call wrapping."""
        return await self.call_external(
            operation_name="peek_documents",
            call_func=self._collection.peek,
            limit=limit,
            timeout=10.0,
            retries=1,
            retry_delay=1.0
        )
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check for the collection.
        
        Returns:
            Dictionary containing health status and metrics
        """
        base_health = await super().health_check()
        
        try:
            # Test basic collection operations
            document_count = await self.count()
            peek_result = await self.peek(limit=1)
            
            collection_health = {
                "collection_name": self.collection_name,
                "document_count": document_count,
                "peek_successful": bool(peek_result),
                "metadata": getattr(self._collection, "metadata", {})
            }
            
            base_health.update({
                "collection_specific": collection_health,
                "status": "healthy"
            })
            
        except Exception as e:
            base_health.update({
                "collection_specific": {"error": str(e)},
                "status": "unhealthy"
            })
        
        return base_health
    
    def __getattr__(self, name: str) -> Any:
        """
        Delegate unknown attributes to the underlying collection.
        
        This allows access to collection methods not explicitly wrapped.
        """
        if hasattr(self._collection, name):
            return getattr(self._collection, name)
        raise AttributeError(f"ChromaDBCollection has no attribute '{name}'")
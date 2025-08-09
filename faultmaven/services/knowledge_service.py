"""Knowledge Service Refactored Module - Phase 3.3

Purpose: Interface-based knowledge service using dependency injection

This refactored service implements clean architecture principles by depending
on interfaces rather than concrete implementations, enabling better testability
and flexibility in the knowledge management system.

Core Responsibilities:
- Knowledge document management via interfaces
- Semantic search operations using IVectorStore
- Knowledge ingestion through IKnowledgeIngester
- Content validation and sanitization via ISanitizer
- Distributed tracing via ITracer

Key Improvements over Original:
- Interface-based dependency injection
- Cleaner separation of concerns
- Better error handling and validation
- Improved testability through mocking interfaces
- Standardized tracing and logging patterns
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
import hashlib

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import (
    IKnowledgeIngester, 
    ISanitizer, 
    ITracer, 
    IVectorStore
)
from faultmaven.models import KnowledgeBaseDocument, SearchResult


class KnowledgeService(BaseService):
    """Knowledge service using interface dependencies"""

    def __init__(
        self,
        knowledge_ingester: IKnowledgeIngester,
        sanitizer: ISanitizer,
        tracer: ITracer,
        vector_store: Optional[IVectorStore] = None
    ):
        """
        Initialize with interface dependencies for better testability
        
        Args:
            knowledge_ingester: Interface for document ingestion operations
            sanitizer: Interface for data sanitization (PII redaction)
            tracer: Interface for distributed tracing
            vector_store: Optional interface for vector database operations
        """
        super().__init__("knowledge_service")
        self._ingester = knowledge_ingester
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._vector_store = vector_store
        # Note: self.logger from BaseService replaces self.logger

    async def ingest_document(
        self,
        title: str,
        content: str,
        document_type: str,
        tags: Optional[List[str]] = None,
        source_url: Optional[str] = None,
    ) -> KnowledgeBaseDocument:
        """
        Ingest document using interface dependencies
        
        Args:
            title: Document title
            content: Document content
            document_type: Type of document (e.g., 'manual', 'troubleshooting')
            tags: Optional tags for categorization
            source_url: Optional source URL
            
        Returns:
            KnowledgeBaseDocument model
            
        Raises:
            ValueError: If validation fails
            RuntimeError: If ingestion fails
        """
        with self._tracer.trace("knowledge_service_ingest_document"):
            self.logger.info(f"Ingesting document: {title}")
            
            # Validate input
            self._validate_document_data(title, content)
            
            # Sanitize content for privacy compliance
            sanitized_content = self._sanitizer.sanitize(content)
            sanitized_title = self._sanitizer.sanitize(title)
            
            # Generate unique document ID
            document_id = self._generate_document_id(sanitized_title, document_type)
            
            # Prepare metadata
            metadata = {
                "tags": tags or [],
                "source_url": source_url,
                "document_type": document_type,
                "created_at": datetime.utcnow().isoformat()
            }
            
            try:
                # Ingest via interface
                result_id = await self._ingester.ingest_document(
                    title=sanitized_title,
                    content=sanitized_content,
                    document_type=document_type,
                    metadata=metadata
                )
                
                # Create response model
                document = KnowledgeBaseDocument(
                    document_id=result_id,
                    title=sanitized_title,
                    content=sanitized_content,
                    document_type=document_type,
                    tags=tags or [],
                    source_url=source_url,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                
                # Index in vector store if available
                if self._vector_store:
                    await self._index_document_in_vector_store(document)
                
                self.logger.info(f"Successfully ingested document {result_id}")
                return document
                
            except Exception as e:
                self.logger.error(f"Failed to ingest document: {e}")
                raise RuntimeError(f"Document ingestion failed: {str(e)}") from e

    async def search_knowledge(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[SearchResult]:
        """
        Search knowledge base using interface dependencies
        
        Args:
            query: Search query text
            limit: Maximum number of results to return
            filters: Optional filters for search refinement
            
        Returns:
            List of SearchResult models
            
        Raises:
            ValueError: If query is empty or invalid
        """
        with self._tracer.trace("knowledge_service_search"):
            self.logger.debug(f"Searching knowledge base: {query}")
            
            # Validate and sanitize query
            if not query or not query.strip():
                raise ValueError("Query cannot be empty")
            
            sanitized_query = self._sanitizer.sanitize(query)
            
            try:
                # Search via vector store interface if available
                if self._vector_store:
                    results = await self._vector_store.search(sanitized_query, k=limit)
                    
                    # Convert to SearchResult models
                    search_results = []
                    for result in results:
                        search_result = SearchResult(
                            document_id=result.get("id", "unknown"),
                            title=result.get("title", "Untitled"),
                            document_type=result.get("document_type", "general"),
                            tags=result.get("tags", []),
                            score=result.get("score", 0.0),
                            snippet=result.get("content", "")[:200] + "..."
                        )
                        search_results.append(search_result)
                    
                    self.logger.info(f"Found {len(search_results)} results for query: {query}")
                    return search_results
                else:
                    # Fallback when no vector store available
                    self.logger.warning("No vector store available, returning empty results")
                    return []
                    
            except Exception as e:
                self.logger.error(f"Search failed: {e}")
                raise

    async def update_document(
        self,
        document_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> KnowledgeBaseDocument:
        """
        Update document using interface dependencies
        
        Args:
            document_id: Document identifier
            title: Optional new title
            content: Optional new content
            tags: Optional new tags
            
        Returns:
            Updated KnowledgeBaseDocument
            
        Raises:
            ValueError: If document_id is invalid or no updates provided
        """
        with self._tracer.trace("knowledge_service_update_document"):
            self.logger.info(f"Updating document {document_id}")
            
            if not document_id or not document_id.strip():
                raise ValueError("Document ID cannot be empty")
            
            # Prepare update data
            update_data = {}
            metadata = {}
            
            if title:
                sanitized_title = self._sanitizer.sanitize(title)
                update_data["title"] = sanitized_title
                metadata["title"] = sanitized_title
                
            if content:
                sanitized_content = self._sanitizer.sanitize(content)
                update_data["content"] = sanitized_content
                
            if tags is not None:
                update_data["tags"] = tags
                metadata["tags"] = tags
            
            if not update_data:
                raise ValueError("At least one field must be provided for update")
            
            metadata["updated_at"] = datetime.utcnow().isoformat()
            
            try:
                # Update via interface
                await self._ingester.update_document(
                    document_id=document_id,
                    content=update_data.get("content", ""),
                    metadata=metadata
                )
                
                # Return updated document model
                updated_document = KnowledgeBaseDocument(
                    document_id=document_id,
                    title=update_data.get("title", "Updated Document"),
                    content=update_data.get("content", ""),
                    document_type="updated",
                    tags=tags or [],
                    updated_at=datetime.utcnow(),
                    created_at=datetime.utcnow()  # Would normally fetch from storage
                )
                
                # Re-index in vector store if content was updated
                if content and self._vector_store:
                    await self._index_document_in_vector_store(updated_document)
                
                self.logger.info(f"Successfully updated document {document_id}")
                return updated_document
                
            except Exception as e:
                self.logger.error(f"Failed to update document {document_id}: {e}")
                raise

    async def delete_document(self, document_id: str) -> bool:
        """
        Delete document using interface dependencies
        
        Args:
            document_id: Document identifier
            
        Returns:
            True if deletion was successful
        """
        with self._tracer.trace("knowledge_service_delete_document"):
            self.logger.info(f"Deleting document {document_id}")
            
            if not document_id or not document_id.strip():
                raise ValueError("Document ID cannot be empty")
            
            try:
                await self._ingester.delete_document(document_id)
                
                # Remove from vector store if available
                if self._vector_store:
                    await self._remove_from_vector_store(document_id)
                
                self.logger.info(f"Successfully deleted document {document_id}")
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to delete document {document_id}: {e}")
                return False

    async def get_document_statistics(self) -> Dict[str, Any]:
        """
        Get knowledge base statistics
        
        Returns:
            Dictionary with knowledge base statistics
        """
        with self._tracer.trace("knowledge_service_get_statistics"):
            try:
                # In a full implementation, would gather from storage
                # For now, return basic structure
                return {
                    "total_documents": 0,
                    "documents_by_type": {},
                    "most_used_tags": [],
                    "last_updated": datetime.utcnow().isoformat(),
                    "vector_store_enabled": self._vector_store is not None
                }
            except Exception as e:
                self.logger.error(f"Failed to get statistics: {e}")
                raise

    def _generate_document_id(self, title: str, document_type: str) -> str:
        """
        Generate unique document ID based on title and type
        
        Args:
            title: Document title
            document_type: Type of document
            
        Returns:
            Unique document identifier
        """
        content = f"{title}:{document_type}:{datetime.utcnow().isoformat()}"
        hash_object = hashlib.sha256(content.encode("utf-8"))
        return f"kb_{hash_object.hexdigest()[:16]}"

    def _validate_document_data(self, title: str, content: str) -> None:
        """
        Validate document data before processing
        
        Args:
            title: Document title to validate
            content: Document content to validate
            
        Raises:
            ValueError: If validation fails
        """
        if not isinstance(title, str) or not isinstance(content, str):
            raise ValueError("Title and content must be strings")
        
        if len(title.strip()) == 0:
            raise ValueError("Title cannot be empty")
        
        if len(content.strip()) == 0:
            raise ValueError("Content cannot be empty")
        
        # Additional validation rules can be added here
        if len(title) > 500:
            raise ValueError("Title cannot exceed 500 characters")

    async def _index_document_in_vector_store(self, document: KnowledgeBaseDocument) -> None:
        """
        Index document in vector store for semantic search
        
        Args:
            document: Document to index
        """
        if not self._vector_store:
            return
        
        try:
            # Convert document to format expected by vector store
            doc_dict = {
                "id": document.document_id,
                "title": document.title,
                "content": document.content,
                "document_type": document.document_type,
                "tags": document.tags,
                "metadata": {
                    "source_url": document.source_url,
                    "created_at": document.created_at.isoformat() if document.created_at else None,
                    "updated_at": document.updated_at.isoformat() if document.updated_at else None
                }
            }
            
            await self._vector_store.add_documents([doc_dict])
            self.logger.debug(f"Indexed document {document.document_id} in vector store")
            
        except Exception as e:
            self.logger.error(f"Failed to index document in vector store: {e}")
            # Don't raise exception here as indexing failure shouldn't block ingestion

    async def _remove_from_vector_store(self, document_id: str) -> None:
        """
        Remove document from vector store index
        
        Args:
            document_id: ID of document to remove
        """
        if not self._vector_store:
            return
        
        try:
            # Vector store interface doesn't have delete method in current interface
            # This would need to be added to IVectorStore in a future phase
            self.logger.debug(f"Would remove document {document_id} from vector store")
            
        except Exception as e:
            self.logger.error(f"Failed to remove document from vector store: {e}")


# Phase 4 Complete: Adapter classes have been removed as core components now implement interfaces directly
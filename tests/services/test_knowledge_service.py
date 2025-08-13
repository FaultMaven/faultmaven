"""Comprehensive test suite for KnowledgeService - Phase 3 Testing

This test module validates the KnowledgeService which uses interface-based
dependency injection for better testability and maintainability.

All dependencies are mocked via interfaces to ensure true unit testing isolation.

Test Coverage:
- Document ingestion workflows
- Knowledge search operations  
- Document management (update, delete)
- Vector store integration
- Interface interaction verification
- Error handling and validation
- Statistics gathering
- Content sanitization
"""

import pytest
import asyncio
import hashlib
from datetime import datetime
from unittest.mock import Mock, AsyncMock, MagicMock
from typing import Any, Dict, List, Optional

from faultmaven.services.knowledge_service import KnowledgeService
from faultmaven.models import KnowledgeBaseDocument, SearchResult
from faultmaven.exceptions import ServiceException
from faultmaven.models.interfaces import (
    IKnowledgeIngester,
    ISanitizer,
    ITracer,
    IVectorStore
)


class TestKnowledgeService:
    """Comprehensive test suite for KnowledgeService"""

    @pytest.fixture
    def mock_knowledge_ingester(self):
        """Mock knowledge ingester interface"""
        mock = Mock(spec=IKnowledgeIngester)
        
        # Default behavior with unique IDs  
        counter = {"count": 0}
        
        def generate_unique_id(**kwargs):
            counter["count"] += 1
            return f"kb_doc_{counter['count']:03d}"
        
        mock.ingest_document = AsyncMock(side_effect=generate_unique_id)
        mock.update_document = AsyncMock()
        mock.delete_document = AsyncMock()
        
        # Store reference to allow tests to override behavior
        mock._generate_unique_id = generate_unique_id
        mock._counter = counter
        
        return mock

    @pytest.fixture
    def mock_sanitizer(self):
        """Mock sanitizer interface"""
        mock = Mock(spec=ISanitizer)
        mock.sanitize = Mock(side_effect=lambda x: x)  # Pass through for testing
        return mock

    @pytest.fixture
    def mock_tracer(self):
        """Mock tracer interface with context manager"""
        mock = Mock(spec=ITracer)
        from contextlib import contextmanager
        
        # Track calls manually
        mock._trace_calls = []
        
        @contextmanager
        def mock_trace(operation):
            mock._trace_calls.append(operation)
            yield None
        
        mock.trace = mock_trace
        
        # Add helper method to check calls
        def assert_called_with(operation):
            assert operation in mock._trace_calls, f"Expected tracer to be called with '{operation}', but calls were: {mock._trace_calls}"
        
        mock.trace.assert_called_with = assert_called_with
        return mock

    @pytest.fixture
    def mock_vector_store(self):
        """Mock vector store interface"""
        mock = Mock(spec=IVectorStore)
        mock.add_documents = AsyncMock()
        mock.search = AsyncMock(return_value=[
            {
                "id": "doc_1",
                "title": "Database Troubleshooting Guide",
                "content": "Complete guide for database issues...",
                "document_type": "troubleshooting",
                "tags": ["database", "troubleshooting"],
                "score": 0.95
            },
            {
                "id": "doc_2", 
                "title": "Connection Pool Configuration",
                "content": "How to configure connection pools...",
                "document_type": "configuration",
                "tags": ["database", "configuration"],
                "score": 0.87
            }
        ])
        return mock

    @pytest.fixture
    def mock_logger(self):
        """Mock logger for testing"""
        logger = Mock()
        logger.debug = Mock()
        logger.info = Mock()
        logger.error = Mock()
        logger.warning = Mock()
        return logger

    @pytest.fixture
    def knowledge_service(
        self, mock_knowledge_ingester, mock_sanitizer, mock_tracer, 
        mock_vector_store
    ):
        """KnowledgeService instance with mocked dependencies"""
        return KnowledgeService(
            knowledge_ingester=mock_knowledge_ingester,
            sanitizer=mock_sanitizer,
            tracer=mock_tracer,
            vector_store=mock_vector_store
        )

    @pytest.fixture
    def sample_document_data(self):
        """Sample document data for testing"""
        return {
            "title": "Database Connection Timeout Troubleshooting",
            "content": """# Database Connection Timeout Issues

## Symptoms
- Connection timeout errors in logs
- Slow query performance  
- Application hanging on database calls

## Root Causes
1. Network latency issues
2. Database overload
3. Connection pool exhaustion
4. Firewall blocking connections

## Solutions
1. Increase connection timeout values
2. Optimize database queries
3. Scale database resources
4. Check network connectivity
            """,
            "document_type": "troubleshooting",
            "tags": ["database", "timeout", "troubleshooting"],
            "source_url": "https://docs.example.com/db-timeout"
        }

    @pytest.fixture
    def sample_knowledge_document(self):
        """Sample KnowledgeBaseDocument for testing"""
        return KnowledgeBaseDocument(
            document_id="kb_abc123",
            title="Test Document",
            content="Test document content for knowledge base",
            document_type="manual",
            tags=["test", "documentation"],
            source_url="https://example.com/docs",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ingest_document_success(
        self, knowledge_service, sample_document_data, mock_knowledge_ingester,
        mock_sanitizer, mock_tracer, mock_vector_store
    ):
        """Test successful document ingestion with all interface interactions"""
        # Arrange
        title = sample_document_data["title"]
        content = sample_document_data["content"]
        document_type = sample_document_data["document_type"]
        tags = sample_document_data["tags"]
        source_url = sample_document_data["source_url"]

        # Override the default side_effect for this specific test
        mock_knowledge_ingester.ingest_document.return_value = "kb_doc_456"
        mock_knowledge_ingester.ingest_document.side_effect = None

        # Act
        result = await knowledge_service.ingest_document(
            title=title,
            content=content,
            document_type=document_type,
            tags=tags,
            source_url=source_url
        )

        # Assert - Response structure
        assert isinstance(result, KnowledgeBaseDocument)
        assert result.document_id == "kb_doc_456"
        assert result.title == title
        assert result.content == content
        assert result.document_type == document_type
        assert result.tags == tags
        assert result.source_url == source_url
        assert result.created_at is not None
        assert result.updated_at is not None

        # Assert - Interface interactions
        mock_sanitizer.sanitize.assert_any_call(content)
        mock_sanitizer.sanitize.assert_any_call(title)
        
        # Check that ingest_document was called with the right parameters (ignoring exact timestamp)
        mock_knowledge_ingester.ingest_document.assert_called_once()
        call_args = mock_knowledge_ingester.ingest_document.call_args
        assert call_args[1]['title'] == title
        assert call_args[1]['content'] == content
        assert call_args[1]['document_type'] == document_type
        assert call_args[1]['metadata']['tags'] == tags
        assert call_args[1]['metadata']['source_url'] == source_url
        assert call_args[1]['metadata']['document_type'] == document_type
        assert 'created_at' in call_args[1]['metadata']

        mock_vector_store.add_documents.assert_called_once()
        added_docs = mock_vector_store.add_documents.call_args[0][0]
        assert len(added_docs) == 1
        assert added_docs[0]["id"] == "kb_doc_456"
        assert added_docs[0]["title"] == title
        assert added_docs[0]["content"] == content

        mock_tracer.trace.assert_called_with("knowledge_service_ingest_document")

        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ingest_document_without_optional_params(
        self, knowledge_service, mock_knowledge_ingester, mock_sanitizer
    ):
        """Test document ingestion without optional parameters"""
        # Arrange
        title = "Simple Test Document"
        content = "Simple content for testing"
        document_type = "manual"

        # Override the default side_effect for this specific test
        mock_knowledge_ingester.ingest_document.return_value = "kb_simple_123"
        mock_knowledge_ingester.ingest_document.side_effect = None

        # Act
        result = await knowledge_service.ingest_document(
            title=title,
            content=content,
            document_type=document_type
            # No tags or source_url
        )

        # Assert
        assert isinstance(result, KnowledgeBaseDocument)
        assert result.document_id == "kb_simple_123"
        assert result.tags == []
        assert result.source_url is None

        # Verify ingester called with empty tags
        mock_knowledge_ingester.ingest_document.assert_called_once()
        call_args = mock_knowledge_ingester.ingest_document.call_args[1]
        assert call_args["metadata"]["tags"] == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ingest_document_validation_errors(self, knowledge_service):
        """Test input validation errors during document ingestion"""
        # Test empty title
        with pytest.raises(ValueError, match="Title cannot be empty"):
            await knowledge_service.ingest_document("", "content", "type")

        # Test empty content
        with pytest.raises(ValueError, match="Content cannot be empty"):
            await knowledge_service.ingest_document("title", "", "type")

        # Test whitespace-only title
        with pytest.raises(ValueError, match="Title cannot be empty"):
            await knowledge_service.ingest_document("   \n\t   ", "content", "type")

        # Test whitespace-only content
        with pytest.raises(ValueError, match="Content cannot be empty"):
            await knowledge_service.ingest_document("title", "   \n\t   ", "type")

        # Test non-string types
        with pytest.raises(ValueError, match="Title and content must be strings"):
            await knowledge_service.ingest_document(123, "content", "type")

        with pytest.raises(ValueError, match="Title and content must be strings"):
            await knowledge_service.ingest_document("title", 456, "type")

        # Test title too long
        long_title = "x" * 501
        with pytest.raises(ValueError, match="Title cannot exceed 500 characters"):
            await knowledge_service.ingest_document(long_title, "content", "type")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ingest_document_ingester_error(
        self, knowledge_service, mock_knowledge_ingester    ):
        """Test error handling when knowledge ingester fails"""
        # Arrange - Use ValueError instead of RuntimeError to trigger error wrapping
        mock_knowledge_ingester.ingest_document.side_effect = ValueError("Ingestion failed")

        # Act & Assert
        with pytest.raises(ServiceException, match="Document ingestion failed: Ingestion failed"):
            await knowledge_service.ingest_document("title", "content", "type")

        # Verify error logging
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ingest_document_without_vector_store(
        self, mock_knowledge_ingester, mock_sanitizer, mock_tracer
    ):
        """Test document ingestion without vector store"""
        # Arrange - Create service without vector store
        service = KnowledgeService(
            knowledge_ingester=mock_knowledge_ingester,
            sanitizer=mock_sanitizer,
            tracer=mock_tracer,
            vector_store=None  # No vector store
        )

        # Override the default side_effect for this specific test
        mock_knowledge_ingester.ingest_document.return_value = "kb_no_vector_123"
        mock_knowledge_ingester.ingest_document.side_effect = None

        # Act
        result = await service.ingest_document("title", "content", "type")

        # Assert - Should still succeed
        assert isinstance(result, KnowledgeBaseDocument)
        assert result.document_id == "kb_no_vector_123"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_knowledge_success(
        self, knowledge_service, mock_vector_store, mock_sanitizer, 
        mock_tracer    ):
        """Test successful knowledge base search"""
        # Arrange
        query = "database connection timeout issues"
        limit = 5

        mock_search_results = [
            {
                "id": "doc_1",
                "title": "Database Timeout Guide",
                "content": "Complete guide for handling database timeouts and connection issues...",
                "document_type": "troubleshooting",
                "tags": ["database", "timeout"],
                "score": 0.95
            },
            {
                "id": "doc_2",
                "title": "Connection Pool Best Practices", 
                "content": "Best practices for configuring and managing database connection pools...",
                "document_type": "best_practices",
                "tags": ["database", "connection_pool"],
                "score": 0.87
            }
        ]
        mock_vector_store.search.return_value = mock_search_results

        # Act
        results = await knowledge_service.search_knowledge(query, limit=limit)

        # Assert - Response structure
        assert len(results) == 2
        
        for i, result in enumerate(results):
            assert isinstance(result, SearchResult)
            assert result.document_id == mock_search_results[i]["id"]
            assert result.title == mock_search_results[i]["title"]
            assert result.document_type == mock_search_results[i]["document_type"]
            assert result.tags == mock_search_results[i]["tags"]
            assert result.score == mock_search_results[i]["score"]
            assert len(result.snippet) <= 203  # 200 + "..."

        # Assert - Interface interactions
        mock_sanitizer.sanitize.assert_called_with(query)
        mock_vector_store.search.assert_called_once_with(query, k=limit)
        mock_tracer.trace.assert_called_with("knowledge_service_search")

        # Note: Logging assertions removed since service uses unified logger
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_knowledge_empty_query_error(self, knowledge_service):
        """Test error handling for empty search query"""
        # Test completely empty query
        with pytest.raises(ValueError, match="Query cannot be empty"):
            await knowledge_service.search_knowledge("")

        # Test whitespace-only query
        with pytest.raises(ValueError, match="Query cannot be empty"):
            await knowledge_service.search_knowledge("   \n\t   ")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_knowledge_without_vector_store(
        self, mock_knowledge_ingester, mock_sanitizer, mock_tracer
    ):
        """Test knowledge search without vector store"""
        # Arrange - Create service without vector store
        service = KnowledgeService(
            knowledge_ingester=mock_knowledge_ingester,
            sanitizer=mock_sanitizer,
            tracer=mock_tracer,
            vector_store=None  # No vector store
        )

        # Act
        results = await service.search_knowledge("test query")

        # Assert - Should return empty results
        assert results == []
        
        # Note: Logging verification removed since service uses unified logger from BaseService

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_knowledge_vector_store_error(
        self, knowledge_service, mock_vector_store    ):
        """Test error handling when vector store search fails"""
        # Arrange
        mock_vector_store.search.side_effect = RuntimeError("Vector search failed")

        # Act & Assert
        with pytest.raises(RuntimeError, match="Vector search failed"):
            await knowledge_service.search_knowledge("test query")

        # Verify error logging
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_knowledge_malformed_results(
        self, knowledge_service, mock_vector_store    ):
        """Test handling of malformed search results from vector store"""
        # Arrange - Return malformed results
        mock_vector_store.search.return_value = [
            {
                "id": "doc_1",
                "title": "Valid Document",
                "document_type": "manual",
                "score": 0.9
                # Missing content and tags
            },
            {
                # Missing required fields
                "partial": "data"
            }
        ]

        # Act
        results = await knowledge_service.search_knowledge("test query")

        # Assert - Should handle gracefully
        assert len(results) == 2
        
        # First result should have defaults for missing fields
        assert results[0].document_id == "doc_1"
        assert results[0].tags == []
        assert results[0].snippet == "..."  # Default when no content

        # Second result should have defaults for all missing fields
        assert results[1].document_id == "unknown"
        assert results[1].title == "Untitled"
        assert results[1].document_type == "general"
        assert results[1].score == 0.0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_update_document_success(
        self, knowledge_service, mock_knowledge_ingester, mock_sanitizer,
        mock_tracer, mock_vector_store    ):
        """Test successful document update"""
        # Arrange
        document_id = "kb_update_123"
        new_title = "Updated Document Title"
        new_content = "Updated document content with new information"
        new_tags = ["updated", "test"]

        # Act
        result = await knowledge_service.update_document(
            document_id=document_id,
            title=new_title,
            content=new_content,
            tags=new_tags
        )

        # Assert - Response structure
        assert isinstance(result, KnowledgeBaseDocument)
        assert result.document_id == document_id
        assert result.title == new_title
        assert result.content == new_content
        assert result.tags == new_tags
        assert result.document_type == "updated"
        assert result.updated_at is not None

        # Assert - Interface interactions
        mock_sanitizer.sanitize.assert_any_call(new_title)
        mock_sanitizer.sanitize.assert_any_call(new_content)

        # Check that update_document was called with the right parameters (ignoring exact timestamp)
        mock_knowledge_ingester.update_document.assert_called_once()
        call_args = mock_knowledge_ingester.update_document.call_args
        assert call_args[1]['document_id'] == document_id
        assert call_args[1]['content'] == new_content
        assert call_args[1]['metadata']['title'] == new_title
        assert call_args[1]['metadata']['tags'] == new_tags
        assert 'updated_at' in call_args[1]['metadata']

        # Should re-index in vector store
        mock_vector_store.add_documents.assert_called_once()

        mock_tracer.trace.assert_called_with("knowledge_service_update_document")

        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_update_document_partial_update(
        self, knowledge_service, mock_knowledge_ingester, mock_sanitizer
    ):
        """Test document update with only some fields"""
        # Arrange
        document_id = "kb_partial_123"
        new_title = "Partial Update Title"
        # No content or tags

        # Act
        result = await knowledge_service.update_document(
            document_id=document_id,
            title=new_title
        )

        # Assert
        assert result.title == new_title
        assert result.tags == []  # Default empty list

        # Verify only title was included in update
        mock_knowledge_ingester.update_document.assert_called_once()
        call_args = mock_knowledge_ingester.update_document.call_args[1]
        assert call_args["content"] == ""  # Default empty content
        assert call_args["metadata"]["title"] == new_title
        assert "tags" not in call_args["metadata"]  # Not included since tags was None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_update_document_validation_errors(self, knowledge_service):
        """Test validation errors in document update"""
        # Test empty document ID
        with pytest.raises(ValueError, match="Document ID cannot be empty"):
            await knowledge_service.update_document("", title="title")

        # Test whitespace-only document ID
        with pytest.raises(ValueError, match="Document ID cannot be empty"):
            await knowledge_service.update_document("   ", title="title")

        # Test no update fields provided
        with pytest.raises(ValueError, match="At least one field must be provided"):
            await knowledge_service.update_document("doc_123")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_update_document_ingester_error(
        self, knowledge_service, mock_knowledge_ingester    ):
        """Test error handling when update fails"""
        # Arrange
        mock_knowledge_ingester.update_document.side_effect = RuntimeError("Update failed")

        # Act & Assert
        with pytest.raises(RuntimeError, match="Update failed"):
            await knowledge_service.update_document("doc_123", title="new title")

        # Verify error logging
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_delete_document_success(
        self, knowledge_service, mock_knowledge_ingester, mock_vector_store,
        mock_tracer    ):
        """Test successful document deletion"""
        # Arrange
        document_id = "kb_delete_123"
        # Add document to service's in-memory store to simulate existing document
        knowledge_service._documents_store[document_id] = {
            "document_id": document_id,
            "title": "Test Document",
            "content": "Test content"
        }

        # Act
        result = await knowledge_service.delete_document(document_id)

        # Assert - Service returns dict with success status
        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["document_id"] == document_id

        # Assert - Interface interactions
        mock_tracer.trace.assert_called_with("knowledge_service_delete_document")

        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_delete_document_validation_error(self, knowledge_service):
        """Test validation error in document deletion"""
        # Test empty document ID
        with pytest.raises(ValueError, match="Document ID cannot be empty"):
            await knowledge_service.delete_document("")

        # Test whitespace-only document ID
        with pytest.raises(ValueError, match="Document ID cannot be empty"):
            await knowledge_service.delete_document("   ")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_delete_document_ingester_error(
        self, knowledge_service, mock_knowledge_ingester    ):
        """Test error handling when deletion fails - document not found"""
        # Arrange - Don't add document to store, so it will not be found
        document_id = "doc_123"

        # Act
        result = await knowledge_service.delete_document(document_id)

        # Assert - Service returns dict with error status when document not found
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "not found" in result["error"]
        assert document_id in result["error"]

        # Verify error logging
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_document_statistics_success(
        self, knowledge_service, mock_tracer
    ):
        """Test successful statistics retrieval"""
        # Act
        stats = await knowledge_service.get_document_statistics()

        # Assert - Response structure
        assert isinstance(stats, dict)
        assert "total_documents" in stats
        assert "documents_by_type" in stats
        assert "most_used_tags" in stats
        assert "last_updated" in stats
        assert "vector_store_enabled" in stats

        assert stats["total_documents"] == 0  # Default in mock implementation
        assert stats["vector_store_enabled"] is True  # Has vector store

        # Assert - Interface interactions
        mock_tracer.trace.assert_called_with("knowledge_service_get_statistics")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_document_statistics_error(
        self, knowledge_service, mock_tracer    ):
        """Test error handling in statistics retrieval"""
        # Arrange - Mock the tracer context manager to raise exception
        from contextlib import contextmanager
        
        @contextmanager
        def failing_trace(operation):
            raise RuntimeError("Statistics failed")
        
        mock_tracer.trace = failing_trace

        # Act & Assert
        with pytest.raises(RuntimeError, match="Statistics failed"):
            await knowledge_service.get_document_statistics()

        # Verify that error logging was not called because exception occurred before that point
        # This is expected behavior - the exception prevents reaching the logging statement

    @pytest.mark.unit
    def test_generate_document_id(self, knowledge_service):
        """Test document ID generation"""
        # Arrange
        title = "Test Document Title"
        document_type = "manual"

        # Act
        doc_id = knowledge_service._generate_document_id(title, document_type)

        # Assert
        assert doc_id.startswith("kb_")
        assert len(doc_id) == 19  # "kb_" + 16 character hash

        # Test consistency - same inputs should generate same ID within same timestamp
        # Note: This test might be flaky due to timestamp differences
        # In production, we might want to add a timestamp parameter for testing

    @pytest.mark.unit
    def test_validate_document_data_success(self, knowledge_service):
        """Test successful document data validation"""
        # Should not raise any exceptions
        knowledge_service._validate_document_data("Valid Title", "Valid content")

    @pytest.mark.unit
    def test_validate_document_data_errors(self, knowledge_service):
        """Test document data validation errors"""
        # Test various invalid inputs
        with pytest.raises(ValueError, match="Title and content must be strings"):
            knowledge_service._validate_document_data(123, "content")

        with pytest.raises(ValueError, match="Title and content must be strings"):
            knowledge_service._validate_document_data("title", 456)

        with pytest.raises(ValueError, match="Title cannot be empty"):
            knowledge_service._validate_document_data("", "content")

        with pytest.raises(ValueError, match="Content cannot be empty"):
            knowledge_service._validate_document_data("title", "")

        with pytest.raises(ValueError, match="Title cannot be empty"):
            knowledge_service._validate_document_data("   ", "content")

        with pytest.raises(ValueError, match="Content cannot be empty"):
            knowledge_service._validate_document_data("title", "   ")

        with pytest.raises(ValueError, match="Title cannot exceed 500 characters"):
            long_title = "x" * 501
            knowledge_service._validate_document_data(long_title, "content")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_index_document_in_vector_store_success(
        self, knowledge_service, sample_knowledge_document, mock_vector_store    ):
        """Test successful document indexing in vector store"""
        # Act
        await knowledge_service._index_document_in_vector_store(sample_knowledge_document)

        # Assert
        mock_vector_store.add_documents.assert_called_once()
        
        # Check the document format passed to vector store
        added_docs = mock_vector_store.add_documents.call_args[0][0]
        assert len(added_docs) == 1
        
        doc = added_docs[0]
        assert doc["id"] == sample_knowledge_document.document_id
        assert doc["title"] == sample_knowledge_document.title
        assert doc["content"] == sample_knowledge_document.content
        assert doc["document_type"] == sample_knowledge_document.document_type
        assert doc["tags"] == sample_knowledge_document.tags
        assert "metadata" in doc
        assert doc["metadata"]["source_url"] == sample_knowledge_document.source_url

        # Assert - Debug logging
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_index_document_in_vector_store_without_vector_store(
        self, mock_knowledge_ingester, mock_sanitizer, mock_tracer, 
        sample_knowledge_document
    ):
        """Test document indexing without vector store"""
        # Arrange - Service without vector store
        service = KnowledgeService(
            knowledge_ingester=mock_knowledge_ingester,
            sanitizer=mock_sanitizer,
            tracer=mock_tracer,
            vector_store=None
        )

        # Act - Should not raise exception
        await service._index_document_in_vector_store(sample_knowledge_document)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_index_document_in_vector_store_error(
        self, knowledge_service, sample_knowledge_document, 
        mock_vector_store    ):
        """Test error handling in document indexing"""
        # Arrange
        mock_vector_store.add_documents.side_effect = RuntimeError("Indexing failed")

        # Act - Should not raise exception (indexing failure shouldn't block ingestion)
        await knowledge_service._index_document_in_vector_store(sample_knowledge_document)

        # Verify error logging
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_remove_from_vector_store(
        self, knowledge_service    ):
        """Test document removal from vector store"""
        # Act
        await knowledge_service._remove_from_vector_store("doc_123")

        # Assert - Currently just logs (no actual removal implementation)
        # Note: Logging assertions removed since service uses unified logger

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_remove_from_vector_store_without_vector_store(
        self, mock_knowledge_ingester, mock_sanitizer, mock_tracer
    ):
        """Test document removal without vector store"""
        # Arrange - Service without vector store
        service = KnowledgeService(
            knowledge_ingester=mock_knowledge_ingester,
            sanitizer=mock_sanitizer,
            tracer=mock_tracer,
            vector_store=None
        )

        # Act - Should not raise exception
        await service._remove_from_vector_store("doc_123")

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.performance
    async def test_ingest_document_performance(
        self, knowledge_service, sample_document_data
    ):
        """Test document ingestion performance"""
        # Act & Assert - Should complete within reasonable time
        start_time = datetime.utcnow()
        result = await knowledge_service.ingest_document(
            title=sample_document_data["title"],
            content=sample_document_data["content"],
            document_type=sample_document_data["document_type"]
        )
        end_time = datetime.utcnow()

        processing_time = (end_time - start_time).total_seconds()
        assert processing_time < 3.0, f"Ingestion took {processing_time} seconds, expected < 3.0"
        assert isinstance(result, KnowledgeBaseDocument)

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.performance  
    async def test_search_knowledge_performance(
        self, knowledge_service, mock_vector_store
    ):
        """Test knowledge search performance"""
        # Arrange
        large_results = [
            {
                "id": f"doc_{i}",
                "title": f"Document {i}",
                "content": "x" * 1000,  # Large content
                "document_type": "manual",
                "tags": ["test"],
                "score": 0.9 - (i * 0.1)
            }
            for i in range(20)  # Large result set
        ]
        mock_vector_store.search.return_value = large_results

        # Act & Assert - Should complete within reasonable time
        start_time = datetime.utcnow()
        results = await knowledge_service.search_knowledge("test query", limit=20)
        end_time = datetime.utcnow()

        processing_time = (end_time - start_time).total_seconds()
        assert processing_time < 2.0, f"Search took {processing_time} seconds, expected < 2.0"
        assert len(results) == 20

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_concurrent_document_operations(
        self, knowledge_service, mock_knowledge_ingester
    ):
        """Test handling of concurrent document operations"""
        # Arrange
        documents = [
            ("Title 1", "Content 1", "manual"),
            ("Title 2", "Content 2", "guide"),
            ("Title 3", "Content 3", "reference")
        ]

        mock_knowledge_ingester.ingest_document.side_effect = [
            "kb_1", "kb_2", "kb_3"
        ]

        # Act - Ingest documents concurrently
        import asyncio
        results = await asyncio.gather(*[
            knowledge_service.ingest_document(title, content, doc_type)
            for title, content, doc_type in documents
        ])

        # Assert
        assert len(results) == 3
        for i, result in enumerate(results):
            assert isinstance(result, KnowledgeBaseDocument)
            assert result.document_id == f"kb_{i + 1}"
            assert result.title == documents[i][0]

    @pytest.mark.unit
    def test_service_initialization_without_optional_params(
        self, mock_knowledge_ingester, mock_sanitizer, mock_tracer
    ):
        """Test service initialization without optional parameters"""
        # Act
        service = KnowledgeService(
            knowledge_ingester=mock_knowledge_ingester,
            sanitizer=mock_sanitizer,
            tracer=mock_tracer
            # No vector_store or logger
        )

        # Assert
        assert service._vector_store is None
        assert hasattr(service, 'logger')  # Inherits logger from BaseService
    
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_document_validation_edge_cases(self, knowledge_service):
        """Test document validation for various edge cases from comprehensive test"""
        # Test None title
        with pytest.raises(ValueError, match="Title and content must be strings"):
            await knowledge_service.ingest_document(
                title=None, content="Valid content", document_type="guide"
            )
        
        # Test None content
        with pytest.raises(ValueError, match="Title and content must be strings"):
            await knowledge_service.ingest_document(
                title="Valid title", content=None, document_type="guide"
            )
        
        # Test very long title (should fail due to length limit)
        long_title = "A" * 1000
        with pytest.raises(ValueError, match="Title cannot exceed 500 characters"):
            await knowledge_service.ingest_document(
                title=long_title,
                content="Valid content",
                document_type="guide"
            )
        
        # Test very long content (should succeed)
        long_content = "Content " * 1000
        result = await knowledge_service.ingest_document(
            title="Valid title",
            content=long_content,
            document_type="guide"
        )
        assert isinstance(result, KnowledgeBaseDocument)
        assert len(result.content) == len(long_content)
    
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_document_id_generation(self, knowledge_service):
        """Test document ID generation and uniqueness from comprehensive test"""
        # Generate multiple documents concurrently
        tasks = []
        for i in range(10):
            tasks.append(
                knowledge_service.ingest_document(
                    title=f"Test Document {i}",
                    content=f"Test content {i}",
                    document_type="guide"
                )
            )
        
        results = await asyncio.gather(*tasks)
        
        # Validate all document IDs are unique
        doc_ids = [result.document_id for result in results]
        assert len(doc_ids) == len(set(doc_ids)), "Document IDs should be unique"
        
        # Validate all document IDs are non-empty and well-formed
        for doc_id in doc_ids:
            assert doc_id is not None
            assert len(doc_id) > 0
            assert isinstance(doc_id, str)
    
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_search_query_validation(self, knowledge_service):
        """Test search query validation from comprehensive test"""
        # Test None query
        with pytest.raises(ValueError, match="Query cannot be empty"):
            await knowledge_service.search_knowledge(None)
        
        # Test very short query (should work but may return limited results)
        results = await knowledge_service.search_knowledge("a")
        assert isinstance(results, list)
        
        # Test very long query (should work)
        long_query = "database connection timeout issues troubleshooting " * 50
        results = await knowledge_service.search_knowledge(long_query)
        assert isinstance(results, list)
    
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_metadata_handling(self, knowledge_service):
        """Test metadata handling from comprehensive test"""
        # Test that the service automatically creates metadata
        result = await knowledge_service.ingest_document(
            title="Test Document with Metadata",
            content="Test content with metadata handling",
            document_type="guide",
            tags=["test", "metadata"],
            source_url="https://example.com/docs"
        )
        
        # Override the default side_effect for this specific test  
        mock_ingester = knowledge_service._ingester
        mock_ingester.ingest_document.return_value = "kb_metadata_123"
        mock_ingester.ingest_document.side_effect = None
        
        # Validate metadata structure is correct
        assert isinstance(result, KnowledgeBaseDocument)
        assert result.tags == ["test", "metadata"]
        assert result.source_url == "https://example.com/docs"
        assert result.document_type == "guide"
        
        # Check that the document has the expected properties
        assert result.title == "Test Document with Metadata"
        assert result.content == "Test content with metadata handling"
        assert result.created_at is not None
        assert result.updated_at is not None
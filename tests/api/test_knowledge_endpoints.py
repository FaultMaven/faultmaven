"""Rebuilt Knowledge API Endpoint Tests

Tests complete document ingestion and retrieval workflows with real HTTP processing.
Focus on real vector search and knowledge base integration validation.
"""

import io
import asyncio
from typing import Tuple, Dict, Any, List

import pytest
from httpx import AsyncClient


class TestKnowledgeAPIEndpointsRebuilt:
    """Knowledge API tests using real HTTP document processing workflows."""
    
    @pytest.mark.asyncio
    async def test_complete_document_ingestion_workflow(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str],
        response_validator,
        performance_tracker
    ):
        """Test complete document upload and ingestion workflow."""
        
        filename, content, content_type = sample_document
        
        # Real document upload with metadata
        with performance_tracker.time_request("document_ingestion"):
            response = await client.post(
                "/api/v1/knowledge/documents",
                files={"file": (filename, io.BytesIO(content), content_type)},
                data={
                    "title": "Database Troubleshooting Guide",
                    "document_type": "guide",
                    "category": "troubleshooting",
                    "tags": "database,connection,timeout",
                    "description": "Comprehensive guide for database connection issues"
                }
            )
        
        # Validate real HTTP response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Validate ingestion results
        data = response.json()
        required_fields = ["job_id", "document_id", "status", "metadata"]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Validate document processing
        assert data["status"] in ["processing", "queued", "completed"]
        assert data["document_id"] is not None
        assert data["job_id"] is not None
        
        # Validate metadata preservation
        metadata = data["metadata"]
        assert metadata["title"] == "Database Troubleshooting Guide"
        assert metadata["document_type"] == "guide"
        assert metadata["category"] == "troubleshooting"
        
        # Performance validation
        performance_tracker.assert_performance_target("document_ingestion", 5.0)
        
        return data["document_id"], data["job_id"]
    
    @pytest.mark.asyncio
    async def test_document_processing_status_tracking(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str]
    ):
        """Test tracking document processing status."""
        
        filename, content, content_type = sample_document
        
        # Upload document
        upload_response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": (filename, io.BytesIO(content), content_type)},
            data={
                "title": "Status Tracking Test",
                "document_type": "guide"
            }
        )
        
        assert upload_response.status_code == 200
        upload_data = upload_response.json()
        job_id = upload_data["job_id"]
        
        # Check processing status
        status_response = await client.get(f"/api/v1/knowledge/jobs/{job_id}")
        
        assert status_response.status_code == 200
        status_data = status_response.json()
        
        # Validate status structure
        required_fields = ["job_id", "status", "created_at", "document_id"]
        for field in required_fields:
            assert field in status_data, f"Missing status field: {field}"
        
        assert status_data["status"] in [
            "queued", "processing", "completed", "failed"
        ]
        assert status_data["job_id"] == job_id
        
        # If completed, should have processing results
        if status_data["status"] == "completed":
            assert "processing_results" in status_data
            results = status_data["processing_results"]
            assert "chunks_created" in results
            assert "embeddings_generated" in results
    
    @pytest.mark.asyncio
    async def test_knowledge_search_workflow(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str],
        performance_tracker
    ):
        """Test complete knowledge search workflow with real vector similarity."""
        
        filename, content, content_type = sample_document
        
        # Upload document first for search testing
        upload_response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": (filename, io.BytesIO(content), content_type)},
            data={
                "title": "Search Test Document",
                "document_type": "reference",
                "category": "database"
            }
        )
        
        assert upload_response.status_code == 200
        
        # Wait a moment for processing (in real implementation, would check status)
        await asyncio.sleep(0.1)
        
        # Perform knowledge search
        search_queries = [
            "database connection issues",
            "connection pool configuration",
            "timeout troubleshooting",
            "network connectivity problems"
        ]
        
        # Time the entire database search operation
        with performance_tracker.time_request("search_database"):
            for query in search_queries:
                search_response = await client.post(
                    "/api/v1/knowledge/search",
                    json={
                        "query": query,
                        "limit": 5,
                        "include_metadata": True,
                        "similarity_threshold": 0.7
                    }
                )
                
                assert search_response.status_code == 200
                search_data = search_response.json()
                
                # Validate search results structure
                assert "results" in search_data
                assert "query" in search_data
                assert "total_results" in search_data
                assert search_data["query"] == query
                
                # Validate individual search results
                results = search_data["results"]
                assert isinstance(results, list)
                
                for result in results:
                    required_fields = [
                        "document_id", "content", "metadata", "similarity_score"
                    ]
                    for field in required_fields:
                        assert field in result, f"Missing result field: {field}"
                    
                    # Validate similarity score
                    assert 0.0 <= result["similarity_score"] <= 1.0
                    
                    # Validate metadata structure
                    metadata = result["metadata"]
                    assert "title" in metadata
                    assert "document_type" in metadata
        
        # Validate search performance
        performance_tracker.assert_performance_target("search_database", 2.0)
    
    @pytest.mark.asyncio
    async def test_advanced_search_features(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test advanced search features including filters and ranking."""
        
        # Upload multiple test documents with different categories
        test_documents = [
            {
                "filename": "db_guide.md",
                "content": b"# Database Guide\nConnection pooling and timeout configuration",
                "title": "Database Administration Guide",
                "category": "database",
                "document_type": "guide",
                "priority": "high"
            },
            {
                "filename": "network_ref.md", 
                "content": b"# Network Reference\nFirewall rules and connectivity testing",
                "title": "Network Reference Manual",
                "category": "network",
                "document_type": "reference",
                "priority": "medium"
            },
            {
                "filename": "app_troubleshoot.md",
                "content": b"# Application Troubleshooting\nCommon application errors and solutions",
                "title": "App Troubleshooting Handbook",
                "category": "application",
                "document_type": "troubleshooting",
                "priority": "high"
            }
        ]
        
        # Upload all test documents
        uploaded_docs = []
        for doc in test_documents:
            response = await client.post(
                "/api/v1/knowledge/documents",
                files={"file": (doc["filename"], io.BytesIO(doc["content"]), "text/markdown")},
                data={
                    "title": doc["title"],
                    "document_type": doc["document_type"],
                    "category": doc["category"],
                    "priority": doc["priority"]
                }
            )
            
            assert response.status_code == 200
            uploaded_docs.append(response.json()["document_id"])
        
        # Test filtered search by category
        with performance_tracker.time_request("filtered_search"):
            filtered_response = await client.post(
                "/api/v1/knowledge/search",
                json={
                    "query": "configuration and setup",
                    "filters": {
                        "category": "database",
                        "document_type": "guide"
                    },
                    "limit": 10
                }
            )
        
        assert filtered_response.status_code == 200
        filtered_data = filtered_response.json()
        
        # Should only return database guides
        for result in filtered_data["results"]:
            metadata = result["metadata"]
            assert metadata.get("category") == "database"
            assert metadata.get("document_type") == "guide"
        
        # Test priority-based ranking
        priority_response = await client.post(
            "/api/v1/knowledge/search",
            json={
                "query": "troubleshooting problems",
                "rank_by": "priority",
                "limit": 10
            }
        )
        
        assert priority_response.status_code == 200
        priority_data = priority_response.json()
        
        # Results should include priority information
        for result in priority_data["results"]:
            assert "metadata" in result
            # High priority items should rank higher (implementation dependent)
        
        performance_tracker.assert_performance_target("filtered_search", 3.0)
    
    @pytest.mark.asyncio
    async def test_document_retrieval_and_management(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str]
    ):
        """Test document retrieval and management operations."""
        
        filename, content, content_type = sample_document
        
        # Upload document
        upload_response = await client.post(
            "/api/v1/knowledge/documents", 
            files={"file": (filename, io.BytesIO(content), content_type)},
            data={
                "title": "Management Test Document",
                "document_type": "manual",
                "category": "testing"
            }
        )
        
        assert upload_response.status_code == 200
        document_id = upload_response.json()["document_id"]
        
        # Retrieve specific document
        get_response = await client.get(f"/api/v1/knowledge/documents/{document_id}")
        
        assert get_response.status_code == 200
        doc_data = get_response.json()
        
        # Validate document structure
        required_fields = [
            "document_id", "title", "document_type", "category", 
            "created_at", "status", "metadata"
        ]
        for field in required_fields:
            assert field in doc_data, f"Missing document field: {field}"
        
        assert doc_data["document_id"] == document_id
        assert doc_data["title"] == "Management Test Document"
        assert doc_data["document_type"] == "manual"
        assert doc_data["category"] == "testing"
        
        # List all documents
        list_response = await client.get("/api/v1/knowledge/documents")
        
        assert list_response.status_code == 200
        list_data = list_response.json()
        
        assert "documents" in list_data
        assert "total_count" in list_data
        assert isinstance(list_data["documents"], list)
        
        # Should include our uploaded document
        our_doc = next(
            (doc for doc in list_data["documents"] if doc["document_id"] == document_id),
            None
        )
        assert our_doc is not None
        assert our_doc["title"] == "Management Test Document"
    
    @pytest.mark.asyncio
    async def test_document_update_workflow(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str]
    ):
        """Test document update and versioning."""
        
        filename, content, content_type = sample_document
        
        # Upload initial document
        upload_response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": (filename, io.BytesIO(content), content_type)},
            data={
                "title": "Update Test Document",
                "document_type": "guide",
                "version": "1.0"
            }
        )
        
        assert upload_response.status_code == 200
        document_id = upload_response.json()["document_id"]
        
        # Update document metadata
        update_response = await client.put(
            f"/api/v1/knowledge/documents/{document_id}",
            json={
                "title": "Updated Test Document",
                "document_type": "reference",
                "version": "1.1",
                "description": "Updated with new information"
            }
        )
        
        assert update_response.status_code == 200
        update_data = update_response.json()
        
        # Validate update results
        assert update_data["document_id"] == document_id
        assert update_data["title"] == "Updated Test Document"
        assert update_data["document_type"] == "reference"
        assert update_data["version"] == "1.1"
        
        # Verify changes persisted
        verify_response = await client.get(f"/api/v1/knowledge/documents/{document_id}")
        assert verify_response.status_code == 200
        verify_data = verify_response.json()
        
        assert verify_data["title"] == "Updated Test Document"
        assert verify_data["document_type"] == "reference"
    
    @pytest.mark.asyncio
    async def test_document_deletion_workflow(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str]
    ):
        """Test document deletion and cleanup."""
        
        filename, content, content_type = sample_document
        
        # Upload document to delete
        upload_response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": (filename, io.BytesIO(content), content_type)},
            data={
                "title": "Document To Delete",
                "document_type": "temporary"
            }
        )
        
        assert upload_response.status_code == 200
        document_id = upload_response.json()["document_id"]
        
        # Verify document exists
        check_response = await client.get(f"/api/v1/knowledge/documents/{document_id}")
        assert check_response.status_code == 200
        
        # Delete document
        delete_response = await client.delete(f"/api/v1/knowledge/documents/{document_id}")
        
        assert delete_response.status_code == 200
        delete_data = delete_response.json()
        
        assert delete_data["success"] is True
        assert delete_data["document_id"] == document_id
        
        # Verify document no longer accessible
        verify_response = await client.get(f"/api/v1/knowledge/documents/{document_id}")
        assert verify_response.status_code == 404
        
        # Verify document removed from search
        search_response = await client.post(
            "/api/v1/knowledge/search",
            json={"query": "Document To Delete"}
        )
        
        assert search_response.status_code == 200
        search_data = search_response.json()
        
        # Should not find the deleted document
        matching_results = [
            result for result in search_data["results"]
            if result["document_id"] == document_id
        ]
        assert len(matching_results) == 0
    
    @pytest.mark.asyncio
    async def test_bulk_document_operations(
        self,
        client: AsyncClient,
        performance_tracker
    ):
        """Test bulk document operations and batch processing."""
        
        # Create multiple documents for bulk operations
        test_docs = [
            ("bulk1.md", b"# Bulk Document 1\nContent for bulk testing", "Bulk Test Doc 1"),
            ("bulk2.md", b"# Bulk Document 2\nMore bulk content", "Bulk Test Doc 2"), 
            ("bulk3.md", b"# Bulk Document 3\nAdditional bulk content", "Bulk Test Doc 3"),
        ]
        
        # Upload documents in parallel
        with performance_tracker.time_request("bulk_upload"):
            upload_tasks = []
            for filename, content, title in test_docs:
                task = client.post(
                    "/api/v1/knowledge/documents",
                    files={"file": (filename, io.BytesIO(content), "text/markdown")},
                    data={
                        "title": title,
                        "document_type": "bulk_test",
                        "category": "testing"
                    }
                )
                upload_tasks.append(task)
            
            responses = await asyncio.gather(*upload_tasks)
        
        # Validate all uploads succeeded
        document_ids = []
        for response in responses:
            assert response.status_code == 200
            document_ids.append(response.json()["document_id"])
        
        # Test bulk metadata update
        bulk_update_response = await client.post(
            "/api/v1/knowledge/documents/bulk-update",
            json={
                "document_ids": document_ids,
                "updates": {
                    "category": "bulk_updated",
                    "status": "reviewed"
                }
            }
        )
        
        assert bulk_update_response.status_code == 200
        bulk_data = bulk_update_response.json()
        
        assert bulk_data["updated_count"] == len(document_ids)
        assert bulk_data["success"] is True
        
        # Verify updates applied
        for doc_id in document_ids:
            verify_response = await client.get(f"/api/v1/knowledge/documents/{doc_id}")
            assert verify_response.status_code == 200
            doc_data = verify_response.json()
            
            assert doc_data["category"] == "bulk_updated"
            assert doc_data.get("status") == "reviewed"
        
        # Test bulk deletion
        bulk_delete_response = await client.post(
            "/api/v1/knowledge/documents/bulk-delete",
            json={"document_ids": document_ids}
        )
        
        assert bulk_delete_response.status_code == 200
        delete_data = bulk_delete_response.json()
        
        assert delete_data["deleted_count"] == len(document_ids)
        assert delete_data["success"] is True
        
        # Verify all documents deleted
        for doc_id in document_ids:
            verify_response = await client.get(f"/api/v1/knowledge/documents/{doc_id}")
            assert verify_response.status_code == 404
        
        performance_tracker.assert_performance_target("bulk_upload", 10.0)
    
    @pytest.mark.asyncio
    async def test_knowledge_analytics_and_insights(
        self,
        client: AsyncClient
    ):
        """Test knowledge base analytics and insights endpoints."""
        
        # Get knowledge base statistics
        stats_response = await client.get("/api/v1/knowledge/stats")
        
        assert stats_response.status_code == 200
        stats_data = stats_response.json()
        
        # Validate statistics structure
        expected_stats = [
            "total_documents", "document_types", "categories",
            "total_chunks", "avg_chunk_size", "storage_used"
        ]
        
        for stat in expected_stats:
            assert stat in stats_data, f"Missing statistic: {stat}"
        
        assert isinstance(stats_data["total_documents"], int)
        assert isinstance(stats_data["document_types"], dict)
        assert isinstance(stats_data["categories"], dict)
        
        # Test search analytics
        analytics_response = await client.get("/api/v1/knowledge/analytics/search")
        
        assert analytics_response.status_code == 200
        analytics_data = analytics_response.json()
        
        # Validate analytics structure
        expected_analytics = [
            "popular_queries", "search_volume", "avg_response_time",
            "hit_rate", "category_distribution"
        ]
        
        for analytic in expected_analytics:
            assert analytic in analytics_data, f"Missing analytic: {analytic}"


class TestKnowledgeAPIErrorScenarios:
    """Test error scenarios and edge cases for knowledge API."""
    
    @pytest.mark.asyncio
    async def test_invalid_file_types(self, client: AsyncClient):
        """Test handling of invalid file types."""
        
        # Binary file that shouldn't be processed
        binary_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00'
        
        response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": ("image.png", io.BytesIO(binary_content), "image/png")},
            data={
                "title": "Invalid File Type",
                "document_type": "image"
            }
        )
        
        # Should reject inappropriate file types
        assert response.status_code in [400, 415, 422]
        error_data = response.json()
        assert "detail" in error_data
    
    @pytest.mark.asyncio
    async def test_missing_document_metadata(self, client: AsyncClient):
        """Test handling of missing required metadata."""
        
        response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": ("test.md", io.BytesIO(b"test content"), "text/markdown")}
            # Missing required data fields
        )
        
        assert response.status_code == 422
        error_data = response.json()
        assert "detail" in error_data
    
    @pytest.mark.asyncio
    async def test_nonexistent_document_operations(self, client: AsyncClient):
        """Test operations on non-existent documents."""
        
        fake_document_id = "non-existent-doc-id-12345"
        
        # Get non-existent document
        get_response = await client.get(f"/api/v1/knowledge/documents/{fake_document_id}")
        assert get_response.status_code == 404
        
        # Update non-existent document
        update_response = await client.put(
            f"/api/v1/knowledge/documents/{fake_document_id}",
            json={"title": "Updated Title"}
        )
        assert update_response.status_code == 404
        
        # Delete non-existent document
        delete_response = await client.delete(f"/api/v1/knowledge/documents/{fake_document_id}")
        assert delete_response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_invalid_search_parameters(self, client: AsyncClient):
        """Test search with invalid parameters."""
        
        # Empty query
        response = await client.post(
            "/api/v1/knowledge/search",
            json={"query": ""}
        )
        assert response.status_code == 422
        
        # Invalid similarity threshold
        response = await client.post(
            "/api/v1/knowledge/search",
            json={
                "query": "test",
                "similarity_threshold": 1.5  # Should be <= 1.0
            }
        )
        assert response.status_code == 422
        
        # Invalid limit
        response = await client.post(
            "/api/v1/knowledge/search",
            json={
                "query": "test", 
                "limit": -1  # Should be positive
            }
        )
        assert response.status_code == 422
    
    @pytest.mark.asyncio
    async def test_concurrent_document_modifications(
        self,
        client: AsyncClient,
        sample_document: Tuple[str, bytes, str]
    ):
        """Test concurrent modifications to same document."""
        
        filename, content, content_type = sample_document
        
        # Upload document
        upload_response = await client.post(
            "/api/v1/knowledge/documents",
            files={"file": (filename, io.BytesIO(content), content_type)},
            data={
                "title": "Concurrent Test Document",
                "document_type": "test"
            }
        )
        
        assert upload_response.status_code == 200
        document_id = upload_response.json()["document_id"]
        
        # Attempt concurrent updates
        update_tasks = [
            client.put(
                f"/api/v1/knowledge/documents/{document_id}",
                json={"title": f"Updated Title {i}"}
            )
            for i in range(3)
        ]
        
        responses = await asyncio.gather(*update_tasks, return_exceptions=True)
        
        # At least one should succeed, others might conflict
        success_count = sum(
            1 for r in responses 
            if not isinstance(r, Exception) and r.status_code == 200
        )
        
        assert success_count >= 1
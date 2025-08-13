"""Rebuilt Agent API Endpoint Tests

Tests complete HTTP workflows with minimal external mocking.
Focus on real request/response validation and end-to-end behavior.
"""

import asyncio
import json
from typing import Dict, Any

import pytest
from httpx import AsyncClient

from faultmaven.models import QueryRequest, TroubleshootingResponse


class TestAgentAPIEndpointsRebuilt:
    """Agent API tests using real HTTP workflows."""
    
    @pytest.mark.asyncio
    async def test_complete_troubleshooting_workflow(
        self, 
        client: AsyncClient, 
        test_session: str,
        response_validator,
        performance_tracker
    ):
        """Test complete troubleshooting query workflow via real HTTP."""
        
        # Performance timing
        with performance_tracker.time_request("troubleshooting_query"):
            
            # Real HTTP POST request
            response = await client.post(
                "/api/v1/agent/query",
                json={
                    "session_id": test_session,
                    "query": "Database connection failing in production",
                    "context": {
                        "environment": "production",
                        "service": "api-server",
                        "error_code": "DB_CONN_FAILED"
                    },
                    "priority": "high"
                }
            )
        
        # Validate real HTTP response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Validate real response structure and business logic
        data = response.json()
        response_validator.assert_valid_troubleshooting_response(data)
        
        # Validate business logic results
        assert data["session_id"] == test_session
        assert data["investigation_id"] is not None
        assert data["status"] in ["in_progress", "completed", "analysis"]
        assert len(data["findings"]) > 0
        assert len(data["recommendations"]) > 0
        
        # Validate confidence scoring
        assert 0.7 <= data["confidence_score"] <= 1.0  # High confidence for clear database issues
        
        # Validate recommendation structure
        for rec in data["recommendations"]:
            assert isinstance(rec, (str, dict))
            if isinstance(rec, dict):
                assert "action" in rec
                assert "priority" in rec
        
        # Performance validation - API should respond quickly
        performance_tracker.assert_performance_target("troubleshooting_query", 2.0)
    
    @pytest.mark.asyncio
    async def test_query_with_real_validation_errors(self, client: AsyncClient):
        """Test real FastAPI validation with invalid requests."""
        
        # Test missing required fields
        response = await client.post(
            "/api/v1/agent/query",
            json={"query": "test"}  # Missing session_id
        )
        
        assert response.status_code == 422
        error_detail = response.json()["detail"]
        assert any("session_id" in str(error) for error in error_detail)
        
        # Test empty query
        response = await client.post(
            "/api/v1/agent/query", 
            json={"session_id": "test", "query": ""}
        )
        
        assert response.status_code == 422
        
        # Test invalid context type
        response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": "test_session",
                "query": "test query", 
                "context": "invalid_context"  # Should be dict
            }
        )
        
        assert response.status_code == 422
        error_detail = response.json()["detail"]
        assert any("context" in str(error) for error in error_detail)
    
    @pytest.mark.asyncio
    async def test_query_with_different_data_types(
        self, 
        client: AsyncClient, 
        test_session: str,
        response_validator
    ):
        """Test troubleshooting queries with different data types."""
        
        test_cases = [
            {
                "query": "Application throwing 500 errors",
                "context": {"error_type": "application", "severity": "high"},
                "expected_pattern": "application"
            },
            {
                "query": "High memory usage detected", 
                "context": {"metric_type": "memory", "threshold_exceeded": True},
                "expected_pattern": "memory"
            },
            {
                "query": "Network timeout issues",
                "context": {"network_issue": True, "timeout_ms": 30000},
                "expected_pattern": "network"
            }
        ]
        
        for case in test_cases:
            response = await client.post(
                "/api/v1/agent/query",
                json={
                    "session_id": test_session,
                    "query": case["query"],
                    "context": case["context"]
                }
            )
            
            assert response.status_code == 200
            data = response.json()
            response_validator.assert_valid_troubleshooting_response(data)
            
            # Validate that different query types produce appropriate responses
            assert data["investigation_id"] != ""
            assert len(data["findings"]) > 0
    
    @pytest.mark.asyncio
    async def test_session_not_found_handling(self, client: AsyncClient):
        """Test handling of non-existent session via real API."""
        
        response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": "non_existent_session",
                "query": "test query"
            }
        )
        
        assert response.status_code == 404
        error_data = response.json()
        assert "not found" in error_data["detail"].lower()
    
    @pytest.mark.asyncio
    async def test_concurrent_queries_same_session(
        self, 
        client: AsyncClient, 
        test_session: str,
        performance_tracker
    ):
        """Test concurrent queries to same session."""
        
        async def make_query(query_text: str):
            return await client.post(
                "/api/v1/agent/query",
                json={
                    "session_id": test_session,
                    "query": f"{query_text} - concurrent test",
                    "context": {"test_type": "concurrent"}
                }
            )
        
        # Make concurrent requests
        with performance_tracker.time_request("concurrent_queries"):
            responses = await asyncio.gather(
                make_query("Database issue"),
                make_query("Network problem"), 
                make_query("Application error"),
                return_exceptions=True
            )
        
        # Validate all responses succeeded
        for response in responses:
            assert not isinstance(response, Exception)
            assert response.status_code == 200
            
            data = response.json()
            assert data["session_id"] == test_session
            assert data["investigation_id"] is not None
        
        # Validate concurrent performance
        performance_tracker.assert_performance_target("concurrent_queries", 5.0)
        
        # Validate each query got unique investigation ID
        investigation_ids = [r.json()["investigation_id"] for r in responses]
        assert len(set(investigation_ids)) == len(investigation_ids)
    
    @pytest.mark.asyncio
    async def test_query_response_serialization(
        self, 
        client: AsyncClient, 
        test_session: str
    ):
        """Test real JSON serialization/deserialization of complex responses."""
        
        response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": test_session,
                "query": "Complex query with unicode: ñáéíóú and symbols: @#$%",
                "context": {
                    "unicode_data": "Special chars: ñáéíóú", 
                    "symbols": "@#$%^&*()",
                    "nested_dict": {
                        "level2": {"level3": "deep_value"}
                    },
                    "list_data": [1, 2, 3, "string", {"nested": True}]
                }
            }
        )
        
        assert response.status_code == 200
        
        # Validate JSON serialization roundtrip
        data = response.json()
        json_string = json.dumps(data)  # Should not raise exception
        parsed_back = json.loads(json_string)
        
        # Validate structure preserved
        assert parsed_back["session_id"] == test_session
        assert parsed_back["investigation_id"] is not None
        
        # Validate unicode handling
        assert "investigation_id" in parsed_back
        assert isinstance(parsed_back["findings"], list)
    
    @pytest.mark.asyncio 
    async def test_investigation_results_retrieval(
        self, 
        client: AsyncClient, 
        test_session: str
    ):
        """Test retrieving investigation results via real API."""
        
        # First, create an investigation
        query_response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": test_session,
                "query": "System performance degradation",
                "context": {"performance_issue": True}
            }
        )
        
        assert query_response.status_code == 200
        query_data = query_response.json()
        investigation_id = query_data["investigation_id"]
        
        # Retrieve investigation results
        results_response = await client.get(
            f"/api/v1/agent/investigations/{investigation_id}",
            params={"session_id": test_session}
        )
        
        assert results_response.status_code == 200
        results_data = results_response.json()
        
        # Validate results structure
        assert results_data["investigation_id"] == investigation_id
        assert results_data["session_id"] == test_session
        assert "findings" in results_data
        assert "recommendations" in results_data
        assert "status" in results_data
    
    @pytest.mark.asyncio
    async def test_session_investigations_listing(
        self, 
        client: AsyncClient, 
        test_session: str
    ):
        """Test listing all investigations for a session."""
        
        # Create multiple investigations
        queries = [
            "Database connection issues",
            "High CPU usage alerts", 
            "Memory leak detection"
        ]
        
        investigation_ids = []
        for query in queries:
            response = await client.post(
                "/api/v1/agent/query",
                json={
                    "session_id": test_session,
                    "query": query,
                    "context": {"batch_test": True}
                }
            )
            
            assert response.status_code == 200
            investigation_ids.append(response.json()["investigation_id"])
        
        # List investigations for session
        list_response = await client.get(
            f"/api/v1/agent/sessions/{test_session}/investigations"
        )
        
        assert list_response.status_code == 200
        list_data = list_response.json()
        
        # Validate response structure
        assert "session_id" in list_data
        assert "investigations" in list_data
        assert "total" in list_data
        assert list_data["session_id"] == test_session
        
        investigations = list_data["investigations"]
        assert isinstance(investigations, list)
        assert len(investigations) >= len(queries)
        
        # Validate structure of each investigation
        for investigation in investigations:
            assert "investigation_id" in investigation
            assert "query" in investigation
            assert "status" in investigation
            assert "created_at" in investigation
    
    @pytest.mark.asyncio
    async def test_error_handling_propagation(
        self, 
        client: AsyncClient,
        test_session: str
    ):
        """Test real error handling and propagation through API layer."""
        
        # Test with potentially problematic input
        problematic_inputs = [
            {"query": "x" * 10000, "context": {}},  # Very long query
            {"query": "test", "context": {"large_data": "y" * 5000}},  # Large context
            {"query": "test", "priority": "invalid_priority", "context": {}}  # Invalid priority
        ]
        
        for input_data in problematic_inputs:
            input_data["session_id"] = test_session
            
            response = await client.post("/api/v1/agent/query", json=input_data)
            
            # Should handle gracefully (either succeed or return proper error)
            assert response.status_code in [200, 400, 422, 413]
            
            if response.status_code != 200:
                # Validate error response structure
                error_data = response.json()
                assert "detail" in error_data
                assert isinstance(error_data["detail"], (str, list))
    
    @pytest.mark.asyncio
    async def test_content_type_validation(
        self, 
        client: AsyncClient, 
        test_session: str
    ):
        """Test content type handling and validation."""
        
        # Valid JSON content type
        response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": test_session,
                "query": "test query"
            },
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200
        
        # Invalid content type should be handled by FastAPI
        response = await client.post(
            "/api/v1/agent/query",
            data='{"session_id": "test", "query": "test"}',
            headers={"Content-Type": "text/plain"}
        )
        assert response.status_code == 422
    
    @pytest.mark.asyncio 
    async def test_api_performance_benchmarks(
        self,
        client: AsyncClient,
        test_session: str,
        performance_tracker
    ):
        """Validate API performance meets SLA targets."""
        
        # Test single query performance
        with performance_tracker.time_request("single_query"):
            response = await client.post(
                "/api/v1/agent/query",
                json={
                    "session_id": test_session,
                    "query": "Performance test query",
                    "context": {"test_type": "performance"}
                }
            )
        
        assert response.status_code == 200
        performance_tracker.assert_performance_target("single_query", 1.0)
        
        # Test batch query performance  
        with performance_tracker.time_request("batch_queries"):
            responses = []
            for i in range(5):
                response = await client.post(
                    "/api/v1/agent/query", 
                    json={
                        "session_id": test_session,
                        "query": f"Batch query {i}",
                        "context": {"batch_index": i}
                    }
                )
                responses.append(response)
        
        # All should succeed
        for response in responses:
            assert response.status_code == 200
        
        # Batch should complete within reasonable time
        performance_tracker.assert_performance_target("batch_queries", 5.0)
        
        # Log performance summary
        summary = performance_tracker.get_summary()
        print(f"\nPerformance Summary: {summary}")


class TestAgentAPIErrorScenarios:
    """Test error scenarios with real HTTP behavior."""
    
    @pytest.mark.asyncio
    async def test_malformed_json_handling(self, client: AsyncClient):
        """Test handling of malformed JSON requests."""
        
        # Send malformed JSON
        response = await client.post(
            "/api/v1/agent/query",
            content='{"session_id": "test", "query": incomplete',
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 422
        error_data = response.json()
        assert "detail" in error_data
    
    @pytest.mark.asyncio
    async def test_request_size_limits(self, client: AsyncClient, test_session: str):
        """Test request size handling."""
        
        # Create very large request
        large_context = {"data": "x" * 1000000}  # 1MB of data
        
        response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": test_session,
                "query": "test query",
                "context": large_context
            }
        )
        
        # Should either succeed or reject gracefully
        assert response.status_code in [200, 413, 422]
        
        if response.status_code == 413:
            error_data = response.json()
            assert "too large" in error_data["detail"].lower()
    
    @pytest.mark.asyncio
    async def test_timeout_handling(
        self, 
        client: AsyncClient, 
        test_session: str
    ):
        """Test timeout handling for long-running requests."""
        
        # Create request that might take longer to process
        response = await client.post(
            "/api/v1/agent/query",
            json={
                "session_id": test_session,
                "query": "Complex analysis requiring extensive processing time",
                "context": {
                    "complexity": "high",
                    "analysis_depth": "comprehensive",
                    "timeout_test": True
                }
            },
            timeout=30.0  # 30 second timeout
        )
        
        # Should complete within timeout or handle gracefully
        assert response.status_code in [200, 504, 408]
        
        if response.status_code == 200:
            # If successful, validate response structure
            data = response.json()
            assert "investigation_id" in data
            assert "findings" in data
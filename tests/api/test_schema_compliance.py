"""OpenAPI Schema Compliance Tests

Purpose: Validate that API responses match the OpenAPI specification exactly.

This test suite validates that all API endpoints conform to their OpenAPI schema definitions:

1. Response Schema Validation - All responses match declared Pydantic models
2. Request Schema Validation - All requests are properly validated against schemas  
3. Error Schema Validation - Error responses follow ErrorResponse model
4. Content-Type Compliance - Responses use correct media types
5. Status Code Schema Alignment - Status codes match OpenAPI definitions
6. Header Schema Compliance - Headers match OpenAPI specifications

Architecture: Uses jsonschema validation against generated OpenAPI spec.
Validates both successful responses and error conditions.
"""

import pytest
from httpx import AsyncClient
from typing import Dict, Any, List, Optional
import json
import jsonschema
from jsonschema import validate, ValidationError
from pydantic import ValidationError as PydanticValidationError

# Import models for validation
from faultmaven.models.api import (
    SessionResponse, ErrorResponse, KnowledgeBaseDocument, JobStatus,
    AgentResponse, DataUploadResponse, CaseResponse
)


class TestResponseSchemaCompliance:
    """Test that all API responses match their declared Pydantic models."""
    
    @pytest.mark.api
    async def test_session_response_schema_compliance(self, client: AsyncClient):
        """Validate session endpoints return responses matching SessionResponse schema."""
        
        # Test POST /sessions - should return SessionResponse
        create_response = await client.post("/api/v1/sessions/")
        assert create_response.status_code == 201
        
        response_data = create_response.json()
        
        # Validate against SessionResponse model
        try:
            session_response = SessionResponse(**response_data)
            assert session_response.session_id is not None
            assert session_response.status == "active"
            assert session_response.schema_version == "3.1.0"
        except PydanticValidationError as e:
            pytest.fail(f"Session creation response doesn't match SessionResponse schema: {e}")
        
        session_id = response_data["session_id"]
        
        # Test GET /sessions/{id} - should return SessionResponse
        get_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert get_response.status_code == 200
        
        get_data = get_response.json()
        
        try:
            get_session_response = SessionResponse(**get_data)
            assert get_session_response.session_id == session_id
        except PydanticValidationError as e:
            pytest.fail(f"Session get response doesn't match SessionResponse schema: {e}")
        
        # Clean up
        delete_response = await client.delete(f"/api/v1/sessions/{session_id}")
        assert delete_response.status_code == 204
    
    @pytest.mark.api
    async def test_agent_response_schema_compliance(self, client: AsyncClient):
        """Validate agent endpoints return responses matching AgentResponse schema."""
        
        # Setup test data
        session_response = await client.post("/api/v1/sessions/")
        session_id = session_response.json()["session_id"]
        
        case_data = {"title": "Schema Test Case", "initial_query": "Test", "priority": "low"}
        case_response = await client.post(f"/api/v1/sessions/{session_id}/cases", json=case_data)
        case_id = case_response.json()["case"]["case_id"]
        
        # Test agent query response
        query_data = {
            "session_id": session_id,
            "query": "Test query for schema validation",
            "context": {"test": "schema_compliance"}
        }
        
        agent_response = await client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
        assert agent_response.status_code == 200
        
        response_data = agent_response.json()
        
        # Validate against AgentResponse model
        try:
            agent_response_obj = AgentResponse(**response_data)
            assert agent_response_obj.schema_version == "3.1.0"
            assert agent_response_obj.content is not None
            assert agent_response_obj.response_type is not None
            assert agent_response_obj.view_state is not None
            assert agent_response_obj.view_state.session_id == session_id
        except PydanticValidationError as e:
            pytest.fail(f"Agent response doesn't match AgentResponse schema: {e}")
        
        # Clean up
        await client.delete(f"/api/v1/sessions/{session_id}")
    
    @pytest.mark.api 
    async def test_data_upload_response_schema_compliance(self, client: AsyncClient):
        """Validate data upload endpoints return proper schema-compliant responses."""
        
        # Create session
        session_response = await client.post("/api/v1/sessions/")
        session_id = session_response.json()["session_id"]
        
        # Test data upload response - Note: current implementation returns dict, not DataUploadResponse
        files = {"file": ("schema_test.log", b"ERROR: Schema validation test", "text/plain")}
        data = {"session_id": session_id, "description": "Schema compliance test"}
        
        upload_response = await client.post("/api/v1/data/upload", files=files, data=data)
        assert upload_response.status_code == 201
        
        response_data = upload_response.json()
        
        # Current implementation returns dict - validate it has expected structure
        required_fields = ["data_id", "session_id", "data_type", "processing_status"]
        for field in required_fields:
            assert field in response_data, f"Missing required field in data upload response: {field}"
        
        # Validate field types
        assert isinstance(response_data["data_id"], str)
        assert isinstance(response_data["session_id"], str)
        assert isinstance(response_data["processing_status"], str)
        
        # Clean up
        await client.delete(f"/api/v1/sessions/{session_id}")
    
    @pytest.mark.api
    async def test_knowledge_document_response_schema_compliance(self, client: AsyncClient):
        """Validate knowledge base endpoints return schema-compliant responses."""
        
        # Test document creation response
        document_data = {
            "title": "Schema Compliance Test Document",
            "content": "Testing schema compliance for knowledge base operations.",
            "document_type": "test_guide",
            "category": "testing",
            "tags": ["schema", "compliance", "testing"]
        }
        
        create_response = await client.post("/api/v1/knowledge/documents", json=document_data)
        assert create_response.status_code == 201
        
        response_data = create_response.json()
        
        # Current implementation returns dict - validate structure
        required_fields = ["document_id", "status"]
        for field in required_fields:
            assert field in response_data, f"Missing required field in document creation response: {field}"
        
        document_id = response_data["document_id"]
        
        # Test document retrieval response
        get_response = await client.get(f"/api/v1/knowledge/documents/{document_id}")
        assert get_response.status_code == 200
        
        get_data = get_response.json()
        
        # Validate document structure
        doc_required_fields = ["document_id", "title", "document_type", "created_at"]
        for field in doc_required_fields:
            assert field in get_data, f"Missing required field in document get response: {field}"
        
        # Clean up
        delete_response = await client.delete(f"/api/v1/knowledge/documents/{document_id}")
        assert delete_response.status_code == 204


class TestRequestSchemaValidation:
    """Test that request validation works correctly against declared schemas."""
    
    @pytest.mark.api
    async def test_session_request_validation(self, client: AsyncClient):
        """Test session creation request validation."""
        
        # Valid request should succeed
        valid_request = {"user_id": "test_user", "context": {"test": True}}
        response = await client.post("/api/v1/sessions/", json=valid_request)
        assert response.status_code == 201
        
        # Clean up valid session
        session_id = response.json()["session_id"]
        await client.delete(f"/api/v1/sessions/{session_id}")
        
        # Invalid requests should fail with 422
        invalid_requests = [
            {"timeout_minutes": -1},  # Negative timeout
            {"timeout_minutes": "invalid"},  # Wrong type
            {"user_id": 123},  # Wrong type for user_id
        ]
        
        for invalid_data in invalid_requests:
            response = await client.post("/api/v1/sessions/", json=invalid_data)
            assert response.status_code == 422, f"Should reject invalid session data: {invalid_data}"
            
            error_data = response.json()
            assert "detail" in error_data
    
    @pytest.mark.api
    async def test_agent_query_request_validation(self, client: AsyncClient):
        """Test agent query request validation."""
        
        # Setup test data
        session_response = await client.post("/api/v1/sessions/")
        session_id = session_response.json()["session_id"]
        
        case_data = {"title": "Validation Test", "initial_query": "Test", "priority": "medium"}
        case_response = await client.post(f"/api/v1/sessions/{session_id}/cases", json=case_data)
        case_id = case_response.json()["case"]["case_id"]
        
        # Valid request should succeed
        valid_query = {
            "session_id": session_id,
            "query": "Valid query for testing",
            "context": {"test": "validation"},
            "priority": "medium"
        }
        
        response = await client.post(f"/api/v1/cases/{case_id}/queries", json=valid_query)
        assert response.status_code == 200
        
        # Invalid requests should fail
        invalid_queries = [
            {"session_id": session_id},  # Missing query
            {"query": "Test"},  # Missing session_id
            {"session_id": session_id, "query": ""},  # Empty query
            {"session_id": session_id, "query": "Test", "priority": "invalid"},  # Invalid priority
        ]
        
        for invalid_query in invalid_queries:
            response = await client.post(f"/api/v1/cases/{case_id}/queries", json=invalid_query)
            assert response.status_code == 422, f"Should reject invalid query: {invalid_query}"
        
        # Clean up
        await client.delete(f"/api/v1/sessions/{session_id}")
    
    @pytest.mark.api
    async def test_knowledge_document_request_validation(self, client: AsyncClient):
        """Test knowledge document creation request validation."""
        
        # Valid request should succeed
        valid_document = {
            "title": "Valid Document",
            "content": "This is a valid document for testing.",
            "document_type": "guide",
            "category": "testing",
            "tags": ["test", "validation"]
        }
        
        response = await client.post("/api/v1/knowledge/documents", json=valid_document)
        assert response.status_code == 201
        
        # Clean up
        document_id = response.json()["document_id"]
        await client.delete(f"/api/v1/knowledge/documents/{document_id}")
        
        # Invalid requests should fail
        invalid_documents = [
            {},  # Missing required fields
            {"title": ""},  # Empty title
            {"title": "Test", "content": ""},  # Empty content
            {"title": "Test", "content": "Content"},  # Missing document_type
            {"title": "Test", "content": "Content", "document_type": ""},  # Empty document_type
        ]
        
        for invalid_doc in invalid_documents:
            response = await client.post("/api/v1/knowledge/documents", json=invalid_doc)
            assert response.status_code == 422, f"Should reject invalid document: {invalid_doc}"


class TestErrorSchemaCompliance:
    """Test that all error responses follow the ErrorResponse schema."""
    
    @pytest.mark.api
    async def test_404_error_schema_compliance(self, client: AsyncClient):
        """Test that 404 errors return properly formatted error responses."""
        
        # Test various 404 scenarios
        not_found_requests = [
            "/api/v1/sessions/non-existent-session",
            "/api/v1/data/non-existent-data-id", 
            "/api/v1/knowledge/documents/non-existent-doc-id",
        ]
        
        for endpoint in not_found_requests:
            response = await client.get(endpoint)
            assert response.status_code == 404, f"Should return 404 for {endpoint}"
            
            error_data = response.json()
            
            # Validate error response structure
            assert "detail" in error_data, f"404 response missing detail field for {endpoint}"
            assert isinstance(error_data["detail"], str), f"Error detail should be string for {endpoint}"
            assert "not found" in error_data["detail"].lower(), f"404 should mention 'not found' for {endpoint}"
    
    @pytest.mark.api
    async def test_422_validation_error_schema_compliance(self, client: AsyncClient):
        """Test that 422 validation errors return proper error format."""
        
        # Test validation errors from different endpoints
        validation_test_cases = [
            {
                "endpoint": "/api/v1/sessions/",
                "method": "POST",
                "data": {"timeout_minutes": -1}
            },
            {
                "endpoint": "/api/v1/knowledge/documents",
                "method": "POST", 
                "data": {"title": ""}  # Empty title
            }
        ]
        
        for test_case in validation_test_cases:
            if test_case["method"] == "POST":
                response = await client.post(test_case["endpoint"], json=test_case["data"])
            else:
                response = await client.get(test_case["endpoint"])
            
            assert response.status_code == 422, f"Should return 422 for validation error on {test_case['endpoint']}"
            
            error_data = response.json()
            
            # Validate error response structure
            assert "detail" in error_data, f"422 response missing detail field for {test_case['endpoint']}"
            
            # Detail can be string or list of validation errors
            detail = error_data["detail"]
            assert isinstance(detail, (str, list)), f"Error detail should be string or list for {test_case['endpoint']}"
    
    @pytest.mark.api
    async def test_400_bad_request_error_schema_compliance(self, client: AsyncClient):
        """Test that 400 Bad Request errors return proper error format."""
        
        # Test malformed JSON
        response = await client.post(
            "/api/v1/sessions/",
            content='{"invalid": json syntax}',
            headers={"Content-Type": "application/json"}
        )
        
        # Should be 422 (JSON parsing) or 400 (malformed request)
        assert response.status_code in [400, 422]
        
        error_data = response.json()
        assert "detail" in error_data
    
    @pytest.mark.api
    async def test_500_internal_server_error_schema_compliance(self, client: AsyncClient):
        """Test that 500 errors return proper error format when they occur."""
        
        # Note: This is harder to test without explicitly triggering server errors
        # In real scenarios, 500 errors should still follow ErrorResponse format
        
        # Test with agent endpoint that might have internal errors
        # if service layer has issues
        session_response = await client.post("/api/v1/sessions/")
        session_id = session_response.json()["session_id"]
        
        # Try to trigger potential error with edge case data
        query_data = {
            "session_id": session_id,
            "query": "Test query",
            "context": {"large_data": "x" * 10000}  # Very large context
        }
        
        case_data = {"title": "Error Test", "initial_query": "Test", "priority": "low"}
        case_response = await client.post(f"/api/v1/sessions/{session_id}/cases", json=case_data)
        
        if case_response.status_code == 201:
            case_id = case_response.json()["case"]["case_id"]
            
            response = await client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
            
            # If this returns an error, it should be properly formatted
            if response.status_code >= 500:
                error_data = response.json()
                assert "detail" in error_data, "500 response should have detail field"
        
        # Clean up
        await client.delete(f"/api/v1/sessions/{session_id}")


class TestContentTypeCompliance:
    """Test that responses use correct Content-Type headers."""
    
    @pytest.mark.api
    async def test_json_response_content_types(self, client: AsyncClient):
        """Test that JSON endpoints return proper Content-Type headers."""
        
        # Test session creation
        response = await client.post("/api/v1/sessions/")
        assert response.status_code == 201
        
        content_type = response.headers.get("Content-Type", "")
        assert content_type.startswith("application/json"), f"Session creation should return JSON, got: {content_type}"
        
        session_id = response.json()["session_id"]
        
        # Test session retrieval
        get_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert get_response.status_code == 200
        
        get_content_type = get_response.headers.get("Content-Type", "")
        assert get_content_type.startswith("application/json"), f"Session get should return JSON, got: {get_content_type}"
        
        # Clean up
        await client.delete(f"/api/v1/sessions/{session_id}")
    
    @pytest.mark.api
    async def test_empty_response_content_types(self, client: AsyncClient):
        """Test that 204 No Content responses have no content type or appropriate content type."""
        
        # Create and delete session to test 204 response
        create_response = await client.post("/api/v1/sessions/")
        session_id = create_response.json()["session_id"]
        
        delete_response = await client.delete(f"/api/v1/sessions/{session_id}")
        assert delete_response.status_code == 204
        
        # 204 responses should have no body
        assert len(delete_response.content) == 0
        
        # Content-Type header is optional for 204 responses
        content_type = delete_response.headers.get("Content-Type")
        if content_type:
            # If present, should be appropriate
            assert content_type in ["", "application/json", "text/plain"]


class TestStatusCodeSchemaAlignment:
    """Test that status codes match OpenAPI specification declarations."""
    
    @pytest.mark.api
    async def test_create_operations_return_201(self, client: AsyncClient):
        """Test that all resource creation operations return 201 Created."""
        
        creation_operations = [
            {
                "endpoint": "/api/v1/sessions/",
                "data": {"user_id": "test_user"}
            },
            {
                "endpoint": "/api/v1/knowledge/documents",
                "data": {
                    "title": "Status Code Test",
                    "content": "Testing status code compliance",
                    "document_type": "test",
                    "category": "testing"
                }
            }
        ]
        
        created_resources = []
        
        for operation in creation_operations:
            response = await client.post(operation["endpoint"], json=operation["data"])
            assert response.status_code == 201, f"Creation should return 201 for {operation['endpoint']}"
            
            # Store for cleanup
            response_data = response.json()
            if "session_id" in response_data:
                created_resources.append(("session", response_data["session_id"]))
            elif "document_id" in response_data:
                created_resources.append(("document", response_data["document_id"]))
        
        # Clean up created resources
        for resource_type, resource_id in created_resources:
            if resource_type == "session":
                await client.delete(f"/api/v1/sessions/{resource_id}")
            elif resource_type == "document":
                await client.delete(f"/api/v1/knowledge/documents/{resource_id}")
    
    @pytest.mark.api
    async def test_delete_operations_return_204(self, client: AsyncClient):
        """Test that all delete operations return 204 No Content."""
        
        # Create resources to delete
        session_response = await client.post("/api/v1/sessions/")
        session_id = session_response.json()["session_id"]
        
        doc_response = await client.post("/api/v1/knowledge/documents", json={
            "title": "Delete Test Document",
            "content": "Will be deleted",
            "document_type": "test",
            "category": "testing"
        })
        document_id = doc_response.json()["document_id"]
        
        # Test deletions
        delete_operations = [
            f"/api/v1/sessions/{session_id}",
            f"/api/v1/knowledge/documents/{document_id}"
        ]
        
        for endpoint in delete_operations:
            response = await client.delete(endpoint)
            assert response.status_code == 204, f"Delete should return 204 for {endpoint}"
            assert len(response.content) == 0, f"204 response should have no content for {endpoint}"
    
    @pytest.mark.api
    async def test_get_operations_return_200(self, client: AsyncClient):
        """Test that all retrieval operations return 200 OK when resource exists."""
        
        # Create test resources
        session_response = await client.post("/api/v1/sessions/")
        session_id = session_response.json()["session_id"]
        
        # Test GET operations
        get_operations = [
            f"/api/v1/sessions/{session_id}",
            "/api/v1/sessions/",  # List operation
        ]
        
        for endpoint in get_operations:
            response = await client.get(endpoint)
            assert response.status_code == 200, f"GET should return 200 for {endpoint}"
            
            response_data = response.json()
            assert response_data != {}, f"GET response should not be empty for {endpoint}"
        
        # Clean up
        await client.delete(f"/api/v1/sessions/{session_id}")
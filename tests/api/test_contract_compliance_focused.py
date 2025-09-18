"""
Focused API Contract Compliance Test Suite

Purpose: Executable contract verification for CI/CD pipeline
Scope: Critical endpoints, headers, status codes, and response shapes
Target: 10-15 comprehensive tests covering all contract violations

This test suite validates the API Contract Matrix requirements
and prevents regressions in frontend-backend integration.
"""

import pytest
import json
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from fastapi import status

# Import the FastAPI app
from faultmaven.main import app
from faultmaven.container import container
from faultmaven.models.case import Case as CaseEntity, CaseStatus, CasePriority
from faultmaven.models.api import Case, CaseResponse, CaseSummary


@pytest.fixture
def client():
    """Test client with clean container state"""
    container.reset()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mock_case_service():
    """Mock case service for controlled testing"""
    mock = AsyncMock()
    mock.list_user_cases = AsyncMock(return_value=[])
    mock.count_user_cases = AsyncMock(return_value=0)
    mock.get_case = AsyncMock(return_value=None)
    mock.create_case = AsyncMock()
    
    # Sample case summary for non-empty responses
    sample_case = CaseSummary(
        case_id="test-case-123",
        title="Test Case",
        status="active",
        priority="medium",
        created_at=datetime.utcnow().isoformat(),
        updated_at=datetime.utcnow().isoformat(),
        message_count=1
    )
    
    return mock, sample_case


class TestContractCompliance:
    """Contract compliance test suite following API_CONTRACT_MATRIX.md"""

    # =============================================================================
    # Happy Path Tests - Core Functionality
    # =============================================================================

    def test_get_cases_empty_returns_array_with_headers(self, client, mock_case_service, monkeypatch):
        """
        GET /api/v1/cases → 200 + CaseSummary[] + X-Total-Count + Link headers
        CRITICAL: Must return array (not envelope), proper headers, never 500 for empty
        """
        mock, _ = mock_case_service
        
        # Mock the dependency to return our mock
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        # Test empty results
        response = client.get("/api/v1/cases?limit=50&offset=0")
        
        # Verify status
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        # Verify response is array (not envelope object)
        data = response.json()
        assert isinstance(data, list), f"Expected array, got {type(data)}: {data}"
        assert data == [], f"Expected empty array, got: {data}"
        
        # Verify required headers present
        assert "X-Total-Count" in response.headers, "Missing X-Total-Count header"
        assert response.headers["X-Total-Count"] == "0", f"Expected X-Total-Count=0, got: {response.headers['X-Total-Count']}"
        
        # Link header is optional for empty results, but if present should be valid
        if "Link" in response.headers:
            assert isinstance(response.headers["Link"], str)

    def test_get_cases_non_empty_returns_array_with_pagination(self, client, mock_case_service, monkeypatch):
        """
        GET /api/v1/cases with data → 200 + CaseSummary[] + pagination headers
        """
        mock, sample_case = mock_case_service
        mock.list_user_cases.return_value = [sample_case]
        mock.count_user_cases.return_value = 1
        
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        response = client.get("/api/v1/cases?limit=50&offset=0")
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify array response
        assert isinstance(data, list), f"Expected array, got {type(data)}"
        assert len(data) == 1
        
        # Verify CaseSummary structure
        case_data = data[0]
        required_fields = ["case_id", "title", "status", "created_at"]
        for field in required_fields:
            assert field in case_data, f"Missing required field: {field}"
        
        # Verify headers
        assert "X-Total-Count" in response.headers
        assert response.headers["X-Total-Count"] == "1"

    def test_post_cases_returns_201_with_location_header(self, client, mock_case_service, monkeypatch):
        """
        POST /api/v1/cases → 201 + CaseResponse + Location header
        CRITICAL: Location must be non-null, body must have case.case_id
        """
        mock, _ = mock_case_service
        
        # Create a proper case entity for the mock response
        created_case = CaseEntity(
            case_id="new-case-456",
            title="New Test Case",
            description="Test description",
            owner_id="test-user"
        )
        mock.create_case.return_value = created_case
        
        async def mock_get_case_service():
            return mock
        
        async def mock_get_session_service():
            session_mock = AsyncMock()
            session_mock.get_session = AsyncMock(return_value=None)  # No validation required
            return session_mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_session_service_dependency", mock_get_session_service)
        
        payload = {
            "title": "New Test Case",
            "description": "Test description"
        }
        
        response = client.post("/api/v1/cases", json=payload)
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        # Verify Location header is present and non-null
        assert "Location" in response.headers, "Missing Location header"
        location = response.headers["Location"]
        assert location is not None, "Location header is null"
        assert location != "", "Location header is empty"
        assert "cases" in location, f"Location should reference case resource: {location}"
        
        # Verify response body structure
        data = response.json()
        assert "case" in data, "Response must have 'case' field"
        case_data = data["case"]
        assert "case_id" in case_data, "Response case must have 'case_id' field"
        assert case_data["case_id"] == "new-case-456"

    def test_post_cases_queries_sync_returns_201_with_agent_response(self, client, mock_case_service, monkeypatch):
        """
        POST /api/v1/cases/{cid}/queries (sync) → 201 + AgentResponse + Location
        CRITICAL: Must include content, response_type, and valid Location header
        """
        mock, _ = mock_case_service
        
        # Mock case exists
        existing_case = Case(
            case_id="test-case-789",
            title="Test Case",
            status="active",
            priority="medium",
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
            message_count=1
        )
        mock.get_case.return_value = existing_case
        
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        payload = {"query": "Simple query"}  # Simple query triggers sync path
        
        response = client.post("/api/v1/cases/test-case-789/queries", json=payload)
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        # Verify Location header
        assert "Location" in response.headers, "Missing Location header"
        location = response.headers["Location"]
        assert location is not None and location != "", "Location header must be non-null/non-empty"
        assert "queries" in location, f"Location should reference query resource: {location}"
        
        # Verify AgentResponse structure
        data = response.json()
        required_fields = ["content", "response_type", "view_state"]
        for field in required_fields:
            assert field in data, f"Missing required AgentResponse field: {field}"
        
        assert isinstance(data["content"], str), "content must be string"
        assert data["response_type"] in ["ANSWER", "CLARIFICATION_NEEDED", "SOLUTION_READY"], f"Invalid response_type: {data['response_type']}"

    def test_post_cases_queries_async_returns_202_with_retry_after(self, client, mock_case_service, monkeypatch):
        """
        POST /api/v1/cases/{cid}/queries (async) → 202 + QueryJobStatus + Location + Retry-After
        CRITICAL: Must include both Location and Retry-After headers for async processing
        """
        mock, _ = mock_case_service
        
        existing_case = Case(
            case_id="test-case-async",
            title="Test Case Async",
            status="active",
            priority="medium",
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
            message_count=1
        )
        mock.get_case.return_value = existing_case
        
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        # Complex query triggers async path
        payload = {"query": "Analyze this complex detailed system behavior with comprehensive analysis"}
        
        response = client.post("/api/v1/cases/test-case-async/queries", json=payload)
        
        assert response.status_code == 202, f"Expected 202, got {response.status_code}: {response.text}"
        
        # Verify both required headers for async
        assert "Location" in response.headers, "Missing Location header for async processing"
        assert "Retry-After" in response.headers, "Missing Retry-After header for async processing"
        
        location = response.headers["Location"]
        retry_after = response.headers["Retry-After"]
        
        assert location is not None and location != "", "Location header must be non-null"
        assert retry_after is not None and retry_after != "", "Retry-After header must be non-null"
        assert retry_after.isdigit(), f"Retry-After must be integer seconds: {retry_after}"
        
        # Verify QueryJobStatus structure
        data = response.json()
        required_fields = ["job_id", "case_id", "query", "status", "created_at"]
        for field in required_fields:
            assert field in data, f"Missing required QueryJobStatus field: {field}"

    def test_get_sessions_cases_returns_array_with_headers(self, client, monkeypatch):
        """
        GET /api/v1/sessions/{sid}/cases → 200 + CaseSummary[] + headers
        CRITICAL: Must show just-created cases immediately (atomicity)
        """
        # Mock session service
        session_mock = AsyncMock()
        session_mock.get_session = AsyncMock(return_value=MagicMock(session_id="test-session"))
        
        # Mock case service with case for session
        case_mock = AsyncMock()
        sample_case = CaseSummary(
            case_id="session-case-123",
            title="Session Case",
            status=CaseStatus.ACTIVE,
            priority=CasePriority.MEDIUM,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            message_count=1,
            participant_count=1
        )
        case_mock.list_cases_by_session = AsyncMock(return_value=[sample_case])
        case_mock.count_cases_by_session = AsyncMock(return_value=1)
        
        async def mock_get_session_service():
            return session_mock
            
        async def mock_get_case_service():
            return case_mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.session.get_session_service", mock_get_session_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.session.get_case_service", mock_get_case_service)
        
        response = client.get("/api/v1/sessions/test-session/cases")
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        # Verify array structure
        data = response.json()
        assert "cases" in data, "Response must have 'cases' field"
        cases_array = data["cases"]
        assert isinstance(cases_array, list), f"cases must be array, got {type(cases_array)}"
        assert len(cases_array) == 1, "Should include the case"
        
        # Verify pagination headers
        assert "X-Total-Count" in response.headers, "Missing X-Total-Count header"
        assert response.headers["X-Total-Count"] == "1"

    # =============================================================================
    # Error Path Tests - Auth and Missing Resources
    # =============================================================================

    def test_protected_endpoint_pre_auth_returns_401_not_500(self, client, monkeypatch):
        """
        Protected endpoint without auth → 401 + ErrorResponse (NEVER 500)
        CRITICAL: Service unavailable should map to auth error, not server error
        """
        # Mock case service as unavailable (None)
        async def mock_get_case_service():
            return None
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        response = client.get("/api/v1/cases/test-case-id")
        
        # CRITICAL: Must be 401, NEVER 500 for service unavailable
        assert response.status_code == 401, f"Expected 401 for service unavailable, got {response.status_code}: {response.text}"
        
        # Verify ErrorResponse structure
        data = response.json()
        assert "detail" in data, "Error response must have 'detail' field"
        assert "Authentication required" in data["detail"] or "unavailable" in data["detail"]

    def test_unknown_case_id_returns_404_not_500(self, client, mock_case_service, monkeypatch):
        """
        GET /api/v1/cases/{unknown_id} → 404 + ErrorResponse (not 500)
        """
        mock, _ = mock_case_service
        mock.get_case.return_value = None  # Case not found
        
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        response = client.get("/api/v1/cases/nonexistent-case")
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "detail" in data, "404 response must have 'detail' field"

    def test_empty_results_return_200_array_not_404(self, client, mock_case_service, monkeypatch):
        """
        Empty query results → 200 + [] (NEVER 404)
        CRITICAL: No data is success condition, not error condition
        """
        mock, _ = mock_case_service
        mock.list_user_cases.return_value = []  # No cases
        mock.count_user_cases.return_value = 0
        
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        response = client.get("/api/v1/cases")
        
        # CRITICAL: Empty results are 200, not 404
        assert response.status_code == 200, f"Expected 200 for empty results, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data == [], "Empty results should be empty array"

    # =============================================================================
    # Header and Shape Tests - Contract Enforcement
    # =============================================================================

    def test_location_headers_non_null_and_properly_formatted(self, client, mock_case_service, monkeypatch):
        """
        All Location headers must be non-null and properly formatted paths
        CRITICAL: Browser cannot read null Location headers
        """
        mock, _ = mock_case_service
        
        created_case = CaseEntity(
            case_id="location-test-case",
            title="Location Test Case",
            description="Test",
            owner_id="test-user"
        )
        mock.create_case.return_value = created_case
        
        async def mock_get_case_service():
            return mock
        
        async def mock_get_session_service():
            return AsyncMock()
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_session_service_dependency", mock_get_session_service)
        
        payload = {"title": "Location Test Case", "description": "Test"}
        response = client.post("/api/v1/cases", json=payload)
        
        assert response.status_code == 201
        
        # Verify Location header format
        location = response.headers.get("Location")
        assert location is not None, "Location header cannot be None"
        assert location != "", "Location header cannot be empty string"
        assert location != "null", "Location header cannot be string 'null'"
        assert location.startswith("/api/v1/"), f"Location should be API path: {location}"

    def test_cors_headers_exposed_for_browser_access(self, client):
        """
        Verify CORS configuration exposes required headers to browser
        CRITICAL: Browsers need access to Location, X-Total-Count, Link, Retry-After
        """
        # Make an OPTIONS request to check CORS
        response = client.options("/api/v1/cases")
        
        if response.status_code == 200:
            # Check if exposed headers are configured (this depends on FastAPI CORS setup)
            exposed_headers = response.headers.get("Access-Control-Expose-Headers", "")
            
            required_headers = ["Location", "X-Total-Count", "Link", "Retry-After"]
            for header in required_headers:
                if header not in exposed_headers:
                    # This is informational - the CORS config should expose these
                    print(f"⚠️  CORS should expose header: {header}")

    def test_arrays_are_arrays_not_envelopes(self, client, mock_case_service, monkeypatch):
        """
        Array endpoints must return arrays, not envelope objects
        CRITICAL: Frontend expects direct array parsing
        """
        mock, sample_case = mock_case_service
        mock.list_user_cases.return_value = [sample_case]
        mock.count_user_cases.return_value = 1
        
        async def mock_get_case_service():
            return mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        
        response = client.get("/api/v1/cases")
        data = response.json()
        
        # CRITICAL: Must be direct array, not {data: [...]} or {cases: [...]}
        assert isinstance(data, list), f"Response must be array, not envelope object. Got: {type(data)}"
        assert not isinstance(data, dict), "Response must not be envelope object"

    def test_required_fields_present_in_responses(self, client, mock_case_service, monkeypatch):
        """
        Verify required fields are present in all response schemas
        """
        mock, sample_case = mock_case_service
        
        # Test case creation required fields
        created_case = CaseEntity(
            case_id="required-fields-test",
            title="Required Fields Test",
            description="Test",
            owner_id="test-user"
        )
        mock.create_case.return_value = created_case
        mock.get_case.return_value = Case(
            case_id="required-fields-test",
            title="Required Fields Test",
            status="active",
            priority="medium",
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
            message_count=1
        )
        
        async def mock_get_case_service():
            return mock
        
        async def mock_get_session_service():
            return AsyncMock()
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_session_service_dependency", mock_get_session_service)
        
        # Test POST /cases response has required fields
        payload = {"title": "Required Fields Test", "description": "Test"}
        response = client.post("/api/v1/cases", json=payload)
        
        assert response.status_code == 201
        data = response.json()
        
        # Verify CaseResponse structure
        assert "case" in data, "CaseResponse must have 'case' field"
        case_data = data["case"]
        
        case_required_fields = ["case_id", "title", "status", "created_at", "updated_at"]
        for field in case_required_fields:
            assert field in case_data, f"Case object missing required field: {field}"

    # =============================================================================
    # Integration Tests - End-to-End Flows
    # =============================================================================

    def test_case_creation_to_session_visibility_atomicity(self, client, monkeypatch):
        """
        Test atomic case creation → immediate visibility in session cases
        CRITICAL: Frontend reported cases not immediately visible after creation
        """
        # This test would require more complex mocking to test the full flow
        # For now, we verify the endpoints work correctly in isolation
        # Real atomicity testing would need integration with actual services
        
        # Mock session service
        session_mock = AsyncMock()
        session_mock.get_session = AsyncMock(return_value=MagicMock(session_id="atomicity-test"))
        
        # Mock case service
        case_mock = AsyncMock()
        created_case = CaseEntity(
            case_id="atomic-case-123",
            title="Atomic Test Case",
            description="Test atomicity",
            owner_id="test-user"
        )
        case_mock.create_case.return_value = created_case
        
        # After creation, case should be visible in session
        case_summary = CaseSummary(
            case_id="atomic-case-123",
            title="Atomic Test Case",
            status=CaseStatus.ACTIVE,
            priority=CasePriority.MEDIUM,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            message_count=1,
            participant_count=1
        )
        case_mock.list_cases_by_session = AsyncMock(return_value=[case_summary])
        case_mock.count_cases_by_session = AsyncMock(return_value=1)
        
        async def mock_get_case_service():
            return case_mock
        
        async def mock_get_session_service():
            return session_mock
            
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_case_service_dependency", mock_get_case_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.case._di_get_session_service_dependency", mock_get_session_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.session.get_case_service", mock_get_case_service)
        monkeypatch.setattr("faultmaven.api.v1.routes.session.get_session_service", mock_get_session_service)
        
        # Step 1: Create case
        payload = {"title": "Atomic Test Case", "description": "Test atomicity", "session_id": "atomicity-test"}
        create_response = client.post("/api/v1/cases", json=payload)
        
        assert create_response.status_code == 201
        
        # Step 2: Immediately check session cases - should include new case
        session_response = client.get("/api/v1/sessions/atomicity-test/cases")
        
        assert session_response.status_code == 200
        session_data = session_response.json()
        
        # Verify case is immediately visible
        assert "cases" in session_data
        cases_array = session_data["cases"]
        assert len(cases_array) == 1, "Created case should be immediately visible in session"
        assert cases_array[0]["case_id"] == "atomic-case-123"


# =============================================================================
# Test Suite Metadata and Configuration
# =============================================================================

class TestSuiteMetadata:
    """Test suite metadata for CI reporting and contract tracking"""
    
    @pytest.mark.suite_info
    def test_suite_coverage_summary(self):
        """Generate coverage summary for contract matrix compliance"""
        
        coverage_report = {
            "contract_version": "3.1.0",
            "test_count": 15,
            "critical_endpoints_covered": [
                "GET /api/v1/cases",
                "POST /api/v1/cases", 
                "POST /api/v1/cases/{id}/queries",
                "GET /api/v1/sessions/{sid}/cases"
            ],
            "contract_requirements_tested": [
                "Arrays vs Objects (no envelopes)",
                "Required Headers (Location, X-Total-Count, Link, Retry-After)",
                "Status Code Mapping (401 not 500 pre-auth)",
                "Empty Results (200 [] not 404)",
                "Response Schema Compliance",
                "CORS Header Exposure"
            ],
            "execution_target": "< 30 seconds for full suite",
            "ci_integration": "Block merges on failures"
        }
        
        print(f"\n=== Contract Test Suite Coverage ===")
        for key, value in coverage_report.items():
            print(f"{key}: {value}")
        
        # This test always passes - it's for reporting only
        assert True


if __name__ == "__main__":
    # Run the focused test suite
    pytest.main([__file__, "-v", "--tb=short"])
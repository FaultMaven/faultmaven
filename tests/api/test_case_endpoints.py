"""Test module for case API endpoints.

This module tests the REST API endpoints for case management functionality,
focusing on HTTP request/response handling, authentication, validation,
and error handling.

Tests cover:
- All CRUD operations for cases
- Case sharing and collaboration endpoints
- Case search and filtering endpoints
- Session-case association endpoints
- Authentication and authorization
- Input validation and error handling
- HTTP status codes and responses
"""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from typing import List, Dict, Any

from fastapi.testclient import TestClient
from fastapi import status

from faultmaven.main import app
from faultmaven.models.case import (
    Case,
    CaseCreateRequest,
    CaseUpdateRequest,
    CaseShareRequest,
    CaseSearchRequest,
    CaseListFilter,
    CaseSummary,
    CaseStatus,
    CasePriority,
    MessageType,
    ParticipantRole
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.exceptions import ValidationException, ServiceException


class MockCaseService:
    """Mock implementation of ICaseService for API testing"""
    
    def __init__(self):
        self.cases = {}
        self.create_case = AsyncMock()
        self.get_case = AsyncMock(return_value=None)
        self.update_case = AsyncMock(return_value=False)
        self.share_case = AsyncMock(return_value=False)
        self.add_message_to_case = AsyncMock(return_value=False)
        self.get_or_create_case_for_session = AsyncMock(return_value="case-123")
        self.link_session_to_case = AsyncMock(return_value=False)
        self.get_case_conversation_context = AsyncMock(return_value="")
        self.resume_case_in_session = AsyncMock(return_value=False)
        self.archive_case = AsyncMock(return_value=False)
        self.list_user_cases = AsyncMock(return_value=[])
        self.search_cases = AsyncMock(return_value=[])
        self.get_case_analytics = AsyncMock(return_value={})
        self.cleanup_expired_cases = AsyncMock(return_value=0)


@pytest.fixture
def mock_case_service():
    """Fixture providing mock case service"""
    return MockCaseService()


@pytest.fixture
def client():
    """Fixture providing FastAPI test client"""
    return TestClient(app)


@pytest.fixture
def sample_case():
    """Fixture providing sample case for testing"""
    case = Case(
        case_id="case-123",
        title="Test API Case",
        description="Case for API testing",
        owner_id="user-456",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM,
        tags=["api", "test"]
    )
    case.add_participant("user-456", ParticipantRole.OWNER)
    return case


@pytest.fixture
def sample_case_summary():
    """Fixture providing sample case summary"""
    return CaseSummary(
        case_id="case-123",
        title="Test API Case",
        status=CaseStatus.ACTIVE,
        priority=CasePriority.MEDIUM,
        owner_id="user-456",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        last_activity_at=datetime.utcnow(),
        message_count=5,
        participant_count=1,
        tags=["api", "test"]
    )


@pytest.fixture
def auth_headers():
    """Fixture providing authentication headers"""
    return {"Authorization": "Bearer test-token", "X-User-ID": "user-456"}


class TestCaseCreation:
    """Test case creation API endpoint"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case):
        """Test successful case creation"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.create_case.return_value = sample_case
        
        request_data = {
            "title": "Test API Case",
            "description": "Case for API testing",
            "priority": "medium",
            "tags": ["api", "test"],
            "session_id": "session-789",
            "initial_message": "Initial problem description"
        }
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        assert response.status_code == status.HTTP_201_CREATED
        
        response_data = response.json()
        assert response_data["case_id"] == "case-123"
        assert response_data["title"] == "Test API Case"
        assert response_data["description"] == "Case for API testing"
        assert response_data["status"] == "active"
        assert response_data["priority"] == "medium"
        assert "api" in response_data["tags"]
        
        # Verify service was called correctly
        mock_case_service.create_case.assert_called_once_with(
            title="Test API Case",
            description="Case for API testing",
            owner_id="user-456",
            session_id="session-789",
            initial_message="Initial problem description"
        )
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_minimal_data(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case):
        """Test case creation with minimal required data"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.create_case.return_value = sample_case
        
        request_data = {"title": "Minimal Case"}
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        assert response.status_code == status.HTTP_201_CREATED
        
        mock_case_service.create_case.assert_called_once_with(
            title="Minimal Case",
            description=None,
            owner_id="user-456",
            session_id=None,
            initial_message=None
        )
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_validation_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case creation with validation error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.create_case.side_effect = ValidationException("Title cannot be empty")
        
        request_data = {"title": ""}
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Title cannot be empty" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_service_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case creation with service error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.create_case.side_effect = ServiceException("Database error")
        
        request_data = {"title": "Test Case"}
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "Database error" in response.json()["detail"]
    
    def test_create_case_invalid_json(self, client):
        """Test case creation with invalid JSON"""
        response = client.post(
            "/api/v1/cases/", 
            data="invalid json",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    
    def test_create_case_missing_title(self, client):
        """Test case creation without required title"""
        request_data = {"description": "Missing title"}
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestCaseRetrieval:
    """Test case retrieval API endpoints"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_get_case_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case):
        """Test successful case retrieval"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.get_case.return_value = sample_case
        
        response = client.get("/api/v1/cases/case-123")
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert response_data["case_id"] == "case-123"
        assert response_data["title"] == "Test API Case"
        assert response_data["owner_id"] == "user-456"
        
        mock_case_service.get_case.assert_called_once_with("case-123", "user-456")
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_get_case_not_found(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case retrieval when case doesn't exist"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.get_case.return_value = None
        
        response = client.get("/api/v1/cases/case-999")
        
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "Case not found or access denied" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_get_case_service_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case retrieval with service error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.get_case.side_effect = Exception("Database error")
        
        response = client.get("/api/v1/cases/case-123")
        
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "Failed to get case" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_list_cases_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case_summary):
        """Test successful case listing"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.list_user_cases.return_value = [sample_case_summary]
        
        response = client.get("/api/v1/cases/")
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert len(response_data) == 1
        assert response_data[0]["case_id"] == "case-123"
        assert response_data[0]["title"] == "Test API Case"
        
        mock_case_service.list_user_cases.assert_called_once()
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_list_cases_with_filters(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case listing with query parameters"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.list_user_cases.return_value = []
        
        response = client.get(
            "/api/v1/cases/",
            params={
                "status_filter": "active",
                "priority_filter": "high",
                "owner_id": "user-123",
                "limit": 25,
                "offset": 10
            }
        )
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify filter was constructed correctly
        call_args = mock_case_service.list_user_cases.call_args
        user_id, filters = call_args[0]
        assert user_id == "user-456"
        assert filters.status.value == "active"
        assert filters.priority.value == "high"
        assert filters.owner_id == "user-123"
        assert filters.limit == 25
        assert filters.offset == 10
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_list_cases_invalid_limit(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case listing with invalid limit parameter"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        
        response = client.get("/api/v1/cases/", params={"limit": 150})  # Over max of 100
        
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_list_cases_service_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case listing with service error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.list_user_cases.side_effect = Exception("Database error")
        
        response = client.get("/api/v1/cases/")
        
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "Failed to list cases" in response.json()["detail"]


class TestCaseUpdate:
    """Test case update API endpoint"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_update_case_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test successful case update"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.update_case.return_value = True
        
        request_data = {
            "title": "Updated Case Title",
            "description": "Updated description",
            "status": "investigating",
            "priority": "high",
            "tags": ["updated", "important"]
        }
        
        response = client.put("/api/v1/cases/case-123", json=request_data)
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert response_data["case_id"] == "case-123"
        assert response_data["success"] is True
        assert response_data["message"] == "Case updated successfully"
        
        # Verify service was called with correct updates
        mock_case_service.update_case.assert_called_once()
        call_args = mock_case_service.update_case.call_args[0]
        case_id, updates, user_id = call_args
        assert case_id == "case-123"
        assert updates["title"] == "Updated Case Title"
        assert updates["status"].value == "investigating"
        assert updates["priority"].value == "high"
        assert user_id == "user-456"
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_update_case_partial(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test partial case update"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.update_case.return_value = True
        
        request_data = {"title": "New Title Only"}
        
        response = client.put("/api/v1/cases/case-123", json=request_data)
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify only title was updated
        call_args = mock_case_service.update_case.call_args[0]
        updates = call_args[1]
        assert "title" in updates
        assert "description" not in updates
        assert "status" not in updates
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_update_case_empty_updates(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case update with no updates provided"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        
        request_data = {}
        
        response = client.put("/api/v1/cases/case-123", json=request_data)
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "No updates provided" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_update_case_not_found(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case update when case doesn't exist or access denied"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.update_case.return_value = False
        
        request_data = {"title": "New Title"}
        
        response = client.put("/api/v1/cases/case-999", json=request_data)
        
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "Case not found or access denied" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_update_case_validation_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case update with validation error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.update_case.side_effect = ValidationException("Invalid status")
        
        request_data = {"status": "invalid_status"}
        
        response = client.put("/api/v1/cases/case-123", json=request_data)
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid status" in response.json()["detail"]


class TestCaseSharing:
    """Test case sharing API endpoint"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_share_case_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test successful case sharing"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.share_case.return_value = True
        
        request_data = {
            "user_id": "user-789",
            "role": "collaborator",
            "message": "Please help with this case"
        }
        
        response = client.post("/api/v1/cases/case-123/share", json=request_data)
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert response_data["case_id"] == "case-123"
        assert response_data["shared_with"] == "user-789"
        assert response_data["role"] == "collaborator"
        assert response_data["success"] is True
        assert "shared with user-789 as collaborator" in response_data["message"]
        
        # Verify service was called correctly
        mock_case_service.share_case.assert_called_once_with(
            case_id="case-123",
            target_user_id="user-789",
            role=ParticipantRole.COLLABORATOR,
            sharer_user_id="user-456"
        )
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_share_case_owner_role_rejected(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test that sharing with owner role is rejected"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        
        request_data = {
            "user_id": "user-789",
            "role": "owner"
        }
        
        response = client.post("/api/v1/cases/case-123/share", json=request_data)
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Cannot assign owner role through sharing" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_share_case_not_found(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case sharing when case doesn't exist or sharing not permitted"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.share_case.return_value = False
        
        request_data = {
            "user_id": "user-789",
            "role": "collaborator"
        }
        
        response = client.post("/api/v1/cases/case-999/share", json=request_data)
        
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "Case not found or sharing not permitted" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_share_case_validation_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case sharing with validation error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.share_case.side_effect = ValidationException("Invalid user ID")
        
        request_data = {
            "user_id": "",
            "role": "collaborator"
        }
        
        response = client.post("/api/v1/cases/case-123/share", json=request_data)
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid user ID" in response.json()["detail"]


class TestCaseArchiving:
    """Test case archiving API endpoint"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_archive_case_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test successful case archiving"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.archive_case.return_value = True
        
        response = client.post(
            "/api/v1/cases/case-123/archive",
            params={"reason": "Issue resolved"}
        )
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert response_data["case_id"] == "case-123"
        assert response_data["success"] is True
        assert response_data["message"] == "Case archived successfully"
        assert response_data["reason"] == "Issue resolved"
        
        # Verify service was called correctly
        mock_case_service.archive_case.assert_called_once_with(
            case_id="case-123",
            reason="Issue resolved",
            user_id="user-456"
        )
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_archive_case_without_reason(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case archiving without reason"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.archive_case.return_value = True
        
        response = client.post("/api/v1/cases/case-123/archive")
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert response_data["reason"] is None
        
        # Verify service was called with None reason
        mock_case_service.archive_case.assert_called_once_with(
            case_id="case-123",
            reason=None,
            user_id="user-456"
        )
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_archive_case_not_found(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case archiving when case doesn't exist or archive not permitted"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.archive_case.return_value = False
        
        response = client.post("/api/v1/cases/case-999/archive")
        
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "Case not found or archive not permitted" in response.json()["detail"]


class TestCaseSearch:
    """Test case search API endpoint"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_search_cases_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case_summary):
        """Test successful case search"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.search_cases.return_value = [sample_case_summary]
        
        request_data = {
            "query": "database error",
            "search_in_messages": True,
            "search_in_context": False,
            "filters": {
                "status": "active",
                "limit": 20
            }
        }
        
        response = client.post("/api/v1/cases/search", json=request_data)
        
        assert response.status_code == status.HTTP_200_OK
        
        response_data = response.json()
        assert len(response_data) == 1
        assert response_data[0]["case_id"] == "case-123"
        assert response_data[0]["title"] == "Test API Case"
        
        # Verify service was called correctly
        mock_case_service.search_cases.assert_called_once()
        call_args = mock_case_service.search_cases.call_args[0]
        search_request, user_id = call_args
        assert search_request.query == "database error"
        assert search_request.search_in_messages is True
        assert search_request.search_in_context is False
        assert user_id == "user-456"
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_search_cases_minimal(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case search with minimal parameters"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.search_cases.return_value = []
        
        request_data = {"query": "error"}
        
        response = client.post("/api/v1/cases/search", json=request_data)
        
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_search_cases_validation_error(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test case search with validation error"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.search_cases.side_effect = ValidationException("Query too short")
        
        request_data = {"query": "a"}
        
        response = client.post("/api/v1/cases/search", json=request_data)
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Query too short" in response.json()["detail"]
    
    def test_search_cases_missing_query(self, client):
        """Test case search without required query"""
        request_data = {"search_in_messages": True}
        
        response = client.post("/api/v1/cases/search", json=request_data)
        
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestCaseConversation:
    """Test case conversation context API endpoint"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_get_conversation_context_success(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test successful conversation context retrieval"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.get_case_conversation_context.return_value = "Previous conversation context"
        
        response = client.get("/api/v1/cases/case-123/conversation")
        
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == "Previous conversation context"
        
        # Verify service was called with default limit
        mock_case_service.get_case_conversation_context.assert_called_once_with("case-123", 10)
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_get_conversation_context_with_limit(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test conversation context retrieval with custom limit"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.get_case_conversation_context.return_value = "Limited context"
        
        response = client.get("/api/v1/cases/case-123/conversation", params={"limit": 5})
        
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == "Limited context"
        
        # Verify service was called with custom limit
        mock_case_service.get_case_conversation_context.assert_called_once_with("case-123", 5)
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_get_conversation_context_invalid_limit(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test conversation context with invalid limit"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        
        response = client.get("/api/v1/cases/case-123/conversation", params={"limit": 100})  # Over max of 50
        
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestAuthentication:
    """Test authentication and authorization for case endpoints"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_endpoint_without_authentication(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test endpoint behavior without authentication"""
        mock_get_user_id.return_value = None  # No authenticated user
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.list_user_cases.return_value = []
        
        response = client.get("/api/v1/cases/")
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify service was called with anonymous user
        mock_case_service.list_user_cases.assert_called_once_with("anonymous", mock.ANY)
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_without_user(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case):
        """Test case creation without authenticated user"""
        mock_get_user_id.return_value = None
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.create_case.return_value = sample_case
        
        request_data = {"title": "Anonymous Case"}
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        assert response.status_code == status.HTTP_201_CREATED
        
        # Verify case was created without owner
        mock_case_service.create_case.assert_called_once_with(
            title="Anonymous Case",
            description=None,
            owner_id=None,
            session_id=None,
            initial_message=None
        )


class TestErrorHandling:
    """Test error handling and edge cases"""
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    def test_dependency_injection_failure(self, mock_get_case_service, client):
        """Test behavior when dependency injection fails"""
        mock_get_case_service.side_effect = Exception("Service unavailable")
        
        response = client.get("/api/v1/cases/")
        
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_large_request_data(self, mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case):
        """Test handling of large request data"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.create_case.return_value = sample_case
        
        # Create large request data
        large_description = "x" * 10000  # 10KB description
        request_data = {
            "title": "Large Case",
            "description": large_description
        }
        
        response = client.post("/api/v1/cases/", json=request_data)
        
        # Should handle large data gracefully
        assert response.status_code in [status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST]
    
    def test_malformed_case_id(self, client):
        """Test handling of malformed case IDs"""
        # Test with various malformed IDs
        malformed_ids = ["", " ", "case with spaces", "case/with/slashes", "case%20encoded"]
        
        for case_id in malformed_ids:
            response = client.get(f"/api/v1/cases/{case_id}")
            # Should not cause server errors
            assert response.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR
    
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_concurrent_requests(self, mock_get_user_id, mock_get_case_service, client, mock_case_service):
        """Test handling of concurrent requests"""
        mock_get_user_id.return_value = "user-456"
        mock_get_case_service.return_value = mock_case_service
        mock_case_service.list_user_cases.return_value = []
        
        import threading
        import time
        
        results = []
        
        def make_request():
            response = client.get("/api/v1/cases/")
            results.append(response.status_code)
        
        # Create multiple concurrent requests
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=make_request)
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # All requests should succeed
        assert all(status == status.HTTP_200_OK for status in results)
        assert len(results) == 5


@pytest.mark.parametrize("endpoint,method,data", [
    ("/api/v1/cases/", "POST", {"title": "Test Case"}),
    ("/api/v1/cases/", "GET", None),
    ("/api/v1/cases/case-123", "GET", None),
    ("/api/v1/cases/case-123", "PUT", {"title": "Updated"}),
    ("/api/v1/cases/case-123/share", "POST", {"user_id": "user-789", "role": "viewer"}),
    ("/api/v1/cases/case-123/archive", "POST", None),
    ("/api/v1/cases/search", "POST", {"query": "test"}),
    ("/api/v1/cases/case-123/conversation", "GET", None),
])
@patch('faultmaven.api.v1.dependencies.get_case_service')
@patch('faultmaven.api.v1.dependencies.get_user_id')
def test_all_endpoints_basic_functionality(mock_get_user_id, mock_get_case_service, client, mock_case_service, sample_case, endpoint, method, data):
    """Test basic functionality of all case endpoints"""
    mock_get_user_id.return_value = "user-456"
    mock_get_case_service.return_value = mock_case_service
    
    # Configure mocks for different endpoints
    mock_case_service.create_case.return_value = sample_case
    mock_case_service.get_case.return_value = sample_case
    mock_case_service.update_case.return_value = True
    mock_case_service.share_case.return_value = True
    mock_case_service.archive_case.return_value = True
    mock_case_service.list_user_cases.return_value = []
    mock_case_service.search_cases.return_value = []
    mock_case_service.get_case_conversation_context.return_value = "context"
    
    # Make request based on method
    if method == "POST":
        if data:
            response = client.post(endpoint, json=data)
        else:
            response = client.post(endpoint)
    elif method == "GET":
        response = client.get(endpoint)
    elif method == "PUT":
        response = client.put(endpoint, json=data)
    else:
        pytest.skip(f"Method {method} not implemented in test")
    
    # Should not return server errors
    assert response.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR
    # Should return a valid HTTP status code
    assert 200 <= response.status_code < 600
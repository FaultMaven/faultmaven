"""Test module for session validation in case API endpoints.

This module tests the session validation logic added to case endpoints
to properly handle expired/invalid sessions with 401 responses.

Tests cover:
- Case creation with valid session
- Case creation with expired/invalid session
- Data upload with expired/invalid session
- Query submission with expired/invalid session
- HTTP 401 response format and error codes
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import status
from fastapi.testclient import TestClient

from faultmaven.main import app
from faultmaven.models.case import Case as CaseEntity
from faultmaven.models.api import SessionResponse


@pytest.fixture
def client():
    """Fixture providing FastAPI test client"""
    return TestClient(app)


@pytest.fixture
def valid_session():
    """Fixture providing a valid session"""
    return SessionResponse(
        session_id="session-valid-123",
        user_id="user-456",
        client_id="client-789",
        status="active",
        created_at="2025-10-16T00:00:00Z"
    )


@pytest.fixture
def mock_case_entity():
    """Fixture providing mock case entity"""
    return CaseEntity(
        case_id="case-123",
        title="Test Case",
        description="Test Description",
        owner_id="user-456",
        status="active",
        priority="medium"
    )


class TestCaseCreationSessionValidation:
    """Test session validation in case creation endpoint"""

    @patch('faultmaven.api.v1.routes.case.SessionService')
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_with_valid_session(
        self,
        mock_get_user_id,
        mock_get_case_service,
        mock_session_service_class,
        client,
        valid_session,
        mock_case_entity
    ):
        """Test case creation succeeds with valid session"""
        # Setup mocks
        mock_get_user_id.return_value = "user-456"

        mock_case_service = AsyncMock()
        mock_case_service.create_case = AsyncMock(return_value=mock_case_entity)
        mock_get_case_service.return_value = mock_case_service

        mock_session_service = AsyncMock()
        mock_session_service.get_session = AsyncMock(return_value=valid_session)
        mock_session_service_class.return_value = mock_session_service

        # Make request with valid session_id
        request_data = {
            "title": "Test Case",
            "description": "Test Description",
            "priority": "medium",
            "session_id": "session-valid-123"
        }

        response = client.post("/api/v1/cases/", json=request_data)

        # Should succeed
        assert response.status_code == status.HTTP_201_CREATED

        # Verify session was validated
        mock_session_service.get_session.assert_called_once_with(
            "session-valid-123",
            validate=True
        )

    @patch('faultmaven.api.v1.routes.case.SessionService')
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_with_expired_session(
        self,
        mock_get_user_id,
        mock_get_case_service,
        mock_session_service_class,
        client
    ):
        """Test case creation fails with 401 when session is expired"""
        # Setup mocks
        mock_get_user_id.return_value = "user-456"

        mock_case_service = AsyncMock()
        mock_get_case_service.return_value = mock_case_service

        # Session validation returns None (expired/invalid)
        mock_session_service = AsyncMock()
        mock_session_service.get_session = AsyncMock(return_value=None)
        mock_session_service_class.return_value = mock_session_service

        # Make request with expired session_id
        request_data = {
            "title": "Test Case",
            "description": "Test Description",
            "priority": "medium",
            "session_id": "session-expired-456"
        }

        response = client.post("/api/v1/cases/", json=request_data)

        # Should return 401
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

        # Verify error response format
        response_data = response.json()
        assert "error" in response_data
        assert response_data["error"]["code"] == "SESSION_EXPIRED"
        assert "expired" in response_data["error"]["message"].lower()

        # Verify session was validated
        mock_session_service.get_session.assert_called_once_with(
            "session-expired-456",
            validate=True
        )

        # Verify case was NOT created
        mock_case_service.create_case.assert_not_called()

    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_create_case_without_session(
        self,
        mock_get_user_id,
        mock_get_case_service,
        client,
        mock_case_entity
    ):
        """Test case creation succeeds without session_id (graceful degradation)"""
        # Setup mocks
        mock_get_user_id.return_value = "user-456"

        mock_case_service = AsyncMock()
        mock_case_service.create_case = AsyncMock(return_value=mock_case_entity)
        mock_get_case_service.return_value = mock_case_service

        # Make request without session_id
        request_data = {
            "title": "Test Case",
            "description": "Test Description",
            "priority": "medium"
            # No session_id provided
        }

        response = client.post("/api/v1/cases/", json=request_data)

        # Should succeed (graceful degradation)
        assert response.status_code == status.HTTP_201_CREATED


class TestDataUploadSessionValidation:
    """Test session validation in data upload endpoint"""

    @patch('faultmaven.api.v1.routes.case.SessionService')
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_upload_data_with_expired_session(
        self,
        mock_get_user_id,
        mock_get_case_service,
        mock_session_service_class,
        client,
        mock_case_entity
    ):
        """Test data upload fails with 401 when session is expired"""
        # Setup mocks
        mock_get_user_id.return_value = "user-456"

        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case_entity)
        mock_get_case_service.return_value = mock_case_service

        # Session validation returns None (expired/invalid)
        mock_session_service = AsyncMock()
        mock_session_service.get_session = AsyncMock(return_value=None)
        mock_session_service_class.return_value = mock_session_service

        # Make data upload request with expired session
        with open("/tmp/test_upload.txt", "w") as f:
            f.write("test data")

        with open("/tmp/test_upload.txt", "rb") as f:
            response = client.post(
                "/api/v1/cases/case-123/data",
                files={"file": ("test.txt", f, "text/plain")},
                data={
                    "session_id": "session-expired-789",
                    "description": "Test upload"
                }
            )

        # Should return 401
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

        # Verify error response format
        response_data = response.json()
        assert "error" in response_data
        assert response_data["error"]["code"] == "SESSION_EXPIRED"


class TestQuerySubmissionSessionValidation:
    """Test session validation in query submission endpoint"""

    @patch('faultmaven.api.v1.routes.case.SessionService')
    @patch('faultmaven.api.v1.dependencies.get_case_service')
    @patch('faultmaven.api.v1.dependencies.get_agent_service')
    @patch('faultmaven.api.v1.dependencies.get_user_id')
    def test_submit_query_with_expired_session(
        self,
        mock_get_user_id,
        mock_get_agent_service,
        mock_get_case_service,
        mock_session_service_class,
        client,
        mock_case_entity
    ):
        """Test query submission fails with 401 when session is expired"""
        # Setup mocks
        mock_get_user_id.return_value = "user-456"

        mock_case_service = AsyncMock()
        mock_case_service.get_case = AsyncMock(return_value=mock_case_entity)
        mock_get_case_service.return_value = mock_case_service

        mock_agent_service = AsyncMock()
        mock_get_agent_service.return_value = mock_agent_service

        # Session validation returns None (expired/invalid)
        mock_session_service = AsyncMock()
        mock_session_service.get_session = AsyncMock(return_value=None)
        mock_session_service_class.return_value = mock_session_service

        # Make query request with expired session
        request_data = {
            "session_id": "session-expired-321",
            "query": "What is wrong with my system?"
        }

        response = client.post("/api/v1/cases/case-123/queries", json=request_data)

        # Should return 401
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

        # Verify error response format
        response_data = response.json()
        assert "error" in response_data
        assert response_data["error"]["code"] == "SESSION_EXPIRED"

        # Verify agent was NOT called
        mock_agent_service.process_query.assert_not_called()

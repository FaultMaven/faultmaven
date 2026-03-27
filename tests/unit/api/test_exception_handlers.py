"""Unit tests for API exception handlers (TASK-014).

Tests:
- NotFoundError → 404 JSON response
- AuthorizationError → 403 JSON response
- ValidationException → 400 JSON response
- ConflictError → 409 JSON response
- ServiceError → 500 JSON response
- Error response format (error, detail, status_code fields)
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request, status
from fastapi.responses import JSONResponse

from faultmaven.api.exception_handlers import (
    authorization_exception_handler,
    conflict_exception_handler,
    get_exception_handlers,
    not_found_exception_handler,
    service_error_handler,
    validation_exception_handler,
)
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request."""
    request = MagicMock(spec=Request)
    request.method = "GET"
    request.url.path = "/api/v1/test"
    return request


# ============================================================
# NotFoundError Handler Tests
# ============================================================


class TestNotFoundExceptionHandler:
    """Tests for not_found_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_404_status(self, mock_request):
        """Test handler returns 404 status code."""
        exc = NotFoundError("Case", "case_123")
        response = await not_found_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = NotFoundError("Case", "case_123")
        response = await not_found_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Not Found"' in body
        assert '"status_code":404' in body
        assert "case_123" in body

    @pytest.mark.asyncio
    async def test_includes_exception_message(self, mock_request):
        """Test response includes exception message in detail."""
        exc = NotFoundError("Session", "session_456")
        response = await not_found_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Session" in body
        assert "session_456" in body

    @pytest.mark.asyncio
    async def test_different_resource_types(self, mock_request):
        """Test handler works with different resource types."""
        for resource_type in ["Case", "Session", "Evidence", "Execution"]:
            exc = NotFoundError(resource_type, "test_id")
            response = await not_found_exception_handler(mock_request, exc)
            body = response.body.decode()
            assert resource_type in body


# ============================================================
# AuthorizationError Handler Tests
# ============================================================


class TestAuthorizationExceptionHandler:
    """Tests for authorization_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_403_status(self, mock_request):
        """Test handler returns 403 status code."""
        exc = AuthorizationError("Access denied")
        response = await authorization_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = AuthorizationError("Not authorized to access this resource")
        response = await authorization_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Forbidden"' in body
        assert '"status_code":403' in body

    @pytest.mark.asyncio
    async def test_includes_error_message(self, mock_request):
        """Test response includes error message."""
        exc = AuthorizationError("Organization mismatch")
        response = await authorization_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Organization mismatch" in body

    @pytest.mark.asyncio
    async def test_default_message(self, mock_request):
        """Test default authorization message."""
        exc = AuthorizationError()  # Uses default message
        response = await authorization_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Not authorized" in body


# ============================================================
# ValidationException Handler Tests
# ============================================================


class TestValidationExceptionHandler:
    """Tests for validation_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_400_status(self, mock_request):
        """Test handler returns 422 status code (Unprocessable Entity)."""
        exc = ValidationException("Invalid input")
        response = await validation_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = ValidationException("Title is required")
        response = await validation_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Validation Error"' in body
        assert '"status_code":422' in body

    @pytest.mark.asyncio
    async def test_includes_validation_details(self, mock_request):
        """Test response includes validation error details."""
        exc = ValidationException("Field 'title' must be at least 1 character")
        response = await validation_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "title" in body
        assert "1 character" in body

    @pytest.mark.asyncio
    async def test_multiple_field_errors(self, mock_request):
        """Test handling of complex validation messages."""
        exc = ValidationException("title: required, description: too long")
        response = await validation_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "title" in body or "description" in body


# ============================================================
# ConflictError Handler Tests
# ============================================================


class TestConflictExceptionHandler:
    """Tests for conflict_exception_handler."""

    @pytest.mark.asyncio
    async def test_returns_409_status(self, mock_request):
        """Test handler returns 409 status code."""
        exc = ConflictError("Resource already exists")
        response = await conflict_exception_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = ConflictError("Case already closed")
        response = await conflict_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Conflict"' in body
        assert '"status_code":409' in body

    @pytest.mark.asyncio
    async def test_includes_conflict_details(self, mock_request):
        """Test response includes conflict details."""
        exc = ConflictError(
            "Active session already exists",
            resource_type="Session",
            resource_id="session_123",
            conflict_reason="active_session_exists",
        )
        response = await conflict_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "Active session" in body

    @pytest.mark.asyncio
    async def test_case_already_closed(self, mock_request):
        """Test conflict for already closed case."""
        exc = ConflictError(
            "Case is already closed",
            resource_type="Case",
            resource_id="case_123",
            conflict_reason="already_closed",
        )
        response = await conflict_exception_handler(mock_request, exc)
        body = response.body.decode()
        assert "already closed" in body


# ============================================================
# ServiceError Handler Tests
# ============================================================


class TestServiceErrorHandler:
    """Tests for service_error_handler."""

    @pytest.mark.asyncio
    async def test_returns_500_status(self, mock_request):
        """Test handler returns 500 status code."""
        exc = ServiceError("Database connection failed")
        response = await service_error_handler(mock_request, exc)
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @pytest.mark.asyncio
    async def test_response_format(self, mock_request):
        """Test response has correct format."""
        exc = ServiceError("Internal error occurred")
        response = await service_error_handler(mock_request, exc)
        body = response.body.decode()
        assert '"error":"Internal Server Error"' in body
        assert '"status_code":500' in body

    @pytest.mark.asyncio
    async def test_hides_internal_details(self, mock_request):
        """Test that internal error details are hidden from response."""
        exc = ServiceError("Database password: secret123, connection string: ...")
        response = await service_error_handler(mock_request, exc)
        body = response.body.decode()
        # Should NOT expose internal details
        assert "secret123" not in body
        assert "password" not in body
        # Should have generic message
        assert "An unexpected error occurred" in body


# ============================================================
# get_exception_handlers Tests
# ============================================================


class TestGetExceptionHandlers:
    """Tests for get_exception_handlers function."""

    def test_returns_dict(self):
        """Test function returns a dictionary."""
        handlers = get_exception_handlers()
        assert isinstance(handlers, dict)

    def test_includes_all_handlers(self):
        """Test dictionary includes all exception types."""
        handlers = get_exception_handlers()
        assert NotFoundError in handlers
        assert AuthorizationError in handlers
        assert ValidationException in handlers
        assert ConflictError in handlers
        assert ServiceError in handlers

    def test_handler_is_callable(self):
        """Test each handler is callable."""
        handlers = get_exception_handlers()
        for handler in handlers.values():
            assert callable(handler)

    def test_handler_count(self):
        """Test expected number of handlers."""
        handlers = get_exception_handlers()
        assert len(handlers) == 5


# ============================================================
# Response Format Consistency Tests
# ============================================================


class TestResponseFormatConsistency:
    """Tests for consistent response format across handlers."""

    @pytest.mark.asyncio
    async def test_all_responses_have_error_field(self, mock_request):
        """Test all error responses have 'error' field."""
        exceptions = [
            NotFoundError("Resource", "id"),
            AuthorizationError("Access denied"),
            ValidationException("Invalid"),
            ConflictError("Conflict"),
            ServiceError("Error"),
        ]
        handlers = [
            not_found_exception_handler,
            authorization_exception_handler,
            validation_exception_handler,
            conflict_exception_handler,
            service_error_handler,
        ]

        for exc, handler in zip(exceptions, handlers, strict=False):
            response = await handler(mock_request, exc)
            body = response.body.decode()
            assert '"error"' in body

    @pytest.mark.asyncio
    async def test_all_responses_have_status_code_field(self, mock_request):
        """Test all error responses have 'status_code' field."""
        exceptions = [
            NotFoundError("Resource", "id"),
            AuthorizationError("Access denied"),
            ValidationException("Invalid"),
            ConflictError("Conflict"),
            ServiceError("Error"),
        ]
        handlers = [
            not_found_exception_handler,
            authorization_exception_handler,
            validation_exception_handler,
            conflict_exception_handler,
            service_error_handler,
        ]

        for exc, handler in zip(exceptions, handlers, strict=False):
            response = await handler(mock_request, exc)
            body = response.body.decode()
            assert '"status_code"' in body

    @pytest.mark.asyncio
    async def test_all_responses_have_detail_field(self, mock_request):
        """Test all error responses have 'detail' field."""
        exceptions = [
            NotFoundError("Resource", "id"),
            AuthorizationError("Access denied"),
            ValidationException("Invalid"),
            ConflictError("Conflict"),
            ServiceError("Error"),
        ]
        handlers = [
            not_found_exception_handler,
            authorization_exception_handler,
            validation_exception_handler,
            conflict_exception_handler,
            service_error_handler,
        ]

        for exc, handler in zip(exceptions, handlers, strict=False):
            response = await handler(mock_request, exc)
            body = response.body.decode()
            assert '"detail"' in body

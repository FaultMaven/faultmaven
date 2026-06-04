"""Unit tests for API request/response models (TASK-014).

Tests:
- CaseCreateRequest validation (required fields, constraints)
- CaseUpdateRequest optional fields
- CaseResponse serialization from domain model
- SessionCreateRequest validation
- InvestigationSessionResponse serialization
- EvidenceResponse serialization
- Pydantic validation errors (min_length, ge constraints)
"""

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from faultmaven.api.models import (
    CaseCreateRequest,
    CaseListResponse,
    CaseResponse,
    CaseUpdateRequest,
    ErrorResponse,
    EvidenceUpdateRequest,
    InvestigationSessionResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionUpdateRequest,
    ValidationErrorResponse,
)
from faultmaven.models.investigation_session import SessionState
from faultmaven.modules.case.domain.models import CaseSeverity, CaseState

# ============================================================
# CaseCreateRequest Tests
# ============================================================


class TestCaseCreateRequest:
    """Tests for CaseCreateRequest model."""

    def test_create_request_valid(self):
        """Test valid case create request."""
        request = CaseCreateRequest(
            title="Test Case",
            description="This is a test case",
            severity=CaseSeverity.MEDIUM,
        )
        assert request.title == "Test Case"
        assert request.description == "This is a test case"
        assert request.severity == CaseSeverity.MEDIUM
        assert request.metadata is None

    def test_create_request_with_metadata(self):
        """Test case create request with metadata."""
        metadata = {"source": "api", "tags": ["test", "urgent"]}
        request = CaseCreateRequest(
            title="Test Case",
            description="Test",
            severity=CaseSeverity.HIGH,
            metadata=metadata,
        )
        assert request.metadata == metadata

    def test_create_request_title_min_length(self):
        """Test title minimum length validation."""
        with pytest.raises(ValidationError) as exc_info:
            CaseCreateRequest(
                title="",
                description="Test",
                severity=CaseSeverity.LOW,
            )
        errors = exc_info.value.errors()
        assert any("title" in str(e["loc"]) for e in errors)

    def test_create_request_title_max_length(self):
        """Test title maximum length validation."""
        long_title = "x" * 513
        with pytest.raises(ValidationError) as exc_info:
            CaseCreateRequest(
                title=long_title,
                description="Test",
                severity=CaseSeverity.LOW,
            )
        errors = exc_info.value.errors()
        assert any("title" in str(e["loc"]) for e in errors)

    def test_create_request_description_min_length(self):
        """Test description minimum length validation."""
        with pytest.raises(ValidationError) as exc_info:
            CaseCreateRequest(
                title="Test",
                description="",
                severity=CaseSeverity.LOW,
            )
        errors = exc_info.value.errors()
        assert any("description" in str(e["loc"]) for e in errors)

    def test_create_request_severity_required(self):
        """Test severity is required."""
        with pytest.raises(ValidationError) as exc_info:
            CaseCreateRequest(
                title="Test",
                description="Test description",
            )
        errors = exc_info.value.errors()
        assert any("severity" in str(e["loc"]) for e in errors)

    def test_create_request_all_severities(self):
        """Test all severity levels are valid."""
        for severity in CaseSeverity:
            request = CaseCreateRequest(
                title="Test",
                description="Test",
                severity=severity,
            )
            assert request.severity == severity


# ============================================================
# CaseUpdateRequest Tests
# ============================================================


class TestCaseUpdateRequest:
    """Tests for CaseUpdateRequest model."""

    def test_update_request_all_optional(self):
        """Test all fields are optional."""
        request = CaseUpdateRequest()
        assert request.title is None
        assert request.description is None
        assert request.severity is None
        assert request.state is None
        assert request.assigned_to is None
        assert request.metadata is None

    def test_update_request_partial(self):
        """Test partial update."""
        request = CaseUpdateRequest(
            title="Updated Title",
            state=CaseState.INVESTIGATING,
        )
        assert request.title == "Updated Title"
        assert request.state == CaseState.INVESTIGATING
        assert request.description is None

    def test_update_request_title_validation(self):
        """Test title validation when provided."""
        with pytest.raises(ValidationError):
            CaseUpdateRequest(title="")

    def test_update_request_title_max_length(self):
        """Test title max length when provided."""
        with pytest.raises(ValidationError):
            CaseUpdateRequest(title="x" * 513)

    def test_update_request_description_validation(self):
        """Test description validation when provided."""
        with pytest.raises(ValidationError):
            CaseUpdateRequest(description="")

    def test_update_request_with_metadata(self):
        """Test update with metadata."""
        metadata = {"updated_by": "system"}
        request = CaseUpdateRequest(metadata=metadata)
        assert request.metadata == metadata


# ============================================================
# CaseResponse Tests
# ============================================================


class TestCaseResponse:
    """Tests for CaseResponse model."""

    def test_case_response_all_fields(self):
        """Test case response with all fields."""
        now = datetime.now(timezone.utc)
        response = CaseResponse(
            case_id="case_123",
            organization_id="org_456",
            reporter_user_id="user_789",
            title="Test Case",
            description="Description",
            severity=CaseSeverity.HIGH,
            state=CaseState.INQUIRY,
            assigned_to="user_assigned",
            created_at=now,
            updated_at=now,
            closed_at=None,
            resolution=None,
            metadata={"key": "value"},
        )
        assert response.case_id == "case_123"
        assert response.severity == CaseSeverity.HIGH
        assert response.state == CaseState.INQUIRY

    def test_case_response_from_domain(self):
        """Test creating response from domain model."""
        mock_case = MagicMock()
        mock_case.case_id = "case_123"
        mock_case.organization_id = "org_456"
        mock_case.user_id = "user_789"
        mock_case.title = "Test Case"
        mock_case.description = "Description"
        mock_case.state = CaseState.INVESTIGATING
        mock_case.assigned_to = None
        mock_case.created_at = datetime.now(timezone.utc)
        mock_case.updated_at = datetime.now(timezone.utc)
        mock_case.closed_at = None
        mock_case.resolution = None
        mock_case.problem_verification = None
        mock_case.closure_reason = None
        mock_case.progress = None
        mock_case.metadata = None

        response = CaseResponse.from_domain(mock_case, severity=CaseSeverity.MEDIUM)
        assert response.case_id == "case_123"
        assert response.reporter_user_id == "user_789"
        assert response.severity == CaseSeverity.MEDIUM

    def test_case_response_from_domain_default_severity(self):
        """Test default severity when not provided."""
        mock_case = MagicMock()
        mock_case.case_id = "case_123"
        mock_case.organization_id = "org_456"
        mock_case.user_id = "user_789"
        mock_case.title = "Test Case"
        mock_case.description = "Description"
        mock_case.state = CaseState.INQUIRY
        mock_case.assigned_to = None
        mock_case.created_at = datetime.now(timezone.utc)
        mock_case.updated_at = datetime.now(timezone.utc)
        mock_case.closed_at = None
        mock_case.resolution = None
        mock_case.problem_verification = None
        mock_case.closure_reason = None
        mock_case.progress = None
        mock_case.metadata = None

        response = CaseResponse.from_domain(mock_case)
        assert response.severity == CaseSeverity.MEDIUM


class TestCaseListResponse:
    """Tests for CaseListResponse model."""

    def test_case_list_response(self):
        """Test case list response."""
        now = datetime.now(timezone.utc)
        item = CaseResponse(
            case_id="case_123",
            organization_id="org_456",
            reporter_user_id="user_789",
            title="Test Case",
            description="Description",
            severity=CaseSeverity.LOW,
            state=CaseState.INQUIRY,
            created_at=now,
            updated_at=now,
        )
        response = CaseListResponse(
            items=[item],
            total=1,
            limit=50,
            offset=0,
        )
        assert len(response.items) == 1
        assert response.total == 1


# ============================================================
# SessionCreateRequest Tests
# ============================================================


class TestSessionCreateRequest:
    """Tests for SessionCreateRequest model."""

    def test_session_create_all_optional(self):
        """Test all fields are optional."""
        request = SessionCreateRequest()
        assert request.session_goal is None
        assert request.token_budget_limit is None
        assert request.metadata is None

    def test_session_create_with_goal(self):
        """Test session create with goal."""
        request = SessionCreateRequest(
            session_goal="Investigate database issue",
        )
        assert request.session_goal == "Investigate database issue"

    def test_session_create_with_budget(self):
        """Test session create with budget."""
        request = SessionCreateRequest(
            token_budget_limit=10000,
        )
        assert request.token_budget_limit == 10000

    def test_session_create_budget_ge_zero(self):
        """Test budget must be >= 0."""
        with pytest.raises(ValidationError):
            SessionCreateRequest(token_budget_limit=-1)

    def test_session_create_budget_zero_allowed(self):
        """Test zero budget is allowed."""
        request = SessionCreateRequest(token_budget_limit=0)
        assert request.token_budget_limit == 0


# ============================================================
# SessionUpdateRequest Tests
# ============================================================


class TestSessionUpdateRequest:
    """Tests for SessionUpdateRequest model."""

    def test_session_update_all_optional(self):
        """Test all fields are optional."""
        request = SessionUpdateRequest()
        assert request.session_goal is None
        assert request.token_budget_limit is None
        assert request.metadata is None

    def test_session_update_budget_validation(self):
        """Test budget validation."""
        with pytest.raises(ValidationError):
            SessionUpdateRequest(token_budget_limit=-100)


# ============================================================
# InvestigationSessionResponse Tests
# ============================================================


class TestSessionResponse:
    """Tests for InvestigationSessionResponse model."""

    def test_session_response_all_fields(self):
        """Test session response with all fields."""
        now = datetime.now(timezone.utc)
        response = InvestigationSessionResponse(
            session_id="session_123",
            case_id="case_456",
            user_id="user_789",
            organization_id="org_abc",
            state=SessionState.ACTIVE,
            started_at=now,
            ended_at=None,
            last_activity_at=now,
            total_duration_ms=None,
            session_goal="Investigate issue",
            findings_summary=None,
            total_token_usage=1000,
            total_agent_executions=5,
            token_budget_limit=50000,
            created_at=now,
            updated_at=now,
        )
        assert response.session_id == "session_123"
        assert response.state == SessionState.ACTIVE
        assert response.total_token_usage == 1000

    def test_session_response_from_domain(self):
        """Test creating response from domain model."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()
        mock_session.session_id = "session_123"
        mock_session.case_id = "case_456"
        mock_session.user_id = "user_789"
        mock_session.organization_id = "org_abc"
        mock_session.state = SessionState.PAUSED
        mock_session.started_at = now
        mock_session.ended_at = None
        mock_session.last_activity_at = now
        mock_session.total_duration_ms = 60000
        mock_session.session_goal = "Goal"
        mock_session.findings_summary = None
        mock_session.total_token_usage = 500
        mock_session.total_agent_executions = 3
        mock_session.token_budget_limit = 10000
        mock_session.created_at = now
        mock_session.updated_at = now

        response = InvestigationSessionResponse.from_domain(mock_session)
        assert response.session_id == "session_123"
        assert response.state == SessionState.PAUSED
        assert response.total_duration_ms == 60000


# ============================================================
# EvidenceUpdateRequest Tests
# ============================================================


class TestEvidenceUpdateRequest:
    """Tests for EvidenceUpdateRequest model."""

    def test_evidence_update_all_optional(self):
        """Test all fields are optional."""
        request = EvidenceUpdateRequest()
        assert request.description is None
        assert request.is_primary is None
        assert request.metadata is None

    def test_evidence_update_description(self):
        """Test update with description."""
        request = EvidenceUpdateRequest(description="Updated description")
        assert request.description == "Updated description"

    def test_evidence_update_is_primary(self):
        """Test update is_primary flag."""
        request = EvidenceUpdateRequest(is_primary=True)
        assert request.is_primary is True


# EvidenceResponse / EvidenceListResponse tests removed in 2026-05 cleanup
# — both classes were deleted from faultmaven/api/models.py because they
# referenced dropped Evidence attributes (original_filename, evidence_type,
# mime_type, file_size, user_id) and were never imported by any consumer.
# Evidence is now exposed via the case-detail aggregate (case_ui_adapter).


# ============================================================
# ErrorResponse Tests
# ============================================================


class TestErrorResponse:
    """Tests for ErrorResponse model."""

    def test_error_response_basic(self):
        """Test basic error response."""
        response = ErrorResponse(
            error="Not Found",
            detail="Case not found: case_123",
            status_code=404,
        )
        assert response.error == "Not Found"
        assert response.detail == "Case not found: case_123"
        assert response.status_code == 404

    def test_error_response_no_detail(self):
        """Test error response without detail."""
        response = ErrorResponse(
            error="Internal Server Error",
            status_code=500,
        )
        assert response.error == "Internal Server Error"
        assert response.detail is None
        assert response.status_code == 500


class TestValidationErrorResponse:
    """Tests for ValidationErrorResponse model."""

    def test_validation_error_response(self):
        """Test validation error response."""
        response = ValidationErrorResponse(
            detail="Invalid input",
            errors=[
                {
                    "loc": ["body", "title"],
                    "msg": "field required",
                    "type": "value_error.missing",
                },
            ],
        )
        assert response.error == "Validation Error"
        assert response.status_code == 400
        assert len(response.errors) == 1


# ============================================================
# Additional Edge Cases
# ============================================================


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_case_response_with_closed_status(self):
        """Test case response with closed state."""
        now = datetime.now(timezone.utc)
        response = CaseResponse(
            case_id="case_123",
            organization_id="org_456",
            reporter_user_id="user_789",
            title="Closed Case",
            description="Description",
            severity=CaseSeverity.CRITICAL,
            state=CaseState.CLOSED,
            created_at=now,
            updated_at=now,
            closed_at=now,
            resolution="Issue was a duplicate",
        )
        assert response.state == CaseState.CLOSED
        assert response.closed_at is not None
        assert response.resolution == "Issue was a duplicate"

    def test_session_response_completed(self):
        """Test session response with completed state."""
        now = datetime.now(timezone.utc)
        response = InvestigationSessionResponse(
            session_id="session_123",
            case_id="case_456",
            user_id="user_789",
            organization_id="org_abc",
            state=SessionState.COMPLETED,
            started_at=now,
            ended_at=now,
            last_activity_at=now,
            total_duration_ms=120000,
            session_goal="Investigate",
            findings_summary="Root cause was memory leak",
            total_token_usage=5000,
            total_agent_executions=10,
            token_budget_limit=None,
            created_at=now,
            updated_at=now,
        )
        assert response.state == SessionState.COMPLETED
        assert response.findings_summary == "Root cause was memory leak"
        assert response.ended_at is not None

    def test_unicode_in_fields(self):
        """Test unicode characters in string fields."""
        request = CaseCreateRequest(
            title="Test Case with émojis 🐛",
            description="Description with unicode: 日本語テスト",
            severity=CaseSeverity.MEDIUM,
            metadata={"notes": "Special chars: éàü"},
        )
        assert "émojis" in request.title
        assert "日本語" in request.description

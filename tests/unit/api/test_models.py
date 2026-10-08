"""Unit tests for API request/response models (TASK-014).

Tests:
- SessionCreateRequest validation
- InvestigationSessionResponse serialization
- Pydantic validation errors (min_length, ge constraints)
"""

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from faultmaven.api.models import (
    InvestigationSessionResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionUpdateRequest,
)
from faultmaven.models.investigation_session import SessionState

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
            enterprise_id="ent_abc",
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
        mock_session.enterprise_id = "ent_abc"
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
# Additional Edge Cases
# ============================================================


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_session_response_completed(self):
        """Test session response with completed state."""
        now = datetime.now(timezone.utc)
        response = InvestigationSessionResponse(
            session_id="session_123",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="ent_abc",
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

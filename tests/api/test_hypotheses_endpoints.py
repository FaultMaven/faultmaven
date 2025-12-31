"""Test module for hypotheses & solutions API endpoints (TASK-026).

This module tests the REST API endpoints for hypothesis and solution management,
focusing on HTTP request/response handling, authentication, validation,
and error handling.

Tests cover:
- Hypothesis creation endpoint (POST /cases/{case_id}/hypotheses)
- Hypothesis listing endpoint (GET /cases/{case_id}/hypotheses)
- Hypothesis retrieval endpoint (GET /hypotheses/{hypothesis_id})
- Hypothesis update endpoint (PUT /hypotheses/{hypothesis_id})
- Hypothesis deletion endpoint (DELETE /hypotheses/{hypothesis_id})
- Solution creation endpoint (POST /cases/{case_id}/solutions)
- Solution listing endpoint (GET /cases/{case_id}/solutions)
- Solution retrieval endpoint (GET /solutions/{solution_id})
- Solution deletion endpoint (DELETE /solutions/{solution_id})
- Authentication and authorization
- Input validation and error handling
- HTTP status codes and responses
"""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from typing import List

from faultmaven.models.domain.hypothesis import Hypothesis, HypothesisStatus
from faultmaven.models.domain.solution import Solution
from faultmaven.models.auth import DevUser
from faultmaven.exceptions import ValidationException, NotFoundError


class MockTenantProvider:
    """Mock implementation of TenantProvider for API testing"""

    def __init__(self, organization_id: str = "org-default"):
        self.organization_id = organization_id
        self.get_current_organization = AsyncMock()
        self._setup_default_org()

    def _setup_default_org(self):
        """Setup mock organization response"""
        mock_org = Mock()
        mock_org.organization_id = self.organization_id
        mock_org.name = "Test Organization"
        self.get_current_organization.return_value = mock_org


class MockInvestigationOrchestrator:
    """Mock implementation of InvestigationOrchestrator for API testing"""

    def __init__(self):
        self.create_hypothesis = AsyncMock()
        self.list_hypotheses_for_case = AsyncMock(return_value=[])
        self.get_hypothesis = AsyncMock()
        self.update_hypothesis = AsyncMock()
        self.delete_hypothesis = AsyncMock()
        self.create_solution = AsyncMock()
        self.list_solutions_for_case = AsyncMock(return_value=[])
        self.get_solution = AsyncMock()
        self.delete_solution = AsyncMock()


@pytest.fixture
def mock_orchestrator():
    """Fixture providing mock investigation orchestrator"""
    return MockInvestigationOrchestrator()


@pytest.fixture
def mock_tenant_provider():
    """Fixture providing mock tenant provider"""
    return MockTenantProvider()


@pytest.fixture
def sample_hypothesis():
    """Fixture providing sample hypothesis for testing"""
    return Hypothesis(
        hypothesis_id="hyp-123",
        case_id="case-456",
        organization_id="org-default",
        title="Database connection pool exhausted",
        description="The application is experiencing connection pool exhaustion due to leaked connections.",
        confidence=0.8,
        status=HypothesisStatus.TESTING,
        evidence_supporting=["evidence-1", "evidence-2"],
        evidence_against=[],
        created_by="user-001",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_solution():
    """Fixture providing sample solution for testing"""
    return Solution(
        solution_id="sol-123",
        case_id="case-456",
        organization_id="org-default",
        hypothesis_id="hyp-123",
        title="Implement connection timeout and monitoring",
        description="Add connection timeout configuration and monitoring to prevent pool exhaustion.",
        implementation_steps=[
            "Set connection timeout to 30 seconds",
            "Add connection pool monitoring",
            "Implement connection leak detection"
        ],
        created_by="user-001",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def auth_user():
    """Fixture providing authenticated test user"""
    return DevUser(
        user_id="user-001",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        created_at=datetime.now(timezone.utc),
        is_dev_user=True
    )


# ============================================================
# Hypothesis Endpoint Tests
# ============================================================


@pytest.mark.asyncio
async def test_create_hypothesis_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_hypothesis,
    auth_user,
):
    """Test creating hypothesis with valid data returns 201"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mocks
    mock_orchestrator.create_hypothesis.return_value = sample_hypothesis

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.post(
            "/api/v1/cases/case-456/hypotheses",
            json={
                "title": "Database connection pool exhausted",
                "description": "The application is experiencing connection pool exhaustion due to leaked connections.",
                "confidence": 0.8,
                "evidence_supporting": ["evidence-1", "evidence-2"],
                "evidence_against": []
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["hypothesis_id"] == "hyp-123"
        assert data["title"] == "Database connection pool exhausted"
        assert data["confidence"] == 0.8
        assert data["status"] == "testing"


@pytest.mark.asyncio
async def test_create_hypothesis_validation_error(
    mock_orchestrator,
    mock_tenant_provider,
    auth_user,
):
    """Test creating hypothesis with invalid data returns 422"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock to raise validation error
    mock_orchestrator.create_hypothesis.side_effect = ValidationException(
        "Description must be 10-5000 characters"
    )

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.post(
            "/api/v1/cases/case-456/hypotheses",
            json={
                "title": "Short",
                "description": "Too short",
                "confidence": 0.5
            }
        )

        assert response.status_code == 422
        assert "Description must be 10-5000 characters" in response.json()["detail"]


@pytest.mark.asyncio
async def test_list_hypotheses_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_hypothesis,
    auth_user,
):
    """Test listing hypotheses for case returns 200"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock to return hypothesis list
    mock_orchestrator.list_hypotheses_for_case.return_value = [sample_hypothesis]

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.get("/api/v1/cases/case-456/hypotheses")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["case_id"] == "case-456"
        assert len(data["hypotheses"]) == 1
        assert data["hypotheses"][0]["hypothesis_id"] == "hyp-123"


@pytest.mark.asyncio
async def test_get_hypothesis_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_hypothesis,
    auth_user,
):
    """Test getting hypothesis details returns 200"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock
    mock_orchestrator.get_hypothesis.return_value = sample_hypothesis

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.get("/api/v1/hypotheses/hyp-123")

        assert response.status_code == 200
        data = response.json()
        assert data["hypothesis_id"] == "hyp-123"
        assert data["title"] == "Database connection pool exhausted"


@pytest.mark.asyncio
async def test_get_hypothesis_not_found(
    mock_orchestrator,
    mock_tenant_provider,
    auth_user,
):
    """Test getting non-existent hypothesis returns 404"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock to raise not found error
    mock_orchestrator.get_hypothesis.side_effect = NotFoundError(
        "Hypothesis hyp-999 not found"
    )

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.get("/api/v1/hypotheses/hyp-999")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_hypothesis_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_hypothesis,
    auth_user,
):
    """Test updating hypothesis returns 200"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Create updated hypothesis
    updated_hypothesis = Hypothesis(
        hypothesis_id=sample_hypothesis.hypothesis_id,
        case_id=sample_hypothesis.case_id,
        organization_id=sample_hypothesis.organization_id,
        title="Updated hypothesis title",
        description=sample_hypothesis.description,
        confidence=0.9,
        status=HypothesisStatus.VALIDATED,
        evidence_supporting=sample_hypothesis.evidence_supporting,
        evidence_against=sample_hypothesis.evidence_against,
        created_by=sample_hypothesis.created_by,
        created_at=sample_hypothesis.created_at,
        updated_at=datetime.now(timezone.utc),
        updated_by="user-001"
    )

    mock_orchestrator.update_hypothesis.return_value = updated_hypothesis

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.put(
            "/api/v1/hypotheses/hyp-123",
            json={
                "title": "Updated hypothesis title",
                "confidence": 0.9,
                "status": "validated"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated hypothesis title"
        assert data["confidence"] == 0.9
        assert data["status"] == "validated"


@pytest.mark.asyncio
async def test_delete_hypothesis_success(
    mock_orchestrator,
    mock_tenant_provider,
    auth_user,
):
    """Test deleting hypothesis returns 204"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.delete("/api/v1/hypotheses/hyp-123")

        assert response.status_code == 204


# ============================================================
# Solution Endpoint Tests
# ============================================================


@pytest.mark.asyncio
async def test_create_solution_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_solution,
    auth_user,
):
    """Test creating solution with valid data returns 201"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock
    mock_orchestrator.create_solution.return_value = sample_solution

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.post(
            "/api/v1/cases/case-456/solutions",
            json={
                "title": "Implement connection timeout and monitoring",
                "description": "Add connection timeout configuration and monitoring to prevent pool exhaustion.",
                "hypothesis_id": "hyp-123",
                "implementation_steps": [
                    "Set connection timeout to 30 seconds",
                    "Add connection pool monitoring",
                    "Implement connection leak detection"
                ]
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["solution_id"] == "sol-123"
        assert data["title"] == "Implement connection timeout and monitoring"
        assert data["hypothesis_id"] == "hyp-123"
        assert len(data["implementation_steps"]) == 3


@pytest.mark.asyncio
async def test_list_solutions_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_solution,
    auth_user,
):
    """Test listing solutions for case returns 200"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock
    mock_orchestrator.list_solutions_for_case.return_value = [sample_solution]

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.get("/api/v1/cases/case-456/solutions")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["case_id"] == "case-456"
        assert len(data["solutions"]) == 1
        assert data["solutions"][0]["solution_id"] == "sol-123"


@pytest.mark.asyncio
async def test_get_solution_success(
    mock_orchestrator,
    mock_tenant_provider,
    sample_solution,
    auth_user,
):
    """Test getting solution details returns 200"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    # Setup mock
    mock_orchestrator.get_solution.return_value = sample_solution

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.get("/api/v1/solutions/sol-123")

        assert response.status_code == 200
        data = response.json()
        assert data["solution_id"] == "sol-123"
        assert data["title"] == "Implement connection timeout and monitoring"


@pytest.mark.asyncio
async def test_delete_solution_success(
    mock_orchestrator,
    mock_tenant_provider,
    auth_user,
):
    """Test deleting solution returns 204"""
    from fastapi.testclient import TestClient
    from faultmaven.main import app

    with patch("faultmaven.api.v1.routes.hypotheses.get_investigation_orchestrator", return_value=mock_orchestrator), \
         patch("faultmaven.api.v1.routes.hypotheses.get_tenant_provider", return_value=mock_tenant_provider), \
         patch("faultmaven.api.v1.routes.hypotheses.require_authentication", return_value=auth_user):

        client = TestClient(app)
        response = client.delete("/api/v1/solutions/sol-123")

        assert response.status_code == 204

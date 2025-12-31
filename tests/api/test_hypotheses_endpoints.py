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
from unittest.mock import AsyncMock, Mock
from typing import List

from fastapi.testclient import TestClient

from faultmaven.models.domain.hypothesis import Hypothesis, HypothesisStatus
from faultmaven.models.domain.solution import Solution, SolutionStatus
from faultmaven.models.auth import DevUser
from faultmaven.exceptions import ValidationException, NotFoundError
from faultmaven.main import app


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
def sample_hypothesis_dict(sample_hypothesis):
    """Fixture providing sample hypothesis as API-compatible dictionary"""
    data = sample_hypothesis.to_dict()
    # Map domain fields to API fields
    data["proposed_at"] = data["created_at"]
    data["supporting_evidence_ids"] = data.pop("evidence_supporting", [])
    data["confidence_score"] = data.pop("confidence", 0.5)
    return data


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
        status=SolutionStatus.PROPOSED,
        created_by="user-001",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_solution_dict(sample_solution):
    """Fixture providing sample solution as API-compatible dictionary"""
    data = sample_solution.to_dict()
    # Map domain fields to API fields
    data["proposed_at"] = data["created_at"]
    return data


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


@pytest.fixture
def client(mock_orchestrator, mock_tenant_provider, auth_user):
    """Fixture providing test client with mocked dependencies"""
    from faultmaven.api.v1.routes.hypotheses import (
        get_investigation_orchestrator,
        get_tenant_provider,
    )
    from faultmaven.api.v1.auth_dependencies import require_authentication

    # Override dependencies
    app.dependency_overrides[get_investigation_orchestrator] = lambda: mock_orchestrator
    app.dependency_overrides[get_tenant_provider] = lambda: mock_tenant_provider
    app.dependency_overrides[require_authentication] = lambda: auth_user

    client = TestClient(app)
    yield client

    # Clean up overrides
    app.dependency_overrides.clear()


# ============================================================
# Hypothesis Endpoint Tests
# ============================================================


def test_create_hypothesis_success(client, mock_orchestrator, sample_hypothesis_dict):
    """Test creating hypothesis with valid data returns 201"""
    # Setup mock - orchestrator returns dictionary
    mock_orchestrator.create_hypothesis.return_value = sample_hypothesis_dict

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
    # API returns different field names than domain model
    assert "description" in data
    assert data["status"] == "testing"


def test_create_hypothesis_validation_error(client, mock_orchestrator):
    """Test creating hypothesis with invalid data returns 422"""
    # Pydantic validation will catch this before it hits the orchestrator
    # The description "Too short" is only 9 characters, min is 10

    response = client.post(
        "/api/v1/cases/case-456/hypotheses",
        json={
            "title": "Short",
            "description": "Too short",  # Only 9 characters, need 10+
            "confidence": 0.5
        }
    )

    assert response.status_code == 422
    # Pydantic validation error
    assert "detail" in response.json()


def test_list_hypotheses_success(client, mock_orchestrator, sample_hypothesis_dict):
    """Test listing hypotheses for case returns 200"""
    # Setup mock to return hypothesis list (as dictionaries)
    mock_orchestrator.list_hypotheses_for_case.return_value = [sample_hypothesis_dict]

    response = client.get("/api/v1/cases/case-456/hypotheses")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["case_id"] == "case-456"
    assert len(data["hypotheses"]) == 1
    assert data["hypotheses"][0]["hypothesis_id"] == "hyp-123"


def test_get_hypothesis_success(client, mock_orchestrator, sample_hypothesis_dict):
    """Test getting hypothesis details returns 200"""
    # Setup mock
    mock_orchestrator.get_hypothesis.return_value = sample_hypothesis_dict

    response = client.get("/api/v1/hypotheses/hyp-123")

    assert response.status_code == 200
    data = response.json()
    assert data["hypothesis_id"] == "hyp-123"
    assert "description" in data


def test_get_hypothesis_not_found(client, mock_orchestrator):
    """Test getting non-existent hypothesis returns 404"""
    # Setup mock to raise not found error
    mock_orchestrator.get_hypothesis.side_effect = NotFoundError(
        resource_type="Hypothesis",
        resource_id="hyp-999"
    )

    response = client.get("/api/v1/hypotheses/hyp-999")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_update_hypothesis_success(client, mock_orchestrator, sample_hypothesis_dict):
    """Test updating hypothesis returns 200"""
    # Create updated hypothesis dict
    updated_dict = sample_hypothesis_dict.copy()
    updated_dict["confidence_score"] = 0.9
    updated_dict["status"] = "validated"

    mock_orchestrator.update_hypothesis.return_value = updated_dict

    response = client.put(
        "/api/v1/hypotheses/hyp-123",
        json={
            "confidence_score": 0.9,
            "status": "validated"
        }
    )

    assert response.status_code == 200
    data = response.json()
    # Check that response contains updated values
    assert data["confidence_score"] == 0.9
    assert data["status"] == "validated"


def test_delete_hypothesis_success(client, mock_orchestrator):
    """Test deleting hypothesis returns 204"""
    response = client.delete("/api/v1/hypotheses/hyp-123")
    assert response.status_code == 204


# ============================================================
# Solution Endpoint Tests
# ============================================================


def test_create_solution_success(client, mock_orchestrator, sample_solution_dict):
    """Test creating solution with valid data returns 201"""
    # Setup mock
    mock_orchestrator.create_solution.return_value = sample_solution_dict

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
    assert "description" in data
    assert data["hypothesis_id"] == "hyp-123"
    assert len(data["implementation_steps"]) == 3


def test_list_solutions_success(client, mock_orchestrator, sample_solution_dict):
    """Test listing solutions for case returns 200"""
    # Setup mock
    mock_orchestrator.list_solutions_for_case.return_value = [sample_solution_dict]

    response = client.get("/api/v1/cases/case-456/solutions")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["case_id"] == "case-456"
    assert len(data["solutions"]) == 1
    assert data["solutions"][0]["solution_id"] == "sol-123"


def test_get_solution_success(client, mock_orchestrator, sample_solution_dict):
    """Test getting solution details returns 200"""
    # Setup mock
    mock_orchestrator.get_solution.return_value = sample_solution_dict

    response = client.get("/api/v1/solutions/sol-123")

    assert response.status_code == 200
    data = response.json()
    assert data["solution_id"] == "sol-123"
    assert "description" in data


def test_delete_solution_success(client, mock_orchestrator):
    """Test deleting solution returns 204"""
    response = client.delete("/api/v1/solutions/sol-123")
    assert response.status_code == 204

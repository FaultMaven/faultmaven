"""Unit tests for Investigation Orchestrator Service (TASK-026)

Tests the business logic layer for hypothesis-solution workflow orchestration,
including validation, status transitions, and linking constraints.

Test Coverage:
1. create_hypothesis_success - Happy path hypothesis creation
2. create_hypothesis_invalid_confidence - Confidence validation
3. update_status_to_validated_high_confidence - Valid status transition
4. update_status_to_validated_low_confidence_fails - Business rule enforcement
5. update_status_to_refuted_low_confidence - Valid refutation
6. link_solution_to_validated_hypothesis - Valid solution linking
7. link_solution_to_rejected_hypothesis_fails - Linking constraint enforcement
8. get_investigation_progress - Progress tracking

Testing Pattern: AsyncMock for repositories, verify business logic without database.
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from faultmaven.modules.agent.domain.services.investigation_orchestrator import (
    InvestigationOrchestrator,
)
from faultmaven.exceptions import (
    ValidationException,
    NotFoundError,
    ConflictError,
)


@pytest.fixture
def mock_hypothesis_repo():
    """Mock hypothesis repository for testing."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_solution_repo():
    """Mock solution repository for testing."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def orchestrator(mock_hypothesis_repo, mock_solution_repo):
    """Create InvestigationOrchestrator with mocked repositories."""
    return InvestigationOrchestrator(
        hypothesis_repo=mock_hypothesis_repo,
        solution_repo=mock_solution_repo,
    )


@pytest.fixture
def sample_hypothesis():
    """Create sample hypothesis dict for testing (matching repository return type)."""
    return {
        "hypothesis_id": "hyp_abc123456789",
        "case_id": "case_test123",
        "organization_id": "org_test456",
        "description": "Database connection pool exhaustion causing timeouts",
        "status": "captured",
        "confidence_score": 0.5,
        "supporting_evidence_ids": [],
        "validation_result": None,
        "validation_timestamp": None,
        "proposed_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "created_by": "user_test789",
        "updated_by": None,
        "metadata": {},
    }


@pytest.fixture
def sample_solution():
    """Create sample solution dict for testing (matching repository return type)."""
    return {
        "solution_id": "sol_xyz987654321",
        "case_id": "case_test123",
        "organization_id": "org_test456",
        "title": "Increase database connection pool size",
        "description": "Increase max_connections from 100 to 200",
        "steps": ["Update config", "Restart service"],
        "implemented": False,
        "applied_at": None,
        "verified_at": None,
        "hypothesis_id": None,
        "proposed_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "created_by": "user_test789",
        "updated_by": None,
        "metadata": {},
    }


# ============================================================
# Test 1: Create Hypothesis - Success
# ============================================================


@pytest.mark.asyncio
async def test_create_hypothesis_success(
    orchestrator, mock_hypothesis_repo, sample_hypothesis
):
    """Test successful hypothesis creation with valid inputs."""
    # Arrange
    case_id = "case_test123"
    organization_id = "org_test456"
    description = "Database connection pool exhausted"
    created_by = "user_test789"
    confidence = 0.6
    evidence_ids = ["ev_001", "ev_002"]
    metadata = {"source": "manual", "rationale": "Pool at 95%"}

    mock_hypothesis_repo.create_hypothesis.return_value = sample_hypothesis

    # Act
    result = await orchestrator.create_hypothesis(
        case_id=case_id,
        organization_id=organization_id,
        description=description,
        created_by=created_by,
        confidence=confidence,
        supporting_evidence_ids=evidence_ids,
        metadata=metadata,
    )

    # Assert
    assert result == sample_hypothesis
    mock_hypothesis_repo.create_hypothesis.assert_called_once_with(
        case_id=case_id,
        organization_id=organization_id,
        description=description,
        created_by=created_by,
        status="captured",
        confidence_score=Decimal("0.6"),
        supporting_evidence_ids=evidence_ids,
        metadata=metadata,
    )


# ============================================================
# Test 2: Create Hypothesis - Invalid Confidence
# ============================================================


@pytest.mark.asyncio
async def test_create_hypothesis_invalid_confidence(orchestrator):
    """Test hypothesis creation fails with invalid confidence score."""
    # Arrange
    case_id = "case_test123"
    organization_id = "org_test456"
    description = "Valid description with enough characters"
    created_by = "user_test789"
    invalid_confidence = 1.5  # Out of range

    # Act & Assert
    with pytest.raises(ValidationException) as exc_info:
        await orchestrator.create_hypothesis(
            case_id=case_id,
            organization_id=organization_id,
            description=description,
            created_by=created_by,
            confidence=invalid_confidence,
        )

    assert "Confidence score must be between 0.0 and 1.0" in str(exc_info.value)
    assert exc_info.value.details["confidence"] == 1.5


# ============================================================
# Test 3: Update Status to Validated - High Confidence
# ============================================================


@pytest.mark.asyncio
async def test_update_status_to_validated_high_confidence(
    orchestrator, mock_hypothesis_repo, sample_hypothesis
):
    """Test hypothesis can be validated with confidence ≥ 0.7."""
    # Arrange
    hypothesis_id = "hyp_abc123456789"
    organization_id = "org_test456"
    new_status = "validated"
    updated_by = "user_test789"
    high_confidence = 0.85

    # Set up initial hypothesis with high confidence
    initial_hypothesis = sample_hypothesis.copy()
    initial_hypothesis["confidence_score"] = 0.85

    # Set up updated hypothesis
    updated_hypothesis = sample_hypothesis.copy()
    updated_hypothesis["status"] = "validated"
    updated_hypothesis["confidence_score"] = 0.85

    mock_hypothesis_repo.get_hypothesis.return_value = initial_hypothesis
    mock_hypothesis_repo.update_hypothesis.return_value = updated_hypothesis

    # Act
    result = await orchestrator.update_hypothesis_status(
        hypothesis_id=hypothesis_id,
        organization_id=organization_id,
        new_status=new_status,
        updated_by=updated_by,
        confidence=high_confidence,
    )

    # Assert
    assert result["status"] == "validated"
    mock_hypothesis_repo.update_hypothesis.assert_called_once_with(
        hypothesis_id=hypothesis_id,
        organization_id=organization_id,
        status=new_status,
        confidence_score=Decimal("0.85"),
    )


# ============================================================
# Test 4: Update Status to Validated - Low Confidence Fails
# ============================================================


@pytest.mark.asyncio
async def test_update_status_to_validated_low_confidence_fails(
    orchestrator, mock_hypothesis_repo, sample_hypothesis
):
    """Test hypothesis cannot be validated with confidence < 0.7."""
    # Arrange
    hypothesis_id = "hyp_abc123456789"
    organization_id = "org_test456"
    new_status = "validated"
    updated_by = "user_test789"
    low_confidence = 0.5

    # Set up initial hypothesis with low confidence
    initial_hypothesis = sample_hypothesis.copy()
    initial_hypothesis["confidence_score"] = 0.5

    mock_hypothesis_repo.get_hypothesis.return_value = initial_hypothesis

    # Act & Assert
    with pytest.raises(ConflictError) as exc_info:
        await orchestrator.update_hypothesis_status(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
            new_status=new_status,
            updated_by=updated_by,
            confidence=low_confidence,
        )

    assert "Cannot validate hypothesis" in str(exc_info.value)
    assert "0.7" in str(exc_info.value)
    assert exc_info.value.conflict_reason == "Low confidence (0.5 < 0.7)"


# ============================================================
# Test 5: Update Status to Refuted - Low Confidence
# ============================================================


@pytest.mark.asyncio
async def test_update_status_to_refuted_low_confidence(
    orchestrator, mock_hypothesis_repo, sample_hypothesis
):
    """Test hypothesis can be refuted with confidence ≤ 0.3."""
    # Arrange
    hypothesis_id = "hyp_abc123456789"
    organization_id = "org_test456"
    new_status = "refuted"
    updated_by = "user_test789"
    low_confidence = 0.2

    # Set up initial hypothesis
    initial_hypothesis = sample_hypothesis.copy()
    initial_hypothesis["confidence_score"] = 0.2

    # Set up updated hypothesis
    updated_hypothesis = sample_hypothesis.copy()
    updated_hypothesis["status"] = "refuted"
    updated_hypothesis["confidence_score"] = 0.2

    mock_hypothesis_repo.get_hypothesis.return_value = initial_hypothesis
    mock_hypothesis_repo.update_hypothesis.return_value = updated_hypothesis

    # Act
    result = await orchestrator.update_hypothesis_status(
        hypothesis_id=hypothesis_id,
        organization_id=organization_id,
        new_status=new_status,
        updated_by=updated_by,
        confidence=low_confidence,
    )

    # Assert
    assert result["status"] == "refuted"
    mock_hypothesis_repo.update_hypothesis.assert_called_once_with(
        hypothesis_id=hypothesis_id,
        organization_id=organization_id,
        status=new_status,
        confidence_score=Decimal("0.2"),
    )


# ============================================================
# Test 6: Link Solution to Validated Hypothesis
# ============================================================


@pytest.mark.asyncio
async def test_link_solution_to_validated_hypothesis(
    orchestrator,
    mock_hypothesis_repo,
    mock_solution_repo,
    sample_hypothesis,
    sample_solution,
):
    """Test solution can be linked to validated hypothesis."""
    # Arrange
    solution_id = "sol_xyz987654321"
    hypothesis_id = "hyp_abc123456789"
    organization_id = "org_test456"

    # Set up validated hypothesis
    validated_hypothesis = sample_hypothesis.copy()
    validated_hypothesis["status"] = "validated"

    mock_hypothesis_repo.get_hypothesis.return_value = validated_hypothesis
    mock_solution_repo.get_solution.return_value = sample_solution
    mock_solution_repo.link_to_hypothesis.return_value = sample_solution

    # Act
    result = await orchestrator.link_solution_to_hypothesis(
        solution_id=solution_id,
        hypothesis_id=hypothesis_id,
        organization_id=organization_id,
    )

    # Assert
    assert result == sample_solution
    mock_solution_repo.link_to_hypothesis.assert_called_once_with(
        solution_id=solution_id,
        hypothesis_id=hypothesis_id,
        organization_id=organization_id,
    )


# ============================================================
# Test 7: Link Solution to Rejected Hypothesis Fails
# ============================================================


@pytest.mark.asyncio
async def test_link_solution_to_rejected_hypothesis_fails(
    orchestrator,
    mock_hypothesis_repo,
    mock_solution_repo,
    sample_hypothesis,
    sample_solution,
):
    """Test solution cannot be linked to refuted hypothesis."""
    # Arrange
    solution_id = "sol_xyz987654321"
    hypothesis_id = "hyp_abc123456789"
    organization_id = "org_test456"

    # Set up refuted hypothesis
    refuted_hypothesis = sample_hypothesis.copy()
    refuted_hypothesis["status"] = "refuted"

    mock_hypothesis_repo.get_hypothesis.return_value = refuted_hypothesis
    mock_solution_repo.get_solution.return_value = sample_solution

    # Act & Assert
    with pytest.raises(ConflictError) as exc_info:
        await orchestrator.link_solution_to_hypothesis(
            solution_id=solution_id,
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

    assert "Cannot link solution to refuted hypothesis" in str(exc_info.value)
    assert "Only validated hypotheses" in str(exc_info.value)


# ============================================================
# Test 8: Get Investigation Progress
# ============================================================


@pytest.mark.asyncio
async def test_get_investigation_progress(
    orchestrator,
    mock_hypothesis_repo,
    mock_solution_repo,
    sample_hypothesis,
    sample_solution,
):
    """Test investigation progress summary calculation."""
    # Arrange
    case_id = "case_test123"
    organization_id = "org_test456"

    # Create multiple hypotheses with different statuses
    hyp1 = sample_hypothesis.copy()
    hyp1["hypothesis_id"] = "hyp_001"
    hyp1["status"] = "validated"

    hyp2 = sample_hypothesis.copy()
    hyp2["hypothesis_id"] = "hyp_002"
    hyp2["status"] = "refuted"

    hyp3 = sample_hypothesis.copy()
    hyp3["hypothesis_id"] = "hyp_003"
    hyp3["status"] = "active"

    hyp4 = sample_hypothesis.copy()
    hyp4["hypothesis_id"] = "hyp_004"
    hyp4["status"] = "captured"

    mock_hypothesis_repo.list_by_case.return_value = [hyp1, hyp2, hyp3, hyp4]

    # Create solutions (2 implemented, 1 proposed)
    sol1 = sample_solution.copy()
    sol1["solution_id"] = "sol_001"
    sol1["applied_at"] = datetime.now(timezone.utc)

    sol2 = sample_solution.copy()
    sol2["solution_id"] = "sol_002"
    sol2["applied_at"] = datetime.now(timezone.utc)

    sol3 = sample_solution.copy()
    sol3["solution_id"] = "sol_003"
    sol3["applied_at"] = None  # Not implemented yet

    mock_solution_repo.list_by_case.return_value = [sol1, sol2, sol3]

    # Act
    progress = await orchestrator.get_investigation_progress(
        case_id=case_id,
        organization_id=organization_id,
    )

    # Assert
    assert progress["hypotheses"]["total"] == 4
    assert progress["hypotheses"]["validated"] == 1
    assert progress["hypotheses"]["refuted"] == 1
    assert progress["hypotheses"]["active"] == 1
    assert progress["hypotheses"]["completion_rate"] == 50.0  # (1+1)/4 * 100

    assert progress["solutions"]["total"] == 3
    assert progress["solutions"]["implemented"] == 2
    assert progress["solutions"]["implementation_rate"] == 66.7  # 2/3 * 100

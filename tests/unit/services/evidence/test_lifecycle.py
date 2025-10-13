"""
Comprehensive tests for evidence lifecycle management service.

Tests the lifecycle management of evidence requests including:
- Status transitions (PENDING → PARTIAL → COMPLETE / BLOCKED / OBSOLETE)
- Completeness score updates using max() logic (not additive)
- Evidence request deprecation when hypotheses change
- Evidence record creation and status tracking

Target Module: faultmaven/services/evidence/lifecycle.py
"""

import pytest
from datetime import datetime
from typing import List
from unittest.mock import patch, Mock

from faultmaven.models.evidence import (
    EvidenceRequest,
    EvidenceProvided,
    EvidenceClassification,
    EvidenceStatus,
    EvidenceCategory,
    CompletenessLevel,
    UserIntent,
    EvidenceForm,
    EvidenceType,
    FileMetadata,
    AcquisitionGuidance,
)
from faultmaven.models.case import CaseDiagnosticState
from faultmaven.services.evidence.lifecycle import (
    update_evidence_lifecycle,
    mark_obsolete_requests,
    get_active_evidence_requests,
    create_evidence_record,
    summarize_evidence_status,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_acquisition_guidance():
    """Sample acquisition guidance for evidence requests."""
    return AcquisitionGuidance(
        commands=["kubectl logs -l app=api --since=2h"],
        file_locations=["/var/log/application.log"],
        ui_locations=["Dashboard > Errors"],
        alternatives=["Check monitoring dashboard"],
        prerequisites=["kubectl access"],
        expected_output="Error count and patterns"
    )


@pytest.fixture
def sample_evidence_request(sample_acquisition_guidance):
    """Sample evidence request in PENDING state."""
    return EvidenceRequest(
        request_id="req-001",
        label="Error rate metrics",
        description="Current error rate vs baseline",
        category=EvidenceCategory.METRICS,
        guidance=sample_acquisition_guidance,
        status=EvidenceStatus.PENDING,
        created_at_turn=1,
        completeness=0.0,
        metadata={"hypothesis_id": "hyp-001"}
    )


@pytest.fixture
def sample_evidence_request_with_completeness(sample_acquisition_guidance):
    """Sample evidence request with existing completeness score."""
    return EvidenceRequest(
        request_id="req-002",
        label="Log analysis",
        description="Recent error logs",
        category=EvidenceCategory.SYMPTOMS,
        guidance=sample_acquisition_guidance,
        status=EvidenceStatus.PARTIAL,
        created_at_turn=1,
        completeness=0.5,
        metadata={"hypothesis_id": "hyp-001"}
    )


@pytest.fixture
def diagnostic_state_with_requests(sample_evidence_request, sample_evidence_request_with_completeness):
    """Diagnostic state with sample evidence requests."""
    return CaseDiagnosticState(
        evidence_requests=[
            sample_evidence_request,
            sample_evidence_request_with_completeness,
        ],
        evidence_provided=[]
    )


@pytest.fixture
def classification_partial():
    """Classification with partial completeness (0.5)."""
    return EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.5,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE,
        rationale="Provides some information but needs more detail"
    )


@pytest.fixture
def classification_complete():
    """Classification with complete completeness (0.9)."""
    return EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.COMPLETE,
        completeness_score=0.9,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE,
        rationale="Fully answers the evidence request"
    )


@pytest.fixture
def classification_low():
    """Classification with low completeness (0.2)."""
    return EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.2,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE,
        rationale="Minimal information provided"
    )


@pytest.fixture
def classification_unavailable():
    """Classification for unavailable evidence."""
    return EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.0,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.ABSENCE,
        user_intent=UserIntent.REPORTING_UNAVAILABLE,
        rationale="User cannot access this information"
    )


@pytest.fixture
def sample_evidence_provided():
    """Sample evidence provided record."""
    return EvidenceProvided(
        evidence_id="ev-001",
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content="Error rate is approximately 5% in production",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE,
        key_findings=[],
        confidence_impact=None
    )


@pytest.fixture
def sample_file_metadata():
    """Sample file metadata for document uploads."""
    return FileMetadata(
        filename="error_logs.txt",
        content_type="text/plain",
        size_bytes=2048,
        upload_timestamp=datetime.now(),
        file_id="file-001"
    )


# =============================================================================
# Test 1: test_pending_to_partial_transition
# =============================================================================


def test_pending_to_partial_transition(
    diagnostic_state_with_requests,
    sample_evidence_provided,
    classification_partial
):
    """Test evidence with 0.3-0.7 score transitions to PARTIAL status."""
    # Arrange
    state = diagnostic_state_with_requests
    initial_request = state.evidence_requests[0]
    assert initial_request.status == EvidenceStatus.PENDING
    assert initial_request.completeness == 0.0

    # Act
    update_evidence_lifecycle(
        evidence_provided=sample_evidence_provided,
        classification=classification_partial,
        diagnostic_state=state,
        current_turn=2
    )

    # Assert
    updated_request = state.evidence_requests[0]
    assert updated_request.status == EvidenceStatus.PARTIAL, \
        "Request with 0.5 completeness should be PARTIAL"
    assert updated_request.completeness == 0.5
    assert updated_request.updated_at_turn == 2
    assert len(state.evidence_provided) == 1
    assert state.evidence_provided[0] == sample_evidence_provided


# =============================================================================
# Test 2: test_partial_to_complete_transition
# =============================================================================


def test_partial_to_complete_transition(
    diagnostic_state_with_requests,
    sample_evidence_provided,
    classification_complete
):
    """Test evidence with 0.8+ score transitions to COMPLETE status."""
    # Arrange
    state = diagnostic_state_with_requests
    initial_request = state.evidence_requests[0]
    initial_request.status = EvidenceStatus.PARTIAL
    initial_request.completeness = 0.6

    # Act
    update_evidence_lifecycle(
        evidence_provided=sample_evidence_provided,
        classification=classification_complete,
        diagnostic_state=state,
        current_turn=3
    )

    # Assert
    updated_request = state.evidence_requests[0]
    assert updated_request.status == EvidenceStatus.COMPLETE, \
        "Request with 0.9 completeness should be COMPLETE"
    assert updated_request.completeness == 0.9
    assert updated_request.updated_at_turn == 3


# =============================================================================
# Test 3: test_blocked_status_on_unavailable
# =============================================================================


def test_blocked_status_on_unavailable(
    diagnostic_state_with_requests,
    sample_evidence_provided,
    classification_unavailable
):
    """Test UserIntent.REPORTING_UNAVAILABLE sets status to BLOCKED."""
    # Arrange
    state = diagnostic_state_with_requests
    initial_request = state.evidence_requests[0]
    evidence_content = "I don't have access to the production metrics dashboard"
    sample_evidence_provided.content = evidence_content

    # Act
    update_evidence_lifecycle(
        evidence_provided=sample_evidence_provided,
        classification=classification_unavailable,
        diagnostic_state=state,
        current_turn=2
    )

    # Assert
    updated_request = state.evidence_requests[0]
    assert updated_request.status == EvidenceStatus.BLOCKED, \
        "Request should be BLOCKED when user reports unavailable"
    assert "blocked_reason" in updated_request.metadata
    assert updated_request.metadata["blocked_reason"] == evidence_content[:200]
    assert updated_request.metadata["blocked_at_turn"] == 2


# =============================================================================
# Test 4: test_obsolete_status_on_hypothesis_refuted
# =============================================================================


def test_obsolete_status_on_hypothesis_refuted(diagnostic_state_with_requests):
    """Test mark_obsolete_requests() marks requests as OBSOLETE."""
    # Arrange
    state = diagnostic_state_with_requests
    request1 = state.evidence_requests[0]
    request2 = state.evidence_requests[1]

    # Both requests linked to hyp-001
    request1.metadata["hypothesis_id"] = "hyp-001"
    request2.metadata["hypothesis_id"] = "hyp-001"

    # Act
    count = mark_obsolete_requests(
        diagnostic_state=state,
        hypothesis_ids_to_deprecate=["hyp-001"],
        current_turn=5
    )

    # Assert
    assert count == 2, "Both requests should be marked obsolete"
    assert request1.status == EvidenceStatus.OBSOLETE
    assert request2.status == EvidenceStatus.OBSOLETE
    assert request1.metadata["obsolete_reason"] == "Associated hypothesis was refuted/changed"
    assert request1.updated_at_turn == 5
    assert request2.updated_at_turn == 5


# =============================================================================
# Test 5: test_max_completeness_logic_not_additive
# =============================================================================


def test_max_completeness_logic_not_additive(
    diagnostic_state_with_requests,
    classification_partial,
    classification_low
):
    """Test max() logic for completeness - two 0.5 scores = 0.5, not 1.0."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]

    # First evidence: 0.5 completeness
    evidence1 = EvidenceProvided(
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content="First piece of evidence",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification1 = classification_partial  # 0.5 score

    # Act - First evidence
    update_evidence_lifecycle(evidence1, classification1, state, current_turn=2)

    # Assert - First evidence
    assert request.completeness == 0.5, "Completeness should be 0.5 after first evidence"
    assert request.status == EvidenceStatus.PARTIAL

    # Second evidence: 0.2 completeness (lower)
    evidence2 = EvidenceProvided(
        turn_number=3,
        form=EvidenceForm.USER_INPUT,
        content="Second piece of evidence",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification2 = classification_low  # 0.2 score

    # Act - Second evidence (lower score)
    update_evidence_lifecycle(evidence2, classification2, state, current_turn=3)

    # Assert - Should remain 0.5 (max), NOT 0.7 (additive)
    assert request.completeness == 0.5, \
        "Completeness should remain 0.5 (max), not increase to 0.7 (additive)"

    # Third evidence: 0.9 completeness (higher)
    evidence3 = EvidenceProvided(
        turn_number=4,
        form=EvidenceForm.USER_INPUT,
        content="Third piece of evidence",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.COMPLETE,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification3 = EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.COMPLETE,
        completeness_score=0.9,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )

    # Act - Third evidence (higher score)
    update_evidence_lifecycle(evidence3, classification3, state, current_turn=4)

    # Assert - Should update to 0.9 (new max)
    assert request.completeness == 0.9, \
        "Completeness should update to 0.9 (new max)"
    assert request.status == EvidenceStatus.COMPLETE


# =============================================================================
# Test 6: test_get_active_evidence_requests_filtering
# =============================================================================


def test_get_active_evidence_requests_filtering(diagnostic_state_with_requests):
    """Test get_active_evidence_requests() returns only PENDING/PARTIAL."""
    # Arrange
    state = diagnostic_state_with_requests

    # Set different statuses
    state.evidence_requests[0].status = EvidenceStatus.PENDING
    state.evidence_requests[1].status = EvidenceStatus.PARTIAL

    # Add COMPLETE, BLOCKED, OBSOLETE requests
    state.evidence_requests.append(
        EvidenceRequest(
            request_id="req-complete",
            label="Complete request",
            description="Already completed",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.COMPLETE,
            created_at_turn=1
        )
    )
    state.evidence_requests.append(
        EvidenceRequest(
            request_id="req-blocked",
            label="Blocked request",
            description="Cannot provide",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1
        )
    )
    state.evidence_requests.append(
        EvidenceRequest(
            request_id="req-obsolete",
            label="Obsolete request",
            description="No longer needed",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.OBSOLETE,
            created_at_turn=1
        )
    )

    # Act
    active_requests = get_active_evidence_requests(state)

    # Assert
    assert len(active_requests) == 2, \
        "Should only return PENDING and PARTIAL requests"
    assert all(
        req.status in [EvidenceStatus.PENDING, EvidenceStatus.PARTIAL]
        for req in active_requests
    )
    assert active_requests[0].request_id == "req-001"
    assert active_requests[1].request_id == "req-002"


# =============================================================================
# Test 7: test_create_evidence_record_creation
# =============================================================================


def test_create_evidence_record_creation(classification_complete, sample_file_metadata):
    """Test create_evidence_record() creates EvidenceProvided correctly."""
    # Arrange
    content = "Detailed error metrics from production"
    turn_number = 5

    # Act
    evidence = create_evidence_record(
        content=content,
        classification=classification_complete,
        turn_number=turn_number,
        file_metadata=sample_file_metadata
    )

    # Assert
    assert isinstance(evidence, EvidenceProvided)
    assert evidence.content == content
    assert evidence.turn_number == turn_number
    assert evidence.form == classification_complete.form
    assert evidence.file_metadata == sample_file_metadata
    assert evidence.addresses_requests == classification_complete.matched_request_ids
    assert evidence.completeness == classification_complete.completeness
    assert evidence.evidence_type == classification_complete.evidence_type
    assert evidence.user_intent == classification_complete.user_intent
    assert evidence.key_findings == []
    assert evidence.confidence_impact is None


# =============================================================================
# Test 8: test_summarize_evidence_status_reporting
# =============================================================================


def test_summarize_evidence_status_reporting(diagnostic_state_with_requests):
    """Test summarize_evidence_status() counts requests by status."""
    # Arrange
    state = diagnostic_state_with_requests

    # Set up various statuses
    state.evidence_requests[0].status = EvidenceStatus.PENDING
    state.evidence_requests[1].status = EvidenceStatus.PARTIAL

    # Add more requests
    state.evidence_requests.append(
        EvidenceRequest(
            request_id="req-003",
            label="Complete",
            description="Complete request",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.COMPLETE,
            created_at_turn=1
        )
    )
    state.evidence_requests.append(
        EvidenceRequest(
            request_id="req-004",
            label="Blocked",
            description="Blocked request",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1
        )
    )
    state.evidence_requests.append(
        EvidenceRequest(
            request_id="req-005",
            label="Obsolete",
            description="Obsolete request",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.OBSOLETE,
            created_at_turn=1
        )
    )

    # Add evidence provided
    state.evidence_provided.append(
        EvidenceProvided(
            turn_number=2,
            form=EvidenceForm.USER_INPUT,
            content="Evidence 1",
            addresses_requests=["req-001"],
            completeness=CompletenessLevel.PARTIAL,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE
        )
    )
    state.evidence_provided.append(
        EvidenceProvided(
            turn_number=3,
            form=EvidenceForm.USER_INPUT,
            content="Evidence 2",
            addresses_requests=["req-002"],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE
        )
    )

    # Act
    summary = summarize_evidence_status(state)

    # Assert
    assert summary["total_requests"] == 5
    assert summary["evidence_provided_count"] == 2
    assert summary["status_breakdown"]["pending"] == 1
    assert summary["status_breakdown"]["partial"] == 1
    assert summary["status_breakdown"]["complete"] == 1
    assert summary["status_breakdown"]["blocked"] == 1
    assert summary["status_breakdown"]["obsolete"] == 1
    assert summary["completion_rate"] == 1/5  # 1 complete out of 5


# =============================================================================
# Additional Test: test_multiple_evidence_for_same_request
# =============================================================================


def test_multiple_evidence_for_same_request(diagnostic_state_with_requests):
    """Test multiple evidence submissions for same request use max logic."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]

    # Submit three pieces of evidence with scores: 0.4, 0.6, 0.5
    evidence_submissions = [
        (0.4, CompletenessLevel.PARTIAL, 2),
        (0.6, CompletenessLevel.PARTIAL, 3),
        (0.5, CompletenessLevel.PARTIAL, 4),
    ]

    for score, level, turn in evidence_submissions:
        evidence = EvidenceProvided(
            turn_number=turn,
            form=EvidenceForm.USER_INPUT,
            content=f"Evidence with score {score}",
            addresses_requests=["req-001"],
            completeness=level,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE
        )
        classification = EvidenceClassification(
            matched_request_ids=["req-001"],
            completeness=level,
            completeness_score=score,
            form=EvidenceForm.USER_INPUT,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE
        )
        update_evidence_lifecycle(evidence, classification, state, current_turn=turn)

    # Assert - Should be 0.6 (max), not 1.5 (sum)
    assert request.completeness == 0.6, \
        "Completeness should be max(0.4, 0.6, 0.5) = 0.6, not sum"
    assert request.status == EvidenceStatus.PARTIAL
    assert len(state.evidence_provided) == 3


# =============================================================================
# Additional Test: test_evidence_updates_turn_number
# =============================================================================


def test_evidence_updates_turn_number(
    diagnostic_state_with_requests,
    sample_evidence_provided,
    classification_partial
):
    """Test evidence updates correctly track turn numbers."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]
    assert request.updated_at_turn is None

    # Act
    update_evidence_lifecycle(
        sample_evidence_provided,
        classification_partial,
        state,
        current_turn=7
    )

    # Assert
    assert request.updated_at_turn == 7, "Turn number should be updated"


# =============================================================================
# Additional Test: test_blocked_reason_metadata
# =============================================================================


def test_blocked_reason_metadata(
    diagnostic_state_with_requests,
    classification_unavailable
):
    """Test blocked status includes reason metadata."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]
    long_reason = "A" * 300  # 300 characters, should be truncated to 200

    evidence = EvidenceProvided(
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content=long_reason,
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.ABSENCE,
        user_intent=UserIntent.REPORTING_UNAVAILABLE
    )

    # Act
    update_evidence_lifecycle(
        evidence,
        classification_unavailable,
        state,
        current_turn=2
    )

    # Assert
    assert request.status == EvidenceStatus.BLOCKED
    assert "blocked_reason" in request.metadata
    assert len(request.metadata["blocked_reason"]) == 200, \
        "Blocked reason should be truncated to 200 chars"
    assert request.metadata["blocked_at_turn"] == 2


# =============================================================================
# Additional Test: test_obsolete_reason_metadata
# =============================================================================


def test_obsolete_reason_metadata(diagnostic_state_with_requests):
    """Test obsolete status includes reason metadata."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]
    request.metadata["hypothesis_id"] = "hyp-refuted"

    # Act
    count = mark_obsolete_requests(
        state,
        hypothesis_ids_to_deprecate=["hyp-refuted"],
        current_turn=10
    )

    # Assert
    assert count == 1
    assert request.status == EvidenceStatus.OBSOLETE
    assert "obsolete_reason" in request.metadata
    assert request.metadata["obsolete_reason"] == "Associated hypothesis was refuted/changed"
    assert request.updated_at_turn == 10


# =============================================================================
# Additional Test: test_unknown_request_id_warning
# =============================================================================


def test_unknown_request_id_warning(diagnostic_state_with_requests, classification_partial):
    """Test warning when classification matches unknown request ID."""
    # Arrange
    state = diagnostic_state_with_requests
    classification_partial.matched_request_ids = ["req-unknown"]

    evidence = EvidenceProvided(
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content="Evidence for unknown request",
        addresses_requests=["req-unknown"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )

    # Act - Should not raise, but log warning
    with patch("faultmaven.services.evidence.lifecycle.logger") as mock_logger:
        update_evidence_lifecycle(evidence, classification_partial, state, current_turn=2)

        # Assert
        mock_logger.warning.assert_called_once()
        assert "unknown request ID" in mock_logger.warning.call_args[0][0]

    # Evidence should still be added
    assert len(state.evidence_provided) == 1


# =============================================================================
# Additional Test: test_empty_evidence_requests_list
# =============================================================================


def test_empty_evidence_requests_list():
    """Test functions handle empty evidence requests list."""
    # Arrange
    state = CaseDiagnosticState(
        evidence_requests=[],
        evidence_provided=[]
    )

    # Act & Assert - get_active_evidence_requests
    active = get_active_evidence_requests(state)
    assert active == []

    # Act & Assert - mark_obsolete_requests
    count = mark_obsolete_requests(state, ["hyp-001"], current_turn=1)
    assert count == 0

    # Act & Assert - summarize_evidence_status
    summary = summarize_evidence_status(state)
    assert summary["total_requests"] == 0
    assert summary["evidence_provided_count"] == 0
    assert summary["completion_rate"] == 0.0


# =============================================================================
# Additional Test: test_completion_rate_calculation
# =============================================================================


def test_completion_rate_calculation():
    """Test completion rate calculation in summary."""
    # Arrange
    state = CaseDiagnosticState(
        evidence_requests=[
            EvidenceRequest(
                request_id=f"req-{i}",
                label=f"Request {i}",
                description="Test",
                category=EvidenceCategory.METRICS,
                guidance=AcquisitionGuidance(),
                status=EvidenceStatus.COMPLETE if i < 3 else EvidenceStatus.PENDING,
                created_at_turn=1
            )
            for i in range(10)
        ],
        evidence_provided=[]
    )

    # Act
    summary = summarize_evidence_status(state)

    # Assert
    assert summary["total_requests"] == 10
    assert summary["status_breakdown"]["complete"] == 3
    assert summary["status_breakdown"]["pending"] == 7
    assert summary["completion_rate"] == 0.3  # 3/10


# =============================================================================
# Additional Test: test_obsolete_skips_already_complete
# =============================================================================


def test_obsolete_skips_already_complete(diagnostic_state_with_requests):
    """Test mark_obsolete_requests skips already COMPLETE requests."""
    # Arrange
    state = diagnostic_state_with_requests
    request1 = state.evidence_requests[0]
    request2 = state.evidence_requests[1]

    request1.metadata["hypothesis_id"] = "hyp-001"
    request2.metadata["hypothesis_id"] = "hyp-001"

    # Mark first request as complete
    request1.status = EvidenceStatus.COMPLETE

    # Act
    count = mark_obsolete_requests(state, ["hyp-001"], current_turn=5)

    # Assert
    assert count == 1, "Should only mark 1 request obsolete (skip COMPLETE)"
    assert request1.status == EvidenceStatus.COMPLETE, \
        "COMPLETE status should not change to OBSOLETE"
    assert request2.status == EvidenceStatus.OBSOLETE


# =============================================================================
# Additional Test: test_obsolete_skips_already_obsolete
# =============================================================================


def test_obsolete_skips_already_obsolete(diagnostic_state_with_requests):
    """Test mark_obsolete_requests skips already OBSOLETE requests."""
    # Arrange
    state = diagnostic_state_with_requests
    request1 = state.evidence_requests[0]
    request2 = state.evidence_requests[1]

    request1.metadata["hypothesis_id"] = "hyp-001"
    request2.metadata["hypothesis_id"] = "hyp-001"

    # Mark first request as obsolete
    request1.status = EvidenceStatus.OBSOLETE

    # Act
    count = mark_obsolete_requests(state, ["hyp-001"], current_turn=5)

    # Assert
    assert count == 1, "Should only mark 1 request obsolete (skip already OBSOLETE)"
    assert request1.status == EvidenceStatus.OBSOLETE
    assert request2.status == EvidenceStatus.OBSOLETE


# =============================================================================
# Additional Test: test_create_evidence_record_without_file_metadata
# =============================================================================


def test_create_evidence_record_without_file_metadata(classification_complete):
    """Test create_evidence_record() works without file metadata."""
    # Arrange
    content = "User-provided text evidence"
    turn_number = 3

    # Act
    evidence = create_evidence_record(
        content=content,
        classification=classification_complete,
        turn_number=turn_number,
        file_metadata=None
    )

    # Assert
    assert evidence.file_metadata is None
    assert evidence.form == EvidenceForm.USER_INPUT
    assert evidence.content == content


# =============================================================================
# Additional Test: test_boundary_completeness_scores
# =============================================================================


def test_boundary_completeness_scores(diagnostic_state_with_requests):
    """Test boundary completeness scores (0.3, 0.7, 0.8) for status transitions."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]

    # Test 0.3 boundary (should be PARTIAL)
    evidence1 = EvidenceProvided(
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content="Evidence 1",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification1 = EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.3,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )

    update_evidence_lifecycle(evidence1, classification1, state, current_turn=2)
    assert request.status == EvidenceStatus.PARTIAL, "0.3 should be PARTIAL"

    # Test 0.8 boundary (should be COMPLETE)
    evidence2 = EvidenceProvided(
        turn_number=3,
        form=EvidenceForm.USER_INPUT,
        content="Evidence 2",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.COMPLETE,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification2 = EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.COMPLETE,
        completeness_score=0.8,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )

    update_evidence_lifecycle(evidence2, classification2, state, current_turn=3)
    assert request.status == EvidenceStatus.COMPLETE, "0.8 should be COMPLETE"


# =============================================================================
# Additional Test: test_below_threshold_remains_pending
# =============================================================================


def test_below_threshold_remains_pending(diagnostic_state_with_requests):
    """Test evidence with score < 0.3 keeps request in PENDING status."""
    # Arrange
    state = diagnostic_state_with_requests
    request = state.evidence_requests[0]
    assert request.status == EvidenceStatus.PENDING

    evidence = EvidenceProvided(
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content="Minimal evidence",
        addresses_requests=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification = EvidenceClassification(
        matched_request_ids=["req-001"],
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.2,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )

    # Act
    update_evidence_lifecycle(evidence, classification, state, current_turn=2)

    # Assert
    assert request.status == EvidenceStatus.PENDING, \
        "Score < 0.3 should keep status as PENDING"
    assert request.completeness == 0.2


# =============================================================================
# Additional Test: test_multiple_requests_in_classification
# =============================================================================


def test_multiple_requests_in_classification(diagnostic_state_with_requests):
    """Test evidence that matches multiple requests updates all of them."""
    # Arrange
    state = diagnostic_state_with_requests
    request1 = state.evidence_requests[0]
    request2 = state.evidence_requests[1]

    evidence = EvidenceProvided(
        turn_number=2,
        form=EvidenceForm.USER_INPUT,
        content="Evidence for multiple requests",
        addresses_requests=["req-001", "req-002"],
        completeness=CompletenessLevel.PARTIAL,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )
    classification = EvidenceClassification(
        matched_request_ids=["req-001", "req-002"],
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.6,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceType.SUPPORTIVE,
        user_intent=UserIntent.PROVIDING_EVIDENCE
    )

    # Act
    update_evidence_lifecycle(evidence, classification, state, current_turn=2)

    # Assert
    assert request1.completeness == 0.6
    assert request2.completeness == 0.6
    assert request1.status == EvidenceStatus.PARTIAL
    assert request2.status == EvidenceStatus.PARTIAL
    assert request1.updated_at_turn == 2
    assert request2.updated_at_turn == 2

"""Tests for evidence consumption utilities

Verifies evidence consumption logic used by phase handlers to incorporate
user-provided findings into investigation state.
"""

import pytest
from datetime import datetime, timedelta
from uuid import uuid4

from faultmaven.models.evidence import (
    EvidenceProvided,
    EvidenceRequest,
    EvidenceStatus,
    EvidenceForm,
    EvidenceType,
    UserIntent,
    CompletenessLevel,
    EvidenceCategory,
    AcquisitionGuidance,
)
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn_from_diagnostic,
    get_evidence_for_requests,
    check_requests_complete,
    summarize_evidence_findings,
    calculate_evidence_coverage,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_evidence_provided():
    """Sample evidence provided at different turns"""
    return [
        EvidenceProvided(
            evidence_id="ev-001",
            turn_number=1,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.USER_INPUT,
            content="Error logs show 500 errors starting at 10:00 AM",
            addresses_requests=["req-001"],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
            key_findings=["500 errors", "Started at 10:00 AM"],
        ),
        EvidenceProvided(
            evidence_id="ev-002",
            turn_number=2,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.USER_INPUT,
            content="Deployment happened at 9:55 AM",
            addresses_requests=["req-002"],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
            key_findings=["Deployment at 9:55 AM"],
        ),
        EvidenceProvided(
            evidence_id="ev-003",
            turn_number=3,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.DOCUMENT,
            content="log_file_path.txt",
            addresses_requests=["req-001", "req-003"],
            completeness=CompletenessLevel.OVER_COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
            key_findings=["Multiple error patterns found"],
        ),
        EvidenceProvided(
            evidence_id="ev-004",
            turn_number=4,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.USER_INPUT,
            content="Config rollback didn't fix the issue",
            addresses_requests=["req-004"],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.REFUTING,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
            key_findings=["Rollback ineffective"],
        ),
    ]


@pytest.fixture
def sample_evidence_requests():
    """Sample evidence requests with different statuses"""
    return [
        EvidenceRequest(
            request_id="req-001",
            label="Error logs",
            description="Recent error logs from API service",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(
                commands=["kubectl logs -l app=api --tail=100"],
            ),
            status=EvidenceStatus.COMPLETE,
            completeness=1.0,
            created_at_turn=1,
        ),
        EvidenceRequest(
            request_id="req-002",
            label="Deployment timeline",
            description="When was the last deployment",
            category=EvidenceCategory.TIMELINE,
            guidance=AcquisitionGuidance(
                commands=["git log --oneline -10"],
            ),
            status=EvidenceStatus.COMPLETE,
            completeness=1.0,
            created_at_turn=1,
        ),
        EvidenceRequest(
            request_id="req-003",
            label="Detailed stack traces",
            description="Full stack traces for errors",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(
                file_locations=["/var/log/app.log"],
            ),
            status=EvidenceStatus.PARTIAL,
            completeness=0.5,
            created_at_turn=2,
        ),
        EvidenceRequest(
            request_id="req-004",
            label="Config verification",
            description="Current configuration state",
            category=EvidenceCategory.CONFIGURATION,
            guidance=AcquisitionGuidance(
                commands=["cat /etc/app/config.yaml"],
            ),
            status=EvidenceStatus.PENDING,
            completeness=0.0,
            created_at_turn=3,
        ),
        EvidenceRequest(
            request_id="req-005",
            label="Blocked request",
            description="Evidence user cannot provide",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            completeness=0.0,
            created_at_turn=3,
        ),
    ]


# =============================================================================
# Test get_new_evidence_since_turn_from_diagnostic
# =============================================================================


def test_get_new_evidence_since_turn_returns_new_only(sample_evidence_provided):
    """Test filtering evidence by turn number"""
    # Get evidence since turn 2
    new_evidence = get_new_evidence_since_turn_from_diagnostic(
        sample_evidence_provided,
        since_turn=2
    )

    assert len(new_evidence) == 2
    assert all(e.turn_number > 2 for e in new_evidence)
    assert new_evidence[0].evidence_id == "ev-003"
    assert new_evidence[1].evidence_id == "ev-004"


def test_get_new_evidence_since_turn_empty_when_none_new(sample_evidence_provided):
    """Test returns empty list when no new evidence"""
    new_evidence = get_new_evidence_since_turn_from_diagnostic(
        sample_evidence_provided,
        since_turn=10
    )

    assert len(new_evidence) == 0


def test_get_new_evidence_since_turn_with_turn_zero(sample_evidence_provided):
    """Test getting all evidence when since_turn=0"""
    new_evidence = get_new_evidence_since_turn_from_diagnostic(
        sample_evidence_provided,
        since_turn=0
    )

    assert len(new_evidence) == 4  # All evidence


def test_get_new_evidence_since_turn_with_empty_list():
    """Test handling empty evidence list"""
    new_evidence = get_new_evidence_since_turn_from_diagnostic(
        [],
        since_turn=1
    )

    assert len(new_evidence) == 0


# =============================================================================
# Test get_evidence_for_requests
# =============================================================================


def test_get_evidence_for_requests_single_match(sample_evidence_provided):
    """Test getting evidence for single request ID"""
    evidence = get_evidence_for_requests(
        sample_evidence_provided,
        request_ids=["req-001"]
    )

    assert len(evidence) == 2  # ev-001 and ev-003 both address req-001
    assert evidence[0].evidence_id == "ev-001"
    assert evidence[1].evidence_id == "ev-003"


def test_get_evidence_for_requests_multiple_matches(sample_evidence_provided):
    """Test getting evidence for multiple request IDs"""
    evidence = get_evidence_for_requests(
        sample_evidence_provided,
        request_ids=["req-001", "req-002"]
    )

    assert len(evidence) == 3  # ev-001, ev-002, ev-003
    assert set(e.evidence_id for e in evidence) == {"ev-001", "ev-002", "ev-003"}


def test_get_evidence_for_requests_no_matches(sample_evidence_provided):
    """Test returns empty when no matches"""
    evidence = get_evidence_for_requests(
        sample_evidence_provided,
        request_ids=["req-999"]
    )

    assert len(evidence) == 0


def test_get_evidence_for_requests_empty_request_ids(sample_evidence_provided):
    """Test handling empty request IDs list"""
    evidence = get_evidence_for_requests(
        sample_evidence_provided,
        request_ids=[]
    )

    assert len(evidence) == 0


def test_get_evidence_for_requests_empty_evidence_list():
    """Test handling empty evidence list"""
    evidence = get_evidence_for_requests(
        [],
        request_ids=["req-001"]
    )

    assert len(evidence) == 0


# =============================================================================
# Test check_requests_complete
# =============================================================================


def test_check_requests_complete_all_complete(sample_evidence_requests):
    """Test when all specified requests are complete"""
    is_complete = check_requests_complete(
        sample_evidence_requests,
        request_ids=["req-001", "req-002"]
    )

    assert is_complete is True


def test_check_requests_complete_some_incomplete(sample_evidence_requests):
    """Test when some requests are incomplete"""
    is_complete = check_requests_complete(
        sample_evidence_requests,
        request_ids=["req-001", "req-003"]  # req-003 is PARTIAL (0.5)
    )

    assert is_complete is False


def test_check_requests_complete_custom_threshold(sample_evidence_requests):
    """Test with custom completeness threshold"""
    is_complete = check_requests_complete(
        sample_evidence_requests,
        request_ids=["req-003"],  # req-003 has completeness 0.5
        completeness_threshold=0.5
    )

    assert is_complete is True  # 0.5 meets threshold of 0.5


def test_check_requests_complete_threshold_not_met(sample_evidence_requests):
    """Test when completeness below threshold"""
    is_complete = check_requests_complete(
        sample_evidence_requests,
        request_ids=["req-003"],  # req-003 has completeness 0.5
        completeness_threshold=0.8
    )

    assert is_complete is False  # 0.5 < 0.8


def test_check_requests_complete_empty_request_ids(sample_evidence_requests):
    """Test with empty request IDs returns True"""
    is_complete = check_requests_complete(
        sample_evidence_requests,
        request_ids=[]
    )

    assert is_complete is True


def test_check_requests_complete_nonexistent_request(sample_evidence_requests):
    """Test handling nonexistent request ID"""
    # Should return False because request doesn't exist
    is_complete = check_requests_complete(
        sample_evidence_requests,
        request_ids=["req-999"]
    )

    assert is_complete is False


# =============================================================================
# Test summarize_evidence_findings
# =============================================================================


def test_summarize_evidence_findings_formats_correctly(sample_evidence_provided):
    """Test evidence summary formatting"""
    summary = summarize_evidence_findings(sample_evidence_provided[:2])

    assert "User input" in summary
    assert "500 errors" in summary
    assert "Deployment happened at 9:55 AM" in summary
    assert "Type: Supportive" in summary
    assert "\n" in summary  # Multiple lines


def test_summarize_evidence_findings_truncates_long_content():
    """Test content truncation in summary"""
    long_content = "A" * 300
    evidence = [
        EvidenceProvided(
            evidence_id="ev-long",
            turn_number=1,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.USER_INPUT,
            content=long_content,
            addresses_requests=[],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.NEUTRAL,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
        )
    ]

    summary = summarize_evidence_findings(evidence)

    assert "..." in summary  # Truncation marker
    assert len(summary) < len(long_content)  # Should be shorter


def test_summarize_evidence_findings_includes_key_findings():
    """Test key findings are included in summary"""
    evidence = [
        EvidenceProvided(
            evidence_id="ev-findings",
            turn_number=1,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.DOCUMENT,
            content="document.log",
            addresses_requests=[],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
            key_findings=["Finding 1", "Finding 2", "Finding 3", "Finding 4"],
        )
    ]

    summary = summarize_evidence_findings(evidence)

    assert "Findings:" in summary
    # Should only include first 3 findings
    assert "Finding 1" in summary
    assert "Finding 2" in summary
    assert "Finding 3" in summary
    # Finding 4 might or might not be included (we limit to 3)


def test_summarize_evidence_findings_empty_list():
    """Test with empty evidence list"""
    summary = summarize_evidence_findings([])

    assert summary == ""


def test_summarize_evidence_findings_document_upload():
    """Test formatting for document uploads"""
    evidence = [
        EvidenceProvided(
            evidence_id="ev-doc",
            turn_number=1,
            timestamp=datetime.utcnow(),
            form=EvidenceForm.DOCUMENT,
            content="uploaded_file.log",
            addresses_requests=[],
            completeness=CompletenessLevel.COMPLETE,
            evidence_type=EvidenceType.SUPPORTIVE,
            user_intent=UserIntent.PROVIDING_EVIDENCE,
        )
    ]

    summary = summarize_evidence_findings(evidence)

    assert "Document upload" in summary
    assert "uploaded_file.log" in summary


# =============================================================================
# Test calculate_evidence_coverage
# =============================================================================


def test_calculate_evidence_coverage_all_complete(sample_evidence_requests):
    """Test coverage when all requests complete"""
    # Filter to only complete requests
    complete_requests = [r for r in sample_evidence_requests if r.status == EvidenceStatus.COMPLETE]

    coverage = calculate_evidence_coverage(complete_requests, [])

    assert coverage == 1.0


def test_calculate_evidence_coverage_mixed_status(sample_evidence_requests):
    """Test coverage with mixed request statuses"""
    # req-001: COMPLETE (1.0)
    # req-002: COMPLETE (1.0)
    # req-003: PARTIAL (0.5) -> weight 0.5
    # req-004: PENDING (0.0) -> weight 0.0
    # req-005: BLOCKED -> excluded

    active_requests = [r for r in sample_evidence_requests if r.status != EvidenceStatus.BLOCKED]

    coverage = calculate_evidence_coverage(active_requests, [])

    # Expected: (1.0 + 1.0 + 0.5 + 0.0) / 4 = 0.625
    assert coverage == 0.625


def test_calculate_evidence_coverage_no_requests():
    """Test coverage with no requests returns 1.0"""
    coverage = calculate_evidence_coverage([], [])

    assert coverage == 1.0


def test_calculate_evidence_coverage_all_blocked():
    """Test coverage when all requests blocked"""
    blocked_requests = [
        EvidenceRequest(
            request_id="req-blocked",
            label="Blocked",
            description="Blocked request",
            category=EvidenceCategory.METRICS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.BLOCKED,
            created_at_turn=1,
        )
    ]

    coverage = calculate_evidence_coverage(blocked_requests, [])

    assert coverage == 1.0  # Blocked requests excluded


def test_calculate_evidence_coverage_partial_requests():
    """Test coverage calculation with partial requests"""
    partial_requests = [
        EvidenceRequest(
            request_id="req-p1",
            label="Partial 1",
            description="Partially complete",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.PARTIAL,
            completeness=0.5,
            created_at_turn=1,
        ),
        EvidenceRequest(
            request_id="req-p2",
            label="Partial 2",
            description="Partially complete",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(),
            status=EvidenceStatus.PARTIAL,
            completeness=0.6,
            created_at_turn=1,
        ),
    ]

    coverage = calculate_evidence_coverage(partial_requests, [])

    # Expected: (0.5 + 0.5) / 2 = 0.5 (both get weight 0.5 for being partial)
    assert coverage == 0.5

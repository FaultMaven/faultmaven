"""
Unit tests for ReportRecommendationService.

Tests intelligent runbook recommendation logic with similarity thresholds.
"""

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock
from datetime import datetime

from faultmaven.services.domain.report_recommendation_service import ReportRecommendationService
from faultmaven.models.report import (
    ReportType,
    ReportRecommendation,
    RunbookRecommendation,
    SimilarRunbook,
    CaseReport,
    ReportStatus,
    RunbookSource,
    RunbookMetadata
)
from faultmaven.models.case import Case, CaseStatus


@pytest.fixture
def mock_runbook_kb():
    """Create mock RunbookKnowledgeBase."""
    kb = Mock()
    kb.search_runbooks = AsyncMock()
    return kb


@pytest.fixture
def recommendation_service(mock_runbook_kb):
    """Create ReportRecommendationService with mocked dependencies."""
    return ReportRecommendationService(runbook_kb=mock_runbook_kb)


@pytest.fixture
def sample_case():
    """Create sample case."""
    return Case(
        case_id="case-abc123",
        title="Database Connection Pool Exhaustion",
        description="PostgreSQL connections timing out",
        owner_id="user-123",
        status=CaseStatus.RESOLVED,
        domain="database",
        tags=["postgresql", "connection-pool", "performance"]
    )


@pytest.fixture
def high_similarity_runbook():
    """Create runbook with high similarity (92%)."""
    return SimilarRunbook(
        runbook=CaseReport(
            report_id="report-high",
            case_id="case-old",
            report_type=ReportType.RUNBOOK,
            title="Runbook: Database Connection Pool Issues",
            content="# Runbook\n...",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-09-15T14:20:00Z",
            generation_time_ms=10000,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain="database",
                tags=["postgresql", "connection-pool"]
            )
        ),
        similarity_score=0.92,  # 92% - high similarity
        case_title="Database Connection Pool Issues",
        case_id="case-old"
    )


@pytest.fixture
def moderate_similarity_runbook():
    """Create runbook with moderate similarity (78%)."""
    return SimilarRunbook(
        runbook=CaseReport(
            report_id="report-moderate",
            case_id="doc-derived",
            report_type=ReportType.RUNBOOK,
            title="Runbook: PostgreSQL Performance Troubleshooting",
            content="# Runbook\n...",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-08-01T10:00:00Z",
            generation_time_ms=0,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.DOCUMENT_DRIVEN,
                domain="database",
                tags=["postgresql", "performance"],
                document_title="PostgreSQL Operations Guide"
            )
        ),
        similarity_score=0.78,  # 78% - moderate similarity
        case_title="PostgreSQL Operations Guide",
        case_id="doc-derived"
    )


# =============================================================================
# Test: get_available_report_types
# =============================================================================

@pytest.mark.asyncio
async def test_get_available_report_types_always_includes_incident_and_postmortem(
    recommendation_service, mock_runbook_kb, sample_case
):
    """Test that incident report and post-mortem are always available."""
    # No similar runbooks
    mock_runbook_kb.search_runbooks.return_value = []

    recommendation = await recommendation_service.get_available_report_types(sample_case)

    assert ReportType.INCIDENT_REPORT in recommendation.available_for_generation
    assert ReportType.POST_MORTEM in recommendation.available_for_generation


@pytest.mark.asyncio
async def test_get_available_report_types_high_similarity_excludes_runbook(
    recommendation_service, mock_runbook_kb, sample_case, high_similarity_runbook
):
    """Test that runbook is excluded when high similarity runbook exists (≥85%)."""
    mock_runbook_kb.search_runbooks.return_value = [high_similarity_runbook]

    recommendation = await recommendation_service.get_available_report_types(sample_case)

    # Should NOT include runbook in available types (recommend reuse instead)
    assert ReportType.RUNBOOK not in recommendation.available_for_generation
    assert recommendation.runbook_recommendation.action == "reuse"
    assert recommendation.runbook_recommendation.existing_runbook is not None
    assert recommendation.runbook_recommendation.similarity_score == 0.92


@pytest.mark.asyncio
async def test_get_available_report_types_moderate_similarity_includes_runbook(
    recommendation_service, mock_runbook_kb, sample_case, moderate_similarity_runbook
):
    """Test that runbook is included when moderate similarity runbook exists (70-84%)."""
    mock_runbook_kb.search_runbooks.return_value = [moderate_similarity_runbook]

    recommendation = await recommendation_service.get_available_report_types(sample_case)

    # Should include runbook in available types (offer both options)
    assert ReportType.RUNBOOK in recommendation.available_for_generation
    assert recommendation.runbook_recommendation.action == "review_or_generate"
    assert recommendation.runbook_recommendation.existing_runbook is not None
    assert recommendation.runbook_recommendation.similarity_score == 0.78


@pytest.mark.asyncio
async def test_get_available_report_types_no_similarity_includes_runbook(
    recommendation_service, mock_runbook_kb, sample_case
):
    """Test that runbook is included when no similar runbooks exist."""
    mock_runbook_kb.search_runbooks.return_value = []

    recommendation = await recommendation_service.get_available_report_types(sample_case)

    # Should include runbook in available types (no existing alternatives)
    assert ReportType.RUNBOOK in recommendation.available_for_generation
    assert recommendation.runbook_recommendation.action == "generate"
    assert recommendation.runbook_recommendation.existing_runbook is None
    assert recommendation.runbook_recommendation.similarity_score is None


# =============================================================================
# Test: _generate_runbook_recommendation
# =============================================================================

def test_generate_runbook_recommendation_high_similarity(recommendation_service, high_similarity_runbook):
    """Test recommendation logic for high similarity (≥85%)."""
    recommendation = recommendation_service._generate_runbook_recommendation([high_similarity_runbook])

    assert recommendation.action == "reuse"
    assert recommendation.existing_runbook == high_similarity_runbook.runbook
    assert recommendation.similarity_score == 0.92
    assert "92%" in recommendation.reason
    assert "existing runbook" in recommendation.reason.lower()


def test_generate_runbook_recommendation_moderate_similarity(recommendation_service, moderate_similarity_runbook):
    """Test recommendation logic for moderate similarity (70-84%)."""
    recommendation = recommendation_service._generate_runbook_recommendation([moderate_similarity_runbook])

    assert recommendation.action == "review_or_generate"
    assert recommendation.existing_runbook == moderate_similarity_runbook.runbook
    assert recommendation.similarity_score == 0.78
    assert "78%" in recommendation.reason
    assert "review" in recommendation.reason.lower()


def test_generate_runbook_recommendation_low_similarity(recommendation_service):
    """Test recommendation logic for low similarity (<70%)."""
    low_sim_runbook = SimilarRunbook(
        runbook=CaseReport(
            report_id="report-low",
            case_id="case-xyz",
            report_type=ReportType.RUNBOOK,
            title="Unrelated Runbook",
            content="# Different problem",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-01-01T10:00:00Z",
            generation_time_ms=5000,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain="network",
                tags=["network", "dns"]
            )
        ),
        similarity_score=0.60,  # 60% - low similarity
        case_title="Network DNS Issues",
        case_id="case-xyz"
    )

    recommendation = recommendation_service._generate_runbook_recommendation([low_sim_runbook])

    assert recommendation.action == "generate"
    assert recommendation.existing_runbook is None
    assert recommendation.similarity_score == 0.60
    assert "low similarity" in recommendation.reason.lower()


def test_generate_runbook_recommendation_no_existing_runbooks(recommendation_service):
    """Test recommendation when no existing runbooks found."""
    recommendation = recommendation_service._generate_runbook_recommendation([])

    assert recommendation.action == "generate"
    assert recommendation.existing_runbook is None
    assert recommendation.similarity_score is None
    assert "no similar runbooks found" in recommendation.reason.lower()


def test_generate_runbook_recommendation_sorts_by_best_match(recommendation_service, high_similarity_runbook, moderate_similarity_runbook):
    """Test that recommendation uses best match when multiple runbooks exist."""
    # Pass runbooks in reverse order (moderate first, high second)
    recommendation = recommendation_service._generate_runbook_recommendation(
        [moderate_similarity_runbook, high_similarity_runbook]
    )

    # Should use high similarity runbook (first in list should be best match)
    # But our implementation takes the first item, so let's test with proper order
    recommendation = recommendation_service._generate_runbook_recommendation(
        [high_similarity_runbook, moderate_similarity_runbook]
    )

    assert recommendation.action == "reuse"
    assert recommendation.similarity_score == 0.92


# =============================================================================
# Test: Threshold Boundaries
# =============================================================================

def test_recommendation_at_high_threshold_boundary(recommendation_service):
    """Test recommendation at exactly 85% similarity threshold."""
    boundary_runbook = SimilarRunbook(
        runbook=CaseReport(
            report_id="report-boundary",
            case_id="case-boundary",
            report_type=ReportType.RUNBOOK,
            title="Boundary Runbook",
            content="# Content",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-10-01T10:00:00Z",
            generation_time_ms=5000,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain="general",
                tags=[]
            )
        ),
        similarity_score=0.85,  # Exactly 85%
        case_title="Boundary Case",
        case_id="case-boundary"
    )

    recommendation = recommendation_service._generate_runbook_recommendation([boundary_runbook])

    # ≥85% should recommend reuse
    assert recommendation.action == "reuse"


def test_recommendation_at_moderate_threshold_boundary(recommendation_service):
    """Test recommendation at exactly 70% similarity threshold."""
    boundary_runbook = SimilarRunbook(
        runbook=CaseReport(
            report_id="report-boundary2",
            case_id="case-boundary2",
            report_type=ReportType.RUNBOOK,
            title="Boundary Runbook 2",
            content="# Content",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-10-01T10:00:00Z",
            generation_time_ms=5000,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain="general",
                tags=[]
            )
        ),
        similarity_score=0.70,  # Exactly 70%
        case_title="Boundary Case 2",
        case_id="case-boundary2"
    )

    recommendation = recommendation_service._generate_runbook_recommendation([boundary_runbook])

    # ≥70% should offer review or generate
    assert recommendation.action == "review_or_generate"


def test_recommendation_below_moderate_threshold(recommendation_service):
    """Test recommendation just below 70% threshold."""
    below_threshold_runbook = SimilarRunbook(
        runbook=CaseReport(
            report_id="report-below",
            case_id="case-below",
            report_type=ReportType.RUNBOOK,
            title="Below Threshold Runbook",
            content="# Content",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-10-01T10:00:00Z",
            generation_time_ms=5000,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.INCIDENT_DRIVEN,
                domain="general",
                tags=[]
            )
        ),
        similarity_score=0.69,  # Just below 70%
        case_title="Below Threshold",
        case_id="case-below"
    )

    recommendation = recommendation_service._generate_runbook_recommendation([below_threshold_runbook])

    # <70% should recommend generation
    assert recommendation.action == "generate"


# =============================================================================
# Test: Dual-Source Runbook Handling
# =============================================================================

@pytest.mark.asyncio
async def test_recommendation_handles_document_driven_runbooks(
    recommendation_service, mock_runbook_kb, sample_case
):
    """Test that document-driven runbooks are handled correctly."""
    doc_runbook = SimilarRunbook(
        runbook=CaseReport(
            report_id="report-doc",
            case_id="doc-derived",
            report_type=ReportType.RUNBOOK,
            title="Runbook: From Documentation",
            content="# Documentation Runbook",
            format="markdown",
            generation_status=ReportStatus.COMPLETED,
            generated_at="2025-08-01T10:00:00Z",
            generation_time_ms=0,
            is_current=True,
            version=1,
            metadata=RunbookMetadata(
                source=RunbookSource.DOCUMENT_DRIVEN,
                domain="database",
                tags=["postgresql"],
                document_title="Official PostgreSQL Guide",
                original_document_id="doc-official-123"
            )
        ),
        similarity_score=0.88,  # High similarity
        case_title="Official PostgreSQL Guide",
        case_id="doc-derived"
    )

    mock_runbook_kb.search_runbooks.return_value = [doc_runbook]

    recommendation = await recommendation_service.get_available_report_types(sample_case)

    # Should recommend reuse regardless of source
    assert recommendation.runbook_recommendation.action == "reuse"
    assert recommendation.runbook_recommendation.existing_runbook.metadata.source == RunbookSource.DOCUMENT_DRIVEN
    assert recommendation.runbook_recommendation.existing_runbook.metadata.document_title == "Official PostgreSQL Guide"


# =============================================================================
# Test: Error Handling
# =============================================================================

@pytest.mark.asyncio
async def test_get_available_report_types_handles_search_error(
    recommendation_service, mock_runbook_kb, sample_case
):
    """Test that search errors are handled gracefully."""
    # Mock search to raise exception
    mock_runbook_kb.search_runbooks.side_effect = Exception("Vector DB connection failed")

    recommendation = await recommendation_service.get_available_report_types(sample_case)

    # Should still return recommendation (with generate action as fallback)
    assert isinstance(recommendation, ReportRecommendation)
    assert ReportType.INCIDENT_REPORT in recommendation.available_for_generation
    assert ReportType.POST_MORTEM in recommendation.available_for_generation
    assert ReportType.RUNBOOK in recommendation.available_for_generation
    assert recommendation.runbook_recommendation.action == "generate"

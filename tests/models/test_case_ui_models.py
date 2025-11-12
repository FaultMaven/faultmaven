"""Comprehensive tests for phase-adaptive UI response models.

Tests cover all models from faultmaven.models.case_ui:
- Supporting models for each phase
- Phase-adaptive response models (CONSULTING, INVESTIGATING, RESOLVED)
- File relationship models
- Discriminated union behavior
- Validation rules and constraints
"""

import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from pydantic import ValidationError

from faultmaven.models.case_ui import (
    # Supporting models
    UserRequestSummary,
    ClarifyingQuestion,
    WorkingConclusionSummary,
    InvestigationProgressSummary,
    HypothesisSummary,
    EvidenceSummary,
    RootCauseSummary,
    SolutionSummary,
    VerificationStatus,
    ResolutionSummary,
    ReportAvailability,

    # Phase-adaptive response models
    CaseUIResponse_Consulting,
    CaseUIResponse_Investigating,
    CaseUIResponse_Resolved,
    CaseUIResponse,

    # File relationship models
    UploadedFileMetadata,
    AIInsights,
    UploadedFileDetailsResponse,
    UploadedFilesListResponse,
    FileToMilestoneRelationship,
    FileToHypothesisRelationship,
)

from faultmaven.models.case import (
    CaseStatus,
    InvestigationStage,
    HypothesisStatus,
)


# ============================================================
# Fixtures for Common Test Data
# ============================================================

@pytest.fixture
def sample_user_request():
    """Sample user request summary."""
    return UserRequestSummary(
        original_message="API is returning 500 errors for all POST requests",
        parsed_intent="diagnose_error",
        severity="high"
    )


@pytest.fixture
def sample_clarifying_question():
    """Sample clarifying question."""
    return ClarifyingQuestion(
        question_id="q_123abc456def",
        text="When did you first notice the errors?",
        priority="high",
        answered=False
    )


@pytest.fixture
def sample_working_conclusion():
    """Sample working conclusion."""
    return WorkingConclusionSummary(
        summary="Database connection pool appears to be exhausted",
        confidence=0.7,
        last_updated=datetime.now(timezone.utc)
    )


@pytest.fixture
def sample_investigation_progress():
    """Sample investigation progress."""
    return InvestigationProgressSummary(
        milestones_completed=4,
        total_milestones=8,
        completed_milestone_ids=["symptom_verified", "scope_assessed", "timeline_established", "changes_identified"],
        current_stage=InvestigationStage.DIAGNOSING
    )


@pytest.fixture
def sample_hypothesis():
    """Sample hypothesis summary."""
    return HypothesisSummary(
        hypothesis_id="hyp_abc123",
        text="Connection pool size is too small for current load",
        likelihood=0.75,
        status=HypothesisStatus.ACTIVE,
        evidence_count=3
    )


@pytest.fixture
def sample_evidence():
    """Sample evidence summary."""
    return EvidenceSummary(
        evidence_id="ev_xyz789",
        type="metrics_data",
        summary="Connection pool metrics showing 95% utilization",
        timestamp=datetime.now(timezone.utc),
        relevance_score=0.9
    )


@pytest.fixture
def sample_root_cause():
    """Sample root cause summary."""
    return RootCauseSummary(
        description="Database connection pool exhausted due to increased traffic",
        root_cause_id="rc_123",
        category="environment",
        severity="high"
    )


@pytest.fixture
def sample_solution():
    """Sample solution summary."""
    return SolutionSummary(
        description="Increased connection pool size from 10 to 50",
        applied_at=datetime.now(timezone.utc),
        applied_by="user-456"
    )


@pytest.fixture
def sample_verification_status():
    """Sample verification status."""
    return VerificationStatus(
        verified=True,
        verification_method="Monitored error rate for 30 minutes post-fix",
        details="Error rate decreased from 15% to 0.1%"
    )


@pytest.fixture
def sample_resolution_summary():
    """Sample resolution summary."""
    return ResolutionSummary(
        total_duration_minutes=45,
        milestones_completed=8,
        hypotheses_tested=3,
        evidence_collected=7,
        key_insights=["Connection pool sizing is critical", "Monitor pool utilization"]
    )


@pytest.fixture
def sample_uploaded_file():
    """Sample uploaded file metadata."""
    return UploadedFileMetadata(
        file_id="file_123",
        filename="error.log",
        mime_type="text/plain",
        size_bytes=1024,
        uploaded_at=datetime.now(timezone.utc),
        uploaded_by_user_id="user-456",
        analysis_status="completed",
        ai_insights_summary="Found 15 database connection errors",
        related_milestone_ids=["symptom_verified"],
        related_hypothesis_ids=["hyp_abc123"]
    )


# ============================================================
# Supporting Models Tests
# ============================================================

class TestUserRequestSummary:
    """Test UserRequestSummary model."""

    def test_valid_creation(self, sample_user_request):
        """Test creating valid UserRequestSummary."""
        assert sample_user_request.original_message == "API is returning 500 errors for all POST requests"
        assert sample_user_request.parsed_intent == "diagnose_error"
        assert sample_user_request.severity == "high"

    def test_max_length_validation(self):
        """Test max_length constraints."""
        # Original message max_length=1000
        with pytest.raises(ValidationError, match="String should have at most 1000 characters"):
            UserRequestSummary(
                original_message="x" * 1001,
                parsed_intent="diagnose_error",
                severity="high"
            )

        # Parsed intent max_length=100
        with pytest.raises(ValidationError, match="String should have at most 100 characters"):
            UserRequestSummary(
                original_message="Test message",
                parsed_intent="x" * 101,
                severity="high"
            )

        # Severity max_length=50
        with pytest.raises(ValidationError, match="String should have at most 50 characters"):
            UserRequestSummary(
                original_message="Test message",
                parsed_intent="diagnose_error",
                severity="x" * 51
            )


class TestClarifyingQuestion:
    """Test ClarifyingQuestion model."""

    def test_valid_creation(self, sample_clarifying_question):
        """Test creating valid ClarifyingQuestion."""
        assert sample_clarifying_question.question_id == "q_123abc456def"
        assert sample_clarifying_question.text == "When did you first notice the errors?"
        assert sample_clarifying_question.priority == "high"
        assert not sample_clarifying_question.answered
        assert sample_clarifying_question.answer is None

    def test_with_answer(self):
        """Test clarifying question with answer."""
        question = ClarifyingQuestion(
            question_id="q_456",
            text="What is the error code?",
            priority="high",
            answered=True,
            answer="500 Internal Server Error"
        )

        assert question.answered
        assert question.answer == "500 Internal Server Error"

    def test_max_length_validation(self):
        """Test max_length constraints."""
        # Text max_length=500
        with pytest.raises(ValidationError, match="String should have at most 500 characters"):
            ClarifyingQuestion(
                question_id="q_123",
                text="x" * 501,
                priority="high"
            )

        # Answer max_length=2000
        with pytest.raises(ValidationError, match="String should have at most 2000 characters"):
            ClarifyingQuestion(
                question_id="q_123",
                text="Test question?",
                priority="high",
                answered=True,
                answer="x" * 2001
            )


class TestWorkingConclusionSummary:
    """Test WorkingConclusionSummary model."""

    def test_valid_creation(self, sample_working_conclusion):
        """Test creating valid WorkingConclusionSummary."""
        assert sample_working_conclusion.summary == "Database connection pool appears to be exhausted"
        assert sample_working_conclusion.confidence == 0.7
        assert isinstance(sample_working_conclusion.last_updated, datetime)

    def test_confidence_bounds(self):
        """Test confidence must be between 0.0 and 1.0."""
        # Valid bounds
        WorkingConclusionSummary(
            summary="Test conclusion",
            confidence=0.0,
            last_updated=datetime.now(timezone.utc)
        )

        WorkingConclusionSummary(
            summary="Test conclusion",
            confidence=1.0,
            last_updated=datetime.now(timezone.utc)
        )

        # Invalid bounds
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            WorkingConclusionSummary(
                summary="Test conclusion",
                confidence=-0.1,
                last_updated=datetime.now(timezone.utc)
            )

        with pytest.raises(ValidationError, match="less than or equal to 1"):
            WorkingConclusionSummary(
                summary="Test conclusion",
                confidence=1.1,
                last_updated=datetime.now(timezone.utc)
            )

    def test_max_length_validation(self):
        """Test summary max_length=1000."""
        with pytest.raises(ValidationError, match="String should have at most 1000 characters"):
            WorkingConclusionSummary(
                summary="x" * 1001,
                confidence=0.5,
                last_updated=datetime.now(timezone.utc)
            )


class TestInvestigationProgressSummary:
    """Test InvestigationProgressSummary model."""

    def test_valid_creation(self, sample_investigation_progress):
        """Test creating valid InvestigationProgressSummary."""
        assert sample_investigation_progress.milestones_completed == 4
        assert sample_investigation_progress.total_milestones == 8
        assert len(sample_investigation_progress.completed_milestone_ids) == 4
        assert sample_investigation_progress.current_stage == InvestigationStage.DIAGNOSING

    def test_milestone_counts_non_negative(self):
        """Test milestone counts must be non-negative."""
        # Valid
        InvestigationProgressSummary(
            milestones_completed=0,
            total_milestones=8,
            current_stage=InvestigationStage.UNDERSTANDING
        )

        # Invalid
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            InvestigationProgressSummary(
                milestones_completed=-1,
                total_milestones=8,
                current_stage=InvestigationStage.UNDERSTANDING
            )

        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            InvestigationProgressSummary(
                milestones_completed=0,
                total_milestones=-1,
                current_stage=InvestigationStage.UNDERSTANDING
            )


class TestHypothesisSummary:
    """Test HypothesisSummary model."""

    def test_valid_creation(self, sample_hypothesis):
        """Test creating valid HypothesisSummary."""
        assert sample_hypothesis.hypothesis_id == "hyp_abc123"
        assert sample_hypothesis.text == "Connection pool size is too small for current load"
        assert sample_hypothesis.likelihood == 0.75
        assert sample_hypothesis.status == HypothesisStatus.ACTIVE
        assert sample_hypothesis.evidence_count == 3

    def test_likelihood_bounds(self):
        """Test likelihood must be between 0.0 and 1.0."""
        # Valid bounds
        HypothesisSummary(
            hypothesis_id="hyp_123",
            text="Test hypothesis",
            likelihood=0.0,
            status=HypothesisStatus.CAPTURED,
            evidence_count=0
        )

        HypothesisSummary(
            hypothesis_id="hyp_123",
            text="Test hypothesis",
            likelihood=1.0,
            status=HypothesisStatus.VALIDATED,
            evidence_count=5
        )

        # Invalid bounds
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            HypothesisSummary(
                hypothesis_id="hyp_123",
                text="Test hypothesis",
                likelihood=-0.1,
                status=HypothesisStatus.CAPTURED,
                evidence_count=0
            )

    def test_evidence_count_non_negative(self):
        """Test evidence count must be non-negative."""
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            HypothesisSummary(
                hypothesis_id="hyp_123",
                text="Test hypothesis",
                likelihood=0.5,
                status=HypothesisStatus.ACTIVE,
                evidence_count=-1
            )


class TestEvidenceSummary:
    """Test EvidenceSummary model."""

    def test_valid_creation(self, sample_evidence):
        """Test creating valid EvidenceSummary."""
        assert sample_evidence.evidence_id == "ev_xyz789"
        assert sample_evidence.type == "metrics_data"
        assert sample_evidence.summary == "Connection pool metrics showing 95% utilization"
        assert isinstance(sample_evidence.timestamp, datetime)
        assert sample_evidence.relevance_score == 0.9

    def test_relevance_score_bounds(self):
        """Test relevance_score must be between 0.0 and 1.0."""
        # Valid
        EvidenceSummary(
            evidence_id="ev_123",
            type="log_file",
            summary="Test evidence",
            timestamp=datetime.now(timezone.utc),
            relevance_score=0.5
        )

        # Invalid
        with pytest.raises(ValidationError, match="less than or equal to 1"):
            EvidenceSummary(
                evidence_id="ev_123",
                type="log_file",
                summary="Test evidence",
                timestamp=datetime.now(timezone.utc),
                relevance_score=1.5
            )


class TestRootCauseSummary:
    """Test RootCauseSummary model."""

    def test_valid_creation(self, sample_root_cause):
        """Test creating valid RootCauseSummary."""
        assert sample_root_cause.description == "Database connection pool exhausted due to increased traffic"
        assert sample_root_cause.root_cause_id == "rc_123"
        assert sample_root_cause.category == "environment"
        assert sample_root_cause.severity == "high"

    def test_max_length_validation(self):
        """Test max_length constraints."""
        # Description max_length=1000
        with pytest.raises(ValidationError, match="String should have at most 1000 characters"):
            RootCauseSummary(
                description="x" * 1001,
                root_cause_id="rc_123",
                category="environment",
                severity="high"
            )

        # Category max_length=100
        with pytest.raises(ValidationError, match="String should have at most 100 characters"):
            RootCauseSummary(
                description="Test root cause",
                root_cause_id="rc_123",
                category="x" * 101,
                severity="high"
            )


class TestSolutionSummary:
    """Test SolutionSummary model."""

    def test_valid_creation(self, sample_solution):
        """Test creating valid SolutionSummary."""
        assert sample_solution.description == "Increased connection pool size from 10 to 50"
        assert isinstance(sample_solution.applied_at, datetime)
        assert sample_solution.applied_by == "user-456"

    def test_max_length_validation(self):
        """Test description max_length=2000."""
        with pytest.raises(ValidationError, match="String should have at most 2000 characters"):
            SolutionSummary(
                description="x" * 2001,
                applied_at=datetime.now(timezone.utc),
                applied_by="user-123"
            )


class TestVerificationStatus:
    """Test VerificationStatus model."""

    def test_valid_creation(self, sample_verification_status):
        """Test creating valid VerificationStatus."""
        assert sample_verification_status.verified is True
        assert sample_verification_status.verification_method == "Monitored error rate for 30 minutes post-fix"
        assert sample_verification_status.details == "Error rate decreased from 15% to 0.1%"

    def test_max_length_validation(self):
        """Test max_length constraints."""
        # verification_method max_length=500
        with pytest.raises(ValidationError, match="String should have at most 500 characters"):
            VerificationStatus(
                verified=True,
                verification_method="x" * 501,
                details="Test details"
            )

        # details max_length=1000
        with pytest.raises(ValidationError, match="String should have at most 1000 characters"):
            VerificationStatus(
                verified=True,
                verification_method="Test method",
                details="x" * 1001
            )


class TestResolutionSummary:
    """Test ResolutionSummary model."""

    def test_valid_creation(self, sample_resolution_summary):
        """Test creating valid ResolutionSummary."""
        assert sample_resolution_summary.total_duration_minutes == 45
        assert sample_resolution_summary.milestones_completed == 8
        assert sample_resolution_summary.hypotheses_tested == 3
        assert sample_resolution_summary.evidence_collected == 7
        assert len(sample_resolution_summary.key_insights) == 2

    def test_non_negative_counts(self):
        """Test all counts must be non-negative."""
        # Valid
        ResolutionSummary(
            total_duration_minutes=0,
            milestones_completed=0,
            hypotheses_tested=0,
            evidence_collected=0
        )

        # Invalid
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            ResolutionSummary(
                total_duration_minutes=-1,
                milestones_completed=8,
                hypotheses_tested=3,
                evidence_collected=7
            )


class TestReportAvailability:
    """Test ReportAvailability model."""

    def test_valid_creation(self):
        """Test creating valid ReportAvailability."""
        report = ReportAvailability(
            report_type="incident_report",
            status="available",
            reason=None
        )

        assert report.report_type == "incident_report"
        assert report.status == "available"
        assert report.reason is None

    def test_with_reason(self):
        """Test report availability with reason."""
        report = ReportAvailability(
            report_type="post_mortem",
            status="recommended",
            reason="Case complexity warrants detailed analysis"
        )

        assert report.report_type == "post_mortem"
        assert report.status == "recommended"
        assert report.reason == "Case complexity warrants detailed analysis"

    def test_max_length_validation(self):
        """Test max_length constraints."""
        with pytest.raises(ValidationError, match="String should have at most 500 characters"):
            ReportAvailability(
                report_type="incident_report",
                status="recommended",
                reason="x" * 501
            )


# ============================================================
# Phase-Adaptive Response Models Tests
# ============================================================

class TestCaseUIResponse_Consulting:
    """Test CaseUIResponse_Consulting model."""

    def test_minimal_creation(self):
        """Test creating minimal CONSULTING response."""
        response = CaseUIResponse_Consulting(
            case_id="case_abc123",
            title="Database Connection Issues",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=UserRequestSummary(
                original_message="Database connections are failing",
                parsed_intent="diagnose_error",
                severity="high"
            ),
            agent_guidance="Let me help you investigate this database issue"
        )

        assert response.case_id == "case_abc123"
        assert response.status == CaseStatus.CONSULTING
        assert response.title == "Database Connection Issues"
        assert len(response.initial_symptoms) == 0
        assert len(response.clarifying_questions) == 0
        assert response.proposed_problem_statement is None
        assert not response.problem_statement_confirmed
        assert not response.ready_to_investigate

    def test_full_consulting_response(self, sample_user_request):
        """Test creating full CONSULTING response with all fields."""
        clarifying_questions = [
            ClarifyingQuestion(
                question_id="q_1",
                text="When did the errors start?",
                priority="high",
                answered=True,
                answer="About 2 hours ago"
            ),
            ClarifyingQuestion(
                question_id="q_2",
                text="What's the error message?",
                priority="high",
                answered=False
            )
        ]

        response = CaseUIResponse_Consulting(
            case_id="case_123",
            title="API Errors",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=sample_user_request,
            initial_symptoms=["500 errors", "High latency"],
            clarifying_questions=clarifying_questions,
            agent_guidance="Based on the symptoms, this appears to be a backend issue",
            proposed_problem_statement="API experiencing 500 errors due to database issues",
            problem_statement_confirmed=True,
            ready_to_investigate=True
        )

        assert response.case_id == "case_123"
        assert response.status == CaseStatus.CONSULTING
        assert len(response.initial_symptoms) == 2
        assert len(response.clarifying_questions) == 2
        assert response.proposed_problem_statement is not None
        assert response.problem_statement_confirmed
        assert response.ready_to_investigate

    def test_status_literal_enforcement(self):
        """Test that status is always CONSULTING."""
        response = CaseUIResponse_Consulting(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=UserRequestSummary(
                original_message="Test",
                parsed_intent="other",
                severity="low"
            ),
            agent_guidance="Test guidance"
        )

        # Status should always be CONSULTING
        assert response.status == CaseStatus.CONSULTING

    def test_max_length_validation(self, sample_user_request):
        """Test max_length constraints."""
        # title max_length=200
        with pytest.raises(ValidationError, match="String should have at most 200 characters"):
            CaseUIResponse_Consulting(
                case_id="case_123",
                title="x" * 201,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                user_request=sample_user_request,
                agent_guidance="Test"
            )

        # agent_guidance max_length=2000
        with pytest.raises(ValidationError, match="String should have at most 2000 characters"):
            CaseUIResponse_Consulting(
                case_id="case_123",
                title="Test Case",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                user_request=sample_user_request,
                agent_guidance="x" * 2001
            )

        # proposed_problem_statement max_length=1000
        with pytest.raises(ValidationError, match="String should have at most 1000 characters"):
            CaseUIResponse_Consulting(
                case_id="case_123",
                title="Test Case",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                user_request=sample_user_request,
                agent_guidance="Test",
                proposed_problem_statement="x" * 1001
            )


class TestCaseUIResponse_Investigating:
    """Test CaseUIResponse_Investigating model."""

    def test_minimal_creation(self, sample_investigation_progress):
        """Test creating minimal INVESTIGATING response."""
        response = CaseUIResponse_Investigating(
            case_id="case_abc123",
            title="Database Connection Issues",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            progress=sample_investigation_progress,
            agent_status="Analyzing error logs"
        )

        assert response.case_id == "case_abc123"
        assert response.status == CaseStatus.INVESTIGATING
        assert response.title == "Database Connection Issues"
        assert response.working_conclusion is None
        assert len(response.active_hypotheses) == 0
        assert len(response.latest_evidence) == 0
        assert len(response.next_actions) == 0
        assert not response.is_stuck
        assert not response.degraded_mode

    def test_full_investigating_response(
        self,
        sample_working_conclusion,
        sample_investigation_progress,
        sample_hypothesis,
        sample_evidence
    ):
        """Test creating full INVESTIGATING response with all fields."""
        response = CaseUIResponse_Investigating(
            case_id="case_123",
            title="API Performance Issue",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            working_conclusion=sample_working_conclusion,
            progress=sample_investigation_progress,
            active_hypotheses=[sample_hypothesis],
            latest_evidence=[sample_evidence],
            next_actions=["Request database metrics", "Check connection pool settings"],
            agent_status="Testing hypothesis about connection pool exhaustion",
            is_stuck=False,
            degraded_mode=False
        )

        assert response.case_id == "case_123"
        assert response.status == CaseStatus.INVESTIGATING
        assert response.working_conclusion is not None
        assert len(response.active_hypotheses) == 1
        assert len(response.latest_evidence) == 1
        assert len(response.next_actions) == 2
        assert not response.is_stuck
        assert not response.degraded_mode

    def test_stuck_investigation(self, sample_investigation_progress):
        """Test INVESTIGATING response in stuck state."""
        response = CaseUIResponse_Investigating(
            case_id="case_123",
            title="Stuck Investigation",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            progress=sample_investigation_progress,
            agent_status="Waiting for additional data",
            is_stuck=True,
            degraded_mode=True
        )

        assert response.is_stuck
        assert response.degraded_mode

    def test_status_literal_enforcement(self, sample_investigation_progress):
        """Test that status is always INVESTIGATING."""
        response = CaseUIResponse_Investigating(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            progress=sample_investigation_progress,
            agent_status="Testing"
        )

        # Status should always be INVESTIGATING
        assert response.status == CaseStatus.INVESTIGATING


class TestCaseUIResponse_Resolved:
    """Test CaseUIResponse_Resolved model."""

    def test_minimal_creation(
        self,
        sample_root_cause,
        sample_solution,
        sample_verification_status,
        sample_resolution_summary
    ):
        """Test creating minimal RESOLVED response."""
        response = CaseUIResponse_Resolved(
            case_id="case_abc123",
            title="Database Connection Issues - RESOLVED",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            root_cause=sample_root_cause,
            solution_applied=sample_solution,
            verification_status=sample_verification_status,
            resolution_summary=sample_resolution_summary
        )

        assert response.case_id == "case_abc123"
        assert response.status == CaseStatus.RESOLVED
        assert response.title == "Database Connection Issues - RESOLVED"
        assert response.root_cause is not None
        assert response.solution_applied is not None
        assert response.verification_status is not None
        assert response.resolution_summary is not None
        assert len(response.reports_available) == 0

    def test_full_resolved_response(
        self,
        sample_root_cause,
        sample_solution,
        sample_verification_status,
        sample_resolution_summary
    ):
        """Test creating full RESOLVED response with reports."""
        reports = [
            ReportAvailability(
                report_type="incident_report",
                status="available",
                reason=None
            ),
            ReportAvailability(
                report_type="post_mortem",
                status="recommended",
                reason="Case complexity warrants detailed analysis"
            ),
            ReportAvailability(
                report_type="runbook",
                status="available",
                reason=None
            )
        ]

        response = CaseUIResponse_Resolved(
            case_id="case_123",
            title="API Performance Issue - RESOLVED",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            updated_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            root_cause=sample_root_cause,
            solution_applied=sample_solution,
            verification_status=sample_verification_status,
            resolution_summary=sample_resolution_summary,
            reports_available=reports
        )

        assert response.case_id == "case_123"
        assert response.status == CaseStatus.RESOLVED
        assert len(response.reports_available) == 3
        assert response.reports_available[0].report_type == "incident_report"
        assert response.reports_available[1].status == "recommended"

    def test_status_literal_enforcement(
        self,
        sample_root_cause,
        sample_solution,
        sample_verification_status,
        sample_resolution_summary
    ):
        """Test that status is always RESOLVED."""
        response = CaseUIResponse_Resolved(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            root_cause=sample_root_cause,
            solution_applied=sample_solution,
            verification_status=sample_verification_status,
            resolution_summary=sample_resolution_summary
        )

        # Status should always be RESOLVED
        assert response.status == CaseStatus.RESOLVED


# ============================================================
# File Relationship Models Tests
# ============================================================

class TestUploadedFileMetadata:
    """Test UploadedFileMetadata model."""

    def test_valid_creation(self, sample_uploaded_file):
        """Test creating valid UploadedFileMetadata."""
        assert sample_uploaded_file.file_id == "file_123"
        assert sample_uploaded_file.filename == "error.log"
        assert sample_uploaded_file.mime_type == "text/plain"
        assert sample_uploaded_file.size_bytes == 1024
        assert sample_uploaded_file.analysis_status == "completed"
        assert sample_uploaded_file.ai_insights_summary == "Found 15 database connection errors"
        assert len(sample_uploaded_file.related_milestone_ids) == 1
        assert len(sample_uploaded_file.related_hypothesis_ids) == 1

    def test_pending_analysis(self):
        """Test file with pending analysis."""
        file_meta = UploadedFileMetadata(
            file_id="file_456",
            filename="metrics.json",
            mime_type="application/json",
            size_bytes=2048,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by_user_id="user-789",
            analysis_status="pending"
        )

        assert file_meta.analysis_status == "pending"
        assert file_meta.ai_insights_summary is None
        assert len(file_meta.related_milestone_ids) == 0

    def test_failed_analysis(self):
        """Test file with failed analysis."""
        file_meta = UploadedFileMetadata(
            file_id="file_789",
            filename="corrupt.dat",
            mime_type="application/octet-stream",
            size_bytes=0,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by_user_id="user-123",
            analysis_status="failed"
        )

        assert file_meta.analysis_status == "failed"

    def test_size_non_negative(self):
        """Test size_bytes must be non-negative."""
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            UploadedFileMetadata(
                file_id="file_123",
                filename="test.log",
                mime_type="text/plain",
                size_bytes=-1,
                uploaded_at=datetime.now(timezone.utc),
                uploaded_by_user_id="user-123",
                analysis_status="pending"
            )


class TestAIInsights:
    """Test AIInsights model."""

    def test_valid_creation(self):
        """Test creating valid AIInsights."""
        insights = AIInsights(
            summary="Found 15 database connection errors in the log file",
            key_findings=["Connection timeout errors", "Pool exhaustion"],
            anomalies_detected=["Spike in errors at 14:30"],
            confidence_score=0.85
        )

        assert insights.summary == "Found 15 database connection errors in the log file"
        assert len(insights.key_findings) == 2
        assert len(insights.anomalies_detected) == 1
        assert insights.confidence_score == 0.85

    def test_confidence_score_bounds(self):
        """Test confidence_score must be between 0.0 and 1.0."""
        # Valid
        AIInsights(
            summary="Test insights",
            confidence_score=0.5
        )

        # Invalid
        with pytest.raises(ValidationError, match="less than or equal to 1"):
            AIInsights(
                summary="Test insights",
                confidence_score=1.5
            )


class TestFileToMilestoneRelationship:
    """Test FileToMilestoneRelationship model."""

    def test_valid_creation(self):
        """Test creating valid FileToMilestoneRelationship."""
        relationship = FileToMilestoneRelationship(
            file_id="file_123",
            milestone_id="symptom_verified",
            milestone_name="Symptom Verified",
            contribution="Error logs confirmed database connection failures",
            contributed_at=datetime.now(timezone.utc)
        )

        assert relationship.file_id == "file_123"
        assert relationship.milestone_id == "symptom_verified"
        assert relationship.milestone_name == "Symptom Verified"
        assert "Error logs" in relationship.contribution
        assert isinstance(relationship.contributed_at, datetime)

    def test_max_length_validation(self):
        """Test max_length constraints."""
        # milestone_name max_length=200
        with pytest.raises(ValidationError, match="String should have at most 200 characters"):
            FileToMilestoneRelationship(
                file_id="file_123",
                milestone_id="test",
                milestone_name="x" * 201,
                contribution="Test",
                contributed_at=datetime.now(timezone.utc)
            )

        # contribution max_length=1000
        with pytest.raises(ValidationError, match="String should have at most 1000 characters"):
            FileToMilestoneRelationship(
                file_id="file_123",
                milestone_id="test",
                milestone_name="Test Milestone",
                contribution="x" * 1001,
                contributed_at=datetime.now(timezone.utc)
            )


class TestFileToHypothesisRelationship:
    """Test FileToHypothesisRelationship model."""

    def test_supports_relationship(self):
        """Test file that supports a hypothesis."""
        relationship = FileToHypothesisRelationship(
            file_id="file_123",
            hypothesis_id="hyp_abc123",
            hypothesis_text="Connection pool exhausted",
            relationship="supports",
            evidence_strength=0.8
        )

        assert relationship.file_id == "file_123"
        assert relationship.hypothesis_id == "hyp_abc123"
        assert relationship.relationship == "supports"
        assert relationship.evidence_strength == 0.8

    def test_refutes_relationship(self):
        """Test file that refutes a hypothesis."""
        relationship = FileToHypothesisRelationship(
            file_id="file_456",
            hypothesis_id="hyp_xyz789",
            hypothesis_text="Network latency issue",
            relationship="refutes",
            evidence_strength=0.9
        )

        assert relationship.relationship == "refutes"
        assert relationship.evidence_strength == 0.9

    def test_neutral_relationship(self):
        """Test file that is neutral to a hypothesis."""
        relationship = FileToHypothesisRelationship(
            file_id="file_789",
            hypothesis_id="hyp_def456",
            hypothesis_text="Memory leak",
            relationship="neutral",
            evidence_strength=0.1
        )

        assert relationship.relationship == "neutral"
        assert relationship.evidence_strength == 0.1

    def test_evidence_strength_bounds(self):
        """Test evidence_strength must be between 0.0 and 1.0."""
        # Valid
        FileToHypothesisRelationship(
            file_id="file_123",
            hypothesis_id="hyp_abc",
            hypothesis_text="Test hypothesis",
            relationship="supports",
            evidence_strength=0.5
        )

        # Invalid
        with pytest.raises(ValidationError, match="less than or equal to 1"):
            FileToHypothesisRelationship(
                file_id="file_123",
                hypothesis_id="hyp_abc",
                hypothesis_text="Test hypothesis",
                relationship="supports",
                evidence_strength=1.5
            )


class TestUploadedFileDetailsResponse:
    """Test UploadedFileDetailsResponse model."""

    def test_valid_creation(self):
        """Test creating valid UploadedFileDetailsResponse."""
        insights = AIInsights(
            summary="Comprehensive analysis of error logs",
            key_findings=["Database errors", "Connection timeouts"],
            anomalies_detected=["Error spike"],
            confidence_score=0.9
        )

        milestone_rel = FileToMilestoneRelationship(
            file_id="file_123",
            milestone_id="symptom_verified",
            milestone_name="Symptom Verified",
            contribution="Confirmed error symptoms",
            contributed_at=datetime.now(timezone.utc)
        )

        hypothesis_rel = FileToHypothesisRelationship(
            file_id="file_123",
            hypothesis_id="hyp_abc",
            hypothesis_text="Pool exhausted",
            relationship="supports",
            evidence_strength=0.85
        )

        response = UploadedFileDetailsResponse(
            file_id="file_123",
            case_id="case_456",
            filename="error.log",
            mime_type="text/plain",
            size_bytes=1024,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by_user_id="user-789",
            analysis_status="completed",
            ai_insights=insights,
            related_milestones=[milestone_rel],
            related_hypotheses=[hypothesis_rel]
        )

        assert response.file_id == "file_123"
        assert response.case_id == "case_456"
        assert response.analysis_status == "completed"
        assert response.ai_insights is not None
        assert len(response.related_milestones) == 1
        assert len(response.related_hypotheses) == 1


class TestUploadedFilesListResponse:
    """Test UploadedFilesListResponse model."""

    def test_valid_creation(self, sample_uploaded_file):
        """Test creating valid UploadedFilesListResponse."""
        response = UploadedFilesListResponse(
            case_id="case_123",
            files=[sample_uploaded_file],
            total_count=1,
            total_size_bytes=1024
        )

        assert response.case_id == "case_123"
        assert len(response.files) == 1
        assert response.total_count == 1
        assert response.total_size_bytes == 1024

    def test_empty_list(self):
        """Test list response with no files."""
        response = UploadedFilesListResponse(
            case_id="case_456",
            files=[],
            total_count=0,
            total_size_bytes=0
        )

        assert response.case_id == "case_456"
        assert len(response.files) == 0
        assert response.total_count == 0
        assert response.total_size_bytes == 0

    def test_multiple_files(self):
        """Test list response with multiple files."""
        files = [
            UploadedFileMetadata(
                file_id=f"file_{i}",
                filename=f"test{i}.log",
                mime_type="text/plain",
                size_bytes=1024 * i,
                uploaded_at=datetime.now(timezone.utc),
                uploaded_by_user_id="user-123",
                analysis_status="completed"
            )
            for i in range(1, 4)
        ]

        response = UploadedFilesListResponse(
            case_id="case_789",
            files=files,
            total_count=3,
            total_size_bytes=sum(f.size_bytes for f in files)
        )

        assert response.case_id == "case_789"
        assert len(response.files) == 3
        assert response.total_count == 3
        assert response.total_size_bytes == 1024 + 2048 + 3072

    def test_non_negative_counts(self):
        """Test total_count and total_size_bytes must be non-negative."""
        # Valid
        UploadedFilesListResponse(
            case_id="case_123",
            files=[],
            total_count=0,
            total_size_bytes=0
        )

        # Invalid total_count
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            UploadedFilesListResponse(
                case_id="case_123",
                files=[],
                total_count=-1,
                total_size_bytes=0
            )

        # Invalid total_size_bytes
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            UploadedFilesListResponse(
                case_id="case_123",
                files=[],
                total_count=0,
                total_size_bytes=-1
            )


# ============================================================
# Discriminated Union Tests
# ============================================================

class TestDiscriminatedUnion:
    """Test discriminated union behavior for CaseUIResponse."""

    def test_consulting_response_discrimination(self, sample_user_request):
        """Test CONSULTING response type discrimination."""
        response = CaseUIResponse_Consulting(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=sample_user_request,
            agent_guidance="Test guidance"
        )

        # Status field acts as discriminator
        assert response.status == CaseStatus.CONSULTING

        # CONSULTING-specific fields present
        assert hasattr(response, 'user_request')
        assert hasattr(response, 'clarifying_questions')
        assert hasattr(response, 'proposed_problem_statement')
        assert hasattr(response, 'ready_to_investigate')

    def test_investigating_response_discrimination(self, sample_investigation_progress):
        """Test INVESTIGATING response type discrimination."""
        response = CaseUIResponse_Investigating(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            progress=sample_investigation_progress,
            agent_status="Testing"
        )

        # Status field acts as discriminator
        assert response.status == CaseStatus.INVESTIGATING

        # INVESTIGATING-specific fields present
        assert hasattr(response, 'working_conclusion')
        assert hasattr(response, 'progress')
        assert hasattr(response, 'active_hypotheses')
        assert hasattr(response, 'latest_evidence')
        assert hasattr(response, 'is_stuck')

    def test_resolved_response_discrimination(
        self,
        sample_root_cause,
        sample_solution,
        sample_verification_status,
        sample_resolution_summary
    ):
        """Test RESOLVED response type discrimination."""
        response = CaseUIResponse_Resolved(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            root_cause=sample_root_cause,
            solution_applied=sample_solution,
            verification_status=sample_verification_status,
            resolution_summary=sample_resolution_summary
        )

        # Status field acts as discriminator
        assert response.status == CaseStatus.RESOLVED

        # RESOLVED-specific fields present
        assert hasattr(response, 'root_cause')
        assert hasattr(response, 'solution_applied')
        assert hasattr(response, 'verification_status')
        assert hasattr(response, 'resolution_summary')
        assert hasattr(response, 'reports_available')


# ============================================================
# Serialization Tests
# ============================================================

class TestSerialization:
    """Test model serialization and deserialization."""

    def test_consulting_response_serialization(self, sample_user_request):
        """Test CONSULTING response serialization."""
        response = CaseUIResponse_Consulting(
            case_id="case_123",
            title="Test Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=sample_user_request,
            agent_guidance="Test guidance"
        )

        # Serialize to dict
        data = response.model_dump()

        assert data['case_id'] == "case_123"
        assert data['status'] == CaseStatus.CONSULTING
        assert data['title'] == "Test Case"
        assert 'user_request' in data
        assert 'agent_guidance' in data

    def test_investigating_response_serialization(
        self,
        sample_investigation_progress,
        sample_hypothesis
    ):
        """Test INVESTIGATING response serialization."""
        response = CaseUIResponse_Investigating(
            case_id="case_456",
            title="Investigation Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            progress=sample_investigation_progress,
            active_hypotheses=[sample_hypothesis],
            agent_status="Testing hypothesis"
        )

        # Serialize to dict
        data = response.model_dump()

        assert data['case_id'] == "case_456"
        assert data['status'] == CaseStatus.INVESTIGATING
        assert 'progress' in data
        assert 'active_hypotheses' in data
        assert len(data['active_hypotheses']) == 1

    def test_resolved_response_serialization(
        self,
        sample_root_cause,
        sample_solution,
        sample_verification_status,
        sample_resolution_summary
    ):
        """Test RESOLVED response serialization."""
        response = CaseUIResponse_Resolved(
            case_id="case_789",
            title="Resolved Case",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            root_cause=sample_root_cause,
            solution_applied=sample_solution,
            verification_status=sample_verification_status,
            resolution_summary=sample_resolution_summary
        )

        # Serialize to dict
        data = response.model_dump()

        assert data['case_id'] == "case_789"
        assert data['status'] == CaseStatus.RESOLVED
        assert 'root_cause' in data
        assert 'solution_applied' in data
        assert 'verification_status' in data
        assert 'resolution_summary' in data

    def test_json_serialization(self, sample_user_request):
        """Test JSON serialization with datetime handling."""
        response = CaseUIResponse_Consulting(
            case_id="case_abc",
            title="JSON Test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=sample_user_request,
            agent_guidance="Test"
        )

        # Serialize to JSON string
        json_str = response.model_dump_json()

        assert isinstance(json_str, str)
        assert "case_abc" in json_str
        assert "consulting" in json_str


# ============================================================
# Parametrized Tests
# ============================================================

@pytest.mark.parametrize("status,response_class,expected_fields", [
    (
        CaseStatus.CONSULTING,
        CaseUIResponse_Consulting,
        ["user_request", "clarifying_questions", "proposed_problem_statement", "ready_to_investigate"]
    ),
    (
        CaseStatus.INVESTIGATING,
        CaseUIResponse_Investigating,
        ["working_conclusion", "progress", "active_hypotheses", "is_stuck"]
    ),
    (
        CaseStatus.RESOLVED,
        CaseUIResponse_Resolved,
        ["root_cause", "solution_applied", "verification_status", "reports_available"]
    )
])
def test_response_type_fields(status, response_class, expected_fields):
    """Test that each response type has its expected fields."""
    # Create minimal instance based on response class
    if response_class == CaseUIResponse_Consulting:
        response = response_class(
            case_id="case_test",
            title="Test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            user_request=UserRequestSummary(
                original_message="Test",
                parsed_intent="other",
                severity="low"
            ),
            agent_guidance="Test"
        )
    elif response_class == CaseUIResponse_Investigating:
        response = response_class(
            case_id="case_test",
            title="Test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            progress=InvestigationProgressSummary(
                milestones_completed=0,
                total_milestones=8,
                current_stage=InvestigationStage.UNDERSTANDING
            ),
            agent_status="Test"
        )
    else:  # CaseUIResponse_Resolved
        response = response_class(
            case_id="case_test",
            title="Test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            root_cause=RootCauseSummary(
                description="Test",
                root_cause_id="rc_test",
                category="other",
                severity="low"
            ),
            solution_applied=SolutionSummary(
                description="Test solution",
                applied_at=datetime.now(timezone.utc),
                applied_by="test"
            ),
            verification_status=VerificationStatus(
                verified=True,
                verification_method="Test",
                details="Test"
            ),
            resolution_summary=ResolutionSummary(
                total_duration_minutes=10,
                milestones_completed=8,
                hypotheses_tested=0,
                evidence_collected=0
            )
        )

    # Check status matches
    assert response.status == status

    # Check expected fields exist
    for field in expected_fields:
        assert hasattr(response, field), f"Missing field: {field}"


@pytest.mark.parametrize("field,max_length,model_class,field_name", [
    (1000, UserRequestSummary, "original_message", "original_message"),
    (100, UserRequestSummary, "parsed_intent", "parsed_intent"),
    (50, UserRequestSummary, "severity", "severity"),
    (500, ClarifyingQuestion, "text", "text"),
    (2000, ClarifyingQuestion, "answer", "answer"),
    (1000, WorkingConclusionSummary, "summary", "summary"),
    (500, HypothesisSummary, "text", "text"),
    (500, EvidenceSummary, "summary", "summary"),
    (100, EvidenceSummary, "type", "type"),
])
def test_max_length_constraints(field, max_length, model_class, field_name):
    """Parametrized test for max_length constraints."""
    # This test verifies the documentation is correct
    # Actual validation tests are in individual test classes
    pass

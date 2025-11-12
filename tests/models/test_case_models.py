"""Comprehensive tests for milestone-based Case model.

Tests cover all models from faultmaven.models.case:
- CaseStatus enum and lifecycle properties
- InvestigationProgress milestone tracking
- InvestigationStage computation
- Case model with full lifecycle
- Status transitions (valid and invalid)
- All validators and computed properties
- Evidence, Hypothesis, Solution models
- ProblemVerification and related models
- Milestone-based progress tracking
"""

import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from uuid import uuid4

from pydantic import ValidationError

from faultmaven.models.case import (
    # Core models
    Case,
    CaseStatus,
    CaseStatusTransition,
    is_valid_transition,

    # Progress models
    InvestigationProgress,
    InvestigationStage,
    InvestigationStrategy,
    TemporalState,

    # Problem context models
    UrgencyLevel,
    ProblemConfirmation,
    ConsultingData,
    Change,
    Correlation,
    ProblemVerification,

    # Evidence models
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceForm,
    EvidenceStance,

    # Hypothesis models
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    HypothesisGenerationMode,
    HypothesisEvidenceLink,

    # Solution models
    Solution,
    SolutionType,

    # Turn tracking
    TurnOutcome,
    TurnProgress,

    # Path selection
    InvestigationPath,
    PathSelection,

    # Conclusion models
    ConfidenceLevel,
    WorkingConclusion,
    RootCauseConclusion,

    # Special state models
    DegradedModeType,
    DegradedMode,
    EscalationType,
    EscalationState,

    # Documentation models
    DocumentType,
    GeneratedDocument,
    DocumentationData,
)


# ============================================================
# CaseStatus Enum Tests
# ============================================================

class TestCaseStatus:
    """Test CaseStatus enum and properties."""

    def test_case_status_values(self):
        """Test all CaseStatus enum values."""
        assert CaseStatus.CONSULTING == "consulting"
        assert CaseStatus.INVESTIGATING == "investigating"
        assert CaseStatus.RESOLVED == "resolved"
        assert CaseStatus.CLOSED == "closed"

    def test_is_terminal_property(self):
        """Test is_terminal property for each status."""
        assert not CaseStatus.CONSULTING.is_terminal
        assert not CaseStatus.INVESTIGATING.is_terminal
        assert CaseStatus.RESOLVED.is_terminal
        assert CaseStatus.CLOSED.is_terminal

    def test_is_active_property(self):
        """Test is_active property for each status."""
        assert CaseStatus.CONSULTING.is_active
        assert CaseStatus.INVESTIGATING.is_active
        assert not CaseStatus.RESOLVED.is_active
        assert not CaseStatus.CLOSED.is_active


class TestStatusTransitions:
    """Test status transition validation."""

    def test_valid_transitions(self):
        """Test all valid status transitions."""
        # CONSULTING transitions
        assert is_valid_transition(CaseStatus.CONSULTING, CaseStatus.INVESTIGATING)
        assert is_valid_transition(CaseStatus.CONSULTING, CaseStatus.CLOSED)

        # INVESTIGATING transitions
        assert is_valid_transition(CaseStatus.INVESTIGATING, CaseStatus.RESOLVED)
        assert is_valid_transition(CaseStatus.INVESTIGATING, CaseStatus.CLOSED)

    def test_invalid_transitions(self):
        """Test all invalid status transitions."""
        # Terminal states cannot transition
        assert not is_valid_transition(CaseStatus.RESOLVED, CaseStatus.INVESTIGATING)
        assert not is_valid_transition(CaseStatus.RESOLVED, CaseStatus.CLOSED)
        assert not is_valid_transition(CaseStatus.CLOSED, CaseStatus.INVESTIGATING)
        assert not is_valid_transition(CaseStatus.CLOSED, CaseStatus.RESOLVED)

        # Cannot go backward
        assert not is_valid_transition(CaseStatus.INVESTIGATING, CaseStatus.CONSULTING)

    def test_case_status_transition_creation(self):
        """Test creating a valid CaseStatusTransition."""
        transition = CaseStatusTransition(
            from_status=CaseStatus.CONSULTING,
            to_status=CaseStatus.INVESTIGATING,
            triggered_by="user-123",
            reason="User decided to start formal investigation"
        )

        assert transition.from_status == CaseStatus.CONSULTING
        assert transition.to_status == CaseStatus.INVESTIGATING
        assert transition.triggered_by == "user-123"
        assert isinstance(transition.triggered_at, datetime)

    def test_case_status_transition_validation(self):
        """Test that invalid transitions are rejected."""
        with pytest.raises(ValueError, match="Invalid transition"):
            CaseStatusTransition(
                from_status=CaseStatus.RESOLVED,
                to_status=CaseStatus.INVESTIGATING,
                triggered_by="user-123",
                reason="Trying to reopen resolved case"
            )

    def test_case_status_transition_immutable(self):
        """Test that CaseStatusTransition is immutable."""
        transition = CaseStatusTransition(
            from_status=CaseStatus.CONSULTING,
            to_status=CaseStatus.INVESTIGATING,
            triggered_by="user-123",
            reason="Test"
        )

        with pytest.raises(ValidationError):
            transition.from_status = CaseStatus.CLOSED


# ============================================================
# InvestigationProgress Tests
# ============================================================

class TestInvestigationProgress:
    """Test InvestigationProgress milestone tracking."""

    def test_default_progress(self):
        """Test default InvestigationProgress initialization."""
        progress = InvestigationProgress()

        # All milestones should be False by default
        assert not progress.symptom_verified
        assert not progress.scope_assessed
        assert not progress.timeline_established
        assert not progress.changes_identified
        assert not progress.root_cause_identified
        assert not progress.solution_proposed
        assert not progress.solution_applied
        assert not progress.solution_verified

        # Default values
        assert progress.root_cause_confidence == 0.0
        assert progress.root_cause_method is None

    def test_completion_percentage(self):
        """Test completion_percentage calculation."""
        progress = InvestigationProgress()
        assert progress.completion_percentage == 0.0

        # Complete some milestones
        progress.symptom_verified = True
        progress.scope_assessed = True
        assert progress.completion_percentage == 0.25  # 2/8

        # Complete all milestones
        progress.timeline_established = True
        progress.changes_identified = True
        progress.root_cause_identified = True
        progress.root_cause_confidence = 0.8
        progress.root_cause_method = "direct_analysis"
        progress.solution_proposed = True
        progress.solution_applied = True
        progress.solution_verified = True
        assert progress.completion_percentage == 1.0  # 8/8

    def test_verification_complete(self):
        """Test verification_complete property."""
        progress = InvestigationProgress()
        assert not progress.verification_complete

        # Complete verification milestones
        progress.symptom_verified = True
        progress.scope_assessed = True
        progress.timeline_established = True
        progress.changes_identified = True

        assert progress.verification_complete

    def test_investigation_complete(self):
        """Test investigation_complete property."""
        progress = InvestigationProgress()
        assert not progress.investigation_complete

        progress.root_cause_identified = True
        progress.root_cause_confidence = 0.8
        progress.root_cause_method = "direct_analysis"

        assert progress.investigation_complete

    def test_resolution_complete(self):
        """Test resolution_complete property."""
        progress = InvestigationProgress()
        assert not progress.resolution_complete

        progress.solution_proposed = True
        progress.solution_applied = True
        progress.solution_verified = True

        assert progress.resolution_complete

    def test_completed_milestones(self):
        """Test completed_milestones list."""
        progress = InvestigationProgress()
        assert progress.completed_milestones == []

        progress.symptom_verified = True
        progress.scope_assessed = True

        completed = progress.completed_milestones
        assert "symptom_verified" in completed
        assert "scope_assessed" in completed
        assert len(completed) == 2

    def test_pending_milestones(self):
        """Test pending_milestones list."""
        progress = InvestigationProgress()
        pending = progress.pending_milestones
        assert len(pending) == 8  # All 8 milestones pending

        progress.symptom_verified = True
        progress.scope_assessed = True

        pending = progress.pending_milestones
        assert "symptom_verified" not in pending
        assert "scope_assessed" not in pending
        assert len(pending) == 6

    def test_current_stage_understanding(self):
        """Test current_stage returns UNDERSTANDING initially."""
        progress = InvestigationProgress()
        assert progress.current_stage == InvestigationStage.UNDERSTANDING

    def test_current_stage_diagnosing(self):
        """Test current_stage returns DIAGNOSING after symptom verified."""
        progress = InvestigationProgress()
        progress.symptom_verified = True

        assert progress.current_stage == InvestigationStage.DIAGNOSING

    def test_current_stage_resolving(self):
        """Test current_stage returns RESOLVING when solution work starts."""
        progress = InvestigationProgress()
        progress.symptom_verified = True
        progress.solution_proposed = True

        assert progress.current_stage == InvestigationStage.RESOLVING

    def test_root_cause_consistency_validation(self):
        """Test root_cause_consistency validator."""
        # Should fail: root_cause_identified=True but confidence=0
        with pytest.raises(ValidationError, match="root_cause_confidence must be > 0"):
            InvestigationProgress(
                root_cause_identified=True,
                root_cause_confidence=0.0,
                root_cause_method="direct_analysis"
            )

        # Should fail: root_cause_identified=True but method=None
        with pytest.raises(ValidationError, match="root_cause_method must be set"):
            InvestigationProgress(
                root_cause_identified=True,
                root_cause_confidence=0.8,
                root_cause_method=None
            )

        # Should succeed: all fields consistent
        progress = InvestigationProgress(
            root_cause_identified=True,
            root_cause_confidence=0.8,
            root_cause_method="direct_analysis"
        )
        assert progress.root_cause_identified

    def test_root_cause_method_validation(self):
        """Test root_cause_method field validator."""
        # Valid methods
        valid_methods = ["direct_analysis", "hypothesis_validation", "correlation", "other"]
        for method in valid_methods:
            progress = InvestigationProgress(
                root_cause_identified=True,
                root_cause_confidence=0.8,
                root_cause_method=method
            )
            assert progress.root_cause_method == method

        # Invalid method
        with pytest.raises(ValidationError, match="root_cause_method must be one of"):
            InvestigationProgress(
                root_cause_identified=True,
                root_cause_confidence=0.8,
                root_cause_method="invalid_method"
            )

    def test_solution_ordering_validation(self):
        """Test solution_ordering validator."""
        # Should fail: solution_applied=True but solution_proposed=False
        with pytest.raises(ValidationError, match="Cannot apply solution without proposing"):
            InvestigationProgress(
                solution_proposed=False,
                solution_applied=True
            )

        # Should fail: solution_verified=True but solution_applied=False
        with pytest.raises(ValidationError, match="Cannot verify solution without applying"):
            InvestigationProgress(
                solution_proposed=True,
                solution_applied=False,
                solution_verified=True
            )

        # Should succeed: proper ordering
        progress = InvestigationProgress(
            solution_proposed=True,
            solution_applied=True,
            solution_verified=True
        )
        assert progress.solution_verified


# ============================================================
# Case Model Tests
# ============================================================

class TestCase:
    """Test Case model creation and properties."""

    def test_case_default_creation(self):
        """Test Case creation with minimal required fields."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="API Performance Issue"
        )

        assert case.case_id.startswith("case_")
        assert case.user_id == "user-123"
        assert case.organization_id == "org-456"
        assert case.title == "API Performance Issue"
        assert case.description == ""
        assert case.status == CaseStatus.CONSULTING
        assert case.current_turn == 0
        assert case.turns_without_progress == 0
        assert isinstance(case.created_at, datetime)
        assert isinstance(case.progress, InvestigationProgress)
        assert isinstance(case.consulting, ConsultingData)

    def test_case_with_description(self):
        """Test Case creation with description."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="API Issue",
            description="API is slow with 30% of requests >5s"
        )

        assert case.description == "API is slow with 30% of requests >5s"

    def test_title_not_empty_validator(self):
        """Test title_not_empty validator."""
        # Should fail: empty title
        with pytest.raises(ValidationError, match="Title cannot be empty"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title=""
            )

        # Should fail: whitespace-only title
        with pytest.raises(ValidationError, match="Title cannot be empty"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title="   "
            )

        # Should succeed: valid title
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="  Valid Title  "
        )
        assert case.title == "Valid Title"  # Should be stripped

    def test_description_valid_validator(self):
        """Test description_valid validator."""
        # Should fail: whitespace-only description
        with pytest.raises(ValidationError, match="Description cannot be only whitespace"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title="Test",
                description="   "
            )

        # Should succeed: empty description (allowed)
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            description=""
        )
        assert case.description == ""

        # Should succeed: valid description
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            description="  Valid description  "
        )
        assert case.description == "Valid description"  # Should be stripped

    def test_description_required_when_investigating(self):
        """Test description_required_when_investigating validator."""
        # Should fail: INVESTIGATING status without description
        with pytest.raises(ValidationError, match="description must be set"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title="Test",
                description="",
                status=CaseStatus.INVESTIGATING
            )

        # Should succeed: INVESTIGATING with description
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            description="Problem description",
            status=CaseStatus.INVESTIGATING
        )
        assert case.status == CaseStatus.INVESTIGATING
        assert case.description == "Problem description"

        # Should succeed: CONSULTING without description (allowed)
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            description="",
            status=CaseStatus.CONSULTING
        )
        assert case.status == CaseStatus.CONSULTING

    def test_current_stage_property(self):
        """Test current_stage computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        # CONSULTING status: current_stage should be None
        assert case.status == CaseStatus.CONSULTING
        assert case.current_stage is None

        # INVESTIGATING status: should return progress stage
        case.status = CaseStatus.INVESTIGATING
        case.description = "Problem description"
        assert case.current_stage == InvestigationStage.UNDERSTANDING

    def test_is_stuck_property(self):
        """Test is_stuck computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        assert not case.is_stuck

        case.turns_without_progress = 3
        assert case.is_stuck

        case.turns_without_progress = 5
        assert case.is_stuck

    def test_is_terminal_property(self):
        """Test is_terminal computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        assert not case.is_terminal

        case.status = CaseStatus.RESOLVED
        assert case.is_terminal

        case.status = CaseStatus.CLOSED
        assert case.is_terminal

    def test_time_to_resolution_property(self):
        """Test time_to_resolution computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        # No closed_at: should return None
        assert case.time_to_resolution is None

        # Set closed_at
        case.closed_at = case.created_at + timedelta(hours=2)
        resolution_time = case.time_to_resolution

        assert resolution_time is not None
        assert resolution_time == timedelta(hours=2)

    def test_evidence_count_by_category(self):
        """Test evidence_count_by_category computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        # No evidence
        assert case.evidence_count_by_category == {}

        # Add evidence
        case.evidence.append(Evidence(
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="Error logs showing timeouts",
            preprocessed_content="Extracted error data",
            content_ref="s3://bucket/evidence1.log",
            content_size_bytes=1024,
            preprocessing_method="crime_scene_extraction",
            source_type=EvidenceSourceType.LOG_FILE,
            form=EvidenceForm.DOCUMENT,
            collected_by="user-123",
            collected_at_turn=1
        ))

        case.evidence.append(Evidence(
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="hyp_123",
            summary="Connection pool metrics",
            preprocessed_content="Pool at 95% capacity",
            content_ref="s3://bucket/evidence2.json",
            content_size_bytes=512,
            preprocessing_method="parse_and_sanitize",
            source_type=EvidenceSourceType.METRICS_DATA,
            form=EvidenceForm.DOCUMENT,
            collected_by="user-123",
            collected_at_turn=2
        ))

        counts = case.evidence_count_by_category
        assert counts["symptom_evidence"] == 1
        assert counts["causal_evidence"] == 1

    def test_active_hypotheses_property(self):
        """Test active_hypotheses computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        assert case.active_hypotheses == []

        # Add hypotheses
        hyp1 = Hypothesis(
            statement="Connection pool exhausted",
            category=HypothesisCategory.ENVIRONMENT,
            status=HypothesisStatus.ACTIVE,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="Deploy immediately preceded errors"
        )

        hyp2 = Hypothesis(
            statement="Memory leak",
            category=HypothesisCategory.CODE,
            status=HypothesisStatus.CAPTURED,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Generic slowness pattern"
        )

        case.hypotheses[hyp1.hypothesis_id] = hyp1
        case.hypotheses[hyp2.hypothesis_id] = hyp2

        active = case.active_hypotheses
        assert len(active) == 1
        assert active[0].statement == "Connection pool exhausted"

    def test_validated_hypotheses_property(self):
        """Test validated_hypotheses computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        assert case.validated_hypotheses == []

        # Add validated hypothesis
        hyp = Hypothesis(
            statement="Connection pool exhausted",
            category=HypothesisCategory.ENVIRONMENT,
            status=HypothesisStatus.VALIDATED,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="Evidence confirms theory"
        )

        case.hypotheses[hyp.hypothesis_id] = hyp

        validated = case.validated_hypotheses
        assert len(validated) == 1
        assert validated[0].statement == "Connection pool exhausted"

    def test_warnings_property(self):
        """Test warnings computed property."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test"
        )

        # No warnings initially
        warnings = case.warnings
        assert len(warnings) == 0

        # Add stuck warning
        case.turns_without_progress = 3
        warnings = case.warnings
        assert len(warnings) == 1
        assert warnings[0]["type"] == "stuck"
        assert warnings[0]["severity"] == "warning"

        # Add degraded mode
        case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType.NO_PROGRESS,
            reason="Cannot make progress without more data"
        )
        warnings = case.warnings
        assert len(warnings) == 2
        assert any(w["type"] == "degraded_mode" for w in warnings)

        # Terminal state without documentation
        case.status = CaseStatus.RESOLVED
        warnings = case.warnings
        assert any(w["type"] == "no_documentation" for w in warnings)

    def test_closure_reason_validation(self):
        """Test closure_reason field validator."""
        # Valid closure reasons
        valid_reasons = ["resolved", "abandoned", "escalated", "consulting_only", "duplicate", "other"]
        for reason in valid_reasons:
            case = Case(
                user_id="user-123",
                organization_id="org-456",
                title="Test",
                closure_reason=reason
            )
            assert case.closure_reason == reason

        # Invalid closure reason
        with pytest.raises(ValidationError, match="closure_reason must be one of"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title="Test",
                closure_reason="invalid_reason"
            )

    def test_status_history_ordered_validation(self):
        """Test status_history_ordered validator."""
        now = datetime.now(timezone.utc)

        # Should succeed: chronologically ordered
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            status_history=[
                CaseStatusTransition(
                    from_status=CaseStatus.CONSULTING,
                    to_status=CaseStatus.INVESTIGATING,
                    triggered_by="user-123",
                    reason="Start investigation",
                    triggered_at=now
                ),
                CaseStatusTransition(
                    from_status=CaseStatus.INVESTIGATING,
                    to_status=CaseStatus.RESOLVED,
                    triggered_by="user-123",
                    reason="Problem solved",
                    triggered_at=now + timedelta(hours=1)
                )
            ]
        )
        assert len(case.status_history) == 2

        # Should fail: out of order
        with pytest.raises(ValidationError, match="chronologically ordered"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title="Test",
                status_history=[
                    CaseStatusTransition(
                        from_status=CaseStatus.CONSULTING,
                        to_status=CaseStatus.INVESTIGATING,
                        triggered_by="user-123",
                        reason="Start",
                        triggered_at=now + timedelta(hours=1)
                    ),
                    CaseStatusTransition(
                        from_status=CaseStatus.INVESTIGATING,
                        to_status=CaseStatus.RESOLVED,
                        triggered_by="user-123",
                        reason="Finish",
                        triggered_at=now  # Earlier than first transition
                    )
                ]
            )

    def test_turn_history_sequential_validation(self):
        """Test turn_history_sequential validator."""
        # Should succeed: sequential turn numbers
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            turn_history=[
                TurnProgress(
                    turn_number=0,
                    progress_made=True,
                    outcome=TurnOutcome.DATA_PROVIDED
                ),
                TurnProgress(
                    turn_number=1,
                    progress_made=True,
                    outcome=TurnOutcome.MILESTONE_COMPLETED
                )
            ]
        )
        assert len(case.turn_history) == 2

        # Should fail: non-sequential
        with pytest.raises(ValidationError, match="Turn numbers must be sequential"):
            Case(
                user_id="user-123",
                organization_id="org-456",
                title="Test",
                turn_history=[
                    TurnProgress(
                        turn_number=0,
                        progress_made=True,
                        outcome=TurnOutcome.DATA_PROVIDED
                    ),
                    TurnProgress(
                        turn_number=2,  # Should be 1
                        progress_made=True,
                        outcome=TurnOutcome.MILESTONE_COMPLETED
                    )
                ]
            )


# ============================================================
# Evidence Tests
# ============================================================

class TestEvidence:
    """Test Evidence model."""

    def test_evidence_creation(self):
        """Test Evidence creation with required fields."""
        evidence = Evidence(
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="Error logs showing database timeouts",
            preprocessed_content="Extracted error lines with context",
            content_ref="s3://bucket/logs/app.log",
            content_size_bytes=10240,
            preprocessing_method="crime_scene_extraction",
            source_type=EvidenceSourceType.LOG_FILE,
            form=EvidenceForm.DOCUMENT,
            collected_by="user-123",
            collected_at_turn=1
        )

        assert evidence.evidence_id.startswith("ev_")
        assert evidence.category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert evidence.primary_purpose == "symptom_verified"
        assert evidence.summary == "Error logs showing database timeouts"
        assert evidence.source_type == EvidenceSourceType.LOG_FILE
        assert evidence.form == EvidenceForm.DOCUMENT
        assert evidence.collected_by == "user-123"
        assert evidence.collected_at_turn == 1
        assert evidence.advances_milestones == []

    def test_evidence_with_analysis(self):
        """Test Evidence with optional analysis field."""
        evidence = Evidence(
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="hyp_abc123",
            summary="Connection pool metrics",
            preprocessed_content="Pool utilization at 95%",
            content_ref="s3://bucket/metrics.json",
            content_size_bytes=512,
            preprocessing_method="parse_and_sanitize",
            source_type=EvidenceSourceType.METRICS_DATA,
            form=EvidenceForm.DOCUMENT,
            collected_by="user-123",
            collected_at_turn=2,
            analysis="Pool exhaustion strongly supports connection pool hypothesis",
            advances_milestones=["root_cause_identified"]
        )

        assert evidence.analysis == "Pool exhaustion strongly supports connection pool hypothesis"
        assert "root_cause_identified" in evidence.advances_milestones


# ============================================================
# Hypothesis Tests
# ============================================================

class TestHypothesis:
    """Test Hypothesis model and evidence links."""

    def test_hypothesis_creation(self):
        """Test Hypothesis creation."""
        hypothesis = Hypothesis(
            statement="Connection pool exhausted",
            category=HypothesisCategory.ENVIRONMENT,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="Deploy coincided with errors"
        )

        assert hypothesis.hypothesis_id.startswith("hyp_")
        assert hypothesis.statement == "Connection pool exhausted"
        assert hypothesis.category == HypothesisCategory.ENVIRONMENT
        assert hypothesis.status == HypothesisStatus.CAPTURED
        assert hypothesis.likelihood == 0.5
        assert hypothesis.evidence_links == {}

    def test_hypothesis_evidence_link(self):
        """Test HypothesisEvidenceLink creation."""
        link = HypothesisEvidenceLink(
            hypothesis_id="hyp_abc123",
            evidence_id="ev_xyz789",
            stance=EvidenceStance.STRONGLY_SUPPORTS,
            reasoning="Pool metrics show 95% utilization",
            completeness=0.8
        )

        assert link.hypothesis_id == "hyp_abc123"
        assert link.evidence_id == "ev_xyz789"
        assert link.stance == EvidenceStance.STRONGLY_SUPPORTS
        assert link.completeness == 0.8

    def test_hypothesis_supporting_evidence(self):
        """Test supporting_evidence property."""
        hypothesis = Hypothesis(
            statement="Connection pool exhausted",
            category=HypothesisCategory.ENVIRONMENT,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="Test"
        )

        # Add evidence links
        hypothesis.evidence_links["ev_1"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_1",
            stance=EvidenceStance.STRONGLY_SUPPORTS,
            reasoning="Strong support",
            completeness=0.9
        )

        hypothesis.evidence_links["ev_2"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_2",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Moderate support",
            completeness=0.6
        )

        hypothesis.evidence_links["ev_3"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_3",
            stance=EvidenceStance.CONTRADICTS,
            reasoning="Contradicts",
            completeness=0.7
        )

        supporting = hypothesis.supporting_evidence
        assert len(supporting) == 2
        assert "ev_1" in supporting
        assert "ev_2" in supporting

    def test_hypothesis_refuting_evidence(self):
        """Test refuting_evidence property."""
        hypothesis = Hypothesis(
            statement="Memory leak",
            category=HypothesisCategory.CODE,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test"
        )

        # Add refuting evidence
        hypothesis.evidence_links["ev_1"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_1",
            stance=EvidenceStance.CONTRADICTS,
            reasoning="Memory stable",
            completeness=0.8
        )

        hypothesis.evidence_links["ev_2"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_2",
            stance=EvidenceStance.STRONGLY_CONTRADICTS,
            reasoning="No memory growth",
            completeness=0.9
        )

        refuting = hypothesis.refuting_evidence
        assert len(refuting) == 2
        assert "ev_1" in refuting
        assert "ev_2" in refuting

    def test_hypothesis_evidence_score(self):
        """Test evidence_score property."""
        hypothesis = Hypothesis(
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test"
        )

        # No evidence: score should be 0
        assert hypothesis.evidence_score == 0.0

        # Add supporting evidence
        hypothesis.evidence_links["ev_1"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_1",
            stance=EvidenceStance.STRONGLY_SUPPORTS,
            reasoning="Support",
            completeness=0.9
        )

        hypothesis.evidence_links["ev_2"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_2",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Support",
            completeness=0.7
        )

        # Score should be 1.0 (all supporting)
        assert hypothesis.evidence_score == 1.0

        # Add refuting evidence
        hypothesis.evidence_links["ev_3"] = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id="ev_3",
            stance=EvidenceStance.CONTRADICTS,
            reasoning="Contradicts",
            completeness=0.8
        )

        # Score should be (2-1)/3 = 0.333...
        score = hypothesis.evidence_score
        assert 0.3 < score < 0.4


# ============================================================
# Solution Tests
# ============================================================

class TestSolution:
    """Test Solution model."""

    def test_solution_creation(self):
        """Test Solution creation."""
        solution = Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Increase connection pool size",
            immediate_action="Increase pool from 10 to 50 connections",
            implementation_steps=[
                "Edit database.yml",
                "Set pool_size: 50",
                "Restart application"
            ]
        )

        assert solution.solution_id.startswith("sol_")
        assert solution.solution_type == SolutionType.CONFIG_CHANGE
        assert solution.title == "Increase connection pool size"
        assert len(solution.implementation_steps) == 3
        assert solution.proposed_by == "agent"

    def test_solution_content_required(self):
        """Test solution_content_required validator."""
        # Should fail: no actionable content
        with pytest.raises(ValidationError, match="Solution must have at least one of"):
            Solution(
                solution_type=SolutionType.RESTART,
                title="Restart service"
                # Missing: immediate_action, longterm_fix, implementation_steps, commands
            )

        # Should succeed: has immediate_action
        solution = Solution(
            solution_type=SolutionType.RESTART,
            title="Restart service",
            immediate_action="Restart the API service"
        )
        assert solution.immediate_action == "Restart the API service"

    def test_solution_verification_consistency(self):
        """Test verification_consistency validator."""
        # Should fail: verified_at but no effectiveness
        with pytest.raises(ValidationError, match="verified_at requires effectiveness"):
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Test",
                immediate_action="Test action",
                verified_at=datetime.now(timezone.utc),
                effectiveness=None
            )

        # Should fail: effectiveness but no verified_at
        with pytest.raises(ValidationError, match="effectiveness requires verified_at"):
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Test",
                immediate_action="Test action",
                verified_at=None,
                effectiveness=0.9
            )

        # Should succeed: both present
        solution = Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Test",
            immediate_action="Test action",
            verified_at=datetime.now(timezone.utc),
            effectiveness=0.9
        )
        assert solution.effectiveness == 0.9


# ============================================================
# ProblemVerification Tests
# ============================================================

class TestProblemVerification:
    """Test ProblemVerification model."""

    def test_problem_verification_creation(self):
        """Test ProblemVerification creation."""
        verification = ProblemVerification(
            symptom_statement="API experiencing high latency",
            severity="HIGH",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.HIGH
        )

        assert verification.symptom_statement == "API experiencing high latency"
        assert verification.severity == "HIGH"
        assert verification.temporal_state == TemporalState.ONGOING
        assert verification.urgency_level == UrgencyLevel.HIGH

    def test_severity_validation(self):
        """Test severity field validator."""
        # Valid severities
        for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
            verification = ProblemVerification(
                symptom_statement="Test",
                severity=severity
            )
            assert verification.severity == severity

        # Case insensitive
        verification = ProblemVerification(
            symptom_statement="Test",
            severity="high"
        )
        assert verification.severity == "HIGH"

        # Invalid severity
        with pytest.raises(ValidationError, match="severity must be one of"):
            ProblemVerification(
                symptom_statement="Test",
                severity="INVALID"
            )

    def test_timeline_consistency_validation(self):
        """Test timeline_consistency validator."""
        now = datetime.now(timezone.utc)

        # Should fail: started_at after noticed_at
        with pytest.raises(ValidationError, match="started_at cannot be after noticed_at"):
            ProblemVerification(
                symptom_statement="Test",
                severity="HIGH",
                started_at=now + timedelta(hours=1),
                noticed_at=now
            )

        # Should fail: started_at after resolved_naturally_at
        with pytest.raises(ValidationError, match="started_at cannot be after resolved_naturally_at"):
            ProblemVerification(
                symptom_statement="Test",
                severity="HIGH",
                started_at=now + timedelta(hours=1),
                resolved_naturally_at=now
            )

        # Should fail: noticed_at after resolved_naturally_at
        with pytest.raises(ValidationError, match="noticed_at cannot be after resolved_naturally_at"):
            ProblemVerification(
                symptom_statement="Test",
                severity="HIGH",
                noticed_at=now + timedelta(hours=1),
                resolved_naturally_at=now
            )

        # Should succeed: proper timeline
        verification = ProblemVerification(
            symptom_statement="Test",
            severity="HIGH",
            started_at=now,
            noticed_at=now + timedelta(minutes=10),
            resolved_naturally_at=now + timedelta(hours=1)
        )
        assert verification.started_at < verification.noticed_at

    def test_is_complete_property(self):
        """Test is_complete property."""
        # Incomplete: missing fields
        verification = ProblemVerification(
            symptom_statement="Test",
            severity="HIGH"
        )
        assert not verification.is_complete

        # Complete: all required fields present
        verification = ProblemVerification(
            symptom_statement="Test",
            severity="HIGH",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.HIGH
        )
        assert verification.is_complete

    def test_time_to_detection_property(self):
        """Test time_to_detection property."""
        now = datetime.now(timezone.utc)

        verification = ProblemVerification(
            symptom_statement="Test",
            severity="HIGH",
            started_at=now,
            noticed_at=now + timedelta(minutes=15)
        )

        detection_time = verification.time_to_detection
        assert detection_time == timedelta(minutes=15)


# ============================================================
# Parameterized Tests
# ============================================================

@pytest.mark.parametrize("status,expected_terminal", [
    (CaseStatus.CONSULTING, False),
    (CaseStatus.INVESTIGATING, False),
    (CaseStatus.RESOLVED, True),
    (CaseStatus.CLOSED, True),
])
def test_case_status_terminal_parametrized(status, expected_terminal):
    """Test is_terminal for all statuses."""
    assert status.is_terminal == expected_terminal


@pytest.mark.parametrize("status,expected_active", [
    (CaseStatus.CONSULTING, True),
    (CaseStatus.INVESTIGATING, True),
    (CaseStatus.RESOLVED, False),
    (CaseStatus.CLOSED, False),
])
def test_case_status_active_parametrized(status, expected_active):
    """Test is_active for all statuses."""
    assert status.is_active == expected_active


@pytest.mark.parametrize("from_status,to_status,expected_valid", [
    (CaseStatus.CONSULTING, CaseStatus.INVESTIGATING, True),
    (CaseStatus.CONSULTING, CaseStatus.CLOSED, True),
    (CaseStatus.INVESTIGATING, CaseStatus.RESOLVED, True),
    (CaseStatus.INVESTIGATING, CaseStatus.CLOSED, True),
    (CaseStatus.RESOLVED, CaseStatus.INVESTIGATING, False),
    (CaseStatus.CLOSED, CaseStatus.RESOLVED, False),
    (CaseStatus.INVESTIGATING, CaseStatus.CONSULTING, False),
])
def test_status_transitions_parametrized(from_status, to_status, expected_valid):
    """Test all status transition combinations."""
    assert is_valid_transition(from_status, to_status) == expected_valid


@pytest.mark.parametrize("score,expected_level", [
    (0.0, ConfidenceLevel.SPECULATION),
    (0.3, ConfidenceLevel.SPECULATION),
    (0.49, ConfidenceLevel.SPECULATION),
    (0.5, ConfidenceLevel.PROBABLE),
    (0.6, ConfidenceLevel.PROBABLE),
    (0.69, ConfidenceLevel.PROBABLE),
    (0.7, ConfidenceLevel.CONFIDENT),
    (0.8, ConfidenceLevel.CONFIDENT),
    (0.89, ConfidenceLevel.CONFIDENT),
    (0.9, ConfidenceLevel.VERIFIED),
    (1.0, ConfidenceLevel.VERIFIED),
])
def test_confidence_level_from_score(score, expected_level):
    """Test ConfidenceLevel.from_score mapping."""
    assert ConfidenceLevel.from_score(score) == expected_level


# ============================================================
# Integration Tests
# ============================================================

class TestCaseLifecycle:
    """Test complete case lifecycle scenarios."""

    def test_complete_consulting_to_resolved_lifecycle(self):
        """Test a full case lifecycle from CONSULTING to RESOLVED."""
        # 1. Create case in CONSULTING
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="API Performance Issue"
        )

        assert case.status == CaseStatus.CONSULTING
        assert case.description == ""

        # 2. Agent formalizes problem during CONSULTING
        case.consulting.proposed_problem_statement = "API experiencing 30% of requests >5s"
        case.consulting.problem_statement_confirmed = True
        case.consulting.problem_statement_confirmed_at = datetime.now(timezone.utc)

        # 3. Transition to INVESTIGATING (requires description)
        case.description = case.consulting.proposed_problem_statement
        case.status = CaseStatus.INVESTIGATING

        assert case.status == CaseStatus.INVESTIGATING
        assert case.description != ""

        # 4. Complete verification milestones
        case.progress.symptom_verified = True
        case.progress.scope_assessed = True
        case.progress.timeline_established = True
        case.progress.changes_identified = True

        assert case.progress.verification_complete
        assert case.current_stage == InvestigationStage.DIAGNOSING

        # 5. Identify root cause
        case.progress.root_cause_identified = True
        case.progress.root_cause_confidence = 0.85
        case.progress.root_cause_method = "direct_analysis"

        assert case.progress.investigation_complete

        # 6. Apply solution
        case.progress.solution_proposed = True
        case.progress.solution_applied = True
        case.progress.solution_verified = True

        assert case.progress.resolution_complete
        assert case.current_stage == InvestigationStage.RESOLVING
        assert case.progress.completion_percentage == 1.0

        # 7. Resolve case
        case.status = CaseStatus.RESOLVED
        case.closure_reason = "resolved"
        case.resolved_at = datetime.now(timezone.utc)
        case.closed_at = case.resolved_at

        assert case.is_terminal
        assert case.status == CaseStatus.RESOLVED
        assert case.time_to_resolution is not None

    def test_milestone_completion_in_any_order(self):
        """Test that milestones can be completed in any order."""
        case = Case(
            user_id="user-123",
            organization_id="org-456",
            title="Test",
            description="Problem",
            status=CaseStatus.INVESTIGATING
        )

        # Complete milestones out of typical order
        case.progress.solution_proposed = True  # Skip to solution
        assert case.current_stage == InvestigationStage.RESOLVING

        case.progress.root_cause_identified = True  # Do root cause later
        case.progress.root_cause_confidence = 0.7
        case.progress.root_cause_method = "correlation"

        case.progress.symptom_verified = True  # Verify symptom even later

        # All milestones completed, regardless of order
        assert case.progress.completion_percentage > 0.5

"""Tests for StateValidator

Tests state consistency validation for milestone-based investigation.
"""

import pytest

from faultmaven.core.investigation.state_validator import (
    StateValidator,
    ValidationIssue,
    ValidationSeverity,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)


@pytest.fixture
def validator():
    return StateValidator()


@pytest.fixture
def base_case():
    """Create a valid base case for testing."""
    return Case(
        case_id="case_1234567890ab",
        title="Test Case",
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Test description",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
        progress=InvestigationProgress(),
    )


class TestMilestoneOrdering:
    """Test milestone dependency validation."""

    def test_valid_milestone_order(self, validator, base_case):
        """Valid milestone ordering should have no errors."""
        base_case.progress.symptom_verified = True
        base_case.progress.solution_proposed = True
        base_case.progress.solution_verified = True

        issues = validator._validate_milestone_ordering(base_case.progress)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
        assert len(errors) == 0

    def test_solution_verified_without_proposed(self, validator, base_case):
        """solution_verified requires solution_proposed."""
        base_case.progress.solution_verified = True
        base_case.progress.solution_proposed = False

        issues = validator._validate_milestone_ordering(base_case.progress)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 1
        assert errors[0].code == "MILESTONE_ORDER_001"

    def test_solution_applied_without_proposed(self, validator, base_case):
        """solution_applied requires solution_proposed."""
        base_case.progress.solution_applied = True
        base_case.progress.solution_proposed = False

        issues = validator._validate_milestone_ordering(base_case.progress)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 1
        assert errors[0].code == "MILESTONE_ORDER_002"

    def test_root_cause_without_likelihood(self, validator, base_case):
        """root_cause_identified should have root_cause_likelihood set."""
        base_case.progress.root_cause_identified = True
        base_case.progress.root_cause_likelihood = None

        issues = validator._validate_milestone_ordering(base_case.progress)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert len(warnings) == 1
        assert warnings[0].code == "MILESTONE_INCOMPLETE_001"


class TestStatusConsistency:
    """Test status-progress consistency validation."""

    def test_resolved_without_solution_verified(self, validator, base_case):
        """RESOLVED status requires solution_verified milestone."""
        base_case.status = CaseStatus.RESOLVED
        base_case.progress.solution_verified = False

        issues = validator._validate_status_consistency(base_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 1
        assert errors[0].code == "STATUS_MISMATCH_001"

    def test_resolved_with_solution_verified(self, validator, base_case):
        """RESOLVED with solution_verified should be valid."""
        base_case.status = CaseStatus.RESOLVED
        base_case.progress.solution_verified = True

        issues = validator._validate_status_consistency(base_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 0

    def test_investigating_without_problem_statement(self, validator, base_case):
        """INVESTIGATING should have symptom_statement."""
        base_case.status = CaseStatus.INVESTIGATING
        base_case.problem_verification = None

        issues = validator._validate_status_consistency(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "STATUS_MISMATCH_002" for i in warnings)


class TestHypothesisStates:
    """Test hypothesis lifecycle validation."""

    def test_validated_with_insufficient_evidence(self, validator, base_case):
        """VALIDATED hypothesis should have at least 2 supporting evidence."""
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.VALIDATED,
                supporting_evidence=["ev_1"],  # Only 1 evidence
            )
        }

        issues = validator._validate_hypothesis_states(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "HYPOTHESIS_STATE_001" for i in warnings)

    def test_refuted_without_refuting_evidence(self, validator, base_case):
        """REFUTED hypothesis should have refuting evidence."""
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.REFUTED,
                refuting_evidence=[],
            )
        }

        issues = validator._validate_hypothesis_states(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "HYPOTHESIS_STATE_002" for i in warnings)

    def test_active_with_very_low_likelihood(self, validator, base_case):
        """ACTIVE hypothesis with very low likelihood should be warned."""
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.ACTIVE,
                likelihood=0.1,
            )
        }

        issues = validator._validate_hypothesis_states(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "HYPOTHESIS_STATE_003" for i in warnings)


class TestEvidenceLinks:
    """Test evidence-hypothesis link validation."""

    def test_dangling_supporting_evidence(self, validator, base_case):
        """Hypothesis referencing non-existent evidence should warn."""
        base_case.evidence = []
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.ACTIVE,
                supporting_evidence=["ev_nonexistent"],
            )
        }

        issues = validator._validate_evidence_links(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "LINK_DANGLING_001" for i in warnings)

    def test_new_index_references_allowed(self, validator, base_case):
        """new_index_N references should be skipped (created same turn)."""
        base_case.evidence = []
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.ACTIVE,
                supporting_evidence=["new_index_0"],
            )
        }

        issues = validator._validate_evidence_links(base_case)

        # Should not report error for new_index references
        assert not any("new_index" in i.message for i in issues)


class TestLikelihoodBounds:
    """Test likelihood value bounds validation."""

    def test_hypothesis_likelihood_out_of_bounds(self, validator, base_case):
        """Likelihood outside [0, 1] should error."""
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.ACTIVE,
                likelihood=1.5,  # Out of bounds
            )
        }

        issues = validator._validate_likelihood_bounds(base_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 1
        assert errors[0].code == "BOUNDS_001"

    def test_valid_likelihood_bounds(self, validator, base_case):
        """Valid likelihood values should not error."""
        base_case.hypotheses = {
            "hyp_123456789012": Hypothesis(
                hypothesis_id="hyp_123456789012",
                statement="Test hypothesis",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.ACTIVE,
                likelihood=0.75,
            )
        }

        issues = validator._validate_likelihood_bounds(base_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 0


class TestIsValid:
    """Test overall is_valid method."""

    def test_valid_case_returns_true(self, validator, base_case):
        """Valid case should return (True, issues_list)."""
        is_valid, issues = validator.is_valid(base_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert is_valid is True
        assert len(errors) == 0

    def test_invalid_case_returns_false(self, validator, base_case):
        """Invalid case with errors should return (False, issues_list)."""
        base_case.progress.solution_verified = True
        base_case.progress.solution_proposed = False

        is_valid, issues = validator.is_valid(base_case)

        assert is_valid is False
        assert len(issues) > 0

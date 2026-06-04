"""Tests for StateValidator

Tests state consistency validation for milestone-based investigation.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.state_validator import (
    StateValidator,
    ValidationIssue,
    ValidationSeverity,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
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
        state=CaseState.INVESTIGATING,
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
        base_case.progress.solution_accepted = True
        base_case.progress.solution_verified = True

        issues = validator._validate_milestone_ordering(base_case.progress)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
        assert len(errors) == 0

    def test_solution_verified_without_proposed(self, validator, base_case):
        """solution_verified requires solution_proposed and solution_accepted."""
        base_case.progress.solution_verified = True
        base_case.progress.solution_proposed = False

        issues = validator._validate_milestone_ordering(base_case.progress)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        # Triggers MILESTONE_ORDER_001 (no solution_proposed) and
        # MILESTONE_ORDER_003 (no solution_accepted)
        assert len(errors) == 2
        error_codes = {e.code for e in errors}
        assert "MILESTONE_ORDER_001" in error_codes
        assert "MILESTONE_ORDER_003" in error_codes

    def test_solution_accepted_without_proposed(self, validator, base_case):
        """solution_accepted requires solution_proposed."""
        base_case.progress.solution_accepted = True
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
    """Test state-progress consistency validation."""

    def test_resolved_without_solution_verified(self, validator, base_case):
        """RESOLVED state requires solution_verified milestone."""
        # Update state and resolved_at together to pass Pydantic validation
        resolved_case = base_case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": datetime.now(timezone.utc),
            }
        )
        resolved_case.progress.solution_verified = False

        issues = validator._validate_status_consistency(resolved_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 1
        assert errors[0].code == "STATUS_MISMATCH_001"

    def test_resolved_with_solution_verified(self, validator, base_case):
        """RESOLVED with solution_verified should be valid."""
        # Update state and resolved_at together to pass Pydantic validation
        resolved_case = base_case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": datetime.now(timezone.utc),
            }
        )
        resolved_case.progress.solution_verified = True

        issues = validator._validate_status_consistency(resolved_case)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]

        assert len(errors) == 0

    def test_investigating_without_problem_statement(self, validator, base_case):
        """INVESTIGATING should have symptom_statement."""
        base_case.state = CaseState.INVESTIGATING
        base_case.problem_verification = None

        issues = validator._validate_status_consistency(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "STATUS_MISMATCH_002" for i in warnings)


class TestHypothesisStates:
    """Test hypothesis lifecycle validation."""

    def test_validated_with_insufficient_evidence(self, validator, base_case):
        """VALIDATED hypothesis should have at least 2 supporting evidence."""
        from faultmaven.modules.case.contracts import (
            EvidenceStance,
            HypothesisEvidenceLink,
        )

        hyp = Hypothesis(
            hypothesis_id="hyp_123456789012",
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            state=HypothesisState.VALIDATED,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test hypothesis",
        )
        # Add only 1 supporting evidence
        hyp.evidence_links.append(
            HypothesisEvidenceLink(
                hypothesis_id="hyp_123456789012",
                evidence_id="ev_1",
                stance=EvidenceStance.SUPPORTS,
                reasoning="Test evidence",
                stance_confidence=0.8,
            )
        )
        base_case.hypotheses = {"hyp_123456789012": hyp}

        issues = validator._validate_hypothesis_states(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "HYPOTHESIS_STATE_001" for i in warnings)

    def test_refuted_without_refuting_evidence(self, validator, base_case):
        """REFUTED hypothesis should have refuting evidence."""
        hyp = Hypothesis(
            hypothesis_id="hyp_123456789012",
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            state=HypothesisState.REFUTED,
            refutation_reason="disproved by counter-evidence",
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test hypothesis",
        )
        # No refuting evidence added
        base_case.hypotheses = {"hyp_123456789012": hyp}

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
                state=HypothesisState.ACTIVE,
                generated_at_turn=1,
                generation_mode=HypothesisGenerationMode.SYSTEMATIC,
                rationale="Test hypothesis",
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
        from faultmaven.modules.case.contracts import (
            EvidenceStance,
            HypothesisEvidenceLink,
        )

        base_case.evidence = []
        hyp = Hypothesis(
            hypothesis_id="hyp_123456789012",
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            state=HypothesisState.ACTIVE,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test hypothesis",
        )
        # Reference non-existent evidence
        hyp.evidence_links.append(
            HypothesisEvidenceLink(
                hypothesis_id="hyp_123456789012",
                evidence_id="ev_nonexistent",
                stance=EvidenceStance.SUPPORTS,
                reasoning="Test evidence",
                stance_confidence=0.8,
            )
        )
        base_case.hypotheses = {"hyp_123456789012": hyp}

        issues = validator._validate_evidence_links(base_case)
        warnings = [i for i in issues if i.severity == ValidationSeverity.WARNING]

        assert any(i.code == "LINK_DANGLING_001" for i in warnings)

    def test_new_index_references_allowed(self, validator, base_case):
        """new_index_N references should be skipped (created same turn)."""
        from faultmaven.modules.case.contracts import (
            EvidenceStance,
            HypothesisEvidenceLink,
        )

        base_case.evidence = []
        hyp = Hypothesis(
            hypothesis_id="hyp_123456789012",
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            state=HypothesisState.ACTIVE,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test hypothesis",
        )
        # Reference new_index (created same turn)
        hyp.evidence_links.append(
            HypothesisEvidenceLink(
                hypothesis_id="hyp_123456789012",
                evidence_id="new_index_0",
                stance=EvidenceStance.SUPPORTS,
                reasoning="Test evidence",
                stance_confidence=0.8,
            )
        )
        base_case.hypotheses = {"hyp_123456789012": hyp}

        issues = validator._validate_evidence_links(base_case)

        # Should not report error for new_index references
        assert not any("new_index" in i.message for i in issues)


class TestLikelihoodBounds:
    """Test likelihood value bounds validation."""

    def test_hypothesis_likelihood_out_of_bounds(self, validator, base_case):
        """Likelihood outside [0, 1] should error."""
        # Note: Pydantic validates likelihood bounds at model level (ge=0.0, le=1.0)
        # This test verifies StateValidator also catches bounds violations
        # We create a valid hypothesis and then directly modify the field to bypass validation
        hyp = Hypothesis(
            hypothesis_id="hyp_123456789012",
            statement="Test hypothesis",
            category=HypothesisCategory.CODE,
            state=HypothesisState.ACTIVE,
            generated_at_turn=1,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Test hypothesis",
            likelihood=0.5,
        )
        # Bypass Pydantic validation by using object.__setattr__
        object.__setattr__(hyp, "likelihood", 1.5)
        base_case.hypotheses = {"hyp_123456789012": hyp}

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
                state=HypothesisState.ACTIVE,
                generated_at_turn=1,
                generation_mode=HypothesisGenerationMode.SYSTEMATIC,
                rationale="Test hypothesis",
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

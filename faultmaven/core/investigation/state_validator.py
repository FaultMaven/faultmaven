"""State Validator for Milestone-Based Investigation

Validates investigation state consistency ensuring:
- Milestone ordering is respected
- Status matches progress state
- Hypothesis states are valid
- Evidence links are consistent
- Likelihood values are within bounds

Design Reference:
- docs/architecture/investigation-engine/error-handling-and-recovery.md Section 4

Usage:
    validator = StateValidator()
    is_valid, issues = validator.is_valid(case)
    if not is_valid:
        for issue in issues:
            logger.warning(f"State issue: {issue.code} - {issue.message}")
"""

import logging
from dataclasses import asdict, dataclass
from enum import Enum
from typing import List, Optional, Tuple

from faultmaven.core.investigation.causal_graph import is_chain_root_validated
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    HypothesisState,
    InvestigationProgress,
)
from faultmaven.modules.case.domain.models import CauseState

logger = logging.getLogger(__name__)


class ValidationSeverity(str, Enum):
    """Severity of validation issues."""

    WARNING = "warning"  # Non-blocking, log only
    ERROR = "error"  # Blocking, requires correction
    CRITICAL = "critical"  # Severe inconsistency


@dataclass
class ValidationIssue:
    """Single validation issue."""

    code: str
    message: str
    severity: ValidationSeverity
    field: Optional[str] = None
    suggested_fix: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return asdict(self)


class StateValidator:
    """
    Validates investigation state consistency.

    Performs comprehensive validation of case state including:
    - Milestone dependency ordering
    - Status-progress consistency
    - Hypothesis lifecycle states
    - Evidence-hypothesis link validity
    - Likelihood value bounds
    """

    def validate_case(self, case: Case) -> List[ValidationIssue]:
        """
        Run all validations on a case.

        Args:
            case: The case to validate

        Returns:
            List of validation issues found
        """
        issues: List[ValidationIssue] = []
        issues.extend(self._validate_milestone_ordering(case.progress))
        issues.extend(self._validate_state_consistency(case))
        issues.extend(self._validate_hypothesis_states(case))
        issues.extend(self._validate_evidence_links(case))
        issues.extend(self._validate_likelihood_bounds(case))
        return issues

    def _validate_milestone_ordering(
        self, progress: InvestigationProgress
    ) -> List[ValidationIssue]:
        """
        Validate milestone dependencies are respected.

        Gate milestones only go False -> True; ``solution_proposed`` is
        engine-derived from live SOLUTION offers (INV-32) and MAY revert when
        the standing offer leaves liveness — the orderings below still hold
        by construction (the derivation includes the gate ladder).
        Some milestones have logical dependencies:
        - solution_verified requires solution_proposed and solution_accepted
        - solution_accepted requires solution_proposed
        - mitigation_verified requires mitigation_accepted
        - cause_state=IDENTIFIED should have root_cause_likelihood
        """
        issues: List[ValidationIssue] = []

        # solution_verified requires solution_proposed
        if progress.solution_verified and not progress.solution_proposed:
            issues.append(
                ValidationIssue(
                    code="MILESTONE_ORDER_001",
                    message="solution_verified=True but solution_proposed=False",
                    severity=ValidationSeverity.ERROR,
                    field="progress.solution_verified",
                    suggested_fix="Set solution_proposed=True or reset solution_verified=False",
                )
            )

        # solution_accepted requires solution_proposed
        if progress.solution_accepted and not progress.solution_proposed:
            issues.append(
                ValidationIssue(
                    code="MILESTONE_ORDER_002",
                    message="solution_accepted=True but solution_proposed=False",
                    severity=ValidationSeverity.ERROR,
                    field="progress.solution_accepted",
                    suggested_fix="Set solution_proposed=True",
                )
            )

        # solution_verified requires solution_accepted (stage-gate dependency)
        if progress.solution_verified and not progress.solution_accepted:
            issues.append(
                ValidationIssue(
                    code="MILESTONE_ORDER_003",
                    message="solution_verified=True but solution_accepted=False",
                    severity=ValidationSeverity.ERROR,
                    field="progress.solution_verified",
                    suggested_fix="Set solution_accepted=True or reset solution_verified=False",
                )
            )

        # mitigation verified requires accepted (stage-gate dependency).
        # Post-redesign the mitigation gates live on the mitigation record.
        _stab = progress.mitigation
        if _stab is not None and _stab.verified and not _stab.accepted:
            issues.append(
                ValidationIssue(
                    code="MILESTONE_ORDER_004",
                    message="mitigation_verified=True but mitigation_accepted=False",
                    severity=ValidationSeverity.ERROR,
                    field="progress.mitigation",
                    suggested_fix="Set mitigation_accepted=True or reset mitigation_verified=False",
                )
            )

        # cause_state=IDENTIFIED should have likelihood
        if progress.cause_state == CauseState.IDENTIFIED:
            likelihood = getattr(progress, "root_cause_likelihood", None)
            if likelihood is None or likelihood == 0.0:
                issues.append(
                    ValidationIssue(
                        code="MILESTONE_INCOMPLETE_001",
                        message="cause_state=IDENTIFIED but root_cause_likelihood is not set",
                        severity=ValidationSeverity.WARNING,
                        field="progress.root_cause_likelihood",
                        suggested_fix="Set root_cause_likelihood to confidence value",
                    )
                )

        return issues

    def _validate_state_consistency(self, case: Case) -> List[ValidationIssue]:
        """
        Ensure state matches progress state.

        RESOLVED state requires solution_verified milestone.
        INVESTIGATING state should have problem statement.
        """
        issues: List[ValidationIssue] = []

        # RESOLVED requires solution_verified
        if case.state == CaseState.RESOLVED and not case.progress.solution_verified:
            issues.append(
                ValidationIssue(
                    code="STATUS_MISMATCH_001",
                    message="State is RESOLVED but solution_verified=False",
                    severity=ValidationSeverity.ERROR,
                    field="state",
                    suggested_fix="Set solution_verified=True or change state to INVESTIGATING",
                )
            )

        # INVESTIGATING should have problem_statement
        if case.state == CaseState.INVESTIGATING:
            has_statement = (
                case.problem_verification
                and case.problem_verification.symptom_statement
            )
            if not has_statement:
                issues.append(
                    ValidationIssue(
                        code="STATUS_MISMATCH_002",
                        message="State is INVESTIGATING but symptom_statement is empty",
                        severity=ValidationSeverity.WARNING,
                        field="problem_verification.symptom_statement",
                    )
                )

        # Terminal states should have closure metadata
        if case.state in [CaseState.RESOLVED, CaseState.CLOSED]:
            if not case.closed_at:
                issues.append(
                    ValidationIssue(
                        code="STATUS_METADATA_001",
                        message=f"State is {case.state.value} but closed_at is not set",
                        severity=ValidationSeverity.WARNING,
                        field="closed_at",
                    )
                )

        return issues

    def _validate_hypothesis_states(self, case: Case) -> List[ValidationIssue]:
        """
        Validate hypothesis lifecycle states.

        VALIDATED hypotheses must be grounded in a VALIDATED chain root node.
        REFUTED hypotheses should have refuting evidence.
        """
        issues: List[ValidationIssue] = []

        for hyp_id, hypothesis in case.hypotheses.items():
            # VALIDATED is derived from the chain root (project_hypothesis_states_from_roots
            # is the sole writer): a hypothesis reads VALIDATED iff its chain ROOT node is
            # VALIDATED. Assert THAT invariant via the shared is_chain_root_validated —
            # the same predicate the projection and synthesize_rcc use, so the three cannot
            # drift — not the retired flat "≥2 supporting_evidence" bar (node-axis grounding
            # means a graph-validated hypothesis legitimately carries fewer than 2 flat
            # supporting links). A violation here means a writer other than the projection
            # set VALIDATED (the coherence guard for the sole-writer invariant).
            if (
                hypothesis.state == HypothesisState.VALIDATED
                and not is_chain_root_validated(hypothesis, case.causal_nodes)
            ):
                issues.append(
                    ValidationIssue(
                        code="HYPOTHESIS_STATE_001",
                        message=f"Hypothesis {hyp_id} is VALIDATED but its chain root node is not VALIDATED",
                        severity=ValidationSeverity.WARNING,
                        field=f"hypotheses.{hyp_id}",
                        suggested_fix="VALIDATED is derived from the chain root node; only project_hypothesis_states_from_roots may set it",
                    )
                )

            # REFUTED should have refuting evidence
            if hypothesis.state == HypothesisState.REFUTED:
                refuting_count = len(hypothesis.refuting_evidence)
                if refuting_count < 1:
                    issues.append(
                        ValidationIssue(
                            code="HYPOTHESIS_STATE_002",
                            message=f"Hypothesis {hyp_id} is REFUTED with no refuting evidence",
                            severity=ValidationSeverity.WARNING,
                            field=f"hypotheses.{hyp_id}",
                            suggested_fix="Add refuting evidence or change state",
                        )
                    )

            # ACTIVE hypotheses with very low likelihood should be warned
            if (
                hypothesis.state == HypothesisState.ACTIVE
                and hypothesis.likelihood < 0.2
            ):
                issues.append(
                    ValidationIssue(
                        code="HYPOTHESIS_STATE_003",
                        message=f"Hypothesis {hyp_id} is ACTIVE with very low likelihood ({hypothesis.likelihood:.2f})",
                        severity=ValidationSeverity.WARNING,
                        field=f"hypotheses.{hyp_id}.likelihood",
                        suggested_fix="Consider retiring hypotheses with likelihood < 0.2",
                    )
                )

        return issues

    def _validate_evidence_links(self, case: Case) -> List[ValidationIssue]:
        """
        Validate evidence-hypothesis links are consistent.

        Evidence IDs referenced in hypotheses should exist.
        """
        issues: List[ValidationIssue] = []
        evidence_ids = {e.evidence_id for e in case.evidence}

        for hyp_id, hypothesis in case.hypotheses.items():
            # Check supporting evidence
            for ev_id in hypothesis.supporting_evidence:
                # Skip new_index references (created same turn)
                if ev_id.startswith("new_index_"):
                    continue
                if ev_id not in evidence_ids:
                    issues.append(
                        ValidationIssue(
                            code="LINK_DANGLING_001",
                            message=f"Hypothesis {hyp_id} references non-existent supporting evidence {ev_id}",
                            severity=ValidationSeverity.WARNING,
                            field=f"hypotheses.{hyp_id}.supporting_evidence",
                        )
                    )

            # Check refuting evidence
            for ev_id in hypothesis.refuting_evidence:
                if ev_id.startswith("new_index_"):
                    continue
                if ev_id not in evidence_ids:
                    issues.append(
                        ValidationIssue(
                            code="LINK_DANGLING_002",
                            message=f"Hypothesis {hyp_id} references non-existent refuting evidence {ev_id}",
                            severity=ValidationSeverity.WARNING,
                            field=f"hypotheses.{hyp_id}.refuting_evidence",
                        )
                    )

            # Check evidence_links if present
            if hasattr(hypothesis, "evidence_links") and hypothesis.evidence_links:
                for link in hypothesis.evidence_links:
                    ev_ref = link.evidence_id
                    if ev_ref.startswith("new_index_"):
                        continue
                    if ev_ref not in evidence_ids:
                        issues.append(
                            ValidationIssue(
                                code="LINK_DANGLING_003",
                                message=f"Hypothesis {hyp_id} links to non-existent evidence {ev_ref}",
                                severity=ValidationSeverity.WARNING,
                                field=f"hypotheses.{hyp_id}.evidence_links",
                            )
                        )

        return issues

    def _validate_likelihood_bounds(self, case: Case) -> List[ValidationIssue]:
        """
        Validate all likelihood/confidence values are in [0, 1].
        """
        issues: List[ValidationIssue] = []

        # Check hypothesis likelihoods
        for hyp_id, hypothesis in case.hypotheses.items():
            if hypothesis.likelihood < 0.0 or hypothesis.likelihood > 1.0:
                issues.append(
                    ValidationIssue(
                        code="BOUNDS_001",
                        message=f"Hypothesis {hyp_id} likelihood {hypothesis.likelihood} outside [0,1]",
                        severity=ValidationSeverity.ERROR,
                        field=f"hypotheses.{hyp_id}.likelihood",
                    )
                )

        # Check root_cause_likelihood
        if case.progress.root_cause_likelihood is not None:
            if (
                case.progress.root_cause_likelihood < 0.0
                or case.progress.root_cause_likelihood > 1.0
            ):
                issues.append(
                    ValidationIssue(
                        code="BOUNDS_002",
                        message=f"root_cause_likelihood {case.progress.root_cause_likelihood} outside [0,1]",
                        severity=ValidationSeverity.ERROR,
                        field="progress.root_cause_likelihood",
                    )
                )

        # Check working_conclusion likelihood if present
        if case.working_conclusion:
            likelihood = getattr(case.working_conclusion, "likelihood", None)
            if likelihood is not None and (likelihood < 0.0 or likelihood > 1.0):
                issues.append(
                    ValidationIssue(
                        code="BOUNDS_003",
                        message=f"working_conclusion likelihood {likelihood} outside [0,1]",
                        severity=ValidationSeverity.ERROR,
                        field="working_conclusion.likelihood",
                    )
                )

        return issues

    def is_valid(self, case: Case) -> Tuple[bool, List[ValidationIssue]]:
        """
        Check if case state is valid (no ERROR or CRITICAL issues).

        Args:
            case: The case to validate

        Returns:
            Tuple of (is_valid, list_of_issues)
            - is_valid is True if no ERROR or CRITICAL issues
            - issues contains all found issues regardless of severity
        """
        issues = self.validate_case(case)
        blocking = [
            i
            for i in issues
            if i.severity in (ValidationSeverity.ERROR, ValidationSeverity.CRITICAL)
        ]
        return len(blocking) == 0, issues

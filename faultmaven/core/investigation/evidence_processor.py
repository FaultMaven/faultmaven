"""Evidence Processing and Milestone Advancement

Implements opportunistic milestone advancement based on evidence category.

Reference: investigation-lifecycle-logic.md Section 3.1
"""

import logging
from typing import List

from faultmaven.modules.case.contracts import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceStance,
    ProblemVerification,
    Solution,
)

logger = logging.getLogger(__name__)


async def process_evidence(case: Case, evidence: Evidence) -> List[str]:
    """
    Process evidence and advance milestones opportunistically.

    Returns: List of milestone names that transitioned False → True

    Called: After LLM analyzes evidence and creates Evidence object

    Reference: investigation-lifecycle-logic.md lines 738-842
    """

    milestones_advanced = []

    # SYMPTOM_EVIDENCE advances verification milestones
    if evidence.category == EvidenceCategory.SYMPTOM_EVIDENCE:

        # Check each verification milestone
        if not case.progress.symptom_verified:
            if validates_symptom(evidence, case.problem_verification):
                case.progress.symptom_verified = True
                milestones_advanced.append("symptom_verified")
                logger.info(
                    f"Milestone advanced: symptom_verified (evidence: {evidence.evidence_id})"
                )

        if not case.progress.scope_assessed:
            if reveals_scope(evidence, case.problem_verification):
                case.progress.scope_assessed = True
                milestones_advanced.append("scope_assessed")
                logger.info(
                    f"Milestone advanced: scope_assessed (evidence: {evidence.evidence_id})"
                )

        if not case.progress.timeline_established:
            if shows_timeline(evidence, case.problem_verification):
                case.progress.timeline_established = True
                milestones_advanced.append("timeline_established")
                logger.info(
                    f"Milestone advanced: timeline_established (evidence: {evidence.evidence_id})"
                )

        if not case.progress.changes_identified:
            if identifies_changes(evidence, case.problem_verification):
                case.progress.changes_identified = True
                milestones_advanced.append("changes_identified")
                logger.info(
                    f"Milestone advanced: changes_identified (evidence: {evidence.evidence_id})"
                )

    # CAUSAL_EVIDENCE advances root cause identification
    elif evidence.category == EvidenceCategory.CAUSAL_EVIDENCE:

        if not case.progress.root_cause_identified:
            # Check if evidence strongly supports a hypothesis
            if evidence.tests_hypothesis_id:
                hypothesis = case.hypotheses.get(evidence.tests_hypothesis_id)
                if hypothesis and evidence.stance == EvidenceStance.SUPPORTS:
                    if evidence.stance_confidence >= 0.8:
                        # Strong evidence → advance root cause
                        case.progress.root_cause_identified = True
                        case.progress.root_cause_likelihood = evidence.stance_confidence
                        milestones_advanced.append("root_cause_identified")
                        logger.info(
                            f"Milestone advanced: root_cause_identified "
                            f"(hypothesis: {evidence.tests_hypothesis_id}, "
                            f"confidence: {evidence.stance_confidence})"
                        )

    # RESOLUTION_EVIDENCE advances solution verification
    elif evidence.category == EvidenceCategory.RESOLUTION_EVIDENCE:

        if not case.progress.solution_verified:
            if verifies_solution_effectiveness(evidence, case.solutions):
                case.progress.solution_verified = True
                milestones_advanced.append("solution_verified")
                logger.info(
                    f"Milestone advanced: solution_verified (evidence: {evidence.evidence_id})"
                )

    # Update evidence advances_milestones field
    evidence.advances_milestones = milestones_advanced

    # Trigger side effects (path selection, terminal transitions)
    if "symptom_verified" in milestones_advanced:
        if not case.path_selection:
            logger.info("symptom_verified completed → triggering path selection")
            # Path selection happens in milestone_engine after this returns

    if "solution_verified" in milestones_advanced:
        logger.info(
            "solution_verified completed → will trigger terminal transition check"
        )
        # Terminal transition check happens in milestone_engine after this returns

    return milestones_advanced


# ============================================================
# VALIDATION HELPERS (Implementation-specific)
# ============================================================


def validates_symptom(evidence: Evidence, verification: ProblemVerification) -> bool:
    """
    Check if evidence confirms the symptom.

    Implementation: Check if evidence.analysis mentions symptom indicators
    Example: "Error rate confirms timeout symptom"
    """
    if not verification or not verification.symptom_statement:
        return False

    # Check evidence analysis for symptom confirmation keywords
    analysis_lower = evidence.analysis.lower() if evidence.analysis else ""
    symptom_lower = (
        verification.symptom_statement.lower() if verification.symptom_statement else ""
    )

    # Evidence explicitly validates symptom if it:
    # 1. Contains symptom-related keywords
    # 2. Provides data confirming the problem exists
    # 3. References the symptom in its analysis

    validation_keywords = [
        "confirms",
        "validates",
        "shows",
        "demonstrates",
        "proves",
        "indicates",
        "error",
        "timeout",
        "failure",
        "unavailable",
    ]

    # Check if analysis mentions validation keywords
    has_validation = any(keyword in analysis_lower for keyword in validation_keywords)

    # Check if analysis references the symptom
    symptom_keywords = symptom_lower.split()[:3]  # First 3 words of symptom
    references_symptom = any(
        keyword in analysis_lower for keyword in symptom_keywords if len(keyword) > 3
    )

    return has_validation and references_symptom


def reveals_scope(evidence: Evidence, verification: ProblemVerification) -> bool:
    """
    Check if evidence determines affected scope.

    Implementation: Check if evidence identifies services, users, regions
    """
    if not evidence.analysis:
        return False

    analysis_lower = evidence.analysis.lower()

    # Scope indicators
    scope_keywords = [
        "users affected",
        "services impacted",
        "region",
        "% of requests",
        "% of users",
        "all users",
        "specific",
        "production",
        "staging",
        "eu-west",
        "us-east",
    ]

    return any(keyword in analysis_lower for keyword in scope_keywords)


def shows_timeline(evidence: Evidence, verification: ProblemVerification) -> bool:
    """
    Check if evidence establishes timeline.

    Implementation: Check if evidence has timestamps or duration data
    """
    if not evidence.analysis:
        return False

    analysis_lower = evidence.analysis.lower()

    # Timeline indicators
    timeline_keywords = [
        "started at",
        "began at",
        "since",
        "minutes ago",
        "hours ago",
        "at ",  # Timestamp references
        "utc",
        "duration",
        "for the past",
    ]

    return any(keyword in analysis_lower for keyword in timeline_keywords)


def identifies_changes(evidence: Evidence, verification: ProblemVerification) -> bool:
    """
    Check if evidence reveals recent changes.

    Implementation: Check if evidence links to deployments, configs, etc.
    """
    if not evidence.analysis:
        return False

    analysis_lower = evidence.analysis.lower()

    # Change indicators
    change_keywords = [
        "deployment",
        "deployed",
        "release",
        "config change",
        "configuration",
        "update",
        "modified",
        "commit",
        "version",
        "rollout",
    ]

    return any(keyword in analysis_lower for keyword in change_keywords)


def verifies_solution_effectiveness(
    evidence: Evidence, solutions: List[Solution]
) -> bool:
    """
    Check if evidence confirms solution worked.

    Implementation: Check if metrics show problem resolved
    """
    if not evidence.analysis:
        return False

    analysis_lower = evidence.analysis.lower()

    # Solution verification indicators
    verification_keywords = [
        "fixed",
        "resolved",
        "working",
        "back to normal",
        "error rate dropped",
        "errors stopped",
        "success rate",
        "0% error",
        "no errors",
        "stable",
    ]

    return any(keyword in analysis_lower for keyword in verification_keywords)

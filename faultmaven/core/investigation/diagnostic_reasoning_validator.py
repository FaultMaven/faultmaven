"""Diagnostic Reasoning Validator

Validates that agent responses include context-specific diagnostic reasoning
before making suggestions, as required by Section 3.3 of the specification.

Reference: investigation-lifecycle-logic.md Section 3.3 (lines 854-996)
"""

import logging
import re
from typing import List, Tuple

from faultmaven.modules.case.contracts import Case, CaseStatus

logger = logging.getLogger(__name__)


class DiagnosticReasoningError(Exception):
    """Raised when agent response lacks required diagnostic reasoning."""

    pass


def validate_diagnostic_reasoning(
    case: Case, agent_response: str, contains_suggestion: bool = None
) -> Tuple[bool, List[str]]:
    """
    Validate that agent response includes diagnostic reasoning when making suggestions.

    REQUIREMENT: Before suggesting any action, mitigation, or hypothesis, the agent
    MUST demonstrate context-specific diagnostic reasoning.

    Args:
        case: Current case state
        agent_response: Agent's response text
        contains_suggestion: Override auto-detection of suggestions (optional)

    Returns:
        (is_valid, violations) tuple

    Reference: investigation-lifecycle-logic.md lines 854-996
    """
    # EXCEPTION: INQUIRY state doesn't require diagnostic reasoning
    if case.status == CaseStatus.INQUIRY:
        return (True, [])

    # Only validate if response contains suggestions/mitigations/hypotheses
    if contains_suggestion is None:
        contains_suggestion = _detect_suggestions(agent_response)

    if not contains_suggestion:
        # No suggestions → no reasoning required
        return (True, [])

    violations = []

    # Check that response references case-specific data and includes causal
    # reasoning. We do NOT check for specific section headers
    # (OBSERVATION/ANALYSIS) — the prompt guides conversational style.
    has_specific_evidence = _references_specific_evidence(agent_response, case)
    has_causal_reasoning = _has_causal_reasoning(agent_response)

    # Check for prohibited patterns
    has_checklist = _is_checklist_engineering(agent_response)
    has_generic_advice = _is_generic_best_practices(agent_response)
    lacks_specificity = _lacks_case_specificity(agent_response, case)

    if not has_specific_evidence:
        violations.append(
            "Not grounded in case evidence - must reference specific metrics, timestamps, error messages, or data from THIS case"
        )

    if not has_causal_reasoning:
        violations.append(
            "Missing causal reasoning - must explain HOW X causes Y, not just list possibilities"
        )

    if has_checklist:
        violations.append(
            "PROHIBITED: Checklist engineering detected - 'Try these 10 things' is not diagnostic reasoning"
        )

    if has_generic_advice:
        violations.append(
            "PROHIBITED: Generic best practices detected - advice must be specific to THIS case's evidence"
        )

    if lacks_specificity:
        violations.append(
            "Lacks case specificity - could apply to any similar issue without using case-specific data"
        )

    is_valid = len(violations) == 0

    if not is_valid:
        logger.warning(
            f"Diagnostic reasoning validation failed for case {case.case_id}. "
            f"Violations: {violations}"
        )

    return (is_valid, violations)


# ============================================================
# Detection Helpers
# ============================================================


def _detect_suggestions(response: str) -> bool:
    """Detect if response contains suggestions/mitigations/hypotheses."""
    response_lower = response.lower()

    suggestion_indicators = [
        "you should",
        "i recommend",
        "try",
        "suggest",
        "hypothesis",
        "mitigation",
        "solution",
        "could you",
        "can you check",
        "you might want to",
        "consider",
    ]

    return any(indicator in response_lower for indicator in suggestion_indicators)


def _references_specific_evidence(response: str, case: Case) -> bool:
    """Check if response references specific case evidence."""
    response_lower = response.lower()

    # Check for timestamps
    has_timestamps = (
        bool(re.search(r"\d{1,2}:\d{2}", response))
        or bool(re.search(r"\d{4}-\d{2}-\d{2}", response))
        or "utc" in response_lower
        or "minutes ago" in response_lower
        or "hours ago" in response_lower
    )

    # Check for metrics/percentages
    has_metrics = (
        bool(re.search(r"\d+%", response))
        or bool(re.search(r"\d+\.\d+", response))
        or "error rate" in response_lower
        or "latency" in response_lower
    )

    # Check for specific IDs (deployment, commit, etc.)
    has_ids = (
        bool(re.search(r"[a-f0-9]{7,40}", response))
        or "deployment" in response_lower
        or "version" in response_lower
    )

    # Check for error messages or log excerpts
    has_error_details = (
        "error:" in response_lower
        or "exception:" in response_lower
        or "timeout" in response_lower
        or "failed" in response_lower
    )

    # At least 2 of these should be present for specificity
    specificity_count = sum([has_timestamps, has_metrics, has_ids, has_error_details])

    return specificity_count >= 2


def _has_causal_reasoning(response: str) -> bool:
    """Check for causal reasoning (HOW X causes Y)."""
    response_lower = response.lower()

    causal_indicators = [
        "causes",
        "leads to",
        "results in",
        "because",
        "since",
        "therefore",
        "this explains why",
        "this is why",
        "the reason is",
    ]

    return any(indicator in response_lower for indicator in causal_indicators)


def _is_checklist_engineering(response: str) -> bool:
    """Detect prohibited checklist pattern."""
    response_lower = response.lower()

    # Detect "try these N things" pattern
    checklist_patterns = [
        r"try these \d+ things",
        r"here are \d+ things",
        r"try the following:",
        r"\d+\.\s.*\n\s*\d+\.\s.*\n\s*\d+\.",  # Numbered list with 3+ items
    ]

    for pattern in checklist_patterns:
        if re.search(pattern, response_lower):
            return True

    # Check for long bullet lists without reasoning
    bullet_count = response_lower.count("\n-") + response_lower.count("\n*")
    return bullet_count >= 5  # 5+ bullets suggests checklist


def _is_generic_best_practices(response: str) -> bool:
    """Detect generic best practices advice."""
    response_lower = response.lower()

    generic_phrases = [
        "implement monitoring",
        "add logging",
        "set up alerting",
        "follow best practices",
        "best practice is",
        "you should always",
        "it's recommended to",
    ]

    return any(phrase in response_lower for phrase in generic_phrases)


def _lacks_case_specificity(response: str, case: Case) -> bool:
    """Check if advice lacks case-specific details."""
    response_lower = response.lower()

    # If response doesn't mention case ID, problem domain, or specific evidence
    # it's likely generic

    problem_domain_mentioned = False
    if case.problem_verification and case.problem_verification.symptom_statement:
        symptom_keywords = case.problem_verification.symptom_statement.lower().split()[
            :5
        ]
        problem_domain_mentioned = any(
            keyword in response_lower
            for keyword in symptom_keywords
            if len(keyword) > 4  # Skip short words
        )

    case_id_mentioned = case.case_id in response if case.case_id else False

    # Response should mention problem domain or have high specificity
    return (
        not problem_domain_mentioned and not case_id_mentioned and len(response) < 300
    )

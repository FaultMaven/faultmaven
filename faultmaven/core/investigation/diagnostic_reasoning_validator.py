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

    # EXCEPTION: non-diagnostic responses (confirmations, clarifications,
    # acknowledgments) don't carry diagnostic weight. Graduated enforcement
    # per agent-behavioral-rules.md Rule 2 — the validator tolerates absence
    # of diagnostic-reasoning structure on these turns. See
    # _is_non_diagnostic_response for the classification heuristic.
    if _is_non_diagnostic_response(agent_response):
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

    # Rule 8 (Full-Context Reasoning) — counter to recency bias. Only fires
    # when the case has substantial prior context AND the response is long
    # enough to plausibly reference it.
    lacks_prior_context = _has_sufficient_prior_context(
        case
    ) and not _references_prior_context(agent_response, case, min_length=300)

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

    if lacks_prior_context:
        violations.append(
            "Response does not reference prior case context (earlier evidence, "
            "refuted/retired hypotheses, or journal entries) despite substantial "
            "case history - may be exhibiting recency bias (Rule 8: Full-Context Reasoning)"
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


# Non-diagnostic response classification
# ==================================================================
# Markers that open confirmations, clarifications, and acknowledgments.
# Used by _is_non_diagnostic_response as a conservative bypass signal.
_NON_DIAGNOSTIC_OPENERS: Tuple[str, ...] = (
    # Confirmations — short affirmations of user statements
    "yes,",
    "yes.",
    "yes —",
    "yes -",
    "correct",
    "that's right",
    "that is right",
    "exactly",
    "indeed",
    "agreed",
    "confirmed",
    "confirming",
    # Clarifications of prior analysis
    "to clarify",
    "to be clear",
    "let me clarify",
    "what i meant",
    "clarification:",
    # Acknowledgments of user input
    "thanks for",
    "thank you for",
    "got it",
    "understood",
    "noted",
    "good question",
    "i see",
    "i understand",
)

# Upper bound on length for non-diagnostic classification. A response that
# opens with a confirmation/clarification/acknowledgment marker but runs long
# may be packing diagnostic content behind the opener — run normal validation.
_NON_DIAGNOSTIC_MAX_CHARS: int = 500


def _is_non_diagnostic_response(response: str) -> bool:
    """Detect responses that don't carry diagnostic weight.

    Graduated validator enforcement (per agent-behavioral-rules.md Rule 2):
    the validator should not trigger self-correction on responses that are
    confirmations, clarifications of previous analysis, or acknowledgments
    of user input — even when those responses contain words the
    suggestion detector would flag.

    Conservative heuristic: classification requires BOTH
    (a) opens with a known confirmation/clarification/acknowledgment marker, AND
    (b) is short enough (< 500 chars) to be non-substantive.

    Longer responses that happen to start with the same words may be
    making diagnostic claims behind the opener and go through normal
    validation.
    """
    stripped = response.strip()
    if len(stripped) >= _NON_DIAGNOSTIC_MAX_CHARS:
        return False
    lower = stripped.lower()
    return any(lower.startswith(o) for o in _NON_DIAGNOSTIC_OPENERS)


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
    """Check for causal reasoning (HOW X causes Y).

    Substring-match heuristic against an enumerated keyword list. False
    positives are possible (e.g. "the root cause is unknown" trivially
    matches "the root cause is" without doing causal reasoning) — see
    the false-acceptance regression tests in
    ``test_diagnostic_reasoning_validator.py`` which pin the trade-offs
    for each idiom currently in the list.

    The list grows conservatively: each addition must be a phrase that
    is unambiguously causal in the vast majority of contexts. Idioms
    that frequently collocate with negation/uncertainty (e.g. a bare
    "the root cause is" → "is unknown") stay out to keep false-accept
    risk bounded.
    """
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
        # Additions (2026-05-23): widen coverage of semantically-causal
        # idioms the original list missed. Each is conservative — picked
        # specifically so a naive substring match does not trivially
        # accept negated forms.
        "due to",
        "caused by",
        "the root cause of",  # NOT "the root cause is" — too easy to
        # accept "the root cause is unknown" as causal.
        "explains the",
        "resulting from",
    ]

    return any(indicator in response_lower for indicator in causal_indicators)


def _is_checklist_engineering(response: str) -> bool:
    """Detect prohibited checklist pattern.

    Numbered-list and bullet-list thresholds are deliberately aligned
    (both 5+ items) — the previous asymmetry (numbered=3, bullet=5) had
    no principled justification and over-fired on legitimate diagnostic
    enumerations like "1. Pod A OOMKilled. 2. Pod B OOMKilled.
    3. Pod C OOMKilled." Action-shaped checklists ("try these N things")
    are still caught by the explicit-phrase patterns regardless of
    list length.
    """
    response_lower = response.lower()

    # Detect "try these N things" pattern. These explicit phrases catch
    # checklist intent independent of how many items follow.
    checklist_patterns = [
        r"try these \d+ things",
        r"here are \d+ things",
        r"try the following:",
        r"\d+\.\s.*\n\s*\d+\.\s.*\n\s*\d+\.\s.*\n\s*\d+\.\s.*\n\s*\d+\.",  # 5+ numbered items
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


# ============================================================
# Rule 8 — Full-Context Reasoning helpers
# ============================================================

# Thresholds controlling when the Rule 8 prior-context check fires.
# All are conservative to minimize false positives; tune via unit tests.
_RULE8_MIN_EVIDENCE_ITEMS: int = 3
_RULE8_MIN_REFUTED_HYPOTHESES: int = 2
_RULE8_MIN_JOURNAL_ENTRIES: int = 2
_RULE8_MIN_RESPONSE_CHARS: int = 300
_RULE8_HYPOTHESIS_KEYWORD_MATCH: int = 2  # of keywords from the statement


def _has_sufficient_prior_context(case: Case) -> bool:
    """Return True when the case has enough prior investigation state for
    the Rule 8 check to be meaningful. Fresh cases with little prior
    content have nothing to reference — firing the check there would be
    pure noise.

    The thresholds are disjunctive: any one of them being met is enough.
    """
    evidence_count = len(getattr(case, "evidence", []) or [])
    if evidence_count >= _RULE8_MIN_EVIDENCE_ITEMS:
        return True

    hypotheses = getattr(case, "hypotheses", {}) or {}
    refuted = 0
    for h in hypotheses.values():
        status = getattr(h, "status", None)
        status_value = getattr(status, "value", str(status) if status else "")
        if str(status_value).lower() in ("refuted", "retired"):
            refuted += 1
    if refuted >= _RULE8_MIN_REFUTED_HYPOTHESES:
        return True

    journal_entries = len(getattr(case, "investigation_journal", []) or [])
    if journal_entries >= _RULE8_MIN_JOURNAL_ENTRIES:
        return True

    return False


def _references_prior_context(
    response: str,
    case: Case,
    min_length: int = _RULE8_MIN_RESPONSE_CHARS,
) -> bool:
    """Return True when the response references prior case context.

    Looks for any of:

    - Evidence collected in a turn earlier than the current turn, mentioned
      by filename or data-type label.
    - A refuted or retired hypothesis whose statement keywords appear in
      the response (at least ``_RULE8_HYPOTHESIS_KEYWORD_MATCH`` of the
      length-> 4 keywords).
    - An investigation journal entry whose summary keywords appear.

    Responses shorter than ``min_length`` are treated as "no reference
    possible" and return True (short responses are exempt — they cannot
    reasonably be expected to weave in prior context).
    """
    if len(response) < min_length:
        return True  # short response — exempt from the check

    response_lower = response.lower()

    # Prior evidence — by label (filename via FK traversal / source_type).
    # Post-redesign: original_filename and data_type were dropped from
    # Evidence; the filename now lives on uploaded_files (via source_file_id)
    # and the type label is on Evidence.source_type. Use the same aggregate-
    # traversal pattern as the rest of the case-domain consumer code.
    current_turn = getattr(case, "current_turn", 0) or 0
    evidence = getattr(case, "evidence", []) or []
    for ev in evidence:
        collected_at_turn = getattr(ev, "collected_at_turn", None)
        if collected_at_turn is None or collected_at_turn >= current_turn:
            continue  # not prior — skip
        labels: list[str] = []
        file_meta = case.find_uploaded_file(getattr(ev, "source_file_id", None))
        if file_meta is not None and file_meta.filename:
            labels.append(str(file_meta.filename))
        source_type = getattr(ev, "source_type", None)
        if source_type is not None:
            labels.append(str(getattr(source_type, "value", source_type)))
        for label in labels:
            if label and len(label) > 3 and label.lower() in response_lower:
                return True

    # Refuted / retired hypotheses — by statement keywords
    hypotheses = getattr(case, "hypotheses", {}) or {}
    for h in hypotheses.values():
        status = getattr(h, "status", None)
        status_value = getattr(status, "value", str(status) if status else "")
        if str(status_value).lower() not in ("refuted", "retired"):
            continue
        statement = getattr(h, "statement", "") or ""
        keywords = [w for w in statement.lower().split() if len(w) > 4]
        matches = sum(1 for k in keywords if k in response_lower)
        if matches >= _RULE8_HYPOTHESIS_KEYWORD_MATCH:
            return True

    # Investigation journal — by entry summary keywords
    journal = getattr(case, "investigation_journal", []) or []
    for entry in journal:
        summary = (
            getattr(entry, "summary", None) or getattr(entry, "content", None) or ""
        )
        keywords = [w for w in str(summary).lower().split() if len(w) > 4][:8]
        matches = sum(1 for k in keywords if k in response_lower)
        if matches >= _RULE8_HYPOTHESIS_KEYWORD_MATCH:
            return True

    return False

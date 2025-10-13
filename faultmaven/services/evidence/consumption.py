"""Evidence Consumption Utilities

Shared logic for phase handlers to consume evidence provided by users.
Enables OODA investigation framework to incorporate user-provided findings
into investigation state and decision-making.

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
- docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import logging
from typing import List, Optional

from faultmaven.models.evidence import EvidenceProvided, EvidenceRequest, EvidenceStatus
from faultmaven.models.investigation import InvestigationState

logger = logging.getLogger(__name__)


def get_new_evidence_since_turn(
    investigation_state: InvestigationState,
    since_turn: int
) -> List[EvidenceProvided]:
    """Get evidence provided since specified turn.

    Filters the investigation state's evidence_provided list to return only
    evidence submitted after a specific turn number. This enables handlers
    to process only new evidence since their last execution.

    Args:
        investigation_state: Current investigation state with evidence tracking
        since_turn: Turn number threshold (exclusive - returns evidence with turn_number > since_turn)

    Returns:
        List of EvidenceProvided objects with turn_number > since_turn, ordered by turn

    Example:
        >>> last_turn = investigation_state.ooda_engine.iterations[-1].turn_number
        >>> new_evidence = get_new_evidence_since_turn(investigation_state, last_turn)
        >>> for evidence in new_evidence:
        ...     # Process new evidence
    """
    if not investigation_state or not hasattr(investigation_state, 'evidence'):
        logger.warning("Investigation state missing evidence layer")
        return []

    # Note: evidence.evidence_provided contains IDs, need to get actual objects
    # For now, assuming we'll get actual objects from diagnostic state
    # This will be integrated properly with the state management
    new_evidence = []

    # Access evidence from investigation state
    # The evidence layer stores IDs, actual objects are in diagnostic state
    # For phase handlers, they'll need to access the full evidence objects
    logger.debug(f"Filtering evidence provided after turn {since_turn}")

    # TODO: Integrate with actual evidence storage mechanism
    # For now, return empty list as evidence objects need to be passed separately
    return new_evidence


def get_new_evidence_since_turn_from_diagnostic(
    evidence_provided: List[EvidenceProvided],
    since_turn: int
) -> List[EvidenceProvided]:
    """Get evidence provided since specified turn from diagnostic state.

    Helper function that works directly with evidence lists from CaseDiagnosticState
    since InvestigationState only stores evidence IDs.

    Args:
        evidence_provided: List of all evidence provided (from CaseDiagnosticState)
        since_turn: Turn number threshold (exclusive)

    Returns:
        List of EvidenceProvided objects with turn_number > since_turn
    """
    if not evidence_provided:
        return []

    new_evidence = [
        evidence for evidence in evidence_provided
        if evidence.turn_number > since_turn
    ]

    logger.debug(
        f"Found {len(new_evidence)} new evidence items since turn {since_turn} "
        f"(total evidence: {len(evidence_provided)})"
    )

    return new_evidence


def get_evidence_for_requests(
    evidence_provided: List[EvidenceProvided],
    request_ids: List[str]
) -> List[EvidenceProvided]:
    """Get all evidence that addresses specified request IDs.

    Searches through provided evidence to find items that match any of the
    specified evidence request IDs in their addresses_requests field.

    Args:
        evidence_provided: List of all evidence provided
        request_ids: List of evidence request IDs to match against

    Returns:
        List of EvidenceProvided objects addressing any of the request_ids

    Example:
        >>> hypothesis_requests = ["req-001", "req-002"]
        >>> relevant_evidence = get_evidence_for_requests(
        ...     diagnostic_state.evidence_provided,
        ...     hypothesis_requests
        ... )
    """
    if not evidence_provided or not request_ids:
        return []

    matching_evidence = []

    for evidence in evidence_provided:
        # Check if any request_id matches
        if any(req_id in evidence.addresses_requests for req_id in request_ids):
            matching_evidence.append(evidence)

    logger.debug(
        f"Found {len(matching_evidence)} evidence items addressing {len(request_ids)} requests"
    )

    return matching_evidence


def check_requests_complete(
    evidence_requests: List[EvidenceRequest],
    request_ids: List[str],
    completeness_threshold: float = 0.8
) -> bool:
    """Check if specified evidence requests are complete.

    Verifies that all specified requests either:
    1. Have status == COMPLETE, OR
    2. Have completeness score >= completeness_threshold

    Args:
        evidence_requests: List of all evidence requests
        request_ids: List of specific request IDs to check
        completeness_threshold: Minimum completeness score (default 0.8)

    Returns:
        True if ALL specified requests meet completion criteria, False otherwise

    Example:
        >>> validation_requests = hypothesis.validation_request_ids
        >>> if check_requests_complete(
        ...     diagnostic_state.evidence_requests,
        ...     validation_requests,
        ...     completeness_threshold=0.7
        ... ):
        ...     # All validation evidence collected
    """
    if not request_ids:
        logger.debug("No request IDs provided, returning True")
        return True

    if not evidence_requests:
        logger.debug("No evidence requests available, returning False")
        return False

    # Create lookup map for efficiency
    request_map = {req.request_id: req for req in evidence_requests}

    complete_count = 0

    for req_id in request_ids:
        request = request_map.get(req_id)

        if not request:
            logger.warning(f"Request ID {req_id} not found in evidence requests")
            continue

        # Check completion criteria
        is_complete = (
            request.status == EvidenceStatus.COMPLETE or
            request.completeness >= completeness_threshold
        )

        if is_complete:
            complete_count += 1
            logger.debug(
                f"Request {req_id} complete: status={request.status.value}, "
                f"completeness={request.completeness:.2f}"
            )
        else:
            logger.debug(
                f"Request {req_id} incomplete: status={request.status.value}, "
                f"completeness={request.completeness:.2f} (threshold={completeness_threshold})"
            )

    all_complete = complete_count == len(request_ids)

    logger.info(
        f"Evidence completeness check: {complete_count}/{len(request_ids)} requests complete "
        f"(threshold={completeness_threshold})"
    )

    return all_complete


def summarize_evidence_findings(
    evidence_list: List[EvidenceProvided]
) -> str:
    """Generate summary of evidence findings for LLM context.

    Creates a concise, formatted summary of evidence that can be injected
    into LLM prompts for context-aware decision making.

    Args:
        evidence_list: List of evidence to summarize

    Returns:
        Multi-line string summary formatted for LLM consumption
        Empty string if no evidence provided

    Example:
        >>> new_evidence = get_new_evidence_since_turn(state, last_turn)
        >>> if new_evidence:
        ...     summary = summarize_evidence_findings(new_evidence)
        ...     prompt += f"\\n\\n## New Evidence:\\n{summary}"
    """
    if not evidence_list:
        return ""

    summaries = []

    for evidence in evidence_list:
        # Determine source type
        source = "User input" if evidence.form.value == "user_input" else "Document upload"

        # Truncate content for summary
        content_preview = evidence.content[:200]
        if len(evidence.content) > 200:
            content_preview += "..."

        # Format evidence type
        ev_type = evidence.evidence_type.value.capitalize()

        # Build summary line
        summary_line = f"- [{source}]: {content_preview} (Type: {ev_type})"

        # Add key findings if available
        if evidence.key_findings:
            findings_str = "; ".join(evidence.key_findings[:3])  # Max 3 findings
            summary_line += f"\n  Findings: {findings_str}"

        summaries.append(summary_line)

    result = "\n".join(summaries)

    logger.debug(f"Generated evidence summary for {len(evidence_list)} items ({len(result)} chars)")

    return result


def calculate_evidence_coverage(
    evidence_requests: List[EvidenceRequest],
    evidence_provided: List[EvidenceProvided]
) -> float:
    """Calculate overall evidence collection coverage score.

    Computes a coverage metric based on how well evidence requests have been
    fulfilled. Used for stall detection and phase advancement decisions.

    Args:
        evidence_requests: List of all evidence requests
        evidence_provided: List of all evidence provided

    Returns:
        Coverage score between 0.0 and 1.0

    Formula:
        - Complete requests (status=COMPLETE or completeness >= 0.8): weight 1.0
        - Partial requests (0.3 <= completeness < 0.8): weight 0.5
        - Pending requests: weight 0.0
        - Blocked requests: excluded from calculation

    Example:
        >>> coverage = calculate_evidence_coverage(
        ...     diagnostic_state.evidence_requests,
        ...     diagnostic_state.evidence_provided
        ... )
        >>> if coverage < 0.5:
        ...     # Insufficient evidence, request more
    """
    if not evidence_requests:
        logger.debug("No evidence requests, coverage = 1.0")
        return 1.0

    # Filter out blocked/obsolete requests
    active_requests = [
        req for req in evidence_requests
        if req.status not in [EvidenceStatus.BLOCKED, EvidenceStatus.OBSOLETE]
    ]

    if not active_requests:
        logger.debug("No active evidence requests, coverage = 1.0")
        return 1.0

    weighted_score = 0.0

    for request in active_requests:
        if request.status == EvidenceStatus.COMPLETE or request.completeness >= 0.8:
            # Fully complete
            weighted_score += 1.0
        elif request.completeness >= 0.3:
            # Partially complete
            weighted_score += 0.5
        # else: pending, weight 0.0

    coverage = weighted_score / len(active_requests)

    logger.info(
        f"Evidence coverage: {coverage:.2%} "
        f"({len(active_requests)} active requests, {len(evidence_provided)} evidence items)"
    )

    return coverage

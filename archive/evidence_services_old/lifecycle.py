"""
Evidence Lifecycle Management

Manages the lifecycle of evidence requests and evidence provided, including:
- Status transitions (PENDING → PARTIAL → COMPLETE / BLOCKED / OBSOLETE)
- Completeness score updates using max() logic (not additive)
- Evidence request deprecation when hypotheses change

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import logging
from typing import List, Optional

from faultmaven.models.evidence import (
    EvidenceRequest,
    EvidenceProvided,
    EvidenceClassification,
    EvidenceStatus,
    UserIntent,
    FileMetadata,
)
from faultmaven.models.case import CaseDiagnosticState

logger = logging.getLogger(__name__)


def update_evidence_lifecycle(
    evidence_provided: EvidenceProvided,
    classification: EvidenceClassification,
    diagnostic_state: CaseDiagnosticState,
    current_turn: int
) -> None:
    """
    Update evidence request status and completeness based on classification.

    CRITICAL: Uses max() logic for completeness, NOT additive accumulation.
    Rationale: Each piece of evidence is independently scored. The highest
    score reflects true completeness, not sum of partial contributions.

    Args:
        evidence_provided: Evidence record to add to state
        classification: Classification result
        diagnostic_state: Current case diagnostic state (modified in-place)
        current_turn: Current conversation turn number

    Side Effects:
        - Adds evidence_provided to diagnostic_state.evidence_provided
        - Updates matched evidence_requests (status, completeness, updated_at_turn)
        - Marks requests as BLOCKED if user reports unavailable
    """
    # Add evidence to state
    diagnostic_state.evidence_provided.append(evidence_provided)

    # Update matched requests
    for req_id in classification.matched_request_ids:
        request = _find_request(diagnostic_state.evidence_requests, req_id)

        if not request:
            logger.warning(f"Classification matched unknown request ID: {req_id}")
            continue

        # Update completeness using MAX logic (not additive)
        old_completeness = request.completeness
        request.completeness = max(request.completeness, classification.completeness_score)

        logger.info(
            f"Updated {req_id} completeness: {old_completeness:.2f} → {request.completeness:.2f} "
            f"(new evidence score: {classification.completeness_score:.2f})"
        )

        # Update status based on completeness
        if request.completeness >= 0.8:
            request.status = EvidenceStatus.COMPLETE
        elif request.completeness >= 0.3:
            request.status = EvidenceStatus.PARTIAL
        # else: remains PENDING

        request.updated_at_turn = current_turn

    # Handle blocking (user reports evidence unavailable)
    if classification.user_intent == UserIntent.REPORTING_UNAVAILABLE:
        for req_id in classification.matched_request_ids:
            request = _find_request(diagnostic_state.evidence_requests, req_id)
            if request:
                request.status = EvidenceStatus.BLOCKED
                request.metadata["blocked_reason"] = evidence_provided.content[:200]
                request.metadata["blocked_at_turn"] = current_turn

                logger.info(f"Marked {req_id} as BLOCKED: user cannot provide")


def mark_obsolete_requests(
    diagnostic_state: CaseDiagnosticState,
    hypothesis_ids_to_deprecate: List[str],
    current_turn: int
) -> int:
    """
    Mark evidence requests as OBSOLETE when hypotheses are refuted/changed.

    Args:
        diagnostic_state: Current case diagnostic state (modified in-place)
        hypothesis_ids_to_deprecate: IDs of hypotheses that were refuted/changed
        current_turn: Current conversation turn number

    Returns:
        Number of requests marked obsolete
    """
    count = 0

    for request in diagnostic_state.evidence_requests:
        # Skip already obsolete/completed requests
        if request.status in [EvidenceStatus.OBSOLETE, EvidenceStatus.COMPLETE]:
            continue

        # Check if request is tied to deprecated hypothesis
        request_hypothesis = request.metadata.get("hypothesis_id")
        if request_hypothesis in hypothesis_ids_to_deprecate:
            request.status = EvidenceStatus.OBSOLETE
            request.updated_at_turn = current_turn
            request.metadata["obsolete_reason"] = "Associated hypothesis was refuted/changed"
            count += 1

            logger.info(f"Marked {request.request_id} as OBSOLETE (hypothesis {request_hypothesis} deprecated)")

    return count


def get_active_evidence_requests(
    diagnostic_state: CaseDiagnosticState
) -> List[EvidenceRequest]:
    """
    Get evidence requests that are actively awaiting user response.

    Returns:
        List of requests with status PENDING or PARTIAL (not COMPLETE, BLOCKED, or OBSOLETE)
    """
    return [
        req for req in diagnostic_state.evidence_requests
        if req.status in [EvidenceStatus.PENDING, EvidenceStatus.PARTIAL]
    ]


def create_evidence_record(
    content: str,
    classification: EvidenceClassification,
    turn_number: int,
    file_metadata: Optional[FileMetadata] = None
) -> EvidenceProvided:
    """
    Create an EvidenceProvided record from classification result.

    Args:
        content: Text content or file reference
        classification: Classification result
        turn_number: Current turn number
        file_metadata: Optional file metadata (for document uploads)

    Returns:
        EvidenceProvided record ready to add to diagnostic state
    """
    return EvidenceProvided(
        turn_number=turn_number,
        form=classification.form,
        content=content,
        file_metadata=file_metadata,
        addresses_requests=classification.matched_request_ids,
        completeness=classification.completeness,
        evidence_type=classification.evidence_type,
        user_intent=classification.user_intent,
        key_findings=[],  # Will be populated by analysis
        confidence_impact=None  # Will be populated by confidence scorer
    )


def _find_request(
    requests: List[EvidenceRequest],
    request_id: str
) -> Optional[EvidenceRequest]:
    """
    Find evidence request by ID.

    Args:
        requests: List of evidence requests
        request_id: ID to search for

    Returns:
        EvidenceRequest if found, None otherwise
    """
    for req in requests:
        if req.request_id == request_id:
            return req
    return None


def summarize_evidence_status(diagnostic_state: CaseDiagnosticState) -> dict:
    """
    Generate summary of evidence collection progress.

    Returns:
        Dictionary with counts by status
    """
    status_counts = {
        "pending": 0,
        "partial": 0,
        "complete": 0,
        "blocked": 0,
        "obsolete": 0
    }

    for req in diagnostic_state.evidence_requests:
        status_counts[req.status.value] += 1

    total_requests = len(diagnostic_state.evidence_requests)
    evidence_provided_count = len(diagnostic_state.evidence_provided)

    return {
        "total_requests": total_requests,
        "evidence_provided_count": evidence_provided_count,
        "status_breakdown": status_counts,
        "completion_rate": (
            status_counts["complete"] / total_requests if total_requests > 0 else 0.0
        )
    }

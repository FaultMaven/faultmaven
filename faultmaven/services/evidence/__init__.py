"""
Evidence Services Module

Provides services for evidence-centric troubleshooting including:
- Multi-dimensional evidence classification
- Evidence lifecycle management
- Conflict resolution workflow
- Investigation stall detection
"""

from faultmaven.services.evidence.classification import (
    classify_evidence_multidimensional,
    validate_classification,
)

from faultmaven.services.evidence.lifecycle import (
    update_evidence_lifecycle,
    mark_obsolete_requests,
    get_active_evidence_requests,
    create_evidence_record,
    summarize_evidence_status,
)

from faultmaven.services.evidence.stall_detection import (
    check_for_stall,
    increment_stall_counters,
    should_escalate,
    generate_stall_message,
)

__all__ = [
    # Classification
    "classify_evidence_multidimensional",
    "validate_classification",
    # Lifecycle
    "update_evidence_lifecycle",
    "mark_obsolete_requests",
    "get_active_evidence_requests",
    "create_evidence_record",
    "summarize_evidence_status",
    # Stall Detection
    "check_for_stall",
    "increment_stall_counters",
    "should_escalate",
    "generate_stall_message",
]

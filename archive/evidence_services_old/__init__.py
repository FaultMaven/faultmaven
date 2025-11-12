"""
Evidence Services Module

Provides services for evidence-centric troubleshooting including:
- Multi-dimensional evidence classification
- Evidence lifecycle management
- Conflict resolution workflow
- Progress tracking (v3.0)
- Evidence factory (preprocessing → evidence system bridge)
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

from faultmaven.services.evidence.evidence_factory import (
    create_evidence_from_preprocessed,
    map_datatype_to_evidence_category,
    match_evidence_to_requests,
)

from faultmaven.services.evidence.evidence_enhancements import (
    extract_timeline_events,
    should_populate_timeline,
    generate_hypotheses_from_anomalies,
    should_generate_hypotheses,
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
    # Evidence Factory
    "create_evidence_from_preprocessed",
    "map_datatype_to_evidence_category",
    "match_evidence_to_requests",
    # Evidence Enhancements
    "extract_timeline_events",
    "should_populate_timeline",
    "generate_hypotheses_from_anomalies",
    "should_generate_hypotheses",
]

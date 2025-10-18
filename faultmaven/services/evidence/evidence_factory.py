"""
Evidence Factory - Bridge between preprocessing and evidence systems

This module converts PreprocessedData (from data preprocessing pipeline) into
EvidenceProvided objects (for the evidence-centric troubleshooting system).

Design Reference:
- Data Preprocessing: docs/architecture/data-preprocessing-design.md
- Evidence System: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from typing import Dict, List, Optional
from uuid import uuid4

from faultmaven.models.api import DataType, PreprocessedData
from faultmaven.models.evidence import (
    CompletenessLevel,
    EvidenceCategory,
    EvidenceForm,
    EvidenceProvided,
    EvidenceType,
    FileMetadata,
    UserIntent,
)


def create_evidence_from_preprocessed(
    preprocessed: PreprocessedData,
    filename: str,
    turn_number: int,
    evidence_type: EvidenceType = EvidenceType.SUPPORTIVE,
    addresses_requests: List[str] = None,
    data_id: Optional[str] = None,
    processed_at: Optional[str] = None,
) -> EvidenceProvided:
    """Convert PreprocessedData to EvidenceProvided for evidence system integration.

    This function bridges the preprocessing pipeline (which focuses on data
    extraction and summarization) with the evidence system (which tracks
    diagnostic investigation progress).

    Args:
        preprocessed: Output from preprocessing pipeline with summary and insights
        filename: Original filename of uploaded data
        turn_number: Current conversation turn number
        evidence_type: Evidential value (defaults to SUPPORTIVE)
        addresses_requests: List of evidence request IDs this satisfies

    Returns:
        EvidenceProvided object ready for diagnostic state integration

    Example:
        >>> preprocessed = await preprocessing_service.preprocess(...)
        >>> evidence = create_evidence_from_preprocessed(
        ...     preprocessed=preprocessed,
        ...     filename="app.log",
        ...     turn_number=3,
        ...     evidence_type=EvidenceType.SUPPORTIVE
        ... )
        >>> diagnostic_state.evidence_provided.append(evidence)
    """
    # Use provided values or generate defaults
    from datetime import datetime, timezone
    if not data_id:
        import uuid
        data_id = str(uuid.uuid4())
    if not processed_at:
        # Don't serialize yet - pass datetime object to Pydantic
        # Pydantic will handle serialization correctly via model_dump()
        processed_at = datetime.now(timezone.utc)

    # Create file metadata
    file_metadata = FileMetadata(
        filename=filename,
        content_type=_infer_content_type(preprocessed.metadata.data_type),
        size_bytes=preprocessed.original_size,
        upload_timestamp=processed_at,
        file_id=data_id,
    )

    # Extract key findings from insights
    key_findings = _extract_key_findings(preprocessed)

    # Determine completeness (uploaded documents are always complete)
    completeness = CompletenessLevel.COMPLETE

    return EvidenceProvided(
        evidence_id=data_id,
        turn_number=turn_number,
        timestamp=processed_at,
        form=EvidenceForm.DOCUMENT,
        content=preprocessed.content,
        file_metadata=file_metadata,
        addresses_requests=addresses_requests or [],
        completeness=completeness,
        evidence_type=evidence_type,
        user_intent=UserIntent.PROVIDING_EVIDENCE,
        key_findings=key_findings,
        confidence_impact=None,  # Would be calculated by hypothesis validator
    )


def map_datatype_to_evidence_category(data_type: DataType) -> EvidenceCategory:
    """Map preprocessing DataType to evidence system EvidenceCategory.

    This mapping connects the data classification system with the diagnostic
    evidence framework, ensuring uploaded data is categorized correctly for
    the 5-phase troubleshooting workflow.

    Args:
        data_type: DataType from preprocessing classification

    Returns:
        EvidenceCategory for diagnostic evidence tracking

    Mapping Rationale:
        - LOGS_AND_ERRORS → SYMPTOMS: Logs and stack traces show failure symptoms
        - UNSTRUCTURED_TEXT → ENVIRONMENT: Documentation, runbooks, descriptions
        - STRUCTURED_CONFIG → CONFIGURATION: Settings, env vars, deployment configs
        - METRICS_AND_PERFORMANCE → METRICS: Performance data, traces, profiling
        - SOURCE_CODE → ENVIRONMENT: Code snippets, patches, configurations
        - VISUAL_EVIDENCE → SYMPTOMS: Screenshots, diagrams showing failures
        - UNANALYZABLE → SYMPTOMS: Default to symptom category for unknown types
    """
    mapping = {
        DataType.LOGS_AND_ERRORS: EvidenceCategory.SYMPTOMS,
        DataType.UNSTRUCTURED_TEXT: EvidenceCategory.ENVIRONMENT,
        DataType.STRUCTURED_CONFIG: EvidenceCategory.CONFIGURATION,
        DataType.METRICS_AND_PERFORMANCE: EvidenceCategory.METRICS,
        DataType.SOURCE_CODE: EvidenceCategory.ENVIRONMENT,
        DataType.VISUAL_EVIDENCE: EvidenceCategory.SYMPTOMS,
        DataType.UNANALYZABLE: EvidenceCategory.SYMPTOMS,  # Default fallback
    }
    return mapping.get(data_type, EvidenceCategory.SYMPTOMS)


def _infer_content_type(data_type: DataType) -> str:
    """Infer MIME type from DataType classification.

    Args:
        data_type: Classified data type

    Returns:
        MIME type string
    """
    mime_mapping = {
        DataType.LOGS_AND_ERRORS: "text/plain",
        DataType.UNSTRUCTURED_TEXT: "text/plain",
        DataType.STRUCTURED_CONFIG: "application/json",
        DataType.METRICS_AND_PERFORMANCE: "text/csv",
        DataType.SOURCE_CODE: "text/plain",
        DataType.VISUAL_EVIDENCE: "image/png",
        DataType.UNANALYZABLE: "application/octet-stream",
    }
    return mime_mapping.get(data_type, "application/octet-stream")


def _extract_key_findings(preprocessed: PreprocessedData) -> List[str]:
    """Extract key findings from preprocessed content.

    Extracts meaningful findings from the preprocessed content for evidence tracking.

    Args:
        preprocessed: PreprocessedData with content

    Returns:
        List of key finding strings (max 5)
    """
    findings = []

    # Extract from content (which is the LLM-ready summary)
    if preprocessed.content:
        # Extract first meaningful lines from content
        lines = [l.strip() for l in preprocessed.content.split('\n') if l.strip()]
        for line in lines[:5]:
            if len(line) > 20 and not line.startswith('='): # Skip headers
                findings.append(line[:200])
                if len(findings) >= 5:
                    break

    return findings[:5]


def match_evidence_to_requests(
    preprocessed: PreprocessedData,
    pending_requests: List[Dict],
) -> List[str]:
    """Match uploaded evidence to pending evidence requests.

    Uses data type and content to determine which evidence requests
    this upload satisfies.

    Args:
        preprocessed: PreprocessedData from preprocessing pipeline
        pending_requests: List of EvidenceRequest dicts with 'request_id' and 'category'

    Returns:
        List of matched request IDs

    Example:
        >>> requests = [
        ...     {"request_id": "req-1", "category": "symptoms"},
        ...     {"request_id": "req-2", "category": "metrics"},
        ... ]
        >>> matched = match_evidence_to_requests(preprocessed, requests)
        >>> # Returns ["req-1"] if preprocessed is LOG_FILE (symptoms)
    """
    if not pending_requests:
        return []

    # Map data type to evidence category
    evidence_category = map_datatype_to_evidence_category(preprocessed.metadata.data_type)

    # Find requests matching this category
    matched_ids = []
    for request in pending_requests:
        request_category = request.get("category")
        if request_category == evidence_category.value:
            matched_ids.append(request["request_id"])

    return matched_ids

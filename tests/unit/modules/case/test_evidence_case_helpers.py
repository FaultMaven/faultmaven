"""Tests for Evidence new fields and Case helper properties."""

from datetime import datetime, timezone

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)
from tests.utils import generate_evidence_id


def _make_evidence(
    category=EvidenceCategory.SYMPTOM_EVIDENCE,
    evidence_id=None,
    summary="Test evidence",
    **kwargs,
):
    """Helper: create a properly-initialized Evidence object."""
    return Evidence(
        evidence_id=evidence_id or generate_evidence_id(),
        category=category,
        summary=summary,
        primary_purpose="testing",
        preprocessed_content="preprocessed content",
        content_size_bytes=100,
        preprocessing_method="manual",
        source_type=EvidenceSourceType.LOGS,
        form=EvidenceForm.DOCUMENT,
        collected_by="user_123",
        collected_at_turn=1,
        **kwargs,
    )


def _make_case(evidence_list=None):
    """Helper: create a Case with given evidence list."""
    return Case(
        case_id="case_1234567890ab",
        title="Test Case",
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Test description",
        evidence=evidence_list or [],
        progress=InvestigationProgress(),
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )


# =============================================================================
# Evidence new fields
# =============================================================================


class TestEvidenceNewFields:
    # The data_type / content_hash / extraction_method tests previously here
    # were removed in the schema redesign (commit 7b5a1b93). Those fields
    # were dropped from Evidence — file metadata (content_hash, content_type)
    # now lives on uploaded_files, and the extraction_method classification
    # was folded into the preprocessing pipeline's metadata blob. The
    # processing_mode tests below remain — that field is still a valid
    # transient classifier on Evidence.

    def test_processing_mode_defaults_to_none(self):
        ev = _make_evidence()
        assert ev.processing_mode is None

    def test_processing_mode_triage(self):
        ev = _make_evidence(processing_mode="triage")
        assert ev.processing_mode == "triage"

    def test_processing_mode_directed_analysis(self):
        ev = _make_evidence(processing_mode="directed_analysis")
        assert ev.processing_mode == "directed_analysis"

    # da_invocation_count tests removed — the field was a Pydantic-only
    # cross-turn counter with no backing DB column. The save path that
    # was supposed to persist it silently dropped the value (see the
    # 2026-04 additive-save fix), so the field never round-tripped and
    # had no real consumer. Removed in the hierarchy consolidation.
    # Within-turn DA tracking lives on
    # ``EvidenceDAState.da_call_count`` in agent_orchestration_service.

    def test_processing_mode_roundtrip_via_dict(self):
        """processing_mode survives dict serialization."""
        ev = _make_evidence(processing_mode="directed_analysis")
        as_dict = ev.model_dump()
        restored = Evidence(**as_dict)
        assert restored.processing_mode == "directed_analysis"

    def test_backward_compat_missing_processing_fields(self):
        """Evidence created without processing_mode defaults to None."""
        ev = _make_evidence()
        assert ev.processing_mode is None


# =============================================================================
# Case helper properties
# =============================================================================


class TestCaseValidEvidence:
    def test_empty_evidence(self):
        case = _make_case([])
        assert case.valid_evidence == []

    def test_all_valid(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.SYMPTOM_EVIDENCE),
            _make_evidence(category=EvidenceCategory.CONTEXTUAL_EVIDENCE),
        ]
        case = _make_case(evidence)
        assert len(case.valid_evidence) == 2

    def test_excludes_rejected(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.SYMPTOM_EVIDENCE),
            _make_evidence(category=EvidenceCategory.REJECTED),
            _make_evidence(category=EvidenceCategory.CONTEXTUAL_EVIDENCE),
        ]
        case = _make_case(evidence)
        valid = case.valid_evidence
        assert len(valid) == 2
        assert all(e.category != EvidenceCategory.REJECTED for e in valid)


class TestCaseRejectedSubmissions:
    def test_empty_evidence(self):
        case = _make_case([])
        assert case.rejected_submissions == []

    def test_no_rejected(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.SYMPTOM_EVIDENCE),
        ]
        case = _make_case(evidence)
        assert case.rejected_submissions == []

    def test_only_rejected(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.SYMPTOM_EVIDENCE),
            _make_evidence(category=EvidenceCategory.REJECTED),
        ]
        case = _make_case(evidence)
        rejected = case.rejected_submissions
        assert len(rejected) == 1
        assert rejected[0].category == EvidenceCategory.REJECTED


class TestCaseAcceptanceRate:
    def test_empty_evidence_returns_one(self):
        case = _make_case([])
        assert case.acceptance_rate == 1.0

    def test_all_valid(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.SYMPTOM_EVIDENCE),
            _make_evidence(category=EvidenceCategory.CONTEXTUAL_EVIDENCE),
        ]
        case = _make_case(evidence)
        assert case.acceptance_rate == 1.0

    def test_mixed(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.SYMPTOM_EVIDENCE),
            _make_evidence(category=EvidenceCategory.REJECTED),
            _make_evidence(category=EvidenceCategory.CONTEXTUAL_EVIDENCE),
            _make_evidence(category=EvidenceCategory.REJECTED),
        ]
        case = _make_case(evidence)
        assert case.acceptance_rate == pytest.approx(0.5)

    def test_all_rejected(self):
        evidence = [
            _make_evidence(category=EvidenceCategory.REJECTED),
        ]
        case = _make_case(evidence)
        assert case.acceptance_rate == 0.0

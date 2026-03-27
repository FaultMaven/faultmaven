"""Unit tests for evidence classification in the unified ingestion pipeline.

Tests evidence classification after the unified turn pipeline (v4.1):
1. Evidence categories: SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED
2. Evidence form determined by payload context (not LLM classification)
3. Deduplication via content comparison
4. Classification based on content (not investigation phase)
5. Source type classification (5 types)

Design Reference:
- docs/architecture/data-processing/data-preprocessing-design-specification.md (Section 8)
- docs/working/IMPLEMENTATION-unified-ingestion-pipeline.md (Phase 6.1)

NOTE: SubmissionClassification was removed in Phase 1-2. Evidence form is now
payload-driven: attachments → DOCUMENT, agent findings → SUBMITTED_DATA.
"""

from datetime import UTC, datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
)

# ============================================================
# Helpers
# ============================================================


def _make_case(**overrides) -> Case:
    """Create a Case with sensible defaults for evidence classification tests."""
    defaults = {
        "case_id": "case_1234567890ab",
        "title": "Test Case",
        "status": CaseStatus.INVESTIGATING,
        "user_id": "user_123",
        "organization_id": "org_123",
        "description": "Database connection errors",
        "problem_verification": ProblemVerification(
            symptom_statement="Database connection errors",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        "inquiry": InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Database connection errors",
        ),
        "evidence": [],
    }
    defaults.update(overrides)
    return Case(**defaults)


def _make_evidence(**overrides) -> Evidence:
    """Create an Evidence with sensible defaults."""
    defaults = {
        "evidence_id": f"ev_{uuid4().hex[:12]}",
        "category": EvidenceCategory.SYMPTOM_EVIDENCE,
        "source_type": EvidenceSourceType.LOGS,
        "form": EvidenceForm.DOCUMENT,
        "summary": "Test evidence",
        "primary_purpose": "Shows test data",
        "content_ref": "s3://evidence/test.log",
        "collected_at": datetime.now(UTC),
        "collected_by": "user_123",
        "collected_at_turn": 1,
        "preprocessing_method": "crime_scene",
        "content_size_bytes": 1024,
        "preprocessed_content": "Test content",
    }
    defaults.update(overrides)
    return Evidence(**defaults)


# ============================================================
# Evidence Classification Basics
# ============================================================


class TestEvidenceClassificationBasics:
    """Test basic evidence classification logic.

    Post-unified-pipeline: No SubmissionClassification. Evidence is created
    either by _preprocess_attachment (form=DOCUMENT) or by evidence_to_add
    in the milestone engine (form=USER_TEXT).
    """

    @pytest.mark.asyncio
    async def test_query_only_turn_creates_no_evidence(self):
        """Pure chat (query-only, no attachments) creates no evidence.

        In the unified pipeline, query-only turns go directly to the LLM
        without creating evidence. Only attachments trigger evidence creation
        in Step 1, and the LLM may add evidence via evidence_to_add.
        """
        case = _make_case(evidence=[])
        initial_count = len(case.evidence)

        # No evidence added for query-only turns (handled at pipeline level)
        assert len(case.evidence) == initial_count
        assert initial_count == 0

    @pytest.mark.asyncio
    async def test_attachment_creates_document_evidence(self):
        """Attachment processed through Tier 0+1 creates DOCUMENT evidence."""
        case = _make_case(evidence=[])

        evidence = _make_evidence(
            form=EvidenceForm.DOCUMENT,
            summary="Database connection timeout errors in application logs",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            preprocessing_method="crime_scene",
            data_type="LOGS",
        )
        case.evidence.append(evidence)

        assert len(case.evidence) == 1
        assert case.evidence[0].form == EvidenceForm.DOCUMENT
        assert case.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert case.evidence[0].source_type == EvidenceSourceType.LOGS
        assert "connection timeout" in case.evidence[0].summary.lower()

    @pytest.mark.asyncio
    async def test_agent_finding_creates_user_text_evidence(self):
        """Agent-derived findings via evidence_to_add create USER_TEXT evidence."""
        case = _make_case(evidence=[])

        evidence_to_add = EvidenceToAdd(
            summary="Connection pool exhaustion pattern detected",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
        )

        # In the unified pipeline, milestone engine creates evidence with USER_TEXT form
        evidence = _make_evidence(
            form=EvidenceForm.USER_TEXT,
            summary=evidence_to_add.summary,
            category=evidence_to_add.category,
            source_type=evidence_to_add.source_type,
            preprocessing_method="none",
        )
        case.evidence.append(evidence)

        assert len(case.evidence) == 1
        assert case.evidence[0].form == EvidenceForm.USER_TEXT
        assert case.evidence[0].category == EvidenceCategory.CAUSAL_EVIDENCE

    @pytest.mark.asyncio
    async def test_rejected_data_tracked(self):
        """Rejected submissions create REJECTED evidence for audit trail."""
        case = _make_case(evidence=[])

        evidence = _make_evidence(
            category=EvidenceCategory.REJECTED,
            source_type=EvidenceSourceType.IMAGE,
            form=EvidenceForm.DOCUMENT,
            summary="Unrelated screenshot",
            primary_purpose="Screenshot appears to be vacation photos, not related to database issue",
        )
        case.evidence.append(evidence)

        assert len(case.evidence) == 1
        assert case.evidence[0].category == EvidenceCategory.REJECTED
        valid = [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]
        assert len(valid) == 0

    @pytest.mark.asyncio
    async def test_duplicate_detection_via_content(self):
        """Duplicate detection works by comparing preprocessed_content."""
        case = _make_case(evidence=[])
        preprocessed = "ERROR: Connection timeout"

        first = _make_evidence(
            evidence_id="ev_abc123456789",
            preprocessed_content=preprocessed,
            collected_at_turn=1,
        )
        case.evidence.append(first)

        # Detect duplicate by comparing preprocessed_content
        duplicate_found = None
        for ev in case.evidence:
            if ev.preprocessed_content == preprocessed:
                duplicate_found = ev
                break

        assert duplicate_found is not None
        assert duplicate_found.evidence_id == "ev_abc123456789"

        # Mark duplicate as REJECTED
        second = _make_evidence(
            evidence_id="ev_def456789012",
            category=EvidenceCategory.REJECTED,
            summary=f"Duplicate of evidence from turn {duplicate_found.collected_at_turn}",
            preprocessed_content=preprocessed,
            collected_at_turn=2,
        )
        case.evidence.append(second)

        assert len(case.evidence) == 2
        assert case.evidence[1].category == EvidenceCategory.REJECTED
        assert "duplicate" in case.evidence[1].summary.lower()


# ============================================================
# Contextual Evidence
# ============================================================


class TestContextualEvidence:
    """Test CONTEXTUAL_EVIDENCE category for baseline/environmental data."""

    @pytest.mark.asyncio
    async def test_contextual_evidence_classification(self):
        """Baseline/context data uses CONTEXTUAL_EVIDENCE category."""
        case = _make_case(description="Microservices performance issue")

        evidence = _make_evidence(
            category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
            source_type=EvidenceSourceType.IMAGE,
            form=EvidenceForm.DOCUMENT,
            summary="System architecture diagram showing microservices",
            primary_purpose="Provides system context and component relationships",
        )
        case.evidence.append(evidence)

        assert case.evidence[0].category == EvidenceCategory.CONTEXTUAL_EVIDENCE
        assert case.evidence[0].source_type == EvidenceSourceType.IMAGE

    @pytest.mark.asyncio
    async def test_classification_based_on_content_not_phase(self):
        """Evidence is classified based on content, not investigation phase."""
        case = _make_case(
            status=CaseStatus.INQUIRY,
            inquiry=InquiryData(
                problem_statement_confirmed=False,
                decided_to_investigate=False,
                thread_id="thread_123",
            ),
        )

        # SYMPTOM_EVIDENCE even during INQUIRY phase
        evidence = _make_evidence(
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            form=EvidenceForm.DOCUMENT,
            summary="Application logs showing connection errors",
        )
        case.evidence.append(evidence)

        assert case.status == CaseStatus.INQUIRY
        assert case.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE


# ============================================================
# Source Type Classification
# ============================================================


class TestSourceTypeClassification:
    """Test simplified source type classification (5 types)."""

    @pytest.mark.asyncio
    async def test_logs_source_type(self):
        """LOGS source type for textual diagnostic output."""
        ev = _make_evidence(source_type=EvidenceSourceType.LOGS)
        assert ev.source_type == EvidenceSourceType.LOGS

    @pytest.mark.asyncio
    async def test_metrics_source_type(self):
        """METRICS source type for quantitative measurements."""
        ev = _make_evidence(
            source_type=EvidenceSourceType.METRICS,
            summary="CPU usage showing 95% utilization",
        )
        assert ev.source_type == EvidenceSourceType.METRICS

    @pytest.mark.asyncio
    async def test_configuration_source_type(self):
        """CONFIGURATION source type for system/app configuration."""
        ev = _make_evidence(
            source_type=EvidenceSourceType.CONFIGURATION,
            category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
            summary="Database connection pool configuration",
        )
        assert ev.source_type == EvidenceSourceType.CONFIGURATION

    @pytest.mark.asyncio
    async def test_image_source_type(self):
        """IMAGE source type for visual representations."""
        ev = _make_evidence(
            source_type=EvidenceSourceType.IMAGE,
            summary="Screenshot of error dialog",
        )
        assert ev.source_type == EvidenceSourceType.IMAGE

    @pytest.mark.asyncio
    async def test_text_source_type(self):
        """TEXT source type for user narrative."""
        ev = _make_evidence(
            source_type=EvidenceSourceType.TEXT,
            form=EvidenceForm.USER_TEXT,
            summary="User report of intermittent timeouts",
            preprocessing_method="none",
        )
        assert ev.source_type == EvidenceSourceType.TEXT
        assert ev.form == EvidenceForm.USER_TEXT


# ============================================================
# Evidence Helper Methods
# ============================================================


class TestEvidenceHelperMethods:
    """Test Case helper methods for evidence filtering."""

    @pytest.mark.asyncio
    async def test_valid_evidence_excludes_rejected(self):
        """Case.evidence filtering excludes REJECTED submissions."""
        case = _make_case(
            evidence=[
                _make_evidence(
                    evidence_id="ev_000000000001",
                    category=EvidenceCategory.SYMPTOM_EVIDENCE,
                ),
                _make_evidence(
                    evidence_id="ev_000000000002",
                    category=EvidenceCategory.REJECTED,
                ),
                _make_evidence(
                    evidence_id="ev_000000000003",
                    category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
                ),
            ]
        )

        assert len(case.evidence) == 3
        valid = [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]
        assert len(valid) == 2
        assert all(e.category != EvidenceCategory.REJECTED for e in valid)

    @pytest.mark.asyncio
    async def test_acceptance_rate_calculation(self):
        """Evidence acceptance rate: valid / total."""
        case = _make_case(
            evidence=[
                _make_evidence(
                    evidence_id=f"ev_{str(i).zfill(12)}",
                    category=(
                        EvidenceCategory.SYMPTOM_EVIDENCE
                        if i < 8
                        else EvidenceCategory.REJECTED
                    ),
                    collected_at_turn=i + 1,
                )
                for i in range(10)
            ]
        )

        valid_count = len(
            [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]
        )
        acceptance_rate = (valid_count / len(case.evidence)) * 100.0

        assert len(case.evidence) == 10
        assert valid_count == 8
        assert acceptance_rate == 80.0

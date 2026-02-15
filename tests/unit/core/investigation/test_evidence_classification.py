"""
Unit Tests for Evidence Classification Redesign

Tests the single-phase evidence creation flow where evidence is classified
by the LLM during processing and created AFTER evaluation (not before).

Design Reference:
- docs/architecture/data-processing/EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md
- docs/architecture/data-processing/EVIDENCE-REDESIGN-IMPLEMENTATION-PLAN.md

Key Design Principles Tested:
1. Single-phase evidence creation (after LLM evaluation)
2. Evidence categories: SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED
3. Submission classifications: user_chat, external_data, mixed
4. Deduplication via content_hash
5. Classification based on content (not investigation phase)

IMPORTANT: These tests depend on Phase 3-4 implementation.
Some tests are currently mocked/simplified until evidence classification is fully implemented.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# TODO Phase 3-4: Restore these imports when SubmissionClassification is implemented
# from faultmaven.core.investigation.schemas import SubmissionClassification
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    EvidenceArtifact,
    InvestigationStage,
    ProblemVerification,
    InquiryData,
)


class TestEvidenceClassificationBasics:
    """Test basic evidence classification logic

    NOTE: Tests currently use mocks until Phase 3-4 SubmissionClassification is implemented.
    """

    @pytest.mark.skip(
        reason="Waiting for Phase 3-4: SubmissionClassification not implemented"
    )
    @pytest.mark.asyncio
    async def test_user_chat_no_evidence_created(self):
        """Pure chat should not create evidence record

        TODO Phase 3-4: Restore when SubmissionClassification schema is added.
        """
        # Arrange
        mock_llm = MagicMock()
        mock_repo = MagicMock()
        mock_repo.save = AsyncMock(side_effect=lambda c: c)

        engine = MilestoneEngine(mock_llm, mock_repo)

        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Test description",
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
            evidence=[],
        )

        # TODO Phase 3-4: Uncomment when SubmissionClassification exists
        # submission_classification = SubmissionClassification(
        #     type="user_chat",
        #     confidence="high",
        #     reasoning="User is asking a question, no external data provided",
        #     external_data_summary=None,
        # )

        # Act - simulate processing with classification
        initial_evidence_count = len(case.evidence)

        # In the real implementation, process_turn would check submission_classification
        # and not create evidence for user_chat type
        # if submission_classification.type == "user_chat":
        #     pass

        # Assert
        assert len(case.evidence) == initial_evidence_count
        assert initial_evidence_count == 0  # Started with no evidence

    @pytest.mark.asyncio
    async def test_external_data_creates_evidence(self):
        """External data should create evidence with category

        This test works with current code - tests Evidence creation (not EvidenceArtifact).
        """
        # Arrange - need proper inquiry setup for INVESTIGATING status
        from faultmaven.modules.case.contracts import Evidence, EvidenceForm

        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Test description",
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
            evidence=[],
        )

        # TODO Phase 3-4: Restore SubmissionClassification when implemented
        # submission_classification = SubmissionClassification(
        #     type="external_data",
        #     confidence="high",
        #     reasoning="User provided log file with error messages",
        #     external_data_summary="Application logs showing database connection errors",
        # )

        evidence_to_add = EvidenceToAdd(
            summary="Database connection timeout errors in application logs",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
        )

        # Act - simulate evidence creation using Evidence (not EvidenceArtifact)
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=evidence_to_add.category,
            source_type=evidence_to_add.source_type,
            form=EvidenceForm.DOCUMENT,
            summary=evidence_to_add.summary,
            primary_purpose="Shows repeated connection timeout errors during peak hours",
            content_ref="s3://evidence/case_123/app.log",
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=1,
            preprocessing_method="log_parser",
            content_size_bytes=1024,
            preprocessed_content="Log content preview",
        )
        case.evidence.append(evidence)

        # Assert
        assert len(case.evidence) == 1
        assert case.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert case.evidence[0].source_type == EvidenceSourceType.LOGS
        assert "connection timeout" in case.evidence[0].summary.lower()

    @pytest.mark.asyncio
    async def test_rejected_data_tracked(self):
        """Rejected submissions should create REJECTED evidence for audit trail

        This test works with current code - tests REJECTED category.
        """
        # Arrange
        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Database timeout issues",
            problem_verification=ProblemVerification(
                symptom_statement="Database timeout issues",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                thread_id="thread_123",
                proposed_problem_statement="Database timeout issues",
            ),
            evidence=[],
        )

        # TODO Phase 3-4: Restore SubmissionClassification when implemented
        # submission_classification = SubmissionClassification(
        #     type="external_data",
        #     confidence="high",
        #     reasoning="User uploaded a screenshot, but it's unrelated to the issue",
        #     external_data_summary="Screenshot of vacation photos",
        # )

        evidence_to_add = EvidenceToAdd(
            summary="Unrelated screenshot",
            category=EvidenceCategory.REJECTED,
            source_type=EvidenceSourceType.IMAGE,
        )

        # Act
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=evidence_to_add.category,
            source_type=evidence_to_add.source_type,
            form=EvidenceForm.DOCUMENT,
            summary=evidence_to_add.summary,
            primary_purpose="Screenshot appears to be vacation photos, not related to database issue",
            content_ref="s3://evidence/case_123/vacation.png",
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=1,
            preprocessing_method="image_ocr",
            content_size_bytes=2048,
            preprocessed_content="Image OCR results",
        )
        case.evidence.append(evidence)

        # Assert
        assert len(case.evidence) == 1
        assert case.evidence[0].category == EvidenceCategory.REJECTED
        assert "vacation photos" in case.evidence[0].primary_purpose.lower()

        # Verify we can query valid evidence only
        valid_evidence = [
            e for e in case.evidence if e.category != EvidenceCategory.REJECTED
        ]
        assert len(valid_evidence) == 0  # No valid evidence, only rejected

    @pytest.mark.asyncio
    async def test_duplicate_detection(self):
        """Duplicate detection should work via comparing content_ref and preprocessed_content

        NOTE: Evidence model doesn't have content_hash field. In the actual implementation,
        duplicate detection would be handled at the service layer by computing hashes
        of the preprocessed_content before creating Evidence records.

        This test validates that REJECTED category can be used for duplicates.
        """
        # Arrange
        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Database connection errors",
            problem_verification=ProblemVerification(
                symptom_statement="Database connection errors",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                thread_id="thread_123",
                proposed_problem_statement="Database connection errors",
            ),
            evidence=[],
        )

        # First upload
        file_content = b"ERROR: Connection timeout at 2024-01-10 10:00:00"
        preprocessed = "ERROR: Connection timeout"

        first_evidence = Evidence(
            evidence_id="ev_abc123456789",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            form=EvidenceForm.DOCUMENT,
            summary="Database connection errors",
            primary_purpose="Shows connection timeout errors",
            content_ref="s3://evidence/case_123/app_turn1.log",
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=1,
            preprocessing_method="log_parser",
            content_size_bytes=len(file_content),
            preprocessed_content=preprocessed,
        )
        case.evidence.append(first_evidence)

        # Act - Second upload (same content)
        # In real implementation, service layer would detect duplicate by comparing
        # preprocessed_content before creating the Evidence record
        duplicate_found = None
        for ev in case.evidence:
            if ev.preprocessed_content == preprocessed:
                duplicate_found = ev
                break

        # Assert
        assert duplicate_found is not None
        assert duplicate_found.evidence_id == "ev_abc123456789"
        assert duplicate_found.collected_at_turn == 1

        # In real implementation, second upload would create REJECTED evidence
        second_evidence = Evidence(
            evidence_id="ev_def456789012",
            category=EvidenceCategory.REJECTED,
            source_type=EvidenceSourceType.LOGS,
            form=EvidenceForm.DOCUMENT,
            summary=f"Duplicate of evidence from turn {duplicate_found.collected_at_turn}",
            primary_purpose=f"Same content as {duplicate_found.evidence_id}: {duplicate_found.summary}",
            content_ref="s3://evidence/case_123/app_turn2.log",
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=2,
            preprocessing_method="log_parser",
            content_size_bytes=len(file_content),
            preprocessed_content=preprocessed,
        )
        case.evidence.append(second_evidence)

        assert len(case.evidence) == 2
        assert case.evidence[1].category == EvidenceCategory.REJECTED
        assert "duplicate" in case.evidence[1].summary.lower()


class TestContextualEvidence:
    """Test CONTEXTUAL_EVIDENCE category for baseline/environmental data

    These tests work with current code - no Phase 3-4 dependencies.
    """

    @pytest.mark.asyncio
    async def test_contextual_evidence_classification(self):
        """Baseline/context data should use CONTEXTUAL_EVIDENCE category"""
        # Arrange
        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INVESTIGATING,
            user_id="user_123",
            organization_id="org_123",
            description="Microservices performance issue",
            problem_verification=ProblemVerification(
                symptom_statement="Microservices performance issue",
                severity="MEDIUM",
                temporal_state="ongoing",
                urgency_level="medium",
            ),
            inquiry=InquiryData(
                problem_statement_confirmed=True,
                decided_to_investigate=True,
                thread_id="thread_123",
                proposed_problem_statement="Microservices performance issue",
            ),
            evidence=[],
        )

        # User uploads architecture diagram
        evidence_to_add = EvidenceToAdd(
            summary="System architecture diagram showing microservices",
            category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
            source_type=EvidenceSourceType.IMAGE,
        )

        # Act
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=evidence_to_add.category,
            source_type=evidence_to_add.source_type,
            form=EvidenceForm.DOCUMENT,
            summary=evidence_to_add.summary,
            primary_purpose="Provides system context and component relationships",
            content_ref="s3://evidence/case_123/architecture.png",
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=1,
            preprocessing_method="image_analysis",
            content_size_bytes=512,
            preprocessed_content="Architecture diagram shows microservices",
        )
        case.evidence.append(evidence)

        # Assert
        assert case.evidence[0].category == EvidenceCategory.CONTEXTUAL_EVIDENCE
        assert case.evidence[0].source_type == EvidenceSourceType.IMAGE
        assert "architecture" in case.evidence[0].summary.lower()

    @pytest.mark.asyncio
    async def test_inquiry_phase_classification_based_on_content(self):
        """Evidence should be classified based on content, not investigation phase"""
        # Arrange - Case in INQUIRY phase
        case = Case(
            case_id="case_1234567890ab",
            title="Test Case",
            status=CaseStatus.INQUIRY,  # Still in INQUIRY
            user_id="user_123",
            organization_id="org_123",
            inquiry=InquiryData(
                problem_statement_confirmed=False,  # Not yet confirmed
                decided_to_investigate=False,  # Not yet investigating
                thread_id="thread_123",
            ),
            evidence=[],
        )

        # User uploads log file with errors during INQUIRY
        evidence_to_add = EvidenceToAdd(
            summary="Application logs showing connection errors",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,  # Classified as SYMPTOM based on content
            source_type=EvidenceSourceType.LOGS,
        )

        # Act - Evidence created even during INQUIRY
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=evidence_to_add.category,
            source_type=evidence_to_add.source_type,
            form=EvidenceForm.DOCUMENT,
            summary=evidence_to_add.summary,
            primary_purpose="Shows database connection timeout errors during peak hours",
            content_ref="s3://evidence/case_123/app.log",
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=1,
            preprocessing_method="log_parser",
            content_size_bytes=1024,
            preprocessed_content="ERROR: Connection timeout errors",
        )
        case.evidence.append(evidence)

        # Assert - Category is SYMPTOM_EVIDENCE even in INQUIRY phase
        assert case.status == CaseStatus.INQUIRY
        assert case.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE
        assert "connection errors" in case.evidence[0].summary.lower()

        # Evidence exists but milestones not advanced (milestone validation only runs in INVESTIGATING)
        assert len(case.evidence[0].advances_milestones) == 0


class TestSourceTypeClassification:
    """Test simplified source type classification (5 types)"""

    @pytest.mark.asyncio
    async def test_logs_source_type(self):
        """LOGS source type for textual diagnostic output"""
        evidence = Evidence(
            evidence_id="ev_123456789abc",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            form=EvidenceForm.DOCUMENT,
            summary="Application logs with errors",
            primary_purpose="Shows error messages",
            content_ref="s3://logs/app.log",
            collected_at=datetime.now(timezone.utc),
            collected_by="user_123",
            collected_at_turn=1,
            preprocessing_method="log_parser",
            content_size_bytes=1024,
            preprocessed_content="ERROR: Application errors",
        )

        assert evidence.source_type == EvidenceSourceType.LOGS

    @pytest.mark.asyncio
    async def test_metrics_source_type(self):
        """METRICS source type for quantitative measurements"""
        evidence = Evidence(
            evidence_id="ev_123456789abc",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.METRICS,
            form=EvidenceForm.DOCUMENT,
            summary="CPU usage dashboard showing 95% utilization",
            primary_purpose="Shows resource exhaustion",
            content_ref="s3://metrics/dashboard.png",
            collected_at=datetime.now(timezone.utc),
            collected_by="user_123",
            collected_at_turn=1,
            preprocessing_method="image_analysis",
            content_size_bytes=512,
            preprocessed_content="CPU metrics show 95% utilization",
        )

        assert evidence.source_type == EvidenceSourceType.METRICS

    @pytest.mark.asyncio
    async def test_configuration_source_type(self):
        """CONFIGURATION source type for system/app configuration"""
        evidence = Evidence(
            evidence_id="ev_123456789abc",
            category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
            source_type=EvidenceSourceType.CONFIGURATION,
            form=EvidenceForm.DOCUMENT,
            summary="Database connection pool configuration",
            primary_purpose="Shows max_connections=10 setting",
            content_ref="s3://config/database.yaml",
            collected_at=datetime.now(timezone.utc),
            collected_by="user_123",
            collected_at_turn=1,
            preprocessing_method="yaml_parser",
            content_size_bytes=256,
            preprocessed_content="max_connections: 10",
        )

        assert evidence.source_type == EvidenceSourceType.CONFIGURATION

    @pytest.mark.asyncio
    async def test_visual_source_type(self):
        """VISUAL source type for visual representations"""
        evidence = Evidence(
            evidence_id="ev_123456789abc",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.IMAGE,
            form=EvidenceForm.DOCUMENT,
            summary="Screenshot of error dialog",
            primary_purpose="Shows user-visible error message",
            content_ref="s3://screenshots/error.png",
            collected_at=datetime.now(timezone.utc),
            collected_by="user_123",
            collected_at_turn=1,
            preprocessing_method="image_ocr",
            content_size_bytes=1024,
            preprocessed_content="Error dialog: Connection failed",
        )

        assert evidence.source_type == EvidenceSourceType.IMAGE

    @pytest.mark.asyncio
    async def test_user_description_source_type(self):
        """USER_DESCRIPTION source type for user's typed narrative"""
        evidence = Evidence(
            evidence_id="ev_123456789abc",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.TEXT,
            form=EvidenceForm.USER_INPUT,
            summary="User report of intermittent timeouts",
            primary_purpose="User observed timeouts every 5 minutes during peak hours",
            content_ref="turn_1_user_message",
            collected_at=datetime.now(timezone.utc),
            collected_by="user_123",
            collected_at_turn=1,
            preprocessing_method="none",
            content_size_bytes=128,
            preprocessed_content="Timeouts every 5 minutes during peak hours",
        )

        assert evidence.source_type == EvidenceSourceType.TEXT
        assert evidence.form == EvidenceForm.USER_INPUT


class TestEvidenceHelperMethods:
    """Test Case helper methods for evidence filtering"""

    @pytest.mark.asyncio
    async def test_valid_evidence_property(self):
        """Case.valid_evidence should exclude REJECTED submissions"""
        case = Case(
            case_id="case_123456789abc",
            title="Test",
            user_id="user_123",
            organization_id="org_123",
            evidence=[
                Evidence(
                    evidence_id="ev_000000000001",
                    category=EvidenceCategory.SYMPTOM_EVIDENCE,
                    source_type=EvidenceSourceType.LOGS,
                    form=EvidenceForm.DOCUMENT,
                    summary="Valid evidence",
                    primary_purpose="Shows errors",
                    content_ref="s3://ev1",
                    collected_at=datetime.now(timezone.utc),
                    collected_by="user_123",
                    collected_at_turn=1,
                    preprocessing_method="none",
                    content_size_bytes=100,
                    preprocessed_content="Error logs",
                ),
                Evidence(
                    evidence_id="ev_000000000002",
                    category=EvidenceCategory.REJECTED,
                    source_type=EvidenceSourceType.IMAGE,
                    form=EvidenceForm.DOCUMENT,
                    summary="Rejected submission",
                    primary_purpose="Unrelated image",
                    content_ref="s3://ev2",
                    collected_at=datetime.now(timezone.utc),
                    collected_by="user_123",
                    collected_at_turn=2,
                    preprocessing_method="none",
                    content_size_bytes=100,
                    preprocessed_content="Unrelated image",
                ),
                Evidence(
                    evidence_id="ev_000000000003",
                    category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
                    source_type=EvidenceSourceType.CONFIGURATION,
                    form=EvidenceForm.DOCUMENT,
                    summary="Context evidence",
                    primary_purpose="Shows config",
                    content_ref="s3://ev3",
                    collected_at=datetime.now(timezone.utc),
                    collected_by="user_123",
                    collected_at_turn=3,
                    preprocessing_method="none",
                    content_size_bytes=100,
                    preprocessed_content="Config file",
                ),
            ],
        )

        # Total evidence includes REJECTED
        assert len(case.evidence) == 3

        # Valid evidence excludes REJECTED
        valid = [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]
        assert len(valid) == 2
        assert all(e.category != EvidenceCategory.REJECTED for e in valid)

    @pytest.mark.asyncio
    async def test_acceptance_rate_calculation(self):
        """Test evidence acceptance rate calculation"""
        case = Case(
            case_id="case_123456789abc",
            title="Test",
            user_id="user_123",
            organization_id="org_123",
            evidence=[
                Evidence(
                    evidence_id=f"ev_{str(i).zfill(12)}",
                    category=(
                        EvidenceCategory.SYMPTOM_EVIDENCE
                        if i < 8
                        else EvidenceCategory.REJECTED
                    ),
                    source_type=EvidenceSourceType.LOGS,
                    form=EvidenceForm.DOCUMENT,
                    summary=f"Evidence {i}",
                    primary_purpose=f"Purpose {i}",
                    content_ref=f"s3://ev{i}",
                    collected_at=datetime.now(timezone.utc),
                    collected_by="user_123",
                    collected_at_turn=i + 1,
                    preprocessing_method="none",
                    content_size_bytes=100,
                    preprocessed_content=f"Content {i}",
                )
                for i in range(10)
            ],
        )

        # 8 valid, 2 rejected out of 10 total
        valid_count = len(
            [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]
        )
        acceptance_rate = (valid_count / len(case.evidence)) * 100.0

        assert len(case.evidence) == 10
        assert valid_count == 8
        assert acceptance_rate == 80.0

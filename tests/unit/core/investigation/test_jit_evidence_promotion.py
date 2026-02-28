import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, UTC
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationProgress,
)
from faultmaven.modules.case.contracts import (
    UploadedFile,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceForm,
)


@pytest.mark.asyncio
async def test_jit_evidence_promotion():
    llm_mock = MagicMock()
    repo_mock = MagicMock()

    engine = MilestoneEngine(
        llm_provider=llm_mock,
        repository=repo_mock,
    )

    case = Case(
        case_id="case_1234567890ab",
        organization_id="org_1234567890abc",
        title="Test Target Problem",
        user_id="user_1234567890abc",
        status=CaseStatus.INQUIRY,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        uploaded_files=[
            UploadedFile(
                file_id="file_1234567890ab",
                filename="syslog.txt",
                size_bytes=1000,
                data_type="LOGS",
                uploaded_at_turn=1,
                uploaded_at=datetime.now(UTC),
                source_type="file_upload",
                preprocessing_summary="System logs",
                content_ref="s3://something",
            ),
            UploadedFile(  # This one is after current turn, shouldn't be promoted
                file_id="file_abcdef123456",
                filename="metrics.json",
                size_bytes=500,
                data_type="METRICS",
                uploaded_at_turn=6,
                uploaded_at=datetime.now(UTC),
                source_type="file_upload",
                preprocessing_summary="Metric data",
                content_ref="s3://something",
            ),
            UploadedFile(  # Fallback type, promoted as LOGS
                file_id="data_1234567890cc",
                filename="weird.dat",
                size_bytes=200,
                data_type="FLUBBER",  # Unknown type
                uploaded_at_turn=2,
                uploaded_at=datetime.now(UTC),
                source_type="file_upload",
                preprocessing_summary="Weird data",
                content_ref="s3://weird",
            ),
            UploadedFile(  # Duplicate guard, excluded
                file_id="data_1234567890dd",
                filename="already_there.txt",
                size_bytes=300,
                data_type="LOGS",
                uploaded_at_turn=3,
                uploaded_at=datetime.now(UTC),
                source_type="file_upload",
                preprocessing_summary="Already there logs",
                content_ref="s3://duplicate",
            ),
        ],
    )

    # Pre-populate evidence to test duplicate guard
    case.evidence.append(
        Evidence(
            evidence_id="ev_000000000000",
            summary="Pre-existing evidence",
            preprocessed_content="Pre-existing",
            content_ref="s3://duplicate",
            content_size_bytes=300,
            preprocessing_method="manual",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            form=EvidenceForm.DOCUMENT,
            source_file_id="data_1234567890dd",  # Matches the duplicate uploaded file
            advances_milestones=[],
            collected_at=datetime.now(UTC),
            collected_by="user_1234567890abc",
            collected_at_turn=3,
            primary_purpose="File Analysis",
        )
    )

    # Need to fake determine_investigation_path since it does not need deep validation for this
    # Actually _transition_to_investigating uses determine_investigation_path, which needs problem_verification
    # Let's just run it

    # Trigger transition
    await engine._transition_to_investigating(case)

    # Check evidence promotion
    assert case.status == CaseStatus.INVESTIGATING
    assert len(case.evidence) == 3  # 1 pre-existing + 2 promoted

    # Check first promoted evidence (syslog.txt)
    promoted_1 = next(
        e for e in case.evidence if e.source_file_id == "file_1234567890ab"
    )
    assert promoted_1.summary == "System logs"
    assert promoted_1.category.value.lower() == "symptom_evidence"
    assert promoted_1.source_type.value.lower() == "logs"

    # Check second promoted evidence (weird.dat fallback)
    promoted_2 = next(
        e for e in case.evidence if e.source_file_id == "data_1234567890cc"
    )
    assert promoted_2.summary == "Weird data"
    assert promoted_2.category.value.lower() == "symptom_evidence"
    assert promoted_2.source_type.value.lower() == "logs"  # Fallback to LOGS

    # Check that duplicate was not added again (only one evidence with this source_file_id should exist)
    duplicates = [e for e in case.evidence if e.source_file_id == "data_1234567890dd"]
    assert len(duplicates) == 1
    assert (
        duplicates[0].evidence_id == "ev_000000000000"
    )  # Should be the pre-existing one


@pytest.mark.asyncio
async def test_jit_evidence_skipped_for_manual_transition():
    """Manual transition (status dropdown) should NOT promote uploaded files to evidence."""
    engine = MilestoneEngine(
        llm_provider=MagicMock(),
        repository=MagicMock(),
    )

    case = Case(
        case_id="case_1234567890ab",
        organization_id="org_1234567890abc",
        title="Manual Transition Test",
        user_id="user_1234567890abc",
        status=CaseStatus.INQUIRY,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        uploaded_files=[
            UploadedFile(
                file_id="file_1234567890ab",
                filename="syslog.txt",
                size_bytes=1000,
                data_type="LOGS",
                uploaded_at_turn=1,
                uploaded_at=datetime.now(UTC),
                source_type="file_upload",
                preprocessing_summary="System logs",
                content_ref="s3://something",
            ),
            UploadedFile(
                file_id="file_abcdef123456",
                filename="metrics.json",
                size_bytes=500,
                data_type="METRICS",
                uploaded_at_turn=3,
                uploaded_at=datetime.now(UTC),
                source_type="file_upload",
                preprocessing_summary="Metric data",
                content_ref="s3://metrics",
            ),
        ],
    )

    # Trigger MANUAL transition
    await engine._transition_to_investigating(case, manual=True)

    # Case should be fully initialized
    assert case.status == CaseStatus.INVESTIGATING
    assert case.progress is not None
    assert case.problem_verification is not None
    assert case.path_selection is not None

    # But NO evidence should have been promoted from uploaded files
    assert len(case.evidence) == 0

    # Uploaded files should still exist (not removed, just not promoted)
    assert len(case.uploaded_files) == 2

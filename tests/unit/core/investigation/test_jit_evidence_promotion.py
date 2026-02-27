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
from faultmaven.modules.case.contracts import UploadedFile


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
        ],
    )

    # Need to fake determine_investigation_path since it does not need deep validation for this
    # Actually _transition_to_investigating uses determine_investigation_path, which needs problem_verification
    # Let's just run it

    # Trigger transition
    await engine._transition_to_investigating(case)

    # Check evidence promotion
    assert case.status == CaseStatus.INVESTIGATING
    assert len(case.evidence) == 1
    assert case.evidence[0].source_file_id == "file_1234567890ab"
    assert case.evidence[0].summary == "System logs"
    assert case.evidence[0].category.value.lower() == "symptom_evidence"
    assert case.evidence[0].source_type.value.lower() == "logs"

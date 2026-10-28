"""Regression tests for deep_analysis file_id dual-resolution (INV-EC-5).

A file uploaded *this turn* has no Evidence row yet (evidence is born reactively
from ``evidence_to_add``). Before this fix ``deep_analysis`` accepted only an
``evidence_id``, so the LLM — wanting to analyze the fresh upload — reused the
nearest existing ``evidence_id`` and analyzed the WRONG (stale) file. The tool
now mirrors ``search_file``'s fallback: an unknown id is resolved against
``uploaded_files`` by ``file_id`` so a just-uploaded file is analyzable.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.case.contracts import (
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from faultmaven.modules.case.domain.models.case import Case
from tests.model_content import disabled_redaction
from tests.unit.modules.agent.tools.leaky_failure import (
    assert_reported_by_class,
    leaky_error,
)


def _case_with_orphan_file() -> Case:
    """Case in INVESTIGATING with a fresh upload and NO Evidence row."""
    return Case(
        case_id="case_aabb11223344",
        title="Test Case",
        description="Test",
        user_id="user_123",
        enterprise_id="org_123",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            proposed_problem_statement="Test",
        ),
        evidence=[],
        uploaded_files=[
            UploadedFile(
                file_id="file_fa0e00000001",
                filename="platform-deploy.yaml",
                size_bytes=512,
                content_type="text/plain",
                uploaded_at_turn=8,
                uploaded_at=datetime.now(UTC),
                uploaded_by="user_123",
                data_type="code",
                storage_ref="org_123/case_aabb11223344/2026-06-18/abc_platform-deploy.yaml",
                structural_index="env:\n  DB_PASSWORD: ${{ secrets.PG_PASSWORD }}",
            )
        ],
    )


def _ctx(case: Case) -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id=case.case_id,
        enterprise_id="org_123",
        user_id="user_123",
        in_memory_case=case,
        redaction_ctx=disabled_redaction(),
    )


def _fake_tier2():
    svc = AsyncMock()
    svc.analyze.return_value = SimpleNamespace(
        answer="The DB password env var is PG_PASSWORD.",
        excerpts=[],
        confidence=0.9,
        backend_used="test-backend",
    )
    return svc


@pytest.mark.asyncio
async def test_deep_analysis_resolves_bare_file_id():
    """A ``file_…`` id for an upload with no Evidence row resolves and analyzes
    that file (INV-EC-5)."""
    tier2 = _fake_tier2()
    tool = DeepAnalysisTool(tier2_service=tier2)
    case = _case_with_orphan_file()

    result = await tool.execute_with_context(
        {
            "evidence_id": "file_fa0e00000001",
            "query": "What env var holds the DB password?",
        },
        _ctx(case),
    )

    assert result.success, result.error
    # The analysis ran against the fresh file's storage_ref, not a stale file.
    tier2.analyze.assert_awaited_once()
    assert (
        tier2.analyze.await_args.kwargs["file_ref"]
        == "org_123/case_aabb11223344/2026-06-18/abc_platform-deploy.yaml"
    )


@pytest.mark.asyncio
async def test_deep_analysis_unknown_id_still_errors():
    """An id that is neither an evidence_id nor a known file_id returns a
    not-found error (no silent success)."""
    tool = DeepAnalysisTool(tier2_service=_fake_tier2())
    case = _case_with_orphan_file()

    result = await tool.execute_with_context(
        {"evidence_id": "file_dead00000001", "query": "anything"},
        _ctx(case),
    )

    assert not result.success
    assert "not found" in (result.error or "").lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored,expected",
    [
        ("structured_config", "configuration"),
        ("logs_and_errors", "logs"),
        ("metrics_and_performance", "metrics"),
        ("configuration", "configuration"),
        ("logs", "logs"),
        ("code", "code"),
    ],
    ids=[
        "datatype_config",
        "datatype_logs",
        "datatype_metrics",
        "legacy_config",
        "legacy_logs",
        "legacy_code",
    ],
)
async def test_deep_analysis_reads_both_stored_vocabularies(stored, expected):
    """#583: the file-id branch hands Tier 2 the file's unified type whichever
    vocabulary the row holds. It mapped the column through a table keyed on
    the 6-valued strings, so a row written as ``structured_config`` — every
    row since #583 — reached Tier 2 as TEXT."""
    from faultmaven.core.preprocessing.models import UnifiedDataType

    tier2 = _fake_tier2()
    tool = DeepAnalysisTool(tier2_service=tier2)
    case = _case_with_orphan_file()
    case.uploaded_files[0].data_type = stored

    result = await tool.execute_with_context(
        {"evidence_id": "file_fa0e00000001", "query": "anything"},
        _ctx(case),
    )

    assert result.success, result.error
    assert tier2.analyze.await_args.kwargs["data_type"] is UnifiedDataType(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored,expected",
    [("logs_and_errors", "logs"), ("structured_config", "configuration")],
    ids=["logs", "config"],
)
async def test_the_evidence_arm_reads_the_files_type_too(stored, expected):
    """#2375 review: the evidence arm mapped the row's ``category`` (symptom,
    causal, ...) through a table keyed on data types, which never matched, so
    every evidence id reached Tier 2 as TEXT. Both arms read the file's type."""
    from faultmaven.core.preprocessing.models import UnifiedDataType

    tier2 = _fake_tier2()
    case = _case_with_orphan_file()
    case.uploaded_files[0].data_type = stored
    case.evidence.append(
        Evidence(
            evidence_id="ev_fa0e00000001",
            source_file_id="file_fa0e00000001",
            summary="s",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            primary_purpose="Test",
            collected_by="user_123",
            collected_at_turn=8,
        )
    )

    result = await DeepAnalysisTool(tier2_service=tier2).execute_with_context(
        {"evidence_id": "ev_fa0e00000001", "query": "anything"}, _ctx(case)
    )

    assert result.success, result.error
    assert tier2.analyze.await_args.kwargs["data_type"] is UnifiedDataType(expected)


@pytest.mark.asyncio
async def test_a_failed_analysis_names_its_class_not_its_text(caplog):
    """#2332: the model reads the class; the server log, the text."""
    tier2 = _fake_tier2()
    tier2.analyze.side_effect = leaky_error()
    case = _case_with_orphan_file()

    result = await DeepAnalysisTool(tier2_service=tier2).execute_with_context(
        {"evidence_id": "file_fa0e00000001", "query": "anything"}, _ctx(case)
    )

    assert result.success is False
    assert_reported_by_class(result.error, caplog.text)

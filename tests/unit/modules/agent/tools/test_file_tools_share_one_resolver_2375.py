"""The file tools resolve an id the model passes in one place (#2375).

``search_file``, ``deep_analysis`` and ``vectorize_file`` each decided alone
which file an id names, so they disagreed: ``deep_analysis`` called an upload
with no stored file "not found in this case", and ``vectorize_file`` called
every upload "Evidence not found". ``resolve_case_id`` decides it once; whether
the file can be read stays each tool's own test, and only an id the case does
not hold is "not found".
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.search_file_tool import (
    IdOutcome,
    SearchFileTool,
    resolve_case_id,
)
from faultmaven.modules.agent.tools.vectorize_file_tool import VectorizeFileTool
from faultmaven.modules.case.contracts import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)
from tests.case_builders import investigating_case
from tests.model_content import disabled_redaction

pytestmark = pytest.mark.unit

UNSTORED = "file_000000002375"  # an upload with no stored file, no row
STORED = "file_000000002376"  # an upload with a stored file, no row
RECORDED = "file_000000002377"  # a stored upload EV_RECORDED records
EV_RECORDED = "ev_000000002377"
EV_INFERENCE = "ev_000000002378"  # no source file
EV_MISSING = "ev_000000002379"  # names an upload the case did not load
UNKNOWN = "file_00000000dead"
READERS = frozenset({"search_file", "deep_analysis"})


def _upload(file_id: str, stored: bool) -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename=f"{file_id}.log",
        size_bytes=100,
        uploaded_at_turn=1,
        storage_ref=f"org/case/{file_id}.log" if stored else None,
        upload_source="file_upload",
        data_type="logs",
        structural_index="2026-10-28 ERROR timeout\n",
    )


def _row(evidence_id: str, source_file_id: str | None) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_file_id=source_file_id,
        summary="s",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=(
            EvidenceSourceType.LOGS
            if source_file_id
            else EvidenceSourceType.ENGINE_INFERENCE
        ),
        primary_purpose="Test",
        collected_by="user_2375",
        collected_at_turn=1,
    )


def _case():
    return investigating_case(
        case_id="case_2375aaaaaaaa",
        user_id="user_2375",
        enterprise_id="org_2375",
        statement="API timeouts",
        uploaded_files=[
            _upload(UNSTORED, stored=False),
            _upload(STORED, stored=True),
            _upload(RECORDED, stored=True),
        ],
        evidence=[
            _row(EV_RECORDED, RECORDED),
            _row(EV_INFERENCE, None),
            _row(EV_MISSING, "file_00000000beef"),
        ],
    )


def _ctx(case, given=frozenset()) -> ToolContext:
    return ToolContext(
        session_id=case.case_id,
        case_id=case.case_id,
        enterprise_id="org_2375",
        user_id="user_2375",
        in_memory_case=case,
        given_tools=given,
        redaction_ctx=disabled_redaction(),
    )


@pytest.mark.parametrize(
    "id_,outcome,row_id,file_id",
    [
        (UNKNOWN, IdOutcome.NOT_FOUND, None, None),
        (EV_INFERENCE, IdOutcome.ENGINE_INFERENCE, EV_INFERENCE, None),
        (EV_MISSING, IdOutcome.MISSING_FILE_RECORD, EV_MISSING, None),
        (EV_RECORDED, IdOutcome.FOUND, EV_RECORDED, RECORDED),
        (UNSTORED, IdOutcome.FOUND, None, UNSTORED),
        (RECORDED, IdOutcome.FOUND, None, RECORDED),
    ],
    ids=["unknown", "inference", "missing_record", "row", "upload", "recorded_upload"],
)
def test_the_resolver_names_each_outcome(id_, outcome, row_id, file_id):
    resolved = resolve_case_id(_case(), id_)

    assert resolved.outcome is outcome
    assert getattr(resolved.row, "evidence_id", None) == row_id
    assert getattr(resolved.file, "file_id", None) == file_id


def _tools():
    storage = AsyncMock()
    storage.retrieve_file = AsyncMock(return_value=b"2026-10-28 ERROR timeout\n")
    tier2 = AsyncMock()
    tier2.analyze.return_value = MagicMock(
        answer="a", excerpts=[], confidence=0.9, backend_used="t"
    )
    return {
        "search_file": SearchFileTool(storage_service=storage),
        "deep_analysis": DeepAnalysisTool(tier2_service=tier2),
        "vectorize_file": VectorizeFileTool(case_vector_store=MagicMock()),
    }


async def _run(tool_name, id_, given=READERS):
    tool = _tools()[tool_name]
    params = {"evidence_id": id_, "query": "ERROR"}
    return tool, await tool.execute_with_context(params, _ctx(_case(), given))


TOOLS = ["search_file", "deep_analysis", "vectorize_file"]


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.parametrize(
    "id_", [UNSTORED, STORED, RECORDED, EV_RECORDED, EV_INFERENCE, EV_MISSING]
)
async def test_no_tool_calls_an_id_the_case_holds_not_found(tool_name, id_):
    _, result = await _run(tool_name, id_)

    assert "not found" not in (result.error or "").lower(), result.error


@pytest.mark.parametrize("tool_name", TOOLS)
async def test_an_id_the_case_does_not_hold_is_not_found(tool_name):
    _, result = await _run(tool_name, UNKNOWN)

    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("search_file", f"Evidence {UNSTORED} has no stored file content."),
        (
            "deep_analysis",
            f"Evidence {UNSTORED} has no file reference for deep analysis.",
        ),
        (
            "vectorize_file",
            f"Upload {UNSTORED} has no evidence row recording it yet, so it "
            "cannot be vectorized.",
        ),
    ],
)
async def test_an_upload_with_no_stored_file_says_so(tool_name, expected):
    """The #2375 case: ``deep_analysis`` and ``vectorize_file`` answered "not
    found". With nothing stored, no reader is named for it."""
    _, result = await _run(tool_name, UNSTORED)

    assert not result.success
    assert result.error.startswith(expected), result.error
    assert "can still read it" not in result.error


@pytest.mark.parametrize("tool_name", ["search_file", "deep_analysis"])
async def test_a_stored_upload_with_no_row_is_read(tool_name):
    _, result = await _run(tool_name, STORED)

    assert result.success, result.error


@pytest.mark.parametrize(
    "given,reader",
    [
        (READERS, " search_file can still read it by this file_id."),
        (frozenset(), ""),
    ],
    ids=["search_file_given", "toolless"],
)
async def test_vectorize_file_explains_an_upload_no_row_records(given, reader):
    """Owner ruling on #2375: explained, not indexed, and ``search_file`` named
    when the turn gives it (#1939)."""
    tool, result = await _run("vectorize_file", STORED, given)

    assert result.error == (
        f"Upload {STORED} has no evidence row recording it yet, so it cannot be "
        f"vectorized.{reader}"
    )
    assert tool.case_vector_store.method_calls == []  # no write path


@pytest.mark.parametrize(
    "id_,expected",
    [
        (
            RECORDED,
            f"Upload {RECORDED} is recorded as evidence {EV_RECORDED}: pass that "
            "evidence_id to vectorize it.",
        ),
        (
            EV_INFERENCE,
            f"Evidence {EV_INFERENCE} has no stored file behind it (it's an "
            "inference the engine recorded) and cannot be vectorized.",
        ),
        (EV_MISSING, f"Evidence {EV_MISSING} has no file reference for vectorization."),
    ],
    ids=["recorded_upload", "inference", "missing_record"],
)
async def test_vectorize_file_names_why_an_id_cannot_be_vectorized(id_, expected):
    tool, result = await _run("vectorize_file", id_)

    assert result.error == expected
    assert tool.case_vector_store.method_calls == []

"""Regression tests for #666 — minted storage filenames must not reach users.

Pasted text and captured pages arrive with no filename, so the turns route
mints one (``pasted-content-<ts>.txt`` / ``page-capture-<ts>.txt``). That name
is storage metadata: the user never typed it, and Beta transcripts showed the
agent citing it back at them ("pasted-content-20260709T105531.txt (line 20)").

The fix is a display/storage split on ``UploadedFile`` — ``filename`` stays the
name it is STORED under, ``display_name`` is the name it is SHOWN under — and
every surface that puts a name in front of the model or the user reads the
latter. These tests walk each of those surfaces with a minted name and assert
the raw token does not survive.

Deliberately NOT asserted here: that ``filename`` itself changes. It must not
— dedup, the storage backend, extension sniffing and the classifier all read
it, and #836 (storage_ref semantics) / #583 (data_type vocabulary) own that
side of the model.
"""

import json
from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.prompts.context_builder import (
    _build_evidence_context,
)
from faultmaven.core.investigation.prompts.templates import (
    _fallback_current_turn_evidence,
)
from faultmaven.core.investigation.turn_pipeline import generate_implicit_query
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.domain.services.investigation_service import (
    _upload_subject,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from faultmaven.modules.case.domain.models import Case

# The exact string from the #656 P1.3 gate-tier transcript quoted in #666.
LEAKED_NAME = "pasted-content-20260709T105531.txt"
CAPTURE_NAME = "page-capture-20260709T105531.txt"

FILE_ID = "file_0e0e0e0e0e05"


def _pasted_file(
    filename: str = LEAKED_NAME,
    upload_source: str = "text_paste",
    data_type: str | None = "logs",
    summary: str | None = "Pod restart loop on user-service. Second sentence.",
) -> UploadedFile:
    return UploadedFile(
        file_id=FILE_ID,
        filename=filename,
        size_bytes=512,
        content_type="text/plain",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(UTC),
        uploaded_by="user_123",
        upload_source=upload_source,
        storage_ref="evidence/case_x/blob.txt",
        data_type=data_type,
        summary=summary,
        structural_index="2026-07-09 10:55:31 ERROR CrashLoopBackOff\nline two\n",
    )


def _real_file() -> UploadedFile:
    return UploadedFile(
        file_id=FILE_ID,
        filename="nginx-error.log",
        size_bytes=512,
        content_type="text/plain",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(UTC),
        uploaded_by="user_123",
        upload_source="file_upload",
        storage_ref="evidence/case_x/blob.log",
        data_type="logs",
        summary="nginx 502s",
        structural_index="ERROR: upstream timed out\n",
    )


def _case(files: list[UploadedFile], evidence: list[Evidence]) -> Case:
    return Case(
        case_id="case_aabb11223344",
        title="Test Case",
        description="Test description",
        user_id="user_123",
        organization_id="org_123",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test description",
        ),
        evidence=evidence,
        uploaded_files=files,
        current_turn=1,
    )


def _evidence(source_file_id: str = FILE_ID) -> Evidence:
    return Evidence(
        evidence_id="ev_000000000001",
        source_file_id=source_file_id,
        summary="Pods are restarting every 40s",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        collected_at=datetime.now(UTC),
        collected_by="user_123",
        collected_at_turn=1,
        primary_purpose="Test",
    )


# ---------------------------------------------------------------------------
# The model-level split
# ---------------------------------------------------------------------------


class TestUploadedFileDisplayName:
    def test_minted_paste_name_is_recognised_and_replaced(self):
        uf = _pasted_file()
        assert uf.has_synthetic_filename is True
        assert uf.display_name == "pasted logs"
        # The stored name is untouched — storage and dedup still need it.
        assert uf.filename == LEAKED_NAME

    def test_minted_capture_name_is_recognised_and_replaced(self):
        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")
        assert uf.has_synthetic_filename is True
        assert uf.display_name == "captured page"

    def test_untagged_legacy_row_is_caught_by_the_name_shape(self):
        """Rows whose upload_source predates the current values still leak
        without the filename-pattern fallback."""
        uf = _pasted_file(upload_source="file_upload")
        assert uf.has_synthetic_filename is True
        assert uf.display_name == "pasted logs"

    def test_unclassified_paste_falls_back_to_data(self):
        uf = _pasted_file(data_type=None)
        assert uf.display_name == "pasted data"

    def test_real_filename_passes_through_untouched(self):
        uf = _real_file()
        assert uf.has_synthetic_filename is False
        assert uf.display_name == "nginx-error.log"

    def test_a_chosen_file_is_not_mistaken_for_a_paste(self):
        """The pattern is anchored on the minted shape, so a user's own file
        that merely starts with the same word keeps its name — and is not
        described back to them as text they pasted."""
        uf = _pasted_file(filename="pasted-notes.txt", upload_source="file_upload")
        assert uf.has_synthetic_filename is False
        assert uf.is_pasted is False
        assert uf.display_name == "pasted-notes.txt"


# ---------------------------------------------------------------------------
# Evidence context — the block the LLM reads on every turn
# ---------------------------------------------------------------------------


class TestEvidenceContextRender:
    def test_evidence_backed_render_does_not_name_the_minted_file(self):
        case = _case([_pasted_file()], [_evidence()])
        result = _build_evidence_context(case)
        assert LEAKED_NAME not in result
        assert "pasted logs" in result
        # No invented filename in its place either: a paste has no filename,
        # so the element carries none.
        ev_line = next(
            line for line in result.splitlines() if "ev_000000000001" in line
        )
        assert "filename=" not in ev_line

    def test_orphan_uploaded_file_render_does_not_name_the_minted_file(self):
        """INQUIRY phase: files exist, no Evidence rows yet."""
        case = _case([_pasted_file()], [])
        result = _build_evidence_context(case)
        assert LEAKED_NAME not in result
        assert 'label="pasted logs"' in result
        assert "filename=" not in result

    def test_page_capture_render_does_not_name_the_minted_file(self):
        case = _case(
            [_pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")],
            [_evidence()],
        )
        result = _build_evidence_context(case)
        assert CAPTURE_NAME not in result
        assert "captured page" in result

    def test_real_filename_still_rendered_as_a_filename(self):
        case = _case([_real_file()], [_evidence()])
        result = _build_evidence_context(case)
        assert 'filename="nginx-error.log"' in result

    def test_tier_b_summary_render_does_not_name_the_minted_file(self):
        """Tier B (over-budget evidence, summary only) is a separate render
        path from Tier A and leaked independently."""
        case = _case([_pasted_file()], [_evidence()])
        # A budget of ~0 forces every item down to the Tier B path.
        result = _build_evidence_context(case, char_budget_override=1)
        assert LEAKED_NAME not in result


class TestDegradedFallbackRender:
    def test_current_turn_upload_stub_does_not_name_the_minted_file(self):
        """The tightest-budget fallback is a third render path (templates.py),
        reached exactly when a fresh upload must not be dropped."""
        case = _case([_pasted_file()], [])
        stub = _fallback_current_turn_evidence(case)
        assert stub  # the file is present at all
        assert LEAKED_NAME not in stub
        assert 'label="pasted logs"' in stub

    def test_real_filename_survives_the_fallback(self):
        case = _case([_real_file()], [])
        stub = _fallback_current_turn_evidence(case)
        assert 'filename="nginx-error.log"' in stub


# ---------------------------------------------------------------------------
# The implicit query — written in the user's voice and shown back to them
# ---------------------------------------------------------------------------


class TestImplicitQuery:
    def test_single_paste_is_not_described_by_its_minted_name(self):
        uf = _pasted_file()
        query = generate_implicit_query([], [uf])
        assert LEAKED_NAME not in query
        assert "I've submitted pasted logs." in query

    def test_single_real_file_keeps_its_name_and_classification(self):
        uf = _real_file()
        query = generate_implicit_query([], [uf])
        assert "nginx-error.log" in query
        assert "classified as logs" in query

    def test_mixed_submission_does_not_name_the_minted_file(self):
        query = generate_implicit_query([], [_real_file(), _pasted_file()])
        assert LEAKED_NAME not in query
        assert "nginx-error.log" in query
        assert "pasted logs" in query


# ---------------------------------------------------------------------------
# search_file citation guidance — the mechanism behind the observed symptom
# ---------------------------------------------------------------------------


class TestSearchFileCitationGuidance:
    """#666's transcript showed "<name> (line 20)". The engine appends an
    instruction telling the model to cite the name in ``result.data`` with
    line numbers, so whatever sits under that key is what gets quoted."""

    def _format(self, filename: str) -> str:
        result = ToolResult(
            success=True,
            data={
                "evidence_id": "ev_000000000001",
                "filename": filename,
                "results_count": 2,
                "results": [{"line": 20, "content": "ERROR CrashLoopBackOff"}],
            },
        )
        return MilestoneEngine._format_tool_result(result, "search_file")

    def test_guidance_quotes_whatever_the_tool_reported(self):
        content = self._format("pasted logs")
        assert "CITATION" in content
        assert "In pasted logs, line 42" in content

    def test_tool_reports_display_name_so_guidance_cannot_leak(self):
        """Belt: the tool supplies display_name under that key (covered in
        the tool's own tests); braces: even handed the raw name, the guidance
        must not be the thing that invents a filename."""
        content = self._format(LEAKED_NAME)
        # Not a claim that this is safe — it documents that the engine is a
        # pass-through, which is why the fix lives in the tool.
        assert LEAKED_NAME in content
        assert json.loads(content.split("\n\nCITATION:")[0])["filename"] == LEAKED_NAME


# ---------------------------------------------------------------------------
# Agent copy emitted directly to the user (no LLM in the loop)
# ---------------------------------------------------------------------------


class TestReportCitation:
    """The terminal report is read by the person who filed the case."""

    def test_citation_names_the_paste_not_its_storage_key(self):
        from faultmaven.modules.report.domain.services.report_generation_service import (  # noqa: E501
            ReportGenerationService,
        )

        case = _case([_pasted_file()], [_evidence()])
        line = ReportGenerationService()._evidence_citation_line(case, case.evidence[0])
        assert LEAKED_NAME not in line
        assert "_pasted logs_" in line

    def test_citation_keeps_a_real_filename(self):
        from faultmaven.modules.report.domain.services.report_generation_service import (  # noqa: E501
            ReportGenerationService,
        )

        case = _case([_real_file()], [_evidence()])
        line = ReportGenerationService()._evidence_citation_line(case, case.evidence[0])
        assert "_nginx-error.log_" in line


class TestAgentCopySubject:
    def test_paste_is_named_by_how_it_arrived(self):
        assert _upload_subject(_pasted_file()) == "the text you pasted"

    def test_capture_is_named_by_how_it_arrived(self):
        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")
        assert _upload_subject(uf) == "the page you captured"

    def test_real_file_is_quoted_by_name(self):
        assert _upload_subject(_real_file()) == '"nginx-error.log"'

    def test_missing_file_row_degrades_without_naming_anything(self):
        assert _upload_subject(None) == "the uploaded file"


# ---------------------------------------------------------------------------
# Whole-context sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("minted", [LEAKED_NAME, CAPTURE_NAME])
def test_no_render_path_emits_a_minted_name(minted):
    """One assertion over every prompt surface a paste passes through, so a
    new render path added to any of them fails here rather than in a Beta
    transcript."""
    from faultmaven.modules.report.domain.services.report_generation_service import (
        ReportGenerationService,
    )

    source = "page_capture" if minted.startswith("page-capture-") else "text_paste"
    uf = _pasted_file(filename=minted, upload_source=source)
    ev_case = _case([uf], [_evidence()])

    surfaces = [
        _build_evidence_context(ev_case),
        _build_evidence_context(_case([uf], [])),
        _build_evidence_context(_case([uf], [_evidence()]), char_budget_override=1),
        _fallback_current_turn_evidence(_case([uf], [])),
        generate_implicit_query([], [uf]),
        _upload_subject(uf),
        ReportGenerationService()._evidence_citation_line(ev_case, ev_case.evidence[0]),
    ]
    for rendered in surfaces:
        assert minted not in rendered, rendered

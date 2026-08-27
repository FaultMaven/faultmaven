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

Two properties of the substitute name are load-bearing and are pinned here
separately from the leak itself, because the first fix got a name that had
neither (#1198 review):

- **unique** within the case — citation is the point, and "In pasted logs,
  line 42" has to designate one item;
- **stable** across the case's life — ``data_type`` and ``summary`` are both
  rewritten by reclassification, so a name derived from either renames the
  item mid-case and the transcript's own back-references stop resolving.

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
    _clarification_subject,
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
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

# The exact string from the #656 P1.3 gate-tier transcript quoted in #666.
LEAKED_NAME = "pasted-content-20260709T105531.txt"
CAPTURE_NAME = "page-capture-20260709T105531.txt"

FILE_ID = "file_0e0e0e0e0e05"


def _pasted_file(
    filename: str = LEAKED_NAME,
    upload_source: str = "text_paste",
    data_type: str | None = "logs",
    summary: str | None = "Pod restart loop on user-service. Second sentence.",
    file_id: str = FILE_ID,
    turn: int = 1,
) -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename=filename,
        size_bytes=512,
        content_type="text/plain",
        uploaded_at_turn=turn,
        uploaded_at=datetime.now(UTC),
        uploaded_by="user_123",
        upload_source=upload_source,
        storage_ref="evidence/case_x/blob.txt",
        data_type=data_type,
        summary=summary,
        structural_index="2026-07-09 10:55:31 ERROR CrashLoopBackOff\nline two\n",
    )


def _real_file(file_id: str = FILE_ID, turn: int = 1) -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename="nginx-error.log",
        size_bytes=512,
        content_type="text/plain",
        uploaded_at_turn=turn,
        uploaded_at=datetime.now(UTC),
        uploaded_by="user_123",
        upload_source="file_upload",
        storage_ref="evidence/case_x/blob.log",
        data_type="logs",
        summary="nginx 502s",
        structural_index="ERROR: upstream timed out\n",
    )


def _case(files: list[UploadedFile], evidence: list[Evidence], turn: int = 1) -> Case:
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
        current_turn=turn,
    )


def _evidence(
    source_file_id: str = FILE_ID,
    evidence_id: str = "ev_000000000001",
    turn: int = 1,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_file_id=source_file_id,
        summary="Pods are restarting every 40s",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        collected_at=datetime.now(UTC),
        collected_by="user_123",
        collected_at_turn=turn,
        primary_purpose="Test",
    )


class _FakeTarget:
    """Stand-in for ``_PreprocessedAttachment`` — the two fields the
    clarification copy reads."""

    def __init__(self, uf: UploadedFile):
        self.uploaded_file = uf
        self.attachment_filename = uf.filename


# ---------------------------------------------------------------------------
# The model-level split
# ---------------------------------------------------------------------------


class TestUploadedFileDisplayName:
    def test_minted_paste_name_is_recognised_and_replaced(self):
        uf = _pasted_file(turn=3)
        assert uf.has_synthetic_filename is True
        assert uf.display_name == "pasted text (turn 3)"
        # The stored name is untouched — storage and dedup still need it.
        assert uf.filename == LEAKED_NAME

    def test_minted_capture_name_is_recognised_and_replaced(self):
        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="page_capture", turn=2)
        assert uf.has_synthetic_filename is True
        assert uf.display_name == "captured page (turn 2)"

    def test_untagged_legacy_row_is_caught_by_the_name_shape(self):
        """Rows whose upload_source predates the current values still leak
        without the filename-pattern fallback."""
        uf = _pasted_file(upload_source="file_upload", turn=4)
        assert uf.has_synthetic_filename is True
        assert uf.display_name == "pasted text (turn 4)"

    def test_real_filename_passes_through_untouched(self):
        uf = _real_file()
        assert uf.has_synthetic_filename is False
        assert uf.display_name == "nginx-error.log"

    @pytest.mark.parametrize(
        "chosen_name",
        [
            "pasted-notes.txt",
            # Shares the WHOLE minted prefix and differs only after it. This
            # is what anchoring on the full shape buys over a startswith()
            # check — the detection this replaced used the prefix.
            "pasted-content-notes.txt",
            "pasted-content-20260709T105531.log",
            "page-capture-summary.txt",
        ],
    )
    def test_a_chosen_file_is_not_mistaken_for_a_paste(self, chosen_name):
        """The pattern is anchored on the minted shape — prefix, timestamp
        and extension — so a file the user named keeps its own name and is
        not described back to them as text they pasted."""
        uf = _pasted_file(filename=chosen_name, upload_source="file_upload")
        assert uf.has_synthetic_filename is False
        assert uf.is_pasted is False
        assert uf.is_page_capture is False
        assert uf.display_name == chosen_name
        assert uf.submission_phrase is None


class TestCitedNameIsUnique:
    """A cited name has to designate ONE item. The first fix derived it from
    ``data_type``, which does not: two pastes typed the same collided, and
    every capture in every case was "captured page" (#1198 review)."""

    def test_two_pastes_of_the_same_data_type_get_different_names(self):
        a = _pasted_file(file_id="file_0a0a0a0a0a01", turn=1, data_type="logs")
        b = _pasted_file(file_id="file_0b0b0b0b0b02", turn=2, data_type="logs")
        assert a.display_name != b.display_name

    def test_two_captures_get_different_names(self):
        a = _pasted_file(
            filename=CAPTURE_NAME,
            upload_source="page_capture",
            file_id="file_0a0a0a0a0a01",
            turn=1,
        )
        b = _pasted_file(
            filename=CAPTURE_NAME,
            upload_source="page_capture",
            file_id="file_0b0b0b0b0b02",
            turn=5,
        )
        assert a.display_name != b.display_name

    def test_two_pastes_render_as_distinguishable_evidence(self):
        """End to end: the model must be able to tell which one a citation
        means, in the block it actually reads."""
        a = _pasted_file(file_id="file_0a0a0a0a0a01", turn=1)
        b = _pasted_file(file_id="file_0b0b0b0b0b02", turn=2)
        case = _case(
            [a, b],
            [
                _evidence("file_0a0a0a0a0a01", "ev_000000000001", turn=1),
                _evidence("file_0b0b0b0b0b02", "ev_000000000002", turn=2),
            ],
            turn=2,
        )
        result = _build_evidence_context(case)
        assert 'label="pasted text (turn 1)"' in result
        assert 'label="pasted text (turn 2)"' in result

    def test_two_current_turn_pastes_stub_distinguishably(self):
        """The tightest-budget fallback is where a fresh upload must not be
        lost — two byte-identical stubs lose one of them."""
        a = _pasted_file(file_id="file_0a0a0a0a0a01", turn=2)
        b = _pasted_file(file_id="file_0b0b0b0b0b02", turn=2)
        # Same turn is not reachable through the route (one pasted_content
        # field per turn); this asserts the render does not collapse them if
        # it ever becomes reachable.
        stub = _fallback_current_turn_evidence(_case([a, b], [], turn=2))
        assert stub.count("<uploaded_file") == 2
        assert 'file_id="file_0a0a0a0a0a01"' in stub
        assert 'file_id="file_0b0b0b0b0b02"' in stub


class TestCitedNameIsStable:
    """Reclassification rewrites ``data_type`` and ``summary``. A name built
    from either moves under the model mid-case, so a citation from turn 3
    names nothing that is in context on turn 4 (#1198 review)."""

    def test_reclassification_does_not_rename_the_item(self):
        before = _pasted_file(data_type="logs", turn=3)
        # What _handle_file_reclassification does: same row, new data_type,
        # new summary.
        after = before.model_copy(
            update={
                "data_type": "command_output",
                "summary": "kubectl get pods output.",
            }
        )
        assert before.display_name == after.display_name == "pasted text (turn 3)"

    def test_reclassification_does_not_rename_the_rendered_label(self):
        before = _pasted_file(data_type="logs", turn=3)
        after = before.model_copy(update={"data_type": "command_output"})
        ev = _evidence(turn=3)
        label_before = _build_evidence_context(_case([before], [ev], turn=3))
        label_after = _build_evidence_context(_case([after], [ev], turn=3))
        assert 'label="pasted text (turn 3)"' in label_before
        assert 'label="pasted text (turn 3)"' in label_after

    def test_an_unclassified_paste_is_still_named(self):
        """A paste that never got a data_type has to be citable too — the
        old name degraded to 'pasted data' for every one of them."""
        uf = _pasted_file(data_type=None, turn=7)
        assert uf.display_name == "pasted text (turn 7)"


# ---------------------------------------------------------------------------
# Evidence context — the block the LLM reads on every turn
# ---------------------------------------------------------------------------


class TestEvidenceContextRender:
    def test_evidence_backed_render_does_not_name_the_minted_file(self):
        case = _case([_pasted_file()], [_evidence()])
        result = _build_evidence_context(case)
        assert LEAKED_NAME not in result
        assert 'label="pasted text (turn 1)"' in result
        # No invented filename in its place either, and no filename
        # attribute at all — the model is told to cite the label, so a
        # second name slot is exactly what it must not find.
        assert "filename=" not in result

    def test_orphan_uploaded_file_render_does_not_name_the_minted_file(self):
        """INQUIRY phase: files exist, no Evidence rows yet."""
        case = _case([_pasted_file()], [])
        result = _build_evidence_context(case)
        assert LEAKED_NAME not in result
        assert 'label="pasted text (turn 1)"' in result
        assert "filename=" not in result

    def test_page_capture_render_does_not_name_the_minted_file(self):
        case = _case(
            [_pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")],
            [_evidence()],
        )
        result = _build_evidence_context(case)
        assert CAPTURE_NAME not in result
        assert 'label="captured page (turn 1)"' in result

    def test_real_filename_is_the_label(self):
        case = _case([_real_file()], [_evidence()])
        result = _build_evidence_context(case)
        assert 'label="nginx-error.log"' in result

    def test_tier_b_summary_render_does_not_name_the_minted_file(self):
        """Tier B (over-budget evidence, summary only) is a separate render
        path from Tier A and leaked independently."""
        case = _case([_pasted_file()], [_evidence()])
        # A budget of ~0 forces every item down to the Tier B path.
        result = _build_evidence_context(case, char_budget_override=1)
        assert LEAKED_NAME not in result

    def test_label_and_source_line_agree(self):
        """One resolver, one answer. The label and the ``[Source: …]`` line
        inside the extract came from two different lookups that disagreed
        when the engine's duplicate row was in play (#1198 review)."""
        uf = _pasted_file(turn=1)
        # The duplicate milestone_engine appends: same file_id, no data_type,
        # no summary, appended AFTER the real row.
        dup = UploadedFile(
            file_id=FILE_ID,
            filename=LEAKED_NAME,
            size_bytes=512,
            uploaded_at_turn=1,
            upload_source="paste",
            data_type=None,
            summary=None,
        )
        case = _case([uf, dup], [_evidence()])
        result = _build_evidence_context(case)
        assert 'label="pasted text (turn 1)"' in result
        assert "[Source: pasted text (turn 1)]" in result


class TestDegradedFallbackRender:
    def test_current_turn_upload_stub_does_not_name_the_minted_file(self):
        """The tightest-budget fallback is a third render path (templates.py),
        reached exactly when a fresh upload must not be dropped."""
        case = _case([_pasted_file()], [])
        stub = _fallback_current_turn_evidence(case)
        assert stub  # the file is present at all
        assert LEAKED_NAME not in stub
        assert 'label="pasted text (turn 1)"' in stub

    def test_real_filename_survives_the_fallback(self):
        case = _case([_real_file()], [])
        stub = _fallback_current_turn_evidence(case)
        assert 'label="nginx-error.log"' in stub

    def test_engine_duplicate_row_is_not_stubbed_twice(self):
        """Unlike the evidence-context renders, this one has no
        structural_index filter to hide the engine's duplicate append, so it
        stubbed the same upload twice (#1198 review)."""
        uf = _pasted_file(turn=1)
        dup = UploadedFile(
            file_id=FILE_ID,
            filename=LEAKED_NAME,
            size_bytes=512,
            uploaded_at_turn=1,
            upload_source="paste",
        )
        stub = _fallback_current_turn_evidence(_case([uf, dup], [], turn=1))
        assert stub.count("<uploaded_file") == 1


# ---------------------------------------------------------------------------
# The implicit query — written in the user's voice and shown back to them
# ---------------------------------------------------------------------------


class TestImplicitQuery:
    def test_single_paste_is_described_as_something_the_user_did(self):
        query = generate_implicit_query([_pasted_file()])
        assert LEAKED_NAME not in query
        assert "I've pasted some text" in query
        assert "classified as logs" in query

    def test_single_capture_is_described_as_something_the_user_did(self):
        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")
        query = generate_implicit_query([uf])
        assert CAPTURE_NAME not in query
        assert "I've captured a page" in query

    def test_single_real_file_keeps_its_name_and_classification(self):
        query = generate_implicit_query([_real_file()])
        assert "nginx-error.log" in query
        assert "classified as logs" in query

    def test_mixed_submission_does_not_name_the_minted_file(self):
        query = generate_implicit_query(
            [_real_file(file_id="file_0a0a0a0a0a01"), _pasted_file()]
        )
        assert LEAKED_NAME not in query
        assert "nginx-error.log" in query
        assert "the text you pasted" in query


# ---------------------------------------------------------------------------
# What the model is INSTRUCTED to cite
# ---------------------------------------------------------------------------


class TestCitationInstructions:
    """#666's transcript showed "<name> (line 20)". The engine both appends a
    per-result citation instruction AND carries a standing one in the DA
    system prompt; the standing one still commanded filename citation after
    the first fix, which for a paste means citing a name that is not in
    context at all (#1198 review)."""

    def _da_instruction(self) -> str:
        return MilestoneEngine._build_da_system_instruction(
            ["search_file", "deep_analysis", "kb_qa"], "record_investigation"
        )

    def test_standing_instruction_does_not_command_filename_citation(self):
        text = self._da_instruction()
        assert "cite the filename" not in text
        assert "Reference evidence by filename" not in text
        assert "evidence id and filename" not in text

    def test_standing_instruction_points_at_the_attribute_that_exists(self):
        text = self._da_instruction()
        assert "label attribute" in text
        assert "cite the label and line numbers" in text
        # And warns off the failure mode that dropping `filename` could
        # otherwise invite.
        assert "Never invent a filename" in text

    def test_per_result_guidance_quotes_the_label_the_tool_reported(self):
        result = ToolResult(
            success=True,
            data={
                "evidence_id": "ev_000000000001",
                "label": "pasted text (turn 3)",
                "results_count": 2,
                "results": [{"line": 20, "content": "ERROR CrashLoopBackOff"}],
            },
        )
        content = MilestoneEngine._format_tool_result(result, "search_file")
        assert "CITATION" in content
        assert "In pasted text (turn 3), line 42" in content

    def test_per_result_guidance_reads_the_same_key_the_tools_write(self):
        """The engine and the tools have to agree on the key, or the
        instruction quotes the literal string "unknown"."""
        result = ToolResult(
            success=True,
            data={"label": "app.log", "results_count": 1, "results": []},
        )
        content = MilestoneEngine._format_tool_result(result, "search_file")
        assert "unknown" not in content
        assert json.loads(content.split("\n\nCITATION:")[0])["label"] == "app.log"


# ---------------------------------------------------------------------------
# Report citations
# ---------------------------------------------------------------------------


class TestReportCitation:
    """The terminal report is read by the person who filed the case."""

    def test_citation_names_the_paste_not_its_storage_key(self):
        case = _case([_pasted_file()], [_evidence()])
        line = ReportGenerationService()._evidence_citation_line(case, case.evidence[0])
        assert LEAKED_NAME not in line
        assert "_pasted text (turn 1)_" in line

    def test_citation_keeps_a_real_filename(self):
        case = _case([_real_file()], [_evidence()])
        line = ReportGenerationService()._evidence_citation_line(case, case.evidence[0])
        assert "_nginx-error.log_" in line


# ---------------------------------------------------------------------------
# Agent copy emitted directly to the user (no LLM in the loop)
# ---------------------------------------------------------------------------


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


class TestClarificationSubject:
    """The classification-failed card. Reached BEFORE the page_capture
    passthrough in ``classify_and_extract``, so a capture the classifier is
    unsure about lands here — and read
    ``the file you shared ("page-capture-…txt")`` until #1198's review."""

    def test_paste_is_named_by_how_it_arrived(self):
        subject = _clarification_subject(_FakeTarget(_pasted_file()))
        assert subject == "the text you pasted"
        assert LEAKED_NAME not in subject

    def test_capture_is_named_by_how_it_arrived(self):
        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")
        subject = _clarification_subject(_FakeTarget(uf))
        assert subject == "the page you captured"
        assert CAPTURE_NAME not in subject

    def test_untagged_capture_is_caught_by_the_name_shape(self):
        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="file_upload")
        assert _clarification_subject(_FakeTarget(uf)) == "the page you captured"

    def test_real_file_keeps_its_name(self):
        subject = _clarification_subject(_FakeTarget(_real_file()))
        assert subject == 'the file you shared ("nginx-error.log")'


class TestClarificationSeedsStayPasteOnly:
    """``_is_paste_upload`` also picks the clarification CHOICES, seeded with
    command-output/logs — a prior about war-room pastes that would be wrong
    for a captured web page. It stays paste-only while the copy covers both."""

    def test_capture_is_not_treated_as_a_paste_for_seeding(self):
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _is_paste_upload,
        )

        uf = _pasted_file(filename=CAPTURE_NAME, upload_source="page_capture")
        assert _is_paste_upload(_FakeTarget(uf)) is False
        assert _is_paste_upload(_FakeTarget(_pasted_file())) is True


# ---------------------------------------------------------------------------
# Whole-context sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("minted", [LEAKED_NAME, CAPTURE_NAME])
def test_no_render_path_emits_a_minted_name(minted):
    """One assertion over every prompt and copy surface a paste passes
    through, so a new render path added to any of them fails here rather
    than in a Beta transcript."""
    source = "page_capture" if minted.startswith("page-capture-") else "text_paste"
    uf = _pasted_file(filename=minted, upload_source=source)
    ev_case = _case([uf], [_evidence()])

    surfaces = [
        _build_evidence_context(ev_case),
        _build_evidence_context(_case([uf], [])),
        _build_evidence_context(_case([uf], [_evidence()]), char_budget_override=1),
        _fallback_current_turn_evidence(_case([uf], [])),
        generate_implicit_query([uf]),
        _upload_subject(uf),
        _clarification_subject(_FakeTarget(uf)),
        ReportGenerationService()._evidence_citation_line(ev_case, ev_case.evidence[0]),
    ]
    for rendered in surfaces:
        assert minted not in rendered, rendered

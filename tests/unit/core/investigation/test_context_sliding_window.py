"""Unit tests for the Context Sliding Window evidence context builder.

Tests the three-tier evidence context system that includes structural indexes
in LLM prompts for recent evidence, fixing the "I don't have access to file
content" bug.

Tier A: file-backed evidence (source_file_id IS NOT NULL)
        → include preprocessed_content (structural index), capped per item.
Tier B: Older data evidence → summary only.
Tier C: USER_TEXT evidence → summary only, always.

Design Reference:
- docs/working/IMPLEMENTATION-unified-ingestion-pipeline.md (Phase 6.3)
- docs/architecture/data-processing/data-preprocessing-design-specification.md (Section 6)
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM,
    EVIDENCE_CONTEXT_MAX_TOTAL_CHARS,
    EVIDENCE_CONTEXT_RECENT_COUNT,
    _build_evidence_context,
    _rerank_page_capture_sections,
)
from faultmaven.modules.case.contracts import (
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from faultmaven.modules.case.domain.models import Case

# ============================================================
# Helpers
# ============================================================


_EV_COUNTER = 0


def _next_ev_id() -> str:
    """Generate a unique evidence ID matching the ^ev_[a-f0-9]{12}$ pattern."""
    global _EV_COUNTER
    _EV_COUNTER += 1
    return f"ev_{_EV_COUNTER:012x}"


def _make_evidence(
    evidence_id: str | None = None,
    summary: str = "Test evidence summary",
    extract: str | None = "Structural index content",
    verbatim_quote: str | None = None,
    source_type: EvidenceSourceType = EvidenceSourceType.LOGS,
    source_file_id: str | None = "file_aabb12345678",
    **overrides,
) -> Evidence:
    """Create an Evidence instance for context builder tests.

    Post-010 NOTE on the parameter split:
    - ``extract`` is the structural-index content that production routes
      to ``uploaded_files.structural_index``. ``_make_case_with_evidence``
      walks the synthesized fixture and mirrors the value onto the
      backing UploadedFile row.
    - ``verbatim_quote`` is the LLM's claim-supporting snippet and lands
      on the Evidence row's own ``extract`` field (which is what
      ``<verbatim_quote>`` renders from).
    Most context-builder tests exercise the structural-index render
    path; tests that exercise the verbatim-quote slot pass
    ``verbatim_quote=`` explicitly.
    """
    # The structural-index content gets routed to uploaded_file by the
    # case-fixture helper; the Evidence row's own ``extract`` slot
    # carries the LLM's verbatim quote (or None when the test omits it).
    defaults = {
        "evidence_id": evidence_id or _next_ev_id(),
        "source_file_id": source_file_id,
        "summary": summary,
        "extract": verbatim_quote,
        "category": EvidenceCategory.SYMPTOM_EVIDENCE,
        "source_type": source_type,
        "collected_at": datetime.now(UTC),
        "collected_by": "user_123",
        "collected_at_turn": 1,
        "primary_purpose": "Test",
    }
    defaults.update(overrides)
    ev = Evidence(**defaults)
    # Stash the test-supplied structural-index content on the instance so
    # _make_case_with_evidence can route it to the backing UploadedFile.
    ev.__pydantic_extra__ = ev.__pydantic_extra__ or {}
    ev.__test_structural_index__ = extract if extract else None  # type: ignore[attr-defined]
    return ev


def _make_case_with_evidence(evidence_list: list) -> Case:
    """Create a minimal Case with given evidence list.

    Post-010: auto-creates the backing UploadedFile rows so the
    context builder's ``file_lookup`` resolves and the ``file_id``
    attribute renders. Each unique ``ev.source_file_id`` gets one
    synthesized UploadedFile, with ``structural_index`` populated from
    the test fixture's ``extract`` parameter (which production routes
    to the file row, not the evidence row).
    """
    # Group test-supplied structural-index payloads by source_file_id so we
    # can write them to the corresponding UploadedFile rows below.
    structural_by_file: dict[str, str] = {}
    for ev in evidence_list:
        fid = getattr(ev, "source_file_id", None)
        payload = getattr(ev, "__test_structural_index__", None)
        if fid and payload and fid not in structural_by_file:
            structural_by_file[fid] = payload

    seen_file_ids: set[str] = set()
    uploaded_files: list = []
    for ev in evidence_list:
        fid = getattr(ev, "source_file_id", None)
        if fid and fid not in seen_file_ids:
            seen_file_ids.add(fid)
            uploaded_files.append(
                UploadedFile(
                    file_id=fid,
                    filename=(
                        f"{ev.source_type.value}.log"
                        if getattr(ev, "source_type", None)
                        else "data.txt"
                    ),
                    size_bytes=128,
                    content_type="text/plain",
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(UTC),
                    uploaded_by="user_123",
                    structural_index=structural_by_file.get(fid),
                )
            )
    return Case(
        case_id="case_aabb11223344",
        title="Test Case",
        description="Test description",
        user_id="user_123",
        organization_id="org_123",
        status=CaseStatus.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test description",
        ),
        evidence=evidence_list,
        uploaded_files=uploaded_files,
    )


# ============================================================
# Configuration Constants
# ============================================================


class TestSlidingWindowConfig:
    """Test configuration constants exist and have reasonable defaults."""

    def test_recent_count_default(self):
        """EVIDENCE_CONTEXT_RECENT_COUNT defaults to 3."""
        assert EVIDENCE_CONTEXT_RECENT_COUNT == 3

    def test_max_chars_per_item_default(self):
        """EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM defaults to 4000."""
        assert EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM == 4000

    def test_max_total_chars_default(self):
        """EVIDENCE_CONTEXT_MAX_TOTAL_CHARS defaults to 16000."""
        assert EVIDENCE_CONTEXT_MAX_TOTAL_CHARS == 16000


# ============================================================
# Empty / No Evidence
# ============================================================


class TestNoEvidence:
    """Test evidence context with no evidence."""

    def test_no_evidence_shows_placeholder(self):
        """0 evidence items → 'No formal evidence collected yet.'"""
        case = _make_case_with_evidence([])
        result = _build_evidence_context(case)
        assert "No formal evidence collected yet." in result
        assert "<evidence_collected>" in result
        assert "</evidence_collected>" in result


# ============================================================
# INQUIRY: uploaded_files present, no Evidence rows yet
# ============================================================


def _make_inquiry_case_with_uploaded_files(
    uploaded_files: list[UploadedFile],
) -> Case:
    """Build an INQUIRY-phase case carrying uploaded_files but no Evidence.

    Mirrors the post-010 reality: during INQUIRY the LLM extracts files
    into ``uploaded_files`` with preprocessing artifacts; no Evidence row
    exists until the case transitions to INVESTIGATING.
    """
    return Case(
        case_id="case_aabb11223344",
        title="INQUIRY Test Case",
        description="Test description",
        user_id="user_123",
        organization_id="org_123",
        status=CaseStatus.INQUIRY,
        inquiry=InquiryData(),
        evidence=[],
        uploaded_files=uploaded_files,
    )


class TestInquiryUploadedFilesBlock:
    """Cover the INQUIRY fallback path in ``_build_evidence_context``.

    Pre-fix the empty-evidence branch always emitted the
    "No formal evidence collected yet" placeholder even when uploaded_files
    carried a usable structural_index. The fallback now surfaces the
    file under ``<uploaded_file file_id="...">`` so the LLM can read the
    extract on turn 1.
    """

    def _file(self, file_id: str = "file_aabbccdd1122", **overrides) -> UploadedFile:
        defaults = dict(
            file_id=file_id,
            filename="app.log",
            size_bytes=1024,
            content_type="text/plain",
            uploaded_at_turn=1,
            uploaded_at=datetime.now(UTC),
            uploaded_by="user_123",
            data_type="logs",
            structural_index=(
                '{"v":1,"file_extract":"ERROR: OOM at 14:03",'
                '"search_map":"[search: OOM] 142 matches",'
                '"file_meta":{"line_count":2048,"top_error":"OOM"}}'
            ),
        )
        defaults.update(overrides)
        return UploadedFile(**defaults)

    def test_emits_uploaded_file_block_with_file_id_attribute(self):
        """uploaded_file element exposes file_id (not evidence_id) to match the
        <evidence file_id="..."> convention used during INVESTIGATING."""
        case = _make_inquiry_case_with_uploaded_files([self._file()])
        result = _build_evidence_context(case)

        assert "<uploaded_file" in result
        assert 'file_id="file_aabbccdd1122"' in result
        assert 'evidence_id="' not in result
        assert "<file_extract>" in result
        assert "ERROR: OOM at 14:03" in result
        # search_map and file_meta also surface so the agent can plan
        # search_file queries from the preprocessed hints.
        assert "<search_map>" in result
        assert "[search: OOM]" in result
        assert "<file_meta>" in result
        assert "line_count=2048" in result

    def test_marks_uploaded_file_searchable(self):
        """The block declares searchable=\"true\" so the LLM knows it can
        pass the file_id to search_file."""
        case = _make_inquiry_case_with_uploaded_files([self._file()])
        result = _build_evidence_context(case)
        assert 'searchable="true"' in result

    def test_truncates_long_extract_with_file_id_pointer(self):
        """Long file_extract → truncation note mentions file_id (not the
        legacy evidence_id wording), pointing the LLM at search_file."""
        long_extract = "X" * (EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM + 5000)
        index_blob = (
            '{"v":1,"file_extract":"'
            + long_extract
            + '","search_map":null,"file_meta":{}}'
        )
        case = _make_inquiry_case_with_uploaded_files(
            [self._file(structural_index=index_blob)]
        )
        result = _build_evidence_context(case)

        assert "[TRUNCATED:" in result
        assert "file_id" in result
        # Old phrasing must not regress.
        assert "evidence_id above" not in result

    def test_skips_files_without_structural_index(self):
        """Files with empty / missing structural_index do not produce a block.
        When no file qualifies, fall through to the empty placeholder."""
        bare = self._file(file_id="file_111111111111", structural_index=None)
        case = _make_inquiry_case_with_uploaded_files([bare])
        result = _build_evidence_context(case)
        assert "No formal evidence collected yet." in result
        assert "<uploaded_file" not in result

    def test_skips_files_with_trivial_structural_index(self):
        """structural_index shorter than the >10-char threshold is treated as
        absent (defensive against extractor stubs that emit '{}' or similar)."""
        stub = self._file(file_id="file_222222222222", structural_index="{}")
        case = _make_inquiry_case_with_uploaded_files([stub])
        result = _build_evidence_context(case)
        assert "<uploaded_file" not in result

    def test_renders_multiple_files(self):
        """Multiple qualifying files all surface — INQUIRY may have several
        uploads on turn 1 (e.g., logs + a config dump)."""
        files = [
            self._file(file_id="file_aaaaaaaaaaaa", filename="a.log"),
            self._file(file_id="file_bbbbbbbbbbbb", filename="b.log"),
        ]
        case = _make_inquiry_case_with_uploaded_files(files)
        result = _build_evidence_context(case)
        assert 'file_id="file_aaaaaaaaaaaa"' in result
        assert 'file_id="file_bbbbbbbbbbbb"' in result
        assert result.count("<uploaded_file") == 2


# ============================================================
# Tier A: Recent Data Evidence
# ============================================================


class TestTierA:
    """Test Tier A — recent data evidence with full structural_index."""

    def test_single_recent_document_includes_structural_index(self):
        """1 recent DOCUMENT evidence → Tier A with full structural_index."""
        ev = _make_evidence(
            summary="Error burst detected in application logs",
            extract="============\nCRIME SCENE EXTRACTION\n============\nERROR: Connection timeout at 14:03:21",
            source_type=EvidenceSourceType.LOGS,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert f'id="{ev.evidence_id}"' in result
        assert 'file_id="' in result
        assert 'data_type="logs"' in result
        assert "<file_extract>" in result
        assert "CRIME SCENE EXTRACTION" in result
        assert "<summary>" in result

    def test_recent_submitted_data_is_tier_a(self):
        """SUBMITTED_DATA form is also treated as Tier A (data evidence)."""
        ev = _make_evidence(
            summary="Search result finding",
            extract="Matched lines with 'timeout'",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert 'file_id="' in result
        assert "<file_extract>" in result
        assert "timeout" in result

    def test_three_recent_items_all_tier_a(self):
        """3 recent data evidence items from distinct files → all Tier A,
        each surfacing its own structural index."""
        evidence = [
            _make_evidence(
                summary=f"Evidence item {i}",
                extract=f"Structural index for item {i}",
                collected_at_turn=i + 1,
                source_file_id=f"file_aaaaaaaa{i:04d}",
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        for i in range(3):
            assert f'id="{evidence[i].evidence_id}"' in result
            assert f"Structural index for item {i}" in result

    def test_empty_structural_index_omits_tag(self):
        """Evidence with empty preprocessed_content omits <file_extract> tag."""
        ev = _make_evidence(
            extract="",
            summary="Log file with no extractable structure",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert f'id="{ev.evidence_id}"' in result
        assert "<summary>" in result
        # Should NOT include an empty structural_index tag
        # (because structural_index.strip() is empty)


# ============================================================
# Truncation
# ============================================================


class TestTruncation:
    """Test per-item and total budget truncation."""

    def test_structural_index_exceeds_per_item_cap_truncated(self):
        """Structural index > 4000 chars → truncated with [TRUNCATED] marker.

        The truncation note redirects the LLM at ``search_file`` rather than
        the older "suggest a targeted command the user can run" wording,
        which silently pushed work back onto the user instead of using the
        available tool.
        """
        long_content = "X" * 6000  # Exceeds default 4000 cap
        ev = _make_evidence(
            extract=long_content,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "[TRUNCATED:" in result
        assert "more characters" in result
        # Post-fix wording: point the agent at search_file, not a manual command.
        assert "search_file" in result
        assert "evidence id" in result
        # The displayed content should be capped at EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
        assert (
            len(long_content[:EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM])
            <= EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM
        )

    def test_total_budget_causes_tier_a_downgrade(self):
        """When total budget exceeded, remaining Tier A items downgrade to Tier B."""
        # Create 3 items each with large structural index that together exceed budget
        large_content = "Y" * 5500  # Each ~5500 chars, 3 items = ~16500 > 16000
        evidence = [
            _make_evidence(
                extract=large_content,
                summary=f"Big evidence {i}",
                collected_at_turn=i + 1,
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        # At least the first items should have structural_index
        assert "<file_extract>" in result
        # Total result should be within reasonable bounds
        assert (
            len(result) < EVIDENCE_CONTEXT_MAX_TOTAL_CHARS + 5000
        )  # Allow overhead for XML tags


# ============================================================
# Tier B: Older Data Evidence (Summary Only)
# ============================================================


class TestTierB:
    """Test Tier B — older data evidence with summary only."""

    def test_older_data_evidence_is_summary_only(self):
        """Data evidence beyond RECENT_COUNT shows summary only (no structural_index)."""
        # Create 5 items, each from a distinct file, so each carries its
        # own file-level structural_index post-010. First 2 are older
        # (Tier B), last 3 are recent (Tier A).
        evidence = [
            _make_evidence(
                summary=f"Summary for item {i}",
                extract=f"Structural index for item {i}",
                collected_at_turn=i + 1,
                source_file_id=f"file_aaaaaaaa{i:04d}",
            )
            for i in range(5)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        # Recent 3 (Tier A) should have structural_index
        assert "Structural index for item 2" in result
        assert "Structural index for item 3" in result
        assert "Structural index for item 4" in result

        # Older 2 (Tier B) should have summary but NOT structural_index
        assert "Summary for item 0" in result
        assert "Summary for item 1" in result


# ============================================================
# Tier C: USER_TEXT Evidence (Summary Only)
# ============================================================


class TestTierC:
    """Test Tier C — USER_TEXT evidence always shows summary only."""

    def test_user_text_is_always_summary_only(self):
        """USER_TEXT evidence → summary only, never structural_index."""
        ev = _make_evidence(
            source_file_id=None,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            summary="User described intermittent timeouts every 5 minutes",
            extract="This should NOT appear in context",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "User described intermittent timeouts" in result
        # Post-010: chat-extracted evidence has no file_id attribute (skipped)
        # USER_TEXT preprocessed_content should NOT be in the output
        # (it goes to Tier C which is summary-only)
        # Note: the implementation separates text_evidence from data_evidence

    def test_user_text_capped_at_five(self):
        """USER_TEXT evidence capped at 5 most recent items."""
        evidence = [
            _make_evidence(
                source_file_id=None,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                summary=f"User text item {i}",
                collected_at_turn=i + 1,
            )
            for i in range(8)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        # Should include at most 5 most recent USER_TEXT items
        # Items 3-7 (last 5)
        assert "User text item 3" in result
        assert "User text item 7" in result


# ============================================================
# Mixed Forms
# ============================================================


class TestMixedForms:
    """Test correct tier assignment with mixed evidence forms."""

    def test_mixed_forms_correct_tier_assignment(self):
        """File-backed evidence routes to Tier A/B; chat-extracted to Tier C."""
        evidence = [
            # Older data (Tier B)
            _make_evidence(
                summary="Old document evidence",
                extract="Old structural index",
                collected_at_turn=1,
                source_file_id="file_dddddddd0000",
            ),
            # User text (Tier C)
            _make_evidence(
                source_file_id=None,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                summary="User observation about timeouts",
                extract="User text content",
                collected_at_turn=2,
            ),
            # Recent data - these 3 should be Tier A, each from its own file
            _make_evidence(
                summary="Recent log file",
                extract="Crime scene extraction: errors found",
                collected_at_turn=3,
                source_file_id="file_cccccccc0001",
            ),
            _make_evidence(
                summary="Recent metrics file",
                extract="Statistical profile: anomalies detected",
                collected_at_turn=4,
                source_file_id="file_cccccccc0002",
            ),
            _make_evidence(
                summary="Search result finding",
                extract="Matched patterns in raw file",
                collected_at_turn=5,
                source_file_id="file_cccccccc0003",
            ),
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        # Tier A: recent 3 data items with structural_index
        assert "Crime scene extraction: errors found" in result
        assert "Statistical profile: anomalies detected" in result
        assert "Matched patterns in raw file" in result

        # Tier B: old document with summary only
        assert "Old document evidence" in result

        # Tier C: user text with summary only
        assert "User observation about timeouts" in result
        # Post-010: chat-extracted evidence has no file_id attribute (skipped)

    def test_no_data_evidence_only_user_text(self):
        """Case with only USER_TEXT evidence → no structural indexes, summaries only."""
        evidence = [
            _make_evidence(
                source_file_id=None,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                summary=f"User observation {i}",
                collected_at_turn=i + 1,
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        # All should be Tier C (summary only)
        for i in range(3):
            assert f"User observation {i}" in result
        # No structural_index tags expected for USER_TEXT-only cases


# ============================================================
# Filename Attribution
# ============================================================


class TestFilenameAttribution:
    """Test that evidence XML tags include filename when source_file_id maps to an UploadedFile."""

    def test_tier_a_evidence_includes_filename(self):
        """Tier A evidence with source_file_id → filename attribute in XML."""
        ev = _make_evidence(
            summary="Nginx access log errors",
            extract="ERROR: 503 at /api/health",
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aabbccdd1122",
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id="file_aabbccdd1122",
                filename="nginx-access.log",
                size_bytes=5000,
                uploaded_at_turn=1,
                structural_index="ERROR: 503 at /api/health",
            )
        ]
        result = _build_evidence_context(case)

        assert 'filename="nginx-access.log"' in result

    def test_tier_b_evidence_includes_filename(self):
        """Tier B (older) evidence with source_file_id → filename in XML."""
        # Create 4 items: first 1 is older (Tier B), last 3 are recent (Tier A)
        evidence = [
            _make_evidence(
                summary="Old app server log",
                extract="Old structural index",
                source_file_id="file_aabb11223344",
                collected_at_turn=1,
            ),
        ] + [
            _make_evidence(
                summary=f"Recent item {i}",
                extract=f"Structural {i}",
                collected_at_turn=i + 2,
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        case.uploaded_files = [
            UploadedFile(
                file_id="file_aabb11223344",
                filename="app-server.log",
                size_bytes=3000,
                uploaded_at_turn=1,
                structural_index="Old structural index",
            )
        ]
        result = _build_evidence_context(case)

        assert 'filename="app-server.log"' in result

    def test_no_filename_when_no_source_file_id(self):
        """Evidence without source_file_id (chat-extracted) → no
        filename attribute. Post-010: source_file_id=None requires
        source_type=USER_DESCRIPTION to satisfy the source-invariant.
        """
        ev = _make_evidence(
            summary="User pasted logs",
            extract="Some content",
            source_file_id=None,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "filename=" not in result

    def test_multiple_files_distinguished_by_filename(self):
        """Two evidence items from different files → distinct filenames in XML."""
        ev1 = _make_evidence(
            summary="Nginx errors",
            extract="503 errors",
            source_file_id="file_ccdd11223344",
            collected_at_turn=1,
        )
        ev2 = _make_evidence(
            summary="App server errors",
            extract="NullPointerException",
            source_file_id="file_eeff11223344",
            collected_at_turn=2,
        )
        case = _make_case_with_evidence([ev1, ev2])
        case.uploaded_files = [
            UploadedFile(
                file_id="file_ccdd11223344",
                filename="nginx-access.log",
                size_bytes=5000,
                uploaded_at_turn=1,
                structural_index="503 errors",
            ),
            UploadedFile(
                file_id="file_eeff11223344",
                filename="app-server.log",
                size_bytes=8000,
                uploaded_at_turn=2,
                structural_index="NullPointerException",
            ),
        ]
        result = _build_evidence_context(case)

        assert 'filename="nginx-access.log"' in result
        assert 'filename="app-server.log"' in result


# ============================================================
# Verbatim Quote Rendering (Post-010)
# ============================================================


class TestVerbatimQuoteRendering:
    """Post-010: ``Evidence.extract`` is an optional verbatim quote that
    supports the claim, separate from the file's structural index (which
    lives on ``uploaded_files.structural_index``).

    Pins the rendering contract introduced by the third-pass review so
    future refactors can't silently regress either path."""

    def test_tier_a_renders_both_file_extract_and_verbatim_quote(self):
        """File-backed evidence with both a structural index and an LLM
        quote → both blocks appear, structural index first."""
        ev = _make_evidence(
            summary="OOM kills in service-A",
            extract="Structural index content for service-A.log",
            verbatim_quote="[14:02:15] OOM killer fired, pid=4321 service-a",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "<file_extract>" in result
        assert "Structural index content for service-A.log" in result
        assert (
            "<verbatim_quote>[14:02:15] OOM killer fired, pid=4321 service-a"
            "</verbatim_quote>" in result
        )
        # Ordering: file_extract precedes verbatim_quote so the LLM reads
        # the orientation content first and the claim-grounding quote second.
        assert result.find("<file_extract>") < result.find("<verbatim_quote>")

    def test_tier_a_omits_verbatim_quote_when_extract_is_none(self):
        """File-backed evidence with no LLM quote → no <verbatim_quote>
        tag rendered (avoid empty elements that clutter the prompt)."""
        ev = _make_evidence(
            summary="Plain file-backed evidence",
            extract="Structural index content",
            verbatim_quote=None,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)
        assert "<file_extract>" in result
        assert "<verbatim_quote>" not in result

    def test_tier_c_renders_verbatim_quote_for_chat_extracted(self):
        """Chat-extracted evidence (USER_DESCRIPTION, no source file)
        with an LLM quote → ``<verbatim_quote>`` carries the actual
        system-output slice the user typed in. Suppressing it would
        lose the only content this row has."""
        ev = _make_evidence(
            summary="User reported HTTP 503 from checkout API",
            extract=None,  # no structural index — chat-extracted has no file
            verbatim_quote="HTTP/1.1 503 Service Unavailable - upstream connect error",
            source_file_id=None,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "HTTP/1.1 503 Service Unavailable" in result
        assert (
            "<verbatim_quote>HTTP/1.1 503 Service Unavailable - "
            "upstream connect error</verbatim_quote>" in result
        )
        # Tier C is never searchable — no file behind it.
        assert 'searchable="true"' not in result

    def test_tier_c_omits_verbatim_quote_when_extract_is_none(self):
        """Chat-extracted evidence with only a summary → summary
        appears, no empty verbatim_quote tag."""
        ev = _make_evidence(
            summary="User described a vague slowness with no specifics",
            extract=None,
            verbatim_quote=None,
            source_file_id=None,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)
        assert "User described a vague slowness with no specifics" in result
        assert "<verbatim_quote>" not in result


# ============================================================
# Processing Mode: structural_index role="orientation"
# ============================================================


class TestProcessingModeOrientation:
    """Test that processing_mode controls the role attribute on structural_index.

    In Directed Analysis mode, the structural index is tagged with
    role="orientation" to signal it's a map for the LLM, not the answer.
    """

    def test_da_mode_adds_role_orientation(self):
        """processing_mode='directed_analysis' → <file_extract role="orientation">."""
        ev = _make_evidence(
            summary="Nginx errors",
            extract="CRIME SCENE: 502 errors at 14:00",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case, processing_mode="directed_analysis")

        assert '<file_extract role="orientation">' in result
        assert "CRIME SCENE: 502 errors" in result

    def test_triage_mode_no_role_attribute(self):
        """processing_mode='triage' → <file_extract> (no role)."""
        ev = _make_evidence(
            summary="Nginx errors",
            extract="CRIME SCENE: 502 errors at 14:00",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case, processing_mode="triage")

        assert "<file_extract>" in result
        assert 'role="orientation"' not in result

    def test_none_mode_no_role_attribute(self):
        """processing_mode=None (default) → <file_extract> (no role)."""
        ev = _make_evidence(
            summary="Nginx errors",
            extract="CRIME SCENE: 502 errors at 14:00",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)  # no processing_mode arg

        assert "<file_extract>" in result
        assert 'role="orientation"' not in result

    def test_orientation_role_only_on_tier_a(self):
        """role='orientation' applies to Tier A items only; Tier B has no structural_index."""
        evidence = [
            _make_evidence(
                summary=f"Evidence {i}",
                extract=f"Index {i}",
                collected_at_turn=i + 1,
            )
            for i in range(5)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case, processing_mode="directed_analysis")

        # Tier A (recent 3): should have role="orientation"
        assert '<file_extract role="orientation">' in result
        # The older items (Tier B) should be summary-only — no structural_index at all


# ============================================================
# Page Capture Section Reranking (Stage 2)
# ============================================================

# Sample page capture content mimicking htmlToStructuredText output
_PAGE_CAPTURE_CONTENT = (
    "[captured_at: 2026-03-15T14:00:00Z]\n"
    "# Grafana - Production Dashboard\n"
    "Production system overview\n"
    "\n"
    "## Network\n"
    "Packets in: 1200/s\n"
    "Packets out: 980/s\n"
    "Latency: 2ms\n"
    "\n"
    "## Memory\n"
    "Used: 14GB\n"
    "Available: 18GB\n"
    "Swap: 0B\n"
    "\n"
    "## Disk IO\n"
    "Read: 50MB/s\n"
    "Write: 30MB/s\n"
    "IOPS: 1200\n"
    "\n"
    "## CPU Usage\n"
    "User CPU: 92%\n"
    "System CPU: 5%\n"
    "Load average: 8.2\n"
    "\n"
    "## Deployments\n"
    "Last deploy: v2.3.1 at 13:45\n"
    "Rollback available: v2.3.0\n"
)


class TestReankPageCaptureSections:
    """Tests for _rerank_page_capture_sections pure function."""

    def test_promotes_query_relevant_section(self):
        """Section matching query terms is promoted above non-matching sections."""
        result = _rerank_page_capture_sections(
            _PAGE_CAPTURE_CONTENT, "why is CPU usage so high"
        )
        sections = result.split("\n## ")
        # CPU Usage section should be first after preamble
        assert sections[1].startswith("CPU Usage")

    def test_preamble_always_first(self):
        """Preamble (captured_at + title) stays at position 0."""
        result = _rerank_page_capture_sections(
            _PAGE_CAPTURE_CONTENT, "disk IO throughput"
        )
        assert result.startswith("[captured_at: 2026-03-15T14:00:00Z]")

    def test_no_op_without_query(self):
        """Empty query returns content unchanged."""
        result = _rerank_page_capture_sections(_PAGE_CAPTURE_CONTENT, "")
        assert result == _PAGE_CAPTURE_CONTENT

    def test_no_op_single_section(self):
        """Content without ## headings passes through unchanged."""
        simple = "[captured_at: 2026-03-15T14:00:00Z]\nJust some text"
        result = _rerank_page_capture_sections(simple, "CPU usage")
        assert result == simple

    def test_stable_sort_on_tie(self):
        """Sections with equal scores preserve original document order."""
        # Query "system" doesn't match any section-specific terms beyond preamble
        content = (
            "Preamble\n"
            "## Alpha\n"
            "First section\n"
            "## Beta\n"
            "Second section\n"
            "## Gamma\n"
            "Third section\n"
        )
        result = _rerank_page_capture_sections(content, "unrelated query xyz")
        sections = result.split("\n## ")
        assert sections[1].startswith("Alpha")
        assert sections[2].startswith("Beta")
        assert sections[3].startswith("Gamma")

    def test_multiple_query_terms_score_higher(self):
        """Section matching more query terms scores higher than partial match."""
        result = _rerank_page_capture_sections(
            _PAGE_CAPTURE_CONTENT, "disk read write throughput"
        )
        sections = result.split("\n## ")
        # Disk IO has "read" and "write" — best match
        assert sections[1].startswith("Disk IO")

    def test_stopwords_ignored_in_scoring(self):
        """Common words like 'is', 'the', 'what' don't influence section scoring."""
        # "what is the" are all stopwords; only "memory" is meaningful
        result = _rerank_page_capture_sections(
            _PAGE_CAPTURE_CONTENT, "what is the memory usage"
        )
        sections = result.split("\n## ")
        assert sections[1].startswith("Memory")

    def test_case_insensitive_matching(self):
        """Query matching is case-insensitive."""
        result = _rerank_page_capture_sections(
            _PAGE_CAPTURE_CONTENT, "CPU USAGE HIGH LOAD"
        )
        sections = result.split("\n## ")
        assert sections[1].startswith("CPU Usage")


class TestPageCaptureRerankingIntegration:
    """Integration tests: reranking within _build_evidence_context."""

    def test_page_capture_evidence_is_reranked(self):
        """Page capture evidence (UploadedFile.upload_source='page_capture')
        has its sections reranked by user_query."""
        file_id = "file_aaccccccdd11"
        ev = _make_evidence(
            summary="Grafana dashboard capture",
            extract=_PAGE_CAPTURE_CONTENT,
            source_type=EvidenceSourceType.TEXT,
            source_file_id=file_id,
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="dashboard.html",
                size_bytes=len(_PAGE_CAPTURE_CONTENT),
                uploaded_at_turn=1,
                upload_source="page_capture",
                structural_index=_PAGE_CAPTURE_CONTENT,
            )
        ]
        result = _build_evidence_context(case, user_query="CPU usage high")
        # CPU Usage section should appear before Network in the structural index
        cpu_pos = result.find("## CPU Usage")
        network_pos = result.find("## Network")
        assert (
            cpu_pos < network_pos
        ), f"CPU Usage (pos={cpu_pos}) should appear before Network (pos={network_pos})"

    def test_non_page_capture_not_reranked(self):
        """Evidence whose backing UploadedFile is not a page capture is not
        reranked regardless of user_query."""
        content = (
            "Header\n"
            "## Zebra\n"
            "Last section\n"
            "## Alpha\n"
            "First section alpha query match\n"
        )
        file_id = "file_bbccccccdd44"
        ev = _make_evidence(
            summary="Regular log file",
            extract=content,
            source_type=EvidenceSourceType.LOGS,
            source_file_id=file_id,
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="app.log",
                size_bytes=len(content),
                uploaded_at_turn=1,
                upload_source="file_upload",
                structural_index=content,
            )
        ]
        result = _build_evidence_context(case, user_query="alpha")
        # Original order preserved — Zebra before Alpha
        zebra_pos = result.find("## Zebra")
        alpha_pos = result.find("## Alpha")
        assert zebra_pos < alpha_pos

    def test_no_query_preserves_original_order(self):
        """Without user_query, page capture sections stay in original order."""
        file_id = "file_aaccccccdd22"
        ev = _make_evidence(
            summary="Dashboard",
            extract=_PAGE_CAPTURE_CONTENT,
            source_type=EvidenceSourceType.TEXT,
            source_file_id=file_id,
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="dashboard.html",
                size_bytes=len(_PAGE_CAPTURE_CONTENT),
                uploaded_at_turn=1,
                upload_source="page_capture",
                structural_index=_PAGE_CAPTURE_CONTENT,
            )
        ]
        result = _build_evidence_context(case)  # no user_query
        # Original order: Network before CPU Usage
        network_pos = result.find("## Network")
        cpu_pos = result.find("## CPU Usage")
        assert network_pos < cpu_pos

    def test_reranked_content_survives_truncation(self):
        """Relevant section promoted above per-item char cap survives truncation."""
        # Build content where the relevant section is at the end, past char cap
        filler = "x" * 1500  # large filler per section
        content = (
            "[captured_at: 2026-03-15T14:00:00Z]\n"
            f"## Filler A\n{filler}\n"
            f"## Filler B\n{filler}\n"
            f"## Filler C\n{filler}\n"
            f"## Target Section\nCPU usage: 95%\nLoad average: 12.3\n"
        )
        file_id = "file_aaccccccdd33"
        ev = _make_evidence(
            summary="Large dashboard",
            extract=content,
            source_type=EvidenceSourceType.TEXT,
            source_file_id=file_id,
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="dashboard.html",
                size_bytes=len(content),
                uploaded_at_turn=1,
                upload_source="page_capture",
                structural_index=content,
            )
        ]
        result = _build_evidence_context(case, user_query="CPU load average")
        # Target section should survive truncation because it's promoted to top
        assert "Target Section" in result
        assert "CPU usage: 95%" in result

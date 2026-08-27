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
    CaseState,
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
        state=CaseState.INVESTIGATING,
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
        state=CaseState.INQUIRY,
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
# Tier D — orphan uploads (uploaded_files not yet promoted to Evidence)
# ============================================================


class TestTierDOrphanUploads:
    """Tier D surfaces uploaded files that don't have an Evidence row yet.

    Regression: before this fix, ``_build_evidence_context`` only surfaced
    file content via the Evidence list. Once at least one Evidence row
    existed, the function took the non-empty-evidence path and ignored
    any pending uploads — files uploaded in later turns became invisible
    until something else created Evidence for them. The LLM then could
    not emit ``evidence_to_add`` for the pending file (no content to
    react to), producing the user-visible "I don't have direct access
    to the file contents" symptom.
    """

    _PENDING_INDEX = (
        '{"v":1,"file_extract":"PENDING: deployment.yaml shows JAVA_OPTS=-Xmx384m",'
        '"search_map":"[search: heap] 3 matches",'
        '"file_meta":{"line_count":42,"top_error":null}}'
    )

    def _pending_file(
        self,
        file_id: str = "file_aaaaaaaa0001",
        filename: str = "deployment.yaml",
        uploaded_at_turn: int = 7,
        structural_index: str | None = None,
    ) -> UploadedFile:
        return UploadedFile(
            file_id=file_id,
            filename=filename,
            size_bytes=1353,
            content_type="text/plain",
            uploaded_at_turn=uploaded_at_turn,
            uploaded_at=datetime.now(UTC),
            uploaded_by="user_123",
            data_type="text",
            structural_index=(
                structural_index
                if structural_index is not None
                else self._PENDING_INDEX
            ),
        )

    def test_pending_upload_surfaces_when_evidence_exists(self):
        """Existing Evidence + a pending UploadedFile not referenced by any
        Evidence row → context contains the pending file's <uploaded_file>
        block so the LLM can see its content and create Evidence from it."""
        ev = _make_evidence(
            source_file_id="file_aabbccdd1122",
            summary="Earlier file-backed evidence",
            extract="Earlier file's structural index payload",
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files.append(self._pending_file())

        result = _build_evidence_context(case)

        assert "<uploaded_file" in result
        assert 'file_id="file_aaaaaaaa0001"' in result
        assert "PENDING: deployment.yaml shows JAVA_OPTS" in result
        assert "[Source: deployment.yaml]" in result
        # The pending block must be marked searchable so the LLM knows it
        # can pass the file_id to search_file.
        assert 'searchable="true"' in result

    def test_dedup_does_not_emit_orphan_block_for_already_referenced_file(
        self,
    ):
        """Regression guard: a file referenced by an existing Evidence row
        must NOT appear in the Tier D orphan section — it's already
        surfaced via Tier A/B. No double-render."""
        file_id = "file_aabbccdd1122"
        ev = _make_evidence(
            source_file_id=file_id,
            summary="File-backed evidence",
            extract="Existing structural index",
        )
        case = _make_case_with_evidence([ev])
        # case.uploaded_files already contains the file synthesized by
        # _make_case_with_evidence with this same file_id. The Tier D
        # section must skip it because Evidence already references it.

        result = _build_evidence_context(case)

        # The file_id should appear exactly once — on the <evidence> block,
        # not also on a duplicate <uploaded_file> block.
        assert result.count(f'file_id="{file_id}"') == 1
        # No <uploaded_file> tag at all in this scenario.
        assert "<uploaded_file" not in result

    def test_orphan_visible_when_evidence_has_source_file_id_none(self):
        """source_file_id=None on Evidence (e.g., USER_DESCRIPTION rows
        from chat-extracted analysis) must NOT be treated as covering
        any uploaded file. A pending upload remains orphan-visible even
        when the case has such Evidence rows — None ≠ any real file_id.

        Empirically observed: in case_ba5b472f2438 turn 8, FM created an
        Evidence row with source_file_id=None analyzing the user's text;
        the actual turn-8 file upload stayed orphan and was never seen."""
        text_ev = _make_evidence(
            source_file_id=None,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            summary="User described what they saw",
            extract="User's verbatim description",
        )
        case = _make_case_with_evidence([text_ev])
        case.uploaded_files.append(self._pending_file())

        result = _build_evidence_context(case)

        assert 'file_id="file_aaaaaaaa0001"' in result, (
            "Pending upload must remain orphan-visible when the only "
            "Evidence rows have source_file_id=None"
        )
        assert "PENDING: deployment.yaml" in result

    def test_token_budget_drops_oldest_orphans_first(self):
        """Multiple orphans with total content exceeding the budget →
        rendering respects EVIDENCE_CONTEXT_MAX_TOTAL_CHARS. Policy:
        newest uploads win (most-recent-first iteration), so older
        orphans get dropped when the budget is exhausted. This matches
        chat-flow intuition: the file the user just submitted matters
        more than orphans from earlier turns."""
        # Build an existing Evidence row so we're on Path 2 (non-empty
        # evidence) — that's the path the new Tier D section lives on.
        ev = _make_evidence(
            source_file_id="file_aabbccdd1122",
            summary="Existing evidence",
            extract="Some existing content",
        )
        # Use a payload comfortably below the per-item cap so the per-item
        # truncation isn't what shrinks the test, then squeeze the TOTAL
        # budget down so 3 orphans can't all fit. This pins the dropped-
        # oldest-first policy at known thresholds rather than relying on
        # default constants that might be retuned later.
        per_item_payload = "X" * 2000
        index_blob = (
            '{"v":1,"file_extract":"' + per_item_payload + '",'
            '"search_map":null,"file_meta":{}}'
        )
        # 3 orphans × ~2000 chars + overhead ≈ 6500+; cap budget at 5000
        # so exactly one orphan fits with room left, two definitely don't.
        squeezed_budget = 5000

        oldest = self._pending_file(
            file_id="file_bbbbbbbb0001",
            filename="older.txt",
            uploaded_at_turn=5,
            structural_index=index_blob,
        )
        middle = self._pending_file(
            file_id="file_cccccccc0002",
            filename="middle.txt",
            uploaded_at_turn=8,
            structural_index=index_blob,
        )
        newest = self._pending_file(
            file_id="file_dddddddd0003",
            filename="newest.txt",
            uploaded_at_turn=11,
            structural_index=index_blob,
        )

        case = _make_case_with_evidence([ev])
        case.uploaded_files.extend([oldest, middle, newest])

        with patch(
            "faultmaven.core.investigation.prompts.context_builder."
            "EVIDENCE_CONTEXT_MAX_TOTAL_CHARS",
            squeezed_budget,
        ):
            result = _build_evidence_context(case)

        # Newest must be present (highest priority).
        assert 'file_id="file_dddddddd0003"' in result, (
            "newest orphan must survive budget enforcement; "
            f"result len={len(result)}"
        )
        # Oldest must be dropped under the squeezed budget.
        assert (
            'file_id="file_bbbbbbbb0001"' not in result
        ), "oldest orphan must be dropped first when budget exhausted"


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
    """Evidence XML tags name their source file when source_file_id resolves.

    #666: the name lives in ONE attribute, ``label``, carrying
    ``UploadedFile.display_name``. For a file the user chose that IS the
    filename; for a paste it is "pasted text (turn N)". The separate
    ``filename`` attribute was removed — it could only ever be absent or
    invented for a paste, which left the model told to cite a filename that
    is missing from half its context.
    """

    def test_tier_a_evidence_includes_filename(self):
        """Tier A evidence with source_file_id → label attribute in XML."""
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

        assert 'label="nginx-access.log"' in result
        assert "filename=" not in result

    def test_tier_b_evidence_includes_filename(self):
        """Tier B (older) evidence with source_file_id → label in XML."""
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

        assert 'label="app-server.log"' in result
        assert "filename=" not in result

    def test_no_filename_when_no_source_file_id(self):
        """Evidence without source_file_id (chat-extracted) → the label
        falls back to the source type, and there is no filename attribute
        anywhere. Post-010: source_file_id=None requires
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
        assert 'label="user description"' in result

    def test_multiple_files_distinguished_by_filename(self):
        """Two evidence items from different files → distinct labels in XML."""
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

        assert 'label="nginx-access.log"' in result
        assert 'label="app-server.log"' in result


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


# ============================================================
# Evidence-context signaling for hallucinated-freshness mitigation
# (PR follow-up to test scenario that exposed agent re-citing prior
# evidence as if fresh, missing duplicate uploads, and citing
# information-free pasted-content filenames)
# ============================================================


class TestFreshThisTurnAttribute:
    """``fresh_this_turn="true"`` partitions current-turn evidence from
    prior context so the LLM has a positional signal to distinguish
    data the user just provided from data being re-cited."""

    def test_evidence_collected_at_current_turn_gets_fresh_marker(self):
        ev_old = _make_evidence(
            summary="Earlier evidence",
            collected_at_turn=3,
            source_file_id="file_0a0a0a0a0a01",
        )
        ev_new = _make_evidence(
            summary="Just-uploaded evidence",
            collected_at_turn=7,
            source_file_id="file_0b0b0b0b0b02",
        )
        case = _make_case_with_evidence([ev_old, ev_new])
        case.current_turn = 7
        result = _build_evidence_context(case)

        # Each evidence row appears once; only the current-turn one
        # carries the fresh marker.
        old_line = next(
            line for line in result.splitlines() if ev_old.evidence_id in line
        )
        new_line = next(
            line for line in result.splitlines() if ev_new.evidence_id in line
        )
        assert 'fresh_this_turn="true"' not in old_line
        assert 'fresh_this_turn="true"' in new_line

    def test_no_evidence_carries_fresh_when_current_turn_is_zero(self):
        # current_turn=0 (default) and collected_at_turn=1 — nothing is fresh
        # because Case was constructed without advancing the turn counter.
        ev = _make_evidence(collected_at_turn=1)
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)
        assert 'fresh_this_turn="true"' not in result

    def test_orphan_uploaded_file_fresh_marker(self):
        """Tier D orphan upload (no Evidence row yet) gets the marker when
        it landed on the current turn."""
        existing_ev = _make_evidence(
            summary="Older evidence",
            collected_at_turn=1,
            source_file_id="file_0c0c0c0c0c03",
        )
        case = _make_case_with_evidence([existing_ev])
        case.current_turn = 4
        case.uploaded_files.append(
            UploadedFile(
                file_id="file_0d0d0d0d0d04",
                filename="just-uploaded.log",
                size_bytes=200,
                content_type="text/plain",
                uploaded_at_turn=4,
                uploaded_at=datetime.now(UTC),
                uploaded_by="user_123",
                data_type="logs",
                structural_index="ERROR: fresh content",
            )
        )
        result = _build_evidence_context(case)
        # The orphan render is on a separate <uploaded_file> line — find
        # the one with the orphan's file_id and assert the marker is there.
        orphan_line = next(
            line for line in result.splitlines() if "file_0d0d0d0d0d04" in line
        )
        assert 'fresh_this_turn="true"' in orphan_line


class TestIdenticalToPriorUploadAttribute:
    """``identical_to_prior_upload_at_turn="N"`` marks byte-equal re-uploads
    so the LLM can notice e.g. 'the same config has been submitted three
    times — the apply isn't taking effect'. First occurrence never carries
    the marker."""

    def _file(
        self,
        file_id: str,
        content_hash: str,
        uploaded_at_turn: int,
        **overrides,
    ) -> UploadedFile:
        defaults = dict(
            filename=f"{file_id}.yaml",
            size_bytes=100,
            content_type="text/yaml",
            uploaded_at=datetime.now(UTC),
            uploaded_by="user_123",
            data_type="configuration",
            structural_index='{"v":1,"file_extract":"apiVersion: v1"}',
        )
        defaults.update(overrides)
        return UploadedFile(
            file_id=file_id,
            content_hash=content_hash,
            uploaded_at_turn=uploaded_at_turn,
            **defaults,
        )

    def test_re_upload_marked_with_first_turn(self):
        """T4 first upload, T8 re-upload of same bytes → T8 carries the
        marker pointing at T4, T4 does not."""
        case = _make_case_with_evidence([])
        case.uploaded_files = [
            self._file("file_aaaaaaaaaaaa", "hash_aaaa", 4),
            self._file("file_bbbbbbbbbbbb", "hash_aaaa", 8),
        ]
        case.current_turn = 8
        result = _build_evidence_context(case)

        t4_line = next(
            line for line in result.splitlines() if "file_aaaaaaaaaaaa" in line
        )
        t8_line = next(
            line for line in result.splitlines() if "file_bbbbbbbbbbbb" in line
        )
        assert "identical_to_prior_upload_at_turn" not in t4_line
        assert 'identical_to_prior_upload_at_turn="4"' in t8_line

    def test_distinct_content_no_marker(self):
        """Different content_hash → neither gets the marker."""
        case = _make_case_with_evidence([])
        case.uploaded_files = [
            self._file("file_cccccccccccc", "hash_aaa", 1),
            self._file("file_dddddddddddd", "hash_bbb", 2),
        ]
        case.current_turn = 2
        result = _build_evidence_context(case)
        assert "identical_to_prior_upload_at_turn" not in result

    def test_no_hash_no_marker(self):
        """Files without content_hash (e.g., streamed) never get the marker."""
        case = _make_case_with_evidence([])
        case.uploaded_files = [
            self._file("file_eeeeeeeeeeee", "", 1),
            self._file("file_ffffffffffff", "", 2),
        ]
        case.current_turn = 2
        result = _build_evidence_context(case)
        assert "identical_to_prior_upload_at_turn" not in result


class TestSemanticLabelForPastedContent:
    """``_evidence_label`` labels minted-filename rows with
    ``UploadedFile.display_name``. Real filenames pass through unchanged.

    The label is the bare display name and nothing else. It used to be
    enriched with the summary head, which read better but failed the two
    tests a CITED name has to pass: the summary is rewritten by
    reclassification (so the name moved mid-case) and is not unique across
    similar pastes (so the citation designated either). What the summary was
    contributing — what this item IS — is already in the element, on
    ``data_type`` and in ``<summary>``.
    """

    def test_pasted_content_label_uses_data_type_and_summary(self):
        file_id = "file_0e0e0e0e0e05"
        ev = _make_evidence(
            summary="DestinationRule yaml configuring outlier detection",
            extract="apiVersion: networking.istio.io/v1beta1",
            source_file_id=file_id,
            source_type=EvidenceSourceType.CONFIGURATION,
        )
        case = _make_case_with_evidence([ev])
        # Replace the synthesized upload row with a pasted-content one.
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="pasted-content-20260524T043237Z.txt",
                size_bytes=128,
                uploaded_at_turn=1,
                uploaded_at=datetime.now(UTC),
                uploaded_by="user_123",
                data_type="configuration",
                summary=(
                    "DestinationRule for user-service with outlier detection. "
                    "First sentence ends here."
                ),
                structural_index="apiVersion: networking.istio.io/v1beta1",
            )
        ]
        result = _build_evidence_context(case)
        ev_line = next(line for line in result.splitlines() if ev.evidence_id in line)
        # Named by how it arrived and when — stable under reclassification,
        # unique within the case.
        assert 'label="pasted text (turn 1)"' in ev_line
        # #666: the minted name does not ride along on a filename attribute
        # next to the label — the element has no filename at all, because
        # the file has none the user would recognise.
        assert "filename=" not in ev_line
        assert "pasted-content-" not in result

    def test_real_filename_passes_through(self):
        file_id = "file_0f0f0f0f0f06"
        ev = _make_evidence(source_file_id=file_id, source_type=EvidenceSourceType.LOGS)
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="nginx-error.log",
                size_bytes=128,
                uploaded_at_turn=1,
                uploaded_at=datetime.now(UTC),
                uploaded_by="user_123",
                data_type="logs",
                summary="nginx 502 errors",
                structural_index="ERROR: upstream timed out",
            )
        ]
        result = _build_evidence_context(case)
        assert 'label="nginx-error.log"' in result

    def test_pasted_content_no_summary_falls_back_to_data_type(self):
        file_id = "file_1010101010aa"
        ev = _make_evidence(
            source_file_id=file_id, source_type=EvidenceSourceType.CONFIGURATION
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id=file_id,
                filename="pasted-content-20260524T043237Z.txt",
                size_bytes=128,
                uploaded_at_turn=1,
                uploaded_at=datetime.now(UTC),
                uploaded_by="user_123",
                data_type="logs",
                summary=None,
                structural_index="ERROR: 500 internal",
            )
        ]
        result = _build_evidence_context(case)
        assert 'label="pasted text (turn 1)"' in result


class TestRule5NewDataClaimedButNotAttached:
    """Rule 5 has a behavior row covering the case where the user implies
    new data ('latest logs', 'just ran') but no fresh attachment arrived
    this turn. The prompt must instruct the agent to ask for the file
    rather than fabricate analysis of prior-turn evidence."""

    def test_investigation_base_includes_new_data_claim_rule(self):
        from faultmaven.core.investigation.prompts.templates import INVESTIGATION_BASE

        # The trigger language and the prohibition both appear in the
        # WORK WITH WHAT YOU GET block. We assert both halves so a future
        # well-meaning rewrite of one without the other gets caught.
        assert 'fresh_this_turn="true"' in INVESTIGATION_BASE
        assert "create new evidence_to_add rows from prior-turn files" in (
            INVESTIGATION_BASE
        )


# ============================================================
# Current-turn priority floor + graceful fill (evidence-context-assembly.md)
# ============================================================


class TestCurrentTurnFloor:
    """INV-EC-1..4: a file uploaded THIS turn is always present and full,
    never evicted by historical evidence; the fill degrades gracefully (no
    cliff); the budget scales with the model.

    Regression for the production bug where a turn-8 page capture
    (``platform-deploy.yaml``) was dropped from context because the existing
    Evidence rows consumed the flat 16K-char budget before the fresh orphan
    upload was reached — so the agent read a stale file and asked the user to
    paste a file it already had.
    """

    def _orphan(
        self,
        file_id: str,
        uploaded_at_turn: int,
        extract: str = "fresh content",
        filename: str = "fresh.yaml",
    ) -> UploadedFile:
        return UploadedFile(
            file_id=file_id,
            filename=filename,
            size_bytes=len(extract),
            content_type="text/plain",
            uploaded_at_turn=uploaded_at_turn,
            uploaded_at=datetime.now(UTC),
            uploaded_by="user_123",
            data_type="text",
            structural_index=extract,
        )

    def test_current_turn_orphan_survives_budget_pressure(self):
        """INV-EC-1: historical evidence that would exhaust the budget does
        NOT evict the current-turn orphan upload — and it renders first."""
        big = "Y" * 3500
        evidence = [
            _make_evidence(
                extract=big,
                summary=f"historical evidence {i}",
                collected_at_turn=i + 1,
                source_file_id=f"file_dddddddd{i:04d}",
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        case.current_turn = 9
        case.uploaded_files.append(
            self._orphan(
                "file_fafafafa0001",
                uploaded_at_turn=9,
                extract="apiVersion: v1\nkind: Job\nenv: DB_PASSWORD from secret",
            )
        )

        with patch(
            "faultmaven.core.investigation.prompts.context_builder."
            "EVIDENCE_CONTEXT_MAX_TOTAL_CHARS",
            8000,
        ):
            result = _build_evidence_context(case)

        assert 'file_id="file_fafafafa0001"' in result, (
            "current-turn upload must always be present even under budget "
            "pressure from historical evidence"
        )
        assert 'fresh_this_turn="true"' in result
        # Floor renders before the historical <evidence> blocks.
        assert result.find("file_fafafafa0001") < result.find("<evidence id=")

    def test_no_cliff_large_orphan_does_not_drop_smaller(self):
        """INV-EC-2: a large over-budget orphan is skipped, not a `break` that
        drops every smaller orphan behind it."""
        ev = _make_evidence(
            source_file_id="file_aabbccdd1122",
            summary="existing evidence",
            extract="small existing content",
        )
        case = _make_case_with_evidence([ev])
        case.current_turn = 0  # both orphans historical → Tier D fill

        huge = "Z" * 6000  # per-item cap (4000) → entry ~4200 chars
        case.uploaded_files.append(
            self._orphan("file_bbbb00000001", uploaded_at_turn=9, extract=huge)
        )
        case.uploaded_files.append(
            self._orphan(
                "file_5acc00000002", uploaded_at_turn=2, extract="tiny orphan body"
            )
        )

        # Budget admits the small orphan but not the big one. Newest-first
        # ordering tries the big (turn 9) first; `continue` must let the small
        # (turn 2) still render.
        with patch(
            "faultmaven.core.investigation.prompts.context_builder."
            "EVIDENCE_CONTEXT_MAX_TOTAL_CHARS",
            3000,
        ):
            result = _build_evidence_context(case)

        assert 'file_id="file_bbbb00000001"' not in result
        assert 'file_id="file_5acc00000002"' in result, (
            "small orphan must survive even though a larger orphan ahead of it "
            "overflowed the budget (no-cliff)"
        )

    def test_model_aware_budget_scales_with_provider(self):
        """INV-EC-4: a Gemini-class budget renders more orphan content than the
        16K fallback when the total exceeds the fallback cap."""
        ev = _make_evidence(
            source_file_id="file_aabbccdd1122",
            summary="seed evidence",
            extract="seed",
        )
        case = _make_case_with_evidence([ev])
        case.current_turn = 0
        # 8 historical orphans × ~3000 chars ≈ 24K > 16K fallback, < ~36K Gemini.
        for i in range(8):
            case.uploaded_files.append(
                self._orphan(
                    f"file_0a0a0a0a{i:04d}",
                    uploaded_at_turn=i + 1,
                    extract="O" * 3000,
                    filename=f"orphan{i}.txt",
                )
            )

        fallback = _build_evidence_context(case)
        gemini = _build_evidence_context(
            case, provider_name="gemini", model_name="gemini-2.5-pro"
        )
        assert len(gemini) > len(fallback), (
            "model-aware budget should admit more content on a large-context "
            "model than the conservative fallback cap"
        )

    def test_multiple_current_turn_orphans_all_present_under_tight_budget(self):
        """INV-EC-1: ALL current-turn orphan uploads are present (full or summary
        stub) even when several arrive in one turn and the budget is tight — the
        2nd+ orphan must not be dropped."""
        ev = _make_evidence(
            source_file_id="file_aabbccdd1122",
            summary="historical evidence",
            extract="existing content",
        )
        case = _make_case_with_evidence([ev])
        case.current_turn = 9
        ids = ["file_c0c0c0c00000", "file_c0c0c0c00001", "file_c0c0c0c00002"]
        for i, fid in enumerate(ids):
            case.uploaded_files.append(
                self._orphan(
                    fid, uploaded_at_turn=9, extract="C" * 3000, filename=f"cur{i}.txt"
                )
            )

        with patch(
            "faultmaven.core.investigation.prompts.context_builder."
            "EVIDENCE_CONTEXT_MAX_TOTAL_CHARS",
            4000,
        ):
            result = _build_evidence_context(case)

        for fid in ids:
            assert f'file_id="{fid}"' in result, (
                f"current-turn orphan {fid} must be present (full or summary stub), "
                "never dropped"
            )
        # At least one degraded to a summary stub (budget can't hold 3 full).
        assert "Full content omitted to fit budget" in result

    def test_current_turn_evidence_full_render_bounded_by_reserve(self):
        """INV-EC-1b: many current-turn EVIDENCE rows do not all render in full —
        the full-render exemption is bounded by the reserve, so they can't blow
        the evidence budget. Beyond the reserve they degrade to Tier-B summaries
        (still present, not dropped)."""
        evidence = [
            _make_evidence(
                extract="E" * 4000,
                summary=f"current-turn evidence {i}",
                collected_at_turn=9,
                source_file_id=f"file_e0e0e0e0000{i}",
            )
            for i in range(5)
        ]
        case = _make_case_with_evidence(evidence)
        case.current_turn = 9

        with patch(
            "faultmaven.core.investigation.prompts.context_builder."
            "EVIDENCE_CONTEXT_MAX_TOTAL_CHARS",
            8000,
        ):
            result = _build_evidence_context(case)

        # Not every current-turn evidence renders its full structural index —
        # bounded by the ~4000-char reserve (else the block would be ~20K+).
        assert result.count("<file_extract") < 5, (
            "current-turn evidence must be bounded by the reserve, not all "
            "rendered full"
        )
        # But all 5 are still present (degraded to summary, not dropped).
        for ev in evidence:
            assert f'id="{ev.evidence_id}"' in result


# ============================================================
# Directed-analysis index+stub (tool-gated; historical extract elided)
# ============================================================


def test_da_index_stub_elides_historical_extract_keeps_current_turn():
    """In a directed-analysis turn WITH tools available, historical evidence
    renders stub + search_map only (no file_extract body), while the current-turn
    upload keeps its extract and every file stays addressable."""
    PROVIDER, MODEL = "openai", "gpt-4"
    HIST_ID = "file_aaaa11112222"
    CUR_ID = "file_bbbb33334444"
    hist = _make_evidence(
        summary="historical log",
        extract="HISTORICAL_EXTRACT_BODY " * 40,
        source_file_id=HIST_ID,
        collected_at_turn=1,
    )
    cur = _make_evidence(
        summary="fresh log",
        extract="CURRENTTURN_EXTRACT_BODY " * 40,
        source_file_id=CUR_ID,
        collected_at_turn=5,
    )
    case = _make_case_with_evidence([hist, cur])
    case.current_turn = 5

    # DA turn + tools available: historical extract elided (marked + addressable);
    # current-turn extract kept.
    on = _build_evidence_context(
        case,
        processing_mode="directed_analysis",
        provider_name=PROVIDER,
        model_name=MODEL,
        tools_available=True,
    )
    assert "HISTORICAL_EXTRACT_BODY" not in on, "historical extract body must be elided"
    assert 'elided="directed_analysis"' in on, "elision must be marked (INV-4)"
    assert HIST_ID in on, "historical file must stay addressable"
    assert "CURRENTTURN_EXTRACT_BODY" in on, "current-turn extract must be kept"

    # Tools NOT available (tool-less / tool-incapable turn): the extract must NOT
    # be elided — the agent has no search_file to recover it, so eliding would
    # strand it (NO INCORRECT CONCLUSION).
    toolless = _build_evidence_context(
        case,
        processing_mode="directed_analysis",
        provider_name=PROVIDER,
        model_name=MODEL,
        tools_available=False,
    )
    assert (
        "HISTORICAL_EXTRACT_BODY" in toolless
    ), "extract must be kept when search_file is unavailable"


def test_da_index_stub_off_in_triage_mode():
    """Index+stub only fires in directed_analysis; TRIAGE keeps the extract so
    triage can answer from the structural index (even with tools available)."""
    ev = _make_evidence(
        summary="historical log",
        extract="TRIAGE_EXTRACT_BODY " * 40,
        source_file_id="file_cccc55556666",
        collected_at_turn=1,
    )
    case = _make_case_with_evidence([ev])
    case.current_turn = 5
    out = _build_evidence_context(
        case,
        processing_mode="triage",
        provider_name="openai",
        model_name="gpt-4",
        tools_available=True,
    )
    assert "TRIAGE_EXTRACT_BODY" in out


def test_evidence_omitted_marker_when_budget_drops_items():
    """INV-4: when evidence items are skipped for budget, an <evidence_omitted>
    marker is emitted so the omission is never silent."""
    evs = [
        _make_evidence(
            summary="summary " + "x" * 200,
            extract="idx",
            source_file_id=f"file_{i:012x}",
            collected_at_turn=1,
        )
        for i in range(10)
    ]
    case = _make_case_with_evidence(evs)
    case.current_turn = 5
    # Tight char budget → most items can't fit → some are skipped.
    out = _build_evidence_context(case, char_budget_override=600)
    assert "<evidence_omitted" in out, "omitted evidence must be marked (INV-4)"
    assert 'reason="prompt_budget"' in out


def test_tier_c_chat_cap_marks_omitted():
    """The Tier-C 5-most-recent cap on chat-extracted evidence now feeds the
    <evidence_omitted> marker (INV-4) — these have no source_file_id, so a silent
    drop would be unrecoverable."""
    evs = [
        _make_evidence(
            summary=f"chat evidence {i}",
            source_file_id=None,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at_turn=1,
        )
        for i in range(8)  # > 5 → 3 omitted
    ]
    case = _make_case_with_evidence(evs)
    out = _build_evidence_context(case)
    assert "<evidence_omitted" in out, "the >5 chat-evidence cap must be marked"
    assert 'reason="prompt_budget"' in out

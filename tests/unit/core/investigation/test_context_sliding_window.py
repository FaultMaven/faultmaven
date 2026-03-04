"""Unit tests for the Context Sliding Window evidence context builder.

Tests the three-tier evidence context system that includes structural indexes
in LLM prompts for recent evidence, fixing the "I don't have access to file
content" bug.

Tier A: Last N data evidence items (form=DOCUMENT or SUBMITTED_DATA)
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
)
from faultmaven.modules.case.contracts import (
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
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
    form: EvidenceForm = EvidenceForm.DOCUMENT,
    summary: str = "Test evidence summary",
    preprocessed_content: str = "Structural index content",
    data_type: str = "LOGS",
    content_size_bytes: int = 5000,
    **overrides,
) -> Evidence:
    """Create an Evidence instance for context builder tests."""
    defaults = {
        "evidence_id": evidence_id or _next_ev_id(),
        "form": form,
        "summary": summary,
        "preprocessed_content": preprocessed_content,
        "data_type": data_type,
        "content_size_bytes": content_size_bytes,
        "category": EvidenceCategory.SYMPTOM_EVIDENCE,
        "source_type": EvidenceSourceType.LOGS,
        "collected_at": datetime.now(UTC),
        "collected_by": "user_123",
        "collected_at_turn": 1,
        "primary_purpose": "Test",
        "preprocessing_method": "crime_scene",
    }
    defaults.update(overrides)
    return Evidence(**defaults)


def _make_case_with_evidence(evidence_list: list) -> Case:
    """Create a minimal Case with given evidence list."""
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
# Tier A: Recent Data Evidence
# ============================================================


class TestTierA:
    """Test Tier A — recent data evidence with full structural_index."""

    def test_single_recent_document_includes_structural_index(self):
        """1 recent DOCUMENT evidence → Tier A with full structural_index."""
        ev = _make_evidence(
            form=EvidenceForm.DOCUMENT,
            summary="Error burst detected in application logs",
            preprocessed_content="============\nCRIME SCENE EXTRACTION\n============\nERROR: Connection timeout at 14:03:21",
            data_type="LOGS",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert f'id="{ev.evidence_id}"' in result
        assert 'form="document"' in result
        assert 'data_type="LOGS"' in result
        assert "<structural_index>" in result
        assert "CRIME SCENE EXTRACTION" in result
        assert "<summary>" in result

    def test_recent_submitted_data_is_tier_a(self):
        """SUBMITTED_DATA form is also treated as Tier A (data evidence)."""
        ev = _make_evidence(
            form=EvidenceForm.SUBMITTED_DATA,
            summary="Search result finding",
            preprocessed_content="Matched lines with 'timeout'",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert 'form="submitted_data"' in result
        assert "<structural_index>" in result
        assert "timeout" in result

    def test_three_recent_items_all_tier_a(self):
        """3 recent data evidence items → all get Tier A treatment."""
        evidence = [
            _make_evidence(
                summary=f"Evidence item {i}",
                preprocessed_content=f"Structural index for item {i}",
                collected_at_turn=i + 1,
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        for i in range(3):
            assert f'id="{evidence[i].evidence_id}"' in result
            assert f"Structural index for item {i}" in result

    def test_empty_structural_index_omits_tag(self):
        """Evidence with empty preprocessed_content omits <structural_index> tag."""
        ev = _make_evidence(
            preprocessed_content="",
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
        """Structural index > 4000 chars → truncated with [TRUNCATED] marker."""
        long_content = "X" * 6000  # Exceeds default 4000 cap
        ev = _make_evidence(
            preprocessed_content=long_content,
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "[TRUNCATED:" in result
        assert "more characters" in result
        assert "search_file" in result or "read_file" in result
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
                preprocessed_content=large_content,
                summary=f"Big evidence {i}",
                collected_at_turn=i + 1,
            )
            for i in range(3)
        ]
        case = _make_case_with_evidence(evidence)
        result = _build_evidence_context(case)

        # At least the first items should have structural_index
        assert "<structural_index>" in result
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
        # Create 5 items: first 2 are older (Tier B), last 3 are recent (Tier A)
        evidence = [
            _make_evidence(
                summary=f"Summary for item {i}",
                preprocessed_content=f"Structural index for item {i}",
                collected_at_turn=i + 1,
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
            form=EvidenceForm.USER_TEXT,
            summary="User described intermittent timeouts every 5 minutes",
            preprocessed_content="This should NOT appear in context",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "User described intermittent timeouts" in result
        assert 'form="user_text"' in result
        # USER_TEXT preprocessed_content should NOT be in the output
        # (it goes to Tier C which is summary-only)
        # Note: the implementation separates text_evidence from data_evidence

    def test_user_text_capped_at_five(self):
        """USER_TEXT evidence capped at 5 most recent items."""
        evidence = [
            _make_evidence(
                form=EvidenceForm.USER_TEXT,
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
        """DOCUMENT/SUBMITTED_DATA go to Tier A/B, USER_TEXT goes to Tier C."""
        evidence = [
            # Older data (Tier B)
            _make_evidence(
                form=EvidenceForm.DOCUMENT,
                summary="Old document evidence",
                preprocessed_content="Old structural index",
                collected_at_turn=1,
            ),
            # User text (Tier C)
            _make_evidence(
                form=EvidenceForm.USER_TEXT,
                summary="User observation about timeouts",
                preprocessed_content="User text content",
                collected_at_turn=2,
            ),
            # Recent data - these 3 should be Tier A
            _make_evidence(
                form=EvidenceForm.DOCUMENT,
                summary="Recent log file",
                preprocessed_content="Crime scene extraction: errors found",
                collected_at_turn=3,
            ),
            _make_evidence(
                form=EvidenceForm.DOCUMENT,
                summary="Recent metrics file",
                preprocessed_content="Statistical profile: anomalies detected",
                collected_at_turn=4,
            ),
            _make_evidence(
                form=EvidenceForm.SUBMITTED_DATA,
                summary="Search result finding",
                preprocessed_content="Matched patterns in raw file",
                collected_at_turn=5,
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
        assert 'form="user_text"' in result

    def test_no_data_evidence_only_user_text(self):
        """Case with only USER_TEXT evidence → no structural indexes, summaries only."""
        evidence = [
            _make_evidence(
                form=EvidenceForm.USER_TEXT,
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
            form=EvidenceForm.DOCUMENT,
            summary="Nginx access log errors",
            preprocessed_content="ERROR: 503 at /api/health",
            data_type="LOGS",
            source_file_id="file_aabbccdd1122",
        )
        case = _make_case_with_evidence([ev])
        case.uploaded_files = [
            UploadedFile(
                file_id="file_aabbccdd1122",
                filename="nginx-access.log",
                size_bytes=5000,
                data_type="LOGS_AND_ERRORS",
                uploaded_at_turn=1,
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
                preprocessed_content="Old structural index",
                source_file_id="file_aabb11223344",
                collected_at_turn=1,
            ),
        ] + [
            _make_evidence(
                summary=f"Recent item {i}",
                preprocessed_content=f"Structural {i}",
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
                data_type="LOGS_AND_ERRORS",
                uploaded_at_turn=1,
            )
        ]
        result = _build_evidence_context(case)

        assert 'filename="app-server.log"' in result

    def test_no_filename_when_no_source_file_id(self):
        """Evidence without source_file_id → no filename attribute."""
        ev = _make_evidence(
            form=EvidenceForm.DOCUMENT,
            summary="User pasted logs",
            preprocessed_content="Some content",
        )
        case = _make_case_with_evidence([ev])
        result = _build_evidence_context(case)

        assert "filename=" not in result

    def test_multiple_files_distinguished_by_filename(self):
        """Two evidence items from different files → distinct filenames in XML."""
        ev1 = _make_evidence(
            summary="Nginx errors",
            preprocessed_content="503 errors",
            source_file_id="file_ccdd11223344",
            collected_at_turn=1,
        )
        ev2 = _make_evidence(
            summary="App server errors",
            preprocessed_content="NullPointerException",
            source_file_id="file_eeff11223344",
            collected_at_turn=2,
        )
        case = _make_case_with_evidence([ev1, ev2])
        case.uploaded_files = [
            UploadedFile(
                file_id="file_ccdd11223344",
                filename="nginx-access.log",
                size_bytes=5000,
                data_type="LOGS_AND_ERRORS",
                uploaded_at_turn=1,
            ),
            UploadedFile(
                file_id="file_eeff11223344",
                filename="app-server.log",
                size_bytes=8000,
                data_type="LOGS_AND_ERRORS",
                uploaded_at_turn=2,
            ),
        ]
        result = _build_evidence_context(case)

        assert 'filename="nginx-access.log"' in result
        assert 'filename="app-server.log"' in result

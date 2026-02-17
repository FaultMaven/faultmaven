"""Unit tests for Gap #20: Unified Data Processing — Evidence Form Determination.

Tests:
- EvidenceForm enum has all expected values
- Evidence model accepts source_file_id (optional)
- _determine_evidence_form() maps SubmissionClassification to correct EvidenceForm
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import _determine_evidence_form
from faultmaven.modules.case.contracts import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)


class TestEvidenceFormEnum:
    """Test EvidenceForm enum values and backward compatibility."""

    def test_all_values_defined(self):
        """EvidenceForm has DOCUMENT, USER_TEXT, SUBMITTED_DATA."""
        values = {e.value for e in EvidenceForm}
        assert values == {"document", "user_text", "submitted_data"}

    def test_values_constructable(self):
        """Enum values can be constructed from strings (JSON deserialization)."""
        assert EvidenceForm("document") == EvidenceForm.DOCUMENT
        assert EvidenceForm("user_text") == EvidenceForm.USER_TEXT
        assert EvidenceForm("submitted_data") == EvidenceForm.SUBMITTED_DATA

    def test_enum_is_str(self):
        """EvidenceForm values are strings for JSON serialization."""
        for e in EvidenceForm:
            assert isinstance(e.value, str)


class TestEvidenceSourceFileId:
    """Test Evidence model source_file_id field."""

    def _make_evidence(self, **overrides) -> Evidence:
        defaults = {
            "evidence_id": "ev_abcdef012345",
            "summary": "Test evidence",
            "category": EvidenceCategory.SYMPTOM_EVIDENCE,
            "source_type": EvidenceSourceType.LOGS,
            "form": EvidenceForm.USER_TEXT,
            "collected_at": datetime.now(UTC),
            "collected_by": "user_123",
            "collected_at_turn": 1,
            "primary_purpose": "Testing",
            "preprocessed_content": "test content",
            "content_size_bytes": 12,
            "preprocessing_method": "none",
        }
        defaults.update(overrides)
        return Evidence(**defaults)

    def test_source_file_id_defaults_none(self):
        """source_file_id defaults to None when not provided."""
        ev = self._make_evidence()
        assert ev.source_file_id is None

    def test_source_file_id_set(self):
        """source_file_id can be set to a file ID."""
        ev = self._make_evidence(source_file_id="file_abc123")
        assert ev.source_file_id == "file_abc123"

    def test_source_file_id_serializes(self):
        """source_file_id survives model_dump → Evidence round-trip."""
        ev = self._make_evidence(source_file_id="file_xyz999")
        data = ev.model_dump(mode="json")
        restored = Evidence(**data)
        assert restored.source_file_id == "file_xyz999"

    def test_legacy_evidence_without_source_file_id(self):
        """Evidence dict without source_file_id (old data) deserializes with None default."""
        ev = self._make_evidence()
        data = ev.model_dump(mode="json")
        # Simulate old serialized data that doesn't have source_file_id
        data.pop("source_file_id", None)
        restored = Evidence(**data)
        assert restored.source_file_id is None


class TestDetermineEvidenceForm:
    """Test _determine_evidence_form() helper function."""

    def test_none_returns_user_text(self):
        """No classification → default to USER_TEXT."""
        assert _determine_evidence_form(None) is EvidenceForm.USER_TEXT

    def test_user_text_classification(self):
        """'user_text' classification → USER_TEXT form."""
        sc = SimpleNamespace(type="user_text")
        assert _determine_evidence_form(sc) is EvidenceForm.USER_TEXT

    def test_submitted_data_classification(self):
        """'submitted_data' classification → SUBMITTED_DATA form."""
        sc = SimpleNamespace(type="submitted_data")
        assert _determine_evidence_form(sc) is EvidenceForm.SUBMITTED_DATA

    def test_mixed_classification(self):
        """'mixed' classification → SUBMITTED_DATA form (data takes priority)."""
        sc = SimpleNamespace(type="mixed")
        assert _determine_evidence_form(sc) is EvidenceForm.SUBMITTED_DATA

    def test_unknown_type_defaults_user_text(self):
        """Unknown classification type → USER_TEXT (safe default)."""
        sc = SimpleNamespace(type="something_unknown")
        assert _determine_evidence_form(sc) is EvidenceForm.USER_TEXT

    def test_missing_type_attr_defaults_user_text(self):
        """Object without .type attribute → USER_TEXT (safe default)."""
        sc = SimpleNamespace()  # no type attribute
        assert _determine_evidence_form(sc) is EvidenceForm.USER_TEXT

    def test_with_real_submission_classification(self):
        """Works with the actual SubmissionClassification schema object."""
        from faultmaven.core.investigation.schemas import SubmissionClassification

        sc = SubmissionClassification(
            type="submitted_data",
            confidence="high",
            reasoning="User pasted log data",
        )
        assert _determine_evidence_form(sc) is EvidenceForm.SUBMITTED_DATA

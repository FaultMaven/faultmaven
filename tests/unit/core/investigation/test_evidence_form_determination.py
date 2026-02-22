"""Unit tests for Evidence Form Determination.

Tests:
- EvidenceForm enum has all expected values
- Evidence model accepts source_file_id (optional)
- Payload-driven form assignment: attachments → DOCUMENT, agent findings → SUBMITTED_DATA
"""

from datetime import UTC, datetime

import pytest

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


class TestPayloadDrivenFormAssignment:
    """Test that evidence form is determined by payload context, not LLM classification.

    In the unified turn pipeline:
    - Attachments (file uploads, pasted data) → DOCUMENT (set in _preprocess_attachment)
    - Agent-derived findings (evidence_to_add) → SUBMITTED_DATA (set in milestone engine)
    - Query-only turns with no evidence → USER_TEXT (not created as evidence)
    """

    def _make_evidence(self, **overrides) -> Evidence:
        defaults = {
            "evidence_id": "ev_abcdef012345",
            "summary": "Test evidence",
            "category": EvidenceCategory.SYMPTOM_EVIDENCE,
            "source_type": EvidenceSourceType.LOGS,
            "form": EvidenceForm.SUBMITTED_DATA,
            "collected_at": datetime.now(UTC),
            "collected_by": "user_123",
            "collected_at_turn": 1,
            "primary_purpose": "Investigation context",
            "preprocessed_content": "test content",
            "content_size_bytes": 12,
            "preprocessing_method": "none",
        }
        defaults.update(overrides)
        return Evidence(**defaults)

    def test_attachment_evidence_is_document(self):
        """Evidence created from attachments has form=DOCUMENT."""
        ev = self._make_evidence(form=EvidenceForm.DOCUMENT)
        assert ev.form == EvidenceForm.DOCUMENT

    def test_agent_finding_evidence_is_submitted_data(self):
        """Evidence from evidence_to_add (agent findings) has form=SUBMITTED_DATA."""
        ev = self._make_evidence(form=EvidenceForm.SUBMITTED_DATA)
        assert ev.form == EvidenceForm.SUBMITTED_DATA

    def test_user_text_evidence(self):
        """USER_TEXT form is valid for conversational evidence."""
        ev = self._make_evidence(form=EvidenceForm.USER_TEXT)
        assert ev.form == EvidenceForm.USER_TEXT

    def test_attachment_evidence_has_preprocessing(self):
        """Attachment evidence should have preprocessing metadata."""
        ev = self._make_evidence(
            form=EvidenceForm.DOCUMENT,
            preprocessing_method="crime_scene",
            data_type="LOGS",
            content_size_bytes=5000,
        )
        assert ev.form == EvidenceForm.DOCUMENT
        assert ev.preprocessing_method == "crime_scene"
        assert ev.data_type == "LOGS"

    def test_agent_finding_has_no_preprocessing(self):
        """Agent-derived evidence has preprocessing_method='none'."""
        ev = self._make_evidence(
            form=EvidenceForm.SUBMITTED_DATA,
            preprocessing_method="none",
            data_type=None,
        )
        assert ev.form == EvidenceForm.SUBMITTED_DATA
        assert ev.preprocessing_method == "none"
        assert ev.data_type is None

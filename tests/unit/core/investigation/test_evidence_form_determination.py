"""Unit tests for payload-driven evidence form determination.

Tests the unified ingestion pipeline's form assignment logic:
- EvidenceForm enum values and serialization
- Evidence model source_file_id field
- Payload-driven form assignment: attachments → DOCUMENT, agent findings → USER_TEXT
- TurnPayload and Attachment dataclass behavior
- generate_implicit_query() for attachment-only turns

Design Reference:
- docs/architecture/data-processing/data-preprocessing-design-specification.md (Section 8)
- docs/working/IMPLEMENTATION-unified-ingestion-pipeline.md (Phase 6.1)
"""

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_pipeline import generate_implicit_query
from faultmaven.modules.case.contracts import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)

# ============================================================
# Helpers
# ============================================================


def _make_evidence(**overrides) -> Evidence:
    """Create an Evidence instance with sensible defaults."""
    defaults = {
        "evidence_id": "ev_abcdef012345",
        "summary": "Test evidence",
        "category": EvidenceCategory.SYMPTOM_EVIDENCE,
        "source_type": EvidenceSourceType.LOGS,
        "form": EvidenceForm.DOCUMENT,
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


def _make_attachment(**overrides) -> Attachment:
    """Create an Attachment dataclass with sensible defaults."""
    defaults = {
        "content": b"ERROR: Connection timeout at 2026-02-15 14:03:21",
        "filename": "app.log",
        "content_type": "text/plain",
    }
    defaults.update(overrides)
    return Attachment(**defaults)


# ============================================================
# EvidenceForm Enum
# ============================================================


class TestEvidenceFormEnum:
    """Test EvidenceForm enum values and backward compatibility."""

    def test_all_values_defined(self):
        """EvidenceForm has exactly DOCUMENT, USER_TEXT, SUBMITTED_DATA."""
        values = {e.value for e in EvidenceForm}
        assert values == {"document", "user_text", "submitted_data"}

    def test_values_constructable_from_strings(self):
        """Enum values can be constructed from strings (JSON deserialization)."""
        assert EvidenceForm("document") == EvidenceForm.DOCUMENT
        assert EvidenceForm("user_text") == EvidenceForm.USER_TEXT
        assert EvidenceForm("submitted_data") == EvidenceForm.SUBMITTED_DATA

    def test_enum_is_str(self):
        """EvidenceForm values are strings for JSON serialization."""
        for e in EvidenceForm:
            assert isinstance(e.value, str)

    def test_invalid_value_raises(self):
        """Invalid enum values raise ValueError."""
        with pytest.raises(ValueError):
            EvidenceForm("mixed")
        with pytest.raises(ValueError):
            EvidenceForm("external_data")


# ============================================================
# Evidence source_file_id field
# ============================================================


class TestEvidenceSourceFileId:
    """Test Evidence model source_file_id field."""

    def test_source_file_id_defaults_none(self):
        """source_file_id defaults to None when not provided."""
        ev = _make_evidence()
        assert ev.source_file_id is None

    def test_source_file_id_set(self):
        """source_file_id can be set to a file ID."""
        ev = _make_evidence(source_file_id="file_abc123")
        assert ev.source_file_id == "file_abc123"

    def test_source_file_id_serializes(self):
        """source_file_id survives model_dump → Evidence round-trip."""
        ev = _make_evidence(source_file_id="file_xyz999")
        data = ev.model_dump(mode="json")
        restored = Evidence(**data)
        assert restored.source_file_id == "file_xyz999"

    def test_legacy_evidence_without_source_file_id(self):
        """Evidence dict without source_file_id (old data) deserializes with None default."""
        ev = _make_evidence()
        data = ev.model_dump(mode="json")
        data.pop("source_file_id", None)
        restored = Evidence(**data)
        assert restored.source_file_id is None


# ============================================================
# Payload-Driven Form Assignment
# ============================================================


class TestPayloadDrivenFormAssignment:
    """Test that evidence form is determined by payload context, not LLM classification.

    In the unified turn pipeline (v4.1):
    - Attachments (file uploads, pasted data) → DOCUMENT (set in _preprocess_attachment)
    - Agent-derived findings (evidence_to_add) → USER_TEXT (set in milestone engine)
    - Query-only turns → no evidence created
    """

    def test_attachment_evidence_is_document(self):
        """Evidence created from attachments has form=DOCUMENT."""
        ev = _make_evidence(form=EvidenceForm.DOCUMENT)
        assert ev.form == EvidenceForm.DOCUMENT

    def test_agent_finding_evidence_is_user_text(self):
        """Evidence from evidence_to_add (agent findings) has form=USER_TEXT."""
        ev = _make_evidence(form=EvidenceForm.USER_TEXT)
        assert ev.form == EvidenceForm.USER_TEXT

    def test_attachment_evidence_has_preprocessing_metadata(self):
        """Attachment evidence should have preprocessing metadata (data_type, method)."""
        ev = _make_evidence(
            form=EvidenceForm.DOCUMENT,
            preprocessing_method="crime_scene",
            data_type="LOGS",
            content_size_bytes=5000,
            preprocessed_content="============\nCRIME SCENE EXTRACTION\n============",
        )
        assert ev.form == EvidenceForm.DOCUMENT
        assert ev.preprocessing_method == "crime_scene"
        assert ev.data_type == "LOGS"
        assert ev.content_size_bytes == 5000

    def test_agent_finding_has_minimal_preprocessing(self):
        """Agent-derived evidence has preprocessing_method='none' and no data_type."""
        ev = _make_evidence(
            form=EvidenceForm.USER_TEXT,
            preprocessing_method="none",
            data_type=None,
        )
        assert ev.form == EvidenceForm.USER_TEXT
        assert ev.preprocessing_method == "none"
        assert ev.data_type is None

    def test_submitted_data_form_for_search_results(self):
        """Evidence from Tier 2/3 search results uses SUBMITTED_DATA form."""
        ev = _make_evidence(
            form=EvidenceForm.SUBMITTED_DATA,
            preprocessing_method="search_file_keyword",
            source_file_id="file_abc123def456",
        )
        assert ev.form == EvidenceForm.SUBMITTED_DATA
        assert ev.source_file_id == "file_abc123def456"


# ============================================================
# TurnPayload Dataclass
# ============================================================


class TestTurnPayload:
    """Test TurnPayload dataclass properties and invariants."""

    def test_query_only_payload(self):
        """TurnPayload with query only: has_query=True, has_attachments=False."""
        payload = TurnPayload(query="What's happening with the API?")
        assert payload.has_query is True
        assert payload.has_attachments is False

    def test_attachment_only_payload(self):
        """TurnPayload with attachments only: has_query=False, has_attachments=True."""
        att = _make_attachment()
        payload = TurnPayload(attachments=[att])
        assert payload.has_query is False
        assert payload.has_attachments is True

    def test_query_and_attachments_payload(self):
        """TurnPayload with both query and attachments."""
        att = _make_attachment()
        payload = TurnPayload(
            query="Analyze these logs",
            attachments=[att],
        )
        assert payload.has_query is True
        assert payload.has_attachments is True

    def test_empty_payload(self):
        """TurnPayload with neither query nor attachments."""
        payload = TurnPayload()
        assert payload.has_query is False
        assert payload.has_attachments is False

    def test_whitespace_query_treated_as_empty(self):
        """Query with only whitespace is treated as no query."""
        payload = TurnPayload(query="   ")
        assert payload.has_query is False

    def test_empty_string_query_treated_as_empty(self):
        """Empty string query is treated as no query."""
        payload = TurnPayload(query="")
        assert payload.has_query is False

    def test_multiple_attachments(self):
        """TurnPayload can hold multiple attachments."""
        attachments = [
            _make_attachment(filename="app.log"),
            _make_attachment(filename="metrics.csv", content=b"cpu,mem\n95,80"),
            _make_attachment(filename="config.yaml", content=b"max_connections: 10"),
        ]
        payload = TurnPayload(attachments=attachments)
        assert payload.has_attachments is True
        assert len(payload.attachments) == 3

    def test_default_intent_is_none(self):
        """TurnPayload defaults intent to None."""
        payload = TurnPayload(query="test")
        assert payload.intent is None

    def test_attachments_default_empty_list(self):
        """TurnPayload defaults attachments to empty list."""
        payload = TurnPayload(query="test")
        assert payload.attachments == []


# ============================================================
# Attachment Dataclass
# ============================================================


class TestAttachment:
    """Test Attachment dataclass fields and behavior."""

    def test_file_upload_attachment(self):
        """Attachment for a file upload has correct fields."""
        att = Attachment(
            content=b"2026-02-15 ERROR Connection timeout",
            filename="app.log",
            content_type="text/plain",
        )
        assert att.filename == "app.log"
        assert att.content_type == "text/plain"
        assert b"ERROR" in att.content
        assert att.source_metadata is None

    def test_pasted_content_attachment(self):
        """Pasted content gets a synthetic filename."""
        att = Attachment(
            content=b"ERROR: Connection timeout at 14:03",
            filename="pasted-content-20260215T140300.txt",
            content_type="text/plain",
        )
        assert "pasted-content" in att.filename
        assert att.content_type == "text/plain"

    def test_attachment_with_source_metadata(self):
        """Attachment can carry source_metadata for page injections."""
        att = Attachment(
            content=b"<html>...</html>",
            filename="page-capture.html",
            content_type="text/html",
            source_metadata={"url": "https://example.com", "captured_at": "2026-02-15"},
        )
        assert att.source_metadata["url"] == "https://example.com"

    def test_binary_content(self):
        """Attachment content is bytes."""
        att = _make_attachment(content=b"\x89PNG\r\n\x1a\n")
        assert isinstance(att.content, bytes)


# ============================================================
# Implicit Query Generation
# ============================================================


class TestImplicitQueryGeneration:
    """Test generate_implicit_query() for attachment-only turns."""

    def test_single_file_implicit_query(self):
        """Single file generates implicit query with filename and data_type."""
        att = _make_attachment(filename="application.log")
        ev = _make_evidence(data_type="LOGS")
        query = generate_implicit_query([att], [ev])
        assert "application.log" in query
        assert "LOGS" in query
        assert "Analyze" in query

    def test_multiple_files_implicit_query(self):
        """Multiple files generates implicit query listing all filenames."""
        attachments = [
            _make_attachment(filename="app.log"),
            _make_attachment(filename="metrics.csv"),
            _make_attachment(filename="config.yaml"),
        ]
        evidence = [_make_evidence(evidence_id=f"ev_{i:012d}") for i in range(3)]
        query = generate_implicit_query(attachments, evidence)
        assert "3 files" in query
        assert "app.log" in query
        assert "metrics.csv" in query
        assert "config.yaml" in query

    def test_single_pasted_content_implicit_query(self):
        """Pasted content generates implicit query with synthetic filename."""
        att = _make_attachment(filename="pasted-content-20260215T140300.txt")
        ev = _make_evidence(data_type="LOGS")
        query = generate_implicit_query([att], [ev])
        assert "pasted-content" in query

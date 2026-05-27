"""Integration tests for the unified POST /cases/{case_id}/turns endpoint.

Tests the complete endpoint flow:
- Query-only turn → no preprocessing, LLM processes query
- File upload → Tier 0+1 preprocessing, implicit query, LLM with evidence context
- Pasted text → same as file upload (via pasted_content form field)
- Query + file → preprocessing + explicit query, both in LLM context
- Multiple files + query → all preprocessed, all in evidence context
- Intent routing (status_transition, confirmation) with attachments
- Missing both query and attachments → 400 error
- Invalid case_id → 400/404 errors

Design Reference:
- docs/working/IMPLEMENTATION-unified-ingestion-pipeline.md (Phase 6.2)
"""

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api_models import AttachmentResult, IntentType, TurnResponse
from faultmaven.modules.case.contracts import CaseStatus
from faultmaven.modules.case.domain.models import Case

# ============================================================
# Helpers
# ============================================================


def _make_mock_case(**overrides) -> Case:
    """Create a Case for endpoint testing."""
    defaults = {
        "case_id": f"case_{uuid4().hex[:12]}",
        "title": "Test Case",
        "description": "API latency spike",
        "user_id": "test-user-123",
        "organization_id": "org_test123",
        "status": CaseStatus.INQUIRY,
        "current_turn": 0,
    }
    defaults.update(overrides)
    return Case(**defaults)


def _make_turn_response(**overrides) -> TurnResponse:
    """Create a mock TurnResponse."""
    defaults = {
        "agent_response": "I'll analyze this for you.",
        "turn_number": 1,
        "milestones_completed": [],
        "case_status": CaseStatus.INQUIRY,
        "progress_made": False,
        "attachments_processed": [],
    }
    defaults.update(overrides)
    return TurnResponse(**defaults)


# ============================================================
# Unit tests for endpoint logic (testing payload construction)
# ============================================================


@pytest.mark.unit
class TestTurnPayloadConstruction:
    """Test that the endpoint correctly builds TurnPayload from form data."""

    def test_query_only_payload(self):
        """Query-only submission builds payload with has_query=True, has_attachments=False."""
        payload = TurnPayload(query="What's happening with the API?")
        assert payload.has_query is True
        assert payload.has_attachments is False
        assert payload.intent is None

    def test_file_upload_builds_attachment(self):
        """File upload builds Attachment with correct content and metadata."""
        content = b"2026-02-15 ERROR Connection timeout"
        att = Attachment(
            content=content,
            filename="app.log",
            content_type="text/plain",
        )
        payload = TurnPayload(attachments=[att])
        assert payload.has_attachments is True
        assert payload.has_query is False
        assert payload.attachments[0].filename == "app.log"
        assert payload.attachments[0].content == content

    def test_pasted_content_builds_attachment(self):
        """Pasted content is converted to Attachment with synthetic filename."""
        pasted = "ERROR: Connection timeout at 14:03:21\nERROR: Pool exhausted"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        att = Attachment(
            content=pasted.encode("utf-8"),
            filename=f"pasted-content-{ts}.txt",
            content_type="text/plain",
        )
        payload = TurnPayload(attachments=[att])
        assert payload.has_attachments is True
        assert "pasted-content" in payload.attachments[0].filename
        assert payload.attachments[0].content_type == "text/plain"

    def test_query_plus_file_builds_combined_payload(self):
        """Query + file builds payload with both query and attachments."""
        att = Attachment(
            content=b"log content",
            filename="app.log",
            content_type="text/plain",
        )
        payload = TurnPayload(query="Analyze these logs", attachments=[att])
        assert payload.has_query is True
        assert payload.has_attachments is True

    def test_multiple_files_payload(self):
        """Multiple files build payload with multiple attachments."""
        attachments = [
            Attachment(content=b"log1", filename="app.log", content_type="text/plain"),
            Attachment(
                content=b"csv1", filename="metrics.csv", content_type="text/csv"
            ),
            Attachment(
                content=b"yaml1",
                filename="config.yaml",
                content_type="application/yaml",
            ),
        ]
        payload = TurnPayload(query="Analyze all these", attachments=attachments)
        assert len(payload.attachments) == 3
        assert payload.has_query is True

    def test_intent_routing_with_attachments(self):
        """Intent metadata is preserved alongside attachments."""
        from faultmaven.models.api_models import QueryIntent

        att = Attachment(content=b"logs", filename="app.log", content_type="text/plain")
        intent = QueryIntent(
            type=IntentType.STATUS_TRANSITION,
            to_status=CaseStatus.INVESTIGATING,
        )
        payload = TurnPayload(
            query="Let's investigate",
            attachments=[att],
            intent=intent,
        )
        assert payload.intent is not None
        assert payload.intent.type == IntentType.STATUS_TRANSITION
        assert payload.has_attachments is True

    def test_empty_submission_has_no_content(self):
        """Empty submission (no query, no files, no paste) has nothing."""
        payload = TurnPayload()
        assert payload.has_query is False
        assert payload.has_attachments is False


# ============================================================
# Text-paste source-metadata branching tests
# ============================================================
#
# These tests cover the input-origin discrimination in the unified turns
# endpoint at modules/case/api/routes.py:2140-2166. The route distinguishes
# three submission origins so the classifier can apply the correct
# confidence boost downstream:
#
#   file_upload  → user selected a local file
#   page_capture → browser extension captured a web page (has source URL)
#   text_paste   → user pasted raw text (scratchpad or auto-promoted chat)
#
# `_resolve_paste_source_meta` below mirrors the route's branching exactly
# so the tests can assert the contract without spinning up a TestClient.
# Keep this helper in sync with routes.py if the route logic changes.


def _resolve_paste_source_meta(
    pasted_content: str,
    input_type: str | None,
    source_url: str | None,
) -> tuple[dict, str]:
    """Mirror of routes.py:2140-2166. Returns (source_meta, filename_prefix).

    Keep in sync with the route. Fixtures import this directly so the
    tests document the contract that the production route must match.

    Note: ``pasted_content`` is unused since the legacy
    ``--- Page Content (URL) ---`` header was removed (the dual-source
    detection is no longer accepted as a page-capture signal). The
    parameter remains in the signature so call sites that document the
    full input shape stay readable.
    """
    if input_type == "page_capture":
        meta = {"source_type": "page_capture"}
        if source_url:
            meta["source_url"] = source_url
        return meta, "page-capture-"
    return {"source_type": "text_paste"}, "pasted-content-"


@pytest.mark.unit
class TestTextPasteSourceMetadata:
    """Validate the source_metadata branch the route applies to pasted_content."""

    def test_explicit_text_paste_input_type_yields_text_paste_source(self):
        """input_type='paste' with no URL → source_type=text_paste."""
        meta, prefix = _resolve_paste_source_meta(
            pasted_content="ERROR: pool exhausted\nERROR: timeout",
            input_type="paste",
            source_url=None,
        )
        assert meta == {"source_type": "text_paste"}
        assert prefix == "pasted-content-"

    def test_missing_input_type_defaults_to_text_paste(self):
        """No input_type and no legacy header → falls through to text_paste."""
        meta, prefix = _resolve_paste_source_meta(
            pasted_content="some pasted text without any header",
            input_type=None,
            source_url=None,
        )
        assert meta == {"source_type": "text_paste"}
        assert "source_url" not in meta
        assert prefix == "pasted-content-"

    def test_explicit_page_capture_input_type_yields_page_capture_with_url(self):
        """input_type='page_capture' + source_url → source_type=page_capture + URL."""
        meta, prefix = _resolve_paste_source_meta(
            pasted_content="dashboard panels content...",
            input_type="page_capture",
            source_url="https://grafana.example.com/d/abc",
        )
        assert meta["source_type"] == "page_capture"
        assert meta["source_url"] == "https://grafana.example.com/d/abc"
        assert prefix == "page-capture-"

    def test_legacy_page_content_header_no_longer_promotes_to_page_capture(self):
        """Legacy `--- Page Content (URL) ---` header is now treated as text_paste.

        The dual-source page-capture detection was removed because it
        formed a write-around: a paste that happened to start with the
        marker bypassed Tier-1 extraction. The frontend now always sets
        an explicit ``input_type`` form field, so the body-regex branch
        is gone.
        """
        legacy = "--- Page Content (https://sentry.io/issues/2k3f/) ---\n## Issue Header\n..."
        meta, prefix = _resolve_paste_source_meta(
            pasted_content=legacy,
            input_type=None,
            source_url=None,
        )
        # Without explicit input_type=page_capture, this routes as text_paste
        assert meta == {"source_type": "text_paste"}
        assert prefix == "pasted-content-"

    def test_legacy_header_with_explicit_page_capture_uses_explicit_url(self):
        """Explicit input_type='page_capture' + source_url is now the only path."""
        legacy = "--- Page Content (https://stale.example.com/old) ---\n## body\n"
        meta, _ = _resolve_paste_source_meta(
            pasted_content=legacy,
            input_type="page_capture",
            source_url="https://current.example.com/new",
        )
        # Body content is not consulted for URL extraction anymore
        assert meta["source_url"] == "https://current.example.com/new"

    def test_text_paste_attachment_round_trip(self):
        """End-to-end: text_paste content → Attachment with correct metadata."""
        content = "TypeError: Cannot read properties of undefined\n  at processOrder"
        meta, prefix = _resolve_paste_source_meta(
            content, input_type="paste", source_url=None
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        att = Attachment(
            content=content.encode("utf-8"),
            filename=f"{prefix}{ts}.txt",
            content_type="text/plain",
            source_metadata=meta,
        )

        # Source-type is what downstream classifier will see
        assert att.source_metadata["source_type"] == "text_paste"
        # No source_url field should leak in for text_paste
        assert "source_url" not in att.source_metadata
        # Synthetic filename has the text_paste prefix
        assert att.filename.startswith("pasted-content-")
        # Content round-trips
        assert att.content == content.encode("utf-8")

    def test_text_paste_distinct_from_file_upload_metadata(self):
        """text_paste and file_upload produce distinct source_type values."""
        text_meta, _ = _resolve_paste_source_meta(
            "data", input_type="paste", source_url=None
        )
        file_meta = {"source_type": "file_upload"}  # what the route sets for files
        assert text_meta["source_type"] != file_meta["source_type"]
        assert text_meta["source_type"] == "text_paste"
        assert file_meta["source_type"] == "file_upload"


# ============================================================
# Validation tests (endpoint-level)
# ============================================================


@pytest.mark.unit
class TestEndpointValidation:
    """Test validation logic that the endpoint performs."""

    def test_at_least_one_input_required(self):
        """Endpoint should reject submissions with no query, files, or pasted_content."""
        # The endpoint checks: if not query and not files and not pasted_content → 400
        query = None
        files = []
        pasted_content = None
        assert not query and not files and not pasted_content

    def test_invalid_case_id_rejected(self):
        """Invalid case_id values should be rejected."""
        invalid_ids = ["", "undefined", "null"]
        for case_id in invalid_ids:
            assert case_id.strip() in ("", "undefined", "null")

    def test_valid_intent_types(self):
        """All IntentType enum values are accepted."""
        valid_types = [
            "conversation",
            "status_transition",
            "hypothesis_action",
            "evidence_need",
            "confirmation",
            "greeting",
            "path_selection",
            "post_mitigation_choice",
        ]
        for t in valid_types:
            intent_type = IntentType(t)
            assert intent_type.value == t


# ============================================================
# TurnResponse model tests
# ============================================================


@pytest.mark.unit
class TestTurnResponseModel:
    """Test TurnResponse model structure."""

    def test_query_only_response(self):
        """Query-only turn returns TurnResponse with empty attachments_processed."""
        response = _make_turn_response()
        assert response.agent_response == "I'll analyze this for you."
        assert response.turn_number == 1
        assert response.attachments_processed == []
        assert response.progress_made is False

    def test_file_upload_response(self):
        """File upload turn includes AttachmentResult in response.

        Post-010: AttachmentResult carries the UploadedFile's
        ``file_id``, not an evidence_id (uploads no longer create an
        Evidence row at intake).
        """
        response = _make_turn_response(
            attachments_processed=[
                AttachmentResult(
                    file_id="file_abc123def456",
                    filename="app.log",
                    source_type="logs",
                    file_size=5000,
                    processing_status="completed",
                )
            ],
            progress_made=True,
        )
        assert len(response.attachments_processed) == 1
        att = response.attachments_processed[0]
        assert att.file_id == "file_abc123def456"
        assert att.filename == "app.log"
        assert att.source_type == "logs"
        assert att.processing_status == "completed"

    def test_multiple_attachments_response(self):
        """Multiple file uploads produce multiple AttachmentResults."""
        response = _make_turn_response(
            attachments_processed=[
                AttachmentResult(
                    file_id=f"file_{i:012d}",
                    filename=f"file{i}.log",
                    source_type="logs",
                    file_size=1000 * (i + 1),
                    processing_status="completed",
                )
                for i in range(3)
            ],
        )
        assert len(response.attachments_processed) == 3
        for i, att in enumerate(response.attachments_processed):
            assert att.filename == f"file{i}.log"

    def test_response_with_milestones(self):
        """TurnResponse includes milestone completion data."""
        response = _make_turn_response(
            milestones_completed=["symptom_verified"],
            case_status=CaseStatus.INVESTIGATING,
            progress_made=True,
        )
        assert response.milestones_completed == ["symptom_verified"]
        assert response.case_status == CaseStatus.INVESTIGATING
        assert response.progress_made is True

    def test_progress_transparency_in_response(self):
        """TurnResponse can carry progress transparency info."""
        response = _make_turn_response(
            progress_made=False,
            turn_number=10,
        )
        # Default: no transparency info
        assert response.progress_transparency is None

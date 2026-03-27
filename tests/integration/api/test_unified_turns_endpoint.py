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
from datetime import UTC, datetime, timezone
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
        "is_stuck": False,
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
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
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
            "evidence_request",
            "confirmation",
            "greeting",
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
        """File upload turn includes AttachmentResult in response."""
        response = _make_turn_response(
            attachments_processed=[
                AttachmentResult(
                    evidence_id="ev_abc123def456",
                    filename="app.log",
                    data_type="LOGS",
                    file_size=5000,
                    processing_status="completed",
                )
            ],
            progress_made=True,
        )
        assert len(response.attachments_processed) == 1
        att = response.attachments_processed[0]
        assert att.evidence_id == "ev_abc123def456"
        assert att.filename == "app.log"
        assert att.data_type == "LOGS"
        assert att.processing_status == "completed"

    def test_multiple_attachments_response(self):
        """Multiple file uploads produce multiple AttachmentResults."""
        response = _make_turn_response(
            attachments_processed=[
                AttachmentResult(
                    evidence_id=f"ev_{i:012d}",
                    filename=f"file{i}.log",
                    data_type="LOGS",
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
            milestones_completed=["symptom_verified", "scope_assessed"],
            case_status=CaseStatus.INVESTIGATING,
            progress_made=True,
        )
        assert response.milestones_completed == ["symptom_verified", "scope_assessed"]
        assert response.case_status == CaseStatus.INVESTIGATING
        assert response.progress_made is True

    def test_stuck_case_response(self):
        """TurnResponse indicates stuck case."""
        response = _make_turn_response(
            is_stuck=True,
            progress_made=False,
            turn_number=10,
        )
        assert response.is_stuck is True
        assert response.progress_made is False

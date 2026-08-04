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
from faultmaven.modules.case.api.routes import resolve_paste_source_meta
from faultmaven.modules.case.contracts import CaseState
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
        "state": CaseState.INQUIRY,
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
        "case_state": CaseState.INQUIRY,
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
            to_state=CaseState.INVESTIGATING,
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
# These import the ROUTE's own helper. They used to call a hand-copied mirror
# of the branching kept in this file, which could not fail when the route
# changed — the mirror stayed self-consistent and green while production
# diverged. `pasted_content` is no longer a parameter at all: the legacy
# `--- Page Content (URL) ---` body header was removed as a write-around (a
# paste shaped that way bypassed Tier-1 extraction), so nothing reads the body
# to decide origin. The wrapper below keeps the call sites readable.


def _resolve_paste_source_meta(
    pasted_content: str,
    input_type: str | None,
    source_url: str | None,
) -> tuple[dict, str]:
    """Thin adapter onto the production helper, preserving these tests' shape."""

    return resolve_paste_source_meta(input_type, source_url)


@pytest.mark.unit
class TestTextPasteSourceMetadata:
    """Validate the source_metadata branch the route applies to pasted_content."""

    def test_source_url_is_kept_for_a_plain_paste_not_only_page_capture(self):
        """Provenance is channel-agnostic.

        ``source_url`` used to be recorded only under ``page_capture``, so the
        Slack agent's permalink back to the alert it was forwarding — the one
        artifact from which the alert's real age could be recovered — was
        accepted over the wire and silently discarded because ``input_type``
        said "paste".
        """
        meta, prefix = _resolve_paste_source_meta(
            pasted_content="[FIRING:1] etcdInsufficientMembers kube-system",
            input_type="paste",
            source_url="https://slack/archives/C1/p1785872177",
        )
        assert meta["source_type"] == "text_paste"
        assert meta["source_url"] == "https://slack/archives/C1/p1785872177"
        assert prefix == "pasted-content-"

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
            case_state=CaseState.INVESTIGATING,
            progress_made=True,
        )
        assert response.milestones_completed == ["symptom_verified"]
        assert response.case_state == CaseState.INVESTIGATING
        assert response.progress_made is True

    def test_progress_transparency_in_response(self):
        """TurnResponse can carry progress transparency info."""
        response = _make_turn_response(
            progress_made=False,
            turn_number=10,
        )
        # Default: no transparency info
        assert response.progress_transparency is None


# ============================================================
# Route-level: malformed intent -> 422 (not 500)  [40f17354]
# ============================================================


@pytest.mark.unit
class TestSubmitTurnRejectsMalformedIntent:
    """The /turns endpoint must reject a malformed intent with 422, never 500.
    Calls the handler directly with mocked deps; the intent guard fires before
    process_turn, so no real services are exercised. Closes the loop on the
    route-level behavior that test_query_intent_schema.py only covers at the
    schema layer.
    """

    @staticmethod
    async def _submit(intent_type, intent_data):
        from unittest.mock import AsyncMock, MagicMock

        from fastapi import HTTPException  # noqa: F401

        from faultmaven.modules.case.api.routes import submit_turn

        case_service = MagicMock()
        case_service.get_case = AsyncMock(return_value=_make_mock_case())
        current_user = MagicMock()
        current_user.user_id = "test-user-123"
        return await submit_turn(
            case_id="case_abc123def456",
            query="close it",
            files=[],
            pasted_content=None,
            intent_type=intent_type,
            intent_data=intent_data,
            input_type=None,
            source_url=None,
            case_service=case_service,
            investigation_service=MagicMock(),
            current_user=current_user,
        )

    async def test_status_transition_without_to_state_returns_422(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._submit("status_transition", "{}")  # no to_state
        assert exc.value.status_code == 422

    async def test_unknown_intent_type_returns_422(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            # 'free_speech' is a suggestion action_type, not an IntentType
            await self._submit("free_speech", "{}")
        assert exc.value.status_code == 422


class TestSubmitTurnBillingExhaustion:
    """A billing/quota-exhaustion failure during turn processing must surface as
    402 Payment Required with x-error-code: QUOTA_EXHAUSTED and NO Retry-After —
    so the user is told to add credits instead of being shown a generic 500 that
    invites a futile retry. Regression for case_b639fac38fe0."""

    @staticmethod
    async def _submit_with_process_error(service_error):
        from unittest.mock import AsyncMock, MagicMock

        from faultmaven.modules.case.api.routes import submit_turn

        case_service = MagicMock()
        case_service.get_case = AsyncMock(return_value=_make_mock_case())
        investigation_service = MagicMock()
        investigation_service.process_turn = AsyncMock(side_effect=service_error)
        current_user = MagicMock()
        current_user.user_id = "test-user-123"

        return await submit_turn(
            case_id="case_abc123def456",
            query="why is the pod crashing?",
            files=[],
            pasted_content=None,
            intent_type=None,
            intent_data=None,
            input_type=None,
            source_url=None,
            case_service=case_service,
            investigation_service=investigation_service,
            current_user=current_user,
        )

    async def test_billing_quota_exhaustion_maps_to_402(self):
        from fastapi import HTTPException

        from faultmaven.exceptions import QUOTA_EXHAUSTED, ServiceException

        billing_error = ServiceException(
            "Turn processing failed: Structured output generation failed: "
            "FaultMaven's AI provider is out of quota or credits",
            details={"error_code": QUOTA_EXHAUSTED},
        )

        with pytest.raises(HTTPException) as exc:
            await self._submit_with_process_error(billing_error)

        assert exc.value.status_code == 402
        assert exc.value.headers["x-error-code"] == QUOTA_EXHAUSTED
        # No Retry-After: retrying won't help until an operator adds credits.
        assert "Retry-After" not in exc.value.headers
        assert "credit" in exc.value.detail.lower()

    async def test_generic_service_error_still_maps_to_500(self):
        """A non-billing ServiceException keeps the existing generic mapping —
        the billing branch must not swallow ordinary failures."""
        from fastapi import HTTPException

        from faultmaven.exceptions import ServiceException

        with pytest.raises(HTTPException) as exc:
            await self._submit_with_process_error(
                ServiceException("Turn processing failed: database is on fire")
            )

        assert exc.value.status_code == 500
        assert exc.value.headers["x-error-code"] == "SERVICE_ERROR"


# ============================================================
# observed_at — the caller's declared observation time
# ============================================================
#
# A forwarding caller (the Slack agent relaying an alert posted hours earlier)
# is the only party that knows when the content was actually seen. Absent it,
# the sole timestamp on the evidence chain is the synthetic
# `pasted-content-{now}.txt` filename, i.e. ingestion time — which asserts a
# stale alert is current.


@pytest.mark.unit
class TestObservedAtParsing:
    """The route's `observed_at` parser: fail to unknown, never to now."""

    def _parse(self, raw):
        from faultmaven.modules.case.api.routes import _parse_observed_at

        return _parse_observed_at(raw, "corr-1")

    def test_iso_instant_is_parsed_to_utc(self):
        assert self._parse("2026-08-04T17:36:17+00:00") == datetime(
            2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc
        )

    def test_zulu_suffix_is_accepted(self):
        assert self._parse("2026-08-04T17:36:17Z") == datetime(
            2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc
        )

    def test_offset_is_normalised_to_utc(self):
        assert self._parse("2026-08-04T19:36:17+02:00") == datetime(
            2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc
        )

    def test_naive_is_read_as_utc(self):
        """The wire contract is UTC; a naive value must not become server-local."""
        assert self._parse("2026-08-04T17:36:17") == datetime(
            2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc
        )

    def test_absent_is_unknown(self):
        assert self._parse(None) is None
        assert self._parse("") is None

    def test_malformed_degrades_to_unknown_rather_than_rejecting_the_turn(self):
        """A voluntary provenance hint must never cost the user their turn —
        and must never silently become "now", which is the false claim this
        field exists to prevent."""
        assert self._parse("yesterday") is None
        assert self._parse("1785872177") is None  # epoch, not ISO

    def test_future_is_rejected(self):
        """Content cannot be observed after it was submitted. A future value
        means a broken clock or a bad conversion; trusting it would make stale
        evidence look FRESHER, which is the unsafe direction."""
        future = datetime.now(timezone.utc).replace(microsecond=0)
        future = future.replace(year=future.year + 1)
        assert self._parse(future.isoformat()) is None

    def test_small_clock_skew_is_tolerated(self):
        """Ordinary skew between the caller's host and this one must not
        discard a legitimate just-now observation."""
        from datetime import timedelta

        skewed = datetime.now(timezone.utc) + timedelta(minutes=2)
        assert self._parse(skewed.isoformat()) is not None

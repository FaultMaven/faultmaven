"""Integration tests for document-to-runbook conversion API endpoints.

Tests the complete conversion API flow including:
- POST   /knowledge/convert
- GET    /knowledge/conversions
- GET    /knowledge/conversions/{id}
- PUT    /knowledge/conversions/{id}/drafts/{draft_id}
- POST   /knowledge/conversions/{id}/drafts/{draft_id}/verify
- DELETE /knowledge/conversions/{id}/drafts/{draft_id}

These tests use a minimal FastAPI app with the conversion router and mock
the ConversionService at the app.state level, plus override auth dependencies.
"""

import os
import sys

# Ensure root conftest mocks are loaded before any faultmaven imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import conftest as root_conftest  # noqa: F401
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.knowledge.api.conversion_routes import (
    _get_conversion_service,
    _require_auth,
)
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionErrorCode,
    ConversionResponse,
    ConversionStatus,
    DraftStatus,
    FailureModeAnalysis,
    QualityScore,
    SourceAssessment,
    SourceFileInfo,
    ValidationResult,
    VerifyResponse,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionRejectedError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)


def _make_user(roles=None, user_id="user-001", username="testuser"):
    """Create a DevUser for testing."""
    return DevUser(
        user_id=user_id,
        username=username,
        email="test@example.com",
        display_name="Test User",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        roles=roles or ["user"],
        organization_id="org-001",
    )


def _make_draft(
    draft_id="draft_abc123",
    runbook_id="nginx-502-bad-gateway",
    title="Nginx 502 Bad Gateway",
    scope="personal",
    status=DraftStatus.DRAFT,
    validation_passed=True,
):
    """Create a ConversionDraft for testing."""
    return ConversionDraft(
        draft_id=draft_id,
        runbook_id=runbook_id,
        title=title,
        scope=scope,
        status=status,
        validation=ValidationResult(
            passed=validation_passed,
            errors=[] if validation_passed else ["Missing Diagnostic Steps"],
            warnings=[],
        ),
        quality_score=QualityScore(
            overall=72.0,
            grade="B",
            completeness=0.8,
            clarity=0.7,
            actionability=0.65,
            comprehensiveness=0.75,
        ),
        file_path="/tmp/test/nginx-502-bad-gateway.md",
        content_preview="---\nid: nginx-502-bad-gateway\ntitle: ...",
        content="---\nid: nginx-502-bad-gateway\ntitle: Nginx 502 Bad Gateway\n---\n# Full content here",
    )


def _make_analysis():
    return AnalysisResult(
        is_actionable=True,
        failure_modes=[
            FailureModeAnalysis(
                id="nginx-502",
                title="Nginx 502 Bad Gateway",
                domain="networking",
                service="nginx",
                symptom_class=["http_error", "gateway_timeout"],
                severity="high",
                symptoms_summary="502 errors in access logs",
                resolution_summary="Restart upstream, check connection pool",
            )
        ],
        source_assessment=SourceAssessment(
            content_type="troubleshooting_guide",
            actionability_rating="high",
            missing_information=[],
        ),
    )


def _make_conversion_response(
    conversion_id="conv_abc123",
    status=ConversionStatus.COMPLETED,
    drafts=None,
):
    return ConversionResponse(
        conversion_id=conversion_id,
        status=status,
        source_file=SourceFileInfo(
            filename="nginx-troubleshooting.txt",
            size_bytes=2048,
            content_type="text/plain",
            retained_path="/tmp/test/sources/conv_abc123/nginx-troubleshooting.txt",
        ),
        analysis=_make_analysis(),
        drafts=drafts if drafts is not None else [_make_draft()],
        warnings=[],
        created_at=NOW,
    )


@pytest.fixture
def mock_conversion_service():
    """Create a mock ConversionService with default happy-path responses."""
    service = AsyncMock()
    service.convert_document = AsyncMock(return_value=_make_conversion_response())
    service.list_conversions = AsyncMock(
        return_value=[
            {
                "conversion_id": "conv_abc123",
                "status": "completed",
                "source_filename": "nginx-troubleshooting.txt",
                "failure_modes_detected": 1,
                "scope": "personal",
                "created_at": NOW.isoformat(),
            }
        ]
    )
    service.get_conversion = AsyncMock(return_value=_make_conversion_response())
    service.update_draft = AsyncMock(return_value=_make_draft())
    service.verify_draft = AsyncMock(
        return_value=VerifyResponse(
            draft_id="draft_abc123",
            runbook_id="nginx-502-bad-gateway",
            status="verified",
            knowledge_item_id="ki-001",
            ingested=True,
            ingested_at=NOW,
            collection="personal_kb",
            chunks_created=5,
        )
    )
    service.delete_draft = AsyncMock(return_value=True)
    return service


def _build_app(mock_service, user):
    """Build a minimal FastAPI app with conversion routes and overridden deps.

    Registers the production exception handlers via
    ``get_exception_handlers()`` so typed service exceptions
    (NotFoundError / ConflictError / ValidationException) translate to
    404 / 409 / 422 in the integration tests, matching production
    behavior. Without this, the typed exceptions propagate uncaught
    past FastAPI's dispatch chain and surface as raw 500-shaped
    transport errors — exactly the failure mode that broke PR #334's
    CI (the new typed-exception assertions saw uncaught exceptions
    instead of the expected status codes).
    """
    from faultmaven.api.exception_handlers import get_exception_handlers

    app = FastAPI()
    app.include_router(conversion_router, prefix="/api/v1")
    app.state.conversion_service = mock_service

    # Register the same exception handlers main.py installs so typed
    # service exceptions get translated by the same dispatch the
    # production app uses. Drift between the two would mean integration
    # tests don't exercise the real handler chain.
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)

    # Override auth and service dependencies
    app.dependency_overrides[_require_auth] = lambda: user
    app.dependency_overrides[_get_conversion_service] = lambda: mock_service

    return app


@pytest.fixture
def regular_user():
    return _make_user(roles=["user"])


@pytest.fixture
def admin_user():
    return _make_user(roles=["admin", "user"])


@pytest.fixture
def app_with_user(mock_conversion_service, regular_user):
    """App authenticated as a regular (non-admin) user."""
    return _build_app(mock_conversion_service, regular_user)


@pytest.fixture
def app_with_admin(mock_conversion_service, admin_user):
    """App authenticated as an admin user."""
    return _build_app(mock_conversion_service, admin_user)


async def _client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

API_PREFIX = "/api/v1/knowledge"


def _txt_file(
    content="Troubleshooting guide for nginx 502 errors.", filename="guide.txt"
):
    """Create a dict for httpx file upload."""
    return {"file": (filename, BytesIO(content.encode()), "text/plain")}


# ===========================================================================
# 1. Full conversion flow: upload → drafts → verify → ingested
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestFullConversionFlow:
    async def test_upload_creates_conversion_with_drafts(
        self, app_with_user, mock_conversion_service
    ):
        """Upload a file and receive a conversion response with drafts."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "personal"},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["conversion_id"] == "conv_abc123"
        assert body["status"] == "completed"
        assert len(body["drafts"]) == 1
        assert body["drafts"][0]["draft_id"] == "draft_abc123"
        mock_conversion_service.convert_document.assert_awaited_once()

    async def test_verify_draft_triggers_ingestion(
        self, app_with_user, mock_conversion_service
    ):
        """Verifying a passing draft returns ingestion metadata."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_abc123/verify",
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "verified"
        assert body["ingested"] is True
        assert body["knowledge_item_id"] == "ki-001"
        mock_conversion_service.verify_draft.assert_awaited_once()


# ===========================================================================
# 2. File type validation: unsupported MIME type returns 415
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestFileTypeValidation:
    async def test_unsupported_mime_type_returns_415(
        self, app_with_user, mock_conversion_service
    ):
        """Uploading an unsupported file type returns 415."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files={
                    "file": ("data.zip", BytesIO(b"PK\x03\x04fake"), "application/zip")
                },
                data={"scope": "personal"},
            )

        assert response.status_code == 415
        assert "Unsupported file type" in response.json()["detail"]
        mock_conversion_service.convert_document.assert_not_awaited()

    async def test_allowed_extension_overrides_mime(
        self, app_with_user, mock_conversion_service
    ):
        """A file with allowed extension but generic MIME type is accepted."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files={
                    "file": (
                        "guide.md",
                        BytesIO(b"# Troubleshooting"),
                        "application/octet-stream",
                    )
                },
                data={"scope": "personal"},
            )

        assert response.status_code == 201
        mock_conversion_service.convert_document.assert_awaited_once()


# ===========================================================================
# 3. Missing scope returns 400
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestMissingScopeValidation:
    async def test_invalid_scope_returns_400(
        self, app_with_user, mock_conversion_service
    ):
        """An invalid scope value returns 400."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "enterprise"},
            )

        assert response.status_code == 400
        assert "scope must be" in response.json()["detail"]
        mock_conversion_service.convert_document.assert_not_awaited()


# ===========================================================================
# 4. Scope=team without team_id returns 400
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestTeamScopeWithoutTeamId:
    async def test_team_scope_without_team_id_returns_400(
        self, app_with_user, mock_conversion_service
    ):
        """Using scope=team without providing team_id returns 400."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "team"},
            )

        assert response.status_code == 400
        assert "team_id is required" in response.json()["detail"]
        mock_conversion_service.convert_document.assert_not_awaited()

    async def test_team_scope_with_team_id_succeeds(
        self, app_with_user, mock_conversion_service
    ):
        """Using scope=team with a team_id succeeds."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "team", "team_id": "team-42"},
            )

        assert response.status_code == 201
        mock_conversion_service.convert_document.assert_awaited_once()


# ===========================================================================
# 5. Non-admin user attempting global scope returns 403
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestGlobalScopeAuthorization:
    async def test_non_admin_global_scope_returns_403(
        self, app_with_user, mock_conversion_service
    ):
        """A non-admin user requesting global scope is forbidden."""
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "global"},
            )

        assert response.status_code == 403
        assert "admin" in response.json()["detail"].lower()
        mock_conversion_service.convert_document.assert_not_awaited()

    async def test_admin_global_scope_succeeds(
        self, app_with_admin, mock_conversion_service
    ):
        """An admin user can use global scope."""
        async with await _client(app_with_admin) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "global"},
            )

        assert response.status_code == 201
        mock_conversion_service.convert_document.assert_awaited_once()


# ===========================================================================
# 6. Upload a text file with troubleshooting content → successful conversion
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestTextFileUpload:
    async def test_text_file_conversion_passes_correct_args(
        self, app_with_user, mock_conversion_service
    ):
        """Verify the service receives the correct parameters from the upload."""
        content = (
            "# Nginx 502 Bad Gateway Troubleshooting\n"
            "## Symptoms\n"
            "Users report intermittent 502 errors.\n"
            "## Diagnostic Steps\n"
            "1. Check upstream service health\n"
            "2. Review nginx error.log\n"
            "## Resolution\n"
            "Restart the upstream service and increase keepalive connections.\n"
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(content=content, filename="nginx-guide.txt"),
                data={"scope": "personal"},
            )

        assert response.status_code == 201

        call_kwargs = mock_conversion_service.convert_document.call_args.kwargs
        assert call_kwargs["original_filename"] == "nginx-guide.txt"
        assert call_kwargs["content_type"] == "text/plain"
        assert call_kwargs["scope"] == "personal"
        assert call_kwargs["user_id"] == "user-001"

    async def test_conversion_rejected_returns_422(
        self, app_with_user, mock_conversion_service
    ):
        """When the service rejects a document, the API returns 422."""
        mock_conversion_service.convert_document.side_effect = ConversionRejectedError(
            "Source document does not contain actionable failure modes.",
            error_code=ConversionErrorCode.NO_FAILURE_MODES,
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(content="Just a random essay about cats."),
                data={"scope": "personal"},
            )

        assert response.status_code == 422
        assert "does not contain" in response.json()["detail"]

    async def test_conversion_token_limit_returns_413(
        self, app_with_user, mock_conversion_service
    ):
        """When the document exceeds the token limit, the API returns 413."""
        mock_conversion_service.convert_document.side_effect = ConversionRejectedError(
            "Document has 150000 tokens which exceeds the limit of 100000",
            error_code=ConversionErrorCode.DOCUMENT_TOO_LONG,
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "personal"},
            )

        assert response.status_code == 413
        assert "tokens" in response.json()["detail"]
        assert "limit" in response.json()["detail"]


# ===========================================================================
# 7. GET /conversions lists user's jobs
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestListConversions:
    async def test_list_conversions_returns_jobs(
        self, app_with_user, mock_conversion_service
    ):
        """GET /conversions returns a list of the user's conversion jobs."""
        async with await _client(app_with_user) as client:
            response = await client.get(f"{API_PREFIX}/conversions")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["conversion_id"] == "conv_abc123"
        mock_conversion_service.list_conversions.assert_awaited_once()

    async def test_list_conversions_passes_pagination(
        self, app_with_user, mock_conversion_service
    ):
        """Pagination params are forwarded to the service."""
        async with await _client(app_with_user) as client:
            response = await client.get(
                f"{API_PREFIX}/conversions",
                params={"limit": 5, "offset": 10},
            )

        assert response.status_code == 200
        call_kwargs = mock_conversion_service.list_conversions.call_args.kwargs
        assert call_kwargs["limit"] == 5
        assert call_kwargs["offset"] == 10

    async def test_list_conversions_empty(self, app_with_user, mock_conversion_service):
        """An empty list is returned when the user has no conversions."""
        mock_conversion_service.list_conversions.return_value = []
        async with await _client(app_with_user) as client:
            response = await client.get(f"{API_PREFIX}/conversions")

        assert response.status_code == 200
        assert response.json() == []


# ===========================================================================
# 8. GET /conversions/{id} returns job with drafts
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestGetConversion:
    async def test_get_conversion_returns_details_with_drafts(
        self, app_with_user, mock_conversion_service
    ):
        """GET /conversions/{id} returns the full conversion response."""
        async with await _client(app_with_user) as client:
            response = await client.get(
                f"{API_PREFIX}/conversions/conv_abc123",
            )

        assert response.status_code == 200
        body = response.json()
        assert body["conversion_id"] == "conv_abc123"
        assert len(body["drafts"]) == 1
        assert body["analysis"]["is_actionable"] is True

    async def test_get_conversion_not_found_returns_404(
        self, app_with_user, mock_conversion_service
    ):
        """GET /conversions/{id} returns 404 when conversion doesn't exist."""
        mock_conversion_service.get_conversion.return_value = None
        async with await _client(app_with_user) as client:
            response = await client.get(
                f"{API_PREFIX}/conversions/conv_nonexistent",
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


# ===========================================================================
# 9. PUT .../drafts/{draft_id} updates content and re-validates
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestUpdateDraft:
    async def test_update_draft_returns_updated_draft(
        self, app_with_user, mock_conversion_service
    ):
        """PUT updates draft content and returns the re-validated draft."""
        updated_content = (
            "---\nid: nginx-502-bad-gateway\ntitle: Nginx 502 Bad Gateway\n---\n"
            "# Runbook: Nginx 502 Bad Gateway\n\n"
            "## Problem Definition\n502 errors in access logs after upstream restart.\n\n"
            "## Diagnostic Steps\n### Step 1: Check upstream\n```bash\ncurl -I http://upstream\n```\n"
        )
        async with await _client(app_with_user) as client:
            response = await client.put(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_abc123",
                json={"content": updated_content},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["draft_id"] == "draft_abc123"
        mock_conversion_service.update_draft.assert_awaited_once()
        call_kwargs = mock_conversion_service.update_draft.call_args.kwargs
        assert call_kwargs["content"] == updated_content

    async def test_update_draft_not_found_returns_404(
        self, app_with_user, mock_conversion_service
    ):
        """PUT returns 404 when the draft does not exist."""
        mock_conversion_service.update_draft.return_value = None
        async with await _client(app_with_user) as client:
            response = await client.put(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_missing",
                json={"content": "x" * 200},
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    async def test_update_draft_too_short_returns_422(
        self, app_with_user, mock_conversion_service
    ):
        """PUT returns 422 when content is shorter than min_length (100)."""
        async with await _client(app_with_user) as client:
            response = await client.put(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_abc123",
                json={"content": "too short"},
            )

        assert response.status_code == 422
        # Pydantic validation error for min_length
        mock_conversion_service.update_draft.assert_not_awaited()


# ===========================================================================
# 10. DELETE .../drafts/{draft_id} soft-deletes
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestDeleteDraft:
    async def test_delete_draft_returns_204(
        self, app_with_user, mock_conversion_service
    ):
        """DELETE soft-deletes the draft and returns 204 No Content."""
        async with await _client(app_with_user) as client:
            response = await client.delete(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_abc123",
            )

        assert response.status_code == 204
        assert response.content == b""
        mock_conversion_service.delete_draft.assert_awaited_once()
        call_kwargs = mock_conversion_service.delete_draft.call_args.kwargs
        assert call_kwargs["conversion_id"] == "conv_abc123"
        assert call_kwargs["draft_id"] == "draft_abc123"
        assert call_kwargs["user_id"] == "user-001"

    async def test_delete_draft_not_found_returns_404(
        self, app_with_user, mock_conversion_service
    ):
        """DELETE returns 404 when the draft does not exist."""
        mock_conversion_service.delete_draft.return_value = False
        async with await _client(app_with_user) as client:
            response = await client.delete(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_missing",
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


# ===========================================================================
# Edge cases / additional integration scenarios
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestConversionEdgeCases:
    async def test_verify_draft_missing_returns_404(
        self, app_with_user, mock_conversion_service
    ):
        """Missing draft maps to 404 via NotFoundError -> global handler.

        Refactored from the legacy ValueError -> 400 contract under Item 3
        (PR #334). The service now raises NotFoundError with structured
        resource_type / resource_id; the global handler in
        api/exception_handlers.py translates to 404.
        """
        from faultmaven.exceptions import NotFoundError

        mock_conversion_service.verify_draft.side_effect = NotFoundError(
            resource_type="draft",
            resource_id="draft_fail",
            message="Draft not found",
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_fail/verify",
            )

        assert response.status_code == 404
        body = response.json()
        assert "not found" in body["detail"].lower()
        # Structured metadata reaches clients so they can branch on
        # which resource was missing (conversion_job vs draft) without
        # parsing the detail string.
        assert body["resource_type"] == "draft"
        assert body["resource_id"] == "draft_fail"

    async def test_verify_draft_already_verified_returns_409(
        self, app_with_user, mock_conversion_service
    ):
        """Trying to verify an already-verified draft maps to 409 via
        the global conflict_exception_handler. Structured metadata
        (``resource_type``, ``resource_id``, ``conflict_reason``) is
        surfaced in the response body so clients can branch on the
        conflict shape programmatically — see exception_handlers.py and
        docs/architecture/specifications/exception-contract.md.
        """
        from faultmaven.exceptions import ConflictError

        mock_conversion_service.verify_draft.side_effect = ConflictError(
            "This runbook has already been verified and ingested",
            resource_type="draft",
            resource_id="draft_dup",
            conflict_reason="already_verified",
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_dup/verify",
            )

        assert response.status_code == 409
        body = response.json()
        assert "already been verified" in body["detail"].lower()
        # Structured metadata reaches clients — they can branch on
        # `conflict_reason` rather than parsing the detail string.
        assert body["resource_type"] == "draft"
        assert body["resource_id"] == "draft_dup"
        assert body["conflict_reason"] == "already_verified"

    async def test_verify_draft_validation_failed_returns_422(
        self, app_with_user, mock_conversion_service
    ):
        """A draft whose runbook-validator failed maps to 422 (the schema
        is invalid; the client should fix the draft before re-verifying)."""
        from faultmaven.exceptions import ValidationException

        mock_conversion_service.verify_draft.side_effect = ValidationException(
            "Draft has validation errors that must be fixed before verification"
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/conversions/conv_abc123/drafts/draft_invalid/verify",
            )

        assert response.status_code == 422
        assert "validation" in response.json()["detail"].lower()

    async def test_service_internal_error_returns_500(
        self, app_with_user, mock_conversion_service
    ):
        """Unhandled service errors return 500."""
        mock_conversion_service.convert_document.side_effect = RuntimeError(
            "Unexpected failure"
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "personal"},
            )

        assert response.status_code == 500
        assert "failed" in response.json()["detail"].lower()

    async def test_value_error_from_service_returns_422(
        self, app_with_user, mock_conversion_service
    ):
        """ValueError from the service layer returns 422."""
        mock_conversion_service.convert_document.side_effect = ValueError(
            "LLM analysis response could not be parsed"
        )
        async with await _client(app_with_user) as client:
            response = await client.post(
                f"{API_PREFIX}/convert",
                files=_txt_file(),
                data={"scope": "personal"},
            )

        assert response.status_code == 422
        assert "parsed" in response.json()["detail"].lower()

    async def test_conversion_service_unavailable_returns_503(self):
        """When conversion_service is not on app.state, returns 503."""
        app = FastAPI()
        app.include_router(conversion_router, prefix="/api/v1")
        # No conversion_service set on app.state
        user = _make_user()
        app.dependency_overrides[_require_auth] = lambda: user
        # Do NOT override _get_conversion_service — let it hit the real one

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"{API_PREFIX}/conversions")

        assert response.status_code == 503
        assert "not available" in response.json()["detail"].lower()

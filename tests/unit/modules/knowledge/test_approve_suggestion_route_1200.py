"""Route-level pins for #1200 — the status codes the fix actually changes.

The service-level set (`test_approve_suggestion_1200.py`) proves the call binds
and that failures propagate. These prove what a CLIENT sees, which is the half
the issue was filed about: a `TypeError` was surfacing as
``400 "Cannot approve: PII scan not complete"`` — a false statement about a
suggestion whose scan had passed.

Without these, a future change that reinstates `except Exception: return None`
in the service, or that lets the route's `except FaultMavenException: raise`
map a service failure to 409/422, passes the whole service-level suite.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_platform_admin
from faultmaven.exceptions import ConflictError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api.routes import (
    get_suggestion_service,
)
from faultmaven.modules.knowledge.api.routes import (
    router as knowledge_router,
)

pytestmark = pytest.mark.unit

SUGGESTION_ID = "sug_0001"


def _admin_user() -> DevUser:
    return DevUser(
        user_id="user-admin",
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        created_at=datetime.now(timezone.utc),
        roles=["admin", "platform_admin"],
    )


def _client(service):
    app = FastAPI()
    app.include_router(knowledge_router)
    # The REAL global handlers. Without them a `ConflictError` reaching the
    # route's `except FaultMavenException: raise` becomes a raw 500 rather than
    # the documented 409 — measured, and the reason the 409 pin below is worth
    # having: the mapping lives in the handler registry, not in the route.
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_suggestion_service] = lambda: service
    app.dependency_overrides[require_platform_admin] = _admin_user
    return TestClient(app, raise_server_exceptions=False)


def _service():
    service = MagicMock()
    service.get_suggestion_visible = AsyncMock(return_value=object())
    return service


class TestTheMisleading400IsGone:
    def test_a_programming_error_is_a_500_not_a_pii_claim(self):
        """The #1200 shape: `upload_document` raising `TypeError`."""
        service = _service()
        service.approve_suggestion = AsyncMock(
            side_effect=TypeError("got an unexpected keyword argument 'metadata'")
        )

        resp = _client(service).post(
            f"/knowledge/suggestions/{SUGGESTION_ID}/approve", json={}
        )

        assert resp.status_code == 500
        assert "PII" not in resp.text

    def test_an_ingestion_failure_is_a_500_not_a_pii_claim(self):
        service = _service()
        service.approve_suggestion = AsyncMock(
            side_effect=RuntimeError("chromadb unreachable")
        )

        resp = _client(service).post(
            f"/knowledge/suggestions/{SUGGESTION_ID}/approve", json={}
        )

        assert resp.status_code == 500
        assert "PII" not in resp.text


class TestTheCasesThat400And409AreStillAbout:
    def test_a_not_ready_suggestion_is_still_a_400_about_pii(self):
        """Unchanged, and the ONE thing that 400 is now reserved for."""
        service = _service()
        service.approve_suggestion = AsyncMock(return_value=None)

        resp = _client(service).post(
            f"/knowledge/suggestions/{SUGGESTION_ID}/approve", json={}
        )

        assert resp.status_code == 400
        assert "PII scan not complete" in resp.json()["detail"]

    def test_a_re_approval_is_a_409(self):
        service = _service()
        service.approve_suggestion = AsyncMock(
            side_effect=ConflictError(
                "Suggestion has already been approved",
                resource_type="suggestion",
                resource_id=SUGGESTION_ID,
                conflict_reason="already_approved",
            )
        )

        resp = _client(service).post(
            f"/knowledge/suggestions/{SUGGESTION_ID}/approve", json={}
        )

        assert resp.status_code == 409

    def test_an_absent_suggestion_is_still_a_404(self):
        service = _service()
        service.get_suggestion_visible = AsyncMock(return_value=None)
        service.approve_suggestion = AsyncMock()

        resp = _client(service).post(
            f"/knowledge/suggestions/{SUGGESTION_ID}/approve", json={}
        )

        assert resp.status_code == 404
        service.approve_suggestion.assert_not_awaited()


class TestASuccessfulApproval:
    def test_returns_the_knowledge_item_id(self):
        service = _service()
        service.approve_suggestion = AsyncMock(
            return_value={
                "suggestion_id": SUGGESTION_ID,
                "knowledge_item_id": "kb_abcdef0123456789",
                "status": "approved",
            }
        )

        resp = _client(service).post(
            f"/knowledge/suggestions/{SUGGESTION_ID}/approve", json={}
        )

        # 201, not 200 — the route declares a created resource.
        assert resp.status_code == 201
        assert resp.json()["knowledge_item_id"] == "kb_abcdef0123456789"

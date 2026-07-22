"""Tests for the global-tier (platform corpus) authoring gate under multi (#770).

Global scope is the org-free platform tier served to every tenant, so under
``TENANT_PROVIDER=multi`` NO tenant session — org admins included — may author
it; content ships via the audited kb_seed maintenance job instead. These tests
cover the gate helper and its wiring into every route that can set
``scope='global'``: document conversion, manual runbook creation, and the
always-global runbook upload.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.role_dependencies import require_admin
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api.conversion_routes import (
    _get_conversion_service,
)
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)
from faultmaven.modules.knowledge.api.platform_tier import (
    GLOBAL_AUTHORING_FORBIDDEN_MSG,
    require_global_authoring_allowed,
)
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE

_MULTI = patch(
    "faultmaven.modules.knowledge.api.platform_tier.requested_tenant_provider",
    return_value=BUILTIN_MULTI,
)
_SINGLE = patch(
    "faultmaven.modules.knowledge.api.platform_tier.requested_tenant_provider",
    return_value=BUILTIN_SINGLE,
)


def _admin_user() -> DevUser:
    return DevUser(
        user_id="user-admin",
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        created_at=datetime.now(timezone.utc),
        roles=["admin"],
    )


def _member_user() -> DevUser:
    return DevUser(
        user_id="user-member",
        username="member",
        email="member@example.com",
        display_name="Member",
        created_at=datetime.now(timezone.utc),
        roles=["user"],
    )


class TestGateHelper:
    def test_single_tenant_allows(self):
        with _SINGLE:
            require_global_authoring_allowed()  # no raise

    def test_multi_tenant_refuses_403(self):
        with _MULTI:
            with pytest.raises(HTTPException) as exc:
                require_global_authoring_allowed()
        assert exc.value.status_code == 403
        assert exc.value.detail == GLOBAL_AUTHORING_FORBIDDEN_MSG


@pytest.fixture
def conversion_client():
    """Conversion router with a mocked service + admin auth."""
    app = FastAPI()
    app.include_router(conversion_router)
    service = MagicMock()
    service.convert_document = AsyncMock(
        return_value=MagicMock(model_dump=lambda: {"ok": True})
    )
    service.create_runbook_from_template = AsyncMock(
        return_value={
            "conversion_id": "conv_1",
            "draft": MagicMock(model_dump=lambda: {"draft_id": "draft_1"}),
        }
    )
    app.dependency_overrides[_get_conversion_service] = lambda: service
    app.dependency_overrides[require_authentication] = _admin_user
    return TestClient(app), service


@pytest.fixture
def upload_client():
    """Knowledge router with a mocked service + admin auth."""
    app = FastAPI()
    app.include_router(knowledge_router)
    service = MagicMock()
    service.upload_document = AsyncMock(return_value={"document_id": "kb_1"})
    app.dependency_overrides[get_knowledge_service] = lambda: service
    app.dependency_overrides[require_admin] = _admin_user
    return TestClient(app), service


_RUNBOOK_BODY = {
    "title": "Test runbook title",
    "domain": "networking",
    "service": "svc",
    "symptom_class": ["timeouts"],
    "severity": "high",
    "scope": "global",
    "symptom_recognition": "x" * 20,
    "applicability": "x" * 20,
    "diagnostic_steps": "x" * 20,
    "causes": "x" * 20,
    "prevention": "x" * 20,
}


class TestConversionRoutesUnderMulti:
    def test_convert_global_refused_for_admin_under_multi(self, conversion_client):
        client, service = conversion_client
        with _MULTI:
            resp = client.post(
                "/knowledge/convert",
                data={"scope": "global"},
                files={"file": ("doc.md", b"# doc", "text/markdown")},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == GLOBAL_AUTHORING_FORBIDDEN_MSG
        service.convert_document.assert_not_awaited()

    def test_convert_personal_still_allowed_under_multi(self, conversion_client):
        client, service = conversion_client
        with _MULTI:
            resp = client.post(
                "/knowledge/convert",
                data={"scope": "personal"},
                files={"file": ("doc.md", b"# doc", "text/markdown")},
            )
        assert resp.status_code == 201
        service.convert_document.assert_awaited()

    def test_convert_global_admin_allowed_single_tenant(self, conversion_client):
        client, service = conversion_client
        with _SINGLE:
            resp = client.post(
                "/knowledge/convert",
                data={"scope": "global"},
                files={"file": ("doc.md", b"# doc", "text/markdown")},
            )
        assert resp.status_code == 201
        service.convert_document.assert_awaited()

    def test_manual_create_global_refused_under_multi(self, conversion_client):
        client, service = conversion_client
        with _MULTI:
            resp = client.post("/knowledge/runbooks/create", json=_RUNBOOK_BODY)
        assert resp.status_code == 403
        assert resp.json()["detail"] == GLOBAL_AUTHORING_FORBIDDEN_MSG
        service.create_runbook_from_template.assert_not_awaited()

    def test_manual_create_global_admin_allowed_single_tenant(self, conversion_client):
        client, service = conversion_client
        with _SINGLE:
            resp = client.post("/knowledge/runbooks/create", json=_RUNBOOK_BODY)
        assert resp.status_code == 201
        service.create_runbook_from_template.assert_awaited()


class TestConversionRoutesSingleTenantAdminGate:
    """The pre-existing org-admin gate keeps applying in single-tenant."""

    def test_convert_global_non_admin_still_403_single_tenant(self):
        app = FastAPI()
        app.include_router(conversion_router)
        service = MagicMock()
        service.convert_document = AsyncMock()
        app.dependency_overrides[_get_conversion_service] = lambda: service
        app.dependency_overrides[require_authentication] = _member_user
        client = TestClient(app)
        with _SINGLE:
            resp = client.post(
                "/knowledge/convert",
                data={"scope": "global"},
                files={"file": ("doc.md", b"# doc", "text/markdown")},
            )
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"]
        service.convert_document.assert_not_awaited()


class TestUploadRouteUnderMulti:
    def test_upload_refused_under_multi(self, upload_client):
        client, service = upload_client
        with _MULTI:
            resp = client.post(
                "/knowledge/documents",
                data={"title": "T", "document_type": "runbook"},
                files={"file": ("doc.md", b"# doc", "text/markdown")},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == GLOBAL_AUTHORING_FORBIDDEN_MSG
        service.upload_document.assert_not_awaited()


class TestSuggestionApprovalUnderMulti:
    def test_approve_suggestion_refused_under_multi(self):
        """Suggestion approval publishes via upload_document's global default —
        it must carry the same platform-tier gate (#770 security review M2)."""
        from faultmaven.modules.knowledge.api.routes import get_suggestion_service

        app = FastAPI()
        app.include_router(knowledge_router)
        service = MagicMock()
        service.approve_suggestion = AsyncMock()
        app.dependency_overrides[get_suggestion_service] = lambda: service
        app.dependency_overrides[require_admin] = _admin_user
        client = TestClient(app)

        with _MULTI:
            resp = client.post("/knowledge/suggestions/s-1/approve", json={})

        assert resp.status_code == 403
        assert resp.json()["detail"] == GLOBAL_AUTHORING_FORBIDDEN_MSG
        service.approve_suggestion.assert_not_awaited()
